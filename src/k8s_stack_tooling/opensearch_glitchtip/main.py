"""Single-process worker; only fixed diagnostic codes and counts reach stdout."""

from __future__ import annotations

import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta

from .settings import MAX_PENDING, MAX_RECORDS, Settings, utc
from .state import State, state_lock
from .transport import AdapterError, Transport, WindowFullError

DELIVERY_BATCH_SIZE = 100
DELIVERY_CYCLE_ATTEMPTS = MAX_PENDING
DELIVERY_CYCLE_SECONDS = 30


def log(message: str) -> None:
    print(message, flush=True)


def deliver_due(
    state: State, transport: Transport, now: float, stop: threading.Event | None = None
) -> None:
    deadline = time.monotonic() + DELIVERY_CYCLE_SECONDS
    tried = 0
    while tried < DELIVERY_CYCLE_ATTEMPTS:
        if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
            return
        batch = state.due(now, min(DELIVERY_BATCH_SIZE, DELIVERY_CYCLE_ATTEMPTS - tried))
        if not batch:
            return
        for source, payload, attempts in batch:
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                return
            tried += 1
            try:
                transport.deliver(payload)
            except AdapterError as exc:
                state.defer(source, attempts, now)
                log(f"delivery_stalled attempts={attempts + 1} reason={exc}")
            else:
                state.acknowledge(source)


def run(settings: Settings, stop: threading.Event) -> None:
    scopes = settings.validate_runtime()
    initial = utc(settings.initial_start_at)
    with state_lock(settings.state_path):
        state = State(settings, scopes)
        transport: Transport | None = None
        try:
            transport = Transport(settings)
            while not stop.is_set():
                for scope in scopes:
                    if stop.is_set():
                        break
                    if state.pending_count() > MAX_PENDING - MAX_RECORDS:
                        log("poll_stalled reason=pending_outbox_limit")
                        break
                    cursor = state.cursor(scope, initial)
                    lower = max(initial, cursor - timedelta(seconds=settings.overlap_seconds))
                    target = datetime.now(UTC) - timedelta(seconds=settings.settle_seconds)
                    # Bounded catch-up; subsequent cycles move forward from committed progress.
                    target = min(target, max(cursor, initial) + timedelta(minutes=5))
                    if target <= lower:
                        continue
                    upper = min(target, lower + timedelta(minutes=5))
                    segments = 0
                    while not stop.is_set() and lower < target:
                        if segments >= 512 or state.pending_count() > MAX_PENDING - MAX_RECORDS:
                            log("poll_stalled reason=window_work_or_outbox_limit")
                            break
                        try:
                            records, labels = transport.window(scope, lower, upper)
                        except WindowFullError as exc:
                            if upper - lower <= timedelta(milliseconds=1):
                                log(
                                    "poll_stalled reason=same_timestamp_or_minimum_window_cap "
                                    f"detail={exc}"
                                )
                                break
                            upper = lower + (upper - lower) / 2
                            continue
                        except AdapterError as exc:
                            log(f"poll_stalled reason={exc}")
                            break
                        added = state.enqueue(scope, records, labels, upper)
                        log(f"window_committed occurrences={len(records)} queued={added}")
                        segments += 1
                        lower = upper
                        upper = min(target, lower + timedelta(minutes=5))
                deliver_due(state, transport, time.time(), stop)
                stop.wait(settings.poll_seconds)
        finally:
            if transport is not None:
                transport.close()
            state.close()


def main() -> int:
    os.umask(0o077)
    stop = threading.Event()

    def stopping(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    try:
        # Required BaseSettings fields are supplied by the process environment.
        run(Settings(), stop)  # ty: ignore[missing-argument]
    except Exception:
        # Validation and HTTP exception representations can contain secrets and raw records.
        log("adapter_fatal reason=invalid_configuration_state_or_unexpected_failure")
        return 1
    return 0
