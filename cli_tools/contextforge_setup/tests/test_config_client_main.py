import getpass
import json
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

from contextforge_setup.client import Client
from contextforge_setup.config import SetupError, load_config
from contextforge_setup.main import main

MANIFEST = {
    "origin": "https://contextforge.example.com",
    "team_id": "00000000000000000000000000000001",
    "global_role_id": "global-empty",
    "team_role_id": "team-invoke",
    "accounts": [
        {
            "email": "caller@example.com",
            "issuer": "https://identity.example.com/realms/platform",
            "subject": "principal-1",
            "kind": "service",
            "enabled": True,
        }
    ],
}


def test_local_http_requires_explicit_literal_loopback_approval(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({**MANIFEST, "origin": "http://127.0.0.1:8000"}))
    with pytest.raises(SetupError, match="HTTPS"):
        load_config(path)
    assert load_config(path, allow_loopback_http=True).origin == "http://127.0.0.1:8000"
    for value in [
        "http://contextforge.example.com",
        "https://secret@contextforge.example.com",
        "https://contextforge.example.com/path",
        "https://contextforge.example.com?token=secret",
    ]:
        path.write_text(json.dumps({**MANIFEST, "origin": value}))
        with pytest.raises(SetupError):
            load_config(path, allow_loopback_http=True)


@pytest.mark.parametrize(
    "change",
    [
        "personal",
        "duplicate",
        "password",
        "uppercase",
        "subject-duplicate",
        "disabled",
        "missing-email",
    ],
)
def test_unsupported_or_conflicting_input_rejected_before_credentials_or_api(
    tmp_path, monkeypatch, change
):
    manifest = json.loads(json.dumps(MANIFEST))
    if change == "personal":
        manifest["integrations"] = [{"authentication_model": "individual-authentication"}]
    elif change == "duplicate":
        manifest["accounts"] *= 2
    elif change == "password":
        manifest["accounts"][0]["password"] = "never-print-me"
    elif change == "uppercase":
        manifest["accounts"][0]["email"] = "Caller@example.com"
    elif change == "disabled":
        manifest["accounts"][0]["enabled"] = False
    elif change == "missing-email":
        manifest["accounts"][0]["email"] = ""
    else:
        manifest["accounts"].append({**manifest["accounts"][0], "email": "different@example.com"})
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        sys, "argv", ["contextforge-setup", "reconcile-accounts", "--config", str(path), "--apply"]
    )
    with (
        patch("contextforge_setup.main.Client") as api,
        patch("contextforge_setup.main.getpass.getpass") as prompt,
    ):
        assert main() == 1
    api.assert_not_called()
    prompt.assert_not_called()


def test_validate_only_never_contacts_api(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(MANIFEST))
    monkeypatch.setattr(
        sys, "argv", ["contextforge-setup", "reconcile-accounts", "--config", str(path)]
    )
    with patch("contextforge_setup.main.Client") as api:
        assert main() == 0
    api.assert_not_called()


def test_http_tls_proxy_redirect_and_error_redaction(tmp_path):
    api = Client("https://contextforge.example.com", tmp_path / "private-ca.pem")
    assert api.session.verify == str(tmp_path / "private-ca.pem")
    assert api.session.trust_env is False
    api.authenticate("secret-native-token")
    response = MagicMock(status_code=302, content=b'{"secret":"secret-native-token"}')
    with patch.object(api.session, "request", return_value=response) as request:
        with pytest.raises(SetupError) as caught:
            api.request("GET", "/v1/auth/email/me")
        assert "secret-native-token" not in str(caught.value)
        assert request.call_args.kwargs["allow_redirects"] is False
    with patch.object(
        api.session, "request", side_effect=requests.ConnectionError("secret-native-token")
    ):
        with pytest.raises(SetupError) as caught:
            api.request("POST", "/v1/auth/email/login", {"password": "secret-native-token"})
        assert "secret-native-token" not in str(caught.value)
        assert "outcome may be unknown" in str(caught.value)
    api.close()
    assert "Authorization" not in api.session.headers


def test_login_keeps_native_token_private_and_only_get_404_is_missing():
    api = Client("https://contextforge.example.com")
    response = MagicMock(status_code=200, content=b'{"access_token":"native-secret"}')
    with patch.object(api.session, "request", return_value=response):
        api.login("operator@example.com", "hidden-password")
        assert api.session.headers["Authorization"] == "Bearer native-secret"
        with pytest.raises(SetupError):
            api.request("POST", "/auth/email/admin/users", {}, expected_status=201)
    response.status_code = 404
    with patch.object(api.session, "request", return_value=response):
        assert api.request("GET", "/missing", missing=True) is None
        with pytest.raises(SetupError):
            api.request("POST", "/missing", missing=True)
    api.close()


@pytest.mark.parametrize("login", [False, True])
def test_apply_uses_private_credentials_closes_client_and_prints_no_secrets(
    tmp_path, monkeypatch, capsys, login
):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(MANIFEST))
    argv = ["contextforge-setup", "reconcile-accounts", "--config", str(path), "--apply"]
    for name in [
        "CONTEXTFORGE_ADMIN_TOKEN",
        "CONTEXTFORGE_ADMIN_EMAIL",
        "CONTEXTFORGE_ADMIN_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)
    if login:
        argv.append("--login")
        monkeypatch.setenv("CONTEXTFORGE_ADMIN_EMAIL", "operator@example.com")
        monkeypatch.setenv("CONTEXTFORGE_ADMIN_PASSWORD", "secret-password")
    else:
        monkeypatch.setenv("CONTEXTFORGE_ADMIN_TOKEN", "secret-token")
    monkeypatch.setattr(sys, "argv", argv)
    with (
        patch("contextforge_setup.main.Client") as client,
        patch("contextforge_setup.main.reconcile_accounts") as reconcile,
    ):
        assert main() == 0
        reconcile.assert_called_once()
        if login:
            client.return_value.login.assert_called_once_with(
                "operator@example.com", "secret-password"
            )
        else:
            client.return_value.authenticate.assert_called_once_with("secret-token")
        client.return_value.close.assert_called_once()
    output = capsys.readouterr()
    assert "secret-password" not in output.out + output.err
    assert "secret-token" not in output.out + output.err


def test_unsupported_cli_secret_argument_is_not_echoed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["contextforge-setup", "--token", "secret-never-print"])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    assert "secret-never-print" not in capsys.readouterr().err


def test_hidden_input_never_falls_back_to_echo(tmp_path, monkeypatch, capsys):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(MANIFEST))
    monkeypatch.delenv("CONTEXTFORGE_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["contextforge-setup", "reconcile-accounts", "--config", str(path), "--apply"]
    )
    with (
        patch("contextforge_setup.main.Client") as client,
        patch(
            "contextforge_setup.main.getpass.getpass",
            side_effect=getpass.GetPassWarning("echo fallback"),
        ),
        patch("contextforge_setup.main.reconcile_accounts") as reconcile,
    ):
        assert main() == 1
    reconcile.assert_not_called()
    client.return_value.close.assert_called_once()
    assert "Hidden input is unavailable" in capsys.readouterr().err


def test_native_jwt_length_and_header_injection_safety():
    api = Client("https://contextforge.example.com")
    api.authenticate("a" * 1200)
    assert len(api.session.headers["Authorization"]) == 1207
    with pytest.raises(SetupError, match="token format"):
        api.authenticate("token\r\nsecret-header")
    api.close()
