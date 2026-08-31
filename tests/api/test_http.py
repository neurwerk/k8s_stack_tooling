"""Tests for shared HTTP retry behavior."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_stack_tooling.api.http import wait_for_service


def test_wait_for_service_obeys_wall_clock_deadline() -> None:
    """Cap request and sleep durations by the remaining retry budget."""
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        now += duration

    with (
        patch("k8s_stack_tooling.api.http.time.monotonic", side_effect=monotonic),
        patch("k8s_stack_tooling.api.http.time.sleep", side_effect=sleep),
        patch("k8s_stack_tooling.api.http.request", return_value=(503, None)) as request,
        pytest.raises(SystemExit),
    ):
        wait_for_service("https://service/ready", timeout=12, interval=5)

    assert [call.kwargs["timeout"] for call in request.call_args_list] == [12, 7, 2]
    assert now == 12
