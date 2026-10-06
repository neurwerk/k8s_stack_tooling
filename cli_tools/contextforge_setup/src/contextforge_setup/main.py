"""Operator entry point; no live API operation runs on import."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from pathlib import Path
from typing import Never, override

from contextforge_setup.accounts import reconcile_accounts
from contextforge_setup.client import Client
from contextforge_setup.config import SetupError, email, load_config


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
    """Reconcile only explicitly approved native accounts, never provider credentials."""
    parser = _Parser(description="Reconcile limited native ContextForge accounts")
    parser.add_argument("operation", choices=["reconcile-accounts"])
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
        help="Authorize account/role/membership mutations; keep gateway routes blocked",
    )
    args = parser.parse_args()
    api: Client | None = None
    try:
        config = load_config(args.config, allow_loopback_http=args.allow_loopback_http)
        if not args.apply:
            print(
                f"Validated {len(config.accounts)} account(s); no API calls. "
                "Use --apply for reconciliation."
            )
            return 0
        api = Client(config.origin, args.ca_cert)
        _authenticate(api, login=args.login)
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
