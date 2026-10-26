"""Actual Helm outputs must respect effective Base/client ownership after Flux patches."""

import pytest

from k8s_stack_tooling.addons import ContractError
from k8s_stack_tooling.addons.sources import validate_selection_images
from tests import test_addon_selection as fixtures

composition = fixtures.composition
write = fixtures.write


def rendered_composition(composition, owner, external_kind, helm_kind, *, handoff=False):
    client, addon, base, manifest, selection, _, resources, verify = composition
    external = {
        "apiVersion": "v1",
        "kind": external_kind,
        "metadata": {"namespace": "app", "name": "shared-output"},
    }
    if external_kind == "ExternalSecret":
        external["apiVersion"] = "external-secrets.io/v1"
        external["metadata"]["name"] = "external-delivery"
        external["spec"] = {"target": {"name": "shared-output"}}
    generated = {
        "apiVersion": "v1",
        "kind": helm_kind,
        "metadata": {"namespace": "app", "name": "shared-output"},
    }
    if helm_kind == "ExternalSecret":
        generated["apiVersion"] = "external-secrets.io/v1"
        generated["metadata"]["name"] = "chart-delivery"
        generated["spec"] = {"target": {"name": "shared-output"}}
    elif helm_kind == "ConfigMap":
        generated["data"] = {"public-setting": "chart-owned"}
    write(
        addon / "charts/application/Chart.yaml",
        {"apiVersion": "v2", "name": "application", "version": "1.0.0"},
    )
    write(addon / "charts/application/values.yaml", {})
    write(addon / "charts/application/templates/output.yaml", generated)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "application"},
        "spec": {
            "chart": {
                "spec": {
                    "chart": "./charts/application",
                    "sourceRef": selection["addons"][0]["sourceRef"],
                }
            }
        },
    }
    write(addon / "releases/app/object.yaml", release)
    assert "helmOutputs" not in manifest["spec"]["packages"][0]
    external_root = base if owner == "base" else client
    write(external_root / "releases/external/object.yaml", external)
    write(external_root / "releases/external/kustomization.yaml", {"resources": ["object.yaml"]})
    stage_spec: dict[str, object] = {
        "path": "releases/external",
        "sourceRef": {"name": "base" if owner == "base" else "flux-system"},
    }
    stage = {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"namespace": "flux-system", "name": f"{owner}-external"},
        "spec": stage_spec,
    }
    if handoff:
        stage_spec["patches"] = [
            {
                "patch": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
                "  name: shared-output\n  namespace: app\n$patch: delete\n"
            }
        ]
    resources.append(stage)
    snapshot = verify(installable=False)

    def validate():
        return validate_selection_images(
            snapshot,
            client=client,
            base=base,
            configmaps={},
            kube_version="1.35.0",
            api_versions=(),
        )

    return validate, stage_spec


@pytest.mark.parametrize("owner", ["base", "client"])
@pytest.mark.parametrize(
    ("external_kind", "helm_kind"),
    [
        ("ConfigMap", "ConfigMap"),
        ("Secret", "Secret"),
        ("ExternalSecret", "Secret"),
        ("Secret", "ExternalSecret"),
    ],
)
def test_undeclared_actual_helm_outputs_cannot_claim_base_or_client_resources(
    composition, owner, external_kind, helm_kind
):
    validate, _ = rendered_composition(composition, owner, external_kind, helm_kind)
    with pytest.raises(
        ContractError, match="effective selected resources have overlapping ownership"
    ):
        validate()


@pytest.mark.parametrize("owner", ["base", "client"])
def test_effective_flux_delete_releases_claim_but_retained_object_still_collides(
    composition, owner
):
    validate, stage_spec = rendered_composition(
        composition, owner, "ConfigMap", "ConfigMap", handoff=True
    )
    assert validate()["synthetic"].rendered[("app", "app", "application")][0]["kind"] == "ConfigMap"
    # A patch which changes data is not an ownership handoff.
    stage_spec["patches"] = [
        {
            "patch": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            '  name: shared-output\n  namespace: app\ndata:\n  retained: "yes"\n'
        }
    ]
    with pytest.raises(
        ContractError, match="effective selected resources have overlapping ownership"
    ):
        validate()
