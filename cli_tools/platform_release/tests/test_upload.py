import argparse
from pathlib import Path
from typing import override
from unittest.mock import Mock

import pytest
from test_release import OLD, SHA, TAG, Fake, release_pr

from platform_release import main as m
from platform_release import notes as n
from platform_release import upload as u

COMMIT = "d" * 40
TREE = "e" * 40
CHANGED = ("CHANGELOG.md", f"release/migrations/{TAG}.md")
PUSH = (
    "git",
    "push",
    "--atomic",
    "--no-follow-tags",
    "origin",
    f"{COMMIT}:refs/heads/release/{TAG}",
)


class UploadRunner(Fake):
    @override
    def run(self, arguments, *, cwd=None):
        a = tuple(arguments)
        if a[:2] == ("git", "-c"):
            assert a[2].startswith("core.hooksPath=")
            assert (Path(a[2].split("=", 1)[1]) / "pre-push").is_file()
            a = ("git", *a[3:])
        return super().run(a, cwd=cwd)

    def __init__(self, repo):
        super().__init__()
        self.repo = repo
        self.heads[repo.path] = SHA
        self.index = False
        self.dirty = True
        self.remote = SHA
        self.tree = TREE
        self.move_after_commit = False
        self.preview = (
            "diff --git a/CHANGELOG.md b/CHANGELOG.md\n-reviewed TODO\n+reviewed evidence"
        )

    @override
    def respond(self, a):  # noqa: C901 - Explicit stateful command table keeps writes auditable.
        if a == ("git", "rev-list", "--parents", "-n", "1", COMMIT):
            return f"{COMMIT} {SHA}"
        if a == ("git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"):
            return str(self.repo.path / ".git/hooks")
        if a == ("git", "diff", "--cached", "--name-only"):
            return "\n".join(CHANGED) if self.index else ""
        if a[: len(u.DIFF)] == u.DIFF:
            if "--cached" in a:
                return self.preview if self.index else ""
            return self.preview
        if a[:3] == ("git", "diff", "--name-only"):
            return "\0".join(CHANGED) + "\0"
        if a == ("git", "diff", "--exit-code"):
            return ""
        if a[:2] == ("git", "status"):
            return " M CHANGELOG.md\0" if self.dirty else ""
        if a == ("git", "branch", "--show-current"):
            return f"release-notes/42-{SHA}"
        if a == ("git", "rev-list", "--parents", "-n", "1", "HEAD"):
            return f"{self.heads[self.repo.path]} {SHA}"
        if a == ("git", "write-tree") or a == ("git", "rev-parse", "HEAD^{tree}"):
            return self.tree
        if a[:2] == ("git", "ls-files"):
            return "\n".join(n.evidence_paths(TAG))
        if a[:2] == ("git", "ls-remote") and a[-1] == f"refs/heads/release/{TAG}":
            return f"{self.remote}\t{a[-1]}"
        if a[:2] == ("git", "add"):
            self.index = True
            return ""
        if a[:2] == ("git", "commit"):
            self.heads[self.repo.path] = COMMIT
            self.index = self.dirty = False
            if self.move_after_commit:
                self.remote = OLD
            return "committed"
        if a == PUSH:
            self.remote = self.heads[self.repo.path]
            return "pushed"
        if a[-1] == "--scan-only":
            return "confidentiality scan passed"
        return super().respond(a)


@pytest.fixture
def upload_setup(tmp_path, monkeypatch):
    original = tmp_path / "base"
    original.mkdir()
    (original / "user.txt").write_text("original user edit")
    path = tmp_path / "notes"
    (path / "release/migrations").mkdir(parents=True)
    repo = m.Repository(path, "example/base", "main")
    for name in n.evidence_paths(TAG):
        (path / name).write_text("reviewed evidence\n")
    (path / "CHANGELOG.md").write_text("## [1.0.1]\n- Fix routing.\n")
    guard = tmp_path / ".config/confidentiality-guard/public-pr-check"
    guard.parent.mkdir(parents=True)
    guard.write_text("#!/bin/sh\nexit 0\n")
    guard.chmod(0o700)
    runner = UploadRunner(repo)
    runner.overrides[("uv", "run", "--frozen", "python", "-c", n.CANONICAL)] = (
        '{"compact_renderer": true}'
    )
    pr = release_pr()
    monkeypatch.setattr(m, "_release_prs", lambda *a: [pr])
    args = argparse.Namespace(base_repo=original, public_check=None, selected_pr=pr)
    return runner, repo, args, guard


def git_writes(runner):
    return [
        a for a, _ in runner.calls if a[:2] in (("git", "add"), ("git", "commit"), ("git", "push"))
    ]


def test_confirmed_upload_scopes_all_writes_and_reports_success_only_after_push(
    upload_setup, capsys
):
    runner, repo, args, guard = upload_setup
    prompt = Mock(ask=Mock(return_value="yes"))
    u.offer_upload(runner, repo, args, prompt)
    assert git_writes(runner) == [
        ("git", "add", "--", *CHANGED),
        ("git", "commit", "-m", f"docs: finish platform {TAG} release notes"),
        PUSH,
    ]
    assert all(cwd == repo.path for a, cwd in runner.calls if a in git_writes(runner))
    commands = [a for a, _ in runner.calls]
    commit = next(i for i, a in enumerate(commands) if a[:2] == ("git", "commit"))
    push = commands.index(PUSH)
    assert commands.index(("make", "release-check")) < commit
    assert commands.index((str(guard), "--scan-only")) < commit
    assert (str(guard), "--scan-only") in commands[commit + 1 : push]
    remote = ("git", "ls-remote", "origin", f"refs/heads/release/{TAG}")
    assert remote in commands[:commit] and remote in commands[commit + 1 : push]
    assert ("git", "log", "--oneline", "-10") in commands[:commit]
    assert (*u.DIFF, "--cached", SHA, "--") in commands[:commit]
    output = capsys.readouterr().out
    assert "Exact changes proposed" not in output and "Staged diff for commit" not in output
    assert "git status" not in output and "git log" not in output
    assert output.count("Final release notes preview:") == 1
    assert "Final release notes preview:\n## v1.0.1\n\n- Fix routing.\n" in output
    prompt.ask.assert_called_once_with("Update release PR? [yes/No]: ")
    assert "UPDATED release PR: https://github.com/example/base/pull/42" in output
    assert "3. Validate release" in output
    assert (args.base_repo / "user.txt").read_text() == "original user edit"
    assert not any(
        flag in a for a in commands for flag in ("--force", "--amend", "--no-verify", "--admin")
    )


@pytest.mark.parametrize("answer", ["", "no", "yes please"])
def test_refusal_keeps_local_notes_without_staging(upload_setup, answer, capsys):
    runner, repo, args, _ = upload_setup
    u.offer_upload(runner, repo, args, Mock(ask=Mock(return_value=answer)))
    assert not git_writes(runner)
    assert not runner.live_calls
    assert "UPDATED release PR" not in capsys.readouterr().out


def test_terminal_confirmation_defaults_no(upload_setup, monkeypatch):
    runner, repo, args, _ = upload_setup
    confirm = Mock(return_value=Mock(ask=Mock(return_value=False)))
    monkeypatch.setattr(m.questionary, "confirm", confirm)
    u.offer_upload(runner, repo, args, m.TerminalPrompt())
    confirm.assert_called_once_with("Update release PR?", default=False)
    assert not git_writes(runner)


@pytest.mark.parametrize(
    "problem",
    [
        "bad-guard",
        "prose",
        "staged",
        "remote",
        "hook",
        "remote-after-commit",
        "push",
    ],
)
def test_upload_failures_never_claim_completion_or_discard_work(upload_setup, problem, capsys):
    runner, repo, args, guard = upload_setup
    if problem == "bad-guard":
        runner.overrides[(str(guard), "--scan-only")] = (1, "confidential identifier detected")
    elif problem == "prose":
        runner.overrides[("make", "release-check")] = (1, "TODO migration incomplete")
    elif problem == "staged":
        runner.index = True
    elif problem == "remote":
        runner.remote = OLD
    elif problem == "hook":
        runner.overrides[("git", "commit", "-m", f"docs: finish platform {TAG} release notes")] = (
            1,
            "commit hook failed",
        )
    elif problem == "remote-after-commit":
        runner.move_after_commit = True
    else:
        runner.overrides[PUSH] = (1, "non-fast-forward or transport failure")
    prompt = Mock(ask=Mock(return_value="yes"))
    with pytest.raises(m.ReleaseError):
        u.offer_upload(runner, repo, args, prompt)
    assert "UPDATED release PR" not in capsys.readouterr().out
    assert (repo.path / "CHANGELOG.md").read_text() == "## [1.0.1]\n- Fix routing.\n"
    assert not any(a == PUSH for a in git_writes(runner)) or problem == "push"
    if problem in ("bad-guard", "prose", "staged", "remote"):
        assert not git_writes(runner)
    if problem == "hook":
        assert runner.index and runner.heads[repo.path] == SHA
    if problem in ("remote-after-commit", "push"):
        assert runner.heads[repo.path] == COMMIT


def test_missing_guard_keeps_notes_local_without_git_writes(upload_setup, capsys):
    runner, repo, args, guard = upload_setup
    args.public_check = guard.parent / "missing"
    u.offer_upload(runner, repo, args, Mock(ask=Mock(return_value="yes")))
    assert not git_writes(runner) and not runner.live_calls
    output = capsys.readouterr().out
    assert "--public-check" in output and "NOT UPLOADED" in output
    assert "UPDATED release PR" not in output


def test_failed_push_retry_uses_existing_pending_commit_without_another_commit(
    upload_setup, capsys
):
    runner, repo, args, _ = upload_setup
    runner.overrides[PUSH] = (1, "transport failed")
    prompt = Mock(ask=Mock(return_value="yes"))
    with pytest.raises(m.ReleaseError, match="without another commit"):
        u.offer_upload(runner, repo, args, prompt)
    runner.overrides.pop(PUSH)
    runner.calls.clear()
    # The same Finish release notes entry point recognizes the retained child
    # and goes directly to its exact-diff review, without re-editing or committing.
    n.finish_notes(runner, repo, args, prompt)
    assert git_writes(runner) == [PUSH]
    assert "UPDATED release PR" in capsys.readouterr().out


@pytest.mark.parametrize("capability", [True, False, "failure"])
def test_pending_commit_checks_renderer_before_preview_or_upload(upload_setup, capsys, capability):
    runner, repo, args, _ = upload_setup
    runner.heads[repo.path] = COMMIT
    runner.dirty = False
    command = ("uv", "run", "--frozen", "python", "-c", n.CANONICAL)
    runner.overrides[command] = (
        (1, "renderer probe failed")
        if capability == "failure"
        else '{"compact_renderer": ' + str(capability).lower() + "}"
    )
    before = n._snapshot(repo, n.evidence_paths(TAG))

    def decline(message):
        assert command in [a for a, _ in runner.calls]
        assert message == "Update release PR? [yes/No]: "
        return ""

    prompt = Mock(ask=Mock(side_effect=decline))
    if capability == "failure":
        with pytest.raises(m.ReleaseError, match="renderer probe failed"):
            n.finish_notes(runner, repo, args, prompt)
        prompt.ask.assert_not_called()
        assert "## v1.0.1" not in capsys.readouterr().out
    else:
        n.finish_notes(runner, repo, args, prompt)
        prompt.ask.assert_called_once()
        output = capsys.readouterr().out
        if capability:
            assert output.count("Final release notes preview:") == 1
            assert "older Base renderer" not in output
        else:
            label = "Proposed notes (not actual publication output until Base updated):"
            assert output.index("older Base renderer") < output.index(label)
            assert output.count(label) == 1 and "Final release notes preview:" not in output
    assert not git_writes(runner)
    assert n._snapshot(repo, n.evidence_paths(TAG)) == before


@pytest.mark.parametrize("problem", ["parent", "extra-parent", "outside-file", "dirty"])
def test_pending_commit_must_be_single_clean_evidence_child(upload_setup, problem):
    runner, repo, args, _ = upload_setup
    runner.heads[repo.path] = COMMIT
    runner.dirty = False
    if problem in ("parent", "extra-parent"):
        runner.overrides[("git", "rev-list", "--parents", "-n", "1", "HEAD")] = (
            f"{COMMIT} {OLD}" if problem == "parent" else f"{COMMIT} {SHA} {OLD}"
        )
    elif problem == "outside-file":
        runner.overrides[
            ("git", "diff", "--name-only", "--no-renames", "-z", SHA, "HEAD", "--")
        ] = "scripts/changed.py\0"
    else:
        runner.dirty = True
    with pytest.raises(m.ReleaseError, match="pending commit"):
        n.finish_notes(runner, repo, args, Mock())
    assert not git_writes(runner)


def test_guard_discovery_uses_original_ancestry_and_explicit_override(upload_setup, tmp_path):
    _, _, args, guard = upload_setup
    assert u.public_check(args.base_repo, None) == guard
    assert u.public_check(tmp_path / "elsewhere", guard) == guard
    assert u.public_check(args.base_repo, Path("/missing/guard")) is None
    guard.chmod(0o600)
    assert u.public_check(args.base_repo, None) is None


@pytest.mark.parametrize(
    "phase", ["preview", "stage", "staged-guard", "commit-tree", "postcommit-guard", "origin"]
)
def test_changed_evidence_and_postcommit_checks_block_upload(upload_setup, monkeypatch, phase):
    runner, repo, args, guard = upload_setup
    run = runner.run
    scans = 0

    def intercept(arguments, *, cwd=None):
        nonlocal scans
        a = tuple(arguments)
        result = run(arguments, cwd=cwd)
        if a == ("make", "release-check") and phase == "preview":
            (repo.path / "CHANGELOG.md").write_text("concurrent user edit\n")
        if a[:2] == ("git", "add") and phase == "stage":
            runner.preview = "unreviewed staged diff"
        if a[:2] == ("git", "commit") and phase == "commit-tree":
            runner.tree = OLD
        if a == (str(guard), "--scan-only"):
            scans += 1
            if scans == 2 and phase == "origin":
                runner.overrides[("git", "remote", "get-url", "--push", "--all", "origin")] = (
                    "https://github.com/other/base.git"
                )
            if (scans == 2 and phase == "staged-guard") or (
                scans == 3 and phase == "postcommit-guard"
            ):
                return type(result)(
                    a, 1, "", "confidential identifier in evidence or commit message"
                )
        return result

    monkeypatch.setattr(runner, "run", intercept)
    with pytest.raises(m.ReleaseError):
        u.offer_upload(runner, repo, args, Mock(ask=Mock(return_value="yes")))
    assert PUSH not in git_writes(runner)
    if phase in ("preview", "stage", "staged-guard", "origin"):
        assert not any(a[:2] == ("git", "commit") for a in git_writes(runner))
        assert all(a == ("git", "add", "--", *CHANGED) for a in git_writes(runner))
        assert runner.index == bool(git_writes(runner))
    if phase == "preview":
        assert (repo.path / "CHANGELOG.md").read_text() == "concurrent user edit\n"


def test_public_check_override_is_a_global_option(tmp_path):
    args = m.parse_arguments(
        [
            "--base-repo",
            str(tmp_path),
            "--public-check",
            str(tmp_path / "guard"),
            "finish-notes",
            "--pr",
            "42",
        ]
    )
    assert args.public_check == tmp_path / "guard" and args.pr == 42
