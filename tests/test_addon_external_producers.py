"""Generated Base ownership and operator input approvals cannot be inferred or fabricated."""

import pytest

from k8s_stack_tooling.addons import ContractError, ExternalInput, validate_external_input
from tests import test_addon_selection as fixtures

composition = fixtures.composition
write = fixtures.write


@pytest.mark.parametrize("failure", [None, "missing", "chart", "source", "producer", "collision"])
def test_base_generated_input_requires_selected_exact_helm_producer(composition, failure):
    client, addon, base, manifest, _, _, resources, verify = composition
    output = {
        "kind": "Secret",
        "namespace": "identity",
        "name": "identity-credentials",
        "helmRelease": "identity-server",
    }
    manifest["spec"]["inputs"] = [
        {"kind": "Secret", "namespace": "identity", "name": "identity-credentials", "owner": "base"}
    ]
    write(addon / "release/manifest.yaml", manifest)
    declaration = {
        "path": "releases/identity/object.yaml",
        "namespace": "identity",
        "name": "identity-server",
        "chart": "./charts/identity/server",
        "helmOutputs": [output],
    }
    base_manifest = {"spec": {"version": "1.0.0", "artifacts": {"helmReleases": [declaration]}}}
    write(base / "release/manifest.yaml", base_manifest)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "identity", "name": "identity-server"},
        "spec": {
            "chart": {
                "spec": {
                    "chart": "./charts/identity/server",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "base",
                    },
                }
            }
        },
    }
    if failure == "chart":
        release["spec"]["chart"]["spec"]["chart"] = "./charts/different"
    if failure == "source":
        release["spec"]["chart"]["spec"]["sourceRef"]["name"] = "other"
    if failure == "producer":
        output["helmRelease"] = "other"
        write(base / "release/manifest.yaml", base_manifest)
    write(base / "releases/identity/object.yaml", release)
    if failure != "missing":
        resources.append(
            {
                "kind": "Kustomization",
                "metadata": {"namespace": "flux-system", "name": "identity"},
                "spec": {"path": "releases/identity", "sourceRef": {"name": "base"}},
            }
        )
    if failure == "collision":
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"namespace": "identity", "name": "identity-credentials"},
            }
        )
    if failure is None:
        result = verify(installable=False)
        assert result.addons[0].definition.spec.inputs[0].owner == "base"
        assert not any(obj.get("kind") == "Secret" for obj in resources)
    else:
        with pytest.raises(ContractError, match=r"producer|generated|declared owner"):
            verify(installable=False)


@pytest.mark.parametrize(
    "failure", [None, "absent", "namespace", "name", "type", "keys", "producer"]
)
def test_operator_input_requires_client_identity_type_keys_and_no_producer(composition, failure):
    client, addon, _, manifest, selection, _, resources, verify = composition
    product = {
        "kind": "Secret",
        "namespace": "synthetic",
        "name": "tooling-pull",
        "owner": "client",
        "type": "kubernetes.io/dockerconfigjson",
        "keys": [".dockerconfigjson"],
    }
    manifest["spec"]["inputs"] = [product]
    external = {key: value for key, value in product.items() if key != "owner"}
    if failure in ("namespace", "name", "type"):
        external[failure] = "other"
    if failure == "keys":
        external["keys"] = ["wrong"]
    selection["externalInputs"] = [] if failure == "absent" else [external]
    write(client / "config/addons.yaml", selection)
    write(addon / "release/manifest.yaml", manifest)
    if failure == "producer":
        resources.append(
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ExternalSecret",
                "metadata": {"namespace": "synthetic", "name": "conflicting-producer"},
                "spec": {"target": {"name": "tooling-pull"}},
            }
        )
    if failure is None:
        result = verify(installable=False)
        # Offline verification records a prerequisite, not presence or a generated Secret.
        assert result.selection.external_inputs[0].keys == [".dockerconfigjson"]
        assert not any(obj.get("kind") == "Secret" for obj in resources)
    else:
        with pytest.raises(ContractError, match=r"external input|declared owner"):
            verify(installable=False)


@pytest.mark.parametrize("failure", [None, "missing", "namespace", "name", "type", "keys"])
def test_external_input_live_preflight_is_still_required(failure):
    required = ExternalInput(
        kind="Secret",
        namespace="synthetic",
        name="tooling-pull",
        type="kubernetes.io/dockerconfigjson",
        keys=[".dockerconfigjson"],
    )
    resource = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"namespace": "synthetic", "name": "tooling-pull"},
        "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": "synthetic-not-real-credentials"},
    }
    if failure in ("namespace", "name"):
        resource["metadata"][failure] = "other"
    elif failure == "type":
        resource["type"] = "Opaque"
    elif failure == "keys":
        resource["data"] = {}
    elif failure == "missing":
        resource = {}
    if failure is None:
        validate_external_input(required, resource)
    else:
        with pytest.raises(ContractError, match="identity, type, or required keys") as error:
            validate_external_input(required, resource)
        assert "synthetic-not-real-credentials" not in str(error.value)
