"""Opt-in in-memory OpenAI test backend with separate capture-reader authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

from docling_tester.transport import ID, MARKER, strict_json


class Store:
    def __init__(self, max_bytes: int = 128 * 1024 * 1024, ttl: float = 600):
        self.max_bytes, self.ttl = max_bytes, ttl
        self.items: dict[str, tuple[float, bytes]] = {}
        self.lock = threading.Lock()

    def put(self, correlation: str, messages: list[Any]) -> bool:
        value = {
            "schema_version": 1,
            "correlation": correlation,
            "complete": True,
            "messages": messages,
            "messages_sha256": hashlib.sha256(
                json.dumps(messages, sort_keys=True).encode()
            ).hexdigest(),
        }
        data = json.dumps(value).encode()
        with self.lock:
            self._expire()
            if (
                correlation in self.items
                or len(self.items) >= 64
                or sum(len(item[1]) for item in self.items.values()) + len(data) > self.max_bytes
            ):
                return False
            self.items[correlation] = (time.monotonic(), data)
        return True

    def _expire(self) -> None:
        now = time.monotonic()
        self.items = {key: item for key, item in self.items.items() if now - item[0] < self.ttl}

    def take(self, correlation: str) -> bytes | None:
        with self.lock:
            self._expire()
            item = self.items.pop(correlation, None)
            return item[1] if item else None


def correlation_id(messages: Any) -> str | None:
    if not isinstance(messages, list):
        return None
    ids: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    ids.extend(re.findall(re.escape(MARKER) + r"([a-f0-9]{32})\b", part["text"]))
    return ids[0] if len(ids) == 1 else None


def server(
    host: str,
    port: int,
    backend_key: str,
    read_key: str,
    store: Store | None = None,
) -> ThreadingHTTPServer:
    if not backend_key or not read_key or backend_key == read_key:
        raise ValueError("Capture backend and reader need distinct nonempty credentials")
    captures = store or Store()

    class Handler(BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: Any) -> None:
            pass  # No headers, bodies, paths or request identifiers in server logs.

        def authorized(self, key: str) -> bool:
            return hmac.compare_digest(
                self.headers.get("Authorization", "").encode(), f"Bearer {key}".encode()
            )

        def send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not self.authorized(read_key):
                self.send(401, b'{"error":{"code":"unauthorized"}}')
                return
            identifier = self.path.removeprefix("/captures/")
            if not self.path.startswith("/captures/") or not ID.fullmatch(identifier):
                self.send(404, b"{}")
                return
            data = captures.take(identifier)
            self.send(200 if data is not None else 404, data if data is not None else b"{}")

        def do_POST(self) -> None:
            if not self.authorized(backend_key):
                self.send(401, b'{"error":{"code":"unauthorized"}}')
                return
            if self.path not in {"/chat/completions", "/v1/chat/completions"}:
                self.send(404, b"{}")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 64 * 1024 * 1024
                    or self.headers.get("Transfer-Encoding") is not None
                ):
                    self.send(413, b"{}")
                    return
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                body = strict_json(raw)
                messages = body.get("messages") if isinstance(body, dict) else None
                identifier = correlation_id(messages)
                if identifier is None or not isinstance(messages, list):
                    raise ValueError
            except (ValueError, OSError):
                self.send(400, b'{"error":{"code":"invalid_capture_request"}}')
                return
            if not captures.put(identifier, messages):
                self.send(503, b'{"error":{"code":"capture_full_or_duplicate"}}')
                return
            self.send(
                200,
                json.dumps(
                    {
                        "id": "chatcmpl-" + identifier,
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "docling-tester-capture",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "Captured."},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    }
                ).encode(),
            )

    return ThreadingHTTPServer((host, port), Handler)
