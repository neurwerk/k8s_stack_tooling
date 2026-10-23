"""Validate the deliberately small, secret-safe runtime contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

INDEX = "fluent-bit-*"
MAX_RECORDS = 1000
MAX_MESSAGE_BYTES = 65536
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PENDING = 10000


def utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != UTC.utcoffset(result):
        raise ValueError("timestamp must have an explicit UTC offset")
    return result.astimezone(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value):
        raise ValueError("invalid scope identifier")
    return value


@dataclass(frozen=True)
class Scope:
    id: str
    application: str
    service: str
    namespaces: tuple[str, ...]
    containers: tuple[str, ...]
    failure_types: tuple[str, ...]
    release: str | None = None

    def binding(self) -> dict[str, object]:
        return {
            "id": self.id,
            "application": self.application,
            "service": self.service,
            "namespaces": sorted(self.namespaces),
            "containers": sorted(self.containers),
            "failure_types": sorted(self.failure_types),
        }


def load_scopes(path: Path) -> tuple[Scope, ...]:
    if path.stat().st_size > 65536:
        raise ValueError("scope configuration too large")
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not 1 <= len(data) <= 32:
        raise ValueError("scopes must be a nonempty bounded array")
    scopes = []
    for item in data:
        required = {"id", "application", "service", "namespaces", "containers", "failure_types"}
        if not isinstance(item, dict) or not required <= item.keys():
            raise ValueError("missing scope fields")
        if item.keys() - required - {"release"}:
            raise ValueError("unknown scope fields")
        groups: dict[str, tuple[str, ...]] = {}
        for name in ("namespaces", "containers", "failure_types"):
            values = item[name]
            if not isinstance(values, list) or not 1 <= len(values) <= 32:
                raise ValueError("scope selections must be nonempty bounded arrays")
            groups[name] = tuple(sorted({identifier(value) for value in values}))
        release = item.get("release")
        if release is not None:
            release = identifier(release)
        scopes.append(
            Scope(
                id=identifier(item["id"]),
                application=identifier(item["application"]),
                service=identifier(item["service"]),
                release=release,
                **groups,
            )
        )
    if len({scope.id for scope in scopes}) != len(scopes):
        raise ValueError("duplicate scope IDs")
    # Overlapping selections would count one source occurrence more than once.
    for position, left in enumerate(scopes):
        for right in scopes[position + 1 :]:
            if all(
                set(getattr(left, field)) & set(getattr(right, field))
                for field in ("namespaces", "containers", "failure_types")
            ):
                raise ValueError("overlapping scope selections")
    return tuple(scopes)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADAPTER_", hide_input_in_errors=True)

    opensearch_url: str
    opensearch_username: str
    opensearch_password: SecretStr
    opensearch_ca_file: Path
    glitchtip_dsn: SecretStr
    glitchtip_ingest_base_url: str | None = None
    glitchtip_ca_file: Path | None = None
    environment: str
    scopes_file: Path
    state_path: Path = Path("/var/lib/opensearch-glitchtip/state.sqlite3")
    initial_start_at: str
    poll_seconds: int = Field(default=60, ge=1, le=3600)
    overlap_seconds: int = Field(default=900, ge=0, le=86400)
    settle_seconds: int = Field(default=30, ge=0, le=3600)

    def validate_runtime(self) -> tuple[Scope, ...]:
        identifier(self.environment)
        utc(self.initial_start_at)
        source = urlsplit(self.opensearch_url)
        if (
            source.scheme != "https"
            or not source.hostname
            or source.username
            or source.password
            or source.path not in ("", "/")
            or source.query
            or source.fragment
        ):
            raise ValueError("OpenSearch URL must be an HTTPS origin")
        destination(self.glitchtip_dsn.get_secret_value(), self.glitchtip_ingest_base_url)
        if not self.opensearch_username or not self.opensearch_password.get_secret_value():
            raise ValueError("reader credentials are required")
        for path in (self.opensearch_ca_file, self.glitchtip_ca_file):
            if path is not None and (not path.is_absolute() or not path.is_file()):
                raise ValueError("CA bundle must be an existing absolute file")
        if not self.state_path.is_absolute():
            raise ValueError("state path must be absolute")
        return load_scopes(self.scopes_file)


def destination(dsn: str, ingest_base_url: str | None = None) -> tuple[str, str, str]:
    parsed = urlsplit(dsn)
    # Plain HTTP is supported only for a Kubernetes-internal GlitchTip Service.
    internal = bool(parsed.hostname and parsed.hostname.endswith(".svc.cluster.local"))
    if (
        parsed.scheme not in (("https", "http") if internal else ("https",))
        or not parsed.hostname
        or not parsed.username
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", parsed.username)
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[1-9][0-9]{0,18}", parsed.path)
    ):
        raise ValueError("invalid GlitchTip project DSN")
    host = parsed.hostname
    if parsed.port is not None:
        host += f":{parsed.port}"
    origin = f"{parsed.scheme}://{host}"
    project = parsed.path[1:]
    ingest_origin = origin
    if ingest_base_url is not None:
        # Operator-owned routing only: no credentials, arbitrary paths or public hosts.
        # Keep the native DSN as the credential/project and durable-state identity.
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        if not re.fullmatch(
            rf"https?://{label}\.{label}\.svc\.cluster\.local:8000/?", ingest_base_url
        ):
            raise ValueError("GlitchTip ingestion base URL must be a Service origin on port 8000")
        ingest_origin = ingest_base_url.rstrip("/")
    return f"{ingest_origin}/api/{project}/store/", parsed.username, f"{origin}/{project}"
