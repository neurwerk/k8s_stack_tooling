"""Effective settings dependencies and active ESO/approved-reader consistency."""

import copy
import json
from typing import Any

import pytest
import yaml

from k8s_stack_tooling.addons import ContractError, expand_setup
from k8s_stack_tooling.addons.helm_images import validate_helm_images
from k8s_stack_tooling.addons.schema import Addon, AddonSelection
from k8s_stack_tooling.addons.validation import (
    check_secret_deliveries,
    check_verifier_reader_separation,
)

IMAGE = "example.test/app:1.0.0@sha256:" + "ab" * 32
SOURCE = ("GitRepository", "flux-system", "synthetic")


def definition() -> dict[str, Any]:
    return {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonRelease",
        "metadata": {"name": "synthetic"},
        "spec": {
            "version": "1.0.0",
            "status": "staged",
            "sourceName": "synthetic",
            "configurationKey": "synthetic",
            "prerequisites": {"base": "0.4.0", "tooling": "0.14.0", "stackSetup": "0.7.0"},
            "packages": [{"name": "app", "path": "releases/app"}],
        },
    }


@pytest.fixture
def render_graph(tmp_path):
    for name in ("settings", "application"):
        root = tmp_path / "charts" / name
        (root / "templates").mkdir(parents=True)
        (root / "Chart.yaml").write_text(f"apiVersion: v2\nname: {name}\nversion: 1.0.0\n")
        (root / "values.yaml").write_text("image: incorrect:latest\n")
    settings = tmp_path / "charts/settings/templates/settings.yaml"
    settings.write_text(f"""apiVersion: v1
kind: ConfigMap
metadata:
  name: generated-settings
data:
  values.yaml: |
    image: {IMAGE}
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: database-delivery
spec:
  target:
    name: database-password
---
apiVersion: v1
kind: Secret
metadata:
  name: native-state
stringData:
  token: synthetic-output-not-for-return
""")
    (tmp_path / "charts/application/templates/job.yaml").write_text("""apiVersion: batch/v1
kind: Job
metadata:
  name: consumer
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: app
          image: {{ .Values.image | quote }}
""")
    data = definition()
    data["spec"]["packages"][0]["helmOutputs"] = [
        {
            "kind": "ConfigMap",
            "namespace": "app",
            "name": "generated-settings",
            "helmRelease": "settings",
        },
        {
            "kind": "Secret",
            "namespace": "app",
            "name": "database-password",
            "helmRelease": "settings",
        },
    ]
    data["spec"]["artifacts"] = [{"id": "app", "image": IMAGE, "role": "native"}]
    data["spec"]["consumers"] = [
        {
            "artifact": "app",
            "kind": "HelmRelease",
            "package": "app",
            "namespace": "app",
            "name": "application",
            "container": "app",
        }
    ]

    def release(name) -> dict[str, Any]:
        return {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {"name": name, "namespace": "app"},
            "spec": {
                "chart": {
                    "spec": {
                        "chart": "charts/" + name,
                        "sourceRef": {
                            "kind": "GitRepository",
                            "namespace": "flux-system",
                            "name": "synthetic",
                        },
                    }
                }
            },
        }

    consumer, producer = release("application"), release("settings")
    consumer["spec"]["valuesFrom"] = [{"kind": "ConfigMap", "name": "generated-settings"}]
    packages = {"app": [consumer, producer]}

    def verify():
        return validate_helm_images(
            Addon.model_validate(data),
            packages,
            sources={SOURCE: tmp_path},
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )

    return verify, packages, settings, tmp_path


def test_settings_render_before_consumers_and_outputs_are_real_and_redacted(render_graph):
    verify, _, _, _ = render_graph
    result = verify()
    assert list(result.rendered) == [("app", "app", "settings"), ("app", "app", "application")]
    assert result.effective_values[("app", "app", "application")]["image"] == IMAGE
    assert (
        yaml.safe_load(result.configmaps[("app", "generated-settings")]["data"]["values.yaml"])[
            "image"
        ]
        == IMAGE
    )
    assert "synthetic-output-not-for-return" not in json.dumps(list(result.rendered.values()))
    assert any(
        obj["kind"] == "ExternalSecret" for obj in result.rendered[("app", "app", "settings")]
    )


def test_settings_cycles_fail_without_assuming_existing_values(render_graph):
    verify, packages, _, _ = render_graph
    packages["app"][1]["spec"]["valuesFrom"] = [{"kind": "ConfigMap", "name": "generated-settings"}]
    with pytest.raises(ContractError, match="cyclic"):
        verify()


def test_install_upgrade_settings_disagreement_fails(render_graph):
    verify, _, settings, _ = render_graph
    settings.write_text(
        settings.read_text().replace(
            f"image: {IMAGE}",
            "image: {{ if .Release.IsUpgrade }}different:latest{{ else }}" + IMAGE + "{{ end }}",
        )
    )
    with pytest.raises(ContractError, match="install/upgrade output"):
        verify()


def test_runtime_lookup_remains_an_explicit_blocker(render_graph):
    verify, _, settings, _ = render_graph
    settings.write_text('{{ lookup "v1" "Secret" "app" "previous" }}\n' + settings.read_text())
    with pytest.raises(ContractError, match="runtime state"):
        verify()


def deliveries(stt=True) -> tuple[Addon, dict[str, Any], list[dict[str, Any]]]:
    data = definition()
    data["spec"]["features"] = {"stt": False}
    data["spec"]["setup"] = {
        "records": [
            {
                "id": "runtime",
                "path": "addons/synthetic/runtime",
                "fields": {
                    "key": {"generate": {"encoding": "hex", "bytes": 32}},
                    "stt": {"when": "stt", "supplied": {"group": "speech", "key": "token"}},
                },
            },
            {
                "id": "verifier",
                "path": "addons/synthetic/verifier",
                "fields": {"hash": {"sha256": {"record": "runtime", "field": "key"}}},
            },
        ],
        "inputGroups": [{"id": "speech", "when": "stt", "fields": {"token": {}}}],
        "readers": [
            {
                "role": "runtime",
                "namespace": "app",
                "serviceAccount": "eso",
                "audience": "openbao",
                "records": ["runtime"],
            },
            {
                "role": "verifier",
                "namespace": "bridge",
                "serviceAccount": "eso",
                "audience": "openbao",
                "records": ["verifier"],
            },
        ],
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
    addon = Addon.model_validate(data)
    selection = AddonSelection.model_validate(
        {
            "id": "synthetic",
            "sourceRef": {"kind": "GitRepository", "namespace": "flux-system", "name": "synthetic"},
            "features": {"stt": stt},
        }
    )
    addon.spec.setup = expand_setup(addon, selection)
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"namespace": "app", "name": "eso"},
    }
    store = {
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
                            "mountPath": "kubernetes",
                            "role": "runtime",
                            "serviceAccountRef": {"name": "eso", "audiences": ["openbao"]},
                        }
                    },
                }
            }
        },
    }
    external = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"namespace": "app", "name": "runtime"},
        "spec": {
            "secretStoreRef": {"kind": "SecretStore", "name": "shared-store"},
            "target": {"name": "runtime-secret", "template": {"data": {"config": "{{ .raw }}"}}},
            "data": [
                {
                    "secretKey": "raw",
                    "remoteRef": {"key": "addons/synthetic/runtime", "property": "key"},
                },
                {
                    "secretKey": "stt",
                    "remoteRef": {"key": "addons/synthetic/runtime", "property": "stt"},
                },
            ],
        },
    }
    return addon, external, [account, store]


def test_active_fields_and_exact_base_store_binding_are_accepted():
    addon, external, base = deliveries()
    check_secret_deliveries(addon, [external, copy.deepcopy(external)], base)
    check_verifier_reader_separation([addon])


def test_own_runtime_store_and_account_are_valid_without_base_copies():
    addon, external, owned = deliveries()
    check_secret_deliveries(addon, [*owned, external, *copy.deepcopy(owned)], [])


def test_client_store_requires_explicit_owned_input_declarations():
    addon, external, client = deliveries()
    with pytest.raises(ContractError, match="owned or declared"):
        check_secret_deliveries(addon, [external], [], client_resources=client)
    data = addon.model_dump(by_alias=True, exclude_none=True)
    data["spec"]["inputs"] = [
        {"kind": "SecretStore", "namespace": "app", "name": "shared-store", "owner": "client"},
        {"kind": "ServiceAccount", "namespace": "app", "name": "eso", "owner": "client"},
    ]
    addon = Addon.model_validate(data)
    check_secret_deliveries(addon, [external], [], client_resources=client)
    with pytest.raises(ContractError, match="declared owner"):
        check_secret_deliveries(addon, [*client, external], [])


def test_store_identity_cannot_have_two_owners():
    addon, external, base = deliveries()
    with pytest.raises(ContractError, match="overlapping owners"):
        check_secret_deliveries(addon, [*base, external], base)


@pytest.mark.parametrize(
    "failure",
    [
        "disabled-field",
        "omitted-field",
        "foreign-record",
        "target",
        "store",
        "namespace",
        "account",
        "audience",
        "role",
        "missing-account",
        "missing-store",
        "missing-delivery",
        "undeclared-delivery",
        "dataFrom",
        "version",
        "item-store",
        "duplicate-key",
        "phase-drift",
    ],
)
def test_eso_and_active_setup_mismatches_fail_closed(failure):
    addon, external, base = deliveries(stt=failure != "disabled-field")
    objects = [external]
    spec = external["spec"]
    auth = base[1]["spec"]["provider"]["vault"]["auth"]["kubernetes"]
    if failure == "omitted-field":
        spec["data"].pop()
    elif failure == "foreign-record":
        spec["data"][0]["remoteRef"]["key"] = "addons/foreign/runtime"
    elif failure == "target":
        spec["target"]["name"] = "different"
    elif failure == "store":
        spec["secretStoreRef"]["kind"] = "ClusterSecretStore"
    elif failure == "namespace":
        auth["serviceAccountRef"]["namespace"] = "different"
    elif failure == "account":
        auth["serviceAccountRef"]["name"] = "different"
    elif failure == "audience":
        auth["serviceAccountRef"]["audiences"] = ["different"]
    elif failure == "role":
        auth["role"] = "verifier"
    elif failure == "missing-account":
        base.pop(0)
    elif failure == "missing-store":
        base.pop()
    elif failure == "missing-delivery":
        objects = []
    elif failure == "undeclared-delivery":
        external["metadata"]["name"] = "different"
    elif failure == "dataFrom":
        spec["dataFrom"] = [{"extract": {"key": "addons/synthetic/runtime"}}]
    elif failure == "version":
        spec["data"][0]["remoteRef"]["version"] = "1"
    elif failure == "item-store":
        spec["data"][0]["sourceRef"] = {"storeRef": {"name": "other"}}
    elif failure == "duplicate-key":
        spec["data"][1]["secretKey"] = "raw"
    elif failure == "phase-drift":
        second = copy.deepcopy(external)
        second["spec"]["data"].pop()
        objects.append(second)
    with pytest.raises(ContractError):
        check_secret_deliveries(addon, objects, base)


def test_composed_reader_must_not_combine_verifier_and_raw_records():
    addon, _, _ = deliveries()
    addon.spec.setup.readers[1].records.append("runtime")
    with pytest.raises(ContractError, match="verifier reader can access raw"):
        check_verifier_reader_separation([addon])
