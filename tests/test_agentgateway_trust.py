"""Public-only trust publication, concurrency and multi-replica release boundaries."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from kubernetes import client
from kubernetes.client.rest import ApiException

from k8s_stack_tooling import agentgateway_trust as trust

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def certificate(
    name: str = "Example inspection root",
    *,
    ca: bool = True,
    signer_name: str | None = None,
    wrong_signature: bool = False,
    expired: bool = False,
) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, signer_name or name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=2))
        .not_valid_after(NOW + timedelta(days=-1 if expired else 365))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, ca, ca, False, False), True)
        .sign(ec.generate_private_key(ec.SECP256R1()) if wrong_signature else key, hashes.SHA256())
    )
    return cert.public_bytes(Encoding.PEM).decode()


def source(pem: str, name: str = "root", state: str = "active") -> trust.RootSource:
    cert = x509.load_pem_x509_certificate(pem.encode())
    return trust.RootSource.model_validate(
        {
            "config_map": name,
            "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
            "state": state,
        }
    )


class MemoryPublisher(trust.KubernetesPublisher):
    def __init__(self, pem: str) -> None:
        self.roots = {"root": pem}
        self.reviewed = trust.Inventory(schema_version=1, roots=[source(pem)])
        self.inventory_revision = "1"
        self.publication: trust.Publication | None = None
        self.revision = 0
        self.before_write: Any = None
        self.barrier_hook: Any = None
        self.barriers = 0

    def inventory(self) -> tuple[trust.Inventory, str]:
        return self.reviewed.model_copy(deep=True), self.inventory_revision

    def source(self, source: trust.RootSource) -> str:
        return self.roots[source.config_map]

    def current(self) -> tuple[trust.Publication | None, Any]:
        return deepcopy(self.publication), self.revision if self.publication else None

    def write(self, publication: trust.Publication, previous: Any) -> None:
        if self.before_write:
            self.before_write()
        if previous != (self.revision if self.publication else None):
            raise ApiException(status=409)
        self.publication = deepcopy(publication)
        self.revision += 1

    def barrier(self, publication: trust.Publication) -> None:
        self.barriers += 1
        if self.barrier_hook:
            self.barrier_hook()

    def add_root(self) -> None:
        pem = certificate("Example next root")
        self.roots["next-root"] = pem
        self.reviewed.roots.append(source(pem, "next-root", "transitional"))
        self.inventory_revision = "2"


@pytest.mark.parametrize(
    "pem",
    [
        certificate(ca=False),
        certificate(signer_name="Example parent root"),
        certificate(wrong_signature=True),
        certificate() + certificate(),
        certificate() + "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
    ],
)
def test_invalid_or_non_root_material_is_rejected(pem: str) -> None:
    with pytest.raises(trust.PublicationError):
        trust.parse_root(pem)


def test_bootstrap_stage_and_promote_preserve_the_archive() -> None:
    api = MemoryPublisher(certificate())
    with pytest.raises(trust.PublicationError, match="initialize"):
        trust.publish(api, "stage", now=NOW)
    first = trust.publish(api, "stage", initialize=True, now=NOW)
    assert first["version"] == first["current_version"] == first["required_version"]
    initial = deepcopy(api.publication)
    assert not trust.publish(api, "stage", initialize=True, now=NOW + timedelta(hours=1))["changed"]
    api.add_root()
    staged = trust.publish(api, "stage", now=NOW)
    assert staged["current_version"] == first["version"]
    assert staged["required_version"] == staged["version"] != first["version"]
    assert api.publication is not None and initial is not None
    assert api.publication.versions[0] == initial.versions[0]
    assert trust.publish(api, "promote", now=NOW)["current_version"] == staged["version"]
    assert len(api.publication.versions) == 2
    assert api.barriers == 1
    with pytest.raises(trust.PublicationError, match="history"):
        trust.publish(api, "stage", initialize=True, now=NOW)


def test_source_replacement_cannot_replace_reviewed_trust() -> None:
    api = MemoryPublisher(certificate())
    trust.publish(api, "stage", initialize=True, now=NOW)
    previous = deepcopy(api.publication)
    api.roots["root"] = certificate()
    with pytest.raises(trust.PublicationError, match="fingerprint"):
        trust.publish(api, "stage", now=NOW)
    assert api.publication == previous


def test_history_can_expire_without_invalidating_public_downloads() -> None:
    pem = certificate(expired=True)
    old = trust.candidate_snapshot(
        trust.Inventory(schema_version=1, roots=[source(pem)]),
        lambda _: pem,
        NOW - timedelta(days=2),
    )
    archive = trust.stage_snapshot(None, old)
    assert trust.validate_publication(archive)
    api = MemoryPublisher(pem)
    with pytest.raises(trust.PublicationError, match="currently valid"):
        trust.publish(api, "stage", initialize=True, now=NOW)


@pytest.mark.parametrize("mutation", ["digest", "version", "duplicate-root", "duplicate-version"])
def test_corrupt_archive_is_not_accepted(mutation: str) -> None:
    api = MemoryPublisher(certificate())
    trust.publish(api, "stage", initialize=True, now=NOW)
    assert api.publication is not None
    archive = api.publication.model_copy(deep=True)
    entry = archive.versions[0]
    if mutation == "digest":
        entry.bundle_sha256 = "0" * 64
    elif mutation == "version":
        entry.version = "0" * 64
    elif mutation == "duplicate-root":
        entry.certificates.append(entry.certificates[0])
    else:
        archive.versions.append(entry)
    with pytest.raises(trust.PublicationError):
        trust.validate_publication(archive)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1,"roots":[]}',
        '{"schema_version":true,"roots":[]}',
        '{"schema_version":2,"roots":[]}',
        " " * (trust.MAX_BYTES + 1),
    ],
)
def test_document_bounds_and_ambiguous_json_fail_closed(raw: str) -> None:
    with pytest.raises(trust.PublicationError):
        trust.decode_document(raw, trust.Inventory)


def test_state_only_updates_do_not_duplicate_content_addressed_versions() -> None:
    api = MemoryPublisher(certificate())
    first = trust.publish(api, "stage", initialize=True, now=NOW)
    api.reviewed.roots[0].state = "transitional"
    updated = trust.publish(api, "stage", now=NOW + timedelta(hours=1))
    assert updated["version"] == first["version"]
    assert updated["retained_versions"] == 1
    assert api.publication is not None
    assert api.publication.versions[0].published_at == NOW.isoformat().replace("+00:00", "Z")


def test_conflicting_writer_is_reloaded_without_losing_history() -> None:
    api = MemoryPublisher(certificate())
    trust.publish(api, "stage", initialize=True, now=NOW)
    api.add_root()
    other_pem = certificate("Example concurrent root")
    other = trust.candidate_snapshot(
        trust.Inventory(schema_version=1, roots=[source(other_pem)]), lambda _: other_pem, NOW
    )

    def conflict() -> None:
        api.publication = trust.stage_snapshot(api.publication, other)
        api.revision += 1
        api.before_write = None

    api.before_write = conflict
    result = trust.publish(api, "stage", now=NOW)
    assert result["retained_versions"] == 3
    assert api.publication is not None
    assert other in api.publication.versions


def test_conflicting_promotion_repeats_replica_proof() -> None:
    api = MemoryPublisher(certificate())
    first = trust.publish(api, "stage", initialize=True, now=NOW)
    api.add_root()
    trust.publish(api, "stage", now=NOW)

    def conflict() -> None:
        api.revision += 1
        api.before_write = None

    api.before_write = conflict
    assert trust.publish(api, "promote", now=NOW)["current_version"] != first["version"]
    assert api.barriers == 2
    api.before_write = lambda: (_ for _ in ()).throw(ApiException(status=409))
    api.reviewed.roots[0].state = "transitional"
    with pytest.raises(trust.PublicationError, match="conflicted repeatedly"):
        trust.publish(api, "stage", now=NOW)


def test_failed_barrier_and_changed_inventory_cannot_advance_current() -> None:
    api = MemoryPublisher(certificate())
    first = trust.publish(api, "stage", initialize=True, now=NOW)
    api.add_root()
    with pytest.raises(trust.PublicationError, match="staged"):
        trust.publish(api, "promote", now=NOW)
    trust.publish(api, "stage", now=NOW)
    api.barrier_hook = lambda: (_ for _ in ()).throw(trust.PublicationError("stale replica"))
    with pytest.raises(trust.PublicationError, match="stale replica"):
        trust.publish(api, "promote", now=NOW)
    api.barrier_hook = lambda: setattr(api, "inventory_revision", "3")
    with pytest.raises(trust.PublicationError, match="inventory changed"):
        trust.publish(api, "promote", now=NOW)
    assert api.publication is not None
    assert api.publication.current_version == first["version"]


def test_configmap_write_is_a_resource_version_precondition() -> None:
    api = MemoryPublisher(certificate())
    trust.publish(api, "stage", initialize=True, now=NOW)
    publisher = trust.KubernetesPublisher(trust.Settings(), client.ApiClient())
    publisher.core = Mock()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="output", uid="original", resource_version="42")
    )
    assert api.publication is not None
    publisher.write(api.publication, cm)
    body = publisher.core.replace_namespaced_config_map.call_args.args[2]
    assert body["metadata"]["resourceVersion"] == "42"
    assert body["metadata"]["uid"] == "original"
    assert not publisher.core.create_namespaced_config_map.called


def test_unrelated_output_configmap_is_never_adopted() -> None:
    publisher = trust.KubernetesPublisher(trust.Settings(), client.ApiClient())
    publisher.core = Mock()
    publisher.core.read_namespaced_config_map.return_value = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="output", labels={"owner": "someone-else"}),
        data={"unrelated": "preserve"},
    )
    with pytest.raises(trust.PublicationError, match="another publisher"):
        publisher.current()
    assert not publisher.core.replace_namespaced_config_map.called


def test_root_limit_is_per_bundle_and_version_limit_preserves_history() -> None:
    pem = certificate()
    candidate = trust.candidate_snapshot(
        trust.Inventory(schema_version=1, roots=[source(pem)]), lambda _: pem, NOW
    )
    too_many = {
        "schema_version": 1,
        "current_version": candidate.version,
        "versions": [candidate.model_dump()] * 65,
    }
    with pytest.raises(trust.PublicationError):
        trust.decode_document(json.dumps(too_many), trust.Publication)
    archive = trust.stage_snapshot(None, candidate)
    for i in range(16):
        pem = certificate(f"Example historical root {i}")
        candidate = trust.candidate_snapshot(
            trust.Inventory(schema_version=1, roots=[source(pem)]), lambda _: pem, NOW
        )
        archive = trust.stage_snapshot(archive, candidate)
    # Retention may span more root generations than any single bundle can hold.
    assert len(archive.versions) == len(trust.validate_publication(archive)) == 17
    oversized_bundle = archive.model_dump()
    oversized_bundle["versions"][0]["certificates"] = [
        snapshot.certificates[0].model_dump() for snapshot in archive.versions
    ]
    with pytest.raises(trust.PublicationError):
        trust.decode_document(json.dumps(oversized_bundle), trust.Publication)


def test_output_size_limit_includes_retained_configmap_data() -> None:
    api = MemoryPublisher(certificate())
    trust.publish(api, "stage", initialize=True, now=NOW)
    publisher = trust.KubernetesPublisher(trust.Settings(), client.ApiClient())
    publisher.core = Mock()
    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(resource_version="42"),
        data={"retained": "x" * trust.MAX_BYTES},
    )
    assert api.publication is not None
    with pytest.raises(trust.PublicationError, match="1 MiB"):
        publisher.write(api.publication, cm)
    assert not publisher.core.replace_namespaced_config_map.called


def test_reselecting_an_older_bundle_keeps_the_newest_readiness_floor() -> None:
    api = MemoryPublisher(certificate())
    first = trust.publish(api, "stage", initialize=True, now=NOW)
    api.add_root()
    latest = trust.publish(api, "stage", now=NOW)
    trust.publish(api, "promote", now=NOW)
    api.reviewed.roots.pop()
    trust.publish(api, "stage", now=NOW)
    result = trust.publish(api, "promote", now=NOW)
    assert result["current_version"] == first["version"]
    assert result["required_version"] == latest["version"]
    assert result["retained_versions"] == 2


@pytest.mark.parametrize(
    "failure", [ApiException(status=403, reason="private input"), RuntimeError("private input")]
)
def test_cli_sanitizes_client_and_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    monkeypatch.setattr(trust.sys, "argv", ["agentgateway-trust-publish", "stage"])
    monkeypatch.setattr(trust.config, "load_incluster_config", Mock(side_effect=failure))
    with pytest.raises(SystemExit) as error:
        trust.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "private input" not in captured.err
    assert "Traceback" not in captured.err
    assert "Trust publication failed" in captured.err


def obj(**kwargs: Any) -> Any:
    return SimpleNamespace(**kwargs)


def kube_replicas(version: str) -> tuple[trust.KubernetesPublisher, list[Any], list[Any]]:
    publisher = trust.KubernetesPublisher(trust.Settings(), client.ApiClient())
    floor = obj(
        containers=[
            obj(
                ports=[obj(container_port=4010)],
                env=[obj(name=trust.FLOOR_ENV, value=version, value_from=None)],
            )
        ]
    )
    labels = {"app.kubernetes.io/name": "studio-api"}
    deployment = obj(
        metadata=obj(uid="deployment", generation=2),
        spec=obj(replicas=2, template=obj(spec=floor, metadata=obj(labels=labels))),
        status=obj(
            observed_generation=2,
            replicas=2,
            updated_replicas=2,
            ready_replicas=2,
            available_replicas=2,
        ),
    )
    pods = [
        obj(
            metadata=obj(
                uid=f"pod-{i}",
                deletion_timestamp=None,
                owner_references=[obj(controller=True, uid="rs")],
            ),
            spec=deepcopy(floor),
            status=obj(
                conditions=[obj(type="Ready", status="True")], pod_ip=f"192.0.2.{i}", pod_ips=None
            ),
        )
        for i in (1, 2)
    ]
    endpoints = [
        obj(
            conditions=obj(ready=True, serving=True, terminating=False),
            target_ref=obj(kind="Pod", namespace="frontend-studio", uid=pod.metadata.uid),
            addresses=[pod.status.pod_ip],
        )
        for pod in pods
    ]
    publisher.core = Mock()
    publisher.apps = Mock()
    publisher.discovery = Mock()
    publisher.apps.read_namespaced_deployment.return_value = deployment
    publisher.core.read_namespaced_service.return_value = obj(spec=obj(selector=labels))
    publisher.core.list_namespaced_pod.return_value = obj(items=pods, metadata=obj(_continue=None))
    publisher.apps.list_namespaced_replica_set.return_value = obj(
        items=[
            obj(metadata=obj(uid="rs", owner_references=[obj(controller=True, uid="deployment")]))
        ],
        metadata=obj(_continue=None),
    )
    publisher.discovery.list_namespaced_endpoint_slice.return_value = obj(
        items=[obj(endpoints=endpoints, ports=[obj(port=4010)])], metadata=obj(_continue=None)
    )
    return publisher, pods, endpoints


@pytest.mark.parametrize(
    "failure",
    [
        "floor",
        "duplicate-floor",
        "wrong-container",
        "generation",
        "draining",
        "foreign",
        "address",
        "missing",
        "pagination",
    ],
)
def test_replica_barrier_rejects_incomplete_or_stale_membership(failure: str) -> None:
    version = "a" * 64
    publisher, pods, endpoints = kube_replicas(version)
    if failure == "floor":
        pods[1].spec.containers[0].env[0].value = "b" * 64
    elif failure == "duplicate-floor":
        pods[1].spec.containers[0].env.append(
            obj(name=trust.FLOOR_ENV, value=None, value_from=obj())
        )
    elif failure == "wrong-container":
        pods[1].spec.containers[0].ports[0].container_port = 4011
    elif failure == "generation":
        publisher.apps.read_namespaced_deployment(
            "deployment", "namespace"
        ).status.observed_generation = 1
    elif failure == "draining":
        endpoints[1].conditions.ready = False
        endpoints[1].conditions.terminating = True
    elif failure == "foreign":
        endpoints[1].target_ref.uid = "foreign-pod"
    elif failure == "address":
        endpoints[1].addresses.append("192.0.2.99")
    elif failure == "pagination":
        publisher.core.list_namespaced_pod("namespace").metadata._continue = "next-page"
    else:
        endpoints.pop()
    with pytest.raises(trust.PublicationError):
        publisher.replicas(version)


def test_barrier_fetches_all_immutable_assets_on_each_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = MemoryPublisher(certificate())
    trust.publish(memory, "stage", initialize=True, now=NOW)
    memory.add_root()
    staged = trust.publish(memory, "stage", now=NOW)
    publisher, _, _ = kube_replicas(staged["required_version"])
    publication = memory.publication
    assert publication is not None
    assets = {f"bundles/{s.version}.pem": s.bundle() for s in publication.versions}
    assets.update(
        {
            f"certificates/{fp}.cer": der
            for fp, der in trust.validate_publication(publication).items()
        }
    )
    calls = []

    class Response:
        status_code = 200

        def __init__(self, data: bytes) -> None:
            self.data = data

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def iter_content(self, _: int) -> list[bytes]:
            return [self.data]

    class Session(Response):
        def __init__(self) -> None:
            super().__init__(b"")

        def get(self, url: str, **kwargs: Any) -> Response:
            assert kwargs["allow_redirects"] is False
            calls.append(url)
            return Response(assets[url.split("/api/gateway/trust/")[1]])

    monkeypatch.setattr(trust.requests, "Session", Session)
    publisher.barrier(publication)
    assert len(calls) == 2 * len(assets)
    assert any("192.0.2.1:4010" in url for url in calls)
    assert any("192.0.2.2:4010" in url for url in calls)
    original_replicas = publisher.replicas
    membership = original_replicas(staged["required_version"])
    monkeypatch.setattr(
        publisher, "replicas", Mock(side_effect=[membership, ("new-deployment", 3, membership[2])])
    )
    with pytest.raises(trust.PublicationError, match="membership changed"):
        publisher.barrier(publication)
    monkeypatch.setattr(publisher, "replicas", original_replicas)
    assets[next(iter(assets))] = b"wrong bytes"
    with pytest.raises(trust.PublicationError, match="unexpected asset"):
        publisher.barrier(publication)
