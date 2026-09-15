"""Operate Base-approved maintenance routes from a trusted workstation."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import shutil
import ssl
import subprocess
import time
from collections.abc import Sequence
from subprocess import SubprocessError
from typing import Any

type Object = dict[str, Any]
NAMESPACE = "maintenance"
LOCK = "maintenance-operation-lock"
ROUTES = "ingressroutes.traefik.io"
SCOPES = ("global", "studio", "dify", "librechat", "langfuse")
LABELS = {
    "app.kubernetes.io/name": "maintenance",
    "app.kubernetes.io/instance": "maintenance",
    "app.kubernetes.io/part-of": "maintenance",
    "maintenance.neurwerk.com/managed-by": "operator",
}
HOSTS = "maintenance.neurwerk.com/hosts"
IMAGE = re.compile(r"ghcr\.io/neurwerk/k8s-stack-tooling:\d+\.\d+\.\d+@sha256:[a-f0-9]{64}")
WAIT, GRACE = 120, 5
APIS = {
    "ConfigMap": ("v1", "configmaps"),
    "Deployment": ("apps/v1", "deployments"),
    "IngressRoute": ("traefik.io/v1alpha1", "ingressroutes"),
}


def require(condition: object, message: str) -> None:
    """Stop an unsafe operation without attempting compensating deletes."""
    if not condition:
        raise RuntimeError(message)


def owned(obj: Object, kind: str, name: str) -> None:
    """Check the fixed identity and maintenance labels; never adopt foreign objects."""
    meta = obj["metadata"]
    require(
        obj["kind"] == kind
        and obj["apiVersion"] == APIS[kind][0]
        and meta["name"] == name
        and meta["namespace"] == NAMESPACE,
        "Unexpected resource identity",
    )
    require(
        all(meta.get("labels", {}).get(key) == value for key, value in LABELS.items())
        and not meta.get("ownerReferences")
        and not meta.get("finalizers")
        and not meta.get("deletionTimestamp")
        and meta.get("labels", {}).get("app.kubernetes.io/managed-by") not in {"Helm", "Flux"}
        and not any(key.startswith("meta.helm.sh/") for key in meta.get("annotations", {})),
        "Foreign ownership; refusing adoption or deletion",
    )


def image(deployment: Object) -> str:
    """Read the single maintenance container image, not its defaulted Pod settings."""
    containers = deployment["spec"]["template"]["spec"]["containers"]
    require(len(containers) == 1, "Expected one maintenance container")
    return str(containers[0]["image"])


def hosts(route: Object) -> set[str]:
    """Use Base's approved hostname annotation."""
    return set(json.loads(route["metadata"]["annotations"][HOSTS]))


def verify(names: set[str], maintained: set[str]) -> None:
    """Wait for trusted HTTPS headers; normal applications need not return 200."""
    pending, deadline = names.copy(), time.monotonic() + WAIT
    while pending:
        for host in tuple(pending):
            remaining = deadline - time.monotonic()
            require(remaining > 0, "HTTPS convergence failed; backend retained")
            connection = http.client.HTTPSConnection(
                host, timeout=min(5, remaining), context=ssl.create_default_context()
            )
            try:
                connection.request("GET", "/", headers={"Cache-Control": "no-cache"})
                response = connection.getresponse()
                marker = response.getheader("X-Platform-Maintenance", "").lower() == "true"
                if (host in maintained and response.status == 503 and marker) or (
                    host not in maintained and not marker
                ):
                    pending.remove(host)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        if pending:
            time.sleep(1)


class Maintenance:
    """Keep all Kubernetes operations scoped to one explicit context."""

    def __init__(self, context: str) -> None:
        """Resolve kubectl locally without using an ambient cluster context."""
        executable = shutil.which("kubectl")
        require(executable and context, "kubectl and an explicit --context are required")
        self.command = [str(executable), f"--context={context}", f"--namespace={NAMESPACE}"]

    def run(self, *args: str, payload: Object | None = None) -> str:
        """Run fixed kubectl arguments and JSON stdin, never commands from Base config."""
        result = subprocess.run(
            [*self.command, f"--request-timeout={WAIT}s", *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=WAIT + 5,
            check=False,
        )
        require(result.returncode == 0, f"kubectl {args[0]} failed; inspect status before retrying")
        return result.stdout

    def get(self, resource: str, name: str) -> Object:
        """Return an empty object only for confirmed absence, never for an API error."""
        raw = self.run("get", resource, name, "--ignore-not-found", "-o", "json")
        return json.loads(raw or "{}")

    def items(self, resource: str, *args: str) -> list[Object]:
        """Require a complete inventory before any cleanup decision."""
        result = json.loads(self.run("get", resource, *args, "-o", "json"))
        require(
            isinstance(result["items"], list) and not result.get("metadata", {}).get("continue"),
            "Incomplete Kubernetes inventory; backend retained",
        )
        return result["items"]

    def create(self, obj: Object) -> Object:
        """Create atomically; a conflict is not permission to update or adopt."""
        return json.loads(self.run("create", "-f", "-", "-o", "json", payload=obj))

    def delete(self, obj: Object, kind: str, name: str) -> None:
        """Delete only the owned object observed, including lock release."""
        owned(obj, kind, name)
        meta, (api, plural) = obj["metadata"], APIS[kind]
        require(meta.get("uid") and meta.get("resourceVersion"), "Missing deletion preconditions")
        prefix = "/api" if api == "v1" else "/apis"
        path = f"{prefix}/{api}/namespaces/{NAMESPACE}/{plural}/{name}"
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Background",
            "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]},
        }
        self.run("delete", f"--raw={path}", "-f", "-", payload=options)

    def contract(self) -> Object:
        """Trust Base's manifests after checking contract version, identities and scopes."""
        data = json.loads(self.get("configmap", "maintenance-runtime")["data"]["contract.json"])
        require(data["version"] == 1 and data["namespace"] == NAMESPACE, "Unsupported contract")
        require(
            "global" in data["routes"] and set(data["routes"]) <= set(SCOPES),
            "Unsupported maintenance scopes",
        )
        owned(data["deployment"], "Deployment", "maintenance")
        require(
            IMAGE.fullmatch(image(data["deployment"])),
            "Base must pin a versioned image digest",
        )
        for scope, route in data["routes"].items():
            owned(route, "IngressRoute", f"maintenance-{scope}")
            require(hosts(route), "Missing approved hosts")
        return data

    def references(self) -> list[Object]:
        """Retain the backend for any direct or unproven indirect Service reference."""
        return [
            route
            for route in self.items(ROUTES, "--all-namespaces")
            if any(
                service.get("kind", "Service") != "Service"
                or (
                    service["name"] == "maintenance"
                    and (service.get("namespace") or route["metadata"]["namespace"]) == NAMESPACE
                )
                for rule in route["spec"]["routes"]
                for service in rule["services"]
            )
        ]

    def ready(self) -> None:
        """Prove rollout and a ready Service endpoint before creating a route."""
        self.run("rollout", "status", "deployment/maintenance", f"--timeout={WAIT}s")
        deadline = time.monotonic() + WAIT
        while True:
            slices = self.items(
                "endpointslices.discovery.k8s.io",
                "--selector=kubernetes.io/service-name=maintenance",
            )
            if any(
                endpoint.get("conditions", {}).get("ready") is True
                and not endpoint.get("conditions", {}).get("terminating")
                and endpoint.get("addresses")
                for item in slices
                for endpoint in item.get("endpoints", [])
            ):
                return
            require(time.monotonic() < deadline, "No ready maintenance endpoint; routes unchanged")
            time.sleep(1)

    def on(self, data: Object, scope: str) -> None:
        """Reuse the backend unchanged or create it, then enable only the selected route."""
        route = self.get(ROUTES, f"maintenance-{scope}")
        if route:
            owned(route, "IngressRoute", f"maintenance-{scope}")
        deployment = self.get("deployment", "maintenance") or self.create(data["deployment"])
        owned(deployment, "Deployment", "maintenance")
        require(
            image(deployment) == image(data["deployment"]),
            "Live image differs from Base; turn off all scopes before changing image",
        )
        self.ready()
        if not route:
            self.create(data["routes"][scope])
        names = hosts(data["routes"][scope])
        verify(names, names)

    def off(self, data: Object, scope: str) -> None:
        """Remove one route; clean orphaned or idle backends only after convergence."""
        name = f"maintenance-{scope}"
        route = self.get(ROUTES, name)
        if route:
            self.delete(route, "IngressRoute", name)
        remaining = self.references()
        known = {f"maintenance-{key}": value for key, value in data["routes"].items()}
        require(
            all(
                item["metadata"]["namespace"] == NAMESPACE and item["metadata"]["name"] in known
                for item in remaining
            ),
            "Unknown maintenance reference; backend retained",
        )
        require(
            not any(item["metadata"]["name"] == name for item in remaining),
            "Route deletion pending; backend retained; retry off",
        )
        maintained = {host for item in remaining for host in hosts(known[item["metadata"]["name"]])}
        if remaining:
            verify(hosts(data["routes"][scope]), maintained)
            return
        names = hosts(data["routes"]["global"])
        verify(names, set())
        time.sleep(GRACE)
        verify(names, set())
        require(not self.references(), "Service references changed; backend retained")
        deployment = self.get("deployment", "maintenance")
        if deployment:
            self.delete(deployment, "Deployment", "maintenance")

    def execute(self, command: str, scope: str) -> None:
        """Serialize every on/off, including retries, without taking over stale locks."""
        if command == "status":
            report = {
                "routes": [
                    f"{r['metadata']['namespace']}/{r['metadata']['name']}"
                    for r in self.references()
                ],
                "deploymentPresent": bool(self.get("deployment", "maintenance")),
                "lockUID": self.get("configmap", LOCK).get("metadata", {}).get("uid"),
            }
            print(json.dumps(report))
            return
        metadata = {"name": LOCK, "namespace": NAMESPACE, "labels": LABELS}
        lock = self.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata})
        try:
            data = self.contract()
            require(scope in data["routes"], "Scope is not approved by Base")
            if command == "on":
                self.on(data, scope)
            else:
                self.off(data, scope)
        finally:
            self.delete(lock, "ConfigMap", LOCK)
        print(f"Maintenance {command} {scope} completed.")


def main(arguments: Sequence[str] | None = None) -> int:
    """Expose the kubectl plugin and alias without implicit context or automatic recovery."""
    parser = argparse.ArgumentParser(prog="kubectl maintenance")
    parser.add_argument("command", choices=("on", "off", "status"))
    parser.add_argument("scope", nargs="?", default="global", choices=SCOPES)
    parser.add_argument("--context", required=True)
    args = parser.parse_args(arguments)
    try:
        Maintenance(args.context).execute(args.command, args.scope)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, SubprocessError) as error:
        print(str(error) if isinstance(error, RuntimeError) else "Failed; inspect status")
        return 1
    return 0
