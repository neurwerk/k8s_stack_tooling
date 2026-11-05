"""Saved runtime choices must reach Compose without losing environment fallbacks."""

import os
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    save_workstation_config,
)
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker


class RuntimeSettingsTests(TestCase):
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
                        llm_context_tokens=8192,
                        llm_kv_cache_type="q8_0",
                    ),
                )
            )
            docker = Docker(DeploymentSettings(_env_file=None))
            self.assertEqual(docker.environment["NER_ENGLISH_DTYPE"], "float16")
            self.assertEqual(docker.environment["NER_GERMAN_DTYPE"], "float16")
            self.assertEqual(docker.environment["VLLM_GRANITE_GPU_MEMORY_UTILIZATION"], "0.18")
            self.assertEqual(docker.settings.llm_context_tokens, 8192)
            self.assertEqual(docker.settings.llm_kv_cache_type, "q8_0")
            with patch("subprocess.run") as run:
                run.return_value.stdout = '["--dtype=float32"]'
                self.assertFalse(docker.runtime_settings_match("ner-english"))
                run.return_value.stdout = '["--dtype=float16"]'
                self.assertTrue(docker.runtime_settings_match("ner-english"))
