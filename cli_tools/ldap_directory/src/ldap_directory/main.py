"""Inspect the LDAP directory selected by a Kubernetes context."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import questionary
import requests
import yaml
from rich.console import Console
from rich.table import Table


class DiagnosticError(RuntimeError):
    """Operator-facing failure without command output or credentials."""


@dataclass(frozen=True)
class Target:
    """Selected live, non-secret LDAP settings."""

    context: str
    client: str
    settings: dict[str, object]


_IMAGE = "ghcr.io/neurwerk/k8s-stack-tooling"
_RELEASES = "https://api.github.com/repos/neurwerk/k8s_stack_tooling/releases?per_page=100"
_MINIMUM_WORKER_VERSION = (0, 7, 3)


def select_image() -> str:
    """Offer published worker images from the Tooling release's recorded digest."""
    try:
        response = requests.get(
            _RELEASES,
            headers={"Accept": "application/vnd.github+json"},
            timeout=(3.05, 15),
        )
        response.raise_for_status()
        releases: object = response.json()
    except (requests.RequestException, ValueError):
        raise DiagnosticError("Could not check published Tooling images on GitHub") from None
    if not isinstance(releases, list):
        raise DiagnosticError("GitHub returned an invalid Tooling release list")

    images: list[tuple[tuple[int, int, int], str]] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = release.get("tag_name")
        body = release.get("body")
        if (
            not isinstance(tag, str)
            or not isinstance(body, str)
            or not isinstance(release.get("published_at"), str)
            or release.get("draft") is not False
            or release.get("prerelease") is not False
        ):
            continue
        match = re.fullmatch(r"v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
        if not match:
            continue
        version = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if version < _MINIMUM_WORKER_VERSION:
            continue
        name = f"{_IMAGE}:{tag[1:]}"
        row = re.compile(
            rf"^\|\s*`{re.escape(name)}`\s*\|\s*"
            rf"`{re.escape(_IMAGE)}@sha256:([a-f0-9]{{64}})`\s*\|\s*$",
            re.MULTILINE,
        )
        digests = row.findall(body)
        if len(digests) == 1:
            images.append((version, f"{name}@sha256:{digests[0]}"))

    if not images:
        raise DiagnosticError("No published Tooling image includes the LDAP worker (needs 0.7.3+)")
    choices = [
        questionary.Choice(
            f"Tooling {'.'.join(map(str, version))} ({image.rsplit(':', 1)[1][:12]}…)",
            value=image,
        )
        for version, image in sorted(images, reverse=True)
    ]
    selected = questionary.select("Published Tooling image:", choices=choices).ask()
    if not isinstance(selected, str) or selected not in {image for _, image in images}:
        raise DiagnosticError("Cancelled")
    return selected


def kubectl(
    context: str | None, *args: str, input_text: str | None = None, timeout: int = 150
) -> str:
    """Run kubectl without a shell, suppressing possibly sensitive server errors."""
    command = ["kubectl"]
    if context is not None:
        command.extend(["--context", context])
    command.extend(args)
    try:
        result = subprocess.run(
            command, input=input_text, text=True, capture_output=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DiagnosticError("kubectl is unavailable or timed out") from None
    if result.returncode:
        raise DiagnosticError(f"kubectl {args[0]} failed for the selected context")
    return result.stdout


def resource(context: str, kind: str, name: str, namespace: str) -> dict[str, object]:
    """Fetch a named Kubernetes object, never a Secret value."""
    try:
        result = json.loads(kubectl(context, "-n", namespace, "get", kind, name, "-o", "json"))
    except (ValueError, TypeError):
        raise DiagnosticError("Kubernetes returned an invalid resource") from None
    if not isinstance(result, dict):
        raise DiagnosticError("Kubernetes returned an invalid resource")
    return cast(dict[str, object], result)


def data_map(resource_object: dict[str, object]) -> dict[str, str]:
    """Extract non-secret ConfigMap data."""
    data = resource_object.get("data")
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()
    ):
        raise DiagnosticError("Required ConfigMap data is missing")
    return cast(dict[str, str], data)


def target(context: str) -> Target:
    """Require the live context to have an enabled LDAP selector and identity."""
    identity = data_map(resource(context, "configmap", "neurwerk-stack-identity", "flux-system"))
    client = identity.get("client")
    if identity.get("schemaVersion") != "1" or not client or not identity.get("clusterId"):
        raise DiagnosticError("Selected context has no valid client identity")
    config = data_map(resource(context, "configmap", "keycloak-product-values", "auth-keycloak"))
    try:
        values = yaml.safe_load(config["values.yaml"])
        settings = values["authKeycloak"]["activeDirectory"]
    except (KeyError, TypeError, yaml.YAMLError):
        raise DiagnosticError("Keycloak LDAP settings are unavailable") from None
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        raise DiagnosticError("LDAP is not enabled for this client")
    required = ("connectionUrl", "usersDn", "groupsDn", "usernameAttribute", "egressCidrs")
    if any(not settings.get(field) for field in required):
        raise DiagnosticError("Enabled LDAP configuration is incomplete")
    cidrs = settings["egressCidrs"]
    if (
        not isinstance(cidrs, list)
        or not cidrs
        or any(not isinstance(cidr, str) or not re.fullmatch(r"[0-9./]+", cidr) for cidr in cidrs)
    ):
        raise DiagnosticError("LDAP destination CIDRs are invalid")
    try:
        for cidr in cidrs:
            ipaddress.IPv4Network(cidr, strict=True)
    except ValueError:
        raise DiagnosticError("LDAP destination CIDRs are invalid") from None
    url = settings["connectionUrl"]
    if not isinstance(url, str) or not re.fullmatch(r"ldaps://[^/:]+:636|ldap://[^/:]+:389", url):
        raise DiagnosticError("LDAP URL is invalid")
    if url.startswith("ldap://") and settings.get("allowInsecureLdap") is not True:
        raise DiagnosticError("Plain LDAP is not enabled for this client")
    return Target(context, client, settings)


def select_target(explicit: str | None) -> Target:
    """Show only reachable, LDAP-enabled contexts in the interactive menu."""
    if explicit:
        return target(explicit)
    names = kubectl(None, "config", "get-contexts", "-o", "name").splitlines()
    available: list[Target] = []
    for name in names:
        try:
            available.append(target(name))
        except DiagnosticError:
            continue
    if not available:
        raise DiagnosticError("No reachable Kubernetes context has LDAP enabled")
    selected = questionary.select(
        "LDAP context:",
        choices=[
            questionary.Choice(f"{item.context} ({item.client})", value=item) for item in available
        ],
    ).ask()
    if not isinstance(selected, Target):
        raise DiagnosticError("Cancelled")
    return selected


def pod_spec(item: Target, name: str, image: str) -> dict[str, object]:
    """Mount only the two OpenBao-backed bind fields, never send them to the workstation."""
    settings = item.settings
    url = str(settings["connectionUrl"])
    env: list[dict[str, object]] = [
        {"name": key, "value": str(settings[field])}
        for key, field in (
            ("LDAP_URL", "connectionUrl"),
            ("LDAP_USERS_DN", "usersDn"),
            ("LDAP_GROUPS_DN", "groupsDn"),
            ("LDAP_USERNAME_ATTRIBUTE", "usernameAttribute"),
        )
    ]
    env.append(
        {
            "name": "LDAP_ALLOW_INSECURE",
            "value": str(settings.get("allowInsecureLdap") is True).lower(),
        }
    )
    env.extend(
        {
            "name": variable,
            "valueFrom": {
                "secretKeyRef": {"name": "auth-keycloak-active-directory-secret", "key": key}
            },
        }
        for variable, key in (
            ("LDAP_BIND_DN", "activeDirectoryBindDn"),
            ("LDAP_BIND_CREDENTIAL", "activeDirectoryBindCredential"),
        )
    )
    volumes = []
    mounts = []
    if url.startswith("ldaps://"):
        volumes.append(
            {
                "name": "ldap-ca",
                "configMap": {
                    "name": settings.get("caConfigMapName", "auth-keycloak-active-directory-ca"),
                    "items": [{"key": settings.get("caKey", "ca.crt"), "path": "ca.crt"}],
                },
            }
        )
        mounts.append({"name": "ldap-ca", "mountPath": "/run/ldap-ca", "readOnly": True})
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "auth-keycloak", "labels": {"app": name}},
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 600,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "diagnostic",
                    "image": image,
                    "command": ["sleep", "540"],
                    "env": env,
                    "volumeMounts": mounts,
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "64Mi"},
                        "limits": {"cpu": "250m", "memory": "256Mi"},
                    },
                }
            ],
            "volumes": volumes,
        },
    }


def policy(name: str, uid: str, cidrs: list[str], port: int) -> dict[str, object]:
    """Allow DNS and only the selected LDAP CIDRs for this one Pod."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": name,
            "namespace": "auth-keycloak",
            "ownerReferences": [{"apiVersion": "v1", "kind": "Pod", "name": name, "uid": uid}],
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": name}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                },
                {
                    "to": [{"ipBlock": {"cidr": cidr}} for cidr in cidrs],
                    "ports": [{"protocol": "TCP", "port": port}],
                },
            ],
        },
    }


def show_results(item: Target, name: str, mode: str) -> None:
    """Run only the read-only worker and display its JSON output as tables."""
    raw = kubectl(
        item.context,
        "-n",
        "auth-keycloak",
        "exec",
        name,
        "-c",
        "diagnostic",
        "--",
        "ldap-directory-worker",
        mode,
        timeout=540,
    )
    found: dict[str, set[str]] = {"user": set(), "group": set()}
    try:
        for line in raw.splitlines():
            result = json.loads(line)
            kind, value = result["kind"], result["name"]
            if kind not in found or not isinstance(value, str):
                raise ValueError
            found[kind].add(value)
    except (ValueError, KeyError, TypeError):
        raise DiagnosticError("LDAP returned an invalid result") from None
    for kind in ("user", "group"):
        if mode not in ("both", f"{kind}s"):
            continue
        table = Table(title=f"{item.client}: {kind}s ({len(found[kind])})")
        table.add_column("#", justify="right")
        table.add_column("Username" if kind == "user" else "Group")
        for index, value in enumerate(sorted(found[kind], key=str.casefold), 1):
            table.add_row(str(index), value)
        Console().print(table)


def run(item: Target, image: str, mode: str) -> None:
    """Create narrow temporary resources and remove them even on failures."""
    name = f"ldap-directory-{uuid4().hex[:12]}"
    try:
        created = json.loads(
            kubectl(
                item.context,
                "create",
                "-f",
                "-",
                "-o",
                "json",
                input_text=json.dumps(pod_spec(item, name, image)),
            )
        )
        uid = created["metadata"]["uid"]
        settings = item.settings
        kubectl(
            item.context,
            "create",
            "-f",
            "-",
            input_text=json.dumps(
                policy(
                    name,
                    uid,
                    cast(list[str], settings["egressCidrs"]),
                    636 if str(settings["connectionUrl"]).startswith("ldaps://") else 389,
                )
            ),
        )
        kubectl(
            item.context,
            "-n",
            "auth-keycloak",
            "wait",
            "--for=condition=Ready",
            f"pod/{name}",
            "--timeout=120s",
        )
        show_results(item, name, mode)
    finally:
        for kind in ("pod", "networkpolicy"):
            try:
                kubectl(
                    item.context,
                    "-n",
                    "auth-keycloak",
                    "delete",
                    kind,
                    name,
                    "--ignore-not-found",
                    "--wait=false",
                )
            except DiagnosticError:
                print(f"Cleanup needed: {kind} auth-keycloak/{name}", file=sys.stderr)


def main() -> int:
    """Select a live LDAP context and print complete user and group inventories."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", help="Kubernetes context; otherwise choose from the menu")
    args = parser.parse_args()
    try:
        item = select_target(args.context)
        image = select_image()
        mode = questionary.select("Show:", choices=["Users", "Groups", "Both"]).ask()
        if mode is None:
            return 0
        run(item, image, mode.lower())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        return 130
    except DiagnosticError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
