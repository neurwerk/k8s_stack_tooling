"""Reconcile exact add-on reader grants while retaining their policy ownership."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
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
class AccessPlan:
    """Previous and selected reader grants for ordinary idempotent reconciliation."""

    previous: tuple[OwnedReader, ...]
    intended: tuple[OwnedReader, ...]


def plan_access(
    client: OpenBaoClient,
    record: SecretRecord,
    readers: tuple[AccessReader, ...],
) -> AccessPlan:
    """Remember policy identities before writes so an interrupted run can simply retry."""
    readers = _validated_readers(readers)
    ownership = load_access(record.values.get("access"))
    existing = {item.reader.role: item for item in ownership}
    intended = []
    for reader in readers:
        previous = existing.get(reader.role)
        if previous is not None and _binding(previous.reader) != _binding(reader):
            raise OpenBaoError("Active reader binding changed; remove access before reactivation")
        policy = previous.policy if previous else f"stack-setup-addon-{secrets.token_hex(16)}"
        intended.append(OwnedReader(reader, policy))
    plan = AccessPlan(ownership, tuple(intended))
    validate_access_roles(client, plan)
    # Keep the union until obsolete grants have been removed. This is ownership,
    # not an operation lock: retries may use the current desired selection.
    remembered = dict(existing)
    for item in intended:
        previous = existing.get(item.reader.role)
        paths = set(item.reader.records) | (set(previous.reader.records) if previous else set())
        remembered[item.reader.role] = OwnedReader(
            AccessReader(*_binding(item.reader), tuple(sorted(paths))), item.policy
        )
    values = dict(record.values)
    values["access"] = {"owned": _owned_values(tuple(remembered.values()))}
    client.ensure_kv_v2_mount()
    client.write_secret(RECONCILIATION_STATE_PATH, values, record.version)
    return plan


def apply_access(client: OpenBaoClient, plan: AccessPlan) -> None:
    """Shrink all obsolete grants before changing roles; preserve records and active identities."""
    previous = {item.reader.role: item for item in plan.previous}
    intended = {item.reader.role: item for item in plan.intended}
    validate_access_roles(client, plan, creating=True)
    for role, old in previous.items():
        current = intended.get(role)
        retained = tuple(
            path for path in old.reader.records if current and path in current.reader.records
        )
        if retained != old.reader.records:
            if current is None:
                client.delete_policy(old.policy)
            else:
                client.write_policy(old.policy, _policy(retained))
    for role in previous.keys() - intended.keys():
        old = previous[role]
        _validate_live_role(client, old, owned=True, pending_creation=True)
        base = _base_policies(old.reader)
        if base:
            _write_role(client, old.reader, base)
        else:
            client.delete_kubernetes_role(role)
    for role, current in intended.items():
        old = previous.get(role)
        # Ownership is recorded before grants: a previous attempt may have failed
        # before creating this policy, so always apply its complete desired value.
        client.write_policy(current.policy, _policy(current.reader.records))
        live = _validate_live_role(client, current, owned=old is not None, pending_creation=True)
        policies = (*_base_policies(current.reader), current.policy)
        if live is None or live.get("token_policies") != list(policies):
            _write_role(client, current.reader, policies)


def validate_access_roles(
    client: OpenBaoClient, plan: AccessPlan, *, creating: bool = False
) -> None:
    """Reject foreign bindings and policy attachments before changing reader grants."""
    previous = {item.reader.role: item for item in plan.previous}
    selected = previous | {item.reader.role: item for item in plan.intended}
    for role, item in selected.items():
        _validate_live_role(client, item, owned=role in previous, pending_creation=creating)


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


def complete_access(client: OpenBaoClient, plan: AccessPlan) -> None:
    """Forget obsolete ownership only after all grant changes have succeeded."""
    record = client.read_secret(RECONCILIATION_STATE_PATH)
    if record is None:
        raise OpenBaoError("Reconciliation ownership state is missing")
    values = dict(record.values)
    values["access"] = {"owned": _owned_values(plan.intended)}
    client.write_secret(RECONCILIATION_STATE_PATH, values, record.version)


def load_access(value: JsonValue) -> tuple[OwnedReader, ...]:
    """Validate only private ownership state, not product manifests or source selection."""
    if value is None:
        return ()
    if not isinstance(value, dict) or set(value) != {"owned"}:
        raise OpenBaoError("Reconciliation access state is invalid")
    return _load_owned(value["owned"])


def _validated_readers(readers: tuple[AccessReader, ...]) -> tuple[AccessReader, ...]:
    if len({item.role for item in readers}) != len(readers):
        raise OpenBaoError("Reader grants must be composed before reconciliation")
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


def _binding(reader: AccessReader) -> tuple[str, str, str, str]:
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
