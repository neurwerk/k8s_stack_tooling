"""Bounded OpenAI-compatible OCR prompt adapter for local inference backends."""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, override

LISTEN_ADDRESS = ("0.0.0.0", 8000)
BACKEND_URL = "http://127.0.0.1:8001"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_PROMPT_CHARS = 4096
BACKEND_TIMEOUT_SECONDS = 80
CLIENT_TIMEOUT_SECONDS = 15
MODEL_ALIAS = "vlm-images"
INFERENCE_SLOT = threading.BoundedSemaphore(1)

NANONETS_PROMPT = (
    "Extract the text from the above document as if you were reading it naturally. "
    "Return the tables in html format. Return the equations in LaTeX representation. "
    "If there is an image in the document and image caption is not present, add a small "
    "description of the image inside the <img></img> tag; otherwise, add the image caption "
    "inside <img></img>. Watermarks should be wrapped in brackets. Ex: "
    "<watermark>OFFICIAL COPY</watermark>. Page numbers should be wrapped in brackets. Ex: "
    "<page_number>14</page_number> or <page_number>9/22</page_number>. Prefer using ☐ and ☑ "
    "for check boxes."
)

OLMOCR_PROMPT = (
    "Attached is one page of a document that you must process. Just return the plain text "
    "representation of this document as if you were reading it naturally. Convert equations "
    "to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure]"
    "(page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for "
    "the primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram "
    "parameters."
)

DATA_URL = re.compile(r"\Adata:image/(?:png|jpeg);base64,([A-Za-z0-9+/]*(?:={0,2}))\Z")
FRONT_MATTER_KEYS = {
    "primary_language",
    "is_rotation_valid",
    "rotation_correction",
    "is_table",
    "is_diagram",
}


class OCRServer(ThreadingHTTPServer):
    """Bound queued connections and never let request threads block shutdown."""

    daemon_threads = True
    request_queue_size = 8
    request_slots = threading.BoundedSemaphore(8)

    @override
    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    @override
    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class RequestError(Exception):
    """A caller supplied an invalid request."""


class BackendError(Exception):
    """The private backend failed or returned an invalid response."""


def _image_request(payload: object, profile: str) -> tuple[str, str | None]:
    if not isinstance(payload, dict):
        raise RequestError("Request body must be a JSON object")
    if payload.get("model") != MODEL_ALIAS:
        raise RequestError("Unsupported model")
    if payload.get("stream", False) is not False:
        raise RequestError("Streaming is not supported")

    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise RequestError("Exactly one user message is required")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise RequestError("Exactly one user message is required")
    content = message.get("content")
    if not isinstance(content, list) or len(content) not in {1, 2}:
        raise RequestError("The user message must contain one image and at most one prompt")
    images = [
        part for part in content if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    if len(images) != 1:
        raise RequestError("The user message must contain exactly one image")
    image_part = images[0]
    prompt = None
    if len(content) == 2:
        text_parts = [
            part for part in content if isinstance(part, dict) and part.get("type") == "text"
        ]
        if profile != "olmocr" or len(text_parts) != 1:
            raise RequestError("A custom prompt is supported only for olmOCR")
        value: object = text_parts[0].get("text")
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_PROMPT_CHARS:
            raise RequestError(f"Prompt must contain 1–{MAX_PROMPT_CHARS} characters")
        prompt = value.strip()
    image = image_part.get("image_url")
    if not isinstance(image, dict):
        raise RequestError("A base64 image data URL is required")
    url = image.get("url")
    if not isinstance(url, str):
        raise RequestError("A base64 image data URL is required")
    match = DATA_URL.fullmatch(url)
    if match is None or not match.group(1):
        raise RequestError("A base64 image data URL is required")
    try:
        base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestError("A valid base64 image data URL is required") from exc
    return url, prompt


def rewrite_request(payload: object, profile: str) -> tuple[dict[str, Any], bool]:
    """Validate a public request and return the backend request and prompt-override flag."""
    image_url, prompt = _image_request(payload, profile)
    assert isinstance(payload, dict)
    requested_max = payload.get("max_tokens", 2048)
    if isinstance(requested_max, bool) or not isinstance(requested_max, int) or requested_max < 1:
        raise RequestError("max_tokens must be a positive integer")

    image = {"type": "image_url", "image_url": {"url": image_url}}
    if profile == "nanonets":
        content = [image, {"type": "text", "text": NANONETS_PROMPT}]
        temperature = 0.0
    elif profile == "olmocr":
        content = [{"type": "text", "text": prompt or OLMOCR_PROMPT}, image]
        temperature = 0.1
    else:
        raise ValueError("Unknown OCR profile")

    return {
        "model": MODEL_ALIAS,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": min(requested_max, 2048),
        "temperature": temperature,
        "n": 1,
        "stream": False,
    }, prompt is not None


def _yaml_scalar(value: str, key: str) -> object:
    value = value.strip()
    if key in {"is_rotation_valid", "is_table", "is_diagram"}:
        if value.lower() not in {"true", "false"}:
            raise BackendError("Backend returned malformed OCR output")
        return value.lower() == "true"
    if key == "rotation_correction":
        if value not in {"0", "90", "180", "270"}:
            raise BackendError("Backend returned malformed OCR output")
        return int(value)
    if value.lower() in {"null", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if not value or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
        raise BackendError("Backend returned malformed OCR output")
    return value


def strip_olmocr_front_matter(content: str) -> str:
    """Remove the expected simple YAML metadata without accepting arbitrary YAML."""
    normalized = content.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise BackendError("Backend returned malformed OCR output")
    closing = normalized.find("\n---\n", 4)
    if closing < 0:
        raise BackendError("Backend returned malformed OCR output")

    metadata: dict[str, object] = {}
    for line in normalized[4:closing].splitlines():
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in FRONT_MATTER_KEYS or key in metadata:
            raise BackendError("Backend returned malformed OCR output")
        metadata[key] = _yaml_scalar(value, key)
    if metadata.keys() != FRONT_MATTER_KEYS:
        raise BackendError("Backend returned malformed OCR output")

    markdown = normalized[closing + 5 :].strip()
    if not markdown:
        raise BackendError("Backend returned empty OCR output")
    return markdown


def rewrite_response(raw: bytes, profile: str, *, custom_prompt: bool = False) -> bytes:
    """Validate one backend completion and replace its internal model identity."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackendError("Backend returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise BackendError("Backend returned malformed JSON")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise BackendError("Backend returned malformed completion")
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("finish_reason"), str):
        raise BackendError("Backend returned malformed completion")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise BackendError("Backend returned malformed completion")
    content = message["content"]
    if choice["finish_reason"] == "stop":
        if profile == "olmocr" and not custom_prompt:
            content = strip_olmocr_front_matter(content)
        elif not content.strip():
            raise BackendError("Backend returned empty OCR output")
    message["content"] = content
    payload["model"] = MODEL_ALIAS
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


def _read_backend_response(response: Any) -> bytes:
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            if int(length) > MAX_RESPONSE_BYTES:
                raise BackendError("Backend response exceeded the size limit")
        except ValueError as exc:
            raise BackendError("Backend returned invalid headers") from exc
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes):
        raise BackendError("Backend returned an invalid response body")
    if len(body) > MAX_RESPONSE_BYTES:
        raise BackendError("Backend response exceeded the size limit")
    return body


def forward_to_backend(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        BACKEND_URL + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=BACKEND_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise BackendError("Backend request failed")
            return _read_backend_response(response)
    except BackendError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BackendError("Backend request failed") from exc


def backend_is_healthy() -> bool:
    try:
        with urllib.request.urlopen(BACKEND_URL + "/health", timeout=5) as response:
            return bool(response.status == 200)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return False


def make_handler(profile: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ocr-proxy"
        sys_version = ""

        @override
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(CLIENT_TIMEOUT_SECONDS)

        @override
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(
            self, status: int, payload: object, extra_headers: dict[str, str] | None = None
        ) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str, code: str) -> None:
            self._json(
                status,
                {"error": {"message": message, "type": "invalid_request_error", "code": code}},
            )

        def do_GET(self) -> None:
            if self.path != "/health":
                self._error(404, "Not found", "not_found")
            elif backend_is_healthy():
                self._json(200, {"status": "ready"})
            else:
                self._error(503, "Backend is not ready", "backend_unavailable")

        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self._error(404, "Not found", "not_found")
                return
            key = os.environ.get("VLM_IMAGES_API_KEY", "")
            supplied = self.headers.get("Authorization", "")
            if key and not hmac.compare_digest(supplied, "Bearer " + key):
                self._error(401, "Unauthorized", "unauthorized")
                return

            length_header = self.headers.get("Content-Length")
            try:
                length = int(length_header) if length_header is not None else -1
            except ValueError:
                length = -1
            if length < 0 or length > MAX_REQUEST_BYTES:
                self._error(413, "Request body exceeded the size limit", "request_too_large")
                return
            try:
                body = self.rfile.read(length)
            except (TimeoutError, OSError):
                self.close_connection = True
                return
            try:
                public_payload = json.loads(body)
                backend_payload, custom_prompt = rewrite_request(public_payload, profile)
            except (UnicodeDecodeError, json.JSONDecodeError, RequestError) as exc:
                message = (
                    str(exc) if isinstance(exc, RequestError) else "Request body must be valid JSON"
                )
                self._error(400, message, "invalid_request")
                return

            if not INFERENCE_SLOT.acquire(blocking=False):
                self._error(429, "OCR backend is busy", "backend_busy")
                return
            try:
                try:
                    response = rewrite_response(
                        forward_to_backend(backend_payload), profile, custom_prompt=custom_prompt
                    )
                except BackendError:
                    self._error(502, "OCR backend returned an invalid response", "backend_error")
                    return
            finally:
                INFERENCE_SLOT.release()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    return Handler


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run(profile: str, command: list[str]) -> int:
    child = subprocess.Popen(command)
    stopping = threading.Event()

    def stop(signum: int, _frame: object) -> None:
        stopping.set()
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        with OCRServer(LISTEN_ADDRESS, make_handler(profile)) as server:
            server.timeout = 0.5
            while not stopping.is_set() and child.poll() is None:
                server.handle_request()
    finally:
        _stop_child(child)
    return_code = child.returncode
    return return_code if isinstance(return_code, int) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=("nanonets", "olmocr"))
    parser.add_argument("backend_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.backend_command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a backend command is required after --")
    return run(args.profile, command)


if __name__ == "__main__":
    sys.exit(main())
