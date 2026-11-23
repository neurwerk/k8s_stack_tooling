from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from conftest import document, tiny_corpus

from docling_tester.capture import Store, correlation_id, server
from docling_tester.corpus import Case, digest
from docling_tester.runner import Settings, check_direct, run
from docling_tester.transport import ProtocolError, captured_text


@pytest.fixture
def capture_backend():
    store = Store()
    backend = server("127.0.0.1", 0, "backend-only", "reader-only", store)
    thread = threading.Thread(target=backend.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    yield f"http://127.0.0.1:{backend.server_port}", store
    backend.shutdown()
    backend.server_close()
    thread.join()


def test_capture_authentication_correlation_consumption_and_expiry(capture_backend):
    url, store = capture_backend
    identifier = "a" * 32
    messages = [{"role": "user", "content": "docling-tester:" + identifier}]
    with httpx.Client(trust_env=False) as client:
        assert (
            client.post(url + "/v1/chat/completions", json={"messages": messages}).status_code
            == 401
        )
        reply = client.post(
            url + "/v1/chat/completions",
            json={"messages": messages},
            headers={"Authorization": "Bearer backend-only"},
        )
        assert reply.status_code == 200
        assert "docling-tester:" not in reply.text  # No echo through response restoration.
        path = url + "/captures/" + identifier
        assert client.get(path, headers={"Authorization": "Bearer backend-only"}).status_code == 401
        captured = client.get(path, headers={"Authorization": "Bearer reader-only"})
        assert captured.status_code == 200
        assert captured.json()["messages"] == messages
        assert client.get(path, headers={"Authorization": "Bearer reader-only"}).status_code == 404
        with pytest.raises(ProtocolError):
            captured_text(captured.json(), "b" * 32)
    store.ttl = -1
    assert store.put(identifier, messages)
    assert store.take(identifier) is None


@pytest.mark.parametrize("extension", ["xlsx", "docx", "pptx", "pdf", "png"])
def test_gateway_sends_fixture_bytes_but_never_trusts_a_response_echo(
    tmp_path, http_server, extension
):
    corpus = tiny_corpus(tmp_path, (f"source.{extension}",))
    seen = []

    def callback(method, path, raw, headers):
        assert method == "POST" and path == "/chat/completions"
        body = json.loads(raw)
        part = body["messages"][0]["content"][1]
        value = part["image_url"]["url"] if extension == "png" else part["file"]["file_data"]
        seen.append(base64.b64decode(value.split(",", 1)[1], validate=True))
        return 200, {
            "choices": [{"message": {"content": "Perfect echo of every expected table cell"}}]
        }

    report, code = asyncio.run(
        run(
            Settings("gateway", http_server(callback), tmp_path / "results", model="fixture-model"),
            corpus,
        )
    )
    assert seen == [(corpus.root / f"source.{extension}").read_bytes()]
    assert code == 2
    assert report["results"][0]["status"] == "UNVERIFIED"


@pytest.mark.parametrize(
    ("status", "reason", "outcome"),
    [
        (400, "unsupported_format", "EXPECTED_REJECTION"),
        (400, "authentication_failed", "FAIL"),
        (401, "unsupported_format", "FAIL"),
    ],
)
def test_gateway_rejection_requires_both_status_and_reason(
    tmp_path, http_server, status, reason, outcome
):
    corpus = tiny_corpus(tmp_path, ("source.xls",))
    url = http_server(lambda *args: (status, {"error": {"reason": reason}}))
    report, code = asyncio.run(
        run(Settings("gateway", url, tmp_path / "results", model="fixture-model"), corpus)
    )
    assert report["results"][0]["status"] == outcome
    assert (code == 0) == (outcome == "EXPECTED_REJECTION")


@pytest.mark.parametrize(
    ("observed", "outcome"),
    [
        ("Document: source.xlsx\n[PERSON_1] | 10", "PASS"),
        ("Document: source.xlsx\n[PERSON_1] | 100", "FAIL"),
        (None, "UNVERIFIED"),
    ],
)
def test_gateway_checks_post_policy_backend_input(
    tmp_path, http_server, capture_backend, observed, outcome
):
    corpus = tiny_corpus(tmp_path, ("source.xlsx",))
    capture_url, store = capture_backend
    golden = tmp_path / "approved.txt"
    golden.write_text("Document: source.xlsx\n[PERSON_1] | 10")
    config = tmp_path / "expectations.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": {
                    "source.xlsx": {
                        "gateway": {
                            "outcome": "convert",
                            "reference": golden.name,
                            "sha256": digest(golden),
                        }
                    }
                },
            }
        )
    )

    def callback(method, path, raw, headers):
        payload = json.loads(raw)
        messages = payload["messages"]
        identifier = correlation_id(messages)
        if observed is not None:
            messages[0]["content"][1] = {"type": "text", "text": observed}
            assert store.put(identifier, messages)
        return 200, {"choices": [{"message": {"content": "Restored original name | 10"}}]}

    report, code = asyncio.run(
        run(
            Settings(
                "gateway",
                http_server(callback),
                tmp_path / "results",
                model="fixture-model",
                capture_url=capture_url,
                capture_key="reader-only",
                expectations=config,
            ),
            corpus,
        )
    )
    assert report["results"][0]["status"] == outcome
    assert (code == 0) == (outcome == "PASS")


def test_incomplete_capture_cannot_pass_and_raw_attachments_fail():
    identifier = "c" * 32
    with pytest.raises(ProtocolError, match="incomplete"):
        captured_text({"complete": False}, identifier)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "docling-tester:" + identifier},
                {"type": "file", "file": {"filename": "still-original.pdf"}},
            ],
        }
    ]
    capture = {
        "schema_version": 1,
        "complete": True,
        "correlation": identifier,
        "messages": messages,
        "messages_sha256": hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode()
        ).hexdigest(),
    }
    with pytest.raises(ProtocolError, match="unextracted"):
        captured_text(capture, identifier)


def test_prompt_only_capture_with_blank_reference_is_unverified(
    tmp_path, http_server, capture_backend
):
    corpus = tiny_corpus(tmp_path)
    url, store = capture_backend
    reference = tmp_path / "blank.txt"
    reference.write_text(" \n")
    config = tmp_path / "rules.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": {
                    "sample.xlsx": {
                        "gateway": {
                            "outcome": "convert",
                            "reference": reference.name,
                            "sha256": digest(reference),
                        }
                    }
                },
            }
        )
    )

    def callback(method, path, raw, headers):
        messages = json.loads(raw)["messages"]
        identifier = correlation_id(messages)
        messages[0]["content"].pop()
        assert store.put(identifier, messages)
        return 200, {"choices": [{"message": {"content": "OK"}}]}

    report, code = asyncio.run(
        run(
            Settings(
                "gateway",
                http_server(callback),
                tmp_path / "out",
                model="fixture",
                capture_url=url,
                capture_key="reader-only",
                expectations=config,
            ),
            corpus,
        )
    )
    assert code == 2
    assert report["results"][0]["reason"] == "attachment_content_evidence_missing"


def test_mixed_run_retains_failures_unverified_timeouts_and_redacts_credentials(
    tmp_path, http_server, capsys
):
    corpus = tiny_corpus(tmp_path, ("a.xlsx", "b.xlsx", "c.xlsx", "d.xlsx", "e.xlsx"))
    # Missing reference is explicit, not a request to generate or accept live output.
    corpus.cases[2] = Case("c.xlsx", "fixtures", "xlsx", (), ())
    source_hashes = {path: entry["sha256"] for path, entry in corpus.files.items()}
    submissions = 0
    credential = 'TOKEN"credential-sentinel'

    def callback(method, path, raw, headers):
        nonlocal submissions
        if method == "POST":
            submissions += 1
            if submissions == 4:
                time.sleep(0.15)
            return 200, {
                "task_id": str(submissions),
                "task_type": "convert",
                "task_status": "success",
            }
        actual = document()
        if submissions == 2:
            actual["tables"][0]["data"]["table_cells"][1]["text"] = "100"
        return 200, {
            "status": "success",
            "errors": [],
            "document": {"json_content": actual},
            "debug": credential,
        }

    report, code = asyncio.run(
        run(
            Settings(
                "direct",
                http_server(callback),
                tmp_path / "results",
                key=credential,
                timeout=0.08,
            ),
            corpus,
        )
    )
    assert code == 1
    assert [item["status"] for item in report["results"]] == [
        "PASS",
        "FAIL",
        "UNVERIFIED",
        "ERROR",
        "NOT_RUN",
    ]
    assert report["complete"] is False
    assert report["results"][3]["trace"]["remote_job_may_continue"] is True
    assert submissions == 4
    for path in (tmp_path / "results").iterdir():
        assert credential not in path.read_text()
        assert "credential-sentinel" not in path.read_text()
        assert path.stat().st_mode & 0o077 == 0
    assert "credential-sentinel" not in str(capsys.readouterr())
    assert {path: digest(corpus.root / path) for path in corpus.files} == source_hashes
    assert json.loads((tmp_path / "results/report.json").read_text())["counts"] == report["counts"]


def test_missing_profile_and_empty_selection_are_not_success(tmp_path):
    corpus = tiny_corpus(tmp_path, ("source.pdf",))
    settings = Settings("direct", "http://example.invalid", tmp_path / "results")
    status, reason, _ = check_direct(
        corpus,
        corpus.cases[0],
        settings,
        {"document": {"json_content": document()}},
        {"outcome": "convert"},
    )
    assert status == "UNVERIFIED" and reason == "compatible_reference_missing"
    settings.match = "absent/*"
    with pytest.raises(ValueError, match="No inputs"):
        asyncio.run(run(settings, corpus))
    assert not settings.output.exists()


def test_cancellation_keeps_completed_results_and_marks_unfinished_work(tmp_path, http_server):
    corpus = tiny_corpus(tmp_path, ("first.xlsx", "second.xlsx", "third.xlsx"))
    submitted = threading.Event()
    count = 0

    def callback(method, path, raw, headers):
        nonlocal count
        if method == "POST":
            count += 1
            if count == 2:
                submitted.set()
                time.sleep(0.1)
            return 200, {"task_id": str(count), "task_type": "convert", "task_status": "success"}
        return 200, {"status": "success", "errors": [], "document": {"json_content": document()}}

    async def execute():
        running = asyncio.create_task(
            run(Settings("direct", http_server(callback), tmp_path / "results"), corpus)
        )
        assert await asyncio.to_thread(submitted.wait, 5)
        running.cancel()
        return await running

    report, code = asyncio.run(execute())
    assert code == 130
    assert [item["status"] for item in report["results"]] == ["PASS", "ERROR", "NOT_RUN"]
    assert Path(tmp_path / "results/report.json").is_file()
