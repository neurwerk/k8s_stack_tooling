"""Fragmented SSE and incomplete-stream failures, without live inference."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from inference_runtime_manager.installer import streaming
from inference_runtime_manager.installer.streaming import chat_tokens


def event(content=None, reason=None):
    return (
        "data: "
        + json.dumps(
            {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": reason}]},
            ensure_ascii=False,
        )
        + "\r\n\r\n"
    ).encode()


class StreamingTests(unittest.TestCase):
    def test_curl_transport_cancel_channel_stops_remote_reader(self):
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "cancelled"
            curl = Path(folder) / "curl"
            curl.write_text(
                f"#!{sys.executable}\n"
                "import signal, sys, time\n"
                "from pathlib import Path\n"
                "def stop(*args):\n"
                f"    Path({str(marker)!r}).touch()\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "sys.stdout.buffer.write(b'first'); sys.stdout.buffer.flush()\n"
                "time.sleep(5)\n"
            )
            curl.chmod(0o700)

            def local_helper(command, **kwargs):
                return popen(["/bin/sh", "-c", command[-1]], **kwargs)

            docker = Mock(command=["unused"], environment={**os.environ, "PATH": folder})
            docker.settings.api_key_for.return_value = ""
            preset = {"enabled": True, "service": "llm", "transport": "curl", "internal_port": 8000}
            with (
                patch.object(
                    streaming, "Settings", return_value=Mock(storage_root=Path("/unused"))
                ),
                patch.object(streaming, "assigned_recipe", return_value=preset),
                patch.object(streaming.subprocess, "Popen", side_effect=local_helper),
            ):
                chunks = streaming.request_stream(docker, "llm-general", "/v1/chat/completions", {})
                try:
                    self.assertEqual(next(chunks), b"first")
                finally:
                    chunks.close()
            self.assertTrue(marker.exists(), "remote curl must receive termination on cancellation")

    def test_python_transport_forwards_first_bytes_and_cancels_remote_reader(self):
        # Run the real remote helper locally with a fake HTTP response. A
        # buffering regression would wait for its second read and time out.
        stub = """
import time, urllib.request
from email.message import Message
class FakeResponse:
    first = True
    headers = Message()
    headers["Content-Type"] = "audio/pcm"
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read1(self, size):
        if self.first:
            self.first = False
            return b"first"
        time.sleep(2)
        raise RuntimeError("response was buffered instead of streamed")
urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
"""
        popen = subprocess.Popen
        processes = []

        def local_helper(command, **kwargs):
            process = popen([sys.executable, "-u", "-c", stub + command[-1]], **kwargs)
            processes.append(process)
            return process

        docker = Mock(command=["unused"], environment=os.environ.copy())
        docker.settings.api_key_for.return_value = ""
        preset = {"enabled": True, "service": "tts", "transport": "python3", "internal_port": 8000}
        with (
            patch.object(streaming, "Settings", return_value=Mock(storage_root=Path("/unused"))),
            patch.object(streaming, "assigned_recipe", return_value=preset),
            patch.object(streaming.subprocess, "Popen", side_effect=local_helper),
        ):
            chunks = streaming.request_stream(docker, "tts-german", "/v1/audio/speech", {})
            try:
                self.assertEqual(next(chunks), b"first")
            finally:
                chunks.close()
        self.assertEqual(processes[0].returncode, 0)

    def test_fragmented_utf8_comments_usage_and_done(self):
        data = (
            b": heartbeat\r\n\r\n"
            + event("Grüße")
            + event(reason="stop")
            + b'data: {"choices":[],"usage":{"total_tokens":2}}\r\n\r\n'
            + b"data: [DONE]\r\n\r\n"
        )
        self.assertEqual(list(chat_tokens(bytes([byte]) for byte in data)), ["Grüße"])

    def test_first_text_is_not_buffered_until_completion(self):
        def chunks():
            yield event("Hello")
            self.assertTrue(consumed)
            yield event(reason="stop") + b"data: [DONE]\n\n"

        consumed = False
        tokens = chat_tokens(chunks())
        self.assertEqual(next(tokens), "Hello")
        consumed = True
        self.assertEqual(list(tokens), [])

    def test_errors_and_incomplete_completions_fail(self):
        for data in [
            event("partial"),
            event("partial") + b"data: [DONE]\n\n",
            event("partial") + event(reason="length") + b"data: [DONE]\n\n",
            b'data: {"error":{"message":"failure"}}\n\n',
            b"data: not-json\n\n",
            event("text") + event(reason="stop") + b"data: [DONE]",
            b"data: \xff\n\n",
        ]:
            with self.subTest(data=data), self.assertRaises((ValueError, UnicodeError)):
                list(chat_tokens([data]))
