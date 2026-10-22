"""Bounded native diagnostics: exception types and code locations, never values."""

from __future__ import annotations

import re
import unittest
from collections import deque
from collections.abc import Mapping
from types import TracebackType

type ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType | None]

SAFE_SUBCASES = (
    "existing-user",
    "owner-role",
    "duplicate-alert",
    "duplicate-monitor",
    "cross-team",
    "cross-page",
)
PROJECT_SOURCES = {"acceptance.py", "payload.py", "guard.py"}


def identifier(value: object) -> str:
    """Permit bounded code identifiers only, with no arbitrary text fallback."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        return value
    return "unknown"


def safe_test_name(test: unittest.TestCase) -> str:
    """Never format subtest parameter values, descriptions, or user-defined reprs."""
    case = getattr(test, "test_case", test)
    name = (
        identifier(type(case).__name__) + "." + identifier(getattr(case, "_testMethodName", None))
    )
    parameters = getattr(test, "params", None)
    value = parameters.get("conflict") if isinstance(parameters, Mapping) else None
    # Return a source literal, never arbitrary subtest data or its repr.
    label = next((label for label in SAFE_SUBCASES if type(value) is str and value == label), None)
    return name + (" [subcase=" + label + "]" if label is not None else "")


def exception_metadata(error: ExceptionInfo) -> str:
    """Bound each of four nodes to three project locations plus eight tail locations."""
    pending = deque([("root", error[1], error[2])])
    seen: set[int] = set()
    lines = []
    while pending and len(seen) < 4:
        relation, exception, traceback = pending.popleft()
        if id(exception) in seen:
            continue
        seen.add(id(exception))
        lines.append(relation + ": " + identifier(type(exception).__name__))
        frames: deque[str] = deque(maxlen=8)
        project_frames = []
        walked = 0
        while traceback is not None and walked < 256:
            code = traceback.tb_frame.f_code
            basename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}\.py", basename):
                basename = "unknown.py"
            location = f"  {basename}:{identifier(code.co_name)}:{traceback.tb_lineno}"
            frames.append(location)
            if basename in PROJECT_SOURCES and len(project_frames) < 3:
                project_frames.append(location)
            traceback = traceback.tb_next
            walked += 1
        lines.extend(project_frames)
        lines.extend(frame for frame in frames if frame not in project_frames)
        if traceback is not None:
            lines.append("  additional frames omitted")
        for relation, related in (
            ("cause", exception.__cause__),
            ("context", exception.__context__),
        ):
            if related is not None and id(related) not in seen:
                pending.append((relation, related, related.__traceback__))
    if pending:
        lines.append("additional exception links omitted")
    return "\n".join(lines)


class SafeResult(unittest.TestResult):
    """Avoid unittest's raw traceback/message formatting, including for subtests."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__()

    def _exc_info_to_string(self, err: ExceptionInfo, test: unittest.TestCase) -> str:
        # TestResult routes errors, failures, subtests and expected failures through this hook.
        return exception_metadata(err)

    def printErrors(self) -> None:  # noqa: N802
        """The harness emits stored safe metadata, never unittest descriptions or values."""
