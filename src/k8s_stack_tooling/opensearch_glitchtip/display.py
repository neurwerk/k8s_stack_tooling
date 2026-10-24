"""Terminal-log presentation only; never use this output for occurrence identity."""

from __future__ import annotations

import re
from datetime import datetime

_C1_STARTERS = {"\x9b": "[", "\x9d": "]", "\x90": "P", "\x98": "X", "\x9e": "^", "\x9f": "_"}
_TRACING_PREFIX = re.compile(
    r"\A(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))[ \t]+"
    r"(?:TRACE|DEBUG|INFO|WARN|ERROR)[ \t]+"
    r"[A-Za-z_][A-Za-z_0-9]*(?:::[A-Za-z_][A-Za-z_0-9]*)+[ \t]*:[ \t]+(?=\S)"
)


def _strip_terminal_sequences(message: str) -> tuple[str, bool]:
    """Scan ECMA-48 sequences once, preserving incomplete/malformed input.

    The cursor never moves backwards. Invalid control strings resume at the
    incompatible starter, never rescanning the prefix. Only complete sequences
    without incompatible embedded starters are removed.
    """
    chunks: list[str] = []
    cursor = kept_from = 0
    size = len(message)
    while cursor < size:
        start = cursor
        char = message[cursor]
        cursor += 1
        if char == "\x1b":
            if cursor == size:
                break
            kind = message[cursor]
        elif char in _C1_STARTERS:
            kind = _C1_STARTERS[char]
        else:
            continue

        if kind in "]PX^_":
            if char == "\x1b":
                cursor += 1
            complete = False
            while cursor < size:
                control_char = message[cursor]
                if control_char == "\x9c" or (kind == "]" and control_char == "\x07"):
                    cursor += 1
                    complete = True
                    break
                if control_char == "\x1b":
                    if cursor + 1 < size and message[cursor + 1] == "\\":
                        cursor += 2
                        complete = True
                    # Any other ESC invalidates this string. Preserve its
                    # prefix and resume at the new escape, without rescanning.
                    break
                # CAN/SUB cancel a string just as an incompatible starter
                # invalidates it. Leave the prefix and cancellation character
                # intact; a later ST must not swallow intervening error text.
                if control_char in _C1_STARTERS or control_char in "\x18\x1a":
                    break
                cursor += 1
            if not complete:
                continue
        elif kind == "[":
            if char == "\x1b":
                cursor += 1
            while cursor < size and "0" <= message[cursor] <= "?":
                cursor += 1
            while cursor < size and " " <= message[cursor] <= "/":
                cursor += 1
            if cursor == size or not "@" <= message[cursor] <= "~":
                continue
            cursor += 1
        else:
            while cursor < size and " " <= message[cursor] <= "/":
                cursor += 1
            if cursor == size or not "0" <= message[cursor] <= "~":
                continue
            cursor += 1
        chunks.append(message[kept_from:start])
        kept_from = cursor
    if not chunks:
        return message, False
    chunks.append(message[kept_from:])
    return "".join(chunks), True


def display_message(message: str) -> str:
    """Clean proven terminal output; leave all escape-free messages unchanged."""
    cleaned, stripped = _strip_terminal_sequences(message)
    if not stripped:
        return message
    # Only the observed Rust tracing envelope is recognized, and only for
    # terminal-bearing logs. Do not generalize or summarize any error content.
    prefix = _TRACING_PREFIX.match(cleaned)
    if prefix:
        try:
            datetime.fromisoformat(prefix["timestamp"])
        except ValueError:
            pass
        else:
            return cleaned[prefix.end() :]
    return cleaned
