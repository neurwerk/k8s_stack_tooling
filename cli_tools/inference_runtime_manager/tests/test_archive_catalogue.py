"""Keep retained archive choices out of downloads and make router edits atomic."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from inference_runtime_manager.downloader.catalog import load_catalog
from inference_runtime_manager.downloader.hierarchy import grouping
from inference_runtime_manager.downloader.main import _request_from_selection, _synchronize_locked
from inference_runtime_manager.downloader.models import Selection
from inference_runtime_manager.downloader.ollama_archive import ARCHIVE_RECIPES
from inference_runtime_manager.installer.router import (
    RouterConfig,
    RouterModel,
    add_catalogue_model,
    load_router,
    save_router,
)


class ArchiveCatalogueTests(TestCase):
    def test_archive_queue_never_fetches_from_hugging_face(self):
        catalog = load_catalog()
        selections = []
        for archive, (model_id, variant_id, _) in ARCHIVE_RECIPES.items():
            model = catalog.model(model_id)
            variant = model.variant(variant_id)
            self.assertEqual(variant.archive_model, archive)
            self.assertEqual(grouping(model, variant)[-1], "Ollama")
            selection = Selection(modelId=model_id, variantId=variant_id)
            selections.append(selection)
            with self.assertRaisesRegex(ValueError, "Archive-only"):
                _request_from_selection(catalog, selection)
        store = Mock()
        with TemporaryDirectory() as folder:
            remaining = _synchronize_locked(
                store, Mock(storage_root=Path(folder)), catalog, selections, Mock()
            )
        self.assertEqual(remaining, selections)
        store.synchronize.assert_not_called()

    def initial(self):
        return RouterConfig(
            target="server-a",
            bind_address="192.0.2.80",
            models=[
                RouterModel(
                    model_id="qwen38-27b",
                    variant_id="gguf-ud-q6-k",
                    serving_id="qwen3.8:27b",
                    context_tokens=32768,
                )
            ],
        )

    def test_cancel_at_each_prompt_and_invalid_edit_preserve_saved_state(self):
        for answers in [
            None,
            [None],
            ["llama3:latest", None],
            ["llama3:latest", "8192", None],
            ["qwen3.8:27b", "8192", "server-a"],
            ["llama3:latest", "99999", "server-a"],
            ["llama3:latest", "8192", "../bad"],
        ]:
            with (
                self.subTest(answers=answers),
                TemporaryDirectory() as folder,
                patch(
                    "inference_runtime_manager.installer.router.state_root",
                    return_value=Path(folder),
                ),
                patch(
                    "inference_runtime_manager.downloader.hierarchy.browse",
                    return_value=None if answers is None else {("llama3-8b", "gguf-q4-0")},
                ),
                patch("questionary.text") as text,
                patch("inference_runtime_manager.installer.router.apply_router") as apply,
            ):
                save_router(self.initial())
                path = Path(folder) / "router.json"
                previous = path.read_bytes()
                text.return_value.ask.side_effect = answers or []
                if answers is not None and len(answers) == 3 and answers[-1] is not None:
                    with self.assertRaises(ValueError):
                        add_catalogue_model("server-a")
                else:
                    add_catalogue_model("server-a")
                self.assertEqual(path.read_bytes(), previous)
                apply.assert_not_called()

    def test_each_archive_can_be_added_without_deploying(self):
        for archive, (model_id, variant_id, _) in ARCHIVE_RECIPES.items():
            with (
                self.subTest(archive=archive),
                TemporaryDirectory() as folder,
                patch(
                    "inference_runtime_manager.installer.router.state_root",
                    return_value=Path(folder),
                ),
                patch(
                    "inference_runtime_manager.downloader.hierarchy.browse",
                    return_value={(model_id, variant_id)},
                ),
                patch("questionary.text") as text,
                patch("inference_runtime_manager.installer.router.apply_router") as apply,
            ):
                original = self.initial()
                save_router(original)
                alias = archive.replace("/", ":")
                text.return_value.ask.side_effect = [alias, "8192", "server-a"]
                add_catalogue_model("server-a")
                result = load_router("server-a")
                self.assertEqual(result.models[0], original.models[0])
                self.assertEqual(result.bind_address, original.bind_address)
                self.assertEqual(result.archive_profile, "server-a")
                model = result.models[-1]
                self.assertEqual(
                    (
                        model.model_id,
                        model.variant_id,
                        model.archive_model,
                        model.serving_id,
                        model.context_tokens,
                    ),
                    (model_id, variant_id, archive, alias, 8192),
                )
                apply.assert_not_called()
