"""Bounded HTTP adapters. The caller supplies one absolute asyncio deadline."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from docling_tester.corpus import GATEWAY_IMAGES, Case

MARKER = "docling-tester:"
ID = re.compile(r"^[a-f0-9]{32}$")
TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
ACTIVE = {"pending", "started"}
TERMINAL = {"success", "failure", "partial_success", "skipped", "cancelled"}
MIMES = {
    "md": "text/markdown",
    "csv": "text/csv",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class ProtocolError(Exception):
    """Fixed, content-free diagnostic safe to put in a report."""


@dataclass
class Reply:
    status: int
    body: Any
    seconds: float


def endpoint(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("Endpoint must be an HTTP(S) URL without credentials, query or fragment")
    return value.rstrip("/")


def mime(case: Case) -> str:
    return (
        MIMES.get(case.format) or mimetypes.guess_type(case.path)[0] or "application/octet-stream"
    )


def strict_json(data: bytes | bytearray | str) -> Any:
    def fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError("Non-finite JSON value")

    return json.loads(data, object_pairs_hook=fields, parse_constant=invalid_constant)


def task_status(value: Any, expected: str | None = None) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_task_envelope")
    task, status = value.get("task_id"), value.get("task_status")
    if (
        not isinstance(task, str)
        or not TASK_ID.fullmatch(task)
        or (expected is not None and task != expected)
        or value.get("task_type") != "convert"
        or not isinstance(status, str)
        or status not in ACTIVE | TERMINAL
    ):
        raise ProtocolError("invalid_task_identity_or_status")
    return task, status


def reason_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    # Only explicit machine fields count. Never accept matching prose/error messages.
    objects = [body, body.get("error"), body.get("detail")]
    for obj in objects:
        if isinstance(obj, dict):
            for key in ("reason", "code"):
                value = obj.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                    return value
    return None


class Transport:
    def __init__(self, client: httpx.AsyncClient, max_bytes: int = 64 * 1024 * 1024):
        self.client = client
        self.max_bytes = max_bytes

    async def request(self, method: str, url: str, **kwargs: Any) -> Reply:
        start = time.perf_counter()
        kwargs["headers"] = {"Accept-Encoding": "identity", **kwargs.get("headers", {})}
        async with self.client.stream(method, url, **kwargs) as response:
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ProtocolError("response_encoding_rejected")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > self.max_bytes:
                    raise ProtocolError("response_size_limit")
                raw.extend(chunk)
            try:
                value = strict_json(raw)
            except (ValueError, UnicodeDecodeError):
                value = {"invalid_json_response": raw.decode("utf-8", errors="replace")}
            return Reply(response.status_code, value, time.perf_counter() - start)


class Direct:
    def __init__(
        self,
        transport: Transport,
        url: str,
        key: str,
        options: dict[str, str],
        poll_interval: float = 0.2,
    ):
        self.transport, self.url = transport, endpoint(url)
        self.headers = {"X-Api-Key": key} if key else {}
        self.options, self.poll_interval = options, poll_interval

    async def convert(self, case: Case, data: bytes, trace: dict[str, Any]) -> Reply:
        trace.update(submission="started", remote_job_may_continue=True)
        started = time.perf_counter()
        options: dict[str, Any] = dict(self.options)
        if case.format in {"asciidoc", "md", "eml", "msg", "mhtml"}:
            options["to_formats"] = ["json", "md"]
        if case.format in {"asciidoc", "md"}:
            options["md_compact_tables"] = "true"
        source_format = {
            "png": "image",
            "jpg": "image",
            "jpeg": "image",
            "tif": "image",
            "webp": "image",
            "txt": "md",
            "htm": "html",
        }.get(case.format, case.format)
        if source_format in {
            "pdf",
            "image",
            "docx",
            "xlsx",
            "pptx",
            "md",
            "csv",
            "html",
            "asciidoc",
            "vtt",
        }:
            options["from_formats"] = source_format
        reply = await self.transport.request(
            "POST",
            self.url + "/v1/convert/file/async",
            headers=self.headers,
            data=options,
            files={"files": (case.path.rsplit("/", 1)[-1], data, mime(case))},
        )
        trace["submit_seconds"] = reply.seconds
        trace["last_response"] = reply.body
        trace.update(http_status=reply.status, phase="submit")
        if reply.status >= 300:
            trace["remote_job_may_continue"] = False
            return reply
        task, status = task_status(reply.body)
        trace.update(task_id=task, task_status=status)
        while status in ACTIVE:
            await asyncio.sleep(self.poll_interval)
            reply = await self.transport.request(
                "GET", self.url + f"/v1/status/poll/{task}?wait=1", headers=self.headers
            )
            trace["last_response"] = reply.body
            trace.update(http_status=reply.status, phase="poll")
            if reply.status != 200:
                raise ProtocolError("poll_http_error")
            _, status = task_status(reply.body, task)
            trace["task_status"] = status
        trace["conversion_wait_seconds"] = time.perf_counter() - started - trace["submit_seconds"]
        trace["remote_job_may_continue"] = False
        if status != "success":
            raise ProtocolError("conversion_not_success")
        reply = await self.transport.request(
            "GET", self.url + f"/v1/result/{task}", headers=self.headers
        )
        trace["result_download_seconds"] = reply.seconds
        trace["last_response"] = reply.body
        trace.update(http_status=reply.status, phase="result")
        if reply.status != 200:
            raise ProtocolError("result_http_error")
        value = reply.body
        trace["last_response"] = value
        if (
            not isinstance(value, dict)
            or value.get("status") != "success"
            or value.get("errors") != []
            or not isinstance(value.get("document"), dict)
            or not isinstance(value["document"].get("json_content"), dict)
        ):
            raise ProtocolError("partial_or_invalid_conversion_result")
        return reply


def gateway_payload(case: Case, data: bytes, model: str, correlation: str) -> dict[str, Any]:
    encoded = f"data:{mime(case)};base64," + base64.b64encode(data).decode("ascii")
    if case.format in GATEWAY_IMAGES:
        part = {"type": "image_url", "image_url": {"url": encoded}}
    else:
        part = {
            "type": "file",
            "file": {
                "filename": case.path.rsplit("/", 1)[-1],
                "file_data": encoded,
            },
        }
    return {
        "model": model,
        "stream": False,
        "max_tokens": 32,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"{MARKER}{correlation}\nInspect the attached document.",
                    },
                    part,
                ],
            }
        ],
    }


def captured_text(value: Any, correlation: str) -> str:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("correlation") != correlation
        or value.get("complete") is not True
        or not isinstance(value.get("messages"), list)
    ):
        raise ProtocolError("capture_missing_or_incomplete")
    messages = value["messages"]
    checksum = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    if checksum != value.get("messages_sha256"):
        raise ProtocolError("capture_checksum_mismatch")
    texts = []
    marker_seen = False
    for message in messages:
        if not isinstance(message, dict):
            raise ProtocolError("capture_invalid_message")
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise ProtocolError("capture_invalid_content")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text":
                raise ProtocolError("capture_contains_unextracted_attachment")
            text = part.get("text")
            if not isinstance(text, str):
                raise ProtocolError("capture_invalid_text")
            if f"{MARKER}{correlation}" in text:
                marker_seen = True
                # Remove only our exact prompt, retaining coalesced attachment text.
                text = text.replace(f"{MARKER}{correlation}\nInspect the attached document.", "", 1)
            if text.strip():
                texts.append(text)
    if not marker_seen:
        raise ProtocolError("capture_wrong_request")
    return "\n".join(texts)
