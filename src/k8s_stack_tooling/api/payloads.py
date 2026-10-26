"""Stage declared inert resources, without importing or executing their code."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

DEFAULT_DESTINATION = Path("/work/payloads")
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_MAX_BYTES = 1_048_576


class PayloadError(ValueError):
    """Redacted staging failure; previously staged files may remain."""


def _parts(resource: str) -> tuple[str, ...]:
    parts = resource.split("/")
    if (
        not resource
        or "\\" in resource
        or "\0" in resource
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(resource).is_absolute()
    ):
        raise PayloadError("Resource paths must be confined relative paths")
    return tuple(parts)


def _directory(path: Path, *, create: bool) -> int:
    if ".." in path.parts:
        raise PayloadError("Directory traversal is not allowed")
    absolute = path.absolute()
    fd = os.open("/", _DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _parent(root: int, parts: tuple[str, ...], *, create: bool) -> int:
    fd = os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(parent: int, name: str, *, staged: bool = False) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            raise PayloadError("Payload must be a bounded regular file")
        if staged and (info.st_nlink != 1 or info.st_mode & 0o222):
            raise PayloadError("Existing payload must be read-only and unlinked elsewhere")
        content = stream.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise PayloadError("Payload exceeds size limit")
        return content


def stage_payloads(
    source: Path,
    resources: Sequence[str],
    *,
    destination: Path = DEFAULT_DESTINATION,
) -> tuple[Path, ...]:
    """Copy declared files from an explicitly supplied package resource directory.

    Callers can use importlib.resources.as_file on their package's payloads
    directory; this helper never imports a package or payload. Paths retain their
    relative layout. Each file is at most 1 MiB, newly created with mode 0444;
    existing identical read-only files are accepted. Symlinks in either tree are
    rejected using directory-relative no-follow opens, including parent paths.
    Staging is not a multi-file transaction; a failed write is not silently retried.
    """
    if not resources or len(resources) > 128 or len(set(resources)) != len(resources):
        raise PayloadError("Declare between one and 128 unique resources")
    paths = [_parts(resource) for resource in resources]
    try:
        source_fd = _directory(source, create=False)
        try:
            contents = []
            for parts in paths:
                parent = _parent(source_fd, parts, create=False)
                try:
                    contents.append(_read(parent, parts[-1]))
                finally:
                    os.close(parent)
        finally:
            os.close(source_fd)
        target_fd = _directory(destination, create=True)
        try:
            for parts, content in zip(paths, contents, strict=True):
                parent = _parent(target_fd, parts, create=True)
                try:
                    try:
                        fd = os.open(
                            parts[-1],
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o444,
                            dir_fd=parent,
                        )
                    except FileExistsError:
                        if _read(parent, parts[-1], staged=True) != content:
                            raise PayloadError("Existing payload does not match") from None
                    else:
                        with os.fdopen(fd, "wb") as output:
                            output.write(content)
                            output.flush()
                            os.fchmod(output.fileno(), 0o444)
                finally:
                    os.close(parent)
        finally:
            os.close(target_fd)
    except OSError:
        raise PayloadError("Could not stage declared payloads") from None
    return tuple(destination.joinpath(*parts) for parts in paths)
