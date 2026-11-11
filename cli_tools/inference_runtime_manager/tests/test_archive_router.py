"""Prevent unverified archive data from reaching a managed router."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, override
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.downloader.ollama_archive import prepare_archived_bundle
from inference_runtime_manager.installer.router import RouterConfig, RouterModel, prepare_router


class ArchiveRouterTests(TestCase):
    @override
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = Path(folder.name).resolve()
        self.root = self.storage / "ollama-archive/server-a"
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True)
        self.layers: list[dict[str, Any]] = []
        for kind, content in [("model", b"GGUFweights"), ("license", b"notice"), ("config", b"{}")]:
            digest = hashlib.sha256(content).hexdigest()
            (self.blobs / ("sha256-" + digest)).write_bytes(content)
            self.layers.append(
                {
                    "mediaType": "application/vnd.ollama.image." + kind,
                    "size": len(content),
                    "digest": "sha256:" + digest,
                }
            )
        self.manifest_path = self.root / "manifests/registry.ollama.ai/library/llama3/latest"
        self.manifest_path.parent.mkdir(parents=True)
        manifest = {"schemaVersion": 2, "config": self.layers[-1], "layers": self.layers[:-1]}
        raw = json.dumps(manifest).encode()
        self.manifest_path.write_bytes(raw)
        self.index = {
            "source_host": "server-a",
            "format": "ollama-v2",
            "models": [
                {
                    "model": "llama3/latest",
                    "path": str(self.manifest_path.relative_to(self.root)),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            ],
            "blob_count": 3,
            "verified_blobs": {
                layer["digest"].replace(":", "-"): layer["size"] for layer in self.layers
            },
        }
        (self.root / "index.json").write_text(json.dumps(self.index))
        self.digest = self.layers[0]["digest"].removeprefix("sha256:")
        patched = patch.dict(
            "inference_runtime_manager.downloader.ollama_archive.ARCHIVE_RECIPES",
            {"llama3/latest": ("llama3-8b", "gguf-q4-0", self.digest)},
        )
        patched.start()
        self.addCleanup(patched.stop)

    def prepare(self, *, verify=True):
        return prepare_archived_bundle(self.storage, "server-a", "llama3/latest", verify=verify)

    def test_mixed_router_keeps_aliases_and_original_archive_untouched(self):
        before = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        config = RouterConfig(
            target="server-a",
            bind_address="192.0.2.80",
            archive_profile="server-a",
            models=[
                RouterModel(
                    model_id="llama3-8b",
                    variant_id="gguf-q4-0",
                    archive_model="llama3/latest",
                    serving_id="llama3:latest",
                    context_tokens=8192,
                ),
                RouterModel(
                    model_id="gemma3-4b-it",
                    variant_id="gguf-q4-k-m",
                    serving_id="gemma3:latest",
                    context_tokens=8192,
                ),
            ],
        )
        with (
            patch.dict(
                "inference_runtime_manager.installer.router.ROUTER_RECIPES",
                {("llama3-8b", "gguf-q4-0"): ("sha256-" + self.digest, None)},
            ),
            patch(
                "inference_runtime_manager.installer.router.prepare_artifact",
                return_value=(
                    self.storage,
                    {
                        "destination": "library/gemma/test",
                        "files": [
                            {"path": "gemma-3-4b-it-Q4_K_M.gguf"},
                            {"path": "mmproj-model-f16.gguf"},
                        ],
                    },
                ),
            ) as hf,
        ):
            files, bundles = prepare_router(config, "server-a", self.storage)
            hf.assert_called_once()
            self.assertEqual(bundles[0][0], self.blobs)
            self.assertEqual(len(bundles[0][1]["files"]), 3)
            self.assertIn("alias = llama3:latest\n", files["models.ini"])
            self.assertIn("/sha256-" + self.digest, files["models.ini"])
            hf.return_value = (self.storage, {"files": []})
            with self.assertRaisesRegex(ValueError, "missing required"):
                prepare_router(config, "server-a", self.storage)
        self.assertEqual(
            before,
            {
                str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )

    def test_same_size_weight_and_ancillary_corruption_fails(self):
        expected = self.prepare()
        for layer in self.layers:
            path = self.blobs / layer["digest"].replace(":", "-")
            original = path.read_bytes()
            with self.subTest(layer=layer["mediaType"]):
                path.write_bytes(original[:-1] + b"X")
                with patch(
                    "inference_runtime_manager.downloader.ollama_archive.sha256",
                    side_effect=AssertionError("Metadata preparation must not hash blobs"),
                ):
                    self.assertEqual(self.prepare(verify=False), expected)
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    self.prepare()
                path.write_bytes(original)

    def test_wrong_reviewed_identity_and_manifest_mismatch_fail(self):
        with patch.dict(
            "inference_runtime_manager.downloader.ollama_archive.ARCHIVE_RECIPES",
            {"llama3/latest": ("llama3-8b", "gguf-q4-0", "0" * 64)},
        ):
            with self.assertRaisesRegex(ValueError, "reviewed model identity"):
                self.prepare()
            with self.assertRaisesRegex(ValueError, "reviewed model identity"):
                self.prepare(verify=False)
        self.manifest_path.write_text("{}")
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            self.prepare(verify=False)
        with self.assertRaisesRegex(ValueError, "Archive selection"):
            RouterModel(
                model_id="llama3-8b",
                variant_id="gguf-q4-0",
                archive_model="deepseek-r1/32b",
                serving_id="llama3:latest",
                context_tokens=8192,
            )

    def test_traversal_and_parent_symlinks_fail(self):
        with self.assertRaises(ValueError):
            prepare_archived_bundle(self.storage, "../server-a", "llama3/latest")
        moved = self.root / "original-blobs"
        self.blobs.rename(moved)
        self.blobs.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare(verify=False)

    def test_metadata_preparation_rejects_resized_blob(self):
        (self.blobs / ("sha256-" + self.digest)).write_bytes(b"GGUFshort")
        with self.assertRaisesRegex(ValueError, "blob size"):
            self.prepare(verify=False)
