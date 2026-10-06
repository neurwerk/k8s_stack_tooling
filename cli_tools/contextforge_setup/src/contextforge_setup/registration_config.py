"""Non-secret, operator-approved mappings, independent of provider names."""

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
    visibility: str = "team"
    oauth: Object | None = None

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
    operator_authentication: str = "native-session"
    oauth_secret_env: tuple[tuple[str, str], ...] = ()


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
    path: Path, *, allow_loopback_http: bool = False, catalog: Path | None = None
) -> RegistrationConfig:
    """Validate non-secret source declarations and mappings before any API access."""
    try:
        raw = object_value(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raise SetupError("Cannot read valid registration JSON") from None
    fields = {"origin", "team_id", "owner_email"}
    if catalog is None:
        fields.add("registrations")
    if not fields <= set(raw) or set(raw) - fields - {
        "operator_authentication",
        "oauth_secret_env",
    }:
        raise SetupError(
            "Use origin, team_id, owner_email and either inline registrations or --catalog"
        )
    if catalog is None:
        rows = objects(raw["registrations"])
    else:
        try:
            rows = objects(json.loads(catalog.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise SetupError("Cannot read valid generated registrations catalog JSON") from None
    registrations = tuple(_registration(row) for row in rows)
    if not registrations or len(registrations) > 200:
        raise SetupError("Select between 1 and 200 approved MCP registrations")
    _unique(registrations)
    authentication = text(raw.get("operator_authentication", "native-session"))
    if authentication not in {"native-session", "trusted-proxy"}:
        raise SetupError("Use native-session or approved trusted-proxy operator authentication")
    secret_env = object_value(raw.get("oauth_secret_env", {}))
    individual_ids = {
        row.id for row in registrations if row.authentication_model == "individual-authentication"
    }
    if (
        not set(secret_env) <= individual_ids
        or any(
            not re.fullmatch(r"CONTEXTFORGE_OAUTH_[A-Z0-9_]+", text(name))
            for name in secret_env.values()
        )
        or len(set(secret_env.values())) != len(secret_env)
    ):
        raise SetupError(
            "Map individual integration IDs to distinct CONTEXTFORGE_OAUTH_* environment names"
        )
    return RegistrationConfig(
        origin(raw["origin"], allow_loopback_http=allow_loopback_http),
        identifier(raw["team_id"]),
        email(raw["owner_email"]),
        registrations,
        authentication,
        tuple((key, text(value)) for key, value in secret_env.items()),
    )


def _registration(raw: Object) -> Registration:
    required = {
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
    }
    if not required <= set(raw) or set(raw) - required - {"visibility", "oauth"}:
        raise SetupError(
            "Use only the documented non-secret registration fields; "
            "credentials and headers are forbidden"
        )
    platform_id = text(raw["id"])
    if len(platform_id) > 50 or not re.fullmatch(
        r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", platform_id
    ):
        raise SetupError("Use the approved lower-case platform integration ID")
    provider, model = text(raw["provider"]), text(raw["authentication_model"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", provider):
        raise SetupError("Use a generic lower-case provider identifier")
    if model not in {"no-authentication", "shared-authentication", "individual-authentication"}:
        raise SetupError("Use a documented MCP authentication model")
    visibility = text(raw.get("visibility", "team"))
    if visibility not in {"team", "public"}:
        raise SetupError("Use team or public native visibility behind private ingress")
    oauth = _oauth(raw["oauth"]) if "oauth" in raw else None
    if oauth is not None and model != "individual-authentication":
        raise SetupError("OAuth metadata requires individual-authentication")
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
        visibility,
        oauth,
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
    if not re.fullmatch(r"/mcp/[a-z0-9./_-]+", result) or any(
        part in {".", ".."} for part in result.split("/")
    ):
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
        [row.server_id for row in registrations],
        [row.public_route for row in registrations],
        [url_key(row.upstream_url) for row in registrations],
        [row.gateway_id for row in registrations if row.gateway_id is not None],
    ]:
        if len(set(values)) != len(values):
            raise SetupError(
                "Duplicate integration, route, native ID or upstream URL "
                "across authentication models"
            )


def _oauth(value: Json) -> Object:
    """Validate non-secret app metadata; credentials never enter the input file."""
    raw = object_value(value)
    if set(raw) != {
        "authorization_url",
        "token_url",
        "client_id",
        "redirect_uri",
        "scopes",
        "client_secret_ref",
        "pkce",
    }:
        raise SetupError("Use only documented OAuth metadata; plaintext secrets are forbidden")
    for field in ("authorization_url", "token_url", "redirect_uri"):
        if not upstream_url(raw[field]).startswith("https://"):
            raise SetupError("OAuth endpoints and approved callback must use HTTPS")
    text(raw["client_id"])
    if raw["pkce"] is not True:
        raise SetupError("OAuth metadata requires PKCE")
    scopes = raw["scopes"]
    if not isinstance(scopes, list):
        raise SetupError("OAuth scopes must be a list")
    for scope in scopes:
        text(scope)
    ref = object_value(raw["client_secret_ref"])
    if set(ref) != {"name", "key"}:
        raise SetupError("OAuth client_secret_ref requires only name and key")
    identifier(ref["name"])
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", text(ref["key"])):
        raise SetupError("Use the approved Kubernetes Secret key")
    return raw


def require_supported_registration_apply(config: RegistrationConfig) -> None:
    """Require an approved operator app and the fixed ESO reference before credentials."""
    for row in config.registrations:
        if row.authentication_model != "individual-authentication":
            continue
        if row.oauth is None or row.oauth["client_secret_ref"] != {
            "name": "contextforge-oauth-apps",
            "key": row.id,
        }:
            raise SetupError(
                "Individual-authentication apply requires OAuth app metadata and "
                "client_secret_ref name=contextforge-oauth-apps, key=<integration-id>"
            )
