"""Profiles must not direct saved assignments or runtime settings to another server."""

import json
import os
import sys
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager import cli
from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    config_path,
    create_profile,
    load_workstation_config,
    management_state,
    profile_names,
    save_workstation_config,
    select_profile,
    state_root,
)
from inference_runtime_manager.installer.docker import DeploymentSettings


class ProfileTests(TestCase):
    @override
    def tearDown(self) -> None:
        select_profile("default")

    def test_existing_settings_and_assignments_survive_new_profile(self) -> None:
        with TemporaryDirectory() as base:
            root = Path(base)
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(root), "XDG_STATE_HOME": str(root)}
            ):
                save_workstation_config(WorkstationConfig(docker_context="old-server"))
                original_config = config_path().read_bytes()
                state_root().mkdir(parents=True, exist_ok=True)
                old_state = state_root() / "deployment.json"
                old_state.write_text('{"target":"old-server","assignments":{}}')
                storage = root / "models"
                storage.mkdir()
                (storage / "deployment.json").write_text('{"target":"old-server","assignments":{}}')

                create_profile("new-server")
                select_profile("new-server")
                self.assertEqual(load_workstation_config().runtime.bind_address, "127.0.0.1")
                self.assertFalse((management_state(storage) / "deployment.json").exists())
                save_workstation_config(
                    WorkstationConfig(
                        docker_context="new-server", runtime=RuntimeConfig(bind_address="127.0.0.1")
                    )
                )
                (state_root() / "deployment.json").write_text(
                    '{"target":"new-server","assignments":{}}'
                )
                select_profile("default")
                self.assertEqual(config_path().read_bytes(), original_config)
                self.assertEqual(json.loads(old_state.read_text())["target"], "old-server")

    def test_migrated_legacy_profile_does_not_leave_ghost_default(self) -> None:
        with TemporaryDirectory() as base:
            root = Path(base)
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(root), "XDG_STATE_HOME": str(root)}
            ):
                save_workstation_config(WorkstationConfig(docker_context="old-server"))
                state_root().mkdir(exist_ok=True)
                (state_root() / "deployment.json").write_text(
                    '{"target":"old-server","assignments":{}}'
                )
                create_profile("old-server")
                select_profile("old-server")
                named_config, named_state = config_path(), state_root()
                select_profile("default")
                config_path().replace(named_config)
                named_state.mkdir(parents=True, exist_ok=True)
                (state_root() / "deployment.json").replace(named_state / "deployment.json")
                self.assertTrue(named_config.is_file())
                self.assertTrue((named_state / "deployment.json").is_file())
                self.assertEqual(profile_names(), ["old-server"])

    def test_new_profile_does_not_inherit_legacy_target_or_bind(self) -> None:
        with TemporaryDirectory() as base:
            root = Path(base)
            env_file = root / ".env"
            env_file.write_text(
                "INFERENCE_RUNTIME_MANAGER_DOCKER_CONTEXT=old-server\n"
                "LOCALAI_BIND_ADDRESS=192.0.2.83\n"
                "LLM_GENERAL_API_KEY=reference-only\n"
            )
            with (
                chdir(root),
                patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": str(root), "XDG_STATE_HOME": str(root)},
                    clear=True,
                ),
            ):
                self.assertEqual(DeploymentSettings().docker_context, "old-server")
                create_profile("new-server")
                select_profile("new-server")
                with self.assertRaisesRegex(ValueError, "Set the target Docker context"):
                    DeploymentSettings()
                save_workstation_config(
                    WorkstationConfig(
                        docker_context="new-server", runtime=RuntimeConfig(bind_address="127.0.0.1")
                    )
                )
                settings = DeploymentSettings()
                self.assertEqual(settings.docker_context, "new-server")
                self.assertEqual(settings.bind_address, "127.0.0.1")
                self.assertIsNone(settings.llm_general_api_key)
                config_path().with_name(".env").write_text("LLM_GENERAL_API_KEY=profile-only\n")
                key = DeploymentSettings().llm_general_api_key
                assert key is not None
                self.assertEqual(key.get_secret_value(), "profile-only")

    def test_explicit_cli_profile_blocks_mismatched_target_before_docker(self) -> None:
        with TemporaryDirectory() as base:
            root = Path(base)
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": str(root), "XDG_STATE_HOME": str(root)}
            ):
                create_profile("new-server")
                select_profile("new-server")
                save_workstation_config(
                    WorkstationConfig(
                        docker_context="new-server", runtime=RuntimeConfig(bind_address="127.0.0.1")
                    )
                )
                state_root().mkdir(parents=True, exist_ok=True)
                deployment = state_root() / "deployment.json"
                deployment.write_text('{"target":"old-server","assignments":{}}')
                select_profile("default")
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["inference-runtime-manager", "--profile", "new-server", "status"],
                    ),
                    patch.object(cli.Docker, "run") as run,
                    self.assertRaises(SystemExit) as result,
                ):
                    cli.main()
                self.assertEqual(result.exception.code, 1)
                run.assert_not_called()
                self.assertEqual(json.loads(deployment.read_text())["target"], "old-server")
