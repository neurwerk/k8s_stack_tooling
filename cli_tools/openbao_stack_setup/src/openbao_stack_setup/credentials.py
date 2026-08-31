"""Generate and convergently store namespace-owned internal credentials."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError

type Generator = Callable[[], str]

INTERNAL_PATHS: tuple[str, ...] = (
    "auth-keycloak/internal",
    "frontend-dify/internal",
    "auth-keycloak-api-key-bridge/internal",
    "frontend-librechat/internal",
    "librechat-code-interpreter/internal",
    "monitor-langfuse/internal",
    "monitor-opensearch/internal",
    "frontend-studio/internal",
    "infra-postgres-auth/internal",
    "infra-postgres-operations/internal",
    "monitor-fluent-bit/internal",
    "monitor-kube-prometheus-stack/internal",
    "monitor-pii-engine/internal",
)


@dataclass(frozen=True)
class InternalResult:
    """Report internal records changed and fields added."""

    changed_paths: tuple[str, ...]
    added_fields: int


@dataclass(frozen=True)
class BootstrapPassword:
    """Describe one human password that is delivered during bootstrap."""

    key: str
    label: str
    path: str
    field: str


BOOTSTRAP_PASSWORDS: tuple[BootstrapPassword, ...] = (
    BootstrapPassword(
        "keycloak",
        "Keycloak bootstrap administrator",
        "auth-keycloak/internal",
        "adminPassword",
    ),
    BootstrapPassword(
        "dify",
        "Dify break-glass administrator",
        "frontend-dify/internal",
        "initPassword",
    ),
    BootstrapPassword(
        "langfuse",
        "Langfuse initial administrator",
        "monitor-langfuse/internal",
        "initUserPassword",
    ),
    BootstrapPassword(
        "grafana",
        "Grafana administrator",
        "monitor-kube-prometheus-stack/internal",
        "adminPassword",
    ),
)


def plan_bootstrap_passwords(client: OpenBaoClient) -> dict[str, str]:
    """Generate values only for human passwords that are absent from OpenBao."""
    planned: dict[str, str] = {}
    for password in BOOTSTRAP_PASSWORDS:
        current = client.read_secret(password.path)
        if current is None or password.field not in current.values:
            planned[password.key] = _random_secret()
    return planned


def reconcile_internal_credentials(
    client: OpenBaoClient, bootstrap_passwords: dict[str, str]
) -> InternalResult:
    """Add every missing internal field while preserving all existing values."""
    _validate_bootstrap_passwords(bootstrap_passwords)
    changed: list[str] = []
    added = 0

    keycloak, count = _upsert(
        client,
        "auth-keycloak/internal",
        _random_fields(
            "dbPassword",
            "difyOidcClientSecret",
            "difyAgentgatewayClientSecret",
            "bridgeOidcClientSecret",
            "librechatOidcClientSecret",
        ),
        _bootstrap_password_fields("auth-keycloak/internal", bootstrap_passwords),
    )
    _record_change(changed, "auth-keycloak/internal", count)
    added += count

    dify, count = _upsert(
        client,
        "frontend-dify/internal",
        _random_fields(
            "secretKey",
            "postgresPassword",
            "redisPassword",
            "sandboxApiKey",
            "pluginDaemonKey",
            "agentgatewayApiKey",
        ),
        {
            **_bootstrap_password_fields("frontend-dify/internal", bootstrap_passwords),
            "keycloakOidcClientSecret": _required_text(keycloak, "difyOidcClientSecret"),
        },
    )
    _record_change(changed, "frontend-dify/internal", count)
    added += count

    bridge_path = "auth-keycloak-api-key-bridge/internal"
    bridge_current = client.read_secret(bridge_path)
    bridge_values = dict(bridge_current.values) if bridge_current else {}
    dify_verifier = hashlib.sha256(_required_text(dify, "agentgatewayApiKey").encode()).hexdigest()
    primary_verifier = bridge_values.get("difyAgentgatewayPrimaryVerifierSha256")
    secondary_verifier = bridge_values.get("difyAgentgatewaySecondaryVerifierSha256")
    if primary_verifier not in (None, dify_verifier) and secondary_verifier != dify_verifier:
        raise OpenBaoError("Dify AgentGateway key does not match either managed verifier slot")
    bridge_fixed = {"keycloakClientSecret": _required_text(keycloak, "bridgeOidcClientSecret")}
    if primary_verifier is None:
        bridge_fixed["difyAgentgatewayPrimaryVerifierSha256"] = dify_verifier
    if secondary_verifier is None:
        bridge_fixed["difyAgentgatewaySecondaryVerifierSha256"] = ""

    _, count = _upsert(client, bridge_path, {}, bridge_fixed)
    _record_change(changed, bridge_path, count)
    added += count

    librechat_path = "frontend-librechat/internal"
    librechat, count = _upsert(
        client,
        librechat_path,
        {
            **_random_fields(
                "jwtSecret",
                "jwtRefreshSecret",
                "meiliMasterKey",
                "documentdbPassword",
                "openidSessionSecret",
                "valkeyPassword",
                "ragPostgresqlPassword",
                "ragOpenaiApiKey",
                "adminPanelSessionSecret",
                "adminPanelMetricsSecret",
            ),
            "credsKey": _hex_64,
            "credsIv": _hex_32,
        },
        {
            "documentdbUser": "librechat",
            "ragPostgresqlUser": "librechat_rag",
            "openidClientSecret": _required_text(keycloak, "librechatOidcClientSecret"),
        },
        ed25519_pair=("codeInterpreterJwtPrivateKey", "codeInterpreterJwtPublicKey"),
    )
    _record_change(changed, librechat_path, count)
    added += count

    code_interpreter_path = "librechat-code-interpreter/internal"
    _, count = _upsert(
        client,
        code_interpreter_path,
        _random_fields("internalServiceToken", "valkeyPassword", "egressGrantSecret"),
        {"jwtPublicKey": _required_text(librechat, "codeInterpreterJwtPublicKey")},
        ed25519_pair=("executionManifestPrivateKey", "executionManifestPublicKey"),
    )
    _record_change(changed, code_interpreter_path, count)
    added += count

    langfuse, count = _upsert(
        client,
        "monitor-langfuse/internal",
        {
            **_random_fields(
                "salt",
                "nextauthSecret",
                "postgresqlPassword",
                "clickhousePassword",
                "redisPassword",
            ),
            "encryptionKey": _hex_64,
            "initProjectPublicKey": _langfuse_public_key,
            "initProjectSecretKey": _langfuse_secret_key,
        },
        _bootstrap_password_fields("monitor-langfuse/internal", bootstrap_passwords),
    )
    _record_change(changed, "monitor-langfuse/internal", count)
    added += count

    opensearch, count = _upsert(
        client,
        "monitor-opensearch/internal",
        _random_fields("adminPassword", "fluentBitPassword", "studioPassword"),
    )
    _record_change(changed, "monitor-opensearch/internal", count)
    added += count

    remaining_records: tuple[tuple[str, dict[str, Generator], dict[str, str]], ...] = (
        (
            "frontend-studio/internal",
            {},
            {
                "opensearchPassword": _required_text(opensearch, "studioPassword"),
                "langfusePublicKey": _required_text(langfuse, "initProjectPublicKey"),
                "langfuseSecretKey": _required_text(langfuse, "initProjectSecretKey"),
            },
        ),
        (
            "monitor-fluent-bit/internal",
            {},
            {"ingestPassword": _required_text(opensearch, "fluentBitPassword")},
        ),
        (
            "monitor-kube-prometheus-stack/internal",
            {},
            {
                **_bootstrap_password_fields(
                    "monitor-kube-prometheus-stack/internal", bootstrap_passwords
                ),
                "adminUser": "admin",
            },
        ),
        (
            "monitor-pii-engine/internal",
            {"hashKey": _hex_64, "encryptionKey": _printable_32},
            {},
        ),
    )
    for path, generators, fixed in remaining_records:
        _, count = _upsert(client, path, generators, fixed)
        _record_change(changed, path, count)
        added += count

    postgres_records = (
        (
            "infra-postgres-auth/internal",
            {"keycloakPassword": _required_text(keycloak, "dbPassword")},
        ),
        (
            "infra-postgres-operations/internal",
            {
                "documentdbPassword": _required_text(librechat, "documentdbPassword"),
                "difyPassword": _required_text(dify, "postgresPassword"),
                "langfusePassword": _required_text(langfuse, "postgresqlPassword"),
                "librechatRagPassword": _required_text(librechat, "ragPostgresqlPassword"),
            },
        ),
    )
    for path, fixed in postgres_records:
        _, count = _upsert(client, path, _random_fields("adminPassword"), fixed)
        _record_change(changed, path, count)
        added += count
    return InternalResult(tuple(changed), added)


def migrate_schema_2_internal_credentials(client: OpenBaoClient) -> None:
    """Migrate the exact retired LibreChat fields written by schema 2."""
    path = "frontend-librechat/internal"
    current = client.read_secret(path)
    if current is None:
        return

    values = dict(current.values)
    rag_user = values.get("ragPostgresqlUser")
    if rag_user not in ("librechat", "librechat_rag"):
        raise OpenBaoError(f"Internal credential mismatch at {path}/ragPostgresqlUser")

    changed = False
    if rag_user == "librechat":
        values["ragPostgresqlUser"] = "librechat_rag"
        changed = True
    for field in ("ferretdbPassword", "ferretdbUser", "weaviateApiKey"):
        if field in values:
            del values[field]
            changed = True
    if changed:
        client.write_secret(path, values, current.version)


def _validate_bootstrap_passwords(passwords: dict[str, str]) -> None:
    known = {password.key for password in BOOTSTRAP_PASSWORDS}
    if not set(passwords).issubset(known) or any(not value for value in passwords.values()):
        raise OpenBaoError("Bootstrap passwords are invalid")


def _bootstrap_password_fields(path: str, passwords: dict[str, str]) -> dict[str, str]:
    return {
        password.field: passwords[password.key]
        for password in BOOTSTRAP_PASSWORDS
        if password.path == path and password.key in passwords
    }


def _upsert(
    client: OpenBaoClient,
    path: str,
    generators: dict[str, Generator],
    fixed: dict[str, str] | None = None,
    *,
    ed25519_pair: tuple[str, str] | None = None,
) -> tuple[dict[str, JsonValue], int]:
    current = client.read_secret(path)
    values = dict(current.values) if current else {}
    added = 0
    if ed25519_pair is not None:
        added += _reconcile_ed25519_pair(values, path, *ed25519_pair)
    for name, generator in generators.items():
        if name not in values:
            values[name] = generator()
            added += 1
    for name, value in (fixed or {}).items():
        if name in values:
            if values[name] != value:
                raise OpenBaoError(f"Internal credential mismatch at {path}/{name}")
            continue
        values[name] = value
        added += 1
    if added:
        client.write_secret(path, values, current.version if current else 0)
    return values, added


def _reconcile_ed25519_pair(
    values: dict[str, JsonValue], path: str, private_field: str, public_field: str
) -> int:
    private_present = private_field in values
    public_present = public_field in values
    if private_present != public_present:
        raise OpenBaoError(f"Ed25519 key pair is incomplete at {path}")
    if not private_present:
        private_key = Ed25519PrivateKey.generate()
        values[private_field] = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        values[public_field] = (
            private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        return 2

    private_value = _required_text(values, private_field)
    public_value = _required_text(values, public_field)
    try:
        private_key = serialization.load_pem_private_key(
            private_value.encode("ascii"), password=None
        )
        public_key = serialization.load_pem_public_key(public_value.encode("ascii"))
    except (TypeError, UnicodeError, ValueError, UnsupportedAlgorithm):
        raise OpenBaoError(f"Ed25519 key pair is invalid at {path}") from None
    if not isinstance(private_key, Ed25519PrivateKey) or not isinstance(
        public_key, Ed25519PublicKey
    ):
        raise OpenBaoError(f"Ed25519 key pair is invalid at {path}")
    expected_public = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    actual_public = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if expected_public != actual_public:
        raise OpenBaoError(f"Ed25519 key pair does not match at {path}")
    return 0


def _random_fields(*names: str) -> dict[str, Generator]:
    return dict.fromkeys(names, _random_secret)


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _hex_64() -> str:
    return secrets.token_hex(32)


def _hex_32() -> str:
    return secrets.token_hex(16)


def _printable_32() -> str:
    return secrets.token_urlsafe(24)


def _langfuse_public_key() -> str:
    return f"lf_pk_{secrets.token_urlsafe(32)}"


def _langfuse_secret_key() -> str:
    return f"lf_sk_{secrets.token_urlsafe(32)}"


def _required_text(values: dict[str, JsonValue], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise OpenBaoError(f"Internal credential field {name} is not a nonblank string")
    return value


def _record_change(changed: list[str], path: str, count: int) -> None:
    if count:
        changed.append(path)
