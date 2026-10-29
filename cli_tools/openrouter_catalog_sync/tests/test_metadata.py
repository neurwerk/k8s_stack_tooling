from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from k8s_stack_tooling.gateway_catalog.metadata import MetadataError

from openrouter_catalog_sync.metadata import retain_metadata, with_metadata
from openrouter_catalog_sync.sync import CatalogSyncError, GeneratedFiles


def generated() -> GeneratedFiles:
    return GeneratedFiles(
        b"policy",
        yaml.safe_dump(
            {
                "openrouterCatalog": {
                    "models": [
                        {
                            "name": "remote/relay/chat",
                            "upstreamModel": "publisher/chat",
                            "label": "Chat",
                            "group": "Remote",
                        }
                    ]
                }
            }
        ).encode(),
        b"pricing",
    )


def snapshot() -> tuple[str, str]:
    now = datetime.now(UTC)
    return (
        (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )


def values(tmp_path: Path) -> Path:
    path = tmp_path / "values.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "external-api-providers": [
                    {
                        "id": "global-relay",
                        "providerId": "openrouter",
                        "enabled": True,
                        "upstreamURL": "https://openrouter.ai/api/v1",
                        "credentialSources": ["global"],
                        "profiles": ["openai-chat-completions-v1"],
                        "catalogModels": ["remote/relay/chat"],
                    },
                    {
                        "id": "override-relay",
                        "providerId": "openrouter",
                        "enabled": True,
                        "upstreamURL": "https://openrouter.ai/api/v1",
                        "credentialSources": ["override"],
                        "profiles": ["openai-chat-completions-v1"],
                        "catalogModels": ["remote/relay/chat"],
                    },
                ]
            }
        )
    )
    return path


def inventory() -> list[object]:
    return [
        {
            "id": "publisher/chat",
            "context_length": 20000,
            "top_provider": {"context_length": 16000, "max_completion_tokens": 4000},
            "supported_parameters": ["tools", "structured_outputs"],
            "architecture": {"input_modalities": ["text", "image"]},
        }
    ]


def test_preserves_fetched_metadata_binding_identity_and_pricing(tmp_path: Path) -> None:
    result = with_metadata(generated(), inventory(), [values(tmp_path)], *snapshot())
    rows = yaml.safe_load(result.catalog)["gatewayModelMetadata"]["models"]
    assert len(rows) == 1
    assert rows[0]["binding_id"] == "global-relay"
    assert rows[0]["limits"] == {
        "context_tokens": 16000,
        "max_input_tokens": None,
        "max_output_tokens": 4000,
    }
    assert rows[0]["capabilities"]["image_input"] == "supported"
    assert rows[0]["capabilities"]["streaming_tool_calling"] == "unknown"
    assert result.pricing == b"pricing"
    assert result.policy == b"policy"
    output = tmp_path / "catalog.yaml"
    output.write_bytes(result.catalog)
    retained = retain_metadata(generated(), output)
    assert yaml.safe_load(retained.catalog)["gatewayModelMetadata"]["models"] == rows


def test_missing_limits_and_expired_source_do_not_guess(tmp_path: Path) -> None:
    path = values(tmp_path)
    with pytest.raises(CatalogSyncError, match="timestamps"):
        with_metadata(generated(), inventory(), [path], None, None)
    with pytest.raises(MetadataError):
        with_metadata(
            generated(), inventory(), [path], "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"
        )
    data = inventory()
    assert isinstance(data[0], dict)
    data[0]["top_provider"] = {}
    with pytest.raises(CatalogSyncError, match="limits"):
        with_metadata(generated(), data, [path], *snapshot())


@pytest.mark.parametrize("enabled", [True, False, None])
def test_metadata_refresh_and_retention_preserve_explicit_enablement(
    tmp_path: Path, enabled: bool | None
) -> None:
    path = values(tmp_path)
    config = yaml.safe_load(path.read_text())
    config["gatewayModelMetadata"] = {"models": []}
    if enabled is not None:
        config["gatewayModelMetadata"]["enabled"] = enabled
    path.write_text(yaml.safe_dump(config))
    refreshed = with_metadata(generated(), inventory(), [path], *snapshot())
    output = tmp_path / "catalog.yaml"
    output.write_bytes(refreshed.catalog)
    retained = retain_metadata(generated(), output)
    refreshed_again = with_metadata(retained, inventory(), [path], *snapshot())
    for result in (refreshed, retained, refreshed_again):
        metadata = yaml.safe_load(result.catalog)["gatewayModelMetadata"]
        assert len(metadata["models"]) == 1
        if enabled is None:
            assert "enabled" not in metadata
        else:
            assert metadata["enabled"] is enabled
