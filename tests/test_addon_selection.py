"""Source authority, bounded setup definitions, and rendering confinement."""

import copy
import subprocess
from typing import Any

import pytest
import yaml

from k8s_stack_tooling.addons import (
    ContractError,
    load_manifest,
    selection_projection,
    verify_selection,
)
from k8s_stack_tooling.addons.git import run_git
from k8s_stack_tooling.addons.schema import MAX_DOCUMENT_BYTES, load_selection, parse_yaml
from k8s_stack_tooling.addons.sources import projection_main, validate_selection_images
from k8s_stack_tooling.addons.validation import base_revision, confine


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def repository(root, name):
    git(root, "init", "-b", "main")
    git(root, "config", "remote.origin.url", f"https://example.test/{name}.git")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "synthetic fixture",
    )
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def composition(tmp_path, monkeypatch):
    client, addon, base = [tmp_path / name for name in ("client", "addon", "base")]
    manifest = {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonRelease",
        "metadata": {"name": "synthetic"},
        "spec": {
            "version": "1.0.0",
            "status": "ready",
            "sourceName": "synthetic",
            "configurationKey": "synthetic",
            "prerequisites": {"base": "1.0.0", "tooling": "0.15.2", "stackSetup": "0.7.0"},
            "packages": [{"name": "app", "path": "releases/app"}],
        },
    }
    write(addon / "release/manifest.yaml", manifest)
    write(
        addon / "releases/app/object.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "synthetic"}},
    )
    addon_revision = repository(addon, "synthetic")
    write(base / "release/manifest.yaml", {"spec": {"version": "1.0.0"}})
    base_revision = repository(base, "base")
    refs = {
        name: {"kind": "GitRepository", "namespace": "flux-system", "name": name}
        for name in ("synthetic", "base")
    }
    selection = {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonSelection",
        "baseSourceRef": refs["base"],
        "addons": [{"id": "synthetic", "sourceRef": refs["synthetic"]}],
    }
    write(client / "config/addons.yaml", selection)
    sources = [
        {
            "apiVersion": "source.toolkit.fluxcd.io/v1",
            "kind": "GitRepository",
            "metadata": {"namespace": "flux-system", "name": name},
            "spec": {"url": f"https://example.test/{name}.git", "ref": {"commit": revision}},
        }
        for name, revision in (("base", base_revision), ("synthetic", addon_revision))
    ]
    resources = [
        *sources,
        {
            "kind": "Kustomization",
            "metadata": {"name": "app", "namespace": "flux-system"},
            "spec": {"path": "releases/app", "sourceRef": {"name": "synthetic"}, "wait": True},
        },
    ]

    def render(path, **kwargs):
        if path == client / "clusters/test":
            for resource in resources:
                resource.setdefault(
                    "apiVersion",
                    {
                        "GitRepository": "source.toolkit.fluxcd.io/v1",
                        "Kustomization": "kustomize.toolkit.fluxcd.io/v1",
                    }.get(resource["kind"], "v1"),
                )
            return resources
        return [yaml.safe_load((path / "object.yaml").read_text())]

    monkeypatch.setattr("k8s_stack_tooling.addons.sources.render", render)
    monkeypatch.setattr("k8s_stack_tooling.addons.validation.render", render)

    def verify(**kwargs):
        return verify_selection(
            client, {"synthetic": addon}, cluster_dir="clusters/test", base=base, **kwargs
        )

    return client, addon, base, manifest, selection, sources, resources, verify


def test_minimal_complete_selection_and_staged_binding(composition):
    *_, verify = composition
    verified = verify()
    assert verified.installable and not verified.projection_verified
    assert verified.addons[0].definition.spec.setup.records == []
    projection = selection_projection(verified)
    assert verify(staged_projection=projection).projection_verified
    projection["data"]["selection.json"] += " "
    with pytest.raises(ContractError, match="staged selection"):
        verify(staged_projection=projection)


def test_projection_cli_uses_offline_discovery_and_emits_configmap(
    composition, monkeypatch, capsys, tmp_path
):
    client, addon, base, *_, verify = composition
    calls = []

    def discover(path, *, cluster_dir, cache):
        calls.append((path, cluster_dir, cache))
        return base, {"synthetic": addon}

    def forbidden_fetch(*args, **kwargs):
        pytest.fail("projection generation must not acquire sources")

    monkeypatch.setattr("k8s_stack_tooling.addons.acquisition.source_checkouts", discover)
    monkeypatch.setattr("k8s_stack_tooling.addons.acquisition.acquire_selection", forbidden_fetch)
    args = [
        "--client",
        str(client),
        "--cluster-dir",
        "clusters/test",
        "--cache",
        str(tmp_path / "cache"),
    ]
    assert projection_main(args) == 0
    output = capsys.readouterr()
    assert not output.err
    assert yaml.safe_load(output.out) == selection_projection(verify())
    assert calls == [(client, "clusters/test", tmp_path / "cache")]
    target = tmp_path / "projection.yaml"
    assert projection_main([*args, "--candidate", "--output", str(target)]) == 0
    assert not capsys.readouterr().out
    assert yaml.safe_load(target.read_text()) == selection_projection(verify(installable=False))


def test_projection_cli_failure_preserves_existing_output(
    composition, monkeypatch, capsys, tmp_path
):
    client, addon, base, *_ = composition
    monkeypatch.setattr(
        "k8s_stack_tooling.addons.acquisition.source_checkouts",
        lambda *a, **kw: (base, {"synthetic": addon}),
    )
    (addon / "unreviewed").write_text("candidate")
    target = tmp_path / "projection.yaml"
    target.write_text("existing projection")
    assert (
        projection_main(
            [
                "--client",
                str(client),
                "--cluster-dir",
                "clusters/test",
                "--cache",
                str(tmp_path),
                "--output",
                str(target),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert "Cannot generate selection projection" in output.err
    assert target.read_text() == "existing projection"


@pytest.mark.parametrize(
    "failure",
    [
        "omitted",
        "duplicate",
        "namespace",
        "url",
        "revision",
        "filtered",
        "dirty",
        "unselected-stage",
    ],
)
def test_source_selection_failures(composition, failure):
    client, addon, base, _, selection, sources, resources, verify = composition
    if failure == "omitted":
        with pytest.raises(ContractError, match="complete selection"):
            verify_selection(client, {}, cluster_dir="clusters/test", base=base)
        return
    if failure == "duplicate":
        selection["addons"].append(copy.deepcopy(selection["addons"][0]))
        write(client / "config/addons.yaml", selection)
    elif failure == "namespace":
        sources[1]["metadata"]["namespace"] = "other"
    elif failure == "url":
        sources[1]["spec"]["url"] = "https://example.test/substitute.git"
    elif failure == "revision":
        sources[1]["spec"]["ref"]["commit"] = "a" * 40
    elif failure == "filtered":
        sources[1]["spec"]["ignore"] = "release/"
    elif failure == "dirty":
        (addon / "unreviewed").write_text("candidate")
    elif failure == "unselected-stage":
        resources.append(
            {
                "kind": "Kustomization",
                "metadata": {"name": "extra", "namespace": "flux-system"},
                "spec": {"path": "releases/app", "sourceRef": {"name": "omitted"}},
            }
        )
    with pytest.raises(ContractError):
        verify()


def test_candidate_is_explicit_and_projection_cannot_authorize_installable(composition):
    _, addon, _, _, _, _, _, verify = composition
    (addon / "candidate").write_text("reviewed working change")
    candidate = verify(installable=False)
    assert not candidate.installable
    (addon / "candidate").unlink()
    with pytest.raises(ContractError, match="staged selection"):
        verify(staged_projection=selection_projection(candidate))


def test_exact_base_checked_without_chart_versions(composition):
    _, _, base, _, _, sources, _, verify = composition
    write(base / "release/manifest.yaml", {"spec": {"version": "2.0.0"}})
    revision = repository(base, "base")
    sources[0]["spec"]["ref"]["commit"] = revision
    write(base / "release/manifest.yaml", {"spec": {"version": "1.0.0"}})
    with pytest.raises(ContractError, match="reviewed prerequisite"):
        verify(installable=False)


def test_working_base_candidate_is_explicit_and_never_installable(composition):
    _, addon, base, manifest, _, _, _, verify = composition
    manifest["spec"]["prerequisites"]["base"] = "2.0.0"
    write(addon / "release/manifest.yaml", manifest)
    write(base / "release/manifest.yaml", {"spec": {"version": "2.0.0"}})
    with pytest.raises(ContractError, match="reviewed prerequisite"):
        verify(installable=False)
    candidate = verify(installable=False, base_candidate=base)
    assert candidate.working_base and not candidate.installable
    with pytest.raises(ContractError, match="candidate mode"):
        verify(base_candidate=base)


def test_selected_incompatible_base_cannot_be_replaced_with_compatible_chart(composition):
    _, addon, base, manifest, _, sources, _, verify = composition
    chart = "charts/postgres/addon"
    manifest["spec"]["prerequisites"].update(
        {"baseCharts": [chart], "baseChartVersions": {chart: "2.0.0"}}
    )
    write(addon / "release/manifest.yaml", manifest)
    write(base / chart / "Chart.yaml", {"version": "1.0.0"})
    incompatible = repository(base, "base")
    git(base, "tag", "v1.0.0", incompatible)
    write(base / chart / "Chart.yaml", {"version": "2.0.0"})
    compatible = repository(base, "base")
    git(base, "replace", incompatible, compatible)
    git(base, "checkout", "--detach", incompatible)
    sources[0]["spec"]["ref"] = {"tag": "v1.0.0"}
    assert base_revision(base, {"tag": "v1.0.0"}) == incompatible
    # Prove the fixture substitutes compatible content for an unhardened Git read.
    assert "2.0.0" in git(base, "show", f"{incompatible}:{chart}/Chart.yaml")
    with pytest.raises(ContractError, match="Base chart version"):
        verify(installable=False)


def test_source_and_base_reads_ignore_ambient_git_directory_and_config(
    composition, monkeypatch, tmp_path
):
    _, addon, base, _, _, sources, _, verify = composition
    expected = sources[0]["spec"]["ref"]["commit"]
    config = tmp_path / "injected-config"
    config.write_text('[remote "origin"]\nurl = https://untrusted.example/repository.git\n')
    monkeypatch.setenv("GIT_DIR", str(addon / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(addon))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://override.example/repository.git")
    assert run_git(base, "config", "--get", "remote.origin.url") == "https://example.test/base.git"
    assert base_revision(base, {"branch": "main"}) == expected
    assert verify().base.revision == expected


def test_foreign_namespace_flux_system_is_not_an_implicit_client_source(composition):
    *_, resources, verify = composition
    resources.append(
        {
            "kind": "Kustomization",
            "metadata": {"namespace": "foreign", "name": "omitted"},
            "spec": {
                "path": "not-client-content",
                "sourceRef": {"kind": "GitRepository", "name": "flux-system"},
            },
        }
    )
    with pytest.raises(ContractError, match="absent from complete selection"):
        verify()


def test_selected_foreign_flux_system_paths_are_not_loaded_from_client_checkout(composition):
    client, addon, _, manifest, selection, sources, resources, verify = composition
    source_ref = {"kind": "GitRepository", "name": "flux-system", "namespace": "foreign"}
    manifest["spec"]["sourceName"] = "flux-system"
    selection["addons"][0]["sourceRef"] = source_ref
    sources[1]["metadata"].update({"name": "flux-system", "namespace": "foreign"})
    resources[-1]["spec"]["sourceRef"] = source_ref
    write(addon / "release/manifest.yaml", manifest)
    write(client / "config/addons.yaml", selection)
    # The fixture renderer fails if releases/app is wrongly read from the client checkout.
    assert verify(installable=False).addons[0].definition.metadata.name == "synthetic"


@pytest.mark.parametrize("owner", ["base", "client"])
def test_addon_cannot_overlap_selected_base_or_client_resource(composition, owner):
    _, _, base, _, _, _, resources, verify = composition
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "synthetic"}}
    if owner == "client":
        resources.append(namespace)
    else:
        write(base / "releases/shared/object.yaml", namespace)
        resources.append(
            {
                "kind": "Kustomization",
                "metadata": {"name": "base-shared", "namespace": "flux-system"},
                "spec": {"path": "releases/shared", "sourceRef": {"name": "base"}},
            }
        )
    with pytest.raises(ContractError, match="overlapping ownership"):
        verify(installable=False)


def test_base_input_requires_a_base_producer_not_matching_client_name(composition):
    _, addon, base, manifest, _, _, resources, verify = composition
    settings = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "defaults", "namespace": "shared"},
        "data": {"values.yaml": "enabled: true"},
    }
    manifest["spec"]["inputs"] = [
        {"kind": "ConfigMap", "namespace": "shared", "name": "defaults", "owner": "base"}
    ]
    write(addon / "release/manifest.yaml", manifest)
    resources.append(settings)
    with pytest.raises(ContractError, match="declared owner"):
        verify(installable=False)
    resources.remove(settings)
    write(base / "releases/shared/object.yaml", settings)
    resources.append(
        {
            "kind": "Kustomization",
            "metadata": {"name": "base-shared", "namespace": "flux-system"},
            "spec": {"path": "releases/shared", "sourceRef": {"name": "base"}},
        }
    )
    assert verify(installable=False).addons[0].definition.metadata.name == "synthetic"


def test_client_and_addon_generated_secret_targets_cannot_overlap(composition):
    _, addon, _, _, _, _, resources, verify = composition
    external = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"namespace": "synthetic", "name": "client-sync"},
        "spec": {"target": {"name": "shared-secret"}},
    }
    resources.append(external)
    duplicate = {**external, "metadata": {"namespace": "synthetic", "name": "addon-sync"}}
    write(addon / "releases/app/object.yaml", duplicate)
    with pytest.raises(ContractError, match="overlapping ownership"):
        verify(installable=False)


def test_full_candidate_composition_checks_helm_eso_against_selected_base_store(composition):
    client, addon, base, manifest, selection, _, resources, verify = composition
    reader = {
        "role": "runtime",
        "namespace": "app",
        "serviceAccount": "eso",
        "audience": "openbao",
        "records": ["runtime"],
    }
    manifest["spec"]["setup"] = {
        "records": [
            {
                "id": "runtime",
                "path": "addons/synthetic/runtime",
                "fields": {"key": {"generate": {"encoding": "hex", "bytes": 32}}},
            }
        ],
        "readers": [reader],
        "deliveries": [
            {
                "namespace": "app",
                "name": "runtime",
                "secretStore": "shared-store",
                "targetSecret": "runtime-secret",
                "records": ["runtime"],
            }
        ],
    }
    manifest["spec"]["packages"][0]["helmOutputs"] = [
        {"kind": "Secret", "namespace": "app", "name": "runtime-secret", "helmRelease": "secrets"}
    ]
    selection["addons"][0]["approvedReaders"] = [reader]
    write(client / "config/addons.yaml", selection)
    write(addon / "release/manifest.yaml", manifest)
    external = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"namespace": "app", "name": "runtime"},
        "spec": {
            "secretStoreRef": {"kind": "SecretStore", "name": "shared-store"},
            "target": {"name": "runtime-secret"},
            "data": [
                {
                    "secretKey": "key",
                    "remoteRef": {"key": "addons/synthetic/runtime", "property": "key"},
                }
            ],
        },
    }
    write(
        addon / "charts/secrets/Chart.yaml",
        {"apiVersion": "v2", "name": "secrets", "version": "1.0.0"},
    )
    write(addon / "charts/secrets/values.yaml", {})
    write(addon / "charts/secrets/templates/delivery.yaml", external)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "secrets"},
        "spec": {
            "chart": {
                "spec": {
                    "chart": "./charts/secrets",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "synthetic",
                    },
                }
            }
        },
    }
    write(addon / "releases/app/object.yaml", release)
    store: dict[str, Any] = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "SecretStore",
        "metadata": {"namespace": "app", "name": "shared-store"},
        "spec": {
            "provider": {
                "vault": {
                    "path": "secret",
                    "version": "v2",
                    "server": "https://bao.example.test",
                    "auth": {
                        "kubernetes": {
                            "role": "runtime",
                            "mountPath": "kubernetes",
                            "serviceAccountRef": {"name": "eso", "audiences": ["openbao"]},
                        }
                    },
                }
            }
        },
    }
    write(base / "releases/store/object.yaml", store)
    write(
        base / "releases/account/object.yaml",
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"namespace": "app", "name": "eso"},
        },
    )
    for name in ("store", "account"):
        resources.append(
            {
                "kind": "Kustomization",
                "metadata": {"name": "base-" + name, "namespace": "flux-system"},
                "spec": {"path": "releases/" + name, "sourceRef": {"name": "base"}},
            }
        )
    snapshot = verify(installable=False)
    result = validate_selection_images(
        snapshot,
        client=client,
        base=base,
        packages={"synthetic": {"app": [release]}},
        configmaps={},
        kube_version="1.35.0",
        api_versions=(),
    )
    assert result["synthetic"].rendered[("app", "app", "secrets")][0]["kind"] == "ExternalSecret"
    assert not snapshot.installable and not snapshot.projection_verified
    store["spec"]["provider"]["vault"]["auth"]["kubernetes"]["serviceAccountRef"]["audiences"] = [
        "unapproved"
    ]
    write(base / "releases/store/object.yaml", store)
    with pytest.raises(ContractError, match="approved reader"):
        validate_selection_images(
            snapshot,
            client=client,
            base=base,
            packages={"synthetic": {"app": [release]}},
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )


def test_none_helm_result_cannot_claim_composition_complete(composition, monkeypatch):
    client, addon, base, *_, verify = composition
    snapshot = verify(installable=False)
    monkeypatch.setattr(
        "k8s_stack_tooling.addons.helm_images.validate_helm_images", lambda *args, **kwargs: None
    )
    with pytest.raises(ContractError, match="did not return validated outputs"):
        validate_selection_images(
            snapshot,
            client=client,
            base=base,
            packages={
                "synthetic": {
                    "app": [yaml.safe_load((addon / "releases/app/object.yaml").read_text())]
                }
            },
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )


def setup_definition():
    return {
        "records": [
            {
                "id": "runtime",
                "path": "addons/synthetic/runtime",
                "fields": {
                    "key": {"generate": {"encoding": "base64url", "bytes": 32}},
                    "copy": {"ref": {"record": "runtime", "field": "key"}},
                },
            },
            {
                "id": "verifier",
                "path": "addons/synthetic/verifier",
                "fields": {"hash": {"sha256": {"record": "runtime", "field": "key"}}},
            },
            {
                "id": "speech",
                "path": "addons/synthetic/external/speech",
                "when": "speech",
                "fields": {"token": {"supplied": {"group": "speech", "key": "token"}}},
            },
        ],
        "inputGroups": [{"id": "speech", "when": "speech", "fields": {"token": {}}}],
        "readers": [
            {
                "role": "synthetic-runtime",
                "namespace": "synthetic",
                "serviceAccount": "runtime",
                "audience": "openbao",
                "records": ["runtime"],
            }
        ],
        "handover": [
            {
                "id": "admin",
                "label": "Synthetic administrator",
                "value": {"record": "runtime", "field": "key"},
            }
        ],
    }


def test_typed_setup_and_exact_reader_approval(composition):
    client, addon, _, manifest, selection, _, _, verify = composition
    manifest["spec"]["features"] = {"speech": False}
    manifest["spec"]["setup"] = setup_definition()
    write(addon / "release/manifest.yaml", manifest)
    with pytest.raises(ContractError, match="exact client approval"):
        verify(installable=False)
    selection["addons"][0]["approvedReaders"] = manifest["spec"]["setup"]["readers"]
    write(client / "config/addons.yaml", selection)
    selected = verify(installable=False).addons[0]
    assert selected.features == {"speech": False}
    assert len(selected.declared_definition.spec.setup.records) == 3
    assert len(selected.definition.spec.setup.records) == 2
    assert selected.definition.spec.setup.records[0].fields["key"].generate.bytes == 32
    assert selected.definition.spec.setup.records[1].fields["hash"].sha256.record == "runtime"


@pytest.mark.parametrize("conflict", [None, "namespace", "serviceAccount", "audience"])
def test_independently_approved_products_can_share_only_identical_reader_bindings(
    composition, tmp_path, conflict
):
    client, addon, base, manifest, selection, _, resources, _ = composition
    reader = {
        "role": "auth-keycloak",
        "namespace": "auth-keycloak",
        "serviceAccount": "auth-keycloak-external-secrets",
        "audience": "openbao",
        "records": ["runtime"],
    }
    manifest["spec"]["setup"] = {
        "records": [
            {
                "id": "runtime",
                "path": "addons/synthetic/runtime",
                "fields": {"secret": {"generate": {"encoding": "hex", "bytes": 32}}},
            }
        ],
        "readers": [reader],
    }
    selection["addons"][0]["approvedReaders"] = [reader]
    write(addon / "release/manifest.yaml", manifest)
    other = tmp_path / "other"
    second = copy.deepcopy(manifest)
    second["metadata"]["name"] = "other"
    second["spec"].update(sourceName="other", configurationKey="other")
    second["spec"]["setup"]["records"][0]["path"] = "addons/other/runtime"
    second_reader = second["spec"]["setup"]["readers"][0]
    if conflict:
        second_reader[conflict] = "different"
    write(other / "release/manifest.yaml", second)
    write(
        other / "releases/app/object.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "other"}},
    )
    revision = repository(other, "other")
    reference = {"kind": "GitRepository", "namespace": "flux-system", "name": "other"}
    selection["addons"].append(
        {"id": "other", "sourceRef": reference, "approvedReaders": [second_reader]}
    )
    write(client / "config/addons.yaml", selection)
    resources.extend(
        [
            {
                "kind": "GitRepository",
                "metadata": {"namespace": "flux-system", "name": "other"},
                "spec": {"url": "https://example.test/other.git", "ref": {"commit": revision}},
            },
            {
                "kind": "Kustomization",
                "metadata": {"namespace": "flux-system", "name": "other-app"},
                "spec": {"path": "releases/app", "sourceRef": reference, "wait": True},
            },
        ]
    )

    def verify_both():
        return verify_selection(
            client,
            {"synthetic": addon, "other": other},
            base=base,
            cluster_dir="clusters/test",
            installable=False,
        )

    if conflict:
        with pytest.raises(ContractError, match="shared reader role has conflicting bindings"):
            verify_both()
    else:
        verified = verify_both()
        assert not verified.installable and not verified.projection_verified
        assert {item.definition.spec.setup.records[0].path for item in verified.addons} == {
            "addons/synthetic/runtime",
            "addons/other/runtime",
        }
        assert len(verified.addons) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "two-operations",
        "cycle",
        "missing",
        "foreign",
        "conditional",
        "feature",
        "bad-bytes",
        "unknown",
        "old",
    ],
)
def test_setup_rejects_invalid_authority(composition, failure):
    _, addon, _, manifest, *_ = composition
    manifest["spec"]["features"] = {"speech": False}
    setup = manifest["spec"]["setup"] = setup_definition()
    field = setup["records"][0]["fields"]["key"]
    if failure == "two-operations":
        field["literal"] = "secret-marker-not-for-errors"
    elif failure == "cycle":
        setup["records"][0]["fields"]["key"] = {"ref": {"record": "runtime", "field": "copy"}}
    elif failure == "missing":
        setup["records"][1]["fields"]["hash"]["sha256"]["field"] = "absent"
    elif failure == "foreign":
        setup["records"][0]["path"] = "addons/other/runtime"
    elif failure == "conditional":
        setup["records"][0]["when"] = "speech"
    elif failure == "feature":
        manifest["spec"]["features"] = {}
    elif failure == "bad-bytes":
        field["generate"]["bytes"] = True
    elif failure == "unknown":
        field["script"] = "secret-marker-not-for-errors"
    elif failure == "old":
        manifest["apiVersion"] = "addons.neurwerk.com/v1alpha1"
    write(addon / "release/manifest.yaml", manifest)
    with pytest.raises(ContractError) as error:
        load_manifest(addon / "release/manifest.yaml")
    assert "secret-marker" not in str(error.value)


@pytest.mark.parametrize(
    "data",
    [
        b"key: one\nkey: secret-marker\n",
        b"x: &x [*x]",
        b"x" * (MAX_DOCUMENT_BYTES + 1),
        b"[" * 100 + b"]" * 100,
    ],
)
def test_bounded_unique_yaml(data):
    with pytest.raises(ContractError) as error:
        parse_yaml(data)
    assert "secret-marker" not in str(error.value)


def test_selection_rejects_coerced_feature(composition):
    client, _, _, _, selection, *_ = composition
    selection["addons"][0]["features"] = {"speech": "false"}
    write(client / "config/addons.yaml", selection)
    with pytest.raises(ContractError):
        load_selection(client / "config/addons.yaml")


def test_scoped_identity_requires_exact_approval_and_resolved_secret(composition):
    client, addon, _, manifest, selection, _, _, verify = composition
    manifest["spec"]["setup"] = {
        "records": [
            {
                "id": "service",
                "path": "addons/synthetic/oidc/service",
                "fields": {"secret": {"generate": {"encoding": "hex", "bytes": 32}}},
            }
        ]
    }
    identity = {
        "id": "gateway",
        "mode": "service",
        "clientId": "synthetic-service",
        "secret": {"record": "service", "field": "secret"},
        "gatewayPermissions": ["llm:invoke", "model:synthetic:invoke"],
        "audiences": ["agentgateway"],
    }
    manifest["spec"]["identities"] = [identity]
    write(addon / "release/manifest.yaml", manifest)
    with pytest.raises(ContractError, match="identity requests require exact"):
        verify(installable=False)
    selection["addons"][0]["approvedIdentities"] = [identity]
    write(client / "config/addons.yaml", selection)
    verified = verify(installable=False)
    assert verified.addons[0].definition.spec.identities[0].secret.record == "service"
    identity["secret"]["field"] = "missing"
    write(addon / "release/manifest.yaml", manifest)
    with pytest.raises(ContractError):
        verify(installable=False)


@pytest.mark.parametrize(
    "failure", ["remote", "nested-escape", "symlink", "generator", "replacement"]
)
def test_nested_rendering_confinement(tmp_path, failure):
    root = tmp_path / "repo"
    nested = root / "packages/nested"
    write(root / "packages/kustomization.yaml", {"resources": ["nested"]})
    outside = tmp_path / "outside.yaml"
    outside.write_text("kind: ConfigMap\n")
    reference = "https://example.test/remote"
    if failure == "nested-escape":
        reference = "../../../outside.yaml"
    elif failure == "symlink":
        nested.mkdir(parents=True)
        (nested / "escape.yaml").symlink_to(outside)
        reference = "escape.yaml"
    document = {"resources": [reference]}
    if failure == "generator":
        document = {"generators": ["plugin.yaml"]}
    elif failure == "replacement":
        document = {"replacements": [{"path": "../../../outside.yaml"}]}
    write(nested / "kustomization.yaml", document)
    with pytest.raises(ContractError):
        confine(root / "packages", root)
