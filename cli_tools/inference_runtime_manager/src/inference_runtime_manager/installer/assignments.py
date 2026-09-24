"""Persist model choices while runtime recipes remain package-owned."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from inference_runtime_manager.downloader.catalog import load_catalog
from inference_runtime_manager.installer.docker import service_recipes, services

ALIAS_CATEGORIES = {
    "llm-general": "llm",
    "vlm-general": "vlm",
    "vlm-documents": "ocr",
    "stt-general": "asr",
    "tts-german": "tts",
    "ner-german": "ner",
    "image-generation-general": "image-generation",
    "vad-general": "vad",
}


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str
    variant_id: str
    enabled: bool = True
    # Retained in state to make plans readable and to accept pre-migration files.
    runtime: str | None = None


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    assignments: dict[str, Assignment] = Field(default_factory=dict)


def recipe(alias: str, model_id: str, variant_id: str) -> dict[str, Any]:
    """Return the reviewed service recipe for one alias/model combination."""
    matches = [
        preset
        for preset in service_recipes().get(alias, [])
        if (preset["model_id"], preset["variant_id"]) == (model_id, variant_id)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"No reviewed standalone runtime recipe for {alias}: {model_id}/{variant_id}"
        )
    preset = matches[0]
    model = load_catalog().model(model_id)
    if model.category != ALIAS_CATEGORIES[alias]:
        raise ValueError(f"Model category does not match {alias}")
    return copy.deepcopy(preset)


def compose_services(alias: str) -> tuple[str, ...]:
    """Return all mutually exclusive Compose services for one stable alias."""
    recipes = service_recipes().get(alias)
    if recipes is None:
        raise ValueError(f"Unknown deployment alias: {alias}")
    result = []
    for preset in recipes:
        service = preset["service"]
        if not isinstance(service, str):
            raise ValueError(f"Service {alias} has an invalid Compose service name")
        result.append(service)
    return tuple(result)


def assigned_recipe(root: Path, target: str, alias: str) -> dict[str, Any] | None:
    """Return the explicitly assigned recipe, including its desired enabled state."""
    choice = load_deployment(root, target).assignments.get(alias)
    if choice is None:
        return None
    preset = recipe(alias, choice.model_id, choice.variant_id)
    preset["enabled"] = choice.enabled
    return preset


def load_deployment(root: Path, target: str | None = None) -> Deployment:
    path = root / "deployment.json"
    deployment = (
        Deployment.model_validate_json(path.read_text())
        if path.exists()
        else Deployment(target=target or "")
    )
    if target is not None and deployment.target != target:
        raise ValueError("Saved assignments belong to a different Docker context")
    for alias, choice in deployment.assignments.items():
        if alias not in ALIAS_CATEGORIES:
            raise ValueError(f"Unknown deployment alias: {alias}")
        try:
            preset = recipe(alias, choice.model_id, choice.variant_id)
        except ValueError:
            replacement = services().get(alias)
            if (
                alias != "vlm-documents"
                or choice.runtime not in {None, "localai"}
                or replacement is None
                or replacement["model_id"] != choice.model_id
            ):
                raise
            choice.variant_id = replacement["variant_id"]
            preset = recipe(alias, choice.model_id, choice.variant_id)
        # Old files omitted runtime or recorded LocalAI. The reviewed alias recipe
        # is now authoritative, so migration does not preserve a removed backend.
        choice.runtime = preset["runtime"]
    return deployment


def save_deployment(root: Path, deployment: Deployment) -> None:
    path = root / "deployment.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(deployment.model_dump(), indent=2) + "\n")
    temporary.replace(path)


def deployment_services(root: Path) -> dict[str, Any]:
    result = services()
    for alias, choice in load_deployment(root).assignments.items():
        preset = recipe(alias, choice.model_id, choice.variant_id)
        preset["enabled"] = choice.enabled
        result[alias] = preset
    return result
