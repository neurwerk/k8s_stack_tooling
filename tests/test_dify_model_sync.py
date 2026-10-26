"""Contracts and manual launch boundaries, without a cluster or provider inference."""

import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from kubernetes.client.rest import ApiException
from pydantic import ValidationError

from k8s_stack_tooling import model_metadata_refresh as refresh
from k8s_stack_tooling.dify_model_sync import launcher, main
from k8s_stack_tooling.dify_model_sync.projection import (
    build_projection,
    require_installed_projection,
)
from k8s_stack_tooling.gateway_catalog import GatewayCatalog
from k8s_stack_tooling.gateway_model_metadata import (
    MetadataError,
    SourceModel,
    canonical_json,
    load_json,
    revision,
    validate_companion,
)

NS = SimpleNamespace


def catalog():
    def entry(name, source, *, custom=False):
        path = "custom/api" if custom else "bindings/" + name
        return {
            "id": name,
            "provider_id": name,
            "display_name": name,
            "routes": [
                {
                    "id": name,
                    "url": f"https://gateway.example.test/{path}/v1",
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
                    "profile_ids": ["openai-chat-completions-v1"],
                    "required_permissions": ["llm:invoke", f"model:{name}/chat:invoke"],
                    "credential_modes": [
                        {
                            "source": source,
                            "required_permissions": [f"provider:{name}:company-use"]
                            if source == "company"
                            else [],
                        }
                    ],
                    "provider_id": name,
                }
            ],
        }

    return GatewayCatalog.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": "a" * 64,
            "policy_revision": "b" * 64,
            "profiles": [
                {
                    "id": "openai-chat-completions-v1",
                    "revision": "1",
                    "operations": [],
                    "limitations": [],
                }
            ],
            "self-hosted-api-providers": [entry("local", "none")],
            "external-api-providers": [
                entry("cloud", "company", custom=True),
                entry("personal", "caller_supplied"),
            ],
            "external-subscription-providers": [entry("subscription", "company")],
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


def companion():
    value = {"schema_version": 1, "catalog_revision": "a" * 64, "models": [record()]}
    value["metadata_revision"] = revision(value, "metadata_revision")
    return value


def rehash(value):
    value["metadata_revision"] = revision(value, "metadata_revision")
    return value


@pytest.mark.parametrize("value", [True, "100", 1.5, 0, -1])
def test_limits_are_strict_and_positive(value):
    data = companion()
    data["models"][0]["limits"]["context_tokens"] = value
    with pytest.raises((ValidationError, MetadataError)):
        validate_companion(rehash(data), catalog())


def test_individual_limits_not_sum_and_restrictions_do_not_rewrite_upstream():
    validated = validate_companion(companion(), catalog())
    model = validated.models[0]
    assert model.limits.max_input_tokens is not None
    assert model.limits.max_output_tokens is not None
    assert model.limits.context_tokens is not None
    assert (
        model.limits.max_input_tokens + model.limits.max_output_tokens > model.limits.context_tokens
    )
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
    assert (
        canonical_json({"z": "<&>\u2028\u2029café", "a": 1})
        == '{"a":1,"z":"\\u003c\\u0026\\u003e\\u2028\\u2029café"}'
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


def test_projection_same_grants_selection_custom_routes_and_missing_default_metadata():
    projection = build_projection(catalog(), companion())
    assert [(m["binding_id"], m["credential_mode"]) for m in projection["models"]] == [
        ("cloud", "company"),
        ("local", "local"),
    ]
    assert projection["models"][1]["metadata"] is None
    assert projection["bindings"] == [
        {"id": "cloud", "api_path": "/custom/api"},
        {"id": "local", "api_path": "/bindings/local"},
    ]
    assert projection["roles"] == [
        "llm:invoke",
        "model:cloud/chat:invoke",
        "model:local/chat:invoke",
        "provider:cloud:company-use",
    ]
    assert revision(projection, "projection_revision") == projection["projection_revision"]
    require_installed_projection(projection, projection, projection["projection_revision"])
    stale = copy.deepcopy(projection)
    stale["metadata_revision"] = "0" * 64
    with pytest.raises(MetadataError):
        require_installed_projection(stale, projection, projection["projection_revision"])


@pytest.mark.parametrize("mutation", ["grant", "billing", "path", "responses"])
def test_projection_rejects_bad_grants_routes_or_excludes_non_chat(mutation):
    raw = catalog().model_dump(by_alias=True)
    entry = raw["external-api-providers"][0]
    if mutation == "grant":
        entry["destinations"][0]["required_permissions"] = ["llm:invoke"]
    elif mutation == "billing":
        entry["destinations"][0]["credential_modes"][0]["required_permissions"] = []
    elif mutation == "path":
        entry["routes"][0]["url"] = "https://gateway.example.test/custom/api/v1/"
    else:
        entry["destinations"][0]["profile_ids"] = []
        assert len(build_projection(GatewayCatalog.model_validate(raw), companion())["models"]) == 1
        return
    with pytest.raises(MetadataError):
        build_projection(GatewayCatalog.model_validate(raw), companion())


def template():
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"generateName": "sync-"},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {"name": "model-sync", "image": "example.test/dify@sha256:" + "a" * 64}
                    ],
                    "volumes": [{"name": "input", "configMap": {"name": launcher.INSTALLED}}],
                }
            },
        },
    }


def cluster_inputs():
    primary = catalog()
    meta = companion()
    projection = build_projection(primary, meta)
    data = {
        "projection.json": json.dumps(projection),
        "job-template.json": json.dumps(template()),
        "sync_models.py": "# installed reviewed payload\n",
    }
    maps = [
        NS(data={"catalog.json": primary.model_dump_json(by_alias=True)}),
        NS(data={"metadata.json": json.dumps(meta)}),
        NS(data=data),
    ]
    for i, item in enumerate(maps):
        item.metadata = NS(uid=str(i), resource_version="1")
    return projection, maps


def test_preview_launcher_snapshots_rechecks_and_never_calls_inference(monkeypatch):
    projection, maps = cluster_inputs()
    core, batch = Mock(), Mock()
    core.read_namespaced_config_map.side_effect = maps + maps
    batch.create_namespaced_job.return_value = NS(metadata=NS(uid="run-uid"))
    monkeypatch.setattr("requests.post", Mock(side_effect=AssertionError("inference forbidden")))
    result = launcher.launch(
        core,
        batch,
        catalog_namespace="catalog",
        namespace="dify",
        mode="preview",
        expected=projection["projection_revision"],
        max_company=0,
        wait_seconds=0,
    )
    assert result["status"] == "submitted"
    snapshot = core.create_namespaced_config_map.call_args.args[1]
    job = batch.create_namespaced_job.call_args.args[1]
    assert snapshot.immutable
    assert snapshot.data == {k: maps[2].data[k] for k in ("projection.json", "sync_models.py")}
    pod = job["spec"]["template"]["spec"]
    assert pod["volumes"][0]["configMap"]["name"] == snapshot.metadata.name
    assert {v["name"]: v["value"] for v in pod["containers"][0]["env"]}[
        "DIFY_MODEL_SYNC_MODE"
    ] == "preview"
    assert core.read_namespaced_config_map.call_count == 6


@pytest.mark.parametrize("mutation", ["changed", "stale", "cap", "retries"])
def test_launcher_fail_closed_before_creating_resources(mutation):
    projection, maps = cluster_inputs()
    again = copy.deepcopy(maps)
    cap = 27
    if mutation == "changed":
        again[2].metadata.resource_version = "2"
    elif mutation == "stale":
        maps[2].data["projection.json"] = "{}"
    elif mutation == "cap":
        cap = 0
    else:
        value = template()
        value["spec"]["backoffLimit"] = 1
        maps[2].data["job-template.json"] = json.dumps(value)
    core, batch = Mock(), Mock()
    core.read_namespaced_config_map.side_effect = maps + again
    with pytest.raises(MetadataError):
        launcher.launch(
            core,
            batch,
            catalog_namespace="catalog",
            namespace="dify",
            mode="apply",
            expected=projection["projection_revision"],
            max_company=cap,
            wait_seconds=0,
        )
    core.create_namespaced_config_map.assert_not_called()
    batch.create_namespaced_job.assert_not_called()


def test_result_only_exposes_numeric_counts():
    assert launcher.safe_counts(
        "sensitive provider error\n"
        '{"counts":{"created":2,"failed":1,"secret":"hidden","updated":true}}'
    ) == {"created": 2, "failed": 1}


def test_uncertain_job_creation_is_named_and_not_retried():
    projection, maps = cluster_inputs()
    core, batch = Mock(), Mock()
    core.read_namespaced_config_map.side_effect = maps + maps
    batch.create_namespaced_job.side_effect = ApiException(status=504)
    result = launcher.launch(
        core,
        batch,
        catalog_namespace="catalog",
        namespace="dify",
        mode="apply",
        expected=projection["projection_revision"],
        max_company=27,
        wait_seconds=0,
    )
    assert result["status"] == "uncertain" and result["phase"] == "job"
    assert result["job"].startswith("frontend-dify-model-sync-")
    assert batch.create_namespaced_job.call_count == 1


def test_wait_does_not_report_partial_errors_as_success():
    core, batch = Mock(), Mock()
    batch.read_namespaced_job_status.return_value = NS(
        status=NS(conditions=[NS(type="Complete", status="True")])
    )
    core.list_namespaced_pod.return_value = NS(items=[NS(metadata=NS(name="run-pod"))])
    core.read_namespaced_pod_log.return_value = '{"counts":{"created":1,"failed":1}}'
    assert launcher.wait_result(core, batch, "run", "dify", 1) == {
        "status": "failed",
        "counts": {"created": 1, "failed": 1},
    }


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
        lambda *_: {"data": [{"id": "native", "context_window": 12000, "max_output_tokens": 8000}]},
    )
    with pytest.raises(MetadataError, match="conflicts"):
        refresh.refresh(catalog(), values, sources)


def test_preview_generation_does_not_write_and_write_preserves_enablement(
    tmp_path, monkeypatch, capsys
):
    primary, metadata, output = (
        tmp_path / name for name in ("primary.json", "metadata.json", "values.yaml")
    )
    primary.write_text(catalog().model_dump_json(by_alias=True))
    metadata.write_text(json.dumps(companion()))
    output.write_text("frontendDify:\n  modelSync:\n    enabled: true\nunrelated: preserved\n")
    before = output.read_text()
    args = [
        "project",
        "--catalog",
        str(primary),
        "--metadata",
        str(metadata),
        "--output",
        str(output),
    ]
    assert main.main(args) == 0
    assert output.read_text() == before
    assert "projection_revision" in capsys.readouterr().out
    assert main.main([*args, "--write"]) == 0
    result = yaml.safe_load(output.read_text())
    assert result["frontendDify"]["modelSync"]["enabled"] is True
    assert result["unrelated"] == "preserved"


def test_explicit_context_required_without_api_calls(monkeypatch):
    create_client = Mock()
    monkeypatch.setattr(main.config, "new_client_from_config", create_client)
    with pytest.raises(SystemExit):
        main.main(["launch", "--expected-projection-revision", "a" * 64])
    create_client.assert_not_called()


def test_source_model_rejects_gateway_manufactured_restrictions():
    with pytest.raises(ValidationError):
        SourceModel.model_validate(record())


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
        if field == "binding_id":
            malformed[field] = invalid
        else:
            malformed["provenance"][field] = invalid
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
    main.write_values(path, {"gatewayModelMetadata": {"models": []}})
    expected = copy.deepcopy(before)
    expected["gatewayModelMetadata"]["models"] = []
    assert yaml.safe_load(path.read_text()) == expected


def test_ollama_unloaded_training_context_is_not_an_effective_limit():
    shown = {
        "model_info": {"llama.context_length": 131072},
        "capabilities": ["completion", "tools"],
        "parameters": 'stop "example"',
    }
    limits, caps = refresh.ollama_metadata("example:small", shown, {"models": []})
    assert limits == {"context_tokens": None, "max_input_tokens": None, "max_output_tokens": None}
    assert caps["tool_calling"] == "supported"
    assert caps["streaming_tool_calling"] == "unknown"
    limits, _ = refresh.ollama_metadata(
        "example:small", shown, {"models": [{"model": "example:small", "context_length": 8192}]}
    )
    assert limits["context_tokens"] == 8192 and limits["max_output_tokens"] is None


def test_native_provider_metadata_cannot_be_borrowed_from_relay():
    source = refresh.ProviderSource(kind="openrouter", source="https://openrouter.ai/api/v1/models")
    values = {
        "external-api-providers": [
            {"id": "direct", "providerId": "deepseek", "upstreamURL": "https://api.deepseek.com/v1"}
        ]
    }
    with pytest.raises(MetadataError, match="native"):
        refresh.require_native_source("direct", source, values)


def test_ollama_refresh_only_reads_show_and_running_inventory(monkeypatch):
    source = refresh.RefreshSources.model_validate(
        {"bindings": {"local": {"kind": "ollama", "source": "http://runtime.example.test:11434"}}}
    )
    values = {
        "self-hosted-api-providers": [
            {
                "id": "local",
                "enabled": True,
                "upstreamURL": "http://runtime.example.test:11434/v1",
                "credentialSources": ["none"],
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
    rows, counts = refresh.refresh(catalog(), values, source)
    assert len(rows) == 1 and rows[0]["limits"]["context_tokens"] is None
    assert rows[0]["provenance"]["kind"] == "runtime"
    assert counts["skipped_missing_limits"] == 1
    assert len(calls) == 2


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
