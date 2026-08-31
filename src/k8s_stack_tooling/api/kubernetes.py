"""Kubernetes workload helpers for tooling jobs."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, Never, cast

import kubernetes.client
import kubernetes.config

from k8s_stack_tooling.api.http import log

WorkloadKind = Literal["Deployment", "DaemonSet"]
_WORKLOAD_KINDS = {"Deployment", "DaemonSet"}


def target_workload_kind(env_name: str = "TARGET_WORKLOAD_KIND") -> WorkloadKind:
    """Read and validate a target workload kind from the environment."""
    value = os.environ.get(env_name, "Deployment")
    if value not in _WORKLOAD_KINDS:
        log(f"ERROR: {env_name} must be Deployment or DaemonSet, got {value!r}.")
        raise SystemExit(1)
    return cast("WorkloadKind", value)


def credential_hash(values: dict[str, str]) -> str:
    """Return a stable non-reversible hash for a set of credentials."""
    digest = hashlib.sha256()
    for key, value in sorted(values.items()):
        digest.update(key.encode())
        digest.update(b"\0")
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def credential_purpose(category: str, namespace: str, secret: str, key: str) -> str:
    """Return a stable annotation purpose unique to one credential source."""
    identity = hashlib.sha256(f"{namespace}/{secret}:{key}".encode()).hexdigest()[:12]
    return f"{category}-{identity}"


def reconcile_workload(
    namespace: str,
    name: str,
    kind: WorkloadKind,
    digest: str,
    purpose: str,
) -> bool:
    """Reconcile a credential hash and report whether its rollout is fully ready."""
    fail_on_404 = os.environ.get("FAIL_ON_DEPLOYMENT_NOT_FOUND", "true").lower() != "false"
    kubernetes.config.load_incluster_config()
    client = kubernetes.client.AppsV1Api()
    annotation = f"credentials.neurwerk.com/{purpose}-hash"
    read = (
        client.read_namespaced_deployment
        if kind == "Deployment"
        else client.read_namespaced_daemon_set
    )
    try:
        workload = read(name, namespace)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 404 and not fail_on_404:
            log(f"{kind} {namespace}/{name} not found; skipping credential rollout.")
            return False
        _fail_workload_api("read", kind, namespace, name, exc)
    annotations = workload.spec.template.metadata.annotations or {}
    if annotations.get(annotation) == digest:
        ready = _workload_ready(workload, kind)
        state = "ready" if ready else "still rolling out"
        log(f"{kind} {namespace}/{name} has the current {purpose} hash and is {state}.")
        return ready
    body = {"spec": {"template": {"metadata": {"annotations": {annotation: digest}}}}}
    patch = (
        client.patch_namespaced_deployment
        if kind == "Deployment"
        else client.patch_namespaced_daemon_set
    )
    try:
        patch(name, namespace, body)
    except kubernetes.client.exceptions.ApiException as exc:
        _fail_workload_api("patch", kind, namespace, name, exc)
    log(f"{kind} {namespace}/{name} {purpose} credential rollout triggered.")
    return False


def _workload_ready(workload: object, kind: WorkloadKind) -> bool:
    """Return whether every desired pod uses the workload's current generation."""
    if kind == "Deployment":
        deployment = cast("kubernetes.client.V1Deployment", workload)
        desired = deployment.spec.replicas if deployment.spec.replicas is not None else 1
        status = deployment.status
        return bool(
            status.observed_generation is not None
            and status.observed_generation >= deployment.metadata.generation
            and status.updated_replicas == desired
            and status.replicas == status.updated_replicas
            and status.available_replicas == desired
            and not status.unavailable_replicas
        )
    daemon_set = cast("kubernetes.client.V1DaemonSet", workload)
    desired = daemon_set.status.desired_number_scheduled
    status = daemon_set.status
    return bool(
        status.observed_generation is not None
        and status.observed_generation >= daemon_set.metadata.generation
        and status.updated_number_scheduled == desired
        and status.number_available == desired
        and not status.number_unavailable
    )


def _fail_workload_api(
    action: str,
    kind: WorkloadKind,
    namespace: str,
    name: str,
    exc: kubernetes.client.exceptions.ApiException,
) -> Never:
    """Log and fail a workload API operation."""
    log(f"ERROR: Could not {action} {kind} {namespace}/{name}: HTTP {exc.status}")
    if exc.body:
        log(f"  Response: {exc.body[:300]}")
    raise SystemExit(1) from exc


def _lease_expired(lease: kubernetes.client.V1Lease, now: datetime) -> bool:
    """Return whether a Kubernetes Lease is unheld or expired."""
    spec = lease.spec
    if not spec.holder_identity:
        return True
    renewed = spec.renew_time or spec.acquire_time
    if renewed is None:
        return True
    if renewed.tzinfo is None:
        renewed = renewed.replace(tzinfo=UTC)
    duration = spec.lease_duration_seconds or 60
    return renewed + timedelta(seconds=duration) <= now


def _try_acquire_lease(
    client: kubernetes.client.CoordinationV1Api,
    namespace: str,
    name: str,
    holder: str,
) -> bool:
    """Atomically acquire an existing Lease when it is free or expired."""
    lease = client.read_namespaced_lease(name, namespace)
    now = datetime.now(UTC)
    if lease.spec.holder_identity not in {None, holder} and not _lease_expired(lease, now):
        return False
    lease.spec.holder_identity = holder
    lease.spec.acquire_time = now
    lease.spec.renew_time = now
    lease.spec.lease_duration_seconds = max(
        900, int(os.environ.get("RECONCILE_LOCK_DURATION_SECONDS", "900"))
    )
    try:
        client.replace_namespaced_lease(name, namespace, lease)
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status == 409:
            return False
        raise
    return True


@contextmanager
def reconciliation_lock() -> Iterator[bool]:
    """Acquire the configured Lease for one reconciliation operation."""
    name = os.environ.get("RECONCILE_LOCK_NAME", "")
    if not name:
        yield True
        return
    namespace = os.environ.get("RECONCILE_LOCK_NAMESPACE", os.environ.get("POD_NAMESPACE", ""))
    holder = os.environ.get("POD_NAME", "")
    if not namespace or not holder:
        log("ERROR: Lease locking requires RECONCILE_LOCK_NAMESPACE/POD_NAMESPACE and POD_NAME.")
        raise SystemExit(1)
    wait_seconds = max(0.0, float(os.environ.get("RECONCILE_LOCK_WAIT_SECONDS", "0")))
    deadline = time.monotonic() + wait_seconds
    kubernetes.config.load_incluster_config()
    client = kubernetes.client.CoordinationV1Api()
    acquired = False
    while True:
        try:
            acquired = _try_acquire_lease(client, namespace, name, holder)
        except kubernetes.client.exceptions.ApiException as exc:
            log(f"ERROR: Could not acquire Lease {namespace}/{name}: HTTP {exc.status}")
            raise SystemExit(1) from exc
        if acquired or time.monotonic() >= deadline:
            break
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    if not acquired:
        if os.environ.get("RECONCILE_LOCK_FAIL_IF_UNAVAILABLE", "false").lower() == "true":
            log(f"ERROR: Lease {namespace}/{name} remained held after the lock wait.")
            raise SystemExit(1)
        log(f"Lease {namespace}/{name} is held; skipping this periodic reconciliation.")
        yield False
        return
    try:
        yield True
    finally:
        _release_lease(client, namespace, name, holder)


def _release_lease(
    client: kubernetes.client.CoordinationV1Api,
    namespace: str,
    name: str,
    holder: str,
) -> None:
    """Release a Lease when it is still held by this process."""
    try:
        lease = client.read_namespaced_lease(name, namespace)
        if lease.spec.holder_identity != holder:
            return
        lease.spec.holder_identity = None
        lease.spec.renew_time = datetime.now(UTC)
        client.replace_namespaced_lease(name, namespace, lease)
    except kubernetes.client.exceptions.ApiException as exc:
        log(f"WARNING: Could not release Lease {namespace}/{name}: HTTP {exc.status}")


def run_with_reconciliation_lock(operation: Callable[[], None]) -> None:
    """Run an operation only while holding its configured reconciliation Lease."""
    with reconciliation_lock() as acquired:
        if acquired:
            operation()
