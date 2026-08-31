"""Describe prompted provider credentials and their OpenBao records."""

from __future__ import annotations

from dataclasses import dataclass

from openbao_stack_setup.catalog import SMTP_REPLICA
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError


@dataclass(frozen=True)
class Provider:
    """Map one CLI credential name to exact namespace-local KV v2 records."""

    name: str
    paths: tuple[str, ...]
    fields: tuple[str, ...]


PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider("openrouter", ("infra-agentgateway/external",), ("openrouterApiKey",)),
    "deepseek": Provider("deepseek", ("infra-agentgateway/external",), ("deepseekApiKey",)),
    "brave": Provider("brave", ("infra-agentgateway/external",), ("braveApiKey",)),
    "route53": Provider(
        "route53", ("infra-cert-manager/external",), ("accessKeyId", "secretAccessKey")
    ),
}

SMTP = Provider(
    "smtp",
    (SMTP_REPLICA.source_path, *SMTP_REPLICA.destination_paths),
    SMTP_REPLICA.fields,
)
ACTIVE_DIRECTORY = Provider(
    "active-directory",
    ("auth-keycloak/external",),
    ("activeDirectoryBindDn", "activeDirectoryBindCredential"),
)
MANAGED_CREDENTIALS: dict[str, Provider] = {
    **PROVIDERS,
    "smtp": SMTP,
    "active-directory": ACTIVE_DIRECTORY,
}


def update_provider(client: OpenBaoClient, provider: Provider, values: dict[str, str]) -> None:
    """CAS-update all credential records while preserving sibling values."""
    if set(values) != set(provider.fields) or any(not value.strip() for value in values.values()):
        raise OpenBaoError("Provider credentials are incomplete")
    for path in provider.paths:
        current = client.read_secret(path)
        merged: dict[str, JsonValue] = dict(current.values) if current else {}
        merged.update(values)
        client.write_secret(path, merged, current.version if current else 0)
