"""Capture short commands and stream cancellable validation without a shell."""

from __future__ import annotations

import codecs
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast


def _drain_output(
    pipe: BinaryIO, log: BinaryIO, recent: deque[str], errors: deque[str], *, verbose: bool
) -> None:
    """Log fixed-size byte chunks while retaining bounded, Unicode-safe excerpts."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    partial = ""
    truncated = False
    failed = False
    lookbehind = ""

    def consume(text: str, *, final: bool = False) -> None:
        nonlocal partial, truncated, failed, lookbehind
        if verbose:
            sys.stdout.write(text)
            sys.stdout.flush()
        # CR progress updates are records too. No fragment exceeds one input chunk.
        for part in [*re.split(r"([\r\n])", text), *(["\n"] if final else [])]:
            if part in ("\r", "\n"):
                marker = " ... [truncated] ... "
                record = (
                    partial[:1000] + marker + partial[-(1000 - len(marker)) :]
                    if truncated
                    else partial
                ).rstrip()
                if record:
                    recent.append(record)
                    if failed:
                        errors.append(record)
                partial, truncated, failed, lookbehind = "", False, False, ""
                continue
            search = lookbehind + part.lower()
            failed = failed or any(
                word in search for word in ("todo", "error", "fail", "exception")
            )
            lookbehind = search[-8:]
            partial += part
            if len(partial) > 2000:
                partial = partial[:1000] + partial[-1000:]
                truncated = True

    while chunk := os.read(pipe.fileno(), 8192):
        log.write(chunk)
        consume(decoder.decode(chunk))
    consume(decoder.decode(b"", final=True), final=True)


@dataclass(frozen=True)
class CommandResult:
    """Store one command execution result."""

    arguments: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Define the small command boundary used by the release orchestrator."""

    def run(self, arguments: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Execute arguments and return their result without raising."""

    def run_live(self, arguments: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Stream validation output, propagating cancellation after child cleanup."""


class SubprocessRunner:
    """Execute fixed argument vectors through the local process launcher."""

    def __init__(
        self, *, timeout: float = 1800, termination_grace: float = 2, verbose: bool = False
    ) -> None:
        """Set bounded execution and child-process shutdown deadlines."""
        self.timeout = timeout
        self.termination_grace = termination_grace
        self.verbose = verbose

    def run_live(self, arguments: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Keep private full logs and bounded failures, with optional live output."""
        with tempfile.NamedTemporaryFile(
            prefix="platform-release-check-", suffix=".log", delete=False
        ) as log:
            return self._logged(arguments, cwd, log)

    def _logged(self, arguments: Sequence[str], cwd: Path | None, log: object) -> CommandResult:
        """Run one cancellable process group while draining its output."""
        # The file is owned by run_live and closed even when cancellation propagates.
        output = cast(BinaryIO, log)
        recent: deque[str] = deque(maxlen=40)
        errors: deque[str] = deque(maxlen=20)
        drained = threading.Event()
        drain_errors: list[str] = []
        sys.stdout.write(f"Full validation log: {output.name}\n")

        def drain() -> None:
            try:
                _drain_output(
                    cast(BinaryIO, process.stdout), output, recent, errors, verbose=self.verbose
                )
            except OSError as error:
                drain_errors.append(f"validation output capture failed: {error}")
                self._terminate_group(process)
            finally:
                drained.set()

        try:
            process = subprocess.Popen(
                arguments,
                cwd=cwd,
                env=self._environment(),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            return CommandResult(tuple(arguments), 1, "", str(error))
        reader = threading.Thread(target=drain, name="platform-release-output", daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=self.timeout)
            reader.join(timeout=5)
            if not drained.is_set():
                self._terminate_group(process)
                drained.wait(timeout=5)
        except (KeyboardInterrupt, subprocess.TimeoutExpired) as error:
            self._terminate_group(process)
            if isinstance(error, KeyboardInterrupt):
                raise
            return CommandResult(tuple(arguments), 1, "", f"timed out after {self.timeout:g}s")
        finally:
            try:
                # An interrupted Thread.join can mark a still-running reader as
                # stopped on Python 3.12. Wait for its own completion signal.
                if not drained.is_set():
                    self._terminate_group(process)
                    drained.wait(timeout=5)
            finally:
                cast(BinaryIO, process.stdout).close()
        if code == -signal.SIGINT:
            self._terminate_group(process)
            raise KeyboardInterrupt
        code = code or int(bool(drain_errors))
        detail = "\n".join(dict.fromkeys([*errors, *recent, *drain_errors])) if code else ""
        return CommandResult(tuple(arguments), code, "", detail)

    def _terminate_group(self, process: subprocess.Popen[bytes]) -> None:
        """Stop only our new session, including descendants that outlive its leader."""
        deadline = time.monotonic() + self.termination_grace
        try:
            os.killpg(process.pid, signal.SIGTERM)
            while time.monotonic() < deadline:
                process.poll()
                os.killpg(process.pid, 0)
                time.sleep(0.05)
        except ProcessLookupError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    @staticmethod
    def _environment() -> dict[str, str]:
        """Keep captured and streamed commands under the same trust environment."""
        environment = os.environ.copy()
        environment["GH_HOST"] = "github.com"
        environment["GIT_NO_LAZY_FETCH"] = "1"
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        # Base's verifier independently checks this public trust anchor's fingerprint.
        environment["PLATFORM_RELEASE_ALLOWED_SIGNER"] = (
            'platform-release namespaces="git" ssh-ed25519 '
            "AAAAC3NzaC1lZDI1NTE5AAAAIOaoKMNPBk8+i23jqEmS7rwXso1HjEoe+8iDIXiJkLeD"
        )
        environment.pop("TAG", None)
        environment.pop("VIRTUAL_ENV", None)
        environment.pop("MAKEFLAGS", None)
        environment.pop("MAKEOVERRIDES", None)
        environment.pop("GNUMAKEFLAGS", None)
        environment.pop("PLATFORM_RELEASE_TEST_TAG", None)
        environment.pop("PLATFORM_RELEASE_PUBLIC_KEY_FILE", None)
        return environment

    def run(self, arguments: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Capture API and short command output without invoking a shell."""
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(tuple(arguments), 1, "", str(error))
        if (
            self.verbose
            and len(arguments) > 1
            and arguments[0] == "git"
            and arguments[1] in ("status", "diff", "log")
        ):
            sys.stdout.write(f"\n{' '.join(arguments)}\n{completed.stdout}{completed.stderr}\n")
        return CommandResult(
            arguments=tuple(arguments),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
