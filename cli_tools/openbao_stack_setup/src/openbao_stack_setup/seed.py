"""Seed a fresh OpenBao instance without file-based credential escrow."""

from __future__ import annotations

from dataclasses import dataclass

from openbao_stack_setup.catalog import secret_operator_policy
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS
from openbao_stack_setup.reconcile import (
    ReconciliationIdentity,
    reconcile_openbao,
)


@dataclass(frozen=True)
class SeedReport:
    """Summarize non-sensitive reconciliation counts."""

    external_records_changed: int
    internal_records_changed: int
    internal_fields_added: int


def seed_bootstrap(
    client: OpenBaoClient,
    provider_values: dict[str, dict[str, str]],
    smtp: dict[str, str],
    bootstrap_passwords: dict[str, str],
    identity: ReconciliationIdentity,
    active_directory: dict[str, str] | None = None,
) -> SeedReport:
    """Converge a fresh instance and refuse unexpected external record changes."""
    if set(provider_values) != set(PROVIDERS):
        raise OpenBaoError("Bootstrap provider set is incomplete")
    client.ensure_kv_v2_mount()
    smtp_values: dict[str, JsonValue] = {
        "smtpUsername": smtp.get("username", ""),
        "smtpPassword": smtp.get("password", ""),
    }
    records: dict[str, dict[str, JsonValue]] = {
        path: dict(smtp_values) for path in MANAGED_CREDENTIALS["smtp"].paths
    }
    for name, provider in PROVIDERS.items():
        values = provider_values[name]
        if set(values) != set(provider.fields) or any(not value for value in values.values()):
            raise OpenBaoError(f"Bootstrap provider values are invalid for {name}")
        for path in provider.paths:
            record = records.setdefault(path, {})
            record.update(dict(values.items()))
    active_directory_values = active_directory or {}
    if active_directory_values:
        active_directory_provider = MANAGED_CREDENTIALS["active-directory"]
        if set(active_directory_values) != set(active_directory_provider.fields) or any(
            not active_directory_values[field].strip() for field in active_directory_provider.fields
        ):
            raise OpenBaoError("Active Directory credentials are invalid or incomplete")
        for path in active_directory_provider.paths:
            record = records.setdefault(path, {})
            record.update(active_directory_values)
    external_changed = 0
    for path, values in records.items():
        external_changed += _reconcile_exact_record(client, path, values)
    reconciled = reconcile_openbao(client, identity, bootstrap_passwords)
    return SeedReport(
        external_changed,
        reconciled.internal_records_changed,
        reconciled.internal_fields_added,
    )


def _reconcile_exact_record(client: OpenBaoClient, path: str, values: dict[str, JsonValue]) -> int:
    current = client.read_secret(path)
    if current is None:
        client.write_secret(path, values, 0)
        return 1
    if current.values != values:
        raise OpenBaoError(f"OpenBao external record differs at {path}")
    return 0


def _secret_operator_policy() -> str:
    """Restrict provider updates to exact KV v2 records and metadata reads."""
    return secret_operator_policy(
        tuple(path for provider in MANAGED_CREDENTIALS.values() for path in provider.paths)
    )
