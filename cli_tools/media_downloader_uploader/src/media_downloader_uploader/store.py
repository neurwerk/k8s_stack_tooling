"""Synchronize immutable artifact directories on the configured external volume."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import yaml

from media_downloader_uploader.errors import HuggingFaceCommandError, IntegrityError
from media_downloader_uploader.huggingface import HuggingFaceClient
from media_downloader_uploader.integrity import (
    calculate_checksums,
    verify_checksums,
    write_checksums,
)
from media_downloader_uploader.models import ArtifactRequest, FileChecksum, StoredArtifact

_METADATA_NAME = "artifact.yaml"
_CHECKSUMS_NAME = "checksums.sha256"


class ArtifactStore:
    """Manage immutable artifact bundles beneath an external storage root.

    Args:
        root: Validated external storage root.
        client: Authenticated Hugging Face downloader.
    """

    def __init__(self, root: Path, client: HuggingFaceClient) -> None:
        """Initialize a store for one external volume.

        Args:
            root: Validated external storage root.
            client: Authenticated Hugging Face downloader.
        """
        self._root = root
        self._client = client

    def synchronize(self, request: ArtifactRequest) -> StoredArtifact:
        """Ensure the requested immutable artifact exists and has valid checksums.

        Args:
            request: Desired Hugging Face artifact.

        Returns:
            Validated stored-artifact metadata.
        """
        revision = self._client.resolve_revision(request)
        destination = self.destination(request, revision)
        existing = self._load_valid(destination, request, revision)
        if existing is not None:
            return existing
        return self._download(request, revision, destination)

    def verify(self, request: ArtifactRequest) -> StoredArtifact:
        """Verify an already-downloaded artifact for the requested revision.

        Args:
            request: Desired Hugging Face artifact.

        Returns:
            Verified stored-artifact metadata.

        Raises:
            IntegrityError: If the requested artifact is absent or invalid.
        """
        revision = self._client.resolve_revision(request)
        destination = self.destination(request, revision)
        artifact = self._load_valid(destination, request, revision)
        if artifact is None:
            raise IntegrityError(f"Artifact is missing or invalid: {destination}")
        return artifact

    def destination(self, request: ArtifactRequest, revision: str) -> Path:
        """Return the canonical immutable local destination for an artifact.

        Args:
            request: Desired Hugging Face artifact.
            revision: Resolved immutable Hugging Face commit SHA.

        Returns:
            Canonical artifact directory path.
        """
        return (
            self._root
            / "models"
            / request.category
            / request.owner
            / request.name
            / revision
            / request.variant_id
        )

    def _load_valid(
        self, destination: Path, request: ArtifactRequest, revision: str
    ) -> StoredArtifact | None:
        """Load a valid existing artifact, or remove invalid incomplete content.

        Args:
            destination: Expected artifact directory.
            request: Desired Hugging Face artifact.
            revision: Resolved immutable Hugging Face commit SHA.

        Returns:
            Valid stored metadata, or None when the artifact must be downloaded.
        """
        if not destination.exists():
            return None
        try:
            artifact = _load_metadata(destination)
            _validate_identity(artifact, request, revision)
            verify_checksums(destination, artifact.files)
        except (IntegrityError, OSError, ValueError, yaml.YAMLError):
            shutil.rmtree(destination)
            return None
        return artifact

    def _download(
        self, request: ArtifactRequest, revision: str, destination: Path
    ) -> StoredArtifact:
        """Download and atomically publish an immutable artifact version.

        Args:
            request: Desired Hugging Face artifact.
            revision: Resolved immutable Hugging Face commit SHA.
            destination: Final immutable artifact directory.

        Returns:
            Stored metadata written for the newly downloaded artifact.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{revision}.partial-{uuid4().hex}"
        try:
            temporary.mkdir()
            self._client.download(request, revision, temporary)
            self._client.verify_download(request, revision, temporary)
            checksums = calculate_checksums(temporary)
            _require_model_files(checksums, request.source)
            artifact = StoredArtifact.create(request, revision, checksums)
            write_checksums(temporary / _CHECKSUMS_NAME, checksums)
            _write_metadata(temporary / _METADATA_NAME, artifact)
            verify_checksums(temporary, checksums)
            temporary.replace(destination)
        except (HuggingFaceCommandError, IntegrityError, OSError, ValueError, yaml.YAMLError):
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return artifact


def _load_metadata(destination: Path) -> StoredArtifact:
    """Load artifact metadata from its completed local directory.

    Args:
        destination: Artifact directory containing `artifact.yaml`.

    Returns:
        Parsed stored-artifact metadata.

    Raises:
        IntegrityError: If metadata is absent or malformed.
    """
    metadata_path = destination / _METADATA_NAME
    if not metadata_path.is_file():
        raise IntegrityError(f"Artifact completion metadata is missing: {destination}")
    raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise IntegrityError(f"Artifact completion metadata is invalid: {metadata_path}")
    return StoredArtifact.model_validate(raw)


def _validate_identity(artifact: StoredArtifact, request: ArtifactRequest, revision: str) -> None:
    """Verify that stored metadata belongs to the desired immutable artifact.

    Args:
        artifact: Stored metadata to validate.
        request: Desired Hugging Face artifact.
        revision: Resolved immutable Hugging Face commit SHA.

    Raises:
        IntegrityError: If metadata does not match the requested artifact.
    """
    if (
        artifact.model_id,
        artifact.variant_id,
        artifact.category,
        artifact.source,
        artifact.revision,
    ) != (
        request.model_id,
        request.variant_id,
        request.category,
        request.source,
        revision,
    ):
        raise IntegrityError("Artifact metadata does not match requested source or revision")


def _require_model_files(checksums: list[FileChecksum], source: str) -> None:
    """Require a completed download to contain at least one artifact file.

    Args:
        checksums: Calculated downloaded-file checksums.
        source: Hugging Face model repository used for error reporting.

    Raises:
        IntegrityError: If the download did not create model content.
    """
    if not checksums:
        raise IntegrityError(f"Hugging Face download contained no model files: {source}")


def _write_metadata(path: Path, artifact: StoredArtifact) -> None:
    """Write completion metadata only after all artifact content is present.

    Args:
        path: Metadata output path.
        artifact: Complete artifact metadata.
    """
    content = yaml.safe_dump(
        artifact.model_dump(by_alias=True, mode="json"), default_flow_style=False, sort_keys=False
    )
    path.write_text(content, encoding="utf-8")
