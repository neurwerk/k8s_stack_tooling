"""Converge a browser OIDC client with an explicit realm-role scope."""

import contextlib
import json
import os
import re
import sys
from urllib.parse import quote, urlsplit

from k8s_stack_tooling.api.http import request
from k8s_stack_tooling.api.keycloak import (
    get_admin_token,
    get_client_secret,
)


def reconcile() -> None:
    base = os.environ["KC_INTERNAL_URL"]
    realm = os.environ["KC_REALM"]
    secret = os.environ["KC_CLIENT_SECRET"]
    if not secret:
        raise ValueError("Missing client secret")
    client_id = os.environ["KC_CLIENT_ID"]
    role_names = json.loads(os.environ["KC_REALM_ROLE_SCOPE"])
    claim = os.environ["KC_ROLE_CLAIM"]
    audiences = json.loads(os.environ.get("KC_CLIENT_AUDIENCES", "[]"))
    if (
        not isinstance(audiences, list)
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", item)
            for item in audiences
        )
        or len(set(audiences)) != len(audiences)
    ):
        raise ValueError("Invalid OIDC audiences")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", client_id):
        raise ValueError("Invalid OIDC client ID")
    if (
        not isinstance(role_names, list)
        or not role_names
        or any(
            not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9:-]*", role)
            for role in role_names
        )
        or len(set(role_names)) != len(role_names)
    ):
        raise ValueError("Invalid OIDC realm-role scope")
    if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?", claim):
        raise ValueError("Invalid OIDC role claim")
    callback = os.environ["KC_REDIRECT_URI"]
    origin = os.environ["KC_WEB_ORIGIN"]
    for value in (callback, origin):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Invalid OIDC browser URL")
    rotation = os.environ.get("KC_REFRESH_TOKEN_ROTATION_ENABLED", "false")
    if rotation not in ("true", "false"):
        raise ValueError("Invalid refresh token rotation setting")
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

    expected = {
        "clientId": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "implicitFlowEnabled": False,
        "fullScopeAllowed": False,
        "redirectUris": [callback],
        "webOrigins": [origin],
    }
    attributes = {
        "post.logout.redirect.uris": origin,
        "oauth2.device.authorization.grant.enabled": "false",
        "oidc.ciba.grant.enabled": "false",
        "client.use.refresh.tokens": "true",
        "client.refresh.token.rotation.enabled": rotation,
    }
    # Never use the generic upsert helper: it temporarily enables full scope.
    clients = api("/clients?clientId=" + quote(client_id, safe=""))
    if not isinstance(clients, list) or len(clients) > 1:
        raise RuntimeError("Ambiguous OIDC client lookup")
    if not clients:
        api("/clients", "POST", {**expected, "attributes": attributes, "secret": secret})
        clients = api("/clients?clientId=" + quote(client_id, safe=""))
    if (
        not isinstance(clients, list)
        or len(clients) != 1
        or clients[0].get("clientId") != client_id
        or not isinstance(clients[0].get("id"), str)
        or not clients[0]["id"]
    ):
        raise RuntimeError("OIDC client lookup mismatch")
    uuid = clients[0]["id"]
    url = f"{realm_url}/clients/{quote(uuid, safe='')}"
    api(method="PUT", body={**expected, "attributes": attributes, "secret": secret})

    scopes = api("/client-scopes", root=realm_url)
    desired_scopes = set()
    for name in ("profile", "email"):
        matches = [scope for scope in scopes if scope.get("name") == name]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("id"), str)
            or not matches[0]["id"]
            or matches[0].get("protocol") != "openid-connect"
        ):
            raise RuntimeError("Required OIDC client scope lookup mismatch")
        desired_scopes.add(matches[0]["id"])
    if len(desired_scopes) != 2:
        raise RuntimeError("Ambiguous OIDC client scope IDs")
    # Change only this client's associations, not realm-wide scope definitions.
    for kind in ("default-client-scopes", "optional-client-scopes"):
        desired = desired_scopes if kind == "default-client-scopes" else set()
        current = {scope["id"] for scope in api("/" + kind)}
        for scope_id in sorted(current - desired):
            api(f"/{kind}/{quote(scope_id, safe='')}", "DELETE")
        for scope_id in sorted(desired - current):
            api(f"/{kind}/{quote(scope_id, safe='')}", "PUT")
    roles = [api("/roles/" + quote(name, safe=""), root=realm_url) for name in role_names]
    for role, name in zip(roles, role_names):
        if not role.get("id") or role.get("name") != name or role.get("clientRole") is not False:
            raise RuntimeError("OIDC realm role lookup mismatch")
    expected_roles = {(role["id"], role["name"]) for role in roles}
    mappings = api("/scope-mappings")
    current = mappings.get("realmMappings", [])
    stale = [role for role in current if (role["id"], role["name"]) not in expected_roles]
    if stale:
        api("/scope-mappings/realm", "DELETE", stale)
    missing = [role for role in roles if role["id"] not in {item["id"] for item in current}]
    if missing:
        api("/scope-mappings/realm", "POST", missing)
    # Only this client's assignments are reconciled. Never
    # delete realm roles or alter role mappings on shared client scopes.
    for mapping in mappings.get("clientMappings", {}).values():
        if mapping.get("mappings"):
            api(
                "/scope-mappings/clients/" + quote(mapping["id"], safe=""),
                "DELETE",
                mapping["mappings"],
            )

    # With full scope disabled, this built-in mapper intersects effective user
    # roles (including groups/composites) with the explicit role-scope allowlist.
    mapper = {
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
    mappers = api("/protocol-mappers/models")
    matches = [item for item in mappers if item.get("name") == mapper["name"]]
    if len(matches) > 1:
        raise RuntimeError("Ambiguous role mapper")
    if matches:
        api(
            "/protocol-mappers/models/" + quote(matches[0]["id"], safe=""),
            "PUT",
            {**mapper, "id": matches[0]["id"]},
        )
    else:
        api("/protocol-mappers/models", "POST", mapper)
    configured = api()
    if any(configured.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Client readback mismatch")
    if any(configured.get("attributes", {}).get(key) != value for key, value in attributes.items()):
        raise RuntimeError("Client attribute readback mismatch")
    if get_client_secret(base, token, realm, uuid) != secret:
        raise RuntimeError("Client secret readback mismatch")
    mappers = api("/protocol-mappers/models")
    matches = [item for item in mappers if item.get("name") == mapper["name"]]
    if len(matches) == 1:
        # Keycloak omits the empty prefix on readback; still send it to clear drift.
        matches[0].get("config", {}).setdefault("usermodel.realmRoleMapping.rolePrefix", "")
    if len(matches) != 1 or any(matches[0].get(key) != value for key, value in mapper.items()):
        raise RuntimeError("Role mapper readback mismatch")
    if any(
        item.get("config", {}).get("claim.name") == claim and item.get("name") != mapper["name"]
        for item in mappers
    ):
        raise RuntimeError("Conflicting role claim mapper")
    for audience in audiences:
        audience_mapper = {
            "name": "audience-" + audience,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": audience,
                "id.token.claim": "false",
                "access.token.claim": "true",
                "introspection.token.claim": "false",
            },
        }
        existing = [
            item
            for item in api("/protocol-mappers/models")
            if item.get("name") == audience_mapper["name"]
        ]
        if len(existing) > 1:
            raise RuntimeError("Ambiguous audience mapper")
        if existing:
            api(
                "/protocol-mappers/models/" + quote(existing[0]["id"], safe=""),
                "PUT",
                {**audience_mapper, "id": existing[0]["id"]},
            )
        else:
            api("/protocol-mappers/models", "POST", audience_mapper)
        configured_audiences = [
            item
            for item in api("/protocol-mappers/models")
            if item.get("name") == audience_mapper["name"]
        ]
        if len(configured_audiences) != 1 or any(
            configured_audiences[0].get(key) != value for key, value in audience_mapper.items()
        ):
            raise RuntimeError("Audience mapper readback mismatch")
    mappings = api("/scope-mappings")
    actual = mappings.get("realmMappings", [])
    if (
        len(actual) != len(roles)
        or {(role["id"], role["name"]) for role in actual} != expected_roles
        or any(item.get("mappings") for item in mappings.get("clientMappings", {}).values())
    ):
        raise RuntimeError("Client role scope readback mismatch")
    effective = api("/scope-mappings/realm/composite")
    if {(role["id"], role["name"]) for role in effective} != expected_roles:
        raise RuntimeError("Effective role scope readback mismatch")
    # Verify exact associations and reject tampered shared profile/email scopes.
    for kind in ("default-client-scopes", "optional-client-scopes"):
        attached = api("/" + kind)
        desired = desired_scopes if kind == "default-client-scopes" else set()
        if len(attached) != len(desired) or {scope["id"] for scope in attached} != desired:
            raise RuntimeError("Client scope attachment readback mismatch")
        for scope in attached:
            root = realm_url + "/client-scopes/" + quote(scope["id"], safe="")
            mappings = api("/scope-mappings", root=root)
            effective = api("/scope-mappings/realm/composite", root=root)
            if not {(role["id"], role["name"]) for role in effective} <= expected_roles or any(
                item.get("mappings") for item in mappings.get("clientMappings", {}).values()
            ):
                raise RuntimeError("Attached client scope broadens OIDC role access")
            if any(
                item.get("config", {}).get("claim.name") == claim
                for item in api("/protocol-mappers/models", root=root)
            ):
                raise RuntimeError("Attached client scope conflicts with Forgejo role mapper")


def main() -> None:
    """Keep credential-bearing helper diagnostics out of Job logs."""
    # Existing helpers can include response bodies in diagnostics. Never forward
    # those diagnostics from this credential-bearing reconciliation job.
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
