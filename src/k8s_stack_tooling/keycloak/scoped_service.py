"""Reconcile an explicitly approved, owned machine identity without rotating secrets."""

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import cast
from urllib.parse import quote

from k8s_stack_tooling.api.http import request
from k8s_stack_tooling.api.keycloak import get_admin_token, get_client_secret
from k8s_stack_tooling.keycloak.scoped_oidc import RESERVED_CLIENT_IDS, ScopeDriftError

OWNER_ATTRIBUTE = "neurwerk.scoped-service-client"
PERMISSION = (
    r"(?:llm:invoke|model:[A-Za-z0-9][A-Za-z0-9._/-]{0,127}:invoke|"
    r"provider:[a-z0-9][a-z0-9-]{0,47}:company-use)"
)


def names(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not re.fullmatch(PERMISSION, item) for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("Invalid machine permission allowlist")
    return cast(list[str], value)


def reconcile() -> None:
    client_id = os.environ["KC_CLIENT_ID"]
    if (
        not re.fullmatch(r"[a-z](?:[a-z0-9-]{0,126}[a-z0-9])?", client_id)
        or client_id in RESERVED_CLIENT_IDS
    ):
        raise ValueError("Invalid or reserved machine client ID")
    realm_names = names(json.loads(os.environ.get("KC_REALM_ROLE_SCOPE", "[]")))
    gateway_names = names(json.loads(os.environ["KC_GATEWAY_ROLE_SCOPE"]))
    audiences = json.loads(os.environ["KC_CLIENT_AUDIENCES"])
    approval = json.loads(os.environ["KC_CLIENT_APPROVAL"])
    if (
        audiences != ["agentgateway"]
        or not isinstance(approval, dict)
        or set(approval) != {"clientId", "realmRoles", "gatewayPermissions", "audiences"}
        or approval["clientId"] != client_id
        or approval["audiences"] != audiences
        or set(names(approval["realmRoles"])) != set(realm_names)
        or set(names(approval["gatewayPermissions"])) != set(gateway_names)
        or "llm:invoke" not in gateway_names
        or not any(name.startswith("model:") for name in gateway_names)
    ):
        raise ValueError("Machine client requires exact trusted approval")
    if ("KC_CLIENT_SECRET" in os.environ) == ("KC_CLIENT_SECRET_FILE" in os.environ):
        raise ValueError("Select exactly one machine secret source")
    secret = (
        os.environ["KC_CLIENT_SECRET"]
        if "KC_CLIENT_SECRET" in os.environ
        else Path(os.environ["KC_CLIENT_SECRET_FILE"]).read_text()
    )
    if not secret or secret != secret.strip():
        raise ValueError("Invalid machine client secret")

    base, realm = os.environ["KC_INTERNAL_URL"], os.environ["KC_REALM"]
    token = get_admin_token(base, os.environ["KC_ADMIN_USER"], os.environ["KC_ADMIN_PASSWORD"])
    root = f"{base}/admin/realms/{quote(realm, safe='')}"

    def api(path, method="GET", body=None):
        status, data = request(
            root + path, method=method, body=body, headers={"Authorization": f"Bearer {token}"}
        )
        if status not in (200, 201, 204):
            raise RuntimeError("Machine identity API request failed")
        return data

    def lookup(identity):
        clients = api("/clients?clientId=" + quote(identity, safe=""))
        if not isinstance(clients, list) or len(clients) > 1:
            raise RuntimeError("Ambiguous machine client lookup")
        if clients and (clients[0].get("clientId") != identity or not clients[0].get("id")):
            raise RuntimeError("Machine client lookup mismatch")
        return clients[0]["id"] if clients else None

    uuid = lookup(client_id)
    path = "/clients/" + quote(uuid, safe="") if uuid else ""
    owner = "v1:" + client_id
    if uuid:
        existing = api(path)
        if existing.get("attributes", {}).get(OWNER_ATTRIBUTE) != owner:
            raise ValueError("Existing machine client is not owned by this helper")
        if get_client_secret(base, token, realm, uuid) != secret:
            raise ValueError("Machine secret mismatch; rotation is a separate operation")

    def disable():
        api(path, "PUT", {"enabled": False})
        if api(path).get("enabled") is not False:
            raise RuntimeError("Machine disable readback failed")

    gateway = lookup("agentgateway")
    if not gateway:
        raise RuntimeError("Missing gateway client")
    gateway_path = "/clients/" + quote(gateway, safe="")
    roles = []
    gateway_roles = []
    for selected, target, prefix, client_role in (
        (realm_names, roles, "", False),
        (gateway_names, gateway_roles, gateway_path, True),
    ):
        for name in selected:
            role = api(prefix + "/roles/" + quote(name, safe=""))
            if (
                not role.get("id")
                or role.get("name") != name
                or role.get("clientRole") is not client_role
                or (client_role and role.get("containerId") != gateway)
            ):
                raise RuntimeError("Machine permission lookup mismatch")
            if role.get("composite") is True:
                # A successful identity-checked read proves unsafe scope drift.
                # Do not leave an owned identity issuing tokens while rejecting it.
                if uuid:
                    disable()
                raise ValueError("Machine permission must be an exact non-composite role")
            if role.get("composite") is not False:
                raise RuntimeError("Machine permission lookup incomplete")
            target.append(role)

    attributes = {
        OWNER_ATTRIBUTE: owner,
        "oauth2.device.authorization.grant.enabled": "false",
        "oidc.ciba.grant.enabled": "false",
        "client.use.refresh.tokens": "false",
        "client_credentials.use_refresh_token": "false",
        "standard.token.exchange.enabled": "false",
    }
    expected = {
        "clientId": client_id,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "fullScopeAllowed": False,
        "authorizationServicesEnabled": False,
        "redirectUris": [],
        "webOrigins": [],
        "defaultClientScopes": [],
        "optionalClientScopes": [],
    }
    mappers = [
        {
            "name": "machine-subject",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-sub-mapper",
            "config": {"access.token.claim": "true", "introspection.token.claim": "false"},
        },
        {
            "name": "machine-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": "agentgateway",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "introspection.token.claim": "false",
            },
        },
    ]
    for kind, claim, mapper, extra in (
        ("realm", "realm_access.roles", "realm", {}),
        (
            "gateway",
            "resource_access.agentgateway.roles",
            "client",
            {"usermodel.clientRoleMapping.clientId": "agentgateway"},
        ),
    ):
        mappers.append(
            {
                "name": "machine-" + kind + "-roles",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-" + mapper + "-role-mapper",
                "config": {
                    "claim.name": claim,
                    "jsonType.label": "String",
                    "multivalued": "true",
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "userinfo.token.claim": "false",
                    "introspection.token.claim": "false",
                    **extra,
                },
            }
        )

    def account():
        user = api(path + "/service-account-user")
        if not user.get("id") or user.get("username") != "service-account-" + client_id:
            raise RuntimeError("Machine service account lookup mismatch")
        user_path = "/users/" + quote(user["id"], safe="")
        groups = api(user_path + "/groups")
        if not isinstance(groups, list):
            raise RuntimeError("Machine service account group lookup incomplete")
        if groups:
            disable()
            raise ValueError("Machine service account must not inherit group permissions")
        return user_path

    def pairs(items):
        return {(item["id"], item["name"]) for item in items}

    def check_mappings(mapping_path):
        mappings = api(mapping_path)
        if pairs(mappings.get("realmMappings", [])) != pairs(roles):
            raise ScopeDriftError("Machine realm permissions differ")
        actual = {
            (entry["id"], role["id"], role["name"])
            for entry in mappings.get("clientMappings", {}).values()
            for role in entry.get("mappings", [])
        }
        if actual != {(gateway, role["id"], role["name"]) for role in gateway_roles}:
            raise ScopeDriftError("Machine gateway permissions differ")
        for suffix, wanted in (("/realm", roles), ("/clients/" + gateway, gateway_roles)):
            if pairs(api(mapping_path + suffix + "/composite")) != pairs(wanted):
                raise ScopeDriftError("Machine inherited permissions differ")

    def check(enabled):
        configured = api(path)
        # Keycloak omits this nullable flag when authorization services are disabled.
        configured.setdefault("authorizationServicesEnabled", False)
        # Scope attachment fields are brief/optional in ClientRepresentation.
        for key, value in {**expected, "enabled": enabled}.items():
            if key not in {"defaultClientScopes", "optionalClientScopes"}:
                if configured.get(key) != value:
                    raise ScopeDriftError("Machine client configuration differs")
        if any(configured.get("attributes", {}).get(k) != v for k, v in attributes.items()):
            raise ScopeDriftError("Machine client attributes differ")
        if get_client_secret(base, token, realm, uuid) != secret:
            raise ScopeDriftError("Machine secret readback differs")
        for kind in ("default-client-scopes", "optional-client-scopes"):
            if api(path + "/" + kind):
                raise ScopeDriftError("Machine client has shared scopes")
        actual_mappers = api(path + "/protocol-mappers/models")
        if len(actual_mappers) != len(mappers) or any(
            not any(all(item.get(k) == v for k, v in desired.items()) for item in actual_mappers)
            for desired in mappers
        ):
            raise ScopeDriftError("Machine protocol mapper readback differs")
        check_mappings(path + "/scope-mappings")
        check_mappings(account() + "/role-mappings")

    if uuid:
        try:
            check(True)
        except ScopeDriftError:
            pass
        else:
            return
        disable()
    else:
        api(
            "/clients",
            "POST",
            {
                **expected,
                "enabled": False,
                "attributes": attributes,
                "secret": secret,
            },
        )
        uuid = lookup(client_id)
        if not uuid:
            raise RuntimeError("Created machine client lookup failed")
        path = "/clients/" + quote(uuid, safe="")
        if api(path).get("attributes", {}).get(OWNER_ATTRIBUTE) != owner:
            raise RuntimeError("Created machine ownership readback failed")
    # Never send a secret on update: an existing identity cannot be silently rotated.
    api(path, "PUT", {**expected, "enabled": False, "attributes": attributes})
    for kind in ("default-client-scopes", "optional-client-scopes"):
        for scope in api(path + "/" + kind):
            api(path + "/" + kind + "/" + quote(scope["id"], safe=""), "DELETE")
    user_path = account()
    for mapping_path in (path + "/scope-mappings", user_path + "/role-mappings"):
        mappings = api(mapping_path)
        for suffix, current, desired in [
            ("/realm", mappings.get("realmMappings", []), roles),
            *[
                (
                    "/clients/" + quote(entry["id"], safe=""),
                    entry.get("mappings", []),
                    gateway_roles if entry["id"] == gateway else [],
                )
                for entry in mappings.get("clientMappings", {}).values()
            ],
        ]:
            stale = [role for role in current if (role["id"], role["name"]) not in pairs(desired)]
            if stale:
                api(mapping_path + suffix, "DELETE", stale)
        for suffix, desired in (("/realm", roles), ("/clients/" + gateway, gateway_roles)):
            current = api(mapping_path + suffix)
            missing = [role for role in desired if (role["id"], role["name"]) not in pairs(current)]
            if missing:
                api(mapping_path + suffix, "POST", missing)
    for mapper in api(path + "/protocol-mappers/models"):
        api(path + "/protocol-mappers/models/" + quote(mapper["id"], safe=""), "DELETE")
    for mapper in mappers:
        api(path + "/protocol-mappers/models", "POST", mapper)
    check(False)
    try:
        api(path, "PUT", {"enabled": True})
        check(True)
    except (Exception, SystemExit):
        api(path, "PUT", {"enabled": False})
        raise


def main() -> None:
    """Suppress credential-bearing transport errors, including during token acquisition."""
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            reconcile()
    except (Exception, SystemExit):
        print("Scoped service client reconciliation or readback failed", file=sys.stderr)
        sys.exit(1)
    print("Scoped service client and exact permissions verified")


if __name__ == "__main__":
    main()
