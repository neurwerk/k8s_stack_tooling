import asyncio
import json
import os
import subprocess
import sys

import pytest

from docling_tester.corpus import Corpus
from docling_tester.local import convert
from docling_tester.local_worker import page_request


@pytest.mark.parametrize("reason", ["stop", "length", None, "unknown"])
def test_docker_bridge_rejects_incomplete_pages_and_forces_settings(monkeypatch, reason):
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, json.loads(kwargs["input"])))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "choices": [{"finish_reason": reason, "message": {"content": "example"}}],
                }
            ).encode(),
        )

    monkeypatch.setattr(subprocess, "run", execute)
    config = {"context": "test-context", "container": "test-container", "timeout": 10}
    if reason == "stop":
        page_request({"model": "wrong", "max_tokens": 8192}, config)
    else:
        with pytest.raises(ValueError, match="Incomplete"):
            page_request({}, config)
    assert calls[0][0][:7] == [
        "docker",
        "--context",
        "test-context",
        "exec",
        "-i",
        "test-container",
        "python3",
    ]
    assert calls[0][1]["model"] == "vlm-documents"
    assert calls[0][1]["max_tokens"] == 4096


def test_native_csv_works_without_docker_or_model_downloads(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    path = Corpus().root / "tests/data/csv/sources/csv-comma.csv"
    trace = {}
    reply = asyncio.run(convert(path, "nonexistent-context", "nonexistent-container", 60, trace))
    assert reply.body["status"] == "success"
    assert reply.body["document"]["json_content"]["tables"]
    assert trace["remote_job_may_continue"] is False


def test_cancelled_worker_is_reaped_and_remote_completion_stays_unknown(monkeypatch, tmp_path):
    original = asyncio.create_subprocess_exec
    children = []

    async def child(*args, **kwargs):
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", child)
    trace = {}

    async def execute():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await convert(tmp_path / "unused", "test-context", "test-container", 10, trace)

    asyncio.run(execute())
    assert children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
    assert trace["remote_job_may_continue"] is True
