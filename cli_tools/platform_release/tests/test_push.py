import shlex
import subprocess

import pytest

from platform_release import main as m
from platform_release import upload as u
from platform_release.commands import SubprocessRunner

REF = "refs/heads/release/v1.0.1"


@pytest.fixture
def local_push(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    for key in ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "notes"
    remote = tmp_path / "remote.git"
    repo.mkdir()

    def git(*args, cwd=repo):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
        ).stdout.strip()

    git("init", "--initial-branch=notes")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    git("init", "--bare", str(remote))
    git("remote", "add", "origin", str(remote))
    commits = []
    for text in ("ancestor", "authorized", "notes"):
        (repo / "CHANGELOG.md").write_text(text)
        git("add", "CHANGELOG.md")
        git("commit", "-m", text)
        commits.append(git("rev-parse", "HEAD"))
    ancestor, base, head = commits
    git("push", "--no-follow-tags", "origin", f"{base}:{REF}")
    git("fetch", "--no-tags", str(repo), head, cwd=remote)
    hooks = repo / "policy hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", "policy hooks")
    marker = repo / "hook-input"
    return (
        m.Repository(repo, "example/base", "main"),
        remote,
        git,
        ancestor,
        base,
        head,
        hooks,
        marker,
    )


@pytest.mark.parametrize("race", ["unchanged", "deleted", "rewound", "advanced"])
def test_advertisement_guard_rejects_race_without_touching_remote_or_local(local_push, race):
    repo, remote, git, ancestor, base, head, hooks, marker = local_push
    (hooks / "pre-push").write_text(f"#!/bin/sh\ncat > {shlex.quote(str(marker))}\n")
    (hooks / "pre-push").chmod(0o700)
    (hooks / "other-policy").write_text("preserved")
    before = {path.name: path.read_bytes() for path in hooks.iterdir()}
    if race == "deleted":
        git("update-ref", "-d", REF, base, cwd=remote)
    elif race in ("rewound", "advanced"):
        git("update-ref", REF, ancestor if race == "rewound" else head, base, cwd=remote)
    runner = SubprocessRunner()
    if race == "unchanged":
        u._push(runner, repo, head, REF, base)
        assert git("rev-parse", REF, cwd=remote) == head
        assert marker.read_text() == f"{head} {head} {REF} {base}\n"
    else:
        with pytest.raises(m.ReleaseError):
            u._push(runner, repo, head, REF, base)
        assert not marker.exists()
        assert (
            git("for-each-ref", "--format=%(objectname)", REF, cwd=remote)
            == {"deleted": "", "rewound": ancestor, "advanced": head}[race]
        )
    assert git("rev-parse", "HEAD") == head
    assert git("diff", "--cached") == ""
    assert (repo.path / "CHANGELOG.md").read_text() == "notes"
    assert git("config", "core.hooksPath") == "policy hooks"
    assert {path.name: path.read_bytes() for path in hooks.iterdir()} == before


@pytest.mark.parametrize("action", ["reject", "delete", "rewind"])
def test_existing_hook_rejection_and_post_advertisement_races_fail_closed(local_push, action):
    repo, remote, git, ancestor, base, head, hooks, marker = local_push
    mutation = {
        "reject": "exit 7",
        "delete": shlex.join(["git", "--git-dir", str(remote), "update-ref", "-d", REF, base]),
        "rewind": shlex.join(["git", "--git-dir", str(remote), "update-ref", REF, ancestor, base]),
    }[action]
    hook = hooks / "pre-push"
    hook.write_text(
        f'#!/bin/sh\ntest "$1" = origin || exit 8\n'
        f'test "$2" = {shlex.quote(str(remote))} || exit 9\n'
        f"cat > {shlex.quote(str(marker))}\n{mutation}\n"
    )
    hook.chmod(0o700)
    with pytest.raises(m.ReleaseError):
        u._push(SubprocessRunner(), repo, head, REF, base)
    assert marker.read_text() == f"{head} {head} {REF} {base}\n"
    assert (
        git("for-each-ref", "--format=%(objectname)", REF, cwd=remote)
        == {"reject": base, "delete": "", "rewind": ancestor}[action]
    )
    assert git("rev-parse", "HEAD") == head


def test_no_hook_and_wrong_parent(local_push):
    repo, remote, git, ancestor, base, head, _, _ = local_push
    with pytest.raises(m.ReleaseError, match="exactly the authorized PR head"):
        u._push(SubprocessRunner(), repo, head, REF, ancestor)
    assert git("rev-parse", REF, cwd=remote) == base
    u._push(SubprocessRunner(), repo, head, REF, base)
    assert git("rev-parse", REF, cwd=remote) == head
