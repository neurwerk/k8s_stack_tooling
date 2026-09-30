"""Regression checks for local storage and unavailable staging volumes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.downloader.config import Settings, validate_storage
from inference_runtime_manager.downloader.errors import StorageUnavailableError
from inference_runtime_manager.workflow import configured_storage


class StorageValidationTests(TestCase):
    def test_home_storage_creates_huggingface_home(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = home / ".storage" / "inference-runtime-manager"
            root.mkdir(parents=True)
            settings = Settings.model_construct(storage_root=root, hf_home=root / "huggingface")

            with (
                patch.object(Path, "home", return_value=home),
                patch(
                    "inference_runtime_manager.downloader.config._mount_root",
                    return_value=Path("/"),
                ),
            ):
                validate_storage(settings)
                self.assertTrue(configured_storage(settings))

            self.assertTrue(settings.hf_home.is_dir())

    def test_home_storage_rejects_huggingface_home_outside_root(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = home / ".storage" / "inference-runtime-manager"
            root.mkdir(parents=True)
            settings = Settings.model_construct(storage_root=root, hf_home=home / "huggingface")

            with (
                patch.object(Path, "home", return_value=home),
                patch(
                    "inference_runtime_manager.downloader.config._mount_root",
                    return_value=Path("/"),
                ),
            ):
                with self.assertRaisesRegex(StorageUnavailableError, "HF_HOME must be inside"):
                    validate_storage(settings)

            self.assertFalse(settings.hf_home.exists())

    def test_unmounted_external_storage_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = Path(directory) / "Volumes" / "ModelStorage"
            root.mkdir(parents=True)
            settings = Settings.model_construct(storage_root=root, hf_home=root / "huggingface")

            with (
                patch.object(Path, "home", return_value=home),
                patch(
                    "inference_runtime_manager.downloader.config._mount_root",
                    return_value=Path("/"),
                ),
            ):
                with self.assertRaisesRegex(StorageUnavailableError, "non-root mounted volume"):
                    validate_storage(settings)
                self.assertFalse(configured_storage(settings))

            self.assertFalse(settings.hf_home.exists())

    def test_mounted_storage_still_allows_cache_on_same_volume(self) -> None:
        with TemporaryDirectory() as directory:
            mount = Path(directory) / "ModelStorage"
            root = mount / "models"
            root.mkdir(parents=True)
            settings = Settings.model_construct(storage_root=root, hf_home=mount / "huggingface")

            with patch(
                "inference_runtime_manager.downloader.config._mount_root",
                return_value=mount.resolve(),
            ):
                validate_storage(settings)

            self.assertTrue(settings.hf_home.is_dir())
