"""Exporter lifecycle stays scoped to the selected host and offline images."""

import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    create_profile,
    save_workstation_config,
    select_profile,
)
from inference_runtime_manager.installer import monitoring


class MonitoringTests(TestCase):
    def test_lifecycle_and_failed_install_stay_on_selected_target(self):
        with (
            TemporaryDirectory() as folder,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": folder, "XDG_STATE_HOME": folder}),
        ):
            create_profile("metrics-test")
            select_profile("metrics-test")
            try:
                save_workstation_config(
                    WorkstationConfig(
                        docker_context="target-test",
                        runtime=RuntimeConfig(bind_address="192.0.2.83"),
                    )
                )
                with (
                    patch.object(monitoring, "Settings") as settings,
                    patch.object(monitoring, "prepare_local_images") as prepare,
                    patch.object(monitoring, "install_local_images") as install,
                    patch.object(monitoring.subprocess, "run") as run,
                ):
                    run.return_value.stdout = "target-host\n"
                    monitoring.run("prepare")
                    prepare.assert_called_once_with(
                        settings.return_value, ["node-exporter", "dcgm-exporter"]
                    )
                    run.assert_not_called()
                    for action, suffix in [
                        ("status", ["ps", "--all"]),
                        ("apply", ["up", "-d", "--pull", "never"]),
                        ("stop", ["stop"]),
                    ]:
                        monitoring.run(action)
                        command = run.call_args.args[0]
                        self.assertEqual(command[:3], ["docker", "--context", "target-test"])
                        self.assertEqual(
                            command[3:6], ["compose", "--project-name", "local-ai-monitoring"]
                        )
                        self.assertEqual(command[-len(suffix) :], suffix)
                        self.assertEqual(
                            run.call_args.kwargs["env"]["MONITORING_BIND_ADDRESS"], "192.0.2.83"
                        )
                    self.assertEqual(install.call_args.args[2], ["node-exporter", "dcgm-exporter"])
                    run.reset_mock()
                    install.side_effect = ValueError("image unavailable")
                    with self.assertRaisesRegex(ValueError, "image unavailable"):
                        monitoring.run("apply")
                    self.assertEqual(run.call_count, 1)
                    self.assertIn("info", run.call_args.args[0])
            finally:
                select_profile("default")

    def test_compose_renders_host_metrics_and_gpu_access(self):
        resource = Path(
            str(files("inference_runtime_manager.resources").joinpath("monitoring.yaml"))
        )
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "-f",
                str(resource),
                "config",
                "--format",
                "json",
            ],
            env={
                **os.environ,
                "NODE_EXPORTER_IMAGE": "node:test",
                "DCGM_EXPORTER_IMAGE": "gpu:test",
                "MONITORING_HOSTNAME": "target-test",
                "MONITORING_BIND_ADDRESS": "192.0.2.83",
            },
            check=True,
            capture_output=True,
            text=True,
        )
        services = json.loads(result.stdout)["services"]
        node, gpu = services["node-exporter"], services["dcgm-exporter"]
        self.assertEqual(node["network_mode"], "host")
        self.assertEqual(node["pid"], "host")
        self.assertIn("--path.rootfs=/host", node["command"])
        self.assertIn("--web.listen-address=192.0.2.83:9100", node["command"])
        self.assertEqual(node["volumes"][0]["source"], "/")
        self.assertTrue(node["volumes"][0]["read_only"])
        self.assertEqual(gpu["network_mode"], "host")
        self.assertEqual(gpu["hostname"], "target-test")
        self.assertEqual(gpu["environment"]["DCGM_EXPORTER_LISTEN"], "192.0.2.83:9400")
        self.assertIn("SYS_ADMIN", gpu["cap_add"])
        self.assertEqual(gpu["gpus"][0]["count"], -1)
