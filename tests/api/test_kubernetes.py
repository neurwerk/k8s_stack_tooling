"""Tests for shared Kubernetes helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_stack_tooling.api import kubernetes


@pytest.mark.parametrize("kind", ["Deployment", "DaemonSet"])
def test_target_workload_kind_accepts_supported_values(monkeypatch, kind) -> None:
    monkeypatch.setenv("TARGET_WORKLOAD_KIND", kind)
    assert kubernetes.target_workload_kind() == kind


def test_target_workload_kind_rejects_other_kinds(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_WORKLOAD_KIND", "StatefulSet")
    with pytest.raises(SystemExit):
        kubernetes.target_workload_kind()


def _workload(annotations=None, ready=True):
    replicas = 1 if ready else 0
    return SimpleNamespace(
        metadata=SimpleNamespace(generation=2),
        spec=SimpleNamespace(
            replicas=1,
            template=SimpleNamespace(metadata=SimpleNamespace(annotations=annotations)),
        ),
        status=SimpleNamespace(
            observed_generation=2,
            replicas=replicas,
            updated_replicas=replicas,
            available_replicas=replicas,
            unavailable_replicas=None if ready else 1,
            desired_number_scheduled=1,
            updated_number_scheduled=replicas,
            number_available=replicas,
            number_unavailable=None if ready else 1,
        ),
    )


@pytest.mark.parametrize(
    ("kind", "read_method", "patch_method"),
    [
        ("Deployment", "read_namespaced_deployment", "patch_namespaced_deployment"),
        ("DaemonSet", "read_namespaced_daemon_set", "patch_namespaced_daemon_set"),
    ],
)
def test_reconcile_workload_sets_purpose_hash(kind, read_method, patch_method) -> None:
    client = Mock()
    getattr(client, read_method).return_value = _workload()
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert not kubernetes.reconcile_workload("target", "app", kind, "digest", "oidc")
    body = getattr(client, patch_method).call_args.args[2]
    assert body["spec"]["template"]["metadata"]["annotations"] == {
        "credentials.neurwerk.com/oidc-hash": "digest"
    }


def test_reconcile_workload_retries_stale_annotation_after_secret_is_current() -> None:
    client = Mock()
    client.read_namespaced_deployment.return_value = _workload(
        {"credentials.neurwerk.com/api-key-hash": "old"}
    )
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert not kubernetes.reconcile_workload("target", "app", "Deployment", "new", "api-key")
    client.patch_namespaced_deployment.assert_called_once()


def test_reconcile_workload_is_noop_when_hash_matches() -> None:
    client = Mock()
    client.read_namespaced_deployment.return_value = _workload(
        {"credentials.neurwerk.com/oidc-hash": "digest"}
    )
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert kubernetes.reconcile_workload("target", "app", "Deployment", "digest", "oidc")
    client.patch_namespaced_deployment.assert_not_called()


def test_reconcile_workload_reports_current_but_unready_rollout() -> None:
    client = Mock()
    client.read_namespaced_deployment.return_value = _workload(
        {"credentials.neurwerk.com/api-key-hash": "digest"}, ready=False
    )
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert not kubernetes.reconcile_workload("target", "app", "Deployment", "digest", "api-key")


def test_reconcile_workload_waits_for_old_deployment_replicas_to_terminate() -> None:
    client = Mock()
    workload = _workload({"credentials.neurwerk.com/api-key-hash": "digest"})
    workload.status.replicas = 2
    client.read_namespaced_deployment.return_value = workload
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert not kubernetes.reconcile_workload("target", "app", "Deployment", "digest", "api-key")


def test_reconcile_workload_accepts_scaled_to_zero_deployment() -> None:
    client = Mock()
    workload = _workload({"credentials.neurwerk.com/api-key-hash": "digest"})
    workload.spec.replicas = 0
    workload.status.replicas = 0
    workload.status.updated_replicas = 0
    workload.status.available_replicas = 0
    client.read_namespaced_deployment.return_value = workload
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert kubernetes.reconcile_workload("target", "app", "Deployment", "digest", "api-key")


def test_reconcile_workload_tolerates_missing_periodic_target(monkeypatch) -> None:
    monkeypatch.setenv("FAIL_ON_DEPLOYMENT_NOT_FOUND", "false")
    client = Mock()
    client.read_namespaced_deployment.side_effect = ApiException(status=404)
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "AppsV1Api", return_value=client),
    ):
        assert not kubernetes.reconcile_workload("target", "app", "Deployment", "digest", "oidc")


def _lease(holder=None, renewed=None):
    return SimpleNamespace(
        spec=SimpleNamespace(
            holder_identity=holder,
            renew_time=renewed,
            acquire_time=renewed,
            lease_duration_seconds=60,
        )
    )


def test_reconciliation_lock_acquires_and_releases(monkeypatch) -> None:
    monkeypatch.setenv("RECONCILE_LOCK_NAME", "lock")
    monkeypatch.setenv("POD_NAMESPACE", "source")
    monkeypatch.setenv("POD_NAME", "job-1")
    client = Mock()
    client.read_namespaced_lease.side_effect = [_lease(), _lease("job-1")]
    operation = Mock()
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "CoordinationV1Api", return_value=client),
    ):
        kubernetes.run_with_reconciliation_lock(operation)
    operation.assert_called_once()
    assert client.replace_namespaced_lease.call_count == 2


def test_reconciliation_lock_skips_live_holder(monkeypatch) -> None:
    monkeypatch.setenv("RECONCILE_LOCK_NAME", "lock")
    monkeypatch.setenv("POD_NAMESPACE", "source")
    monkeypatch.setenv("POD_NAME", "job-2")
    client = Mock()
    client.read_namespaced_lease.return_value = _lease("job-1", datetime.now(UTC))
    operation = Mock()
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "CoordinationV1Api", return_value=client),
    ):
        kubernetes.run_with_reconciliation_lock(operation)
    operation.assert_not_called()


def test_reconciliation_lock_takes_over_expired_holder(monkeypatch) -> None:
    monkeypatch.setenv("RECONCILE_LOCK_NAME", "lock")
    monkeypatch.setenv("POD_NAMESPACE", "source")
    monkeypatch.setenv("POD_NAME", "job-2")
    expired = datetime.now(UTC) - timedelta(minutes=2)
    client = Mock()
    client.read_namespaced_lease.side_effect = [_lease("job-1", expired), _lease("job-2")]
    operation = Mock()
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "CoordinationV1Api", return_value=client),
    ):
        kubernetes.run_with_reconciliation_lock(operation)
    operation.assert_called_once()


def test_reconciliation_lock_fails_hook_after_wait(monkeypatch) -> None:
    monkeypatch.setenv("RECONCILE_LOCK_NAME", "lock")
    monkeypatch.setenv("POD_NAMESPACE", "source")
    monkeypatch.setenv("POD_NAME", "hook")
    monkeypatch.setenv("RECONCILE_LOCK_FAIL_IF_UNAVAILABLE", "true")
    client = Mock()
    client.read_namespaced_lease.return_value = _lease("other", datetime.now(UTC))
    with (
        patch.object(kubernetes.kubernetes.config, "load_incluster_config"),
        patch.object(kubernetes.kubernetes.client, "CoordinationV1Api", return_value=client),
        pytest.raises(SystemExit),
    ):
        kubernetes.run_with_reconciliation_lock(Mock())
