from __future__ import annotations

import asyncio
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from conftest import document

from docling_tester.corpus import Case
from docling_tester.runner import Settings
from docling_tester.transport import Direct, ProtocolError, Transport


def task(status="success", identifier="test-job"):
    return {"task_id": identifier, "task_type": "convert", "task_status": status}


def converted():
    return {"status": "success", "errors": [], "document": {"json_content": document()}}


@pytest.mark.parametrize("extension", ["xlsx", "docx", "pptx"])
def test_binary_upload_pending_poll_and_full_result(http_server, extension, tmp_path):
    calls = []
    payload = b"PK\x03\x04\x00binary\xff\r\nfile"

    def callback(method, path, raw, headers):
        calls.append((method, path))
        assert headers["X-Api-Key"] == "test-credential"
        if method == "POST":
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + raw
            )
            uploaded = next(
                part for part in message.iter_parts() if part.get_filename() is not None
            )
            assert uploaded.get_filename() == f"source.{extension}"
            assert uploaded.get_payload(decode=True) == payload
            fields = {}
            for part in message.iter_parts():
                if part.get_filename() is None:
                    name = part.get_param("name", header="content-disposition")
                    content = part.get_payload(decode=True)
                    assert isinstance(name, str) and isinstance(content, bytes)
                    fields[name] = content.decode()
            # Docling 2.142.1 service/options.py, used by Serve 1.38.2.
            assert fields["from_formats"] == extension
            assert fields["pipeline"] == "standard"
            assert fields["document_timeout"] == "300"
            assert fields["do_ocr"] == fields["do_table_structure"] == "true"
            assert all(
                fields[name] == "default"
                for name in ("layout_preset", "ocr_preset", "table_structure_preset")
            )
            assert fields["to_formats"] == "json"
            assert fields["target_type"] == "inbody"
            assert not any("vlm" in name for name in fields)
            return 200, task("pending")
        if "/poll/" in path:
            return 200, task("started" if len(calls) == 2 else "success")
        return 200, converted()

    url = http_server(callback)

    async def execute():
        async with httpx.AsyncClient(trust_env=False) as client:
            adapter = Direct(
                Transport(client),
                url,
                "test-credential",
                Settings("direct", url, tmp_path).options(),
                poll_interval=0,
            )
            trace = {}
            async with asyncio.timeout(5):
                reply = await adapter.convert(
                    Case(f"source.{extension}", "office", extension, (), ()), payload, trace
                )
            assert reply.body == converted()
            assert trace["remote_job_may_continue"] is False
            assert {
                "submit_seconds",
                "conversion_wait_seconds",
                "result_download_seconds",
            } <= trace.keys()

    asyncio.run(execute())
    assert [method for method, _ in calls] == ["POST", "GET", "GET", "GET"]


@pytest.mark.parametrize(
    "damage",
    ["partial", "errors", "errors_type", "document", "task_type", "task_id", "status"],
)
def test_malformed_and_partial_results_never_pass(damage):
    async def callback(request):
        if request.method == "POST":
            value = task("pending")
            if damage == "task_type":
                value["task_type"] = "other"
            if damage == "status":
                value["task_status"] = "mystery"
        elif "/poll/" in request.url.path:
            value = task(identifier="another-job" if damage == "task_id" else "test-job")
        else:
            value = converted()
            if damage == "partial":
                value["status"] = "partial_success"
            elif damage == "errors":
                value["errors"] = [{"error": "bad page"}]
            elif damage == "errors_type":
                value["errors"] = None
            elif damage == "document":
                value["document"] = {"json_content": None}
        return httpx.Response(200, json=value)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            adapter = Direct(Transport(client), "http://example.invalid", "", {}, poll_interval=0)
            with pytest.raises(ProtocolError):
                await adapter.convert(Case("file.xlsx", "xlsx", "xlsx", (), ()), b"file", {})

    asyncio.run(execute())


def test_lost_post_is_never_retried():
    submissions = []

    async def callback(request):
        submissions.append(request)
        raise httpx.ReadError("credential-sentinel-must-not-be-reported")

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            trace = {}
            adapter = Direct(Transport(client), "http://example.invalid", "", {})
            with pytest.raises(httpx.ReadError):
                await adapter.convert(Case("file.xlsx", "xlsx", "xlsx", (), ()), b"file", trace)
            assert trace["remote_job_may_continue"] is True

    asyncio.run(execute())
    assert len(submissions) == 1


def test_one_deadline_covers_submit_poll_and_download():
    requests = []

    async def callback(request):
        requests.append(request)
        await asyncio.sleep(0.04)
        value = (
            task("pending")
            if request.method == "POST"
            else (task() if "/poll/" in request.url.path else converted())
        )
        return httpx.Response(200, json=value)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            adapter = Direct(Transport(client), "http://example.invalid", "", {}, poll_interval=0)
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.10):
                    await adapter.convert(Case("file.xlsx", "xlsx", "xlsx", (), ()), b"file", {})

    asyncio.run(execute())
    assert sum(request.method == "POST" for request in requests) == 1


def test_duplicate_error_fields_cannot_hide_a_partial_result():
    async def callback(request):
        if request.method == "POST":
            return httpx.Response(200, json=task())
        raw = (
            '{"status":"success","errors":["lost page"],"errors":[],'
            '"document":{"json_content":' + json.dumps(document()) + "}}"
        )
        return httpx.Response(200, content=raw.encode())

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            adapter = Direct(Transport(client), "http://example.invalid", "", {})
            with pytest.raises(ProtocolError, match="partial_or_invalid"):
                await adapter.convert(Case("file.xlsx", "xlsx", "xlsx", (), ()), b"file", {})

    asyncio.run(execute())
