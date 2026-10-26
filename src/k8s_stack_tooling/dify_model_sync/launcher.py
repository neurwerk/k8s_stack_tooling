"""Snapshot installed, inert job inputs; never execute code inside application pods."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException
from urllib3.exceptions import HTTPError

from k8s_stack_tooling.dify_model_sync.projection import (
    build_projection,
    require_installed_projection,
)
from k8s_stack_tooling.gateway_catalog import GatewayCatalog
from k8s_stack_tooling.gateway_model_metadata import MetadataError, canonical_json, load_json

PRIMARY = "agentgateway-catalog-config-map"
COMPANION = "gateway-model-metadata-config-map"
INSTALLED = "frontend-dify-model-sync"
TIMEOUT = (5, 30)


def read_inputs(
    core: Any, catalog_namespace: str, namespace: str
) -> tuple[dict[str, Any], list[Any]]:
    maps = [
        core.read_namespaced_config_map(name, ns, _request_timeout=TIMEOUT)
        for name, ns in (
            (PRIMARY, catalog_namespace),
            (COMPANION, catalog_namespace),
            (INSTALLED, namespace),
        )
    ]
    primary = GatewayCatalog.model_validate(load_json(maps[0].data["catalog.json"]))
    proposed = build_projection(primary, load_json(maps[1].data["metadata.json"]))
    return proposed, maps


def prepare_job(
    data: dict[str, str], *, namespace: str, name: str, mode: str, expected: str, max_company: int
) -> dict[str, Any]:
    job = copy.deepcopy(load_json(data["job-template.json"]))
    if job.get("apiVersion") != "batch/v1" or job.get("kind") != "Job":
        raise MetadataError("installed template is not a batch/v1 Job")
    metadata = job.setdefault("metadata", {})
    if metadata.get("namespace", namespace) != namespace or metadata.get("ownerReferences"):
        raise MetadataError("invalid installed job ownership")
    metadata.pop("generateName", None)
    metadata.pop("name", None)
    metadata.update({"name": name, "namespace": namespace})
    annotations = metadata.get("annotations", {})
    if any(key.startswith("helm.sh/hook") for key in annotations):
        raise MetadataError("manual job must not be a Helm hook")
    spec = job["spec"]
    if spec.get("backoffLimit") != 0:
        raise MetadataError("manual job retries must be disabled")
    pod = spec["template"]["spec"]
    if pod.get("restartPolicy") != "Never":
        raise MetadataError("manual job must not restart its payload")
    containers = pod["containers"]
    if len(containers) != 1:
        raise MetadataError("manual job requires exactly one payload container")
    env = containers[0].setdefault("env", [])
    supplied = {
        "DIFY_MODEL_SYNC_MODE": mode,
        "EXPECTED_PROJECTION_REVISION": expected,
        "MAX_COMPANY_MODELS": str(max_company),
    }
    env[:] = [item for item in env if item.get("name") not in supplied]
    env.extend({"name": key, "value": value} for key, value in supplied.items())
    replaced = 0
    for volume in pod.get("volumes", []):
        cm = volume.get("configMap", {})
        if cm.get("name") == INSTALLED:
            cm["name"] = name
            replaced += 1
    if replaced != 1:
        raise MetadataError("template must mount exactly one installed sync ConfigMap")
    return job


def launch(
    core: Any,
    batch: Any,
    *,
    catalog_namespace: str,
    namespace: str,
    mode: str,
    expected: str,
    max_company: int,
    wait_seconds: int,
) -> dict[str, Any]:
    if mode not in {"preview", "apply"} or not 0 <= max_company <= 27:
        raise MetadataError("invalid mode or company-model cap (maximum 27)")
    proposed, maps = read_inputs(core, catalog_namespace, namespace)
    data = maps[2].data
    require_installed_projection(load_json(data["projection.json"]), proposed, expected)
    company_count = sum(m["credential_mode"] == "company" for m in proposed["models"])
    if mode == "apply" and company_count > max_company:
        raise MetadataError("selected company models exceed the approved cap")
    name = f"frontend-dify-model-sync-{uuid.uuid4().hex[:12]}"
    job = prepare_job(
        data, namespace=namespace, name=name, mode=mode, expected=expected, max_company=max_company
    )
    # Check helper/template AND both catalog objects again immediately before snapshotting.
    current, current_maps = read_inputs(core, catalog_namespace, namespace)
    if canonical_json(current) != canonical_json(proposed) or any(
        (a.metadata.uid, a.metadata.resource_version)
        != (b.metadata.uid, b.metadata.resource_version)
        for a, b in zip(maps, current_maps, strict=True)
    ):
        raise MetadataError("sync inputs changed during preparation; review again")
    if not data.get("sync_models.py") or len(data["sync_models.py"].encode()) > 900_000:
        raise MetadataError("installed sync payload is missing or oversized")
    snapshot = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        immutable=True,
        data={key: data[key] for key in ("projection.json", "sync_models.py")},
    )
    result: dict[str, Any] = {
        "job": name,
        "mode": mode,
        "company_models": company_count,
        "selected_models": len(proposed["models"]),
        "status": "submitted",
    }
    phase = "snapshot"
    try:
        core.create_namespaced_config_map(namespace, snapshot, _request_timeout=TIMEOUT)
        # Never retry creation/apply: an uncertain API response requires operator inspection.
        phase = "job"
        created = batch.create_namespaced_job(namespace, job, _request_timeout=TIMEOUT)
        phase = "ownership"
        core.patch_namespaced_config_map(
            name,
            namespace,
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": name,
                            "uid": created.metadata.uid,
                            "controller": True,
                        }
                    ]
                }
            },
            _request_timeout=TIMEOUT,
        )
    except ApiException:
        # The job may already be running. Report its exact name, never promise rollback.
        return {**result, "status": "uncertain", "phase": phase}
    if wait_seconds:
        try:
            result.update(wait_result(core, batch, name, namespace, wait_seconds, mode=mode))
        except ApiException:
            return {**result, "status": "uncertain", "phase": "wait"}
    return result


def safe_summary(logs: str, *, mode: str) -> dict[str, Any]:
    """Keep completed and planned counts separate; suppress every other log field."""
    allowed = {"created", "updated", "unchanged", "skipped"}
    summary: dict[str, Any] = {}
    for line in logs.splitlines():
        candidate = line.removeprefix("DIFY_MODEL_SYNC_RESULT ")
        try:
            value = load_json(candidate, max_bytes=16384)
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, dict) or value.get("mode") != mode:
            continue
        counts, planned_counts = value.get("counts"), value.get("planned_counts")
        if not isinstance(counts, dict) or not isinstance(planned_counts, dict):
            continue
        if any(
            any(type(group.get(key)) is not int or not 0 <= group[key] <= 256 for key in allowed)
            for group in (counts, planned_counts)
        ):
            continue
        if mode == "preview" and (counts["created"] or counts["updated"]):
            continue
        summary = {
            "mode": mode,
            "counts": {k: counts[k] for k in sorted(allowed)},
            "planned_counts": {k: planned_counts[k] for k in sorted(allowed)},
        }
    return summary


def wait_result(
    core: Any, batch: Any, name: str, namespace: str, seconds: int, *, mode: str
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = batch.read_namespaced_job_status(name, namespace, _request_timeout=TIMEOUT).status
        conditions = status.conditions or []
        complete = any(c.type == "Complete" and c.status == "True" for c in conditions)
        failed = any(c.type == "Failed" and c.status == "True" for c in conditions)
        if complete or failed:
            pods = core.list_namespaced_pod(
                namespace, label_selector=f"job-name={name}", _request_timeout=TIMEOUT
            ).items
            summary: dict[str, Any] = {}
            if len(pods) == 1:
                try:
                    response = core.read_namespaced_pod_log(
                        pods[0].metadata.name,
                        namespace,
                        tail_lines=1,
                        limit_bytes=16384,
                        _preload_content=False,
                        _request_timeout=TIMEOUT,
                    )
                    try:
                        # Enforce the budget locally too; the API limit is approximate.
                        raw = response.read(16385)
                        if isinstance(raw, bytes) and len(raw) <= 16384:
                            summary = safe_summary(raw.decode("utf-8"), mode=mode)
                    finally:
                        try:
                            response.close()
                        finally:
                            response.release_conn()
                except (ApiException, HTTPError, OSError, ValueError):
                    return {"status": "uncertain", "counts": {}, "planned_counts": {}}
            return {
                "status": "failed" if failed else ("complete" if summary else "uncertain"),
                **(summary or {"counts": {}, "planned_counts": {}}),
            }
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return {"status": "timeout"}
