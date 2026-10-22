"""Describe prompted provider credentials and their OpenBao records."""

from __future__ import annotations

from dataclasses import dataclass

from openbao_stack_setup.catalog import SMTP_REPLICA, TAILSCALE_OAUTH_FIELDS, TAILSCALE_OAUTH_PATH
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
    "tailscale-oauth": Provider("tailscale-oauth", (TAILSCALE_OAUTH_PATH,), TAILSCALE_OAUTH_FIELDS),
    "glitchtip-smtp": Provider(
        "glitchtip-smtp", ("glitchtip/external",), ("smtpUsername", "smtpPassword")
    ),
    "glitchtip-owner": Provider("glitchtip-owner", ("glitchtip/owner",), ("email", "password")),
    "smtp": SMTP,
    "active-directory": ACTIVE_DIRECTORY,
    "docling-inference": Provider("docling-inference", ("docling/external",), ("inferenceToken",)),
    "image-inspection": Provider(
        "image-inspection", ("monitor-agentgateway-extproc/external",), ("apiKey",)
    ),
    "librechat-stt": Provider("librechat-stt", ("frontend-librechat/external",), ("sttApiKey",)),
    "librechat-tts": Provider("librechat-tts", ("frontend-librechat/external",), ("ttsApiKey",)),
}


def managed_credential_paths(*, tailscale_enabled: bool = False) -> tuple[str, ...]:
    """Select routine operator paths without granting access to an unselected OAuth catalog."""
    return tuple(
        path
        for name, provider in MANAGED_CREDENTIALS.items()
        if name != "tailscale-oauth" or tailscale_enabled
        for path in provider.paths
    )


def update_provider(client: OpenBaoClient, provider: Provider, values: dict[str, str]) -> None:
    """CAS-update all credential records while preserving sibling values."""
    if set(values) != set(provider.fields) or any(not value.strip() for value in values.values()):
        raise OpenBaoError("Provider credentials are incomplete")
    if provider.name == "tailscale-oauth" and any(
        len(value) > 16384
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values.values()
    ):
        raise OpenBaoError("Invalid Tailscale OAuth credentials; values are hidden")
    if provider.name == "docling-inference" and any(
        "\r" in value or "\n" in value for value in values.values()
    ):
        raise OpenBaoError("Docling inference token must not contain CR or LF")
    if provider.name.startswith("glitchtip-") and any(
        any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values.values()
    ):
        raise OpenBaoError("GlitchTip credentials contain invalid control characters")
    for path in provider.paths:
        current = client.read_secret(path)
        merged: dict[str, JsonValue] = dict(current.values) if current else {}
        merged.update(values)
        client.write_secret(path, merged, current.version if current else 0)
