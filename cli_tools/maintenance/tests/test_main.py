import copy
import json
import ssl
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from maintenance import main as cli

IMAGE = "ghcr.io/neurwerk/k8s-stack-tooling:0.6.2@sha256:" + "a" * 64


def resource(kind, name, spec=None):
    obj = {
        "apiVersion": cli.APIS[kind][0],
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": "maintenance",
            "labels": dict(cli.LABELS),
        },
    }
    if spec is not None:
        obj["spec"] = spec
    return obj


class Cluster:
    def __init__(self):
        self.objects, self.calls, self.sequence, self.clock = {}, [], 0, 0
        self.rollout, self.endpoints, self.inventory = True, True, True
        self.tls_error, self.marker_override, self.replace_lock = False, None, False
        self.deployment = resource(
            "Deployment", "maintenance", {"template": {"spec": {"containers": [{"image": IMAGE}]}}}
        )
        self.routes = {}
        for scope in cli.SCOPES:
            names = (
                [f"{s}.example.com" for s in cli.SCOPES[1:]]
                if scope == "global"
                else [f"{scope}.example.com"]
            )
            route = resource(
                "IngressRoute",
                f"maintenance-{scope}",
                {"routes": [{"services": [{"name": "maintenance", "port": 8080}]}]},
            )
            route["metadata"]["annotations"] = {cli.HOSTS: json.dumps(names)}
            self.routes[scope] = route
        self.data = {
            "version": 1,
            "namespace": "maintenance",
            "serviceName": "maintenance",
            "servicePort": 8080,
            "deployment": self.deployment,
            "routes": self.routes,
        }
        config = resource("ConfigMap", "maintenance-runtime")
        config["data"] = {"contract.json": json.dumps(self.data)}
        self.put(config)

    def put(self, obj):
        obj = copy.deepcopy(obj)
        self.sequence += 1
        obj["metadata"].update(uid=f"uid-{self.sequence}", resourceVersion=str(self.sequence))
        self.objects[(obj["kind"], obj["metadata"]["name"])] = obj
        return obj

    def run(self, command, **kwargs):
        assert command[:3] == ["/mock/kubectl", "--context=test", "--namespace=maintenance"]
        assert command[3].startswith("--request-timeout=") and kwargs["timeout"] > 0
        args = command[4:]
        payload = json.loads(kwargs["input"]) if kwargs["input"] else None
        self.calls.append((args, payload))
        code, result = self.dispatch(args, payload)
        return subprocess.CompletedProcess(command, code, json.dumps(result) if result else "", "")

    def dispatch(self, args, payload):
        if args[0] == "create":
            key = (payload["kind"], payload["metadata"]["name"])
            return (1, None) if key in self.objects else (0, self.put(payload))
        if args[0] == "delete":
            plural, name = args[1].split("/")[-2:]
            kind = next(kind for kind, (_, value) in cli.APIS.items() if value == plural)
            key = (kind, name)
            obj = self.objects[key]
            if self.replace_lock and name == cli.LOCK:
                obj["metadata"]["uid"] = "replacement"
            expected = {field: obj["metadata"][field] for field in ("uid", "resourceVersion")}
            if payload["preconditions"] != expected:
                return 1, None
            del self.objects[key]
            return 0, {"status": "Success"}
        if args[0] == "rollout":
            return (0 if self.rollout else 1), None
        return self.read(args)

    def read(self, args):
        if "--all-namespaces" in args:
            items = [obj for (kind, _), obj in self.objects.items() if kind == "IngressRoute"]
            return (0, {"items": items}) if self.inventory else (1, None)
        if args[1] == "endpointslices.discovery.k8s.io":
            return 0, {
                "items": [
                    {
                        "endpoints": [
                            {"addresses": ["192.0.2.1"], "conditions": {"ready": self.endpoints}}
                        ]
                    }
                ]
            }
        kind = {"deployment": "Deployment", "configmap": "ConfigMap", cli.ROUTES: "IngressRoute"}[
            args[1]
        ]
        return 0, self.objects.get((kind, args[2]))

    def https(self, host, *, timeout, context):
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        assert 0 < timeout <= 5
        maintained = any(
            host in cli.hosts(obj)
            for (kind, _), obj in self.objects.items()
            if kind == "IngressRoute" and cli.HOSTS in obj["metadata"].get("annotations", {})
        )
        status, marker = self.marker_override or ((503, True) if maintained else (401, False))
        response = SimpleNamespace(
            status=status, getheader=Mock(return_value="true" if marker else "")
        )
        return SimpleNamespace(
            request=Mock(side_effect=ssl.SSLCertVerificationError() if self.tls_error else None),
            getresponse=Mock(return_value=response),
            close=Mock(),
        )

    def sleep(self, seconds):
        self.clock += seconds


@pytest.fixture
def cluster(monkeypatch):
    cluster = Cluster()
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/mock/kubectl")
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=cluster.run))
    monkeypatch.setattr(cli.http.client, "HTTPSConnection", Mock(side_effect=cluster.https))
    monkeypatch.setattr(cli.time, "monotonic", lambda: cluster.clock)
    monkeypatch.setattr(cli.time, "sleep", cluster.sleep)
    monkeypatch.setattr(cli, "WAIT", 2)
    return cluster


def invoke(command, scope="global"):
    return cli.main([command, scope, "--context", "test"])


def test_on_is_idempotent_and_always_locked(cluster):
    assert invoke("on", "studio") == 0
    deployment = cluster.objects[("Deployment", "maintenance")]
    deployment["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "requests": {"cpu": "10m"}
    }
    assert invoke("on", "studio") == 0
    creates = [payload for args, payload in cluster.calls if args[0] == "create"]
    assert sum(obj["metadata"]["name"] == cli.LOCK for obj in creates) == 2
    assert sum(obj["kind"] == "Deployment" for obj in creates) == 1
    assert sum(obj["kind"] == "IngressRoute" for obj in creates) == 1
    assert ("ConfigMap", cli.LOCK) not in cluster.objects


def test_independent_scopes_and_old_image_cleanup(cluster):
    assert invoke("on", "studio") == invoke("on", "global") == 0
    assert invoke("off", "global") == 0
    assert ("Deployment", "maintenance") in cluster.objects
    assert ("IngressRoute", "maintenance-studio") in cluster.objects
    cluster.objects[("Deployment", "maintenance")]["spec"]["template"]["spec"]["containers"][0][
        "image"
    ] = "old"
    assert invoke("off", "studio") == 0
    assert ("Deployment", "maintenance") not in cluster.objects
    assert ("ConfigMap", "maintenance-runtime") in cluster.objects
    assert cluster.clock >= cli.GRACE


@pytest.mark.parametrize("failure", ["rollout", "endpoints"])
def test_startup_failure_keeps_routes_unchanged(cluster, failure):
    cluster.put(cluster.routes["studio"])
    setattr(cluster, failure, False)
    assert invoke("on") == 1
    assert ("IngressRoute", "maintenance-global") not in cluster.objects
    assert ("IngressRoute", "maintenance-studio") in cluster.objects
    assert ("Deployment", "maintenance") in cluster.objects


@pytest.mark.parametrize("kind", ["Deployment", "IngressRoute"])
def test_foreign_ownership_never_adopted_or_deleted(cluster, kind):
    obj = copy.deepcopy(cluster.deployment if kind == "Deployment" else cluster.routes["global"])
    obj["metadata"]["labels"] = {}
    cluster.put(obj)
    assert invoke("on") == 1
    assert invoke("off") == 1
    assert (kind, obj["metadata"]["name"]) in cluster.objects


@pytest.mark.parametrize("command", ["on", "off"])
def test_stale_lock_refused_even_for_idempotent_on(cluster, command):
    assert invoke("on") == 0
    lock = resource("ConfigMap", cli.LOCK)
    lock["metadata"]["creationTimestamp"] = "2000-01-01T00:00:00Z"
    cluster.put(lock)
    before = copy.deepcopy(cluster.objects)
    assert invoke(command) == 1
    assert cluster.objects == before


def test_lock_release_uses_uid_and_resource_version(cluster):
    cluster.replace_lock = True
    assert invoke("on") == 1
    assert cluster.objects[("ConfigMap", cli.LOCK)]["metadata"]["uid"] == "replacement"
    args, payload = cluster.calls[-1]
    assert args[0] == "delete"
    assert set(payload["preconditions"]) == {"uid", "resourceVersion"}
    assert payload["preconditions"]["uid"] != "replacement"


@pytest.mark.parametrize("failure", ["tls", "missing-marker", "wrong-status"])
def test_https_failure_retains_activated_backend(cluster, failure):
    cluster.tls_error = failure == "tls"
    if failure != "tls":
        cluster.marker_override = (503, False) if failure == "missing-marker" else (200, True)
    assert invoke("on") == 1
    assert ("Deployment", "maintenance") in cluster.objects
    assert ("IngressRoute", "maintenance-global") in cluster.objects


@pytest.mark.parametrize("failure", ["listing", "unknown", "indirect"])
def test_unproven_references_keep_backend(cluster, failure):
    assert invoke("on") == 0
    if failure == "listing":
        cluster.inventory = False
    else:
        route = resource(
            "IngressRoute",
            "unknown",
            {
                "routes": [
                    {
                        "services": [
                            {
                                "name": "maintenance",
                                "kind": "TraefikService" if failure == "indirect" else "Service",
                            }
                        ]
                    }
                ]
            },
        )
        cluster.put(route)
    assert invoke("off") == 1
    assert ("Deployment", "maintenance") in cluster.objects


def test_retry_off_global_cleans_orphan_after_failed_convergence(cluster):
    assert invoke("on") == 0
    cluster.marker_override = (503, True)
    assert invoke("off") == 1
    assert ("IngressRoute", "maintenance-global") not in cluster.objects
    assert ("Deployment", "maintenance") in cluster.objects
    cluster.marker_override = None
    assert invoke("off") == 0
    assert ("Deployment", "maintenance") not in cluster.objects


def test_relist_after_grace_protects_backend(cluster, monkeypatch):
    cluster.put(cluster.deployment)
    cluster.marker_override = (401, False)

    def sleep(seconds):
        cluster.sleep(seconds)
        if seconds == cli.GRACE:
            cluster.put(cluster.routes["studio"])

    monkeypatch.setattr(cli.time, "sleep", sleep)
    assert invoke("off") == 1
    assert ("Deployment", "maintenance") in cluster.objects


def test_image_mismatch_prevents_activation_but_not_cleanup(cluster):
    deployment = copy.deepcopy(cluster.deployment)
    deployment["spec"]["template"]["spec"]["containers"][0]["image"] = "old-image"
    cluster.put(deployment)
    assert invoke("on") == 1
    assert ("IngressRoute", "maintenance-global") not in cluster.objects
    assert invoke("off") == 0


def test_status_is_read_only_and_context_required(cluster, capsys):
    assert invoke("status") == 0
    assert json.loads(capsys.readouterr().out)["deploymentPresent"] is False
    assert all(args[0] == "get" for args, _ in cluster.calls)
    with pytest.raises(SystemExit):
        cli.main(["on"])


def test_base_scope_and_version_checks(cluster):
    del cluster.data["routes"]["studio"]
    config = cluster.objects[("ConfigMap", "maintenance-runtime")]
    config["data"]["contract.json"] = json.dumps(cluster.data)
    assert invoke("on", "studio") == 1
    cluster.data["version"] = 2
    config["data"]["contract.json"] = json.dumps(cluster.data)
    assert invoke("on") == 1
    assert ("Deployment", "maintenance") not in cluster.objects


def test_partial_inventory_is_not_empty(cluster, monkeypatch):
    runner = cli.Maintenance("test")
    monkeypatch.setattr(
        runner, "run", Mock(return_value='{"items": [], "metadata": {"continue": "next"}}')
    )
    with pytest.raises(RuntimeError, match="Incomplete"):
        runner.references()
