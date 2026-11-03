"""Versioned, configuration-owned classification; BRAIN never supplies these rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .display import display_message
from .grouping import grouping_parts
from .settings import Scope

if TYPE_CHECKING:
    from .state import Record

MAX_POLICY_BYTES = 262144
CONTEXT_FIELDS = {
    "environment",
    "application",
    "service",
    "namespace",
    "container",
    "failure_type",
    "level",
}
# These names require explicit, format-specific review before removal. Even "time"
# may describe a duration rather than an occurrence timestamp.
OCCURRENCE_FIELDS = {
    "timestamp",
    "@timestamp",
    "time",
    "request_id",
    "requestId",
    "trace_id",
    "traceId",
    "span_id",
    "spanId",
    "operation_id",
    "operationId",
}
DEFAULT_GROUP_BY = ["json.operation", "json.target", "json.stage", "json.code", "json.cause"]
Scalar = str | int | float | bool | None
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def field_path(value: str) -> str:
    if value not in CONTEXT_FIELDS and not re.fullmatch(r"json\.[A-Za-z_@][\w.@-]{0,127}", value):
        raise ValueError("unknown reporting field path")
    return value


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(text: str) -> Any:
    def invalid_constant(_value: str) -> None:
        raise ValueError("nonfinite JSON value")

    return json.loads(text, object_pairs_hook=strict_object, parse_constant=invalid_constant)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
    enabled: bool = True
    priority: int = Field(default=0, ge=-1000, le=1000)
    match: dict[str, list[Scalar]] = Field(min_length=1, max_length=32)
    action: Literal["report", "log_only"] = "report"
    group_by: list[str] = Field(
        default_factory=lambda: list(DEFAULT_GROUP_BY), min_length=1, max_length=32
    )
    drop_fields: list[str] = Field(default_factory=list, max_length=32)
    count_unit: Literal["error_events", "failed_operations"] = "error_events"
    title: str | None = Field(default=None, min_length=1, max_length=512)
    explanation: str = Field(
        default=(
            "Matching observed failures; a shared signature does not prove a shared root cause."
        ),
        max_length=2048,
    )
    level: Literal["debug", "info", "warning", "error", "fatal"] | None = None

    @field_validator("match")
    @classmethod
    def valid_match(cls, value: dict[str, list[Scalar]]) -> dict[str, list[Scalar]]:
        for path, choices in value.items():
            field_path(path)
            if not 1 <= len(choices) <= 32 or any(len(canonical(v)) > 1024 for v in choices):
                raise ValueError("invalid reporting match choices")
        return value

    @field_validator("group_by")
    @classmethod
    def valid_group_by(cls, value: list[str]) -> list[str]:
        for path in value:
            field_path(path)
        return value

    @field_validator("drop_fields")
    @classmethod
    def valid_drop_fields(cls, value: list[str]) -> list[str]:
        if any(path not in OCCURRENCE_FIELDS for path in value):
            raise ValueError("only known occurrence envelope fields may be removed")
        return value

    @model_validator(mode="after")
    def preserve_grouping_fields(self) -> Self:
        for path in self.group_by:
            if any(
                path == f"json.{name}" or path.startswith(f"json.{name}.")
                for name in self.drop_fields
            ):
                raise ValueError("grouping fields cannot also be dropped")
        return self

    @field_validator("title", "explanation")
    @classmethod
    def valid_template(cls, value: str | None) -> str | None:
        if value is not None:
            for path in _PLACEHOLDER.findall(value):
                field_path(path)
            rest = _PLACEHOLDER.sub("", value)
            if "{" in rest or "}" in rest:
                raise ValueError("invalid reporting template")
        return value


@dataclass(frozen=True)
class Decision:
    rule_id: str
    action: str
    count_unit: str
    title: str
    explanation: str
    level: str
    original_level: str | None
    identity_parts: tuple[str, ...] | None
    diagnostics: dict[str, Any] | None
    fields: dict[str, Any]


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Policy:
        if len(text.encode()) > MAX_POLICY_BYTES:
            raise ValueError("reporting policy exceeds size limit")
        data = read_json(text)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data["version"] != 1
            or data.keys() - {"version", "defaults", "products", "overrides"}
        ):
            raise ValueError("unsupported reporting policy")
        merged: dict[str, dict[str, Any]] = {}
        for layer in ("defaults", "products", "overrides"):
            entries = data.get(layer, [])
            if not isinstance(entries, list) or len(entries) > 256:
                raise ValueError("invalid reporting policy layer")
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    raise ValueError("reporting rules require IDs")
                key = entry["id"]
                if key in seen:
                    raise ValueError("duplicate rule ID in reporting layer")
                seen.add(key)
                merged[key] = {**merged.get(key, {}), **entry}
        if len(merged) > 256:
            raise ValueError("too many reporting rules")
        rules = [Rule.model_validate(rule) for rule in merged.values()]
        result = cls(tuple(sorted(rules, key=lambda r: (-r.priority, r.id))))
        if len(result.serialize().encode()) > MAX_POLICY_BYTES:
            raise ValueError("resolved reporting policy exceeds size limit")
        return result

    @classmethod
    def load(cls, path: Path) -> Policy:
        with path.open("rb") as stream:
            content = stream.read(MAX_POLICY_BYTES + 1)
        return cls.parse(content.decode("utf-8"))

    def serialize(self) -> str:
        return canonical({"version": 1, "defaults": [r.model_dump() for r in self.rules]})

    def validate_scopes(self, environment: str, scopes: tuple[Scope, ...]) -> None:
        for rule in self.rules:
            if not rule.enabled or "failure_type" not in rule.match:
                continue
            required = set(rule.match["failure_type"])
            covered: dict[tuple[str, str, str, str], set[str]] = {}
            for scope in scopes:
                choices = {
                    "environment": environment,
                    "application": scope.application,
                    "service": scope.service,
                }
                if any(
                    field in rule.match and value not in rule.match[field]
                    for field, value in choices.items()
                ):
                    continue
                for namespace in scope.namespaces:
                    if "namespace" in rule.match and namespace not in rule.match["namespace"]:
                        continue
                    for container in scope.containers:
                        if "container" in rule.match and container not in rule.match["container"]:
                            continue
                        source = (scope.application, scope.service, namespace, container)
                        covered.setdefault(source, set()).update(scope.failure_types)
            if any(not required <= types for types in covered.values()):
                raise ValueError("reporting rule requires failure types outside its source scopes")

    def evaluate(self, environment: str, scope: Scope, record: Record) -> Decision:
        diagnostics = structured(record.message)
        original_level = record.original_level
        envelope = grouping_parts(record.message)
        if original_level is None and envelope[0] == "rust-tracing":
            original_level = envelope[1]
        if diagnostics:
            for key in ("level", "severity", "levelname"):
                if isinstance(diagnostics.get(key), (str, int)):
                    original_level = str(diagnostics[key])
                    break
        level = severity(original_level)
        fields: dict[str, Any] = {
            "environment": environment,
            "application": scope.application,
            "service": scope.service,
            "namespace": record.namespace,
            "container": record.container,
            "failure_type": record.failure_type,
            "level": level,
        }
        rules = self.rules
        if diagnostics is not None:
            try:
                fields.update(flatten(diagnostics, "json"))
            except ValueError:
                # Ambiguous paths cannot authorize exclusions or grouping. Keep the
                # complete diagnostics, but evaluate no rules or partially flattened fields.
                rules = ()
        for rule in rules:
            if not rule.enabled or not all(
                path in fields
                and any(canonical(fields[path]) == canonical(choice) for choice in choices)
                for path, choices in rule.match.items()
            ):
                continue
            complete = diagnostics is not None and all(
                path in fields
                and fields[path] is not None
                and not (isinstance(fields[path], (str, dict, list)) and not fields[path])
                for path in rule.group_by
            )
            parts = None
            if complete:
                normalized = {k: v for k, v in diagnostics.items() if k not in rule.drop_fields}
                # Retain every diagnostic field, including unknown and non-error fields.
                dimensions = {path: fields[path] for path in rule.group_by}
                parts = ("structured-v1", canonical(dimensions), canonical(normalized))
            title = (
                title_text(render(rule.title, fields))
                if rule.title
                else readable(record.message, diagnostics)
            )
            return Decision(
                rule.id,
                rule.action,
                rule.count_unit if complete else "error_events",
                title,
                render(rule.explanation, fields)[:2048]
                + (
                    "" if complete else " Incomplete diagnostic fields; exact grouping is retained."
                ),
                rule.level or level,
                original_level,
                parts,
                diagnostics,
                fields,
            )
        return Decision(
            "fallback-exact-v2",
            "report",
            "error_events",
            readable(record.message, diagnostics),
            "Unrecognized or unconfigured failure; exact source content is kept separate.",
            level,
            original_level,
            None,
            diagnostics,
            fields,
        )


def structured(message: str) -> dict[str, Any] | None:
    try:
        value = read_json(message)
        if isinstance(value, dict):
            # Reject nonfinite numbers and invalid Unicode inside encoded JSON too.
            # Preserve such unfamiliar input as the original raw string instead.
            canonical(value).encode("utf-8")
            return value
    except (ValueError, RecursionError):
        pass
    return None


def flatten(value: dict[str, Any], prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}"
        if path in result:
            raise ValueError("ambiguous structured field paths")
        result[path] = item
        if isinstance(item, dict):
            children = flatten(item, path)
            if result.keys() & children.keys():
                raise ValueError("ambiguous structured field paths")
            result.update(children)
    return result


def severity(value: str | None) -> str:
    levels = {
        "trace": "debug",
        "warn": "warning",
        "critical": "fatal",
        "50": "error",
        "60": "fatal",
        "40": "warning",
        "30": "info",
        "20": "debug",
        "10": "debug",
    }
    normalized = (value or "error").lower()
    normalized = levels.get(normalized, normalized)
    return normalized if normalized in {"debug", "info", "warning", "error", "fatal"} else "error"


def render(template: str, fields: dict[str, Any]) -> str:
    def substitute(match: re.Match[str]) -> str:
        value = fields.get(match[1], "unknown")
        return "see diagnostic details" if isinstance(value, (dict, list)) else str(value)

    return _PLACEHOLDER.sub(substitute, template)


def readable(message: str, diagnostics: dict[str, Any] | None) -> str:
    if diagnostics:
        for key in ("message", "msg", "event"):
            if isinstance(diagnostics.get(key), str) and diagnostics[key]:
                return title_text(diagnostics[key])
        return "Structured application failure"
    return title_text(message)


def title_text(value: str) -> str:
    cleaned = "".join(
        " " if ord(char) < 32 or 127 <= ord(char) <= 159 else char
        for char in display_message(value)
    )
    return " ".join(cleaned.split())[:512] or "Application failure"


def identity_parts(
    environment: str, scope: Scope, record: Record, decision: Decision
) -> tuple[str, ...]:
    context = (scope.id, record.namespace, record.container, record.failure_type)
    if decision.identity_parts is None:
        # Preserve the v2 identity for unconfigured records. The persisted identity key
        # is bound to environment and service, so these can never cross that boundary.
        return ("issue-v2", *context, *grouping_parts(record.message))
    return (
        "issue-policy-v1",
        environment,
        scope.application,
        scope.service,
        *context,
        *decision.identity_parts,
    )
