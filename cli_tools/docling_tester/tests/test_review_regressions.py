import asyncio
import copy
import json

import httpx
import pytest
from conftest import document, tiny_corpus

from docling_tester.compare import compare_documents
from docling_tester.corpus import Case, digest
from docling_tester.runner import Settings, approved_reference, overrides
from docling_tester.transport import Direct, ProtocolError, Transport


@pytest.mark.parametrize("field", ["graph", "marker", "content_layer"])
def test_semantic_content_cannot_disappear(field):
    expected = document()
    expected["texts"][0][field] = {
        "graph": {"cells": [{"cell_id": 1, "text": "1625"}], "links": []},
        "marker": "1.",
        "content_layer": "body",
    }[field]
    actual = copy.deepcopy(expected)
    del actual["texts"][0][field]
    assert not compare_documents(expected, actual)["matches"]


def test_reference_replacement_after_preflight_is_rejected(tmp_path):
    corpus = tiny_corpus(tmp_path)
    reference = tmp_path / "approved.txt"
    reference.write_text("approved")
    rule = {"outcome": "convert", "reference": reference.name, "sha256": digest(reference)}
    config = tmp_path / "rules.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": {"sample.xlsx": {"gateway": rule}},
            }
        )
    )
    overrides(config, corpus.cases)
    reference.write_text("unapproved")
    settings = Settings(
        "gateway", "http://example.invalid", tmp_path / "out", model="test", expectations=config
    )
    with pytest.raises(ValueError, match="changed"):
        approved_reference(settings, rule)


@pytest.mark.parametrize("phase", ["poll", "result"])
def test_failing_response_replaces_previous_success(phase):
    async def callback(request):
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "task_id": "job",
                    "task_type": "convert",
                    "task_status": "pending" if phase == "poll" else "success",
                },
            )
        return httpx.Response(502, content=b"upstream failure diagnostic")

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            trace = {}
            adapter = Direct(Transport(client), "http://example.invalid", "", {}, poll_interval=0)
            with pytest.raises(ProtocolError):
                await adapter.convert(Case("file.xlsx", "xlsx", "xlsx", (), ()), b"file", trace)
            assert trace["phase"] == phase
            assert trace["http_status"] == 502
            assert trace["last_response"] == {
                "invalid_json_response": "upstream failure diagnostic",
            }

    asyncio.run(execute())
