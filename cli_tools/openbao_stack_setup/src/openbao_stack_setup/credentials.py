"""Generate and convergently store namespace-owned internal credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError

type Generator = Callable[[], str]

INTERNAL_PATHS: tuple[str, ...] = (
    "auth-keycloak/internal",
    "auth-keycloak-api-key-bridge/internal",
    "monitor-langfuse/internal",
    "monitor-opensearch/internal",
    "frontend-studio/internal",
    "infra-agentgateway/internal",
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


def plan_bootstrap_passwords(
    client: OpenBaoClient, *, addons: tuple[str, ...] = ()
) -> dict[str, str]:
    """Generate values only for human passwords that are absent from OpenBao."""
    planned: dict[str, str] = {}
    for password in BOOTSTRAP_PASSWORDS:
        if password.key == "dify" and "dify" not in addons:
            continue
        current = client.read_secret(password.path)
        if current is None or password.field not in current.values:
            planned[password.key] = _random_secret()
    return planned


def reconcile_internal_credentials(
    client: OpenBaoClient,
    bootstrap_passwords: dict[str, str],
    *,
    docling_enabled: bool = False,
    addons: tuple[str, ...] = (),
    contextforge_admin_email: str | None = None,
    glitchtip_enabled: bool = False,
) -> InternalResult:
    """Add every missing internal field while preserving all existing values."""
    _validate_bootstrap_passwords(bootstrap_passwords)
    changed: list[str] = []
    added = 0

    keycloak_generators = _random_fields("dbPassword", "bridgeOidcClientSecret")
    addon_oidc_fields = {
        "open-webui": ("openwebuiClientSecret",),
        "librechat": ("librechatOidcClientSecret",),
        "dify": ("difyOidcClientSecret", "difyAgentgatewayClientSecret"),
    }
    keycloak_generators.update(
        _random_fields(*(field for addon in addons for field in addon_oidc_fields.get(addon, ())))
    )
    keycloak, count = _upsert(
        client,
        "auth-keycloak/internal",
        keycloak_generators,
        _bootstrap_password_fields("auth-keycloak/internal", bootstrap_passwords),
    )
    _record_change(changed, "auth-keycloak/internal", count)
    added += count

    bridge_path = "auth-keycloak-api-key-bridge/internal"
    bridge_current = client.read_secret(bridge_path)
    bridge_values = dict(bridge_current.values) if bridge_current else {}
    bridge_fixed = {"keycloakClientSecret": _required_text(keycloak, "bridgeOidcClientSecret")}
    dify: dict[str, JsonValue] = {}
    if "dify" in addons:
        dify, count = _reconcile_dify(
            client, keycloak, bridge_values, bridge_fixed, bootstrap_passwords
        )
        _record_change(changed, "frontend-dify/internal", count)
        added += count

    bridge, count = _upsert(client, bridge_path, _random_fields("postgresqlPassword"), bridge_fixed)
    _record_change(changed, bridge_path, count)
    added += count

    librechat: dict[str, JsonValue] = {}
    if "librechat" in addons:
        librechat, count = _upsert(
            client,
            "frontend-librechat/internal",
            {
                **_random_fields(
                    "jwtSecret",
                    "jwtRefreshSecret",
                    "meiliMasterKey",
                    "documentdbPassword",
                    "openidSessionSecret",
                    "valkeyPassword",
                    "adminPanelSessionSecret",
                    "adminPanelMetricsSecret",
                ),
                "credsKey": _hex_64,
                "credsIv": _hex_32,
            },
            {
                "documentdbUser": "librechat",
                "openidClientSecret": _required_text(keycloak, "librechatOidcClientSecret"),
            },
        )
        _record_change(changed, "frontend-librechat/internal", count)
        added += count

    openwebui: dict[str, JsonValue] = {}
    if "open-webui" in addons:
        openwebui, count = _upsert(
            client,
            "frontend-open-webui/internal",
            _random_fields(
                "postgresqlPassword",
                "webuiSecretKey",
                "oauthSessionTokenEncryptionKey",
                "oauthClientInfoEncryptionKey",
            ),
            {"openidClientSecret": _required_text(keycloak, "openwebuiClientSecret")},
        )
        _record_change(changed, "frontend-open-webui/internal", count)
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
        {
            **_random_fields(
                "adminPassword",
                "dashboardCookieSecret",
                "dashboardPassword",
                "fluentBitPassword",
                "provisionerPassword",
                "reportReaderPassword",
                "studioPassword",
            ),
            "queryDatasourceEncryptionKey": _printable_32,
        },
    )
    _validate_opensearch_query_encryption_key(opensearch)
    _record_change(changed, "monitor-opensearch/internal", count)
    added += count

    studio, count = _upsert(
        client,
        "frontend-studio/internal",
        _random_fields("postgresqlPassword"),
        {
            "opensearchPassword": _required_text(opensearch, "studioPassword"),
            "llmLogsPublicKey": _required_text(langfuse, "initProjectPublicKey"),
            "llmLogsSecretKey": _required_text(langfuse, "initProjectSecretKey"),
        },
    )
    _record_change(changed, "frontend-studio/internal", count)
    added += count

    remaining_records: tuple[tuple[str, dict[str, Generator], dict[str, str]], ...] = (
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

    agentgateway, count = _upsert(
        client,
        "infra-agentgateway/internal",
        _random_fields("postgresqlPassword"),
    )
    _record_change(changed, "infra-agentgateway/internal", count)
    added += count

    postgres_records = (
        (
            "infra-postgres-auth/internal",
            {"keycloakPassword": _required_text(keycloak, "dbPassword")},
        ),
        (
            "infra-postgres-operations/internal",
            {
                "agentgatewayPassword": _required_text(agentgateway, "postgresqlPassword"),
                "apiKeyBridgePassword": _required_text(bridge, "postgresqlPassword"),
                "studioPassword": _required_text(studio, "postgresqlPassword"),
                "langfusePassword": _required_text(langfuse, "postgresqlPassword"),
            },
        ),
    )
    postgres_records[1][1].update(
        {
            field: _required_text(values, source)
            for addon, field, values, source in (
                ("dify", "difyPassword", dify, "postgresPassword"),
                ("librechat", "documentdbPassword", librechat, "documentdbPassword"),
                ("open-webui", "openwebuiPassword", openwebui, "postgresqlPassword"),
            )
            if addon in addons
        }
    )
    for path, fixed in postgres_records:
        _, count = _upsert(client, path, _random_fields("adminPassword"), fixed)
        _record_change(changed, path, count)
        added += count
    if "knowledge-center" in addons:
        forgejo, count = _upsert(
            client,
            "forgejo/internal",
            {
                **_random_fields(
                    "dbPassword",
                    "oidcClientSecret",
                    "oauth2JwtSecret",
                    "lfsJwtSecret",
                    "adminPassword",
                ),
                "secretKey": _hex_64,
                "internalToken": _forgejo_internal_token,
            },
        )
        _record_change(changed, "forgejo/internal", count)
        added += count
        for path, field, source in (
            ("infra-postgres-operations/internal", "forgejoPassword", "dbPassword"),
            ("auth-keycloak/internal", "forgejoClientSecret", "oidcClientSecret"),
        ):
            _, count = _upsert(client, path, {}, {field: _required_text(forgejo, source)})
            _record_change(changed, path, count)
            added += count
    if docling_enabled:
        docling, count = _upsert(client, "docling/internal", _random_fields("apiKey"))
        api_key = _docling_api_key(docling)
        _record_change(changed, "docling/internal", count)
        added += count
        path = "monitor-agentgateway-extproc/internal"
        _, count = _upsert(client, path, {}, {"doclingApiKey": api_key})
        _record_change(changed, path, count)
        added += count
    for result in (
        _reconcile_contextforge(client, contextforge_admin_email),
        _reconcile_glitchtip(client, enabled=glitchtip_enabled),
    ):
        changed = list(dict.fromkeys((*changed, *result.changed_paths)))
        added += result.added_fields
    return InternalResult(tuple(changed), added)


def _reconcile_glitchtip(client: OpenBaoClient, *, enabled: bool) -> InternalResult:
    if not enabled:
        return InternalResult((), 0)
    changed: list[str] = []
    added = 0
    password = _glitchtip_database_password(client)
    for path, generators, fixed in (
        ("glitchtip/internal", _random_fields("secretKey"), {"databasePassword": password}),
        ("infra-postgres-operations/internal", {}, {"glitchtipPassword": password}),
        (
            "glitchtip/adapter",
            _random_fields("opensearchPassword"),
            {"opensearchUsername": "glitchtip-adapter"},
        ),
    ):
        values, count = _upsert(client, path, generators, fixed)
        for field in generators:
            value = _required_text(values, field)
            if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise OpenBaoError("GlitchTip internal credential is invalid; refusing replacement")
        _record_change(changed, path, count)
        added += count
    for path, fields in (
        ("glitchtip/adapter", ("dsn",)),
        ("glitchtip/external", ("smtpUsername", "smtpPassword")),
        ("glitchtip/owner", ("email", "password")),
    ):
        _, count = _upsert(client, path, dict.fromkeys(fields, lambda: ""))
        _record_change(changed, path, count)
        added += count
    return InternalResult(tuple(changed), added)


def _glitchtip_database_password(client: OpenBaoClient) -> str:
    """Preserve either existing copy and reject conflicts before changing either record."""
    copies = []
    for path, field in (
        ("glitchtip/internal", "databasePassword"),
        ("infra-postgres-operations/internal", "glitchtipPassword"),
    ):
        current = client.read_secret(path)
        if current is not None and field in current.values:
            value = _required_text(current.values, field)
            if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise OpenBaoError("GlitchTip database credential is invalid; refusing replacement")
            copies.append(value)
    if len(set(copies)) > 1:
        raise OpenBaoError("GlitchTip database credential copies conflict; refusing replacement")
    return copies[0] if copies else _random_secret()


def _reconcile_contextforge(client: OpenBaoClient, admin_email: str | None) -> InternalResult:
    if admin_email is None:
        return InternalResult((), 0)
    fields = _random_fields(
        "postgresqlPassword",
        "jwtSecretKey",
        "authEncryptionSecret",
        "platformAdminPassword",
        "defaultUserPassword",
    )
    current = client.read_secret("contextforge/internal")
    for field in fields:
        if current is not None and field in current.values:
            value = _required_text(current.values, field)
            if not value.strip() or "\r" in value or "\n" in value:
                raise OpenBaoError("ContextForge credential is invalid; refusing to replace it")
    values, added = _upsert(
        client, "contextforge/internal", fields, {"platformAdminEmail": admin_email}
    )
    changed = ["contextforge/internal"] if added else []
    path = "infra-postgres-operations/internal"
    _, count = _upsert(
        client, path, {}, {"contextforgePassword": _required_text(values, "postgresqlPassword")}
    )
    _record_change(changed, path, count)
    return InternalResult(tuple(changed), added + count)


def _reconcile_dify(
    client: OpenBaoClient,
    keycloak: dict[str, JsonValue],
    bridge_values: dict[str, JsonValue],
    bridge_fixed: dict[str, str],
    bootstrap_passwords: dict[str, str],
) -> tuple[dict[str, JsonValue], int]:
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
            "difyAgentApiToken",
            "difyAgentServerSecretKey",
            "difyAgentSandboxAuthToken",
        ),
        {
            **_bootstrap_password_fields("frontend-dify/internal", bootstrap_passwords),
            "keycloakOidcClientSecret": _required_text(keycloak, "difyOidcClientSecret"),
        },
    )
    dify_verifier = hashlib.sha256(_required_text(dify, "agentgatewayApiKey").encode()).hexdigest()
    primary_verifier = bridge_values.get("difyAgentgatewayPrimaryVerifierSha256")
    secondary_verifier = bridge_values.get("difyAgentgatewaySecondaryVerifierSha256")
    if primary_verifier not in (None, dify_verifier) and secondary_verifier != dify_verifier:
        raise OpenBaoError("Dify AgentGateway key does not match either managed verifier slot")
    if primary_verifier is None:
        bridge_fixed["difyAgentgatewayPrimaryVerifierSha256"] = dify_verifier
    if secondary_verifier is None:
        bridge_fixed["difyAgentgatewaySecondaryVerifierSha256"] = ""
    return dify, count


def _docling_api_key(values: dict[str, JsonValue]) -> str:
    """Validate the stored service key without changing it."""
    value = _required_text(values, "apiKey")
    if not value.strip() or "\r" in value or "\n" in value:
        raise OpenBaoError("Docling API key is invalid; refusing to replace it")
    return value


def _validate_opensearch_query_encryption_key(values: dict[str, JsonValue]) -> None:
    value = _required_text(values, "queryDatasourceEncryptionKey")
    if len(value) != 32:
        raise OpenBaoError("OpenSearch query data-source encryption key must be 32 characters")


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
) -> tuple[dict[str, JsonValue], int]:
    current = client.read_secret(path)
    values = dict(current.values) if current else {}
    added = 0
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


def _random_fields(*names: str) -> dict[str, Generator]:
    return dict.fromkeys(names, _random_secret)


def _random_secret() -> str:
    return secrets.token_urlsafe(32)


def _forgejo_internal_token() -> str:
    """Match Forgejo generate.NewInternalToken's HS256 token and random signing key."""
    header = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"nbf": int(time.time())}, separators=(",", ":")).encode("ascii")
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    message = f"{header}.{payload}"
    signature = hmac.digest(_random_secret().encode("ascii"), message.encode("ascii"), "sha256")
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{message}.{encoded}"


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
    if count and path not in changed:
        changed.append(path)
