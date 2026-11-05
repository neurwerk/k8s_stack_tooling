"""Saved CPU/CUDA choices must select the right runtime and VRAM budget."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import yaml

from inference_runtime_manager.installer.assignments import (
    Assignment,
    Deployment,
    deployment_services,
    save_deployment,
)
from inference_runtime_manager.installer.docker import resource
from inference_runtime_manager.workflow import memory_label, warn_vram


class KokoroDeviceTests(TestCase):
    def test_saved_device_controls_budget_and_service_wiring(self):
        compose = yaml.safe_load(resource("compose.yaml"))["services"]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for variant, enabled, device in (
                ("onnx", True, "cpu"),
                ("onnx-cuda", True, "cuda"),
                ("onnx-cuda", False, "cuda"),
            ):
                with self.subTest(variant=variant, enabled=enabled):
                    save_deployment(
                        root,
                        Deployment(
                            target="test-server",
                            assignments={
                                "tts-german": Assignment(
                                    model_id="kokoro-german-martin",
                                    variant_id=variant,
                                    enabled=enabled,
                                ),
                            },
                        ),
                    )
                    recipe = deployment_services(root)["tts-german"]
                    service = compose[recipe["service"]]
                    self.assertEqual(service["environment"]["KOKORO_DEVICE"], device)
                    self.assertEqual(service.get("gpus"), "all" if device == "cuda" else None)
                    self.assertIn(
                        "KOKORO_CUDA_IMAGE" if device == "cuda" else "KOKORO_IMAGE",
                        service["image"],
                    )
                    with patch("inference_runtime_manager.workflow.print") as output:
                        warn_vram(root)
                    total = recipe["estimated_vram_gib"] if enabled else 0
                    self.assertIn(f"~{total:g} GiB", output.call_args_list[0].args[0])
                    if device == "cpu":
                        self.assertEqual(total, 0)
                        self.assertIn("system RAM only", memory_label(recipe))
                    else:
                        self.assertGreater(recipe["estimated_vram_gib"], 0)
