"""Idempotently create/update the Keycloak realm used by all apps.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Imports shared helpers from :mod:`k8s_stack_tooling.api`.
"""

from __future__ import annotations

import os
import sys

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token, upsert_realm_api


def main() -> None:
    base_url: str = os.environ["KC_INTERNAL_URL"]
    health_port: str = os.environ.get("KC_HEALTH_PORT", "9000")
    health_url = f"{base_url}:{health_port}/health/ready"
    admin_user: str = os.environ["KC_ADMIN_USER"]
    admin_pass: str = os.environ["KC_ADMIN_PASSWORD"]
    realm: str = os.environ["KC_REALM"]

    log("=== Realm init started ===")
    log(f"  KC_INTERNAL_URL = {base_url}")
    log(f"  KC_HEALTH_PORT  = {health_port}")
    log(f"  Health URL      = {health_url}")
    log(f"  KC_ADMIN_USER   = {admin_user}")
    log(f"  KC_REALM        = {realm}")

    wait_for_service(health_url, prefix="keycloak-realm-init")
    token = get_admin_token(base_url, admin_user, admin_pass)
    upsert_realm_api(base_url, token, realm)

    log(f"=== Realm init complete: {realm} ===")


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
