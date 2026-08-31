"""Calculate and verify immutable local artifact checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path

from media_downloader_uploader.errors import IntegrityError
from media_downloader_uploader.models import FileChecksum

_METADATA_FILES = frozenset({"artifact.yaml", "checksums.sha256", "manifest.yaml"})


def calculate_checksums(root: Path) -> list[FileChecksum]:
    """Calculate SHA-256 checksums for downloaded artifact files.

    Args:
        root: Artifact directory containing downloaded model files.

    Returns:
        Checksums sorted by their relative POSIX path.
    """
    files = [path for path in root.rglob("*") if path.is_file() and _include(path, root)]
    return [_checksum(path, root) for path in sorted(files)]


def write_checksums(path: Path, checksums: list[FileChecksum]) -> None:
    """Write portable SHA-256 checksum records.

    Args:
        path: Destination checksum file.
        checksums: File checksums to write.
    """
    content = "".join(f"{item.sha256}  {item.path}\n" for item in checksums)
    path.write_text(content, encoding="utf-8")


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of exact file bytes without loading the whole file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_checksums(root: Path, expected: list[FileChecksum]) -> None:
    """Verify that the artifact content exactly matches its recorded checksums.

    Args:
        root: Artifact directory to verify.
        expected: Recorded checksums.

    Raises:
        IntegrityError: If files are missing, added, resized, or hash-mismatched.
    """
    actual = calculate_checksums(root)
    if actual != expected:
        raise IntegrityError(f"Artifact checksums do not match: {root}")


def _include(path: Path, root: Path) -> bool:
    """Exclude local Hugging Face metadata and application metadata from checksums.

    Args:
        path: Candidate file.
        root: Artifact root directory.

    Returns:
        Whether the file is downloaded artifact content.
    """
    relative = path.relative_to(root)
    return ".cache" not in relative.parts and relative.name not in _METADATA_FILES


def _checksum(path: Path, root: Path) -> FileChecksum:
    """Calculate one SHA-256 checksum without loading the complete file in memory.

    Args:
        path: Downloaded artifact file.
        root: Artifact root directory.

    Returns:
        File checksum and byte size.
    """
    return FileChecksum(
        path=path.relative_to(root).as_posix(), sha256=sha256_file(path), size=path.stat().st_size
    )
