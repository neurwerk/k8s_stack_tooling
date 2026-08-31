"""Idempotently upsert an OIDC client in Keycloak.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Fully parameterized via environment variables -- no app-specific hardcoding.

``KC_CLIENT_SECRET`` optionally supplies the desired confidential-client secret from
a local Secret managed by External Secrets Operator and backed by OpenBao. When supplied,
the script verifies that Keycloak persisted the exact value. Public and JWKS-only clients
may omit it.

The optional env vars ``KC_REDIRECT_URIS`` and ``KC_WEB_ORIGINS`` accept JSON
arrays of URLs. The singular ``KC_REDIRECT_URI`` and ``KC_WEB_ORIGIN`` variables
remain supported for existing clients. ``KC_CLIENT_AUDIENCES`` (JSON array of
strings) and ``KC_SERVICE_ACCOUNT_ROLES`` (JSON array of
``{"clientId": …, "roleName": …}``) automate what was previously documented as
manual one-time setup steps in ``API_KEYS.md``.

``KC_CLIENT_ROLES`` optionally declares client roles created by this client
registration. They are intentionally add-only; role removals require an
explicit authorization-contract change rather than an incidental client update.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import (
    add_audience_mapper,
    assign_service_account_realm_roles,
    assign_service_account_role,
    get_admin_token,
    get_client_secret,
    upsert_client,
    upsert_client_roles,
)
from k8s_stack_tooling.api.kubernetes import run_with_reconciliation_lock

# ── Main ──────────────────────────────────────────────────────────────────


def _read_url_list(plural_name: str, singular_name: str) -> list[str]:
    raw_urls = os.environ.get(plural_name)
    if raw_urls is None:
        legacy_url = os.environ[singular_name]
        # Existing service-only clients intentionally have no browser URLs.
        if not legacy_url:
            return []
        urls = [legacy_url]
    else:
        try:
            parsed_urls = json.loads(raw_urls)
        except json.JSONDecodeError as exc:
            log(f"ERROR: {plural_name} must be a non-empty JSON array of URLs.")
            raise SystemExit(1) from exc
        if not isinstance(parsed_urls, list) or not parsed_urls:
            log(f"ERROR: {plural_name} must be a non-empty JSON array of URLs.")
            raise SystemExit(1)
        urls = []
        for value in parsed_urls:
            if not isinstance(value, str) or not value:
                log(f"ERROR: {plural_name} must contain non-empty URL strings.")
                raise SystemExit(1)
            urls.append(value)

    if len(set(urls)) != len(urls):
        log(f"ERROR: {plural_name} must not contain duplicate URLs.")
        raise SystemExit(1)

    for url in urls:
        try:
            parsed_url = urlsplit(url)
            hostname = parsed_url.hostname
            parsed_url.port
        except ValueError:
            log(f"ERROR: {plural_name} must contain absolute HTTPS URLs.")
            raise SystemExit(1) from None
        if (
            parsed_url.scheme.lower() != "https"
            or not hostname
            or any(character.isspace() for character in url)
        ):
            log(f"ERROR: {plural_name} must contain absolute HTTPS URLs.")
            raise SystemExit(1)
    return urls


def _reconcile() -> None:
    base_url: str = os.environ["KC_INTERNAL_URL"]
    health_port: str = os.environ.get("KC_HEALTH_PORT", "9000")
    health_url = f"{base_url}:{health_port}/health/ready"
    admin_user: str = os.environ["KC_ADMIN_USER"]
    admin_pass: str = os.environ["KC_ADMIN_PASSWORD"]
    realm: str = os.environ["KC_REALM"]
    client_id: str = os.environ["KC_CLIENT_ID"]
    redirect_uris = _read_url_list("KC_REDIRECT_URIS", "KC_REDIRECT_URI")
    web_origins = _read_url_list("KC_WEB_ORIGINS", "KC_WEB_ORIGIN")
    client_secret: str | None = os.environ.get("KC_CLIENT_SECRET")

    # Client access type — "confidential" (default) or "public" (PKCE, browser-based)
    access_type: str = os.environ.get("KC_CLIENT_ACCESS_TYPE", "confidential")

    # Optional audience mappers — JSON array of audience strings
    raw_audiences = os.environ.get("KC_CLIENT_AUDIENCES", "")
    audiences: list[str] = json.loads(raw_audiences) if raw_audiences else []

    # Optional service account roles — JSON array of {clientId, roleName}
    raw_roles = os.environ.get("KC_SERVICE_ACCOUNT_ROLES", "")
    sa_roles: list[dict[str, str]] = json.loads(raw_roles) if raw_roles else []

    # Optional service account realm roles — JSON array of role name strings
    raw_realm_roles = os.environ.get("KC_SERVICE_ACCOUNT_REALM_ROLES", "")
    sa_realm_roles: list[str] = json.loads(raw_realm_roles) if raw_realm_roles else []

    # Optional client roles — JSON array of role name strings.
    raw_client_roles = os.environ.get("KC_CLIENT_ROLES", "")
    client_roles: list[str] = json.loads(raw_client_roles) if raw_client_roles else []

    log("=== OIDC configuration started ===")
    log(f"  KC_INTERNAL_URL = {base_url}")
    log(f"  KC_HEALTH_PORT  = {health_port}")
    log(f"  Health URL      = {health_url}")
    log(f"  KC_ADMIN_USER   = {admin_user}")
    log(f"  KC_REALM        = {realm}")
    log(f"  KC_CLIENT_ID    = {client_id}")
    log(f"  Redirect URIs   = {json.dumps(redirect_uris)}")
    log(f"  Web origins     = {json.dumps(web_origins)}")
    log(f"  KC_CLIENT_ACCESS_TYPE = {access_type}")
    if audiences:
        log(f"  KC_CLIENT_AUDIENCES = {audiences}")
    if sa_roles:
        log(f"  KC_SERVICE_ACCOUNT_ROLES = {sa_roles}")
    if client_roles:
        log(f"  KC_CLIENT_ROLES = {client_roles}")

    wait_for_service(health_url, prefix="keycloak-oidc")
    token = get_admin_token(base_url, admin_user, admin_pass)
    uuid = upsert_client(
        base_url,
        token,
        realm,
        client_id,
        redirect_uris,
        web_origins,
        access_type,
        client_secret,
    )
    if client_secret is not None:
        current_secret = get_client_secret(base_url, token, realm, uuid)
        if current_secret != client_secret:
            log("ERROR: Keycloak client secret does not match the desired secret.")
            raise SystemExit(1)
        log("Keycloak client secret matches the desired secret.")

    upsert_client_roles(base_url, token, realm, uuid, client_id, client_roles)

    # Optional: add audience mapper(s)
    for audience in audiences:
        add_audience_mapper(base_url, token, realm, uuid, audience)

    # Optional: assign service account role(s)
    for role_def in sa_roles:
        assign_service_account_role(
            base_url,
            token,
            realm,
            uuid,
            role_def["clientId"],
            role_def["roleName"],
        )

    # Optional: assign service account realm role(s)
    if sa_realm_roles:
        assign_service_account_realm_roles(
            base_url,
            token,
            realm,
            uuid,
            sa_realm_roles,
        )

    log("=== OIDC configuration complete ===")
    log(f"  Realm:        {realm}")
    log(f"  Client ID:    {client_id}")
    log(f"  Redirect URIs: {json.dumps(redirect_uris)}")


def main() -> None:
    """Reconcile an OIDC client while holding the optional shared Lease."""
    run_with_reconciliation_lock(_reconcile)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"UNEXPECTED ERROR: {exc}")
        log(f"Type: {type(exc).__name__}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
