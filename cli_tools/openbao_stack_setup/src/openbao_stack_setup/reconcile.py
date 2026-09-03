"""Apply versioned, catalog-only OpenBao reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version as distribution_version

from openbao_stack_setup.catalog import (
    ACTIVE_DIRECTORY_FIELDS,
    RECONCILIATION_STATE_PATH,
    ROLE_NAMESPACES,
    SMTP_REPLICA,
    namespace_policy,
    secret_operator_policy,
)
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError, SecretRecord
from openbao_stack_setup.credentials import (
    BOOTSTRAP_PASSWORDS,
    migrate_schema_2_internal_credentials,
    reconcile_internal_credentials,
)
from openbao_stack_setup.providers import MANAGED_CREDENTIALS

STATE_SCHEMA_VERSION = 1
CURRENT_RECONCILIATION_VERSION = 4


@dataclass(frozen=True)
class ReconciliationIdentity:
    """Bind persisted reconciliation state to one selected cluster."""

    client: str
    cluster_id: str
    namespace_uid: str


@dataclass(frozen=True)
class ReconciliationReport:
    """Summarize a reconciliation without exposing secret values."""

    previous_version: int
    applied_version: int
    replicated_records: int
    internal_records_changed: int
    internal_fields_added: int


@dataclass(frozen=True)
class _State:
    identity: ReconciliationIdentity
    applied_version: int
    record_version: int


def reconcile_openbao(
    client: OpenBaoClient,
    identity: ReconciliationIdentity,
    bootstrap_passwords: dict[str, str] | None = None,
) -> ReconciliationReport:
    """Converge the reviewed catalog and persist its cluster-bound schema version."""
    passwords = bootstrap_passwords or {}
    client.ensure_kv_v2_mount()
    state = _load_state(client.read_secret(RECONCILIATION_STATE_PATH), identity)
    _require_bootstrap_passwords(client, passwords)
    if state.applied_version > CURRENT_RECONCILIATION_VERSION:
        raise OpenBaoError("OpenBao reconciliation state is newer than this stack-setup version")

    client.ensure_kubernetes_auth()
    client.configure_kubernetes_auth()
    for namespace in ROLE_NAMESPACES:
        client.write_policy(namespace, namespace_policy(namespace))
        client.write_kubernetes_role(namespace)
    client.write_policy(
        "secret-operator",
        secret_operator_policy(
            tuple(path for provider in MANAGED_CREDENTIALS.values() for path in provider.paths)
        ),
    )
    client._write(
        "auth/kubernetes/role/secret-operator",
        {
            "bound_service_account_names": ["secret-operator"],
            "bound_service_account_namespaces": ["infra-openbao"],
            "audience": "openbao",
            "token_policies": ["secret-operator"],
            "token_ttl": "10m",
            "token_max_ttl": "10m",
        },
    )
    replicated = _reconcile_replica(client)
    if state.applied_version == 2:
        migrate_schema_2_internal_credentials(client)
    internal = reconcile_internal_credentials(client, passwords)

    if state.applied_version < CURRENT_RECONCILIATION_VERSION:
        client.write_secret(
            RECONCILIATION_STATE_PATH,
            _state_values(identity),
            state.record_version,
        )
    return ReconciliationReport(
        state.applied_version,
        CURRENT_RECONCILIATION_VERSION,
        replicated,
        len(internal.changed_paths),
        internal.added_fields,
    )


def _load_state(record: SecretRecord | None, expected_identity: ReconciliationIdentity) -> _State:
    if record is None:
        return _State(expected_identity, 0, 0)
    values = record.values
    expected_keys = {
        "schemaVersion",
        "appliedVersion",
        "client",
        "clusterId",
        "namespaceUid",
        "packageVersion",
    }
    if set(values) != expected_keys:
        raise OpenBaoError("OpenBao reconciliation state has an invalid schema")
    schema_version = values["schemaVersion"]
    applied_version = values["appliedVersion"]
    package_version = values["packageVersion"]
    if (
        type(schema_version) is not int
        or schema_version != STATE_SCHEMA_VERSION
        or type(applied_version) is not int
        or applied_version < 0
        or not isinstance(package_version, str)
        or not package_version
    ):
        raise OpenBaoError("OpenBao reconciliation state has invalid values")
    identity = ReconciliationIdentity(
        _required_state_text(values, "client"),
        _required_state_text(values, "clusterId"),
        _required_state_text(values, "namespaceUid"),
    )
    if identity != expected_identity:
        raise OpenBaoError("OpenBao reconciliation state does not belong to the selected cluster")
    return _State(identity, applied_version, record.version)


def _state_values(identity: ReconciliationIdentity) -> dict[str, JsonValue]:
    return {
        "schemaVersion": STATE_SCHEMA_VERSION,
        "appliedVersion": CURRENT_RECONCILIATION_VERSION,
        "client": identity.client,
        "clusterId": identity.cluster_id,
        "namespaceUid": identity.namespace_uid,
        "packageVersion": distribution_version("openbao-stack-setup"),
    }


def _required_state_text(values: dict[str, JsonValue], field: str) -> str:
    value = values[field]
    if not isinstance(value, str) or not value:
        raise OpenBaoError("OpenBao reconciliation state has invalid values")
    return value


def _require_bootstrap_passwords(
    client: OpenBaoClient, bootstrap_passwords: dict[str, str]
) -> None:
    known = {password.key for password in BOOTSTRAP_PASSWORDS}
    if not set(bootstrap_passwords).issubset(known) or any(
        not value for value in bootstrap_passwords.values()
    ):
        raise OpenBaoError("Bootstrap passwords are invalid")
    records: dict[str, SecretRecord | None] = {}
    for password in BOOTSTRAP_PASSWORDS:
        if password.key in bootstrap_passwords:
            continue
        record = records.setdefault(password.path, client.read_secret(password.path))
        if record is None:
            raise OpenBaoError("Existing OpenBao installation is missing a bootstrap password")
        value = record.values.get(password.field)
        if not isinstance(value, str) or not value:
            raise OpenBaoError("Existing OpenBao installation is missing a bootstrap password")


def _reconcile_replica(client: OpenBaoClient) -> int:
    source = client.read_secret(SMTP_REPLICA.source_path)
    if source is None:
        source = client.read_secret(SMTP_REPLICA.legacy_source_path)
        if source is None:
            raise OpenBaoError("Approved SMTP migration source record is missing")
        values = _smtp_values(source, allow_active_directory=True)
        client.write_secret(SMTP_REPLICA.source_path, values, 0)
        changed = 1
    else:
        values = _smtp_values(source)
        changed = 0

    for path in SMTP_REPLICA.destination_paths:
        destination = client.read_secret(path)
        if destination is None:
            client.write_secret(path, values, 0)
            changed += 1
            continue
        destination_values = _smtp_values(
            destination,
            allow_active_directory=path == SMTP_REPLICA.legacy_source_path,
        )
        if destination_values != values:
            raise OpenBaoError("Approved SMTP destination conflicts with its canonical record")
    return changed


def _smtp_values(
    source: SecretRecord, *, allow_active_directory: bool = False
) -> dict[str, JsonValue]:
    expected_fields = set(SMTP_REPLICA.fields)
    extra_fields = set(source.values) - expected_fields
    if extra_fields:
        if not allow_active_directory or extra_fields != set(ACTIVE_DIRECTORY_FIELDS):
            raise OpenBaoError("Approved SMTP record has an invalid field set")
        if any(
            not isinstance(source.values[field], str) or not source.values[field]
            for field in ACTIVE_DIRECTORY_FIELDS
        ):
            raise OpenBaoError("Approved Active Directory sibling record is incomplete")
    values: dict[str, JsonValue] = {}
    for field in SMTP_REPLICA.fields:
        value = source.values.get(field)
        if not isinstance(value, str):
            raise OpenBaoError("Approved SMTP record is incomplete")
        values[field] = value
    if bool(values[SMTP_REPLICA.fields[0]]) != bool(values[SMTP_REPLICA.fields[1]]):
        raise OpenBaoError("Approved SMTP record is incomplete")
    return values
