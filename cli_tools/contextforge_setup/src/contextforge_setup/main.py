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
from contextforge_setup.registration_config import RegistrationConfig, load_registration_config
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
    """Reconcile approved native accounts or scoped registrations, never credentials."""
    parser = _Parser(description="Reconcile approved native ContextForge resources")
    parser.add_argument("operation", choices=["reconcile-accounts", "reconcile-registrations"])
    parser.add_argument("--config", type=Path, required=True)
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
        api = Client(config.origin, args.ca_cert)
        _authenticate(api, login=args.login)
        if isinstance(config, RegistrationConfig):
            mappings = reconcile_registrations(
                api, config, update_owned_tools=args.update_owned_tools
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
    finally:
        if api is not None:
            api.close()
    return 0


def _configuration(args: argparse.Namespace) -> Config | RegistrationConfig:
    if args.operation == "reconcile-registrations":
        return load_registration_config(args.config, allow_loopback_http=args.allow_loopback_http)
    if args.update_owned_tools:
        raise SetupError("--update-owned-tools is only supported by reconcile-registrations")
    return load_config(args.config, allow_loopback_http=args.allow_loopback_http)


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
