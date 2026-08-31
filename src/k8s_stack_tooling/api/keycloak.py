"""Keycloak admin API helpers for init scripts.

Provides token acquisition, client CRUD, realm CRUD, audience mapper, and
service account role assignment.  All HTTP calls go through
:mod:`k8s_stack_tooling.api.http`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlencode

from k8s_stack_tooling.api.http import log, request

# ── Token ──────────────────────────────────────────────────────────────────


def get_admin_token(base_url: str, username: str, password: str) -> str:
    """Obtain an admin access token from the master realm."""
    token_url = f"{base_url}/realms/master/protocol/openid-connect/token"
    log(f"Obtaining admin access token from {token_url} …")
    body = {
        "client_id": "admin-cli",
        "username": username,
        "password": password,
        "grant_type": "password",
    }
    try:
        resp = request(
            token_url,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
            form_data=True,
        )
    except Exception as exc:
        log(f"ERROR: Failed to obtain admin token: {exc}")
        raise SystemExit(1) from exc

    status, data = resp
    if status != 200 or not data:
        detail = f" (HTTP {status})" if status else ""
        log(f"ERROR: Failed to obtain admin token.{detail}")
        if data:
            log(f"  Response keys: {list(data.keys()) if isinstance(data, dict) else data[:500]}")
        raise SystemExit(1)

    token: str | None = data.get("access_token")
    if not token:
        log("ERROR: Admin token response missing access_token.")
        raise SystemExit(1)

    log("Admin token obtained.")
    return token


# ── Client operations ──────────────────────────────────────────────────────


def find_client_uuid(
    admin_url: str,
    token: str,
    realm: str,
    client_id: str,
) -> str | None:
    """Look up a client by its ``clientId`` and return the internal UUID."""
    url = f"{admin_url}/admin/realms/{realm}/clients?clientId={client_id}"
    status, data = request(url, headers={"Authorization": f"Bearer {token}"})
    if status != 200 or not data:
        return None
    if isinstance(data, list) and len(data) > 0:
        return data[0].get("id")
    return None


def client_exists(
    admin_url: str,
    token: str,
    realm: str,
    client_id: str,
) -> tuple[bool, str | None]:
    """Check if an OIDC client already exists.

    Returns ``(exists, uuid)`` where *uuid* is the internal Keycloak UUID
    if found.
    """
    uuid = find_client_uuid(admin_url, token, realm, client_id)
    return (uuid is not None, uuid)


def upsert_client(
    admin_url: str,
    token: str,
    realm: str,
    client_id: str,
    redirect_uris: list[str],
    web_origins: list[str],
    access_type: str = "confidential",
    client_secret: str | None = None,
) -> str:
    """Create or update an OIDC client. Returns the client internal UUID.

    *access_type*: ``"confidential"`` (default, server-side with client secret)
    or ``"public"`` (browser-based, PKCE flow, no client secret).
    *client_secret*: desired caller-owned secret, omitted for clients that do not use one.
    """
    direct_access_grants = (
        os.environ.get("KC_DIRECT_ACCESS_GRANTS_ENABLED", "false").lower() == "true"
    )
    standard_flow = os.environ.get("KC_STANDARD_FLOW_ENABLED", "true").lower() == "true"
    service_accounts = os.environ.get("KC_SERVICE_ACCOUNTS_ENABLED", "false").lower() == "true"
    refresh_token_rotation = (
        os.environ.get("KC_REFRESH_TOKEN_ROTATION_ENABLED", "false").lower() == "true"
    )
    refresh_token_min_lifespan = os.environ.get("KC_REFRESH_TOKEN_MIN_LIFESPAN", "2592000")

    if access_type == "public":
        public_client = True
        authenticator_type = "none"
    else:
        public_client = False
        authenticator_type = "client-secret"

    body: dict[str, Any] = {
        "clientId": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        # Stack clients need group-derived realm and AgentGateway client roles
        # in their access tokens. Narrowing this requires explicit role scopes.
        "fullScopeAllowed": True,
        "publicClient": public_client,
        "clientAuthenticatorType": authenticator_type,
        "standardFlowEnabled": standard_flow,
        "directAccessGrantsEnabled": direct_access_grants,
        "serviceAccountsEnabled": service_accounts,
        "redirectUris": redirect_uris,
        "webOrigins": web_origins,
        "attributes": {
            "post.logout.redirect.uris": "##".join(f"{origin}/*" for origin in web_origins),
            "client.use.refresh.tokens": "true",
            "client.refresh.token.rotation.enabled": "true" if refresh_token_rotation else "false",
            "client.refresh.token.minimum.lifespan": refresh_token_min_lifespan,
        },
    }
    if client_secret is not None:
        body["secret"] = client_secret

    url = f"{admin_url}/admin/realms/{realm}/clients"
    token_header = {"Authorization": f"Bearer {token}"}

    exists, uuid = client_exists(admin_url, token, realm, client_id)

    if exists and uuid:
        log(f"Client '{client_id}' already exists (UUID: {uuid}) — updating …")
        update_url = f"{admin_url}/admin/realms/{realm}/clients/{uuid}"
        status, parsed = request(update_url, method="PUT", headers=token_header, body=body)
        if status in (200, 204):
            log(f"Client '{client_id}' updated.")
        elif status is None:
            log(f"ERROR: Request failed when updating client '{client_id}'.")
            raise SystemExit(1)
        else:
            log(f"ERROR: HTTP {status} when updating client '{client_id}'.")
            if parsed:
                log(f"  Response: {json.dumps(parsed, indent=2)[:300]}")
            raise SystemExit(1)
        return uuid

    log(f"Creating OIDC client '{client_id}' in realm '{realm}' …")
    status, _ = request(url, method="POST", headers=token_header, body=body)

    if status == 201:
        log(f"Client '{client_id}' created.")
        _, uuid = client_exists(admin_url, token, realm, client_id)
        if not uuid:
            log("ERROR: Created client but could not find its UUID.")
            raise SystemExit(1)
        return uuid

    if status is None:
        log("ERROR: Request to create OIDC client failed (no response).")
        raise SystemExit(1)

    log(f"ERROR: Unexpected HTTP status {status} when creating client.")
    raise SystemExit(1)


def get_client_secret(admin_url: str, token: str, realm: str, uuid: str) -> str:
    """Retrieve the client secret from Keycloak."""
    url = f"{admin_url}/admin/realms/{realm}/clients/{uuid}/client-secret"
    log(f"Retrieving client secret from {url} …")
    status, data = request(url, headers={"Authorization": f"Bearer {token}"})
    if status != 200 or not data:
        log(f"ERROR: Could not retrieve client secret (HTTP {status}).")
        raise SystemExit(1)
    secret: str = data.get("value", "")
    if not secret:
        log("ERROR: Client secret is empty.")
        raise SystemExit(1)
    log("Client secret retrieved.")
    return secret


def upsert_client_roles(
    admin_url: str,
    token: str,
    realm: str,
    client_uuid: str,
    client_id: str,
    role_names: list[str],
) -> None:
    """Create the client roles declared by the stack when they are absent."""
    if not role_names:
        return
    roles_url = f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/roles"
    token_header = {"Authorization": f"Bearer {token}"}
    status, data = request(roles_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(data, list):
        log(f"ERROR: Could not list roles for client '{client_id}' (HTTP {status}).")
        raise SystemExit(1)
    existing = {role.get("name") for role in data}
    for role_name in role_names:
        if role_name in existing:
            continue
        status, response = request(
            roles_url,
            method="POST",
            headers={**token_header, "Content-Type": "application/json"},
            body={"name": role_name},
        )
        if status != 201:
            log(
                f"ERROR: Could not create role '{role_name}' for client '{client_id}' "
                f"(HTTP {status}): {response}"
            )
            raise SystemExit(1)


# ── Realm operations ───────────────────────────────────────────────────────


def realm_exists(admin_url: str, token: str, realm: str) -> bool:
    """Check if a realm already exists by listing all accessible realms."""
    token_header = {"Authorization": f"Bearer {token}"}
    list_url = f"{admin_url}/admin/realms"
    status, parsed = request(list_url, method="GET", headers=token_header)
    if status != 200:
        log(f"ERROR: Unexpected HTTP status {status} when listing realms.")
        raise SystemExit(1)
    for r in parsed or []:
        if r.get("realm") == realm:
            return True
    return False


def upsert_realm_api(admin_url: str, token: str, realm: str) -> None:
    """Idempotently create or update a Keycloak realm with token/session settings."""
    token_header = {"Authorization": f"Bearer {token}"}

    display_name = os.environ.get("KC_REALM_DISPLAY_NAME", "")
    if not display_name:
        log("ERROR: KC_REALM_DISPLAY_NAME environment variable is required but not set.")
        raise SystemExit(1)

    sso_session_max = os.environ.get("KC_SSO_SESSION_MAX_LIFESPAN", "2592000")
    sso_session_idle = os.environ.get("KC_SSO_SESSION_IDLE_TIMEOUT", "86400")
    access_token_lifespan = os.environ.get("KC_ACCESS_TOKEN_LIFESPAN", "900")
    events_expiration = os.environ.get("KC_EVENTS_EXPIRATION", "604800")
    action_token_lifespan = _bounded_action_token_lifespan(
        os.environ.get("KC_ADMIN_ACTION_TOKEN_LIFESPAN", "1800")
    )
    smtp_server = _smtp_server_from_environment()

    realm_body: dict[str, Any] = {
        "realm": realm,
        "enabled": True,
        "displayName": display_name,
        "registrationAllowed": False,
        "resetPasswordAllowed": True,
        "rememberMe": True,
        "verifyEmail": smtp_server is not None,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "sslRequired": "external",
        "accessTokenLifespan": int(access_token_lifespan),
        "ssoSessionMaxLifespan": int(sso_session_max),
        "ssoSessionIdleTimeout": int(sso_session_idle),
        # Event logging: route login + admin events to the jboss-logging listener
        # (Keycloak dispatches admin events to the same listeners as login events),
        # so they land in stdout → Fluent Bit → OpenSearch. The listener log level
        # is configured server-side via KC_SPI_EVENTS_LISTENER_JBOSS_LOGGING_*.
        "eventsEnabled": True,
        "eventsListeners": ["jboss-logging"],
        "eventsExpiration": int(events_expiration),
        "adminEventsEnabled": True,
        "adminEventsDetailsEnabled": True,
        "actionTokenGeneratedByAdminLifespan": action_token_lifespan,
        "smtpServer": smtp_server or {},
    }

    if realm_exists(admin_url, token, realm):
        log(f"Realm '{realm}' already exists — updating token/session settings …")
        update_url = f"{admin_url}/admin/realms/{realm}"
        status, parsed = request(update_url, method="PUT", headers=token_header, body=realm_body)
        if status in (200, 204):
            log(f"Realm '{realm}' updated.")
        elif status is None:
            log(f"ERROR: Request failed when updating realm '{realm}'.")
            raise SystemExit(1)
        else:
            log(f"ERROR: HTTP {status} when updating realm '{realm}'.")
            if parsed:
                log(f"  Response: {json.dumps(parsed, indent=2)[:300]}")
            raise SystemExit(1)
        return

    log(f"Creating realm '{realm}' …")
    create_url = f"{admin_url}/admin/realms"
    status, parsed = request(create_url, method="POST", headers=token_header, body=realm_body)
    if status == 201:
        log(f"Realm '{realm}' created.")
    elif status is None:
        log("ERROR: Request to create realm failed (no response).")
        raise SystemExit(1)
    else:
        log(f"ERROR: Unexpected HTTP status {status} when creating realm.")
        raise SystemExit(1)


def _bounded_action_token_lifespan(value: str) -> int:
    """Require a short but usable lifespan for administrator-issued action links."""
    try:
        lifespan = int(value)
    except ValueError:
        log("ERROR: KC_ADMIN_ACTION_TOKEN_LIFESPAN must be an integer number of seconds.")
        raise SystemExit(1) from None
    if not 300 <= lifespan <= 3600:
        log("ERROR: KC_ADMIN_ACTION_TOKEN_LIFESPAN must be between 300 and 3600 seconds.")
        raise SystemExit(1)
    return lifespan


def _enabled_environment(name: str) -> bool:
    """Read a strict boolean environment setting without accepting typos."""
    value = os.environ.get(name, "false").lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    log(f"ERROR: {name} must be a boolean.")
    raise SystemExit(1)


def _smtp_server_from_environment() -> dict[str, str] | None:
    """Build Keycloak's string-valued SMTP representation when SMTP is enabled."""
    if not _enabled_environment("KC_SMTP_ENABLED"):
        return None
    values = {
        "host": os.environ.get("KC_SMTP_HOST", "").strip(),
        "port": os.environ.get("KC_SMTP_PORT", "").strip(),
        "from": os.environ.get("KC_SMTP_FROM", "").strip(),
        "user": os.environ.get("KC_SMTP_USERNAME", "").strip(),
        "password": os.environ.get("KC_SMTP_PASSWORD", "").strip(),
    }
    if any(not value for value in values.values()):
        log("ERROR: Enabled SMTP requires host, port, sender, username, and password.")
        raise SystemExit(1)
    try:
        port = int(values["port"])
    except ValueError:
        log("ERROR: KC_SMTP_PORT must be an integer.")
        raise SystemExit(1) from None
    if not 1 <= port <= 65535:
        log("ERROR: KC_SMTP_PORT must be between 1 and 65535.")
        raise SystemExit(1)
    ssl = _enabled_environment("KC_SMTP_SSL")
    starttls = _enabled_environment("KC_SMTP_STARTTLS")
    if ssl == starttls:
        log("ERROR: Enabled SMTP requires exactly one of KC_SMTP_SSL or KC_SMTP_STARTTLS.")
        raise SystemExit(1)
    return {
        **values,
        "port": str(port),
        "auth": "true",
        "ssl": str(ssl).lower(),
        "starttls": str(starttls).lower(),
    }


def _exact_user_search_url(admin_url: str, realm: str, username: str) -> str:
    """Return the encoded exact-username search URL used by user operations."""
    query = urlencode({"username": username, "exact": "true"})
    return f"{admin_url}/admin/realms/{realm}/users?{query}"


# ── Audience mapper (new) ──────────────────────────────────────────────────


def add_audience_mapper(
    admin_url: str,
    token: str,
    realm: str,
    client_uuid: str,
    audience: str,
) -> None:
    """Ensure an OIDC audience protocol mapper exists on a client.

    This ensures the specified *audience* appears in the ``aud`` claim of
    access tokens issued for this client.
    """
    url = f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/protocol-mappers/models"
    headers = {"Authorization": f"Bearer {token}"}
    status, existing = request(url, method="GET", headers=headers)
    if status != 200 or not isinstance(existing, list):
        log(f"ERROR: Could not list existing audience mappers (HTTP {status}).")
        raise SystemExit(1)

    if any(
        mapper.get("protocolMapper") == "oidc-audience-mapper"
        and mapper.get("config", {}).get("included.client.audience") == audience
        for mapper in existing
    ):
        log(f"  Audience mapper for '{audience}' already exists.")
        return

    mapper_body: dict[str, Any] = {
        "name": f"audience-{audience}",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": audience,
            "id.token.claim": "false",
            "access.token.claim": "true",
        },
    }
    log(f"Adding audience mapper for '{audience}' to client {client_uuid} …")
    status, data = request(
        url,
        method="POST",
        headers=headers,
        body=mapper_body,
    )
    if status == 201:
        log(f"  Audience mapper for '{audience}' created.")
        return

    if status == 409:
        # A concurrent reconciler may have created the mapper after the initial
        # list. Re-read it before accepting the conflict as successful.
        status, existing = request(url, method="GET", headers=headers)
        if (
            status == 200
            and isinstance(existing, list)
            and any(
                mapper.get("protocolMapper") == "oidc-audience-mapper"
                and mapper.get("config", {}).get("included.client.audience") == audience
                for mapper in existing
            )
        ):
            log(f"  Audience mapper for '{audience}' already exists.")
            return

    log(f"ERROR: Could not create audience mapper for '{audience}' (HTTP {status}).")
    if data:
        log(f"  Response: {str(data)[:300]}")
    raise SystemExit(1)


# ── Service account role assignment (new) ─────────────────────────────────


def assign_service_account_role(
    admin_url: str,
    token: str,
    realm: str,
    client_uuid: str,
    role_client_id: str,
    role_name: str,
) -> None:
    """Assign a client-level role from *role_client_id* to the client's
    service account user.

    For example, to assign ``view-users`` from the ``realm-management``
    client to the ``keycloack_api_key_bridge`` client's service account::

        assign_service_account_role(
            admin_url, token, realm, apikey_manager_uuid,
            "realm-management", "view-users",
        )
    """
    # 1. Find the service account user
    sa_url = f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/service-account-user"
    sa_status, sa_data = request(sa_url, headers={"Authorization": f"Bearer {token}"})
    if sa_status != 200 or not sa_data:
        log(f"ERROR: Could not find service account user for client {client_uuid}.")
        raise SystemExit(1)
    sa_user_id: str = sa_data.get("id", "")

    # 2. Find the role client
    role_client_uuid = find_client_uuid(admin_url, token, realm, role_client_id)
    if not role_client_uuid:
        log(f"ERROR: Could not find client '{role_client_id}' for role lookup.")
        raise SystemExit(1)

    # 3. Find the role by name
    roles_url = f"{admin_url}/admin/realms/{realm}/clients/{role_client_uuid}/roles"
    roles_status, roles_data = request(roles_url, headers={"Authorization": f"Bearer {token}"})
    if roles_status != 200 or not roles_data:
        log(f"ERROR: Could not list roles for client '{role_client_id}'.")
        raise SystemExit(1)

    target_role: dict[str, Any] | None = None
    for r in roles_data:
        if r.get("name") == role_name:
            target_role = r
            break

    if not target_role:
        log(f"ERROR: Role '{role_name}' not found on client '{role_client_id}'.")
        raise SystemExit(1)

    # 4. Assign the role
    assign_url = (
        f"{admin_url}/admin/realms/{realm}/users/{sa_user_id}/role-mappings"
        f"/clients/{role_client_uuid}"
    )
    log(f"Assigning role '{role_name}' (from '{role_client_id}') to service account …")
    status, data = request(
        assign_url,
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        body=[target_role],
    )
    if status in (200, 204):
        status, assigned_roles = request(assign_url, headers={"Authorization": f"Bearer {token}"})
        if (
            status == 200
            and isinstance(assigned_roles, list)
            and any(role.get("id") == target_role.get("id") for role in assigned_roles)
        ):
            log(f"  Role '{role_name}' assigned.")
            return
        log(f"ERROR: Could not verify role '{role_name}' assignment (HTTP {status}).")
        raise SystemExit(1)
    log(f"ERROR: Unexpected HTTP {status} when assigning role.")
    if data:
        log(f"  Response: {str(data)[:300]}")
    raise SystemExit(1)


# ── Service account realm role assignment (new) ───────────────────────────


def assign_service_account_realm_roles(
    admin_url: str,
    token: str,
    realm: str,
    client_uuid: str,
    role_names: list[str],
) -> None:
    """Assign realm-level roles to a client's service account user.

    For example, to assign ``api-key-admin`` to the
    ``keycloak-api-key-bridge`` service account::

        assign_service_account_realm_roles(
            admin_url, token, realm, bridge_client_uuid,
            ["api-key-admin"],
        )
    """
    # 1. Find the service account user
    sa_url = f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/service-account-user"
    sa_status, sa_data = request(sa_url, headers={"Authorization": f"Bearer {token}"})
    if sa_status != 200 or not sa_data:
        log(f"ERROR: Could not find service account user for client {client_uuid}.")
        raise SystemExit(1)
    sa_user_id: str = sa_data.get("id", "")

    # 2. Fetch existing realm roles
    list_url = f"{admin_url}/admin/realms/{realm}/roles"
    status, data = request(list_url, method="GET", headers={"Authorization": f"Bearer {token}"})
    if status != 200 or not data:
        log(f"ERROR: Could not list realm roles (HTTP {status}).")
        raise SystemExit(1)

    # 3. Find target roles
    target_roles: list[dict[str, Any]] = []
    for r in data:
        if r.get("name") in role_names:
            target_roles.append(r)

    missing = set(role_names) - {r.get("name") for r in target_roles}
    if missing:
        log(f"ERROR: Realm role(s) not found: {', '.join(sorted(missing))}")
        raise SystemExit(1)

    # 4. Assign the roles to the service account user
    assign_url = f"{admin_url}/admin/realms/{realm}/users/{sa_user_id}/role-mappings/realm"
    log(f"Assigning realm roles {role_names} to service account …")
    status, data = request(
        assign_url,
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        body=target_roles,
    )
    if status in (200, 204):
        status, assigned_roles = request(assign_url, headers={"Authorization": f"Bearer {token}"})
        assigned_names = (
            {role.get("name") for role in assigned_roles}
            if status == 200 and isinstance(assigned_roles, list)
            else set()
        )
        if set(role_names).issubset(assigned_names):
            log(f"  Realm roles {role_names} assigned.")
            return
        log("ERROR: Could not verify service account realm role assignments.")
        raise SystemExit(1)
    log(f"ERROR: Unexpected HTTP {status} when assigning realm roles.")
    if data:
        log(f"  Response: {str(data)[:300]}")
    raise SystemExit(1)


# ── Composite roles (client-level roles as composites of a realm role) ────


def upsert_composite_roles_api(
    admin_url: str,
    token: str,
    realm: str,
    parent_role_name: str,
    client_id: str,
    client_role_names: list[str],
) -> None:
    """Idempotently add client-level roles from *client_id* as composites of
    a parent realm role.

    For example, to add ``view-users``, ``query-users``, ``view-clients``, and
    ``view-realm`` from the ``realm-management`` client as composites of the
    ``keycloak-admin`` realm role::

        upsert_composite_roles_api(
            admin_url, token, realm,
            parent_role_name="keycloak-admin",
            client_id="realm-management",
            client_role_names=["view-users", "query-users", "view-clients", "view-realm"],
        )
    """
    token_header = {"Authorization": f"Bearer {token}"}

    # 1. Look up the client UUID
    client_uuid = find_client_uuid(admin_url, token, realm, client_id)
    if not client_uuid:
        log(f"ERROR: Client '{client_id}' not found in realm '{realm}'.")
        raise SystemExit(1)
    log(f"Client '{client_id}' has UUID: {client_uuid}")

    # 2. Look up the parent realm role. The chart normally serializes this after
    #    role creation, but a short bounded retry tolerates Keycloak propagation.
    role_url = f"{admin_url}/admin/realms/{realm}/roles/{parent_role_name}"
    parent_role_id = ""
    max_attempts = 60
    for attempt in range(1, max_attempts + 1):
        status, role_data = request(role_url, method="GET", headers=token_header)
        if status == 200 and role_data:
            rid: str | None = role_data.get("id")
            if rid:
                parent_role_id = rid
                break
        if attempt < max_attempts:
            delay = 5
            log(
                f"  Parent realm role '{parent_role_name}' not ready yet "
                f"(attempt {attempt}/{max_attempts}) — retrying in {delay}s …"
            )
            time.sleep(delay)
    if not parent_role_id:
        log(f"ERROR: Realm role '{parent_role_name}' not found after {max_attempts} attempts.")
        raise SystemExit(1)
    log(f"Parent realm role '{parent_role_name}' has ID: {parent_role_id}")

    # 3. Fetch existing composites of the parent realm role
    composites_url = f"{admin_url}/admin/realms/{realm}/roles-by-id/{parent_role_id}/composites"
    status, composites_data = request(composites_url, method="GET", headers=token_header)
    if (
        status != 200
        or not isinstance(composites_data, list)
        or not all(isinstance(composite, dict) for composite in composites_data)
    ):
        log(
            f"ERROR: Could not list composites for realm role '{parent_role_name}' (HTTP {status})."
        )
        raise SystemExit(1)
    existing_composites: list[dict[str, Any]] = composites_data
    existing_composite_ids: set[str] = {c.get("id", "") for c in existing_composites}
    count = len(existing_composite_ids)
    log(f"Parent role '{parent_role_name}' currently has {count} composite(s).")

    # 4. Build the desired set of composite IDs for the given client
    desired_roles: dict[str, dict[str, Any]] = {}

    for role_name in client_role_names:
        role_detail_url = (
            f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/roles/{role_name}"
        )
        status, role_detail = request(role_detail_url, method="GET", headers=token_header)
        if status != 200 or not role_detail:
            log(f"ERROR: Role '{role_name}' missing on client '{client_id}' (HTTP {status}).")
            raise SystemExit(1)
        role_id: str = role_detail.get("id", "")
        if not role_id:
            log(f"ERROR: Client-level role '{role_name}' has no ID.")
            raise SystemExit(1)
        desired_roles[role_id] = role_detail
    desired_ids = set(desired_roles)

    # 5. Remove composites that belong to this client but are NOT in the desired set
    to_remove = [
        c
        for c in existing_composites
        if c.get("containerId") == client_uuid and c.get("id", "") not in desired_ids
    ]
    if to_remove:
        removed_names = [c.get("name", c.get("id", "?")) for c in to_remove]
        log(f"  Removing {len(to_remove)} composite(s) no longer in desired set: {removed_names}")
        status, del_data = request(
            composites_url,
            method="DELETE",
            headers={**token_header, "Content-Type": "application/json"},
            body=to_remove,
        )
        if status in (200, 204):
            log("    Stale composites removed.")
        else:
            log(f"ERROR: Unexpected HTTP {status} when removing composites.")
            if del_data:
                log(f"  Response: {str(del_data)[:300]}")
            raise SystemExit(1)

    # 6. Add composites that are desired but not yet present
    for role_id, role_detail in desired_roles.items():
        role_name = str(role_detail["name"])
        if role_id in existing_composite_ids:
            log(f"  Composite role '{role_name}' already present — keeping.")
            continue

        log(f"  Adding composite role '{role_name}' (ID: {role_id}) …")
        composite_body = [
            {
                "id": role_id,
                "clientRole": True,
                "containerId": client_uuid,
            }
        ]
        status, add_data = request(
            composites_url,
            method="POST",
            headers={**token_header, "Content-Type": "application/json"},
            body=composite_body,
        )
        if status in (200, 204):
            log(f"    Composite role '{role_name}' added.")
        else:
            log(f"ERROR: Unexpected HTTP {status} when adding composite role '{role_name}'.")
            if add_data:
                log(f"  Response: {str(add_data)[:300]}")
            raise SystemExit(1)

    status, verified_composites = request(composites_url, method="GET", headers=token_header)
    if (
        status != 200
        or not isinstance(verified_composites, list)
        or not all(isinstance(composite, dict) for composite in verified_composites)
    ):
        log(
            f"ERROR: Could not verify composites for realm role '{parent_role_name}' "
            f"(HTTP {status})."
        )
        raise SystemExit(1)
    verified_composite_dicts: list[dict[str, Any]] = verified_composites
    actual_ids = {
        role.get("id")
        for role in verified_composite_dicts
        if role.get("containerId") == client_uuid and role.get("clientRole") is True
    }
    if actual_ids != desired_ids:
        log(f"ERROR: Composite roles for '{parent_role_name}' did not converge.")
        raise SystemExit(1)


# ── Realm authorization ────────────────────────────────────────────────────


def upsert_realm_roles_api(
    admin_url: str,
    token: str,
    realm: str,
    role_names: list[str],
) -> None:
    """Idempotently create realm-level roles in Keycloak.

    Checks each role by name; creates it if missing, skips if it already
    exists.  This is safe to re-run — it never duplicates roles.
    """
    token_header = {"Authorization": f"Bearer {token}"}

    # Fetch existing realm roles
    list_url = f"{admin_url}/admin/realms/{realm}/roles"
    status, data = request(list_url, method="GET", headers=token_header)
    if status != 200 or not data:
        log(f"ERROR: Could not list realm roles (HTTP {status}).")
        raise SystemExit(1)

    existing_names: set[str] = {r.get("name", "") for r in data}

    for role_name in role_names:
        if role_name in existing_names:
            log(f"Role '{role_name}' already exists — skipping.")
            continue

        log(f"Creating realm role '{role_name}' …")
        create_url = f"{admin_url}/admin/realms/{realm}/roles"
        status, _ = request(
            create_url,
            method="POST",
            headers=token_header,
            body={"name": role_name},
        )
        if status == 201:
            log(f"  Role '{role_name}' created.")
        else:
            log(f"ERROR: Unexpected HTTP {status} when creating role '{role_name}'.")
            raise SystemExit(1)

    status, verified_roles = request(list_url, method="GET", headers=token_header)
    actual_names = (
        {role.get("name") for role in verified_roles}
        if status == 200 and isinstance(verified_roles, list)
        else set()
    )
    if not set(role_names).issubset(actual_names):
        log("ERROR: Realm role creation did not converge.")
        raise SystemExit(1)


def upsert_realm_role_composites_api(
    admin_url: str,
    token: str,
    realm: str,
    role_composites: dict[str, list[str]],
) -> None:
    """Reconcile realm-role composites declared by the stack."""
    token_header = {"Authorization": f"Bearer {token}"}
    roles_url = f"{admin_url}/admin/realms/{realm}/roles"
    status, data = request(roles_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(data, list):
        log(f"ERROR: Could not list realm roles (HTTP {status}).")
        raise SystemExit(1)

    role_map = {role["name"]: role for role in data if "name" in role and "id" in role}
    for parent_name, child_names in role_composites.items():
        parent = role_map.get(parent_name)
        if parent is None:
            log(f"ERROR: Composite parent realm role '{parent_name}' does not exist.")
            raise SystemExit(1)

        missing = sorted(set(child_names) - role_map.keys())
        if missing:
            log(
                f"ERROR: Composite child realm role(s) for '{parent_name}' do not exist: "
                f"{', '.join(missing)}"
            )
            raise SystemExit(1)

        composites_url = f"{admin_url}/admin/realms/{realm}/roles-by-id/{parent['id']}/composites"
        status, current = request(composites_url, method="GET", headers=token_header)
        if status != 200 or not isinstance(current, list):
            log(f"ERROR: Could not list composites for realm role '{parent_name}' (HTTP {status}).")
            raise SystemExit(1)

        desired_names = set(child_names)
        current_realm_roles = [
            role
            for role in current
            if role.get("clientRole") is False and role.get("containerId") == realm
        ]
        stale = [role for role in current_realm_roles if role.get("name") not in desired_names]
        if stale:
            status, response = request(
                composites_url,
                method="DELETE",
                headers={**token_header, "Content-Type": "application/json"},
                body=stale,
            )
            if status not in (200, 204):
                log(
                    f"ERROR: Could not remove stale composites from realm role "
                    f"'{parent_name}' (HTTP {status}): {response}"
                )
                raise SystemExit(1)

        current_ids = {role.get("id") for role in current_realm_roles}
        missing_roles = [
            role_map[name] for name in child_names if role_map[name]["id"] not in current_ids
        ]
        if missing_roles:
            status, response = request(
                composites_url,
                method="POST",
                headers={**token_header, "Content-Type": "application/json"},
                body=missing_roles,
            )
            if status not in (200, 204):
                log(
                    f"ERROR: Could not add composites to realm role '{parent_name}' "
                    f"(HTTP {status}): {response}"
                )
                raise SystemExit(1)


def _find_or_create_group_path(
    admin_url: str,
    token: str,
    realm: str,
    path: str,
    *,
    create: bool = True,
) -> str:
    """Return the ID for *path*, creating its missing hierarchy in order."""
    if not path.startswith("/") or path == "/":
        log(f"ERROR: Group path must be an absolute non-root path: {path!r}")
        raise SystemExit(1)
    segments = [segment for segment in path.split("/") if segment]
    if path != f"/{'/'.join(segments)}":
        log(f"ERROR: Group path contains an empty segment: {path!r}")
        raise SystemExit(1)

    token_header = {"Authorization": f"Bearer {token}"}
    parent_id: str | None = None
    for segment in segments:
        if parent_id is None:
            groups_url = (
                f"{admin_url}/admin/realms/{realm}/groups?briefRepresentation=false&max=1000"
            )
        else:
            groups_url = f"{admin_url}/admin/realms/{realm}/groups/{parent_id}/children?max=1000"
        status, groups = request(groups_url, method="GET", headers=token_header)
        if status != 200 or not isinstance(groups, list):
            log(f"ERROR: Could not list groups while resolving '{path}' (HTTP {status}).")
            raise SystemExit(1)

        existing = next(
            (group for group in groups if group.get("name") == segment and group.get("id")), None
        )
        if existing is None:
            if not create:
                log(f"ERROR: Required Keycloak group does not exist: '{path}'.")
                raise SystemExit(1)
            create_url = (
                f"{admin_url}/admin/realms/{realm}/groups"
                if parent_id is None
                else f"{admin_url}/admin/realms/{realm}/groups/{parent_id}/children"
            )
            status, response = request(
                create_url,
                method="POST",
                headers={**token_header, "Content-Type": "application/json"},
                body={"name": segment},
            )
            if status not in (201, 204, 409):
                log(f"ERROR: Could not create Keycloak group '{path}' (HTTP {status}): {response}")
                raise SystemExit(1)
            status, groups = request(groups_url, method="GET", headers=token_header)
            if status != 200 or not isinstance(groups, list):
                log(f"ERROR: Could not re-list groups after creating '{path}' (HTTP {status}).")
                raise SystemExit(1)
            existing = next(
                (group for group in groups if group.get("name") == segment and group.get("id")),
                None,
            )
            if existing is None:
                log(f"ERROR: Keycloak did not return the newly created group '{path}'.")
                raise SystemExit(1)
        parent_id = str(existing["id"])
    if parent_id is None:
        log(f"ERROR: Group path has no segments: {path!r}")
        raise SystemExit(1)
    return parent_id


def _reconcile_group_role_mappings(
    admin_url: str,
    token: str,
    url: str,
    group_path: str,
    kind: str,
    role_map: dict[str, dict[str, Any]],
    managed_role_names: set[str],
    desired_role_names: list[str],
) -> None:
    """Reconcile the stack-managed role mappings of one Keycloak group."""
    missing = sorted(set(desired_role_names) - role_map.keys())
    if missing:
        log(
            f"ERROR: {kind} role(s) configured for group '{group_path}' do not exist: "
            f"{', '.join(missing)}"
        )
        raise SystemExit(1)

    token_header = {"Authorization": f"Bearer {token}"}
    status, current = request(url, method="GET", headers=token_header)
    if status != 200 or not isinstance(current, list):
        log(f"ERROR: Could not list {kind} role mappings for group '{group_path}' (HTTP {status}).")
        raise SystemExit(1)

    desired_names = set(desired_role_names)
    stale = [
        role
        for role in current
        if role.get("name") in managed_role_names and role.get("name") not in desired_names
    ]
    if stale:
        status, response = request(
            url,
            method="DELETE",
            headers={**token_header, "Content-Type": "application/json"},
            body=stale,
        )
        if status not in (200, 204):
            log(
                f"ERROR: Could not remove stale {kind} roles from group '{group_path}' "
                f"(HTTP {status}): {response}"
            )
            raise SystemExit(1)

    current_names = {role.get("name") for role in current}
    missing_roles = [role_map[name] for name in desired_role_names if name not in current_names]
    if missing_roles:
        status, response = request(
            url,
            method="POST",
            headers={**token_header, "Content-Type": "application/json"},
            body=missing_roles,
        )
        if status not in (200, 204):
            log(
                f"ERROR: Could not add {kind} roles to group '{group_path}' "
                f"(HTTP {status}): {response}"
            )
            raise SystemExit(1)


def upsert_groups_api(
    admin_url: str,
    token: str,
    realm: str,
    groups: dict[str, dict[str, Any]],
    managed_realm_role_names: set[str],
) -> None:
    """Create configured groups and reconcile their realm and client-role mappings."""
    token_header = {"Authorization": f"Bearer {token}"}
    roles_url = f"{admin_url}/admin/realms/{realm}/roles"
    status, realm_roles = request(roles_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(realm_roles, list):
        log(f"ERROR: Could not list realm roles (HTTP {status}).")
        raise SystemExit(1)
    realm_role_map = {role["name"]: role for role in realm_roles if "name" in role and "id" in role}

    group_ids = {
        path: _find_or_create_group_path(admin_url, token, realm, path)
        for path in sorted(groups, key=lambda value: (value.count("/"), value))
    }
    client_role_maps: dict[str, dict[str, dict[str, Any]]] = {}
    for definition in groups.values():
        client_roles = definition.get("clientRoles", {})
        if not isinstance(client_roles, dict):
            log("ERROR: Group clientRoles must be a mapping of client IDs to role lists.")
            raise SystemExit(1)
        for client_id in client_roles:
            if client_id in client_role_maps:
                continue
            client_uuid = find_client_uuid(admin_url, token, realm, client_id)
            if client_uuid is None:
                log(f"ERROR: Client '{client_id}' configured for group roles does not exist.")
                raise SystemExit(1)
            roles_url = f"{admin_url}/admin/realms/{realm}/clients/{client_uuid}/roles"
            status, client_roles_data = request(roles_url, method="GET", headers=token_header)
            if status != 200 or not isinstance(client_roles_data, list):
                log(f"ERROR: Could not list roles for client '{client_id}' (HTTP {status}).")
                raise SystemExit(1)
            client_role_maps[client_id] = {
                role["name"]: role for role in client_roles_data if "name" in role and "id" in role
            }

    for path, definition in groups.items():
        realm_role_names = definition.get("realmRoles", [])
        if not isinstance(realm_role_names, list) or not all(
            isinstance(name, str) for name in realm_role_names
        ):
            log(f"ERROR: Group '{path}' realmRoles must be a list of role names.")
            raise SystemExit(1)
        group_id = group_ids[path]
        _reconcile_group_role_mappings(
            admin_url,
            token,
            f"{admin_url}/admin/realms/{realm}/groups/{group_id}/role-mappings/realm",
            path,
            "realm",
            realm_role_map,
            managed_realm_role_names,
            realm_role_names,
        )
        for client_id, client_role_names in definition.get("clientRoles", {}).items():
            if not isinstance(client_role_names, list) or not all(
                isinstance(name, str) for name in client_role_names
            ):
                log(f"ERROR: Group '{path}' client role names must be a list of strings.")
                raise SystemExit(1)
            client_uuid = find_client_uuid(admin_url, token, realm, client_id)
            if client_uuid is None:
                log(f"ERROR: Client '{client_id}' configured for group roles does not exist.")
                raise SystemExit(1)
            client_role_map = client_role_maps[client_id]
            _reconcile_group_role_mappings(
                admin_url,
                token,
                f"{admin_url}/admin/realms/{realm}/groups/{group_id}/role-mappings/clients/{client_uuid}",
                path,
                f"client '{client_id}'",
                client_role_map,
                set(client_role_map),
                client_role_names,
            )


def assign_user_groups_api(
    admin_url: str,
    token: str,
    realm: str,
    user_id: str,
    username: str,
    group_paths: list[str],
) -> None:
    """Add a user to configured groups without granting direct roles."""
    token_header = {"Authorization": f"Bearer {token}"}
    for path in group_paths:
        group_id = _find_or_create_group_path(admin_url, token, realm, path, create=False)
        membership_url = f"{admin_url}/admin/realms/{realm}/users/{user_id}/groups/{group_id}"
        status, response = request(membership_url, method="PUT", headers=token_header)
        if status != 204:
            log(
                f"ERROR: Could not add user '{username}' to group '{path}' "
                f"(HTTP {status}): {response}"
            )
            raise SystemExit(1)


# ── User operations ────────────────────────────────────────────────────────


def upsert_user_api(
    admin_url: str,
    token: str,
    realm: str,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    group_paths: list[str],
    required_actions: list[str],
) -> None:
    """Idempotently create or update a user and add it to access groups.

    Searches for the user by *username* and creates or updates it.
    If the user does not exist, creates it without a password and applies
    *required_actions*. Existing users retain their credential, verification,
    and completed-action state while their profile is updated.

    The caller supplies access-group paths rather than direct role names so
    application authorization remains based on group-derived role claims.
    """
    token_header = {"Authorization": f"Bearer {token}"}

    # ── Look up user by username ───────────────────────────────────────
    search_url = _exact_user_search_url(admin_url, realm, username)
    status, data = request(search_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(data, list):
        log(f"ERROR: Could not search for user '{username}' (HTTP {status}).")
        raise SystemExit(1)

    if len(data) > 1:
        log(f"ERROR: More than one exact user match exists for '{username}'.")
        raise SystemExit(1)
    if len(data) == 0:
        # ── User does not exist — create ───────────────────────────────
        log(f"User '{username}' not found — creating …")
        create_url = f"{admin_url}/admin/realms/{realm}/users"
        user_body: dict[str, Any] = {
            "username": username,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": True,
            "emailVerified": False,
            "requiredActions": required_actions,
        }
        status, _ = request(
            create_url,
            method="POST",
            headers={**token_header, "Content-Type": "application/json"},
            body=user_body,
        )
        if status == 201:
            log(f"  User '{username}' created without credentials.")
        else:
            log(f"  ERROR: Failed to create user '{username}' (HTTP {status}).")
            raise SystemExit(1)

        # Re-fetch to get the new user's ID
        status, data = request(search_url, method="GET", headers=token_header)
        if status != 200 or not isinstance(data, list) or len(data) != 1:
            log("ERROR: Could not re-fetch newly created user.")
            raise SystemExit(1)
        user_id: str = data[0]["id"]
    else:
        # ── User exists ────────────────────────────────────────────────
        user_id = data[0]["id"]
        log(f"User '{username}' already exists (id={user_id}).")

        log(f"  Updating profile for '{username}' (credentials and actions unchanged) …")
        update_url = f"{admin_url}/admin/realms/{realm}/users/{user_id}"
        update_body: dict[str, Any] = {
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
        }
        status, _ = request(
            update_url,
            method="PUT",
            headers={**token_header, "Content-Type": "application/json"},
            body=update_body,
        )
        if status == 204:
            log(f"  Profile updated for '{username}'.")
        else:
            log(f"ERROR: Could not update profile for '{username}' (HTTP {status}).")
            raise SystemExit(1)

    assign_user_groups_api(admin_url, token, realm, user_id, username, group_paths)


def send_user_actions_email_api(
    admin_url: str,
    token: str,
    realm: str,
    username: str,
    lifespan: int,
) -> None:
    """Send the user's remaining required actions in one short-lived email."""
    lifespan = _bounded_action_token_lifespan(str(lifespan))
    token_header = {"Authorization": f"Bearer {token}"}
    search_url = _exact_user_search_url(admin_url, realm, username)
    status, users = request(search_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(users, list):
        log(f"ERROR: Could not search for user '{username}' (HTTP {status}).")
        raise SystemExit(1)
    if len(users) != 1:
        log(f"ERROR: Expected exactly one user named '{username}', found {len(users)}.")
        raise SystemExit(1)
    user_id = users[0].get("id")
    if not isinstance(user_id, str) or not user_id:
        log(f"ERROR: Keycloak returned no user ID for '{username}'.")
        raise SystemExit(1)
    user_url = f"{admin_url}/admin/realms/{realm}/users/{user_id}"
    status, user = request(user_url, method="GET", headers=token_header)
    if status != 200 or not isinstance(user, dict):
        log(f"ERROR: Could not read required actions for '{username}' (HTTP {status}).")
        raise SystemExit(1)
    actions = user.get("requiredActions", [])
    if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
        log(f"ERROR: Keycloak returned invalid required actions for '{username}'.")
        raise SystemExit(1)
    if not actions:
        log(f"ERROR: User '{username}' has no remaining required actions.")
        raise SystemExit(1)
    email_url = f"{user_url}/execute-actions-email?{urlencode({'lifespan': lifespan})}"
    status, _ = request(
        email_url,
        method="PUT",
        headers={**token_header, "Content-Type": "application/json"},
        body=actions,
    )
    if status != 204:
        log(f"ERROR: Could not send required-action email for '{username}' (HTTP {status}).")
        raise SystemExit(1)
    log(f"Sent required-action email for '{username}' ({', '.join(actions)}; {lifespan}s).")
