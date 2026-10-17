"""Stage provisioning scripts for the existing DocumentDB client image."""

from importlib.resources import files
from pathlib import Path


def main() -> int:
    """An init container stages scripts; the pinned DocumentDB image runs them."""
    destination = Path("/runtime")
    for name in ("provision.sh", "provision.js", "verify.js"):
        source = files("k8s_stack_tooling.postgres").joinpath(name)
        (destination / name).write_bytes(source.read_bytes())
    return 0
