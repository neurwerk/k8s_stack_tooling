"""Fixed workstation adapters for the existing Tooling and standalone setup tag workflows."""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

from platform_release.commands import CommandRunner
from platform_release.main import (
    DEFAULT_EXPECTED_SIGNER_FINGERPRINT,
    SHA_PATTERN,
    Prompt,
    ReleaseError,
    Repository,
    _checked,
    _clean_target,
    _confirm,
    _create_signed_tag,
    _fetch_target,
    _gh,
    _ref_exists,
    _remote_ref,
    _repository,
    _require_signing_key,
    _tag,
)


@dataclass(frozen=True)
class PythonRelease:
    """One of the two fixed distributions, bound to committed source rather than local edits."""

    target: str
    commit: str
    version: str
    tag: str
    distribution: str
    directory: str
    workflow: str


def committed_toml(
    runner: CommandRunner, repo: Repository, commit: str, path: str
) -> dict[str, Any]:
    """Read nonsecret package metadata from an exact local Git commit."""
    content = _checked(runner, ("git", "show", f"{commit}:{path}"), cwd=repo.path)
    return tomllib.loads(content)


def release_spec(
    runner: CommandRunner, repo: Repository, target: str, commit: str
) -> PythonRelease:
    """Select the existing workflow and require matching committed project/lock versions."""
    if target not in {"tooling", "setup"} or not SHA_PATTERN.fullmatch(commit):
        raise ReleaseError("select tooling/setup and an exact full commit SHA")
    directory = "." if target == "tooling" else "cli_tools/openbao_stack_setup"
    name = "k8s-stack-tooling" if target == "tooling" else "openbao-stack-setup"
    project = committed_toml(runner, repo, commit, f"{directory}/pyproject.toml")["project"]
    version = project["version"]
    version_tag = _tag(version)
    lock = committed_toml(runner, repo, commit, f"{directory}/uv.lock")
    packages = [row for row in lock["package"] if row["name"] == name]
    if project["name"] != name or len(packages) != 1 or packages[0]["version"] != version:
        raise ReleaseError("committed distribution name, version and lock do not agree")
    return PythonRelease(
        target,
        commit,
        version,
        version_tag if target == "tooling" else f"stack-setup-{version_tag}",
        name,
        directory,
        "build-push.yaml" if target == "tooling" else "stack-setup-release.yaml",
    )


def reviewed_checkout(
    runner: CommandRunner, repo: Repository, target: str, commit: str, root: Path | None
) -> Repository:
    """Reuse the hardened fetch, preserving the original checkout and immutable reviewed SHA."""
    if not SHA_PATTERN.fullmatch(commit):
        raise ReleaseError("--commit must be the reviewed full 40-character commit SHA")
    tip = _remote_ref(runner, repo, f"refs/heads/{repo.default_branch}")
    _fetch_target(runner, repo, tip)
    _checked(runner, ("git", "merge-base", "--is-ancestor", commit, tip), cwd=repo.path)
    common = Path(
        _checked(
            runner,
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=repo.path,
        )
    ).resolve()
    parent = (root or common.parent.parent / ".worktrees").expanduser().resolve()
    if parent.is_relative_to(common.parent) or parent.is_relative_to(repo.path):
        raise ReleaseError("release worktree root must be outside the repository")
    path = parent / f"release-manager-{target}-{commit}"
    checkout = Repository(path, repo.slug, repo.default_branch)
    if path.exists() or path.is_symlink():
        registered = _checked(runner, ("git", "worktree", "list", "--porcelain"), cwd=repo.path)
        if path.is_symlink() or f"worktree {path}\n" not in registered + "\n":
            raise ReleaseError("release worktree destination is occupied; nothing was replaced")
        actual = _checked(
            runner, ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=path
        )
        if Path(actual).resolve() != common:
            raise ReleaseError("release checkout belongs to another repository")
    else:
        parent.mkdir(parents=True, exist_ok=True)
        _checked(runner, ("git", "worktree", "add", "--detach", str(path), commit), cwd=repo.path)
    _clean_target(runner, checkout, commit)
    print(f"Reviewed release checkout: {path}")
    return checkout


def required_ci(runner: CommandRunner, repo: Repository, commit: str) -> None:
    """Require the actual full-repository GitHub Actions aggregate on this exact commit."""
    response = json.loads(
        _checked(
            runner, ("gh", "api", f"repos/{repo.slug}/commits/{commit}/check-runs"), cwd=repo.path
        )
    )
    checks = [
        row
        for row in response["check_runs"]
        if row["name"] == "Required CI" and row.get("app", {}).get("slug") == "github-actions"
    ]
    if not checks:
        raise ReleaseError("exact reviewed commit has no GitHub Actions Required CI result")
    latest = max(checks, key=lambda row: row["id"])
    if latest["status"] != "completed" or latest["conclusion"] != "success":
        raise ReleaseError("Required CI has not passed at the exact reviewed commit")


def check_python(runner: CommandRunner, repo: Repository, release: PythonRelease) -> None:
    """Run the existing target quality commands and require the full repository CI gate."""
    workflow = f".github/workflows/{release.workflow}"
    _checked(runner, ("git", "cat-file", "-e", f"{release.commit}:{workflow}"), cwd=repo.path)
    directory = repo.path / release.directory
    if (repo.path / "Makefile").is_file():
        _checked(runner, ("make", "check"), cwd=repo.path, live=True)
    else:
        for command in (
            ("uv", "lock", "--check"),
            ("uv", "sync", "--frozen", "--dev"),
            ("uv", "run", "--frozen", "ruff", "check", "."),
            ("uv", "run", "--frozen", "ruff", "format", "--check", "."),
            ("uv", "run", "--frozen", "ty", "check"),
            ("uv", "run", "--frozen", "pytest"),
            ("uv", "build"),
        ):
            _checked(runner, command, cwd=directory, live=True)
    if release.target == "setup":
        published_sdk(runner, repo, release)
    required_ci(runner, repo, release.commit)
    _clean_target(runner, repo, release.commit)
    print("Target checks and exact-commit full repository Required CI passed.")


def _release_metadata(runner: CommandRunner, repo: Repository, tag: str) -> dict[str, Any] | None:
    result = runner.run(("gh", "api", f"repos/{repo.slug}/releases/tags/{tag}"), cwd=repo.path)
    if result.returncode:
        if "HTTP 404" in result.stderr:
            return None
        raise ReleaseError("could not inspect the exact GitHub Release; absence is not established")
    return cast(dict[str, Any], json.loads(result.stdout))


def status_python(runner: CommandRunner, repo: Repository, release: PythonRelease) -> None:
    """Show exact target/tag and its publication state without fetching or mutation."""
    remote = _remote_ref(runner, repo, f"refs/tags/{release.tag}")
    metadata = _release_metadata(runner, repo, release.tag)
    print(f"Target: {release.target}\nRepository: {repo.slug}\nReviewed commit: {release.commit}")
    print(f"Version: {release.version}\nTag: {release.tag}\nRemote tag: {remote or 'absent'}")
    print(f"Workflow: .github/workflows/{release.workflow}")
    print(f"GitHub Release: {metadata['html_url'] if metadata else 'not published'}")
    print("Tag/release presence is not verification; choose verify to check signature and assets.")


def _public_key(runner: CommandRunner, key: Path) -> str:
    key = key.expanduser()
    if key.suffix != ".pub" or key.is_symlink() or not key.is_file():
        raise ReleaseError("select an existing public .pub key, never a private key")
    text = key.read_text().strip()
    if not text.startswith("ssh-") or "\n" in text:
        raise ReleaseError("signing file must contain one SSH public key")
    fingerprint = _checked(runner, ("ssh-keygen", "-lf", str(key), "-E", "sha256"))
    if (
        len(fingerprint.split()) < 2
        or fingerprint.split()[1] != DEFAULT_EXPECTED_SIGNER_FINGERPRINT
    ):
        raise ReleaseError("public key is not the approved release signer")
    return text


def verify_tag(
    runner: CommandRunner,
    repo: Repository,
    release: PythonRelease,
    public_key: Path,
    *,
    remote_required: bool = True,
) -> str:
    """Verify the annotated object's exact commit and approved SSH signature, never retag."""
    public = _public_key(runner, public_key)
    tag_object = _checked(runner, ("git", "rev-parse", f"{release.tag}^{{tag}}"), cwd=repo.path)
    commit = _checked(runner, ("git", "rev-parse", f"{release.tag}^{{commit}}"), cwd=repo.path)
    if commit != release.commit:
        raise ReleaseError("existing tag targets a different commit; never move or replace it")
    remote = _remote_ref(runner, repo, f"refs/tags/{release.tag}")
    if (remote_required and not remote) or (remote and remote != tag_object):
        raise ReleaseError(
            "remote signed tag differs or is absent; refusing publication/verification"
        )
    with tempfile.TemporaryDirectory(prefix="release-manager-signers-") as directory:
        allowed = Path(directory) / "allowed-signers"
        allowed.write_text(f"release-manager {public}\n")
        _checked(
            runner,
            (
                "git",
                "-c",
                "gpg.format=ssh",
                "-c",
                f"gpg.ssh.allowedSignersFile={allowed}",
                "verify-tag",
                release.tag,
            ),
            cwd=repo.path,
        )
    return tag_object


def _assets(
    runner: CommandRunner, repo: Repository, tag: str, distribution: str, version: str
) -> dict[str, str]:
    metadata = _release_metadata(runner, repo, tag)
    if (
        metadata is None
        or metadata["draft"]
        or metadata["prerelease"]
        or metadata["tag_name"] != tag
    ):
        raise ReleaseError("exact non-draft stable GitHub Release is not available")
    stem = distribution.replace("-", "_")
    names = (f"{stem}-{version}-py3-none-any.whl", f"{stem}-{version}.tar.gz")
    for name in (*names, "SHA256SUMS"):
        if sum(row["name"] == name for row in metadata["assets"]) != 1:
            raise ReleaseError("release has a missing or duplicate required distribution asset")
    with tempfile.TemporaryDirectory(prefix="release-manager-assets-") as directory:
        root = Path(directory)
        for name in (*names, "SHA256SUMS"):
            _gh(runner, repo, "release", "download", tag, "--pattern", name, "--dir", str(root))
        expected = _checksum_file(root / "SHA256SUMS", names)
        for name in names:
            if (root / name).stat().st_size > 100 * 1024 * 1024:
                raise ReleaseError("published distribution exceeds its 100 MiB bound")
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected[name]:
                raise ReleaseError("published distribution checksum does not match SHA256SUMS")
        _distribution_metadata(root, names, distribution, version)
        return expected


def _distribution_metadata(
    root: Path, names: tuple[str, str], distribution: str, version: str
) -> None:
    stem = distribution.replace("-", "_")
    try:
        with zipfile.ZipFile(root / names[0]) as wheel:
            payload = wheel.read(f"{stem}-{version}.dist-info/METADATA")
        with tarfile.open(root / names[1]) as source:
            stream = source.extractfile(f"{stem}-{version}/PKG-INFO")
            if stream is None:
                raise ReleaseError("published source archive lacks package metadata")
            source_metadata = stream.read(1_048_577)
    except (tarfile.TarError, zipfile.BadZipFile):
        raise ReleaseError("published distribution archive is invalid") from None
    for content in (payload, source_metadata):
        if len(content) > 1_048_576:
            raise ReleaseError("published distribution metadata exceeds its bound")
        message = BytesParser().parsebytes(content)
        if (message["Name"], message["Version"]) != (distribution, version):
            raise ReleaseError("published distribution metadata does not match its release")


def _checksum_file(path: Path, names: tuple[str, str]) -> dict[str, str]:
    if path.stat().st_size > 4096:
        raise ReleaseError("published checksum manifest exceeds its bound")
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None or match[2] not in names or match[2] in result:
            raise ReleaseError("published checksum manifest has unexpected or duplicate entries")
        result[match[2]] = match[1]
    if set(result) != set(names):
        raise ReleaseError("published checksum manifest omits a required distribution")
    return result


def published_sdk(runner: CommandRunner, repo: Repository, release: PythonRelease) -> None:
    """Require setup's final lock to bind the actual published SDK wheel and checksum."""
    lock = committed_toml(runner, repo, release.commit, f"{release.directory}/uv.lock")
    sdk = [row for row in lock["package"] if row["name"] == "k8s-stack-tooling"]
    if len(sdk) != 1:
        raise ReleaseError("setup requires a final published SDK dependency lock")
    version = sdk[0]["version"]
    tag = _tag(version)
    wheel = f"k8s_stack_tooling-{version}-py3-none-any.whl"
    url = f"https://github.com/{repo.slug}/releases/download/{tag}/{wheel}"
    if sdk[0]["source"] != {"url": url}:
        raise ReleaseError("setup SDK source must be its canonical published release wheel URL")
    checksums = _assets(runner, repo, tag, "k8s-stack-tooling", version)
    if not any(
        row.get("url") == url and row.get("hash") == f"sha256:{checksums[wheel]}"
        for row in sdk[0].get("wheels", [])
    ):
        raise ReleaseError("setup SDK lock hash does not match the published wheel")


def _workflow(runner: CommandRunner, repo: Repository, release: PythonRelease) -> None:
    runs = json.loads(
        _gh(
            runner,
            repo,
            "run",
            "list",
            "--workflow",
            release.workflow,
            "--commit",
            release.commit,
            "--event",
            "push",
            "--limit",
            "100",
            "--json",
            "databaseId,headSha,headBranch,status,conclusion,url",
        )
    )
    matching = [
        row for row in runs if row["headSha"] == release.commit and row["headBranch"] == release.tag
    ]
    if not matching:
        raise ReleaseError("no publication workflow matches this exact tag and commit")
    latest = max(matching, key=lambda row: row["databaseId"])
    if latest["status"] != "completed" or latest["conclusion"] != "success":
        raise ReleaseError(f"publication is not successful: {latest['url']}")


def _image(runner: CommandRunner, release: PythonRelease) -> None:
    image = f"ghcr.io/neurwerk/k8s-stack-tooling:{release.version}"
    manifest = json.loads(
        _checked(
            runner,
            (
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image,
                "--format",
                "{{json .Manifest}}",
            ),
        )
    )
    digest = manifest["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ReleaseError("published Tooling image digest is invalid")
    config = json.loads(
        _checked(
            runner,
            (
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                f"ghcr.io/neurwerk/k8s-stack-tooling@{digest}",
                "--format",
                "{{json .Image}}",
            ),
        )
    )
    labels = config["config"]["Labels"]
    if (
        config["os"] != "linux"
        or config["architecture"] != "amd64"
        or any(
            labels.get(f"org.opencontainers.image.{key}") != value
            for key, value in {
                "revision": release.commit,
                "version": release.version,
                "source": "https://github.com/neurwerk/k8s_stack_tooling",
            }.items()
        )
    ):
        raise ReleaseError(
            "published image architecture or source/revision/version labels disagree"
        )
    print(f"Verified image: {image}@{digest}")


def verify_python(
    runner: CommandRunner, repo: Repository, release: PythonRelease, public_key: Path
) -> None:
    """Verify immutable tag, successful workflow, checksummed assets and effective image."""
    verify_tag(runner, repo, release, public_key)
    _workflow(runner, repo, release)
    _assets(runner, repo, release.tag, release.distribution, release.version)
    if release.target == "tooling":
        _image(runner, release)
    else:
        published_sdk(runner, repo, release)
    print(f"Verified {repo.slug} {release.tag} at {release.commit}; no deployment performed.")


def publish_python(
    runner: CommandRunner,
    repo: Repository,
    release: PythonRelease,
    public_key: Path,
    prompt: Prompt,
    confirmation: str | None,
) -> None:
    """Run all gates, confirm once, then push only one immutable signed tag object."""
    print(f"Publish target: {repo.slug} {release.tag} at reviewed commit {release.commit}")
    if _remote_ref(runner, repo, f"refs/tags/{release.tag}"):
        verify_tag(runner, repo, release, public_key)
        raise ReleaseError(
            "tag already exists unchanged; inspect its workflow or use verify, never retag"
        )
    _public_key(runner, public_key)
    _require_signing_key(runner, public_key)
    check_python(runner, repo, release)
    _confirm(prompt, "PUBLISH", repo, release.tag, release.commit, confirmation)
    _clean_target(runner, repo, release.commit)
    if _repository(runner, repo.path) != repo:
        raise ReleaseError("repository identity changed while checking or confirming")
    if _remote_ref(runner, repo, f"refs/tags/{release.tag}"):
        raise ReleaseError("tag appeared during checks; inspect it instead of retrying publication")
    if _ref_exists(runner, repo, f"refs/tags/{release.tag}"):
        object_id = verify_tag(runner, repo, release, public_key, remote_required=False)
    else:
        object_id = _create_signed_tag(
            runner,
            repo,
            release.tag,
            public_key,
            release.commit,
            message=f"{release.distribution} {release.tag}",
        )
        verify_tag(runner, repo, release, public_key, remote_required=False)
    _checked(
        runner,
        (
            "git",
            "push",
            "--no-follow-tags",
            "origin",
            f"{object_id}:refs/tags/{release.tag}",
        ),
        cwd=repo.path,
    )
    print(
        f"Signed tag pushed; {release.workflow} performs publication. Choose status, then verify."
    )
