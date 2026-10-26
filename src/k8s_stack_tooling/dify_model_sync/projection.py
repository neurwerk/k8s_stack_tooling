"""Select approved company/local chat destinations without duplicating model lists."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from k8s_stack_tooling.gateway_catalog import GatewayCatalog
from k8s_stack_tooling.gateway_model_metadata import (
    MetadataError,
    canonical_json,
    revision,
    validate_companion,
)


def select_models(catalog: GatewayCatalog) -> dict[str, Any]:
    """Use one eligibility/grant/route decision for refresh and installation."""
    models: list[dict[str, Any]] = []
    bindings: list[dict[str, str]] = []
    roles = {"llm:invoke"}
    for group, local in (
        (catalog.self_hosted_api_providers, True),
        (catalog.external_api_providers, False),
    ):
        for entry in group:
            paths: set[str] = set()
            selected = False
            for dest in entry.destinations:
                if dest.kind != "model" or "openai-chat-completions-v1" not in dest.profile_ids:
                    continue
                mode = next(
                    (
                        m
                        for m in dest.credential_modes
                        if m.source == ("none" if local else "company")
                    ),
                    None,
                )
                if mode is None:
                    continue
                routes = [
                    r
                    for r in entry.routes
                    if r.id in dest.endpoint_ids
                    and r.entry_mode == "api"
                    and set(r.caller_auth_modes) & {"neurwerk_bearer", "oidc_bearer"}
                ]
                if not routes:
                    continue
                required = {"llm:invoke", f"model:{dest.id}:invoke"}
                if not required <= set(dest.required_permissions):
                    raise MetadataError("destination lacks invocation grants")
                if not local and (
                    not entry.provider_id
                    or f"provider:{entry.provider_id}:company-use" not in mode.required_permissions
                ):
                    raise MetadataError("company mode lacks billing grant")
                for route in routes:
                    path = urlsplit(route.url).path
                    if not re.fullmatch(r"(?:/[a-z0-9][a-z0-9-]*)+/v1", path):
                        raise MetadataError("chat route is not an admitted API base path")
                    paths.add(path[:-3])
                selected = True
                roles.update(dest.required_permissions)
                roles.update(mode.required_permissions)
                models.append(
                    {
                        "binding_id": entry.id,
                        "public_id": dest.id,
                        "credential_mode": "local" if local else "company",
                    }
                )
            if selected:
                if len(paths) != 1:
                    raise MetadataError("binding has ambiguous API paths")
                bindings.append({"id": entry.id, "api_path": paths.pop()})
    if len({b["api_path"] for b in bindings}) != len(bindings):
        raise MetadataError("binding API paths collide")
    return {
        "roles": sorted(roles),
        "bindings": sorted(bindings, key=lambda b: b["id"]),
        "models": sorted(models, key=lambda m: (m["binding_id"], m["public_id"])),
    }


def build_projection(catalog: GatewayCatalog, metadata: dict[str, Any]) -> dict[str, Any]:
    companion = validate_companion(metadata, catalog)
    lookup = {(m.binding_id, m.public_id): m.model_dump() for m in companion.models}
    selection = select_models(catalog)
    for model in selection["models"]:
        model["metadata"] = lookup.get((model["binding_id"], model["public_id"]))
    result: dict[str, Any] = {
        "schema_version": 1,
        "catalog_revision": catalog.catalog_revision,
        "metadata_revision": companion.metadata_revision,
        **selection,
    }
    result["projection_revision"] = revision(result, "projection_revision")
    return result


def require_installed_projection(
    installed: dict[str, Any], proposed: dict[str, Any], expected: str
) -> None:
    if proposed["projection_revision"] != expected or canonical_json(installed) != canonical_json(
        proposed
    ):
        raise MetadataError("installed projection differs from current reviewed projection")
