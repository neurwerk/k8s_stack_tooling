"""Check the receive-pack advertisement before forwarding the existing pre-push hook.

Executed as a standalone trusted script, not through the release checkout's imports.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Reject anything except the one authorized existing-ref fast-forward."""
    head, ref, base, original, *remote = sys.argv[1:]
    data = sys.stdin.buffer.read()
    if not isinstance(data, bytes):
        return 1
    if data.splitlines() != [f"{head} {head} {ref} {base}".encode()]:
        sys.stderr.write("remote release branch changed at push; retain local notes and reselect\n")
        return 1
    hook = Path(original)
    if hook.is_file() and os.access(hook, os.X_OK):
        # Preserve Git's arguments, byte-for-byte stdin, environment, cwd and exit status.
        result: subprocess.CompletedProcess[bytes] = subprocess.run(  # noqa: S603
            [str(hook), *remote], input=data, check=False
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
