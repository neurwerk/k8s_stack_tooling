"""Prepare isolated shared MCP keys; Studio owns later key entry and replacement."""

from __future__ import annotations

import re
from uuid import UUID

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError, SecretRecord

_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def shared_ids(rows: object) -> tuple[str, ...]:
    """Accept only unique chart-selected shared-key integrations, never caller paths."""
    if not isinstance(rows, list) or len(rows) > 200:
        raise OpenBaoError("Invalid Studio MCP credential catalog")
    identities: set[str] = set()
    selected: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise OpenBaoError("Invalid Studio MCP credential catalog")
        identity = row.get("id")
        credential = row.get("credential")
        if (
            not isinstance(identity, str)
            or not _ID_PATTERN.fullmatch(identity)
            or identity in identities
            or not isinstance(credential, dict)
            or credential.get("owner") not in ("shared", "individual", "none")
        ):
            raise OpenBaoError("Invalid Studio MCP credential catalog")
        identities.add(identity)
        if credential["owner"] == "shared":
            if credential.get("method") not in ("upstream-env", "gateway-header"):
                raise OpenBaoError("Unsupported shared MCP credential delivery")
            selected.append(identity)
    return tuple(sorted(selected))


def reconcile_mcp(client: OpenBaoClient, identities: tuple[str, ...] | None) -> int:
    """Scope both workload roles and return the number of newly initialized blank records."""
    if identities is None:
        return 0
    if (
        len(identities) > 200
        or any(
            not isinstance(identity, str) or not _ID_PATTERN.fullmatch(identity)
            for identity in identities
        )
        or len(identities) != len(set(identities))
    ):
        raise OpenBaoError("Invalid Studio MCP credential catalog")
    created = 0
    for identity in identities:
        path = f"mcp/shared/{identity}"
        record = client.read_secret(path)
        if record is None:
            client.write_secret(
                path, {"apiKey": "", "version": "initial", "operationId": "", "kvVersion": 1}, 0
            )
            created += 1
        elif not _valid_record(record):
            raise OpenBaoError("Existing MCP credential record has an invalid schema")
    for role, namespace, account, capabilities in (
        ("studio-mcp", "frontend-studio", "studio-mcp", '["read", "create", "update"]'),
        ("mcp-shared-delivery", "infra-agentgateway", "mcp-shared-delivery", '["read"]'),
    ):
        policy = (
            "\n\n".join(
                f'path "secret/data/mcp/shared/{identity}" {{\n  capabilities = {capabilities}\n}}'
                for identity in identities
            )
            or 'path "secret/data/mcp/shared/*" { capabilities = ["deny"] }'
        )
        if role == "mcp-shared-delivery":
            # ESO validates and closes its own token even with the default policy disabled.
            policy += (
                '\npath "auth/token/lookup-self" { capabilities = ["read"] }'
                '\npath "auth/token/revoke-self" { capabilities = ["update"] }'
            )
        client.write_policy(role, policy + "\n")
        client._write(
            f"auth/kubernetes/role/{role}",
            {
                "bound_service_account_names": [account],
                "bound_service_account_namespaces": [namespace],
                "audience": "openbao",
                "token_policies": [role],
                "token_no_default_policy": True,
                "token_ttl": "5m",
                "token_max_ttl": "5m",
            },
        )
    return created


def _valid_record(record: SecretRecord) -> bool:
    values = record.values
    if (
        set(values) != {"apiKey", "version", "operationId", "kvVersion"}
        or not isinstance(values["apiKey"], str)
        or not isinstance(values["operationId"], str)
        or type(values["kvVersion"]) is not int
        or values["kvVersion"] != record.version
        or record.version < 1
    ):
        return False
    if values["version"] == "initial":
        return values["apiKey"] == values["operationId"] == "" and record.version == 1
    try:
        operation = UUID(values["operationId"])
    except ValueError:
        return False
    return values["version"] == operation.hex and values["operationId"] == str(operation)
