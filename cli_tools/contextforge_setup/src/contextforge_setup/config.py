"""Validate a small, non-secret account manifest before any API access."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type Object = dict[str, Json]


class SetupError(Exception):
    """Safe operator error, never containing request or response payloads."""


def object_value(value: Json) -> Object:
    """Require a JSON object without printing invalid contents."""
    if not isinstance(value, dict):
        raise SetupError("Expected a JSON object")
    return value


def objects(value: Json) -> list[Object]:
    """Require an array of JSON objects."""
    if not isinstance(value, list):
        raise SetupError("Expected a JSON object array")
    return [object_value(item) for item in value]


def text(value: Json) -> str:
    """Require nonempty, bounded text without control characters."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SetupError("Expected bounded nonempty text")
    if value.strip() != value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SetupError("Whitespace or control characters are not allowed")
    return value


def email(value: Json) -> str:
    """Require an unambiguous lower-case ASCII native account email."""
    result = text(value)
    if len(result) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", result):
        raise SetupError("Use the authoritative lower-case ASCII Keycloak email")
    return result


def origin(value: Json, *, allow_loopback_http: bool) -> str:
    """Allow verified HTTPS or explicitly authorized literal-loopback HTTP only."""
    result = text(value).rstrip("/")
    try:
        parts = urlsplit(result)
    except ValueError:
        raise SetupError("Invalid ContextForge origin") from None
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise SetupError(
            "Configure an approved private ContextForge origin, without a path or credentials"
        )
    try:
        _ = parts.port
    except ValueError:
        raise SetupError("Invalid origin port") from None
    if parts.scheme == "https":
        return result
    if parts.scheme == "http" and allow_loopback_http and parts.hostname in {"127.0.0.1", "::1"}:
        return result
    raise SetupError(
        "HTTPS is required; local tunnels need --allow-loopback-http and a loopback IP"
    )


@dataclass(frozen=True)
class Account:
    """An operator-approved authoritative user or service principal."""

    email: str
    issuer: str
    subject: str
    kind: str


@dataclass(frozen=True)
class Config:
    """The same native team and prepared role IDs used by Studio onboarding."""

    origin: str
    team_id: str
    global_role_id: str
    team_role_id: str
    accounts: tuple[Account, ...]


def load_config(path: Path, *, allow_loopback_http: bool = False) -> Config:
    """Reject unsupported fields and conflicting identities before credentials or HTTP."""
    try:
        raw = object_value(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise SetupError("Cannot read valid account JSON") from None
    if set(raw) != {"origin", "team_id", "global_role_id", "team_role_id", "accounts"}:
        raise SetupError(
            "Account config requires origin, team_id, global_role_id, team_role_id and accounts; "
            "registrations are unsupported"
        )
    team_id = identifier(raw["team_id"])
    global_role_id = identifier(raw["global_role_id"])
    team_role_id = identifier(raw["team_role_id"])
    if global_role_id == team_role_id:
        raise SetupError("Global and team role IDs must differ")
    accounts = tuple(_account(item) for item in objects(raw["accounts"]))
    if not accounts or len(accounts) > 100:
        raise SetupError("Select between 1 and 100 authoritative accounts")
    if len({item.email for item in accounts}) != len(accounts) or len(
        {(item.issuer, item.subject) for item in accounts}
    ) != len(accounts):
        raise SetupError("Duplicate account email or authoritative identity")
    return Config(
        origin(raw["origin"], allow_loopback_http=allow_loopback_http),
        team_id,
        global_role_id,
        team_role_id,
        accounts,
    )


def identifier(value: Json) -> str:
    """Require a safe native identifier without changing its spelling."""
    result = text(value)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", result):
        raise SetupError("Use the approved native ID")
    return result


def _account(raw: Object) -> Account:
    if set(raw) != {"email", "issuer", "subject", "kind", "enabled"}:
        raise SetupError("Accounts require only email, issuer, subject, kind and enabled")
    if raw["enabled"] is not True:
        raise SetupError("Only enabled authoritative Keycloak principals may be prepared")
    issuer = text(raw["issuer"])
    try:
        parts = urlsplit(issuer)
    except ValueError:
        raise SetupError("Invalid Keycloak realm issuer") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise SetupError("Use the authoritative HTTPS Keycloak realm issuer")
    kind = text(raw["kind"])
    if kind not in {"user", "service"}:
        raise SetupError("Account kind must be user or service")
    return Account(email(raw["email"]), issuer, text(raw["subject"]), kind)
