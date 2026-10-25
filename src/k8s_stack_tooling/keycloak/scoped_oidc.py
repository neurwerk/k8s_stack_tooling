"""Converge a browser OIDC client with explicit realm and gateway role scopes."""

import contextlib
import json
import os
import re
import sys
from typing import Any
from urllib.parse import quote, urlsplit

from k8s_stack_tooling.api.http import request
from k8s_stack_tooling.api.keycloak import get_admin_token, get_client_secret

RESERVED_CLIENT_IDS = frozenset(
    {
        "agentgateway",
        "studio",
        "keycloak-api-key-bridge",
        "account",
        "account-console",
        "admin-cli",
        "broker",
        "realm-management",
        "security-admin-console",
    }
)


class ScopeDriftError(RuntimeError):
    """A successful read established a configuration mismatch, not an API outage."""


def browser_urls(plural: str, singular: str, *, origin: bool = False) -> list[str]:
    values = json.loads(os.environ[plural]) if plural in os.environ else [os.environ[singular]]
    if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
        raise ValueError("Invalid OIDC browser URL list")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate OIDC browser URL")
    for value in values:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "#" in value
            or any(c.isspace() or c in "*\\" for c in value)
            or (origin and (parsed.path or parsed.query))
        ):
            raise ValueError("Invalid OIDC browser URL")
        _ = parsed.port
    return values


def reconcile() -> None:
    base, realm = os.environ["KC_INTERNAL_URL"], os.environ["KC_REALM"]
    secret, client_id = os.environ["KC_CLIENT_SECRET"], os.environ["KC_CLIENT_ID"]
    if not secret:
        raise ValueError("Missing client secret")
    if client_id in RESERVED_CLIENT_IDS:
        raise ValueError("Reserved platform OIDC client ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", client_id):
        raise ValueError("Invalid OIDC client ID")
    role_names = json.loads(os.environ["KC_REALM_ROLE_SCOPE"])
    audiences = json.loads(os.environ.get("KC_CLIENT_AUDIENCES", "[]"))
    gateway_roles = json.loads(os.environ.get("KC_GATEWAY_ROLE_SCOPE", "[]"))
    for names, pattern, label in (
        (role_names, r"[a-z][a-z0-9:-]*", "realm-role scope"),
        (audiences, r"[a-z][a-z0-9-]*", "audiences"),
        (gateway_roles, r"[a-z][A-Za-z0-9:._/-]{0,255}", "gateway-role scope"),
    ):
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) or not re.fullmatch(pattern, name) for name in names)
            or len(set(names)) != len(names)
        ):
            raise ValueError("Invalid OIDC " + label)
    if not role_names:
        raise ValueError("Invalid OIDC realm-role scope")
    if gateway_roles and "agentgateway" not in audiences:
        raise ValueError("Gateway role scope requires the gateway audience")
    claim = os.environ["KC_ROLE_CLAIM"]
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?", claim):
        raise ValueError("Invalid OIDC role claim")
    if claim.split(".")[0] in {"sub", "aud", "azp", "iss", "exp", "iat", "nbf", "resource_access"}:
        raise ValueError("Reserved OIDC role claim")
    groups_claim = os.environ.get("KC_GROUPS_CLAIM", "")
    if groups_claim not in ("", "groups") or (groups_claim and claim.split(".")[0] == groups_claim):
        raise ValueError("Invalid OIDC groups claim")
    callbacks = browser_urls("KC_REDIRECT_URIS", "KC_REDIRECT_URI")
    origins = browser_urls("KC_WEB_ORIGINS", "KC_WEB_ORIGIN", origin=True)
    logout_uris = origins
    if "KC_POST_LOGOUT_REDIRECT_URIS" in os.environ:
        if "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS" not in os.environ:
            raise ValueError("Missing approved OIDC post-logout redirect URIs")
        logout_uris = browser_urls("KC_POST_LOGOUT_REDIRECT_URIS", "KC_POST_LOGOUT_REDIRECT_URIS")
        approved_logout_uris = browser_urls(
            "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS", "KC_APPROVED_POST_LOGOUT_REDIRECT_URIS"
        )
        if not set(logout_uris) <= set(approved_logout_uris):
            raise ValueError("Unapproved OIDC post-logout redirect URI")
    rotation = os.environ.get("KC_REFRESH_TOKEN_ROTATION_ENABLED", "false")
    if rotation not in ("true", "false"):
        raise ValueError("Invalid refresh token rotation setting")

    realm_mapper = {
        "name": client_id + "-effective-realm-roles",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-realm-role-mapper",
        "consentRequired": False,
        "config": {
            "claim.name": claim,
            "jsonType.label": "String",
            "multivalued": "true",
            "access.token.claim": "true",
            "id.token.claim": "true",
            "userinfo.token.claim": "true",
            "introspection.token.claim": "false",
            "usermodel.realmRoleMapping.rolePrefix": "",
        },
    }
    desired_mappers: list[dict[str, Any]] = [realm_mapper]
    if groups_claim:
        desired_mappers.append(
            {
                "name": client_id + "-full-path-groups",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-group-membership-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": groups_claim,
                    "full.path": "true",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "userinfo.token.claim": "true",
                    "introspection.token.claim": "false",
                },
            }
        )
    for audience in audiences:
        desired_mappers.append(
            {
                "name": "audience-" + audience,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": audience,
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                    "introspection.token.claim": "false",
                    "userinfo.token.claim": "false",
                },
            }
        )
    gateway_mapper_name = client_id + "-effective-gateway-roles"
    if gateway_roles:
        desired_mappers.append(
            {
                "name": gateway_mapper_name,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-client-role-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "resource_access.agentgateway.roles",
                    "usermodel.clientRoleMapping.clientId": "agentgateway",
                    "usermodel.clientRoleMapping.rolePrefix": "",
                    "jsonType.label": "String",
                    "multivalued": "true",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "userinfo.token.claim": "false",
                    "introspection.token.claim": "false",
                },
            }
        )
    desired_names = {mapper["name"] for mapper in desired_mappers}
    if len(desired_names) != len(desired_mappers):
        raise ValueError("Conflicting OIDC mapper names")

    token = get_admin_token(base, os.environ["KC_ADMIN_USER"], os.environ["KC_ADMIN_PASSWORD"])
    realm_url = f"{base}/admin/realms/{quote(realm, safe='')}"
    url = realm_url

    def api(path="", method="GET", body=None, root=None):
        status, data = request(
            (root or url) + path,
            method=method,
            body=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        if status not in (200, 201, 204):
            raise RuntimeError("Keycloak request failed")
        return data

    def lookup_client():
        clients = api("/clients?clientId=" + quote(client_id, safe=""), root=realm_url)
        if not isinstance(clients, list) or len(clients) > 1:
            raise RuntimeError("Ambiguous OIDC client lookup")
        if clients and (
            clients[0].get("clientId") != client_id
            or not isinstance(clients[0].get("id"), str)
            or not clients[0]["id"]
        ):
            raise RuntimeError("OIDC client lookup mismatch")
        return clients[0]["id"] if clients else None

    uuid = lookup_client()
    if uuid:
        url = f"{realm_url}/clients/{quote(uuid, safe='')}"
    expected = {
        "clientId": client_id,
        "enabled": False,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "implicitFlowEnabled": False,
        "fullScopeAllowed": False,
        "redirectUris": callbacks,
        "webOrigins": origins,
    }
    attributes = {
        "post.logout.redirect.uris": "##".join(logout_uris),
        "oauth2.device.authorization.grant.enabled": "false",
        "oidc.ciba.grant.enabled": "false",
        "client.use.refresh.tokens": "true",
        "client.refresh.token.rotation.enabled": rotation,
    }

    def disable():
        # A confirmed unsafe client must stop issuing tokens before correction.
        api(method="PUT", body={"enabled": False})
        if api().get("enabled") is not False:
            raise RuntimeError("Client disable readback mismatch")

    # This is already proven broader than the approved contract, so do not
    # leave it issuing unrestricted tokens if a later prerequisite read fails.
    if uuid and api().get("fullScopeAllowed") is True:
        disable()

    # Resolve prerequisites before any client mutation. An API outage here does
    # not disable a scoped client or create a partial new client. The exception
    # above is a confirmed unrestricted client, not a speculative outage guard.
    scopes = api("/client-scopes", root=realm_url)
    identity_scopes = {}
    for name in ("basic", "profile", "email"):
        matches = [scope for scope in scopes if scope.get("name") == name]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("id"), str)
            or not matches[0]["id"]
            or matches[0].get("protocol") != "openid-connect"
        ):
            raise RuntimeError("Required OIDC client scope lookup mismatch")
        identity_scopes[matches[0]["id"]] = name
    desired_scopes = set(identity_scopes)
    if len(desired_scopes) != 3:
        raise RuntimeError("Ambiguous OIDC client scope IDs")
    roles = [api("/roles/" + quote(name, safe=""), root=realm_url) for name in role_names]
    for role, name in zip(roles, role_names):
        if not role.get("id") or role.get("name") != name or role.get("clientRole") is not False:
            raise RuntimeError("OIDC realm role lookup mismatch")
    expected_roles = {(role["id"], role["name"]) for role in roles}
    gateway_uuid, gateway_scope = None, []
    if gateway_roles:
        gateways = api("/clients?clientId=agentgateway", root=realm_url)
        if (
            not isinstance(gateways, list)
            or len(gateways) != 1
            or gateways[0].get("clientId") != "agentgateway"
            or not gateways[0].get("id")
        ):
            raise RuntimeError("Gateway client lookup mismatch")
        gateway_uuid = gateways[0]["id"]
        for name in gateway_roles:
            role = api(
                f"/clients/{quote(gateway_uuid, safe='')}/roles/{quote(name, safe='')}",
                root=realm_url,
            )
            if (
                not role.get("id")
                or role.get("name") != name
                or role.get("clientRole") is not True
                or role.get("containerId") != gateway_uuid
            ):
                raise RuntimeError("Gateway role lookup mismatch")
            gateway_scope.append(role)
    expected_gateway = {(role["id"], role["name"]) for role in gateway_scope}
    allowed_ids = {role["id"] for role in roles + gateway_scope}

    def mapped_ids(mappings):
        return {r["id"] for r in mappings.get("realmMappings", [])} | {
            r["id"]
            for m in mappings.get("clientMappings", {}).values()
            for r in m.get("mappings", [])
        }

    unsafe_roles = set()

    def check_composites(role, visited):
        if role["id"] in visited:
            return
        visited.add(role["id"])
        if role.get("composite"):
            children = api(
                "/roles-by-id/" + quote(role["id"], safe="") + "/composites", root=realm_url
            )
            try:
                if any(child.get("id") not in allowed_ids for child in children):
                    raise ScopeDriftError("Composite role broadens OIDC scope")
                for child in children:
                    check_composites(child, visited)
            except ScopeDriftError:
                unsafe_roles.add(role["id"])
                raise

    for role in roles + gateway_scope:
        unsafe_roles.clear()
        try:
            check_composites(role, set())
        except ScopeDriftError:
            if uuid:
                assigned = mapped_ids(api("/scope-mappings"))
                for kind in ("default-client-scopes", "optional-client-scopes"):
                    for scope in api("/" + kind):
                        assigned |= mapped_ids(
                            api(
                                "/scope-mappings",
                                root=realm_url + "/client-scopes/" + quote(scope["id"], safe=""),
                            )
                        )
                if unsafe_roles & assigned or api().get("fullScopeAllowed") is True:
                    disable()
            raise

    def check_shared_scope(scope_id):
        root = realm_url + "/client-scopes/" + quote(scope_id, safe="")
        definition = api(root=root)
        if (
            not isinstance(definition, dict)
            or definition.get("id") != scope_id
            or definition.get("name") != identity_scopes[scope_id]
            or definition.get("protocol") != "openid-connect"
        ):
            raise ScopeDriftError("Identity client scope readback mismatch")
        mappings = api("/scope-mappings", root=root)
        effective = api("/scope-mappings/realm/composite", root=root)
        if not {(r["id"], r["name"]) for r in effective} <= expected_roles or any(
            item.get("mappings") for item in mappings.get("clientMappings", {}).values()
        ):
            raise ScopeDriftError("Attached client scope broadens OIDC role access")
        mappers = api("/protocol-mappers/models", root=root)
        if identity_scopes[scope_id] == "basic":
            subjects = [m for m in mappers if m.get("protocolMapper") == "oidc-sub-mapper"]
            if (
                len(subjects) != 1
                or subjects[0].get("protocol") != "openid-connect"
                or subjects[0].get("config", {}).get("access.token.claim") != "true"
            ):
                raise ScopeDriftError("Basic client scope must preserve the access token subject")
        if any(
            m.get("config", {}).get("claim.name", "").split(".")[0]
            in {
                claim.split(".")[0],
                "sub",
                "iss",
                "exp",
                "iat",
                "nbf",
                "aud",
                "azp",
                "resource_access",
                "realm_access",
            }
            or (
                groups_claim
                and m.get("config", {}).get("claim.name", "").split(".")[0] == groups_claim
            )
            or m.get("protocolMapper")
            not in {
                "oidc-sub-mapper",
                "oidc-usersessionmodel-note-mapper",
                "oidc-usermodel-property-mapper",
                "oidc-usermodel-attribute-mapper",
                "oidc-full-name-mapper",
            }
            for m in mappers
        ):
            raise ScopeDriftError("Attached client scope conflicts with OIDC permission mappers")

    for scope_id in desired_scopes:
        try:
            check_shared_scope(scope_id)
        except ScopeDriftError:
            if uuid and any(
                scope_id == scope["id"]
                for kind in ("default-client-scopes", "optional-client-scopes")
                for scope in api("/" + kind)
            ):
                disable()
            raise

    def check_client(enabled):
        configured = api()
        for key, value in {**expected, "enabled": enabled}.items():
            actual = configured.get(key)
            if isinstance(value, list):
                # Keycloak stores URL lists as sets; order is not part of the grant.
                if (
                    not isinstance(actual, list)
                    or len(actual) != len(value)
                    or set(actual) != set(value)
                ):
                    raise ScopeDriftError("Client readback mismatch")
            elif actual != value:
                raise ScopeDriftError("Client readback mismatch")
        if any(configured.get("attributes", {}).get(k) != v for k, v in attributes.items()):
            raise ScopeDriftError("Client attribute readback mismatch")
        if get_client_secret(base, token, realm, uuid) != secret:
            raise ScopeDriftError("Client secret readback mismatch")

    def mapper_matches(actual, desired):
        actual = {**actual, "config": dict(actual.get("config", {}))}
        for prefix in (
            "usermodel.realmRoleMapping.rolePrefix",
            "usermodel.clientRoleMapping.rolePrefix",
        ):
            if prefix in desired["config"]:
                actual["config"].setdefault(prefix, "")
        return all(actual.get(key) == value for key, value in desired.items())

    def check_state(enabled):
        check_client(enabled)
        mappers = api("/protocol-mappers/models")
        for desired in desired_mappers:
            matches = [m for m in mappers if m.get("name") == desired["name"]]
            if len(matches) != 1 or not mapper_matches(matches[0], desired):
                label = (
                    "Role"
                    if desired is realm_mapper
                    else "Gateway role"
                    if desired["name"] == gateway_mapper_name
                    else "Groups"
                    if desired["protocolMapper"] == "oidc-group-membership-mapper"
                    else "Audience"
                )
                raise ScopeDriftError(label + " mapper readback mismatch")
        if len(mappers) != len(desired_names) or {m.get("name") for m in mappers} != desired_names:
            raise ScopeDriftError("Protocol mapper allowlist readback mismatch")
        mappings = api("/scope-mappings")
        actual = mappings.get("realmMappings", [])
        actual_gateway = [
            (m["id"], r["id"], r["name"])
            for m in mappings.get("clientMappings", {}).values()
            for r in m.get("mappings", [])
        ]
        if (
            len(actual) != len(roles)
            or {(r["id"], r["name"]) for r in actual} != expected_roles
            or len(actual_gateway) != len(gateway_scope)
            or set(actual_gateway) != {(gateway_uuid, r["id"], r["name"]) for r in gateway_scope}
        ):
            raise ScopeDriftError("Client role scope readback mismatch")
        effective = api("/scope-mappings/realm/composite")
        if {(r["id"], r["name"]) for r in effective} != expected_roles:
            raise ScopeDriftError("Effective role scope readback mismatch")
        if gateway_uuid:
            effective = api(
                "/scope-mappings/clients/" + quote(gateway_uuid, safe="") + "/composite"
            )
            if {(r["id"], r["name"]) for r in effective} != expected_gateway:
                raise ScopeDriftError("Effective gateway role scope readback mismatch")
        for kind in ("default-client-scopes", "optional-client-scopes"):
            attached = api("/" + kind)
            desired_ids = desired_scopes if kind == "default-client-scopes" else set()
            if len(attached) != len(desired_ids) or {s["id"] for s in attached} != desired_ids:
                raise ScopeDriftError("Client scope attachment readback mismatch")
            for scope in attached:
                # Attachment responses are brief representations; identity is
                # verified using the canonical scope record, including protocol.
                check_shared_scope(scope["id"])

    if uuid:
        try:
            check_state(True)
        except ScopeDriftError:
            disable()
        else:
            return  # A fully verified no-op never interrupts login or refresh.
    else:
        api(
            "/clients",
            "POST",
            {**expected, "attributes": attributes, "secret": secret},
            root=realm_url,
        )
        uuid = lookup_client()
        if not uuid:
            raise RuntimeError("OIDC client lookup mismatch")
        url = f"{realm_url}/clients/{quote(uuid, safe='')}"
    # Never use the generic upsert helper: it temporarily enables full scope.
    api(method="PUT", body={**expected, "attributes": attributes, "secret": secret})
    for kind in ("default-client-scopes", "optional-client-scopes"):
        desired_ids = desired_scopes if kind == "default-client-scopes" else set()
        current = {scope["id"] for scope in api("/" + kind)}
        for scope_id in sorted(current - desired_ids):
            api(f"/{kind}/{quote(scope_id, safe='')}", "DELETE")
        for scope_id in sorted(desired_ids - current):
            api(f"/{kind}/{quote(scope_id, safe='')}", "PUT")
    mappings = api("/scope-mappings")
    current = mappings.get("realmMappings", [])
    stale = [r for r in current if (r["id"], r["name"]) not in expected_roles]
    if stale:
        api("/scope-mappings/realm", "DELETE", stale)
    missing = [r for r in roles if r["id"] not in {item["id"] for item in current}]
    if missing:
        api("/scope-mappings/realm", "POST", missing)
    for mapping in mappings.get("clientMappings", {}).values():
        stale = [
            r
            for r in mapping.get("mappings", [])
            if mapping["id"] != gateway_uuid or (r["id"], r["name"]) not in expected_gateway
        ]
        if stale:
            api("/scope-mappings/clients/" + quote(mapping["id"], safe=""), "DELETE", stale)
    if gateway_uuid:
        current_ids = {
            r["id"]
            for m in mappings.get("clientMappings", {}).values()
            if m["id"] == gateway_uuid
            for r in m.get("mappings", [])
        }
        missing = [r for r in gateway_scope if r["id"] not in current_ids]
        if missing:
            api("/scope-mappings/clients/" + quote(gateway_uuid, safe=""), "POST", missing)
    mappers = api("/protocol-mappers/models")
    for item in mappers:
        if item.get("name") not in desired_names:
            if (
                item.get("protocolMapper") == "oidc-audience-mapper"
                or item.get("name") == gateway_mapper_name
            ):
                api("/protocol-mappers/models/" + quote(item["id"], safe=""), "DELETE")
            else:
                raise ScopeDriftError("Unexpected role claim mapper")
    for desired in desired_mappers:
        matches = [m for m in mappers if m.get("name") == desired["name"]]
        if len(matches) > 1:
            raise ScopeDriftError("Ambiguous role mapper")
        if not matches:
            api("/protocol-mappers/models", "POST", desired)
        elif not mapper_matches(matches[0], desired):
            api(
                "/protocol-mappers/models/" + quote(matches[0]["id"], safe=""),
                "PUT",
                {**desired, "id": matches[0]["id"]},
            )
    check_state(False)
    try:
        api(method="PUT", body={"enabled": True})
        check_client(True)
    except (Exception, SystemExit):
        disable()
        raise


def main() -> None:
    """Keep credential-bearing helper diagnostics out of Job logs."""
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            reconcile()
    except (Exception, SystemExit):
        print("Scoped OIDC reconciliation or readback failed", file=sys.stderr)
        sys.exit(1)
    print("Scoped OIDC client and effective-role mapper verified")


if __name__ == "__main__":
    main()
