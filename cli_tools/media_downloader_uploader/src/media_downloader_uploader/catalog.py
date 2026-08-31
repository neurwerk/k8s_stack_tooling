"""Load the curated catalog and maintain external-drive queue and installed state."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

from media_downloader_uploader.models import (
    AvailableCatalog,
    DownloadQueue,
    InstalledModel,
    InstalledState,
)


def load_catalog(path: Path | None = None) -> AvailableCatalog:
    """Load the committed manually curated available-model catalog.

    Args:
        path: Optional catalog YAML path. The packaged catalog is used by default.

    Returns:
        Validated available catalog.
    """
    if path is None:
        resource = files("media_downloader_uploader").joinpath("available.yaml")
        mapping = _parse_mapping(
            resource.read_text(encoding="utf-8"), "Available catalog", str(resource)
        )
    else:
        mapping = _load_mapping(path, "Available catalog")
    return AvailableCatalog.model_validate(mapping)


def load_queue(path: Path) -> DownloadQueue:
    """Load the external-drive download queue, using an empty state when absent.

    Args:
        path: Download queue YAML path.

    Returns:
        Existing or empty download queue.
    """
    if not path.exists():
        return DownloadQueue(schemaVersion=1, selected=[])
    return DownloadQueue.model_validate(_load_mapping(path, "Download queue"))


def write_queue(path: Path, queue: DownloadQueue) -> None:
    """Write the complete external-drive download queue atomically.

    Args:
        path: Download queue YAML path.
        queue: Complete queue state.
    """
    _write_yaml(path, queue)


def load_installed(path: Path) -> InstalledState:
    """Load external-drive installed state, using an empty state when absent.

    Args:
        path: Installed-state YAML path.

    Returns:
        Existing or empty installed state.
    """
    if not path.exists():
        return InstalledState(schemaVersion=1, installed=[])
    return InstalledState.model_validate(_load_mapping(path, "Installed state"))


def write_installed(path: Path, state: InstalledState) -> None:
    """Write complete external-drive installed state atomically.

    Args:
        path: Installed-state YAML path.
        state: Complete installed state.
    """
    _write_yaml(path, state)


def upsert_installed(state: InstalledState, model: InstalledModel) -> InstalledState:
    """Replace or append one installed model variant without losing other entries.

    Args:
        state: Existing installed state.
        model: Newly verified installed model.

    Returns:
        Updated installed state.
    """
    installed = state.installed.copy()
    for index, existing in enumerate(installed):
        if (existing.model_id, existing.variant_id) == (model.model_id, model.variant_id):
            installed[index] = model
            return InstalledState(schemaVersion=state.schema_version, installed=installed)
    installed.append(model)
    return InstalledState(schemaVersion=state.schema_version, installed=installed)


def _load_mapping(path: Path, name: str) -> dict[str, object]:
    """Load a YAML mapping.

    Args:
        path: YAML input path.
        name: User-facing state name for validation errors.

    Returns:
        YAML root mapping.

    Raises:
        TypeError: If the YAML root is not a mapping.
    """
    return _parse_mapping(path.read_text(encoding="utf-8"), name, str(path))


def _parse_mapping(content: str, name: str, location: str) -> dict[str, object]:
    """Load a YAML mapping from text and identify its source in validation errors."""
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise TypeError(f"{name} root must be a YAML mapping: {location}")
    return dict(raw)


def _write_yaml(path: Path, model: DownloadQueue | InstalledState) -> None:
    """Write one external-drive state model atomically.

    Args:
        path: YAML output path.
        model: State model to serialize.
    """
    content = yaml.safe_dump(
        model.model_dump(by_alias=True, mode="json"), default_flow_style=False, sort_keys=False
    )
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
