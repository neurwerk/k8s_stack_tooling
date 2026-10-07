"""Offline companion files must be pinned, complete and covered by checksums."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from inference_runtime_manager.downloader.errors import IntegrityError
from inference_runtime_manager.downloader.huggingface import HuggingFaceClient
from inference_runtime_manager.downloader.models import ArtifactRequest, ModelCompanion
from inference_runtime_manager.downloader.store import ArtifactStore


class CompanionTests(TestCase):
    def test_companion_files_are_verified_before_bundle_publication(self) -> None:
        request = ArtifactRequest(
            model_id="test-model",
            variant_id="gliner",
            category="ner",
            source="example/model",
            revision="a" * 40,
            companions=[
                ModelCompanion(
                    directory="backbone",
                    source="example/backbone",
                    revision="b" * 40,
                    include=["config.json", "spm.model"],
                )
            ],
        )
        missing = False

        def download(item: ArtifactRequest, revision: str, destination: Path) -> None:
            names = item.include or ["pytorch_model.bin"]
            for name in names:
                if missing and name == "spm.model":
                    continue
                (destination / name).write_text("test content")

        client = HuggingFaceClient({})
        with (
            TemporaryDirectory() as root,
            patch.object(client, "resolve_revision", return_value="a" * 40),
            patch.object(client, "download", side_effect=download),
            patch.object(client, "verify_download") as verify,
        ):
            store = ArtifactStore(Path(root), client)
            artifact = store.synchronize(request)
            self.assertEqual(artifact.companions, request.companions)
            self.assertIn("backbone/spm.model", [file.path for file in artifact.files])
            self.assertEqual(verify.call_args_list[-1].args[1], "b" * 40)
            missing = True
            other = request.model_copy(update={"variant_id": "incomplete"})
            with self.assertRaises(IntegrityError):
                store.synchronize(other)
            self.assertFalse(store.destination(other, "a" * 40).exists())

    def test_companions_reject_unpinned_or_escaping_paths(self) -> None:
        for revision, name in [("main", "config.json"), ("a" * 40, "../config.json")]:
            with self.subTest(revision=revision, name=name), self.assertRaises(ValidationError):
                ModelCompanion(
                    directory="backbone",
                    source="example/backbone",
                    revision=revision,
                    include=[name],
                )
