"""Isolated workstation Docling process."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from docling_tester.transport import ProtocolError, Reply, strict_json


async def convert(
    path: Path, context: str, container: str, timeout: float, trace: dict[str, Any]
) -> Reply:
    if not context or not container or context.startswith("-") or container.startswith("-"):
        raise ValueError("Configure Docker context and container")
    start = time.perf_counter()
    trace.update(phase="local_conversion", remote_job_may_continue=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "docling_tester.local_worker",
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await process.communicate(
            json.dumps(
                {
                    "path": str(path),
                    "context": context,
                    "container": container,
                    "timeout": timeout,
                }
            ).encode()
        )
    except BaseException:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode != 0:
        raise ProtocolError("local_conversion_failed")
    value = strict_json(output)
    trace["remote_job_may_continue"] = (
        value.get("remote_job_may_continue", False) if isinstance(value, dict) else True
    )
    trace["local_conversion_seconds"] = time.perf_counter() - start
    if (
        not isinstance(value, dict)
        or value.get("status") != "success"
        or value.get("errors") != []
        or not isinstance(value.get("document"), dict)
        or not isinstance(value["document"].get("json_content"), dict)
    ):
        raise ProtocolError("partial_or_invalid_local_result")
    return Reply(200, value, trace["local_conversion_seconds"])
