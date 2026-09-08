"""Upload reviewed release evidence only after explicit operator authorization."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any

from platform_release import main as m
from platform_release import notes as n
from platform_release.commands import CommandRunner
from platform_release.compact import section_bounds

DIFF = ("git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames")


def _push(runner: CommandRunner, repo: m.Repository, head: str, ref: str, base: str) -> None:
    """Bind a non-force push to the authorized advertisement without replacing hooks."""
    if (
        m._checked(runner, ("git", "rev-list", "--parents", "-n", "1", head), cwd=repo.path)
        != f"{head} {base}"
    ):
        raise m.ReleaseError("upload commit must have exactly the authorized PR head as its parent")
    hooks = Path(
        m._checked(
            runner,
            ("git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"),
            cwd=repo.path,
        )
    )
    with tempfile.TemporaryDirectory(prefix="platform-release-push-") as directory:
        scoped = Path(directory)
        if hooks.is_dir():
            for hook in hooks.iterdir():
                if hook.name != "pre-push":
                    (scoped / hook.name).symlink_to(hook)
        command = shlex.join(
            [
                sys.executable,
                str(Path(__file__).with_name("push_hook.py")),
                head,
                ref,
                base,
                str(hooks / "pre-push"),
            ]
        )
        wrapper = scoped / "pre-push"
        wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
        wrapper.chmod(0o700)
        # The hook checks the advertised old OID; receive-pack locks and compares
        # that same OID at update time. No lease/force or persistent hook changes.
        m._checked(
            runner,
            (
                "git",
                "-c",
                f"core.hooksPath={scoped}",
                "push",
                "--atomic",
                "--no-follow-tags",
                "origin",
                f"{head}:{ref}",
            ),
            cwd=repo.path,
        )


def public_check(original: Path, explicit: Path | None) -> Path | None:
    """Discover the workstation guard, never from the downloaded PR checkout."""
    original = original.expanduser().resolve()
    candidates = (
        [explicit.expanduser().resolve()]
        if explicit
        else [
            parent / ".config/confidentiality-guard/public-pr-check"
            for parent in (original, *original.parents)
        ]
    )
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _empty_index(runner: CommandRunner, repo: m.Repository) -> None:
    """Preserve any pre-existing staged work rather than including or unstaging it."""
    if m._checked(runner, ("git", "diff", "--cached", "--name-only"), cwd=repo.path):
        raise m.ReleaseError(
            "staged index is not empty. Local notes are retained; inspect the staged diff "
            "and resolve it before retrying. Nothing was unstaged automatically."
        )


def _gate(
    runner: CommandRunner, repo: m.Repository, args: argparse.Namespace, head: str, branch: str
) -> None:
    """Bind writes to the original public repository and unchanged open PR branch."""
    original = m._repository(runner, args.base_repo)
    if (
        (original.slug, original.default_branch) != (repo.slug, repo.default_branch)
        or m._repository(runner, repo.path) != repo
        or m._checked(runner, ("git", "rev-parse", "HEAD"), cwd=repo.path) != head
        or m._checked(runner, ("git", "branch", "--show-current"), cwd=repo.path) != branch
    ):
        raise m.ReleaseError("repository or local checkout changed; no upload authorized")
    pr = args.selected_pr
    m._check_pr_head(runner, repo, pr)
    if m._remote_ref(runner, repo, f"refs/heads/{pr['headRefName']}") != pr["headRefOid"]:
        raise m.StaleTargetError(
            "remote release branch moved; preserve local notes and reselect the PR"
        )


def _review(runner: CommandRunner, repo: m.Repository, base: str) -> str:
    """Show the complete upload diff, not merely the last wizard's local edits."""
    for command in (
        ("git", "status", "--short"),
        (*DIFF, "--cached"),
        ("git", "log", "--oneline", "-10"),
    ):
        m._checked(runner, command, cwd=repo.path)
    return m._checked(runner, (*DIFF, base, "--"), cwd=repo.path)


def _unchanged(
    runner: CommandRunner, repo: m.Repository, base: str, preview: str, snapshot: dict[str, str]
) -> None:
    """Do not stage or push evidence changed by a concurrent edit or hook."""
    paths = tuple(snapshot)
    n._evidence_only(runner, repo, paths)
    if (
        n._snapshot(repo, paths) != snapshot
        or m._checked(runner, (*DIFF, base, "--"), cwd=repo.path) != preview
    ):
        raise m.ReleaseError("evidence changed after preview; retain local work and review again")


def _commit(
    runner: CommandRunner,
    repo: m.Repository,
    args: argparse.Namespace,
    preview: str,
    guard: Path,
    branch: str,
) -> str:
    """Stage only the reviewed evidence, keep hooks enabled, and verify their result."""
    pr = args.selected_pr
    base = pr["headRefOid"]
    changed = m._checked(
        runner, ("git", "diff", "--name-only", "--no-renames", "-z", base, "--"), cwd=repo.path
    ).split("\0")
    paths = n.evidence_paths(pr["headRefName"].removeprefix("release/"))
    intended = [path for path in changed if path]
    if not intended or any(path not in paths for path in intended):
        raise m.ReleaseError("upload must change only the five release evidence files")
    _empty_index(runner, repo)
    m._checked(runner, ("git", "add", "--", *intended), cwd=repo.path)
    staged = m._checked(runner, (*DIFF, "--cached", base, "--"), cwd=repo.path)
    if staged != preview:
        raise m.ReleaseError("staged diff differs from the confirmed preview; no commit created")
    tree = m._checked(runner, ("git", "write-tree"), cwd=repo.path)
    m._checked(runner, (str(guard), "--scan-only"), cwd=repo.path)
    _gate(runner, repo, args, base, branch)
    n._evidence_only(runner, repo, paths)
    if m._checked(runner, (*DIFF, "--cached", base, "--"), cwd=repo.path) != preview:
        raise m.ReleaseError("staged content changed before commit; preserve and review it")
    m._checked(runner, ("git", "diff", "--exit-code"), cwd=repo.path)
    m._checked(
        runner,
        ("git", "commit", "-m", f"docs: finish platform {pr['headRefName'][8:]} release notes"),
        cwd=repo.path,
    )
    head = n.notes_head(runner, repo, pr)
    if (
        head == base
        or m._checked(runner, ("git", "rev-parse", "HEAD^{tree}"), cwd=repo.path) != tree
    ):
        raise m.ReleaseError(
            "commit or hooks changed the reviewed tree; inspect the retained local commit"
        )
    return head


def _upload(
    runner: CommandRunner,
    repo: m.Repository,
    args: argparse.Namespace,
    guard: Path,
    preview: str,
    head: str,
) -> None:
    """Validate locally, commit when needed, then push one explicit non-force ref."""
    pr = args.selected_pr
    base = pr["headRefOid"]
    paths = n.evidence_paths(pr["headRefName"].removeprefix("release/"))
    snapshot = n._snapshot(repo, paths)
    branch = f"release-notes/{pr['number']}-{base}"
    # Prepared PRs already track all five files. This also ensures the guard scans
    # all evidence before staging, rather than missing a new untracked document.
    m._checked(runner, ("git", "ls-files", "--error-unmatch", "--", *paths), cwd=repo.path)
    failures = m.run_checks(runner, repo)
    if failures:
        raise m.ReleaseError("; ".join(failures))
    m._checked(runner, (str(guard), "--scan-only"), cwd=repo.path)
    _gate(runner, repo, args, head, branch)
    _unchanged(runner, repo, base, preview, snapshot)
    if head == base:
        head = _commit(runner, repo, args, preview, guard, branch)
    # Re-scan after commit so commit messages and hook side effects are covered.
    m._checked(runner, (str(guard), "--scan-only"), cwd=repo.path)
    _empty_index(runner, repo)
    _unchanged(runner, repo, base, preview, snapshot)
    if m._checked(runner, ("git", "status", "--porcelain"), cwd=repo.path):
        raise m.ReleaseError("notes checkout is no longer clean; inspect it before pushing")
    _gate(runner, repo, args, head, branch)
    _push(runner, repo, head, f"refs/heads/{pr['headRefName']}", base)


def _format_for_upload(
    runner: CommandRunner,
    repo: m.Repository,
    args: argparse.Namespace,
    prompt: m.Prompt,
    preview: str,
    head: str,
    *,
    confirmed: bool = True,
) -> str:
    """Run configured hooks before staging; permit one bounded formatting-fix retry."""
    pr = args.selected_pr
    paths = n.evidence_paths(pr["headRefName"].removeprefix("release/"))
    n._evidence_only(runner, repo, paths)
    m._checked(runner, ("git", "ls-files", "--error-unmatch", "--", *paths), cwd=repo.path)
    before = n._snapshot(repo, paths)
    command = ("pre-commit", "run", "--files", *paths)
    sys.stdout.write("Running formatting and pre-commit checks\n")
    result = runner.run_live(command, cwd=repo.path)
    n._evidence_only(runner, repo, paths)
    _empty_index(runner, repo)
    after = n._snapshot(repo, paths)
    changed = [name for name in paths if before[name] != after[name]]
    if changed and head != pr["headRefOid"]:
        raise m.ReleaseError("hooks modified a pending commit; preserve it and review manually")
    if changed:
        sys.stdout.write("Formatting updated: " + ", ".join(changed) + "\n")
        preview = _review(runner, repo, pr["headRefOid"])
        can_prompt = not isinstance(prompt, m.TerminalPrompt) or sys.stdin.isatty()
        if can_prompt and prompt.ask("Show full file changes? [yes/No]: ") == "yes":
            sys.stdout.write(preview + "\n")
        # A fixing hook returns 1. Never retry a failure unless files actually changed.
        if result.returncode in (0, 1):
            result = runner.run_live(command, cwd=repo.path)
        _unchanged(runner, repo, pr["headRefOid"], preview, after)
        _empty_index(runner, repo)
    sys.stdout.write(f"{'FAIL' if result.returncode else 'PASS'} formatting and pre-commit\n")
    if result.returncode:
        failures = m.run_checks(runner, repo)
        raise m.ReleaseError("pre-commit failed: " + result.stderr + "; " + "; ".join(failures))
    if (
        changed
        and confirmed
        and prompt.ask("Upload these formatting corrections too? [yes/No]: ") != "yes"
    ):
        raise m.ReleaseError("formatting corrections retained locally; no upload authorized")
    return preview


def offer_upload(
    runner: CommandRunner,
    repo: m.Repository,
    args: argparse.Namespace,
    prompt: m.Prompt,
    *,
    compact_renderer: bool | None = None,
    formatting_checked: bool = False,
) -> bool:
    """Offer a default-No upload and report completion only after push succeeds."""
    pr: dict[str, Any] = args.selected_pr
    url = f"https://github.com/{repo.slug}/pull/{pr['number']}"
    sys.stdout.write(f"NOT UPLOADED. Local notes: {repo.path}\nRelease PR: {url}\n")
    _empty_index(runner, repo)
    head = n.notes_head(runner, repo, pr)
    if compact_renderer is None:
        n._evidence_only(runner, repo, n.evidence_paths(pr["headRefName"].removeprefix("release/")))
        compact_renderer = bool(n._canonical(runner, repo).get("compact_renderer"))
    label = "Final release notes preview"
    if not compact_renderer:
        sys.stdout.write(
            "This release branch uses an older Base renderer. Compact publication is not "
            "available until the Base renderer change is merged and this release branch "
            "is refreshed.\n"
        )
        label = "Proposed notes (not actual publication output until Base updated)"
    preview = _review(runner, repo, pr["headRefOid"])
    if getattr(args, "verbose", False):
        sys.stdout.write(f"\nExact changes proposed for the release PR:\n{preview}\n")
    tag = pr["headRefName"].removeprefix("release/")
    changelog = (repo.path / "CHANGELOG.md").read_text()
    start, end = section_bounds(changelog, tag[1:])
    body = changelog[start:end].strip()
    sys.stdout.write(f"\n{label}:\n## {tag}\n\n{body}\n")
    if not preview:
        sys.stdout.write("No changes to upload. Select 3. Check for the current PR.\n")
        return False
    changed = m._checked(
        runner,
        ("git", "diff", "--name-only", "--no-renames", pr["headRefOid"], "--"),
        cwd=repo.path,
    )
    sys.stdout.write("Changed release files:\n" + changed + "\n")
    if isinstance(prompt, m.TerminalPrompt) and not sys.stdin.isatty():
        raise m.ReleaseError("corrections remain local; use interactive check to authorize upload")
    question = "Update release PR?"
    accepted = (
        m.questionary.confirm(question, default=False).ask() is True
        if isinstance(prompt, m.TerminalPrompt)
        else prompt.ask(question + " [yes/No]: ") == "yes"
    )
    if not accepted:
        sys.stdout.write("Kept locally; rerun Review notes to review and upload later.\n")
        return False
    guard = public_check(args.base_repo, getattr(args, "public_check", None))
    if guard is None:
        sys.stdout.write(
            "Upload unavailable: no executable public confidentiality guard found. "
            "Use --public-check /path/to/public-pr-check before finish-notes, or ask a maintainer "
            "to configure .config/confidentiality-guard above the original Base checkout. "
            "Notes remain local; no commit or push attempted.\n"
        )
        return False
    try:
        if not formatting_checked:
            preview = _format_for_upload(runner, repo, args, prompt, preview, head)
        _upload(runner, repo, args, guard, preview, head)
    except m.ReleaseError as error:
        raise m.ReleaseError(
            f"Upload did not complete: {error}\n"
            "Local files, staged changes and any commit are retained "
            f"at {repo.path}. Inspect Release status before retrying an uncertain push. "
            "A clean single evidence commit on the unchanged PR head can be retried with "
            "finish-notes without another commit; resolve staged changes or other history manually."
        ) from error
    sys.stdout.write(
        f"UPDATED release PR: {url}\nNext: 3. Check. Full make check and release-check "
        "remain required on the uploaded PR. Merge manually after CI and review, "
        "before 5. Publish. No merge, tag or publication was performed.\n"
    )
    return True
