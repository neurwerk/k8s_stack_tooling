from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
import requests
import yaml

import openrouter_catalog_sync.sync as sync
from openrouter_catalog_sync.sync import (
    CatalogSyncError,
    Policy,
    check_files,
    fetch_models,
    generate,
    load_policy,
    validate_paths,
    write_files,
)


class FakeResponse:
    def __init__(
        self,
        payload: object,
        error: Exception | None = None,
        *,
        status_code: int = 200,
        stream_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.stream_error = stream_error
        self.closed = False

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        body = json.dumps(self.payload).encode()
        for offset in range(0, len(body), chunk_size):
            yield body[offset : offset + chunk_size]
        if self.stream_error:
            raise self.stream_error

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, float, bool]] = []

    def get(self, url: str, *, timeout: float, allow_redirects: bool, stream: bool) -> FakeResponse:
        assert stream is True
        self.requests.append((url, timeout, allow_redirects))
        return self.responses[url]


def model(
    upstream_id: str,
    *,
    name: str | None = "Acme: Useful Model",
    outputs: list[str] | None = None,
    parameters: list[str] | None = None,
    expiration: str | None = None,
    pricing: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": upstream_id,
        "name": name,
        "description": "must never be emitted",
        "architecture": {"output_modalities": ["text"] if outputs is None else outputs},
        "supported_parameters": ["temperature"] if parameters is None else parameters,
        "expiration_date": expiration,
        "pricing": pricing or {"prompt": "0.000001", "completion": "0.000002"},
    }


def empty_policy() -> Policy:
    return Policy({}, frozenset())


def decoded(generated: object) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog_bytes = cast("Any", generated).catalog
    pricing_bytes = cast("Any", generated).pricing
    return cast("dict[str, Any]", yaml.safe_load(catalog_bytes)), cast(
        "dict[str, Any]", json.loads(pricing_bytes)
    )


def page(data: list[object], total_count: int, next_url: str | None) -> dict[str, object]:
    return {"data": data, "total_count": total_count, "links": {"next": next_url}}


def test_fetch_models_follows_relative_and_absolute_pagination() -> None:
    client = FakeClient(
        {
            "https://openrouter.ai/models": FakeResponse(page([{"id": "one"}], 3, "?offset=1")),
            "https://openrouter.ai/models?offset=1": FakeResponse(
                page([{"id": "two"}], 3, "https://openrouter.ai/final")
            ),
            "https://openrouter.ai/final": FakeResponse(page([{"id": "three"}], 3, None)),
        }
    )

    assert fetch_models("https://openrouter.ai/models", client) == [
        {"id": "one"},
        {"id": "two"},
        {"id": "three"},
    ]
    assert client.requests == [
        ("https://openrouter.ai/models", 30.0, False),
        ("https://openrouter.ai/models?offset=1", 30.0, False),
        ("https://openrouter.ai/final", 30.0, False),
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"total_count": 0, "links": {"next": None}}, "missing data"),
        ({"data": {}, "total_count": 0, "links": {"next": None}}, "invalid data"),
        ({"data": [], "total_count": True, "links": {"next": None}}, "total_count"),
        ({"data": [], "total_count": -1, "links": {"next": None}}, "total_count"),
        ({"data": [], "total_count": 0}, "missing links"),
        ({"data": [], "total_count": 0, "links": {}}, "missing next"),
        ({"data": [], "total_count": 0, "links": {"next": ""}}, "URL or null"),
    ],
)
def test_fetch_models_rejects_missing_or_invalid_page_fields(payload: object, message: str) -> None:
    client = FakeClient({"https://openrouter.ai/models": FakeResponse(payload)})
    with pytest.raises(CatalogSyncError, match=message):
        fetch_models("https://openrouter.ai/models", client)


def test_fetch_models_rejects_cycles_and_inconsistent_counts() -> None:
    cycling = FakeClient(
        {"https://openrouter.ai/models": FakeResponse(page([], 0, "https://openrouter.ai/models"))}
    )
    with pytest.raises(CatalogSyncError, match="pagination cycle"):
        fetch_models("https://openrouter.ai/models", cycling)

    inconsistent = FakeClient(
        {
            "https://openrouter.ai/models": FakeResponse(page([{"id": "one"}], 2, "/next")),
            "https://openrouter.ai/next": FakeResponse(page([{"id": "two"}], 3, None)),
        }
    )
    with pytest.raises(CatalogSyncError, match="total_count changed"):
        fetch_models("https://openrouter.ai/models", inconsistent)

    premature = FakeClient(
        {"https://openrouter.ai/models": FakeResponse(page([{"id": "one"}], 2, None))}
    )
    with pytest.raises(CatalogSyncError, match="expected 2"):
        fetch_models("https://openrouter.ai/models", premature)

    excess = FakeClient(
        {"https://openrouter.ai/models": FakeResponse(page([{"id": "one"}], 0, None))}
    )
    with pytest.raises(CatalogSyncError, match="exceeding total_count"):
        fetch_models("https://openrouter.ai/models", excess)


@pytest.mark.parametrize(
    "source_url",
    [
        "http://example.test/models",
        "https://user@example.test/models",
        "https://localhost/models",
        "https://127.0.0.1/models",
        "https://10.0.0.1/models",
        "https://169.254.1.1/models",
    ],
)
def test_fetch_models_rejects_unsafe_initial_urls(source_url: str) -> None:
    client = FakeClient({})
    with pytest.raises(CatalogSyncError, match="source URL"):
        fetch_models(source_url, client)
    assert client.requests == []


def test_fetch_models_rejects_cross_origin_redirects_and_page_exhaustion() -> None:
    cross_origin = FakeClient(
        {"https://openrouter.ai/models": FakeResponse(page([], 0, "https://other.test/models"))}
    )
    with pytest.raises(CatalogSyncError, match="changes origin"):
        fetch_models("https://openrouter.ai/models", cross_origin)

    redirect = FakeClient({"https://openrouter.ai/models": FakeResponse({}, status_code=302)})
    with pytest.raises(CatalogSyncError, match="redirect response rejected"):
        fetch_models("https://openrouter.ai/models", redirect)
    assert redirect.requests == [("https://openrouter.ai/models", 30.0, False)]

    responses: dict[str, FakeResponse] = {}
    for index in range(20):
        url = (
            "https://openrouter.ai/models" if index == 0 else f"https://openrouter.ai/page/{index}"
        )
        responses[url] = FakeResponse(page([{"id": f"model-{index}"}], 21, f"/page/{index + 1}"))
    exhausted = FakeClient(responses)
    with pytest.raises(CatalogSyncError, match="exceeds 20 pages"):
        fetch_models("https://openrouter.ai/models", exhausted)


def test_fetch_models_rejects_oversized_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, "MAX_RESPONSE_BYTES", 10)
    response = FakeResponse(page([], 0, None))
    client = FakeClient({"https://openrouter.ai/models": response})

    with pytest.raises(CatalogSyncError, match=r"response.*exceeds"):
        fetch_models("https://openrouter.ai/models", client)

    assert response.closed is True


def test_fetch_models_translates_stream_errors_and_closes_response() -> None:
    response = FakeResponse(
        page([], 0, None), stream_error=requests.ConnectionError("connection reset")
    )
    client = FakeClient({"https://openrouter.ai/models": response})

    with pytest.raises(CatalogSyncError, match=r"could not fetch.*connection reset"):
        fetch_models("https://openrouter.ai/models", client)

    assert response.closed is True


def test_generate_filters_models_and_sorts_stably() -> None:
    models = [
        model("zeta/model"),
        model("~temporary/model"),
        model("acme/model:batch"),
        model("acme/model:batch-capable"),
        model("excluded/model"),
        model("image/model", outputs=["image"]),
        model("parameterless/model", parameters=[]),
        model("expired/model", expiration="2026-09-03T23:59:59Z"),
        model("utc-expired/model", expiration="2026-09-04T00:30:00+02:00"),
        model("future/model", expiration="2026-09-04"),
        model("alpha/model"),
    ]
    policy = Policy({}, frozenset({"excluded/model"}))

    catalog, pricing = decoded(generate(models, policy, today=date(2026, 9, 3)))

    entries = catalog["openrouterCatalog"]["models"]
    assert [entry["upstreamModel"] for entry in entries] == [
        "acme/model:batch-capable",
        "alpha/model",
        "future/model",
        "zeta/model",
    ]
    assert list(pricing["providers"]["openrouter"]["models"]) == [
        "acme/model:batch-capable",
        "alpha/model",
        "future/model",
        "zeta/model",
    ]
    assert "description" not in str(catalog)


def test_alias_name_override_labels_and_collision() -> None:
    models = [
        model("google/gemini-2.5:free", name="  Google   DeepMind : Gemini  2.5 "),
        model("nous_research/hermes", name=None),
    ]
    policy = Policy(
        {"google/gemini-2.5:free": "remote/openrouter/google/legacy-gemini"}, frozenset()
    )

    catalog, _pricing = decoded(generate(models, policy, today=date(2026, 9, 3)))

    assert catalog["openrouterCatalog"]["models"] == [
        {
            "name": "remote/openrouter/google/legacy-gemini",
            "upstreamModel": "google/gemini-2.5:free",
            "label": "Gemini 2.5",
            "group": "Remote-OpenRouter-Google DeepMind",
        },
        {
            "name": "remote/openrouter/nous_research/hermes",
            "upstreamModel": "nous_research/hermes",
            "label": "nous_research/hermes",
            "group": "Remote-OpenRouter-nous research",
        },
    ]

    collision_policy = Policy(
        {"google/gemini-2.5:free": "remote/openrouter/nous_research/hermes"}, frozenset()
    )
    with pytest.raises(CatalogSyncError, match="public name collision"):
        generate(models, collision_policy, today=date(2026, 9, 3))


def test_default_alias_replaces_colon_and_rejects_bad_id() -> None:
    catalog, _pricing = decoded(
        generate([model("author/model:free")], empty_policy(), today=date(2026, 9, 3))
    )
    assert catalog["openrouterCatalog"]["models"][0]["name"] == (
        "remote/openrouter/author/model-free"
    )

    with pytest.raises(CatalogSyncError, match="author and slug"):
        generate([model("model")], empty_policy(), today=date(2026, 9, 3))


def test_pricing_tiers_inherit_overlap_and_apply_source_order() -> None:
    pricing: dict[str, object] = {
        "prompt": "0.000000123456789",
        "completion": "0.000002",
        "input_cache_read": "0",
        "input_cache_write": "0.0000031",
        "internal_reasoning": "0.000004",
        "audio": "0.000005",
        "request": "1.25",
        "overrides": [
            {
                "min_prompt_tokens": 200000,
                "prompt": "0.000000987654321",
                "request": "2",
            },
            {"min_prompt_tokens": 100000.0, "completion": "0.000003"},
            {"min_prompt_tokens": 200000, "prompt": "0.0000007777777"},
            {"min_prompt_tokens": 10, "utc_start": 800, "prompt": "9"},
            {"min_prompt_tokens": 20, "new_condition": True, "prompt": "9"},
            {"min_prompt_tokens": 30, "request": "9"},
        ],
    }

    _catalog, rendered = decoded(
        generate([model("acme/model", pricing=pricing)], empty_policy(), today=date(2026, 9, 3))
    )
    result = rendered["providers"]["openrouter"]["models"]["acme/model"]

    assert result == {
        "rates": {
            "input": "0.123457",
            "output": "2",
            "cacheRead": "0",
            "cacheWrite": "3.1",
            "reasoning": "4",
            "inputAudio": "5",
        },
        "tiers": [
            {
                "contextOver": 100000,
                "rates": {
                    "input": "0.123457",
                    "output": "3",
                    "cacheRead": "0",
                    "cacheWrite": "3.1",
                    "reasoning": "4",
                    "inputAudio": "5",
                },
            },
            {
                "contextOver": 200000,
                "rates": {
                    "input": "0.777778",
                    "output": "3",
                    "cacheRead": "0",
                    "cacheWrite": "3.1",
                    "reasoning": "4",
                    "inputAudio": "5",
                },
            },
        ],
    }


def test_tier_rates_inherit_omitted_base_fields() -> None:
    item = model(
        "acme/model",
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "overrides": [{"min_prompt_tokens": 10, "prompt": "0.000003"}],
        },
    )
    _catalog, pricing = decoded(generate([item], empty_policy(), today=date(2026, 9, 3)))
    assert pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"] == [
        {"contextOver": 10, "rates": {"input": "3", "output": "2"}}
    ]


def test_higher_tier_inherits_lower_threshold_changes() -> None:
    item = model(
        "acme/model",
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "overrides": [
                {"min_prompt_tokens": 10, "prompt": "0.000003"},
                {"min_prompt_tokens": 20, "completion": "0.000004"},
            ],
        },
    )
    _catalog, pricing = decoded(generate([item], empty_policy(), today=date(2026, 9, 3)))
    tiers = pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"]
    assert tiers[1] == {"contextOver": 20, "rates": {"input": "3", "output": "4"}}


def test_applicable_tier_overrides_apply_in_global_source_order() -> None:
    item = model(
        "acme/model",
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "overrides": [
                {"min_prompt_tokens": 20, "prompt": "0.000009"},
                {"min_prompt_tokens": 10, "prompt": "0.000003"},
            ],
        },
    )
    _catalog, pricing = decoded(generate([item], empty_policy(), today=date(2026, 9, 3)))
    tiers = pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"]
    assert tiers[1] == {"contextOver": 20, "rates": {"input": "3", "output": "2"}}


def test_negative_base_token_pricing_excludes_routing_models() -> None:
    models = [
        model("openrouter/auto", pricing={"prompt": "-1", "completion": "-1"}),
        model("acme/negative-output", pricing={"prompt": "0.1", "completion": "-0.1"}),
        model("acme/supported"),
    ]

    catalog, pricing = decoded(generate(models, empty_policy(), today=date(2026, 9, 3)))

    assert [entry["upstreamModel"] for entry in catalog["openrouterCatalog"]["models"]] == [
        "acme/supported"
    ]
    assert list(pricing["providers"]["openrouter"]["models"]) == ["acme/supported"]


def test_signed_zero_rates_normalize_to_zero() -> None:
    _catalog, pricing = decoded(
        generate(
            [model("acme/zero", pricing={"prompt": "-0", "completion": "+0.0000000"})],
            empty_policy(),
            today=date(2026, 9, 3),
        )
    )
    assert pricing["providers"]["openrouter"]["models"]["acme/zero"]["rates"] == {
        "input": "0",
        "output": "0",
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expiration_date": "tomorrow"}, "invalid expiration_date"),
        ({"pricing": {"prompt": 0.1}}, "decimal string"),
        (
            {"pricing": {"prompt": "not-a-decimal", "completion": "0.1"}},
            "pricing prompt",
        ),
        (
            {"pricing": {"prompt": "-1", "completion": "not-a-decimal"}},
            "pricing completion",
        ),
        (
            {"pricing": {"prompt": "1e-6", "completion": "0.1"}},
            "decimal string",
        ),
        (
            {"pricing": {"prompt": "1000000000000", "completion": "0.1"}},
            "decimal string",
        ),
        (
            {"pricing": {"prompt": f"0.{'1' * 65}", "completion": "0.1"}},
            "decimal string",
        ),
        ({"pricing": {"prompt": "0.1"}}, "pricing completion"),
        ({"architecture": None}, "architecture"),
    ],
)
def test_generate_rejects_malformed_required_data(change: dict[str, object], message: str) -> None:
    item = model("acme/model")
    item.update(change)
    with pytest.raises(CatalogSyncError, match=message):
        generate([item], empty_policy(), today=date(2026, 9, 3))


def test_generate_rejects_duplicate_ids_and_thresholds() -> None:
    item = model("acme/model")
    with pytest.raises(CatalogSyncError, match="duplicate model id"):
        generate([item, item], empty_policy(), today=date(2026, 9, 3))

    item = model(
        "acme/model",
        pricing={
            "prompt": "0.1",
            "completion": "0.2",
            "overrides": [
                {"min_prompt_tokens": 10, "prompt": "0.2"},
                {"min_prompt_tokens": 10, "completion": "0.3"},
            ],
        },
    )
    _catalog, pricing = decoded(generate([item], empty_policy(), today=date(2026, 9, 3)))
    assert pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"] == [
        {"contextOver": 10, "rates": {"input": "200000", "output": "300000"}}
    ]


@pytest.mark.parametrize("threshold", [True, 1.5, -1, float("inf"), 2_147_483_648])
def test_generate_rejects_invalid_thresholds(threshold: object) -> None:
    item = model(
        "acme/model",
        pricing={
            "prompt": "0.1",
            "completion": "0.2",
            "overrides": [{"min_prompt_tokens": threshold, "prompt": "0.3"}],
        },
    )
    with pytest.raises(CatalogSyncError, match="invalid min_prompt_tokens"):
        generate([item], empty_policy(), today=date(2026, 9, 3))


def test_generated_model_limit_accepts_512_and_rejects_513() -> None:
    models = [model(f"author/model-{index}") for index in range(513)]
    catalog, _pricing = decoded(generate(models[:512], empty_policy(), today=date(2026, 9, 3)))
    assert len(catalog["openrouterCatalog"]["models"]) == 512
    with pytest.raises(CatalogSyncError, match="exceeds 512 models"):
        generate(models, empty_policy(), today=date(2026, 9, 3))


def test_load_policy_defaults_and_validation(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    assert load_policy(policy_path) == empty_policy()

    policy_path.write_text('{"unknown": true}', encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="unknown field"):
        load_policy(policy_path)

    policy_path.write_text('{"publicNameOverrides": {"acme/model": "bad name"}}', encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="invalid public name"):
        load_policy(policy_path)

    policy_path.write_text('{"excludedModels": ["same", "same"]}', encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="duplicates"):
        load_policy(policy_path)

    policy_path.write_text('{"excludedModels": [], "excludedModels": []}', encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="duplicate JSON key: excludedModels"):
        load_policy(policy_path)

    policy_path.write_text(
        '{"publicNameOverrides": {"acme/model": "one", "acme/model": "two"}}',
        encoding="utf-8",
    )
    with pytest.raises(CatalogSyncError, match="duplicate JSON key: acme/model"):
        load_policy(policy_path)


def test_write_then_check_is_deterministic_and_reports_both_mismatches(tmp_path: Path) -> None:
    generated = generate([model("acme/model")], empty_policy(), today=date(2026, 9, 3))
    catalog_path = tmp_path / "nested" / "catalog.yaml"
    pricing_path = tmp_path / "other" / "pricing.json"

    write_files(generated, catalog_path, pricing_path)
    check_files(generated, catalog_path, pricing_path)
    assert catalog_path.read_bytes() == generated.catalog
    assert pricing_path.read_bytes() == generated.pricing

    catalog_path.write_text("stale", encoding="utf-8")
    pricing_path.unlink()
    with pytest.raises(CatalogSyncError) as error:
        check_files(generated, catalog_path, pricing_path)
    assert "catalog output is out of date" in str(error.value)
    assert "pricing output is missing" in str(error.value)


def test_output_paths_must_differ(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    output = tmp_path / "same"
    with pytest.raises(CatalogSyncError, match="must differ"):
        write_files(generated, output, output)
    with pytest.raises(CatalogSyncError, match="must differ"):
        check_files(generated, output, output)


def test_output_paths_must_not_contain_one_another(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    catalog = tmp_path / "catalog"
    pricing = catalog / "pricing"

    with pytest.raises(CatalogSyncError, match="must not contain"):
        write_files(generated, catalog, pricing)
    with pytest.raises(CatalogSyncError, match="must not contain"):
        check_files(generated, catalog, pricing)

    assert not catalog.exists()


def test_generate_rejects_oversized_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_OUTPUT_BYTES", 100)

    with pytest.raises(CatalogSyncError, match="generated catalog exceeds"):
        generate([model("acme/model")], empty_policy(), today=date(2026, 9, 3))


def test_write_rejects_non_regular_existing_outputs(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    directory = tmp_path / "catalog"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"target")
    symlink = tmp_path / "pricing"
    symlink.symlink_to(target)

    with pytest.raises(CatalogSyncError, match="regular file"):
        write_files(generated, directory, tmp_path / "new-pricing")
    with pytest.raises(CatalogSyncError, match="regular file"):
        write_files(generated, tmp_path / "new-catalog", symlink)
    with pytest.raises(CatalogSyncError, match="regular file"):
        check_files(generated, directory, tmp_path / "new-pricing")
    with pytest.raises(CatalogSyncError, match="regular file"):
        check_files(generated, tmp_path / "new-catalog", symlink)

    assert directory.is_dir()
    assert symlink.is_symlink()
    assert target.read_bytes() == b"target"


@pytest.mark.parametrize("pricing_exists", [True, False])
def test_write_rolls_back_pair_when_second_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pricing_exists: bool
) -> None:
    generated = generate([model("acme/model")], empty_policy(), today=date(2026, 9, 3))
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    catalog_path.write_bytes(b"old catalog")
    if pricing_exists:
        pricing_path.write_bytes(b"old pricing")
    original_replace = Path.replace
    failed = False

    def replace_with_fault(source: Path, target: Path) -> Path:
        nonlocal failed
        if not failed and ".stage." in source.name and target == pricing_path:
            failed = True
            raise OSError("injected second replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_fault)

    with pytest.raises(CatalogSyncError, match="injected second replacement failure"):
        write_files(generated, catalog_path, pricing_path)

    assert catalog_path.read_bytes() == b"old catalog"
    if pricing_exists:
        assert pricing_path.read_bytes() == b"old pricing"
    else:
        assert not pricing_path.exists()
    assert list(tmp_path.glob(".*.stage.*")) == []
    assert list(tmp_path.glob(".*.backup.*")) == []


def test_write_preserves_backup_when_rollback_restore_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generated = generate([model("acme/model")], empty_policy(), today=date(2026, 9, 3))
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    catalog_path.write_bytes(b"old catalog")
    pricing_path.write_bytes(b"old pricing")
    original_replace = Path.replace

    def replace_with_fault(source: Path, target: Path) -> Path:
        if ".stage." in source.name and target == pricing_path:
            raise OSError("injected replacement failure")
        if ".backup." in source.name and target == pricing_path:
            raise OSError("injected restore failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_fault)

    with pytest.raises(CatalogSyncError, match=r"rollback failed.*could not restore"):
        write_files(generated, catalog_path, pricing_path)

    assert catalog_path.read_bytes() == b"old catalog"
    assert not pricing_path.exists()
    backups = list(tmp_path.glob(".pricing.json.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old pricing"
    assert list(tmp_path.glob(".*.stage.*")) == []


def test_validate_paths_rejects_relative_symlink_and_hardlink_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    other = tmp_path / "other"

    monkeypatch.chdir(tmp_path)
    with pytest.raises(CatalogSyncError, match="policy and catalog output"):
        validate_paths(Path("policy.json"), Path("./policy.json"), other)

    symlink = tmp_path / "policy-link.json"
    symlink.symlink_to(policy)
    with pytest.raises(CatalogSyncError, match="policy and catalog output"):
        validate_paths(policy, symlink, other)

    hardlink = tmp_path / "policy-hardlink.json"
    hardlink.hardlink_to(policy)
    with pytest.raises(CatalogSyncError, match="policy and pricing output"):
        validate_paths(policy, other, hardlink)
