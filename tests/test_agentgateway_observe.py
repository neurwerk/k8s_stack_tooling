"""False-health boundaries and real credential-free TLS-in-CONNECT observations."""

import json
import socket
import ssl
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Thread
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import VerificationError
from OpenSSL import SSL

from k8s_stack_tooling import agentgateway_observe as observe
from k8s_stack_tooling import agentgateway_trust as trust

REVISION = "a" * 64
NS = SimpleNamespace


def catalog_route(endpoint_id, *, proxy=False):
    return {
        "id": endpoint_id,
        "url": "https://gateway.example.test/" + ("" if proxy else endpoint_id),
        "entry_mode": "forward_proxy" if proxy else "api",
        "caller_auth_modes": ["proxy_basic"] if proxy else ["oidc_bearer", "neurwerk_bearer"],
    }


def catalog_entry(entry_id, routes):
    return {
        "id": entry_id,
        "provider_id": None,
        "display_name": entry_id,
        "routes": routes,
        "destinations": [],
    }


def inventory():
    return observe.Inventory.model_validate(
        {
            "policy_revision": REVISION,
            "endpoints": [
                {
                    "id": "api-first",
                    "listeners": [
                        {
                            "name": "api-bindings",
                            "port": 8082,
                            "routes": [
                                {
                                    "name": "ai-first-api",
                                    "path": "/bindings/first",
                                    "models": {"first-model": ["first-internal"]},
                                }
                            ],
                        }
                    ],
                },
                {
                    "id": "api-second",
                    "listeners": [
                        {
                            "name": "api-bindings",
                            "port": 8082,
                            "routes": [
                                {
                                    "name": "ai-second-api",
                                    "path": "/bindings/second",
                                    "models": {"second-model": ["second-internal"]},
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    )


def native_dump(revision=REVISION):
    # v1.6.0 DumpBind/RouteSet/TargetedPolicy and conditional RequestPolicy serialization.
    dump = {
        "binds": [
            {
                "address": "[::]:8082",
                "listeners": {
                    "listener-key": {
                        "key": "listener-key",
                        "gatewayName": observe.GATEWAY,
                        "gatewayNamespace": observe.NAMESPACE,
                        "listenerName": "api-bindings",
                        "protocol": "HTTP",
                        "routes": {},
                    }
                },
            }
        ],
        "policies": [
            {
                "name": {
                    "kind": "AgentgatewayPolicy",
                    "namespace": observe.NAMESPACE,
                    "name": "ai-admission-api-bindings",
                },
                "target": {
                    "gateway": {
                        "gatewayName": observe.GATEWAY,
                        "gatewayNamespace": observe.NAMESPACE,
                        "listenerName": "api-bindings",
                    }
                },
                "policy": {
                    "traffic": {
                        "phase": "gateway",
                        "transformation": [
                            {
                                "condition": "true",
                                "pol": {
                                    "request": {
                                        "metadata": [[observe.MARKER, json.dumps(revision)]]
                                    }
                                },
                            },
                        ],
                    }
                },
            }
        ],
        "backends": [],
        "models": [],
    }
    for name in ("first", "second"):
        native_model_route(
            dump,
            dump["binds"][0]["listeners"]["listener-key"],
            f"ai-{name}-api",
            f"/bindings/{name}",
            name,
        )
    return dump


def native_model_route(dump, listener, route_name, path, model):
    """Relevant v1.6.0 DumpModel and live scoped LLMRouter fields (not desired CRDs)."""
    router = f"/llm:router:httproute:{observe.NAMESPACE}:{route_name}:models"
    concrete_name = f"{model}-internal"
    reference = (
        f"{observe.NAMESPACE}/{concrete_name}/backend.{observe.NAMESPACE}.{route_name}.models"
    )
    concrete = {
        "name": concrete_name,
        "created": 0,
        "visibility": "internal",
        "headerMatches": [],
        "backend": {"weight": 1, "backend": reference},
        "policies": {"llm": {}, "authorization": None},
        "backendPolicies": [],
    }
    virtual = {
        "name": f"{model}-model",
        "created": 0,
        "llmPolicy": {},
        "routing": {"conditional": [{"model": concrete_name, "when": "true"}]},
    }
    listener["routes"][route_name] = {
        "key": f"{observe.NAMESPACE}/{route_name}.0.0.{listener['listenerName']}",
        "name": route_name,
        "namespace": observe.NAMESPACE,
        "kind": "HTTPRoute",
        "ruleName": "models",
        "matches": [{"path": {"pathPrefix": path}, "method": "POST"}],
        "backends": [{"weight": 1, "backend": router}],
        "hostnames": [listener["hostname"]] if listener.get("hostname") else [],
    }
    dump["backends"].extend(
        [
            {
                "backend": {
                    "llmRouter": {
                        "name": router[1:],
                        "namespace": "",
                        "target": {"models": [concrete], "virtualModels": [virtual]},
                    }
                }
            },
            {
                "backend": {
                    "ai": {
                        "name": concrete_name,
                        "namespace": observe.NAMESPACE,
                        "target": {
                            "providers": [
                                {
                                    "active": {
                                        "default": {
                                            "endpoint": {
                                                "name": "default",
                                                "provider": {"openAI": {"model": None}},
                                            },
                                            "info": {},
                                            "capacity": 1,
                                        }
                                    },
                                    "rejected": {},
                                }
                            ]
                        },
                    }
                }
            },
        ]
    )
    for kind, item in (("concrete", concrete), ("virtual", virtual)):
        dump["models"].append(
            {
                "listenerKey": listener["key"],
                "routerKey": router,
                "key": f"{observe.NAMESPACE}/{item['name']}.{route_name}",
                "name": item["name"],
                "kind": {kind: deepcopy(item)},
            }
        )


@pytest.mark.parametrize(
    "failure",
    [
        "missing-marker",
        "orphan-marker",
        "wrong-policy",
        "phase",
        "missing-route",
        "orphan-route",
        "mixed-markers",
        "zero-listeners",
    ],
)
def test_matching_strings_do_not_prove_attached_runtime_configuration(failure):
    dump = native_dump()
    policy = dump["policies"][0]
    listener = dump["binds"][0]["listeners"]["listener-key"]
    branches = policy["policy"]["traffic"]["transformation"]
    if failure == "missing-marker":
        branches[0]["pol"]["request"]["metadata"] = []
        dump["irrelevant"] = REVISION
    elif failure == "orphan-marker":
        policy["target"]["gateway"]["listenerName"] = "other"
    elif failure == "wrong-policy":
        policy["name"]["name"] = "other"
    elif failure == "phase":
        policy["policy"]["traffic"]["phase"] = "route"
    elif failure == "missing-route":
        del listener["routes"]["ai-first-api"]
    elif failure == "orphan-route":
        dump["routes"] = listener.pop("routes")
    elif failure == "mixed-markers":
        other = deepcopy(branches[0])
        other["pol"]["request"]["metadata"] = [[observe.MARKER, json.dumps("b" * 64)]]
        branches.append(other)
    else:
        dump["binds"] = []
    assert observe.runtime_revision(dump, inventory().endpoints) is None


class MemoryObserver(observe.KubernetesObserver):
    def __init__(self):
        self.members = observe.Membership(
            (
                observe.Replica("old-uid", "old-pod", ("192.0.2.1",)),
                observe.Replica("new-uid", "new-pod", ("192.0.2.2",)),
            ),
            "stable",
        )
        self.calls = 0
        self.change = False
        self.inventory = inventory()
        self.catalog: dict[str, Any] = {
            "schema_version": 2,
            "catalog_revision": "catalog-1",
            "policy_revision": REVISION,
            "profiles": [],
            "self-hosted-api-providers": [
                catalog_entry("first", [catalog_route("api-first")]),
            ],
            "external-api-providers": [
                catalog_entry("second", [catalog_route("api-second")]),
            ],
            "external-subscription-providers": [],
            "mcp-servers": [],
        }
        self.written = None

    def read(self, name, namespace=observe.TARGET_NAMESPACE):
        return NS(
            metadata=NS(resource_version="1"),
            data={
                "catalog.json": json.dumps(self.catalog),
                "observation.json": self.inventory.model_dump_json(),
            },
        )

    def membership(self, inventory):
        self.calls += 1
        if self.change and self.calls > 1:
            return observe.Membership(self.members.replicas, "changed-address-or-serving-condition")
        return self.members

    def publish(self, snapshot, previous):
        self.written = snapshot


def test_observer_consumes_routes_from_every_group(monkeypatch):
    api = MemoryObserver()
    api.catalog["profiles"] = [
        {
            "id": "inspected",
            "revision": "profile-1",
            "limitations": [],
            "operations": [
                {
                    "id": "chat",
                    "support": "supported",
                    "restoration_scope": "request",
                    "limitations": [],
                    "verified_at": "2026-10-21T00:00:00Z",
                }
            ],
        }
    ]
    api.catalog["self-hosted-api-providers"][0]["destinations"] = [
        {
            "id": "first-model",
            "kind": "model",
            "display_name": "First model",
            "endpoint_ids": ["api-first"],
            "profile_ids": ["inspected"],
            "required_permissions": ["llm:invoke"],
            "credential_modes": [{"source": "none", "required_permissions": []}],
            "provider_id": None,
            "model": "first-model",
        }
    ]
    # Base publishes configuration status on provider owners, not runtime health.
    api.catalog["self-hosted-api-providers"][0]["global_credential_status"] = "disabled"
    api.catalog["external-api-providers"][0].update(
        provider_id="openai",
        global_credential_status="configured",
        destinations=[
            {
                "id": "second-model",
                "kind": "model",
                "display_name": "Second model",
                "endpoint_ids": ["api-second"],
                "profile_ids": [],
                "required_permissions": ["llm:invoke", "model:second-model:invoke"],
                "credential_modes": [
                    {"source": "company", "required_permissions": ["provider:openai:company-use"]}
                ],
                "provider_id": "openai",
            }
        ],
    )
    for group, endpoint_id in (
        ("external-subscription-providers", "api-subscription"),
        ("mcp-servers", "mcp-tools"),
    ):
        api.catalog[group] = [catalog_entry(endpoint_id, [catalog_route(endpoint_id)])]
        api.catalog[group][0]["global_credential_status"] = (
            "missing" if group == "external-subscription-providers" else None
        )
        api.inventory.endpoints.append(
            api.inventory.endpoints[0].model_copy(update={"id": endpoint_id})
        )
    monkeypatch.setattr(observe, "_route_loaded", lambda *_args: True)
    monkeypatch.setattr(
        observe,
        "_http",
        lambda _s, _a, port, *_args: (
            200,
            b"ready" if port == 15021 else json.dumps(native_dump()).encode(),
        ),
    )
    result = observe.observe(api, observe.Deadline(5))
    assert result is api.written
    assert result["schema_version"] == 1
    assert {row["id"]: row["state"] for row in result["endpoints"]} == {
        endpoint_id: "available"
        for endpoint_id in ("api-first", "api-second", "api-subscription", "mcp-tools")
    }


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "bool-version",
        "float-version",
        "extra-root",
        "extra-entry",
        "unknown-credential-status",
        "invalid-credential-status-type",
        "missing-group",
        "missing-provider",
        "duplicate-api",
        "duplicate-local-proxy",
        "conflicting-proxy",
        "foreign-endpoint",
        "foreign-profile",
        "wrong-provider",
        "duplicate-entry",
    ],
)
def test_invalid_catalog_stops_before_probing_or_publication(monkeypatch, fault):
    api = MemoryObserver()
    first = api.catalog["self-hosted-api-providers"][0]
    second = api.catalog["external-api-providers"][0]
    if fault == "legacy":
        api.catalog = {"schema_version": 1, "endpoints": [], "destinations": []}
    elif fault == "bool-version":
        api.catalog["schema_version"] = True
    elif fault == "float-version":
        api.catalog["schema_version"] = 2.0
    elif fault == "extra-root":
        api.catalog["endpoints"] = []
    elif fault == "extra-entry":
        first["unrelated_status"] = "configured"
    elif fault == "unknown-credential-status":
        first["global_credential_status"] = "ready"
    elif fault == "invalid-credential-status-type":
        first["global_credential_status"] = True
    elif fault == "missing-group":
        del api.catalog["mcp-servers"]
    elif fault == "missing-provider":
        del first["provider_id"]
    elif fault == "duplicate-api":
        second["routes"] = deepcopy(first["routes"])
    elif fault == "duplicate-local-proxy":
        first["routes"] = [catalog_route("forward-proxy", proxy=True)] * 2
    elif fault == "conflicting-proxy":
        first["routes"].append(catalog_route("forward-proxy", proxy=True))
        second["routes"].append(catalog_route("forward-proxy", proxy=True))
        second["routes"][-1]["url"] = "https://other.example.test"
    elif fault == "duplicate-entry":
        second["id"] = first["id"]
    else:
        first["destinations"] = [
            {
                "id": "model",
                "kind": "model",
                "display_name": "Model",
                "endpoint_ids": ["api-second" if fault == "foreign-endpoint" else "api-first"],
                "profile_ids": ["missing"] if fault == "foreign-profile" else [],
                "required_permissions": ["llm:invoke"],
                "credential_modes": [],
                "provider_id": "wrong" if fault == "wrong-provider" else None,
            }
        ]
    probe = Mock(side_effect=AssertionError("invalid catalog must not be probed"))
    monkeypatch.setattr(observe, "_http", probe)
    with pytest.raises(ValueError):
        observe.observe(api, observe.Deadline(5))
    probe.assert_not_called()
    assert api.calls == 0
    assert api.written is None


@pytest.mark.parametrize(
    "fault", [None, "mixed", "missing", "zero", "membership", "not-ready", "old"]
)
def test_publication_never_turns_incomplete_evidence_into_current_health(monkeypatch, fault):
    # Isolate aggregation with independently proven routes; scoped-alias proof is
    # deliberately unavailable and covered by the shared-provider regression below.
    monkeypatch.setattr(observe, "_route_loaded", lambda *_args: True)
    api = MemoryObserver()
    api.catalog["external-api-providers"][0]["global_credential_status"] = "configured"
    if fault == "zero":
        api.members = observe.Membership((), "empty")
    api.change = fault == "membership"

    def http(session, address, port, path, deadline, limit):
        if port == 15021:
            return (503 if fault == "not-ready" else 200), b"ready"
        if fault == "missing" and address == "192.0.2.1":
            return 200, json.dumps({"some_other_hash": REVISION}).encode()
        revision = (
            "b" * 64
            if fault == "old" or (fault == "mixed" and address == "192.0.2.1")
            else REVISION
        )
        return 200, json.dumps(native_dump(revision)).encode()

    monkeypatch.setattr(observe, "_http", http)
    result = observe.observe(api, observe.Deadline(5))
    assert result is api.written
    assert result["schema_version"] == 1
    assert result["profiles"] == []
    assert datetime.fromisoformat(result["valid_until"]) - datetime.fromisoformat(
        result["observed_at"]
    ) == timedelta(minutes=5)
    assert {e["state"] for e in result["endpoints"]} == {
        "available" if fault is None else "unavailable" if fault == "not-ready" else "unknown"
    }
    assert result["applied_policy_revision"] == (
        REVISION if fault in {None, "not-ready"} else "b" * 64 if fault == "old" else None
    )


def kubernetes_observer():
    api = observe.KubernetesObserver(Mock(), observe.Deadline(5))
    api.core = Mock()
    api.discovery = Mock()
    api.core.read_namespaced_service.return_value = NS(
        metadata=NS(uid="service-uid"),
        spec=NS(
            selector={"app": "gateway"},
            ports=[
                NS(name="http", port=8082, target_port=8082, protocol="TCP"),
                NS(name="admin", port=15000, target_port=15000, protocol="TCP"),
            ],
        ),
    )
    endpoints = [
        NS(
            conditions=NS(ready=False, serving=True, terminating=True),
            target_ref=NS(kind="Pod", namespace=observe.NAMESPACE, uid="old-uid", name="old-pod"),
            addresses=["192.0.2.1"],
        )
    ]
    api.discovery.list_namespaced_endpoint_slice.return_value = NS(
        metadata=NS(_continue=""),
        items=[
            NS(
                metadata=NS(owner_references=[NS(kind="Service", uid="service-uid")]),
                ports=[
                    NS(name="http", port=8082, protocol="TCP"),
                    NS(name="admin", port=15000, protocol="TCP"),
                ],
                endpoints=endpoints,
            )
        ],
    )
    pod = NS(
        metadata=NS(uid="old-uid", labels={"app": "gateway"}),
        status=NS(pod_ips=[NS(ip="192.0.2.1")]),
        spec=NS(containers=[NS(ports=[NS(name="api", container_port=8082, protocol="TCP")])]),
    )
    api.core.read_namespaced_pod.return_value = pod
    return api


def test_discovery_keeps_old_draining_serving_pods_and_checks_uid_addresses():
    api = kubernetes_observer()
    pod = api.core.read_namespaced_pod.return_value
    assert api.membership(inventory()).replicas == (
        observe.Replica("old-uid", "old-pod", ("192.0.2.1",)),
    )
    pod.metadata.uid = "replacement-uid"
    with pytest.raises(observe.ObservationError):
        api.membership(inventory())
    pod.metadata.uid = "old-uid"
    pod.status.pod_ips = [NS(ip="192.0.2.3")]
    with pytest.raises(observe.ObservationError):
        api.membership(inventory())


@pytest.mark.parametrize(
    "fault",
    [
        "empty-slice",
        "wrong-slice-port",
        "wrong-slice-name",
        "wrong-protocol",
        "missing-service",
        "wrong-target",
        "missing-named-target",
    ],
)
def test_service_or_slice_misrouting_cannot_publish_available(monkeypatch, fault):
    discovery = kubernetes_observer()
    service = discovery.core.read_namespaced_service.return_value
    endpoint_slice = discovery.discovery.list_namespaced_endpoint_slice.return_value.items[0]
    if fault == "empty-slice":
        endpoint_slice.ports = []
    elif fault == "wrong-slice-port":
        endpoint_slice.ports[0].port = 9
    elif fault == "wrong-slice-name":
        endpoint_slice.ports[0].name = "other"
    elif fault == "wrong-protocol":
        endpoint_slice.ports[0].protocol = "UDP"
    elif fault == "missing-service":
        service.spec.ports = []
    elif fault == "wrong-target":
        service.spec.ports[0].target_port = 9
    else:
        service.spec.ports[0].target_port = "missing"
    api = MemoryObserver()
    api.membership = discovery.membership
    result = observe.observe(api, observe.Deadline(5))
    assert result["applied_policy_revision"] is None
    assert all(endpoint["state"] == "unknown" for endpoint in result["endpoints"])


def test_service_mapping_change_is_part_of_membership_barrier(monkeypatch):
    discovery = kubernetes_observer()
    service_port = discovery.core.read_namespaced_service.return_value.spec.ports[0]
    before = discovery.membership(inventory())
    service_port.target_port = "api"
    after = discovery.membership(inventory())
    assert before.replicas == after.replicas
    assert before != after
    api = MemoryObserver()
    monkeypatch.setattr(api, "membership", Mock(side_effect=[before, after]))
    monkeypatch.setattr(
        observe,
        "_http",
        lambda _s, _a, port, *_args: (
            200,
            b"ready" if port == 15021 else json.dumps(native_dump()).encode(),
        ),
    )
    result = observe.observe(api, observe.Deadline(5))
    assert result["applied_policy_revision"] is None
    assert all(endpoint["state"] == "unknown" for endpoint in result["endpoints"])


@pytest.mark.parametrize(
    "fault",
    [
        "empty-matches",
        "wrong-path",
        "wrong-method",
        "empty-backends",
        "wrong-backend",
        "zero-weight",
        "no-router",
        "no-models",
        "orphan-model",
        "no-concrete",
        "invalid-target",
        "wrong-target",
        "no-provider",
        "wrong-provider-ref",
    ],
)
def test_broken_route_or_model_association_cannot_publish_available(monkeypatch, fault):
    dump = native_dump()
    route = dump["binds"][0]["listeners"]["listener-key"]["routes"]["ai-first-api"]
    router = dump["backends"][0]["backend"]["llmRouter"]["target"]
    if fault == "empty-matches":
        route["matches"] = []
    elif fault == "wrong-path":
        route["matches"][0]["path"] = {"pathPrefix": "/wrong"}
    elif fault == "wrong-method":
        route["matches"][0]["method"] = "GET"
    elif fault == "empty-backends":
        route["backends"] = []
    elif fault == "wrong-backend":
        route["backends"][0]["backend"] = "/unrelated-router"
    elif fault == "zero-weight":
        route["backends"][0]["weight"] = 0
    elif fault == "no-router":
        dump["backends"].pop(0)
    elif fault == "no-models":
        dump["models"] = []
    elif fault == "orphan-model":
        dump["models"][1]["listenerKey"] = "orphan-listener"
    elif fault == "no-concrete":
        router["models"] = []
    elif fault == "invalid-target":
        router["virtualModels"][0]["routing"]["conditional"][0]["invalid"] = True
    elif fault == "wrong-target":
        router["virtualModels"][0]["routing"]["conditional"][0]["model"] = "unrelated-model"
    elif fault == "no-provider":
        dump["backends"].pop(1)
    else:
        router["models"][0]["backend"]["backend"] = "unrelated/provider"
    if fault in {"invalid-target", "wrong-target"}:
        dump["models"][1]["kind"]["virtual"] = deepcopy(router["virtualModels"][0])
    if fault == "wrong-provider-ref":
        dump["models"][0]["kind"]["concrete"] = deepcopy(router["models"][0])
    api = MemoryObserver()
    monkeypatch.setattr(
        observe,
        "_http",
        lambda _s, _a, port, *_args: (
            200,
            b"ready" if port == 15021 else json.dumps(dump).encode(),
        ),
    )
    result = observe.observe(api, observe.Deadline(5))
    assert result["applied_policy_revision"] is None
    assert all(endpoint["state"] == "unknown" for endpoint in result["endpoints"])


@pytest.mark.parametrize("missing_api_provider", [False, True])
def test_shared_provider_stays_unknown_without_losing_signer_evidence(
    monkeypatch, missing_api_provider
):
    api = MemoryObserver()
    listener = observe.Listener(
        name="inspect-first",
        port=443,
        hostname="provider.example.test",
        routes=[
            observe.Route(
                name="ai-first-proxy",
                path="/",
                models={"first-model": ["first-internal"]},
            )
        ],
    )
    api.inventory.endpoints.append(observe.Endpoint(id="forward-proxy", listeners=[listener]))
    api.inventory.proxy = observe.Proxy(
        hostname="proxy.example.test",
        port=8443,
        listener=listener.name,
        inspection_hostname="provider.example.test",
    )
    for group in ("self-hosted-api-providers", "external-api-providers"):
        api.catalog[group][0]["routes"].append(catalog_route("forward-proxy", proxy=True))
    dump = native_dump()
    inspected = deepcopy(dump["binds"][0])
    inspected["address"] = "[::]:443"
    inspected["listeners"]["listener-key"].update(
        key="inspect-listener-key",
        listenerName=listener.name,
        hostname=listener.hostname,
        protocol={"HTTPS": {"certificateSource": "DynamicCa"}},
        routes={},
    )
    native_model_route(dump, inspected["listeners"]["listener-key"], "ai-first-proxy", "/", "first")
    # The dump loses registry keys: API and proxy scoped providers look identical.
    api_provider, proxy_provider = dump["backends"][1], dump["backends"][-1]
    assert api_provider == proxy_provider
    if missing_api_provider:
        assert dump["backends"].pop(1) is api_provider
        assert any(provider is proxy_provider for provider in dump["backends"])
    dump["binds"].append(inspected)
    policy = deepcopy(dump["policies"][0])
    policy["name"]["name"] = "ai-admission-inspect-first"
    policy["target"]["gateway"]["listenerName"] = listener.name
    dump["policies"].append(policy)
    monkeypatch.setattr(
        observe,
        "_http",
        lambda _s, _a, port, *_args: (
            200,
            b"ready" if port == 15021 else json.dumps(dump).encode(),
        ),
    )
    monkeypatch.setattr(observe, "current_roots", lambda _raw: [certificate("Root", ca=True)[0]])
    calls = []

    def probe(address, proxy, roots, deadline):
        calls.append((address, proxy.listener))
        if address == "192.0.2.2":
            raise OSError("failed handshake")
        return "b" * 64, (datetime.now(UTC) + timedelta(days=1)).isoformat()

    monkeypatch.setattr(observe, "probe_signer", probe)
    result = observe.observe(api, observe.Deadline(5))
    assert result["applied_policy_revision"] is None
    assert len(calls) == 2
    assert {row["pod_uid"]: row["state"] for row in result["signers"]} == {
        "old-uid": "verified",
        "new-uid": "unknown",
    }
    assert {e["id"]: e["state"] for e in result["endpoints"]} == {
        "api-first": "unknown",
        "api-second": "unknown",
        "forward-proxy": "unknown",
    }
    assert len(result["endpoints"]) == 3


def certificate(name, issuer=None, *, ca=False, expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer if issuer else (None, key)
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_cert.subject if issuer_cert else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=-1 if expired else 30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
        )
        .add_extension(x509.KeyUsage(True, False, False, False, False, ca, ca, False, False), True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), False
        ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
    return builder.sign(issuer_key, hashes.SHA256()), key


def server_context(tmp_path, prefix, cert, key, chain):
    pem = tmp_path / f"{prefix}.pem"
    pem.write_bytes(b"".join(c.public_bytes(Encoding.PEM) for c in [cert, *chain]))
    private = tmp_path / f"{prefix}.key"
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem, private)
    return context


@contextmanager
def tls_proxy(tmp_path, outer_root, signer, *, inner_hostname="provider.example.test"):
    outer_cert, outer_key = certificate("proxy.example.test", outer_root)
    inner_cert, inner_key = certificate(inner_hostname, signer)
    outer_ctx = server_context(tmp_path, "outer", outer_cert, outer_key, [])
    inner_ctx = server_context(tmp_path, "inner", inner_cert, inner_key, [signer[0]])
    ca_file = tmp_path / "outer-ca.pem"
    ca_file.write_bytes(outer_root[0].public_bytes(Encoding.PEM))
    received = {"connect": b"", "application": b""}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)

    def serve():
        try:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with outer_ctx.wrap_socket(raw, server_side=True) as outer:
                    while not received["connect"].endswith(b"\r\n\r\n"):
                        data = outer.recv(1)
                        if not data:
                            return
                        received["connect"] += data
                    outer.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
                    inner = inner_ctx.wrap_bio(incoming, outgoing, server_side=True)

                    def flush():
                        while outgoing.pending:
                            outer.sendall(outgoing.read())

                    while True:
                        try:
                            inner.do_handshake()
                            flush()
                            break
                        except ssl.SSLWantReadError:
                            flush()
                            data = outer.recv(16384)
                            if not data:
                                return
                            incoming.write(data)
                    while True:
                        try:
                            received["application"] += inner.read(4096)
                        except ssl.SSLWantReadError:
                            flush()
                            data = outer.recv(16384)
                            if not data:
                                return
                            incoming.write(data)
        except (OSError, ssl.SSLError):
            # Clients reject bad certificates and close without an application request.
            return

    thread = Thread(target=serve, daemon=True)
    thread.start()
    proxy = observe.Proxy(
        hostname="proxy.example.test",
        port=listener.getsockname()[1],
        listener="inspect-example",
        inspection_hostname="provider.example.test",
    )
    try:
        yield proxy, str(ca_file), received, inner_cert
    finally:
        thread.join(6)
        listener.close()
        assert not thread.is_alive()
        assert received["application"] == b""
        assert b"authorization" not in received["connect"].lower()


def test_real_two_replica_signers_are_intermediates_and_no_application_is_sent(tmp_path):
    root = certificate("Inspection root", ca=True)
    outer_root = certificate("Public proxy root", ca=True)
    fingerprints = []
    for name in ["First signer", "Second signer"]:
        signer = certificate(name, root, ca=True)
        with tls_proxy(tmp_path, outer_root, signer) as (proxy, ca, received, leaf):
            fingerprint, expiry = observe.probe_signer(
                "127.0.0.1", proxy, [root[0]], observe.Deadline(5), ca
            )
            fingerprints.append(fingerprint)
            assert fingerprint == signer[0].fingerprint(hashes.SHA256()).hex()
            assert fingerprint != leaf.fingerprint(hashes.SHA256()).hex()
            assert expiry == signer[0].not_valid_after_utc.isoformat()
            assert (
                received["connect"] == b"CONNECT provider.example.test:443 HTTP/1.1\r\n"
                b"Host: provider.example.test:443\r\n\r\n"
            )
    assert fingerprints[0] != fingerprints[1]


@pytest.mark.parametrize(
    "fault", ["trust", "expired", "outer-hostname", "inner-hostname", "deadline"]
)
def test_real_tls_rejects_untrusted_expired_and_wrong_hostname_chains(tmp_path, fault):
    root = certificate("Inspection root", ca=True)
    signer = certificate("Signer", root, ca=True, expired=fault == "expired")
    outer_root = certificate("Proxy root", ca=True)
    with tls_proxy(
        tmp_path,
        outer_root,
        signer,
        inner_hostname=(
            "wrong.example.test" if fault == "inner-hostname" else "provider.example.test"
        ),
    ) as (proxy, ca, _, _leaf):
        if fault == "outer-hostname":
            proxy = proxy.model_copy(update={"hostname": "wrong.example.test"})
        roots = (
            [certificate("Retired unrelated root", ca=True)[0]] if fault == "trust" else [root[0]]
        )
        with pytest.raises((OSError, SSL.Error, VerificationError, observe.ObservationError)):
            observe.probe_signer(
                "127.0.0.1", proxy, roots, observe.Deadline(-1 if fault == "deadline" else 5), ca
            )


def test_only_current_publication_roots_are_used():
    root, key = certificate("Current root", ca=True)
    retired, _ = certificate("Retired root", ca=True)
    snapshots = []
    for cert in [retired, root]:
        pem = cert.public_bytes(Encoding.PEM).decode()
        source = trust.RootSource(
            config_map="example",
            fingerprint_sha256=cert.fingerprint(hashes.SHA256()).hex(),
            state="active",
        )
        snapshots.append(
            trust.candidate_snapshot(
                trust.Inventory(schema_version=1, roots=[source]), lambda _: pem, datetime.now(UTC)
            )
        )
    publication = trust.Publication(current_version=snapshots[-1].version, versions=snapshots)
    assert observe.current_roots(publication.model_dump_json()) == [root]
