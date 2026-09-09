from unittest.mock import Mock

import httpx
import pytest

from keycloak_users import main
from keycloak_users.profile import SafeError


def answers(monkeypatch, values):
    sequence = iter(values)
    monkeypatch.setattr(main, "answer", lambda _: next(sequence))


@pytest.fixture
def normal_groups(realm):
    for index, path in enumerate(main.NORMAL_USER_GROUPS):
        group_id = f"normal-{index}"
        realm.groups[group_id] = {"id": group_id, "name": path.split("/")[-1], "path": path}
        realm.children["root"].append(group_id)
    realm.groups["dify"] = {
        "id": "dify",
        "name": "neurwerk-dify-users",
        "path": "/access/neurwerk-dify-users",
    }
    realm.children["root"].append("dify")


def test_cancel_before_create_is_harmless(monkeypatch, realm, normal_groups, capsys):
    answers(
        monkeypatch,
        [
            "New",
            "User",
            "new@example.com",
            "new.user",
            ["UPDATE_PASSWORD", "VERIFY_EMAIL"],
            [],
            True,
            False,
        ],
    )
    main.workflow(realm.accounts, "create", realm.accounts.catalog())
    assert not realm.writes
    output = capsys.readouterr().out
    assert "604800" in output
    assert "Selected groups: none" in output
    assert "default-roles-example" in output
    assert "no writes" in output


def test_ui_create_and_failure_reports_partial_progress(monkeypatch, realm, normal_groups, capsys):
    answers(
        monkeypatch,
        ["New", "User", "new@example.com", "new.user", ["UPDATE_PASSWORD"], ["app"], True, True],
    )
    realm.failure = "execute-actions-email"
    with pytest.raises(SafeError):
        main.workflow(realm.accounts, "create", realm.accounts.catalog())
    output = capsys.readouterr().out
    assert "CONFIGURE_TOTP" in output
    assert "user ID: new-id" in output
    assert "memberships accepted: 1" in output
    assert "outcome unknown" in output
    assert "SECRET" not in output


def test_email_only_retry_requires_identity_and_final_confirmation(monkeypatch, realm, user):
    realm.users = [{**user, "id": "existing", "enabled": True, "requiredActions": ["VERIFY_EMAIL"]}]
    answers(monkeypatch, [user["username"], user["username"], ["VERIFY_EMAIL"], True])
    main.workflow(realm.accounts, "send-actions-email", realm.accounts.catalog())
    assert len(realm.writes) == 1
    assert realm.writes[0].url.path.endswith("execute-actions-email")


def test_bad_identity_and_email_never_write(monkeypatch, realm, user):
    answers(monkeypatch, ["New", "User", "not-an-email"])
    with pytest.raises(SafeError, match="email"):
        main.identity(realm.accounts, "create")
    realm.users = [{**user, "id": "existing"}]
    answers(monkeypatch, [user["username"], "wrong"])
    with pytest.raises(SafeError, match="confirmation"):
        main.identity(realm.accounts, "resume")
    assert not realm.writes


def test_questions_default_no_and_interrupt(monkeypatch):
    confirm = Mock(return_value=Mock(unsafe_ask=Mock(return_value=False)))
    monkeypatch.setattr(main.questionary, "confirm", confirm)
    assert not main.confirm("Write?")
    confirm.assert_called_once_with("Write:", default=False)
    with pytest.raises(KeyboardInterrupt):
        main.answer(Mock(unsafe_ask=Mock(return_value=None)))
    assert main.checklist("Groups", {}) == []


def test_setup_exact_instructions_and_manual_cancel(monkeypatch, capsys):
    answers(
        monkeypatch,
        ["test", "https://identity.example", "realm", "keycloak-users", "12345", "", False],
    )
    assert main.setup_profile() is None
    output = capsys.readouterr().out
    assert "http://127.0.0.1:12345/callback" in output
    assert "S256" in output
    assert "query-groups" in output
    assert "NOT CLI-constrained" in output
    assert "Navigation: Settings > Capability config" in output
    assert "PKCE method: S256" in output
    assert "Full Scope Allowed: ON" in output
    assert "Optional hardening: Full Scope Allowed: OFF" in output


@pytest.mark.parametrize("approved", [True, False])
def test_setup_saves_only_after_auth_reads_and_approval(
    monkeypatch, realm, normal_groups, approved
):
    login = Mock()
    save = Mock()
    monkeypatch.setattr(main, "setup_profile", lambda: realm.profile)
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", login)
    monkeypatch.setattr(main, "save_profile", save)

    def confirm(_label):
        login.assert_called_once()
        assert any(request.url.path.endswith("/users") for request in realm.requests)
        save.assert_not_called()
        return approved

    monkeypatch.setattr(main, "confirm", confirm)
    main.run("setup", None)
    assert save.call_count == int(approved)
    assert not realm.writes
    assert realm.session.tokens == {}


def test_failed_setup_auth_never_saves(monkeypatch, realm):
    monkeypatch.setattr(main, "setup_profile", lambda: realm.profile)
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", Mock(side_effect=SafeError("Auth failed")))
    save = Mock()
    monkeypatch.setattr(main, "save_profile", save)
    with pytest.raises(SafeError):
        main.run("setup", None)
    save.assert_not_called()
    assert realm.session.tokens == {}


def test_failed_setup_read_probe_never_saves(monkeypatch, realm):
    monkeypatch.setattr(main, "setup_profile", lambda: realm.profile)
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", Mock())
    realm.session.http.close()
    realm.session.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="SECRET"))
    )
    save = Mock()
    monkeypatch.setattr(main, "save_profile", save)
    with pytest.raises(SafeError, match="HTTP 403"):
        main.run("setup", None)
    save.assert_not_called()
    assert realm.session.tokens == {}


def test_doctor_is_read_only_and_warns(monkeypatch, realm, capsys):
    realm.components = [{"config": {"enabled": ["true"]}}]
    realm.realm["duplicateEmailsAllowed"] = True
    realm.actions = []
    monkeypatch.setattr(main, "load_profiles", lambda: {"test": realm.profile})
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", Mock())
    with pytest.raises(SafeError, match="Creation preflight: Blocked") as error:
        main.run("doctor", "test")
    output = capsys.readouterr().out
    assert "Token verification: Passed" in output
    assert "SMTP delivery has not been tested" in output
    assert "user-storage" in str(error.value)
    assert "duplicate emails" in str(error.value)
    assert "VERIFY_EMAIL" in str(error.value)
    assert not realm.writes


def test_unknown_profile_and_menu_cancellation(monkeypatch, realm):
    monkeypatch.setattr(main, "load_profiles", lambda: {"test": realm.profile})
    with pytest.raises(SafeError, match="Select"):
        main.run(None, "unknown")
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", Mock())
    answers(monkeypatch, ["cancel"])
    main.run(None, "test")
    assert not realm.writes


def test_first_run_offers_setup_without_browser_until_approved(monkeypatch, realm):
    monkeypatch.setattr(main, "load_profiles", lambda: {})
    setup = Mock(return_value=realm.profile)
    monkeypatch.setattr(main, "setup_profile", setup)
    answers(monkeypatch, [True])
    assert main.select_target(None, None) == ("setup", realm.profile)
    setup.assert_called_once()
    assert not realm.writes


def test_target_choice_and_exit_are_explicit(monkeypatch, realm):
    monkeypatch.setattr(main, "load_profiles", lambda: {"test": realm.profile})
    answers(monkeypatch, ["test", False])
    assert main.select_target("create", None) == ("create", realm.profile)
    session = Mock()
    monkeypatch.setattr(main, "BrowserSession", session)
    main.run(None, None)
    session.assert_not_called()


def test_create_without_email_retains_required_actions(monkeypatch, realm, normal_groups):
    answers(
        monkeypatch,
        ["New", "User", "new@example.com", "new.user", ["UPDATE_PASSWORD"], [], False, True],
    )
    main.workflow(realm.accounts, "create", realm.accounts.catalog())
    assert len(realm.writes) == 1
    assert realm.users[0]["requiredActions"] == ["UPDATE_PASSWORD"]


def test_normal_preset_selects_exact_groups_and_disables_dify(monkeypatch, realm, normal_groups):
    def checkbox(label, choices):
        assert label == "User groups:"
        checked = [choice for choice in choices if choice.checked]
        assert {choice.value for choice in checked} == {f"normal-{i}" for i in range(4)}
        assert all(": /access/" in choice.title for choice in choices)
        assert next(choice for choice in choices if choice.value == "dify").disabled
        return Mock(unsafe_ask=lambda: [choice.value for choice in checked])

    monkeypatch.setattr(main.questionary, "checkbox", checkbox)
    assert len(main.select_groups(realm.accounts.catalog(), normal_user=True)) == 4
    assert not realm.writes


def test_missing_normal_group_fails_without_creating_user(monkeypatch, realm):
    identity = Mock()
    monkeypatch.setattr(main, "identity", identity)
    with pytest.raises(
        SafeError, match=r"Required default group missing.*neurwerk-librechat-users"
    ):
        main.workflow(realm.accounts, "create", realm.accounts.catalog())
    identity.assert_not_called()
    assert not realm.writes


@pytest.mark.parametrize(
    "command", ["setup", "doctor", "create", "resume", "send-actions-email", None]
)
def test_missing_write_authority_stops_before_reads_profiles_or_user_prompts(
    monkeypatch, realm, command
):
    realm.session.accept_tokens(
        {
            "access_token": realm.signed_token(
                resource_access={"realm-management": {"roles": ["view-users", "view-realm"]}}
            ),
            "expires_in": 300,
            "token_type": "Bearer",
        }
    )
    monkeypatch.setattr(main, "select_target", lambda *_: (command, realm.profile))
    monkeypatch.setattr(main, "BrowserSession", lambda _: realm.session)
    monkeypatch.setattr(realm.session, "login", Mock())
    identity, save = Mock(), Mock()
    monkeypatch.setattr(main, "identity", identity)
    monkeypatch.setattr(main, "save_profile", save)
    with pytest.raises(SafeError, match="Permission preflight: Blocked") as error:
        main.run(command, "test")
    assert "realm-management -> manage-users" in str(error.value)
    assert "unassigned role from scope exclusion" in str(error.value)
    assert not realm.requests
    assert not realm.writes
    identity.assert_not_called()
    save.assert_not_called()


def test_smtp_missing_disables_default_email_and_blocks_email_retry_before_identity(
    monkeypatch, realm, normal_groups
):
    realm.realm.pop("smtpServer")
    catalog = realm.accounts.catalog()
    identity = Mock()
    monkeypatch.setattr(main, "identity", identity)
    with pytest.raises(SafeError, match="SMTP configuration"):
        main.workflow(realm.accounts, "send-actions-email", catalog)
    identity.assert_not_called()
    assert not realm.writes


def test_no_smtp_allows_creation_without_email(monkeypatch, realm, normal_groups):
    realm.realm.pop("smtpServer")
    answers(
        monkeypatch,
        ["New", "User", "new@example.com", "new.user", ["UPDATE_PASSWORD"], [], False, True],
    )
    confirm = Mock(return_value=Mock())
    monkeypatch.setattr(main.questionary, "confirm", confirm)
    main.workflow(realm.accounts, "create", realm.accounts.catalog())
    assert confirm.call_args_list[0].kwargs["default"] is False
    assert len(realm.writes) == 1


def test_resume_has_no_group_defaults_and_rejects_disabled_selection(
    monkeypatch, realm, normal_groups
):
    def checkbox(label, choices):
        assert not any(choice.checked for choice in choices)
        return Mock(unsafe_ask=lambda: ["dify"])

    monkeypatch.setattr(main.questionary, "checkbox", checkbox)
    with pytest.raises(SafeError, match="disabled group"):
        main.select_groups(realm.accounts.catalog(), normal_user=False)
    assert not realm.writes


def test_text_and_checkbox_prompt_labels_end_in_colon(monkeypatch):
    prompt = Mock(return_value=Mock(unsafe_ask=lambda: "Jane"))
    checkbox = Mock(return_value=Mock(unsafe_ask=lambda: []))
    monkeypatch.setattr(main.questionary, "text", prompt)
    monkeypatch.setattr(main.questionary, "checkbox", checkbox)
    assert main.prompt("First name") == "Jane"
    main.checklist("Required actions", {"VERIFY_EMAIL": "Verify email: VERIFY_EMAIL"})
    assert prompt.call_args.args[0] == "First name:"
    assert checkbox.call_args.args[0] == "Required actions:"


@pytest.mark.parametrize(
    "error,code",
    [
        (SafeError("Safe message"), 1),
        (httpx.ConnectError("SECRET"), 1),
        (KeyboardInterrupt(), 130),
        (ValueError("SECRET"), 1),
    ],
)
def test_cli_errors_are_sanitized(monkeypatch, capsys, error, code):
    monkeypatch.setattr(main.sys, "argv", ["keycloak-users", "doctor", "--profile", "test"])
    monkeypatch.setattr(main, "run", Mock(side_effect=error))
    assert main.main() == code
    assert "SECRET" not in capsys.readouterr().out


def test_cli_success(monkeypatch):
    monkeypatch.setattr(main.sys, "argv", ["keycloak-users", "setup"])
    monkeypatch.setattr(main, "run", Mock())
    assert main.main() == 0
