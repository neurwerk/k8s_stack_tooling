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
    selection_choices,
    validate_paths,
    with_selected_models,
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
    return Policy((), {}, {}, {}, True)


def selected_policy(*upstream_ids: str) -> Policy:
    return Policy(tuple(upstream_ids), {}, {}, {}, True)


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


def test_selection_filters_models_and_generate_sorts_selected_stably() -> None:
    models = [
        model("zeta/model"),
        model("~temporary/model"),
        model("acme/model:batch"),
        model("acme/model:batch-capable"),
        model("openrouter/free"),
        model("image/model", outputs=["image"]),
        model("parameterless/model", parameters=[]),
        model("expired/model", expiration="2026-09-03T23:59:59Z"),
        model("utc-expired/model", expiration="2026-09-04T00:30:00+02:00"),
        model("future/model", expiration="2026-09-04"),
        model("alpha/model"),
    ]
    policy = selected_policy(
        "zeta/model", "future/model", "alpha/model", "acme/model:batch-capable"
    )

    choices = selection_choices(models, policy, today=date(2026, 9, 3))

    catalog, pricing = decoded(generate(models, policy, today=date(2026, 9, 3)))

    assert [choice.upstream_id for choice in choices] == [
        "acme/model:batch-capable",
        "alpha/model",
        "future/model",
        "zeta/model",
    ]
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
    assert {entry["upstreamModel"] for entry in entries} == set(
        pricing["providers"]["openrouter"]["models"]
    )
    assert "description" not in str(catalog)


def test_selection_exposes_stale_models_as_preselected_removable_choices() -> None:
    models = [model("acme/good"), model("image/model", outputs=["image"])]
    schedule = {"rates": {"input": "1", "output": "2"}}
    policy = Policy(
        ("missing/model", "image/model", "acme/good"),
        {},
        {"acme/good": schedule},
        {},
        True,
    )

    choices = selection_choices(models, policy, today=date(2026, 9, 3))

    assert [(choice.upstream_id, choice.display_name) for choice in choices] == [
        ("acme/good", "Useful Model [acme/good]"),
        ("image/model", "Unavailable (incompatible) [image/model]"),
        ("missing/model", "Unavailable (unknown) [missing/model]"),
    ]
    cleaned = with_selected_models(policy, ["acme/good"])
    assert cleaned.negotiated_pricing == {"acme/good": schedule}
    generate(models, cleaned, today=date(2026, 9, 3))


def test_alias_name_override_labels_and_collision() -> None:
    models = [
        model("google/gemini-2.5:free", name="  Google   DeepMind : Gemini  2.5 "),
        model("nous_research/hermes", name=None),
    ]
    policy = Policy(
        ("google/gemini-2.5:free", "nous_research/hermes"),
        {"google/gemini-2.5:free": "remote/openrouter/google/legacy-gemini"},
        {},
        {},
        True,
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
            "group": "Remote-OpenRouter-Nous Research",
        },
    ]

    collision_policy = Policy(
        ("google/gemini-2.5:free", "nous_research/hermes"),
        {"google/gemini-2.5:free": "remote/openrouter/nous_research/hermes"},
        {},
        {},
        True,
    )
    with pytest.raises(CatalogSyncError, match="public name collision"):
        generate(models, collision_policy, today=date(2026, 9, 3))


def test_default_alias_replaces_colon_and_rejects_bad_id() -> None:
    catalog, _pricing = decoded(
        generate(
            [model("author/model:free")],
            selected_policy("author/model:free"),
            today=date(2026, 9, 3),
        )
    )
    assert catalog["openrouterCatalog"]["models"][0]["name"] == (
        "remote/openrouter/author/model-free"
    )

    with pytest.raises(CatalogSyncError, match="author and slug"):
        generate([model("model")], selected_policy("model"), today=date(2026, 9, 3))


def test_pricing_tiers_inherit_overlap_and_apply_source_order() -> None:
    pricing: dict[str, object] = {
        "prompt": "0.000000123456789",
        "completion": "0.000002",
        "input_cache_read": "0",
        "input_cache_write": "0.0000031",
        "input_cache_write_1h": "0.0000035",
        "input_audio_cache": "0.0000009",
        "internal_reasoning": "0.000004",
        "audio": "0.000005",
        "audio_output": "0.000006",
        "request": "0",
        "overrides": [
            {
                "min_prompt_tokens": 200000,
                "prompt": "0.000000987654321",
                "request": "0",
            },
            {"min_prompt_tokens": 100000.0, "completion": "0.000003"},
            {"min_prompt_tokens": 200000, "prompt": "0.0000007777777"},
            {"min_prompt_tokens": 30, "request": "0"},
        ],
    }

    _catalog, rendered = decoded(
        generate(
            [model("acme/model", pricing=pricing)],
            selected_policy("acme/model"),
            today=date(2026, 9, 3),
        )
    )
    result = rendered["providers"]["openrouter"]["models"]["acme/model"]

    assert result == {
        "rates": {
            "input": "0.123457",
            "output": "2",
            "cacheRead": "0.9",
            "cacheWrite": "3.5",
            "reasoning": "4",
            "inputAudio": "5",
            "outputAudio": "6",
        },
        "tiers": [
            {
                "contextOver": 100000,
                "rates": {
                    "input": "0.123457",
                    "output": "3",
                    "cacheRead": "0.9",
                    "cacheWrite": "3.5",
                    "reasoning": "4",
                    "inputAudio": "5",
                    "outputAudio": "6",
                },
            },
            {
                "contextOver": 200000,
                "rates": {
                    "input": "0.777778",
                    "output": "3",
                    "cacheRead": "0.9",
                    "cacheWrite": "3.5",
                    "reasoning": "4",
                    "inputAudio": "5",
                    "outputAudio": "6",
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
    _catalog, pricing = decoded(
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))
    )
    assert pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"] == [
        {"contextOver": 10, "rates": {"input": "3", "output": "2"}}
    ]


def test_tier_cache_aliases_use_effective_source_rates() -> None:
    item = model(
        "acme/model",
        pricing={
            "prompt": "0.000001",
            "completion": "0.000002",
            "input_cache_read": "0.0000005",
            "input_audio_cache": "0.0000004",
            "input_cache_write": "0.000003",
            "input_cache_write_1h": "0.000004",
            "overrides": [
                {
                    "min_prompt_tokens": 10,
                    "prompt": "0.000002",
                    "input_audio_cache": "0.0000006",
                    "input_cache_write": "0.0000035",
                },
                {
                    "min_prompt_tokens": 20,
                    "input_cache_read": "0.0000007",
                    "input_cache_write_1h": "0.000005",
                },
            ],
        },
    )

    _catalog, pricing = decoded(
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))
    )

    assert pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"] == [
        {
            "contextOver": 10,
            "rates": {
                "input": "2",
                "output": "2",
                "cacheRead": "0.6",
                "cacheWrite": "4",
            },
        },
        {
            "contextOver": 20,
            "rates": {
                "input": "2",
                "output": "2",
                "cacheRead": "0.7",
                "cacheWrite": "5",
            },
        },
    ]


def test_unrepresentable_fetched_pricing_requires_reviewed_pricing() -> None:
    for override in (
        {"min_prompt_tokens": 10, "utc_start": 800, "prompt": "0.000003"},
        {"min_prompt_tokens": 10, "discount": "0.5", "prompt": "0.000003"},
    ):
        item = model(
            "acme/model",
            pricing={
                "prompt": "0.000001",
                "completion": "0.000002",
                "overrides": [override],
            },
        )
        with pytest.raises(CatalogSyncError, match=r"negotiatedPricing|unknown field"):
            generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))


@pytest.mark.parametrize("location", ["base", "override"])
def test_positive_per_request_pricing_requires_reviewed_pricing(location: str) -> None:
    pricing: dict[str, object] = {
        "prompt": "0.000001",
        "completion": "0.000002",
        "request": "0.01" if location == "base" else "0",
    }
    if location == "override":
        pricing["overrides"] = [{"min_prompt_tokens": 10, "prompt": "0.000003", "request": "0.01"}]

    with pytest.raises(CatalogSyncError, match=r"per-request pricing.*negotiatedPricing"):
        generate(
            [model("acme/model", pricing=pricing)],
            selected_policy("acme/model"),
            today=date(2026, 9, 3),
        )


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
    _catalog, pricing = decoded(
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))
    )
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
    _catalog, pricing = decoded(
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))
    )
    tiers = pricing["providers"]["openrouter"]["models"]["acme/model"]["tiers"]
    assert tiers[1] == {"contextOver": 20, "rates": {"input": "3", "output": "2"}}


def test_negative_base_token_pricing_excludes_routing_models_from_selection() -> None:
    models = [
        model("openrouter/auto", pricing={"prompt": "-1", "completion": "-1"}),
        model("acme/negative-output", pricing={"prompt": "0.1", "completion": "-0.1"}),
        model("acme/supported"),
    ]

    choices = selection_choices(models, selected_policy("acme/supported"), today=date(2026, 9, 3))
    catalog, pricing = decoded(
        generate(models, selected_policy("acme/supported"), today=date(2026, 9, 3))
    )

    assert [choice.upstream_id for choice in choices] == ["acme/supported"]
    assert [entry["upstreamModel"] for entry in catalog["openrouterCatalog"]["models"]] == [
        "acme/supported"
    ]
    assert list(pricing["providers"]["openrouter"]["models"]) == ["acme/supported"]


def test_signed_zero_rates_normalize_to_zero() -> None:
    _catalog, pricing = decoded(
        generate(
            [model("acme/zero", pricing={"prompt": "-0", "completion": "+0.0000000"})],
            selected_policy("acme/zero"),
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
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))


def test_generate_rejects_duplicate_ids_and_thresholds() -> None:
    item = model("acme/model")
    with pytest.raises(CatalogSyncError, match="duplicate model id"):
        generate([item, item], selected_policy("acme/model"), today=date(2026, 9, 3))

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
    _catalog, pricing = decoded(
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))
    )
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
        generate([item], selected_policy("acme/model"), today=date(2026, 9, 3))


def test_selected_model_limit_accepts_256_and_rejects_257() -> None:
    models = [model(f"author/model-{index}") for index in range(257)]
    selected = tuple(f"author/model-{index}" for index in range(256))
    catalog, _pricing = decoded(
        generate(models, selected_policy(*selected), today=date(2026, 9, 3))
    )
    assert len(catalog["openrouterCatalog"]["models"]) == 256
    with pytest.raises(CatalogSyncError, match="exceeds 256 models"):
        generate(
            models,
            selected_policy(*(f"author/model-{index}" for index in range(257))),
            today=date(2026, 9, 3),
        )


def test_load_policy_requires_complete_contract_and_validates_it(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text("{}", encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="missing required field"):
        load_policy(policy_path)

    valid = {
        "selectedModels": ["acme/model"],
        "publicNameOverrides": {},
        "negotiatedPricing": {},
        "customPricing": {},
        "grantToAccessGroups": True,
    }
    policy_path.write_text(json.dumps(valid), encoding="utf-8")
    assert load_policy(policy_path) == selected_policy("acme/model")

    policy_path.write_text('{"unknown": true}', encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="unknown field"):
        load_policy(policy_path)

    invalid = valid | {"publicNameOverrides": {"acme/model": "bad name"}}
    policy_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="invalid public name"):
        load_policy(policy_path)

    invalid = valid | {"selectedModels": ["same", "same"]}
    policy_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="duplicates"):
        load_policy(policy_path)

    invalid = valid | {"grantToAccessGroups": 1}
    policy_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(CatalogSyncError, match="must be a boolean"):
        load_policy(policy_path)

    policy_path.write_text(
        '{"selectedModels": [], "selectedModels": [], '
        '"publicNameOverrides": {}, "negotiatedPricing": {}, "customPricing": {}}',
        encoding="utf-8",
    )
    with pytest.raises(CatalogSyncError, match="duplicate JSON key: selectedModels"):
        load_policy(policy_path)

    policy_path.write_text(
        '{"selectedModels": [], "publicNameOverrides": '
        '{"acme/model": "one", "acme/model": "two"}, '
        '"negotiatedPricing": {}, "customPricing": {}}',
        encoding="utf-8",
    )
    with pytest.raises(CatalogSyncError, match="duplicate JSON key: acme/model"):
        load_policy(policy_path)


def test_reviewed_pricing_is_complete_and_canonical(tmp_path: Path) -> None:
    raw_policy = {
        "selectedModels": ["acme/model"],
        "publicNameOverrides": {},
        "negotiatedPricing": {
            "acme/model": {
                "rates": {"input": "+0001.2300000", "output": "-0.0000000"},
                "tiers": [
                    {
                        "contextOver": 100000,
                        "rates": {"input": "0.000001", "output": "2.500000"},
                    }
                ],
            }
        },
        "customPricing": {
            "custom": {"llama3.2:3b": {"rates": {"input": "0", "output": "0"}}},
            "deepseek": {
                "deepseek-chat": {
                    "rates": {"input": "0.14", "output": "0.28", "cacheRead": "0.0028"}
                }
            },
            "openrouter": {"legacy/model": {"rates": {"input": "0.5", "output": "1"}}},
        },
        "grantToAccessGroups": False,
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(raw_policy), encoding="utf-8")
    policy = load_policy(policy_path)

    catalog, pricing = decoded(generate([model("acme/model")], policy, today=date(2026, 9, 3)))

    assert catalog["openrouterCatalog"]["grantToAccessGroups"] is False
    assert catalog["infraAgentgatewayWrapper"] == {
        "modelCatalog": {
            "sources": [
                {
                    "configMap": {
                        "name": "client-model-cost-catalog",
                        "key": "catalog.json",
                    }
                }
            ]
        }
    }
    assert policy.negotiated_pricing["acme/model"] == {
        "rates": {"input": "1.23", "output": "0"},
        "tiers": [{"contextOver": 100000, "rates": {"input": "0.000001", "output": "2.5"}}],
    }
    providers = pricing["providers"]
    assert list(providers) == ["custom", "deepseek", "openrouter"]
    assert providers["custom"]["models"] == {
        "llama3.2:3b": {"rates": {"input": "0", "output": "0"}}
    }
    assert providers["deepseek"]["models"] == {
        "deepseek-chat": {"rates": {"input": "0.14", "output": "0.28", "cacheRead": "0.0028"}}
    }
    assert providers["openrouter"]["models"] == {
        "acme/model": policy.negotiated_pricing["acme/model"],
        "legacy/model": {"rates": {"input": "0.5", "output": "1"}},
    }


def test_custom_openrouter_pricing_must_not_overlap_selected_models() -> None:
    schedule = {"rates": {"input": "1", "output": "2"}}
    policy = Policy(
        ("acme/model",),
        {},
        {},
        {"openrouter": {"acme/model": schedule}},
        True,
    )

    with pytest.raises(CatalogSyncError, match="also selected: acme/model"):
        generate([model("acme/model")], policy, today=date(2026, 9, 3))


@pytest.mark.parametrize(
    ("custom_pricing", "message"),
    [
        ({"DeepSeek": {}}, "invalid customPricing provider id"),
        (
            {"deepseek": {"bad model": {"rates": {"input": "1", "output": "2"}}}},
            "invalid customPricing provider deepseek model id",
        ),
        (
            {"deepseek": {"deepseek-chat": {"rates": {"input": "1"}}}},
            "missing rate: output",
        ),
        (
            {"deepseek": {"deepseek-chat": {"rates": {"input": "-1", "output": "0"}}}},
            "invalid custom pricing",
        ),
        (
            {"deepseek": {"deepseek-chat": {"rates": {"input": "0.0000001", "output": "1"}}}},
            "at most six fractional digits",
        ),
    ],
)
def test_custom_pricing_rejects_collisions_and_invalid_identifiers(
    tmp_path: Path, custom_pricing: object, message: str
) -> None:
    raw_policy = {
        "selectedModels": [],
        "publicNameOverrides": {},
        "negotiatedPricing": {},
        "customPricing": custom_pricing,
        "grantToAccessGroups": True,
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(raw_policy), encoding="utf-8")

    with pytest.raises(CatalogSyncError, match=message):
        load_policy(policy_path)


@pytest.mark.parametrize(
    ("selected", "message"),
    [(("missing/model",), "unknown"), (("image/model",), "incompatible")],
)
def test_selected_models_fail_closed(selected: tuple[str, ...], message: str) -> None:
    models = [model("image/model", outputs=["image"])]
    with pytest.raises(CatalogSyncError, match=message):
        generate(models, selected_policy(*selected), today=date(2026, 9, 3))


def test_public_name_metadata_limit_uses_compact_utf8_map() -> None:
    count = 122
    upstream_ids = tuple(f"author/model-{index}" for index in range(count))
    overrides = {
        upstream_id: f"n{index:03d}" + "x" * 124 for index, upstream_id in enumerate(upstream_ids)
    }
    policy = Policy(upstream_ids, overrides, {}, {}, True)

    with pytest.raises(CatalogSyncError, match="16384 UTF-8 bytes"):
        generate(
            [model(upstream_id) for upstream_id in upstream_ids],
            policy,
            today=date(2026, 9, 3),
        )


def test_write_then_check_is_deterministic_and_reports_both_mismatches(tmp_path: Path) -> None:
    generated = generate(
        [model("acme/model")], selected_policy("acme/model"), today=date(2026, 9, 3)
    )
    policy_path = tmp_path / "policy.json"
    catalog_path = tmp_path / "nested" / "catalog.yaml"
    pricing_path = tmp_path / "other" / "pricing.json"

    write_files(generated, policy_path, catalog_path, pricing_path)
    check_files(generated, policy_path, catalog_path, pricing_path)
    assert policy_path.read_bytes() == generated.policy
    assert catalog_path.read_bytes() == generated.catalog
    assert pricing_path.read_bytes() == generated.pricing

    catalog_path.write_text("stale", encoding="utf-8")
    pricing_path.unlink()
    with pytest.raises(CatalogSyncError) as error:
        check_files(generated, policy_path, catalog_path, pricing_path)
    assert "catalog output is out of date" in str(error.value)
    assert "pricing output is missing" in str(error.value)


def test_output_paths_must_differ(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    output = tmp_path / "same"
    with pytest.raises(CatalogSyncError, match="must differ"):
        write_files(generated, tmp_path / "policy", output, output)
    with pytest.raises(CatalogSyncError, match="must differ"):
        check_files(generated, tmp_path / "policy", output, output)


def test_output_paths_must_not_contain_one_another(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    catalog = tmp_path / "catalog"
    pricing = catalog / "pricing"

    with pytest.raises(CatalogSyncError, match="must not contain"):
        write_files(generated, tmp_path / "policy", catalog, pricing)
    with pytest.raises(CatalogSyncError, match="must not contain"):
        check_files(generated, tmp_path / "policy", catalog, pricing)

    assert not catalog.exists()


def test_generate_rejects_oversized_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "MAX_OUTPUT_BYTES", 100)

    with pytest.raises(CatalogSyncError, match="generated policy exceeds"):
        generate([model("acme/model")], selected_policy("acme/model"), today=date(2026, 9, 3))


def test_write_rejects_non_regular_existing_outputs(tmp_path: Path) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    directory = tmp_path / "catalog"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"target")
    symlink = tmp_path / "pricing"
    symlink.symlink_to(target)

    with pytest.raises(CatalogSyncError, match="regular file"):
        write_files(generated, tmp_path / "policy", directory, tmp_path / "new-pricing")
    with pytest.raises(CatalogSyncError, match="regular file"):
        write_files(generated, tmp_path / "policy", tmp_path / "new-catalog", symlink)
    with pytest.raises(CatalogSyncError, match="regular file"):
        check_files(generated, tmp_path / "policy", directory, tmp_path / "new-pricing")
    with pytest.raises(CatalogSyncError, match="regular file"):
        check_files(generated, tmp_path / "policy", tmp_path / "new-catalog", symlink)

    assert directory.is_dir()
    assert symlink.is_symlink()
    assert target.read_bytes() == b"target"


@pytest.mark.parametrize(
    ("pricing_exists", "failure", "expected"),
    [
        (True, OSError("injected final replacement failure"), CatalogSyncError),
        (False, OSError("injected final replacement failure"), CatalogSyncError),
        (True, KeyboardInterrupt("injected final replacement failure"), KeyboardInterrupt),
    ],
)
def test_write_rolls_back_all_files_when_final_replace_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pricing_exists: bool,
    failure: BaseException,
    expected: type[BaseException],
) -> None:
    generated = generate(
        [model("acme/model")], selected_policy("acme/model"), today=date(2026, 9, 3)
    )
    policy_path = tmp_path / "policy.json"
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    policy_path.write_bytes(b"old policy")
    catalog_path.write_bytes(b"old catalog")
    if pricing_exists:
        pricing_path.write_bytes(b"old pricing")
    original_replace = Path.replace
    failed = False

    def replace_with_fault(source: Path, target: Path) -> Path:
        nonlocal failed
        if not failed and ".stage." in source.name and target == pricing_path:
            failed = True
            raise failure
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_fault)

    with pytest.raises(expected, match="injected final replacement failure"):
        write_files(generated, policy_path, catalog_path, pricing_path)

    assert policy_path.read_bytes() == b"old policy"
    assert catalog_path.read_bytes() == b"old catalog"
    if pricing_exists:
        assert pricing_path.read_bytes() == b"old pricing"
    else:
        assert not pricing_path.exists()
    assert list(tmp_path.glob(".*.stage.*")) == []
    assert list(tmp_path.glob(".*.backup.*")) == []


def test_write_rolls_back_replace_that_succeeds_before_interruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generated = generate([], empty_policy(), today=date(2026, 9, 3))
    policy_path = tmp_path / "policy.json"
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    paths = (policy_path, catalog_path, pricing_path)
    originals = (b"old policy", b"old catalog", b"old pricing")
    for path, content in zip(paths, originals, strict=True):
        path.write_bytes(content)
    original_replace = Path.replace

    def replace_then_interrupt(source: Path, target: Path) -> Path:
        result = original_replace(source, target)
        if ".stage." in source.name and target == catalog_path:
            raise KeyboardInterrupt("interrupted after replacement")
        return result

    monkeypatch.setattr(Path, "replace", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt, match="interrupted after replacement"):
        write_files(generated, policy_path, catalog_path, pricing_path)

    assert tuple(path.read_bytes() for path in paths) == originals
    assert list(tmp_path.glob(".*.stage.*")) == []
    assert list(tmp_path.glob(".*.backup.*")) == []


def test_write_preserves_backup_when_rollback_restore_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generated = generate(
        [model("acme/model")], selected_policy("acme/model"), today=date(2026, 9, 3)
    )
    policy_path = tmp_path / "policy.json"
    catalog_path = tmp_path / "catalog.yaml"
    pricing_path = tmp_path / "pricing.json"
    policy_path.write_bytes(b"old policy")
    catalog_path.write_bytes(b"old catalog")
    pricing_path.write_bytes(b"old pricing")
    original_replace = Path.replace

    def replace_with_fault(source: Path, target: Path) -> Path:
        if ".stage." in source.name and target == pricing_path:
            raise OSError("injected replacement failure")
        if ".backup." in source.name and target == catalog_path:
            raise OSError("injected restore failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_with_fault)

    with pytest.raises(CatalogSyncError, match=r"rollback failed.*could not restore"):
        write_files(generated, policy_path, catalog_path, pricing_path)

    assert policy_path.read_bytes() == b"old policy"
    assert catalog_path.read_bytes() == generated.catalog
    assert pricing_path.read_bytes() == b"old pricing"
    backups = list(tmp_path.glob(".catalog.yaml.backup.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"old catalog"
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
