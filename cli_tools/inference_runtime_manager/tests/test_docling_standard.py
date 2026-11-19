"""Official Serve preparation, offline startup, credentials and model integrity."""

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    management_state,
    save_workstation_config,
)
from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.downloader.errors import IntegrityError
from inference_runtime_manager.downloader.http_resume import download_file
from inference_runtime_manager.installer.api_tests import request
from inference_runtime_manager.installer.assignments import Assignment, Deployment, save_deployment
from inference_runtime_manager.installer.docker import (
    DeploymentSettings,
    Docker,
    resource,
    services,
)
from inference_runtime_manager.installer.images import prepare_local_images
from inference_runtime_manager.installer.pending import pending_aliases, record_applied, snapshots
from inference_runtime_manager.installer.upload import provision


class DownloadResponse(io.BytesIO):
    status = 200

    def __init__(self, payload: bytes):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}


class StandardRuntimeTests(TestCase):
    def test_page_limit_reaches_launch_without_reusing_old_credentials_or_concurrency(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": directory, "DOCLING_STANDARD_API_KEY": "old-model-key"},
                clear=True,
            ),
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
            with patch("subprocess.run") as run:
                for key in (None, "", "   "):
                    with self.subTest(key=key):
                        unauthenticated = DeploymentSettings(
                            _env_file=None, docling_serve_api_key=key
                        )
                        with self.assertRaisesRegex(ValueError, "DOCLING_SERVE_API_KEY"):
                            provision(Docker(unauthenticated), root, root, ["docling-standard"])
                run.assert_not_called()
            with self.assertRaisesRegex(ValidationError, "Remove docling_standard_concurrency"):
                RuntimeConfig.model_validate({"docling_standard_concurrency": 3})
            save_workstation_config(
                WorkstationConfig(
                    docker_context="synthetic", runtime=RuntimeConfig(docling_max_pages=500)
                )
            )
            settings = DeploymentSettings(_env_file=None, docling_serve_api_key="serve-key")
            self.assertEqual(pending_aliases(root, settings), ["docling-standard"])
            docker = Docker(settings)
            with patch("subprocess.run") as run:
                docker.run("up", "--pull", "never", "--no-deps", "-d", "docling-standard")
            self.assertEqual(run.call_count, 1)
            launch = run.call_args
            self.assertEqual(
                launch.args[0][-6:],
                ["up", "--pull", "never", "--no-deps", "-d", "docling-standard"],
            )
            self.assertEqual(launch.kwargs["env"]["DOCLING_SERVE_MAX_NUM_PAGES"], "500")
            self.assertEqual(launch.kwargs["env"]["DOCLING_SERVE_API_KEY"], "serve-key")
            save_deployment(root, Deployment(target="synthetic"))
            self.assertEqual(pending_aliases(root, settings), [])

    def test_official_image_preparation_rejects_lookalikes_and_reuses_verified_cache(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}, clear=True),
        ):
            settings = Settings(
                _env_file=None,
                storage_root=Path(directory),
                hf_home=Path(directory) / "hub",
                build_docker_context="workstation",
            )
            current = {
                "Os": "linux",
                "Architecture": "amd64",
                "RepoDigests": ["synthetic/custom@sha256:" + "0" * 64],
            }
            mutations = []

            def docker_operation(command, **kwargs):
                self.assertEqual(command[:3], ["docker", "--context", "workstation"])
                action = command[3:]
                if action[:2] == ["context", "inspect"]:
                    return subprocess.CompletedProcess(command, 0, '"unix:///synthetic.sock"')
                if action[:2] == ["image", "inspect"]:
                    return subprocess.CompletedProcess(command, 0, json.dumps(current))
                mutations.append(action)
                if action[:2] == ["image", "rm"]:
                    current.clear()
                elif action[0] == "pull":
                    self.assertEqual(action[1:3], ["--platform", "linux/amd64"])
                    self.assertRegex(
                        action[3],
                        r"^ghcr\.io/docling-project/docling-serve-cu128:v1\.38\.2"
                        r"@sha256:[a-f0-9]{64}$",
                    )
                    current.update(Os="linux", Architecture="amd64", RepoDigests=[action[3]])
                elif action[0] != "tag":
                    self.fail(f"Unexpected image operation: {action}")
                return subprocess.CompletedProcess(command, 0)

            with (
                patch("shutil.which", return_value="/synthetic/docker"),
                patch("subprocess.run", side_effect=docker_operation),
            ):
                prepare_local_images(settings, ["docling-standard"])
                self.assertEqual([action[0] for action in mutations], ["image", "pull", "tag"])
                mutations.clear()
                prepare_local_images(settings, ["docling-standard"])
                self.assertEqual(mutations, [])

    def test_startup_reuses_readonly_cache_and_executes_official_serve(self):
        command = yaml.safe_load(resource("compose.yaml"))["services"]["docling-standard"][
            "command"
        ]
        with TemporaryDirectory() as directory:
            source = Path(directory).resolve() / "cache"
            artifacts = Path(directory).resolve() / "artifacts"
            artifacts.mkdir()
            for name in services()["docling-standard"]["required_files"]:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic model")
            original_path = Path

            def local_path(value):
                return (
                    source
                    if str(value) == "/models/active/docling-standard"
                    else original_path(value)
                )

            with (
                patch.dict(os.environ, {"DOCLING_STANDARD_API_KEY": "old-model-key"}, clear=True),
                patch("pathlib.Path", side_effect=local_path),
                patch("tempfile.mkdtemp", return_value=str(artifacts)),
                patch("os.execvp") as launch,
            ):
                with self.assertRaisesRegex(SystemExit, "DOCLING_SERVE_API_KEY"):
                    exec(compile(command[-1], "<docling-startup>", "exec"), {})
                launch.assert_not_called()
                os.environ["DOCLING_SERVE_API_KEY"] = "serve-key"
                model = source / "model.safetensors"
                model.unlink()
                with self.assertRaisesRegex(SystemExit, "Incomplete Docling model bundle"):
                    exec(compile(command[-1], "<docling-startup>", "exec"), {})
                launch.assert_not_called()
                model.write_bytes(b"synthetic model")
                exec(compile(command[-1], "<docling-startup>", "exec"), {})
                launch.assert_called_once_with("docling-serve", ["docling-serve", "run"])
                self.assertEqual(Path(os.environ["DOCLING_SERVE_ARTIFACTS_PATH"]), artifacts)
                self.assertEqual(
                    (artifacts / "docling-project--docling-layout-heron").resolve(), source
                )
                self.assertEqual(
                    (artifacts / "docling-project--docling-models").resolve(),
                    source / "tableformer",
                )
                presets = json.loads(os.environ["DOCLING_SERVE_CUSTOM_OCR_PRESETS"])
                for name in ("auto", "default"):
                    self.assertEqual(presets[name]["backend"], "torch")
                    self.assertEqual(presets[name]["lang"], ["ch"])
                    for field in (
                        "det_model_path",
                        "cls_model_path",
                        "rec_model_path",
                        "rec_keys_path",
                    ):
                        self.assertEqual(
                            Path(presets[name][field]).read_bytes(), b"synthetic model"
                        )
                self.assertFalse(any(path.is_symlink() for path in source.rglob("*")))

    def test_generated_requests_use_serve_key_header_and_keep_other_bearer_auth(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": directory,
                    "XDG_STATE_HOME": directory,
                    "DOCLING_SERVE_API_KEY": "serve-key",
                    "VLM_DOCUMENTS_API_KEY": "model-key",
                },
                clear=True,
            ),
        ):
            root = Path(directory)
            save_workstation_config(
                WorkstationConfig(
                    docker_context="synthetic", storage_root=root / "models", hf_home=root / "hub"
                )
            )
            save_deployment(
                management_state(),
                Deployment(
                    target="synthetic",
                    assignments={
                        alias: Assignment(
                            model_id=services()[alias]["model_id"],
                            variant_id=services()[alias]["variant_id"],
                        )
                        for alias in ("docling-standard", "vlm-documents")
                    },
                ),
            )
            docker = Docker(DeploymentSettings(_env_file=None))

            def execute_request(command, *, env, input, **kwargs):
                output = io.BytesIO()
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(input))),
                    patch("sys.stdout", SimpleNamespace(buffer=output)),
                ):
                    exec(compile(command[-1], "<generated-request>", "exec"), {})
                return subprocess.CompletedProcess(command, 0, stdout=output.getvalue())

            for alias, expected in (
                ("docling-standard", {"X-api-key": "serve-key"}),
                ("vlm-documents", {"Authorization": "Bearer model-key"}),
            ):
                with (
                    self.subTest(alias=alias),
                    patch("subprocess.run", side_effect=execute_request),
                    patch("urllib.request.urlopen", return_value=DownloadResponse(b"{}")) as send,
                ):
                    self.assertEqual(request(docker, alias, "/ready"), b"{}")
                    self.assertEqual(dict(send.call_args.args[0].header_items()), expected)

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
