"""The new Qwen recipe must preserve the old assignment and reject corrupt weights."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, call

import yaml

from inference_runtime_manager.downloader.catalog import write_installed
from inference_runtime_manager.downloader.errors import IntegrityError
from inference_runtime_manager.downloader.integrity import write_checksums
from inference_runtime_manager.downloader.models import (
    ArtifactRequest,
    FileChecksum,
    InstalledState,
    StoredArtifact,
)
from inference_runtime_manager.installer.assignments import load_deployment, recipe, services
from inference_runtime_manager.installer.upload import activate_with_rollback, prepare_recipe


class QwenRecipeTests(TestCase):
    def test_existing_and_new_assignments_select_distinct_services(self) -> None:
        self.assertEqual(services()["llm-general"]["service"], "llm-general")
        with TemporaryDirectory() as directory:
            state = Path(directory) / "deployment.json"
            for model_id, variant_id, expected in (
                ("qwen3-4b", "gguf-q4-k-m", "llm-general"),
                ("qwen38-27b", "gguf-ud-q6-k", "llm-general-qwen38"),
            ):
                state.write_text(
                    json.dumps(
                        {
                            "target": "test-server",
                            "assignments": {
                                "llm-general": {
                                    "model_id": model_id,
                                    "variant_id": variant_id,
                                    "enabled": False,
                                }
                            },
                        }
                    )
                )
                assignment = load_deployment(state.parent, "test-server").assignments["llm-general"]
                self.assertEqual(
                    recipe("llm-general", assignment.model_id, assignment.variant_id)["service"],
                    expected,
                )

    def test_failed_switch_restores_previous_service(self) -> None:
        docker = Mock()
        docker.worker.return_value = {"destination": "library/old"}

        def run(*args: str) -> None:
            if args[0] == "up" and args[-1] == "llm-general-qwen38":
                raise ValueError("startup failed")

        docker.run.side_effect = run
        application = {
            "alias": "llm-general",
            "service": "llm-general-qwen38",
            "runtime": "llama.cpp",
        }
        with self.assertRaisesRegex(RuntimeError, "previous service restored"):
            activate_with_rollback(
                docker, application, {"llm-general": ("running", "healthy", "old")}
            )
        self.assertEqual(docker.run.call_args_list[0], call("stop", "llm-general"))
        self.assertEqual(docker.run.call_args_list[2], call("stop", "llm-general-qwen38"))
        self.assertEqual(docker.run.call_args_list[-1].args[-1], "llm-general")
        docker.worker.assert_any_call(
            {"action": "restore_active", "alias": "llm-general", "destination": "library/old"}
        )

    def test_local_artifact_is_verified_before_recipe_preparation(self) -> None:
        preset = recipe("llm-general", "qwen38-27b", "gguf-ud-q6-k")
        name = "Qwen3.8-27B-UD-Q6_K.gguf"
        revision = "4ca720788d1e01f1bff70c033e0d0028fd02e502"
        request = ArtifactRequest(
            model_id="qwen38-27b",
            variant_id="gguf-ud-q6-k",
            category="llm",
            source="unsloth/Qwen3.8-27B-GGUF",
            revision=revision,
            include=[name],
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "models/llm/unsloth/Qwen3.8-27B-GGUF" / revision / request.variant_id
            model.mkdir(parents=True)
            content = b"synthetic GGUF fixture"
            (model / name).write_bytes(content)
            record = FileChecksum(
                path=name, sha256=hashlib.sha256(content).hexdigest(), size=len(content)
            )
            artifact = StoredArtifact.create(request, revision, [record])
            (model / "artifact.yaml").write_text(
                yaml.safe_dump(artifact.model_dump(by_alias=True, mode="json"))
            )
            write_checksums(model / "checksums.sha256", [record])
            write_installed(
                root / "installed.yaml",
                InstalledState(
                    schemaVersion=1, installed=[artifact.to_installed(model.relative_to(root))]
                ),
            )
            _, _, application = prepare_recipe(root, "llm-general", preset)
            self.assertEqual(application["service"], "llm-general-qwen38")
            (model / name).write_bytes(b"corrupted")
            with self.assertRaisesRegex(IntegrityError, "checksums do not match"):
                prepare_recipe(root, "llm-general", preset)
