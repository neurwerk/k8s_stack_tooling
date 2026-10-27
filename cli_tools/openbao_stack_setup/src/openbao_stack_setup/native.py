"""Private preparation of missing native credentials, without OpenBao authority.

Requires the supported platform's enforcing NetworkPolicy CNI and trusted admission
and administrators. The product delivery selects an existing namespace. This module
does not create namespaces, inspect credentials, or establish a new sandbox system.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING, Any

from k8s_stack_tooling.addons import ExternalInput
from k8s_stack_tooling.addons.schema import Artifact, NativeOutput, decode_native_output
from kubernetes import client

if TYPE_CHECKING:
    from openbao_stack_setup.cluster import Cluster

_TIMEOUT = (3, 10)
_LABEL = "setup.neurwerk.com/native-preparation"
REGISTRY_TYPE = "kubernetes.io/dockerconfigjson"


class NativeError(RuntimeError):
    """A redacted failure; native output must never appear in diagnostics."""


def _document(value: Any) -> dict[str, Any]:  # noqa: ANN401 -- Kubernetes models or dicts
    with client.ApiClient() as api:
        return api.sanitize_for_serialization(value)


def _uid(value: Any) -> str:  # noqa: ANN401 -- Kubernetes models or dicts
    uid = _document(value).get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        raise NativeError("Native preparation resource identity is unavailable")
    return uid


def _selects(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for term in selector.get("matchExpressions", []):
        key, op, values = term["key"], term["operator"], term.get("values", [])
        if op not in {"In", "NotIn", "Exists", "DoesNotExist"}:
            raise NativeError("Unsupported network selector")
        matches = {
            "In": key in labels and labels[key] in values,
            "NotIn": key not in labels or labels[key] not in values,
            "Exists": key in labels,
            "DoesNotExist": key not in labels,
        }
        if not matches[op]:
            return False
    return True


def _check_policies(network: Any, namespace: str, labels: dict[str, str]) -> None:  # noqa: ANN401
    policies = _document(
        network.list_namespaced_network_policy(namespace, _request_timeout=_TIMEOUT)
    )
    for policy in policies["items"]:
        spec = policy["spec"]
        if not _selects(spec["podSelector"], labels):
            continue
        types = spec.get("policyTypes", ["Ingress"] + (["Egress"] if spec.get("egress") else []))
        if ("Ingress" in types and spec.get("ingress")) or (
            "Egress" in types and spec.get("egress")
        ):
            raise NativeError("A selecting network policy allows native preparation traffic")


def _pod(
    name: str,
    namespace: str,
    definition: NativeOutput,
    artifact: Artifact,
    pull_inputs: tuple[ExternalInput, ...] = (),
) -> dict[str, Any]:
    pod: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {_LABEL: name}},
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": definition.timeout_seconds,
            "terminationGracePeriodSeconds": 0,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "native",
                    "image": artifact.image,
                    "command": definition.idle_argv,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "1", "memory": "256Mi"},
                    },
                }
            ],
        },
    }
    if pull_inputs:
        pod["spec"]["imagePullSecrets"] = [{"name": item.name} for item in pull_inputs]
    return pod


def _verify_pod(actual: dict[str, Any], expected: dict[str, Any], uid: str) -> None:
    if _uid(actual) != uid or actual["metadata"].get("deletionTimestamp"):
        raise NativeError("Native preparation Pod identity changed")
    if actual["metadata"].get("labels", {}).get(_LABEL) != expected["metadata"]["name"]:
        raise NativeError("Native preparation isolation label changed")
    spec, wanted = actual["spec"], expected["spec"]
    # Only harmless API/scheduler defaults may supplement the submitted spec.
    defaults = {
        "dnsPolicy",
        "schedulerName",
        "nodeName",
        "tolerations",
        "priority",
        "preemptionPolicy",
        "serviceAccount",
        "serviceAccountName",
    }
    if set(spec) - set(wanted) - defaults:
        raise NativeError("Unexpected native preparation Pod fields")
    if spec.get("serviceAccountName", "default") != "default":
        raise NativeError("Unexpected native preparation ServiceAccount")
    for key, value in wanted.items():
        if key != "containers" and spec.get(key) != value:
            raise NativeError("Native preparation Pod security or execution settings changed")
    _verify_container(spec["containers"], wanted["containers"][0])


def _verify_container(containers: list[dict[str, Any]], original: dict[str, Any]) -> None:
    if len(containers) != 1:
        raise NativeError("Unexpected native preparation containers")
    container = containers[0]
    if (
        set(container)
        - set(original)
        - {
            "imagePullPolicy",
            "terminationMessagePath",
            "terminationMessagePolicy",
        }
    ):
        raise NativeError("Unexpected native preparation container fields")
    if any(container.get(key) != value for key, value in original.items()):
        raise NativeError("Native preparation container was modified")
    if container.get("terminationMessagePolicy", "File") != "File":
        raise NativeError("Native preparation log fallback is forbidden")


def _verify_policy(actual: dict[str, Any], expected: dict[str, Any], uid: str) -> None:
    spec = actual["spec"]
    if (
        _uid(actual) != uid
        or actual["metadata"].get("deletionTimestamp")
        or spec.get("podSelector") != expected["spec"]["podSelector"]
        or set(spec.get("policyTypes", [])) != {"Ingress", "Egress"}
        or spec.get("ingress")
        or spec.get("egress")
    ):
        raise NativeError("Native preparation deny policy changed")


def _private_exec(command: list[str], deadline: float, limit: int) -> bytes:
    """Drain only bounded stdout; never collect stderr or Pod logs."""
    if time.monotonic() >= deadline:
        raise NativeError("Private native exec deadline expired")
    output = bytearray()
    with subprocess.Popen(  # noqa: S603 -- resolved kubectl and fixed non-shell argv
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            if process.stdout is None:
                raise NativeError("Private native exec has no output stream")
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise NativeError("Private native exec timed out")
                    chunk = os.read(process.stdout.fileno(), min(4096, limit + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > limit:
                        raise NativeError("Private native exec exceeded output limit")
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise NativeError("Private native exec failed; output hidden")
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _ready(
    core: client.CoreV1Api, namespace: str, pod: dict[str, Any], uid: str, deadline: float
) -> dict[str, Any]:
    while time.monotonic() < deadline:
        actual = _document(
            core.read_namespaced_pod(
                pod["metadata"]["name"],
                namespace,
                _request_timeout=_TIMEOUT,
            )
        )
        _verify_pod(actual, pod, uid)
        status = actual.get("status", {})
        if status.get("phase") in {"Succeeded", "Failed"}:
            raise NativeError("Native idle command exited before preparation")
        states = status.get("containerStatuses", [])
        if len(states) == 1 and states[0].get("state", {}).get("running") is not None:
            return actual
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise NativeError("Native preparation Pod did not start within its deadline")


def _pod_absent(core: Any, namespace: str, name: str) -> None:  # noqa: ANN401
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            core.read_namespaced_pod(name, namespace, _request_timeout=_TIMEOUT)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise
        time.sleep(0.1)
    raise NativeError("Native preparation Pod removal is unconfirmed")


def _cleanup(
    core: client.CoreV1Api,
    network: client.NetworkingV1Api,
    namespace: str,
    name: str,
    owned: dict[str, str],
    pod_attempted: bool,
) -> None:
    if pod_attempted and "pod" not in owned:
        raise NativeError("Native preparation creation uncertain; retain deny policy for review")
    failed = False
    for kind, api in (("pod", core), ("network_policy", network)):
        if kind not in owned:
            continue
        try:
            getattr(api, f"delete_namespaced_{kind}")(
                name,
                namespace,
                body={"preconditions": {"uid": owned[kind]}, "gracePeriodSeconds": 0},
                _request_timeout=_TIMEOUT,
            )
            if kind == "pod":
                _pod_absent(core, namespace, name)
        except client.exceptions.ApiException as exc:
            failed |= exc.status != 404
        except Exception:  # noqa: BLE001 -- cleanup must continue without leaking API responses
            failed = True
        if failed:
            break  # Keep deny policy when Pod deletion is not confirmed.
    if failed:
        raise NativeError("Native preparation cleanup incomplete; inspect owned resources")


def prepare_native_output(
    cluster: Cluster,
    *,
    namespace: str,
    definition: NativeOutput,
    artifact: Artifact,
    pull_inputs: tuple[ExternalInput, ...] = (),
) -> dict[str, str]:
    """Prepare one missing output, trusting supported CNI/admission enforcement.

    Namespace must already exist. No root client or secret input is accepted. The
    caller must first verify source/selection. Native execution gets no custody material.
    Cleanup uses UID preconditions and never deletes namespaces or storage. An
    ambiguous creation response is reported, not retried or deleted without a UID.
    """
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", namespace)
        or definition.artifact != artifact.id
        or artifact.role != "native"
        or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", artifact.image)
        or not cluster.context
    ):
        raise NativeError("Invalid native preparation identity or pinned artifact")
    if any(
        item.namespace != namespace or item.secret_type != REGISTRY_TYPE for item in pull_inputs
    ) or len({item.name for item in pull_inputs}) != len(pull_inputs):
        raise NativeError("Native pull inputs must be exact namespace-local registry prerequisites")
    if pull_inputs:
        cluster.require_external_inputs(list(pull_inputs))
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise NativeError("Native preparation requires kubectl")
    name = f"native-{uuid.uuid4().hex}"
    pod = _pod(name, namespace, definition, artifact, pull_inputs)
    network = client.NetworkingV1Api(api_client=cluster.core.api_client)
    core, owned = cluster.core, {}
    pod_attempted = False
    deadline = time.monotonic() + definition.timeout_seconds
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace, "labels": {_LABEL: name}},
        "spec": {
            "podSelector": {"matchLabels": {_LABEL: name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }
    try:
        namespace_uid = _uid(core.read_namespace(namespace, _request_timeout=_TIMEOUT))
        _check_policies(network, namespace, pod["metadata"]["labels"])
        created = network.create_namespaced_network_policy(
            namespace, policy, _request_timeout=_TIMEOUT
        )
        owned["network_policy"] = _uid(created)
        _verify_policy(_document(created), policy, owned["network_policy"])
        pod_attempted = True
        created = core.create_namespaced_pod(namespace, pod, _request_timeout=_TIMEOUT)
        owned["pod"] = _uid(created)
        actual = _ready(core, namespace, pod, owned["pod"], deadline)
        if _uid(core.read_namespace(namespace, _request_timeout=_TIMEOUT)) != namespace_uid:
            raise NativeError("Native preparation namespace identity changed")  # noqa: TRY301
        current = _document(
            network.read_namespaced_network_policy(name, namespace, _request_timeout=_TIMEOUT)
        )
        _verify_policy(current, policy, owned["network_policy"])
        _check_policies(network, namespace, actual["metadata"].get("labels", {}))
        command = [
            kubectl,
            "--context",
            cluster.context,
            "--namespace",
            namespace,
            f"--request-timeout={definition.timeout_seconds}s",
            "exec",
            name,
            "--container",
            "native",
            "--",
            *definition.argv,
        ]
        return decode_native_output(
            definition, _private_exec(command, deadline, definition.max_bytes)
        )
    except Exception:  # noqa: BLE001 -- redact all upstream failures, including output validation
        raise NativeError("Native preparation failed; output hidden") from None
    finally:
        _cleanup(core, network, namespace, name, owned, pod_attempted)
