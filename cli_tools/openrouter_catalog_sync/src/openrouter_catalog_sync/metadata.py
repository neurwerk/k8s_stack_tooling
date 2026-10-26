"""Retain metadata from the already fetched inventory using actual binding selections."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml
from k8s_stack_gateway_catalog.values import read_values
from k8s_stack_tooling.gateway_model_metadata import SourceModel
from k8s_stack_tooling.model_metadata_refresh import openrouter_metadata

from openrouter_catalog_sync.sync import CatalogSyncError, GeneratedFiles


def retain_metadata(generated: GeneratedFiles, path: Path) -> GeneratedFiles:
    """Legacy pricing/name refreshes must not erase the independent metadata source."""
    if not path.exists():
        return generated
    existing = read_values([path])
    if "gatewayModelMetadata" not in existing:
        return generated
    models = existing["gatewayModelMetadata"]["models"]
    validated = [SourceModel.model_validate(row).model_dump() for row in models]
    catalog = yaml.safe_load(generated.catalog)
    catalog["gatewayModelMetadata"] = {**existing["gatewayModelMetadata"], "models": validated}
    return replace(
        generated, catalog=yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False).encode()
    )


def with_metadata(
    generated: GeneratedFiles,
    models: list[object],
    values: list[Path],
    observed_at: str | None,
    valid_until: str | None,
) -> GeneratedFiles:
    """Use exact selected binding IDs without inferred provider aliases or guessed limits."""
    if not observed_at or not valid_until:
        raise CatalogSyncError("metadata requires explicit snapshot and expiry timestamps")
    config = read_values(values)
    catalog = yaml.safe_load(generated.catalog)
    selected = {row["name"]: row for row in catalog["openrouterCatalog"]["models"]}
    inventory = {row["id"]: row for row in models if isinstance(row, dict)}
    metadata = []
    for binding in config.get("external-api-providers", []):
        if (
            not binding.get("enabled", False)
            or binding.get("providerId") != "openrouter"
            or binding.get("upstreamURL", "").rstrip("/") != "https://openrouter.ai/api/v1"
            or not set(binding.get("credentialSources", [])) & {"global", "company"}
            or "openai-chat-completions-v1" not in binding.get("profiles", [])
        ):
            continue
        for public_id in binding.get("catalogModels", []):
            if public_id not in selected:
                continue
            row = inventory[selected[public_id]["upstreamModel"]]
            limits, caps = openrouter_metadata(row)
            if limits["context_tokens"] is None or limits["max_output_tokens"] is None:
                raise CatalogSyncError("selected OpenRouter metadata is missing required limits")
            model = SourceModel.model_validate(
                {
                    "binding_id": binding["id"],
                    "public_id": public_id,
                    "limits": limits,
                    "capabilities": caps,
                    "provenance": {
                        "kind": "provider_api",
                        "source": "https://openrouter.ai/api/v1/models",
                        "observed_at": observed_at,
                        "valid_until": valid_until,
                    },
                }
            )
            model.provenance.require_fresh(datetime.now(UTC))
            metadata.append(model.model_dump())
    # Preserve independently refreshed direct/local records in this shared central output.
    previous_metadata = {
        **config.get("gatewayModelMetadata", {}),
        **catalog.get("gatewayModelMetadata", {}),
    }
    previous = previous_metadata.get("models", [])
    replaced_bindings = {
        binding["id"]
        for binding in config.get("external-api-providers", [])
        if binding.get("providerId") == "openrouter"
    }
    retained = [
        SourceModel.model_validate(row).model_dump()
        for row in previous
        if row.get("binding_id") not in replaced_bindings
    ]
    catalog["gatewayModelMetadata"] = {
        **previous_metadata,
        "models": sorted(
            retained + metadata, key=lambda row: (row["binding_id"], row["public_id"])
        ),
    }
    content = yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False).encode()
    if len(content) > 900_000:
        raise CatalogSyncError("generated metadata exceeds output size limit")
    return replace(generated, catalog=content)
