"""Tests for the Keycloak required-actions email job."""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from k8s_stack_tooling.keycloak import send_user_actions_email


@pytest.fixture
def action_email_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "KC_INTERNAL_URL": "http://keycloak",
        "KC_PUBLIC_URL": "https://auth.example.test",
        "KC_ADMIN_USER": "admin",
        "KC_ADMIN_PASSWORD": "password",
        "KC_REALM": "test realm",
        "KC_INITIAL_USER_USERNAME": "initial-admin",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_waits_for_exact_public_issuer_before_sending(action_email_env: None) -> None:
    issuer = "https://auth.example.test/realms/test%20realm"
    with (
        patch.object(send_user_actions_email, "wait_for_service") as wait,
        patch.object(
            send_user_actions_email,
            "request",
            return_value=(200, {"issuer": issuer}),
        ),
        patch.object(send_user_actions_email, "get_admin_token", return_value="token") as token,
        patch.object(send_user_actions_email, "send_user_actions_email_api") as send,
    ):
        send_user_actions_email.main()

    assert wait.call_args_list == [
        call("http://keycloak:9000/health/ready", prefix="keycloak-action-email"),
        call(
            f"{issuer}/.well-known/openid-configuration",
            prefix="keycloak-public-issuer",
        ),
    ]
    token.assert_called_once_with("http://keycloak", "admin", "password")
    send.assert_called_once_with("http://keycloak", "token", "test realm", "initial-admin", 1800)


def test_refuses_unexpected_public_issuer(action_email_env: None) -> None:
    with (
        patch.object(send_user_actions_email, "wait_for_service"),
        patch.object(
            send_user_actions_email,
            "request",
            return_value=(200, {"issuer": "https://wrong.example.test/realms/test"}),
        ),
        patch.object(send_user_actions_email, "get_admin_token") as token,
        pytest.raises(SystemExit),
    ):
        send_user_actions_email.main()

    token.assert_not_called()
