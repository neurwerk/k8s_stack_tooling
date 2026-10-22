from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession, request_body
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster

from openbao_stack_setup.catalog import namespace_policy
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import ClusterError
from openbao_stack_setup.credentials import plan_bootstrap_passwords
from openbao_stack_setup.main import SetupError, _converge_runtime, _set_provider
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, update_provider
from openbao_stack_setup.reconcile import (
    CURRENT_RECONCILIATION_VERSION,
    ReconciliationIdentity,
    reconcile_openbao,
)
from openbao_stack_setup.seed import _secret_operator_policy

SELECTED = "tailscale: {enabled: true, oauthSecret: operator-oauth}"
OAUTH = MANAGED_CREDENTIALS["tailscale-oauth"]
VALUES = {"client_id": "synthetic-client", "client_secret": "synthetic-secret"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("{}", False),
        ("tailscale: {}", False),
        ("tailscale: {enabled: false}", False),
        (SELECTED, True),
    ],
)
def test_selection_reads_only_canonical_product_values(text: str, expected: bool) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": text}
    )
    assert target.tailscale_enabled() is expected
    target.core.read_namespaced_config_map.assert_called_once_with(
        "tailscale-product-values", "tailscale"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[",
        "[]",
        "null",
        "tailscale: null",
        "tailscale: []",
        *[f"tailscale: {{enabled: {value}}}" for value in ('"true"', "1", "null", "[]", "{}")],
        "tailscale: {enabled: true}",
        SELECTED.replace("operator-oauth", "other"),
    ],
)
def test_selection_rejects_malformed_values(text: str) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": text}
    )
    with pytest.raises(ClusterError, match="Tailscale"):
        target.tailscale_enabled()


def test_only_missing_config_map_is_disabled() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = ApiException(status=404)
    assert target.tailscale_enabled() is False
    target.core.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ClusterError, match="HTTP 403"):
        target.tailscale_enabled()


def test_selected_catalog_is_scoped_and_never_generates_oauth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api))
    assert not any("tailscale" in str(request.body) for request in session.calls)
    assert "tailscale-oauth" not in PROVIDERS
    assert "tailscale" not in _secret_operator_policy()
    session.calls.clear()
    report = reconcile_openbao(api, identity, tailscale_enabled=True)
    assert report.previous_version == report.applied_version == CURRENT_RECONCILIATION_VERSION
    assert report.internal_fields_added == 0
    assert "tailscale/external" not in session.secrets
    role = next(r for r in session.calls if r.path == "auth/kubernetes/role/tailscale")
    assert request_body(role) == {
        "bound_service_account_names": ["tailscale-external-secrets"],
        "bound_service_account_namespaces": ["tailscale"],
        "audience": "openbao",
        "token_policies": ["tailscale"],
        "token_ttl": "10m",
        "token_max_ttl": "30m",
    }
    policy = namespace_policy("tailscale")
    assert 'path "secret/data/tailscale/external"' in policy
    assert 'path "secret/metadata/tailscale/external"' in policy
    assert policy.count('capabilities = ["read"]') == 2
    assert "*" not in policy
    writer = _secret_operator_policy(tailscale_enabled=True)
    assert (
        'path "secret/data/tailscale/external" {\n  capabilities = ["create", "read", "update"]'
        in writer
    )
    operator_request = next(
        r for r in session.calls if r.path == "sys/policies/acl/secret-operator"
    )
    assert "tailscale/external" in str(operator_request.body)
    assert capsys.readouterr() == ("", "")


def test_staged_convergence_waits_only_for_store() -> None:
    target = MagicMock()
    _converge_runtime(target, False)
    assert not any("tailscale" in str(c) for c in target.mock_calls)
    target.reset_mock()
    _converge_runtime(target, False, tailscale_enabled=True)
    target.ensure_secret_store_ready.assert_any_call("tailscale-openbao-secret-store", "tailscale")
    assert not any("operator-oauth" in str(c) for c in target.mock_calls)
    assert not any("tailscale" in str(c) for c in target.force_reconcile.mock_calls)


def test_deselection_removes_issued_token_policy_and_login_but_retains_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api), tailscale_enabled=True)
    update_provider(api, OAUTH, VALUES)
    saved = deepcopy(session.secrets)
    api.write_policy("unrelated", 'path "secret/data/unrelated/*" { capabilities = ["read"] }')
    api.write_kubernetes_role("unrelated")
    unrelated_policy = deepcopy(session.policies["unrelated"])
    unrelated_role = deepcopy(session.kubernetes_roles["unrelated"])
    selected_policy = deepcopy(session.policies["tailscale"])
    selected_role = deepcopy(session.kubernetes_roles["tailscale"])
    assert isinstance(selected_role, dict)
    policy_names = selected_role["token_policies"]
    assert isinstance(policy_names, list)
    issued_token_policy_names = tuple(policy_names)
    assert issued_token_policy_names == ("tailscale",)

    session.calls.clear()
    for _ in range(2):
        reconcile_openbao(api, identity)
        assert "tailscale" not in session.kubernetes_roles
        # This fake stores policy state, not an authorization engine. OpenBao
        # resolves a token's policy names against current policy definitions on
        # each request: removal eliminates this issued ESO token's read grant.
        # The standard default policy does not grant application KV reads.
        assert not any(name in session.policies for name in issued_token_policy_names)
        writer = session.policies["secret-operator"]
        assert isinstance(writer, dict)
        assert "tailscale/external" not in str(writer["policy"])
        assert session.secrets == saved
        assert session.policies["unrelated"] == unrelated_policy
        assert session.kubernetes_roles["unrelated"] == unrelated_role
    policy_delete = next(
        i
        for i, request in enumerate(session.calls)
        if request.method == "DELETE" and request.path == "sys/policies/acl/tailscale"
    )
    role_delete = next(
        i
        for i, request in enumerate(session.calls)
        if request.method == "DELETE" and request.path == "auth/kubernetes/role/tailscale"
    )
    assert policy_delete < role_delete
    assert all(
        request.path in ("sys/policies/acl/tailscale", "auth/kubernetes/role/tailscale")
        for request in session.calls
        if request.method == "DELETE"
    )

    reconcile_openbao(api, identity, tailscale_enabled=True)
    assert session.policies["tailscale"] == selected_policy
    assert session.kubernetes_roles["tailscale"] == selected_role
    assert session.secrets == saved
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["disabled", "missing", "forbidden", "identity"])
def test_credential_preflight_precedes_prompts_or_login(failure: str) -> None:
    target = MagicMock()
    target.tailscale_enabled.return_value = failure != "disabled"
    target.external_secret_exists.return_value = failure != "missing"
    if failure == "forbidden":
        target.tailscale_enabled.side_effect = ClusterError("HTTP 403")
    if failure == "identity":
        target.identity.side_effect = ClusterError("Identity mismatch")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch("openbao_stack_setup.main._confirm") as confirm,
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as openbao,
        pytest.raises((SetupError, ClusterError)),
    ):
        _set_provider("ctx", "client", "tailscale-oauth")
    confirm.assert_not_called()
    prompt.assert_not_called()
    openbao.assert_not_called()
    target.token_request.assert_not_called()


def test_credential_pair_uses_cas_and_preserves_siblings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "operator", tmp_path / "ca.crt", session)
    update_provider(api, OAUTH, VALUES)
    first = session.calls[-1]
    assert request_body(first)["options"] == {"cas": 0}
    api.write_secret("tailscale/external", {**VALUES, "sibling": "keep"}, 1)
    rotated = {"client_id": "rotated-client", "client_secret": "rotated-secret"}
    update_provider(api, OAUTH, rotated)
    assert request_body(session.calls[-1])["options"] == {"cas": 2}
    saved = deepcopy(session.secrets["tailscale/external"])
    assert saved.values == {**rotated, "sibling": "keep"}
    session.failure = ("POST", "secret/data/tailscale/external", 400, {"errors": ["CAS mismatch"]})
    with pytest.raises(OpenBaoError):
        update_provider(api, OAUTH, VALUES)
    assert session.secrets["tailscale/external"] == saved
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"client_id": "id"},
        {**VALUES, "client_secret": " "},
        {**VALUES, "extra": "unexpected"},
        {**VALUES, "client_id": "id\n"},
        {**VALUES, "client_secret": "secret\x00"},
        {**VALUES, "client_secret": "x" * 16385},
    ],
)
def test_invalid_credentials_are_never_written(tmp_path: Path, values: dict[str, str]) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "operator", tmp_path / "ca.crt", session)
    with pytest.raises(OpenBaoError):
        update_provider(api, OAUTH, values)
    assert session.calls == []


@pytest.mark.parametrize("failure", [None, "write", "revoke", "refresh"])
def test_credential_lifecycle_revokes_before_refresh_without_rollback(
    failure: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    target = MagicMock()
    unauthenticated = MagicMock()
    operator = MagicMock()
    events = MagicMock()
    events.attach_mock(operator, "operator")
    events.attach_mock(target, "cluster")
    update = MagicMock()
    events.attach_mock(update, "write")
    if failure == "write":
        update.side_effect = OpenBaoError("Write failed")
    elif failure == "revoke":
        operator.revoke_self.side_effect = OpenBaoError("Revocation failed")
    elif failure == "refresh":
        target.force_external_secret_refresh.side_effect = ClusterError("Refresh failed")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main._prompt_provider", return_value=VALUES.copy()),
        patch("openbao_stack_setup.main._openbao", return_value=nullcontext(unauthenticated)),
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=operator),
        patch("openbao_stack_setup.main.update_provider", update),
        pytest.raises((OpenBaoError, ClusterError)) if failure else nullcontext(),
    ):
        _set_provider("ctx", "client", "tailscale-oauth")
    update.assert_called_once_with(operator, OAUTH, VALUES)
    operator.revoke_self.assert_called_once_with()
    write_index = events.mock_calls.index(call.write(operator, OAUTH, VALUES))
    revoke_index = events.mock_calls.index(call.operator.revoke_self())
    assert write_index < revoke_index
    if failure in ("write", "revoke"):
        target.force_external_secret_refresh.assert_not_called()
    else:
        refresh = call.cluster.force_external_secret_refresh(
            "operator-oauth", "tailscale", "operator-oauth"
        )
        assert revoke_index < events.mock_calls.index(refresh)
    target.force_reconcile.assert_not_called()
    output = capsys.readouterr()
    assert not any(value in output.out + output.err for value in VALUES.values())
    if failure:
        assert "Updated OpenBao" not in output.out
