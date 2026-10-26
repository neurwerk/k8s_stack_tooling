"""Client-selected generic service scopes are checked after real Helm projection."""

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from k8s_stack_tooling.addons.helm_images import validate_helm_images
from k8s_stack_tooling.addons.schema import Addon, ContractError


def test_service_settings_use_rendered_generic_fields_and_trusted_approval(tmp_path):
    chart = tmp_path / "charts/service"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: service\nversion: 1.0.0\n")
    image = "example.test/shared:1.0.0@sha256:" + "ab" * 32
    (chart / "values.yaml").write_text(yaml.safe_dump({"image": image}))
    (chart / "templates/job.yaml").write_text("""apiVersion: batch/v1
kind: Job
metadata:
  name: configure
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: configure
          image: {{ .Values.image | quote }}
          command: [upsert-scoped-service-client]
          env:
            - name: KC_CLIENT_ID
              value: {{ .Values.service.clientId | quote }}
            - name: KC_GATEWAY_ROLE_SCOPE
              value: {{ .Values.service.gatewayPermissions | toJson | quote }}
            - name: KC_CLIENT_AUDIENCES
              value: {{ .Values.service.audiences | toJson | quote }}
            - name: KC_CLIENT_APPROVAL
              value: {{ .Values.serviceApproval | toJson | quote }}
            - name: KC_CLIENT_SECRET
              valueFrom:
                secretKeyRef: {name: service-secret, key: secret}
""")
    data: dict[str, Any] = {
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
            "inputs": [
                {"kind": "ConfigMap", "namespace": "app", "name": name, "owner": "client"}
                for name in ("settings", "approval")
            ],
            "artifacts": [{"id": "shared", "role": "shared", "image": image}],
            "consumers": [
                {
                    "artifact": "shared",
                    "package": "app",
                    "kind": "HelmRelease",
                    "namespace": "app",
                    "name": "service",
                    "container": "configure",
                }
            ],
            "identities": [
                {
                    "id": "service",
                    "mode": "service",
                    "clientId": "synthetic-client",
                    "gatewayPermissions": [],
                    "secret": {"record": "runtime", "field": "secret"},
                    "serviceSettings": {
                        "namespace": "app",
                        "settings": "settings",
                        "approval": "approval",
                    },
                }
            ],
            "setup": {
                "records": [
                    {
                        "id": "runtime",
                        "path": "addons/synthetic/runtime",
                        "fields": {"secret": {"generate": {"encoding": "hex", "bytes": 32}}},
                    }
                ]
            },
        },
    }
    service = {
        "clientId": "synthetic-client",
        "realmRoles": [],
        "gatewayPermissions": ["llm:invoke", "model:selected:invoke"],
        "audiences": ["agentgateway"],
    }
    configmaps = {
        ("app", name): {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": "app", "name": name},
            "data": {"values.yaml": yaml.safe_dump(value)},
        }
        for name, value in (
            ("settings", {"service": service}),
            ("approval", {"serviceApproval": service}),
        )
    }
    overrides: dict[str, Any] = {}
    release: dict[str, Any] = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "service"},
        "spec": {
            "values": overrides,
            "chart": {
                "spec": {
                    "chart": "charts/service",
                    "sourceRef": {
                        "kind": "GitRepository",
                        "namespace": "flux-system",
                        "name": "synthetic",
                    },
                }
            },
            "valuesFrom": [
                {"kind": "ConfigMap", "name": name} for name in ("settings", "approval")
            ],
        },
    }

    def verify():
        return validate_helm_images(
            Addon.model_validate(data),
            {"app": [release]},
            sources={("GitRepository", "flux-system", "synthetic"): tmp_path},
            configmaps=configmaps,
            kube_version="1.35.0",
            api_versions=(),
        )

    result = verify()
    assert result.rendered[("app", "app", "service")]
    widened = copy.deepcopy(service)
    widened["gatewayPermissions"] = ["llm:invoke", "model:unapproved:invoke"]
    overrides["service"] = widened
    with pytest.raises(ContractError, match="differs from client approval"):
        verify()
    overrides["serviceApproval"] = widened
    with pytest.raises(ContractError, match="differs from client approval"):
        verify()
    data["spec"]["identities"][0]["gatewayPermissions"] = ["llm:invoke", "model:fabricated:invoke"]
    with pytest.raises(ValidationError, match="literal identity scopes"):
        Addon.model_validate(data)
