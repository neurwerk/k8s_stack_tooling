"""Reserve reconciliation and retain exact add-on reader ownership in its existing record."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
from importlib.metadata import version as distribution_version
from typing import cast

from openbao_stack_setup.catalog import (
    DOCLING_NAMESPACES,
    RECONCILIATION_STATE_PATH,
    REJECTION_DEBUG_ROLE,
    ROLE_NAMESPACES,
)
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError, SecretRecord


@dataclass(frozen=True)
class AccessReader:
    """One approved reader with the complete union of its exact record grants."""

    role: str
    namespace: str
    service_account: str
    audience: str
    records: tuple[str, ...]


@dataclass(frozen=True)
class OwnedReader:
    """Bind a reader activation to a policy identity that is never recycled."""

    reader: AccessReader
    policy: str


@dataclass(frozen=True)
class AccessReservation:
    """Handle for one CAS-reserved operation; resumption invalidates previous handles."""

    operation_id: str
    selection: str
    version: int
    previous: tuple[OwnedReader, ...]
    intended: tuple[OwnedReader, ...]


def reserve_access(
    client: OpenBaoClient,
    record: SecretRecord,
    operation_id: str,
    selection: str,
    readers: tuple[AccessReader, ...],
    *,
    resume: bool = False,
    previous_process_stopped: bool = False,
) -> AccessReservation:
    """Reserve before grants mutate; the caller first validates the cluster-bound record."""
    if not _safe_text(operation_id) or not _safe_text(selection):
        raise OpenBaoError("Reconciliation operation identity is invalid")
    readers = _validated_readers(readers)
    ownership, pending = load_access(record.values.get("access"))
    if pending is not None:
        reservation = _resume(
            pending, readers, operation_id, selection, resume, previous_process_stopped
        )
    else:
        if resume:
            raise OpenBaoError("No reconciliation operation is pending")
        existing = {item.reader.role: item for item in ownership}
        intended = []
        for reader in readers:
            previous = existing.get(reader.role)
            if previous is not None and _binding(previous.reader) != _binding(reader):
                raise OpenBaoError(
                    "Active reader binding changed; remove access before reactivation"
                )
            policy = previous.policy if previous else f"stack-setup-addon-{secrets.token_hex(16)}"
            intended.append(OwnedReader(reader, policy))
        reservation = AccessReservation(operation_id, selection, 0, ownership, tuple(intended))
    validate_access_roles(client, reservation, pending_creation=pending is not None)
    values = dict(record.values)
    values["access"] = _access_values(ownership, reservation)
    client.ensure_kv_v2_mount()
    client.write_secret(RECONCILIATION_STATE_PATH, values, record.version)
    return AccessReservation(
        operation_id, selection, record.version + 1, reservation.previous, reservation.intended
    )


def require_reservation(client: OpenBaoClient, reservation: AccessReservation) -> SecretRecord:
    """Reject stale or replaced handles before any policy, role, or completion mutation."""
    record = client.read_secret(RECONCILIATION_STATE_PATH)
    if record is None or record.version != reservation.version:
        raise OpenBaoError("Reconciliation reservation changed; refusing mutation")
    _, pending = load_access(record.values.get("access"))
    if pending != _pending_values(reservation):
        raise OpenBaoError("Reconciliation reservation does not match pending operation")
    return record


def apply_access(client: OpenBaoClient, reservation: AccessReservation) -> None:
    """Shrink all obsolete grants before changing roles; preserve records and active identities."""
    previous = {item.reader.role: item for item in reservation.previous}
    intended = {item.reader.role: item for item in reservation.intended}
    require_reservation(client, reservation)
    validate_access_roles(client, reservation)
    for role, old in previous.items():
        current = intended.get(role)
        retained = tuple(
            path for path in old.reader.records if current and path in current.reader.records
        )
        if retained != old.reader.records:
            require_reservation(client, reservation)
            if current is None:
                client.delete_policy(old.policy)
            else:
                client.write_policy(old.policy, _policy(retained))
    for role in previous.keys() - intended.keys():
        require_reservation(client, reservation)
        old = previous[role]
        _validate_live_role(client, old, owned=True, pending_creation=True)
        base = _base_policies(old.reader)
        if base:
            _write_role(client, old.reader, base)
        else:
            client.delete_kubernetes_role(role)
    for role, current in intended.items():
        old = previous.get(role)
        if old is None or set(current.reader.records) - set(old.reader.records):
            require_reservation(client, reservation)
            client.write_policy(current.policy, _policy(current.reader.records))
        require_reservation(client, reservation)
        live = _validate_live_role(client, current, owned=old is not None, pending_creation=True)
        policies = (*_base_policies(current.reader), current.policy)
        if live is None or live.get("token_policies") != list(policies):
            _write_role(client, current.reader, policies)


def validate_access_roles(
    client: OpenBaoClient, reservation: AccessReservation, *, pending_creation: bool = True
) -> None:
    """Check all live roles before writes; only persisted intent permits resuming creation."""
    previous = {item.reader.role: item for item in reservation.previous}
    selected = previous | {item.reader.role: item for item in reservation.intended}
    for role, item in selected.items():
        _validate_live_role(client, item, owned=role in previous, pending_creation=pending_creation)


def _base_policies(reader: AccessReader) -> tuple[str, ...]:
    if reader.role in ROLE_NAMESPACES:
        if _binding(reader) != (
            reader.role,
            reader.role,
            f"{reader.role}-external-secrets",
            "openbao",
        ):
            raise OpenBaoError("Shared Base reader requires its exact fixed binding")
        return (reader.role,)
    if reader.role in {
        *DOCLING_NAMESPACES,
        REJECTION_DEBUG_ROLE,
        "secret-operator",
        "tailscale",
        "contextforge",
        "glitchtip",
        "studio-mcp",
        "mcp-shared-delivery",
    }:
        raise OpenBaoError("Selected reader role collides with Base reconciliation")
    return ()


def _validate_live_role(
    client: OpenBaoClient, item: OwnedReader, *, owned: bool, pending_creation: bool
) -> dict[str, JsonValue] | None:
    reader = item.reader
    base = _base_policies(reader)
    live = client.read_kubernetes_role(reader.role)
    if live is None:
        return None
    if not base and not owned and not pending_creation:
        raise OpenBaoError("Existing non-Base reader role is not owned by setup")
    allowed = [list(base)] if base else []
    if owned or pending_creation:
        allowed.append([*base, item.policy])
    if (
        live.get("bound_service_account_names") != [reader.service_account]
        or live.get("bound_service_account_namespaces") != [reader.namespace]
        or live.get("audience") != reader.audience
        or live.get("bound_service_account_namespace_selector") not in (None, "")
        or (not base and live.get("token_no_default_policy") is not True)
        or live.get("token_policies") not in allowed
    ):
        raise OpenBaoError("Live reader binding or policy ownership conflicts with setup")
    return live


def _write_role(client: OpenBaoClient, reader: AccessReader, policies: tuple[str, ...]) -> None:
    client._write(
        f"auth/kubernetes/role/{reader.role}",
        {
            "bound_service_account_names": [reader.service_account],
            "bound_service_account_namespaces": [reader.namespace],
            "audience": reader.audience,
            "token_policies": list(policies),
            "token_no_default_policy": not bool(_base_policies(reader)),
            "token_ttl": "10m",
            "token_max_ttl": "30m",
        },
    )


def complete_access(
    client: OpenBaoClient, reservation: AccessReservation, *, applied_version: int | None = None
) -> None:
    """Commit ownership after all caller-owned reconciliation work has succeeded."""
    record = require_reservation(client, reservation)
    values = dict(record.values)
    values["access"] = _access_values(reservation.intended, None)
    if applied_version is not None:
        if type(applied_version) is not int or applied_version < 0:
            raise OpenBaoError("Completed reconciliation version is invalid")
        values["appliedVersion"] = applied_version
        values["packageVersion"] = distribution_version("openbao-stack-setup")
    client.write_secret(RECONCILIATION_STATE_PATH, values, record.version)


def load_access(value: JsonValue) -> tuple[tuple[OwnedReader, ...], dict[str, JsonValue] | None]:
    """Validate only private ownership state, not product manifests or source selection."""
    if value is None:
        return (), None
    if not isinstance(value, dict) or set(value) != {"owned", "pending"}:
        raise OpenBaoError("Reconciliation access state is invalid")
    owned = _load_owned(value["owned"])
    pending = value["pending"]
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {
            "operationId",
            "selection",
            "previous",
            "intended",
        }:
            raise OpenBaoError("Pending reconciliation state is invalid")
        if not _safe_text(pending["operationId"]) or not _safe_text(pending["selection"]):
            raise OpenBaoError("Pending reconciliation identity is invalid")
        if _load_owned(pending["previous"]) != owned:
            raise OpenBaoError("Pending reconciliation ownership is inconsistent")
        intended = _load_owned(pending["intended"])
        old = {item.reader.role: item for item in owned}
        for item in intended:
            previous = old.get(item.reader.role)
            if previous is not None and (
                previous.policy != item.policy or _binding(previous.reader) != _binding(item.reader)
            ):
                raise OpenBaoError("Pending reader activation identity is inconsistent")
    return owned, pending


def _resume(
    pending: dict[str, JsonValue],
    readers: tuple[AccessReader, ...],
    operation_id: str,
    selection: str,
    resume: bool,
    stopped: bool,
) -> AccessReservation:
    if not resume or not stopped:
        raise OpenBaoError("Reconciliation is pending; explicit stopped-process resume is required")
    intended = _load_owned(pending["intended"])
    if (
        pending["operationId"] != operation_id
        or pending["selection"] != selection
        or tuple(item.reader for item in intended) != readers
    ):
        raise OpenBaoError("Pending reconciliation operation or selection conflicts")
    return AccessReservation(operation_id, selection, 0, _load_owned(pending["previous"]), intended)


def _validated_readers(readers: tuple[AccessReader, ...]) -> tuple[AccessReader, ...]:
    if len({item.role for item in readers}) != len(readers):
        raise OpenBaoError("Reader grants must be composed before reservation")
    for reader in readers:
        _base_policies(reader)
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", text)
            for text in (reader.role, reader.namespace, reader.service_account)
        ) or not _safe_text(reader.audience):
            raise OpenBaoError("Reader binding is invalid")
        if not reader.records or any(
            not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", path) for path in reader.records
        ):
            raise OpenBaoError("Reader grants must name exact records")
        if tuple(sorted(set(reader.records))) != reader.records:
            raise OpenBaoError("Reader records must be unique and sorted")
    return tuple(sorted(readers, key=lambda item: item.role))


def _binding(reader: AccessReader) -> tuple[str, ...]:
    return reader.role, reader.namespace, reader.service_account, reader.audience


def _load_owned(value: JsonValue) -> tuple[OwnedReader, ...]:
    if not isinstance(value, list):
        raise OpenBaoError("Reader ownership is invalid")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"reader", "policy"}:
            raise OpenBaoError("Reader ownership entry is invalid")
        raw, policy = item["reader"], item["policy"]
        if not isinstance(raw, dict) or set(raw) != {
            "role",
            "namespace",
            "service_account",
            "audience",
            "records",
        }:
            raise OpenBaoError("Stored reader is invalid")
        if any(
            not isinstance(raw[key], str)
            for key in ("role", "namespace", "service_account", "audience")
        ):
            raise OpenBaoError("Stored reader binding is invalid")
        records = raw["records"]
        if not isinstance(records, list) or any(not isinstance(path, str) for path in records):
            raise OpenBaoError("Stored reader records are invalid")
        if not isinstance(policy, str) or not re.fullmatch(
            r"stack-setup-addon-[0-9a-f]{32}", policy
        ):
            raise OpenBaoError("Owned policy identity is invalid")
        reader = AccessReader(
            cast(str, raw["role"]),
            cast(str, raw["namespace"]),
            cast(str, raw["service_account"]),
            cast(str, raw["audience"]),
            tuple(cast(list[str], records)),
        )
        result.append(OwnedReader(reader, policy))
    if len({item.policy for item in result}) != len(result):
        raise OpenBaoError("Owned policy identities are duplicated")
    _validated_readers(tuple(item.reader for item in result))
    return tuple(result)


def _safe_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and all(32 <= ord(char) < 127 for char in value)
    )


def _owned_values(owned: tuple[OwnedReader, ...]) -> list[JsonValue]:
    return [
        {
            "policy": item.policy,
            "reader": {**asdict(item.reader), "records": list(item.reader.records)},
        }
        for item in owned
    ]


def _pending_values(reservation: AccessReservation) -> dict[str, JsonValue]:
    return {
        "operationId": reservation.operation_id,
        "selection": reservation.selection,
        "previous": _owned_values(reservation.previous),
        "intended": _owned_values(reservation.intended),
    }


def _access_values(
    owned: tuple[OwnedReader, ...], reservation: AccessReservation | None
) -> dict[str, JsonValue]:
    return {
        "owned": _owned_values(owned),
        "pending": _pending_values(reservation) if reservation else None,
    }


def _policy(records: tuple[str, ...]) -> str:
    # ESO checks and closes its own token; these calls grant no access to other tokens.
    return (
        'path "auth/token/lookup-self" { capabilities = ["read"] }\n'
        'path "auth/token/revoke-self" { capabilities = ["update"] }\n'
        + "\n".join(
            f'path "secret/{endpoint}/{path}" {{ capabilities = ["read"] }}'
            for path in records
            for endpoint in ("data", "metadata")
        )
        + "\n"
    )
