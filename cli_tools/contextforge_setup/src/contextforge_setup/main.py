"""Operator entry point; no live API operation runs on import."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings
from pathlib import Path
from typing import Never, override

from contextforge_setup.accounts import reconcile_accounts
from contextforge_setup.client import Client
from contextforge_setup.config import Config, SetupError, email, load_config
from contextforge_setup.registration_config import (
    RegistrationConfig,
    load_registration_config,
    require_supported_registration_apply,
)
from contextforge_setup.registrations import reconcile_registrations


class _Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        # argparse's normal error includes unknown raw argv, potentially an accidental secret.
        self.exit(
            2,
            "Invalid arguments; use contextforge-setup --help. "
            "Credentials are never CLI arguments.\n",
        )


def main() -> int:
    """Reconcile approved accounts, app registrations and explicitly approved tools."""
    parser = _Parser(description="Reconcile approved native ContextForge resources")
    parser.add_argument("operation", choices=["reconcile-accounts", "reconcile-registrations"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        help="Generated registrations.json from Base; config then contains only origin/team/owner",
    )
    parser.add_argument("--ca-cert", type=Path)
    parser.add_argument("--allow-loopback-http", action="store_true")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Use native admin email/password instead of an admin token",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Authorize native changes; keep gateway MCP routes blocked",
    )
    parser.add_argument(
        "--update-owned-tools",
        action="store_true",
        help="Explicitly replace only owned server tool membership; registrations only",
    )
    parser.add_argument(
        "--refresh-oauth",
        action="store_true",
        help="Refresh after native operator consent; requires --update-owned-tools",
    )
    args = parser.parse_args()
    api: Client | None = None
    try:
        config = _configuration(args)
        if not args.apply:
            count = (
                len(config.registrations)
                if isinstance(config, RegistrationConfig)
                else len(config.accounts)
            )
            print(
                f"Validated {count} {args.operation.removeprefix('reconcile-')}; no API calls. "
                "Use --apply for reconciliation."
            )
            return 0
        if isinstance(config, RegistrationConfig):
            require_supported_registration_apply(config)
        api = Client(config.origin, args.ca_cert)
        _authenticate_operator(api, config, login=args.login)
        if isinstance(config, RegistrationConfig):
            mappings = reconcile_registrations(
                api,
                config,
                update_owned_tools=args.update_owned_tools,
                refresh_oauth=args.refresh_oauth,
                resolve_secret=lambda spec: _secret(
                    dict(config.oauth_secret_env).get(spec.id, ""),
                    f"Approved {spec.id} operator app secret (contextforge-oauth-apps:{spec.id}): ",
                ),
            )
            print(json.dumps({"registrations": mappings}, sort_keys=True))
        else:
            reconcile_accounts(api, config)
            print(
                f"Verified {len(config.accounts)} native account(s); "
                "no registrations or gateway permissions changed."
            )
    except (SetupError, EOFError, KeyboardInterrupt) as error:
        print(
            str(error)
            if isinstance(error, SetupError)
            else "Operation cancelled; keep routes blocked",
            file=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 - Never expose unexpected credential-bearing API errors.
        print(
            "Operation failed; details hidden. Keep routes blocked and inspect privately",
            file=sys.stderr,
        )
        return 1
    finally:
        if api is not None:
            api.close()
    return 0


def _configuration(args: argparse.Namespace) -> Config | RegistrationConfig:
    if args.operation == "reconcile-registrations":
        if args.refresh_oauth and not args.update_owned_tools:
            raise SetupError("--refresh-oauth requires explicit --update-owned-tools")
        return load_registration_config(
            args.config, allow_loopback_http=args.allow_loopback_http, catalog=args.catalog
        )
    if args.update_owned_tools or args.catalog or args.refresh_oauth:
        raise SetupError(
            "--catalog/--update-owned-tools/--refresh-oauth require reconcile-registrations"
        )
    return load_config(args.config, allow_loopback_http=args.allow_loopback_http)


def _authenticate_operator(
    api: Client, config: Config | RegistrationConfig, *, login: bool
) -> None:
    if (
        not isinstance(config, RegistrationConfig)
        or config.operator_authentication == "native-session"
    ):
        _authenticate(api, login=login)
        return
    if login or any(
        os.environ.get(name)
        for name in (
            "CONTEXTFORGE_ADMIN_TOKEN",
            "CONTEXTFORGE_ADMIN_EMAIL",
            "CONTEXTFORGE_ADMIN_PASSWORD",
        )
    ):
        raise SetupError("Choose fixed trusted-proxy operator identity or native login, not both")
    api.authenticate_operator_proxy(config.owner_email)


def _authenticate(api: Client, *, login: bool) -> None:
    if login:
        if os.environ.get("CONTEXTFORGE_ADMIN_TOKEN"):
            raise SetupError("Choose native login or admin token, not both")
        admin_email = email(
            os.environ.get("CONTEXTFORGE_ADMIN_EMAIL") or input("Native admin email: ")
        )
        password = _secret("CONTEXTFORGE_ADMIN_PASSWORD", "Native admin password: ")
        try:
            api.login(admin_email, password)
        finally:
            password = ""
    else:
        token = _secret("CONTEXTFORGE_ADMIN_TOKEN", "Native admin token: ")
        try:
            api.authenticate(token)
        finally:
            token = ""


def _secret(name: str, prompt: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise SetupError(
            "Hidden input is unavailable; supply the credential through private environment custody"
        ) from None


if __name__ == "__main__":
    raise SystemExit(main())
