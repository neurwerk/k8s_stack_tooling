import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from platform_release import commands as c


def test_cancel_during_join_after_leader_exits_stops_pipe_holding_child(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    script = f"""
import os, signal, time
from pathlib import Path
if os.fork():
    while not Path({str(ready)!r}).exists():
        time.sleep(0.01)
    os._exit(0)
signal.signal(signal.SIGTERM, lambda *args: (Path({str(stopped)!r}).write_text('stopped'), exit(0)))
Path({str(ready)!r}).write_text('ready')
time.sleep(60)
"""
    processes, logs, timers = [], [], []
    popen, temporary, join = subprocess.Popen, tempfile.NamedTemporaryFile, threading.Thread.join

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    def open_log(*args, **kwargs):
        log = temporary(*args, **kwargs)
        logs.append(log)
        return log

    def interrupt_join(thread, timeout=None):
        if thread.name == "platform-release-output" and not timers:
            assert processes[0].returncode == 0 and ready.exists()
            timer = threading.Timer(0.05, os.kill, args=(os.getpid(), signal.SIGINT))
            timers.append(timer)
            timer.start()
        return join(thread, timeout)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", open_log)
    monkeypatch.setattr(threading.Thread, "join", interrupt_join)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            c.SubprocessRunner(termination_grace=0.1).run_live((sys.executable, "-c", script))
        assert stopped.read_text() == "stopped"
        assert processes[0].stdout.closed and logs[0].closed
        assert time.monotonic() - started < 5
    finally:
        for timer in timers:
            join(timer, timeout=2)
        for process in processes:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_large_newline_free_record_has_complete_log_and_bounded_capture(capsys):
    size = 8 * 1024 * 1024
    script = (
        "import os; "
        "os.write(1, b'ERROR start '); "
        f"[os.write(1, b'x' * 8192) for _ in range({size // 8192})]; "
        "os.write(1, b' final evidence'); raise SystemExit(2)"
    )
    tracemalloc.start()
    try:
        result = c.SubprocessRunner().run_live((sys.executable, "-c", script))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    log = Path(capsys.readouterr().out.strip().removeprefix("Full validation log: "))
    assert log.read_bytes() == b"ERROR start " + b"x" * size + b" final evidence"
    assert result.returncode == 2 and len(result.stderr) <= 2000
    assert result.stderr.startswith("ERROR start ") and result.stderr.endswith(" final evidence")
    assert "[truncated]" in result.stderr
    assert peak < 2 * 1024 * 1024


def test_chunk_decoder_preserves_unicode_split_across_reads_and_final_invalid_bytes(
    monkeypatch, capsys
):
    chunks = iter(
        [b"ok \xe2", b"\x82", b"\xac\rERR", b"OR \xf0\x9f", b"\x8c\x90\nend \xe2\x82", b""]
    )
    requested = []

    def read(_fd, size):
        requested.append(size)
        return next(chunks)

    monkeypatch.setattr(os, "read", read)
    log, pipe = io.BytesIO(), type("Pipe", (), {"fileno": lambda _: 123})()
    recent, errors = deque(maxlen=40), deque(maxlen=20)
    c._drain_output(cast(BinaryIO, pipe), log, recent, errors, verbose=True)
    assert set(requested) == {8192}
    assert log.getvalue() == b"ok \xe2\x82\xac\rERROR \xf0\x9f\x8c\x90\nend \xe2\x82"
    assert capsys.readouterr().out == "ok \u20ac\rERROR \U0001f310\nend \ufffd"
    assert list(recent) == ["ok \u20ac", "ERROR \U0001f310", "end \ufffd"]
    assert list(errors) == ["ERROR \U0001f310"]


def test_carriage_return_progress_keeps_only_bounded_records(monkeypatch):
    remaining = 2000

    def read(_fd, size):
        nonlocal remaining
        assert size == 8192
        remaining -= 1
        return b"progress\r" * 900 if remaining >= 0 else b""

    monkeypatch.setattr(os, "read", read)

    # Count bytes without retaining the full synthetic log in memory.
    class Log:
        count = 0

        def write(self, value):
            self.count += len(value)

    log, pipe = Log(), type("Pipe", (), {"fileno": lambda _: 123})()
    recent, errors = deque(maxlen=40), deque(maxlen=20)
    c._drain_output(cast(BinaryIO, pipe), cast(BinaryIO, log), recent, errors, verbose=False)
    assert log.count == 2000 * 900 * len(b"progress\r")
    assert list(recent) == ["progress"] * 40 and not errors
