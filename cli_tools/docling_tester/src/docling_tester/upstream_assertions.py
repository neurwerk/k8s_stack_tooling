"""Literal Markdown assertions from the pinned Docling tests (MIT, Docling Contributors).

These check the upstream assertions, not full-document equivalence.
See upstream/LICENSE and the named test functions for provenance.
"""

from typing import TypedDict


class Assertions(TypedDict):
    source: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...]


SIMPLE = (
    "Simple Email",
    "From: Alice Example &lt;alice@example.com&gt;",
    "To: Bob Example &lt;bob@example.com&gt;",
    "Hello Bob,",
    "This is a simple email body.",
)

CONTRACTS: dict[str, Assertions] = {
    "tests/data/email/sources/eml_simple.eml": {
        "source": "tests/test_backend_email.py::test_convert_email_backend_from_path",
        "required": SIMPLE,
        "forbidden": (),
    },
    "tests/data/email/sources/msg_simple.msg": {
        "source": "tests/test_backend_email.py::test_convert_msg_backend_from_path",
        "required": SIMPLE,
        "forbidden": (),
    },
    "tests/data/email/sources/eml_with_attachment.eml": {
        "source": "tests/test_backend_email.py::test_email_with_attachment_excludes_encoded_content",
        "required": (
            "Email with Attachment",
            "From: Alice Example &lt;alice@example.com&gt;",
            "To: Bob Example &lt;bob@example.com&gt;",
            "This email contains an attachment.",
        ),
        "forbidden": (
            "VGhpcyBpcyBhIHRlc3QgYXR0YWNobWVudCBmaWxlLgpJdCBjb250YWlucyBzb21lIGR1bW15IGNv",
            "bnRlbnQuCg==",
            "This is a test attachment file.",
            "It contains some dummy content.",
        ),
    },
    "tests/data/email/sources/msg_with_attachment.msg": {
        "source": (
            "tests/test_backend_email.py::test_msg_with_attachment_excludes_content_and_names_by_default"
        ),
        "required": ("Email with Attachment", "This email contains an attachment."),
        "forbidden": (
            "This is a test attachment file.",
            "test.txt",
            "report.pdf",
            "Attachments",
        ),
    },
    "tests/data/mhtml/sources/example.mhtml": {
        "source": "tests/test_backend_mhtml.py::test_real_blink_fixture_decodes_quoted_printable_html",
        "required": ("# Example Domain", "[Learn more](https://iana.org/domains/example)"),
        "forbidden": (),
    },
}
