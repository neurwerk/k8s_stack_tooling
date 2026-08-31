from pathlib import Path

import pytest

from media_downloader_uploader.config import Settings, validate_storage
from media_downloader_uploader.errors import StorageUnavailableError


def test_validate_storage_accepts_writable_non_root_mount(monkeypatch, tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount)
    settings = Settings(storage_root=mount, hf_home=mount / "huggingface")

    validate_storage(settings)

    assert settings.hf_home.is_dir()


def test_validate_storage_rejects_root_filesystem(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "is_mount", lambda path: path == Path("/"))
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    with pytest.raises(StorageUnavailableError, match="non-root"):
        validate_storage(settings)


def test_validate_storage_rejects_huggingface_cache_outside_volume(
    monkeypatch, tmp_path: Path
) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(Path, "is_mount", lambda path: path == mount)
    settings = Settings(storage_root=mount, hf_home=tmp_path / "outside")

    with pytest.raises(StorageUnavailableError, match="HF_HOME"):
        validate_storage(settings)


def test_validate_storage_rejects_missing_root(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path / "missing", hf_home=tmp_path / "huggingface")

    with pytest.raises(StorageUnavailableError, match="available directory"):
        validate_storage(settings)


def test_settings_reuse_explicit_rollout_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_DOWNLOADER_UPLOADER_STORAGE_ROOT", "/external-media")
    monkeypatch.setenv("HF_HOME", "/external-media/huggingface")
    monkeypatch.setenv("KUBE_CONTEXT", "example-cluster")
    assert Settings().kube_context == "example-cluster"
