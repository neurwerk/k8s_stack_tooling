"""Neutral metadata regressions retained from the retired shared product suite."""

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from pydantic import ValidationError

from k8s_stack_tooling import gateway_model_metadata_cli as cli
from k8s_stack_tooling import model_metadata_refresh as refresh
from k8s_stack_tooling.gateway_catalog.catalog import GatewayCatalog
from k8s_stack_tooling.gateway_catalog.metadata import (
    MetadataError,
    SourceModel,
    canonical_json,
    load_json,
    revision,
    validate_companion,
)
from k8s_stack_tooling.gateway_catalog.values import write_values


def catalog():
    def entry(name):
        return {
            "id": name,
            "provider_id": name,
            "display_name": name,
            "routes": [
                {
                    "id": name,
                    "url": f"https://gateway.example.test/bindings/{name}/v1",
                    "entry_mode": "api",
                    "caller_auth_modes": ["neurwerk_bearer"],
                }
            ],
            "destinations": [
                {
                    "id": name + "/chat",
                    "kind": "model",
                    "display_name": name,
                    "endpoint_ids": [name],
                    "profile_ids": [],
                    "required_permissions": [f"model:{name}/chat:invoke"],
                    "credential_modes": [{"source": "override", "required_permissions": []}],
                    "provider_id": name,
                }
            ],
        }

    return GatewayCatalog.model_validate(
        {
            "schema_version": 3,
            "catalog_revision": "a" * 64,
            "policy_revision": "b" * 64,
            "profiles": [],
            "self-hosted-api-providers": [entry("local")],
            "external-api-providers": [entry("cloud")],
            "external-subscription-providers": [],
            "mcp-servers": [],
        }
    )


def record(binding="cloud"):
    now = datetime.now(UTC)
    return {
        "binding_id": binding,
        "public_id": binding + "/chat",
        "limits": {"context_tokens": 10000, "max_input_tokens": 9000, "max_output_tokens": 8000},
        "capabilities": {
            **refresh.unknown_capabilities(),
            "tool_calling": "supported",
            "image_input": "supported",
        },
        "provenance": {
            "kind": "provider_api",
            "source": "https://provider.example.test/models",
            "observed_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "valid_until": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
        "gateway_restrictions": {
            "blocked_capabilities": ["image_input"],
            "evidence_refs": ["attachment-policy"],
        },
    }


def rehash(value):
    value["metadata_revision"] = revision(value, "metadata_revision")
    return value


def companion():
    return rehash({"schema_version": 1, "catalog_revision": "a" * 64, "models": [record()]})


@pytest.mark.parametrize("value", [True, "100", 1.5, 0, -1])
def test_limits_are_strict_and_positive(value):
    data = companion()
    data["models"][0]["limits"]["context_tokens"] = value
    with pytest.raises((ValidationError, MetadataError)):
        validate_companion(rehash(data), catalog())


def test_individual_limits_not_sum_and_restrictions_do_not_rewrite_upstream():
    model = validate_companion(companion(), catalog()).models[0]
    assert model.limits.max_input_tokens == 9000 and model.limits.max_output_tokens == 8000
    assert model.limits.context_tokens == 10000
    assert model.capabilities.image_input == "supported"
    assert model.gateway_restrictions.blocked_capabilities == ["image_input"]


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "limit",
        "unknown_capability",
        "expired",
        "future",
        "timezone",
        "duplicate",
        "pair",
        "schema",
    ],
)
def test_reject_invalid_metadata(mutation):
    value = companion()
    model = value["models"][0]
    if mutation == "extra":
        model["credentials"] = "not-allowed"
    elif mutation == "limit":
        model["limits"]["max_output_tokens"] = 10001
    elif mutation == "unknown_capability":
        model["gateway_restrictions"]["blocked_capabilities"] = ["invented"]
    elif mutation == "expired":
        model["provenance"].update(
            observed_at="2020-01-01T00:00:00Z", valid_until="2020-01-02T00:00:00Z"
        )
    elif mutation == "future":
        model["provenance"].update(
            observed_at="2090-01-01T00:00:00Z", valid_until="2090-01-02T00:00:00Z"
        )
    elif mutation == "timezone":
        model["provenance"]["observed_at"] = "2026-01-01T00:00:00+01:00"
    elif mutation == "duplicate":
        value["models"].append(copy.deepcopy(model))
    elif mutation == "pair":
        model["binding_id"] = "absent"
    else:
        value["schema_version"] = True
    with pytest.raises((ValidationError, MetadataError)):
        validate_companion(rehash(value), catalog())


def test_hashes_and_duplicate_keys_are_checked():
    assert canonical_json({"z": "<&>\u2028\u2029café", "a": 1}) == (
        '{"a":1,"z":"\\u003c\\u0026\\u003e\\u2028\\u2029café"}'
    )
    with pytest.raises(MetadataError):
        load_json('{"models":[],"models":[]}')
    for invalid in ({"value": "\ud800"}, {"value": 1.0}, {"value": float("nan")}):
        with pytest.raises(MetadataError):
            canonical_json(invalid)
    value = companion()
    value["metadata_revision"] = "0" * 64
    with pytest.raises(MetadataError, match="revision"):
        validate_companion(value, catalog())
    value = companion()
    value["catalog_revision"] = "f" * 64
    with pytest.raises(MetadataError, match="primary"):
        validate_companion(rehash(value), catalog())


@pytest.mark.parametrize("limit", [2_147_483_648, 9_007_199_254_740_991])
def test_source_limits_keep_base_safe_integer_range(limit):
    source = record()
    source.pop("gateway_restrictions")
    source["limits"] = dict.fromkeys(source["limits"], limit)
    assert SourceModel.model_validate(source).limits.context_tokens == limit
    assert refresh.positive(limit) == limit


def test_source_limits_reject_values_above_base_safe_integer_range():
    source = record()
    source.pop("gateway_restrictions")
    source["limits"]["context_tokens"] = 9_007_199_254_740_992
    with pytest.raises(ValidationError):
        SourceModel.model_validate(source)
    with pytest.raises(MetadataError):
        refresh.positive(9_007_199_254_740_992)


def test_source_binding_provenance_and_timestamp_boundaries_match_base():
    source = record()
    source.pop("gateway_restrictions")
    source["binding_id"] = "a" + "-" * 30 + "b"
    source["provenance"].update(
        source="s" * 512,
        observed_at="2025-01-01T00:00:00.123456789Z",
        valid_until="2026-01-01T00:00:00.123456789Z",
    )
    SourceModel.model_validate(source)
    for field, invalid in (
        ("binding_id", "a" * 33),
        ("binding_id", "a-"),
        ("source", "s" * 513),
        ("observed_at", "2025-01-01T00:00:00.1234567890Z"),
        ("observed_at", "2025-01-01T00:00:00+00:00"),
    ):
        malformed = copy.deepcopy(source)
        target = malformed if field == "binding_id" else malformed["provenance"]
        target[field] = invalid
        with pytest.raises(ValidationError):
            SourceModel.model_validate(malformed)


@pytest.mark.parametrize("enabled", [True, False, None])
def test_generic_metadata_write_preserves_enablement_and_unrelated_fields(tmp_path, enabled):
    path = tmp_path / "values.yaml"
    before: dict[str, Any] = {
        "gatewayModelMetadata": {"models": ["old"]},
        "unrelated": {"keep": True},
    }
    if enabled is not None:
        before["gatewayModelMetadata"]["enabled"] = enabled
    path.write_text(yaml.safe_dump(before))
    write_values(path, {"gatewayModelMetadata": {"models": []}})
    expected = copy.deepcopy(before)
    expected["gatewayModelMetadata"]["models"] = []
    assert yaml.safe_load(path.read_text()) == expected


@pytest.mark.parametrize("params", [{"n_predict": -1}, {"n_predict": 512}, {"max_tokens": 512}])
def test_native_metadata_exact_identity_runtime_context_and_unbounded_output(params):
    limits, caps = refresh.deepseek_metadata(
        {
            "id": "native-chat",
            "context_window": 12000,
            "max_output_tokens": 4000,
            "input_modalities": ["text"],
        }
    )
    assert limits["context_tokens"] == 12000 and caps["image_input"] == "unsupported"
    limits, caps = refresh.runtime_metadata(
        {"meta": {"n_ctx_train": 128000}},
        {
            "default_generation_settings": {"n_ctx": 4096, "params": params},
            "chat_template_caps": {"supports_tool_calls": False},
        },
    )
    assert limits["context_tokens"] == 4096 and limits["max_output_tokens"] is None
    assert caps["tool_calling"] == "unsupported"


def test_source_model_rejects_gateway_manufactured_restrictions():
    with pytest.raises(ValidationError):
        SourceModel.model_validate(record())


def test_native_provider_metadata_cannot_be_borrowed_from_relay():
    source = refresh.ProviderSource(kind="openrouter", source="https://openrouter.ai/api/v1/models")
    values = {
        "external-api-providers": [
            {
                "id": "direct",
                "providerId": "deepseek",
                "upstreamURL": "https://api.deepseek.com/v1",
            }
        ]
    }
    with pytest.raises(MetadataError, match="native"):
        refresh.require_native_source("direct", source, values)


def test_ollama_unloaded_training_context_is_not_an_effective_limit():
    shown = {
        "model_info": {"llama.context_length": 131072},
        "capabilities": ["completion", "tools"],
        "parameters": 'stop "example"',
    }
    limits, caps = refresh.ollama_metadata("example:small", shown, {"models": []})
    assert limits == {"context_tokens": None, "max_input_tokens": None, "max_output_tokens": None}
    assert caps["tool_calling"] == "supported" and caps["streaming_tool_calling"] == "unknown"
    limits, _ = refresh.ollama_metadata(
        "example:small", shown, {"models": [{"model": "example:small", "context_length": 8192}]}
    )
    assert limits["context_tokens"] == 8192 and limits["max_output_tokens"] is None


def test_refresh_skips_unresolved_native_alias_and_missing_runtime_limit(monkeypatch):
    sources = refresh.RefreshSources.model_validate(
        {
            "bindings": {
                "cloud": {"kind": "deepseek", "source": "https://api.deepseek.com/models"},
                "local": {"kind": "llamacpp", "source": "http://runtime.example.test:8000"},
            }
        }
    )
    values = {
        "external-api-providers": [
            {
                "id": "cloud",
                "enabled": True,
                "providerId": "deepseek",
                "upstreamURL": "https://api.deepseek.com/v1",
                "models": [{"name": "cloud/chat", "upstreamModel": "direct-alias"}],
            }
        ],
        "self-hosted-api-providers": [
            {
                "id": "local",
                "enabled": True,
                "credentialSources": ["none"],
                "upstreamURL": "http://runtime.example.test:8000/v1",
                "models": [{"name": "local/chat", "upstreamModel": "loaded"}],
            }
        ],
    }

    def fetch(url, source):
        if source.kind == "deepseek":
            return {"data": [{"id": "other-provider-alias", "context_window": 100000}]}
        if url.endswith("/props"):
            return {"default_generation_settings": {"n_ctx": 4096, "params": {"n_predict": -1}}}
        return {"data": [{"id": "loaded"}]}

    monkeypatch.setattr(refresh, "fetch_json", fetch)
    rows, counts = refresh.refresh(catalog(), values, sources)
    assert len(rows) == 1 and rows[0]["binding_id"] == "local"
    assert rows[0]["limits"]["max_output_tokens"] is None
    assert counts == {
        "selected": 2,
        "refreshed": 1,
        "skipped_identity_unresolved": 1,
        "skipped_missing_limits": 1,
    }


def test_reviewed_fallback_cannot_override_authoritative_limit(monkeypatch):
    fallback = record()
    fallback.pop("gateway_restrictions")
    fallback["provenance"]["kind"] = "reviewed_config"
    sources = refresh.RefreshSources.model_validate(
        {
            "bindings": {
                "cloud": {"kind": "deepseek", "source": "https://api.deepseek.com/models"}
            },
            "reviewed_models": [fallback],
        }
    )
    values = {
        "external-api-providers": [
            {
                "id": "cloud",
                "enabled": True,
                "providerId": "deepseek",
                "upstreamURL": "https://api.deepseek.com/v1",
                "models": [{"name": "cloud/chat", "upstreamModel": "native"}],
            }
        ]
    }
    monkeypatch.setattr(
        refresh,
        "fetch_json",
        lambda *_: {
            "data": [{"id": "native", "context_window": 12000, "max_output_tokens": 8000}],
        },
    )
    with pytest.raises(MetadataError, match="conflicts"):
        refresh.refresh(catalog(), values, sources)


def test_ollama_refresh_only_reads_show_and_running_inventory(monkeypatch):
    sources = refresh.RefreshSources.model_validate(
        {
            "bindings": {
                "local": {"kind": "ollama", "source": "http://runtime.example.test:11434"}
            },
        }
    )
    values = {
        "self-hosted-api-providers": [
            {
                "id": "local",
                "enabled": True,
                "credentialSources": ["none"],
                "upstreamURL": "http://runtime.example.test:11434/v1",
                "models": [{"name": "local/chat", "upstreamModel": "example:small"}],
            }
        ]
    }
    calls = []

    def fetch(url, source, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/ps"):
            return {"models": []}
        assert url.endswith("/api/show") and kwargs == {"show_model": "example:small"}
        return {
            "model_info": {"example.context_length": 131072},
            "capabilities": ["completion", "tools"],
        }

    monkeypatch.setattr(refresh, "fetch_json", fetch)
    rows, counts = refresh.refresh(catalog(), values, sources)
    assert len(rows) == 1 and rows[0]["limits"]["context_tokens"] is None
    assert rows[0]["provenance"]["kind"] == "runtime"
    assert counts["skipped_missing_limits"] == 1 and len(calls) == 2


def test_ollama_http_adapter_cannot_post_to_inference(monkeypatch):
    source = refresh.ProviderSource(kind="ollama", source="http://runtime.example.test:11434")
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b'{"capabilities":["completion","tools"]}']
    session.post.return_value = response
    monkeypatch.setattr(refresh.requests, "Session", lambda: session)
    refresh.fetch_json(source.source + "/api/show", source, show_model="example:small")
    assert session.post.call_args.kwargs["json"] == {"model": "example:small", "verbose": False}
    with pytest.raises(MetadataError, match="only Ollama"):
        refresh.fetch_json(source.source + "/api/generate", source, show_model="example:small")
    assert session.post.call_count == 1


def test_cli_preview_then_explicit_write_preserves_existing_settings(tmp_path, monkeypatch, capsys):
    primary, values, sources, output = (
        tmp_path / name
        for name in (
            "catalog.json",
            "values.yaml",
            "sources.json",
            "output.yaml",
        )
    )
    primary.write_text(catalog().model_dump_json(by_alias=True))
    model = record()
    model.pop("gateway_restrictions")
    model["provenance"]["kind"] = "reviewed_config"
    sources.write_text(json.dumps({"reviewed_models": [model]}))
    values.write_text("{}")
    output.write_text("gatewayModelMetadata:\n  enabled: false\n  models: []\nunrelated: keep\n")
    monkeypatch.setattr(
        refresh, "fetch_json", Mock(side_effect=AssertionError("no provider requested"))
    )
    args = [
        "refresh",
        "--catalog",
        str(primary),
        "--values",
        str(values),
        "--sources",
        str(sources),
        "--output",
        str(output),
    ]
    before = output.read_bytes()
    assert cli.main(args) == 0
    assert output.read_bytes() == before
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview" and preview["selected"] == 1
    assert cli.main([*args, "--write"]) == 0
    written = yaml.safe_load(output.read_text())
    assert written["gatewayModelMetadata"]["enabled"] is False
    assert written["gatewayModelMetadata"]["models"] == [model]
    assert written["unrelated"] == "keep"
    assert json.loads(capsys.readouterr().out)["mode"] == "write"


def test_cli_write_requires_explicit_output_before_reading_inputs(capsys):
    assert (
        cli.main(
            [
                "refresh",
                "--catalog",
                "absent",
                "--values",
                "absent",
                "--sources",
                "absent",
                "--write",
            ]
        )
        == 1
    )
    assert "--write requires an explicit --output" in capsys.readouterr().err


def test_cli_redacts_malformed_document_values(tmp_path, capsys):
    primary = tmp_path / "catalog.json"
    primary.write_text('{"private":"synthetic-sensitive-marker"}')
    assert (
        cli.main(
            [
                "refresh",
                "--catalog",
                str(primary),
                "--values",
                "absent",
                "--sources",
                "absent",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert not output.out
    assert "synthetic-sensitive-marker" not in output.err
    assert "Traceback" not in output.err
