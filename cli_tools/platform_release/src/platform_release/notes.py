"""Guide local release prose without inventing transition or acceptance evidence."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

from platform_release import main as m
from platform_release.commands import CommandRunner
from platform_release.compact import draft as draft_notes

TODO = re.compile(r"\bTODO\b", re.IGNORECASE)

# Execute only after the operator authorizes the selected Base PR's code. Base owns
# serialization, inventory, schema and the exact compatibility declaration grammar.
CANONICAL = r"""
import json, runpy, sys, tempfile
from pathlib import Path
b = runpy.run_path('scripts/platform_release.py')
config = b['load_yaml'](b['CONFIG_PATH'])
if len(sys.argv) > 1:
    config['summary'] = sys.argv[1]
if config.get('summary') is not None and not isinstance(config['summary'], str):
    raise b['ReleaseError']('release summary must be text or null; fix its config type')
loader = b['load_yaml']
def load(path):
    return config if path == b['CONFIG_PATH'] else loader(path)
b['build_manifest'].__globals__['load_yaml'] = load
manifest = b['build_manifest']()
b['validate_manifest_schema'](manifest)
c = config['compatibility']
scaffold = b['migration_scaffold'](
    config['version'], c['stableUpgrade'], c['upgradesFromAlphaRevisions'], c['recovery'])
with tempfile.TemporaryDirectory(prefix='platform-notes-capability-') as directory:
    root = Path(directory)
    (root / 'release/migrations').mkdir(parents=True)
    (root / 'VERSION').write_text('1.2.3\n')
    (root / 'CHANGELOG.md').write_text('## [1.2.3] - 2026-01-01\n\n- Existing fix.\n')
    (root / 'release/migrations/v1.2.3.md').write_text('INTERNAL METADATA\n')
    compact = b['render_release_notes'](root).strip() == '## v1.2.3\n\n- Existing fix.'
print(json.dumps({
    'config': config,
    'config_text': b['yaml'].safe_dump(config, sort_keys=False, width=100),
    'manifest': b['yaml'].safe_dump(manifest, sort_keys=False, width=100),
    'scaffold': scaffold,
    'compact_renderer': compact,
}, default=str))
"""


def _canonical(
    runner: CommandRunner, repo: m.Repository, summary: str | None = None
) -> dict[str, Any]:
    """Ask this PR's canonical Base generator for its current contract."""
    command = ["uv", "run", "--frozen", "python", "-c", CANONICAL]
    if summary is not None:
        command.append(summary)
    return json.loads(m._checked(runner, command, cwd=repo.path))


def editable_checkout(
    runner: CommandRunner, repo: m.Repository, root: Path | None, pr: dict[str, Any]
) -> m.Repository:
    """Keep a dedicated edit branch separate from immutable validation snapshots."""
    if m._repository(runner, repo.path) != repo:
        raise m.ReleaseError("repository identity changed before notes preparation")
    common = Path(
        m._checked(
            runner,
            ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"),
            cwd=repo.path,
        )
    ).resolve()
    root = (root or common.parent.parent / "worktrees").expanduser().resolve()
    if root.is_relative_to(repo.path) or root.is_relative_to(common.parent):
        raise m.ReleaseError("worktree root must be outside the selected and primary checkouts")
    name = f"platform-notes-{repo.slug.replace('/', '-')}-{pr['number']}-{pr['headRefOid']}"
    path = root / name
    branch = f"release-notes/{pr['number']}-{pr['headRefOid']}"
    if path == repo.path:
        raise m.ReleaseError("select the original Base checkout, not the editable notes checkout")
    if path.exists() or path.is_symlink():
        registered = m._checked(runner, ("git", "worktree", "list", "--porcelain"), cwd=repo.path)
        if path.is_symlink() or f"worktree {path}\n" not in registered + "\n":
            raise m.ReleaseError("notes destination is not a registered linked worktree")
        linked = m._checked(
            runner, ("git", "rev-parse", "--path-format=absolute", "--git-common-dir"), cwd=path
        )
        current = m._checked(runner, ("git", "branch", "--show-current"), cwd=path)
        if Path(linked).resolve() != common or current != branch:
            raise m.ReleaseError("notes checkout identity changed; preserve it and review manually")
        notes_head(runner, m.Repository(path, repo.slug, repo.default_branch), pr)
    m._fetch_pr(runner, repo, pr)
    if not path.exists():
        root.mkdir(parents=True, exist_ok=True)
        m._checked(
            runner,
            ("git", "worktree", "add", "-b", branch, str(path), pr["headRefOid"]),
            cwd=repo.path,
        )
    return m.Repository(path, repo.slug, repo.default_branch)


def _snapshot(repo: m.Repository, paths: tuple[str, ...]) -> dict[str, str]:
    """Read only the evidence allowlist without following links out of the checkout."""
    result = {}
    for name in paths:
        path = repo.path / name
        if path.is_symlink() or not path.resolve().is_relative_to(repo.path):
            raise m.ReleaseError(
                "release evidence must be ordinary files inside the notes checkout"
            )
        result[name] = path.read_text() if path.exists() else ""
    return result


def _evidence_only(runner: CommandRunner, repo: m.Repository, paths: tuple[str, ...]) -> None:
    """Do not generate release inventory from unrelated uncommitted implementation."""
    result = runner.run(
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"), cwd=repo.path
    )
    if result.returncode:
        raise m.ReleaseError("could not inspect notes checkout status: " + result.stderr)
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if "R" in entry[:2] or "C" in entry[:2] or entry[3:] not in paths:
            raise m.ReleaseError(
                "notes checkout has non-evidence edits; preserve and review them manually"
            )


def evidence_paths(tag: str) -> tuple[str, ...]:
    """Return Base's five allowed release evidence files."""
    return (
        "VERSION",
        "CHANGELOG.md",
        "release/config.yaml",
        "release/manifest.yaml",
        f"release/migrations/{tag}.md",
    )


def notes_head(runner: CommandRunner, repo: m.Repository, pr: dict[str, Any]) -> str:
    """Accept the exact PR head or one clean, evidence-only child pending upload."""
    head = m._checked(runner, ("git", "rev-parse", "HEAD"), cwd=repo.path)
    if head == pr["headRefOid"]:
        return head
    parents = m._checked(runner, ("git", "rev-list", "--parents", "-n", "1", "HEAD"), cwd=repo.path)
    changed = m._checked(
        runner,
        ("git", "diff", "--name-only", "--no-renames", "-z", pr["headRefOid"], "HEAD", "--"),
        cwd=repo.path,
    )
    paths = evidence_paths(pr["headRefName"].removeprefix("release/"))
    if (
        not m.SHA_PATTERN.fullmatch(head)
        or parents.split() != [head, pr["headRefOid"]]
        or not changed
        or any(name not in paths for name in changed.split("\0") if name)
        or m._checked(runner, ("git", "status", "--porcelain"), cwd=repo.path)
    ):
        raise m.ReleaseError(
            "notes checkout identity changed or pending commit is not clean evidence-only; "
            "preserve it and review manually. No reset or duplicate commit attempted."
        )
    return head


def _complete_summary(
    runner: CommandRunner, repo: m.Repository, data: dict[str, Any], prompt: m.Prompt
) -> dict[str, Any]:
    """Treat null as missing and never turn malformed config values into prose."""
    summary = data["config"].get("summary")
    if summary is not None and not isinstance(summary, str):
        raise m.ReleaseError(
            "release summary must be text or null; fix its config type before continuing"
        )
    if summary is None or not summary.strip() or TODO.search(summary):
        answer = prompt.ask(
            "Release summary: what does this release change? Enter keeps an unfinished TODO: "
        ).strip()
        replacement = answer or summary or "TODO: Describe the release changes."
        if not replacement.strip():
            replacement = "TODO: Describe the release changes."
        if replacement != summary:
            return _canonical(runner, repo, replacement)
    return data


def finish_notes(
    runner: CommandRunner, repo: m.Repository, args: argparse.Namespace, prompt: m.Prompt
) -> None:
    """Preview and save drafts, then offer a separately confirmed PR update."""
    from platform_release.upload import offer_upload

    pr = args.selected_pr
    tag = pr["headRefName"].removeprefix("release/")
    paths = evidence_paths(tag)
    if notes_head(runner, repo, pr) != pr["headRefOid"]:
        offer_upload(runner, repo, args, prompt)
        return
    _evidence_only(runner, repo, paths)
    before = _snapshot(repo, paths)
    branch = m._checked(runner, ("git", "branch", "--show-current"), cwd=repo.path)
    if (
        before["VERSION"].strip() != tag[1:]
        or m._checked(runner, ("git", "rev-parse", "HEAD"), cwd=repo.path) != pr["headRefOid"]
    ):
        raise m.ReleaseError("notes PR VERSION or HEAD does not match selected release")
    data = _canonical(runner, repo)
    summary = data["config"].get("summary")
    data = _complete_summary(runner, repo, data, prompt)
    migration, changelog = draft_notes(
        before[paths[-1]] or data["scaffold"],
        before["CHANGELOG.md"],
        data["scaffold"],
        tag[1:],
        prompt,
    )
    after = {
        **before,
        paths[-1]: migration,
        "CHANGELOG.md": changelog,
        "release/manifest.yaml": data["manifest"],
        "release/config.yaml": (
            data["config_text"]
            if data["config"]["summary"] != summary
            else before["release/config.yaml"]
        ),
    }
    for name in paths if getattr(args, "verbose", False) else ():
        sys.stdout.write(
            "".join(
                difflib.unified_diff(
                    before[name].splitlines(True),
                    after[name].splitlines(True),
                    fromfile=name,
                    tofile=name,
                )
            )
        )
    sys.stdout.write("Draft changes are saved locally; only Update release PR authorizes upload.\n")
    m._check_pr_head(runner, repo, pr)
    _evidence_only(runner, repo, paths)
    if (
        _snapshot(repo, paths) != before
        or m._checked(runner, ("git", "rev-parse", "HEAD"), cwd=repo.path) != pr["headRefOid"]
        or m._checked(runner, ("git", "branch", "--show-current"), cwd=repo.path) != branch
    ):
        raise m.ReleaseError(
            "evidence or checkout changed during prompts; preserve edits and retry"
        )
    for name in paths:
        if after[name] != before[name]:
            (repo.path / name).write_text(after[name])
    sys.stdout.write(f"\nSaved local draft: {repo.path}\n")
    offer_upload(runner, repo, args, prompt, compact_renderer=bool(data.get("compact_renderer")))
