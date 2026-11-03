"""Small Prometheus snapshot; the HTTP thread never accesses SQLite."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import State


class Health:
    def __init__(self, scopes: tuple[str, ...]):
        self.lock = threading.Lock()
        self.values: dict[str, float] = {
            "last_delivery_success_timestamp_seconds": 0,
            "pending_events": 0,
            "policy_valid": 1,
            "policy_rejections_total": 0,
        }
        self.oldest_pending = 0.0
        for scope in scopes:
            self.values[f'last_poll_success_timestamp_seconds{{scope="{scope}"}}'] = 0
            self.values[f'cursor_timestamp_seconds{{scope="{scope}"}}'] = 0

    def poll_success(self, scope: str, cursor: float) -> None:
        with self.lock:
            self.values[f'last_poll_success_timestamp_seconds{{scope="{scope}"}}'] = time.time()
            self.values[f'cursor_timestamp_seconds{{scope="{scope}"}}'] = cursor

    def delivery_success(self) -> None:
        with self.lock:
            self.values["last_delivery_success_timestamp_seconds"] = time.time()

    def update(self, state: State) -> None:
        count, oldest = state.db.execute(
            "SELECT COUNT(*), MIN(queued_at) FROM records WHERE delivered=0"
        ).fetchone()
        with self.lock:
            self.values["pending_events"] = count
            self.oldest_pending = oldest or 0
            self.values["policy_valid"] = int(state.policy_valid)
            self.values["policy_rejections_total"] = state.policy_rejections

    def render(self) -> bytes:
        with self.lock:
            values = {
                **self.values,
                "oldest_pending_age_seconds": (
                    max(0, time.time() - self.oldest_pending) if self.oldest_pending else 0
                ),
            }
        return "".join(
            f"reporting_adapter_{name} {value}\n" for name, value in sorted(values.items())
        ).encode()

    @contextmanager
    def serve(self, port: int):
        if not port:
            yield
            return
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = health.render()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
