"""Search the same fluent-bit indices used by the Studio logs viewer."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import re
import selectors
import shutil
import socket
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any

NAMESPACE = "monitor-opensearch"
SERVICES = ("opensearch-v3-master", "opensearch-cluster-master")
USER = "studio-logs-read"
INDEX = "fluent-bit-*"
FORWARD_TIMEOUT = 15


class SearchError(Exception):
    """A safe, actionable error without log contents or credentials."""


def kubectl(context: str, *args: str, namespace: str | None = None) -> str:
    """Read cluster resources through one selected context."""
    command = ["kubectl", f"--context={context}", "--request-timeout=15s"]
    if namespace:
        command.extend(["-n", namespace])
    try:
        result = subprocess.run(
            [*command, *args], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SearchError("kubectl is unavailable or timed out") from exc
    if result.returncode:
        raise SearchError(f"kubectl {args[0]} failed in context {context}; check access")
    return result.stdout


def resource(context: str, kind: str, name: str) -> dict[str, Any]:
    """Fetch a single named resource, without printing Secret data."""
    return json.loads(kubectl(context, "get", kind, name, "-o", "json", namespace=NAMESPACE))


def selected_context(explicit: str | None) -> str:
    """Resolve and report the context before reading cluster resources."""
    if explicit:
        try:
            result = subprocess.run(
                ["kubectl", "config", "get-contexts", "-o", "name"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SearchError("Cannot check Kubernetes contexts") from exc
        if result.returncode:
            raise SearchError("Cannot check Kubernetes contexts")
        if explicit not in result.stdout.splitlines():
            raise SearchError(f"Unknown Kubernetes context: {explicit}")
        return explicit
    try:
        result = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SearchError("Cannot determine the current Kubernetes context") from exc
    if result.returncode or not result.stdout.strip():
        raise SearchError("No current Kubernetes context; specify --context")
    return result.stdout.strip()


def service(context: str) -> str:
    """Choose only a known OpenSearch service with a port numbered 9200."""
    items = json.loads(kubectl(context, "get", "services", "-o", "json", namespace=NAMESPACE))[
        "items"
    ]
    for name in SERVICES:
        for item in items:
            if item["metadata"]["name"] == name and any(
                port.get("port") == 9200 for port in item["spec"].get("ports", [])
            ):
                return name
    raise SearchError("No supported OpenSearch service on port 9200 in monitor-opensearch")


def credentials(context: str) -> tuple[str, str]:
    """Load only the read-only password and public trust bundle into memory."""
    secret = resource(context, "secret", "monitor-opensearch-secret")
    ca = resource(context, "configmap", "monitor-opensearch-ca-trust")
    try:
        password = base64.b64decode(secret["data"]["studioPassword"], validate=True).decode()
        certificate = ca["data"]["ca.crt"]
    except (KeyError, ValueError, UnicodeError) as exc:
        raise SearchError("Missing Studio read-only password or OpenSearch CA") from exc
    if not password or not certificate:
        raise SearchError("Missing Studio read-only password or OpenSearch CA")
    return password, certificate


def start_forward(context: str, name: str) -> tuple[subprocess.Popen[str], int]:
    """Start a loopback-only forward and read kubectl's randomly selected port."""
    try:
        process = subprocess.Popen(
            [
                "kubectl",
                f"--context={context}",
                "-n",
                NAMESPACE,
                "port-forward",
                "--address=127.0.0.1",
                f"service/{name}",
                ":9200",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise SearchError("Could not start kubectl port-forward") from exc
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + FORWARD_TIMEOUT
            while time.monotonic() < deadline:
                if not selector.select(timeout=min(0.5, deadline - time.monotonic())):
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) -> 9200", line)
                if match:
                    return process, int(match.group(1))
                if not line and process.poll() is not None:
                    break
        raise SearchError("kubectl port-forward failed or timed out")
    except BaseException:
        stop_forward(process)
        raise


def stop_forward(process: subprocess.Popen[str]) -> None:
    """Release the temporary port-forward on success, error, or interruption."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stdout:
        process.stdout.close()


class ForwardedHTTPS(http.client.HTTPSConnection):
    """Connect to loopback while checking the service's real TLS hostname."""

    def __init__(self, hostname: str, port: int, ca: str) -> None:
        self.forwarded_port = port
        self.tls = ssl.create_default_context(cadata=ca)
        super().__init__(hostname, 9200, timeout=15)

    def connect(self) -> None:
        """Connect only to the local forward, with service DNS for SNI and verification."""
        sock = socket.create_connection(("127.0.0.1", self.forwarded_port), timeout=self.timeout)
        try:
            self.sock = self.tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def utc_time(value: str) -> datetime:
    """Accept ISO 8601 timestamps explicitly in UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected an ISO 8601 UTC time, e.g. 2026-10-01T09:00:00Z"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("timestamp must include UTC (Z or +00:00)")
    return parsed.astimezone(UTC)


def duration(value: str) -> timedelta:
    """Parse a positive duration with s, m, h, or d units."""
    match = re.fullmatch(r"([1-9]\d*)([smhd])", value)
    if not match:
        raise argparse.ArgumentTypeError("expected a positive duration such as 30m, 1h, or 2d")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    try:
        return timedelta(**{unit: int(match.group(1))})
    except OverflowError as exc:
        raise argparse.ArgumentTypeError("duration is too large") from exc


def search_body(args: argparse.Namespace) -> dict[str, Any]:
    """Use Studio's filters, optionally bounding either end of the time range."""
    start: datetime | None = args.starts_at_utc
    since: timedelta | None = args.since
    end: datetime | None = args.ends_at_utc
    if start is None and since is not None:
        start = datetime.now(UTC) - since
    if start and end and start >= end:
        raise SearchError("--starts-at-utc / --since must be before --ends-at-utc")
    filters: list[dict[str, Any]] = []
    if args.namespace:
        filters.append({"term": {"kubernetes.namespace_name.keyword": args.namespace}})
    if args.pod:
        filters.append({"term": {"kubernetes.pod_name.keyword": args.pod}})
    if args.level == "UNKNOWN":
        filters.append(
            {
                "bool": {
                    "must_not": [
                        {
                            "terms": {
                                "stack_log.level.keyword": [
                                    "TRACE",
                                    "DEBUG",
                                    "INFO",
                                    "WARNING",
                                    "ERROR",
                                    "FATAL",
                                ]
                            }
                        }
                    ]
                }
            }
        )
    elif args.level:
        filters.append({"term": {"stack_log.level.keyword": args.level}})
    if args.failure_type:
        filters.append({"term": {"stack_log.failure_type.keyword": args.failure_type}})
    bounds: dict[str, str] = {}
    if start:
        bounds["gte"] = start.isoformat()
    if end:
        bounds["lte"] = end.isoformat()
    if bounds:
        filters.append({"range": {"@timestamp": bounds}})
    query = (
        {"query_string": {"query": args.query, "default_field": "log"}}
        if args.query
        else {"match_all": {}}
    )
    return {
        "size": args.limit,
        "from": args.offset,
        "sort": [{"@timestamp": {"order": "desc"}}],
        "track_total_hits": True,
        "query": {"bool": {"must": [query], "filter": filters}},
    }


def search(
    hostname: str, port: int, ca: str, password: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Execute a read-only search and return Studio-shaped log entries."""
    try:
        connection = ForwardedHTTPS(hostname, port, ca)
    except (ValueError, ssl.SSLError) as exc:
        raise SearchError("OpenSearch CA is invalid") from exc
    auth = base64.b64encode(f"{USER}:{password}".encode()).decode()
    try:
        connection.request(
            "POST",
            f"/{INDEX}/_search",
            body=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise SearchError(
                f"OpenSearch returned HTTP {response.status}; check access and filters"
            )
        data = json.loads(response.read())
    except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as exc:
        raise SearchError("OpenSearch search failed; check service health and TLS") from exc
    finally:
        connection.close()
    hits = []
    for hit in data["hits"]["hits"]:
        source = hit["_source"]
        kubernetes = source.get("kubernetes") or {}
        classification = source.get("stack_log") or {}
        hits.append(
            {
                "timestamp": source.get("@timestamp", ""),
                "log": source.get("log", ""),
                "namespace": kubernetes.get("namespace_name", ""),
                "pod": kubernetes.get("pod_name", ""),
                "container": kubernetes.get("container_name", ""),
                "level": classification.get("level", "UNKNOWN"),
                "failure_type": classification.get("failure_type"),
                "index": hit.get("_index", ""),
            }
        )
    return {"total": data["hits"]["total"]["value"], "hits": hits}


def parser() -> argparse.ArgumentParser:
    """Provide fully optional flags for human and automated callers."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--context", help="Kubernetes context (default: current context)")
    result.add_argument("--namespace", help="Exact Kubernetes namespace")
    result.add_argument("--pod", help="Exact Kubernetes pod name")
    result.add_argument("--query", help="OpenSearch query_string expression against the log field")
    result.add_argument(
        "--level", choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "FATAL", "UNKNOWN"]
    )
    result.add_argument("--failure-type", help="Exact collector failure type")
    result.add_argument("--since", type=duration, help="Relative start time, e.g. 30m, 1h, 2d")
    result.add_argument("--starts-at-utc", type=utc_time, help="Inclusive UTC start (ISO 8601)")
    result.add_argument("--ends-at-utc", type=utc_time, help="Inclusive UTC end (ISO 8601)")
    result.add_argument(
        "--limit", type=int, default=100, help="Max entries (default: 100; max: 1000)"
    )
    result.add_argument("--offset", type=int, default=0, help="Skip entries (default: 0)")
    result.add_argument("--json", action="store_true", help="Output JSON (the default)")
    return result


def main() -> int:
    """Read indexed logs without modifying the cluster or disclosing credentials."""
    args = parser().parse_args()
    if args.limit < 1 or args.limit > 1000 or args.offset < 0 or args.offset + args.limit > 10000:
        raise SystemExit("--limit must be 1–1000 and --offset + --limit must be at most 10000")
    try:
        body = search_body(args)
        if not shutil.which("kubectl"):
            raise SearchError("kubectl is required")
        context = selected_context(args.context)
        print(f"Searching OpenSearch in Kubernetes context: {context}", file=sys.stderr)
        name = service(context)
        password, ca = credentials(context)
        process, port = start_forward(context, name)
        try:
            results = search(f"{name}.{NAMESPACE}.svc.cluster.local", port, ca, password, body)
        finally:
            stop_forward(process)
        print(json.dumps({"context": context, **results}, ensure_ascii=False))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except SearchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
