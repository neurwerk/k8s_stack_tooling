"""Observe gateway serving replicas once and publish an expiring public snapshot."""

from __future__ import annotations

import ipaddress
import json
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from OpenSSL import SSL, crypto
from pydantic import Field, model_validator

from k8s_stack_tooling.agentgateway_trust import (
    Digest,
    Document,
    Name,
    Publication,
    decode_document,
    parse_root,
    validate_publication,
)

NAMESPACE = "infra-agentgateway"
GATEWAY = "infra-agentgateway-gateway"
TARGET_NAMESPACE = "frontend-studio"
CATALOG = "agentgateway-catalog-config-map"
STATUS = "agentgateway-status-config-map"
TRUST = "agentgateway-inspection-trust-config-map"
INVENTORY = "agentgateway-observation-config-map"
MAX_BYTES = 8 * 1024 * 1024
MAX_REPLICAS = 64
MARKER = "neurwerk_policy_revision"


class ObservationError(ValueError):
    """Observation is incomplete; client responses must never appear in diagnostics."""


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def timeout(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise ObservationError("observation deadline exceeded")
        return min(5.0, remaining)


class Route(Document):
    """The two direct route shapes rendered by Base, not a general routing graph."""

    name: Name
    path: str = Field(min_length=1, max_length=2048, pattern=r"^/")
    models: dict[Annotated[str, Field(min_length=1, max_length=256)], list[Name]] = Field(
        default_factory=dict, max_length=64
    )
    mcp_backend: Name | None = None

    @model_validator(mode="after")
    def one_backend_kind(self) -> Route:
        if bool(self.models) == bool(self.mcp_backend) or any(
            not targets or len(targets) > 3 for targets in self.models.values()
        ):
            raise ObservationError("route must select bounded model targets or one MCP backend")
        return self


class Listener(Document):
    name: Name
    port: int = Field(ge=1, le=65535)
    routes: list[Route] = Field(min_length=1, max_length=128)
    hostname: Name | None = None


class Endpoint(Document):
    id: Name
    listeners: list[Listener] = Field(min_length=1, max_length=8)


class Proxy(Document):
    hostname: Name
    port: int = Field(ge=1, le=65535)
    listener: Name
    inspection_hostname: Name


class Inventory(Document):
    policy_revision: Digest
    endpoints: list[Endpoint] = Field(max_length=32)
    proxy: Proxy | None = None

    @model_validator(mode="after")
    def consistent_targets(self) -> Inventory:
        if len({endpoint.id for endpoint in self.endpoints}) != len(self.endpoints):
            raise ObservationError("duplicate endpoint identity")
        if self.proxy and not any(
            endpoint.id == "forward-proxy"
            and listener.name == self.proxy.listener
            and listener.hostname == self.proxy.inspection_hostname
            for endpoint in self.endpoints
            for listener in endpoint.listeners
        ):
            raise ObservationError("sampled listener differs from the expected proxy route")
        return self


@dataclass(frozen=True, order=True)
class Replica:
    uid: str
    name: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class Membership:
    replicas: tuple[Replica, ...]
    # Include service identity, ports and serving/draining conditions in the barrier.
    signature: str


def _document(raw: bytes, limit: int = MAX_BYTES) -> dict[str, Any]:
    if len(raw) > limit:
        raise ObservationError("observation response exceeds its size limit")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ObservationError("duplicate document field")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ObservationError("expected an object")
    return value


class KubernetesObserver:
    def __init__(self, api_client: client.ApiClient, deadline: Deadline) -> None:
        self.core = client.CoreV1Api(api_client)
        self.discovery = client.DiscoveryV1Api(api_client)
        self.deadline = deadline

    def read(self, name: str, namespace: str = TARGET_NAMESPACE) -> Any:
        return self.core.read_namespaced_config_map(
            name, namespace, _request_timeout=self.deadline.timeout()
        )

    def membership(self, inventory: Inventory) -> Membership:
        replicas: dict[str, Replica] = {}
        signature: list[Any] = []
        services = {
            GATEWAY: {15000}
            | {
                listener.port
                for endpoint in inventory.endpoints
                for listener in endpoint.listeners
                if listener.name == "api-bindings"
            }
        }
        if inventory.proxy:
            services["infra-agentgateway-forward-proxy"] = {inventory.proxy.port}
        for name, required_ports in services.items():
            service = self.core.read_namespaced_service(
                name, NAMESPACE, _request_timeout=self.deadline.timeout()
            )
            if not service.metadata.uid or not service.spec.selector:
                raise ObservationError("gateway Service has no identity or selector")
            mappings = []
            for port in sorted(required_ports):
                selected = [p for p in service.spec.ports or [] if p.port == port]
                if len(selected) != 1 or not selected[0].name or selected[0].protocol != "TCP":
                    raise ObservationError("required gateway Service port is missing or ambiguous")
                selected_port = selected[0]
                target = (
                    selected_port.target_port if selected_port.target_port is not None else port
                )
                if not isinstance(target, str) and target != port:
                    raise ObservationError("Service target differs from the expected listener")
                mappings.append((selected_port.name, port, target, selected_port.protocol))
            slices = self.discovery.list_namespaced_endpoint_slice(
                NAMESPACE,
                label_selector=f"kubernetes.io/service-name={name}",
                limit=128,
                _request_timeout=self.deadline.timeout(),
            )
            if slices.metadata._continue:
                raise ObservationError("serving endpoint discovery exceeded its bound")
            service_members: list[Any] = []
            for item in slices.items:
                if not any(
                    owner.kind == "Service" and owner.uid == service.metadata.uid
                    for owner in item.metadata.owner_references or []
                ):
                    raise ObservationError("unowned gateway EndpointSlice")
                ports = sorted((p.name or "", p.port, p.protocol) for p in item.ports or [])
                for endpoint in item.endpoints:
                    conditions = endpoint.conditions
                    if conditions and conditions.ready is False and conditions.serving is not True:
                        continue
                    ref = endpoint.target_ref
                    if (
                        ref is None
                        or ref.kind != "Pod"
                        or ref.namespace != NAMESPACE
                        or not ref.uid
                        or not ref.name
                        or not endpoint.addresses
                    ):
                        raise ObservationError("serving endpoint has no Pod identity")
                    addresses = tuple(
                        sorted(str(ipaddress.ip_address(a)) for a in endpoint.addresses)
                    )
                    pod = self.core.read_namespaced_pod(
                        ref.name, NAMESPACE, _request_timeout=self.deadline.timeout()
                    )
                    pod_addresses = {p.ip for p in pod.status.pod_ips or []}
                    if (
                        pod.metadata.uid != ref.uid
                        or not set(addresses) <= pod_addresses
                        or any(
                            (pod.metadata.labels or {}).get(k) != v
                            for k, v in service.spec.selector.items()
                        )
                    ):
                        raise ObservationError("serving Pod identity or address changed")
                    for port_name, expected_port, target, protocol in mappings:
                        if isinstance(target, str):
                            targets = [
                                p.container_port
                                for container in pod.spec.containers
                                for p in container.ports or []
                                if p.name == target and p.protocol == protocol
                            ]
                            if targets != [expected_port]:
                                raise ObservationError("named Service target differs from listener")
                        if [p for p in ports if p[0] == port_name] != [
                            (port_name, expected_port, protocol)
                        ]:
                            raise ObservationError(
                                "serving EndpointSlice port differs from Service"
                            )
                    previous = replicas.get(ref.uid)
                    if previous and previous.name != ref.name:
                        raise ObservationError("ambiguous serving Pod identity")
                    replicas[ref.uid] = Replica(
                        ref.uid,
                        ref.name,
                        tuple(sorted(set(addresses) | set(previous.addresses if previous else ()))),
                    )
                    if len(replicas) > MAX_REPLICAS:
                        raise ObservationError("too many serving replicas")
                    service_members.append(
                        (
                            ref.uid,
                            addresses,
                            ports,
                            (conditions.ready, conditions.serving, conditions.terminating)
                            if conditions
                            else None,
                        )
                    )
            # A missing proxy Service membership must not inherit API readiness.
            if not service_members:
                raise ObservationError("gateway Service has no serving endpoints")
            signature.append(
                (
                    name,
                    service.metadata.uid,
                    sorted(service.spec.selector.items()),
                    mappings,
                    sorted(service_members, key=lambda x: (x[0], x[1])),
                )
            )
        return Membership(tuple(sorted(replicas.values())), json.dumps(signature, sort_keys=True))

    def publish(self, snapshot: dict[str, Any], previous: Any) -> None:
        if (previous.metadata.labels or {}).get(
            "app.kubernetes.io/name"
        ) != "agentgateway-observation":
            raise ObservationError("status ConfigMap has an unexpected owner")
        payload = json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 1024 * 1024:
            raise ObservationError("status document exceeds its size limit")
        self.core.patch_namespaced_config_map(
            STATUS,
            TARGET_NAMESPACE,
            {
                "metadata": {"resourceVersion": previous.metadata.resource_version},
                "data": {"status.json": payload},
            },
            _request_timeout=self.deadline.timeout(),
        )


def _http(
    session: requests.Session, address: str, port: int, path: str, deadline: Deadline, limit: int
) -> tuple[int, bytes]:
    host = f"[{address}]" if ":" in address else address
    with session.get(
        f"http://{host}:{port}{path}",
        stream=True,
        allow_redirects=False,
        timeout=deadline.timeout(),
    ) as response:
        body = bytearray()
        for chunk in response.iter_content(16384):
            deadline.timeout()
            body.extend(chunk)
            if len(body) > limit:
                raise ObservationError("gateway response exceeds its size limit")
        return response.status_code, bytes(body)


def _registered_model(
    dump: dict[str, Any], listener: str, router: str, kind: str, model: dict[str, Any]
) -> bool:
    return any(
        item.get("listenerKey") == listener
        and item.get("routerKey") == router
        and item.get("kind", {}).get(kind) == model
        for item in dump.get("models", [])
    )


def _route_loaded(dump: dict[str, Any], listener: dict[str, Any], expected: Route) -> bool:
    routes = [
        route
        for route in (listener.get("routes") or {}).values()
        if route.get("name") == expected.name
        and route.get("namespace") == NAMESPACE
        and route.get("kind") == "HTTPRoute"
    ]
    if len(routes) != 1:
        return False
    route = routes[0]
    model_route = bool(expected.models)
    match: dict[str, Any] = {"path": {"pathPrefix" if model_route else "exact": expected.path}}
    if model_route:
        match["method"] = "POST"
    if route.get("matches") != [match] or route.get("hostnames", []) != (
        [listener["hostname"]] if listener.get("hostname") else []
    ):
        return False
    key = (
        f"/llm:router:httproute:{NAMESPACE}:{expected.name}:models"
        if model_route
        else f"{NAMESPACE}/{expected.mcp_backend}"
    )
    if route.get("backends") != [{"backend": key, "weight": 1}]:
        return False
    kind = "llmRouter" if model_route else "mcp"
    backends = [
        item["backend"][kind]
        for item in dump.get("backends", [])
        if kind in item.get("backend", {})
    ]
    selected = [
        backend
        for backend in backends
        if backend.get("namespace") == ("" if model_route else NAMESPACE)
        and backend.get("name") == (key[1:] if model_route else expected.mcp_backend)
    ]
    if len(selected) != 1:
        return False
    target = selected[0].get("target", {})
    if not model_route:
        return bool(target.get("targets"))
    if route.get("ruleName") != "models":
        return False
    for name, expected_targets in expected.models.items():
        models = [model for model in target.get("virtualModels", []) if model.get("name") == name]
        if len(models) != 1 or not _registered_model(
            dump, listener["key"], key, "virtual", models[0]
        ):
            return False
        branches = models[0].get("routing", {}).get("conditional", [])
        if [branch.get("model") for branch in branches] != expected_targets or any(
            branch.get("invalid") or not branch.get("when") for branch in branches
        ):
            return False
        for name in expected_targets:
            concrete = [model for model in target.get("models", []) if model.get("name") == name]
            if (
                len(concrete) != 1
                or concrete[0].get("visibility") != "internal"
                or not _registered_model(dump, listener["key"], key, "concrete", concrete[0])
            ):
                return False
            reference = f"{NAMESPACE}/{name}/backend.{NAMESPACE}.{expected.name}.models"
            if concrete[0].get("backend") != {"backend": reference, "weight": 1}:
                return False
            # v1.6.0 omits backend map keys from its dump. A provider's namespace/name
            # cannot prove this scoped reference, even if only one same-name entry
            # remains: it could belong to a sibling API/proxy route. Stay unknown
            # for these aliases until native evidence can establish the association.
            return False
    return True


def runtime_revision(dump: dict[str, Any], endpoints: list[Endpoint]) -> str | None:
    """Read only attached v1.6 policy markers and routes, never arbitrary dump text."""
    revisions: set[str] = set()
    expected_listeners: dict[str, Listener] = {}
    for endpoint in endpoints:
        for listener in endpoint.listeners:
            previous = expected_listeners.get(listener.name)
            if previous and (
                previous.port != listener.port or previous.hostname != listener.hostname
            ):
                return None
            routes = {route.name: route for route in previous.routes} if previous else {}
            for route in listener.routes:
                if route.name in routes and routes[route.name] != route:
                    return None
                routes[route.name] = route
            expected_listeners[listener.name] = listener.model_copy(
                update={"routes": list(routes.values())}
            )
    for expected in expected_listeners.values():
        listeners = [
            listener
            for bind in dump.get("binds", [])
            if int(str(bind.get("address", "")).rsplit(":", 1)[-1]) == expected.port
            for listener in bind.get("listeners", {}).values()
            if listener.get("gatewayName") == GATEWAY
            and listener.get("gatewayNamespace") == NAMESPACE
            and listener.get("listenerName") == expected.name
        ]
        if len(listeners) != 1:
            return None
        expected_protocol = (
            {"HTTPS": {"certificateSource": "DynamicCa"}} if expected.hostname else "HTTP"
        )
        if listeners[0].get("protocol") != expected_protocol or (
            expected.hostname and listeners[0].get("hostname") != expected.hostname
        ):
            return None
        if not all(_route_loaded(dump, listeners[0], route) for route in expected.routes):
            return None
        policies = [
            p
            for p in dump.get("policies", [])
            if p.get("name")
            == {
                "kind": "AgentgatewayPolicy",
                "namespace": NAMESPACE,
                "name": f"ai-admission-{expected.name}",
            }
            and p.get("target")
            == {
                "gateway": {
                    "gatewayName": GATEWAY,
                    "gatewayNamespace": NAMESPACE,
                    "listenerName": expected.name,
                }
            }
            and "transformation" in p.get("policy", {}).get("traffic", {})
        ]
        if len(policies) != 1:
            return None
        traffic = policies[0]["policy"]["traffic"]
        if traffic.get("phase") != "gateway":
            return None
        branches = traffic["transformation"]
        if isinstance(branches, dict):
            branches = [branches]
        if not isinstance(branches, list) or not branches:
            return None
        for branch in branches:
            transformation = branch.get("pol", branch)
            # Native TransformerConfig serializes metadata as key/expression pairs.
            metadata = (transformation.get("request") or {}).get("metadata", [])
            expressions = [value for key, value in metadata if key == MARKER]
            if len(expressions) != 1:
                return None
            expression = expressions[0]
            if not isinstance(expression, str):
                return None
            revision = json.loads(expression)
            if (
                not isinstance(revision, str)
                or len(revision) != 64
                or any(c not in "0123456789abcdef" for c in revision)
            ):
                return None
            revisions.add(revision)
    return next(iter(revisions)) if len(revisions) == 1 else None


def probe_signer(
    address: str,
    proxy: Proxy,
    roots: list[x509.Certificate],
    deadline: Deadline,
    outer_ca: str | None = None,
) -> tuple[str, str]:
    """Sample one inner TLS chain, closing before any decrypted HTTP request."""
    context = SSL.Context(SSL.TLS_CLIENT_METHOD)
    context.set_verify(SSL.VERIFY_PEER, lambda _c, _x, _e, _d, ok: bool(ok))
    store = context.get_cert_store()
    if store is None:
        raise ObservationError("TLS trust store is unavailable")
    for root in roots:
        store.add_cert(crypto.X509.from_cryptography(root))
    inner = SSL.Connection(context, None)
    inner.set_connect_state()
    inner.set_tlsext_host_name(proxy.inspection_hostname.encode("ascii"))
    outer_context = ssl.create_default_context(cafile=outer_ca)
    with socket.create_connection((address, proxy.port), timeout=deadline.timeout()) as raw:
        with outer_context.wrap_socket(raw, server_hostname=proxy.hostname) as outer:
            outer.settimeout(deadline.timeout())
            authority = f"{proxy.inspection_hostname}:443"
            outer.sendall(
                f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
            )
            headers = bytearray()
            while not headers.endswith(b"\r\n\r\n"):
                outer.settimeout(deadline.timeout())
                part = outer.recv(1)
                if not part or len(headers) >= 8192:
                    raise ObservationError("invalid CONNECT response")
                headers.extend(part)
            if headers.split(b"\r\n", 1)[0].split(b" ")[0:2] != [b"HTTP/1.1", b"200"]:
                raise ObservationError("CONNECT was not accepted")
            transferred = 0

            def flush() -> None:
                nonlocal transferred
                while True:
                    try:
                        data = inner.bio_read(16384)
                    except SSL.WantReadError:
                        return
                    transferred += len(data)
                    if transferred > 262144:
                        raise ObservationError("TLS handshake exceeded its size limit")
                    outer.settimeout(deadline.timeout())
                    outer.sendall(data)

            while True:
                deadline.timeout()
                try:
                    inner.do_handshake()
                    flush()
                    break
                except SSL.WantReadError:
                    flush()
                    outer.settimeout(deadline.timeout())
                    data = outer.recv(16384)
                    transferred += len(data)
                    if not data or transferred > 262144:
                        raise ObservationError("incomplete TLS handshake")
                    inner.bio_write(data)
                except SSL.WantWriteError:
                    flush()
            peer = inner.get_peer_cert_chain() or []
            if not 2 <= len(peer) <= 8:
                raise ObservationError("inspection chain has no signer")
            certificates = [cert.to_cryptography() for cert in peer]
            verified = (
                PolicyBuilder()
                .store(Store(roots))
                .build_server_verifier(x509.DNSName(proxy.inspection_hostname))
                .verify(certificates[0], certificates[1:])
            )
            if (
                len(verified) < 3
                or not verified[1]
                .extensions.get_extension_for_class(x509.BasicConstraints)
                .value.ca
            ):
                raise ObservationError("inspection chain has no intermediate signer")
            signer = verified[1]
            return signer.fingerprint(hashes.SHA256()).hex(), signer.not_valid_after_utc.isoformat()


def current_roots(raw: str) -> list[x509.Certificate]:
    publication = decode_document(raw, Publication)
    validate_publication(publication)
    current = next(s for s in publication.versions if s.version == publication.current_version)
    return [parse_root(cert.pem) for cert in current.certificates]


def observe(api: KubernetesObserver, deadline: Deadline) -> dict[str, Any]:
    """Publish unknown on incomplete evidence; never refresh a previous good result."""
    inventory_cm = api.read(INVENTORY, NAMESPACE)
    inventory = Inventory.model_validate(
        _document((inventory_cm.data or {}).get("observation.json", "").encode(), 1024 * 1024)
    )
    previous = api.read(STATUS)
    catalog_cm = api.read(CATALOG)
    catalog = _document((catalog_cm.data or {}).get("catalog.json", "").encode(), 1024 * 1024)
    started = datetime.now(UTC)
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "catalog_revision": catalog["catalog_revision"],
        "applied_policy_revision": None,
        "observed_at": started.isoformat(),
        "valid_until": (started + timedelta(minutes=5)).isoformat(),
        "endpoints": [{"id": e["id"], "state": "unknown"} for e in catalog["endpoints"]],
        "profiles": [],
        "signers": [],
    }
    try:
        if inventory.policy_revision != catalog["policy_revision"] or {
            e.id for e in inventory.endpoints
        } != {e["id"] for e in catalog["endpoints"]}:
            raise ObservationError("observation inventory differs from catalog")
        before = api.membership(inventory)
        if not before.replicas:
            raise ObservationError("no serving replicas")
        roots: list[x509.Certificate] = []
        trust_version = None
        if inventory.proxy:
            try:
                trust_cm = api.read(TRUST)
                trust_version = trust_cm.metadata.resource_version
                roots = current_roots((trust_cm.data or {}).get("snapshots.json", ""))
            except (ApiException, ValueError):
                # Missing public trust leaves each signer unknown, independently of API readiness.
                pass
        revisions: list[str | None] = []
        readiness: list[bool] = []
        with requests.Session() as session:
            session.trust_env = False
            for replica in before.replicas:
                # Membership covers every serving address; one address/listener is sampled per Pod.
                address = replica.addresses[0]
                try:
                    ready, _ = _http(session, address, 15021, "/healthz/ready", deadline, 4096)
                    code, raw = _http(session, address, 15000, "/config_dump", deadline, MAX_BYTES)
                    revision = (
                        runtime_revision(_document(raw), inventory.endpoints)
                        if code == 200
                        else None
                    )
                    del raw
                    revisions.append(revision)
                    readiness.append(ready == 200)
                except (
                    ObservationError,
                    requests.RequestException,
                    ValueError,
                    KeyError,
                    TypeError,
                    AttributeError,
                ):
                    revisions.append(None)
                    readiness.append(False)
                if inventory.proxy:
                    observation: dict[str, Any] = {
                        "pod_uid": replica.uid,
                        "pod_name": replica.name,
                        "listener": inventory.proxy.listener,
                        "state": "unknown",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "fingerprint_sha256": None,
                        "not_after": None,
                    }
                    try:
                        if not roots:
                            raise ObservationError("current public inspection trust is unavailable")
                        fingerprint, expiry = probe_signer(
                            address, inventory.proxy, roots, deadline
                        )
                        observation.update(
                            state="verified",
                            fingerprint_sha256=fingerprint,
                            not_after=expiry,
                            observed_at=datetime.now(UTC).isoformat(),
                        )
                    except (OSError, SSL.Error, ValueError, VerificationError):
                        pass
                    snapshot["signers"].append(observation)
        if api.membership(inventory) != before:
            raise ObservationError("serving membership changed during observation")
        if trust_version is not None and api.read(TRUST).metadata.resource_version != trust_version:
            raise ObservationError("public trust changed during observation")
        if None not in revisions and len(set(revisions)) == 1:
            snapshot["applied_policy_revision"] = revisions[0]
            if revisions[0] == inventory.policy_revision:
                for endpoint in snapshot["endpoints"]:
                    endpoint["state"] = "available" if all(readiness) else "unavailable"
                    if endpoint["id"] == "forward-proxy" and any(
                        s["state"] != "verified" for s in snapshot["signers"]
                    ):
                        endpoint["state"] = "unknown"
    except (ObservationError, ApiException, ValueError, KeyError, TypeError, AttributeError):
        snapshot["applied_policy_revision"] = None
        snapshot["signers"] = []
        for endpoint in snapshot["endpoints"]:
            endpoint["state"] = "unknown"
    if api.read(CATALOG).metadata.resource_version != catalog_cm.metadata.resource_version:
        raise ObservationError("catalog changed during observation")
    if (
        api.read(INVENTORY, NAMESPACE).metadata.resource_version
        != inventory_cm.metadata.resource_version
    ):
        raise ObservationError("observation inventory changed during observation")
    api.publish(snapshot, previous)
    return snapshot


def main() -> None:
    def expired(_signal: int, _frame: Any) -> None:
        raise ObservationError("run deadline exceeded")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        config.load_incluster_config()
        configuration = client.Configuration.get_default_copy()
        configuration.retries = 0
        with client.ApiClient(configuration) as api_client:
            observe(KubernetesObserver(api_client, Deadline(55)), Deadline(40))
        print("Gateway observation published")
    except Exception as exc:
        # Client errors can embed private configuration, headers, or API responses.
        print(f"Gateway observation failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    main()
