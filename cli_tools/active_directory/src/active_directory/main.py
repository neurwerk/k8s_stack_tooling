"""Run bounded LDAP reads inside the existing Keycloak Pod."""

import argparse
import base64
import binascii
import json
import subprocess
import sys
import unicodedata
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

NAMESPACE = "auth-keycloak"
POD = "auth-keycloak-keycloak-stateful-set-0"
MARKER = "NEURWERK_AD_REPORT:"
PERSON_FILTER = (
    "(&(objectCategory=person)(objectClass=user)(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
)


class ReportError(Exception):
    """A diagnostic that is safe to print without captured credentials."""


def run(arguments: list[str], *, input_text: str | None = None, timeout: int = 20) -> str:
    """Capture subprocess output without forwarding potentially sensitive errors."""
    try:
        result = subprocess.run(
            arguments, input=input_text, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReportError(
            "Command unavailable or timed out; check kubectl and the tunnel."
        ) from None
    if result.returncode:
        raise ReportError("Kubernetes read/exec failed; check the tunnel, permissions and Pod.")
    return result.stdout


class Cluster:
    def __init__(self, context: str | None) -> None:
        self.context = context or run(["kubectl", "config", "current-context"]).strip()
        if not self.context:
            raise ReportError("Select a Kubernetes context with --context.")
        self.command = ["kubectl", "--context", self.context, "--request-timeout=15s"]

    def get(self, kind: str, name: str, namespace: str = NAMESPACE) -> dict[str, Any]:
        return json.loads(run([*self.command, "get", kind, name, "-n", namespace, "-o", "json"]))


def merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Apply Helm's map layering; replace scalar and list values."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = value


def settings(cluster: Cluster) -> dict[str, Any]:
    """Read ordered Helm inputs instead of relying on an expiring hook Job."""
    release = cluster.get("helmrelease", "keycloak-active-directory")
    values: dict[str, Any] = {}
    for ref in release["spec"].get("valuesFrom", []):
        if ref.get("targetPath") or ref.get("optional"):
            raise ReportError("Unsupported Helm valuesFrom targetPath/optional input.")
        kind = ref.get("kind", "Secret")
        if kind not in {"Secret", "ConfigMap"}:
            raise ReportError("Unsupported Helm value source.")
        data = cluster.get(kind, ref["name"])["data"][ref.get("valuesKey", "values.yaml")]
        if kind == "Secret":
            data = base64.b64decode(data, validate=True).decode()
        layer = yaml.safe_load(data)
        if not isinstance(layer, dict):
            raise ReportError("Helm input must be a YAML mapping.")
        merge(values, layer)
    merge(values, release["spec"].get("values", {}))
    ad = values["authKeycloak"]["activeDirectory"]
    if ad.get("enabled") is not True:
        raise ReportError("Active Directory is not enabled in the selected client configuration.")
    parsed = urlsplit(ad["connectionUrl"])
    secure = parsed.scheme == "ldaps" and parsed.port == 636
    plain = parsed.scheme == "ldap" and parsed.port == 389 and ad.get("allowInsecureLdap") is True
    if not (secure or plain) or not parsed.hostname or parsed.username or parsed.password:
        raise ReportError("Expected configured LDAPS:636 or explicitly enabled LDAP:389.")
    if parsed.path or parsed.query or parsed.fragment:
        raise ReportError("LDAP URL must not contain a path, query or fragment.")
    legacy, mappings = ad.get("groupNames", []), ad.get("groupMappings", [])
    if bool(legacy) == bool(mappings):
        raise ReportError("Expected exactly one configured group list.")
    mappings = mappings or [
        {"sourceName": name, "targetParent": f"/access/{name}"} for name in legacy
    ]
    if not all(
        isinstance(m, dict) and m.get("sourceName") and m.get("targetParent") for m in mappings
    ):
        raise ReportError("Invalid directory group mappings.")
    secret = cluster.get("secret", "auth-keycloak-active-directory-secret")["data"]
    ca = ""
    if secure:
        ca = cluster.get(
            "configmap", ad.get("caConfigMapName", "auth-keycloak-active-directory-ca")
        )["data"][ad.get("caKey", "ca.crt")]
        if not ca.strip():
            raise ReportError("The configured LDAP CA is empty.")
    return {
        "url": ad["connectionUrl"],
        "usersDn": ad["usersDn"],
        "groupsDn": ad["groupsDn"],
        "usernameAttribute": ad.get("usernameAttribute", "sAMAccountName"),
        "mappings": mappings,
        "bindDn": base64.b64decode(secret["activeDirectoryBindDn"], validate=True).decode(),
        "password": base64.b64decode(
            secret["activeDirectoryBindCredential"], validate=True
        ).decode(),
        "ca": ca,
    }


def java_string(value: str) -> str:
    """Keep all caller and directory inputs out of executable Java syntax."""
    encoded = base64.b64encode(value.encode()).decode()
    return f'new String(Base64.getDecoder().decode("{encoded}"), StandardCharsets.UTF_8)'


def query(cluster: Cluster, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Send a read-only JNDI program via stdin; print no raw JShell output."""
    pod = cluster.get("pod", POD)
    if not any(
        c["name"] == "keycloak" and c.get("ready")
        for c in pod.get("status", {}).get("containerStatuses", [])
    ):
        raise ReportError("The existing Keycloak container is not Ready.")
    source = files("active_directory").joinpath("report.jsh").read_text()
    base = args.base_dn or config["usersDn"]
    ldap_filter = f"(&{PERSON_FILTER}{args.filter})" if args.filter else PERSON_FILTER
    group_args = ",".join(java_string(m["sourceName"]) for m in config["mappings"])
    target_args = ",".join(java_string(m["targetParent"]) for m in config["mappings"])
    arguments = [
        config["url"],
        config["bindDn"],
        config["password"],
        config["ca"],
        base,
        config["groupsDn"],
        config["usernameAttribute"],
        ldap_filter,
    ]
    source += "\nDirectoryReport.execute(" + ",".join(java_string(v) for v in arguments)
    source += f",new String[]{{{group_args}}},new String[]{{{target_args}}},{args.limit});\n/exit\n"
    output = run(
        [
            *cluster.command,
            "exec",
            "-i",
            "-n",
            NAMESPACE,
            POD,
            "-c",
            "keycloak",
            "--",
            "java",
            "-XX:-UsePerfData",
            "-Dcom.sun.jndi.ldap.object.disableEndpointIdentification=false",
            "-m",
            "jdk.jshell/jdk.internal.jshell.tool.JShellToolProvider",
            "--execution",
            "local",
            "--feedback",
            "silent",
            "--no-startup",
            "-",
        ],
        input_text=source,
        timeout=180,
    )
    reports = [line.removeprefix(MARKER) for line in output.splitlines() if line.startswith(MARKER)]
    if len(reports) != 1:
        raise ReportError("No complete diagnostic response; Keycloak needs JDK 21 with JShell.")
    report = json.loads(reports[0])
    if report.get("error"):
        raise ReportError(f"LDAP read failed ({report['error']}); no complete account report.")
    return report


def clean(value: str) -> str:
    """Prevent directory strings from injecting terminal control sequences."""
    return "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)


def display(report: dict[str, Any], args: argparse.Namespace) -> None:
    rows = report["users"]
    report["accountCount"] = len(rows)
    report["unassignedCount"] = sum(not row["groups"] for row in rows)
    report["users"] = sorted(
        (row for row in rows if not args.unassigned_only or not row["groups"]),
        key=lambda row: ((row["name"] or row["username"]).casefold(), row["username"].casefold()),
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return
    console = Console(markup=False, highlight=False)
    console.print("Directory access", style="bold cyan")
    console.print(f"{clean(report['client'])}  ·  {clean(report['context'])}")
    console.print(f"Scope: {clean(report['baseDn'])}", style="dim")
    if report["warnings"]:
        console.print(
            Panel(
                Text("\n".join(clean(warning) for warning in report["warnings"])),
                title="Incomplete report",
                border_style="yellow",
            )
        )
    table = Table(box=box.SIMPLE_HEAVY, expand=True, header_style="bold", padding=(0, 1))
    table.add_column("Account", ratio=3, overflow="fold")
    table.add_column("Email", ratio=3, overflow="fold")
    table.add_column("Access", width=10, overflow="fold")
    table.add_column("Neurwerk groups", ratio=3, overflow="fold")
    table.add_column("Other AD groups", ratio=3, overflow="fold")
    for row in report["users"]:
        account = Text(clean(row["name"] or row["username"]), style="bold")
        if row["name"] and row["name"] != row["username"]:
            account.append("\n" + clean(row["username"]), style="dim")
        groups = "\n".join(clean(group.removeprefix("/access/")) for group in row["groups"])
        other = "\n".join(sorted(clean(group["name"]) for group in row["otherGroups"]))
        table.add_row(
            account,
            Text(clean(row["email"]) or "—"),
            Text(
                "Assigned" if row["groups"] else "Unassigned",
                style="green" if row["groups"] else "yellow",
            ),
            Text(groups or "—"),
            Text(other or "—"),
        )
    console.print(table)
    console.print(
        f"{report['accountCount']} accounts  ·  {report['unassignedCount']} unassigned"
        f"  ·  {len(report['users'])} shown",
        style="bold",
    )
    console.print(
        "Visible group assignments only; not invitation, login or effective-access history.",
        style="dim",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["report"])
    parser.add_argument("--context", help="Kubernetes context; defaults to current-context.")
    parser.add_argument(
        "--base-dn", help="Employee OU/search base; defaults to configured usersDn."
    )
    parser.add_argument(
        "--filter", help="Additional LDAP filter, ANDed with enabled person accounts."
    )
    parser.add_argument(
        "--limit", type=int, default=10000, help="Maximum accounts (default 10000)."
    )
    parser.add_argument("--unassigned-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100000:
        parser.error("--limit must be between 1 and 100000")
    try:
        cluster = Cluster(args.context)
        identity = cluster.get("configmap", "neurwerk-stack-identity", "flux-system")["data"]
        config = settings(cluster)
        report = query(cluster, config, args)
        report.update(
            client=identity["client"],
            context=cluster.context,
            baseDn=args.base_dn or config["usersDn"],
        )
        display(report, args)
        return 2 if report["warnings"] else 0
    except (ReportError, ValueError, KeyError, TypeError, binascii.Error, yaml.YAMLError) as exc:
        message = (
            str(exc) if isinstance(exc, ReportError) else "Invalid or missing cluster report input."
        )
        print("ERROR: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
