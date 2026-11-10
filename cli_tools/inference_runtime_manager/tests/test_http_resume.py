"""Exercise actual interrupted HTTP responses and authenticated redirects."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, override
from unittest import TestCase
from unittest.mock import patch

from inference_runtime_manager.downloader.errors import HuggingFaceCommandError, IntegrityError
from inference_runtime_manager.downloader.http_resume import download_file


class HttpResumeTests(TestCase):
    def test_empty_and_complete_partial_publish_without_network(self):
        with TemporaryDirectory() as folder:
            for content in [b"", b"complete before interruption"]:
                target = Path(folder) / f"model-{len(content)}"
                url = f"https://example.invalid/{target.name}"
                digest = hashlib.sha256(content).hexdigest()
                cache = target.parent / ".cache" / "inference-runtime-manager"
                cache.mkdir(parents=True, exist_ok=True)
                if content:
                    partial = cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{digest}.partial"
                    partial.write_bytes(content)
                with patch("urllib.request.build_opener") as network:
                    download_file(url, target, len(content), digest, False, {})
                    network.assert_not_called()
                self.assertEqual(target.read_bytes(), content)

    def test_interrupted_transfer_resumes_and_checks_ranges(self):
        content = b"a" * (3 * 1024 * 1024)
        digest = hashlib.sha256(content).hexdigest()
        state: dict[str, Any] = {"requests": [], "mode": "interrupt"}

        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                value = self.headers.get("Range")
                state["requests"].append(value)
                start = int(value.split("=")[1].split("-")[0]) if value else 0
                self.send_response(206 if value and state["mode"] != "ignore" else 200)
                self.send_header("Content-Length", str(len(content) - start))
                if value:
                    actual_start = start + 1 if state["mode"] == "wrong" else start
                    self.send_header(
                        "Content-Range", f"bytes {actual_start}-{len(content) - 1}/{len(content)}"
                    )
                self.end_headers()
                payload = content[start:]
                if state["mode"] == "interrupt":
                    payload = payload[: 1024 * 1024 + 100]
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as folder:
                target = Path(folder) / "model.gguf"
                url = f"http://127.0.0.1:{server.server_port}/model"
                with self.assertRaises(HuggingFaceCommandError):
                    download_file(url, target, len(content), digest, False, {})
                partial = next(Path(folder).rglob("*.partial"))
                before = partial.read_bytes()
                self.assertTrue(before)
                self.assertFalse(target.exists())
                for mode in ["ignore", "wrong"]:
                    state["mode"] = mode
                    with self.assertRaises(HuggingFaceCommandError):
                        download_file(url, target, len(content), digest, False, {})
                    self.assertEqual(partial.read_bytes(), before)
                state["mode"] = "complete"
                download_file(url, target, len(content), digest, False, {})
                self.assertEqual(state["requests"][-1], f"bytes={len(before)}-")
                self.assertEqual(target.read_bytes(), content)
                count = len(state["requests"])
                download_file(url, target, len(content), digest, False, {})
                self.assertEqual(len(state["requests"]), count)
                target.write_bytes(b"b" * len(content))
                download_file(url, target, len(content), digest, False, {})
                self.assertEqual(target.read_bytes(), content)
                target.unlink()
                with self.assertRaises(IntegrityError):
                    download_file(url, target, len(content), "0" * 64, False, {})
                self.assertFalse(target.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_redirect_strips_token_and_git_blob_digest_is_verified(self):
        content = b"model config"
        digest = hashlib.sha1(b"blob 12\0" + content, usedforsecurity=False).hexdigest()
        tokens = []

        class Destination(BaseHTTPRequestHandler):
            @override
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                tokens.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

        class Redirect(Destination):
            @override
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/file")
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [
            threading.Thread(target=s.serve_forever, daemon=True) for s in [destination, redirect]
        ]
        for thread in threads:
            thread.start()
        try:
            with TemporaryDirectory() as folder:
                target = Path(folder) / "config.json"
                download_file(
                    f"http://127.0.0.1:{redirect.server_port}/file",
                    target,
                    len(content),
                    digest,
                    True,
                    {"HF_TOKEN": "synthetic-token"},
                )
                self.assertEqual(tokens, [None])
                self.assertEqual(target.read_bytes(), content)
        finally:
            for server in [destination, redirect]:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()
