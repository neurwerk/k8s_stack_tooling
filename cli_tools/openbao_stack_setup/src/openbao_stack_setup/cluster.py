"""Kubernetes operations used by the local OpenBao setup operator."""

from __future__ import annotations

import base64
import ipaddress
import time
from dataclasses import dataclass
from typing import cast

import kubernetes.client
import kubernetes.config
import yaml
from kubernetes.client.exceptions import ApiException


class ClusterError(RuntimeError):
    """Raised for a redacted Kubernetes API failure or unsafe cluster state."""


@dataclass(frozen=True)
class StackIdentity:
    """Non-secret identity stored in the tenant Flux root."""

    client: str
    cluster_id: str
    namespace_uid: str


@dataclass(frozen=True)
class KubernetesApiEndpoint:
    """Client-owned fixed K3s API endpoint."""

    address: str
    port: int


def _monitoring_email_enabled(values: object) -> bool:
    """Read the default-enabled monitoring email alerting choice."""
    if values is None:
        return True
    if not isinstance(values, dict):
        raise ClusterError("Monitoring product values contain an invalid email alerting contract")
    current = values
    for key in ("monitorKubePrometheusStack", "alerting", "email"):
        if key not in current:
            return True
        nested = current[key]
        if not isinstance(nested, dict):
            raise ClusterError(
                "Monitoring product values contain an invalid email alerting contract"
            )
        current = nested
    enabled = current.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ClusterError("Monitoring email alerting enabled value must be a boolean")
    return enabled


class Cluster:
    """Access one explicitly selected kubeconfig context."""

    def __init__(self, context: str) -> None:
        """Load one explicit kubeconfig context and create API clients."""
        try:
            kubernetes.config.load_kube_config(context=context)
        except kubernetes.config.ConfigException:
            raise ClusterError("Could not load the selected Kubernetes context") from None
        self.core = kubernetes.client.CoreV1Api()
        self.discovery = kubernetes.client.DiscoveryV1Api()
        self.custom = kubernetes.client.CustomObjectsApi()
        self.context = context

    def identity(self, client: str) -> StackIdentity:
        """Read and validate the cluster identity for the requested client."""
        try:
            config_map = self.core.read_namespaced_config_map(
                "neurwerk-stack-identity", "flux-system"
            )
            namespace = self.core.read_namespace("infra-openbao")
        except ApiException as exc:
            raise ClusterError(
                f"Required tenant identity resource is unavailable: HTTP {exc.status}"
            ) from None
        data = config_map.data or {}
        if (
            data.get("schemaVersion") != "1"
            or data.get("client") != client
            or not data.get("clusterId")
        ):
            raise ClusterError("Selected context does not match the requested client identity")
        if not namespace.metadata.uid:
            raise ClusterError("OpenBao namespace does not have a UID")
        return StackIdentity(client, data["clusterId"], namespace.metadata.uid)

    def seal_exists(self) -> bool:
        """Return whether the cluster already contains the static seal."""
        try:
            self.core.read_namespaced_secret("infra-openbao-static-seal-secret", "infra-openbao")
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise ClusterError(
                f"Could not inspect the static seal Secret: HTTP {exc.status}"
            ) from None
        else:
            return True

    def smtp_required(self) -> bool:
        """Return whether Keycloak or monitoring requires SMTP credentials."""
        keycloak_values = self._product_values(
            "keycloak-product-values", "auth-keycloak", "Keycloak"
        )
        if not isinstance(keycloak_values, dict):
            raise ClusterError("Keycloak product values contain an invalid SMTP contract")
        try:
            keycloak_enabled = keycloak_values["authKeycloak"]["smtp"]["enabled"]
        except (KeyError, TypeError):
            raise ClusterError("Keycloak product values contain an invalid SMTP contract") from None
        if not isinstance(keycloak_enabled, bool):
            raise ClusterError("Keycloak SMTP enabled value must be a boolean")
        monitoring_values = self._product_values(
            "kube-prometheus-stack-product-values",
            "monitor-kube-prometheus-stack",
            "Monitoring",
        )
        monitoring_enabled = _monitoring_email_enabled(monitoring_values)
        return keycloak_enabled or monitoring_enabled

    def active_directory_required(self) -> bool:
        """Return whether Keycloak requires Active Directory credentials."""
        keycloak_values = self._product_values(
            "keycloak-product-values", "auth-keycloak", "Keycloak"
        )
        if not isinstance(keycloak_values, dict):
            raise ClusterError(
                "Keycloak product values contain an invalid Active Directory contract"
            )
        auth_keycloak = keycloak_values.get("authKeycloak")
        if not isinstance(auth_keycloak, dict):
            raise ClusterError(
                "Keycloak product values contain an invalid Active Directory contract"
            )
        active_directory = auth_keycloak.get("activeDirectory")
        if active_directory is None:
            return False
        if not isinstance(active_directory, dict):
            raise ClusterError(
                "Keycloak product values contain an invalid Active Directory contract"
            )
        enabled = active_directory.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ClusterError("Keycloak Active Directory enabled value must be a boolean")
        return enabled

    def _product_values(self, name: str, namespace: str, product: str) -> object:
        """Read one namespace-local product values document."""
        try:
            config_map = self.core.read_namespaced_config_map(name, namespace)
        except ApiException as exc:
            raise ClusterError(
                f"{product} product values are unavailable: HTTP {exc.status}"
            ) from None
        raw_values = (config_map.data or {}).get("values.yaml")
        if not raw_values:
            raise ClusterError(f"{product} product values do not contain values.yaml")
        try:
            return yaml.safe_load(raw_values)
        except yaml.YAMLError:
            raise ClusterError(f"{product} product values contain invalid values.yaml") from None

    def require_openbao_release(self) -> None:
        """Ensure Flux has accepted the OpenBao release before bootstrap starts."""
        try:
            self.custom.get_namespaced_custom_object(
                "helm.toolkit.fluxcd.io", "v2", "infra-openbao", "helmreleases", "openbao"
            )
        except ApiException as exc:
            raise ClusterError(f"OpenBao HelmRelease is unavailable: HTTP {exc.status}") from None

    def require_bootstrap_prerequisites(self) -> bool:
        """Require the foundation resources and client values needed by bootstrap."""
        self.require_openbao_release()
        for name, namespace in (
            ("external-secrets", "infra-external-secrets"),
            ("rook-ceph", "infra-rook-ceph"),
            ("trust-manager", "infra-trust-manager"),
        ):
            self._require_ready_resource(
                "helm.toolkit.fluxcd.io",
                "v2",
                namespace,
                "helmreleases",
                "HelmRelease",
                name,
            )
        self._require_ready_resource(
            "cert-manager.io",
            "v1",
            "infra-openbao",
            "certificates",
            "Certificate",
            "infra-openbao-server-certificate",
        )
        return self.smtp_required()

    def _require_ready_resource(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        kind: str,
        name: str,
    ) -> None:
        """Require one custom resource to expose a true Ready condition."""
        try:
            resource = self.custom.get_namespaced_custom_object(
                group, version, namespace, plural, name
            )
        except ApiException as exc:
            raise ClusterError(
                f"Required {kind} {namespace}/{name} is unavailable: HTTP {exc.status}"
            ) from None
        if not self._resource_ready(resource):
            raise ClusterError(f"Required {kind} {namespace}/{name} is not Ready")

    def _configured_kubernetes_api_endpoint(self) -> KubernetesApiEndpoint:
        """Read the client-owned fixed K3s API endpoint."""
        try:
            config_map = self.core.read_namespaced_config_map(
                "openbao-product-values", "infra-openbao"
            )
        except ApiException as exc:
            raise ClusterError(
                f"OpenBao product values are unavailable: HTTP {exc.status}"
            ) from None
        raw_values = (config_map.data or {}).get("values.yaml")
        if not raw_values:
            raise ClusterError("OpenBao product values do not contain values.yaml")
        try:
            values = yaml.safe_load(raw_values)
            configured = values["infraOpenbaoWrapper"]["kubernetesApi"]["endpoint"]
            address = configured["address"]
            port = configured["port"]
        except (KeyError, TypeError, yaml.YAMLError):
            raise ClusterError("OpenBao product values contain an invalid API endpoint") from None
        if not isinstance(address, str):
            raise ClusterError("OpenBao Kubernetes API endpoint address must be an IPv4 address")
        try:
            parsed_address = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            raise ClusterError(
                "OpenBao Kubernetes API endpoint address must be an IPv4 address"
            ) from None
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ClusterError("OpenBao Kubernetes API endpoint port is invalid")
        return KubernetesApiEndpoint(str(parsed_address), port)

    def _ready_kubernetes_api_state(self) -> tuple[set[str], set[tuple[str, int]]]:
        """Return ready Node InternalIPs and Kubernetes API EndpointSlice targets."""
        try:
            nodes = self.core.list_node()
            endpoint_slices = self.discovery.list_namespaced_endpoint_slice(
                "default", label_selector="kubernetes.io/service-name=kubernetes"
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not validate the Kubernetes API endpoint: HTTP {exc.status}"
            ) from None

        ready_node_addresses: set[str] = set()
        for node in nodes.items:
            conditions = node.status.conditions or []
            if not any(
                condition.type == "Ready" and condition.status == "True" for condition in conditions
            ):
                continue
            ready_node_addresses.update(
                item.address
                for item in node.status.addresses or []
                if item.type == "InternalIP" and item.address
            )
        ready_api_endpoints: set[tuple[str, int]] = set()
        for endpoint_slice in endpoint_slices.items:
            ports = [item.port for item in endpoint_slice.ports or [] if item.port is not None]
            for item in endpoint_slice.endpoints or []:
                if item.conditions is not None and item.conditions.ready is False:
                    continue
                ready_api_endpoints.update(
                    (address, port) for address in item.addresses or [] for port in ports
                )
        return ready_node_addresses, ready_api_endpoints

    def validate_kubernetes_api_endpoint(self) -> KubernetesApiEndpoint:
        """Require client values, the ready Node, and API EndpointSlice to agree."""
        endpoint = self._configured_kubernetes_api_endpoint()
        ready_node_addresses, ready_api_endpoints = self._ready_kubernetes_api_state()
        if endpoint.address not in ready_node_addresses:
            raise ClusterError(
                "Configured Kubernetes API endpoint does not match a Ready Node InternalIP"
            )
        if ready_api_endpoints != {(endpoint.address, endpoint.port)}:
            raise ClusterError(
                "Configured Kubernetes API endpoint does not match the ready EndpointSlice"
            )
        return endpoint

    def create_seal(self, key: bytes, key_id: str) -> None:
        """Create the immutable static-seal Secret exactly once."""
        if self.seal_exists():
            raise ClusterError("Static seal Secret already exists")
        body = kubernetes.client.V1Secret(
            metadata=kubernetes.client.V1ObjectMeta(
                name="infra-openbao-static-seal-secret",
                namespace="infra-openbao",
                labels={"app.kubernetes.io/part-of": "infra-openbao"},
                annotations={"secrets.neurwerk.com/static-seal-key-id": key_id},
            ),
            type="Opaque",
            immutable=True,
            data={"key": base64.b64encode(key).decode("ascii")},
        )
        try:
            self.core.create_namespaced_secret("infra-openbao", body)
        except ApiException as exc:
            raise ClusterError(
                f"Could not create the static seal Secret: HTTP {exc.status}"
            ) from None

    def token_request(self, service_account: str, *, ttl_seconds: int = 600) -> str:
        """Create and validate a short-lived service-account token."""
        body = kubernetes.client.AuthenticationV1TokenRequest(
            spec=kubernetes.client.V1TokenRequestSpec(
                audiences=["openbao"], expiration_seconds=ttl_seconds
            )
        )
        try:
            result = self.core.create_namespaced_service_account_token(
                service_account, "infra-openbao", body
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not create a short-lived operator token: HTTP {exc.status}"
            ) from None
        token = result.status.token if result.status else None
        if not isinstance(token, str) or not token:
            raise ClusterError("Kubernetes did not return a service account token")
        return token

    def force_reconcile(self, name: str, namespace: str = "flux-system") -> str:
        """Request an immediate forced HelmRelease reconciliation."""
        token = str(time.time_ns())
        body = {
            "metadata": {
                "annotations": {
                    "reconcile.fluxcd.io/requestedAt": token,
                    "reconcile.fluxcd.io/forceAt": token,
                }
            }
        }
        try:
            self.custom.patch_namespaced_custom_object(
                "helm.toolkit.fluxcd.io", "v2", namespace, "helmreleases", name, body
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not request Flux reconciliation for {name}: HTTP {exc.status}"
            ) from None
        return token

    def reconcile_kustomization(
        self,
        name: str,
        namespace: str = "flux-system",
        timeout_seconds: int = 3900,
    ) -> None:
        """Request and wait for one Flux Kustomization reconciliation."""
        token = str(time.time_ns())
        body = {"metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": token}}}
        try:
            self.custom.patch_namespaced_custom_object(
                "kustomize.toolkit.fluxcd.io", "v1", namespace, "kustomizations", name, body
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not request Flux reconciliation for Kustomization {name}: HTTP {exc.status}"
            ) from None

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                resource = self.custom.get_namespaced_custom_object(
                    "kustomize.toolkit.fluxcd.io", "v1", namespace, "kustomizations", name
                )
            except ApiException as exc:
                raise ClusterError(
                    f"Could not inspect Kustomization {namespace}/{name}: HTTP {exc.status}"
                ) from None
            status = resource.get("status", {})
            if status.get("lastHandledReconcileAt") != token:
                time.sleep(5)
                continue
            conditions = status.get("conditions", [])
            if any(
                item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
            ):
                return
            time.sleep(5)
        raise ClusterError(f"Timed out waiting for Kustomization {namespace}/{name}")

    def wait_helm_release(
        self,
        name: str,
        namespace: str,
        timeout_seconds: int = 900,
        force_token: str | None = None,
    ) -> None:
        """Wait for a HelmRelease to become ready or fail explicitly."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                resource = self.custom.get_namespaced_custom_object(
                    "helm.toolkit.fluxcd.io", "v2", namespace, "helmreleases", name
                )
            except ApiException as exc:
                raise ClusterError(
                    f"Could not inspect HelmRelease {namespace}/{name}: HTTP {exc.status}"
                ) from None
            status = resource.get("status", {})
            if force_token is not None and status.get("lastHandledForceAt") != force_token:
                time.sleep(5)
                continue
            conditions = status.get("conditions", [])
            if any(
                item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
            ):
                return
            reconciling = any(
                item.get("type") == "Reconciling" and item.get("status") == "True"
                for item in conditions
            )
            stalled = any(
                item.get("type") == "Stalled" and item.get("status") == "True"
                for item in conditions
            )
            if stalled and not reconciling:
                raise ClusterError(f"HelmRelease {namespace}/{name} failed")
            time.sleep(5)
        raise ClusterError(f"Timed out waiting for HelmRelease {namespace}/{name}")

    def wait_openbao_endpoint(self, timeout_seconds: int = 300) -> None:
        """Wait until the OpenBao Service has a ready HTTPS endpoint."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                pod = self.core.read_namespaced_pod("infra-openbao-0", "infra-openbao")
                endpoints = self.core.read_namespaced_endpoints("infra-openbao", "infra-openbao")
            except ApiException as exc:
                if exc.status != 404:
                    raise ClusterError(
                        f"Could not inspect OpenBao Service endpoints: HTTP {exc.status}"
                    ) from None
            else:
                for status in getattr(pod.status, "init_container_statuses", None) or []:
                    if status.name != "kubernetes-api-connectivity":
                        continue
                    states = (status.state, status.last_state)
                    failed = any(
                        state is not None
                        and state.terminated is not None
                        and state.terminated.exit_code != 0
                        for state in states
                    )
                    crash_looping = (
                        status.state is not None
                        and status.state.waiting is not None
                        and status.state.waiting.reason == "CrashLoopBackOff"
                    )
                    if failed or crash_looping:
                        raise ClusterError(
                            "OpenBao cannot reach the Kubernetes API through its NetworkPolicy"
                        )
                pod_ready = any(
                    condition.type == "Ready" and condition.status == "True"
                    for condition in pod.status.conditions or []
                )
                for subset in endpoints.subsets or []:
                    has_address = bool(subset.addresses)
                    has_https = any(port.port == 8200 for port in subset.ports or [])
                    if pod_ready and has_address and has_https:
                        return
            time.sleep(3)
        raise ClusterError("Timed out waiting for the OpenBao Service endpoint")

    def wait_secret(self, name: str, namespace: str, timeout_seconds: int = 300) -> None:
        """Wait for target Secret metadata without requesting its data."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self.core.api_client.call_api(
                    "/api/v1/namespaces/{namespace}/secrets/{name}",
                    "GET",
                    path_params={"namespace": namespace, "name": name},
                    query_params=[],
                    header_params={
                        "Accept": ("application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1")
                    },
                    response_types_map={200: "object"},
                    auth_settings=["BearerToken"],
                    _return_http_data_only=True,
                    collection_formats={},
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise ClusterError(
                        f"Could not inspect Secret {namespace}/{name}: HTTP {exc.status}"
                    ) from None
            else:
                return
            time.sleep(3)
        raise ClusterError(f"Timed out waiting for Secret {namespace}/{name}")

    def ensure_secret_store_ready(
        self, name: str, namespace: str, timeout_seconds: int = 300
    ) -> None:
        """Wake a stale SecretStore and wait until ESO reports it ready."""
        resource = self._secret_store(name, namespace)
        if self._resource_ready(resource):
            return
        token = str(time.time_ns())
        body = {"metadata": {"annotations": {"force-sync": token}}}
        try:
            self.custom.patch_namespaced_custom_object(
                "external-secrets.io", "v1", namespace, "secretstores", name, body
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not reconcile SecretStore {namespace}/{name}: HTTP {exc.status}"
            ) from None
        self.wait_secret_store_ready(name, namespace, timeout_seconds)

    def wait_secret_store_ready(
        self, name: str, namespace: str, timeout_seconds: int = 300
    ) -> None:
        """Wait until ESO reports a SecretStore ready."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._resource_ready(self._secret_store(name, namespace)):
                return
            time.sleep(3)
        raise ClusterError(f"Timed out waiting for SecretStore {namespace}/{name}")

    def _secret_store(self, name: str, namespace: str) -> dict[str, object]:
        """Read one SecretStore while redacting Kubernetes API details."""
        try:
            return cast(
                dict[str, object],
                self.custom.get_namespaced_custom_object(
                    "external-secrets.io", "v1", namespace, "secretstores", name
                ),
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not inspect SecretStore {namespace}/{name}: HTTP {exc.status}"
            ) from None

    @staticmethod
    def _resource_ready(resource: dict[str, object]) -> bool:
        """Return whether a custom resource has a true Ready condition."""
        status = resource.get("status")
        if not isinstance(status, dict):
            return False
        conditions = status.get("conditions")
        if not isinstance(conditions, list):
            return False
        return any(
            isinstance(item, dict) and item.get("type") == "Ready" and item.get("status") == "True"
            for item in conditions
        )

    def external_secret_refresh_time(self, name: str, namespace: str) -> str | None:
        """Read ESO's last successful refresh marker without reading Secret data."""
        try:
            resource = self.custom.get_namespaced_custom_object(
                "external-secrets.io", "v1", namespace, "externalsecrets", name
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not inspect ExternalSecret {namespace}/{name}: HTTP {exc.status}"
            ) from None
        value = resource.get("status", {}).get("refreshTime")
        return value if isinstance(value, str) else None

    def force_external_secret_refresh(
        self,
        name: str,
        namespace: str,
        target_secret: str,
        timeout_seconds: int = 300,
    ) -> None:
        """Force one ExternalSecret refresh and wait for its target Secret metadata."""
        previous = self.external_secret_refresh_time(name, namespace)
        token = str(time.time_ns())
        body = {"metadata": {"annotations": {"force-sync": token}}}
        try:
            self.custom.patch_namespaced_custom_object(
                "external-secrets.io", "v1", namespace, "externalsecrets", name, body
            )
        except ApiException as exc:
            raise ClusterError(
                f"Could not refresh ExternalSecret {namespace}/{name}: HTTP {exc.status}"
            ) from None
        self.wait_external_secret_refresh(name, namespace, previous, timeout_seconds)
        self.wait_secret(target_secret, namespace, timeout_seconds)

    def wait_external_secret_refresh(
        self, name: str, namespace: str, previous: str | None, timeout_seconds: int = 300
    ) -> None:
        """Wait until ESO records a new ready refresh after a force-sync request."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                resource = self.custom.get_namespaced_custom_object(
                    "external-secrets.io", "v1", namespace, "externalsecrets", name
                )
            except ApiException as exc:
                raise ClusterError(
                    f"Could not inspect ExternalSecret {namespace}/{name}: HTTP {exc.status}"
                ) from None
            status = resource.get("status", {})
            refreshed = status.get("refreshTime")
            conditions = status.get("conditions", [])
            ready = any(
                item.get("type") == "Ready" and item.get("status") == "True" for item in conditions
            )
            if isinstance(refreshed, str) and refreshed != previous and ready:
                return
            time.sleep(3)
        raise ClusterError(f"Timed out waiting for ExternalSecret {namespace}/{name} refresh")
