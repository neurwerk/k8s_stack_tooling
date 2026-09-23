"""Load local storage configuration and validate external-volume availability."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from inference_runtime_manager.downloader.errors import StorageUnavailableError


class Settings(BaseSettings):
    """Load external storage and Hugging Face cache paths from `.env`.

    Args:
        storage_root: Existing mount or directory on the external storage volume.
        hf_home: Hugging Face cache and authenticated-user configuration directory.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    storage_root: Path = Field(
        validation_alias=AliasChoices(
            "INFERENCE_RUNTIME_MANAGER_STORAGE_ROOT",
            "LOCAL_AI_INSTALLER_STORAGE_ROOT",
            "MEDIA_DOWNLOADER_UPLOADER_STORAGE_ROOT",
        ),
    )
    hf_home: Path = Field(
        validation_alias=AliasChoices("HF_HOME", "MEDIA_DOWNLOADER_UPLOADER_HF_HOME"),
    )

    @field_validator("storage_root", "hf_home")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        """Expand user home notation in configured paths.

        Args:
            value: Configured filesystem path.

        Returns:
            Expanded path.
        """
        return value.expanduser()


def validate_storage(settings: Settings) -> None:
    """Require an existing writable non-root mounted volume for all media data.

    Args:
        settings: Local application configuration.

    Raises:
        StorageUnavailableError: If the configured storage is unavailable or unsafe.
    """
    root = settings.storage_root.resolve()
    if not root.is_dir():
        raise StorageUnavailableError(
            f"Configured storage root is not an available directory: {root}"
        )
    mount = _mount_root(root)
    if mount == Path("/"):
        raise StorageUnavailableError(
            f"Configured storage root must be on a non-root mounted volume: {root}"
        )
    _require_writable(root)
    _require_within_mount(settings.hf_home.resolve(), mount, "HF_HOME")
    settings.hf_home.mkdir(parents=True, exist_ok=True)
    _require_writable(settings.hf_home)


def huggingface_environment(settings: Settings) -> dict[str, str]:
    """Build the Hugging Face CLI environment with its data on external storage.

    Args:
        settings: Validated local application configuration.

    Returns:
        Environment for Hugging Face CLI subprocesses.
    """
    environment = os.environ.copy()
    environment["HF_HOME"] = str(settings.hf_home)
    return environment


def _mount_root(path: Path) -> Path:
    """Return the mounted filesystem containing a path.

    Args:
        path: Existing directory to inspect.

    Returns:
        Nearest mount-point ancestor.
    """
    for candidate in (path, *path.parents):
        if candidate.is_mount():
            return candidate
    return Path("/")


def _require_writable(path: Path) -> None:
    """Require a directory to permit a small write probe.

    Args:
        path: Existing directory to test.

    Raises:
        StorageUnavailableError: If a temporary probe cannot be written and removed.
    """
    probe = path / ".media-downloader-uploader-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        raise StorageUnavailableError(f"Configured directory is not writable: {path}") from error


def _require_within_mount(path: Path, mount: Path, setting_name: str) -> None:
    """Require an auxiliary path to remain on the verified external volume.

    Args:
        path: Auxiliary configured path.
        mount: Validated external-volume mount root.
        setting_name: Configuration key for error reporting.

    Raises:
        StorageUnavailableError: If the path is outside the external volume.
    """
    try:
        path.relative_to(mount)
    except ValueError as error:
        raise StorageUnavailableError(
            f"{setting_name} must be inside external storage mount {mount}: {path}"
        ) from error
