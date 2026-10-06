"""Native account semantics shared with Studio PR66, without an ownership store."""

from __future__ import annotations

import secrets
from urllib.parse import quote

from contextforge_setup.client import Client
from contextforge_setup.config import (
    Account,
    Config,
    Object,
    SetupError,
    email,
    object_value,
    objects,
    text,
)

INVOKE_PERMISSIONS = {"servers.read", "servers.use", "tools.read", "tools.execute", "gateways.read"}
_REPAIR = (
    "Keep routes blocked; inspect with an authorized native operator. "
    "Existing disabled or partial accounts require explicit repair, not automatic regranting"
)


def reconcile_accounts(api: Client, config: Config) -> None:
    """Verify existing accounts read-only; prepare only accounts created in this run."""
    operator = object_value(api.request("GET", "/auth/email/me"))
    if operator.get("is_active") is not True:
        raise SetupError("Use an active native provisioning service principal")
    operator_email = email(operator.get("email"))
    team = object_value(api.request("GET", f"/teams/{config.team_id}"))
    if (
        team.get("id") != config.team_id
        or team.get("is_active") is not True
        or team.get("is_personal") is not False
    ):
        raise SetupError("Select the approved active non-personal controlled team")
    members = _members(api, config)
    if not any(
        row.get("user_email") == operator_email
        and row.get("role") == "owner"
        and row.get("team_id") == config.team_id
        and row.get("is_active") is True
        for row in members
    ):
        raise SetupError("The native provisioning service principal must own the approved team")
    _check_role(api, config.global_role_id, "global", set())
    _check_role(api, config.team_role_id, "team", INVOKE_PERMISSIONS)
    # No first-account mutations if a later existing identity conflicts.
    for account in config.accounts:
        _existing(api, config, account, members)
    for account in config.accounts:
        _prepare(api, config, account)


def _check_role(api: Client, role_id: str, scope: str, permissions: set[str]) -> None:
    role = object_value(api.request("GET", f"/rbac/roles/{role_id}"))
    actual = role.get("permissions")
    if (
        role.get("id") != role_id
        or role.get("scope") != scope
        or role.get("is_active") is not True
        or role.get("inherits_from") is not None
    ):
        raise SetupError("Native role ID, scope, state or inheritance conflicts; no overwrite")
    if not isinstance(actual, list) or {text(item) for item in actual} != permissions:
        raise SetupError(
            "Native role permissions conflict; global must be empty and team invocation-only"
        )


def _members(api: Client, config: Config) -> list[Object]:
    # Pinned native no-limit/no-cursor response returns all active memberships.
    return objects(api.request("GET", f"/teams/{config.team_id}/members"))


def _user_path(account: Account) -> str:
    return "/auth/email/admin/users/" + quote(account.email, safe="")


def _roles_path(account: Account) -> str:
    return "/rbac/users/" + quote(account.email, safe="") + "/roles"


def _check_user(value: Object, account: Account, *, active: bool) -> None:
    if (
        value.get("email") != account.email
        or value.get("is_admin") is not False
        or value.get("email_verified") is not True
    ):
        raise SetupError("Native account email, verification or admin state conflicts; " + _REPAIR)
    if value.get("is_active") is not active:
        raise SetupError("Native account active state conflicts; " + _REPAIR)


def _roles(api: Client, config: Config, account: Account) -> set[str]:
    expected = {
        config.global_role_id: ("global", None),
        config.team_role_id: ("team", config.team_id),
    }
    found: set[str] = set()
    for assignment in objects(api.request("GET", _roles_path(account) + "?active_only=false")):
        role_id = text(assignment.get("role_id"))
        if (
            role_id not in expected
            or assignment.get("user_email") != account.email
            or assignment.get("is_active") is not True
            or assignment.get("expires_at") is not None
            or (assignment.get("scope"), assignment.get("scope_id")) != expected[role_id]
        ):
            raise SetupError(
                "Unexpected native grants; verify configured defaults and personal-team settings; "
                + _REPAIR
            )
        if role_id in found:
            raise SetupError("Duplicate native grant; " + _REPAIR)
        found.add(role_id)
    return found


def _check_membership(members: list[Object], config: Config, account: Account) -> None:
    selected = [row for row in members if row.get("user_email") == account.email]
    if (
        len(selected) != 1
        or selected[0].get("team_id") != config.team_id
        or selected[0].get("role") != "member"
        or selected[0].get("is_active") is not True
    ):
        raise SetupError("Controlled membership is missing or conflicting; " + _REPAIR)


def _existing(api: Client, config: Config, account: Account, members: list[Object]) -> bool:
    value = api.request("GET", _user_path(account), missing=True)
    if value is None:
        return False
    _check_user(object_value(value), account, active=True)
    if _roles(api, config, account) != {config.global_role_id, config.team_role_id}:
        raise SetupError("Existing native roles are missing; " + _REPAIR)
    _check_membership(members, config, account)
    return True


def _prepare(api: Client, config: Config, account: Account) -> None:
    if _existing(api, config, account, _members(api, config)):
        return
    # Required native password is random, discarded, and never distributed as another login.
    payload: Object = {
        "email": account.email,
        "password": "Aa1!" + secrets.token_urlsafe(48),
        "is_admin": False,
        "is_active": False,
        "password_change_required": False,
    }
    try:
        value = object_value(
            api.request("POST", "/auth/email/admin/users", payload, expected_status=201)
        )
    finally:
        payload.clear()
    _check_user(value, account, active=False)
    # Refuse automatic unrelated/default grants before adding anything.
    _roles(api, config, account)
    api.request(
        "POST", f"/teams/{config.team_id}/members", {"email": account.email, "role": "member"}
    )
    _check_membership(_members(api, config), config, account)
    found = _roles(api, config, account)
    for role_id, scope, scope_id in [
        (config.global_role_id, "global", None),
        (config.team_role_id, "team", config.team_id),
    ]:
        if role_id not in found:
            api.request(
                "POST",
                _roles_path(account),
                {"role_id": role_id, "scope": scope, "scope_id": scope_id},
            )
    if _roles(api, config, account) != {config.global_role_id, config.team_role_id}:
        raise SetupError("Required native role preparation was not confirmed; " + _REPAIR)
    _check_membership(_members(api, config), config, account)
    _check_user(object_value(api.request("GET", _user_path(account))), account, active=False)
    _check_role(api, config.global_role_id, "global", set())
    _check_role(api, config.team_role_id, "team", INVOKE_PERMISSIONS)
    api.request("PATCH", _user_path(account), {"is_active": True})
    if not _existing(api, config, account, _members(api, config)):
        raise SetupError("Native activation was not confirmed; " + _REPAIR)
