from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fake import FakeSession, StoredSecret, request_body
from kubernetes.client.exceptions import ApiException

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import Cluster, ClusterError
from openbao_stack_setup.mcp import reconcile_mcp, shared_ids


def test_selects_shared_keys_but_not_personal_connections_or_no_auth() -> None:
    assert shared_ids(
        [
            {"id": "exa", "credential": {"owner": "shared", "method": "gateway-header"}},
            {"id": "github", "credential": {"owner": "individual", "method": "oauth"}},
            {"id": "brave", "credential": {"owner": "shared", "method": "upstream-env"}},
            {"id": "public", "credential": {"owner": "none", "method": "none"}},
        ]
    ) == ("brave", "exa")
    assert shared_ids([]) == ()


@pytest.mark.parametrize(
    "rows",
    [
        {},
        [None],
        [{"id": "brave", "credential": {"owner": "shared", "method": "oauth"}}],
        [{"id": "brave", "credential": {"owner": ["shared"], "method": "upstream-env"}}],
        [{"id": "brave", "credential": {"owner": "shared", "method": ["upstream-env"]}}],
        [{"id": "brave", "credential": None}],
        [{"id": "brave", "credential": {"owner": "shared", "method": "upstream-env"}}] * 2,
    ],
)
def test_rejects_malformed_or_duplicate_catalog_entries(rows) -> None:
    with pytest.raises(OpenBaoError):
        shared_ids(rows)


@pytest.mark.parametrize("identity", ["../github", "a/b", "*", "+", 'a"', "a\n", "A", "a" * 49])
def test_rejects_ids_that_could_escape_exact_acl_paths(identity) -> None:
    with pytest.raises(OpenBaoError):
        shared_ids([{"id": identity, "credential": {"owner": "shared", "method": "upstream-env"}}])
    api = MagicMock(spec=OpenBaoClient)
    with pytest.raises(OpenBaoError):
        reconcile_mcp(api, (identity,))
    assert api.mock_calls == []


def test_cluster_selects_only_the_fixed_nonsecret_catalog_and_redacts_errors() -> None:
    cluster = Cluster.__new__(Cluster)
    cluster.core = MagicMock()
    read = cluster.core.read_namespaced_config_map
    read.side_effect = ApiException(status=404)
    assert cluster.studio_mcp_ids() is None
    read.assert_called_once_with("infra-agentgateway-mcp-catalog", "infra-agentgateway")
    read.side_effect = ApiException(status=403, reason="private response")
    with pytest.raises(ClusterError, match="HTTP 403") as error:
        cluster.studio_mcp_ids()
    assert "private response" not in str(error.value)
    read.side_effect = None
    with patch.object(cluster, "contextforge_admin_email", return_value="recovery@example.com"):
        for data in ({}, {"studioSetup": "false", "studio.json": "invalid"}):
            read.return_value = SimpleNamespace(data=data)
            assert cluster.studio_mcp_ids() is None
        for data in (
            {"studioSetup": "TRUE"},
            {"studioSetup": "true", "studio.json": "private invalid JSON"},
            {"studioSetup": "true", "studio.json": '[{"id": "private invalid ID"}]'},
        ):
            read.return_value = SimpleNamespace(data=data)
            with pytest.raises(ClusterError, match="Invalid Studio MCP") as error:
                cluster.studio_mcp_ids()
            assert "private" not in str(error.value)
        read.return_value = SimpleNamespace(
            data={
                "studioSetup": "true",
                "studio.json": json.dumps(
                    [
                        {
                            "id": "brave",
                            "credential": {"owner": "shared", "method": "upstream-env"},
                        },
                    ]
                ),
            }
        )
        assert cluster.studio_mcp_ids() == ("brave",)
    with (
        patch.object(cluster, "contextforge_admin_email", return_value=None),
        pytest.raises(ClusterError, match="requires selected ContextForge"),
    ):
        cluster.studio_mcp_ids()
    cluster.core.read_namespaced_secret.assert_not_called()


def test_initialization_is_create_only_and_reruns_retain_studio_changes(tmp_path, capsys) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    session.secrets["infra-agentgateway/external"] = StoredSecret({"braveApiKey": "legacy-key"})
    assert reconcile_mcp(api, None) == 0
    assert session.calls == []
    assert reconcile_mcp(api, ("brave",)) == 1
    path = "mcp/shared/brave"
    assert session.secrets[path] == StoredSecret(
        {"apiKey": "", "version": "initial", "operationId": "", "kvVersion": 1}
    )
    assert reconcile_mcp(api, ("brave",)) == 0
    operation = "afcc9856-17ca-4a30-96f6-016a63b46f83"
    values: dict[str, JsonValue] = {
        "apiKey": "studio-entered-test-key",
        "version": operation.replace("-", ""),
        "operationId": operation,
        "kvVersion": 2,
    }
    api.write_secret(path, values, 1)
    session.calls.clear()
    assert reconcile_mcp(api, ("brave",)) == 0
    assert session.secrets[path] == StoredSecret(values, 2)
    assert not any(
        call.method == "POST" and call.path.startswith("secret/") for call in session.calls
    )
    # A stale missing-record read must not overwrite a concurrent Studio write.
    with (
        patch.object(api, "read_secret", return_value=None),
        pytest.raises(OpenBaoError, match="HTTP 400"),
    ):
        reconcile_mcp(api, ("brave",))
    assert session.secrets[path] == StoredSecret(values, 2)
    # Studio removal writes a blank new version, rather than deleting the KV record.
    values = {**values, "apiKey": "", "kvVersion": 3}
    api.write_secret(path, values, 2)
    assert reconcile_mcp(api, ("brave",)) == 0
    assert session.secrets[path] == StoredSecret(values, 3)
    assert all(call.path != "secret/data/infra-agentgateway/external" for call in session.calls)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "changes",
    [
        {"apiKey": 1},
        {"apiKey": "unexpected-initial-key"},
        {"version": "legacy"},
        {"operationId": "not-a-uuid"},
        {"version": "a" * 32, "operationId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
        {"version": "a" * 32, "operationId": "not-a-uuid"},
        {"kvVersion": 2},
        {"kvVersion": True},
        {"extra": "value"},
    ],
)
def test_invalid_existing_records_fail_without_replacing_or_printing_values(
    tmp_path, changes
) -> None:
    session = FakeSession()
    values = {"apiKey": "", "version": "initial", "operationId": "", "kvVersion": 1, **changes}
    session.secrets["mcp/shared/brave"] = StoredSecret(values)
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    with pytest.raises(OpenBaoError, match="invalid schema") as error:
        reconcile_mcp(api, ("brave",))
    assert str(error.value) == "Existing MCP credential record has an invalid schema"
    assert all(call.method == "GET" for call in session.calls)
    assert session.secrets["mcp/shared/brave"] == StoredSecret(values)


def test_roles_grant_only_selected_shared_paths_and_shrink_on_reselection(tmp_path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    for identities in (("brave", "exa"), ("exa",), ()):
        session.calls.clear()
        reconcile_mcp(api, identities)
        for role, namespace, capabilities in (
            ("studio-mcp", "frontend-studio", ["read", "create", "update"]),
            ("mcp-shared-delivery", "infra-agentgateway", ["read"]),
        ):
            calls = {
                call.path: request_body(call) for call in session.calls if call.method == "POST"
            }
            policy = calls[f"sys/policies/acl/{role}"]["policy"]
            assert isinstance(policy, str)
            grants = {
                path: json.loads(rules)
                for path, rules in re.findall(
                    r'path "([^"]+)" \{\s*capabilities = (\[[^\]]+\])', policy
                )
            }
            expected = {
                f"secret/data/mcp/shared/{identity}": capabilities for identity in identities
            } or {"secret/data/mcp/shared/*": ["deny"]}
            if role == "mcp-shared-delivery":
                expected.update(
                    {
                        "auth/token/lookup-self": ["read"],
                        "auth/token/revoke-self": ["update"],
                    }
                )
            assert grants == expected
            assert calls[f"auth/kubernetes/role/{role}"] == {
                "bound_service_account_names": [role],
                "bound_service_account_namespaces": [namespace],
                "audience": "openbao",
                "token_policies": [role],
                "token_no_default_policy": True,
                "token_ttl": "5m",
                "token_max_ttl": "5m",
            }
    assert set(session.secrets) == {"mcp/shared/brave", "mcp/shared/exa"}
