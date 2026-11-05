"""A partial apply and rollback must not hide unapplied saved runtime settings."""

import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from inference_runtime_manager import workflow
from inference_runtime_manager.configuration import (
    WorkstationConfig,
    load_workstation_config,
    save_workstation_config,
)
from inference_runtime_manager.installer import upload
from inference_runtime_manager.installer.assignments import Assignment, Deployment, save_deployment
from inference_runtime_manager.installer.docker import DeploymentSettings, service_recipes
from inference_runtime_manager.installer.pending import pending_aliases, record_applied, snapshots


class PendingRollbackTests(TestCase):
    def exercise_rollback(self, fail_restore: bool) -> None:
        with TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory},
                    clear=True,
                )
            )
            stack.enter_context(
                patch("subprocess.run", side_effect=AssertionError("Unexpected subprocess"))
            )
            save_workstation_config(WorkstationConfig(docker_context="synthetic"))
            root = Path(directory) / "inference-runtime-manager"
            aliases = ["ner-german", "ner-english", "ner-multilingual"]
            presets = {
                alias: next(
                    item for item in service_recipes()[alias] if item["runtime"] == "gliner"
                )
                for alias in aliases
            }
            save_deployment(
                root,
                Deployment(
                    target="synthetic",
                    assignments={
                        alias: Assignment(
                            model_id=preset["model_id"], variant_id=preset["variant_id"]
                        )
                        for alias, preset in presets.items()
                    },
                ),
            )
            old_settings = DeploymentSettings()
            record_applied(root, "synthetic", snapshots(root, old_settings))
            restored = []

            def docker(settings):
                instance = Mock(settings=settings)
                instance.service_status.return_value = {}

                def run(*args):
                    self.assertEqual(settings.gliner_threshold, 0.5)
                    self.assertIn("--wait", args)
                    if fail_restore and args[-1] == presets[aliases[1]]["service"]:
                        raise ValueError("Restore failed")
                    restored.append(args[-1])

                instance.run.side_effect = run
                return instance

            def prepare(_model_root, _state_root, alias):
                return (
                    root,
                    {"destination": "synthetic"},
                    {"alias": alias, "service": presets[alias]["service"]},
                )

            def activate(_docker, application, _states):
                if application["alias"] == aliases[-1]:
                    # The first two real provision iterations have saved receipts.
                    self.assertEqual(pending_aliases(root, DeploymentSettings()), [aliases[-1]])
                    raise ValueError("Activation failed")

            stack.enter_context(patch.object(workflow, "state", return_value=root))
            stack.enter_context(patch.object(workflow, "storage", return_value=root))
            stack.enter_context(patch.object(workflow, "Docker", side_effect=docker))
            stack.enter_context(patch.object(workflow, "table"))
            stack.enter_context(patch.object(workflow, "print"))
            stack.enter_context(
                patch.object(
                    workflow.questionary,
                    "select",
                    return_value=Mock(ask=Mock(return_value="gliner_threshold")),
                )
            )
            stack.enter_context(
                patch.object(
                    workflow.questionary, "text", return_value=Mock(ask=Mock(return_value="0.6"))
                )
            )
            stack.enter_context(
                patch.object(
                    workflow.questionary, "confirm", return_value=Mock(ask=Mock(return_value=True))
                )
            )
            stack.enter_context(patch.object(upload, "prepare", side_effect=prepare))
            stack.enter_context(patch.object(upload, "transfer"))
            stack.enter_context(
                patch.object(upload, "activate_with_rollback", side_effect=activate)
            )
            with self.assertRaisesRegex(
                ValueError, "Restore failed" if fail_restore else "Activation failed"
            ):
                workflow.runtime_configuration()

            self.assertEqual(load_workstation_config().runtime.gliner_threshold, 0.6)
            self.assertEqual(pending_aliases(root, DeploymentSettings()), aliases)
            self.assertEqual(
                restored,
                [presets[alias]["service"] for alias in (aliases[:1] if fail_restore else aliases)],
            )
            # A successful restore confirms the old values. A failed restore
            # remains unconfirmed even if the user subsequently chooses those values.
            self.assertEqual(
                pending_aliases(root, old_settings), [aliases[1]] if fail_restore else []
            )

    def test_partial_apply_then_successful_rollback_keeps_all_changes_pending(self) -> None:
        self.exercise_rollback(fail_restore=False)

    def test_failed_rollback_confirms_only_successfully_restored_services(self) -> None:
        self.exercise_rollback(fail_restore=True)
