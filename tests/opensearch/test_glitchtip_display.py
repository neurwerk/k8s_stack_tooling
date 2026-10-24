"""Astra-reviewed synthetic presentation regressions; no client log fixtures."""

from __future__ import annotations

from typing import SupportsIndex

import pytest

from k8s_stack_tooling.opensearch_glitchtip.display import display_message


@pytest.mark.parametrize(
    "message,expected",
    [
        ("\x1b[31mÉchec — 数据\x1b[0m\n\tCaused by: reset", "Échec — 数据\n\tCaused by: reset"),
        ("\x9b31mfailed\x9b0m", "failed"),
        ("\x1b]8;;https://example.test\x1b\\failed\x1b]8;;\x1b\\", "failed"),
        ("\x1b]0;window title\x07failed", "failed"),
        ("\x9d0;window title\x9cfailed", "failed"),
        ("\x1bPterminal data\nsecond line\x1b\\failed", "failed"),
        ("\x1bXterminal data\x1b\\failed", "failed"),
        ("\x1b^terminal data\x1b\\failed", "failed"),
        ("\x1b_terminal data\x1b\\failed", "failed"),
        ("\x90data\x9c\x98data\x9c\x9edata\x9c\x9fdata\x9cfailed", "failed"),
        ("\x1b(Bfailed\x1bM", "failed"),
        ("\x1b[31mfirst\x85second\x1b[0m\r\nthird\tline", "first\x85second\r\nthird\tline"),
        ("\x1b[31mfailed\x1b[0m\x00\x08\x7f", "failed\x00\x08\x7f"),
        ("\x1b[31mfailed\x1b[0m\x1b[", "failed\x1b["),
        ("\x1b[31mfailed\x1b[0m\x1b]unterminated", "failed\x1b]unterminated"),
        ("\x1b[31mfailed\x1b[0m\x1b[12\nÉchec", "failed\x1b[12\nÉchec"),
        ("\x1b\\failed", "failed"),
        ("\x9cfailed", "\x9cfailed"),
    ],
)
def test_complete_terminal_sequences_only(message: str, expected: str) -> None:
    assert display_message(message) == expected


def test_colored_tracing_envelope_preserves_complete_failure() -> None:
    body = (
        "request failed: code=301 status=503 destination=db.example.test:27017; "
        "cause=connection reset by peer "
    )
    continuation = "\nretry exhausted after 3 attempts; operation=find database=example"
    message = (
        "\x1b[31m2026-01-01T00:00:00Z ERROR documentdb::client: " + body + "\x1b[0m" + continuation
    )
    assert display_message(message) == body + continuation


@pytest.mark.parametrize("starter", ["\x1b]", "\x1bP", "\x9d", "\x90"])
@pytest.mark.parametrize(
    "color,title", [("\x1b[31m", "\x1b]0;title\x07"), ("\x9b31m", "\x9d0;title\x07")]
)
def test_embedded_escape_invalidates_control_string_without_losing_failure(
    starter: str, color: str, title: str
) -> None:
    body = (
        "request failed: code=301 status=503 destination=db.example.test:27017; "
        "cause=connection reset by peer"
    )
    malformed = starter + "unterminated"
    message = malformed + color + body + "\x1b[0m" + title
    assert display_message(message) == malformed + body


@pytest.mark.parametrize("cancel", ["\x18", "\x1a"])
@pytest.mark.parametrize(
    "prefix",
    [
        "\x1b]unfinished",
        "\x9dunfinished",
        "\x1bPunfinished",
        "\x90unfinished",
        "\x1bXunfinished",
        "\x98unfinished",
        "\x1b^unfinished",
        "\x9eunfinished",
        "\x1b_unfinished",
        "\x9funfinished",
        "\x1b[31",
        "\x9b31",
        "\x1b[31 ",
        "\x1b(",
    ],
)
def test_cancellation_preserves_malformed_prefix_and_complete_failure(
    prefix: str, cancel: str
) -> None:
    body = (
        "request failed: code=301 status=503 destination=db.example.test:27017; "
        "cause=connection reset by peer"
    )
    # The late ST is a standalone ESC sequence, not the end of the cancelled
    # string. CSI and ordinary ESC paths must preserve cancellation too.
    message = prefix + cancel + body + "\x1b\\"
    assert display_message(message) == prefix + cancel + body
    assert display_message(prefix + cancel + body) == prefix + cancel + body
    assert display_message(prefix + cancel + body + "\x9c") == prefix + cancel + body + "\x9c"
    colored = prefix + cancel + "\x1b[31m" + body + "\x1b[0m\x1b]0;title\x07"
    assert display_message(colored) == prefix + cancel + body


@pytest.mark.parametrize(
    "prefix",
    [
        "2026-13-01T00:00:00Z ERROR example::client: ",
        "2026-01-01T00:00:00 ERROR example::client: ",
        "2026-01-01T00:00:00Z UNKNOWN example::client: ",
        "2026-01-01T00:00:00Z ERROR destination.example.test: ",
        "2026-01-01T00:00:00Z ERROR example::client: \n",
        "context: 2026-01-01T00:00:00Z ERROR example::client: ",
    ],
)
def test_unrecognized_envelope_is_preserved(prefix: str) -> None:
    assert display_message(prefix + "\x1b[31mfailed\x1b[0m") == prefix + "failed"


@pytest.mark.parametrize(
    "message",
    [
        '{"level":"error","logger":"studio.api","message":"Échec — 数据",'
        '"timestamp":"2026-01-01T00:00:00Z"}',
        "2026-01-01T00:00:00Z ERROR studio::api: request failed\n\tCause: Übertragung",
        "studio.api: request failed — 数据\r\nsecond line\x85third line",
        r'{"message":"literal \\u001b[31m text"}',
        "\x1b[",
        "\x1b]unterminated",
        "\x90unterminated",
        "\x1b[12\nmalformed",
        "trailing escape\x1b",
        "\x1b[31 ",
        "\x1b(",
        "\x1b ",
        "\x9b31",
    ],
)
def test_readable_studio_and_malformed_messages_are_exact_passthrough(message: str) -> None:
    assert display_message(message).encode() == message.encode()


@pytest.mark.parametrize(
    "starter",
    [
        "\x9d",
        "\x90",
        "\x98",
        "\x9e",
        "\x9f",
        "\x1b]",
        "\x1bP",
        "\x1b]\x90\x1bP\x9d",
        "\x1b]\x18",
        "\x90\x1a",
        "\x1b]\x1a\x90\x18\x1b[31\x18",
    ],
)
def test_unterminated_repeated_starters_are_scanned_linearly(starter: str) -> None:
    class CountedText(str):
        reads = 0

        def __getitem__(self, index: SupportsIndex | slice, /) -> str:
            self.reads += 1
            return super().__getitem__(index)

    # Count input reads instead of using a flaky wall-clock timeout. Repeated
    # unterminated starters previously caused overlapping scans of the tail.
    message = CountedText((starter * 65536)[:65536])
    assert display_message(message) == message
    assert message.reads <= 5 * len(message)
    # A valid sequence after the malformed starters must still be cleaned,
    # rather than making a later terminator erase the malformed prefixes.
    with_valid_tail = CountedText(message + "\x1b[31mrequest failed\x1b[0m\x1b]0;title\x07")
    assert display_message(with_valid_tail) == message + "request failed"
    assert with_valid_tail.reads <= 5 * len(with_valid_tail)
