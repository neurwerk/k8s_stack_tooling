"""Provision opt-in Langfuse debug keys without granting runtime namespace access."""

from __future__ import annotations

import re
import secrets

from openbao_stack_setup.catalog import (
    REJECTION_DEBUG_PATH,
    REJECTION_DEBUG_ROLE,
    namespace_policy,
)
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import InternalResult

_SOURCE = "monitor-langfuse/internal"
_FIELDS = {"debugProjectPublicKey": "publicKey", "debugProjectSecretKey": "secretKey"}


def reconcile_rejection_debug(client: OpenBaoClient, *, enabled: bool = False) -> InternalResult:
    """Preserve a distinct key pair, copy it with CAS, and scope a dedicated ESO role."""
    if not enabled:
        return InternalResult((), 0)
    source = client.read_secret(_SOURCE)
    values = dict(source.values) if source else {}
    pair = _project_keys(values)
    destination = client.read_secret(REJECTION_DEBUG_PATH)
    copied = dict(destination.values) if destination else {}
    present = [field in copied for field in _FIELDS.values()]
    if any(present) and not all(present):
        raise OpenBaoError(
            "Rejected-request debug credential copy is incomplete; refusing to repair"
        )
    for field, value in pair.items():
        target = _FIELDS[field]
        if target in copied and copied[target] != value:
            raise OpenBaoError(
                "Rejected-request debug credential copy conflicts; refusing to replace"
            )

    # Close the pre-existing Docling wildcard before writing any debug credentials.
    namespace = "monitor-agentgateway-extproc"
    client.write_policy(namespace, namespace_policy(namespace))
    # Read and validate both records before writes. Source-first CAS makes partial retries safe.
    changed: list[str] = []
    added = sum(field not in values for field in pair)
    if added:
        client.write_secret(_SOURCE, {**values, **pair}, source.version if source else 0)
        changed.append(_SOURCE)
    missing = {target: pair[field] for field, target in _FIELDS.items() if target not in copied}
    if missing:
        client.write_secret(
            REJECTION_DEBUG_PATH, {**copied, **missing}, destination.version if destination else 0
        )
        changed.append(REJECTION_DEBUG_PATH)

    client.write_policy(
        REJECTION_DEBUG_ROLE,
        f'path "secret/data/{REJECTION_DEBUG_PATH}" {{ capabilities = ["read"] }}\n'
        'path "auth/token/lookup-self" { capabilities = ["read"] }\n'
        'path "auth/token/revoke-self" { capabilities = ["update"] }\n',
    )
    client._write(
        f"auth/kubernetes/role/{REJECTION_DEBUG_ROLE}",
        {
            "bound_service_account_names": [REJECTION_DEBUG_ROLE],
            "bound_service_account_namespaces": [namespace],
            "audience": "openbao",
            "token_policies": [REJECTION_DEBUG_ROLE],
            "token_no_default_policy": True,
            "token_ttl": "5m",
            "token_max_ttl": "5m",
        },
    )
    return InternalResult(tuple(changed), added + len(missing))


def _project_keys(values: dict[str, JsonValue]) -> dict[str, str]:
    present = [field in values for field in _FIELDS]
    if any(present) and not all(present):
        raise OpenBaoError("Rejected-request debug key pair is incomplete; refusing to replace")
    if not any(present):
        return {
            "debugProjectPublicKey": f"pk-lf-{secrets.token_urlsafe(32)}",
            "debugProjectSecretKey": f"sk-lf-{secrets.token_urlsafe(32)}",
        }
    pair: dict[str, str] = {}
    for field, prefix, normal in (
        ("debugProjectPublicKey", "pk-lf-", "initProjectPublicKey"),
        ("debugProjectSecretKey", "sk-lf-", "initProjectSecretKey"),
    ):
        value = values[field]
        if (
            not isinstance(value, str)
            or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{32,}", value)
            or value == values.get(normal)
        ):
            raise OpenBaoError("Rejected-request debug key pair is invalid or reuses tracing keys")
        pair[field] = value
    return pair
