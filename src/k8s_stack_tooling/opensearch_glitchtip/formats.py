"""Opt-in, versioned source formats; unrecognized content stays exact."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime

_PREFIX = re.compile(
    r"(?P<timestamp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"
    r"[ \t]+(?P<level>error)[ \t]+request "
)
_FIELD = re.compile(r'([A-Za-z_][\w.-]*)=("(?:[^"\\]|\\.)*"|[^\s"=]+)(?: |$)')
_CORE = {
    "timestamp",
    "level",
    "gateway",
    "listener",
    "route",
    "src.addr",
    "http.method",
    "http.host",
    "http.path",
    "http.version",
    "http.status",
    "trace.id",
    "span.id",
    "protocol",
    "error",
    "reason",
    "duration",
}
_OCCURRENCE = {"timestamp", "trace.id", "span.id", "duration", "neurwerk.request_id"}
_NOISE_METADATA = {
    "neurwerk.billing_classification": "unknown",
    "neurwerk.cost_known": "false",
    "neurwerk.credential_mode": "none",
    "neurwerk.destination_kind": "mcp",
    "neurwerk.dispatch_attempted": "true",
    "neurwerk.entry_mode": "api",
    "neurwerk.ignored_proxy_credential_headers": "[]",
    "neurwerk.operation_id": "exchange",
    "neurwerk.protocol_profile": "mcp-streamable-http-v1",
    "neurwerk.provider_credential_source": "none",
    "neurwerk.request_outcome": "gateway_error",
    "neurwerk.request_text_inspection": "unknown",
    "neurwerk.stripped_credential_headers": "[]",
    "neurwerk.target_credential_mode": "unknown",
    "neurwerk.token_usage_known": "false",
}
_NOISE_IDENTITIES = {
    "jwt.sub",
    "neurwerk.app_id",
    "neurwerk.binding_id",
    "neurwerk.matched_route",
    "neurwerk.policy_revision",
    "neurwerk.request_id",
}


def source_ip(address: str) -> str:
    host, port = address.rsplit(":", 1)
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid source port")
    bracketed = host.startswith("[") and host.endswith("]")
    ip = ipaddress.ip_address(host[1:-1] if bracketed else host)
    if (ip.version == 6) != bracketed:
        raise ValueError("invalid source address envelope")
    return str(ip)


def gateway_mcp(message: str) -> dict[str, str] | None:
    """Consume the whole access record, rejecting ambiguous or incomplete envelopes."""
    prefix = _PREFIX.match(message)
    if prefix is None or any(
        ord(c) < 32 and c != "\t" or ord(c) >= 127 and ord(c) < 160 for c in message
    ):
        return None
    fields = prefix.groupdict()
    position = prefix.end()
    try:
        datetime.fromisoformat(fields["timestamp"])
        while position < len(message):
            match = _FIELD.match(message, position)
            if match is None:
                return None
            key, value = match.groups()
            if any(key == k or key.startswith(k + ".") or k.startswith(key + ".") for k in fields):
                return None
            fields[key] = json.loads(value) if value.startswith('"') else value
            fields[key].encode("utf-8")
            if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in fields[key]):
                return None
            position = match.end()
        if not _CORE <= fields.keys() or any(not v for v in fields.values()):
            return None
        source_ip(fields["src.addr"])
        if not re.fullmatch(r"/mcp/[a-zA-Z0-9_-]+", fields["http.path"]):
            return None
        checks = {
            "http.method": r"GET|POST|DELETE|PUT|PATCH|HEAD|OPTIONS",
            "http.status": r"[1-5][0-9]{2}",
            "http.version": r"HTTP/(?:1\.0|1\.1|2|2\.0|3|3\.0)",
            "trace.id": r"[a-f0-9]{32}",
            "span.id": r"[a-f0-9]{16}",
            "duration": r"[0-9]+(?:\.[0-9]+)?(?:ns|µs|us|ms|s)",
            "neurwerk.request_id": r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",
        }
        if any(
            k in fields and not re.fullmatch(pattern, fields[k]) for k, pattern in checks.items()
        ):
            return None
    except (ValueError, OverflowError):
        return None
    return fields


def gateway_identity(fields: dict[str, str]) -> dict[str, str]:
    normalized = {k: v for k, v in fields.items() if k not in _OCCURRENCE}
    normalized["src.addr"] = source_ip(fields["src.addr"])
    return normalized


def stateless_get_noise(fields: dict[str, str]) -> bool:
    """Additional exclusion gate; unknown semantics always remain reportable."""
    destination = fields["http.path"].removeprefix("/mcp/")
    namespace = fields["gateway"].split("/")[0]
    return (
        fields.keys() <= _CORE | _NOISE_IDENTITIES | _NOISE_METADATA.keys()
        and fields["http.method"] == "GET"
        and fields["http.status"] == "405"
        and fields["protocol"] == "mcp"
        and fields["reason"] == "MCP"
        and fields["error"] == "mcp: method not allowed; must be GET, POST, or DELETE"
        and fields["listener"] == "api-bindings"
        and fields["route"] == f"{namespace}/mcp-{destination}-route"
        and all(fields.get(k) == v for k, v in _NOISE_METADATA.items())
    )
