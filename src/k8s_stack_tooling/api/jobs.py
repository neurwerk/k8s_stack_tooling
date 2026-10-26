"""One-shot Kubernetes Jobs; callers own templates, permissions and safe summaries."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

_TIMEOUT = (5, 30)
_API_ERRORS = (ApiException, HTTPError, OSError)


class JobError(ValueError):
    """A redacted input/precondition failure; no submission was attempted."""


@contextmanager
def job_api_client(*, context: str, config_file: str | None = None) -> Iterator[client.ApiClient]:
    """Open an explicit kubeconfig context with transport retries/debugging disabled.

    Credentials are not persisted back to kubeconfig. Client construction failures
    are redacted; caller operations retain their own error handling. The client is
    closed on exit. Use this client for both CoreV1Api and BatchV1Api instances.
    """
    if not isinstance(context, str) or not context.strip():
        raise JobError("An explicit nonempty Kubernetes context is required")
    try:
        configuration = client.Configuration()
        configuration.retries = 0
        configuration.debug = False
        api = config.new_client_from_config(
            config_file=config_file,
            context=context,
            persist_config=False,
            client_configuration=configuration,
        )
    except Exception:
        # Config/provider errors can contain credentials or private file contents.
        raise JobError("Could not initialize Kubernetes Job client") from None
    with api:
        yield api


@dataclass(frozen=True)
class ConfigMapVersion:
    """Identity captured when the caller read and validated an input ConfigMap."""

    namespace: str
    name: str
    uid: str
    resource_version: str


@dataclass(frozen=True)
class Submission:
    namespace: str
    name: str
    status: Literal["submitted", "uncertain"]
    phase: Literal["snapshot", "job", "ownership", "complete"]
    uid: str | None = None


@dataclass(frozen=True)
class LogSummary[T]:
    status: Literal["available", "unavailable"]
    summary: T | None = None


def require_configmap_versions(core: Any, expected: Sequence[ConfigMapVersion]) -> None:
    """Reject stale inputs before writes. This is a preflight, not a cluster transaction."""
    for item in expected:
        if not all((item.namespace, item.name, item.uid, item.resource_version)):
            raise JobError("Input identity is incomplete")
        try:
            current = core.read_namespaced_config_map(
                item.name, item.namespace, _request_timeout=_TIMEOUT
            )
        except _API_ERRORS:
            raise JobError("Could not verify Job inputs") from None
        if (current.metadata.uid, current.metadata.resource_version) != (
            item.uid,
            item.resource_version,
        ):
            raise JobError("Job inputs changed; review again")


def submit_snapshot_job(
    core: Any,
    batch: Any,
    *,
    job: dict[str, Any],
    snapshot_data: Mapping[str, str],
    inputs: Sequence[ConfigMapVersion] = (),
) -> Submission:
    """Submit once and attach an immutable, nonsecret ConfigMap to the Job.

    The caller supplies a reviewed Job with explicit name/namespace and a volume
    referencing a ConfigMap of that same name. Use job_api_client, or configure
    injected API clients with transport retries disabled. Never retries or deletes on
    failure: an uncertain response can mean the Job is already running.
    """
    body = copy.deepcopy(job)
    metadata = body.get("metadata", {})
    name, namespace = metadata.get("name"), metadata.get("namespace")
    if (
        body.get("apiVersion") != "batch/v1"
        or body.get("kind") != "Job"
        or not isinstance(name, str)
        or not name
        or not isinstance(namespace, str)
        or not namespace
        or metadata.get("generateName")
        or metadata.get("ownerReferences")
    ):
        raise JobError("An explicitly named, namespace-bound Job is required")
    require_configmap_versions(core, inputs)
    phase: Literal["snapshot", "job", "ownership"] = "snapshot"
    uid = None
    try:
        snapshot = core.create_namespaced_config_map(
            namespace,
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": namespace},
                "immutable": True,
                "data": dict(snapshot_data),
            },
            _request_timeout=_TIMEOUT,
        )
        if not snapshot.metadata.uid or not snapshot.metadata.resource_version:
            return Submission(namespace, name, "uncertain", phase)
        phase = "job"
        created = batch.create_namespaced_job(namespace, body, _request_timeout=_TIMEOUT)
        uid = created.metadata.uid
        if not uid:
            return Submission(namespace, name, "uncertain", phase)
        phase = "ownership"
        core.patch_namespaced_config_map(
            name,
            namespace,
            {
                "metadata": {
                    "uid": snapshot.metadata.uid,
                    "resourceVersion": snapshot.metadata.resource_version,
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": name,
                            "uid": uid,
                            "controller": True,
                        }
                    ],
                }
            },
            _request_timeout=_TIMEOUT,
        )
    except _API_ERRORS:
        return Submission(namespace, name, "uncertain", phase, uid)
    return Submission(namespace, name, "submitted", "complete", uid)


def wait_for_job(
    batch: Any,
    *,
    namespace: str,
    name: str,
    uid: str,
    seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Literal["complete", "failed", "timeout", "uncertain"]:
    """Wait for this exact Job, without retrying failed API requests or logging bodies.

    Each request uses at most the remaining wait budget for its connect/read
    timeouts; transport scheduling can still extend wall-clock completion.
    """
    if not uid or not 0 <= seconds <= 86400:
        raise JobError("A Job UID and bounded wait are required")
    deadline = clock() + seconds
    while (remaining := deadline - clock()) > 0:
        try:
            job = batch.read_namespaced_job_status(
                name, namespace, _request_timeout=(min(5, remaining), min(30, remaining))
            )
        except _API_ERRORS:
            return "uncertain"
        if job.metadata.uid != uid:
            return "uncertain"
        conditions = job.status.conditions or []
        if any(c.type == "Failed" and c.status == "True" for c in conditions):
            return "failed"
        if any(c.type == "Complete" and c.status == "True" for c in conditions):
            return "complete"
        sleep(min(2, max(0, deadline - clock())))
    return "timeout"


def read_job_summary[T](
    core: Any,
    *,
    namespace: str,
    name: str,
    uid: str,
    container: str,
    summarize: Callable[[str], T | None],
    max_bytes: int = 16384,
) -> LogSummary[T]:
    """Pass bounded logs to a trusted product redactor; never return raw logs.

    The callback must allowlist safe output fields. None means no valid summary.
    Callback failures and API errors are suppressed without logging exception text.
    """
    if not uid or not container or not 1 <= max_bytes <= 1_048_576:
        raise JobError("A Job UID, container and bounded log budget are required")
    try:
        pods = core.list_namespaced_pod(
            namespace, label_selector=f"job-name={name}", _request_timeout=_TIMEOUT
        ).items
        if len(pods) != 1 or not any(
            owner.uid == uid and owner.kind == "Job" and owner.controller is True
            for owner in (pods[0].metadata.owner_references or [])
        ):
            return LogSummary("unavailable")
        response = core.read_namespaced_pod_log(
            pods[0].metadata.name,
            namespace,
            container=container,
            tail_lines=1,
            limit_bytes=max_bytes,
            _preload_content=False,
            _request_timeout=_TIMEOUT,
        )
        try:
            raw = response.read(max_bytes + 1)
        finally:
            try:
                response.close()
            finally:
                response.release_conn()
        if not isinstance(raw, bytes) or len(raw) > max_bytes:
            return LogSummary("unavailable")
        summary = summarize(raw.decode("utf-8"))
        if summary is None:
            return LogSummary("unavailable")
        return LogSummary("available", summary)
    except Exception:
        # Includes product redactor failures, whose messages may contain log data.
        return LogSummary("unavailable")
