import pytest
from conftest import document

from docling_tester.corpus import Corpus
from docling_tester.runner import Settings, check_direct

SIMPLE = (
    "# Simple Email\nFrom: Alice Example &lt;alice@example.com&gt;\n"
    "To: Bob Example &lt;bob@example.com&gt;\nHello Bob,\nThis is a simple email body."
)
ATTACHMENT = (
    "# Email with Attachment\nFrom: Alice Example &lt;alice@example.com&gt;\n"
    "To: Bob Example &lt;bob@example.com&gt;\nThis email contains an attachment."
)


def check(tmp_path, path, markdown):
    corpus = Corpus()
    case = next(c for c in corpus.cases if c.path == path)
    return check_direct(
        corpus,
        case,
        Settings("direct", "https://example.invalid", tmp_path),
        {"document": {"json_content": document(), "md_content": markdown}},
        {"outcome": "convert"},
    )


@pytest.mark.parametrize(
    "path,text,removed",
    [
        ("email/sources/eml_simple.eml", SIMPLE, "simple "),
        ("email/sources/msg_simple.msg", SIMPLE, "simple "),
        ("email/sources/eml_with_attachment.eml", ATTACHMENT, "contains "),
        ("email/sources/msg_with_attachment.msg", ATTACHMENT, "contains "),
        (
            "mhtml/sources/example.mhtml",
            "# Example Domain\n[Learn more](https://iana.org/domains/example)",
            "https://",
        ),
    ],
)
def test_upstream_assertions_detect_missing_content(tmp_path, path, text, removed):
    path = "tests/data/" + path
    status, _, details = check(tmp_path, path, text)
    assert status == "PASS"
    assert details["verification_scope"] == "upstream_assertions"
    assert details["full_content_verified"] is False
    assert check(tmp_path, path, text.replace(removed, ""))[0] == "FAIL"
    assert check(tmp_path, path, None)[:2] == ("FAIL", "markdown_output_missing")


@pytest.mark.parametrize(
    "name,prohibited",
    [
        ("eml_with_attachment.eml", "This is a test attachment file."),
        ("eml_with_attachment.eml", "It contains some dummy content."),
        ("eml_with_attachment.eml", "bnRlbnQuCg=="),
        ("msg_with_attachment.msg", "test.txt"),
        ("msg_with_attachment.msg", "report.pdf"),
        ("msg_with_attachment.msg", "Attachments"),
    ],
)
def test_attachment_exclusions_are_enforced(tmp_path, name, prohibited):
    assert (
        check(tmp_path, f"tests/data/email/sources/{name}", ATTACHMENT + "\n" + prohibited)[0]
        == "FAIL"
    )


def test_markdown_uses_complete_upstream_export_without_trimming_response(tmp_path):
    corpus = Corpus()
    case = next(c for c in corpus.cases if c.path == "tests/data/md/sources/blocks.md")
    path = corpus.markdown_reference(case)
    assert path is not None
    expected = corpus.read_markdown(path).rstrip()
    assert check(tmp_path, case.path, expected)[0] == "PASS"
    for damaged in (expected + "\n", expected + " ", expected[10:], expected.replace("\n", "\r\n")):
        assert check(tmp_path, case.path, damaged)[0] == "FAIL"


@pytest.mark.parametrize("name", ["inline_and_formatting", "mixed_without_h1"])
def test_markdown_does_not_replace_required_yaml_docitem_checks(tmp_path, name):
    corpus = Corpus()
    expected = corpus.read_markdown(f"tests/data/md/groundtruth/{name}.md.md").rstrip()
    assert check(tmp_path, f"tests/data/md/sources/{name}.md", expected)[0] == "UNVERIFIED"


def test_markdown_fallback_cannot_rescue_structured_content_mismatch(tmp_path):
    corpus = Corpus()
    expected = corpus.read_markdown("tests/data/md/groundtruth/line_breaks.md.md").rstrip()
    assert check(tmp_path, "tests/data/md/sources/line_breaks.md", expected)[0] == "FAIL"
