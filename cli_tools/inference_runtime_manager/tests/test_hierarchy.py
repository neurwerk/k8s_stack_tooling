"""Protect catalogue selections and existing model bytes during layout changes."""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import yaml

from inference_runtime_manager.downloader.catalog import (
    load_catalog,
    load_installed,
    write_installed,
)
from inference_runtime_manager.downloader.hierarchy import (
    artifact_path,
    browse,
    component,
    grouping,
)
from inference_runtime_manager.downloader.integrity import write_checksums
from inference_runtime_manager.downloader.main import _request_from_selection
from inference_runtime_manager.downloader.migration import library_lock, migrate_library
from inference_runtime_manager.downloader.models import (
    FileChecksum,
    InstalledState,
    Selection,
    StoredArtifact,
)
from inference_runtime_manager.installer.upload import prepare_artifact


class HierarchyTests(TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        catalog = load_catalog()
        model = catalog.model("qwen38-27b")
        variant = model.variant("gguf-ud-q6-k")
        request = _request_from_selection(
            catalog, Selection(modelId=model.id, variantId=variant.id)
        )
        revision = request.revision
        old = root / "models" / model.category / request.source / revision / variant.id
        old.mkdir(parents=True)
        content = b"synthetic model bytes"
        name = variant.include[0]
        (old / name).write_bytes(content)
        record = FileChecksum(
            path=name, size=len(content), sha256=hashlib.sha256(content).hexdigest()
        )
        artifact = StoredArtifact.create(request, revision, [record])
        (old / "artifact.yaml").write_text(
            yaml.safe_dump(artifact.model_dump(mode="json", by_alias=True))
        )
        write_checksums(old / "checksums.sha256", [record])
        write_installed(
            root / "installed.yaml",
            InstalledState(
                schemaVersion=1, installed=[artifact.to_installed(old.relative_to(root))]
            ),
        )
        return old, root / artifact_path(model, variant, revision)

    def test_migration_preserves_bytes_identity_and_preparation_and_is_repeatable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = self.fixture(root)
            before = prepare_artifact(root, "qwen38-27b", "gguf-ud-q6-k")[1]
            self.assertEqual(len(migrate_library(root)), 1)
            self.assertTrue(old.exists())
            migrate_library(root, dry_run=False)
            self.assertFalse(old.exists())
            source, after = prepare_artifact(root, "qwen38-27b", "gguf-ud-q6-k")
            self.assertEqual(source, new)
            self.assertEqual(before, after)
            self.assertEqual(migrate_library(root, dry_run=False), [])

    def test_move_interrupted_before_inventory_write_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = self.fixture(root)
            with patch(
                "inference_runtime_manager.downloader.migration.write_installed",
                side_effect=OSError("interrupted"),
            ):
                with self.assertRaises(OSError):
                    migrate_library(root, dry_run=False)
            self.assertTrue(new.is_dir())
            self.assertFalse(old.exists())
            migrate_library(root, dry_run=False)
            self.assertEqual(
                load_installed(root / "installed.yaml").installed[0].path,
                new.relative_to(root).as_posix(),
            )

    def test_conflict_keeps_both_directories_and_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = self.fixture(root)
            new.mkdir(parents=True)
            before = (root / "installed.yaml").read_bytes()
            with self.assertRaisesRegex(ValueError, "already exists"):
                migrate_library(root, dry_run=False)
            self.assertTrue(old.is_dir())
            self.assertTrue(new.is_dir())
            self.assertEqual(before, (root / "installed.yaml").read_bytes())

    def test_active_writer_blocks_migration_and_preserves_partials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = self.fixture(root)
            partial = old.parent / ".download.partial"
            partial.mkdir()
            (partial / "chunk").write_bytes(b"keep")
            with library_lock(root):
                with self.assertRaisesRegex(ValueError, "Another model"):
                    migrate_library(root, dry_run=False)
            migrate_library(root, dry_run=False)
            self.assertTrue(new.exists())
            self.assertEqual((partial / "chunk").read_bytes(), b"keep")

    def test_editions_and_formats_do_not_merge_and_paths_are_safe(self):
        catalog = load_catalog()
        base = catalog.model("qwen38-27b")
        tuned = catalog.model("qwen38-27b-turbo-fable-cold-fusion-neo-coder-max-mtp")
        self.assertEqual(
            grouping(base, base.variants[0])[:3], grouping(tuned, tuned.variants[0])[:3]
        )
        self.assertNotEqual(
            grouping(base, base.variants[0])[3], grouping(tuned, tuned.variants[0])[3]
        )
        speech = catalog.model("kokoro-german-martin")
        self.assertNotEqual(
            artifact_path(speech, speech.variants[0], "a" * 40),
            artifact_path(speech, speech.variants[1], "a" * 40),
        )
        for bad in ("../escape", "..", "/absolute", "x\\y"):
            with self.assertRaises(ValueError):
                component(bad)

    def test_editing_one_edition_keeps_other_categories_and_cancel_discards_edits(self):
        catalog = load_catalog()
        other = ("silero-vad", "onnx")
        picked = ("qwen38-27b", "gguf-ud-q6-k")
        selected = {other}
        answers = [
            "Language models",
            "Alibaba",
            "Qwen",
            "3.8 27B",
            "unsloth",
            "__back",
            "__back",
            "__back",
            "__back",
            "__save",
        ]
        with (
            patch("inference_runtime_manager.downloader.hierarchy.questionary.select") as select,
            patch(
                "inference_runtime_manager.downloader.hierarchy.questionary.checkbox"
            ) as checkbox,
        ):
            select.side_effect = [Mock(ask=Mock(return_value=x)) for x in answers]
            checkbox.return_value.ask.return_value = [picked]
            self.assertEqual(browse(catalog, selected=selected, multiple=True), {other, picked})
            self.assertEqual(select.call_args_list[0].args[0], "Category")
            categories = {choice.title for choice in select.call_args_list[0].kwargs["choices"]}
            self.assertTrue(
                {"Language models", "Speech recognition", "General vision", "Text-to-speech"}
                <= categories
            )
            self.assertEqual(selected, {other})
            select.side_effect = [Mock(ask=Mock(return_value=x)) for x in answers[:5] + [None]]
            self.assertIsNone(browse(catalog, selected=selected, multiple=True))
            self.assertEqual(selected, {other})

    def test_categoryless_layout_migrates_under_original_category(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old, new = self.fixture(root)
            relative = new.relative_to(root)
            categoryless = root / Path("models", *relative.parts[2:])
            categoryless.parent.mkdir(parents=True)
            old.rename(categoryless)
            inventory = load_installed(root / "installed.yaml")
            inventory.installed[0].path = categoryless.relative_to(root).as_posix()
            write_installed(root / "installed.yaml", inventory)
            before = prepare_artifact(root, "qwen38-27b", "gguf-ud-q6-k")[1]
            migrate_library(root, dry_run=False)
            source, after = prepare_artifact(root, "qwen38-27b", "gguf-ud-q6-k")
            self.assertEqual(source.relative_to(root).parts[:3], ("models", "llm", "alibaba"))
            self.assertFalse(categoryless.exists())
            self.assertEqual(before, after)
