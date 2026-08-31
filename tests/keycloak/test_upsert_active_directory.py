from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from k8s_stack_tooling.keycloak.upsert_active_directory import (
    config_from_environment,
    main,
)


def _enabled_environment() -> dict[str, str]:
    return {
        "KC_ACTIVE_DIRECTORY_ENABLED": "true",
        "KC_ACTIVE_DIRECTORY_CONNECTION_URL": "ldaps://directory.example.com:636",
        "KC_ACTIVE_DIRECTORY_USERS_DN": "OU=People,DC=example,DC=com",
        "KC_ACTIVE_DIRECTORY_GROUPS_DN": "OU=Access,DC=example,DC=com",
        "KC_ACTIVE_DIRECTORY_USERNAME_ATTRIBUTE": "sAMAccountName",
        "KC_ACTIVE_DIRECTORY_GROUP_NAMES": ('["neurwerk-studio-users", "neurwerk-studio-admins"]'),
        "KC_ACTIVE_DIRECTORY_BIND_DN": ("CN=Keycloak,OU=Service Accounts,DC=example,DC=com"),
        "KC_ACTIVE_DIRECTORY_BIND_CREDENTIAL": "do-not-log-this",
        "KC_ACTIVE_DIRECTORY_EMAIL_VERIFIED": "true",
    }


def test_disabled_environment_needs_no_other_active_directory_values() -> None:
    assert config_from_environment({"KC_ACTIVE_DIRECTORY_ENABLED": "false"}) is None
    assert config_from_environment({}) is None


def test_enabled_environment_builds_valid_config() -> None:
    config = config_from_environment(_enabled_environment())

    assert config is not None
    assert config.connection_url == "ldaps://directory.example.com:636"
    assert config.group_names == (
        "neurwerk-studio-users",
        "neurwerk-studio-admins",
    )
    assert config.email_verified is True
    assert "do-not-log-this" not in repr(config)


def test_enabled_environment_accepts_upn_bind_principal() -> None:
    environment = _enabled_environment()
    environment["KC_ACTIVE_DIRECTORY_BIND_DN"] = "keycloak-bind@example.com"

    config = config_from_environment(environment)

    assert config is not None
    assert config.bind_dn == "keycloak-bind@example.com"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("KC_ACTIVE_DIRECTORY_ENABLED", "yes", "must be true or false"),
        ("KC_ACTIVE_DIRECTORY_GROUP_NAMES", "studio-users", "JSON array"),
        ("KC_ACTIVE_DIRECTORY_GROUP_NAMES", '["ok", 1]', "JSON array"),
        ("KC_ACTIVE_DIRECTORY_EMAIL_VERIFIED", "false", "emailVerified"),
        ("KC_ACTIVE_DIRECTORY_BIND_CREDENTIAL", "", "is required"),
    ],
)
def test_invalid_environment_fails_without_printing_credentials(
    name: str, value: str, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    environment = _enabled_environment()
    environment[name] = value

    with pytest.raises(SystemExit):
        config_from_environment(environment)

    assert message in caplog.text
    assert "do-not-log-this" not in caplog.text


@patch("k8s_stack_tooling.keycloak.upsert_active_directory.reconcile_active_directory")
@patch("k8s_stack_tooling.keycloak.upsert_active_directory.requests.Session")
@patch("k8s_stack_tooling.keycloak.upsert_active_directory.get_admin_token")
@patch("k8s_stack_tooling.keycloak.upsert_active_directory.wait_for_service")
@patch("k8s_stack_tooling.keycloak.upsert_active_directory.os.environ")
def test_main_reconciles_disabled_provider_without_secret_environment(
    environment: MagicMock,
    wait_for_service: MagicMock,
    get_admin_token: MagicMock,
    session_factory: MagicMock,
    reconcile: MagicMock,
) -> None:
    environment.__getitem__.side_effect = {
        "KC_INTERNAL_URL": "https://keycloak.example.com",
        "KC_REALM": "platform",
        "KC_ADMIN_USER": "admin",
        "KC_ADMIN_PASSWORD": "admin-password",
    }.__getitem__
    environment.get.side_effect = {
        "KC_ACTIVE_DIRECTORY_ENABLED": "false",
        "KC_HEALTH_PORT": "9000",
    }.get
    session = MagicMock()
    session_factory.return_value.__enter__.return_value = session
    get_admin_token.return_value = "token"

    main()

    wait_for_service.assert_called_once_with(
        "https://keycloak.example.com:9000/health/ready",
        prefix="keycloak-active-directory",
    )
    get_admin_token.assert_called_once_with(
        "https://keycloak.example.com", "admin", "admin-password"
    )
    session.headers.update.assert_called_once_with({"Authorization": "Bearer token"})
    reconcile.assert_called_once_with("https://keycloak.example.com", "platform", session, None)
