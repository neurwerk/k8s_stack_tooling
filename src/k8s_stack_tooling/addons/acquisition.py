"""Explicit Git acquisition, separate from offline selection validation."""

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .git import run_git as _git
from .schema import ContractError, SourceRef, load_selection
from .sources import VerifiedSource, _verify_source
from .validation import inside, render


def _safe_cache(root: Path) -> None:
    """Reject foreign Git configuration before invoking Git in an existing cache."""
    gitdir = root / ".git"
    if not gitdir.is_dir() or gitdir.is_symlink():
        raise ContractError("acquisition cache must be a standalone Git checkout")
    if any(p.is_symlink() for p in gitdir.rglob("*")):
        raise ContractError("acquisition cache contains Git metadata symlinks")
    if any(
        (gitdir / p).exists()
        for p in ("objects/info/alternates", "info/grafts", "config.worktree", "commondir")
    ):
        raise ContractError("acquisition cache contains unsupported Git metadata")
    config = configparser.RawConfigParser(strict=True)
    try:
        config.read_string((gitdir / "config").read_text())
        allowed = {
            # Git probes the filesystem at init, including on macOS and Windows.
            "core": {
                "repositoryformatversion",
                "filemode",
                "bare",
                "logallrefupdates",
                "ignorecase",
                "precomposeunicode",
                "symlinks",
            },
            'remote "origin"': {"url"},
        }
        if config.defaults() or any(
            section not in allowed or not set(config[section]) <= allowed[section]
            for section in config.sections()
        ):
            raise ValueError
        if config.get("core", "bare") != "false":
            raise ValueError
        if config.get("core", "repositoryformatversion") != "0":
            raise ValueError
    except (OSError, UnicodeError, configparser.Error, ValueError):
        raise ContractError("acquisition cache has unsupported Git configuration") from None
    if _git(root, "for-each-ref", "--format=%(refname)", "refs/replace/"):
        raise ContractError("acquisition cache contains replacement objects")


def _verify(root: Path, reference: SourceRef, resource: dict, revision: str) -> VerifiedSource:
    if _git(root, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise ContractError("acquisition cache is dirty")
    # Resolve tags with the hardened offline Git runner, not ambient Git configuration.
    resolved = {**resource, "spec": {**resource["spec"], "ref": {"commit": revision}}}
    return _verify_source(root, reference, resolved, installable=True)


def acquire_source(
    reference: SourceRef,
    resource: dict,
    destination: Path,
    *,
    base: bool = False,
) -> VerifiedSource:
    """Acquire one source into a new cache directory, or verify an existing entry.

    ``resource`` is the rendered Flux object selected by ``reference``. Callers use
    the existing selection parser/rendering and pass every selected source; this
    function neither loads selection nor authorizes setup. Existing directories
    are never updated or removed. Choose a new destination for a changed revision.

    Network Git uses trusted caller configuration and SSH-agent authentication.
    Checkout/verification disable global configuration, hooks, and replacements.
    """
    spec = resource.get("spec", {})
    metadata = resource.get("metadata", {})
    if not isinstance(spec, dict) or not isinstance(metadata, dict):
        raise ContractError("invalid acquisition source object")
    if (resource.get("kind"), metadata.get("namespace"), metadata.get("name")) != (
        reference.kind,
        reference.namespace,
        reference.name,
    ):
        raise ContractError("acquisition source identity mismatch")
    url = spec.get("url", "")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in ("https", "ssh")
            and parsed.hostname
            and parsed.path
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.username is None or (parsed.scheme == "ssh" and parsed.username == "git"))
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ContractError("unsupported acquisition source URL")
    if any(spec.get(k) for k in ("ignore", "include", "sparseCheckout", "recurseSubmodules")):
        raise ContractError("filtered or composite source artifacts are unsupported")
    ref = spec.get("ref", {})
    if not isinstance(ref, dict):
        raise ContractError("selected source requires an immutable revision")
    if (
        set(ref) == {"commit"}
        and isinstance(ref["commit"], str)
        and re.fullmatch(r"[a-f0-9]{40}", ref["commit"])
    ):
        target = ref["commit"]
        tag = False
    elif base and set(ref) == {"tag"} and isinstance(ref["tag"], str):
        target = "refs/tags/" + ref["tag"]
        tag = True
    else:
        raise ContractError("selected source requires an immutable revision")
    destination = destination.absolute()
    if destination != destination.resolve():
        raise ContractError("acquisition destination must not contain symlinks")
    if destination.exists():
        _safe_cache(destination)
        if tag:
            _git(destination, "check-ref-format", target)
        revision = _git(
            destination, "rev-parse", "--verify", "--end-of-options", target + "^{commit}"
        )
        return _verify(destination, reference, resource, revision)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".addon-acquire-", dir=destination.parent))
    try:
        _git(temporary, "-c", "init.templateDir=", "init", "--quiet")
        _git(temporary, "config", "--local", "remote.origin.url", url)
        if tag:
            _git(temporary, "check-ref-format", target)
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(
            {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ALLOW_PROTOCOL": "https:ssh",
            }
        )
        # Only fetch uses caller Git configuration, for ordinary credential/SSH setup.
        # Never expose subprocess diagnostics, which may contain private URLs or tokens.
        subprocess.run(
            [
                "git",
                "--no-pager",
                "-C",
                str(temporary),
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                url,
                target + ":" + target if tag else target,
            ],
            env=environment,
            capture_output=True,
            timeout=180,
            check=True,
        )
        revision = _git(temporary, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        if not tag and revision != target:
            raise ContractError("acquired revision does not match source")
        _git(temporary, "-c", "core.hooksPath=" + os.devnull, "checkout", "--detach", revision)
        _safe_cache(temporary)
        verified = _verify(temporary, reference, resource, revision)
        # Do not replace even an empty directory created by another caller.
        destination.mkdir()
        try:
            for item in temporary.iterdir():
                item.rename(destination / item.name)
        except OSError:
            # Leave an interrupted owned cache for explicit inspection, never overwrite it.
            raise ContractError("cannot publish acquired source cache") from None
        return verified
    except (OSError, subprocess.SubprocessError):
        raise ContractError("cannot acquire selected Git source") from None
    finally:
        shutil.rmtree(temporary)


def _requested_sources(
    client: Path, *, cluster_dir: str, cache: Path
) -> list[tuple[str, SourceRef, dict, Path, bool]]:
    """Resolve the complete selection and deterministic cache paths without Git."""
    client = client.resolve()
    selection = load_selection(inside(client, "config/addons.yaml"))
    sources = {}
    for resource in render(inside(client, cluster_dir), root=client):
        if resource.get("kind") != "GitRepository":
            continue
        reference = SourceRef.model_validate(
            {
                "kind": resource["kind"],
                "namespace": resource.get("metadata", {}).get("namespace"),
                "name": resource.get("metadata", {}).get("name"),
            }
        )
        key = (reference.kind, reference.namespace, reference.name)
        if key in sources:
            raise ContractError("duplicate source identity")
        sources[key] = resource
    selected = [("base", selection.base_source_ref, True)] + [
        (item.id, item.source_ref, False) for item in selection.addons
    ]
    # Resolve the entire selection before any acquisition, including missing sources.
    requested = []
    for identifier, reference, is_base in selected:
        key = (reference.kind, reference.namespace, reference.name)
        if key not in sources:
            raise ContractError("selected namespaced source is missing")
        resource = sources[key]
        identity = {
            "sourceRef": reference.model_dump(by_alias=True),
            "url": resource.get("spec", {}).get("url"),
            "ref": resource.get("spec", {}).get("ref"),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        requested.append((identifier, reference, resource, cache.absolute() / digest, is_base))
    return requested


def source_checkouts(
    client: Path, *, cluster_dir: str, cache: Path
) -> tuple[Path, dict[str, Path]]:
    """Discover existing cache directories offline; this grants no setup authority.

    Call ``verify_selection`` separately to verify sources, cleanliness, and the
    staged projection. Discovery never fetches, creates, or updates a checkout.
    """
    requested = _requested_sources(client, cluster_dir=cluster_dir, cache=cache)
    for _, _, _, destination, _ in requested:
        if destination != destination.resolve() or not destination.is_dir():
            raise ContractError("selected source cache directory is missing or unsafe")
    return requested[0][3], {
        identifier: destination
        for identifier, _, _, destination, is_base in requested
        if not is_base
    }


def acquire_selection(client: Path, *, cluster_dir: str, cache: Path) -> dict:
    """Acquire exactly the parsed selection; return paths for offline validation.

    Cache keys bind namespaced source identity, URL and selected immutable ref.
    Tags retain their first verified resolution in that cache; this is acquisition,
    not tag-signature verification or release approval.
    """
    requested = _requested_sources(client, cluster_dir=cluster_dir, cache=cache)
    base_result = {}
    addon_results = []
    for identifier, reference, resource, destination, is_base in requested:
        verified = acquire_source(reference, resource, destination, base=is_base)
        row = {"id": identifier, "path": str(destination), "revision": verified.revision}
        if is_base:
            base_result = row
        else:
            addon_results.append(row)
    return {"base": base_result, "addons": addon_results}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ContractError("invalid acquisition arguments")


def main(argv: list[str] | None = None) -> int:
    """CLI acquisition emits JSON paths/revisions, never source URLs or diagnostics."""
    parser = _Parser(prog="addon-acquire", description="Acquire selected immutable Git sources")
    parser.add_argument("--client", type=Path, required=True)
    parser.add_argument("--cluster-dir", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        result = acquire_selection(args.client, cluster_dir=args.cluster_dir, cache=args.cache)
    except (ContractError, OSError, ValueError, subprocess.SubprocessError):
        print(
            "Cannot acquire selected sources; check client configuration and Git access.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
