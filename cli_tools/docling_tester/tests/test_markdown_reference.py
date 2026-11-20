import asyncio
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from conftest import document

from docling_tester.corpus import Corpus
from docling_tester.runner import Settings, check_direct
from docling_tester.transport import Direct, Transport


def ascii_case():
    corpus = Corpus()
    case = next(c for c in corpus.cases if c.path.endswith("asciidoc_01.asciidoc"))
    return corpus, case


@pytest.mark.parametrize(
    "damage",
    [
        "none",
        "crlf",
        "cr",
        "text",
        "whitespace",
        "missing",
        "null",
        "profile",
        "invalid_json",
    ],
)
def test_existing_upstream_markdown_is_independent_evidence(tmp_path, damage):
    corpus, case = ascii_case()
    path = corpus.markdown_reference(case)
    assert path is not None
    expected = corpus.read_markdown(path)
    actual = expected
    if damage == "crlf":
        actual = actual.replace("\n", "\r\n")
    elif damage == "cr":
        actual = actual.replace("\n", "\r")
    elif damage == "text":
        actual += "\nUnexpected content"
    elif damage == "whitespace":
        actual += " "
    settings = Settings("direct", "https://example.invalid", tmp_path)
    if damage == "profile":
        settings.reference_profile = "other"
    body = {"document": {"json_content": document(), "md_content": actual}}
    if damage == "missing":
        del body["document"]["md_content"]
    elif damage == "null":
        body["document"]["md_content"] = None
    elif damage == "invalid_json":
        body["document"]["json_content"] = {}
        with pytest.raises(ValueError):
            check_direct(corpus, case, settings, body, {"outcome": "convert"})
        return
    status, reason, detail = check_direct(corpus, case, settings, body, {"outcome": "convert"})
    assert status == (
        "PASS"
        if damage in {"none", "crlf", "cr"}
        else "UNVERIFIED"
        if damage == "profile"
        else "FAIL"
    )
    if status == "PASS":
        assert detail["verification_scope"] == "upstream_markdown_export"
        assert detail["reference_sha256"] == corpus.files[path]["sha256"]


def test_markdown_reference_hash_and_absence_are_checked(tmp_path):
    corpus, case = ascii_case()
    path = corpus.markdown_reference(case)
    assert path is not None
    corpus.files[path] = {**corpus.files[path], "sha256": "changed"}
    with pytest.raises(ValueError, match="checksum"):
        corpus.read_markdown(path)
    del corpus.files[path]
    status, reason, _ = check_direct(
        corpus,
        case,
        Settings("direct", "https://example.invalid", tmp_path),
        {"document": {"json_content": document(), "md_content": "anything"}},
        {"outcome": "convert"},
    )
    assert (status, reason) == ("UNVERIFIED", "compatible_reference_missing")


def test_asciidoc_requests_both_exports_and_compact_tables(tmp_path):
    _, case = ascii_case()

    async def callback(request):
        if request.method == "POST":
            raw = await request.aread()
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + raw
            )
            fields = {}
            for part in message.iter_parts():
                if part.get_filename() is None:
                    name = part.get_param("name", header="content-disposition")
                    value = part.get_payload(decode=True)
                    assert isinstance(name, str) and isinstance(value, bytes)
                    fields.setdefault(name, []).append(value.decode())
            assert fields["to_formats"] == ["json", "md"]
            assert fields["md_compact_tables"] == ["true"]
            return httpx.Response(
                200,
                json={
                    "task_id": "example",
                    "task_type": "convert",
                    "task_status": "success",
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "errors": [],
                "document": {"json_content": document(), "md_content": "example"},
            },
        )

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
            settings = Settings("direct", "https://example.invalid", tmp_path)
            await Direct(Transport(client), settings.url, "", settings.options()).convert(
                case,
                b"= Example",
                {},
            )

    asyncio.run(execute())
