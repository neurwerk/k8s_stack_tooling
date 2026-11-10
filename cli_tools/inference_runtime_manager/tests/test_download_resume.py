"""Interrupted transfers survive process restarts without publishing partial models."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.downloader.errors import HuggingFaceCommandError, IntegrityError
from inference_runtime_manager.downloader.huggingface import HuggingFaceClient
from inference_runtime_manager.downloader.models import ArtifactRequest, ModelCompanion
from inference_runtime_manager.downloader.store import ArtifactStore


def request() -> ArtifactRequest:
    return ArtifactRequest(
        model_id="example",
        variant_id="gguf",
        category="llm",
        source="example/model",
        revision="a" * 40,
        include=["model.gguf"],
    )


def companion() -> ModelCompanion:
    return ModelCompanion(
        directory="backbone",
        source="example/backbone",
        revision="b" * 40,
        include=["weights.bin"],
    )


class DownloadResumeTests(TestCase):
    def test_partial_download_survives_restart(self) -> None:
        for failure in (HuggingFaceCommandError, KeyboardInterrupt, OSError):
            with self.subTest(failure=failure), TemporaryDirectory() as root:
                client = HuggingFaceClient({})
                paths = []

                def download(item, revision, destination, paths=paths, failure=failure):
                    paths.append(destination)
                    model = destination / "model.gguf"
                    if len(paths) == 1:
                        model.write_bytes(b"partial")
                        raise failure("interrupted")
                    self.assertEqual(model.read_bytes(), b"partial")
                    model.write_bytes(b"complete")

                with (
                    patch.object(client, "resolve_revision", return_value="a" * 40),
                    patch.object(client, "download", side_effect=download),
                    patch.object(client, "verify_download"),
                ):
                    store = ArtifactStore(Path(root), client)
                    with self.assertRaises(failure):
                        store.synchronize(request())
                    self.assertFalse(store.destination(request(), "a" * 40).exists())
                    result = ArtifactStore(Path(root), client).synchronize(request())
                    self.assertEqual(paths[0], paths[1])
                    self.assertEqual([file.path for file in result.files], ["model.gguf"])
                    self.assertFalse(paths[0].exists())

    def test_companion_resumes_after_main_verification(self) -> None:
        with TemporaryDirectory() as root:
            client = HuggingFaceClient({})
            selected = request().model_copy(update={"companions": [companion()]})
            attempts = []

            def download(item, revision, destination):
                if item.source == selected.source:
                    (destination / "model.gguf").write_bytes(b"model")
                    return
                attempts.append(destination)
                path = destination / "weights.bin"
                if len(attempts) == 1:
                    path.write_bytes(b"partial")
                    raise KeyboardInterrupt
                self.assertEqual(path.read_bytes(), b"partial")
                path.write_bytes(b"complete")

            with (
                patch.object(client, "resolve_revision", return_value="a" * 40),
                patch.object(client, "download", side_effect=download),
                patch.object(client, "verify_download"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    ArtifactStore(Path(root), client).synchronize(selected)
                result = ArtifactStore(Path(root), client).synchronize(selected)
                self.assertEqual(attempts[0], attempts[1])
                self.assertIn("backbone/weights.bin", [file.path for file in result.files])

    def test_changed_request_never_reuses_partial(self) -> None:
        with TemporaryDirectory() as root:
            client = HuggingFaceClient({})
            paths = []

            def download(item, revision, destination):
                paths.append(destination)
                (destination / "model.gguf").write_bytes(b"partial")
                raise KeyboardInterrupt

            with (
                patch.object(client, "resolve_revision", side_effect=lambda item: item.revision),
                patch.object(client, "download", side_effect=download),
            ):
                for selected in (
                    request(),
                    request().model_copy(update={"revision": "c" * 40}),
                    request().model_copy(update={"include": ["other.gguf"]}),
                    request().model_copy(update={"companions": [companion()]}),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        ArtifactStore(Path(root), client).synchronize(selected)
                self.assertEqual(len(set(paths)), 4)
                self.assertTrue(all((path / "model.gguf").exists() for path in paths))

    def test_invalid_content_is_not_published(self) -> None:
        for failure in ("main", "companion", "overlap"):
            with self.subTest(failure=failure), TemporaryDirectory() as root:
                client = HuggingFaceClient({})
                selected = request().model_copy(update={"companions": [companion()]})
                paths = []

                def download(
                    item, revision, destination, paths=paths, failure=failure, selected=selected
                ):
                    paths.append(destination)
                    (destination / item.include[0]).write_bytes(b"content")
                    if failure == "overlap" and item.source == selected.source:
                        (destination / "backbone").mkdir()

                def verify(item, revision, destination, failure=failure, selected=selected):
                    if (failure == "main" and item.source == selected.source) or (
                        failure == "companion" and item.source != selected.source
                    ):
                        raise IntegrityError("corrupt")

                with (
                    patch.object(client, "resolve_revision", return_value="a" * 40),
                    patch.object(client, "download", side_effect=download),
                    patch.object(client, "verify_download", side_effect=verify),
                ):
                    store = ArtifactStore(Path(root), client)
                    with self.assertRaises(IntegrityError):
                        store.synchronize(selected)
                    self.assertFalse(paths[0].exists())
                    self.assertFalse(store.destination(selected, "a" * 40).exists())
