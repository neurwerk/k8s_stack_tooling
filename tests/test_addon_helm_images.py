"""Exercise actual Helm rendering and the existing effective consumer checker."""

import copy
from pathlib import Path

import pytest
import yaml

from k8s_stack_tooling.addons.helm_images import validate_helm_images
from k8s_stack_tooling.addons.schema import Addon, ContractError

IMAGE = "example.com/app:1.2.3@sha256:" + "ab" * 32
OTHER = "example.com/app:2.0.0@sha256:" + "cd" * 32
SOURCE = ("GitRepository", "flux-system", "base")


def chart(root: Path, template: str = "") -> None:
    path = root / "charts/app"
    (path / "templates").mkdir(parents=True)
    (path / "Chart.yaml").write_text("apiVersion: v2\nname: app\nversion: 1.0.0\n")
    (path / "values.yaml").write_text(yaml.safe_dump({"image": IMAGE}))
    (path / "templates/job.yaml").write_text(
        template
        or """
apiVersion: batch/v1
kind: Job
metadata:
  name: task
  namespace: {{ .Release.Namespace }}
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: app
          image: {{ .Values.image | quote }}
"""
    )


@pytest.fixture
def inputs(tmp_path):
    chart(tmp_path)
    addon = {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonRelease",
        "metadata": {"name": "sample"},
        "spec": {
            "version": "1.0.0",
            "status": "staged",
            "sourceName": "sample",
            "configurationKey": "sample",
            "prerequisites": {"base": "0.4.0", "tooling": "0.14.0", "stackSetup": "0.7.0"},
            "packages": [{"name": "app", "path": "releases/app"}],
            "artifacts": [{"id": "native", "image": IMAGE, "role": "native"}],
            "consumers": [
                {
                    "artifact": "native",
                    "package": "app",
                    "kind": "HelmRelease",
                    "namespace": "app",
                    "name": "app",
                    "container": "app",
                }
            ],
        },
    }
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "app"},
        "spec": {
            "chart": {
                "spec": {
                    "chart": "./charts/app",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "base",
                    },
                }
            }
        },
    }
    return addon, release, tmp_path


def verify(inputs, *, configmaps=None, sources=None):
    addon, release, root = inputs
    validate_helm_images(
        Addon.model_validate(addon),
        {"app": [release]},
        sources=sources if sources is not None else {SOURCE: root},
        configmaps=configmaps or {},
        kube_version="1.35.0",
        api_versions=(),
    )


def cm(name, values):
    return {
        "kind": "ConfigMap",
        "metadata": {"namespace": "app", "name": name},
        "data": {"values.yaml": yaml.safe_dump(values)},
    }


def test_effective_client_overrides_not_chart_defaults(inputs):
    _, release, _ = inputs
    release["spec"]["valuesFrom"] = [
        {"kind": "ConfigMap", "name": "defaults"},
        {"kind": "ConfigMap", "name": "client"},
    ]
    maps = {
        ("app", "defaults"): cm("defaults", {"image": IMAGE}),
        ("app", "client"): cm("client", {"image": OTHER}),
    }
    with pytest.raises(ContractError, match="matching declaration"):
        verify(inputs, configmaps=maps)
    release["spec"]["values"] = {"image": IMAGE}
    verify(inputs, configmaps=maps)
    assert maps[("app", "client")] == cm("client", {"image": OTHER})


@pytest.mark.parametrize("hook,upgrade", [(False, False), (True, False), (True, True)])
def test_undeclared_init_and_hook_images_rejected(inputs, hook, upgrade):
    addon, _, root = inputs
    file = root / "charts/app/templates/job.yaml"
    original = file.read_text()
    if hook:
        extra = original.replace(
            "name: task", "name: hook\n  annotations:\n    helm.sh/hook: post-upgrade"
        )
        extra = extra.replace("name: app", "name: worker").replace(
            ".Values.image", '"' + OTHER + '"'
        )
        if upgrade:
            extra = "{{ if .Release.IsUpgrade }}\n" + extra + "{{ end }}\n"
        file.write_text(original + "\n---\n" + extra)
        location = "containers"
    else:
        file.write_text(
            original + f"      initContainers:\n        - name: worker\n          image: {OTHER}\n"
        )
        location = "initContainers"
    with pytest.raises(ContractError, match="matching declaration"):
        verify(inputs)
    addon["spec"]["artifacts"].append({"id": "worker", "image": OTHER, "role": "product"})
    addon["spec"]["consumers"].append(
        {
            **addon["spec"]["consumers"][0],
            "artifact": "worker",
            "container": "worker",
            "location": location,
        }
    )
    verify(inputs)


def test_exact_namespaced_source_selects_base_contents(inputs, tmp_path):
    other = tmp_path / "different-base"
    chart(other)
    (other / "charts/app/values.yaml").write_text(yaml.safe_dump({"image": OTHER}))
    with pytest.raises(ContractError, match="matching declaration"):
        verify(inputs, sources={SOURCE: other})
    with pytest.raises(ContractError, match="exact verified local source"):
        verify(inputs, sources={("GitRepository", "other", "base"): inputs[2]})


@pytest.mark.parametrize("escape", ["../outside", "/outside", "charts/escape"])
def test_chart_path_confinement(inputs, tmp_path, escape):
    _, release, root = inputs
    if escape == "charts/escape":
        (root / escape).symlink_to(root.parent)
    release["spec"]["chart"]["spec"]["chart"] = escape
    with pytest.raises(ContractError, match="leaves its repository"):
        verify(inputs)


def test_nested_checkout_and_template_symlink_rejected(inputs):
    _, _, root = inputs
    (root / "charts/.git").write_text("gitdir: elsewhere")
    with pytest.raises(ContractError, match="nested checkout"):
        verify(inputs)
    (root / "charts/.git").unlink()
    (root / "charts/app/templates/external.yaml").symlink_to(root / "charts/app/values.yaml")
    with pytest.raises(ContractError, match="symlink"):
        verify(inputs)


@pytest.mark.parametrize("optional", [False, True])
def test_secret_input_rejected_even_if_inline_image_or_optional(inputs, optional):
    _, release, _ = inputs
    release["spec"].update(
        values={"image": IMAGE},
        valuesFrom=[
            {"kind": "Secret", "name": "runtime", "optional": optional},
        ],
    )
    with pytest.raises(ContractError, match="Secret-dependent"):
        verify(inputs)


def test_missing_optional_configmap_is_not_assumed_absent(inputs):
    inputs[1]["spec"]["valuesFrom"] = [{"kind": "ConfigMap", "name": "client", "optional": True}]
    with pytest.raises(ContractError, match="unavailable"):
        verify(inputs)


@pytest.mark.parametrize(
    "field,value", [("postRenderers", [{}]), ("kubeConfig", {"secretRef": {"name": "remote"}})]
)
def test_unmodeled_render_transforms_rejected(inputs, field, value):
    inputs[1]["spec"][field] = value
    with pytest.raises(ContractError, match="rendering transforms"):
        verify(inputs)


def test_helm_failure_output_is_redacted(inputs):
    marker = "fixture-private-value-never-output"
    (inputs[2] / "charts/app/templates/job.yaml").write_text('{{ fail "' + marker + '" }}')
    with pytest.raises(ContractError) as error:
        verify(inputs)
    assert marker not in str(error.value)
    assert marker not in repr(error.value)


def test_list_cannot_hide_undeclared_workloads(inputs):
    template = inputs[2] / "charts/app/templates/job.yaml"
    workload = template.read_text()
    template.write_text(
        "apiVersion: v1\nkind: List\nitems:\n  -\n"
        + "\n".join("    " + line for line in workload.strip().splitlines())
        + "\n"
    )
    with pytest.raises(ContractError, match="List objects"):
        verify(inputs)


def test_target_path_and_alternative_defaults_fail_explicitly(inputs):
    alternative = copy.deepcopy(inputs)
    alternative[1]["spec"]["chart"]["spec"]["valuesFiles"] = ["values-prod.yaml"]
    with pytest.raises(ContractError, match="alternative chart default"):
        verify(alternative)
    inputs[1]["spec"]["valuesFrom"] = [
        {"kind": "ConfigMap", "name": "client", "targetPath": "image"}
    ]
    with pytest.raises(ContractError, match="targetPath"):
        verify(inputs)
