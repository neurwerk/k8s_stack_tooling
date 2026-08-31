"""Tests for the OIDC client init job."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from k8s_stack_tooling.keycloak import upsert_oidc_client


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the required OIDC job environment."""
    values = {
        "KC_INTERNAL_URL": "http://keycloak",
        "KC_ADMIN_USER": "admin",
        "KC_ADMIN_PASSWORD": "password",
        "KC_REALM": "test",
        "KC_CLIENT_ID": "client",
        "KC_REDIRECT_URI": "https://app/callback",
        "KC_WEB_ORIGIN": "https://app",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    for key in (
        "KC_CLIENT_SECRET",
        "KC_REDIRECT_URIS",
        "KC_WEB_ORIGINS",
        "KC_CLIENT_AUDIENCES",
        "KC_CLIENT_ROLES",
        "KC_SERVICE_ACCOUNT_ROLES",
        "KC_SERVICE_ACCOUNT_REALM_ROLES",
    ):
        monkeypatch.delenv(key, raising=False)


@contextmanager
def _patched_job() -> Iterator[dict[str, Mock]]:
    with (
        patch.object(upsert_oidc_client, "wait_for_service") as wait,
        patch.object(upsert_oidc_client, "get_admin_token", return_value="token") as token,
        patch.object(upsert_oidc_client, "upsert_client", return_value="uuid") as upsert,
        patch.object(upsert_oidc_client, "upsert_client_roles") as upsert_roles,
        patch.object(upsert_oidc_client, "get_client_secret") as get_secret,
    ):
        yield {
            "wait": wait,
            "token": token,
            "upsert": upsert,
            "upsert_roles": upsert_roles,
            "get_secret": get_secret,
        }


def test_upserts_without_secret_for_public_or_jwks_client(oidc_env: None) -> None:
    """Allow clients that do not consume a client secret to omit one."""
    with _patched_job() as mocks:
        upsert_oidc_client.main()

    mocks["upsert"].assert_called_once_with(
        "http://keycloak",
        "token",
        "test",
        "client",
        ["https://app/callback"],
        ["https://app"],
        "confidential",
        None,
    )
    mocks["get_secret"].assert_not_called()


def test_uses_plural_url_lists_when_present(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prefer explicit URL arrays while singular variables remain available."""
    monkeypatch.setenv(
        "KC_REDIRECT_URIS",
        '["https://app/callback", "https://admin.app/api/admin/callback"]',
    )
    monkeypatch.setenv("KC_WEB_ORIGINS", '["https://app", "https://admin.app"]')

    with _patched_job() as mocks:
        upsert_oidc_client.main()

    args = mocks["upsert"].call_args.args
    assert args[4] == ["https://app/callback", "https://admin.app/api/admin/callback"]
    assert args[5] == ["https://app", "https://admin.app"]


@pytest.mark.parametrize(
    "raw_urls",
    [
        "not-json",
        "{}",
        "[]",
        '["https://app", "https://app"]',
        '["http://app/callback"]',
        '["/relative/callback"]',
        '["https://app", 1]',
    ],
)
def test_rejects_invalid_plural_url_lists(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch, raw_urls: str
) -> None:
    """Reject malformed, empty, duplicate, insecure, relative, or non-string entries."""
    monkeypatch.setenv("KC_REDIRECT_URIS", raw_urls)

    with pytest.raises(SystemExit):
        upsert_oidc_client._read_url_list("KC_REDIRECT_URIS", "KC_REDIRECT_URI")


def test_allows_empty_singular_url_for_legacy_non_browser_clients(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep service-only clients that intentionally use no browser URL working."""
    monkeypatch.setenv("KC_REDIRECT_URI", "")

    assert upsert_oidc_client._read_url_list("KC_REDIRECT_URIS", "KC_REDIRECT_URI") == []


def test_upserts_and_verifies_desired_secret(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set and verify a confidential-client secret supplied by ESO."""
    monkeypatch.setenv("KC_CLIENT_SECRET", "desired-secret")
    with _patched_job() as mocks:
        mocks["get_secret"].return_value = "desired-secret"
        upsert_oidc_client.main()

    assert mocks["upsert"].call_args.args[-1] == "desired-secret"
    mocks["get_secret"].assert_called_once_with("http://keycloak", "token", "test", "uuid")


def test_fails_when_keycloak_secret_does_not_match(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when Keycloak did not persist the desired secret."""
    monkeypatch.setenv("KC_CLIENT_SECRET", "desired-secret")
    with (
        _patched_job() as mocks,
        patch.object(upsert_oidc_client, "log") as log,
        pytest.raises(SystemExit),
    ):
        mocks["get_secret"].return_value = "different-secret"
        upsert_oidc_client.main()

    mocks["get_secret"].assert_called_once()
    assert "desired-secret" not in " ".join(str(call) for call in log.call_args_list)
    assert "different-secret" not in " ".join(str(call) for call in log.call_args_list)


def test_keeps_audience_and_service_account_role_reconciliation(
    oidc_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile mappers and both service-account role types after client upsert."""
    monkeypatch.setenv("KC_CLIENT_AUDIENCES", '["api"]')
    monkeypatch.setenv(
        "KC_SERVICE_ACCOUNT_ROLES",
        '[{"clientId": "realm-management", "roleName": "view-users"}]',
    )
    monkeypatch.setenv("KC_SERVICE_ACCOUNT_REALM_ROLES", '["api-key-admin"]')
    with (
        _patched_job(),
        patch.object(upsert_oidc_client, "add_audience_mapper") as add_audience,
        patch.object(upsert_oidc_client, "assign_service_account_role") as assign_role,
        patch.object(upsert_oidc_client, "assign_service_account_realm_roles") as assign_realms,
    ):
        upsert_oidc_client.main()

    add_audience.assert_called_once_with("http://keycloak", "token", "test", "uuid", "api")
    assign_role.assert_called_once_with(
        "http://keycloak",
        "token",
        "test",
        "uuid",
        "realm-management",
        "view-users",
    )
    assign_realms.assert_called_once_with(
        "http://keycloak", "token", "test", "uuid", ["api-key-admin"]
    )


def test_reconciles_declared_client_roles(oidc_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass stack-owned client roles to the client reconciliation helper."""
    monkeypatch.setenv("KC_CLIENT_ROLES", '["llm:invoke"]')
    with _patched_job() as mocks:
        upsert_oidc_client.main()

    mocks["upsert_roles"].assert_called_once_with(
        "http://keycloak", "token", "test", "uuid", "client", ["llm:invoke"]
    )
