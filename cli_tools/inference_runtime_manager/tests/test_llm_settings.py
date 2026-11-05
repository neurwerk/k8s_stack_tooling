"""Protect context bounds, restart decisions and cancellable LLM settings."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override
from unittest import TestCase
from unittest.mock import Mock, patch

from pydantic import ValidationError

from inference_runtime_manager import workflow
from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    load_workstation_config,
    save_workstation_config,
)
from inference_runtime_manager.installer.assignments import (
    Assignment,
    Deployment,
    save_deployment,
)
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.upload import provision


class LLMSettingsTests(TestCase):
    @override
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": temporary.name, "XDG_STATE_HOME": temporary.name},
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        save_workstation_config(WorkstationConfig(docker_context="test-server"))
        self.state = self.root / "inference-runtime-manager"
        save_deployment(
            self.state,
            Deployment(
                target="test-server",
                assignments={
                    "llm-general": Assignment(model_id="qwen3-4b", variant_id="gguf-q4-k-m")
                },
            ),
        )

    def test_invalid_context_rejected_before_deployment(self) -> None:
        for value in (0, -1, 4096.5, "invalid", 2147483648):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RuntimeConfig(llm_context_tokens=value)
        settings = DeploymentSettings(_env_file=None, llm_context_tokens=32768)
        settings.validate_llm_context("qwen3-4b")
        settings.llm_context_tokens = 32769
        docker = Docker(settings)
        with (
            patch.object(docker, "service_status") as status,
            patch("inference_runtime_manager.installer.upload.prepare") as prepare,
            patch("inference_runtime_manager.installer.upload.transfer") as transfer,
            patch("inference_runtime_manager.installer.upload.activate_with_rollback") as activate,
        ):
            with self.assertRaisesRegex(ValueError, "32768"):
                provision(docker, self.root, self.state, ["llm-general"])
            status.assert_not_called()
            prepare.assert_not_called()
            transfer.assert_not_called()
            activate.assert_not_called()

    def test_context_or_either_cache_change_restarts_matching_healthy_service(self) -> None:
        for cache in ("q8_0", "q4_0"):
            settings = DeploymentSettings(
                _env_file=None, llm_context_tokens=8192, llm_kv_cache_type=cache
            )
            docker = Docker(settings)
            expected = [
                "--ctx-size",
                "8192",
                "--cache-type-k",
                cache,
                "--cache-type-v",
                cache,
                "--flash-attn",
                "on",
            ]
            for changed_flag in (None, "--ctx-size", "--cache-type-k", "--cache-type-v"):
                with self.subTest(cache=cache, changed_flag=changed_flag):
                    command = expected.copy()
                    if changed_flag:
                        command[command.index(changed_flag) + 1] = (
                            "4096" if changed_flag == "--ctx-size" else "f16"
                        )
                    with (
                        patch.object(
                            docker,
                            "service_status",
                            return_value={
                                "llm-general": (
                                    "running",
                                    "healthy",
                                    docker.image_tag_for("llama.cpp"),
                                )
                            },
                        ),
                        patch.object(
                            docker, "active_identity", return_value=("qwen3-4b", "gguf-q4-k-m")
                        ),
                        patch("subprocess.run", return_value=Mock(stdout=json.dumps(command))),
                        patch(
                            "inference_runtime_manager.installer.upload.prepare",
                            return_value=(
                                self.root,
                                {"destination": "synthetic"},
                                {"alias": "llm-general", "service": "llm-general"},
                            ),
                        ),
                        patch("inference_runtime_manager.installer.upload.transfer"),
                        patch(
                            "inference_runtime_manager.installer.upload.activate_with_rollback"
                        ) as activate,
                    ):
                        provision(docker, self.root, self.state, ["llm-general"])
                        self.assertEqual(activate.call_count, int(changed_flag is not None))

    def test_cancel_inputs_does_not_save_or_apply(self) -> None:
        for action in ("context", "cache"):
            with (
                self.subTest(action=action),
                patch.object(workflow.Docker, "service_status", return_value={}),
                patch.object(workflow.questionary, "select") as select,
                patch.object(workflow.questionary, "text") as text,
                patch.object(workflow, "save_workstation_config") as save,
                patch.object(workflow, "provision") as apply,
            ):
                select.side_effect = [
                    Mock(ask=Mock(return_value=action)),
                    Mock(ask=Mock(return_value=None)),
                ]
                text.return_value.ask.return_value = None
                workflow.llm_configuration()
                save.assert_not_called()
                apply.assert_not_called()

    def test_declining_apply_keeps_saved_choice(self) -> None:
        with (
            patch.object(workflow.Docker, "service_status", return_value={}),
            patch.object(workflow.questionary, "select") as select,
            patch.object(workflow.questionary, "confirm") as confirm,
            patch.object(workflow, "provision") as apply,
        ):
            select.side_effect = [
                Mock(ask=Mock(return_value="cache")),
                Mock(ask=Mock(return_value="q8_0")),
            ]
            confirm.return_value.ask.return_value = False
            workflow.llm_configuration()
            self.assertEqual(load_workstation_config().runtime.llm_kv_cache_type, "q8_0")
            apply.assert_not_called()
