"""Exact grouping keys; recognize formatting, never generalize error content."""

from __future__ import annotations

import re
from datetime import datetime

# Foreground colours, reset, and the dim/italic styles used by Rust tracing.
# Other terminal controls are not proven noise and force exact raw grouping.
_STYLE = r"(?:0|2|3|3[0-7]|39|9[0-7])"
_SGR = re.compile(rf"(?:\x1b\[|\x9b)(?:{_STYLE}(?:;{_STYLE})*)?m")
_TRACING_PREFIX = re.compile(
    r"\A(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]))[ \t]+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)[ \t]+"
    r"(?P<module>[A-Za-z_][A-Za-z_0-9]*(?:::[A-Za-z_][A-Za-z_0-9]*)+)"
    r"[ \t]*: (?=[^\r\n])"
)


def grouping_parts(message: str) -> tuple[str, ...]:
    """Remove only a valid Rust logging timestamp and supported SGR styles.

    Module, severity, and the complete body remain part of the identity. Unknown
    envelopes or remaining terminal controls retain the entire original string.
    This is deliberately stricter than the presentation formatter.
    """
    cleaned = _SGR.sub("", message)
    if any((char < " " and char not in "\t\r\n") or "\x7f" <= char <= "\x9f" for char in cleaned):
        return ("raw", message)
    prefix = _TRACING_PREFIX.match(cleaned)
    if prefix:
        try:
            datetime.fromisoformat(prefix["timestamp"])
        except ValueError:
            pass
        else:
            return ("rust-tracing", prefix["level"], prefix["module"], cleaned[prefix.end() :])
    return ("raw", message)
