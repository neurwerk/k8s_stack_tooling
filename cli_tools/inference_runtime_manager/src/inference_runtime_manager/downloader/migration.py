"""Move verified bundles without changing their identities or model bytes."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from inference_runtime_manager.downloader.catalog import (
    load_catalog,
    load_installed,
    write_installed,
)
from inference_runtime_manager.downloader.hierarchy import artifact_path
from inference_runtime_manager.downloader.integrity import verify_checksums
from inference_runtime_manager.downloader.models import InstalledModel
from inference_runtime_manager.downloader.store import _load_metadata


@contextmanager
def library_lock(root: Path) -> Iterator[None]:
    """Exclude simultaneous downloads and layout migrations on this mounted library."""
    with (root / ".model-library.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another model download or migration is using this library") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _safe(root: Path, relative: Path) -> Path:
    path = root / relative
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not path.resolve().is_relative_to(root.resolve())
    ):
        raise ValueError("Artifact path escapes storage")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Artifact paths must not contain symlinks")
    return path


def _verify(path: Path, entry: InstalledModel) -> None:
    artifact = _load_metadata(path)
    if artifact.schema_version != 1 or (
        artifact.model_id,
        artifact.variant_id,
        artifact.source,
        artifact.revision,
    ) != (entry.model_id, entry.variant_id, entry.source, entry.revision):
        raise ValueError("Inventory and artifact identity disagree")
    if (
        entry.verification != "sha256"
        or entry.file_count != len(artifact.files)
        or entry.total_bytes != sum(f.size for f in artifact.files)
    ):
        raise ValueError("Inventory totals or verification disagree")
    expected = "".join(f"{f.sha256}  {f.path}\n" for f in artifact.files)
    if (path / "checksums.sha256").read_text() != expected:
        raise ValueError("Checksum index disagrees with artifact metadata")
    for record in artifact.files:
        _safe(path, Path(record.path))
    verify_checksums(path, artifact.files)


def migrate_library(root: Path, *, dry_run: bool = True) -> list[tuple[str, str]]:
    """Move indexed bundles; recover a move interrupted before its inventory update.

    Both copies existing is a conflict, never grounds to overwrite or delete either.
    Partial download directories are not moved or removed.
    """
    with library_lock(root):
        catalog = load_catalog()
        inventory_path = root / "installed.yaml"
        inventory = load_installed(inventory_path)
        plan = []
        for entry in inventory.installed:
            model = catalog.model(entry.model_id)
            variant = model.variant(entry.variant_id)
            destination = artifact_path(model, variant, entry.revision)
            if entry.source != (variant.source or model.source):
                raise ValueError(f"Unexpected artifact source: {entry.model_id}")
            if entry.path == destination.as_posix():
                continue
            legacy = Path("models", model.category, entry.source, entry.revision, variant.id)
            categoryless = Path(destination.parts[0], *destination.parts[2:])
            if entry.path not in (legacy.as_posix(), categoryless.as_posix()):
                raise ValueError(f"Unrecognized existing path: {entry.path}")
            old, new = _safe(root, Path(entry.path)), _safe(root, destination)
            if old.exists() and new.exists():
                raise ValueError(f"Migration destination already exists: {destination}")
            source = old if old.exists() else new
            if not source.is_dir():
                raise ValueError(f"Missing artifact: {entry.path}")
            plan.append((entry, old, new))
        if dry_run:
            return [(entry.path, new.relative_to(root).as_posix()) for entry, _, new in plan]
        # Check all bundles before moving the first one.
        for entry, old, new in plan:
            _verify(old if old.exists() else new, entry)
        result = []
        for entry, old, new in plan:
            before = inventory_path.read_bytes()
            current = load_installed(inventory_path)
            matching = [
                item
                for item in current.installed
                if (item.model_id, item.variant_id) == (entry.model_id, entry.variant_id)
            ]
            if matching != [entry]:
                raise ValueError("Inventory changed during migration; retry after downloads finish")
            new.parent.mkdir(parents=True, exist_ok=True)
            if old.exists():
                old.rename(new)
            updated_path = new.relative_to(root).as_posix()
            current.installed = [
                item.model_copy(update={"path": updated_path}) if item == entry else item
                for item in current.installed
            ]
            if inventory_path.read_bytes() != before:
                raise ValueError(
                    "Inventory changed during migration; moved bundle is recoverable on retry"
                )
            write_installed(inventory_path, current)
            result.append((entry.path, updated_path))
            parent = old.parent
            while parent != root / "models":
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return result
