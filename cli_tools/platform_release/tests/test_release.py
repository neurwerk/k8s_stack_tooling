import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from platform_release import main as m
from platform_release.commands import CommandResult, SubprocessRunner

SHA = "a" * 40
OBJECT = "b" * 40
OLD = "c" * 40
TAG = "v1.0.1"


def release_pr(**changes):
    return {
        "number": 42,
        "state": "OPEN",
        "headRefName": f"release/{TAG}",
        "headRefOid": SHA,
        "baseRefName": "main",
        "isCrossRepository": False,
        **changes,
    }


class Prompt:
    def __init__(self, *answers):
        self.answers = iter(answers)

    def ask(self, message):
        return next(self.answers)


class Fake:
    """Strict command-boundary fake: unexpected commands fail the test."""

    def __init__(self):
        self.calls = []
        self.overrides = {}
        self.local = False
        self.staged = False
        self.final = False
        self.worktrees = []
        self.heads = {}
        self.live_calls = []

    def run_live(self, arguments, *, cwd=None):
        self.live_calls.append((tuple(arguments), cwd))
        return self.run(arguments, cwd=cwd)

    def run(self, arguments, *, cwd=None):
        a = tuple(arguments)
        self.calls.append((a, cwd))
        if a in self.overrides:
            value = self.overrides[a]
            if isinstance(value, tuple):
                return CommandResult(a, value[0], "", value[1])
            return CommandResult(a, 0, value, "")
        if a == ("git", "rev-parse", "HEAD") and cwd in self.heads:
            return CommandResult(a, 0, self.heads[cwd], "")
        value = self.respond(a)
        if value is None:
            return CommandResult(a, 1, "", "missing ref")
        return CommandResult(a, 0, value, "")

    def respond(self, a):  # noqa: C901 - Explicit command table keeps the fake auditable.
        if a[:3] == ("git", "remote", "get-url"):
            return "https://github.com/example/base.git"
        if a[:3] == ("git", "rev-parse", "--path-format=absolute"):
            return "/repo/.git"
        if a[:2] == ("git", "fetch"):
            return ""
        if a[:3] == ("git", "worktree", "add"):
            path = Path(a[-2])
            path.mkdir()
            self.worktrees.append(path)
            self.heads[path] = a[-1]
            return ""
        if a[:3] == ("git", "worktree", "list"):
            return "\n\n".join(f"worktree {path}\nHEAD {SHA}\ndetached" for path in self.worktrees)
        if a[:3] == ("gh", "repo", "view"):
            return json.dumps(
                {
                    "nameWithOwner": "example/base",
                    "isPrivate": False,
                    "defaultBranchRef": {"name": "main"},
                }
            )
        if a[:3] == ("git", "rev-parse", "--is-inside-work-tree"):
            return "true"
        if a[:3] == ("git", "rev-parse", "--git-dir"):
            return "/repo/.git/worktrees/release"
        if a[:3] == ("git", "rev-parse", "--git-common-dir"):
            return "/repo/.git"
        if a[:2] == ("git", "status"):
            return ""
        if a == ("git", "diff", "--cached", "--name-only") or a[:3] == ("git", "log", "--oneline"):
            return ""
        if a[:3] == ("git", "diff", "--no-ext-diff"):
            return "reviewed evidence diff" if "--cached" not in a else ""
        if a[:2] == ("git", "branch"):
            return "release-custodian"
        if a == ("git", "rev-parse", "HEAD"):
            return SHA
        if a == ("git", "rev-parse", "FETCH_HEAD^{commit}"):
            return SHA
        if a == ("git", "rev-parse", "--is-shallow-repository"):
            return "false"
        if a[:2] == ("git", "cat-file"):
            return ""
        if a[:2] == ("git", "merge-base"):
            return None
        if a[:4] == ("git", "rev-parse", "--verify", "--quiet"):
            return OBJECT if self.local and a[-1] == f"refs/tags/{TAG}" else None
        if a[:2] == ("git", "rev-parse"):
            if "^{commit}" in a[-1]:
                return SHA
            return OLD if "v1.0.0" in a[-1] else OBJECT
        if a[:2] == ("git", "show"):
            return "1.0.1" if a[-1] == "HEAD:VERSION" else "Reviewed release evidence"
        if a[:2] == ("git", "ls-remote"):
            ref = a[-1]
            refs = {"refs/heads/main": SHA, "refs/tags/v1.0.0": OLD}
            if self.staged:
                refs[f"refs/tags/release-staging/{TAG}"] = OBJECT
            if self.final:
                refs[f"refs/tags/{TAG}"] = OBJECT
            return f"{refs[ref]}\t{ref}" if ref in refs else ""
        if a[0] == "uv":
            return "v1.0.0"
        if a[0] in ("make", "bash"):
            return "passed"
        if a[0] in ("ssh-add", "ssh-keygen"):
            return f"256 {m.DEFAULT_EXPECTED_SIGNER_FINGERPRINT} operator (ED25519)"
        if a[:3] == ("gh", "release", "view"):
            if "--jq" in a:
                return "v1.0.0"
            return json.dumps({"tagName": TAG, "isDraft": False, "isPrerelease": False})
        if a[:3] == ("gh", "pr", "list"):
            if a[a.index("--state") + 1] != "merged":
                return "[]"
            return json.dumps(
                [
                    {
                        "number": 42,
                        "mergeCommit": {"oid": SHA},
                        "headRefOid": OLD,
                        "reviewDecision": "",
                        "author": {"login": "author"},
                        "isCrossRepository": False,
                        "baseRefName": "main",
                        "headRefName": f"release/{TAG}",
                    }
                ]
            )
        if a[:3] == ("gh", "pr", "checks"):
            return "Required CI pass"
        if a[:2] == ("gh", "api"):
            if "check-runs" in a[2]:
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "name": "Required CI",
                            "head_sha": OLD,
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                )
            if "git/ref/tags" in a[2]:
                return OBJECT
            if a[2].endswith("/releases"):
                return "[[]]"
        if a[:3] == ("gh", "run", "list"):
            if "workflow_run" in a:
                return '[{"databaseId": 123, "status": "completed", "conclusion": "success"}]'
            return json.dumps(
                [
                    {
                        "headSha": SHA,
                        "headBranch": TAG,
                        "workflowName": "Verify Platform Release",
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            )
        if a[:3] == ("gh", "run", "download"):
            (Path(a[-1]) / "release-tag.txt").write_text(TAG)
            return ""
        if a[:3] == ("gh", "workflow", "run"):
            self.final = a[3] == "Create Platform Release Tag"
            return ""
        if a[:2] == ("git", "push"):
            self.staged = True
            return ""
        if a[:3] == ("git", "-c", "gpg.format=ssh"):
            self.local = True
            return ""
        raise AssertionError(f"Unexpected command: {a}")


@pytest.fixture
def setup(tmp_path):
    key = tmp_path / "signer.pub"
    key.write_text("ssh-ed25519 public-test-material")
    return Fake(), m.Repository(tmp_path, "example/base", "main"), key


def prepare_args():
    return argparse.Namespace(
        version=TAG,
        confirm=None,
        release_date="2026-09-08",
        summary="Reviewed fixes",
    )


def test_prepare_exact_workflow_and_confirmation(setup, capsys):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    runner.overrides[("git", "show", f"{SHA}:CHANGELOG.md")] = (
        "# Changelog\r\n\r\n## [Unreleased]\r\n\r\n### Fixed\r\n\r\n"
        "- Current fix.\r\n\r\n## [1.0.0] - 2026-09-01\r\nOld notes\r\n"
    )
    prompt = Mock()

    def confirm(message):
        output = capsys.readouterr().out
        for expected in (
            f"Prepare release PR: example/base | {TAG}",
            "Latest verified published predecessor: v1.0.0",
            "Release date: 2026-09-08",
            "Summary: Reviewed fixes",
            f"Source target SHA (main): {SHA}",
            "## [Unreleased]\n\n### Fixed\n\n- Current fix.",
            "does not sign, publish, or deploy",
        ):
            assert expected in output
        for excluded in (
            "Old notes",
            "Reviewed release evidence",
            "Fresh installation",
            "1.0.0]",
            "manifest",
            "migration",
        ):
            assert excluded not in output
        assert "Type exactly" not in message
        return "yes"

    prompt.ask.side_effect = confirm
    result = m.prepare(runner, repo, prepare_args(), prompt)
    assert "Dispatched" in result
    assert "2. Finish release notes (finish-notes), then 3. Validate release (check)" in result
    assert "Sign and publish (publish)" in result
    command = runner.calls[-1][0]
    assert command[:4] == ("gh", "workflow", "run", "Prepare Release PR")
    assert "preparation_mode=successor" in command
    assert "stable_upgrade=supported" in command
    assert "upgrades_from_alpha_revisions=" in command
    assert "recovery=forward-fix" in command
    assert "previous_tag=v1.0.0" in command
    assert not any("manifest.yaml" in a[-1] or "migrations/" in a[-1] for a, _ in runner.calls)


@pytest.mark.parametrize(
    ("changelog", "expected"),
    [
        ("## [Unreleased]\r\n \r\n## [1.0.0]\r\nOld notes", "Unreleased section is empty"),
        ("# Changelog\n## [1.0.0]\nOld notes", "Unreleased section is missing"),
        ("", "Unreleased section is missing"),
        ("## [Unreleased]\n### Fixed\n- Current fix.", "### Fixed\n- Current fix."),
        (
            "```md\n## [Unreleased]\n```\n## [1.0.0]\nOld notes",
            "Unreleased section is missing",
        ),
        (
            "## [Unreleased]\n### Fixed\n````md\n## Example\n```\n````\n"
            "~~~md\n## Another example\n~~~\n- Current fix.\n## [1.0.0]\nOld notes",
            "### Fixed\n````md\n## Example\n```\n````\n"
            "~~~md\n## Another example\n~~~\n- Current fix.",
        ),
    ],
)
def test_prepare_notes_missing_empty_and_markdown_boundaries(setup, capsys, changelog, expected):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    runner.overrides[("git", "show", f"{SHA}:CHANGELOG.md")] = changelog
    m.prepare(runner, repo, prepare_args(), Prompt("yes"))
    output = capsys.readouterr().out
    assert expected in output
    assert "Old notes" not in output


@pytest.mark.parametrize("answer", ["", "no", None, f"PREPARE example/base {TAG} {SHA}"])
def test_prepare_decline_never_dispatches(setup, answer):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    with pytest.raises(m.ReleaseError, match="declined or cancelled"):
        m.prepare(runner, repo, prepare_args(), Prompt(answer))
    assert not any(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)


@pytest.mark.parametrize("keys", ["\r", "n\r", "y\r", "\x03"])
def test_questionary_prepare_confirmation_default_no(setup, monkeypatch, keys):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    confirm = m.questionary.confirm
    with create_pipe_input() as terminal:

        def capture(message, **kwargs):
            assert kwargs["default"] is False
            return confirm(message, **kwargs, input=terminal, output=DummyOutput())

        monkeypatch.setattr(m.questionary, "confirm", capture)
        terminal.send_text(keys)
        if keys == "y\r":
            m.prepare(runner, repo, prepare_args())
        else:
            with pytest.raises(m.ReleaseError, match="declined or cancelled"):
                m.prepare(runner, repo, prepare_args())
    assert sum(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls) == (keys == "y\r")


@pytest.mark.parametrize("change", ["identity", "commit"])
def test_prepare_revalidates_after_yes(setup, change):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    prompt = Mock()

    def confirm(message):
        if change == "identity":
            runner.overrides[("git", "remote", "get-url", "--push", "--all", "origin")] = (
                "https://github.com/other/base.git"
            )
        else:
            runner.overrides[("git", "rev-parse", "HEAD")] = OLD
            runner.overrides[("git", "ls-remote", "origin", "refs/heads/main")] = (
                f"{OLD}\trefs/heads/main"
            )
        return "yes"

    prompt.ask.side_effect = confirm
    with pytest.raises(m.ReleaseError, match=r"push destination|changed while confirming"):
        m.prepare(runner, repo, prepare_args(), prompt)
    assert not any(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)


def test_publish_sequence_and_exact_objects(setup):
    runner, repo, key = setup
    args = argparse.Namespace(tag=TAG, confirm=None, signing_public_key=key)
    result = m.publish(
        runner, repo, args, lambda _: None, Prompt(f"PUBLISH example/base {TAG} {SHA}")
    )
    assert result.startswith("Published")
    commands = [a for a, _ in runner.calls]
    assert ("make", "check") in commands
    assert commands.index(("make", "release-check")) < commands.index(
        ("make", "release-check", f"TAG={TAG}")
    )
    pushes = [a for a in commands if a[:2] == ("git", "push")]
    assert pushes == [
        ("git", "push", "--no-follow-tags", "origin", f"{OBJECT}:refs/tags/release-staging/{TAG}")
    ]
    assert not any("create" in a and "release" in a for a in commands)


@pytest.mark.parametrize("command", ["status", "plan", "continue"])
def test_cli_commands(setup, command, capsys):
    runner, repo, _ = setup
    args = ["--base-repo", str(repo.path)]
    if command in ("status", "plan"):
        args.append("--json")
    assert m.run_cli([*args, command], runner=runner, isatty=False) == 0
    output = capsys.readouterr().out
    if command in ("status", "plan"):
        assert json.loads(output)
    assert not any(a[:2] == ("git", "push") for a, _ in runner.calls)


def test_default_uses_published_not_local_version(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "9.0.0"
    assert m.plan(runner, repo, None)["planned_tag"] == TAG
    with pytest.raises(m.ReleaseError, match="newer"):
        m.plan(runner, repo, "v0.9.0")


@pytest.mark.parametrize("version", ["v01.2.3", "1.2", "v1.2.3-rc1", "--help", "v1.2.3\n"])
def test_invalid_versions(version):
    with pytest.raises(m.ReleaseError):
        m._tag(version)
    with pytest.raises(m.ReleaseError):
        m.next_patch(version)


@pytest.mark.parametrize("remote", ["https://elsewhere/base", "git@github.com:one/two/three", "-x"])
def test_invalid_remotes(remote):
    with pytest.raises(m.ReleaseError):
        m._github_slug(remote)


def test_ref_exact_not_prefix_and_transport_error(setup):
    runner, repo, _ = setup
    cmd = ("git", "ls-remote", "origin", f"refs/tags/{TAG}")
    runner.overrides[cmd] = f"{SHA}\trefs/tags/{TAG}0"
    assert not m._remote_tag_exists(runner, repo, TAG)
    runner.overrides[cmd] = (1, "offline")
    with pytest.raises(m.ReleaseError, match="offline"):
        m._remote_tag_exists(runner, repo, TAG)
    runner.overrides[cmd] = f"invalid\trefs/tags/{TAG}"
    with pytest.raises(m.ReleaseError, match="invalid"):
        m._remote_tag_exists(runner, repo, TAG)


@pytest.mark.parametrize(
    ("command", "value", "message"),
    [
        (("git", "status", "--porcelain"), " M VERSION", "clean"),
        (("git", "rev-parse", "--git-dir"), "/repo/.git", "dedicated"),
        (
            ("git", "ls-remote", "origin", "refs/heads/main"),
            f"{OLD}\trefs/heads/main",
            "exact remote",
        ),
        (("ssh-add", "-l"), f"256 {m.DEFAULT_EXPECTED_SIGNER_FINGERPRINT}suffix", "ssh-agent"),
        (("make", "check"), (1, "validation failed"), "validation failed"),
        (("git", "show", "HEAD:VERSION"), "2.0.0", "VERSION"),
    ],
)
def test_publish_preflight_fails_before_mutation(setup, command, value, message):
    runner, repo, key = setup
    runner.overrides[command] = value
    with pytest.raises(m.ReleaseError, match=message):
        m._require_publish_preconditions(runner, repo, TAG, key)
    assert not runner.local and not runner.staged


def test_signer_and_partial_collisions(setup):
    runner, repo, key = setup
    runner.overrides[("ssh-keygen", "-lf", str(key), "-E", "sha256")] = "256 SHA256:other"
    with pytest.raises(m.ReleaseError, match="fingerprint"):
        m._require_publish_preconditions(runner, repo, TAG, key)
    with pytest.raises(m.ReleaseError, match=r"public \.pub"):
        m._require_publish_preconditions(runner, repo, TAG, key.with_suffix(""))
    runner.local = True
    with pytest.raises(m.ReleaseError, match="already exists"):
        m._require_publish_preconditions(runner, repo, TAG, key)
    runner.local = False
    runner.staged = True
    with pytest.raises(m.ReleaseError, match="do not retry"):
        m._require_publish_preconditions(runner, repo, TAG, key)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("release_date", "bad"),
        ("summary", ""),
    ],
)
def test_prepare_invalid_inputs(setup, attribute, value):
    runner, repo, _ = setup
    args = prepare_args()
    setattr(args, attribute, value)
    with pytest.raises(m.ReleaseError):
        m.prepare(runner, repo, args)
    assert not any(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)


def test_prepare_collision_and_cancel(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    with pytest.raises(m.ReleaseError, match="confirmation"):
        m.prepare(runner, repo, prepare_args(), Prompt("no"))
    runner.staged = True
    with pytest.raises(m.ReleaseError, match="collision"):
        m.prepare(runner, repo, prepare_args())
    runner.staged = False
    runner.overrides[("git", "ls-remote", "origin", f"refs/heads/release/{TAG}")] = (
        f"{SHA}\trefs/heads/release/{TAG}"
    )
    with pytest.raises(m.ReleaseError, match="branch already"):
        m.prepare(runner, repo, prepare_args())


def test_publication_wrong_object_never_passes(setup, monkeypatch):
    runner, repo, _ = setup
    monkeypatch.setattr(m, "POLL_ATTEMPTS", 2)
    runner.overrides[
        ("gh", "api", f"repos/{repo.slug}/git/ref/tags/{TAG}", "--jq", ".object.sha")
    ] = OLD
    sleep = Mock()
    with pytest.raises(m.ReleaseError, match="timed out"):
        m._wait_for_publication(runner, repo, TAG, OBJECT, sleep)
    sleep.assert_called_once()
    assert not m._release_matches("not json", TAG)
    assert not m._release_matches(json.dumps({"tagName": TAG, "isDraft": True}), TAG)


def test_continue_partial_and_conflict(setup):
    runner, repo, _ = setup
    runner.local = True
    report = m.inspect_progress(runner, repo)
    assert report["local_signed_object"] == OBJECT
    assert "Do not rerun" in str(report["next_action"])
    runner.overrides[("git", "ls-remote", "origin", f"refs/tags/{TAG}")] = f"{OLD}\trefs/tags/{TAG}"
    with pytest.raises(m.ReleaseError, match="objects differ"):
        m.inspect_progress(runner, repo)


def test_wizard_defaults_and_entry_paths(setup, monkeypatch):
    runner, repo, key = setup
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    base = ["--base-repo", str(repo.path), "--worktree-root", str(root)]
    assert m.run_cli(base, runner=runner, isatty=False) == 1
    assert m.run_cli([*base, "--json", "check"], runner=runner) == 1
    assert m.run_cli(base, runner=runner, prompt=Prompt("quit"), isatty=True) == 0
    assert m.run_cli(base, runner=runner, prompt=Prompt("plan", "quit"), isatty=True) == 0
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    assert (
        m.run_cli(
            base,
            runner=runner,
            prompt=Prompt("prepare", "yes", "", "", "Fixes", "yes"),
            isatty=True,
        )
        == 0
    )
    monkeypatch.setattr(m, "_default_public_key", lambda: key)
    del runner.overrides[("git", "show", "HEAD:VERSION")]
    assert (
        m.run_cli(
            base,
            runner=runner,
            prompt=Prompt("publish", "yes", TAG, f"PUBLISH example/base {TAG} {SHA}"),
            isatty=True,
            sleep=lambda _: None,
        )
        == 0
    )


def test_questionary_menu_labels_sections_and_safe_default(monkeypatch):
    select = m.questionary.select
    captured = {}
    with create_pipe_input() as terminal:

        def capture(message, **kwargs):
            captured.update(kwargs)
            return select(message, **kwargs, input=terminal, output=DummyOutput())

        monkeypatch.setattr(m.questionary, "select", capture)
        terminal.send_text("\r")
        assert m._interactive_command(m.TerminalPrompt()) == "status"
    assert captured["default"] == "status"
    choices = captured["choices"]
    assert [
        (i, c.title) for i, c in enumerate(choices) if isinstance(c, m.questionary.Separator)
    ] == [
        (0, "Release Steps"),
        (5, "\nInformation"),
    ]
    assert [(c.title, c.value) for c in choices if not isinstance(c, m.questionary.Separator)] == [
        ("1. Prepare release draft", "prepare"),
        ("2. Finish release notes", "finish-notes"),
        ("3. Validate release", "check"),
        ("4. Sign and publish", "publish"),
        ("Release status", "status"),
        ("Changelog", "changelog"),
        ("Preview next release", "plan"),
        ("Publication progress", "continue"),
        ("Diagnostics", "diagnostics"),
        ("Exit", "quit"),
    ]


def test_text_menu_sections_and_action_ids():
    prompt = Mock()
    prompt.ask.return_value = ""
    assert m._interactive_command(prompt) == "status"
    message = prompt.ask.call_args.args[0]
    assert "Release Steps:\n  1. Prepare release draft (prepare)" in message
    assert "\n\nInformation:\n  Release status (status)" in message
    assert "Publication progress (continue)" in message
    assert "Exit (quit)" in message
    prompt.ask.return_value = "prepare"
    assert m._interactive_command(prompt) == "prepare"


def test_status_shows_known_pr_link_without_extra_requests(setup):
    runner, repo, _ = setup
    report = m.inspect_progress(runner, repo, TAG)
    url = "https://github.com/example/base/pull/42"
    report["release_prs"] = [{"url": url}, {}]
    calls = list(runner.calls)
    assert f"Release PR: {url}" in m._render(report, False)
    assert runner.calls == calls


def test_questionary_cancellation(monkeypatch):
    question = Mock()
    question.ask.return_value = None
    monkeypatch.setattr(m.questionary, "text", lambda _: question)
    monkeypatch.setattr(m.questionary, "select", lambda *a, **kw: question)
    with pytest.raises(m.ReleaseError, match="cancelled"):
        m.TerminalPrompt().ask("question")
    with pytest.raises(m.ReleaseError, match="cancelled"):
        m._interactive_command(m.TerminalPrompt())
    question.ask.return_value = "status"
    assert m.TerminalPrompt().ask("question") == "status"
    assert m._interactive_command(m.TerminalPrompt()) == "status"


@pytest.mark.parametrize("change", ["wrong-merge", "fork", "ci", "predecessor", "head", "required"])
def test_review_and_provenance_gates(setup, change):
    runner, repo, key = setup
    m._require_publish_preconditions(runner, repo, TAG, key)
    commands = [a for a, _ in runner.calls]
    pr_command = next(a for a in commands if a[:3] == ("gh", "pr", "list") and "merged" in a)
    prs = json.loads(runner.respond(pr_command))
    if change == "head":
        prs[0]["headRefOid"] = "invalid"
    elif change == "required":
        required = next(a for a in commands if a[:3] == ("gh", "pr", "checks"))
        runner.overrides[required] = (1, "required check failed")
    elif change == "wrong-merge":
        prs[0]["mergeCommit"]["oid"] = OLD
    elif change == "fork":
        prs[0]["isCrossRepository"] = True
    elif change == "ci":
        ci_command = next(a for a in commands if a[:2] == ("gh", "api") and "check-runs" in a[2])
        runner.overrides[ci_command] = "[]"
    else:
        runner.overrides[next(a for a in commands if a[0] == "uv")] = "v0.9.0"
    runner.overrides[pr_command] = json.dumps(prs)
    with pytest.raises(m.ReleaseError):
        m._require_publish_preconditions(runner, repo, TAG, key)
    assert not runner.local and not runner.staged


def test_mismatched_push_remote_and_latest_signature(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "remote", "get-url", "--push", "--all", "origin")] = (
        "git@github.com:other/base.git"
    )
    with pytest.raises(m.ReleaseError, match="push destination"):
        m._repository(runner, repo.path)
    runner.overrides[("git", "ls-remote", "origin", "refs/tags/v1.0.0")] = (
        f"{OBJECT}\trefs/tags/v1.0.0"
    )
    with pytest.raises(m.ReleaseError, match="differs locally"):
        m._latest(runner, repo)
    del runner.overrides[("git", "ls-remote", "origin", "refs/tags/v1.0.0")]
    runner.overrides[("bash", "scripts/verify_release_tag.sh", "v1.0.0", "v1.0.0")] = (
        1,
        "bad signature",
    )
    with pytest.raises(m.ReleaseError, match="bad signature"):
        m._latest(runner, repo)


def test_publication_requires_exact_identity_artifact(setup, monkeypatch):
    runner, repo, _ = setup
    original = runner.respond

    def respond(a):
        if a[:3] == ("gh", "run", "download"):
            (Path(a[-1]) / "release-tag.txt").write_text("v9.9.9")
            return ""
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    monkeypatch.setattr(m, "POLL_ATTEMPTS", 2)
    with pytest.raises(m.ReleaseError, match="identity artifact"):
        m._verify_workflows(runner, repo, TAG, SHA, lambda _: None)


def test_staging_race_never_pushes(setup):
    runner, repo, _ = setup
    runner.final = True
    with pytest.raises(m.ReleaseError, match="appeared"):
        m._dispatch_tag_creation(runner, repo, TAG, OBJECT, SHA)
    assert not runner.staged


def test_repository_checks_without_invented_review_gate(setup):
    runner, repo, key = setup
    m._require_publish_preconditions(runner, repo, TAG, key)
    assert any(a[:3] == ("gh", "pr", "checks") and "--required" in a for a, _ in runner.calls)
    assert not any(
        "/reviews" in " ".join(a) or "/collaborators/" in " ".join(a) or "--admin" in a
        for a, _ in runner.calls
    )


@pytest.mark.parametrize("state", ["open", "merged", "all"])
def test_release_inventory_filters_forks_wrong_bases_and_unknown_provenance(setup, state):
    runner, repo, _ = setup
    m._release_prs(runner, repo, state, TAG)
    command = runner.calls[-1][0]
    assert command[command.index("--base") + 1] == repo.default_branch
    assert command[command.index("--head") + 1] == f"release/{TAG}"
    owned = {
        "number": 1,
        "headRefName": f"release/{TAG}",
        "baseRefName": "main",
        "isCrossRepository": False,
    }
    runner.overrides[command] = json.dumps(
        [
            owned,
            {**owned, "number": 2, "isCrossRepository": True},
            {**owned, "number": 3, "baseRefName": "other"},
            {**owned, "number": 4, "isCrossRepository": None},
            {**owned, "number": 5, "headRefName": "release/v9.9.9"},
        ]
    )
    assert m._release_prs(runner, repo, state, TAG) == [owned]


@pytest.mark.parametrize("foreign", [{"isCrossRepository": True}, {"baseRefName": "other"}])
def test_foreign_release_pr_cannot_hijack_target_or_block_prepare(setup, monkeypatch, foreign):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    original = runner.respond
    pr = {
        "headRefName": f"release/{TAG}",
        "baseRefName": "main",
        "isCrossRepository": False,
        **foreign,
    }

    def respond(a):
        if a[:3] == ("gh", "pr", "list"):
            return json.dumps([pr])
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    assert m._target_tag(runner, repo) == "v1.0.0"
    assert m.inspect_progress(runner, repo, TAG)["release_prs"] == []
    assert "Dispatched" in m.prepare(runner, repo, prepare_args(), Prompt("yes"))


@pytest.mark.parametrize("staged", [False, True])
def test_partial_status_reports_default_branch_tag_creation_candidates(setup, monkeypatch, staged):
    runner, repo, _ = setup
    runner.local = True
    runner.staged = staged
    original = runner.respond
    candidate = {
        "databaseId": 456,
        "url": "https://github.com/example/base/actions/runs/456",
        "headSha": OLD,
        "headBranch": "main",
        "status": "completed",
        "conclusion": "failure",
    }

    def respond(a):
        if a[:3] == ("gh", "run", "list") and "create-release-tag.yaml" in a:
            assert a[a.index("--branch") + 1] == "main"
            assert a[a.index("--event") + 1] == "workflow_dispatch"
            assert "--commit" not in a
            return json.dumps([candidate])
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    report = m.continue_release(runner, repo, TAG, lambda _: None)
    assert report["tag_creation_runs_uncorrelated"] == [candidate]
    assert "verify tag, tag_object and target_commit inputs" in str(
        report["tag_creation_run_guidance"]
    )
    assert report["local_signed_commit"] == SHA
    assert any(
        a[:3] == ("gh", "run", "list")
        and "--commit" in a
        and a[a.index("--commit") + 1] == SHA
        and a[a.index("--branch") + 1] == TAG
        for a, _ in runner.calls
    )
    assert not any(
        a[:3] == ("gh", "workflow", "run") or a[:2] == ("git", "push") for a, _ in runner.calls
    )


def test_poll_waits_for_verification_run_and_late_publication_artifact(setup, monkeypatch):
    runner, repo, _ = setup
    original = runner.respond
    verification_polls = 0
    publication_polls = 0

    def respond(a):
        nonlocal verification_polls, publication_polls
        if a[:3] == ("gh", "run", "list") and "release.yaml" in a:
            verification_polls += 1
            if verification_polls == 1:
                return "[]"
            if verification_polls == 2:
                return json.dumps(
                    [{"headSha": SHA, "headBranch": TAG, "status": "in_progress", "conclusion": ""}]
                )
        if a[:3] == ("gh", "run", "list") and "publish-release.yaml" in a:
            publication_polls += 1
            return json.dumps(
                [
                    {
                        "databaseId": 123,
                        "status": "in_progress" if publication_polls == 1 else "completed",
                        "conclusion": "success",
                    }
                ]
            )
        if a[:3] == ("gh", "run", "download") and publication_polls == 2:
            return None  # Completed run is visible before its artifact can be downloaded.
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    sleep = Mock()
    m._verify_workflows(runner, repo, TAG, SHA, sleep)
    assert sleep.call_count == 4
    assert verification_polls == 5
    assert publication_polls == 3
    assert not runner.local and not runner.staged


@pytest.mark.parametrize("phase", ["verification", "publication"])
def test_correlated_terminal_workflow_failure_stops_without_retry(setup, monkeypatch, phase):
    runner, repo, _ = setup
    original = runner.respond

    def respond(a):
        value = original(a)
        workflow = "release.yaml" if phase == "verification" else "publish-release.yaml"
        if a[:3] == ("gh", "run", "list") and workflow in a:
            rows = json.loads(value)
            rows[0]["conclusion"] = "failure"
            return json.dumps(rows)
        return value

    monkeypatch.setattr(runner, "respond", respond)
    sleep = Mock()
    with pytest.raises(m.ReleaseError, match="failure"):
        m._verify_workflows(runner, repo, TAG, SHA, sleep)
    sleep.assert_not_called()
    assert not runner.staged


def test_uncorrelated_failed_publication_is_not_misattributed(setup, monkeypatch):
    runner, repo, _ = setup
    original = runner.respond

    def respond(a):
        if a[:3] == ("gh", "run", "list") and "publish-release.yaml" in a:
            return '[{"databaseId": 999, "status": "completed", "conclusion": "failure"}]'
        if a[:3] == ("gh", "run", "download"):
            return None
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    monkeypatch.setattr(m, "POLL_ATTEMPTS", 2)
    sleep = Mock()
    with pytest.raises(m.ReleaseError, match="uncorrelated failed runs: 999"):
        m._verify_workflows(runner, repo, TAG, SHA, sleep)
    sleep.assert_called_once()


def test_status_targets_pending_pr_not_old_checkout_version(setup, capsys):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    m._release_prs(runner, repo, "open")
    pending = runner.calls[-1][0]
    pr = {"headRefName": f"release/{TAG}", "isCrossRepository": False, "baseRefName": "main"}
    runner.overrides[pending] = json.dumps([pr])
    assert m.run_cli(["--base-repo", str(repo.path), "--json", "status"], runner=runner) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"]["tag"] == TAG
    assert report["status"]["version"] == "1.0.0"
    assert any(f"release/{TAG}" in a for a, _ in runner.calls)
    runner.overrides[pending] = json.dumps([pr, {**pr, "headRefName": "release/v1.0.2"}])
    with pytest.raises(m.ReleaseError, match="multiple pending"):
        m.inspect_progress(runner, repo)
    status = m.inspect_progress(runner, repo, TAG)["status"]
    assert isinstance(status, dict)
    assert status["tag"] == TAG


def test_interactive_target_choice_and_explicit_tag_cli(setup, capsys):
    runner, repo, _ = setup
    args = argparse.Namespace(command="status")
    m._wizard(args, runner, repo, Prompt("v1.0.0"))
    assert args.tag == "v1.0.0"
    assert (
        m.run_cli(
            ["--base-repo", str(repo.path), "--json", "status", "--tag", "v1.0.0"], runner=runner
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"]["tag"] == "v1.0.0"


def test_continue_rechecks_final_release_independent_of_checkout_head(setup):
    runner, repo, _ = setup
    runner.local = runner.final = True
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    runner.overrides[("git", "rev-parse", "HEAD")] = OLD
    report = m.continue_release(runner, repo, TAG, lambda _: None)
    assert report["publication_verified"] is True
    assert report["local_signed_commit"] == SHA
    assert not any(
        a[:3] == ("gh", "workflow", "run") or a[:2] == ("git", "push") for a, _ in runner.calls
    )
    runner.local = False
    with pytest.raises(m.ReleaseError, match="fetch the final signed tag"):
        m.continue_release(runner, repo, TAG, lambda _: None)


@pytest.mark.parametrize("problem", ["version", "reachable", "pending"])
def test_prepare_matches_base_predecessor_rules_before_dispatch(setup, monkeypatch, problem):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0" if problem != "version" else "1.0.1"
    original = runner.respond

    def respond(a):
        if problem == "reachable" and a[0] == "uv" and "-c" in a:
            return "v1.0.1"
        if problem == "pending" and a[:3] == ("gh", "pr", "list") and "--head" not in a:
            return (
                '[{"headRefName": "release/v1.0.2", '
                '"isCrossRepository": false, "baseRefName": "main"}]'
            )
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    with pytest.raises(m.ReleaseError, match=r"HEAD VERSION|reachable from HEAD|pending release"):
        m.prepare(runner, repo, prepare_args(), Prompt())
    assert not any(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)


def test_wizard_has_no_policy_questionnaire(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    args = argparse.Namespace(command="prepare")
    m._wizard(args, runner, repo, Prompt("", "", "Reviewed upgrade"))
    assert args.version == TAG
    m.prepare(runner, repo, args, Prompt("yes"))
    command = runner.calls[-1][0]
    assert "stable_upgrade=supported" in command
    assert "upgrades_from_alpha_revisions=" in command
    assert "recovery=forward-fix" in command


@pytest.mark.parametrize(
    "flag",
    [
        "--stable-upgrade",
        "--recovery",
        "--upgrades-from-alpha-revisions",
        "--preparation-mode",
        "--previous-tag",
    ],
)
def test_unshipped_legacy_flags_removed(flag):
    with pytest.raises(SystemExit):
        m.parse_arguments(["--base-repo", "/unused", "prepare", "--summary", "Fix", flag, "value"])


def test_local_check_supports_unmerged_pr_without_remote_mutation(setup, monkeypatch):
    runner, repo, _ = setup
    pr = release_pr()
    monkeypatch.setattr(m, "_release_prs", lambda *a: [pr])
    runner.overrides[("git", "ls-remote", "origin", "refs/heads/main")] = f"{OLD}\trefs/heads/main"
    m.check_release(runner, repo, pr)
    assert [a for a, _ in runner.calls if a[0] == "make"] == [
        ("make", "check"),
        ("make", "release-check"),
    ]
    assert all(a[0] in ("git", "make") for a, _ in runner.calls)
    runner.overrides[("git", "status", "--porcelain")] = " M VERSION"
    with pytest.raises(m.ReleaseError, match="clean"):
        m.check_release(runner, repo, pr)


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing"), subprocess.TimeoutExpired("git", 1)]
)
def test_subprocess_failure_is_bounded(monkeypatch, error):
    launch = Mock(side_effect=error)
    monkeypatch.setattr(subprocess, "run", launch)
    result = SubprocessRunner().run(("git", "status"))
    assert result.returncode == 1
    assert launch.call_args.kwargs["timeout"] == 1800
    assert "shell" not in launch.call_args.kwargs


def test_subprocess_public_trust_environment(monkeypatch):
    launch = Mock(return_value=subprocess.CompletedProcess(["git"], 0, "ok", ""))
    monkeypatch.setattr(subprocess, "run", launch)
    monkeypatch.setenv("TAG", "wrong")
    assert SubprocessRunner().run(("git", "status")).stdout == "ok"
    assert "TAG" not in launch.call_args.kwargs["env"]
    assert launch.call_args.kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
    assert (
        "platform-release namespaces"
        in launch.call_args.kwargs["env"]["PLATFORM_RELEASE_ALLOWED_SIGNER"]
    )


@pytest.mark.parametrize(
    "relation",
    [
        "equal",
        "behind",
        "ahead",
        "diverged",
        "missing-object",
        "offline",
        "missing-ref",
        "shallow",
        "ancestry-error",
    ],
)
def test_head_sync_uses_exact_remote_and_only_available_local_ancestry(setup, relation):
    runner, repo, _ = setup
    remote = ("git", "ls-remote", "origin", "refs/heads/main")
    runner.overrides[remote] = f"{OLD}\trefs/heads/main"
    if relation == "equal":
        runner.overrides[remote] = f"{SHA}\trefs/heads/main"
    elif relation == "missing-object":
        runner.overrides[("git", "cat-file", "-e", f"{OLD}^{{commit}}")] = (1, "missing")
    elif relation == "offline":
        runner.overrides[remote] = (1, "private transport diagnostic")
    elif relation == "missing-ref":
        runner.overrides[remote] = ""
    elif relation == "shallow":
        runner.overrides[("git", "rev-parse", "--is-shallow-repository")] = "true"
    elif relation in ("behind", "ahead", "ancestry-error"):
        pair = (OLD, SHA) if relation == "ahead" else (SHA, OLD)
        runner.overrides[("git", "merge-base", "--is-ancestor", *pair)] = (
            (128, "broken history") if relation == "ancestry-error" else ""
        )
    status = m.read_status(runner, repo)
    assert status.head_sync == (
        relation if relation in ("equal", "behind", "ahead", "diverged") else "unknown"
    )
    assert "private transport diagnostic" not in status.sync_note
    assert status.remote_default_commit == (
        "" if relation in ("offline", "missing-ref") else SHA if relation == "equal" else OLD
    )
    commands = [a for a, _ in runner.calls]
    assert remote in commands
    assert not any("fetch" in a or "pull" in a for a in commands)
    if relation in ("equal", "missing-object", "offline", "missing-ref"):
        assert not any(a[:2] == ("git", "merge-base") for a in commands)


def test_status_concise_and_explicit_views_preserve_full_report(setup, capsys):
    runner, repo, _ = setup
    runner.local = runner.final = True
    release = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/example/base/releases/tag/{TAG}",
        "body": "LONG RELEASE BODY\n" * 100,
        "author": {"login": "private-diagnostic-marker"},
    }
    runner.overrides[("gh", "api", f"repos/{repo.slug}/releases", "--paginate", "--slurp")] = (
        json.dumps([[release]])
    )
    args = ["--base-repo", str(repo.path)]
    assert m.run_cli([*args, "status", "--tag", TAG], runner=runner) == 0
    output = capsys.readouterr().out
    for expected in (
        "GitHub Release: published",
        "signature verified",
        "Remote staging: missing",
        "Exact-tag verifier: success",
        "NEW RELEASES",
        "HEAD sync: equal",
        release["html_url"],
        "not checked by status",
        "do not republish",
    ):
        assert expected in output
    for excluded in (
        "LONG RELEASE BODY",
        "private-diagnostic-marker",
        "databaseId",
        "123",
        "approval may be needed",
        "publication_runs_uncorrelated",
    ):
        assert excluded not in output
    assert m.run_cli([*args, "--json", "status", "--tag", TAG], runner=runner) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["github_release"] == [release]
    assert full["publication_runs_uncorrelated"]
    assert m.run_cli([*args, "status", "--diagnostics", "--tag", TAG], runner=runner) == 0
    diagnostics = capsys.readouterr().out
    assert "private-diagnostic-marker" in diagnostics
    assert "publication_runs_uncorrelated" in diagnostics
    assert "LONG RELEASE BODY" not in diagnostics
    assert m.run_cli([*args, "status", "--changelog", "--tag", TAG], runner=runner) == 0
    notes = capsys.readouterr().out
    assert notes.strip() == release["body"].strip()
    assert "private-diagnostic-marker" not in notes
    assert not any(a[:3] == ("gh", "run", "download") for a, _ in runner.calls)


@pytest.mark.parametrize(
    ("release", "state"),
    [
        ([], "missing"),
        ([{"draft": True}], "draft"),
        ([{"draft": False, "prerelease": True}], "prerelease"),
        ([{}], "unknown"),
    ],
)
def test_release_states_and_next_actions(setup, release, state):
    runner, repo, _ = setup
    runner.final = True
    runner.overrides[("gh", "api", f"repos/{repo.slug}/releases", "--paginate", "--slurp")] = (
        json.dumps([[{**r, "tag_name": TAG} for r in release]])
    )
    report = m.inspect_progress(runner, repo, TAG)
    assert report["release_state"] == state
    assert "never recreate" in str(report["next_action"])
    assert "approval may be needed" not in str(report["next_action"])
    assert report["verifier_status"] == "unknown (local signed tag missing)"
    assert "not verified locally" in m._render(report, False)
    assert "incomplete" in m._next_action("published", "", OBJECT, "")
    assert "metadata exists without a final tag" in m._next_action("draft", "", "", "")


def test_verifier_does_not_attribute_other_runs(setup, monkeypatch):
    runner, repo, _ = setup
    runner.local = True
    original = runner.respond

    def respond(a):
        if a[:3] == ("gh", "run", "list") and "--commit" in a:
            return json.dumps(
                [
                    {
                        "workflowName": "Unrelated",
                        "headSha": SHA,
                        "headBranch": TAG,
                        "conclusion": "success",
                    },
                    {
                        "workflowName": "Verify Platform Release",
                        "headSha": OLD,
                        "headBranch": TAG,
                        "conclusion": "failure",
                    },
                ]
            )
        return original(a)

    monkeypatch.setattr(runner, "respond", respond)
    assert m.inspect_progress(runner, repo, TAG)["verifier_status"] == "not found"


def test_questionary_menu_returns_after_reads_and_cancels_safely(setup, monkeypatch, capsys):
    runner, repo, _ = setup
    question = Mock()
    question.ask.side_effect = [
        "status",
        TAG,
        "plan",
        "diagnostics",
        TAG,
        "changelog",
        TAG,
        "status",
        TAG,
        "quit",
    ]
    monkeypatch.setattr(m.questionary, "select", lambda *a, **kw: question)
    args = ["--base-repo", str(repo.path)]
    assert m.run_cli(args, runner=runner, isatty=True) == 0
    output = capsys.readouterr().out
    # Two status views plus the diagnostics summary.
    assert output.count("GitHub Release: missing") == 3
    assert "Planned tag:" in output
    assert "No GitHub Release changelog available" in output
    for answers in ([None], ["status", None], ["publish", None], ["prepare", None]):
        question.ask.side_effect = answers
        monkeypatch.setattr(m.questionary, "text", lambda *a, **kw: question)
        assert m.run_cli(args, runner=runner, isatty=True) == 1
    assert not any(
        a[:3] == ("gh", "workflow", "run") or a[:2] == ("git", "push") for a, _ in runner.calls
    )


def test_menu_mutation_runs_once_and_exits(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    prompt = Prompt(
        "plan",
        "prepare",
        "yes",
        "",
        "",
        "Fixes",
        "yes",
        "prepare",
    )
    assert (
        m.run_cli(
            [
                "--base-repo",
                str(repo.path),
                "--worktree-root",
                str(repo.path.parent / f"worktrees-{repo.path.name}"),
            ],
            runner=runner,
            prompt=prompt,
            isatty=True,
        )
        == 0
    )
    assert next(prompt.answers) == "prepare"
    assert sum(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls) == 1


def test_automatic_checkout_reuse_preserves_dirty_primary(setup, monkeypatch):
    runner, repo, _ = setup
    marker = repo.path / "uncommitted.txt"
    marker.write_text("keep my work")
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    original = runner.run

    def run(arguments, *, cwd=None):
        if tuple(arguments) == ("git", "status", "--porcelain") and cwd == repo.path:
            return CommandResult(tuple(arguments), 0, " M tracked\n?? uncommitted.txt", "")
        return original(arguments, cwd=cwd)

    monkeypatch.setattr(runner, "run", run)
    checkout = m._release_checkout(runner, repo, root)
    assert checkout.path != repo.path
    assert m._release_checkout(runner, repo, root) == checkout
    assert marker.read_text() == "keep my work"
    commands = [a for a, _ in runner.calls]
    assert sum(a[:3] == ("git", "worktree", "add") for a in commands) == 1
    fetch = next(a for a in commands if a[:2] == ("git", "fetch"))
    assert "--atomic" in fetch and "refs/tags/*:refs/tags/*" in fetch
    assert "--refmap=" in fetch and "refs/heads/main" in fetch
    assert "--write-fetch-head" in fetch
    assert not any(":refs/remotes/" in arg for arg in fetch)
    assert not any("--force" in a or any(s.startswith("+") for s in a) for a in commands)
    assert not any(
        a[:2] in (("git", "checkout"), ("git", "reset"), ("git", "clean")) for a in commands
    )


def test_dirty_existing_release_checkout_aborts_before_fetch(setup):
    runner, repo, _ = setup
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    checkout = m._release_checkout(runner, repo, root)
    marker = checkout.path / "unfinished.txt"
    marker.write_text("do not remove")
    runner.overrides[("git", "status", "--porcelain")] = "?? unfinished.txt"
    runner.calls.clear()
    with pytest.raises(m.ReleaseError, match="clean"):
        m._release_checkout(runner, repo, root)
    assert marker.read_text() == "do not remove"
    assert not any("fetch" in a or "add" in a for a, _ in runner.calls)


@pytest.mark.parametrize("problem", ["fetch", "collision", "remote", "push", "existing", "root"])
def test_local_preparation_failures_do_not_execute_repo_code(setup, monkeypatch, problem):
    runner, repo, _ = setup
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    original = runner.respond
    if problem in ("fetch", "collision"):

        def respond(a):
            if a[:2] == ("git", "fetch"):
                return None
            return original(a)

        monkeypatch.setattr(runner, "respond", respond)
        if problem == "collision":
            runner.local = True  # Tags share the original repository's common Git directory.
    elif problem == "remote":
        runner.overrides[("git", "remote", "get-url", "--all", "origin")] = (
            "https://github.com/example/base.git\nhttps://evil.example/base.git"
        )
    elif problem == "push":
        runner.overrides[("git", "remote", "get-url", "--push", "--all", "origin")] = (
            "git@github.com:example/base.git"  # Same slug is not an identical URL.
        )
    elif problem == "existing":
        path = root / f"platform-release-example-base-{SHA}"
        path.mkdir(parents=True)
        (path / "keep").write_text("occupied")
    else:
        root = repo.path / "worktrees"
    with pytest.raises(m.ReleaseError):
        m._release_checkout(runner, repo, root)
    assert all(a[0] in ("git", "gh") for a, _ in runner.calls)
    assert not any(a[:3] == ("git", "worktree", "add") for a, _ in runner.calls)


def test_default_worktree_root_and_fetch_race(setup):
    runner, repo, _ = setup
    common = repo.path / ".git"
    runner.overrides[("git", "rev-parse", "--path-format=absolute", "--git-common-dir")] = str(
        common
    )
    checkout = m._release_checkout(runner, repo, None)
    assert checkout.path.parent == repo.path.parent / "worktrees"
    runner.overrides[("git", "rev-parse", "FETCH_HEAD^{commit}")] = OLD
    with pytest.raises(m.StaleTargetError, match="during fetch"):
        m._release_checkout(runner, repo, None)


def test_remote_advance_revalidates_and_reprompts_before_prepare(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    checkout = m._release_checkout(runner, repo, root)
    messages = []

    class AdvancePrompt:
        def ask(self, message):
            messages.append(message)
            if len(messages) == 1:
                runner.overrides[("git", "ls-remote", "origin", "refs/heads/main")] = (
                    f"{OLD}\trefs/heads/main"
                )
                runner.overrides[("git", "rev-parse", "FETCH_HEAD^{commit}")] = OLD
            return "yes"

    args = prepare_args()
    args.command = "prepare"
    args.worktree_root = root
    m._execute(args, runner, checkout, AdvancePrompt(), lambda _: None)
    assert len(messages) == 2
    assert SHA in messages[0] and OLD in messages[1]
    assert sum(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls) == 1
    assert runner.heads[checkout.path] == SHA


@pytest.mark.parametrize(
    "confirmation",
    [f"PREPARE example/base {TAG} {SHA}", "yes", f"PREPARE example/base {TAG} {OLD}"],
)
def test_noninteractive_defaults_and_exact_confirmation(setup, confirmation):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    argv = ["--base-repo", str(repo.path), "--worktree-root", str(root)]
    assert m.run_cli([*argv, "prepare", "--summary", "Fix"], runner=runner, isatty=False) == 1
    assert runner.calls == []
    assert m.run_cli(
        [
            *argv,
            "--allow-local-preparation",
            "prepare",
            "--summary",
            "Fix",
            "--confirm",
            confirmation,
        ],
        runner=runner,
        isatty=False,
    ) == (0 if confirmation == f"PREPARE example/base {TAG} {SHA}" else 1)
    if confirmation != f"PREPARE example/base {TAG} {SHA}":
        assert not any(a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)
        return
    command = runner.calls[-1][0]
    assert f"version={TAG[1:]}" in command
    assert f"release_date={m.date.today().isoformat()}" in command


@pytest.mark.parametrize("problem", ["stale-head", "rerun-failure", "pending"])
def test_required_ci_must_be_current_success_on_exact_head(setup, problem):
    runner, repo, _ = setup
    m._release_pr_gate(runner, repo, TAG, SHA)
    command = next(a for a, _ in runner.calls if a[:2] == ("gh", "api") and "check-runs" in a[2])
    checks = json.loads(runner.respond(command))
    latest = {**checks[0], "id": 2}
    if problem == "stale-head":
        latest["head_sha"] = SHA
    elif problem == "rerun-failure":
        latest["conclusion"] = "failure"
    else:
        latest["status"] = "in_progress"
    runner.overrides[command] = json.dumps([*checks, latest])
    with pytest.raises(m.ReleaseError, match="Required CI"):
        m._release_pr_gate(runner, repo, TAG, SHA)


def test_signing_never_reads_private_key(setup, monkeypatch):
    runner, repo, key = setup
    private = key.with_suffix("")
    private.write_text("must never be read")
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        assert path != private
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    m.publish(
        runner,
        repo,
        argparse.Namespace(
            tag=TAG,
            confirm=f"PUBLISH example/base {TAG} {SHA}",
            signing_public_key=key,
        ),
        lambda _: None,
        Prompt(),
    )
    assert all(str(private) not in a for a, _ in runner.calls)


def test_stale_supplied_confirmation_never_retries(setup):
    runner, repo, _ = setup
    runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    runner.overrides[("git", "ls-remote", "origin", "refs/heads/main")] = f"{OLD}\trefs/heads/main"
    args = prepare_args()
    args.command = "prepare"
    args.confirm = f"PREPARE example/base {TAG} {SHA}"
    with pytest.raises(m.StaleTargetError):
        m._execute(args, runner, repo, Prompt(), lambda _: None)
    assert not any("fetch" in a or a[:3] == ("gh", "workflow", "run") for a, _ in runner.calls)


def test_publication_changed_main_tip_does_not_sign_new_commit(setup):
    runner, repo, key = setup
    runner.overrides[("git", "ls-remote", "origin", "refs/heads/main")] = f"{OLD}\trefs/heads/main"
    runner.overrides[("git", "rev-parse", "HEAD")] = OLD
    args = argparse.Namespace(tag=TAG, confirm=None, signing_public_key=key)
    with pytest.raises(m.ReleaseError, match="provenance review if main advanced"):
        m.publish(runner, repo, args, lambda _: None, Prompt())
    assert not runner.local and not runner.staged


@pytest.mark.parametrize("ancestry", [0, 1, 128])
def test_fetch_checks_snapshot_of_existing_tracking_commit(setup, ancestry):
    runner, repo, _ = setup
    tracking = "refs/remotes/origin/main"
    runner.overrides[("git", "rev-parse", "--verify", "--quiet", tracking)] = OLD
    snapshot = ("git", "rev-parse", f"{tracking}^{{commit}}")
    runner.overrides[snapshot] = OLD
    check = ("git", "merge-base", "--is-ancestor", OLD, SHA)
    runner.overrides[check] = "" if ancestry == 0 else (ancestry, "ancestry rejected")
    root = repo.path.parent / f"worktrees-{repo.path.name}"
    if ancestry == 0:
        m._release_checkout(runner, repo, root)
    else:
        with pytest.raises(m.ReleaseError, match="not a verified fast-forward"):
            m._release_checkout(runner, repo, root)
        assert not root.exists()
    commands = [a for a, _ in runner.calls]
    fetch = next(a for a in commands if a[:2] == ("git", "fetch"))
    assert commands.index(snapshot) < commands.index(fetch) < commands.index(check)
    assert commands.count(snapshot) == 1


@pytest.mark.parametrize("history", ["fast-forward", "rewrite", "rewind", "pr-push", "pr-rewrite"])
def test_real_local_git_preserves_tracking_and_primary_files(tmp_path, monkeypatch, history):
    # Only temporary local Git repositories; no GitHub, network, signing or release scripts.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Local Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.invalid")
    runner = SubprocessRunner()

    def git(path, *arguments):
        return m._checked(runner, ("git", *arguments), cwd=path)

    primary = tmp_path / "primary"
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "--initial-branch=main", str(primary))
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    tracked = primary / "tracked.txt"
    tracked.write_text("committed content\n")
    git(primary, "add", "tracked.txt")
    git(primary, "-c", "commit.gpgsign=false", "commit", "-m", "Initial test commit")
    initial = git(primary, "rev-parse", "HEAD")
    git(primary, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "Tracked tip")
    old = git(primary, "rev-parse", "HEAD")
    git(remote, "fetch", str(primary), "refs/heads/main:refs/heads/main")
    git(primary, "remote", "add", "origin", str(remote))
    git(primary, "fetch", "origin")
    # Keep the normal +refs/heads/* mapping to catch opportunistic tracking updates.
    assert git(primary, "config", "--get", "remote.origin.fetch") == (
        "+refs/heads/*:refs/remotes/origin/*"
    )
    tree = git(primary, "rev-parse", "HEAD^{tree}")
    parents = () if history in ("rewrite", "pr-rewrite") else ("-p", old)
    target = (
        initial
        if history == "rewind"
        else git(primary, "commit-tree", tree, *parents, "-m", "Remote test target")
    )
    git(remote, "fetch", str(primary), target)
    git(remote, "update-ref", "refs/heads/main", target, old)
    if history.startswith("pr-"):
        git(remote, "update-ref", "refs/pull/42/head", target)
    git(remote, "update-ref", "refs/tags/local-test", old)
    # Both the primary index and worktree contain valuable uncommitted changes.
    tracked.write_text("staged content\n")
    git(primary, "add", "tracked.txt")
    tracked.write_text("unstaged content\n")
    untracked = primary / "untracked.txt"
    untracked.write_text("keep me\n")
    index = (primary / ".git" / "index").read_bytes()
    repo = m.Repository(primary.resolve(), "example/base", "main")
    # Bypass only GitHub identity validation for this local-transport integration test.
    monkeypatch.setattr(m, "_repository", lambda runner, path: repo)
    root = tmp_path / "worktrees"
    if history.startswith("pr-"):
        pr = release_pr(headRefOid=target)
        monkeypatch.setattr(m, "_release_prs", lambda *a: [pr])
        refs = git(primary, "show-ref")
        checkout = m._release_checkout(runner, repo, root, pr=pr)
        assert git(checkout.path, "rev-parse", "HEAD") == target
        assert git(primary, "show-ref") == refs
        marker = checkout.path / "unfinished.md"
        marker.write_text("keep unfinished evidence\n")
        with pytest.raises(m.ReleaseError, match="clean"):
            m._release_checkout(runner, repo, root, pr=pr)
        assert marker.read_text() == "keep unfinished evidence\n"
    elif history != "fast-forward":
        with pytest.raises(m.ReleaseError, match="not a verified fast-forward"):
            m._release_checkout(runner, repo, root)
        assert not root.exists()
    else:
        checkout = m._release_checkout(runner, repo, root)
        assert git(checkout.path, "rev-parse", "HEAD") == target
    assert git(primary, "rev-parse", "refs/remotes/origin/main") == old
    assert git(primary, "rev-parse", "FETCH_HEAD^{commit}") == target
    if not history.startswith("pr-"):
        assert git(primary, "rev-parse", "refs/tags/local-test") == old
    assert git(primary, "rev-parse", "HEAD") == old
    assert git(primary, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert (primary / ".git" / "index").read_bytes() == index
    assert tracked.read_text() == "unstaged content\n"
    assert untracked.read_text() == "keep me\n"


@pytest.fixture
def check_setup(setup, monkeypatch):
    runner, repo, _ = setup
    prs = [release_pr()]
    original = runner.run

    def run(arguments, *, cwd=None):
        a = tuple(arguments)
        if a[:3] == ("gh", "pr", "list"):
            runner.calls.append((a, cwd))
            return CommandResult(a, 0, json.dumps(prs), "")
        if cwd == repo.path and a in (
            ("git", "show", "HEAD:VERSION"),
            ("git", "status", "--porcelain"),
        ):
            return CommandResult(a, 0, "1.0.0" if a[1] == "show" else " M VERSION", "")
        return original(arguments, cwd=cwd)

    monkeypatch.setattr(runner, "run", run)
    runner.heads[repo.path] = OLD
    runner.overrides[("git", "show", "HEAD:CHANGELOG.md")] = (
        "## [Unreleased]\nFuture\n## [1.0.1] - 2026-09-08\nReviewed current evidence\n"
        "## [1.0.0]\nOld release notes"
    )
    root = repo.path.parent / f"checks-{repo.path.name}"
    argv = ["--base-repo", str(repo.path), "--worktree-root", str(root)]
    return runner, repo, prs, argv


@pytest.mark.parametrize("selection", [[], ["--tag", TAG], ["--pr", "42"]])
def test_check_selects_pr_not_stale_dirty_original(check_setup, capsys, selection):
    runner, repo, _, argv = check_setup
    assert (
        m.run_cli(
            [*argv, "--allow-local-preparation", "check", *selection], runner=runner, isatty=False
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"Validate {TAG} | PR #42 | https://github.com/example/base/pull/42" in output
    assert f"SHA: {SHA}" in output
    assert "Reviewed current evidence" in output
    assert "Old release notes" not in output and "Future" not in output
    assert [a for a, _ in runner.live_calls] == [("make", "check"), ("make", "release-check")]
    assert all(path != repo.path and path.name.endswith(SHA) for _, path in runner.live_calls)
    fetch = next(a for a, _ in runner.calls if a[:2] == ("git", "fetch"))
    assert fetch[-1] == "refs/pull/42/head" and "--refmap=" in fetch
    assert not any(":" in arg or arg.startswith("+") or arg == "--force" for arg in fetch)
    assert not runner.local and not runner.staged


def test_menu_check_trust_confirmation_and_reuse(check_setup, capsys):
    runner, _, _, argv = check_setup
    prompt = Mock()
    prompt.ask.side_effect = ["check", "yes", "check", "yes", "quit"]
    assert m.run_cli(argv, runner=runner, prompt=prompt, isatty=True) == 0
    assert "Trust and execute unmerged same-repository code" in prompt.ask.call_args_list[1].args[0]
    assert sum(a[:3] == ("git", "worktree", "add") for a, _ in runner.calls) == 1
    assert capsys.readouterr().out.count("Full Base and pre-tag release checks passed.") == 2


@pytest.mark.parametrize(
    "problem", ["absent", "fork", "base", "closed", "tag", "number", "permission", "decline"]
)
def test_check_never_falls_back_or_executes_without_consent(check_setup, capsys, problem):
    runner, _, prs, argv = check_setup
    selection = []
    prompt = None
    if problem == "absent":
        prs.clear()
    elif problem == "fork":
        prs[0]["isCrossRepository"] = True
    elif problem == "base":
        prs[0]["baseRefName"] = "other"
    elif problem == "closed":
        prs[0]["state"] = "CLOSED"
    elif problem == "tag":
        selection = ["--tag", "v2.0.0"]
    elif problem == "number":
        selection = ["--pr", "99"]
    elif problem == "decline":
        prompt = Prompt("no")
    if problem not in ("permission", "decline"):
        argv += ["--allow-local-preparation"]
    assert m.run_cli([*argv, "check", *selection], runner=runner, prompt=prompt, isatty=False) == 1
    assert not runner.live_calls
    assert not any(a[:2] == ("git", "fetch") for a, _ in runner.calls)
    assert "passed" not in capsys.readouterr().out


def test_check_multiple_requires_tag_and_pr_selection(check_setup, capsys):
    runner, _, prs, argv = check_setup
    prs.append(release_pr(number=43, headRefName="release/v1.0.2"))
    assert (
        m.run_cli([*argv, "--allow-local-preparation", "check"], runner=runner, isatty=False) == 1
    )
    assert "multiple release PRs" in capsys.readouterr().err
    assert not runner.live_calls
    prompt = Mock()
    prompt.ask.side_effect = [f"{TAG} | PR #42", "yes"]
    assert m.run_cli([*argv, "check"], runner=runner, prompt=prompt, isatty=True) == 0
    assert "v1.0.2 | PR #43" in prompt.ask.call_args_list[0].args[0]


@pytest.mark.parametrize("phase", ["fetch", "after-fetch", "before-check", "after-check", "closed"])
def test_moving_pr_requires_fresh_action(check_setup, monkeypatch, capsys, phase):
    runner, _, prs, argv = check_setup
    original = runner.run

    def run(arguments, *, cwd=None):
        a = tuple(arguments)
        result = original(arguments, cwd=cwd)
        if phase == "fetch" and a == ("git", "rev-parse", "FETCH_HEAD^{commit}"):
            return CommandResult(a, 0, OLD, "")
        if (
            (phase == "after-fetch" and a[:2] == ("git", "fetch"))
            or (phase == "before-check" and a == ("git", "show", "HEAD:CHANGELOG.md"))
            or (phase in ("after-check", "closed") and a == ("make", "release-check"))
        ):
            prs[0]["state" if phase == "closed" else "headRefOid"] = (
                "CLOSED" if phase == "closed" else OLD
            )
        return result

    monkeypatch.setattr(runner, "run", run)
    assert (
        m.run_cli([*argv, "--allow-local-preparation", "check"], runner=runner, isatty=False) == 1
    )
    output = capsys.readouterr()
    assert "fresh check" in output.err and "checks passed" not in output.out
    if phase not in ("after-check", "closed"):
        assert not runner.live_calls


@pytest.mark.parametrize("problem", ["version", "dirty", "todo", "make", "cancel"])
def test_check_failures_preserve_evidence_and_never_claim_success(
    check_setup, monkeypatch, capsys, problem
):
    runner, repo, _, argv = check_setup
    if problem == "version":
        runner.overrides[("git", "show", "HEAD:VERSION")] = "1.0.0"
    elif problem == "dirty":
        runner.overrides[("git", "status", "--porcelain")] = "?? unfinished.md"
    elif problem == "cancel":
        monkeypatch.setattr(runner, "run_live", Mock(side_effect=KeyboardInterrupt))
    else:
        runner.overrides[("make", "release-check" if problem == "todo" else "check")] = (
            2,
            "TODO release evidence unfinished",
        )
    result = m.run_cli([*argv, "--allow-local-preparation", "check"], runner=runner, isatty=False)
    assert result == (130 if problem == "cancel" else 1)
    output = capsys.readouterr()
    assert "checks passed" not in output.out
    if problem == "cancel":
        assert output.err == "Validation cancelled.\n"
    elif problem in ("todo", "make"):
        assert "TODO release evidence unfinished" in output.err
        assert len(runner.live_calls) == 2
    else:
        assert not runner.live_calls
    assert runner.heads[repo.path] == OLD


def test_live_streams_stdout_stderr_with_private_log(capfd):
    runner = SubprocessRunner(verbose=True)
    result = runner.run_live(
        (
            sys.executable,
            "-c",
            "import sys; print('live stdout', flush=True); "
            "print('live stderr', file=sys.stderr); sys.exit(2)",
        )
    )
    assert result.returncode == 2 and result.stdout == ""
    assert "live stderr" in result.stderr
    output = capfd.readouterr()
    assert "live stdout\nlive stderr\n" in output.out and output.err == ""
    log = Path(output.out.splitlines()[0].removeprefix("Full validation log: "))
    assert log.read_text() == "live stdout\nlive stderr\n"
    assert log.stat().st_mode & 0o777 == 0o600
    result = runner.run((sys.executable, "-c", "print('captured API')"))
    assert result.stdout == "captured API\n" and capfd.readouterr().out == ""


@pytest.mark.parametrize("cancel", [False, True])
def test_live_timeout_and_sigint_stop_child_tree(tmp_path, cancel):
    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    script = (
        "import os, signal, time; from pathlib import Path; "
        "pid = os.fork(); "
        f"signal.signal(signal.SIGTERM, lambda *a: (Path({str(stopped)!r})"
        ".write_text('stopped'), exit(0))) if pid == 0 else "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(ready)!r}).write_text(str(os.getpid())) if pid == 0 else None; "
        "time.sleep(60)"
    )

    def interrupt_when_ready():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ready.exists():
            os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt_when_ready) if cancel else None
    if thread:
        thread.start()
    runner = SubprocessRunner(timeout=5 if cancel else 1, termination_grace=0.2)
    start = time.monotonic()
    try:
        if cancel:
            with pytest.raises(KeyboardInterrupt):
                runner.run_live((sys.executable, "-c", script))
        else:
            result = runner.run_live((sys.executable, "-c", script))
            assert result.returncode == 1 and "timed out" in result.stderr
    finally:
        if thread:
            thread.join(timeout=6)
    assert stopped.read_text() == "stopped"
    assert time.monotonic() - start < 8
