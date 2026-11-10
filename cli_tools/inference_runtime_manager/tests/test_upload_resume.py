"""Exercise interrupted binary delivery against the real volume worker."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, override
from unittest import TestCase

from inference_runtime_manager.installer import worker
from inference_runtime_manager.installer.docker import Docker
from inference_runtime_manager.installer.upload import transfer


class LocalDocker(Docker):
    def __init__(self, root):
        self.root = root
        self.environment = os.environ.copy()
        self.command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from inference_runtime_manager.installer import worker; "
                f"worker.ROOT=Path({str(root)!r}); worker.main()"
            ),
        ]

    @override
    def worker_command(self):
        return self.command

    def request(self, payload, data=b""):
        return subprocess.run(
            self.command,
            input=json.dumps(payload).encode() + b"\n" + data,
            capture_output=True,
            env=self.environment,
            timeout=15,
        )

    @override
    def worker(self, payload):
        result = self.request(payload)
        if result.returncode:
            raise ValueError(result.stderr.decode())
        return json.loads(result.stdout)


class UploadResumeTests(TestCase):
    @override
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.data = {"first": b"complete", "nested/second": b"abcdefghijk", "empty": b""}
        for name, content in self.data.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.manifest: dict[str, Any] = {
            "destination": "library/model/revision/variant/fingerprint",
            "source": "example/model",
            "revision": "revision",
            "files": [
                {"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in self.data.items()
            ],
        }
        self.docker = LocalDocker(self.root / "remote")
        self.target = self.docker.root / self.manifest["destination"]

    def status(self):
        return self.docker.worker({"action": "upload_status", "manifest": self.manifest})

    def receive(self, prefixes, data):
        return self.docker.request(
            {"action": "receive", "manifest": self.manifest, "prefixes": prefixes}, data
        )

    def stage(self):
        fingerprint = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
        return self.docker.root / ".installer/uploads" / fingerprint / "artifact"

    def interrupt(self):
        result = self.receive(self.status(), b"completeabc")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.target.exists())
        self.assertEqual((self.stage() / "nested/second").read_bytes(), b"abc")

    def test_interruption_then_only_tail_publishes_and_uploader_skips_completed_files(self):
        self.interrupt()
        self.assertEqual(self.status()["nested/second"]["offset"], 3)
        # Send only the unsent tail; exact-length framing rejects a repeated prefix.
        result = self.receive(self.status(), b"defghijk")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, content in self.data.items():
            self.assertEqual((self.target / name).read_bytes(), content)
        # A second independent interrupted transfer exercises the workstation sender.
        self.manifest["destination"] += "-second"
        self.target = self.docker.root / self.manifest["destination"]
        self.interrupt()
        transfer(self.docker, self.source, self.manifest)
        self.assertEqual((self.target / "nested/second").read_bytes(), b"abcdefghijk")

    def test_partial_and_complete_same_length_corruption_refuse_append(self):
        self.interrupt()
        for content in [b"xyz", b"xxxxxxxxxxx"]:
            with self.subTest(content=content):
                staged = self.stage() / "nested/second"
                staged.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "prefix differs"):
                    transfer(self.docker, self.source, self.manifest)
                self.assertEqual(staged.read_bytes(), content)
                self.assertFalse(self.target.exists())

    def test_stale_status_and_unexpected_paths_preserve_bytes(self):
        self.interrupt()
        previous = self.status()
        staged = self.stage() / "nested/second"
        staged.write_bytes(b"abcd")
        self.assertNotEqual(self.receive(previous, b"defghijk").returncode, 0)
        self.assertEqual(staged.read_bytes(), b"abcd")
        for kind in ["unexpected", "symlink"]:
            extra = self.stage() / "extra"
            if kind == "unexpected":
                extra.write_bytes(b"retained")
            else:
                extra.symlink_to(self.source / "first")
            with self.assertRaises(ValueError):
                self.status()
            extra.unlink()
        metadata = self.stage().parent / "manifest.json"
        metadata.write_text("{}")
        with self.assertRaisesRegex(ValueError, "Conflicting upload manifest"):
            self.status()
        self.assertEqual(staged.read_bytes(), b"abcd")

    def test_bad_final_checksum_never_changes_active_bundle(self):
        old = self.docker.root / "library/old"
        old.mkdir(parents=True)
        (old / "weights").write_bytes(b"keep")
        active = self.docker.root / "active"
        active.symlink_to(old, target_is_directory=True)
        self.interrupt()
        result = self.receive(self.status(), b"xxxxxxxx")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Checksum mismatch", result.stderr)
        self.assertFalse(self.target.exists())
        self.assertEqual((self.stage() / "nested/second").read_bytes(), b"abcxxxxxxxx")
        self.assertEqual((active / "weights").read_bytes(), b"keep")

    def test_manifest_identity_and_reserved_path_validation(self):
        self.interrupt()
        previous = self.stage()
        self.manifest["source"] = "example/different"
        self.assertEqual(self.status()["nested/second"]["offset"], 0)
        self.assertEqual((previous / "nested/second").read_bytes(), b"abc")
        for name in ["../escape", ".receipt.json/child", "nested"]:
            manifest = {
                **self.manifest,
                "files": self.manifest["files"]
                + [{"path": name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}],
            }
            with self.subTest(name=name), self.assertRaises(ValueError):
                worker.validate(manifest)
