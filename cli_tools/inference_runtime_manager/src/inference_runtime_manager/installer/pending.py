"""Track service-specific saved changes without contacting the Docker target."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from inference_runtime_manager.installer.assignments import load_deployment, recipe
from inference_runtime_manager.installer.docker import DeploymentSettings


class ChangeState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)


def snapshots(root: Path, settings: DeploymentSettings) -> dict[str, dict[str, Any]]:
    """Include only settings that affect each assigned, enabled service."""
    deployment = load_deployment(root, settings.docker_context)
    result = {}
    for alias, assignment in deployment.assignments.items():
        if not assignment.enabled:
            result[alias] = {"enabled": False}
            continue
        preset = recipe(alias, assignment.model_id, assignment.variant_id)
        fields = []
        if alias == "llm-general":
            fields += ["llm_context_tokens", "llm_kv_cache_type"]
        if alias.startswith("ner-"):
            fields.append(alias.replace("-", "_") + "_dtype")
        if preset["runtime"] == "gliner":
            fields += ["gliner_threshold", "gliner_labels"]
        if preset["service"] == "vlm-documents":
            fields += ["vllm_granite_gpu_memory_utilization"]
        if preset["service"] == "vlm-images-olmocr":
            fields += ["olmocr_gpu_layers"]
        result[alias] = {
            "enabled": True,
            "model": assignment.model_id,
            "variant": assignment.variant_id,
            "service": preset["service"],
            "runtime_options": {name: getattr(settings, name) for name in fields},
        }
    return result


def _load(root: Path) -> ChangeState:
    path = root / "deployment-changes.json"
    return ChangeState.model_validate_json(path.read_text()) if path.exists() else ChangeState()


def _save(root: Path, state: ChangeState) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "deployment-changes.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(state.model_dump_json(indent=2) + "\n")
    temporary.replace(path)


def remember_before_edit(
    root: Path, settings: DeploymentSettings, aliases: list[str] | None = None
) -> None:
    """Track explicit edits even for legacy state whose live status is unknown.

    An untracked assignment alone is not pending. Its saved value before the
    first edit is the comparison point until a successful apply confirms it.
    Existing comparison points must survive further edits and failed applies.
    """
    current = snapshots(root, settings)
    state = _load(root)
    baseline = state.targets.setdefault(settings.docker_context, {})
    for alias in aliases if aliases is not None else current:
        baseline.setdefault(alias, current.get(alias, {"enabled": False}))
    _save(root, state)


def pending_aliases(root: Path, settings: DeploymentSettings) -> list[str]:
    baseline = _load(root).targets.get(settings.docker_context, {})
    if not baseline:
        return []
    return [
        alias
        for alias, desired in snapshots(root, settings).items()
        if alias in baseline and desired != baseline[alias]
    ]


def record_applied(root: Path, target: str, applied: dict[str, dict[str, Any]]) -> None:
    """Record only service states confirmed by a successful operation."""
    state = _load(root)
    state.targets.setdefault(target, {}).update(applied)
    _save(root, state)


def mark_unconfirmed(root: Path, target: str, alias: str) -> None:
    """Keep an attempted rollback pending until its restart confirms the state."""
    state = _load(root)
    # Every real snapshot contains enabled; an empty snapshot is unconfirmed.
    state.targets.setdefault(target, {})[alias] = {}
    _save(root, state)
