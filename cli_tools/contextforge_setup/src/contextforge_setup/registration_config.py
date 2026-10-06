"""Non-secret, operator-approved mappings for the two supported MCP providers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from contextforge_setup.config import (
    Json,
    Object,
    SetupError,
    email,
    identifier,
    object_value,
    objects,
    origin,
    text,
)


@dataclass(frozen=True)
class Registration:
    """One platform integration, one owned native gateway and one scoped server."""

    id: str
    provider: str
    authentication_model: str
    upstream_url: str
    transport: str
    gateway_id: str | None
    server_id: str
    approved_tools: tuple[str, ...]
    permission: str
    public_route: str
    pii_policy: str
    content_trace: bool

    @property
    def alias(self) -> str:
        """Use a stable native name when the gateway API assigns its own ID."""
        return f"neurwerk-contextforge-{self.id}"

    @property
    def marker(self) -> str:
        """Bind native description ownership to provider, platform ID and auth model."""
        return f"neurwerk-contextforge/{self.provider}/{self.id}/{self.authentication_model}"


@dataclass(frozen=True)
class RegistrationConfig:
    """An approved native registration administrator and fixed controlled team."""

    origin: str
    team_id: str
    owner_email: str
    registrations: tuple[Registration, ...]


def native_uuid(value: Json) -> str:
    """Require the exact lower-case hex UUID format emitted by native APIs."""
    result = text(value)
    if not re.fullmatch(r"[0-9a-f]{32}", result):
        raise SetupError("Use the exact native 32-character lower-case hex UUID")
    return result


def url_key(value: Json, *, exact_path: bool = False) -> str:
    """Compare addresses, preserving approved paths but conservatively detecting duplicates."""
    try:
        parts = urlsplit(text(value))
        host = parts.hostname
        if not host:
            raise SetupError("Upstream URL needs a host")
        host = "localhost" if host == "127.0.0.1" else host.lower()
        host = f"[{host}]" if ":" in host else host
        port = parts.port
    except ValueError:
        raise SetupError("Invalid upstream URL") from None
    if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    path = parts.path if exact_path else parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def load_registration_config(
    path: Path, *, allow_loopback_http: bool = False
) -> RegistrationConfig:
    """Reject blocked models, credentials and ambiguous mappings before any API access."""
    try:
        raw = object_value(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise SetupError("Cannot read valid registration JSON") from None
    if set(raw) != {"origin", "team_id", "owner_email", "registrations"}:
        raise SetupError(
            "Registration config requires only origin, team_id, owner_email and registrations"
        )
    rows = objects(raw["registrations"])
    if any(row.get("authentication_model") == "individual-authentication" for row in rows):
        raise SetupError(
            "Individual authentication is blocked by Base #424; no OAuth, PAT or header fallback"
        )
    registrations = tuple(_registration(row) for row in rows)
    if not registrations or len(registrations) > 2:
        raise SetupError("Select only Context7 and/or shared Brave, at most one of each")
    _unique(registrations)
    return RegistrationConfig(
        origin(raw["origin"], allow_loopback_http=allow_loopback_http),
        identifier(raw["team_id"]),
        email(raw["owner_email"]),
        registrations,
    )


def _registration(raw: Object) -> Registration:
    if set(raw) != {
        "id",
        "provider",
        "authentication_model",
        "upstream_url",
        "transport",
        "gateway_id",
        "server_id",
        "approved_tools",
        "permission",
        "public_route",
        "pii_policy",
        "content_trace",
    }:
        raise SetupError(
            "Use only the documented non-secret registration fields; "
            "credentials and headers are forbidden"
        )
    platform_id = text(raw["id"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", platform_id):
        raise SetupError("Use the approved lower-case platform integration ID")
    provider, model = text(raw["provider"]), text(raw["authentication_model"])
    if {"context7": "no-authentication", "brave": "shared-authentication"}.get(provider) != model:
        raise SetupError(
            "Only no-authentication Context7 and shared-authentication Brave are supported"
        )
    transport = text(raw["transport"])
    if transport not in {"SSE", "STREAMABLEHTTP"}:
        raise SetupError("Declare the approved native SSE or STREAMABLEHTTP upstream protocol")
    return Registration(
        platform_id,
        provider,
        model,
        upstream_url(raw["upstream_url"]),
        transport,
        native_uuid(raw["gateway_id"]) if raw["gateway_id"] is not None else None,
        native_uuid(raw["server_id"]),
        _tools(raw["approved_tools"]),
        _permission(raw["permission"], platform_id),
        _route(raw["public_route"]),
        identifier(raw["pii_policy"]),
        _trace(raw["content_trace"]),
    )


def upstream_url(value: Json) -> str:
    """Require an approved HTTP(S) endpoint without credential-bearing URL components."""
    result = text(value)
    try:
        parts = urlsplit(result)
    except ValueError:
        raise SetupError("Invalid approved upstream URL") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "\\" in result
        or any(char.isspace() for char in result)
    ):
        raise SetupError(
            "Approved upstream URL must be HTTP(S), without credentials, query or fragment"
        )
    if "%" in parts.path or any(piece in {".", ".."} for piece in parts.path.split("/")):
        raise SetupError("Encoded or dot-segment upstream paths are unsupported")
    url_key(result)  # Validate the port and comparison form before API access.
    return result


def _tools(value: Json) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise SetupError("Explicitly approve between 1 and 100 upstream original tool names")
    result = tuple(text(item) for item in value)
    if len(set(result)) != len(result):
        raise SetupError("Duplicate approved tool name")
    return result


def _permission(value: Json, platform_id: str) -> str:
    result = text(value)
    if result != f"mcp:{platform_id}:invoke":
        raise SetupError("Permission must retain the exact mcp:<platform-id>:invoke mapping")
    return result


def _route(value: Json) -> str:
    result = text(value)
    if not re.fullmatch(r"/mcp/[a-z0-9/_-]+", result):
        raise SetupError(
            "Declare the approved public /mcp/ route without query, fragment or traversal"
        )
    return result


def _trace(value: Json) -> bool:
    if not isinstance(value, bool):
        raise SetupError("content_trace must be an explicit boolean")
    return value


def _unique(registrations: tuple[Registration, ...]) -> None:
    for values in [
        [row.id for row in registrations],
        [row.provider for row in registrations],
        [row.server_id for row in registrations],
        [row.public_route for row in registrations],
        [url_key(row.upstream_url) for row in registrations],
        [row.gateway_id for row in registrations if row.gateway_id is not None],
    ]:
        if len(set(values)) != len(values):
            raise SetupError(
                "Duplicate integration, provider, route, native ID or upstream URL "
                "across authentication models"
            )
