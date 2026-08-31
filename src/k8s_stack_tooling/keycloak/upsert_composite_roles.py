"""Idempotently add client-level roles as composites of a realm role.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Uses the existing :mod:`k8s_stack_tooling.api` helpers.

Environment variables:
    KC_INTERNAL_URL     — internal Keycloak URL
    KC_ADMIN_USER       — master realm admin username
    KC_ADMIN_PASSWORD   — master realm admin password
    KC_REALM            — target realm name
    KC_PARENT_ROLE      — realm role that will get composite roles added
    KC_CLIENT_ID        — client that owns the client-level roles
    KC_CLIENT_ROLES     — comma-separated list of client-level role names
"""

from __future__ import annotations

import os
import sys

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token, upsert_composite_roles_api


def main() -> None:
    base_url: str = os.environ["KC_INTERNAL_URL"]
    health_port: str = os.environ.get("KC_HEALTH_PORT", "9000")
    health_url = f"{base_url}:{health_port}/health/ready"
    admin_user: str = os.environ["KC_ADMIN_USER"]
    admin_pass: str = os.environ["KC_ADMIN_PASSWORD"]
    realm: str = os.environ["KC_REALM"]
    parent_role: str = os.environ["KC_PARENT_ROLE"]
    client_id: str = os.environ["KC_CLIENT_ID"]
    client_roles_raw: str = os.environ["KC_CLIENT_ROLES"]

    log("=== Composite roles init started ===")
    log(f"  KC_INTERNAL_URL  = {base_url}")
    log(f"  KC_HEALTH_PORT   = {health_port}")
    log(f"  Health URL       = {health_url}")
    log(f"  KC_ADMIN_USER    = {admin_user}")
    log(f"  KC_REALM         = {realm}")
    log(f"  KC_PARENT_ROLE   = {parent_role}")
    log(f"  KC_CLIENT_ID     = {client_id}")
    log(f"  KC_CLIENT_ROLES  = {client_roles_raw}")

    client_role_names = [r.strip() for r in client_roles_raw.split(",") if r.strip()]

    wait_for_service(health_url, prefix="keycloak-composite-roles")
    token = get_admin_token(base_url, admin_user, admin_pass)
    upsert_composite_roles_api(base_url, token, realm, parent_role, client_id, client_role_names)

    log(f"=== Composite roles init complete: {', '.join(client_role_names)} ===")


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
