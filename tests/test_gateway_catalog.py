"""Neutral metadata selection works without a product's chat or billing policy."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from k8s_stack_tooling import model_metadata_refresh as refresh
from k8s_stack_tooling.gateway_catalog.catalog import GatewayCatalog
from k8s_stack_tooling.gateway_catalog.metadata import MetadataError, load_json


def catalog():
    # PR #173's non-chat, caller-supplied case: neither policy belongs in refresh.
    def entry(name):
        return {
            "id": name,
            "provider_id": "deepseek",
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
                    "id": name + "/model",
                    "kind": "model",
                    "display_name": name,
                    "endpoint_ids": [name],
                    "profile_ids": [],
                    "required_permissions": [f"model:{name}/model:invoke"],
                    "credential_modes": [{"source": "caller_supplied", "required_permissions": []}],
                    "provider_id": "deepseek",
                }
            ],
        }

    return GatewayCatalog.model_validate(
        {
            "schema_version": 2,
            "catalog_revision": "a" * 64,
            "policy_revision": "b" * 64,
            "profiles": [],
            "self-hosted-api-providers": [],
            "external-api-providers": [entry("cloud"), entry("unrequested")],
            "external-subscription-providers": [],
            "mcp-servers": [],
        }
    )


def sources(binding="cloud"):
    return refresh.RefreshSources.model_validate(
        {"bindings": {binding: {"kind": "deepseek", "source": "https://api.deepseek.com/models"}}}
    )


def test_explicit_refresh_is_independent_of_chat_profile_and_billing_mode(monkeypatch):
    values = {
        "external-api-providers": [
            {
                "id": "cloud",
                "enabled": True,
                "providerId": "deepseek",
                "upstreamURL": "https://api.deepseek.com/v1",
                "models": [{"name": "cloud/model", "upstreamModel": "native"}],
            }
        ]
    }
    fetch = Mock(
        return_value={
            "data": [
                {
                    "id": "native",
                    "context_window": 12000,
                    "max_output_tokens": 4000,
                }
            ]
        }
    )
    monkeypatch.setattr(refresh, "fetch_json", fetch)
    rows, counts = refresh.refresh(catalog(), values, sources())
    assert [(row["binding_id"], row["public_id"]) for row in rows] == [("cloud", "cloud/model")]
    assert counts == {"selected": 1, "refreshed": 1}
    fetch.assert_called_once()
    assert refresh.source_pairs(catalog(), refresh.RefreshSources()) == set()


def test_unknown_source_fails_before_provider_access(monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(refresh, "fetch_json", fetch)
    with pytest.raises(MetadataError, match="unknown API binding"):
        refresh.refresh(catalog(), {}, sources("absent"))
    fetch.assert_not_called()


def test_api_route_must_belong_to_selected_destination():
    raw = catalog().model_dump(by_alias=True)
    entry = raw["external-api-providers"][0]
    entry["routes"].append(
        {
            "id": "proxy",
            "url": "https://gateway.example.test/proxy",
            "entry_mode": "forward_proxy",
            "caller_auth_modes": ["proxy_basic"],
        }
    )
    entry["destinations"][0]["endpoint_ids"] = ["proxy"]
    assert refresh.source_pairs(GatewayCatalog.model_validate(raw), sources()) == set()


@pytest.mark.parametrize("mutation", ["endpoint", "provider", "duplicate", "schema"])
def test_catalog_reference_and_identity_rules_survive_extraction(mutation):
    raw = catalog().model_dump(by_alias=True)
    entries = raw["external-api-providers"]
    if mutation == "endpoint":
        entries[0]["destinations"][0]["endpoint_ids"] = ["unrequested"]
    elif mutation == "provider":
        entries[0]["destinations"][0]["provider_id"] = "other"
    elif mutation == "duplicate":
        entries.append(entries[0])
    else:
        raw["schema_version"] = True
    with pytest.raises(ValidationError):
        GatewayCatalog.model_validate(raw)


def test_metadata_document_budget_counts_utf8_bytes():
    text = '{"text":"é"}'
    assert load_json(text, max_bytes=len(text.encode())) == {"text": "é"}
    with pytest.raises(MetadataError, match="size limit"):
        load_json(text, max_bytes=len(text.encode()) - 1)
