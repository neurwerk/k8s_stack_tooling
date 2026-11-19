"""Saved runtime choices must reach Compose without losing environment fallbacks."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from pydantic import ValidationError

from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    save_workstation_config,
)
from inference_runtime_manager.installer.assignments import Assignment, Deployment, save_deployment
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker, services
from inference_runtime_manager.installer.pending import pending_aliases, record_applied, snapshots


class RuntimeSettingsTests(TestCase):
    def test_docling_image_selection_and_one_run_override(self) -> None:
        with (
            TemporaryDirectory() as root,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": root}, clear=True),
        ):
            save_workstation_config(WorkstationConfig(docker_context="synthetic"))
            docker = Docker(DeploymentSettings(_env_file=None))
            expected = docker.environment["DOCLING_STANDARD_IMAGE"]
            other_image = docker.environment["VLLM_IMAGE"]
            self.assertEqual(docker.image_tag_for("docling-standard-cuda"), expected)
            self.assertTrue(docker.image_matches("docling-standard-cuda", expected))
            self.assertFalse(docker.image_matches("docling-standard-cuda", other_image))
            replacement = "synthetic/docling-standard:replacement"
            with patch("subprocess.run") as run:
                docker.run_with_image(
                    "docling-standard-cuda", replacement, "up", "-d", "docling-standard"
                )
            run.assert_called_once()
            self.assertEqual(
                run.call_args.args[0], docker.command + ["up", "-d", "docling-standard"]
            )
            self.assertEqual(run.call_args.kwargs["env"]["DOCLING_STANDARD_IMAGE"], replacement)
            self.assertEqual(run.call_args.kwargs["env"]["VLLM_IMAGE"], other_image)
            self.assertEqual(docker.image_tag_for("docling-standard-cuda"), expected)

    def test_saved_overrides_reach_compose_and_unset_values_use_environment(self) -> None:
        with (
            TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": root,
                    "NER_ENGLISH_DTYPE": "float32",
                    "NER_GERMAN_DTYPE": "float16",
                    "VLLM_GRANITE_GPU_MEMORY_UTILIZATION": "0.25",
                    "VLLM_GRANITE_MAX_NUM_SEQS": "4",
                    "LLM_CONTEXT_TOKENS": "4096",
                    "LLM_KV_CACHE_TYPE": "f16",
                },
            ),
        ):
            save_workstation_config(
                WorkstationConfig(
                    docker_context="test-server",
                    runtime=RuntimeConfig(
                        ner_english_dtype="float16",
                        vllm_granite_gpu_memory_utilization=0.18,
                        vllm_granite_max_num_seqs=64,
                        llm_context_tokens=8192,
                        llm_kv_cache_type="q8_0",
                    ),
                )
            )
            docker = Docker(DeploymentSettings(_env_file=None))
            self.assertEqual(docker.environment["NER_ENGLISH_DTYPE"], "float16")
            self.assertEqual(docker.environment["NER_GERMAN_DTYPE"], "float16")
            self.assertEqual(docker.environment["VLLM_GRANITE_GPU_MEMORY_UTILIZATION"], "0.18")
            self.assertEqual(docker.environment["VLLM_GRANITE_MAX_NUM_SEQS"], "64")
            self.assertEqual(docker.settings.llm_context_tokens, 8192)
            self.assertEqual(docker.settings.llm_kv_cache_type, "q8_0")
            with patch("subprocess.run") as run:
                run.return_value.stdout = '["--dtype=float32"]'
                self.assertFalse(docker.runtime_settings_match("ner-english"))
                run.return_value.stdout = '["--dtype=float16"]'
                self.assertTrue(docker.runtime_settings_match("ner-english"))
                run.return_value.stdout = (
                    '["--gpu-memory-utilization", "0.18", "--max-num-seqs", "1"]'
                )
                self.assertFalse(docker.runtime_settings_match("vlm-documents"))
                run.return_value.stdout = (
                    '["--gpu-memory-utilization", "0.18", "--max-num-seqs", "64"]'
                )
                self.assertTrue(docker.runtime_settings_match("vlm-documents"))
            save_workstation_config(WorkstationConfig(docker_context="test-server"))
            self.assertEqual(DeploymentSettings(_env_file=None).vllm_granite_max_num_seqs, 4)

    def test_concurrency_validation_and_legacy_pending_scope(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}, clear=True),
        ):
            save_workstation_config(WorkstationConfig(docker_context="synthetic"))
            settings = DeploymentSettings(_env_file=None)
            self.assertEqual(settings.vllm_granite_max_num_seqs, 1)
            for value in (0, -1, 1.5, "invalid"):
                with self.subTest(value=value):
                    with self.assertRaises(ValidationError):
                        RuntimeConfig.model_validate({"vllm_granite_max_num_seqs": value})
                    with self.assertRaises(ValidationError):
                        DeploymentSettings(_env_file=None, vllm_granite_max_num_seqs=value)
            root = Path(directory)
            presets = services()
            save_deployment(
                root,
                Deployment(
                    target="synthetic",
                    assignments={
                        alias: Assignment(
                            model_id=presets[alias]["model_id"],
                            variant_id=presets[alias]["variant_id"],
                        )
                        for alias in ("vlm-documents", "llm-general")
                    },
                ),
            )
            baseline = snapshots(root, settings)
            del baseline["vlm-documents"]["runtime_options"]["vllm_granite_max_num_seqs"]
            record_applied(root, "synthetic", baseline)
            self.assertEqual(pending_aliases(root, settings), [])
            settings.vllm_granite_max_num_seqs = 64
            self.assertEqual(pending_aliases(root, settings), ["vlm-documents"])
