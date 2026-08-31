"""Idempotently create/update a user and add it to Keycloak access groups.

Run as a Helm post-install/post-upgrade Job inside the cluster.
Imports shared helpers from :mod:`k8s_stack_tooling.api`.
"""

from __future__ import annotations

import json
import os
import sys

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token, upsert_user_api


def main() -> None:
    base_url: str = os.environ["KC_INTERNAL_URL"]
    health_port: str = os.environ.get("KC_HEALTH_PORT", "9000")
    health_url = f"{base_url}:{health_port}/health/ready"
    admin_user: str = os.environ["KC_ADMIN_USER"]
    admin_pass: str = os.environ["KC_ADMIN_PASSWORD"]
    realm: str = os.environ["KC_REALM"]
    username: str = os.environ["KC_INITIAL_USER_USERNAME"]
    email: str = os.environ["KC_INITIAL_USER_EMAIL"]
    first_name: str = os.environ["KC_INITIAL_USER_FIRST_NAME"]
    last_name: str = os.environ["KC_INITIAL_USER_LAST_NAME"]
    group_paths_raw: str = os.environ["KC_INITIAL_USER_GROUPS"]
    required_actions_raw: str = os.environ["KC_INITIAL_USER_REQUIRED_ACTIONS"]

    log("=== User init started ===")
    log(f"  KC_INTERNAL_URL            = {base_url}")
    log(f"  KC_HEALTH_PORT             = {health_port}")
    log(f"  Health URL                 = {health_url}")
    log(f"  KC_ADMIN_USER              = {admin_user}")
    log(f"  KC_REALM                   = {realm}")
    log(f"  KC_INITIAL_USER_USERNAME   = {username}")
    log(f"  KC_INITIAL_USER_EMAIL      = {email}")
    log(f"  KC_INITIAL_USER_FIRST_NAME = {first_name}")
    log(f"  KC_INITIAL_USER_LAST_NAME  = {last_name}")
    log(f"  KC_INITIAL_USER_GROUPS     = {group_paths_raw}")
    log(f"  KC_INITIAL_USER_REQUIRED_ACTIONS = {required_actions_raw}")

    # Accept either a JSON array ('["/access/admins"]') or a comma-separated list
    # ("a,b") — the chart documents the JSON form, but comma-separated is
    # convenient for ad-hoc runs.
    try:
        parsed = json.loads(group_paths_raw)
        group_paths = (
            [str(r).strip() for r in parsed if str(r).strip()]
            if isinstance(parsed, list)
            else [group_paths_raw.strip()]
        )
    except json.JSONDecodeError:
        group_paths = [r.strip() for r in group_paths_raw.split(",") if r.strip()]
    log(f"  Parsed group paths         = {json.dumps(group_paths)}")
    try:
        required_actions = json.loads(required_actions_raw)
    except json.JSONDecodeError:
        log("ERROR: KC_INITIAL_USER_REQUIRED_ACTIONS must be a JSON array.")
        raise SystemExit(1) from None
    if not isinstance(required_actions, list) or not all(
        isinstance(action, str) and action.strip() for action in required_actions
    ):
        log("ERROR: KC_INITIAL_USER_REQUIRED_ACTIONS must contain nonblank action aliases.")
        raise SystemExit(1)

    wait_for_service(health_url, prefix="keycloak-user")
    token = get_admin_token(base_url, admin_user, admin_pass)
    upsert_user_api(
        base_url,
        token,
        realm,
        username,
        email,
        first_name,
        last_name,
        group_paths,
        required_actions,
    )

    log(f"=== User init complete: {username} ===")


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
