"""Opt-in streaming probes; never used by the standard endpoint checks."""

from __future__ import annotations

import codecs
import json
import os
import selectors
import shlex
import subprocess
import tempfile
import time
from collections.abc import Generator, Iterable, Iterator
from typing import Any

from inference_runtime_manager.configuration import management_state
from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.installer.assignments import assigned_recipe
from inference_runtime_manager.installer.docker import Docker

LIMIT = 16 * 1024 * 1024


def chat_tokens(chunks: Iterable[bytes]) -> Iterator[str]:
    """Parse fragmented UTF-8 SSE and require a complete text-only completion."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data: list[str] = []
    finished = done = has_text = False

    def event(lines: list[str]) -> list[str]:
        nonlocal finished, done, has_text
        if not lines:
            return []
        if done:
            raise ValueError("SSE data after [DONE]")
        text = "\n".join(lines)
        if text == "[DONE]":
            done = True
            return []
        value = json.loads(text)
        if not isinstance(value, dict) or "error" in value:
            raise ValueError("SSE returned an error or invalid object")
        choices = value.get("choices")
        if choices == [] and isinstance(value.get("usage"), dict):
            return []
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("SSE must contain one choice")
        choice = choices[0]
        if finished or choice.get("index", 0) != 0:
            raise ValueError("Unexpected SSE choice")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("function_call"):
            raise ValueError("SSE is not a text completion")
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("Invalid SSE text")
        reason = choice.get("finish_reason")
        if reason is not None:
            if reason != "stop":
                raise ValueError(f"Incomplete SSE completion ({reason!r})")
            finished = True
        if content:
            has_text = has_text or bool(content.strip())
            return [content]
        return []

    for chunk in chunks:
        pending += decoder.decode(chunk)
        if len(pending) > LIMIT:
            raise ValueError("SSE event exceeds limit")
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.removesuffix("\r")
            if not line:
                yield from event(data)
                data = []
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
    pending += decoder.decode(b"", final=True)
    if pending or data or not done or not finished or not has_text:
        raise ValueError("Incomplete SSE stream")


def request_stream(
    docker: Docker, alias: str, path: str, payload: dict[str, Any]
) -> Generator[bytes, None, None]:
    """Forward bytes without subprocess buffering; closing also stops the remote reader."""
    preset = assigned_recipe(
        management_state(Settings().storage_root), docker.settings.docker_context, alias
    )
    if preset is None or not preset["enabled"]:
        raise ValueError(f"{alias} is not enabled")
    url = f"http://127.0.0.1:{preset['internal_port']}{path}"
    environment = {
        **docker.environment,
        "INFERENCE_API_KEY": docker.settings.api_key_for(alias) or "",
    }
    command = docker.command + ["exec", "-T", "-e", "INFERENCE_API_KEY", preset["service"]]
    if preset["transport"] == "curl":
        # Keep stdin open as a cancellation channel. The watcher kills curl on
        # a cancel byte or EOF, including when the workstation disappears.
        script = (
            'exec 3<&0; set --; if [ -n "$INFERENCE_API_KEY" ]; then '
            'set -- -H "Authorization: Bearer $INFERENCE_API_KEY"; fi; '
            "curl -fsSN --max-time 300 --max-filesize 16777216 "
            '"$@" '
            '-H "Content-Type: application/json" --data-binary '
            + shlex.quote(json.dumps(payload))
            + " "
            + shlex.quote(url)
            + ' & reader=$!; (read -r cancel <&3; kill "$reader" 2>/dev/null) & watcher=$!; '
            'wait "$reader"; result=$?; kill "$watcher" 2>/dev/null; '
            'wait "$watcher" 2>/dev/null; exit "$result"'
        )
        command += ["sh", "-c", script]
        header = b""
    else:
        script = r"""
import json, os, sys, threading, urllib.request, urllib.error
job = json.loads(sys.stdin.buffer.readline())
def cancel():
    sys.stdin.buffer.read(1)
    os._exit(0)
threading.Thread(target=cancel, daemon=True).start()
headers = {"Content-Type": "application/json"}
key = os.environ.get("INFERENCE_API_KEY")
if key:
    headers["Authorization"] = "Bearer " + key
request = urllib.request.Request(job["url"], data=json.dumps(job["payload"]).encode(), headers=headers)
try:
    with urllib.request.urlopen(request, timeout=300) as response:
        if response.headers.get_content_type() != job["content_type"]:
            sys.exit("Unexpected streaming content type")
        while chunk := response.read1(4096):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
except urllib.error.HTTPError as error:
    sys.exit("Streaming HTTP status: " + str(error.code))
"""
        command += [preset["transport"], "-u", "-c", script]
        content_type = "audio/pcm" if alias == "tts-german" else "text/event-stream"
        header = (
            json.dumps({"url": url, "payload": payload, "content_type": content_type}) + "\n"
        ).encode()
    # stderr goes to a file rather than an unread pipe that could deadlock.
    with (
        tempfile.TemporaryFile() as errors,
        subprocess.Popen(
            command, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors
        ) as process,
    ):
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(header)
            process.stdin.flush()
            deadline = time.monotonic() + 330
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise ValueError("Streaming request timed out")
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > LIMIT:
                        raise ValueError("Streaming response exceeds 16 MiB")
                    yield chunk
            if process.wait(timeout=5):
                raise ValueError("Streaming request failed (HTTP or Docker transport error)")
        finally:
            try:
                process.stdin.write(b"\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
