"""Use native operator app registration and scoped server primitives, never personal tokens."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from contextforge_setup.client import Client
from contextforge_setup.config import Json, Object, SetupError, object_value, objects, text
from contextforge_setup.registration_config import (
    Registration,
    RegistrationConfig,
    native_uuid,
    require_supported_registration_apply,
    upstream_url,
    url_key,
)

_STOP = "Keep MCP routes blocked; inspect and repair privately before retrying"


def reconcile_registrations(
    api: Client,
    config: RegistrationConfig,
    *,
    update_owned_tools: bool = False,
    refresh_oauth: bool = False,
    resolve_secret: Callable[[Registration], str] | None = None,
) -> list[Object]:
    """Preflight every mapping, then create missing owned resources or explicitly update tools."""
    require_supported_registration_apply(config)
    if refresh_oauth and not update_owned_tools:
        raise SetupError("--refresh-oauth requires explicit --update-owned-tools")
    operator = api.registration_operator(config.owner_email, config.operator_authentication)
    if (
        operator.get("email") != config.owner_email
        or operator.get("is_active") is not True
        or operator.get("is_admin") is not True
    ):
        raise SetupError(
            "Use the configured active native registration administrator "
            "with unscoped catalog visibility"
        )
    team = object_value(api.request("GET", f"/teams/{config.team_id}"))
    if (
        team.get("id") != config.team_id
        or team.get("is_active") is not True
        or team.get("is_personal") is not False
    ):
        raise SetupError("Use the approved active non-personal controlled team")
    gateways = _catalog(api, "gateways")
    servers = _catalog(api, "servers")
    for spec in config.registrations:
        gateway = _find_gateway(gateways, spec, config)
        server = _find_server(servers, spec)
        if server is not None and gateway is None:
            raise SetupError(
                "Existing server lost its gateway binding; no automatic replacement; " + _STOP
            )
        if gateway is not None:
            tool_ids = _desired_tools(api, gateway, server, spec, config)
            if server is not None:
                _check_server(
                    api,
                    server,
                    gateway,
                    spec,
                    config,
                    tool_ids,
                    update_owned_tools=update_owned_tools,
                )
        if refresh_oauth and spec.oauth is not None and (gateway is None or server is None):
            raise SetupError("Create the pending OAuth gateway/server before consent and refresh")
    return [
        _prepare(
            api,
            spec,
            config,
            update_owned_tools=update_owned_tools,
            refresh_oauth=refresh_oauth,
            resolve_secret=resolve_secret,
        )
        for spec in config.registrations
    ]


def _catalog(api: Client, kind: str) -> list[Object]:
    return objects(api.request("GET", f"/{kind}?include_inactive=true&limit=0"))


def _find_gateway(
    rows: list[Object], spec: Registration, config: RegistrationConfig
) -> Object | None:
    matches = [
        row
        for row in rows
        if row.get("name") == spec.alias
        or (spec.gateway_id is not None and row.get("id") == spec.gateway_id)
    ]
    if len(matches) > 1:
        raise SetupError("Conflicting native gateway alias/ID; " + _STOP)
    gateway = matches[0] if matches else None
    if spec.gateway_id is not None and gateway is None:
        raise SetupError(
            "Configured gateway ID is absent; native create cannot choose its ID; " + _STOP
        )
    for row in rows:
        if url_key(row.get("url")) == url_key(spec.upstream_url) and row is not gateway:
            raise SetupError(
                "Upstream URL already belongs to another registration/authentication model; "
                + _STOP
            )
    if gateway is not None:
        _check_gateway(gateway, spec, config)
    return gateway


def _find_server(rows: list[Object], spec: Registration) -> Object | None:
    matches = [
        row for row in rows if row.get("id") == spec.server_id or row.get("name") == spec.alias
    ]
    if len(matches) > 1 or any(row.get("id") != spec.server_id for row in matches):
        raise SetupError("Conflicting native server alias/ID; " + _STOP)
    return matches[0] if matches else None


def _owned(row: Object, spec: Registration, config: RegistrationConfig, description: str) -> None:
    if (
        row.get("name") != spec.alias
        or row.get("description") != description
        or row.get("teamId") != config.team_id
        or row.get("ownerEmail") != config.owner_email
        or row.get("createdBy") != config.owner_email
        or row.get("visibility") != spec.visibility
    ):
        raise SetupError("Foreign or changed native ownership; no adoption or overwrite; " + _STOP)


def _empty(value: Json) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _check_gateway(row: Object, spec: Registration, config: RegistrationConfig) -> None:
    _owned(row, spec, config, spec.marker)
    native_uuid(row.get("id"))
    if spec.gateway_id is not None and row.get("id") != spec.gateway_id:
        raise SetupError("Resolved gateway ID differs from the approved mapping; " + _STOP)
    if row.get("status") != "active":
        raise SetupError(
            "Native gateway is not active; let registration finish, then retry the same alias; "
            + _STOP
        )
    if (
        url_key(upstream_url(row.get("url")), exact_path=True)
        != url_key(spec.upstream_url, exact_path=True)
        or row.get("transport") != spec.transport
        or row.get("gatewayMode") != "cache"
        or row.get("enabled") is not True
        or (spec.oauth is None and row.get("reachable") is not True)
    ):
        raise SetupError(
            "Native gateway address, transport, mode or enabled state conflicts; " + _STOP
        )
    if spec.oauth is not None:
        expected = _native_oauth(spec)
        actual = object_value(row.get("oauthConfig"))
        if (
            row.get("authType") != "oauth"
            or set(actual) != set(expected) | {"client_secret"}
            or any(actual.get(key) != value for key, value in expected.items())
            or not isinstance(actual.get("client_secret"), str)
            or not actual.get("client_secret")
        ):
            raise SetupError("Native OAuth operator app metadata conflicts; no overwrite; " + _STOP)
    elif row.get("authType") not in (None, "", "none") or not _empty(row.get("oauthConfig")):
        raise SetupError("Native authentication model conflicts; " + _STOP)
    if any(
        not _empty(row.get(key))
        for key in [
            "authValue",
            "authHeaders",
            "authHeadersUnmasked",
            "authUsername",
            "authPassword",
            "authPasswordUnmasked",
            "authToken",
            "authTokenUnmasked",
            "authHeaderKey",
            "authHeaderValue",
            "authHeaderValueUnmasked",
            "authQueryParamKey",
            "authQueryParamValueMasked",
            "passthroughHeaders",
            "clientCert",
            "clientKey",
            "identityPropagation",
        ]
    ):
        raise SetupError("Credentials, passthrough or identity injection conflict; " + _STOP)


def _check_tool(
    row: Object, gateway: Object, spec: Registration, config: RegistrationConfig
) -> str:
    tool_id = native_uuid(row.get("id"))
    if (
        row.get("gatewayId") != gateway["id"]
        or row.get("teamId") != config.team_id
        or row.get("ownerEmail") != config.owner_email
        or row.get("visibility") != spec.visibility
        or row.get("integrationType") != "MCP"
        or url_key(upstream_url(row.get("url")), exact_path=True)
        != url_key(spec.upstream_url, exact_path=True)
    ):
        raise SetupError("Foreign/cross-integration native tool association; " + _STOP)
    if (
        not _empty(row.get("headers"))
        or not _empty(row.get("auth"))
        or any(
            not _empty(row.get(key))
            for key in ["headerMapping", "pluginChainPre", "pluginChainPost"]
        )
    ):
        raise SetupError("Native tool headers/auth/plugins are not supported; " + _STOP)
    return tool_id


def _approved_tools(
    api: Client,
    gateway: Object,
    spec: Registration,
    config: RegistrationConfig,
) -> set[str]:
    rows = objects(
        api.request("GET", f"/tools?gateway_id={text(gateway['id'])}&include_inactive=true&limit=0")
    )
    selected: set[str] = set()
    for name in spec.approved_tools:
        matches = [row for row in rows if row.get("originalName") == name]
        if len(matches) != 1 or matches[0].get("enabled") is not True:
            raise SetupError(
                "Approved tool is missing, ambiguous or disabled; no automatic discovery refresh; "
                + _STOP
            )
        selected.add(_check_tool(matches[0], gateway, spec, config))
    if len(selected) != len(spec.approved_tools):
        raise SetupError("Approved names resolve to duplicate tool IDs; " + _STOP)
    return selected


def _desired_tools(
    api: Client,
    gateway: Object,
    server: Object | None,
    spec: Registration,
    config: RegistrationConfig,
    *,
    refresh_oauth: bool = False,
) -> set[str]:
    """Never grant OAuth-discovered tools until explicit operator qualification."""
    if spec.oauth is None or refresh_oauth:
        return _approved_tools(api, gateway, spec, config)
    if server is None:
        return set()
    actual = _membership(api, server, gateway, spec, config)
    if actual:
        return _approved_tools(api, gateway, spec, config)
    return set()


def _native_oauth(spec: Registration) -> Object:
    """Project only supported native app fields; PKCE is native and unconditional."""
    if spec.oauth is None:
        raise SetupError("Missing approved OAuth app metadata")
    return {
        key: value for key, value in spec.oauth.items() if key not in {"client_secret_ref", "pkce"}
    } | {"grant_type": "authorization_code"}


def _refresh_oauth_tools(
    api: Client, gateway: Object, spec: Registration, config: RegistrationConfig
) -> Object:
    """Require a fresh discovery timestamp, not stale tools after an empty native refresh."""
    result = object_value(
        api.request(
            "POST",
            f"/gateways/{text(gateway['id'])}/tools/refresh"
            "?include_resources=false&include_prompts=false",
        )
    )
    if (
        result.get("gatewayId") != gateway["id"]
        or result.get("success") is not True
        or result.get("error")
        or result.get("validationErrors")
    ):
        raise SetupError(
            "Native OAuth discovery failed; consent as the fixed operator and inspect privately"
        )
    refreshed = object_value(api.request("GET", f"/gateways/{text(gateway['id'])}"))
    _check_gateway(refreshed, spec, config)
    if not refreshed.get("lastRefreshAt") or refreshed.get("lastRefreshAt") == gateway.get(
        "lastRefreshAt"
    ):
        raise SetupError(
            "Native OAuth refresh returned no fresh catalog; approved tools remain unqualified"
        )
    return refreshed


def _membership(
    api: Client, server: Object, gateway: Object, spec: Registration, config: RegistrationConfig
) -> set[str]:
    rows = objects(api.request("GET", f"/servers/{spec.server_id}/tools?include_inactive=true"))
    full_ids = [_check_tool(row, gateway, spec, config) for row in rows]
    if len(full_ids) != len(set(full_ids)):
        raise SetupError("Duplicate native server tool association; " + _STOP)
    active_ids = {text(row.get("id")) for row in rows if row.get("enabled") is True}
    declared = server.get("associatedToolIds")
    if not isinstance(declared, list) or {text(item) for item in declared} != active_ids:
        raise SetupError("Native server membership views disagree or omit tools; " + _STOP)
    return set(full_ids)


def _check_server(
    api: Client,
    server: Object,
    gateway: Object,
    spec: Registration,
    config: RegistrationConfig,
    desired: set[str],
    *,
    update_owned_tools: bool,
) -> set[str]:
    _owned(server, spec, config, spec.marker + "/" + text(gateway["id"]))
    if (
        server.get("id") != spec.server_id
        or server.get("enabled") is not True
        or server.get("oauthEnabled") is not False
        or not _empty(server.get("oauthConfig"))
        or server.get("associatedResources") != []
        or server.get("associatedPrompts") != []
        or server.get("associatedA2aAgents") != []
    ):
        raise SetupError("Native scoped server state or non-tool associations conflict; " + _STOP)
    actual = _membership(api, server, gateway, spec, config)
    for kind in ("resources", "prompts"):
        if objects(api.request("GET", f"/servers/{spec.server_id}/{kind}?include_inactive=true")):
            raise SetupError("Non-tool server association is outside this slice; " + _STOP)
    if actual != desired and not update_owned_tools:
        raise SetupError(
            "Owned tool approval changed; explicit --update-owned-tools is required; " + _STOP
        )
    return actual


def _prepare(
    api: Client,
    spec: Registration,
    config: RegistrationConfig,
    *,
    update_owned_tools: bool,
    refresh_oauth: bool,
    resolve_secret: Callable[[Registration], str] | None,
) -> Object:
    gateway = _find_gateway(_catalog(api, "gateways"), spec, config)
    if gateway is None:
        payload: Object = {
            "name": spec.alias,
            "description": spec.marker,
            "url": spec.upstream_url,
            "transport": spec.transport,
            "auth_type": "none",
            "passthrough_headers": [],
            "gateway_mode": "cache",
            "team_id": config.team_id,
            "visibility": spec.visibility,
        }
        if spec.oauth is not None:
            if resolve_secret is None:
                raise SetupError("Resolve the approved operator app Secret reference privately")
            secret = resolve_secret(spec)
            if (
                not secret.strip()
                or len(secret) > 16384
                or any(ord(c) < 32 or ord(c) == 127 for c in secret)
            ):
                raise SetupError("Invalid operator app secret; value is hidden")
            payload["auth_type"] = "oauth"
            payload["oauth_config"] = _native_oauth(spec) | {"client_secret": secret}
            secret = ""
        try:
            api.request("POST", "/gateways", payload)
        finally:
            payload.clear()
        gateway = _find_gateway(_catalog(api, "gateways"), spec, config)
    if gateway is None:
        raise SetupError(
            "Native gateway creation was not confirmed; retry the same alias; " + _STOP
        )
    server = _find_server(_catalog(api, "servers"), spec)
    if refresh_oauth and spec.oauth is not None:
        gateway = _refresh_oauth_tools(api, gateway, spec, config)
    desired = _desired_tools(api, gateway, server, spec, config, refresh_oauth=refresh_oauth)
    desired_json = cast("list[Json]", sorted(desired))
    if server is None:
        api.request(
            "POST",
            "/servers",
            {
                "server": {
                    "id": spec.server_id,
                    "name": spec.alias,
                    "description": spec.marker + "/" + text(gateway["id"]),
                    "associated_tools": desired_json,
                    "associated_resources": [],
                    "associated_prompts": [],
                    "associated_a2a_agents": [],
                    "oauth_enabled": False,
                    "team_id": config.team_id,
                    "visibility": spec.visibility,
                },
                "team_id": config.team_id,
                "visibility": spec.visibility,
            },
            expected_status=201,
        )
    else:
        actual = _check_server(
            api, server, gateway, spec, config, desired, update_owned_tools=update_owned_tools
        )
        if actual != desired:
            api.request("PUT", f"/servers/{spec.server_id}", {"associated_tools": desired_json})
    server = object_value(api.request("GET", f"/servers/{spec.server_id}"))
    gateway = object_value(api.request("GET", f"/gateways/{text(gateway['id'])}"))
    _check_gateway(gateway, spec, config)
    desired = _desired_tools(api, gateway, server, spec, config, refresh_oauth=refresh_oauth)
    _check_server(api, server, gateway, spec, config, desired, update_owned_tools=False)
    desired_json = cast("list[Json]", sorted(desired))
    return {
        "id": spec.id,
        "provider": spec.provider,
        "authentication_model": spec.authentication_model,
        "upstream_url": spec.upstream_url,
        "transport": spec.transport,
        "gateway_id": gateway["id"],
        "server_id": spec.server_id,
        "native_mcp_path": f"/servers/{spec.server_id}/mcp",
        "tool_ids": desired_json,
        "permission": spec.permission,
        "public_route": spec.public_route,
        "pii_policy": spec.pii_policy,
        "content_trace": spec.content_trace,
        "state": "pending-consent" if spec.oauth is not None and not desired else "approved-tools",
    }
