import copy
from typing import Any, override
from urllib.parse import unquote

import pytest

from contextforge_setup.accounts import INVOKE_PERMISSIONS, reconcile_accounts
from contextforge_setup.client import Client
from contextforge_setup.config import Account, Config, SetupError

TEAM = "00000000000000000000000000000001"
GLOBAL_ROLE = "global-empty"
TEAM_ROLE = "team-invoke"
ACCOUNT = Account(
    "caller@example.com", "https://identity.example.com/realms/platform", "subject-1", "service"
)
CONFIG = Config("https://contextforge.example.com", TEAM, GLOBAL_ROLE, TEAM_ROLE, (ACCOUNT,))


class Native(Client):
    """Stateful pinned-API fake; no HTTP or persistent storage."""

    def __init__(self):
        self.users = {}
        self.roles: dict[str, dict[str, Any]] = {
            GLOBAL_ROLE: {
                "id": GLOBAL_ROLE,
                "scope": "global",
                "permissions": [],
                "inherits_from": None,
                "is_active": True,
            },
            TEAM_ROLE: {
                "id": TEAM_ROLE,
                "scope": "team",
                "permissions": sorted(INVOKE_PERMISSIONS),
                "inherits_from": None,
                "is_active": True,
            },
        }
        self.assignments = {}
        self.members = [
            {
                "user_email": "service@example.com",
                "role": "owner",
                "team_id": TEAM,
                "is_active": True,
            }
        ]
        self.calls = []
        self.default_role = GLOBAL_ROLE
        self.team_role = TEAM_ROLE
        self.lose_response = None
        self.omit_assignment = False
        self.passwords = []

    @override
    def request(self, method, path, payload=None, *, missing=False, expected_status=None):
        data = copy.deepcopy(payload)
        if data and "password" in data:
            self.passwords.append(data.pop("password"))
        self.calls.append((method, path, data))
        if path == "/auth/email/me":
            return {"email": "service@example.com", "is_admin": False, "is_active": True}
        if path == f"/teams/{TEAM}":
            return {"id": TEAM, "is_active": True, "is_personal": False}
        if path.startswith("/rbac/roles/"):
            return copy.deepcopy(self.roles.get(path.rsplit("/", 1)[-1]))
        return self._account_request(method, path, data)

    def _account_request(self, method, path, data):
        if path == "/auth/email/admin/users":
            assert data is not None
            return self._create_account(data)
        if path.startswith("/auth/email/admin/users/"):
            user_email = unquote(path.rsplit("/", 1)[-1])
            if method == "PATCH":
                self.users[user_email].update(data)
            return copy.deepcopy(self.users.get(user_email))
        if path == f"/teams/{TEAM}/members":
            if method == "POST":
                assert data is not None
                self.members.append(
                    {
                        "user_email": data["email"],
                        "role": data["role"],
                        "team_id": TEAM,
                        "is_active": True,
                    }
                )
                if self.team_role:
                    self._grant(data["email"], self.team_role, "team", TEAM)
                return copy.deepcopy(self.members[-1])
            return copy.deepcopy(self.members)
        if path.startswith("/rbac/users/"):
            user_email = unquote(path.split("/")[3])
            if method == "POST":
                if not self.omit_assignment:
                    self._grant(user_email, data["role_id"], data["scope"], data["scope_id"])
                return {}
            return copy.deepcopy(self.assignments.get(user_email, []))
        raise AssertionError(path)

    def _create_account(self, data):
        row = {**data, "auth_provider": "local", "last_login": None, "email_verified": True}
        self.users[row["email"]] = row
        self.assignments[row["email"]] = []
        self._grant(row["email"], self.default_role, "global", None)
        if self.lose_response == "account":
            self.lose_response = None
            raise SetupError("Transport failed; outcome unknown")
        return copy.deepcopy(row)

    def _grant(self, user_email, role_id, scope, scope_id):
        self.assignments.setdefault(user_email, []).append(
            {
                "user_email": user_email,
                "role_id": role_id,
                "scope": scope,
                "scope_id": scope_id,
                "is_active": True,
                "expires_at": None,
            }
        )


def test_native_flow_disables_until_exact_grants_then_reuses_readonly():
    api = Native()
    reconcile_accounts(api, CONFIG)
    assert api.users[ACCOUNT.email]["is_active"] is True
    creates = [
        data
        for method, path, data in api.calls
        if method == "POST" and path == "/auth/email/admin/users"
    ]
    assert (
        len(creates) == 1 and creates[0]["is_active"] is False and creates[0]["is_admin"] is False
    )
    assert len(api.passwords[0]) >= 64 and all(
        "password" not in (data or {}) for _, _, data in api.calls
    )
    assert {row["role_id"] for row in api.assignments[ACCOUNT.email]} == {GLOBAL_ROLE, TEAM_ROLE}
    assert len(api.members) == 2
    # Unknown harmless profile changes are not overwritten or used as an ownership store.
    api.users[ACCOUNT.email]["full_name"] = "Operator supplied name"
    before = copy.deepcopy((api.users, api.roles, api.assignments, api.members))
    api.calls.clear()
    reconcile_accounts(api, CONFIG)
    assert all(method == "GET" for method, _, _ in api.calls)
    assert (api.users, api.roles, api.assignments, api.members) == before
    assert len(api.passwords) == 1


def test_lost_create_response_cannot_restore_partial_account_or_reset_password():
    api = Native()
    api.lose_response = "account"
    with pytest.raises(SetupError, match="outcome unknown"):
        reconcile_accounts(api, CONFIG)
    assert api.users[ACCOUNT.email]["is_active"] is False
    api.calls.clear()
    with pytest.raises(SetupError, match="explicit repair"):
        reconcile_accounts(api, CONFIG)
    assert api.users[ACCOUNT.email]["is_active"] is False
    assert all(method == "GET" for method, _, _ in api.calls)
    assert len(api.passwords) == 1


def test_default_broad_role_fails_with_new_account_still_disabled():
    api = Native()
    api.default_role = "platform_viewer"
    with pytest.raises(SetupError, match="Unexpected native grants"):
        reconcile_accounts(api, CONFIG)
    assert api.users[ACCOUNT.email]["is_active"] is False
    assert len(api.members) == 1
    assert all(method != "PATCH" for method, _, _ in api.calls)


@pytest.mark.parametrize(
    "change",
    [
        "admin",
        "disabled",
        "grants",
        "member-owner",
        "role-inheritance",
        "lost-role",
        "lost-membership",
        "unverified",
        "expired-grant",
    ],
)
def test_conflicting_state_is_never_overwritten_or_regranted(change):
    api = Native()
    reconcile_accounts(api, CONFIG)
    if change == "admin":
        api.users[ACCOUNT.email]["is_admin"] = True
    elif change == "disabled":
        api.users[ACCOUNT.email]["is_active"] = False
    elif change == "grants":
        api._grant(ACCOUNT.email, "unknown", "team", "other-team")
    elif change == "member-owner":
        api.members[1]["role"] = "owner"
    elif change == "role-inheritance":
        api.roles[TEAM_ROLE]["inherits_from"] = "admin"
    elif change == "lost-role":
        api.assignments[ACCOUNT.email] = []
    elif change == "lost-membership":
        api.members.pop()
    elif change == "unverified":
        api.users[ACCOUNT.email]["email_verified"] = False
    else:
        api.assignments[ACCOUNT.email][0]["expires_at"] = "2026-01-01T00:00:00Z"
    before = copy.deepcopy((api.users, api.roles, api.assignments, api.members))
    api.calls.clear()
    with pytest.raises(SetupError):
        reconcile_accounts(api, CONFIG)
    assert all(method == "GET" for method, _, _ in api.calls)
    assert (api.users, api.roles, api.assignments, api.members) == before


def test_all_existing_accounts_preflight_before_adding_other_accounts():
    api = Native()
    api.users[ACCOUNT.email] = {
        "email": ACCOUNT.email,
        "is_active": False,
        "is_admin": False,
        "email_verified": True,
    }
    other = Account("other@example.com", ACCOUNT.issuer, "other-subject", "user")
    config = Config(CONFIG.origin, TEAM, GLOBAL_ROLE, TEAM_ROLE, (other, ACCOUNT))
    with pytest.raises(SetupError, match="active state conflicts"):
        reconcile_accounts(api, config)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_missing_assignment_persistence_never_activates():
    api = Native()
    api.team_role = ""
    api.omit_assignment = True
    with pytest.raises(SetupError, match="not confirmed"):
        reconcile_accounts(api, CONFIG)
    assert api.users[ACCOUNT.email]["is_active"] is False


@pytest.mark.parametrize("role_id", [GLOBAL_ROLE, TEAM_ROLE])
def test_broad_or_wrong_role_definition_fails_before_creation(role_id):
    api = Native()
    api.roles[role_id]["permissions"].append("gateways.update")
    with pytest.raises(SetupError, match="permissions conflict"):
        reconcile_accounts(api, CONFIG)
    assert all(method == "GET" for method, _, _ in api.calls)
