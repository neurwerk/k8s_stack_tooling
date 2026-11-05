"""Regression checks for local storage and unavailable staging volumes."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.downloader.config import Settings, validate_storage
from inference_runtime_manager.downloader.errors import StorageUnavailableError
from inference_runtime_manager.workflow import configured_storage


class StorageValidationTests(TestCase):
    def test_downloads_subdirectory_keeps_cache_inside_selected_root(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = home / "Downloads" / "inference-runtime-manager"
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
                self.assertTrue(settings.hf_home.is_dir())
                settings.hf_home = home / "Downloads" / "other-cache"
                with self.assertRaisesRegex(StorageUnavailableError, "HF_HOME must be inside"):
                    validate_storage(settings)
                self.assertFalse(settings.hf_home.exists())

    def test_local_parent_itself_is_not_a_model_directory(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            for name in (".storage", "Downloads"):
                root = home / name
                root.mkdir(parents=True)
                settings = Settings.model_construct(storage_root=root, hf_home=root / "huggingface")
                with (
                    self.subTest(parent=name),
                    patch.object(Path, "home", return_value=home),
                    patch(
                        "inference_runtime_manager.downloader.config._mount_root",
                        return_value=Path("/"),
                    ),
                    self.assertRaises(StorageUnavailableError),
                ):
                    validate_storage(settings)
                self.assertFalse(settings.hf_home.exists())

    def test_downloads_storage_and_cache_symlinks_cannot_escape(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            root = home / "Downloads" / "models"
            root.mkdir(parents=True)
            outside = home / "other"
            outside.mkdir()
            linked_root = root.parent / "linked-models"
            linked_root.symlink_to(outside, target_is_directory=True)
            linked_cache = root / "linked-cache"
            linked_cache.symlink_to(outside, target_is_directory=True)
            for model_root, cache in ((linked_root, linked_root / "cache"), (root, linked_cache)):
                settings = Settings.model_construct(storage_root=model_root, hf_home=cache)
                with (
                    self.subTest(root=model_root),
                    patch.object(Path, "home", return_value=home),
                    patch(
                        "inference_runtime_manager.downloader.config._mount_root",
                        return_value=Path("/"),
                    ),
                    self.assertRaises(StorageUnavailableError),
                ):
                    validate_storage(settings)
            self.assertEqual(list(outside.iterdir()), [])

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
