from __future__ import annotations

import json
import re
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fake import FakeSession, StoredSecret, request_body
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.catalog import REJECTION_DEBUG_PATH, REJECTION_DEBUG_ROLE
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import Cluster, ClusterError
from openbao_stack_setup.main import _converge_runtime
from openbao_stack_setup.rejection_debug import reconcile_rejection_debug

SOURCE = "monitor-langfuse/internal"


def test_selector_defaults_off_and_rejects_malformed_values_without_secret_access():
    cluster = Cluster.__new__(Cluster)
    cluster.core = MagicMock()
    read = cluster.core.read_namespaced_config_map
    read.side_effect = ApiException(status=404)
    assert cluster.rejection_debug_enabled() is False
    read.assert_called_once_with("client-values", "monitor-langfuse")
    read.side_effect = ApiException(status=403, reason="private response")
    with pytest.raises(ClusterError, match="HTTP 403") as error:
        cluster.rejection_debug_enabled()
    assert "private" not in str(error.value)
    read.side_effect = None
    for raw, expected in (
        ("{}", False),
        ("rejectedRequestDebug: {}", False),
        ("rejectedRequestDebug: {enabled: false}", False),
        ("rejectedRequestDebug: {enabled: true}", True),
        ("rejectedRequestDebug: {enabled: true, captureEnabled: false}", True),
    ):
        read.return_value = SimpleNamespace(data={"values.yaml": raw})
        assert cluster.rejection_debug_enabled() is expected
    for raw in (
        "[]",
        "null",
        "rejectedRequestDebug: null",
        'rejectedRequestDebug: {enabled: "true"}',
        "rejectedRequestDebug: {enabled: 1}",
        "rejectedRequestDebug: [private]",
    ):
        read.return_value = SimpleNamespace(data={"values.yaml": raw})
        with pytest.raises(ClusterError) as error:
            cluster.rejection_debug_enabled()
        assert "private" not in str(error.value)
    cluster.core.read_namespaced_secret.assert_not_called()


def test_keys_are_distinct_preserved_and_copied_only_to_debug_record(tmp_path, capsys):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    normal: dict[str, JsonValue] = {
        "initProjectPublicKey": "normal-public",
        "initProjectSecretKey": "normal-secret",
    }
    session.secrets[SOURCE] = StoredSecret(normal)
    assert reconcile_rejection_debug(api).added_fields == 0
    assert session.calls == []
    first = reconcile_rejection_debug(api, enabled=True)
    assert first.added_fields == 4
    source = session.secrets[SOURCE].values
    assert all(source[field] == value for field, value in normal.items())
    assert session.secrets[REJECTION_DEBUG_PATH].values == {
        "publicKey": source["debugProjectPublicKey"],
        "secretKey": source["debugProjectSecretKey"],
    }
    snapshot = dict(session.secrets)
    session.calls.clear()
    assert reconcile_rejection_debug(api, enabled=True).added_fields == 0
    assert not any(c.method == "POST" and c.path.startswith("secret/") for c in session.calls)
    assert session.secrets == snapshot
    assert set(session.secrets) == {SOURCE, REJECTION_DEBUG_PATH}
    session.calls.clear()
    reconcile_rejection_debug(api, enabled=False)
    assert session.calls == []
    assert session.secrets == snapshot
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("field", ["publicKey", "secretKey"])
def test_copy_conflicts_leave_both_records_untouched(tmp_path, field):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    reconcile_rejection_debug(api, enabled=True)
    session.secrets[REJECTION_DEBUG_PATH].values[field] = "existing-private-value"
    original = deepcopy(session.secrets)
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="copy conflicts") as error:
        reconcile_rejection_debug(api, enabled=True)
    assert "existing-private-value" not in str(error.value)
    assert session.secrets == original
    assert all(c.method == "GET" for c in session.calls)


@pytest.mark.parametrize("missing", ["publicKey", "secretKey"])
def test_matching_half_pair_destination_fails_before_any_write(tmp_path, missing):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    reconcile_rejection_debug(api, enabled=True)
    destination = session.secrets[REJECTION_DEBUG_PATH].values
    del destination[missing]
    destination["unrelated"] = "preserved"
    original = deepcopy(session.secrets)
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="copy is incomplete"):
        reconcile_rejection_debug(api, enabled=True)
    assert session.secrets == original
    assert all(c.method == "GET" for c in session.calls)


@pytest.mark.parametrize("invalid", [None, "", "sk-lf-secret\n", 1])
def test_invalid_or_partial_source_is_not_rotated(tmp_path, invalid):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    values = {"debugProjectPublicKey": "pk-lf-" + "p" * 43}
    session.secrets[SOURCE] = StoredSecret(values)
    with pytest.raises(OpenBaoError, match="incomplete"):
        reconcile_rejection_debug(api, enabled=True)
    values["debugProjectSecretKey"] = invalid
    with pytest.raises(OpenBaoError, match="invalid"):
        reconcile_rejection_debug(api, enabled=True)
    assert all(c.method == "GET" for c in session.calls)


@pytest.mark.parametrize(
    "normal,debug",
    [
        ("initProjectPublicKey", "debugProjectPublicKey"),
        ("initProjectSecretKey", "debugProjectSecretKey"),
    ],
)
def test_reusing_normal_project_key_is_rejected(tmp_path, normal, debug):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    values = {
        "debugProjectPublicKey": "pk-lf-" + "p" * 43,
        "debugProjectSecretKey": "sk-lf-" + "s" * 43,
    }
    values[normal] = values[debug]
    session.secrets[SOURCE] = StoredSecret(values)
    with pytest.raises(OpenBaoError, match="reuses tracing keys"):
        reconcile_rejection_debug(api, enabled=True)
    assert all(c.method == "GET" for c in session.calls)


def test_retry_after_copy_failure_preserves_generated_pair_and_cas(tmp_path):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    session.failure = ("POST", f"secret/data/{REJECTION_DEBUG_PATH}", 500, {})
    with pytest.raises(OpenBaoError):
        reconcile_rejection_debug(api, enabled=True)
    source = session.secrets[SOURCE]
    session.failure = None
    assert reconcile_rejection_debug(api, enabled=True).added_fields == 2
    assert session.secrets[SOURCE] == source
    with (
        patch.object(api, "read_secret", return_value=None),
        pytest.raises(OpenBaoError, match="HTTP 400"),
    ):
        reconcile_rejection_debug(api, enabled=True)
    assert session.secrets[SOURCE] == source


def test_scoped_role_and_legacy_deny_protect_debug_credentials(tmp_path):
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    reconcile_rejection_debug(api, enabled=True)
    posts = {c.path: request_body(c) for c in session.calls if c.method == "POST"}
    policy = posts[f"sys/policies/acl/{REJECTION_DEBUG_ROLE}"]["policy"]
    assert isinstance(policy, str)
    grants = {
        path: json.loads(capabilities)
        for path, capabilities in re.findall(
            r'path "([^"]+)" \{\s*capabilities = (\[[^\]]+\])',
            policy,
        )
    }
    assert grants == {
        f"secret/data/{REJECTION_DEBUG_PATH}": ["read"],
        "auth/token/lookup-self": ["read"],
        "auth/token/revoke-self": ["update"],
    }
    role = posts[f"auth/kubernetes/role/{REJECTION_DEBUG_ROLE}"]
    assert role["bound_service_account_names"] == [REJECTION_DEBUG_ROLE]
    assert role["bound_service_account_namespaces"] == ["monitor-agentgateway-extproc"]
    assert role["token_policies"] == [REJECTION_DEBUG_ROLE]
    assert role["token_no_default_policy"] is True
    assert role["audience"] == "openbao"
    assert "auth/kubernetes/role/monitor-agentgateway-extproc" not in posts
    legacy = posts["sys/policies/acl/monitor-agentgateway-extproc"]["policy"]
    assert isinstance(legacy, str)
    assert f'path "secret/data/{REJECTION_DEBUG_PATH}" {{ capabilities = ["deny"] }}' in legacy
    policy_index = next(
        i
        for i, c in enumerate(session.calls)
        if c.path == "sys/policies/acl/monitor-agentgateway-extproc"
    )
    record_index = next(
        i
        for i, c in enumerate(session.calls)
        if c.method == "POST" and c.path.startswith("secret/data/")
    )
    assert policy_index < record_index


@pytest.mark.parametrize("enabled", [False, True])
def test_retained_langfuse_cleanup_refresh_never_waits_for_removed_extproc_capture(enabled):
    cluster = MagicMock(spec=Cluster)
    with (
        patch("openbao_stack_setup.main._converge_bootstrap_secret_stores"),
        patch("openbao_stack_setup.main._reconcile_bootstrap_helm_releases"),
    ):
        _converge_runtime(cluster, False, rejection_debug_enabled=enabled)
    stores = cluster.ensure_secret_store_ready.call_args_list
    targets = cluster.force_external_secret_refresh.call_args_list
    debug_stores = [c for c in stores if "rejection-debug" in c.args[0]]
    debug_targets = [c for c in targets if "rejection-debug" in c.args[0]]
    assert debug_stores == []
    assert [target.args for target in debug_targets] == (
        [
            (
                "monitor-langfuse-rejection-debug",
                "monitor-langfuse",
                "monitor-langfuse-rejection-debug",
            )
        ]
        if enabled
        else []
    )
    assert not any(
        "monitor-agentgateway-extproc-rejection-debug" in str(c) for c in cluster.mock_calls
    )
    assert not any("docling" in c.args[0] for c in stores + targets)
