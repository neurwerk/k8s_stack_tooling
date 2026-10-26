"""Verified database provisioners must reach the declared engine, not another port."""

import copy
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
    _database_provider_resources,
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
    providers = [
        {
            "kind": "Service",
            "metadata": {"namespace": "infra-postgres-operations", "name": "postgres-operations"},
            "spec": {
                "selector": {"app": "database"},
                "ports": [
                    {"name": "postgresql", "port": 5432, "targetPort": "postgresql"},
                    {"name": "documentdb", "port": 10260, "targetPort": "documentdb"},
                ],
            },
        },
        {
            "kind": "StatefulSet",
            "metadata": {"namespace": "infra-postgres-operations"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "database"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "database",
                                "ports": [
                                    {"name": "postgresql", "containerPort": 9712},
                                    {"name": "documentdb", "containerPort": 10260},
                                ],
                                "env": [
                                    {"name": "POSTGRESQL_PORT", "value": "9712"},
                                    {"name": "DOCUMENTDB_PORT", "value": "10260"},
                                ],
                            }
                        ],
                    },
                }
            },
        },
    ]
    return selection, packages, results, providers


def test_shared_service_binds_both_actual_engine_listeners(bindings):
    _check_database_bindings(*bindings)


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "host",
        "port",
        "service-port",
        "target",
        "selector",
        "listener",
        "job",
        "missing-provider",
        "missing-job",
        "database",
        "role",
    ],
)
def test_effective_binding_overrides_cannot_bypass_claims(bindings, change):
    selection, packages, results, providers = bindings
    result = results["documents"]
    key = next(iter(result.effective_values))
    settings = result.effective_values[key]["addon"]
    if change == "source":
        packages["documents"]["app"][0]["spec"]["chart"]["spec"]["sourceRef"]["name"] = "unverified"
    elif change == "host":
        settings["host"] = "other.infra-postgres-operations.svc.cluster.local"
    elif change == "port":
        settings["documentdbPort"] = 5432
    elif change == "service-port":
        providers[0]["spec"]["ports"][1]["port"] = 5433
    elif change == "target":
        providers[0]["spec"]["ports"][1]["targetPort"] = "postgresql"
    elif change == "selector":
        providers[0]["spec"]["selector"] = {"app": "other"}
    elif change == "listener":
        providers[1]["spec"]["template"]["spec"]["containers"][0]["env"][1]["value"] = "9712"
    elif change == "job":
        result.rendered[key][0]["spec"]["template"]["spec"]["containers"][0]["env"][-1]["value"] = (
            "5432"
        )
    elif change == "missing-provider":
        providers.clear()
    elif change == "missing-job":
        result.rendered[key].clear()
    elif change == "database":
        settings["database"] = "undeclared"
    else:
        settings["user"] = "undeclared"
    with pytest.raises(ContractError):
        _check_database_bindings(selection, packages, results, providers)


@pytest.fixture
def provider_chart(tmp_path):
    root = tmp_path / "base"
    chart = root / "charts/postgres/operations"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: database\nversion: 1.0.0\n")
    (chart / "values.yaml").write_text("postgresOperations:\n  port: 5432\n")
    (chart / "templates/_helpers.tpl").write_text("")
    (chart / "templates/config-map.yaml").write_text("")
    (chart / "templates/stateful-set.yaml").write_text("")
    (chart / "templates/service.yaml").write_text("""apiVersion: v1
kind: Service
metadata:
  name: postgres-operations
spec:
  ports:
    - name: postgresql
      port: {{ .Values.postgresOperations.port }}
      targetPort: postgresql
""")
    # Evaluating this unrelated template would require private input. It must not run.
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
            "valuesFrom": [{"kind": "ConfigMap", "name": "settings"}],
        },
    }
    maps = {
        ("database", "settings"): {
            "kind": "ConfigMap",
            "metadata": {"namespace": "database", "name": "settings"},
            "data": {"values.yaml": "postgresOperations:\n  port: 15432\n"},
        }
    }
    return root, release, maps


def test_provider_render_uses_real_nonsecret_overrides_not_unrelated_credentials(provider_chart):
    root, release, maps = provider_chart
    result = _database_provider_resources(
        [release],
        base_source=BASE,
        sources={("GitRepository", "flux-system", "base"): root},
        configmaps=maps,
        kube_version="1.35.0",
        api_versions=(),
    )
    assert result[0]["spec"]["ports"][0]["port"] == 15432
    assert result[0]["metadata"]["namespace"] == "database"


def test_provider_secret_values_are_not_silently_ignored(provider_chart):
    root, release, maps = provider_chart
    release = copy.deepcopy(release)
    release["spec"]["valuesFrom"].append({"kind": "Secret", "name": "unknown", "optional": True})
    with pytest.raises(ContractError, match="Secret-dependent"):
        _database_provider_resources(
            [release],
            base_source=BASE,
            sources={("GitRepository", "flux-system", "base"): root},
            configmaps=maps,
            kube_version="1.35.0",
            api_versions=(),
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
    with pytest.raises(ContractError, match="actual selected Base Service"):
        validate_selection_images(
            snapshot,
            client=client,
            base=base,
            packages={"synthetic": {"app": [release]}},
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )
