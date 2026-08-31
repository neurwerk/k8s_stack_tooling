"""Idempotently create/update realm-level roles in Keycloak.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Imports shared helpers from :mod:`k8s_stack_tooling.api`.
"""

from __future__ import annotations

import json
import os
import sys

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import (
    get_admin_token,
    upsert_groups_api,
    upsert_realm_role_composites_api,
    upsert_realm_roles_api,
)


def main() -> None:
    base_url: str = os.environ["KC_INTERNAL_URL"]
    health_port: str = os.environ.get("KC_HEALTH_PORT", "9000")
    health_url = f"{base_url}:{health_port}/health/ready"
    admin_user: str = os.environ["KC_ADMIN_USER"]
    admin_pass: str = os.environ["KC_ADMIN_PASSWORD"]
    realm: str = os.environ["KC_REALM"]
    role_names_raw: str = os.environ["KC_REALM_ROLES"]
    role_composites_raw: str = os.environ.get("KC_REALM_ROLE_COMPOSITES", "{}")
    groups_raw: str = os.environ.get("KC_ACCESS_GROUPS", "{}")

    log("=== Realm roles init started ===")
    log(f"  KC_INTERNAL_URL = {base_url}")
    log(f"  KC_HEALTH_PORT  = {health_port}")
    log(f"  Health URL      = {health_url}")
    log(f"  KC_ADMIN_USER   = {admin_user}")
    log(f"  KC_REALM        = {realm}")
    log(f"  KC_REALM_ROLES  = {role_names_raw}")

    role_names = [r.strip() for r in role_names_raw.split(",") if r.strip()]
    try:
        role_composites = json.loads(role_composites_raw)
        groups = json.loads(groups_raw)
    except json.JSONDecodeError as exc:
        log(f"ERROR: Keycloak authorization configuration is not valid JSON: {exc}")
        raise SystemExit(1) from exc
    if not isinstance(role_composites, dict) or not all(
        isinstance(parent, str)
        and isinstance(children, list)
        and all(isinstance(child, str) for child in children)
        for parent, children in role_composites.items()
    ):
        log("ERROR: KC_REALM_ROLE_COMPOSITES must be a JSON object of role-name arrays.")
        raise SystemExit(1)
    if not isinstance(groups, dict) or not all(
        isinstance(path, str) and isinstance(definition, dict)
        for path, definition in groups.items()
    ):
        log("ERROR: KC_ACCESS_GROUPS must be a JSON object keyed by group path.")
        raise SystemExit(1)

    wait_for_service(health_url, prefix="keycloak-realm-roles")
    token = get_admin_token(base_url, admin_user, admin_pass)
    upsert_realm_roles_api(base_url, token, realm, role_names)
    upsert_realm_role_composites_api(base_url, token, realm, role_composites)
    upsert_groups_api(base_url, token, realm, groups, set(role_names))

    log(f"=== Realm roles init complete: {', '.join(role_names)} ===")


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
