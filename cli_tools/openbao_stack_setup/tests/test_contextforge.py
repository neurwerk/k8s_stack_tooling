from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession, request_body
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.catalog import contextforge_oauth_policy, namespace_policy
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError, SecretRecord
from openbao_stack_setup.cluster import Cluster, ClusterError
from openbao_stack_setup.contextforge import TOKEN_TTL_SECONDS, reconcile_contextforge_token
from openbao_stack_setup.credentials import (
    plan_bootstrap_passwords,
    reconcile_internal_credentials,
)


def test_contextforge_selection_fails_closed() -> None:
    cluster = Cluster.__new__(Cluster)
    cluster.core = MagicMock()
    cluster.core.read_namespaced_config_map.side_effect = ApiException(status=404)
    assert cluster.contextforge_admin_email() is None
    cluster.core.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ClusterError, match="HTTP 403"):
        cluster.contextforge_admin_email()
    cluster.core.read_namespaced_config_map.side_effect = None
    for values in (
        "contextforge: []",
        "contextforge: {enabled: 'true'}",
        "contextforge: {enabled: true}",
    ):
        cluster.core.read_namespaced_config_map.return_value = SimpleNamespace(
            data={"values.yaml": values}
        )
        with pytest.raises(ClusterError):
            cluster.contextforge_admin_email()
    cluster.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={
            "values.yaml": "contextforge: {enabled: true, platformAdminEmail: recovery@example.com}"
        }
    )
    assert cluster.contextforge_admin_email() == "recovery@example.com"


def test_credentials_are_opt_in_preserved_and_copy_conflicts_fail(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    assert "contextforge/internal" not in session.secrets
    reconcile_internal_credentials(api, {}, contextforge_admin_email="recovery@example.com")
    original = {path: dict(record.values) for path, record in session.secrets.items()}
    source = original["contextforge/internal"]
    assert len(set(source.values())) == len(source)
    assert (
        original["infra-postgres-operations/internal"]["contextforgePassword"]
        == source["postgresqlPassword"]
    )
    assert (
        reconcile_internal_credentials(
            api, {}, contextforge_admin_email="recovery@example.com"
        ).changed_paths
        == ()
    )
    reconcile_internal_credentials(api, {})
    assert {path: record.values for path, record in session.secrets.items()} == original
    destination = session.secrets["infra-postgres-operations/internal"].values
    destination["contextforgePassword"] = "conflicting-copy"
    with pytest.raises(OpenBaoError, match="contextforgePassword"):
        reconcile_internal_credentials(api, {}, contextforge_admin_email="recovery@example.com")
    assert destination["contextforgePassword"] == "conflicting-copy"
    assert session.secrets["contextforge/internal"].values == source


def test_native_oauth_and_eso_permissions_are_isolated() -> None:
    def capabilities(policy: str, path: str) -> set[str]:
        return {
            capability
            for pattern, rules in re.findall(r'path "([^"]+)" \{([^}]+)\}', policy)
            if fnmatchcase(path, pattern)
            for capability in re.findall(r'"([^\"]+)"', rules)
        }

    native = contextforge_oauth_policy()
    eso = namespace_policy("contextforge")
    token_path = "secret/data/contextforge/oauth/shared/0123456789abcdef/user%40example.com"
    metadata = token_path.replace("/data/", "/metadata/")
    assert capabilities(native, token_path) >= {"create", "read", "update", "delete"}
    assert capabilities(native, metadata) == {"delete"}
    assert capabilities(eso, token_path) == capabilities(eso, metadata) == set()
    assert capabilities(eso, "secret/data/contextforge/internal") == {"read"}
    for path in (
        "secret/data/contextforge/internal",
        "secret/data/forgejo/internal",
        "auth/token/create",
    ):
        assert capabilities(native, path) == set()


_BINDING = {"client": "client", "clusterId": "cluster", "namespaceUid": "namespace"}
_META = {**_BINDING, "purpose": "contextforge-oauth"}


def token_info(token: str = "old-token", **overrides):
    return {
        "id": token,
        "policies": ["contextforge-oauth"],
        "orphan": True,
        "renewable": False,
        "type": "service",
        "path": "auth/token/create-orphan",
        "meta": _META,
        "num_uses": 0,
        "creation_ttl": TOKEN_TTL_SECONDS,
        "explicit_max_ttl": TOKEN_TTL_SECONDS,
        "ttl": TOKEN_TTL_SECONDS - 10,
        **overrides,
    }


def token_api():
    api = MagicMock(spec=OpenBaoClient)
    api.read_secret.return_value = SecretRecord({"vaultToken": "old-token", "sibling": "keep"}, 2)
    api.lookup_token.return_value = token_info()
    api.create_orphan_token.return_value = "new-token"
    return api


def test_token_issuance_http_contract_and_redaction(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    session.failure = (
        "POST",
        "auth/token/create-orphan",
        200,
        {"auth": {"client_token": "new-token"}},
    )
    assert api.create_orphan_token("contextforge-oauth", TOKEN_TTL_SECONDS, _META) == "new-token"
    assert request_body(session.calls[-1]) == {
        "policies": ["contextforge-oauth"],
        "no_default_policy": True,
        "renewable": False,
        "type": "service",
        "ttl": "2592000s",
        "explicit_max_ttl": "2592000s",
        "meta": _META,
    }
    session.failure = ("POST", "auth/token/lookup", 200, {"data": token_info()})
    assert api.lookup_token("old-token") == token_info()
    api.revoke_token("old-token")
    assert session.calls[-1].path == "auth/token/revoke"
    assert request_body(session.calls[-1]) == {"token": "old-token"}
    session.failure = ("POST", "auth/token/revoke", 500, {"errors": ["old-token"]})
    with pytest.raises(OpenBaoError) as captured:
        api.revoke_token("old-token")
    assert "old-token" not in str(captured.value)
    assert all("old-token" not in request.path for request in session.calls)


def test_valid_token_is_reused_without_writes_or_rotation() -> None:
    api = token_api()
    assert reconcile_contextforge_token(api, _BINDING) == TOKEN_TTL_SECONDS - 10
    api.write_secret.assert_not_called()
    api.revoke_token.assert_not_called()
    api.create_orphan_token.assert_not_called()


def test_rotation_stops_on_unverifiable_lookup_or_unconfirmed_revocation() -> None:
    api = token_api()
    api.lookup_token.side_effect = OpenBaoError("private transport detail old-token")
    with pytest.raises(OpenBaoError) as captured:
        reconcile_contextforge_token(api, _BINDING, rotate=True)
    assert "old-token" not in str(captured.value)
    api.revoke_token.assert_not_called()
    api.create_orphan_token.assert_not_called()
    api = token_api()
    with pytest.raises(OpenBaoError, match="revocation is not confirmed"):
        reconcile_contextforge_token(api, _BINDING, rotate=True)
    api.create_orphan_token.assert_not_called()


@pytest.mark.parametrize(
    "info",
    [
        None,
        token_info(ttl=0),
        token_info(policies=["contextforge-oauth", "default"]),
        token_info(meta={"client": "other"}),
        token_info(orphan=False),
        token_info(renewable=True),
        token_info(creation_ttl=3600),
    ],
)
def test_invalid_or_expired_tokens_fail_closed_without_silent_rotation(info) -> None:
    api = token_api()
    api.lookup_token.return_value = info
    with pytest.raises(OpenBaoError) as captured:
        reconcile_contextforge_token(api, _BINDING)
    assert "old-token" not in str(captured.value)
    api.create_orphan_token.assert_not_called()
    api.revoke_token.assert_not_called()
    api.write_secret.assert_not_called()


@pytest.mark.parametrize("existing", [None, token_info(ttl=0)])
def test_explicit_rotation_revokes_old_before_creation_and_cas_preserves_siblings(existing) -> None:
    api = token_api()
    api.lookup_token.side_effect = [existing, None, token_info("new-token")]
    reconcile_contextforge_token(api, _BINDING, rotate=True)
    assert api.mock_calls.index(call.revoke_token("old-token")) < api.mock_calls.index(
        call.create_orphan_token("contextforge-oauth", TOKEN_TTL_SECONDS, _META)
    )
    api.write_secret.assert_called_once_with(
        "contextforge/internal", {"vaultToken": "new-token", "sibling": "keep"}, 2
    )


def test_issuance_failure_and_cas_failure_leave_no_unintended_live_token() -> None:
    api = token_api()
    api.read_secret.return_value = SecretRecord({"sibling": "keep"}, 1)
    api.lookup_token.return_value = token_info("new-token")
    api.write_secret.side_effect = OpenBaoError("CAS failed")
    with pytest.raises(OpenBaoError, match="CAS failed"):
        reconcile_contextforge_token(api, _BINDING)
    api.revoke_token.assert_called_once_with("new-token")
    api.reset_mock()
    api.create_orphan_token.side_effect = OpenBaoError("transport failure")
    with pytest.raises(OpenBaoError, match="outcome is unknown"):
        reconcile_contextforge_token(api, _BINDING)
    api.write_secret.assert_not_called()
    api.revoke_token.assert_not_called()


def test_rotation_stop_gate_requires_suspended_release_zero_replicas_and_no_active_pods() -> None:
    cluster = Cluster.__new__(Cluster)
    cluster.core = MagicMock()
    cluster.custom = MagicMock()
    cluster.custom.get_namespaced_custom_object.return_value = {"spec": {"suspend": True}}
    apps = MagicMock()
    apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
    cluster.core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
    with patch("openbao_stack_setup.cluster.kubernetes.client.AppsV1Api", return_value=apps):
        cluster.require_contextforge_stopped()
        apps.list_namespaced_deployment.return_value = SimpleNamespace(
            items=[SimpleNamespace(spec=SimpleNamespace(replicas=1))]
        )
        with pytest.raises(ClusterError, match="scaled to zero"):
            cluster.require_contextforge_stopped()
        apps.list_namespaced_deployment.return_value = SimpleNamespace(items=[])
        cluster.core.list_namespaced_pod.return_value = SimpleNamespace(
            items=[SimpleNamespace(status=SimpleNamespace(phase="Pending"))]
        )
        with pytest.raises(ClusterError):
            cluster.require_contextforge_stopped()
        cluster.core.list_namespaced_pod.return_value = SimpleNamespace(items=[])
        cluster.custom.get_namespaced_custom_object.return_value = {"spec": {"suspend": False}}
        with pytest.raises(ClusterError):
            cluster.require_contextforge_stopped()
