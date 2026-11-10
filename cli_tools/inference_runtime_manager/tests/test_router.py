"""Protect target isolation and model identity at the router planning boundary."""

import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from inference_runtime_manager.configuration import (
    WorkstationConfig,
    create_profile,
    save_workstation_config,
    select_profile,
)
from inference_runtime_manager.installer import worker
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.router import (
    MAX_TEMPLATE_BYTES,
    ROUTER_RECIPES,
    RouterConfig,
    RouterModel,
    activate_router,
    apply_router,
    config_identity,
    load_router,
    prepare_router,
    read_template,
    save_router,
)


def config(target: str = "server-a") -> RouterConfig:
    return RouterConfig(
        target=target,
        bind_address="192.0.2.80",
        models=[
            RouterModel(
                model_id="mistral-small32-24b",
                variant_id="gguf-q4-k-m",
                serving_id="mistral-small3.2:24b",
                context_tokens=32768,
            ),
            RouterModel(
                model_id="qwen38-27b",
                variant_id="gguf-ud-q6-k",
                serving_id="qwen3.8:27b",
                context_tokens=262144,
            ),
        ],
    )


class RouterTests(TestCase):
    def test_second_remote_bundle_failure_never_stops_previous_router(self):
        module = "inference_runtime_manager.installer.router"
        with (
            patch(module + ".DeploymentSettings") as deployment,
            patch(module + ".load_router", return_value=config()),
            patch(module + ".applied_router", return_value=None),
            patch(module + ".Settings"),
            patch(
                module + ".prepare_router",
                return_value=({"models.ini": "config"}, [(Path("/a"), {}), (Path("/b"), {})]),
            ),
            patch(module + ".Docker") as factory,
            patch(module + ".install_local_images"),
            patch(module + ".transfer"),
            patch(module + ".subprocess.run") as run,
        ):
            deployment.return_value.docker_context = "server-a"
            factory.return_value.worker.side_effect = [{"present": True}, {"present": False}]
            with self.assertRaisesRegex(ValueError, "full verification"):
                apply_router("legacy-router")
            run.assert_not_called()

    def test_rollback_failure_is_reported(self):
        docker = Mock()
        docker.worker.return_value = {"version": None}
        docker.docker_command = ["docker", "--context", "server-a"]
        docker.environment = {}
        start = Mock(side_effect=ValueError("startup failed"))
        stop = Mock(side_effect=OSError("cannot stop candidate"))
        with patch("inference_runtime_manager.installer.router.subprocess.run"):
            with self.assertRaisesRegex(RuntimeError, "rollback needs attention"):
                activate_router(docker, "a" * 64, "legacy", start, Mock(), stop)

    def test_staging_rejects_bad_files_without_changing_current(self):
        with TemporaryDirectory() as folder, patch.object(worker, "CONFIG_ROOT", Path(folder)):
            first = worker.stage_router({"files": {"models.ini": "version = 1\n"}})["version"]
            worker.select_router({"version": first})
            for files in [
                {"models.ini": "valid", "../escape.jinja": "bad"},
                {"models.ini": "a" * (MAX_TEMPLATE_BYTES + 1)},
                {"missing.ini": "bad"},
            ]:
                with self.assertRaises(ValueError):
                    worker.stage_router({"files": files})
                self.assertEqual(worker.current_router()["version"], first)
            second_files = {"models.ini": "version = 1\n[other]\n"}
            second = worker.stage_router({"files": second_files})["version"]
            self.assertEqual(second, config_identity(second_files))
            self.assertEqual(worker.current_router()["version"], first)
            worker.select_router({"version": second})
            worker.select_router({"version": first})
            self.assertEqual(worker.current_router()["version"], first)

    def test_startup_and_acceptance_failure_restore_prior_version_and_container(self):
        for boundary in ["startup", "acceptance"]:
            with (
                self.subTest(boundary=boundary),
                TemporaryDirectory() as folder,
                patch.object(worker, "CONFIG_ROOT", Path(folder)),
            ):
                first = worker.stage_router({"files": {"models.ini": "old"}})["version"]
                second = worker.stage_router({"files": {"models.ini": "new"}})["version"]
                worker.select_router({"version": first})
                docker = Mock()
                docker.docker_command = ["docker", "--context", "server-a"]
                docker.environment = {}
                docker.worker.side_effect = lambda p: (
                    worker.current_router()
                    if p["action"] == "router_current"
                    else worker.select_router(p)
                )
                start, accept, stop = Mock(), Mock(), Mock()
                (start if boundary == "startup" else accept).side_effect = ValueError(
                    "candidate failed"
                )
                with patch("inference_runtime_manager.installer.router.subprocess.run") as run:
                    with self.assertRaisesRegex(ValueError, "candidate failed"):
                        activate_router(docker, second, "previous-router", start, accept, stop)
                    self.assertEqual(
                        run.call_args_list[0].args[0][-2:], ["stop", "previous-router"]
                    )
                    self.assertEqual(
                        run.call_args_list[-1].args[0][-2:], ["start", "previous-router"]
                    )
                stop.assert_called_once()
                self.assertEqual(worker.current_router()["version"], first)
                self.assertTrue(worker.router_version_path(second).is_dir())

    def test_target_mismatch_precedes_model_operations(self):
        with patch("inference_runtime_manager.installer.router.prepare_artifact") as prepare:
            with self.assertRaisesRegex(ValueError, "different Docker context"):
                prepare_router(config(), "server-b", Path("/unused"))
            prepare.assert_not_called()

    def test_profiles_keep_router_state_separate(self):
        with (
            TemporaryDirectory() as folder,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": folder, "XDG_STATE_HOME": folder}),
        ):
            try:
                create_profile("router-a")
                select_profile("router-a")
                save_router(config())
                create_profile("router-b")
                select_profile("router-b")
                save_router(config("server-b"))
                with self.assertRaisesRegex(ValueError, "different Docker context"):
                    load_router("server-a")
                select_profile("router-a")
                self.assertEqual(load_router("server-a").target, "server-a")
            finally:
                select_profile("default")

    def test_aliases_and_required_projector_survive_rendering(self):
        def artifact(root, model, variant):
            names = [name for name in ROUTER_RECIPES[(model, variant)] if name]
            return root, {
                "destination": f"library/{model}/revision/variant/fingerprint",
                "files": [{"path": n} for n in names],
            }

        with patch(
            "inference_runtime_manager.installer.router.prepare_artifact", side_effect=artifact
        ):
            files, bundles = prepare_router(config(), "server-a", Path("/unused"))
        self.assertEqual(len(bundles), 2)
        self.assertIn("alias = mistral-small3.2:24b\n", files["models.ini"])
        self.assertIn("alias = qwen3.8:27b\n", files["models.ini"])
        self.assertIn("/mmproj-F16.gguf", files["models.ini"])
        with patch(
            "inference_runtime_manager.installer.router.prepare_artifact",
            return_value=(Path("/unused"), {"files": []}),
        ):
            with self.assertRaisesRegex(ValueError, "missing required"):
                prepare_router(config(), "server-a", Path("/unused"))

    def test_rejects_injection_duplicates_and_unsafe_templates(self):
        data = config().model_dump()
        data["models"][0]["serving_id"] = "model\nmodel = /other"
        with self.assertRaises(ValueError):
            RouterConfig.model_validate(data)
        data = config().model_dump()
        data["models"].append(data["models"][0])
        with self.assertRaises(ValueError):
            RouterConfig.model_validate(data)
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            template = root / "template.jinja"
            template.write_text("test")
            link = root / "link"
            link.symlink_to(template)
            for path in [link, root / ".." / root.name / template.name, Path("relative.jinja")]:
                with self.assertRaises(ValueError):
                    read_template(path)
            template.write_bytes(b"a" * (MAX_TEMPLATE_BYTES + 1))
            with self.assertRaises(ValueError):
                read_template(template)

    def test_real_compose_render_keeps_router_opt_in_and_private(self):
        with (
            TemporaryDirectory() as folder,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": folder, "XDG_STATE_HOME": folder}),
        ):
            try:
                create_profile("render-router")
                select_profile("render-router")
                save_workstation_config(WorkstationConfig(docker_context="server-a"))
                docker = Docker(DeploymentSettings())
                docker.environment.update(
                    ROUTER_BIND_ADDRESS="192.0.2.80", ROUTER_PORT="11434", ROUTER_MAX_LOADED="1"
                )
                result = subprocess.run(
                    docker.command + ["--profile", "router", "config", "--format", "json"],
                    env=docker.environment,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                rendered = json.loads(result.stdout)
                router = rendered["services"]["chat-router"]
                self.assertEqual(router["profiles"], ["router"])
                self.assertEqual(router["ports"][0]["host_ip"], "192.0.2.80")
                self.assertEqual(router["ports"][0]["published"], "11434")
                self.assertTrue(all(v["read_only"] for v in router["volumes"]))
                self.assertEqual(
                    rendered["volumes"]["runtime-config"]["name"],
                    "inference-runtime-manager_config",
                )
                self.assertIn("local-ai-offline/llamacpp:", router["image"])
                self.assertIn("--offline", router["command"])
                self.assertNotIn("--models-preset", rendered["services"]["llm-general"]["command"])
            finally:
                select_profile("default")
