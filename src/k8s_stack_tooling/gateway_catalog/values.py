"""Read and explicitly write local gateway values without application-specific policy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from k8s_stack_tooling.gateway_catalog.metadata import MetadataError


def merge_values(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_values(target[key], value)
        else:
            target[key] = value


def read_values(paths: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in paths:
        value = yaml.safe_load(path.read_text())
        if not isinstance(value, dict):
            raise MetadataError("values must be YAML objects")
        merge_values(result, value)
    return result


def write_values(path: Path, additions: dict[str, Any]) -> None:
    """Replace one reviewed values file atomically, preserving unrelated settings."""
    if path.is_symlink():
        raise MetadataError("output must not be a symlink")
    existing = read_values([path]) if path.exists() else {}
    merge_values(existing, additions)
    content = yaml.safe_dump(existing, allow_unicode=True, sort_keys=False)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=".model-metadata-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
