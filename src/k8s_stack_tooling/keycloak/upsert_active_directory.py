"""Reconcile the managed Microsoft Active Directory provider in Keycloak.

The Helm Job contract uses the common ``KC_INTERNAL_URL``, ``KC_HEALTH_PORT``,
``KC_ADMIN_USER``, ``KC_ADMIN_PASSWORD``, and ``KC_REALM`` variables. Its
provider-specific contract is:

- ``KC_ACTIVE_DIRECTORY_ENABLED``: ``true`` or ``false``; defaults to ``false``.
- ``KC_ACTIVE_DIRECTORY_CONNECTION_URL``: an explicit ``ldaps://host:636`` URL.
- ``KC_ACTIVE_DIRECTORY_USERS_DN`` and ``KC_ACTIVE_DIRECTORY_GROUPS_DN``.
- ``KC_ACTIVE_DIRECTORY_USERNAME_ATTRIBUTE``: ``sAMAccountName`` or
  ``userPrincipalName``.
- ``KC_ACTIVE_DIRECTORY_GROUP_NAMES``: a non-empty JSON array of access-group names.
- ``KC_ACTIVE_DIRECTORY_BIND_DN``: an AD bind DN or UPN principal, plus
  ``KC_ACTIVE_DIRECTORY_BIND_CREDENTIAL``.
- ``KC_ACTIVE_DIRECTORY_EMAIL_VERIFIED``: required to be ``true``.

Only ``KC_ACTIVE_DIRECTORY_ENABLED`` is read in disabled mode, so no bind
credential needs to be materialized when federation is disabled.
"""

import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import NoReturn

import requests

from k8s_stack_tooling.api.http import wait_for_service
from k8s_stack_tooling.api.keycloak import get_admin_token
from k8s_stack_tooling.api.keycloak_active_directory import (
    ActiveDirectoryConfig,
    ActiveDirectoryError,
    reconcile_active_directory,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ENV_PREFIX = "KC_ACTIVE_DIRECTORY_"


def _die(message: str) -> NoReturn:
    logger.error(message)
    raise SystemExit(1)


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        _die(f"{name} is required when KC_ACTIVE_DIRECTORY_ENABLED is true")
    return value


def _required_secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value.strip():
        _die(f"{name} is required when KC_ACTIVE_DIRECTORY_ENABLED is true")
    return value


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    _die(f"{name} must be true or false")


def config_from_environment(
    environment: Mapping[str, str],
) -> ActiveDirectoryConfig | None:
    enabled = _parse_bool(
        environment.get(f"{ENV_PREFIX}ENABLED", "false"),
        f"{ENV_PREFIX}ENABLED",
    )
    if not enabled:
        return None

    email_verified = _parse_bool(
        _required(environment, f"{ENV_PREFIX}EMAIL_VERIFIED"),
        f"{ENV_PREFIX}EMAIL_VERIFIED",
    )
    raw_groups = _required(environment, f"{ENV_PREFIX}GROUP_NAMES")
    try:
        parsed_groups = json.loads(raw_groups)
    except json.JSONDecodeError:
        _die(f"{ENV_PREFIX}GROUP_NAMES must be a JSON array of strings")
    if not isinstance(parsed_groups, list) or not all(
        isinstance(group, str) for group in parsed_groups
    ):
        _die(f"{ENV_PREFIX}GROUP_NAMES must be a JSON array of strings")

    try:
        return ActiveDirectoryConfig(
            connection_url=_required(environment, f"{ENV_PREFIX}CONNECTION_URL"),
            users_dn=_required(environment, f"{ENV_PREFIX}USERS_DN"),
            groups_dn=_required(environment, f"{ENV_PREFIX}GROUPS_DN"),
            username_attribute=_required(environment, f"{ENV_PREFIX}USERNAME_ATTRIBUTE"),
            group_names=tuple(parsed_groups),
            bind_dn=_required(environment, f"{ENV_PREFIX}BIND_DN"),
            bind_credential=_required_secret(environment, f"{ENV_PREFIX}BIND_CREDENTIAL"),
            email_verified=email_verified,
        )
    except ValueError as exc:
        _die(str(exc))


def main() -> None:
    keycloak_url = os.environ["KC_INTERNAL_URL"]
    health_port = os.environ.get("KC_HEALTH_PORT", "9000")
    realm_name = os.environ["KC_REALM"]
    try:
        config = config_from_environment(os.environ)
        wait_for_service(
            f"{keycloak_url}:{health_port}/health/ready",
            prefix="keycloak-active-directory",
        )
        token = get_admin_token(
            keycloak_url,
            os.environ["KC_ADMIN_USER"],
            os.environ["KC_ADMIN_PASSWORD"],
        )
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {token}"})
            reconcile_active_directory(keycloak_url, realm_name, session, config)
    except (ActiveDirectoryError, requests.RequestException) as exc:
        logger.error("Active Directory reconciliation failed: %s", exc)
        sys.exit(1)

    state = "enabled" if config is not None else "disabled"
    logger.info("Active Directory federation is reconciled and %s", state)


if __name__ == "__main__":
    main()
