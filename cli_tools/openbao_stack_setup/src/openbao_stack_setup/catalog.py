"""Declare the reviewed OpenBao and Kubernetes reconciliation catalog."""

from __future__ import annotations

from dataclasses import dataclass

RECONCILIATION_STATE_PATH = "stack-setup/reconciliation-state"
ACTIVE_DIRECTORY_FIELDS = ("activeDirectoryBindDn", "activeDirectoryBindCredential")

ROLE_NAMESPACES: tuple[str, ...] = (
    "auth-keycloak",
    "auth-keycloak-api-key-bridge",
    "frontend-dify",
    "frontend-librechat",
    "frontend-studio",
    "infra-agentgateway",
    "infra-cert-manager",
    "infra-postgres-auth",
    "infra-postgres-operations",
    "librechat-code-interpreter",
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
    ("auth-keycloak/external", "monitor-kube-prometheus-stack/external"),
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
    helm_release: HelmReleaseTarget


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
INFRA_AGENTGATEWAY_EXTERNAL_SECRET = ExternalSecretTarget(
    "infra-agentgateway-secrets", "infra-agentgateway", "infra-agentgateway-secrets"
)
CERT_MANAGER_ISSUERS_EXTERNAL_SECRET = ExternalSecretTarget(
    "cert-manager-issuers-values", "infra-cert-manager", "cert-manager-issuers-values"
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
        "frontend-dify-openbao-secret", "frontend-dify", "frontend-dify-openbao-secret"
    ),
    ExternalSecretTarget(
        "frontend-dify-runtime-secret", "frontend-dify", "frontend-dify-runtime-secret"
    ),
    ExternalSecretTarget(
        "frontend-librechat-runtime-secret",
        "frontend-librechat",
        "frontend-librechat-runtime-secret",
    ),
    ExternalSecretTarget(
        "frontend-librechat-code-interpreter-runtime-secret",
        "librechat-code-interpreter",
        "frontend-librechat-code-interpreter-runtime-secret",
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
    ExternalSecretTarget(
        "monitor-opensearch-secret", "monitor-opensearch", "monitor-opensearch-secret"
    ),
    ExternalSecretTarget(
        "monitor-pii-engine-secrets", "monitor-pii-engine", "monitor-pii-engine-secrets"
    ),
)

BOOTSTRAP_SECRET_STORES: tuple[SecretStoreTarget, ...] = (
    SecretStoreTarget("auth-keycloak-openbao-secret-store", "auth-keycloak"),
    SecretStoreTarget(
        "auth-keycloak-api-key-bridge-openbao-secret-store", "auth-keycloak-api-key-bridge"
    ),
    SecretStoreTarget("frontend-dify-openbao-secret-store", "frontend-dify"),
    SecretStoreTarget("frontend-librechat-openbao-secret-store", "frontend-librechat"),
    SecretStoreTarget(
        "librechat-code-interpreter-openbao-secret-store", "librechat-code-interpreter"
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
        "infra-agentgateway/external",
        ("openrouterApiKey", "deepseekApiKey", "braveApiKey"),
        INFRA_AGENTGATEWAY_EXTERNAL_SECRET,
        HelmReleaseTarget("agentgateway", "infra-agentgateway"),
    ),
    ProviderRefreshTarget(
        "infra-cert-manager/external",
        ("accessKeyId", "secretAccessKey"),
        CERT_MANAGER_ISSUERS_EXTERNAL_SECRET,
        HelmReleaseTarget("cert-manager-issuers", "infra-cert-manager"),
    ),
    ProviderRefreshTarget(
        "auth-keycloak/external",
        ("smtpUsername", "smtpPassword"),
        AUTH_KEYCLOAK_SMTP_EXTERNAL_SECRET,
        HelmReleaseTarget("keycloak", "auth-keycloak"),
    ),
    ProviderRefreshTarget(
        "monitor-kube-prometheus-stack/external",
        ("smtpUsername", "smtpPassword"),
        MONITOR_KUBE_PROMETHEUS_STACK_SMTP_EXTERNAL_SECRET,
        HelmReleaseTarget("kube-prometheus-stack", "monitor-kube-prometheus-stack"),
    ),
    ProviderRefreshTarget(
        "auth-keycloak/external",
        ACTIVE_DIRECTORY_FIELDS,
        AUTH_KEYCLOAK_ACTIVE_DIRECTORY_EXTERNAL_SECRET,
        AUTH_KEYCLOAK_ACTIVE_DIRECTORY_HELM_RELEASE,
    ),
)


def namespace_policy(namespace: str) -> str:
    """Return the exact namespace-scoped External Secrets read policy."""
    if namespace not in ROLE_NAMESPACES:
        raise ValueError("Namespace is not present in the reconciliation catalog")
    return f"""path "secret/data/{namespace}/*" {{
  capabilities = ["read"]
}}

path "secret/metadata/{namespace}" {{
  capabilities = ["read", "list"]
}}

path "secret/metadata/{namespace}/*" {{
  capabilities = ["read", "list"]
}}
"""


def secret_operator_policy(managed_paths: tuple[str, ...]) -> str:
    """Restrict routine provider updates to catalog-approved exact records."""
    blocks = []
    for path in sorted(set(managed_paths)):
        blocks.append(
            f"""path "secret/data/{path}" {{
  capabilities = ["read", "update"]
}}

path "secret/metadata/{path}" {{
  capabilities = ["read"]
}}"""
        )
    return "\n\n".join(blocks) + "\n"
