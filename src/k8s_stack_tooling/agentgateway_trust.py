"""Publish public inspection roots with optimistic concurrency and a replica barrier.

This command has no Secret API operations. Certificates come only from explicit,
fingerprint-pinned public ConfigMaps. The target ConfigMap is the complete archive;
there is deliberately no automatic pruning or separate publication database.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import Encoding
from k8s_stack_gateway_catalog.document import Digest as Digest
from k8s_stack_gateway_catalog.document import Document as Document
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_BYTES = 1024 * 1024
MAX_VERSIONS = 64
MAX_ROOTS = 16
MAX_REPLICAS = 64
DATA_KEY = "snapshots.json"
FLOOR_ENV = "STUDIO_GATEWAY_TRUST_REQUIRED_VERSION"
OWNER_LABEL = "app.kubernetes.io/managed-by"
OWNER = "agentgateway-trust-publisher"
REQUEST_TIMEOUT = (5, 15)
Name = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[a-z0-9][a-z0-9.-]*$")]


class PublicationError(ValueError):
    """An actionable failure whose message contains no input certificate or API body."""


class RootSource(Document):
    config_map: Name
    key: str = Field(default="ca.crt", pattern=r"^[A-Za-z0-9._-]+$", max_length=253)
    fingerprint_sha256: Digest
    state: Literal["active", "transitional"]


class Inventory(Document):
    schema_version: Literal[1]
    roots: list[RootSource] = Field(min_length=1, max_length=MAX_ROOTS)

    @model_validator(mode="after")
    def unique_roots(self) -> Self:
        if len({root.fingerprint_sha256 for root in self.roots}) != len(self.roots):
            raise PublicationError("inventory contains duplicate fingerprints")
        return self


class PublicCertificate(Document):
    fingerprint_sha256: Digest
    pem: str = Field(max_length=65536)
    purpose: Literal["inspection-root"]
    state: Literal["active", "transitional"]


class Snapshot(Document):
    version: Digest
    published_at: str
    bundle_sha256: Digest
    certificates: list[PublicCertificate] = Field(min_length=1, max_length=MAX_ROOTS)

    def bundle(self) -> bytes:
        return b"".join(
            parse_root(cert.pem).public_bytes(Encoding.PEM) for cert in self.certificates
        )


class Publication(Document):
    schema_version: Literal[1] = 1
    current_version: Digest
    versions: list[Snapshot] = Field(min_length=1, max_length=MAX_VERSIONS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTGATEWAY_TRUST_", extra="forbid")

    source_namespace: Name = "infra-cert-manager"
    target_namespace: Name = "frontend-studio"
    inventory_config_map: Name = "agentgateway-inspection-trust-inventory-config-map"
    target_config_map: Name = "agentgateway-inspection-trust-config-map"
    deployment: Name = "frontend-studio-api-deployment"
    service: Name = "frontend-studio-api-service"
    pod_port: int = Field(default=4010, ge=1, le=65535)


def parse_root(pem: str) -> x509.Certificate:
    """Accept exactly one self-signed signing CA; historical validity is separate."""
    if not re.fullmatch(
        r"\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+"
        r"-----END CERTIFICATE-----\s*",
        pem,
    ):
        raise PublicationError("expected exactly one public PEM certificate")
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
        if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise PublicationError("certificate is not a CA root")
        if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
            raise PublicationError("root certificate cannot sign certificates")
        if not cert.subject or cert.subject != cert.issuer:
            raise PublicationError("certificate is not a self-issued root")
        cert.verify_directly_issued_by(cert)
        return cert
    except (
        ValueError,
        InvalidSignature,
        UnsupportedAlgorithm,
        x509.ExtensionNotFound,
        x509.DuplicateExtension,
    ) as exc:
        raise PublicationError("invalid self-signed CA root") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PublicationError("duplicate JSON object key")
        result[key] = value
    return result


def decode_document(raw: str, model: type[Inventory] | type[Publication]) -> Any:
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise PublicationError("document exceeds 1 MiB")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            raise PublicationError("schema_version must be integer 1")
        return model.model_validate(value)
    except (ValueError, RecursionError) as exc:
        # Pydantic and JSON exceptions can embed complete input; never log them.
        raise PublicationError("invalid public trust document") from exc


def validate_publication(publication: Publication) -> dict[str, bytes]:
    """Validate the entire archive without rejecting expired historical roots."""
    versions: set[str] = set()
    roots: dict[str, bytes] = {}
    for snapshot in publication.versions:
        if snapshot.version in versions:
            raise PublicationError("duplicate bundle version")
        versions.add(snapshot.version)
        try:
            timestamp = datetime.fromisoformat(snapshot.published_at.replace("Z", "+00:00"))
            if timestamp.utcoffset() is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise PublicationError("invalid publication timestamp") from exc
        seen: set[str] = set()
        for entry in snapshot.certificates:
            cert = parse_root(entry.pem)
            der = cert.public_bytes(Encoding.DER)
            fingerprint = hashlib.sha256(der).hexdigest()
            if fingerprint != entry.fingerprint_sha256 or fingerprint in seen:
                raise PublicationError("invalid or duplicate certificate fingerprint")
            seen.add(fingerprint)
            if fingerprint in roots and roots[fingerprint] != der:
                raise PublicationError("fingerprint reused for different certificate bytes")
            roots[fingerprint] = der
        digest = hashlib.sha256(snapshot.bundle()).hexdigest()
        if snapshot.version != digest or snapshot.bundle_sha256 != digest:
            raise PublicationError("bundle version or digest mismatch")
    if publication.current_version not in versions:
        raise PublicationError("current publication is missing")
    if len(publication.model_dump_json().encode()) + len(DATA_KEY) > MAX_BYTES:
        raise PublicationError("publication exceeds 1 MiB")
    return roots


def candidate_snapshot(
    inventory: Inventory, read_source: Callable[[RootSource], str], now: datetime
) -> Snapshot:
    certificates = []
    for source in inventory.roots:
        cert = parse_root(read_source(source))
        fingerprint = hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()
        if fingerprint != source.fingerprint_sha256:
            raise PublicationError("public root does not match the reviewed fingerprint")
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise PublicationError("selected root is not currently valid")
        certificates.append(
            PublicCertificate(
                fingerprint_sha256=fingerprint,
                pem=cert.public_bytes(Encoding.PEM).decode("ascii"),
                purpose="inspection-root",
                state=source.state,
            )
        )
    bundle = "".join(cert.pem for cert in certificates).encode("ascii")
    version = hashlib.sha256(bundle).hexdigest()
    return Snapshot(
        version=version,
        bundle_sha256=version,
        published_at=now.isoformat().replace("+00:00", "Z"),
        certificates=certificates,
    )


def stage_snapshot(existing: Publication | None, candidate: Snapshot) -> Publication:
    if existing is None:
        result = Publication(current_version=candidate.version, versions=[candidate])
    else:
        result = existing.model_copy(deep=True)
        for snapshot in result.versions:
            if snapshot.version == candidate.version:
                # States are uncached manifest metadata, not immutable asset bytes.
                snapshot.certificates = candidate.certificates
                break
        else:
            if len(result.versions) >= MAX_VERSIONS:
                raise PublicationError(
                    "archive exceeds 64 versions; explicit retirement is required"
                )
            result.versions.append(candidate)
    validate_publication(result)
    return result


class KubernetesPublisher:
    def __init__(self, settings: Settings, api_client: client.ApiClient) -> None:
        self.settings = settings
        self.core = client.CoreV1Api(api_client)
        self.apps = client.AppsV1Api(api_client)
        self.discovery = client.DiscoveryV1Api(api_client)

    def inventory(self) -> tuple[Inventory, str]:
        cm = self.core.read_namespaced_config_map(
            self.settings.inventory_config_map,
            self.settings.target_namespace,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return decode_document((cm.data or {}).get("inventory.json", ""), Inventory), (
            cm.metadata.resource_version
        )

    def source(self, source: RootSource) -> str:
        cm = self.core.read_namespaced_config_map(
            source.config_map, self.settings.source_namespace, _request_timeout=REQUEST_TIMEOUT
        )
        value = (cm.data or {}).get(source.key, "")
        if len(value.encode()) > 65536:
            raise PublicationError("public root source exceeds certificate size limit")
        return value

    def current(self) -> tuple[Publication | None, Any]:
        try:
            cm = self.core.read_namespaced_config_map(
                self.settings.target_config_map,
                self.settings.target_namespace,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status == 404:
                return None, None
            raise
        if (cm.metadata.labels or {}).get(OWNER_LABEL) != OWNER:
            raise PublicationError("refusing to overwrite an output owned by another publisher")
        publication = decode_document((cm.data or {}).get(DATA_KEY, ""), Publication)
        validate_publication(publication)
        return publication, cm

    def write(self, publication: Publication, previous: Any) -> None:
        metadata: dict[str, Any] = {
            "name": self.settings.target_config_map,
            "namespace": self.settings.target_namespace,
            "labels": {OWNER_LABEL: OWNER},
        }
        data = {DATA_KEY: publication.model_dump_json()}
        if previous is not None:
            metadata.update(
                resourceVersion=previous.metadata.resource_version,
                uid=previous.metadata.uid,
                labels={**(previous.metadata.labels or {}), OWNER_LABEL: OWNER},
                annotations=previous.metadata.annotations or {},
                finalizers=previous.metadata.finalizers or [],
            )
            data = {**(previous.data or {}), **data}
            if previous.binary_data or previous.immutable or previous.metadata.owner_references:
                raise PublicationError(
                    "output must be a mutable, independently owned public ConfigMap"
                )
        body = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": data}
        if len(json.dumps(body).encode()) > MAX_BYTES:
            raise PublicationError("output ConfigMap exceeds 1 MiB")
        if previous is None:
            self.core.create_namespaced_config_map(
                self.settings.target_namespace, body, _request_timeout=REQUEST_TIMEOUT
            )
        else:
            self.core.replace_namespaced_config_map(
                self.settings.target_config_map,
                self.settings.target_namespace,
                body,
                _request_timeout=REQUEST_TIMEOUT,
            )

    def _has_floor(self, spec: Any, required_version: str) -> bool:
        containers = [
            container
            for container in spec.containers
            if any(port.container_port == self.settings.pod_port for port in container.ports or [])
        ]
        if len(containers) != 1:
            return False
        values = [
            (env.value, env.value_from)
            for container in containers
            for env in (container.env or [])
            if env.name == FLOOR_ENV
        ]
        return values == [(required_version, None)]

    @staticmethod
    def _items(response: Any) -> list[Any]:
        if response.metadata._continue:
            raise PublicationError("replica discovery exceeded the bounded list size")
        return response.items

    def replicas(self, required_version: str) -> tuple[str, int, tuple[tuple[str, str], ...]]:
        """Require a settled Deployment and account for every serving Service endpoint."""
        ns = self.settings.target_namespace
        deployment = self.apps.read_namespaced_deployment(
            self.settings.deployment, ns, _request_timeout=REQUEST_TIMEOUT
        )
        count = deployment.spec.replicas
        status = deployment.status
        if (
            not count
            or count > MAX_REPLICAS
            or status.observed_generation != deployment.metadata.generation
            or any(
                value != count
                for value in (
                    status.replicas,
                    status.updated_replicas,
                    status.ready_replicas,
                    status.available_replicas,
                )
            )
            or not self._has_floor(deployment.spec.template.spec, required_version)
        ):
            raise PublicationError(
                "Studio deployment has not converged on the required trust version"
            )
        service = self.core.read_namespaced_service(
            self.settings.service, ns, _request_timeout=REQUEST_TIMEOUT
        )
        selector = service.spec.selector or {}
        if not selector or any(
            deployment.spec.template.metadata.labels.get(key) != value
            for key, value in selector.items()
        ):
            raise PublicationError("Studio Service does not select the expected deployment")
        label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        pods = self._items(
            self.core.list_namespaced_pod(
                ns, label_selector=label_selector, limit=256, _request_timeout=REQUEST_TIMEOUT
            )
        )
        replica_sets = self._items(
            self.apps.list_namespaced_replica_set(
                ns, label_selector=label_selector, limit=256, _request_timeout=REQUEST_TIMEOUT
            )
        )
        owned_sets = {
            rs.metadata.uid
            for rs in replica_sets
            if any(
                owner.controller and owner.uid == deployment.metadata.uid
                for owner in (rs.metadata.owner_references or [])
            )
        }
        ready: dict[str, str] = {}
        addresses: dict[str, set[str]] = {}
        for pod in pods:
            if not any(
                c.type == "Ready" and c.status == "True" for c in pod.status.conditions or []
            ):
                continue
            if (
                pod.metadata.deletion_timestamp
                or not self._has_floor(pod.spec, required_version)
                or not any(
                    owner.controller and owner.uid in owned_sets
                    for owner in (pod.metadata.owner_references or [])
                )
            ):
                raise PublicationError("a serving Studio replica is stale, terminating or unowned")
            ready[pod.metadata.uid] = str(ipaddress.ip_address(pod.status.pod_ip))
            addresses[pod.metadata.uid] = {ready[pod.metadata.uid]} | {
                str(ipaddress.ip_address(address.ip)) for address in pod.status.pod_ips or []
            }
        slices = self._items(
            self.discovery.list_namespaced_endpoint_slice(
                ns,
                label_selector=f"kubernetes.io/service-name={self.settings.service}",
                limit=256,
                _request_timeout=REQUEST_TIMEOUT,
            )
        )
        endpoints: set[str] = set()
        for endpoint_slice in slices:
            for endpoint in endpoint_slice.endpoints:
                conditions = endpoint.conditions
                # An unset ready condition is treated as ready by EndpointSlice consumers.
                if conditions and conditions.ready is False and conditions.serving is not True:
                    continue
                ref = endpoint.target_ref
                if (
                    (conditions and conditions.terminating)
                    or ref is None
                    or ref.kind != "Pod"
                    or ref.namespace != ns
                    or ref.uid not in ready
                    or not endpoint.addresses
                    or not set(endpoint.addresses) <= addresses[ref.uid]
                    or not any(
                        port.port == self.settings.pod_port for port in endpoint_slice.ports or []
                    )
                ):
                    raise PublicationError("Studio has an unverified or draining serving endpoint")
                endpoints.add(ref.uid)
        if len(ready) != count or endpoints != set(ready):
            raise PublicationError("Studio serving endpoints do not cover all expected replicas")
        return deployment.metadata.uid, deployment.metadata.generation, tuple(sorted(ready.items()))

    def barrier(self, publication: Publication) -> None:
        # History is append-only. The newest added version proves a mounted document
        # includes all earlier assets, even when promoting a previously retained bundle.
        required = publication.versions[-1].version
        before = self.replicas(required)
        assets = {
            f"bundles/{snapshot.version}.pem": snapshot.bundle()
            for snapshot in publication.versions
        }
        assets.update(
            {f"certificates/{fp}.cer": der for fp, der in validate_publication(publication).items()}
        )
        with requests.Session() as session:
            session.trust_env = False
            for _, address in before[2]:
                host = f"[{address}]" if ":" in address else address
                for path, expected in assets.items():
                    url = f"http://{host}:{self.settings.pod_port}/api/gateway/trust/{path}"
                    with session.get(
                        url, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True
                    ) as response:
                        if response.status_code != 200:
                            raise PublicationError(
                                "a Studio replica does not serve every staged asset"
                            )
                        received = bytearray()
                        for chunk in response.iter_content(8192):
                            received.extend(chunk)
                            if len(received) > len(expected):
                                raise PublicationError(
                                    "a Studio replica returned an unexpected asset"
                                )
                        if bytes(received) != expected:
                            raise PublicationError("a Studio replica returned an unexpected asset")
        if self.replicas(required) != before:
            raise PublicationError(
                "Studio replica membership changed during publication verification"
            )


def publish(
    api: KubernetesPublisher,
    operation: Literal["stage", "promote"],
    *,
    initialize: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Retry only resourceVersion conflicts, rebuilding and rechecking every attempt."""
    for _ in range(3):
        inventory, inventory_revision = api.inventory()
        candidate = candidate_snapshot(inventory, api.source, now or datetime.now(UTC))
        existing, cm = api.current()
        if existing is None and (operation != "stage" or not initialize):
            raise PublicationError("initial publication requires stage --initialize")
        if (
            initialize
            and existing is not None
            and (len(existing.versions) != 1 or existing.current_version != candidate.version)
        ):
            raise PublicationError("initialization cannot replace an existing publication history")
        if operation == "stage":
            desired = stage_snapshot(existing, candidate)
        else:
            assert existing is not None
            staged = next((s for s in existing.versions if s.version == candidate.version), None)
            if staged is None or staged.certificates != candidate.certificates:
                raise PublicationError("requested publication must be staged before promotion")
            desired = existing.model_copy(deep=True)
            api.barrier(desired)
            desired.current_version = candidate.version
        if api.inventory()[1] != inventory_revision:
            raise PublicationError("reviewed inventory changed during publication")
        # Replica verification can take time; do not promote a root that expired
        # after the initial inventory read.
        selected_at = now or datetime.now(UTC)
        for entry in candidate.certificates:
            cert = parse_root(entry.pem)
            if not cert.not_valid_before_utc <= selected_at < cert.not_valid_after_utc:
                raise PublicationError("selected root is not currently valid")
        changed = existing != desired
        try:
            if changed:
                api.write(desired, cm)
            # Even an idempotent promote must not report success using a stale read.
            observed, _ = api.current()
            if observed != desired:
                raise ApiException(status=409)
            return {
                "operation": operation,
                "version": candidate.version,
                "current_version": desired.current_version,
                "required_version": desired.versions[-1].version,
                "retained_versions": len(desired.versions),
                "changed": changed,
            }
        except ApiException as exc:
            if exc.status != 409:
                raise
    raise PublicationError("publication conflicted repeatedly; retry the operation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "promote"))
    parser.add_argument(
        "--initialize", action="store_true", help="create the first complete snapshot"
    )
    args = parser.parse_args()
    if args.initialize and args.operation != "stage":
        parser.error("--initialize is only valid with stage")
    try:
        settings = Settings()
        config.load_incluster_config()
        with client.ApiClient() as api_client:
            result = publish(
                KubernetesPublisher(settings, api_client),
                args.operation,
                initialize=args.initialize,
            )
        print(json.dumps(result, sort_keys=True))
    except PublicationError as exc:
        print(f"Trust publication failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        # Exceptions from clients/settings may contain tokens, URLs or response bodies.
        print(f"Trust publication failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
