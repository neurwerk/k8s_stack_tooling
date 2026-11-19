"""Standard inference settings and pinned external model integrity."""

import hashlib
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.configuration import WorkstationConfig, save_workstation_config
from inference_runtime_manager.downloader.errors import IntegrityError
from inference_runtime_manager.downloader.http_resume import download_file
from inference_runtime_manager.installer.assignments import Assignment, Deployment, save_deployment
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.pending import pending_aliases, record_applied, snapshots


class DownloadResponse(io.BytesIO):
    status = 200

    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}


class StandardRuntimeTests(TestCase):
    def test_standard_concurrency_is_independent_and_live_drift_is_detected(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}, clear=True),
        ):
            root = Path(directory)
            save_workstation_config(WorkstationConfig(docker_context="synthetic"))
            settings = DeploymentSettings(_env_file=None)
            save_deployment(
                root,
                Deployment(
                    target="synthetic",
                    assignments={
                        "docling-standard": Assignment(
                            model_id="docling-standard", variant_id="cuda"
                        ),
                        "vlm-documents": Assignment(
                            model_id="granite-docling-258m", variant_id="transformers"
                        ),
                    },
                ),
            )
            record_applied(root, "synthetic", snapshots(root, settings))
            settings.docling_standard_concurrency = 3
            self.assertEqual(pending_aliases(root, settings), ["docling-standard"])
            docker = Docker(settings)
            with patch.object(docker, "service_command", return_value=["--concurrency", "1"]):
                self.assertFalse(docker.runtime_settings_match("docling-standard"))
            with patch.object(docker, "service_command", return_value=["--concurrency", "3"]):
                self.assertTrue(docker.runtime_settings_match("docling-standard"))
            save_deployment(root, Deployment(target="synthetic"))
            self.assertEqual(pending_aliases(root, settings), [])

    def test_external_file_digest_is_verified_without_hub_credentials(self):
        payload = b"synthetic model bytes"
        with TemporaryDirectory() as directory:
            target = Path(directory) / "model.pth"
            for digest in ("0" * 64, hashlib.sha256(payload).hexdigest()):
                response = DownloadResponse(payload)
                with (
                    patch("urllib.request.build_opener") as opener,
                    patch(
                        "inference_runtime_manager.downloader.http_resume.headers",
                        side_effect=AssertionError("Must not read Hub credentials"),
                    ),
                ):
                    opener.return_value.open.return_value = response
                    if digest == "0" * 64:
                        with self.assertRaises(IntegrityError):
                            download_file(
                                "https://example.invalid/model",
                                target,
                                len(payload),
                                digest,
                                False,
                                {},
                                authenticated=False,
                            )
                        self.assertFalse(target.exists())
                    else:
                        download_file(
                            "https://example.invalid/model",
                            target,
                            len(payload),
                            digest,
                            False,
                            {},
                            authenticated=False,
                        )
                        self.assertEqual(target.read_bytes(), payload)
                    request = opener.return_value.open.call_args.args[0]
                    self.assertFalse(request.has_header("Authorization"))
