from __future__ import annotations

import copy
import inspect
import sys
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest.mock import MagicMock

import pytest
from k8s_stack_tooling.addons.schema import Artifact, NativeOutput
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup import native


class PlatformState(TypedDict):
    policies: list[dict[str, Any]]
    events: list[str]
    mutate: Callable[[dict[str, Any]], None]
    policy: dict[str, Any]
    pod: dict[str, Any]


@pytest.fixture
def definition():
    return NativeOutput.model_validate(
        {
            "id": "sample-output",
            "artifact": "sample-native",
            "argv": ["/app/sample", "generate"],
            "idleArgv": ["/bin/sleep", "60"],
            "outputKey": "token",
            "transport": "private-exec-text",
            "maxBytes": 64,
            "timeoutSeconds": 2,
            "validation": {"format": "hex", "minLength": 32, "maxLength": 32},
        }
    )


@pytest.fixture
def artifact():
    return Artifact(
        id="sample-native", image="example.test/native:1.0.0@sha256:" + "a" * 64, role="native"
    )


@pytest.fixture
def platform(monkeypatch):
    core, network = MagicMock(), MagicMock()
    cluster = SimpleNamespace(core=core, context="synthetic-context")
    core.read_namespace.return_value = {"metadata": {"uid": "namespace-uid"}}
    state: PlatformState = {
        "policies": [],
        "events": [],
        "mutate": lambda pod: None,
        "policy": {},
        "pod": {},
    }

    def create_policy(namespace, body, **kwargs):
        state["events"].append("policy")
        policy = copy.deepcopy(body)
        policy["metadata"]["uid"] = "policy-uid"
        state["policy"] = policy
        return policy

    def create_pod(namespace, body, **kwargs):
        state["events"].append("pod")
        pod = copy.deepcopy(body)
        pod["metadata"]["uid"] = "pod-uid"
        pod["spec"]["serviceAccountName"] = "default"
        pod["status"] = {
            "phase": "Running",
            "containerStatuses": [
                {"name": "native", "state": {"running": {}}},
            ],
        }
        state["mutate"](pod)
        state["pod"] = pod
        return pod

    network.create_namespaced_network_policy.side_effect = create_policy
    network.read_namespaced_network_policy.side_effect = lambda *a, **k: state["policy"]
    network.list_namespaced_network_policy.side_effect = lambda *a, **k: {
        "items": state["policies"],
    }
    core.create_namespaced_pod.side_effect = create_pod
    core.read_namespaced_pod.side_effect = lambda *a, **k: state["pod"]

    def delete_pod(*args, **kwargs):
        core.read_namespaced_pod.side_effect = ApiException(status=404)

    core.delete_namespaced_pod.side_effect = delete_pod
    monkeypatch.setattr(native.client, "NetworkingV1Api", lambda **kwargs: network)
    monkeypatch.setattr(native.shutil, "which", lambda name: "/test/kubectl")
    execute = MagicMock(return_value=b"a" * 32 + b"\n")
    monkeypatch.setattr(native, "_private_exec", execute)
    return cluster, network, state, execute


def run(platform, definition, artifact):
    return native.prepare_native_output(
        platform[0], namespace="scratch", definition=definition, artifact=artifact
    )


def test_private_exec_and_owned_cleanup(platform, definition, artifact):
    assert run(platform, definition, artifact) == {"token": "a" * 32}
    cluster, network, state, execute = platform
    assert state["events"] == ["policy", "pod"]
    spec = state["pod"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert not {"volumes", "imagePullSecrets", "hostNetwork", "initContainers"} & spec.keys()
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert "root" not in inspect.signature(native.prepare_native_output).parameters
    command = execute.call_args.args[0]
    assert command[-3:] == ["--", "/app/sample", "generate"]
    assert "logs" not in command
    for api, kind, uid in (
        (cluster.core, "pod", "pod-uid"),
        (network, "network_policy", "policy-uid"),
    ):
        deletion = getattr(api, f"delete_namespaced_{kind}")
        assert deletion.call_args.kwargs["body"]["preconditions"] == {"uid": uid}
    cluster.core.delete_namespace.assert_not_called()
    cluster.core.read_namespaced_secret.assert_not_called()


@pytest.mark.parametrize(
    "extra",
    [
        {"volumes": [{"secret": {"secretName": "credential"}}]},
        {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]},
        {"hostNetwork": True},
        {"hostPID": True},
        {"automountServiceAccountToken": True},
        {"serviceAccountName": "privileged"},
        {"imagePullSecrets": [{"name": "registry"}]},
        {"securityContext": {"runAsUser": 0}},
        {"initContainers": [{"name": "injected"}]},
    ],
)
def test_rejects_admitted_privilege_or_mounts(platform, definition, artifact, extra):
    platform[2]["mutate"] = lambda pod: pod["spec"].update(extra)
    with pytest.raises(native.NativeError, match="output hidden"):
        run(platform, definition, artifact)
    platform[3].assert_not_called()
    platform[0].core.delete_namespaced_pod.assert_called_once()


@pytest.mark.parametrize(
    "extra",
    [
        {"image": "example.test/changed:latest"},
        {"command": ["/app/changed"]},
        {"envFrom": [{"secretRef": {"name": "credential"}}]},
        {"securityContext": {"privileged": True}},
        {"terminationMessagePolicy": "FallbackToLogsOnError"},
    ],
)
def test_rejects_admitted_container_changes(platform, definition, artifact, extra):
    platform[2]["mutate"] = lambda pod: pod["spec"]["containers"][0].update(extra)
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()


@pytest.mark.parametrize("direction", ["ingress", "egress"])
def test_rejects_selecting_allow_policy_before_pod(platform, definition, artifact, direction):
    platform[2]["policies"] = [{"spec": {"podSelector": {}, direction: [{}]}}]
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[0].core.create_namespaced_pod.assert_not_called()
    platform[1].create_namespaced_network_policy.assert_not_called()
    platform[3].assert_not_called()


def test_admitted_label_can_select_other_allow_policy(platform, definition, artifact):
    platform[2]["policies"] = [
        {
            "spec": {
                "podSelector": {"matchExpressions": [{"key": "injected", "operator": "Exists"}]},
                "ingress": [{}],
            }
        }
    ]
    platform[2]["mutate"] = lambda pod: pod["metadata"]["labels"].update({"injected": "yes"})
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()


def test_namespace_replacement_blocks_exec(platform, definition, artifact):
    platform[0].core.read_namespace.side_effect = [
        {"metadata": {"uid": "namespace-uid"}},
        {"metadata": {"uid": "replacement"}},
    ]
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()


@pytest.mark.parametrize(
    "failure", [RuntimeError("PRIVATE OUTPUT"), TimeoutError("PRIVATE OUTPUT")]
)
def test_exec_failure_redacted_and_cleaned(platform, definition, artifact, failure, capsys):
    platform[3].side_effect = failure
    with pytest.raises(native.NativeError) as error:
        run(platform, definition, artifact)
    assert "PRIVATE" not in str(error.value)
    assert capsys.readouterr() == ("", "")
    platform[0].core.delete_namespaced_pod.assert_called_once()
    platform[1].delete_namespaced_network_policy.assert_called_once()


def test_invalid_output_and_cleanup_conflict(platform, definition, artifact):
    platform[3].return_value = b"PRIVATE INVALID OUTPUT"
    platform[0].core.delete_namespaced_pod.side_effect = ApiException(status=409, reason="PRIVATE")
    with pytest.raises(native.NativeError, match="cleanup incomplete") as error:
        run(platform, definition, artifact)
    assert "PRIVATE" not in str(error.value)
    platform[1].delete_namespaced_network_policy.assert_not_called()


@pytest.mark.parametrize("output", [b"invalid", b'{"wrongKey":"value"}', b"a" * 65])
def test_invalid_output_never_returned(platform, definition, artifact, output):
    platform[3].return_value = output
    with pytest.raises(native.NativeError, match="output hidden"):
        run(platform, definition, artifact)
    platform[3].assert_called_once()
    platform[1].delete_namespaced_network_policy.assert_called_once()


def test_sidecar_blocks_exec(platform, definition, artifact):
    platform[2]["mutate"] = lambda pod: pod["spec"]["containers"].append({"name": "sidecar"})
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()


def test_idle_failure_blocks_exec(platform, definition, artifact):
    platform[2]["mutate"] = lambda pod: pod["status"].update({"phase": "Failed"})
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()
    platform[1].delete_namespaced_network_policy.assert_called_once()


def test_deny_policy_changed_before_exec(platform, definition, artifact):
    def changed_policy(*args, **kwargs):
        policy = copy.deepcopy(platform[2]["policy"])
        policy["spec"]["egress"] = [{}]
        return policy

    platform[1].read_namespaced_network_policy.side_effect = changed_policy
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[3].assert_not_called()


def test_wrong_artifact_reference_rejected_before_api(platform, definition, artifact):
    artifact = artifact.model_copy(update={"id": "different"})
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[0].core.read_namespace.assert_not_called()


def test_ambiguous_pod_creation_retains_deny_policy(platform, definition, artifact):
    platform[0].core.create_namespaced_pod.side_effect = TimeoutError("PRIVATE")
    with pytest.raises(native.NativeError):
        run(platform, definition, artifact)
    platform[0].core.delete_namespaced_pod.assert_not_called()
    platform[1].delete_namespaced_network_policy.assert_not_called()


@pytest.mark.parametrize(
    ("script", "limit", "seconds", "message"),
    [
        ("import os; os.write(1, b'x' * 8192)", 32, 2, "output limit"),
        ("import time; time.sleep(10)", 32, 0.05, "timed out"),
        ("import sys; print('PRIVATE', file=sys.stderr); sys.exit(1)", 32, 2, "output hidden"),
    ],
)
def test_bounded_capture(script, limit, seconds, message, capsys):
    # Local synthetic producer only; no kubectl, native image, or credentials.
    with pytest.raises(native.NativeError, match=message):
        native._private_exec([sys.executable, "-c", script], time.monotonic() + seconds, limit)
    assert capsys.readouterr() == ("", "")
