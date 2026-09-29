"""Persist non-secret workstation configuration and small management state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict


class WorkstationConfig(BaseModel):
    """Non-secret choices managed by the interactive configuration menu."""

    model_config = ConfigDict(extra="forbid")
    storage_root: Path | None = None
    hf_home: Path | None = None
    docker_context: str | None = None
    build_docker_context: str | None = None


def config_root() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "inference-runtime-manager"


def config_path() -> Path:
    return config_root() / "config.json"


def state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "inference-runtime-manager"


def load_workstation_config() -> WorkstationConfig:
    path = config_path()
    if not path.exists():
        return WorkstationConfig()
    return WorkstationConfig.model_validate_json(path.read_text(encoding="utf-8"))


def save_workstation_config(config: WorkstationConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def management_state(storage_root: Path | None = None) -> Path:
    """Return local state, importing legacy small files without moving large data."""
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    if storage_root is not None and storage_root.is_dir():
        resolved = storage_root.resolve()
        legacy_files = [resolved / name for name in ("deployment.json", "download.yaml")]
        if not any(path.is_file() for path in legacy_files):
            return root
        identity = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
        marker = root / f".migrated-{identity}.json"
        if marker.exists():
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            if recorded != {"storage_root": str(resolved)}:
                raise ValueError("Invalid management-state migration marker")
            return root
        _merge_download_queue(resolved / "download.yaml", root / "download.yaml")
        _merge_deployment(resolved / "deployment.json", root / "deployment.json")
        _atomic_text(marker, json.dumps({"storage_root": str(resolved)}) + "\n")
    return root


def _merge_download_queue(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    legacy = _read_download_queue(source)
    if not destination.exists():
        _atomic_text(
            destination,
            yaml.safe_dump(legacy, default_flow_style=False, sort_keys=False),
        )
        return
    local = _read_download_queue(destination)
    merged = []
    seen = set()
    for item in [*local.get("selected", []), *legacy.get("selected", [])]:
        if not isinstance(item, dict):
            raise ValueError("Cannot merge invalid legacy download selection")
        identity = (item.get("modelId"), item.get("variantId"))
        if identity not in seen:
            seen.add(identity)
            merged.append(item)
    _atomic_text(
        destination,
        yaml.safe_dump(
            {
                "schemaVersion": max(local.get("schemaVersion", 1), legacy.get("schemaVersion", 1)),
                "selected": merged,
            },
            default_flow_style=False,
            sort_keys=False,
        ),
    )


def _merge_deployment(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    legacy = _read_deployment(source)
    if not destination.exists():
        _atomic_text(destination, json.dumps(legacy, indent=2) + "\n")
        return
    local = _read_deployment(destination)
    local_target = local.get("target")
    legacy_target = legacy.get("target")
    if local_target and legacy_target and local_target != legacy_target:
        raise ValueError("Legacy and local deployment state use different Docker contexts")
    assignments = {**legacy.get("assignments", {}), **local.get("assignments", {})}
    _atomic_text(
        destination,
        json.dumps(
            {"target": local_target or legacy_target or "", "assignments": assignments},
            indent=2,
        )
        + "\n",
    )


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _read_download_queue(path: Path) -> dict[str, Any]:
    from inference_runtime_manager.downloader.models import DownloadQueue

    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid download queue: {path}")
    validated = DownloadQueue.model_validate(value)
    return cast(dict[str, Any], validated.model_dump(by_alias=True, mode="json"))


def _read_deployment(path: Path) -> dict[str, Any]:
    from inference_runtime_manager.installer.assignments import ALIAS_CATEGORIES, Deployment

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid deployment state: {path}")
    validated = Deployment.model_validate(value)
    if any(alias not in ALIAS_CATEGORIES for alias in validated.assignments):
        raise ValueError(f"Invalid deployment state: {path}")
    return cast(dict[str, Any], validated.model_dump(mode="json"))
