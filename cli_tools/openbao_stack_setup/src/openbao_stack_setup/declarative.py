"""Execute fixed credential operations from the shared parser's concrete setup models."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from k8s_stack_tooling.addons import ContractError, Setup, valid_value, validate_verifier_slots
from k8s_stack_tooling.addons.schema import CredentialField

from openbao_stack_setup.access import AccessReader, AccessReservation, require_reservation
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError, SecretRecord
from openbao_stack_setup.credentials import BootstrapPassword, InternalResult
from openbao_stack_setup.recovery import (
    RecoveryKit,
    update,
    validate_pending_password_bindings,
    with_password_delivery_complete,
)

type FieldKey = tuple[str, str, str]
type SuppliedValues = Mapping[str, Mapping[str, Mapping[str, str]]]
type PreparedValues = Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class CredentialWrite:
    """One private in-memory CAS write, never a serialized input format."""

    path: str
    version: int
    values: dict[str, JsonValue] = field(repr=False)


@dataclass(frozen=True)
class CredentialPlan:
    """Fully validated writes and a nonsecret change count."""

    writes: tuple[CredentialWrite, ...] = field(repr=False)
    added_fields: int
    versions: tuple[tuple[str, int], ...]


def handover_definitions(
    setups: Mapping[str, Setup], definitions: Mapping[str, str]
) -> tuple[BootstrapPassword, ...]:
    """Project selected human-password descriptors for the existing custody planner."""
    result = []
    for addon, setup in setups.items():
        records = {record.id: record for record in setup.records}
        for item in setup.handover:
            if not definitions.get(addon):
                raise OpenBaoError("Password handover requires verified definition identity")
            record = records[item.value.record]
            generator = record.fields[item.value.field].generate
            if generator is None or generator.encoding != "base64url" or generator.bytes != 32:
                raise OpenBaoError("Human password planner requires 32-byte base64url generation")
            result.append(
                BootstrapPassword(
                    f"{addon}/{item.id}",
                    item.label,
                    record.path,
                    item.value.field,
                    definitions[addon],
                )
            )
    if len({(item.path, item.field) for item in result}) != len(result):
        raise OpenBaoError("Human password destinations are duplicated")
    return tuple(result)


def compose_readers(setups: Mapping[str, Setup]) -> tuple[AccessReader, ...]:
    """Union exact add-on grants; access execution retains fixed Base policy attachments."""
    _concrete_fields(setups)
    contributions = []
    for setup in setups.values():
        paths = {record.id: record.path for record in setup.records}
        contributions.extend(
            AccessReader(
                reader.role,
                reader.namespace,
                reader.service_account,
                reader.audience,
                tuple(paths[record] for record in reader.records),
            )
            for reader in setup.readers
        )
    result: dict[str, AccessReader] = {}
    for reader in contributions:
        old = result.get(reader.role)
        if old and (old.namespace, old.service_account, old.audience) != (
            reader.namespace,
            reader.service_account,
            reader.audience,
        ):
            raise OpenBaoError("Selected reader bindings conflict")
        result[reader.role] = AccessReader(
            reader.role,
            reader.namespace,
            reader.service_account,
            reader.audience,
            tuple(sorted(set(reader.records) | (set(old.records) if old else set()))),
        )
    return tuple(result[key] for key in sorted(result))


def plan_credentials(
    client: OpenBaoClient,
    setups: Mapping[str, Setup],
    *,
    supplied: SuppliedValues | None = None,
    prepared: PreparedValues | None = None,
    handover: RecoveryKit | None = None,
    definitions: Mapping[str, str] | None = None,
    base_handover: tuple[BootstrapPassword, ...] = (),
) -> CredentialPlan:
    """Read and preflight the complete selected set before issuing any credential writes."""
    fields = _concrete_fields(setups)
    supplied, prepared = supplied or {}, prepared or {}
    _validate_inputs(setups, supplied, prepared)
    passwords = _handover_values(setups, definitions or {}, handover, base_handover)
    snapshots = {
        (addon, record.id): client.read_secret(record.path)
        for addon, setup in setups.items()
        for record in setup.records
    }
    slots = _slot_fields(setups)
    resolved: dict[FieldKey, str] = {}
    levels = _field_levels(fields)
    for level in levels:
        for key in level:
            current = snapshots[key[:2]]
            existing = current.values.get(key[2]) if current else None
            # Null is an invalid existing value, not permission to regenerate a key.
            present = current is not None and key[2] in current.values
            value = _resolve_field(
                key,
                fields[key],
                present,
                existing,
                resolved,
                supplied,
                prepared,
                passwords,
                key in slots,
            )
            _validate_field(value, fields[key])
            resolved[key] = value
    for addon, setup in setups.items():
        values = {
            record.id: {name: resolved[(addon, record.id, name)] for name in record.fields}
            for record in setup.records
        }
        try:
            validate_verifier_slots(setup, values)
        except ContractError:
            raise OpenBaoError("Credential does not match an accepted verifier slot") from None
    return _writes(setups, snapshots, resolved, levels)


def apply_credentials(
    client: OpenBaoClient, plan: CredentialPlan, *, reservation: AccessReservation
) -> InternalResult:
    """Apply preflighted dependency-ordered CAS writes; stop without rollback on failure."""
    changed = []
    require_reservation(client, reservation)
    _check_versions(client, dict(plan.versions))
    versions = dict(plan.versions)
    for write in plan.writes:
        require_reservation(client, reservation)
        client.write_secret(write.path, write.values, write.version)
        versions[write.path] = write.version + 1
        if write.path not in changed:
            changed.append(write.path)
    _check_versions(client, versions)
    return InternalResult(tuple(changed), plan.added_fields)


def _check_versions(client: OpenBaoClient, versions: Mapping[str, int]) -> None:
    for path, expected in versions.items():
        current = client.read_secret(path)
        if (current.version if current else 0) != expected:
            raise OpenBaoError("Credential snapshot changed; reload before retrying")


def complete_handover(client: OpenBaoClient, kit: RecoveryKit, recovery_file: Path) -> RecoveryKit:
    """Clear pending custody values only after verifying every acknowledged destination."""
    if (
        not kit.bootstrap_passwords_acknowledged
        or not kit.pending_bootstrap_passwords
        or kit.pending_password_bindings is None
    ):
        raise OpenBaoError("Bound password handover is not acknowledged")
    validate_pending_password_bindings(kit, kit.pending_password_bindings)
    for key, value in kit.pending_bootstrap_passwords.items():
        binding = kit.pending_password_bindings[key]
        current = client.read_secret(binding["path"])
        if current is None or current.values.get(binding["field"]) != value:
            raise OpenBaoError("Acknowledged password destination is not persisted")
    completed = with_password_delivery_complete(kit)
    update(recovery_file, completed)
    return completed


def _concrete_fields(setups: Mapping[str, Setup]) -> dict[FieldKey, CredentialField]:
    fields = {}
    paths: set[str] = set()
    for addon, setup in setups.items():
        rows = [
            *setup.records,
            *setup.readers,
            *setup.input_groups,
            *setup.handover,
            *setup.invariants,
        ]
        if any(row.when is not None for row in rows):
            raise OpenBaoError("Setup must be expanded by the shared parser before execution")
        for record in setup.records:
            if record.path in paths or not record.path.startswith(f"addons/{addon}/"):
                raise OpenBaoError("Selected credential record ownership conflicts")
            paths.add(record.path)
            for name, definition in record.fields.items():
                if definition.when is not None:
                    raise OpenBaoError("Credential fields require shared-parser expansion")
                fields[(addon, record.id, name)] = definition
    return fields


def _validate_inputs(
    setups: Mapping[str, Setup], supplied: SuppliedValues, prepared: PreparedValues
) -> None:
    if not supplied.keys() <= setups.keys() or not prepared.keys() <= setups.keys():
        raise OpenBaoError("Inputs name an unselected add-on")
    for addon, setup in setups.items():
        fields = [definition for record in setup.records for definition in record.fields.values()]
        used_groups = {definition.supplied.group for definition in fields if definition.supplied}
        groups = {group.id: group for group in setup.input_groups}
        if not supplied.get(addon, {}).keys() <= used_groups:
            raise OpenBaoError("Supplied inputs name an inactive input group")
        for name, values in supplied.get(addon, {}).items():
            group = groups[name]
            if set(values) != set(group.fields) or any(
                not valid_value(values[key], validation) for key, validation in group.fields.items()
            ):
                raise OpenBaoError("Supplied input group is incomplete or invalid")
        outputs = {
            definition.prepared_output for definition in fields if definition.prepared_output
        }
        if not prepared.get(addon, {}).keys() <= outputs:
            raise OpenBaoError("Prepared outputs name an inactive output")
        for definition in fields:
            if definition.prepared_output in prepared.get(addon, {}):
                value = prepared[addon][cast(str, definition.prepared_output)]
                _validate_field(value, definition)


def _handover_values(
    setups: Mapping[str, Setup],
    definitions: Mapping[str, str],
    kit: RecoveryKit | None,
    base: tuple[BootstrapPassword, ...],
) -> dict[FieldKey, str | None]:
    descriptors = handover_definitions(setups, definitions)
    pending = kit.pending_bootstrap_passwords if kit is not None else None
    if pending:
        if kit is None or not kit.bootstrap_passwords_acknowledged:
            raise OpenBaoError("Pending password handover requires acknowledgement")
        bindings = {
            item.key: {
                "label": item.label,
                "path": item.path,
                "field": item.field,
                "definition": item.definition,
            }
            for item in (*base, *descriptors)
        }
        validate_pending_password_bindings(kit, bindings)
    result = {}
    for addon, setup in setups.items():
        for item in setup.handover:
            result[(addon, item.value.record, item.value.field)] = (pending or {}).get(
                f"{addon}/{item.id}"
            )
    return result


def _slot_fields(setups: Mapping[str, Setup]) -> set[FieldKey]:
    result = set()
    for addon, setup in setups.items():
        fields = {record.id: record.fields for record in setup.records}
        for item in setup.invariants:
            primary = fields[item.primary.record][item.primary.field]
            secondary = fields[item.secondary.record][item.secondary.field]
            if primary.sha256 != item.source or secondary.literal != "":
                raise OpenBaoError(
                    "Verifier slots require a derived primary and explicit empty secondary"
                )
            result.update((addon, ref.record, ref.field) for ref in (item.primary, item.secondary))
    return result


def _field_levels(fields: Mapping[FieldKey, CredentialField]) -> list[list[FieldKey]]:
    pending = dict(fields)
    levels = []
    while pending:
        ready = []
        for key, definition in pending.items():
            ref = definition.ref or definition.sha256
            if ref is not None and (key[0], ref.record, ref.field) not in fields:
                raise OpenBaoError("Credential reference is not active")
            if ref is None or (key[0], ref.record, ref.field) not in pending:
                ready.append(key)
        if not ready:
            raise OpenBaoError("Credential references are cyclic")
        levels.append(sorted(ready))
        for key in ready:
            del pending[key]
    return levels


def _resolve_field(
    key: FieldKey,
    definition: CredentialField,
    present: bool,
    existing: JsonValue,
    resolved: Mapping[FieldKey, str],
    supplied: SuppliedValues,
    prepared: PreparedValues,
    passwords: Mapping[FieldKey, str | None],
    slot: bool,
) -> str:
    if key in passwords:
        pending = passwords[key]
        if pending is not None:
            return _consistent(present, existing, pending)
        if not present:
            raise OpenBaoError("Missing human password requires acknowledged handover")
    if definition.supplied is not None:
        group = supplied.get(key[0], {}).get(definition.supplied.group)
        if group is not None:
            return cast(str, group[definition.supplied.key])
    if present:
        _validate_field(existing, definition)
        if slot or definition.generate or definition.prepared_output or definition.supplied:
            return cast(str, existing)
    value = _field_value(key, definition, resolved, prepared)
    return _consistent(present, existing, value)


def _field_value(
    key: FieldKey,
    definition: CredentialField,
    resolved: Mapping[FieldKey, str],
    prepared: PreparedValues,
) -> str:
    if definition.generate is not None:
        random = secrets.token_bytes(definition.generate.bytes)
        if definition.generate.encoding == "hex":
            return random.hex()
        if definition.generate.encoding == "base64":
            return base64.b64encode(random).decode("ascii")
        return base64.urlsafe_b64encode(random).decode("ascii").rstrip("=")
    if definition.literal is not None:
        return definition.literal
    reference = definition.ref or definition.sha256
    if reference is not None:
        value = resolved[(key[0], reference.record, reference.field)]
        return _sha256(value) if definition.sha256 else value
    if definition.prepared_output is not None:
        value = prepared.get(key[0], {}).get(definition.prepared_output)
        if value is not None:
            return cast(str, value)
        raise OpenBaoError("Missing credential requires a prevalidated prepared output")
    raise OpenBaoError("Missing credential requires a complete supplied input group")


def _sha256(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise OpenBaoError("Credential is not valid UTF-8") from None


def _consistent(present: bool, existing: JsonValue, expected: str) -> str:
    if present and existing != expected:
        raise OpenBaoError("Existing credential conflicts with its declared source")
    return expected


def _validate_field(value: JsonValue, definition: CredentialField) -> None:
    if not isinstance(value, str) or not valid_value(value, definition.validation):
        raise OpenBaoError("Credential value does not satisfy its declared constraints")
    generator = definition.generate
    if generator is None:
        return
    try:
        if generator.encoding == "hex":
            decoded = bytes.fromhex(value)
            canonical = decoded.hex()
        elif generator.encoding == "base64":
            decoded = base64.b64decode(value, validate=True)
            canonical = base64.b64encode(decoded).decode("ascii")
        else:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
            canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    except (ValueError, UnicodeError):
        raise OpenBaoError(
            "Existing generated credential has an invalid encoding or size"
        ) from None
    if len(decoded) != generator.bytes or canonical != value:
        raise OpenBaoError("Existing generated credential has an invalid encoding or size")


def _writes(
    setups: Mapping[str, Setup],
    snapshots: Mapping[tuple[str, str], SecretRecord | None],
    resolved: Mapping[FieldKey, str],
    levels: list[list[FieldKey]],
) -> CredentialPlan:
    paths = {
        (addon, record.id): record.path
        for addon, setup in setups.items()
        for record in setup.records
    }
    values = {key: dict(record.values) if record else {} for key, record in snapshots.items()}
    versions = {key: record.version if record else 0 for key, record in snapshots.items()}
    writes = []
    added = 0
    for level in levels:
        changed: set[tuple[str, str]] = set()
        for key in level:
            current = values[key[:2]]
            added += int(key[2] not in current)
            if key[2] not in current or current[key[2]] != resolved[key]:
                current[key[2]] = resolved[key]
                changed.add(key[:2])
        for key in sorted(changed):
            writes.append(CredentialWrite(paths[key], versions[key], dict(values[key])))
            versions[key] += 1
    observed = tuple(
        (paths[key], record.version if record else 0) for key, record in snapshots.items()
    )
    return CredentialPlan(tuple(writes), added, observed)
