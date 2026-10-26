"""Verified database provisioners must reach the declared engine, not another port."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from k8s_stack_tooling.addons import ContractError
from k8s_stack_tooling.addons.helm_images import HelmImageResult
from k8s_stack_tooling.addons.schema import SourceRef
from k8s_stack_tooling.addons.sources import (
    VerifiedSelection,
    _check_database_bindings,
    _database_provider_endpoints,
    validate_selection_images,
)

from . import test_addon_selection as selection_fixtures
from .test_addon_definitions import shared_endpoint_addon

BASE = SourceRef(kind="GitRepository", namespace="flux-system", name="base")
composition = selection_fixtures.composition


@pytest.fixture
def bindings():
    addons = [
        shared_endpoint_addon("sql", "sql_app"),
        shared_endpoint_addon("documents", "document_app", "documentdb"),
    ]
    selection = cast(
        VerifiedSelection,
        SimpleNamespace(
            addons=[SimpleNamespace(definition=addon) for addon in addons],
            base=SimpleNamespace(reference=BASE),
        ),
    )
    host = "postgres-operations.infra-postgres-operations.svc.cluster.local"
    packages = {}
    results = {}
    for addon in addons:
        claim = addon.spec.databases[0]
        metadata = {"namespace": "infra-postgres-operations", "name": "provision"}
        packages[addon.metadata.name] = {
            "app": [
                {
                    "kind": "HelmRelease",
                    "metadata": metadata,
                    "spec": {
                        "chart": {
                            "spec": {
                                "chart": claim.base_chart,
                                "sourceRef": BASE.model_dump(by_alias=True),
                            }
                        }
                    },
                }
            ]
        }
        settings: dict[str, Any] = {"postgresPort": 5432}
        env = [{"name": "PGHOST", "value": host}, {"name": "PGPORT", "value": "5432"}]
        if claim.engine == "postgresql":
            settings.update(
                postgresHost=host,
                role=claim.role,
                databases=[{"name": claim.database, "vector": False}],
            )
        else:
            settings.update(
                enabled=True,
                host=host,
                documentdbPort=10260,
                user=claim.role,
                database=claim.database,
            )
            env.extend(
                [
                    {"name": "DOCUMENTDB_HOST", "value": host},
                    {"name": "DOCUMENTDB_PORT", "value": "10260"},
                ]
            )
        key = ("app", metadata["namespace"], metadata["name"])
        results[addon.metadata.name] = HelmImageResult(
            {
                key: [
                    {
                        "kind": "Job",
                        "apiVersion": "batch/v1",
                        "metadata": metadata,
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "provision",
                                            "env": env,
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ]
            },
            {},
            {key: {"addon": settings}},
        )
    providers = {
        ("infra-postgres-operations", "postgres-operations"): {
            "postgresql": 5432,
            "documentdb": 10260,
        },
    }
    return selection, packages, results, providers


def test_shared_service_binds_both_verified_engine_endpoints(bindings):
    _check_database_bindings(*bindings)


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "host",
        "port",
        "job",
        "missing-provider",
        "missing-job",
        "database",
        "role",
    ],
)
@pytest.mark.parametrize("engine", ["sql", "documents"])
def test_effective_binding_overrides_cannot_bypass_claims(bindings, change, engine):
    selection, packages, results, providers = bindings
    result = results[engine]
    key = next(iter(result.effective_values))
    settings = result.effective_values[key]["addon"]
    if change == "source":
        packages[engine]["app"][0]["spec"]["chart"]["spec"]["sourceRef"]["name"] = "unverified"
    elif change == "host":
        settings["postgresHost" if engine == "sql" else "host"] = (
            "other.infra-postgres-operations.svc.cluster.local"
        )
    elif change == "port":
        settings["postgresPort" if engine == "sql" else "documentdbPort"] = 15432
    elif change == "job":
        result.rendered[key][0]["spec"]["template"]["spec"]["containers"][0]["env"][-1]["value"] = (
            "15432"
        )
    elif change == "missing-provider":
        providers.clear()
    elif change == "missing-job":
        result.rendered[key].clear()
    elif change == "database":
        if engine == "sql":
            settings["databases"][0]["name"] = "undeclared"
        else:
            settings["database"] = "undeclared"
    else:
        settings["role" if engine == "sql" else "user"] = "undeclared"
    with pytest.raises(ContractError):
        _check_database_bindings(selection, packages, results, providers)


@pytest.fixture
def provider_chart(tmp_path):
    root = tmp_path / "base"
    chart = root / "charts/postgres/operations"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: database\nversion: 1.0.0\n")
    defaults = {
        "service": {"postgresqlPort": 5432, "documentdbPort": 10260},
        "containerPorts": {"postgresql": 9712, "documentdb": 10260},
    }
    (chart / "values.yaml").write_text(yaml.safe_dump({"postgresOperations": defaults}))
    # Provider templates are trusted Base implementation details, not re-rendered here.
    (chart / "templates/secret.yaml").write_text(
        '{{ fail "unrelated credential template evaluated" }}'
    )
    release = {
        "kind": "HelmRelease",
        "metadata": {"namespace": "database", "name": "database"},
        "spec": {
            "chart": {
                "spec": {
                    "chart": "charts/postgres/operations",
                    "sourceRef": BASE.model_dump(by_alias=True),
                }
            },
            "values": {"postgresOperations": defaults},
            "valuesFrom": [
                {"kind": "Secret", "name": "postgres-operations-values"},
                {"kind": "ConfigMap", "name": "settings"},
                {"kind": "Secret", "name": "addon-postgres-values", "optional": True},
            ],
        },
    }
    return root, release


def test_provider_inline_pins_dominate_ordinary_secret_values_without_rendering(
    provider_chart, monkeypatch
):
    root, release = provider_chart

    def forbidden(*args, **kwargs):
        pytest.fail("provider proof must not invoke a renderer")

    monkeypatch.setattr("k8s_stack_tooling.addons.sources.subprocess.run", forbidden)
    release["spec"]["targetNamespace"] = "selected-database"
    release["spec"]["releaseName"] = "other-release-name"
    result = _database_provider_endpoints(
        [release],
        base_source=BASE,
        sources={("GitRepository", "flux-system", "base"): root},
    )
    assert result == {
        ("selected-database", "postgres-operations"): {"postgresql": 5432, "documentdb": 10260}
    }


@pytest.mark.parametrize(
    "section,field",
    [
        ("service", "postgresqlPort"),
        ("service", "documentdbPort"),
        ("containerPorts", "postgresql"),
        ("containerPorts", "documentdb"),
    ],
)
def test_each_endpoint_pin_is_required(provider_chart, section, field):
    root, release = provider_chart
    del release["spec"]["values"]["postgresOperations"][section][field]
    with pytest.raises(ContractError, match="four explicit inline"):
        _database_provider_endpoints(
            [release],
            base_source=BASE,
            sources={("GitRepository", "flux-system", "base"): root},
        )


@pytest.mark.parametrize(
    "change",
    [
        "targetPath",
        "pin",
        "listener-pin",
        "boolean",
        "valuesFiles",
        "postRenderers",
        "source",
        "defaults",
    ],
)
def test_provider_pins_cannot_be_defeated_by_overrides(provider_chart, change):
    root, release = provider_chart
    spec = release["spec"]
    pinned = spec["values"]["postgresOperations"]
    if change == "targetPath":
        spec["valuesFrom"][0]["targetPath"] = "postgresOperations.service.postgresqlPort"
    elif change == "pin":
        pinned["service"]["postgresqlPort"] = 10260
    elif change == "listener-pin":
        pinned["containerPorts"]["postgresql"] = 10260
    elif change == "boolean":
        pinned["service"]["postgresqlPort"] = True
    elif change == "valuesFiles":
        spec["chart"]["spec"]["valuesFiles"] = ["different.yaml"]
    elif change == "postRenderers":
        spec["postRenderers"] = [{"kustomize": {"patches": []}}]
    elif change == "source":
        spec["chart"]["spec"]["sourceRef"]["name"] = "other"
    else:
        (root / "charts/postgres/operations/values.yaml").write_text(
            yaml.safe_dump({"postgresOperations": {"service": {"postgresqlPort": 15432}}})
        )
    with pytest.raises(ContractError):
        _database_provider_endpoints(
            [release],
            base_source=BASE,
            sources={("GitRepository", "flux-system", "base"): root},
        )


def test_source_verified_composition_requires_actual_database_provider(
    composition, bindings, monkeypatch
):
    client, addon, base, _, selected, sources, _, verify = composition
    selected["baseSourceRef"]["name"] = "k8s-stack"
    selection_fixtures.write(client / "config/addons.yaml", selected)
    next(source for source in sources if source["metadata"]["name"] == "base")["metadata"][
        "name"
    ] = "k8s-stack"
    _, packages, results, _ = bindings
    definition = yaml.safe_load((addon / "release/manifest.yaml").read_text())
    definition["spec"]["prerequisites"]["baseCharts"] = ["charts/postgres/addon"]
    database = shared_endpoint_addon("synthetic", "sql_app").spec.databases[0]
    definition["spec"]["databases"] = [database.model_dump(by_alias=True)]
    selection_fixtures.write(addon / "release/manifest.yaml", definition)
    release = packages["sql"]["app"][0]
    release["spec"]["chart"]["spec"]["sourceRef"]["name"] = "k8s-stack"
    release["apiVersion"] = "helm.toolkit.fluxcd.io/v2"
    selection_fixtures.write(addon / "releases/app/object.yaml", release)
    selection_fixtures.write(
        base / "charts/postgres/addon/Chart.yaml",
        {
            "apiVersion": "v2",
            "name": "database",
            "version": "1.0.0",
        },
    )
    snapshot = verify(installable=False, base_candidate=base)
    monkeypatch.setattr(
        "k8s_stack_tooling.addons.helm_images.validate_helm_images",
        lambda *args, **kwargs: results["sql"],
    )
    with pytest.raises(ContractError, match="selected pinned Base Service"):
        validate_selection_images(
            snapshot,
            client=client,
            base=base,
            packages={"synthetic": {"app": [release]}},
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )
