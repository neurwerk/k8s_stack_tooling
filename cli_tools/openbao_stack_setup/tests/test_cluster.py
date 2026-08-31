from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException

from openbao_stack_setup.cluster import Cluster, ClusterError, KubernetesApiEndpoint


def cluster() -> Any:
    result = Cluster.__new__(Cluster)
    result.core = MagicMock()
    result.discovery = MagicMock()
    result.custom = MagicMock()
    result.context = "context"
    return result


def test_initializes_explicit_context() -> None:
    with (
        patch("openbao_stack_setup.cluster.kubernetes.config.load_kube_config") as load,
        patch("openbao_stack_setup.cluster.kubernetes.client.CoreV1Api") as core,
        patch("openbao_stack_setup.cluster.kubernetes.client.DiscoveryV1Api") as discovery,
        patch("openbao_stack_setup.cluster.kubernetes.client.CustomObjectsApi") as custom,
    ):
        result = Cluster("context")

    load.assert_called_once_with(context="context")
    assert result.core is core.return_value
    assert result.discovery is discovery.return_value
    assert result.custom is custom.return_value


def test_redacts_invalid_context() -> None:
    with (
        patch(
            "openbao_stack_setup.cluster.kubernetes.config.load_kube_config",
            side_effect=ConfigException("private path"),
        ),
        pytest.raises(ClusterError, match="selected Kubernetes context") as captured,
    ):
        Cluster("context")
    assert "private path" not in str(captured.value)


def test_reads_bound_identity() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"schemaVersion": "1", "client": "client", "clusterId": "cluster"}
    )
    target.core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="namespace")
    )

    identity = target.identity("client")

    assert (identity.client, identity.cluster_id, identity.namespace_uid) == (
        "client",
        "cluster",
        "namespace",
    )


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"schemaVersion": "2", "client": "client", "clusterId": "cluster"},
        {"schemaVersion": "1", "client": "other", "clusterId": "cluster"},
        {"schemaVersion": "1", "client": "client", "clusterId": ""},
    ],
)
def test_rejects_unbound_identity(data: dict[str, str]) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(data=data)
    target.core.read_namespace.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="namespace")
    )

    with pytest.raises(ClusterError, match="does not match"):
        target.identity("client")


def test_identity_errors_are_redacted() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = ApiException(status=403, reason="detail")

    with pytest.raises(ClusterError, match="HTTP 403") as captured:
        target.identity("client")
    assert "detail" not in str(captured.value)


def test_identity_requires_namespace_uid() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"schemaVersion": "1", "client": "client", "clusterId": "cluster"}
    )
    target.core.read_namespace.return_value = SimpleNamespace(metadata=SimpleNamespace(uid=None))

    with pytest.raises(ClusterError, match="does not have a UID"):
        target.identity("client")


def test_seal_existence_and_creation() -> None:
    target = cluster()
    target.core.read_namespaced_secret.side_effect = ApiException(status=404)
    assert target.seal_exists() is False

    target.core.read_namespaced_secret.side_effect = None
    assert target.seal_exists() is True
    with pytest.raises(ClusterError, match="already exists"):
        target.create_seal(b"x" * 32, "key-id")

    target.core.read_namespaced_secret.side_effect = ApiException(status=404)
    target.create_seal(b"x" * 32, "key-id")
    body = target.core.create_namespaced_secret.call_args.args[1]
    assert body.immutable is True
    assert body.data == {"key": base64.b64encode(b"x" * 32).decode("ascii")}


@pytest.mark.parametrize(
    ("keycloak_enabled", "monitoring_enabled", "expected"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_requires_smtp_for_keycloak_or_monitoring_email(
    keycloak_enabled: bool, monitoring_enabled: bool, expected: bool
) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = [
        SimpleNamespace(
            data={
                "values.yaml": (
                    f"authKeycloak:\n  smtp:\n    enabled: {str(keycloak_enabled).lower()}\n"
                )
            }
        ),
        SimpleNamespace(
            data={
                "values.yaml": (
                    "monitorKubePrometheusStack:\n  alerting:\n    email:\n"
                    f"      enabled: {str(monitoring_enabled).lower()}\n"
                )
            }
        ),
    ]

    assert target.smtp_required() is expected
    assert target.core.read_namespaced_config_map.call_args_list == [
        call("keycloak-product-values", "auth-keycloak"),
        call("kube-prometheus-stack-product-values", "monitor-kube-prometheus-stack"),
    ]


@pytest.mark.parametrize(
    "values",
    ["# client overrides\n", "{}\n", "monitorKubePrometheusStack:\n  alerting: {}\n"],
)
def test_monitoring_email_alerting_defaults_to_enabled(values: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = [
        SimpleNamespace(data={"values.yaml": "authKeycloak:\n  smtp:\n    enabled: false\n"}),
        SimpleNamespace(data={"values.yaml": values}),
    ]

    assert target.smtp_required() is True


@pytest.mark.parametrize(
    "values",
    ["", "authKeycloak: {}", 'authKeycloak:\n  smtp:\n    enabled: "yes"\n'],
)
def test_rejects_invalid_keycloak_smtp_choice(values: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": values}
    )
    with pytest.raises(ClusterError, match=r"SMTP|values\.yaml"):
        target.smtp_required()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ("authKeycloak:\n  activeDirectory:\n    enabled: true\n", True),
        ("authKeycloak:\n  activeDirectory:\n    enabled: false\n", False),
        ("authKeycloak: {}\n", False),
    ],
)
def test_active_directory_requirement(values: str, expected: bool) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": values}
    )

    assert target.active_directory_required() is expected


def test_rejects_non_boolean_active_directory_choice() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": 'authKeycloak:\n  activeDirectory:\n    enabled: "yes"\n'}
    )

    with pytest.raises(ClusterError, match="Active Directory enabled value"):
        target.active_directory_required()


@pytest.mark.parametrize(
    "values", ["[]\n", "authKeycloak: false\n", "authKeycloak:\n  activeDirectory: yes\n"]
)
def test_rejects_invalid_active_directory_contract(values: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": values}
    )

    with pytest.raises(ClusterError, match="Active Directory contract"):
        target.active_directory_required()


@pytest.mark.parametrize(
    "values",
    [
        "[]\n",
        "monitorKubePrometheusStack: false\n",
        'monitorKubePrometheusStack:\n  alerting:\n    email:\n      enabled: "no"\n',
        "monitorKubePrometheusStack: [\n",
    ],
)
def test_rejects_invalid_monitoring_email_alerting_choice(values: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = [
        SimpleNamespace(data={"values.yaml": "authKeycloak:\n  smtp:\n    enabled: false\n"}),
        SimpleNamespace(data={"values.yaml": values}),
    ]

    with pytest.raises(ClusterError, match=r"Monitoring|monitoring"):
        target.smtp_required()


def test_seal_api_failures_are_redacted() -> None:
    target = cluster()
    target.core.read_namespaced_secret.side_effect = ApiException(status=500)
    with pytest.raises(ClusterError, match=r"inspect.*HTTP 500"):
        target.seal_exists()

    target.core.read_namespaced_secret.side_effect = ApiException(status=404)
    target.core.create_namespaced_secret.side_effect = ApiException(status=409)
    with pytest.raises(ClusterError, match=r"create.*HTTP 409"):
        target.create_seal(b"x" * 32, "key-id")


def test_release_and_token_operations() -> None:
    target = cluster()
    target.require_openbao_release()
    target.core.create_namespaced_service_account_token.return_value = SimpleNamespace(
        status=SimpleNamespace(token="jwt")
    )

    assert target.token_request("operator", ttl_seconds=30) == "jwt"
    request = target.core.create_namespaced_service_account_token.call_args.args[2]
    assert request.spec.audiences == ["openbao"]
    assert request.spec.expiration_seconds == 30


def test_requires_ready_bootstrap_prerequisites() -> None:
    target = cluster()
    ready = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    target.custom.get_namespaced_custom_object.side_effect = [{}, ready, ready, ready, ready]
    target.core.read_namespaced_config_map.side_effect = [
        SimpleNamespace(data={"values.yaml": "authKeycloak:\n  smtp:\n    enabled: true\n"}),
        SimpleNamespace(
            data={
                "values.yaml": (
                    "monitorKubePrometheusStack:\n  alerting:\n    email:\n      enabled: false\n"
                )
            }
        ),
    ]

    assert target.require_bootstrap_prerequisites() is True
    assert target.custom.get_namespaced_custom_object.call_args_list == [
        call("helm.toolkit.fluxcd.io", "v2", "infra-openbao", "helmreleases", "openbao"),
        call(
            "helm.toolkit.fluxcd.io",
            "v2",
            "infra-external-secrets",
            "helmreleases",
            "external-secrets",
        ),
        call("helm.toolkit.fluxcd.io", "v2", "infra-rook-ceph", "helmreleases", "rook-ceph"),
        call(
            "helm.toolkit.fluxcd.io",
            "v2",
            "infra-trust-manager",
            "helmreleases",
            "trust-manager",
        ),
        call(
            "cert-manager.io",
            "v1",
            "infra-openbao",
            "certificates",
            "infra-openbao-server-certificate",
        ),
    ]


@pytest.mark.parametrize(
    ("ready_count", "message"),
    [
        (0, "infra-external-secrets/external-secrets"),
        (1, "infra-rook-ceph/rook-ceph"),
        (2, "infra-trust-manager/trust-manager"),
        (3, "infra-openbao/infra-openbao-server-certificate"),
    ],
)
def test_rejects_bootstrap_prerequisite_that_is_not_ready(ready_count: int, message: str) -> None:
    target = cluster()
    ready = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
    target.custom.get_namespaced_custom_object.side_effect = [
        {},
        *([ready] * ready_count),
        {},
    ]

    with pytest.raises(ClusterError, match=message):
        target.require_bootstrap_prerequisites()


def test_bootstrap_prerequisite_api_failures_are_redacted() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {},
        ApiException(status=403, reason="private detail"),
    ]

    with pytest.raises(ClusterError, match=r"external-secrets.*HTTP 403") as captured:
        target.require_bootstrap_prerequisites()
    assert "private detail" not in str(captured.value)


@pytest.mark.parametrize("token", [None, "", 123])
def test_rejects_invalid_service_account_tokens(token: object) -> None:
    target = cluster()
    target.core.create_namespaced_service_account_token.return_value = SimpleNamespace(
        status=SimpleNamespace(token=token)
    )

    with pytest.raises(ClusterError, match="did not return"):
        target.token_request("operator")


def test_release_and_token_api_failures() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    with pytest.raises(ClusterError, match=r"HelmRelease.*HTTP 404"):
        target.require_openbao_release()

    target.core.create_namespaced_service_account_token.side_effect = ApiException(status=403)
    with pytest.raises(ClusterError, match=r"operator token.*HTTP 403"):
        target.token_request("operator")


def test_validates_fixed_kubernetes_api_endpoint() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={
            "values.yaml": (
                "infraOpenbaoWrapper:\n"
                "  kubernetesApi:\n"
                "    endpoint:\n"
                "      address: 172.20.1.202\n"
                "      port: 6443\n"
            )
        }
    )
    target.core.list_node.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                status=SimpleNamespace(
                    conditions=[SimpleNamespace(type="Ready", status="True")],
                    addresses=[SimpleNamespace(type="InternalIP", address="172.20.1.202")],
                )
            )
        ]
    )
    target.discovery.list_namespaced_endpoint_slice.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                ports=[SimpleNamespace(port=6443)],
                endpoints=[
                    SimpleNamespace(
                        addresses=["172.20.1.202"],
                        conditions=SimpleNamespace(ready=True),
                    )
                ],
            )
        ]
    )

    assert target.validate_kubernetes_api_endpoint() == KubernetesApiEndpoint("172.20.1.202", 6443)
    target.discovery.list_namespaced_endpoint_slice.assert_called_once_with(
        "default", label_selector="kubernetes.io/service-name=kubernetes"
    )


@pytest.mark.parametrize(
    "values",
    [
        "",
        "infraOpenbaoWrapper: {}",
        (
            "infraOpenbaoWrapper:\n  kubernetesApi:\n    endpoint:\n"
            "      address: dynamic\n      port: 6443\n"
        ),
        (
            "infraOpenbaoWrapper:\n  kubernetesApi:\n    endpoint:\n"
            "      address: 172.20.1.202\n      port: true\n"
        ),
    ],
)
def test_rejects_invalid_kubernetes_api_endpoint(values: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": values}
    )
    with pytest.raises(ClusterError, match=r"values\.yaml|API endpoint|IPv4|port"):
        target.validate_kubernetes_api_endpoint()


def test_rejects_kubernetes_api_endpoint_drift() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={
            "values.yaml": (
                "infraOpenbaoWrapper:\n"
                "  kubernetesApi:\n"
                "    endpoint: {address: 172.20.1.202, port: 6443}\n"
            )
        }
    )
    target.core.list_node.return_value = SimpleNamespace(
        items=[
            SimpleNamespace(
                status=SimpleNamespace(
                    conditions=[SimpleNamespace(type="Ready", status="True")],
                    addresses=[SimpleNamespace(type="InternalIP", address="172.20.1.203")],
                )
            )
        ]
    )

    with pytest.raises(ClusterError, match="Ready Node InternalIP"):
        target.validate_kubernetes_api_endpoint()


def test_reconcile_and_wait_for_ready_resources() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {"status": {"conditions": []}},
        {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
    ]
    target.core.api_client.call_api.side_effect = [ApiException(status=404), object()]
    with (
        patch("openbao_stack_setup.cluster.time.time_ns", return_value=123),
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2, 3, 4, 5]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        token = target.force_reconcile("release", "namespace")
        target.wait_helm_release("release", "namespace", 10)
        target.wait_secret("secret", "namespace", 10)

    assert token == "123"
    annotation = target.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert annotation["metadata"]["annotations"]["reconcile.fluxcd.io/requestedAt"] == "123"
    metadata_call = target.core.api_client.call_api.call_args_list[-1]
    assert metadata_call.kwargs["header_params"] == {
        "Accept": "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
    }
    target.core.read_namespaced_secret.assert_not_called()


def test_wait_helm_release_ignores_ready_until_force_token_is_handled() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {
            "status": {
                "lastHandledForceAt": "old",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        },
        {
            "status": {
                "lastHandledForceAt": "new",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        },
    ]
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        target.wait_helm_release("release", "namespace", 10, force_token="new")

    assert target.custom.get_namespaced_custom_object.call_count == 2


def test_wait_openbao_endpoint_retries_until_https_is_ready() -> None:
    target = cluster()
    target.core.read_namespaced_pod.side_effect = [
        SimpleNamespace(
            status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="False")])
        ),
        SimpleNamespace(
            status=SimpleNamespace(conditions=[SimpleNamespace(type="Ready", status="True")])
        ),
    ]
    target.core.read_namespaced_endpoints.side_effect = [
        SimpleNamespace(
            subsets=[
                SimpleNamespace(
                    addresses=[SimpleNamespace(ip="10.42.0.1")],
                    ports=[SimpleNamespace(port=8200)],
                )
            ]
        ),
        SimpleNamespace(
            subsets=[
                SimpleNamespace(
                    addresses=[SimpleNamespace(ip="10.42.0.1")],
                    ports=[SimpleNamespace(port=8200)],
                )
            ]
        ),
    ]
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep") as sleep,
    ):
        target.wait_openbao_endpoint(timeout_seconds=10)

    assert target.core.read_namespaced_endpoints.call_count == 2
    assert target.core.read_namespaced_pod.call_count == 2
    sleep.assert_called_once_with(3)


def test_wait_openbao_endpoint_redacts_api_failure_and_times_out() -> None:
    target = cluster()
    target.core.read_namespaced_pod.side_effect = ApiException(status=403, reason="detail")
    with pytest.raises(ClusterError, match=r"Service endpoints.*HTTP 403") as captured:
        target.wait_openbao_endpoint()
    assert "detail" not in str(captured.value)

    target.core.read_namespaced_pod.side_effect = ApiException(status=404)
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
        pytest.raises(ClusterError, match=r"Timed out waiting.*endpoint"),
    ):
        target.wait_openbao_endpoint(timeout_seconds=1)


def test_wait_openbao_endpoint_rejects_failed_api_connectivity_init() -> None:
    target = cluster()
    terminated = SimpleNamespace(terminated=SimpleNamespace(exit_code=1), waiting=None)
    empty_state = SimpleNamespace(terminated=None, waiting=None)
    target.core.read_namespaced_pod.return_value = SimpleNamespace(
        status=SimpleNamespace(
            conditions=[],
            init_container_statuses=[
                SimpleNamespace(
                    name="kubernetes-api-connectivity",
                    state=empty_state,
                    last_state=terminated,
                )
            ],
        )
    )
    target.core.read_namespaced_endpoints.return_value = SimpleNamespace(subsets=[])

    with pytest.raises(ClusterError, match="cannot reach the Kubernetes API"):
        target.wait_openbao_endpoint()


def test_waits_fail_closed() -> None:
    target = cluster()
    failed = {
        "status": {
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "InstallFailed"},
                {"type": "Stalled", "status": "True", "reason": "RetriesExceeded"},
            ]
        }
    }
    target.custom.get_namespaced_custom_object.return_value = failed
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1]),
        pytest.raises(ClusterError, match="failed"),
    ):
        target.wait_helm_release("release", "namespace", 10)

    target.custom.get_namespaced_custom_object.return_value = {
        "status": {
            "lastHandledForceAt": "new",
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "UpgradeFailed"},
                {"type": "Stalled", "status": "True", "reason": "RetriesExceeded"},
            ],
        }
    }
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1]),
        pytest.raises(ClusterError, match="failed"),
    ):
        target.wait_helm_release("release", "namespace", 10, force_token="new")

    target.custom.get_namespaced_custom_object.side_effect = ApiException(status=500)
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1]),
        pytest.raises(ClusterError, match=r"inspect HelmRelease.*HTTP 500"),
    ):
        target.wait_helm_release("release", "namespace", 10)

    target.core.api_client.call_api.side_effect = ApiException(status=403)
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1]),
        pytest.raises(ClusterError, match=r"inspect Secret.*HTTP 403"),
    ):
        target.wait_secret("secret", "namespace", 10)


def test_wait_helm_release_allows_retryable_failure() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "False", "reason": "InstallFailed"},
                    {"type": "Reconciling", "status": "True", "reason": "ProgressingWithRetry"},
                ]
            }
        },
        {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
    ]
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        target.wait_helm_release("release", "namespace", 10)


def test_reconcile_kustomization_waits_for_requested_revision() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {
            "status": {
                "lastHandledReconcileAt": "old",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        },
        {
            "status": {
                "lastHandledReconcileAt": "new",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        },
    ]
    with (
        patch("openbao_stack_setup.cluster.time.time_ns", return_value="new"),
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        target.reconcile_kustomization("infrastructure", timeout_seconds=10)

    patch_call = target.custom.patch_namespaced_custom_object.call_args
    assert patch_call.args[:5] == (
        "kustomize.toolkit.fluxcd.io",
        "v1",
        "flux-system",
        "kustomizations",
        "infrastructure",
    )
    assert patch_call.args[-1] == {
        "metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": "new"}}
    }


def test_reconcile_kustomization_redacts_failures() -> None:
    target = cluster()
    target.custom.patch_namespaced_custom_object.side_effect = ApiException(
        status=500, reason="private controller detail"
    )
    with pytest.raises(ClusterError, match=r"Kustomization infrastructure.*HTTP 500") as captured:
        target.reconcile_kustomization("infrastructure")
    assert "private controller detail" not in str(captured.value)


def test_external_secret_refresh_operations() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {"status": {"refreshTime": "old"}},
        {"status": {"refreshTime": "old", "conditions": []}},
        {
            "status": {
                "refreshTime": "new",
                "conditions": [{"type": "Ready", "status": "True"}],
            }
        },
    ]

    assert target.external_secret_refresh_time("secret", "namespace") == "old"
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        target.wait_external_secret_refresh("secret", "namespace", "old", 10)


def test_ensure_secret_store_ready_skips_ready_store() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.return_value = {
        "status": {"conditions": [{"type": "Ready", "status": "True"}]}
    }

    target.ensure_secret_store_ready("store", "namespace")

    target.custom.patch_namespaced_custom_object.assert_not_called()


def test_ensure_secret_store_ready_wakes_stale_store() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = [
        {"status": {"conditions": [{"type": "Ready", "status": "False"}]}},
        {"status": {"conditions": [{"type": "Ready", "status": "False"}]}},
        {"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
    ]
    with (
        patch("openbao_stack_setup.cluster.time.time_ns", return_value=123),
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
    ):
        target.ensure_secret_store_ready("store", "namespace", 10)

    assert target.custom.patch_namespaced_custom_object.call_args.args == (
        "external-secrets.io",
        "v1",
        "namespace",
        "secretstores",
        "store",
        {"metadata": {"annotations": {"force-sync": "123"}}},
    )


def test_wait_secret_store_ready_times_out() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.return_value = {
        "status": {"conditions": [{"type": "Ready", "status": "False"}]}
    }
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 1, 11]),
        patch("openbao_stack_setup.cluster.time.sleep"),
        pytest.raises(ClusterError, match="Timed out waiting for SecretStore namespace/store"),
    ):
        target.wait_secret_store_ready("store", "namespace", 10)


def test_ensure_secret_store_ready_redacts_api_failure() -> None:
    target = cluster()
    target.custom.get_namespaced_custom_object.side_effect = ApiException(
        status=500, reason="private provider detail"
    )
    with pytest.raises(ClusterError, match=r"inspect SecretStore.*HTTP 500") as captured:
        target.ensure_secret_store_ready("store", "namespace")
    assert "private provider detail" not in str(captured.value)


def test_force_external_secret_refresh_waits_for_refresh_and_target_secret() -> None:
    target = cluster()
    with (
        patch.object(target, "external_secret_refresh_time", return_value="old") as refresh_time,
        patch.object(target, "wait_external_secret_refresh") as wait_refresh,
        patch.object(target, "wait_secret") as wait_secret,
        patch("openbao_stack_setup.cluster.time.time_ns", return_value=123),
    ):
        target.force_external_secret_refresh("external", "namespace", "target", 10)

    refresh_time.assert_called_once_with("external", "namespace")
    body = target.custom.patch_namespaced_custom_object.call_args.args[-1]
    assert body == {"metadata": {"annotations": {"force-sync": "123"}}}
    wait_refresh.assert_called_once_with("external", "namespace", "old", 10)
    wait_secret.assert_called_once_with("target", "namespace", 10)


def test_force_external_secret_refresh_redacts_api_failure() -> None:
    target = cluster()
    target.custom.patch_namespaced_custom_object.side_effect = ApiException(
        status=500, reason="private provider detail"
    )
    with (
        patch.object(target, "external_secret_refresh_time", return_value="old"),
        pytest.raises(
            ClusterError, match=r"ExternalSecret namespace/external.*HTTP 500"
        ) as captured,
    ):
        target.force_external_secret_refresh("external", "namespace", "target")

    assert "private provider detail" not in str(captured.value)


def test_external_secret_failures_and_timeouts() -> None:
    target = cluster()
    target.custom.patch_namespaced_custom_object.side_effect = ApiException(status=500)
    with pytest.raises(ClusterError, match=r"reconciliation.*HTTP 500"):
        target.force_reconcile("release")

    target.custom.get_namespaced_custom_object.side_effect = ApiException(status=500)
    with pytest.raises(ClusterError, match=r"ExternalSecret.*HTTP 500"):
        target.external_secret_refresh_time("secret", "namespace")

    target.custom.get_namespaced_custom_object.side_effect = None
    target.custom.get_namespaced_custom_object.return_value = {"status": {}}
    with (
        patch("openbao_stack_setup.cluster.time.monotonic", side_effect=[0, 2]),
        patch("openbao_stack_setup.cluster.time.sleep"),
        pytest.raises(ClusterError, match="Timed out waiting for ExternalSecret"),
    ):
        target.wait_external_secret_refresh("secret", "namespace", None, 1)
