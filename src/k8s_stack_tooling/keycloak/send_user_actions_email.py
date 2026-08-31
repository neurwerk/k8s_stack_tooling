"""Send a Keycloak required-actions email for an existing human user."""

from __future__ import annotations

import os
import sys

from k8s_stack_tooling.api.http import log, wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token, send_user_actions_email_api


def main() -> None:
    """Send a bounded action email without handling its server-generated token."""
    base_url = os.environ["KC_INTERNAL_URL"]
    health_port = os.environ.get("KC_HEALTH_PORT", "9000")
    username = os.environ["KC_INITIAL_USER_USERNAME"]
    lifespan = int(os.environ.get("KC_ACTION_EMAIL_LIFESPAN", "1800"))
    wait_for_service(f"{base_url}:{health_port}/health/ready", prefix="keycloak-action-email")
    token = get_admin_token(base_url, os.environ["KC_ADMIN_USER"], os.environ["KC_ADMIN_PASSWORD"])
    send_user_actions_email_api(base_url, token, os.environ["KC_REALM"], username, lifespan)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"UNEXPECTED ERROR: {type(exc).__name__}")
        sys.exit(1)
