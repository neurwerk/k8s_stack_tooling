"""Declare the reviewed OpenBao and Kubernetes reconciliation catalog."""

from __future__ import annotations

from dataclasses import dataclass

RECONCILIATION_STATE_PATH = "stack-setup/reconciliation-state"
CONTEXTFORGE_PROVIDER_APPS_PATH = "contextforge/provider-apps"
REJECTION_DEBUG_PATH = "monitor-agentgateway-extproc/langfuse-debug"
REJECTION_DEBUG_ROLE = "monitor-agentgateway-extproc-rejection-debug"
ACTIVE_DIRECTORY_FIELDS = ("activeDirectoryBindDn", "activeDirectoryBindCredential")

ROLE_NAMESPACES: tuple[str, ...] = (
    "auth-keycloak",
    "auth-keycloak-api-key-bridge",
    "frontend-studio",
    "infra-agentgateway",
    "infra-cert-manager",
    "infra-postgres-auth",
    "infra-postgres-operations",
    "monitor-fluent-bit",
    "monitor-kube-prometheus-stack",
    "monitor-langfuse",
    "monitor-opensearch",
    "monitor-pii-engine",
)


@dataclass(frozen=True)
class SecretReplica:
    """Allow exact fields to be copied between namespace-isolated records."""

    source_path: str
    legacy_source_path: str
    destination_paths: tuple[str, ...]
    fields: tuple[str, ...]


SMTP_REPLICA = SecretReplica(
    "stack-setup/providers/smtp",
    "auth-keycloak/external",
    (
        "auth-keycloak/external",
        "monitor-kube-prometheus-stack/external",
        "monitor-opensearch/external",
    ),
    ("smtpUsername", "smtpPassword"),
)


@dataclass(frozen=True)
class ExternalSecretTarget:
    """Identify one ExternalSecret and its namespace-local target Secret."""

    name: str
    namespace: str
    target_secret: str


@dataclass(frozen=True)
class SecretStoreTarget:
    """Identify one namespace-local OpenBao SecretStore."""

    name: str
    namespace: str


@dataclass(frozen=True)
class HelmReleaseTarget:
    """Identify one HelmRelease and its allowed convergence time."""

    name: str
    namespace: str
    timeout_seconds: int = 900


@dataclass(frozen=True)
class ProviderRefreshTarget:
    """Bind an OpenBao provider record to its runtime resources."""

    path: str
    fields: tuple[str, ...]
    external_secret: ExternalSecretTarget
    helm_releases: tuple[HelmReleaseTarget, ...] = ()


AUTH_KEYCLOAK_SMTP_EXTERNAL_SECRET = ExternalSecretTarget(
    "auth-keycloak-smtp-secret", "auth-keycloak", "auth-keycloak-smtp-secret"
)
AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET = ExternalSecretTarget(
    "auth-keycloak-active-directory-secret",
    "auth-keycloak",
    "auth-keycloak-active-directory-secret",
)
AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE = HelmReleaseTarget(
    "keycloak-active-directory", "auth-keycloak"
)
MONITOR_KUBE_PROMETHEUS_STACK_SMTP_EXTERNAL_SECRET = ExternalSecretTarget(
    "monitor-kube-prometheus-stack-smtp-secret",
    "monitor-kube-prometheus-stack",
    "monitor-kube-prometheus-stack-smtp-secret",
)
MONITOR_OPENSEARCH_EXTERNAL_SECRET = ExternalSecretTarget(
    "monitor-opensearch-secret", "monitor-opensearch", "monitor-opensearch-secret"
)
INFRA_AGENTGATEWAY_EXTERNAL_SECRET = ExternalSecretTarget(
    "infra-agentgateway-secrets", "infra-agentgateway", "infra-agentgateway-secrets"
)
CERT_MANAGER_ISSUERS_EXTERNAL_SECRET = ExternalSecretTarget(
    "cert-manager-issuers-values", "infra-cert-manager", "cert-manager-issuers-values"
)
FORGEJO_SECRET_STORE = SecretStoreTarget("forgejo-openbao-secret-store", "forgejo")
CONTEXTFORGE_SECRET_STORE = SecretStoreTarget("contextforge-openbao-secret-store", "contextforge")
CONTEXTFORGE_EXTERNAL_SECRETS = (
    ExternalSecretTarget("contextforge-runtime", "contextforge", "contextforge-runtime"),
    ExternalSecretTarget(
        "contextforge-postgres-values", "infra-postgres-operations", "contextforge-postgres-values"
    ),
)
DOCLING_NAMESPACES = ("docling", "monitor-agentgateway-extproc")
DOCLING_SECRET_STORES = tuple(
    SecretStoreTarget(f"{namespace}-openbao-secret-store", namespace)
    for namespace in DOCLING_NAMESPACES
)
MONITOR_AGENTGATEWAY_EXTPROC_IMAGE_INSPECTION_EXTERNAL_SECRET = ExternalSecretTarget(
    "monitor-agentgateway-extproc-image-inspection-secret",
    "monitor-agentgateway-extproc",
    "monitor-agentgateway-extproc-image-inspection-secret",
)
DOCLING_EXTERNAL_SECRETS = (
    ExternalSecretTarget("docling-api", "docling", "docling-api"),
    ExternalSecretTarget(
        "monitor-agentgateway-extproc-docling-secret",
        "monitor-agentgateway-extproc",
        "monitor-agentgateway-extproc-docling-secret",
    ),
)
WIREGUARD_SECRET_STORE = SecretStoreTarget("wireguard-openbao-secret-store", "wireguard")
REJECTION_DEBUG_SECRET_STORE = SecretStoreTarget(
    REJECTION_DEBUG_ROLE, "monitor-agentgateway-extproc"
)
REJECTION_DEBUG_EXTERNAL_SECRETS = (
    ExternalSecretTarget(
        "monitor-langfuse-rejection-debug", "monitor-langfuse", "monitor-langfuse-rejection-debug"
    ),
    ExternalSecretTarget(
        REJECTION_DEBUG_ROLE, "monitor-agentgateway-extproc", REJECTION_DEBUG_ROLE
    ),
)
WIREGUARD_EXTERNAL_SECRET = ExternalSecretTarget(
    "wireguard-server-key", "wireguard", "wireguard-server-key"
)
FORGEJO_EXTERNAL_SECRETS = (
    ExternalSecretTarget("forgejo-runtime", "forgejo", "forgejo-runtime"),
    ExternalSecretTarget(
        "forgejo-postgres-password", "infra-postgres-operations", "forgejo-postgres-password"
    ),
    ExternalSecretTarget("forgejo-oidc-values", "auth-keycloak", "forgejo-oidc-values"),
)

BOOTSTRAP_EXTERNAL_SECRETS: tuple[ExternalSecretTarget, ...] = (
    ExternalSecretTarget(
        "auth-keycloak-openbao-secret", "auth-keycloak", "auth-keycloak-openbao-secret"
    ),
    ExternalSecretTarget("auth-keycloak-secrets", "auth-keycloak", "auth-keycloak-secrets"),
    AUTH_KEYCLOAK_SMTP_EXTERNAL_SECRET,
    ExternalSecretTarget(
        "auth-keycloak-api-key-bridge-openbao-secret",
        "auth-keycloak-api-key-bridge",
        "auth-keycloak-api-key-bridge-openbao-secret",
    ),
    ExternalSecretTarget(
        "frontend-studio-openbao-secret", "frontend-studio", "frontend-studio-openbao-secret"
    ),
    INFRA_AGENTGATEWAY_EXTERNAL_SECRET,
    CERT_MANAGER_ISSUERS_EXTERNAL_SECRET,
    ExternalSecretTarget("postgres-auth-values", "infra-postgres-auth", "postgres-auth-values"),
    ExternalSecretTarget(
        "postgres-operations-values", "infra-postgres-operations", "postgres-operations-values"
    ),
    ExternalSecretTarget(
        "monitor-fluent-bit-shared-ingest-secret",
        "monitor-fluent-bit",
        "monitor-fluent-bit-shared-ingest-secret",
    ),
    ExternalSecretTarget(
        "monitor-kube-prometheus-stack-secret",
        "monitor-kube-prometheus-stack",
        "monitor-kube-prometheus-stack-secret",
    ),
    MONITOR_KUBE_PROMETHEUS_STACK_SMTP_EXTERNAL_SECRET,
    ExternalSecretTarget(
        "monitor-langfuse-secrets", "monitor-langfuse", "monitor-langfuse-secrets"
    ),
    MONITOR_OPENSEARCH_EXTERNAL_SECRET,
    ExternalSecretTarget(
        "monitor-pii-engine-secrets", "monitor-pii-engine", "monitor-pii-engine-secrets"
    ),
)

DIFY_EXTERNAL_SECRETS: tuple[ExternalSecretTarget, ...] = (
    ExternalSecretTarget("dify-oidc-secret", "auth-keycloak", "dify-oidc-secret"),
    ExternalSecretTarget(
        "frontend-dify-postgres-password",
        "infra-postgres-operations",
        "frontend-dify-postgres-password",
    ),
    ExternalSecretTarget(
        "frontend-dify-openbao-secret", "frontend-dify", "frontend-dify-openbao-secret"
    ),
    ExternalSecretTarget(
        "frontend-dify-runtime-secret", "frontend-dify", "frontend-dify-runtime-secret"
    ),
    ExternalSecretTarget(
        "dify-managed-key-verifiers",
        "auth-keycloak-api-key-bridge",
        "dify-managed-key-verifiers",
    ),
)
DIFY_SECRET_STORE = SecretStoreTarget("frontend-dify-openbao-secret-store", "frontend-dify")


@dataclass(frozen=True)
class AddonCatalog:
    """One reviewed addon selection and its credential consumers."""

    configuration_key: str
    namespace: str
    secret_store: SecretStoreTarget
    external_secrets: tuple[ExternalSecretTarget, ...]
    database_release: HelmReleaseTarget
    consumers: tuple[HelmReleaseTarget, ...]
    workloads: tuple[str, ...]


ADDONS = {
    "librechat": AddonCatalog(
        "frontendLibrechat",
        "frontend-librechat",
        SecretStoreTarget("frontend-librechat-openbao-secret-store", "frontend-librechat"),
        (
            ExternalSecretTarget(
                "frontend-librechat-runtime-secret",
                "frontend-librechat",
                "frontend-librechat-runtime-secret",
            ),
            ExternalSecretTarget("librechat-oidc-secret", "auth-keycloak", "librechat-oidc-secret"),
            ExternalSecretTarget(
                "librechat-documentdb-password",
                "infra-postgres-operations",
                "librechat-documentdb-password",
            ),
        ),
        HelmReleaseTarget("librechat-documentdb", "infra-postgres-operations"),
        (
            HelmReleaseTarget("keycloak-librechat-access", "auth-keycloak"),
            HelmReleaseTarget("librechat-oidc-settings", "auth-keycloak"),
            HelmReleaseTarget("keycloak-librechat-oidc", "auth-keycloak"),
            HelmReleaseTarget("frontend-librechat-shared", "frontend-librechat"),
        ),
        (
            "statefulset/frontend-librechat-valkey",
            "statefulset/frontend-librechat-meilisearch",
            "deployment/frontend-librechat",
            "deployment/frontend-librechat-admin-panel",
        ),
    ),
    "dify": AddonCatalog(
        "dify",
        "frontend-dify",
        DIFY_SECRET_STORE,
        DIFY_EXTERNAL_SECRETS,
        HelmReleaseTarget("frontend-dify-postgres", "infra-postgres-operations"),
        (
            HelmReleaseTarget("keycloak-dify-oidc", "auth-keycloak"),
            HelmReleaseTarget("keycloak-dify-agentgateway-oidc", "auth-keycloak"),
            HelmReleaseTarget("frontend-dify-shared", "frontend-dify"),
        ),
        (
            "statefulset/frontend-dify-redis-stateful-set",
            "deployment/frontend-dify-plugin-daemon-deployment",
            "deployment/frontend-dify-sandbox-deployment",
            "deployment/frontend-dify-agent-backend-deployment",
            "deployment/frontend-dify-agent-sandbox-deployment",
            "deployment/frontend-dify-agent-proxy-deployment",
            "deployment/frontend-dify-api-deployment",
            "deployment/frontend-dify-worker-deployment",
            "deployment/frontend-dify-beat-deployment",
        ),
    ),
    "knowledge-center": AddonCatalog(
        "forgejo",
        "forgejo",
        FORGEJO_SECRET_STORE,
        FORGEJO_EXTERNAL_SECRETS,
        HelmReleaseTarget("forgejo-postgres", "infra-postgres-operations"),
        (
            HelmReleaseTarget("keycloak-forgejo-oidc", "auth-keycloak"),
            HelmReleaseTarget("forgejo", "forgejo"),
        ),
        ("deployment/forgejo",),
    ),
}


def addon_catalogs(names: tuple[str, ...]) -> tuple[AddonCatalog, ...]:
    """Resolve only compiled, reviewed addon entries."""
    if len(set(names)) != len(names) or any(name not in ADDONS for name in names):
        raise ValueError("Unknown or duplicate addon selection")
    return tuple(ADDONS[name] for name in names)


BOOTSTRAP_SECRET_STORES: tuple[SecretStoreTarget, ...] = (
    SecretStoreTarget("auth-keycloak-openbao-secret-store", "auth-keycloak"),
    SecretStoreTarget(
        "auth-keycloak-api-key-bridge-openbao-secret-store", "auth-keycloak-api-key-bridge"
    ),
    SecretStoreTarget("frontend-studio-openbao-secret-store", "frontend-studio"),
    SecretStoreTarget("infra-agentgateway-openbao-secret-store", "infra-agentgateway"),
    SecretStoreTarget("infra-cert-manager-openbao-secret-store", "infra-cert-manager"),
    SecretStoreTarget("infra-postgres-auth-openbao-secret-store", "infra-postgres-auth"),
    SecretStoreTarget(
        "infra-postgres-operations-openbao-secret-store", "infra-postgres-operations"
    ),
    SecretStoreTarget("monitor-fluent-bit-openbao-secret-store", "monitor-fluent-bit"),
    SecretStoreTarget(
        "monitor-kube-prometheus-stack-openbao-secret-store", "monitor-kube-prometheus-stack"
    ),
    SecretStoreTarget("monitor-langfuse-openbao-secret-store", "monitor-langfuse"),
    SecretStoreTarget("monitor-opensearch-openbao-secret-store", "monitor-opensearch"),
    SecretStoreTarget("monitor-pii-engine-openbao-secret-store", "monitor-pii-engine"),
)

BOOTSTRAP_HELM_RELEASES: tuple[HelmReleaseTarget, ...] = (
    HelmReleaseTarget("cert-manager-issuers", "infra-cert-manager", 1800),
    HelmReleaseTarget("postgres-auth", "infra-postgres-auth", 1800),
    HelmReleaseTarget("postgres-operations", "infra-postgres-operations", 1800),
    HelmReleaseTarget("kube-prometheus-stack", "monitor-kube-prometheus-stack", 1800),
    HelmReleaseTarget("opensearch", "monitor-opensearch", 1800),
    HelmReleaseTarget("pii-engine", "monitor-pii-engine", 1800),
)

PROVIDER_REFRESH_TARGETS: tuple[ProviderRefreshTarget, ...] = (
    ProviderRefreshTarget(
        "docling/external",
        ("inferenceToken",),
        ExternalSecretTarget("docling-inference", "docling", "docling-inference"),
    ),
    ProviderRefreshTarget(
        "monitor-agentgateway-extproc/external",
        ("apiKey",),
        MONITOR_AGENTGATEWAY_EXTPROC_IMAGE_INSPECTION_EXTERNAL_SECRET,
    ),
    ProviderRefreshTarget(
        "infra-agentgateway/external",
        ("openrouterApiKey", "deepseekApiKey", "braveApiKey"),
        INFRA_AGENTGATEWAY_EXTERNAL_SECRET,
        (HelmReleaseTarget("agentgateway", "infra-agentgateway"),),
    ),
    ProviderRefreshTarget(
        "infra-cert-manager/external",
        ("accessKeyId", "secretAccessKey"),
        CERT_MANAGER_ISSUERS_EXTERNAL_SECRET,
        (HelmReleaseTarget("cert-manager-issuers", "infra-cert-manager"),),
    ),
    ProviderRefreshTarget(
        "auth-keycloak/external",
        ("smtpUsername", "smtpPassword"),
        AUTH_KEYCLOAK_SMTP_EXTERNAL_SECRET,
        (HelmReleaseTarget("keycloak", "auth-keycloak"),),
    ),
    ProviderRefreshTarget(
        "monitor-kube-prometheus-stack/external",
        ("smtpUsername", "smtpPassword"),
        MONITOR_KUBE_PROMETHEUS_STACK_SMTP_EXTERNAL_SECRET,
        (HelmReleaseTarget("kube-prometheus-stack", "monitor-kube-prometheus-stack"),),
    ),
    ProviderRefreshTarget(
        "monitor-opensearch/external",
        ("smtpUsername", "smtpPassword"),
        MONITOR_OPENSEARCH_EXTERNAL_SECRET,
    ),
    ProviderRefreshTarget(
        "auth-keycloak/external",
        ACTIVE_DIRECTORY_FIELDS,
        AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,
        (AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE,),
    ),
    ProviderRefreshTarget(
        "frontend-librechat/external",
        ("sttApiKey",),
        ExternalSecretTarget(
            "frontend-librechat-stt-secret", "frontend-librechat", "frontend-librechat-stt-secret"
        ),
    ),
    ProviderRefreshTarget(
        "frontend-librechat/external",
        ("ttsApiKey",),
        ExternalSecretTarget(
            "frontend-librechat-tts-secret", "frontend-librechat", "frontend-librechat-tts-secret"
        ),
    ),
)


def namespace_policy(namespace: str) -> str:
    """Return the exact namespace-scoped External Secrets read policy."""
    if namespace == "contextforge":
        # Native OAuth records must never be readable through the ESO role.
        return """path "secret/data/contextforge/internal" {
  capabilities = ["read"]
}

path "secret/metadata/contextforge/internal" {
  capabilities = ["read"]
}

path "secret/data/contextforge/provider-apps" {
  capabilities = ["read"]
}

path "secret/metadata/contextforge/provider-apps" {
  capabilities = ["read"]
}
"""
    if namespace not in (
        *ROLE_NAMESPACES,
        *(addon.namespace for addon in ADDONS.values()),
        "wireguard",
        *DOCLING_NAMESPACES,
    ):
        raise ValueError("Namespace is not present in the reconciliation catalog")
    policy = f"""path "secret/data/{namespace}/*" {{
  capabilities = ["read"]
}}

path "secret/metadata/{namespace}" {{
  capabilities = ["read", "list"]
}}

path "secret/metadata/{namespace}/*" {{
  capabilities = ["read", "list"]
}}
"""
    if namespace == "monitor-agentgateway-extproc":
        # The Docling namespace role must never inherit access to debug content credentials.
        policy += (
            f'\npath "secret/data/{REJECTION_DEBUG_PATH}" {{ capabilities = ["deny"] }}\n'
            f'path "secret/metadata/{REJECTION_DEBUG_PATH}" {{ capabilities = ["deny"] }}\n'
        )
    return policy


def secret_operator_policy(managed_paths: tuple[str, ...]) -> str:
    """Restrict routine provider updates to catalog-approved exact records."""
    blocks = []
    for path in sorted(set(managed_paths)):
        capabilities = (
            '["create", "read", "update"]'
            if path
            in (
                "frontend-librechat/external",
                "docling/external",
                "monitor-agentgateway-extproc/external",
                CONTEXTFORGE_PROVIDER_APPS_PATH,
            )
            else '["read", "update"]'
        )
        blocks.append(
            f"""path "secret/data/{path}" {{
  capabilities = {capabilities}
}}

path "secret/metadata/{path}" {{
  capabilities = ["read"]
}}"""
        )
    return "\n\n".join(blocks) + "\n"


def contextforge_oauth_policy() -> str:
    """Limit the native v1.0.11 backend to OAuth KV v2 data and hard deletion."""
    return """path "secret/data/contextforge/oauth/*" {
  capabilities = ["create", "read", "update", "delete"]
}

path "secret/metadata/contextforge/oauth/*" {
  capabilities = ["delete"]
}
"""
