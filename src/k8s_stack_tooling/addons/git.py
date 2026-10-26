"""One offline Git runner for selected-source and prerequisite verification."""

import os
import subprocess
from pathlib import Path

from .schema import ContractError


def run_git(root: Path, *args: str, strip: bool = True) -> str:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    try:
        output = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", "-C", str(root), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        return output.strip() if strip else output
    except (OSError, subprocess.SubprocessError):
        raise ContractError("cannot verify selected Git source") from None
