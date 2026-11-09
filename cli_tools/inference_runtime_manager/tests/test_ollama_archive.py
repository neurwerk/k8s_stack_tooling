"""Archived Ollama bytes must not be confused with deployable manager models."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import override
from unittest import TestCase
from unittest.mock import Mock, patch

from inference_runtime_manager.configuration import create_profile, select_profile
from inference_runtime_manager.downloader.catalog import load_catalog, write_installed
from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.downloader.main import _run_selection
from inference_runtime_manager.downloader.models import InstalledModel, InstalledState
from inference_runtime_manager.installer.assignments import recipe


def archive(root: Path, profile: str) -> tuple[Path, str]:
    path = root / "ollama-archive" / profile
    name = "example/one"
    manifest_path = path / "manifests/registry.ollama.ai/library" / name
    manifest_path.parent.mkdir(parents=True)
    blobs = path / "blobs"
    blobs.mkdir()
    contents = {
        "model": b"GGUFtiny model",
        "system": b"never-display-private-system-content",
        "config": b"{}",
    }
    records = {}
    indexed_blobs: dict[str, int] = {}
    for kind, content in contents.items():
        digest = hashlib.sha256(content).hexdigest()
        (blobs / f"sha256-{digest}").write_bytes(content)
        records[kind] = {"digest": f"sha256:{digest}", "size": len(content)}
        indexed_blobs[f"sha256-{digest}"] = len(content)
    manifest = {
        "schemaVersion": 2,
        "config": records["config"],
        "layers": [
            {**records["model"], "mediaType": "application/vnd.ollama.image.model"},
            {**records["system"], "mediaType": "application/vnd.ollama.image.system"},
        ],
    }
    data = json.dumps(manifest).encode()
    manifest_path.write_bytes(data)
    index = {
        "source_host": profile,
        "format": "ollama-v2",
        "models": [
            {
                "model": name,
                "path": str(manifest_path.relative_to(path)),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
        "blob_count": len(contents),
        "verified_blobs": indexed_blobs,
    }
    (path / "index.json").write_text(json.dumps(index))
    return path, f"sha256-{hashlib.sha256(contents['model']).hexdigest()}"


class OllamaArchiveTests(TestCase):
    @override
    def tearDown(self) -> None:
        select_profile("default")

    def test_verified_archive_is_listed_without_exposing_system_layer(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}
            ):
                create_profile("server")
                select_profile("server")
                archive(root, "server")
                write_installed(
                    root / "installed.yaml",
                    InstalledState(
                        schemaVersion=1,
                        installed=[
                            InstalledModel(
                                modelId="qwen3-4b",
                                variantId="gguf-q4-k-m",
                                source="Qwen/Qwen3-4B-GGUF",
                                revision="test-revision",
                                path="models/llm/example",
                                installedAt=datetime.now(UTC),
                                totalBytes=123,
                                fileCount=1,
                                verification="sha256",
                            )
                        ],
                    ),
                )
                output: list[str] = []
                _run_selection(
                    "view_installed",
                    Mock(),
                    Mock(),
                    Settings(storage_root=root, hf_home=root / "hf", _env_file=None),
                    load_catalog(),
                    output.append,
                )
                text = "\n".join(output)
                self.assertIn("Qwen3 4B", text)
                self.assertIn("archived ollama models", text.lower())
                self.assertIn("deployable)", text.lower())
                self.assertIn("example:one", text)
                self.assertNotIn("never-display-private-system-content", text)
                with self.assertRaises(ValueError):
                    recipe("llm-general", "example", "ollama-archive")

    def test_missing_archive_keeps_current_inventory_behavior(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}
            ):
                create_profile("server")
                select_profile("server")
                output: list[str] = []
                root = Path(directory)
                _run_selection(
                    "view_installed",
                    Mock(),
                    Mock(),
                    Settings(storage_root=root, hf_home=root / "hf", _env_file=None),
                    load_catalog(),
                    output.append,
                )
                self.assertEqual(output, ["No models are installed."])

    def test_incomplete_or_malformed_archive_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}
            ):
                create_profile("server")
                select_profile("server")
                path, model_blob = archive(root, "server")
                settings = Settings(storage_root=root, hf_home=root / "hf", _env_file=None)
                for corruption in ("missing-blob", "invalid-index"):
                    if corruption == "missing-blob":
                        (path / "blobs" / model_blob).unlink()
                    else:
                        (path / "index.json").write_text('{"unexpected":"private-system-content"}')
                    output: list[str] = []
                    _run_selection(
                        "view_installed", Mock(), Mock(), settings, load_catalog(), output.append
                    )
                    text = "\n".join(output)
                    self.assertIn("archive is incomplete or invalid", text)
                    self.assertNotIn("example:one", text)
                    self.assertNotIn("private-system-content", text)
