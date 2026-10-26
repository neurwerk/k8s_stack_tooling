"""Provider-neutral metadata companion; the existing primary catalog stays unchanged."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, Field, field_validator, model_validator

from k8s_stack_tooling.agentgateway_trust import Digest, Document
from k8s_stack_tooling.gateway_catalog import GatewayCatalog

Capability = Literal[
    "streaming",
    "tool_calling",
    "streaming_tool_calling",
    "structured_output",
    "image_input",
    "audio_input",
    "video_input",
    "document_input",
]
Support = Literal["supported", "unsupported", "unknown"]


def valid_unicode(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("invalid Unicode") from error
    return value


Bounded = Annotated[
    str,
    Field(min_length=1, max_length=2048, pattern=r"^[^\x00-\x1f\x7f]+$"),
    AfterValidator(valid_unicode),
]
BindingID = Annotated[str, Field(min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")]
PublicID = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
]
TokenLimit = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
MAX_BYTES = 1_048_576


class MetadataError(ValueError):
    """A sanitized contract failure; never include provider or credential values."""


def canonical_json(value: Any) -> str:
    """Match Go encoding/json (Helm toJson), including HTML/line-separator escaping."""

    def require_contract_numbers(item: Any) -> None:
        if isinstance(item, float):
            raise MetadataError("contract numbers must be integers")
        if isinstance(item, dict):
            for child in item.values():
                require_contract_numbers(child)
        elif isinstance(item, list):
            for child in item:
                require_contract_numbers(child)

    require_contract_numbers(value)
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    for char, escaped in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        text = text.replace(char, escaped)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise MetadataError("invalid Unicode in contract") from error
    return text


def revision(value: dict[str, Any], field: str) -> str:
    return hashlib.sha256(
        canonical_json({k: v for k, v in value.items() if k != field}).encode()
    ).hexdigest()


def load_json(text: str, *, max_bytes: int = MAX_BYTES) -> dict[str, Any]:
    """Bound input and reject duplicate keys before canonical hashing."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise MetadataError("duplicate JSON key")
            result[key] = value
        return result

    if len(text.encode()) > max_bytes:
        raise MetadataError("document exceeds size limit")
    try:
        value = json.loads(text, object_pairs_hook=unique)
    except (ValueError, RecursionError) as error:
        raise MetadataError("invalid JSON document") from error
    if not isinstance(value, dict):
        raise MetadataError("expected JSON object")
    return value


def utc_time(value: str) -> datetime:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", value):
        raise MetadataError("timestamps must be UTC RFC3339")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Limits(Document):
    context_tokens: TokenLimit | None
    max_input_tokens: TokenLimit | None
    max_output_tokens: TokenLimit | None

    @model_validator(mode="after")
    def within_context(self) -> Self:
        if self.context_tokens is not None and any(
            value is not None and value > self.context_tokens
            for value in (self.max_input_tokens, self.max_output_tokens)
        ):
            raise MetadataError("individual token limit exceeds context")
        return self


class Capabilities(Document):
    streaming: Support
    tool_calling: Support
    streaming_tool_calling: Support
    structured_output: Support
    image_input: Support
    audio_input: Support
    video_input: Support
    document_input: Support


class Provenance(Document):
    kind: Literal["provider_api", "runtime", "reviewed_config"]
    source: Bounded
    observed_at: str
    valid_until: str

    @model_validator(mode="after")
    def ordered_times(self) -> Self:
        if utc_time(self.valid_until) <= utc_time(self.observed_at):
            raise MetadataError("invalid provenance interval")
        return self

    def require_fresh(self, now: datetime) -> None:
        if not utc_time(self.observed_at) <= now < utc_time(self.valid_until):
            raise MetadataError("metadata is expired or observed in the future")


class SourceModel(Document):
    binding_id: BindingID
    public_id: PublicID
    limits: Limits
    capabilities: Capabilities
    provenance: Provenance


class Restrictions(Document):
    blocked_capabilities: list[Capability] = Field(max_length=8)
    evidence_refs: list[Bounded] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_values(self) -> Self:
        for values in (self.blocked_capabilities, self.evidence_refs):
            if len(values) != len(set(values)):
                raise MetadataError("duplicate gateway restriction")
        return self


class ModelMetadata(SourceModel):
    gateway_restrictions: Restrictions


class Companion(Document):
    schema_version: Literal[1]
    catalog_revision: Digest
    metadata_revision: Digest
    models: list[ModelMetadata] = Field(max_length=256)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise MetadataError("unsupported metadata schema")
        return value

    @model_validator(mode="after")
    def unique_pairs(self) -> Self:
        if len({(m.binding_id, m.public_id) for m in self.models}) != len(self.models):
            raise MetadataError("duplicate metadata model")
        return self


def validate_companion(
    raw: dict[str, Any], catalog: GatewayCatalog, *, now: datetime | None = None
) -> Companion:
    companion = Companion.model_validate(raw)
    if companion.catalog_revision != catalog.catalog_revision:
        raise MetadataError("metadata references a different primary catalog")
    if companion.metadata_revision != revision(raw, "metadata_revision"):
        raise MetadataError("metadata revision mismatch")
    pairs = {
        (entry.id, dest.id)
        for group in catalog.groups()
        for entry in group
        for dest in entry.destinations
        if dest.kind == "model"
    }
    for model in companion.models:
        if (model.binding_id, model.public_id) not in pairs:
            raise MetadataError("metadata references an unknown destination")
        model.provenance.require_fresh(now or datetime.now(UTC))
    return companion
