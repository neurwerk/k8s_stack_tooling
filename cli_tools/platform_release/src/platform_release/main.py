"""Provide a fail-closed command line interface for platform release workflows."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, cast

import questionary

from platform_release.commands import CommandRunner, SubprocessRunner

DEFAULT_EXPECTED_SIGNER_FINGERPRINT = "SHA256:+rDcofrsfRE3ElJJxnUVoB3gmoEzZJUrisDqLZMHimw"
SEMVER_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
POLL_ATTEMPTS = 30
POLL_SECONDS = 2


class ReleaseError(RuntimeError):
    """Raised when release preconditions or an external command fail."""


class StaleTargetError(ReleaseError):
    """Require fresh validation and authorization after the remote tip moves."""


class Prompt(Protocol):
    """Define the interactive input boundary."""

    def ask(self, message: str) -> str:
        """Return an operator response to a prompt."""


class TerminalPrompt:
    """Read one line of input from the operator terminal."""

    def ask(self, message: str) -> str:
        """Prompt the operator and return their stripped response."""
        response = questionary.text(message).ask()
        if not isinstance(response, str):
            raise ReleaseError("cancelled")
        return cast(str, response).strip()


@dataclass(frozen=True)
class Repository:
    """Describe the validated public Base repository."""

    path: Path
    slug: str
    default_branch: str


@dataclass(frozen=True)
class Status:
    """Store the read-only state used by status and plan."""

    repository: str
    path: str
    default_branch: str
    branch: str
    commit: str
    clean: bool
    version: str
    tag: str
    next_patch: str
    tag_exists: bool
    staging_tag_exists: bool
    latest_published: str
    remote_default_commit: str
    head_sync: str
    sync_note: str


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the platform-release command line."""
    parser = argparse.ArgumentParser(prog="platform-release")
    parser.add_argument("--base-repo", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--worktree-root", type=Path, help="Parent for isolated release checkouts.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show live validation output as well as private logs.",
    )
    parser.add_argument(
        "--allow-local-preparation",
        action="store_true",
        help="Authorize origin fetch and isolated worktree creation without a local prompt.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("guide", help="Show the five release steps.")
    for name in ("status", "continue", "merge"):
        command = commands.add_parser(
            name, help="Inspect a release; continue waits for final publication."
        )
        command.add_argument("--tag", help="Exact release target, independent of checkout VERSION.")
        views = command.add_mutually_exclusive_group()
        views.add_argument(
            "--diagnostics", action="store_true", help="Show full inspection details."
        )
        views.add_argument(
            "--changelog", action="store_true", help="Show the GitHub Release notes."
        )
    check = commands.add_parser(
        "check", help="Check an open release PR and offer confirmed release-file corrections."
    )
    targets = check.add_mutually_exclusive_group()
    targets.add_argument("--tag", help="Select the open release PR for this exact tag.")
    targets.add_argument("--pr", type=int, help="Select a same-repository open release PR number.")
    notes = commands.add_parser(
        "finish-notes",
        help="Guide release prose in a separate worktree, then optionally update its PR.",
    )
    notes_targets = notes.add_mutually_exclusive_group()
    notes_targets.add_argument("--tag")
    notes_targets.add_argument("--pr", type=int)
    parser.add_argument(
        "--public-check",
        type=Path,
        help="Trusted public-pr-check executable; otherwise discover it above --base-repo.",
    )
    plan = commands.add_parser("plan", help="Preview a patch release without mutation.")
    plan.add_argument("--version", help="Strict release version, with or without v.")
    prepare = commands.add_parser(
        "prepare", help="Explicitly dispatch Base's Prepare Release PR workflow."
    )
    prepare.add_argument(
        "--version", help="Strict release version; default is next published patch."
    )
    prepare.add_argument("--release-date", default=date.today().isoformat())
    prepare.add_argument("--summary", required=True)
    prepare.add_argument("--confirm", help="Exact phrase: PREPARE OWNER/REPO vX.Y.Z COMMIT")
    publish = commands.add_parser(
        "publish", help="Stage a signed tag and dispatch Base's publication flow."
    )
    publish.add_argument("--tag", required=True, help="Strict release tag, including v.")
    publish.add_argument("--confirm", help="Exact phrase: PUBLISH OWNER/REPO vX.Y.Z COMMIT")
    publish.add_argument("--signing-public-key", type=Path, default=_default_public_key())
    return parser.parse_args(arguments)


def _default_public_key() -> Path:
    """Return the public counterpart of the documented operator key location."""
    return Path.home() / ".ssh" / "neurwerk_base_release_ed25519.pub"


def _checked(
    runner: CommandRunner, arguments: Sequence[str], *, cwd: Path | None = None, live: bool = False
) -> str:
    """Run a command and raise a concise error if it fails."""
    if live:
        print(f"\nRunning {' '.join(arguments)}", flush=True)
    started = time.monotonic()
    result = runner.run_live(arguments, cwd=cwd) if live else runner.run(arguments, cwd=cwd)
    if live:
        print(
            f"{'FAIL' if result.returncode else 'PASS'} {' '.join(arguments)} "
            f"({time.monotonic() - started:.1f}s)"
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ReleaseError(f"{arguments[0]} failed: {detail}")
    return result.stdout.strip()


def _ref_exists(runner: CommandRunner, repository: Repository, ref: str) -> bool:
    """Return whether a local Git ref exists, rejecting unexpected Git failures."""
    result = runner.run(("git", "rev-parse", "--verify", "--quiet", ref), cwd=repository.path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ReleaseError(f"git could not inspect {ref}: {result.stderr.strip()}")


def _gh(runner: CommandRunner, repository: Repository, *arguments: str) -> str:
    """Run a repository-scoped GitHub CLI command through the checked boundary."""
    return _checked(runner, ("gh", *arguments, "--repo", repository.slug))


def _remote_tag_exists(runner: CommandRunner, repository: Repository, tag: str) -> bool:
    """Return whether GitHub reports a final or staging tag by exact name."""
    return bool(_remote_ref(runner, repository, f"refs/tags/{tag}"))


def _remote_ref(runner: CommandRunner, repository: Repository, ref: str) -> str:
    """Read an exact remote ref; transport failures are not absence."""
    output = _checked(runner, ("git", "ls-remote", "origin", ref), cwd=repository.path)
    matches = [line.split() for line in output.splitlines() if line.split()[-1] == ref]
    if not matches:
        return ""
    if len(matches) != 1 or len(matches[0]) != 2 or not SHA_PATTERN.fullmatch(matches[0][0]):
        raise ReleaseError(f"invalid remote ref response for {ref}")
    return matches[0][0]


def _latest(runner: CommandRunner, repository: Repository) -> str:
    """Verify the latest published stable tag against the exact remote object."""
    tag = _gh(runner, repository, "release", "view", "--json", "tagName", "--jq", ".tagName")
    tag = _tag(tag)
    local = _checked(runner, ("git", "rev-parse", f"refs/tags/{tag}^{{tag}}"), cwd=repository.path)
    if local != _remote_ref(runner, repository, f"refs/tags/{tag}"):
        raise ReleaseError(
            "latest release tag differs locally; fetch verified tags in a release checkout"
        )
    _checked(runner, ("bash", "scripts/verify_release_tag.sh", tag, tag), cwd=repository.path)
    return tag


def _repository(runner: CommandRunner, path: Path) -> Repository:
    """Validate that path is a public GitHub repository before every operation."""
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ReleaseError("Base repository path does not exist; pass --base-repo explicitly")
    if _checked(runner, ("git", "rev-parse", "--is-inside-work-tree"), cwd=resolved) != "true":
        raise ReleaseError("Base repository path is not a Git worktree")
    remote = _checked(runner, ("git", "remote", "get-url", "--all", "origin"), cwd=resolved)
    slug = _github_slug(remote)
    push_urls = _checked(
        runner, ("git", "remote", "get-url", "--push", "--all", "origin"), cwd=resolved
    )
    if push_urls != remote:
        raise ReleaseError("origin must have one push destination matching the selected repository")
    response = _checked(
        runner, ("gh", "repo", "view", slug, "--json", "nameWithOwner,isPrivate,defaultBranchRef")
    )
    try:
        metadata = cast(dict[str, object], json.loads(response))
        name = metadata["nameWithOwner"]
        private = metadata["isPrivate"]
        default = cast(dict[str, object], metadata["defaultBranchRef"])["name"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ReleaseError("GitHub returned invalid repository metadata") from error
    if name != slug or private is not False or not isinstance(default, str) or not default:
        raise ReleaseError(
            "Base must be an accessible public GitHub repository with a default branch"
        )
    return Repository(path=resolved, slug=slug, default_branch=default)


def _github_slug(remote: str) -> str:
    """Extract owner/repository from supported GitHub origin URL forms."""
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9-]+)/"
        r"([A-Za-z0-9_-][A-Za-z0-9_.-]*?)(?:\.git)?",
        remote,
    )
    if match is None:
        raise ReleaseError("Base origin must be a github.com owner/repository remote")
    return f"{match.group(1)}/{match.group(2)}"


def _release_checkout(
    runner: CommandRunner,
    repository: Repository,
    root: Path | None,
    *,
    pr: dict[str, Any] | None = None,
) -> Repository:
    """Prepare an isolated exact target, preserving original files and shared history."""
    verified = _repository(runner, repository.path)
    if verified != repository:
        raise ReleaseError("repository identity changed before local preparation")
    common = Path(
        _checked(
            runner,
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=repository.path,
        )
    ).resolve()
    if root is None:
        root = (
            repository.path.parent
            if repository.path.parent.name == "worktrees"
            else common.parent.parent / "worktrees"
        )
    root = root.expanduser().resolve()
    if root.is_relative_to(repository.path) or root.is_relative_to(common.parent):
        raise ReleaseError("worktree root must be outside the selected and primary checkouts")
    target = (
        pr["headRefOid"]
        if pr
        else _remote_ref(runner, repository, f"refs/heads/{repository.default_branch}")
    )
    if not target:
        raise ReleaseError("remote default branch is missing")
    path = root / f"platform-release-{repository.slug.replace('/', '-')}-{target}"
    checkout = Repository(path, repository.slug, repository.default_branch)
    if path.exists() or path.is_symlink():
        registered = _checked(
            runner, ("git", "worktree", "list", "--porcelain"), cwd=repository.path
        )
        if path.is_symlink() or f"worktree {path}\n" not in registered + "\n":
            raise ReleaseError(
                "release checkout path exists but is not a registered linked worktree"
            )
        linked_common = _checked(
            runner,
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=path,
        )
        if Path(linked_common).resolve() != common:
            raise ReleaseError("release checkout belongs to another repository")
        _clean_target(runner, checkout, target if pr else None)
    if pr:
        _fetch_pr(runner, repository, pr)
    else:
        _fetch_target(runner, repository, target)
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        _checked(
            runner,
            (
                "git",
                "worktree",
                "add",
                "--detach",
                str(path),
                target,
            ),
            cwd=repository.path,
        )
    _clean_target(runner, checkout, target if pr else None)
    print(f"Release checkout: {path}")
    return checkout


def _fetch_pr(runner: CommandRunner, repository: Repository, pr: dict[str, Any]) -> None:
    """Fetch only the PR source; rewritten PR heads never update shared refs."""
    _checked(
        runner,
        (
            "git",
            "fetch",
            "--no-recurse-submodules",
            "--write-fetch-head",
            "--no-prune",
            "--no-tags",
            "--refmap=",
            "origin",
            f"refs/pull/{pr['number']}/head",
        ),
        cwd=repository.path,
    )
    if (
        _checked(runner, ("git", "rev-parse", "FETCH_HEAD^{commit}"), cwd=repository.path)
        != pr["headRefOid"]
    ):
        raise StaleTargetError("release PR changed during fetch; select it again for a fresh check")
    _check_pr_head(runner, repository, pr)


def _fetch_target(runner: CommandRunner, repository: Repository, target: str) -> None:
    """Fetch the observed target without moving tracking refs, then verify its lineage."""
    tracking = f"refs/remotes/origin/{repository.default_branch}"
    previous = (
        _checked(runner, ("git", "rev-parse", f"{tracking}^{{commit}}"), cwd=repository.path)
        if _ref_exists(runner, repository, tracking)
        else ""
    )
    _checked(
        runner,
        (
            "git",
            "fetch",
            "--atomic",
            "--no-recurse-submodules",
            "--write-fetch-head",
            "--no-prune",
            "--no-tags",
            # Disable opportunistic updates through remote.origin.fetch as well.
            "--refmap=",
            "origin",
            f"refs/heads/{repository.default_branch}",
            "refs/tags/*:refs/tags/*",
        ),
        cwd=repository.path,
    )
    fetched = _checked(
        runner,
        ("git", "rev-parse", "FETCH_HEAD^{commit}"),
        cwd=repository.path,
    )
    if fetched != target or target != _remote_ref(
        runner, repository, f"refs/heads/{repository.default_branch}"
    ):
        raise StaleTargetError("remote tip changed during fetch; rerun for a fresh confirmation")
    if (
        previous
        and runner.run(
            ("git", "merge-base", "--is-ancestor", previous, fetched), cwd=repository.path
        ).returncode
        != 0
    ):
        raise ReleaseError(
            "remote default branch is not a verified fast-forward of the previous origin "
            "tracking commit; history may be rewritten or incomplete. Tracking ref unchanged."
        )


def _tag(version: str) -> str:
    """Normalize a strict version to its v-prefixed release tag."""
    value = version if version.startswith("v") else f"v{version}"
    if SEMVER_PATTERN.fullmatch(value) is None:
        raise ReleaseError("version must be strict SemVer in the form vX.Y.Z")
    return value


def next_patch(tag: str) -> str:
    """Return the next strict semantic patch tag."""
    match = SEMVER_PATTERN.fullmatch(tag)
    if match is None:
        raise ReleaseError("tag must be strict SemVer in the form vX.Y.Z")
    return f"v{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"


def read_status(
    runner: CommandRunner, repository: Repository, requested_tag: str | None = None
) -> Status:
    """Read Git and GitHub state without changing either repository."""
    version = _checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path)
    tag = _tag(requested_tag or version)
    latest = _latest(runner, repository)
    commit = _checked(runner, ("git", "rev-parse", "HEAD"), cwd=repository.path)
    remote, sync, note = _head_sync(runner, repository, commit)
    return Status(
        repository=repository.slug,
        path=str(repository.path),
        default_branch=repository.default_branch,
        branch=_checked(runner, ("git", "branch", "--show-current"), cwd=repository.path),
        commit=commit,
        clean=not _checked(runner, ("git", "status", "--porcelain"), cwd=repository.path),
        version=version,
        tag=tag,
        next_patch=next_patch(latest),
        tag_exists=_ref_exists(runner, repository, f"refs/tags/{tag}")
        or _remote_tag_exists(runner, repository, tag),
        staging_tag_exists=_remote_tag_exists(runner, repository, f"release-staging/{tag}"),
        latest_published=latest,
        remote_default_commit=remote,
        head_sync=sync,
        sync_note=note,
    )


def _head_sync(runner: CommandRunner, repository: Repository, head: str) -> tuple[str, str, str]:
    """Compare the observed remote tip using only locally available Git history."""
    try:
        remote = _remote_ref(runner, repository, f"refs/heads/{repository.default_branch}")
    except ReleaseError:
        return (
            "",
            "unknown",
            "Remote tip unavailable; check connectivity separately. No fetch performed.",
        )
    if not remote:
        return "", "unknown", "Remote default ref is missing. No fetch performed."
    if remote == head:
        return remote, "equal", "HEAD equals the observed remote tip."
    if runner.run(
        ("git", "cat-file", "-e", f"{remote}^{{commit}}"), cwd=repository.path
    ).returncode:
        return (
            remote,
            "unknown",
            "Remote object is absent locally; history may be stale. Fetch separately.",
        )
    for ancestor, descendant, relation in ((head, remote, "behind"), (remote, head, "ahead")):
        result = runner.run(
            ("git", "merge-base", "--is-ancestor", ancestor, descendant), cwd=repository.path
        )
        if result.returncode == 0:
            return (
                remote,
                relation,
                "Compared against the observed remote tip using local ancestry.",
            )
        if result.returncode != 1:
            return remote, "unknown", "Local ancestry check failed; inspect history separately."
    shallow = _checked(runner, ("git", "rev-parse", "--is-shallow-repository"), cwd=repository.path)
    if shallow != "false":
        return remote, "unknown", "Local history is shallow; divergence cannot be established."
    return remote, "diverged", "Neither commit is an ancestor of the other in local history."


def plan(
    runner: CommandRunner, repository: Repository, requested_version: str | None
) -> dict[str, object]:
    """Return a read-only patch-release plan and collision evidence."""
    status = read_status(runner, repository)
    tag = _tag(requested_version) if requested_version else status.next_patch
    if tuple(map(int, tag[1:].split("."))) <= tuple(
        map(int, status.latest_published[1:].split("."))
    ):
        raise ReleaseError("new version must be newer than the latest verified published release")
    return {
        "status": asdict(status),
        "planned_tag": tag,
        "final_tag_exists": _ref_exists(runner, repository, f"refs/tags/{tag}")
        or _remote_tag_exists(runner, repository, tag),
        "staging_tag_exists": _remote_tag_exists(runner, repository, f"release-staging/{tag}"),
        "commands": [
            "make check",
            "make release-check",
            f"git tag -s -a {tag}",
            f"git push origin <signed-tag-object>:refs/tags/release-staging/{tag}",
            "gh workflow run 'Create Platform Release Tag' ...",
        ],
        "note": "No final-tag pushes, automatic retries, client adoption, or cluster access.",
    }


def _validate_prepare_arguments(args: argparse.Namespace) -> str:
    """Validate Base workflow inputs before dispatching the workflow."""
    tag = _tag(args.version)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.release_date) is None:
        raise ReleaseError("release date must use YYYY-MM-DD")
    try:
        date.fromisoformat(args.release_date)
    except ValueError as error:
        raise ReleaseError("release date must use YYYY-MM-DD") from error
    if not args.summary.strip():
        raise ReleaseError("summary must not be empty")
    return tag


def prepare(
    runner: CommandRunner,
    repository: Repository,
    args: argparse.Namespace,
    prompt: Prompt | None = None,
) -> str:
    """Explicitly dispatch the existing Base preparation workflow."""
    args.version = args.version or next_patch(_latest(runner, repository))
    tag = _validate_prepare_arguments(args)
    proposal = plan(runner, repository, tag)
    if proposal["final_tag_exists"] or proposal["staging_tag_exists"]:
        raise ReleaseError("release tag collision; inspect status instead of preparing again")
    latest = cast(str, cast(dict[str, object], proposal["status"])["latest_published"])
    if _remote_ref(runner, repository, f"refs/heads/release/{tag}"):
        raise ReleaseError("release branch already exists; continue its review, never overwrite it")
    if _release_prs(runner, repository, "open", tag):
        raise ReleaseError("release PR already exists; continue its review")
    _prepare_predecessor(runner, repository, latest)
    target = _clean_target(runner, repository)
    _show_prepare_preview(runner, repository, tag, latest, target, args.release_date, args.summary)
    _confirm(prompt or TerminalPrompt(), "PREPARE", repository, tag, target, args.confirm)
    if _repository(runner, repository.path) != repository:
        raise ReleaseError("repository identity changed before preparation")
    if _clean_target(runner, repository) != target:
        raise StaleTargetError("default branch changed while confirming")
    command = [
        "gh",
        "workflow",
        "run",
        "Prepare Release PR",
        "--repo",
        repository.slug,
        "--ref",
        repository.default_branch,
        "-f",
        f"version={tag.removeprefix('v')}",
        "-f",
        "preparation_mode=successor",
        "-f",
        f"release_date={args.release_date}",
        "-f",
        f"summary={args.summary}",
        "-f",
        "stable_upgrade=supported",
        "-f",
        "upgrades_from_alpha_revisions=",
        "-f",
        "recovery=forward-fix",
        "-f",
        f"previous_tag={latest}",
    ]
    _checked(runner, command)
    return (
        f"Dispatched Prepare Release PR for {tag}. "
        "Next: 2. Review notes (finish-notes), then 3. Check (check). "
        "Merge the reviewed release PR on GitHub before "
        "5. Publish (publish). Use Release status to inspect the PR once available."
    )


def _prepare_predecessor(runner: CommandRunner, repository: Repository, previous_tag: str) -> None:
    """Delegate reachable-tag selection to Base and reject unfinished release state."""
    current = _tag(_checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path))
    if previous_tag != current:
        raise ReleaseError(
            f"HEAD VERSION is {current}, not predecessor {previous_tag}; "
            "an unpublished release may be pending. Use continue --tag before preparing."
        )
    pending = _target_tag(runner, repository)
    if pending != previous_tag:
        raise ReleaseError(
            f"pending release {pending}; use continue --tag {pending} before preparing another"
        )
    reachable = _checked(
        runner,
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            "import runpy; "
            "print(runpy.run_path('scripts/platform_release.py')['latest_release_tag']())",
        ),
        cwd=repository.path,
    )
    if previous_tag != reachable:
        raise ReleaseError(
            f"predecessor must be Base's latest release reachable from HEAD: {reachable}"
        )


def _require_publish_preconditions(
    runner: CommandRunner,
    repository: Repository,
    tag: str,
    signing_public_key: Path,
) -> str:
    """Check all mutable-release preconditions before creating a local signed tag."""
    if (
        signing_public_key.suffix != ".pub"
        or signing_public_key.expanduser().is_symlink()
        or not signing_public_key.expanduser().is_file()
    ):
        raise ReleaseError("signing key must be an existing public .pub file")
    target_commit = _clean_target(runner, repository)
    if _tag(_checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path)) != tag:
        raise ReleaseError("requested tag does not match the release commit VERSION")
    proposal = plan(runner, repository, tag)
    previous = _checked(
        runner,
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/platform_release.py",
            "previous-tag",
            "--release-root",
            ".",
        ),
        cwd=repository.path,
    )
    if previous != cast(dict[str, object], proposal["status"])["latest_published"]:
        raise ReleaseError("release provenance must name the latest verified published predecessor")
    _release_pr_gate(runner, repository, tag, target_commit)
    if _ref_exists(runner, repository, f"refs/tags/{tag}") or _remote_tag_exists(
        runner, repository, tag
    ):
        raise ReleaseError(f"final tag {tag} already exists")
    staging = f"release-staging/{tag}"
    if _remote_tag_exists(runner, repository, staging):
        raise ReleaseError(f"staging tag {staging} already exists; do not retry automatically")
    fingerprints = _checked(runner, ("ssh-add", "-l"))
    public = _checked(
        runner, ("ssh-keygen", "-lf", str(signing_public_key.expanduser()), "-E", "sha256")
    )
    if len(public.split()) < 2 or public.split()[1] != DEFAULT_EXPECTED_SIGNER_FINGERPRINT:
        raise ReleaseError("public key fingerprint is not the approved signer")
    if DEFAULT_EXPECTED_SIGNER_FINGERPRINT not in [
        line.split()[1] for line in fingerprints.splitlines() if len(line.split()) >= 2
    ]:
        raise ReleaseError("approved signing key is not available from ssh-agent")
    _checked(runner, ("make", "check"), cwd=repository.path, live=True)
    _checked(runner, ("make", "release-check"), cwd=repository.path, live=True)
    return target_commit


def _clean_target(
    runner: CommandRunner, repository: Repository, expected: str | None = None
) -> str:
    """Require a clean linked worktree at the selected SHA or remote default tip."""
    git_dir = _checked(runner, ("git", "rev-parse", "--git-dir"), cwd=repository.path)
    common = _checked(runner, ("git", "rev-parse", "--git-common-dir"), cwd=repository.path)
    if git_dir == common:
        raise ReleaseError("use a dedicated linked release worktree, not the primary checkout")
    if _checked(runner, ("git", "status", "--porcelain"), cwd=repository.path):
        raise ReleaseError("release checkout must be clean")
    target = _checked(runner, ("git", "rev-parse", "HEAD"), cwd=repository.path)
    expected = expected or _remote_ref(
        runner, repository, f"refs/heads/{repository.default_branch}"
    )
    if not SHA_PATTERN.fullmatch(target) or target != expected:
        raise StaleTargetError("release checkout must equal the exact remote target commit")
    return target


def _release_prs(
    runner: CommandRunner,
    repository: Repository,
    state: str,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Inventory only same-repository release heads targeting the default branch."""
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repository.slug,
        "--state",
        state,
        "--base",
        repository.default_branch,
        "--limit",
        "1000",
        "--json",
        "number,state,url,headRefName,headRefOid,mergeCommit,author,isCrossRepository,baseRefName,isDraft",
    ]
    if tag:
        command.extend(("--head", f"release/{tag}"))
    prs = cast(list[dict[str, Any]], json.loads(_checked(runner, command)))
    if len(prs) >= 1000:
        raise ReleaseError(
            "release PR inventory is truncated; narrow the target or inspect manually"
        )
    return [
        pr
        for pr in prs
        if (
            pr.get("isCrossRepository") is False
            and pr.get("baseRefName") == repository.default_branch
            and isinstance(pr.get("headRefName"), str)
            and pr["headRefName"].startswith("release/")
            and SEMVER_PATTERN.fullmatch(pr["headRefName"].removeprefix("release/"))
            and (tag is None or pr["headRefName"] == f"release/{tag}")
        )
    ]


def _release_pr_gate(runner: CommandRunner, repository: Repository, tag: str, target: str) -> None:
    """Require the exact merged release PR and actual required checks, not extra reviews."""
    prs = _release_prs(runner, repository, "merged", tag)
    matches = [pr for pr in prs if pr.get("mergeCommit", {}).get("oid") == target]
    if len(matches) != 1:
        raise ReleaseError(
            "remote main must be the exact merged release PR commit; "
            "merge the release PR manually, or stop for provenance review if main advanced"
        )
    pr = matches[0]
    if not SHA_PATTERN.fullmatch(pr.get("headRefOid", "")):
        raise ReleaseError("release PR head must be an exact commit")
    _gh(runner, repository, "pr", "checks", str(pr["number"]), "--required")
    checks = json.loads(
        _checked(
            runner,
            (
                "gh",
                "api",
                f"repos/{repository.slug}/commits/{pr['headRefOid']}/check-runs",
                "--jq",
                ".check_runs",
            ),
        )
    )
    required = [c for c in checks if c.get("name") == "Required CI"]
    latest = max(required, key=lambda c: c["id"], default={})
    if (
        latest.get("head_sha") != pr["headRefOid"]
        or latest.get("status") != "completed"
        or latest.get("conclusion") != "success"
    ):
        raise ReleaseError("reviewed PR head lacks successful Required CI")


def _unreleased_notes(changelog: str, section: str = "Unreleased") -> str:
    """Extract one changelog section, ignoring headings inside fenced code."""
    notes: list[str] | None = None
    fence = ""
    for line in changelog.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line
            ):
                fence = ""
        elif marker:
            fence = marker[1]
        elif re.match(r"^##\s+", line):
            if notes is not None:
                break
            if re.fullmatch(r"##\s+\[" + re.escape(section) + r"\](?:\s+.*)?", line):
                notes = []
            continue
        if notes is not None:
            notes.append(line)
    if notes is None:
        return f"{section} section is missing; no release notes available."
    return "\n".join(notes).strip() or f"{section} section is empty; no release notes available."


def _show_prepare_preview(
    runner: CommandRunner,
    repository: Repository,
    tag: str,
    previous: str,
    target: str,
    release_date: str,
    summary: str,
) -> None:
    """Show the proposed draft and notes from its exact observed source commit."""
    changelog = _checked(runner, ("git", "show", f"{target}:CHANGELOG.md"), cwd=repository.path)
    print(
        f"\nPrepare release PR: {repository.slug} | {tag}\n"
        f"Latest verified published predecessor: {previous}\n"
        f"Release date: {release_date}\n"
        f"Summary: {summary}\n"
        f"Source target SHA ({repository.default_branch}): {target}\n"
        f"\n## [Unreleased]\n\n{_unreleased_notes(changelog)}\n\n"
        "This dispatch creates a draft release PR; it does not sign, publish, or deploy.\n"
        f"The workflow uses {repository.default_branch} when it runs; "
        "review the included changes in the draft."
    )


def _show_evidence(runner: CommandRunner, repository: Repository) -> None:
    """Display committed release evidence for validation and publication."""
    tag = _tag(_checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path))
    for path in ("CHANGELOG.md", "release/manifest.yaml", f"release/migrations/{tag}.md"):
        content = _checked(runner, ("git", "show", f"HEAD:{path}"), cwd=repository.path)
        print(f"\n--- {path} ---\n{content}")
    print(
        "Review this release's notes, release files and upgrade instructions before proceeding. "
        "No cluster or adoption action is authorized."
    )


def _confirm(
    prompt: Prompt,
    action: str,
    repository: Repository,
    tag: str,
    target: str,
    supplied: str | None = None,
) -> None:
    """Bind operator authorization to repository, version and exact commit."""
    expected = f"{action} {repository.slug} {tag} {target}"
    if action == "PREPARE" and supplied is None:
        message = f"Create draft release PR for {repository.slug} {tag} from {target}?"
        accepted = (
            questionary.confirm(message, default=False).ask() is True
            if isinstance(prompt, TerminalPrompt)
            else prompt.ask(f"{message} [yes/No]: ") == "yes"
        )
        if not accepted:
            raise ReleaseError(
                "preparation confirmation declined or cancelled; no mutation authorized"
            )
        return
    if (supplied if supplied is not None else prompt.ask(f"Type exactly {expected}: ")) != expected:
        raise ReleaseError("confirmation did not match; no mutation authorized")


def _signed_tag_object(
    runner: CommandRunner, repository: Repository, tag: str, public_key: Path, target: str
) -> str:
    """Create a signed annotated local tag and return its tag-object SHA."""
    _checked(
        runner,
        (
            "git",
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={public_key.expanduser()}",
            "tag",
            "-s",
            "-a",
            tag,
            "-m",
            f"Platform {tag}",
            target,
        ),
        cwd=repository.path,
    )
    tag_object = _checked(runner, ("git", "rev-parse", f"{tag}^{{tag}}"), cwd=repository.path)
    if SHA_PATTERN.fullmatch(tag_object) is None:
        raise ReleaseError("Git did not create an annotated tag object")
    _checked(runner, ("make", "release-check", f"TAG={tag}"), cwd=repository.path, live=True)
    _checked(runner, ("bash", "scripts/verify_release_tag.sh", tag, target), cwd=repository.path)
    return tag_object


def _dispatch_tag_creation(
    runner: CommandRunner,
    repository: Repository,
    tag: str,
    tag_object: str,
    target_commit: str,
) -> None:
    """Stage the immutable tag object and dispatch Base's protected-tag workflow."""
    staging = f"release-staging/{tag}"
    if _repository(runner, repository.path) != repository:
        raise ReleaseError("repository identity changed before staging")
    if _clean_target(runner, repository) != target_commit:
        raise ReleaseError("release target changed before staging")
    if _remote_tag_exists(runner, repository, tag) or _remote_tag_exists(
        runner, repository, staging
    ):
        raise ReleaseError("remote tag appeared before staging; inspect continue")
    _checked(
        runner,
        ("git", "push", "--no-follow-tags", "origin", f"{tag_object}:refs/tags/{staging}"),
        cwd=repository.path,
    )
    _checked(
        runner,
        (
            "gh",
            "workflow",
            "run",
            "Create Platform Release Tag",
            "--repo",
            repository.slug,
            "--ref",
            repository.default_branch,
            "-f",
            f"tag={tag}",
            "-f",
            f"tag_object={tag_object}",
            "-f",
            f"target_commit={target_commit}",
        ),
    )


def _wait_for(
    runner: CommandRunner,
    command: Sequence[str],
    predicate: Callable[[str], bool],
    sleep: Callable[[float], None],
) -> None:
    """Poll a read-only command until it returns a successful expected result."""
    for attempt in range(POLL_ATTEMPTS):
        result = runner.run(command)
        if result.returncode == 0 and predicate(result.stdout.strip()):
            return
        if attempt + 1 < POLL_ATTEMPTS:
            sleep(POLL_SECONDS)
    raise ReleaseError("timed out waiting for Base release workflows; do not retry automatically")


def _wait_for_publication(
    runner: CommandRunner,
    repository: Repository,
    tag: str,
    tag_object: str,
    sleep: Callable[[float], None],
    target: str | None = None,
) -> None:
    """Wait for protected-tag creation and the resulting GitHub Release."""
    _wait_for(
        runner,
        ("gh", "api", f"repos/{repository.slug}/git/ref/tags/{tag}", "--jq", ".object.sha"),
        lambda value: value == tag_object,
        sleep,
    )
    if target is not None:
        _verify_workflows(runner, repository, tag, target, sleep)
    _wait_for(
        runner,
        (
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository.slug,
            "--json",
            "tagName,url,isDraft,isPrerelease",
        ),
        lambda value: _release_matches(value, tag),
        sleep,
    )


def _release_matches(value: str, tag: str) -> bool:
    """Return whether GitHub release JSON identifies the requested release tag."""
    try:
        release = cast(dict[str, object], json.loads(value))
        return (
            release.get("tagName") == tag
            and release.get("isDraft") is False
            and release.get("isPrerelease") is False
        )
    except json.JSONDecodeError:
        return False


def publish(
    runner: CommandRunner,
    repository: Repository,
    args: argparse.Namespace,
    sleep: Callable[[float], None] = time.sleep,
    prompt: Prompt | None = None,
) -> str:
    """Perform the explicitly confirmed staged publication handoff to Base workflows."""
    tag = _tag(args.tag)
    target_commit = _require_publish_preconditions(
        runner,
        repository,
        tag,
        args.signing_public_key,
    )
    _show_evidence(runner, repository)
    _confirm(prompt or TerminalPrompt(), "PUBLISH", repository, tag, target_commit, args.confirm)
    if _clean_target(runner, repository) != target_commit:
        raise StaleTargetError("default branch changed while checking or confirming")
    if _repository(runner, repository.path) != repository:
        raise ReleaseError("repository identity changed before publication")
    _release_pr_gate(runner, repository, tag, target_commit)
    tag_object = _signed_tag_object(runner, repository, tag, args.signing_public_key, target_commit)
    _dispatch_tag_creation(runner, repository, tag, tag_object, target_commit)
    _wait_for_publication(runner, repository, tag, tag_object, sleep, target_commit)
    return f"Published {tag} through Base workflows. No client or cluster actions performed."


def _verify_workflows(
    runner: CommandRunner,
    repository: Repository,
    tag: str,
    target: str,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Poll read-only workflow evidence, distinguishing pending from exact-target failure."""
    detail = "verification run has not appeared"
    for attempt in range(POLL_ATTEMPTS):
        runs = json.loads(
            _gh(
                runner,
                repository,
                "run",
                "list",
                "--workflow",
                "release.yaml",
                "--commit",
                target,
                "--branch",
                tag,
                "--event",
                "push",
                "--limit",
                "100",
                "--json",
                "headSha,headBranch,status,conclusion",
            )
        )
        exact = [r for r in runs if r.get("headSha") == target and r.get("headBranch") == tag]
        if exact and exact[0]["status"] == "completed":
            if exact[0]["conclusion"] != "success":
                raise ReleaseError(
                    f"Verify Platform Release for {tag} {target}: {exact[0]['conclusion']}"
                )
            complete, detail = _publication_complete(runner, repository, tag)
            if complete:
                return
        elif exact:
            detail = f"verification is {exact[0]['status']}"
        if attempt + 1 < POLL_ATTEMPTS:
            sleep(POLL_SECONDS)
    raise ReleaseError(
        f"timed out waiting for {tag}: {detail}; use continue --tag {tag} to recheck"
    )


def _publication_complete(
    runner: CommandRunner, repository: Repository, tag: str
) -> tuple[bool, str]:
    """Correlate workflow_run by artifact, never by its default-branch SHA or timing."""
    publications = json.loads(
        _gh(
            runner,
            repository,
            "run",
            "list",
            "--workflow",
            "publish-release.yaml",
            "--event",
            "workflow_run",
            "--limit",
            "20",
            "--json",
            "databaseId,status,conclusion",
        )
    )
    uncorrelated_failures = []
    for run in publications:
        with tempfile.TemporaryDirectory(prefix="platform-release-") as directory:
            result = runner.run(
                (
                    "gh",
                    "run",
                    "download",
                    str(run["databaseId"]),
                    "--repo",
                    repository.slug,
                    "--name",
                    "published-platform-release",
                    "--dir",
                    directory,
                )
            )
            identity = Path(directory) / "release-tag.txt"
            if (
                result.returncode == 0
                and identity.is_file()
                and identity.read_text().strip() == tag
            ):
                if run["status"] != "completed":
                    return False, f"publication run {run['databaseId']} is {run['status']}"
                if run["conclusion"] != "success":
                    raise ReleaseError(
                        f"publication run {run['databaseId']} for {tag}: {run['conclusion']}"
                    )
                return True, "published"
        if run["status"] == "completed" and run["conclusion"] != "success":
            uncorrelated_failures.append(f"{run['databaseId']} ({run['conclusion']})")
    detail = "publication identity artifact not available (pending, absent or expired)"
    if uncorrelated_failures:
        detail += "; uncorrelated failed runs: " + ", ".join(uncorrelated_failures)
    return False, detail


def _render(
    value: object, as_json: bool, *, diagnostics: bool = False, changelog: bool = False
) -> str:
    """Render JSON for automation or a compact human-readable result."""
    if as_json:
        return json.dumps(value, indent=2, sort_keys=True)
    if isinstance(value, Status):
        return "\n".join(f"{key}: {item}" for key, item in asdict(value).items())
    if isinstance(value, dict):
        report = cast(dict[str, Any], value)
        if diagnostics:
            sections = [_render_summary(report), "\nDiagnostics (candidate runs are uncorrelated):"]
            for key in (
                "status",
                "release_prs",
                "workflow_runs",
                "publication_runs_uncorrelated",
                "tag_creation_runs_uncorrelated",
                "tag_creation_run_guidance",
            ):
                sections.append(f"\n{key}:\n{json.dumps(report[key], indent=2, sort_keys=True)}")
            metadata = [
                {k: v for k, v in r.items() if k != "body"} for r in report["github_release"]
            ]
            sections.append(
                f"\nGitHub Release metadata:\n{json.dumps(metadata, indent=2, sort_keys=True)}"
            )
            return "\n".join(sections)
        if changelog:
            releases = cast(list[dict[str, str]], report["github_release"])
            return "\n\n".join(r.get("body") or "No release notes." for r in releases) or (
                "No GitHub Release changelog available for this target."
            )
        return _render_summary(report)
    return str(value)


def _render_summary(report: dict[str, Any]) -> str:
    """Render an allowlisted human summary, excluding API payloads and diagnostics."""
    status = report["status"]
    lines = [
        f"{status['repository']} | {status['tag']}",
        f"Local checkout: {status['head_sync']} (not the source of a pending release PR).",
        "Prepare and Publish use a separate checkout; your local files stay untouched.",
        f"Latest verified published tag: {status['latest_published']}; "
        f"next patch: {status['next_patch']}",
    ]
    if "planned_tag" in report:
        lines.extend(
            [
                f"Planned tag: {report['planned_tag']}",
                f"Final tag collision: {report['final_tag_exists']}; "
                f"staging collision: {report['staging_tag_exists']}",
                *report["commands"],
                report["note"],
            ]
        )
        return "\n".join(lines)
    local = report["local_signed_object"]
    for pr in report["release_prs"]:
        if pr.get("url"):
            lines.append(f"Release PR: {pr['url']}")
        state = (
            "ready for review" if pr.get("state") == "OPEN" else pr.get("state", "unknown").lower()
        )
        lines.append(
            f"PR: {'draft' if pr.get('isDraft') else state}; "
            f"checks: {pr.get('checks_state', 'unknown')}; "
            f"commit: {pr.get('headRefOid', 'unknown')}"
        )
    lines.extend(
        [
            f"GitHub Release: {report['release_state']}",
            f"Release URL: {report['release_url'] or 'none'}",
            f"Local signed object: {local or 'missing'}"
            + (" (signature verified)" if local else ""),
            f"Signed commit: {report['local_signed_commit'] or 'unknown'}",
        ]
    )
    for label, key in (
        ("Remote final", "remote_final_object"),
        ("Remote staging", "remote_staged_object"),
    ):
        obj = report[key]
        trust = (
            "matches verified local signed object"
            if obj and local == obj
            else "not verified locally"
        )
        lines.append(f"{label}: {obj} ({trust})" if obj else f"{label}: missing")
    lines.extend(
        [
            f"Release verification: {report['verifier_status']}",
            "Publication verification: "
            + (
                "verified"
                if report.get("publication_verified")
                else "not checked by status. Use Publication progress (continue) to verify "
                "the completed workflow; older verification records may have expired."
            ),
            f"Next: {report['next_action']}",
            "Views: status --diagnostics (full details), status --changelog, or --json status.",
        ]
    )
    return "\n".join(lines)


GUIDE = """Release guide
1. Prepare: create a release draft.
2. Review notes: keep or edit the notes and upgrade instructions.
3. Check: update outdated release files if needed, then run all checks.
4. Merge: open the release PR on GitHub, mark Ready for review, wait for green checks,
   resolve required reviews, then merge on GitHub.
5. Publish: sign and publish the merged release after confirmation.

Nothing merges automatically. Publishing does not deploy to a cluster.
"""


def _interactive_command(prompt: Prompt) -> str:
    """Offer the release lifecycle without defaulting to a mutation."""
    choices = [
        questionary.Separator("Release Steps"),
        questionary.Choice("1. Prepare", value="prepare"),
        questionary.Choice("2. Review notes", value="finish-notes"),
        questionary.Choice("3. Check", value="check"),
        questionary.Choice("4. Merge on GitHub", value="merge"),
        questionary.Choice("5. Publish", value="publish"),
        questionary.Separator("\nInformation"),
        questionary.Choice("Release status", value="status"),
        questionary.Choice("Changelog", value="changelog"),
        questionary.Choice("Preview next release", value="plan"),
        questionary.Choice("Publication progress", value="continue"),
        questionary.Choice("Diagnostics", value="diagnostics"),
        questionary.Choice("Exit", value="quit"),
    ]
    if isinstance(prompt, TerminalPrompt):
        choice = questionary.select(
            "Platform release action",
            choices=choices,
            default="status",
        ).ask()
        if not isinstance(choice, str):
            raise ReleaseError("cancelled")
        return choice
    return (
        prompt.ask(
            "\n".join(
                f"{choice.title}:"
                if isinstance(choice, questionary.Separator)
                else f"  {choice.title} ({choice.value})"
                for choice in choices
            )
            + "\nEnter action ID [status]: "
        )
        or "status"
    )


def _wizard(
    args: argparse.Namespace, runner: CommandRunner, repository: Repository, prompt: Prompt
) -> None:
    """Collect release prose and version, leaving policy evidence to the release PR."""
    args.version = None
    if args.command == "prepare":
        latest = _latest(runner, repository)
        suggested = next_patch(latest)
        args.version = prompt.ask(f"Version [{suggested}], or custom strict SemVer: ") or suggested
        args.release_date = (
            prompt.ask(f"Release date [{date.today().isoformat()}]: ") or date.today().isoformat()
        )
        args.summary = prompt.ask("Release summary: ")
        args.confirm = None
    elif args.command in ("status", "continue", "merge"):
        args.tag = _target_tag(runner, repository, prompt)
    elif args.command == "publish":
        args.tag = _tag(prompt.ask("Reviewed release version (vX.Y.Z): "))
        args.confirm = None
        args.signing_public_key = _default_public_key()


def _choose(prompt: Prompt, message: str, choices: Sequence[str]) -> str:
    """Select one explicit option, with the conservative first entry as default."""
    if isinstance(prompt, TerminalPrompt):
        response = questionary.select(message, choices=list(choices), default=choices[0]).ask()
    else:
        response = prompt.ask(f"{message}: {', '.join(choices)} [{choices[0]}]: ") or choices[0]
    if not isinstance(response, str) or response not in choices:
        raise ReleaseError("selection cancelled or invalid")
    return response


def _target_tag(runner: CommandRunner, repository: Repository, prompt: Prompt | None = None) -> str:
    """Prefer pending release PRs over checkout VERSION; never guess between pending targets."""
    prs = _release_prs(runner, repository, "open")
    pending = list(dict.fromkeys(pr["headRefName"].removeprefix("release/") for pr in prs))
    current = _tag(_checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path))
    if prompt is not None:
        choices = list(dict.fromkeys([*pending, current, _latest(runner, repository)]))
        return _choose(prompt, "Release target (pending PRs first)", choices)
    if len(pending) > 1:
        raise ReleaseError("multiple pending releases; pass --tag: " + ", ".join(pending))
    return _tag(pending[0]) if pending else current


def inspect_progress(
    runner: CommandRunner,
    repository: Repository,
    requested_tag: str | None = None,
) -> dict[str, object]:
    """Report exact local and remote state without attempting repairs or retries."""
    tag = _tag(requested_tag) if requested_tag else _target_tag(runner, repository)
    status = read_status(runner, repository, tag)
    local = ""
    signed_commit = ""
    if _ref_exists(runner, repository, f"refs/tags/{status.tag}"):
        local = _checked(
            runner, ("git", "rev-parse", f"refs/tags/{status.tag}^{{tag}}"), cwd=repository.path
        )
        _checked(
            runner,
            ("bash", "scripts/verify_release_tag.sh", status.tag, status.tag),
            cwd=repository.path,
        )
        signed_commit = _checked(
            runner, ("git", "rev-parse", f"refs/tags/{status.tag}^{{commit}}"), cwd=repository.path
        )
    final = _remote_ref(runner, repository, f"refs/tags/{status.tag}")
    staged = _remote_ref(runner, repository, f"refs/tags/release-staging/{status.tag}")
    if local and any(remote and remote != local for remote in (final, staged)):
        raise ReleaseError(
            "local and remote tag objects differ; stop, do not overwrite or delete refs"
        )
    run_filter = (
        ("--commit", signed_commit, "--branch", tag)
        if signed_commit
        else ("--branch", f"release/{tag}")
    )
    runs = json.loads(
        _gh(
            runner,
            repository,
            "run",
            "list",
            *run_filter,
            "--limit",
            "100",
            "--json",
            "databaseId,workflowName,headSha,headBranch,event,status,conclusion,url",
        )
    )
    publication_runs = json.loads(
        _gh(
            runner,
            repository,
            "run",
            "list",
            "--workflow",
            "publish-release.yaml",
            "--event",
            "workflow_run",
            "--limit",
            "20",
            "--json",
            "databaseId,status,conclusion,url",
        )
    )
    creation_runs = json.loads(
        _gh(
            runner,
            repository,
            "run",
            "list",
            "--workflow",
            "create-release-tag.yaml",
            "--event",
            "workflow_dispatch",
            "--branch",
            repository.default_branch,
            "--limit",
            "20",
            "--json",
            "databaseId,headSha,headBranch,status,conclusion,url",
        )
    )
    prs = _release_prs(runner, repository, "all", status.tag)
    for pr in prs:
        pr["checks_state"] = _pr_checks_state(runner, repository, pr)
    releases = json.loads(
        _checked(
            runner,
            (
                "gh",
                "api",
                f"repos/{repository.slug}/releases",
                "--paginate",
                "--slurp",
            ),
        )
    )
    published = [
        release for page in releases for release in page if release.get("tag_name") == status.tag
    ]
    exact = [
        run
        for run in runs
        if signed_commit
        and run.get("headSha") == signed_commit
        and run.get("headBranch") == tag
        and run.get("workflowName") == "Verify Platform Release"
        and run.get("event") == "push"
    ]
    verifier = "not found" if signed_commit else "unknown (local signed tag missing)"
    if exact:
        verifier = exact[0].get("conclusion") or exact[0].get("status") or "unknown"
    release_state = _release_state(published)
    return {
        "status": asdict(status),
        "local_signed_object": local,
        "local_signed_commit": signed_commit,
        "remote_final_object": final,
        "remote_staged_object": staged,
        "release_prs": prs,
        "workflow_runs": runs,
        "publication_runs_uncorrelated": publication_runs,
        "tag_creation_runs_uncorrelated": creation_runs,
        "tag_creation_run_guidance": (
            "Candidate runs only: verify tag, tag_object and target_commit inputs against "
            "the inspected signed identity before attributing a run or authorizing a retry."
        ),
        "github_release": published,
        "release_state": release_state,
        "release_url": published[0].get("html_url", "") if published else "",
        "verifier_status": verifier,
        "next_action": (
            _pr_next_action(prs)
            if release_state == "missing" and not any((local, final, staged))
            else _next_action(release_state, local, final, staged)
        ),
    }


def _pr_checks_state(runner: CommandRunner, repo: Repository, pr: dict[str, Any]) -> str:
    """Correlate required checks with the observed PR head, failing closed on races."""
    result = runner.run(
        (
            "gh",
            "pr",
            "checks",
            str(pr["number"]),
            "--repo",
            repo.slug,
            "--required",
            "--json",
            "name,bucket,link",
        ),
        cwd=repo.path,
    )
    checks = json.loads(result.stdout) if result.stdout.strip() else []
    runs = json.loads(
        _checked(
            runner,
            (
                "gh",
                "api",
                f"repos/{repo.slug}/commits/{pr['headRefOid']}/check-runs",
                "--paginate",
                "--slurp",
                "--jq",
                "[.[].check_runs[]]",
            ),
        )
    )
    latest = max(
        (c for c in runs if c.get("name") == "Required CI"), key=lambda c: c["id"], default={}
    )
    current = _release_prs(runner, repo, "all", pr["headRefName"].removeprefix("release/"))
    if not any(
        p["number"] == pr["number"]
        and p.get("headRefOid") == pr["headRefOid"]
        and p.get("state") == pr.get("state")
        and p.get("isDraft") == pr.get("isDraft")
        for p in current
    ):
        return "unknown (PR changed; refresh status)"
    if latest.get("head_sha") != pr["headRefOid"]:
        return "unknown"
    if any(c.get("bucket") in ("fail", "cancel") for c in checks) or (
        latest.get("status") == "completed" and latest.get("conclusion") not in ("success", None)
    ):
        return "failed"
    if (
        result.returncode == 8
        or any(c.get("bucket") == "pending" for c in checks)
        or (latest.get("status") != "completed")
    ):
        return "pending"
    if (
        result.returncode == 0
        and checks
        and latest.get("conclusion") == "success"
        and all(c.get("bucket") in ("pass", "skipping") for c in checks)
    ):
        return "passing"
    return "unknown"


def _pr_next_action(prs: list[dict[str, Any]]) -> str:
    """Give one next action without treating green CI as release-file validation."""
    opened = [pr for pr in prs if pr.get("state") == "OPEN"]
    if len(opened) > 1:
        return "Select a release with --tag before continuing."
    if opened:
        pr = opened[0]
        url = pr.get("url") or f"PR #{pr['number']}"
        checks = pr.get("checks_state", "unknown")
        if checks == "failed":
            return f"3. Check: fix failed checks for {url}; green CI alone does not check notes."
        if pr.get("isDraft"):
            return (
                f"3. Check, then 4. Merge: choose Ready for review at {url}; wait for green checks."
            )
        if checks == "pending":
            return f"4. Merge: wait for green checks at {url}; do not merge yet."
        if checks == "passing":
            return (
                "4. Merge: after 3. Check passes and required reviews are resolved, "
                f"merge at {url}."
            )
        return f"Refresh checks at {url}; their current result could not be verified."
    merged = [pr for pr in prs if pr.get("state") == "MERGED"]
    if merged:
        if len(merged) != 1 or merged[0].get("checks_state") != "passing":
            return "Resolve or refresh the merged PR checks before 5. Publish."
        return "5. Publish: the release PR is merged; publishing requires your confirmation."
    return "1. Prepare: create a release draft."


def _release_state(releases: list[dict[str, Any]]) -> str:
    """Describe publication existence without claiming correlated workflow verification."""
    if not releases:
        return "missing"
    release = releases[0]
    if release.get("draft") is True:
        return "draft"
    if release.get("draft") is False and release.get("prerelease") is False:
        return "published"
    return "prerelease" if release.get("prerelease") is True else "unknown"


def _next_action(state: str, local: str, final: str, staged: str) -> str:
    """Guide inspection from observed evidence, never from guessed approval state."""
    if state == "published":
        return "Release is published; do not republish or move its tag. " + (
            "Use continue only if artifact-correlated pipeline verification is needed."
            if local and final
            else "Signed final-tag evidence is incomplete; inspect diagnostics "
            "and obtain missing refs separately."
        )
    if final:
        return (
            "Final tag exists; never recreate or move it. Inspect exact-tag verification and "
            f"{state} publication with diagnostics; use continue to recheck. "
            "Tag presence alone does not establish pending approval."
        )
    if local or staged:
        return (
            "Partial signed/staged state: inspect Create Platform Release Tag runs manually. "
            "Do not rerun publish, overwrite, delete, or restage automatically."
        )
    if state != "missing":
        return "Release metadata exists without a final tag; inspect diagnostics before publishing."
    return "Complete and merge the release PR through repository checks, then publish."


def continue_release(
    runner: CommandRunner,
    repository: Repository,
    tag: str | None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Recheck an existing final publication without replaying any release mutation."""
    progress = inspect_progress(runner, repository, tag)
    if progress["remote_final_object"]:
        if not progress["local_signed_object"]:
            raise ReleaseError(
                "fetch the final signed tag into the release checkout separately, then continue"
            )
        status = cast(dict[str, object], progress["status"])
        _wait_for_publication(
            runner,
            repository,
            cast(str, status["tag"]),
            cast(str, progress["remote_final_object"]),
            sleep,
            cast(str, progress["local_signed_commit"]),
        )
        progress["publication_verified"] = True
        progress["release_state"] = "published"
        progress["release_url"] = (
            f"https://github.com/{repository.slug}/releases/tag/{status['tag']}"
        )
        progress["verifier_status"] = "success"
        progress["next_action"] = (
            "Publication verified. No client adoption or cluster action authorized."
        )
    return progress


def _select_check_pr(
    runner: CommandRunner, repository: Repository, args: argparse.Namespace, prompt: Prompt | None
) -> dict[str, Any]:
    """Select an open owned release PR, never falling back to checkout VERSION."""
    tag = _tag(args.tag) if getattr(args, "tag", None) else None
    prs = _release_prs(runner, repository, "open", tag)
    prs = [pr for pr in prs if pr.get("state") == "OPEN"]
    if getattr(args, "pr", None) is not None:
        prs = [pr for pr in prs if pr["number"] == args.pr]
    if not prs:
        raise ReleaseError(
            "no matching open same-repository release PR targeting the default branch; "
            "inspect Release status, prepare a draft separately, or select check --tag/--pr. "
            "The original checkout was not validated."
        )
    choices = {
        f"{pr['headRefName'].removeprefix('release/')} | PR #{pr['number']}": pr for pr in prs
    }
    if len(prs) > 1:
        if prompt is None:
            raise ReleaseError(
                "multiple release PRs; select check --tag or --pr: " + ", ".join(choices)
            )
        pr = choices[_choose(prompt, "Release PR to validate", list(choices))]
    else:
        pr = prs[0]
    if (
        not SHA_PATTERN.fullmatch(pr.get("headRefOid", ""))
        or type(pr.get("number")) is not int
        or pr["number"] <= 0
    ):
        raise ReleaseError("invalid release PR identity")
    return pr


def _check_pr_head(runner: CommandRunner, repository: Repository, selected: dict[str, Any]) -> None:
    """Require the selected PR to remain open, owned, and at its observed head."""
    prs = _release_prs(runner, repository, "open", selected["headRefName"].removeprefix("release/"))
    matches = [
        pr for pr in prs if pr.get("number") == selected["number"] and pr.get("state") == "OPEN"
    ]
    if len(matches) != 1 or matches[0].get("headRefOid") != selected["headRefOid"]:
        raise StaleTargetError(
            "release PR head or state changed; select it again for a fresh check"
        )


def check_release(runner: CommandRunner, repository: Repository, pr: dict[str, Any]) -> None:
    """Validate the exact selected PR and preserve canonical failures, including TODOs."""
    target = pr["headRefOid"]
    tag = pr["headRefName"].removeprefix("release/")
    _clean_target(runner, repository, target)
    version = _checked(runner, ("git", "show", "HEAD:VERSION"), cwd=repository.path)
    if version != tag.removeprefix("v"):
        raise ReleaseError(
            f"selected {tag} does not match PR VERSION {version}; no checks executed"
        )
    print(
        f"Validate {tag} | PR #{pr['number']} | https://github.com/{repository.slug}/pull/{pr['number']}\n"
        f"SHA: {target}\nWorktree: {repository.path}"
    )
    changelog = _checked(runner, ("git", "show", "HEAD:CHANGELOG.md"), cwd=repository.path)
    print(f"\nCHANGELOG.md [{version}]\n{_unreleased_notes(changelog, version)}")
    evidence = [
        _unreleased_notes(changelog, version),
        _checked(runner, ("git", "show", f"HEAD:release/migrations/{tag}.md"), cwd=repository.path),
        _checked(runner, ("git", "show", "HEAD:release/config.yaml"), cwd=repository.path),
    ]
    if any(re.search(r"\bTODO\b", text, re.IGNORECASE) for text in evidence):
        raise ReleaseError(
            "Release documentation is unfinished. Use 2. Review notes (finish-notes), "
            "review and upload the saved draft, then retry 3. Check. "
            "Full checks remain required."
        )
    _check_pr_head(runner, repository, pr)
    failures = run_checks(runner, repository)
    _check_pr_head(runner, repository, pr)
    _clean_target(runner, repository, target)
    if failures:
        raise ReleaseError("; ".join(failures))
    print(
        "Manual handoff: review and merge "
        f"https://github.com/{repository.slug}/pull/{pr['number']} "
        "with 4. Merge on GitHub: mark Ready for review, wait for green checks, "
        "and resolve required reviews. Then choose 5. Publish. "
        "Validation does not merge, sign, publish or deploy."
    )


def run_checks(runner: CommandRunner, repository: Repository) -> list[str]:
    """Run both canonical checks, retaining every ordinary failure and full private logs."""
    failures = []
    for command in (("make", "check"), ("make", "release-check")):
        print(f"\nRunning {' '.join(command)}", flush=True)
        started = time.monotonic()
        result = runner.run_live(command, cwd=repository.path)
        print(
            f"{'FAIL' if result.returncode else 'PASS'} {' '.join(command)} "
            f"({time.monotonic() - started:.1f}s)"
        )
        if result.returncode:
            failures.append(
                f"{' '.join(command)} failed (exit {result.returncode})"
                + (f": {result.stderr.strip()}" if result.stderr.strip() else "")
            )
    return failures


def _select_command(args: argparse.Namespace, prompt: Prompt, isatty: bool | None) -> bool:
    """Validate interactive mode before accessing any repository."""
    interactive = args.command is None
    if interactive:
        if not (sys.stdin.isatty() if isatty is None else isatty):
            raise ReleaseError("a command is required outside an interactive terminal")
        if args.as_json:
            raise ReleaseError("--json requires an explicit status or plan command")
        args.command = _interactive_command(prompt)
    if args.as_json and args.command not in ("status", "plan"):
        raise ReleaseError("--json is available only for read-only status and plan")
    if (
        args.command == "finish-notes"
        and isinstance(prompt, TerminalPrompt)
        and not (sys.stdin.isatty() if isatty is None else isatty)
    ):
        raise ReleaseError("finish-notes requires an interactive terminal for human judgments")
    if (
        args.command in ("prepare", "publish")
        and isinstance(prompt, TerminalPrompt)
        and not (sys.stdin.isatty() if isatty is None else isatty)
        and not (args.allow_local_preparation and args.confirm)
    ):
        raise ReleaseError(
            "noninteractive mutations require --allow-local-preparation and --confirm"
        )
    return interactive


def _prepare_check(
    args: argparse.Namespace,
    runner: CommandRunner,
    repository: Repository,
    prompt: Prompt,
    isatty: bool | None,
) -> Repository:
    """Authorize selected unmerged code and prepare only its isolated checkout."""
    can_prompt = not isinstance(prompt, TerminalPrompt) or (
        sys.stdin.isatty() if isatty is None else isatty
    )
    pr = _select_check_pr(runner, repository, args, prompt if can_prompt else None)
    if not args.allow_local_preparation:
        if not can_prompt:
            raise ReleaseError(
                "check requires --allow-local-preparation to fetch and execute "
                "trusted same-repository PR code"
            )
        if (
            prompt.ask(
                f"Trust and execute unmerged same-repository code for {pr['headRefName']} "
                f"PR #{pr['number']} at {pr['headRefOid']} from {repository.slug}? "
                "Fetch origin and create/reuse an isolated worktree "
                "(original files untouched)? [yes/no]: "
            )
            != "yes"
        ):
            raise ReleaseError("validation preparation declined; no code executed")
    args.selected_pr = pr
    if args.command == "finish-notes":
        from platform_release.notes import editable_checkout

        return editable_checkout(runner, repository, args.worktree_root, pr)
    return _release_checkout(runner, repository, args.worktree_root, pr=pr)


def _prepare_default_checkout(
    args: argparse.Namespace, runner: CommandRunner, repository: Repository, prompt: Prompt
) -> Repository:
    """Authorize routine default-branch fetch separately from publication or dispatch."""
    if (
        not args.allow_local_preparation
        and prompt.ask(
            f"Fetch {repository.slug} origin {repository.default_branch}/tags "
            "and create/reuse an isolated release worktree (original files untouched)? [yes/no]: "
        )
        != "yes"
    ):
        raise ReleaseError("local preparation declined")
    return _release_checkout(runner, repository, args.worktree_root)


def run_cli(  # noqa: C901 - Explicit lifecycle dispatch keeps mutation boundaries visible.
    arguments: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    prompt: Prompt | None = None,
    isatty: bool | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run explicit commands once, returning to the menu after read-only actions."""
    args = parse_arguments(arguments)
    active_runner = runner or SubprocessRunner(verbose=args.verbose)
    active_prompt = prompt or TerminalPrompt()
    try:
        if args.command == "guide":
            print(GUIDE)
            return 0
        if args.command is None:
            print(GUIDE)
        while True:
            interactive = _select_command(args, active_prompt, isatty)
            if args.command == "quit":
                return 0
            repository = _repository(active_runner, args.base_repo)
            if args.command in ("check", "finish-notes"):
                repository = _prepare_check(args, active_runner, repository, active_prompt, isatty)
            if args.command in ("prepare", "publish"):
                repository = _prepare_default_checkout(
                    args, active_runner, repository, active_prompt
                )
            if interactive:
                args.diagnostics = args.command == "diagnostics"
                args.changelog = args.command == "changelog"
                if args.diagnostics or args.changelog:
                    args.command = "status"
                _wizard(args, active_runner, repository, active_prompt)
            _execute(args, active_runner, repository, active_prompt, sleep)
            if not interactive or args.command in ("prepare", "publish"):
                return 0
            # Discard every previous action's inputs before displaying a new menu.
            args = parse_arguments(arguments)
    except KeyboardInterrupt:
        print("Validation cancelled.", file=sys.stderr)
        return 130
    except (
        ReleaseError,
        ValueError,
        TypeError,
        KeyError,
        OSError,
        EOFError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def _execute(
    args: argparse.Namespace,
    runner: CommandRunner,
    repository: Repository,
    prompt: Prompt,
    sleep: Callable[[float], None],
) -> None:
    """Execute one selected action without replaying it on menu return."""
    if args.command in ("status", "continue", "merge"):
        report = (
            inspect_progress(runner, repository, args.tag)
            if args.command in ("status", "merge")
            else continue_release(runner, repository, args.tag, sleep)
        )
        print(_render(report, args.as_json, diagnostics=args.diagnostics, changelog=args.changelog))
        if args.command == "merge":
            print(
                "On the release PR above: Ready for review, wait for green checks, "
                "resolve required reviews, then merge on GitHub. No merge was performed."
            )
    elif args.command == "plan":
        print(_render(plan(runner, repository, args.version), args.as_json))
    elif args.command in ("prepare", "publish"):
        for attempt in range(3):
            try:
                result = (
                    prepare(runner, repository, args, prompt)
                    if args.command == "prepare"
                    else publish(runner, repository, args, sleep, prompt)
                )
                print(result)
                break
            except StaleTargetError:
                if args.confirm is not None or attempt == 2:
                    raise
                print("Remote tip changed; revalidating before requesting a fresh confirmation.")
                repository = _release_checkout(runner, repository, args.worktree_root)
    elif args.command == "check":
        from platform_release.notes import check_updates

        repository = check_updates(runner, repository, args, prompt)
        check_release(runner, repository, args.selected_pr)
        print("Full Base and pre-tag release checks passed.")
    elif args.command == "finish-notes":
        from platform_release.notes import finish_notes

        finish_notes(runner, repository, args, prompt)
    else:
        raise ReleaseError("unknown action; no mutation authorized")


def main() -> None:
    """Run the console-script entry point."""
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
