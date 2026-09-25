"""Play local WAV files during interactive inference workflows."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def play(path: Path) -> None:
    player = shutil.which("afplay") if sys.platform == "darwin" else shutil.which("ffplay")
    if player is None:
        raise ValueError("No supported audio player found (`afplay` or `ffplay`)")
    command = [player, str(path)]
    if Path(player).name == "ffplay":
        command = [player, "-nodisp", "-autoexit", "-loglevel", "error", str(path)]
    subprocess.run(command, check=True)
