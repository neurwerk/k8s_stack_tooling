"""Small read-only provider adapters and explicit reviewed metadata fallback."""

from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import requests
from pydantic import Field, model_validator

from k8s_stack_tooling.agentgateway_trust import Document
from k8s_stack_tooling.dify_model_sync.projection import select_models
from k8s_stack_tooling.gateway_catalog import GatewayCatalog
from k8s_stack_tooling.gateway_model_metadata import (
    BindingID,
    Capabilities,
    MetadataError,
    SourceModel,
    load_json,
    utc_time,
)


class ProviderSource(Document):
    kind: Literal["openrouter", "deepseek", "llamacpp", "ollama"]
    source: str = Field(min_length=1, max_length=2048)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    credential_file: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def safe_endpoint(self) -> ProviderSource:
        parsed = urlsplit(self.source)
        _ = parsed.port
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or c == "\\" for c in self.source)
        ):
            raise MetadataError("invalid provider metadata endpoint")
        if self.kind in {"openrouter", "deepseek"}:
            host = "openrouter.ai" if self.kind == "openrouter" else "api.deepseek.com"
            if (
                parsed.scheme != "https"
                or parsed.hostname != host
                or parsed.port not in (None, 443)
            ):
                raise MetadataError("cloud metadata requires its native HTTPS origin")
            paths = ("/api/v1/models",) if self.kind == "openrouter" else ("/models", "/v1/models")
            if parsed.path not in paths:
                raise MetadataError("cloud metadata requires the native models endpoint")
        elif parsed.scheme not in {"http", "https"} or parsed.path.rstrip("/"):
            raise MetadataError("runtime metadata source must be an explicit server origin")
        if self.credential_env and self.credential_file:
            raise MetadataError("choose one metadata credential source")
        return self


class RefreshSources(Document):
    bindings: dict[BindingID, ProviderSource] = Field(default_factory=dict, max_length=256)
    reviewed_models: list[SourceModel] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def unique_reviewed(self) -> RefreshSources:
        if len({(m.binding_id, m.public_id) for m in self.reviewed_models}) != len(
            self.reviewed_models
        ):
            raise MetadataError("duplicate reviewed model")
        return self


def unknown_capabilities() -> dict[str, str]:
    return dict.fromkeys(Capabilities.model_fields, "unknown")


def positive(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise MetadataError("provider token limit is not a positive integer")
    return value


def modalities(caps: dict[str, str], inputs: Any) -> None:
    if inputs is None:
        return
    if not isinstance(inputs, list) or any(not isinstance(v, str) for v in inputs):
        raise MetadataError("invalid provider modalities")
    for field, names in {
        "image_input": {"image"},
        "audio_input": {"audio"},
        "video_input": {"video"},
        "document_input": {"file", "document"},
    }.items():
        caps[field] = "supported" if set(inputs) & names else "unsupported"


def openrouter_metadata(record: dict[str, Any]) -> tuple[dict[str, int | None], dict[str, str]]:
    """Preserve OpenRouter's exact metadata, never apply it to another provider."""
    context = positive(record.get("context_length"))
    top = record.get("top_provider")
    if not isinstance(top, dict):
        raise MetadataError("missing OpenRouter provider limits")
    # Both are provider claims, not gateway-enforced limits; retain the conservative context.
    provider_context = positive(top.get("context_length"))
    if provider_context is not None:
        context = min(context, provider_context) if context is not None else provider_context
    caps = unknown_capabilities()
    supported = record.get("supported_parameters")
    architecture = record.get("architecture")
    if not isinstance(supported, list) or any(not isinstance(v, str) for v in supported):
        raise MetadataError("invalid OpenRouter supported parameters")
    if not isinstance(architecture, dict):
        raise MetadataError("invalid OpenRouter architecture")
    caps["tool_calling"] = "supported" if "tools" in supported else "unsupported"
    caps["structured_output"] = "supported" if "structured_outputs" in supported else "unsupported"
    modalities(caps, architecture.get("input_modalities"))
    return {
        "context_tokens": context,
        "max_input_tokens": None,
        "max_output_tokens": positive(top.get("max_completion_tokens")),
    }, caps


def deepseek_metadata(record: dict[str, Any]) -> tuple[dict[str, int | None], dict[str, str]]:
    caps = unknown_capabilities()
    modalities(caps, record.get("input_modalities"))
    return {
        "context_tokens": positive(record.get("context_window")),
        "max_input_tokens": None,
        "max_output_tokens": positive(record.get("max_output_tokens")),
    }, caps


def runtime_metadata(
    record: dict[str, Any], props: dict[str, Any]
) -> tuple[dict[str, int | None], dict[str, str]]:
    caps = unknown_capabilities()
    modalities(caps, record.get("architecture", {}).get("input_modalities"))
    template = props.get("chat_template_caps", {})
    if type(template.get("supports_tool_calls")) is bool:
        caps["tool_calling"] = "supported" if template["supports_tool_calls"] else "unsupported"
    vision = props.get("modalities", {}).get("vision")
    if type(vision) is bool:
        caps["image_input"] = "supported" if vision else "unsupported"
    settings = props.get("default_generation_settings", {})
    params = settings.get("params", {})
    # -1 means unbounded/default, not an output ceiling; leave it unknown for review.
    output = params.get("n_predict", params.get("max_tokens"))
    if type(output) is int and output in {-1, -2, 0}:
        output = None
    return {
        "context_tokens": positive(settings.get("n_ctx")),
        "max_input_tokens": None,
        "max_output_tokens": positive(output),
    }, caps


def ollama_metadata(
    upstream: str, shown: dict[str, Any], running: dict[str, Any]
) -> tuple[dict[str, int | None], dict[str, str]]:
    """Training context and generation defaults are not effective request ceilings."""
    caps = unknown_capabilities()
    advertised = shown.get("capabilities")
    if advertised is not None:
        if not isinstance(advertised, list) or any(not isinstance(v, str) for v in advertised):
            raise MetadataError("invalid Ollama capabilities")
        caps["tool_calling"] = "supported" if "tools" in advertised else "unsupported"
        caps["image_input"] = "supported" if "vision" in advertised else "unsupported"
    models = running.get("models")
    if not isinstance(models, list) or any(not isinstance(m, dict) for m in models):
        raise MetadataError("invalid running model inventory")
    loaded = [m for m in models if m.get("model", m.get("name")) == upstream]
    if len(loaded) > 1:
        raise MetadataError("ambiguous loaded runtime identity")
    context = positive(loaded[0].get("context_length")) if loaded else None
    return {"context_tokens": context, "max_input_tokens": None, "max_output_tokens": None}, caps


def fetch_json(
    url: str, source: ProviderSource, *, show_model: str | None = None
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    token = ""
    if source.credential_env:
        token = os.environ.get(source.credential_env, "").strip()
    elif source.credential_file:
        token = Path(source.credential_file).read_text().strip()
    if source.credential_env or source.credential_file:
        if not token or any(c.isspace() for c in token):
            raise MetadataError("metadata credential is unavailable or invalid")
        headers["Authorization"] = f"Bearer {token}"
    with requests.Session() as session:
        # Disable ambient netrc auth; explicit headers above are the only credentials.
        session.auth = lambda request: request
        if show_model is not None:
            if source.kind != "ollama" or url != source.source.rstrip("/") + "/api/show":
                raise MetadataError("only Ollama model-detail POSTs are permitted")
            response_context = session.post(
                url,
                json={"model": show_model, "verbose": False},
                headers=headers,
                timeout=(5, 30),
                allow_redirects=False,
                stream=True,
            )
        else:
            response_context = session.get(
                url, headers=headers, timeout=(5, 30), allow_redirects=False, stream=True
            )
        with response_context as response:
            if response.status_code != 200:
                raise MetadataError("provider metadata request failed")
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 10_000_000:
                    raise MetadataError("provider metadata exceeds size limit")
    return load_json(body.decode("utf-8"), max_bytes=10_000_000)


def require_native_source(binding_id: str, source: ProviderSource, values: dict[str, Any]) -> None:
    """An adapter may only describe its own configured native provider/runtime."""
    local = values.get("self-hosted-api-providers", [])
    external = values.get("external-api-providers", [])
    bindings = [b for b in local + external if b.get("id") == binding_id]
    if len(bindings) != 1:
        raise MetadataError("metadata source requires one exact configured binding")
    binding = bindings[0]
    upstream = urlsplit(binding.get("upstreamURL", ""))
    endpoint = urlsplit(source.source)
    if (upstream.scheme, upstream.hostname, upstream.port) != (
        endpoint.scheme,
        endpoint.hostname,
        endpoint.port,
    ):
        raise MetadataError("metadata source does not match native binding origin")
    if source.kind in {"llamacpp", "ollama"}:
        if binding not in local or binding.get("credentialSources") != ["none"]:
            raise MetadataError("runtime metadata requires a credential-free local binding")
    elif binding not in external or binding.get("providerId") != source.kind:
        raise MetadataError("metadata adapter does not match the native provider")


def exact_upstreams(values: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Resolve native IDs from existing gateway selections, not another model list."""
    rows = {
        r["name"]: r["upstreamModel"] for r in values.get("openrouterCatalog", {}).get("models", [])
    }
    rows.update(
        {
            r["name"]: r["model"]
            for r in values.get("guardrails", {}).get("llmPolicyEngine", {}).get("models", [])
        }
    )
    result: dict[tuple[str, str], str] = {}
    for group in ("self-hosted-api-providers", "external-api-providers"):
        for binding in values.get(group, []):
            if not binding.get("enabled", False):
                continue
            for model in binding.get("models", []):
                result[binding["id"], model["name"]] = model["upstreamModel"]
            for public in binding.get("catalogModels", []):
                if public in rows:
                    result[binding["id"], public] = rows[public]
    return result


def eligible_pairs(catalog: GatewayCatalog) -> set[tuple[str, str]]:
    return {(model["binding_id"], model["public_id"]) for model in select_models(catalog)["models"]}


def refresh(
    catalog: GatewayCatalog,
    values: dict[str, Any],
    sources: RefreshSources,
    *,
    now: datetime | None = None,
    valid_hours: int = 168,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    now = now or datetime.now(UTC)
    if not 1 <= valid_hours <= 720:
        raise MetadataError("freshness must be between 1 and 720 hours")
    pairs = eligible_pairs(catalog)
    mappings = exact_upstreams(values)
    reviewed = {(m.binding_id, m.public_id): m for m in sources.reviewed_models}
    if not set(reviewed) <= pairs:
        raise MetadataError("reviewed metadata references an unselected destination")
    for model in reviewed.values():
        model.provenance.require_fresh(now)
        if model.provenance.kind != "reviewed_config":
            raise MetadataError("reviewed fallback requires reviewed_config provenance")
    inventories: dict[str, tuple[dict[str, dict[str, Any]], dict[str, Any]]] = {}
    failures: set[str] = set()
    for binding in sorted({b for b, _ in pairs}):
        source = sources.bindings.get(binding)
        if source is None:
            continue
        require_native_source(binding, source, values)
        try:
            if source.kind == "ollama":
                props = fetch_json(source.source.rstrip("/") + "/api/ps", source)
                inventory = {}
                for pair in sorted(pairs):
                    if pair[0] != binding or pair not in mappings:
                        continue
                    upstream = mappings[pair]
                    shown = fetch_json(
                        source.source.rstrip("/") + "/api/show", source, show_model=upstream
                    )
                    inventory[upstream] = {"id": upstream, "show": shown}
                inventories[binding] = inventory, props
                continue
            payload = fetch_json(
                source.source
                if source.kind != "llamacpp"
                else source.source.rstrip("/") + "/v1/models",
                source,
            )
            records = payload["data"]
            if not isinstance(records, list) or len(records) > 5000:
                raise MetadataError("invalid provider model inventory")
            inventory = {
                r["id"]: r for r in records if isinstance(r, dict) and isinstance(r.get("id"), str)
            }
            if len(inventory) != len(records):
                raise MetadataError("invalid or duplicate provider model identities")
            if source.kind == "openrouter" and (
                payload.get("total_count") != len(records)
                or payload.get("links", {}).get("next") is not None
            ):
                raise MetadataError("metadata refresh requires a complete provider snapshot")
            props = (
                fetch_json(source.source.rstrip("/") + "/props", source)
                if source.kind == "llamacpp"
                else {}
            )
            inventories[binding] = inventory, props
        except (MetadataError, requests.RequestException, OSError, ValueError, KeyError, TypeError):
            failures.add(binding)
    output: list[dict[str, Any]] = []
    counts: Counter[str] = Counter(selected=len(pairs))
    for binding, public in sorted(pairs):
        pair = binding, public
        fallback = reviewed.get(pair)
        source = sources.bindings.get(binding)
        model: SourceModel | None = None
        reason = "source_unavailable" if binding in failures else "source_missing"
        if binding in inventories and source is not None:
            inventory, props = inventories[binding]
            upstream = mappings.get(pair)
            record = inventory.get(upstream or "")
            reason = "identity_unresolved"
            if record is not None:
                try:
                    if source.kind == "openrouter":
                        limits, caps = openrouter_metadata(record)
                    elif source.kind == "deepseek":
                        limits, caps = deepseek_metadata(record)
                    elif source.kind == "llamacpp":
                        limits, caps = runtime_metadata(record, props)
                    else:
                        limits, caps = ollama_metadata(record["id"], record["show"], props)
                    model = SourceModel.model_validate(
                        {
                            "binding_id": binding,
                            "public_id": public,
                            "limits": limits,
                            "capabilities": caps,
                            "provenance": {
                                "kind": "runtime"
                                if source.kind in {"llamacpp", "ollama"}
                                else "provider_api",
                                "source": source.source,
                                "observed_at": now.isoformat().replace("+00:00", "Z"),
                                "valid_until": (now + timedelta(hours=valid_hours))
                                .isoformat()
                                .replace("+00:00", "Z"),
                            },
                        }
                    )
                except (ValueError, KeyError, TypeError, AttributeError):
                    reason = "source_malformed"
        if fallback is not None:
            if model is not None:
                raw = model.model_dump()
                changed = False
                for field in ("limits", "capabilities"):
                    for key, value in fallback.model_dump()[field].items():
                        if value in (None, "unknown"):
                            continue
                        if raw[field][key] not in (None, "unknown", value):
                            raise MetadataError(
                                "reviewed metadata conflicts with authoritative source"
                            )
                        if raw[field][key] != value:
                            raw[field][key] = value
                            changed = True
                if changed:
                    raw["provenance"] = fallback.provenance.model_dump()
                    # A reviewed fill must not extend the fetched fields' freshness window.
                    expiry = min(
                        utc_time(model.provenance.valid_until),
                        utc_time(fallback.provenance.valid_until),
                    )
                    raw["provenance"]["valid_until"] = expiry.isoformat().replace("+00:00", "Z")
                    model = SourceModel.model_validate(raw)
                    counts["reviewed_fields"] += 1
            elif reason != "source_malformed":
                model = fallback
                counts["reviewed_fallback"] += 1
        if model is None:
            counts["skipped_" + reason] += 1
            continue
        if model.limits.context_tokens is None or model.limits.max_output_tokens is None:
            # Preserve truthful unknowns centrally; Dify must skip this model, not guess defaults.
            output.append(model.model_dump())
            counts["refreshed"] += 1
            counts["skipped_missing_limits"] += 1
            continue
        output.append(model.model_dump())
        counts["refreshed"] += 1
    return output, dict(counts)
