"""Verified HTTP range downloads independent of the installed HF CLI version."""

from __future__ import annotations

import hashlib
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, override

from inference_runtime_manager.downloader.errors import HuggingFaceCommandError, IntegrityError


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    @override
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected is not None
            and urllib.parse.urlsplit(req.full_url)[:2] != urllib.parse.urlsplit(newurl)[:2]
        ):
            redirected.remove_header("Authorization")
        return redirected


def headers(environment: dict[str, str]) -> dict[str, str]:
    token = environment.get("HF_TOKEN", "")
    if not token:
        home = Path(environment.get("HF_HOME", Path.home() / ".cache/huggingface"))
        token_path = Path(environment.get("HF_TOKEN_PATH", str(home / "token")))
        if token_path.is_file():
            token = token_path.read_text().strip()
    result = {"Accept-Encoding": "identity"}
    if token:
        result["Authorization"] = f"Bearer {token}"
    return result


def metadata(source: str, revision: str, environment: dict[str, str]) -> list[dict[str, Any]]:
    url = f"https://huggingface.co/api/models/{source}/revision/{revision}?blobs=true"
    try:
        request = urllib.request.Request(url, headers=headers(environment))
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
            info = json.load(response)
        if info["sha"] != revision:
            raise IntegrityError("Hub metadata does not match the pinned revision")
        return info["siblings"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        raise HuggingFaceCommandError("Cannot read pinned model file metadata") from error


def _valid(path: Path, size: int, digest: str, git_blob: bool) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    checksum = hashlib.sha1(usedforsecurity=False) if git_blob else hashlib.sha256()
    if git_blob:
        checksum.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest() == digest


def download_file(
    url: str,
    destination: Path,
    size: int,
    digest: str,
    git_blob: bool,
    environment: dict[str, str],
) -> None:
    if (
        type(size) is not int
        or size < 0
        or len(digest) != (40 if git_blob else 64)
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise IntegrityError("Invalid Hub file size or digest")
    if _valid(destination, size, digest, git_blob):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    cache = destination.parent / ".cache" / "inference-runtime-manager"
    cache.mkdir(parents=True, exist_ok=True)
    partial = cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{digest}.partial"
    if size == 0 and not partial.exists():
        partial.touch()
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size or (offset == size and not _valid(partial, size, digest, git_blob)):
        partial.unlink()
        offset = 0
    if offset < size:
        request_headers = headers(environment)
        if offset:
            request_headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.build_opener(SafeRedirect()).open(request, timeout=60) as response:
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise IntegrityError("Unexpected content encoding for model download")
                if offset:
                    expected = f"bytes {offset}-{size - 1}/{size}"
                    if response.status != 206 or response.headers.get("Content-Range") != expected:
                        raise HuggingFaceCommandError(
                            "Server did not honor the requested download range"
                        )
                elif response.status != 200:
                    raise HuggingFaceCommandError("Unexpected model download response")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != size - offset:
                    raise HuggingFaceCommandError(
                        "Model response size differs from pinned metadata"
                    )
                with partial.open("ab") as out:
                    while chunk := response.read(1024 * 1024):
                        out.write(chunk)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise HuggingFaceCommandError(
                "Model transfer interrupted; partial bytes retained"
            ) from error
    if not _valid(partial, size, digest, git_blob):
        if partial.exists() and partial.stat().st_size < size:
            raise HuggingFaceCommandError("Model transfer incomplete; partial bytes retained")
        partial.unlink(missing_ok=True)
        raise IntegrityError("Downloaded model file failed digest verification")
    partial.replace(destination)
