"""Read-only safety gate: run before upstream run-migrate.sh, and again in acceptance.

Use a clean Job environment, never envFrom production resources. Required env:
GLITCHTIP_NATIVE_TEST_CONFIRM=DISPOSABLE_DATABASE; GLITCHTIP_NATIVE_TEST_DB_{HOST,
NAME,USER,PORT}; DATABASE_URL matching those four values; synthetic SECRET_KEY;
GLITCHTIP_URL=https://<test-name>.invalid; and the exact values in REQUIRED_ENV.
Only the isolated test PG18.6 service is contacted. This file never migrates,
creates cache tables, enqueues tasks, starts workers, or prints private details.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import unquote, urlsplit

REQUIRED_ENV = {
    "GLITCHTIP_NATIVE_TEST_CONFIRM": "DISPOSABLE_DATABASE",
    "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
    "PYTHONPATH": "/code",
    "GLITCHTIP_VERSION": "6.2.6",
    "VALKEY_URL": "",
    "REDIS_URL": "",
    "DEBUG": "false",
    "TESTING": "false",
    "IS_WORKER": "false",
    "GLITCHTIP_EMBED_WORKER": "false",
    "VTASKS_RUN_SCHEDULER": "false",
    "VTASKS_BACKEND": "default",
    "GLITCHTIP_ENABLE_UPTIME": "true",
    "GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS": "false",
    "EMAIL_BACKEND": "django.core.mail.backends.dummy.EmailBackend",
}
DB_TASK_BACKEND = "django_vtasks.backends.db.DatabaseTaskBackend"
DB_CACHE_BACKEND = "django.core.cache.backends.db.DatabaseCache"


class GuardError(RuntimeError):
    """A fixed message that never includes environment or connection values."""


def validate_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Reject alternate targets and inherited credentials before importing Django."""
    if any(environ.get(key) != value for key, value in REQUIRED_ENV.items()):
        raise GuardError("Explicit isolated native-test environment is required")
    if not environ.get("SECRET_KEY"):
        raise GuardError("A synthetic native-test secret key is required")
    for key, value in environ.items():
        if value and _forbidden_environment_key(key):
            raise GuardError("Alternate connections or inherited credentials are forbidden")
    expected = {}
    for field in ("HOST", "NAME", "USER", "PORT"):
        value = environ.get(f"GLITCHTIP_NATIVE_TEST_DB_{field}", "")
        if not value or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise GuardError("Explicit disposable database identity is required")
        expected[field] = value
    if not expected["NAME"].startswith("glitchtip_provisioning_test_"):
        raise GuardError("A dedicated disposable database name is required")
    database_url = environ.get("DATABASE_URL", "")
    if any(ord(char) < 33 or ord(char) == 127 for char in database_url):
        raise GuardError("Database URL contains invalid control characters")
    target = urlsplit(database_url)
    if (
        target.scheme not in {"postgres", "postgresql"}
        or target.query
        or target.fragment
        or not target.password
        or target.hostname != expected["HOST"]
        or str(target.port or 5432) != expected["PORT"]
        or unquote(target.path.lstrip("/")) != expected["NAME"]
        or unquote(target.username or "") != expected["USER"]
    ):
        raise GuardError("Database URL must match the explicit disposable identity")
    public = urlsplit(environ.get("GLITCHTIP_URL", ""))
    if (
        public.scheme != "https"
        or not public.hostname
        or not public.hostname.endswith(".invalid")
        or public.username
        or public.password
        or public.query
        or public.fragment
    ):
        raise GuardError("An explicit synthetic public endpoint is required")
    return expected


def _forbidden_environment_key(key: str) -> bool:
    if key in {"SECRET_KEY", "GPG_KEY"}:  # Synthetic runtime key; public Python image signing key.
        return False
    if key in {
        "MAINTENANCE_DATABASE_URL",
        "READ_ONLY_DATABASE_URL",
        "EMAIL_URL",
        "KUBECONFIG",
        "SENTRY_DSN",
        "GLITCHTIP_DSN",
    }:
        return True
    database_capacity = {
        "DATABASE_URL",
        "DATABASE_POOL_MIN_SIZE",
        "DATABASE_POOL_MAX_SIZE",
        "DATABASE_CONN_MAX_AGE",
        "DATABASE_CONN_HEALTH_CHECKS",
        "DATABASE_POOL_TIMEOUT",
        "DATABASE_POOL_MAX_LIFETIME",
    }
    if key.startswith(("DATABASE_", "PG")) and key not in database_capacity:
        return True
    if key.startswith(("VALKEY_", "REDIS_")) and key not in {"VALKEY_URL", "REDIS_URL"}:
        return True
    return key.startswith(
        ("VAULT_", "BAO_", "OPENBAO_", "AWS_", "AZURE_", "DJANGO_SUPERUSER_", "GLITCHTIP_OWNER_")
    ) or key.endswith(
        ("_TOKEN", "_TOKEN_FILE", "_KEY", "_SECRET", "_DSN", "_PASSWORD", "_CREDENTIALS")
    )


def validate_runtime(expected: dict[str, str]) -> None:
    """Check native settings and the actual server using read-only queries only."""
    import django
    from django.conf import settings
    from django.db import connection, connections

    if sys.version_info[:2] != (3, 14):
        raise GuardError("Native guard requires the pinned upstream Python runtime")
    django.setup()
    if (
        settings.GLITCHTIP_VERSION != "6.2.6"
        or settings.TESTING
        or settings.DEBUG
        or settings.IS_WORKER
        or not settings.GLITCHTIP_ENABLE_UPTIME
    ):
        raise GuardError("Native runtime must not use test/immediate mode or workers")
    if (
        set(settings.DATABASES) != {"default"}
        or set(connections) != {"default"}
        or settings.MAINTENANCE_DATABASE_ALIAS != "default"
    ):
        raise GuardError("Alternate database connections are forbidden")
    database = settings.DATABASES["default"]
    if any(str(database.get(field, "")) != value for field, value in expected.items()) or set(
        database.get("OPTIONS", {})
    ) - {"pool"}:
        raise GuardError("Native database configuration differs from the disposable identity")
    if (
        set(settings.TASKS) != {"default"}
        or settings.TASKS["default"] != {"BACKEND": DB_TASK_BACKEND}
        or set(settings.CACHES) != {"default"}
        or settings.CACHES["default"].get("BACKEND") != DB_CACHE_BACKEND
        or settings.CACHES["default"].get("LOCATION") != "django_cache"
        or settings.EMAIL_BACKEND != REQUIRED_ENV["EMAIL_BACKEND"]
    ):
        raise GuardError("Only native DB task/cache backends and dummy email are permitted")
    if connection.vendor != "postgresql":
        raise GuardError("Native acceptance requires the isolated PostgreSQL server")
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), current_user")
        if tuple(cursor.fetchone()) != (expected["NAME"], expected["USER"]):
            raise GuardError("Actual disposable database identity did not match")
        for statement, wanted in (
            ("SHOW server_version_num", "180006"),
            ("SHOW max_locks_per_transaction", "64"),
            ("SHOW transaction_read_only", "off"),
            ("SHOW default_transaction_read_only", "off"),
        ):
            cursor.execute(statement)
            if str(cursor.fetchone()[0]) != wanted:
                raise GuardError("Disposable PostgreSQL settings are incompatible")


def verify() -> None:
    """Run both gates before any application initialization command."""
    expected = validate_environment(os.environ)
    validate_runtime(expected)


def main() -> int:
    """Emit only a non-sensitive pass/fail result, never native exception details."""
    logging.disable(logging.CRITICAL)
    try:
        with Path(os.devnull).open("w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            verify()
    except Exception:
        print(
            "Native pre-migration guard failed; private connection details hidden", file=sys.stderr
        )
        return 1
    print("Native pre-migration guard passed: isolated PG18.6/locks64, DB broker, no workers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
