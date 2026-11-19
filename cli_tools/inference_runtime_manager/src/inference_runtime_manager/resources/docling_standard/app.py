"""Authenticated, bounded inference service for the standard Docling model bundle."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import protocol

LOG = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, models, concurrency: int):
        if concurrency not in (1, 2, 3):
            raise ValueError("Concurrency must be 1–3")
        self.models = models
        self.concurrency = concurrency
        self.capacity = threading.BoundedSemaphore(concurrency)
        self.locks = {name: threading.Lock() for name in protocol.SCHEMAS}

    def infer(self, name, inputs):
        # Acquire the model lock first: queued calls cannot monopolize GPU permits.
        with self.locks[name], self.capacity:
            return self.models.infer(name, inputs)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, address, scheduler: Scheduler, key: str):
        if not key:
            raise ValueError("DOCLING_STANDARD_API_KEY is required")
        self.scheduler = scheduler
        self.key = key
        self.connections = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        LOG.error("Request handling failed")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, format, *args):
        pass

    def reply(self, code, value):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        if not hmac.compare_digest(
            self.headers.get("Authorization", "").encode(),
            ("Bearer " + self.server.key).encode(),
        ):
            self.reply(401, {"error": "Unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ready": True, "device": "cuda"})
            return
        if not self.authorized():
            return
        if self.path == "/info":
            self.reply(
                200,
                {
                    **self.server.scheduler.models.info,
                    "concurrency": self.server.scheduler.concurrency,
                    "layout_max_batch_size": 4,
                },
            )
            return
        for name in protocol.SCHEMAS:
            if self.path == f"/v2/models/{name}":
                self.reply(200, protocol.metadata(name))
                return
        self.reply(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if not self.authorized():
            return
        name = next(
            (name for name in protocol.SCHEMAS if self.path == f"/v2/models/{name}/infer"), None
        )
        if name is None:
            self.reply(404, {"error": "Unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            header_length = int(self.headers.get("Inference-Header-Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= protocol.MAX_BODY:
                raise ValueError("Invalid body size")
            if not 0 < header_length <= min(length, protocol.MAX_HEADER):
                raise ValueError("Invalid header size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete body")
            inputs = protocol.decode(body, header_length, name)
        except (ValueError, TypeError, KeyError, AttributeError, TimeoutError):
            self.reply(400, {"error": "Invalid inference request"})
            return
        try:
            values = self.server.scheduler.infer(name, inputs)
            if name == "layout" and len(values["labels"]) != len(inputs["images"]):
                raise ValueError("Incomplete layout output")
            body, header_length = protocol.encode(name, values)
        except Exception:
            # Never include model exceptions: they may contain request content.
            LOG.error("Inference failed for model %s", name)
            self.reply(500, {"error": "Model inference failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Inference-Header-Content-Length", str(header_length))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    key = os.environ.get("DOCLING_STANDARD_API_KEY", "")
    if not key:
        raise ValueError("DOCLING_STANDARD_API_KEY is required")
    from models import Models

    models = Models(Path("/models/active/docling-standard"))
    server = Server(("0.0.0.0", 8011), Scheduler(models, args.concurrency), key)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
