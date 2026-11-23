import asyncio
import json
from typing import override

import httpx
import pytest

from docling_tester import questions
from docling_tester.corpus import Case, Corpus
from docling_tester.runner import Settings
from docling_tester.transport import ProtocolError


def completion(content='{"answer": 3}', finish="stop", **extra):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, **extra},
            }
        ]
    }


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": completion()["choices"] * 2},
        completion(finish="length"),
        completion(finish=None),
        completion(refusal="Cannot answer"),
        completion(tool_calls=[{"id": "call"}]),
        completion(content=None),
        completion(content='{"answer": 3'),
        completion(content='```json\n{"answer": 3}\n```'),
        completion(content='{"answer": 3} extra'),
        completion(content='{"answer": NaN}'),
        completion(content='{"answer": Infinity}'),
        completion(content='{"answer": 0, "answer": 3}'),
        completion(content='{"answer": 3, "extra": true}'),
    ],
)
def test_incomplete_or_ambiguous_answers_never_pass(body):
    with pytest.raises(ProtocolError):
        questions.answer(body)


def test_exact_answers_preserve_types_and_structure():
    assert questions.exact(questions.answer(completion()), 3)
    assert questions.exact({"b": [2], "a": "text"}, {"a": "text", "b": [2]})
    for actual in [True, 3.0, "3", None, 4]:
        assert not questions.exact(actual, 3)
    assert not questions.exact({"rows": [True]}, {"rows": [1]})
    assert not questions.exact({"answer": 3, "extra": 1}, {"answer": 3})
    assert not questions.exact({}, {"answer": 3})


@pytest.mark.parametrize("transport_failure", [False, True])
def test_runner_stops_and_never_counts_unrun_as_pass(tmp_path, monkeypatch, transport_failure):
    class FixtureCorpus(Corpus):
        def __init__(self):
            self.root = tmp_path
            self.cases = [Case(f"sample{i}.csv", "csv", "csv", (), ()) for i in range(4)]

        @override
        def verify(self):
            return {"revision": "fixture"}

    for i in range(4):
        (tmp_path / f"sample{i}.csv").write_text("item,count\npear,3\n")
    scenarios = tmp_path / "questions.json"
    scenarios.write_text(
        json.dumps(
            [
                {
                    "case": f"sample{i}.csv",
                    "question": f"Question {i}?",
                    "expected": 3,
                    "source_evidence": "Original CSV count is 3.",
                }
                for i in range(4)
            ]
        )
    )
    monkeypatch.setattr(questions, "SCENARIOS", scenarios)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if transport_failure:
            raise httpx.ReadTimeout("no result", request=request)
        return httpx.Response(
            200, json=completion('{"answer": 3}' if len(requests) == 1 else '{"answer": false}')
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        questions.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    settings = Settings(
        "gateway",
        "https://test.invalid/v1",
        tmp_path / "results",
        model="test-model",
        key="private-key",
    )
    report, code = asyncio.run(questions.run_questions(settings, FixtureCorpus()))
    assert code == 1
    assert report["scope"] == "end_to_end_question"
    assert report["extraction_completeness_validated"] is False
    assert report["complete"] is False
    assert report["counts"] == (
        {"ERROR": 1, "NOT_RUN": 3} if transport_failure else {"PASS": 1, "FAIL": 2, "NOT_RUN": 1}
    )
    assert len(requests) == (1 if transport_failure else 3)
    assert report["results"][0 if transport_failure else 1]["failure_label"] == (
        "transport_or_remote_completion" if transport_failure else "answer_mismatch"
    )
    assert all(
        "expected" not in request and "response_format" not in request for request in requests
    )
    assert "private-key" not in (settings.output / "report.json").read_text()


@pytest.mark.parametrize("bad_coverage", ["missing", "duplicate", "extra"])
def test_coverage_fails_before_network(tmp_path, monkeypatch, bad_coverage):
    corpus = Corpus()
    scenario = {
        "case": "sample.csv",
        "question": "What count?",
        "expected": 3,
        "source_evidence": "Original CSV count is 3.",
    }
    corpus.cases = [Case("sample.csv", "csv", "csv", (), ())]
    entries = {
        "missing": [],
        "duplicate": [scenario, scenario],
        "extra": [scenario, {**scenario, "case": "unexpected.csv"}],
    }[bad_coverage]
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(questions, "SCENARIOS", path)
    with pytest.raises(ValueError, match="coverage|Duplicate"):
        questions.question_plan(
            corpus, Settings("gateway", "https://test.invalid", tmp_path, model="test-model")
        )


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (400, "unsupported_format", "EXPECTED_REJECTION"),
        (400, "invalid_request", "FAIL"),
        (422, "unsupported_format", "FAIL"),
        (200, "unsupported_format", "FAIL"),
    ],
)
def test_full_inventory_rejections_and_skips(tmp_path, monkeypatch, status, reason, expected):
    class FixtureCorpus(Corpus):
        def __init__(self):
            self.root = tmp_path
            self.cases = [
                Case("sample.csv", "csv", "csv", (), ()),
                Case("skip.xml", "xml", "xml", (), (), "support_file"),
                Case("audio.txt", "audio", "txt", (), ()),
                Case("unsupported.xml", "xml", "xml", (), ()),
            ]

        @override
        def verify(self):
            return {"revision": "fixture"}

    (tmp_path / "sample.csv").write_text("count\n3\n")
    (tmp_path / "unsupported.xml").write_text("<data/>")
    path = tmp_path / "questions.json"
    path.write_text(
        json.dumps(
            [
                {
                    "case": "sample.csv",
                    "question": "What count?",
                    "expected": 3,
                    "source_evidence": "Original CSV count is 3.",
                }
            ]
        )
    )
    monkeypatch.setattr(questions, "SCENARIOS", path)
    requests = []

    def respond(request):
        requests.append(request)
        return (
            httpx.Response(200, json=completion())
            if len(requests) == 1
            else httpx.Response(status, json={"error": {"code": reason}})
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        questions.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    report, code = asyncio.run(
        questions.run_questions(
            Settings("gateway", "https://test.invalid", tmp_path / "results", model="test-model"),
            FixtureCorpus(),
        )
    )
    assert len(requests) == 2
    assert [(r["case"], r["status"]) for r in report["results"]] == [
        ("sample.csv", "PASS"),
        ("skip.xml", "SKIPPED"),
        ("audio.txt", "SKIPPED"),
        ("unsupported.xml", expected),
    ]
    assert report["complete"] is True
    assert code == (0 if expected == "EXPECTED_REJECTION" else 1)
