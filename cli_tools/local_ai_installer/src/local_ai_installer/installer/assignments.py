"""Local alias choices; bundled presets remain immutable runtime recipes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from local_ai_installer.downloader.catalog import load_catalog
from local_ai_installer.installer.docker import slots

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


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str
    assignments: dict[str, Assignment] = Field(default_factory=dict)


def recipe(model_id: str, variant_id: str) -> dict[str, Any]:
    """Only explicitly reviewed recipes can become active LocalAI definitions."""
    model = load_catalog().model(model_id)
    variant = model.variant(variant_id)
    if variant.localai != "supported":
        raise ValueError(f"{model_id}/{variant_id}: LocalAI compatibility is {variant.localai}")
    for preset in slots().values():
        if (preset["model_id"], preset["variant_id"]) == (model_id, variant_id):
            return copy.deepcopy(preset)
    raise ValueError(f"No reviewed deployment recipe for {model_id}/{variant_id}")


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
        if load_catalog().model(choice.model_id).category != ALIAS_CATEGORIES[alias]:
            raise ValueError(f"Model category does not match {alias}")
        recipe(choice.model_id, choice.variant_id)
    return deployment


def save_deployment(root: Path, deployment: Deployment) -> None:
    path = root / "deployment.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(deployment.model_dump(), indent=2) + "\n")
    temporary.replace(path)


def deployment_slots(root: Path) -> dict[str, Any]:
    result = slots()
    for alias, choice in load_deployment(root).assignments.items():
        preset = recipe(choice.model_id, choice.variant_id)
        preset["enabled"] = choice.enabled
        result[alias] = preset
    return result
