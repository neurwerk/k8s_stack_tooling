"""Export the reviewed standalone payload for the unchanged upstream image."""

from __future__ import annotations

import argparse
import sys
from importlib.resources import files
from pathlib import Path


def main() -> int:
    """Copy code only; this command never loads application credentials."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = files("k8s_stack_tooling.glitchtip").joinpath("payload.py").read_bytes()
        if args.output.exists():
            if args.output.is_symlink() or args.output.read_bytes() != payload:
                raise OSError("Existing payload does not match")
            return 0
        with args.output.open("xb") as output:
            output.write(payload)
        args.output.chmod(0o444)
    except OSError:
        print(
            "GlitchTip payload export failed; destination must be new and writable", file=sys.stderr
        )
        return 1
    return 0
