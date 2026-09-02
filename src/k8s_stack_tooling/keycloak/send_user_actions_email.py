"""Send a Keycloak required-actions email for an existing human user."""

from __future__ import annotations

import os
import sys
from urllib.parse import quote, urlsplit

from k8s_stack_tooling.api.http import log, request, wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token, send_user_actions_email_api


def _public_issuer(public_url: str, realm: str) -> str:
    """Return the expected issuer for a public HTTPS origin."""
    parsed = urlsplit(public_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        log("ERROR: KC_PUBLIC_URL must be an HTTPS origin without credentials, path, or query.")
        raise SystemExit(1)
    return f"https://{parsed.netloc}/realms/{quote(realm, safe='')}"


def main() -> None:
    """Send a bounded action email without handling its server-generated token."""
    base_url = os.environ["KC_INTERNAL_URL"]
    health_port = os.environ.get("KC_HEALTH_PORT", "9000")
    realm = os.environ["KC_REALM"]
    username = os.environ["KC_INITIAL_USER_USERNAME"]
    lifespan = int(os.environ.get("KC_ACTION_EMAIL_LIFESPAN", "1800"))
    wait_for_service(f"{base_url}:{health_port}/health/ready", prefix="keycloak-action-email")
    issuer = _public_issuer(os.environ["KC_PUBLIC_URL"], realm)
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    wait_for_service(discovery_url, prefix="keycloak-public-issuer")
    status, discovery = request(discovery_url)
    if status != 200 or not isinstance(discovery, dict) or discovery.get("issuer") != issuer:
        log("ERROR: Public OIDC discovery does not advertise the expected issuer.")
        raise SystemExit(1)
    token = get_admin_token(base_url, os.environ["KC_ADMIN_USER"], os.environ["KC_ADMIN_PASSWORD"])
    send_user_actions_email_api(base_url, token, realm, username, lifespan)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        log(f"UNEXPECTED ERROR: {type(exc).__name__}")
        sys.exit(1)
