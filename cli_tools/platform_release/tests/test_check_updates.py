import argparse
import copy
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_release import OLD, SHA, TAG, Fake, release_pr

from platform_release import main as m
from platform_release import notes as n
from platform_release import upload as u
from platform_release.commands import SubprocessRunner


def test_guide_is_offline_and_has_five_ordered_steps(capsys):
    runner = Fake()
    assert m.run_cli(["--base-repo", "/not-a-repository", "guide"], runner=runner) == 0
    assert not runner.calls
    text = capsys.readouterr().out
    for step in ("1. Prepare", "2. Review notes", "3. Check", "4. Merge", "5. Publish"):
        assert step in text
    assert "Ready for review" in text and "Nothing merges automatically" in text


def test_merge_step_only_reads_and_links_the_selected_release(tmp_path, monkeypatch, capsys):
    runner = Fake()
    url = "https://github.com/example/base/pull/42"
    inspect = Mock(return_value={"url": url})
    monkeypatch.setattr(m, "inspect_progress", inspect)
    monkeypatch.setattr(m, "_render", lambda *args, **kw: url)
    assert m.run_cli(["--base-repo", str(tmp_path), "merge", "--tag", TAG], runner=runner) == 0
    assert inspect.call_args.args[-1] == TAG
    assert all(a[:3] == ("gh", "repo", "view") or a[0] == "git" for a, _ in runner.calls)
    assert not any(a[1] in ("fetch", "push", "merge") for a, _ in runner.calls)
    output = capsys.readouterr().out
    assert url in output and "Ready for review" in output and "No merge was performed" in output


def test_canonical_failure_is_compact_with_private_full_details(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    runner = Fake()
    detail = "traceback detail\n" * 1000 + "ReleaseError: outdated included changes"
    runner.overrides[("uv", "run", "--frozen", "python", "-c", n.CANONICAL)] = (1, detail)
    with pytest.raises(m.ReleaseError, match="outdated included changes") as error:
        n._canonical(runner, m.Repository(tmp_path, "example/base", "main"))
    assert len(str(error.value)) < 500
    log = next(tmp_path.glob("platform-release-files-*.log"))
    assert log.stat().st_mode & 0o777 == 0o600
    assert log.read_text().endswith(detail)


@pytest.mark.parametrize(
    ("draft", "state", "bucket", "conclusion", "expected", "next_action"),
    [
        (True, "OPEN", "pass", "success", "passing", "Ready for review"),
        (True, "OPEN", "fail", "failure", "failed", "fix failed"),
        (False, "OPEN", "pending", None, "pending", "wait for green"),
        (False, "OPEN", "pass", "success", "passing", "4. Merge"),
        (False, "MERGED", "pass", "success", "passing", "5. Publish"),
        (False, "MERGED", "fail", "failure", "failed", "Resolve or refresh"),
    ],
)
def test_current_pr_checks_and_one_next_action(
    tmp_path, monkeypatch, draft, state, bucket, conclusion, expected, next_action
):
    runner = Fake()
    repo = m.Repository(tmp_path, "example/base", "main")
    pr = release_pr(isDraft=draft, state=state, url="https://github.com/example/base/pull/42")
    monkeypatch.setattr(m, "_release_prs", lambda *args: [pr])
    runner.overrides[
        (
            "gh",
            "pr",
            "checks",
            "42",
            "--repo",
            repo.slug,
            "--required",
            "--json",
            "name,bucket,link",
        )
    ] = json.dumps([{"bucket": bucket}])
    command = (
        "gh",
        "api",
        f"repos/{repo.slug}/commits/{SHA}/check-runs",
        "--paginate",
        "--slurp",
        "--jq",
        "[.[].check_runs[]]",
    )
    runner.overrides[command] = json.dumps(
        [
            {
                "id": 1,
                "name": "Required CI",
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "id": 2,
                "name": "Required CI",
                "head_sha": SHA,
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
            },
        ]
    )
    pr["checks_state"] = m._pr_checks_state(runner, repo, pr)
    assert pr["checks_state"] == expected
    assert next_action in m._pr_next_action([pr])
    runner.overrides[command] = json.dumps(
        [
            {
                "id": 3,
                "name": "Required CI",
                "head_sha": OLD,
                "status": "completed",
                "conclusion": "success",
            },
        ]
    )
    assert m._pr_checks_state(runner, repo, pr) == "unknown"
    monkeypatch.setattr(m, "_release_prs", lambda *args: [{**pr, "headRefOid": OLD}])
    assert "PR changed" in m._pr_checks_state(runner, repo, pr)


@pytest.fixture
def correction(tmp_path, monkeypatch):
    snapshot = m.Repository(tmp_path / "snapshot", "example/base", "main")
    editable = m.Repository(tmp_path / "editable", "example/base", "main")
    content = dict.fromkeys(n.evidence_paths(TAG), "authored instructions\n")
    content.update(
        {
            "VERSION": "1.0.1\n",
            "CHANGELOG.md": "## [1.0.1]\nKeep my words.\n",
            "release/config.yaml": "original config\n",
            "release/manifest.yaml": "original inventory\n",
        }
    )
    for repo in (snapshot, editable):
        for name, text in content.items():
            path = repo.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    data: dict[str, Any] = {
        "config": {"summary": "Authored summary", "provenance": {"includedThrough": SHA}},
        "config_text": "corrected config\n",
        "manifest": "corrected inventory\n",
        "compact_renderer": True,
    }
    current = copy.deepcopy(data)
    current["config"]["provenance"]["includedThrough"] = OLD
    runner = Fake()
    runner.overrides[("git", "ls-files", "--error-unmatch", "--", *n.evidence_paths(TAG))] = ""
    runner.overrides[
        ("git", "diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames", SHA, "--")
    ] = "corrected diff"
    pr = release_pr()
    monkeypatch.setattr(m, "_release_prs", lambda *args: [pr])
    monkeypatch.setattr(n, "editable_checkout", lambda *args: editable)
    monkeypatch.setattr(n, "_canonical", lambda runner, repo, **kw: data if kw else current)
    (tmp_path / "original").mkdir()
    args = argparse.Namespace(
        selected_pr=pr, base_repo=tmp_path / "original", worktree_root=tmp_path, tag=TAG, pr=42
    )
    return runner, snapshot, editable, args, data, current, content


def test_recompute_saves_only_existing_generated_files_preserving_notes(correction, monkeypatch):
    runner, snapshot, editable, args, data, _, content = correction
    uploaded = Mock(return_value=False)
    monkeypatch.setattr(u, "offer_upload", uploaded)
    with pytest.raises(m.ReleaseError, match="corrections remain local"):
        n.check_updates(runner, snapshot, args, Mock(ask=Mock(return_value="yes")))
    assert uploaded.call_count == 1
    assert (editable.path / "release/config.yaml").read_text() == data["config_text"]
    assert (editable.path / "release/manifest.yaml").read_text() == data["manifest"]
    for name in ("VERSION", "CHANGELOG.md", f"release/migrations/{TAG}.md"):
        assert (editable.path / name).read_text() == content[name]
    assert n._snapshot(snapshot, n.evidence_paths(TAG)) == content
    assert not any(a[:2] in (("git", "add"), ("git", "push")) for a, _ in runner.calls)


def test_unchanged_files_need_no_prompt(correction):
    runner, snapshot, editable, args, data, current, _ = correction
    current.update(copy.deepcopy(data))
    for repo in (snapshot, editable):
        (repo.path / "release/manifest.yaml").write_text(data["manifest"])
    runner.overrides[(*u.DIFF, SHA, "--")] = ""
    prompt = Mock()
    assert n.check_updates(runner, snapshot, args, prompt) == snapshot
    prompt.ask.assert_not_called()
    assert runner.live_calls[0][0][0] == "pre-commit"


@pytest.mark.parametrize("problem", ["decline", "dirty", "pending", "missing", "race"])
def test_check_corrections_preserve_foreign_or_changed_work(correction, monkeypatch, problem):
    runner, snapshot, editable, args, _, _, content = correction
    if problem == "dirty":
        runner.overrides[("git", "status", "--porcelain")] = " M CHANGELOG.md"
    elif problem == "pending":
        runner.heads[editable.path] = OLD
    elif problem == "missing":
        runner.overrides[("git", "ls-files", "--error-unmatch", "--", *n.evidence_paths(TAG))] = (
            1,
            "missing tracked file",
        )
    elif problem == "race":
        monkeypatch.setattr(m, "_check_pr_head", Mock(side_effect=m.StaleTargetError("moved")))
    with pytest.raises(m.ReleaseError):
        n.check_updates(
            runner,
            snapshot,
            args,
            Mock(ask=Mock(return_value="no" if problem == "decline" else "yes")),
        )
    assert n._snapshot(editable, n.evidence_paths(TAG)) == content
    assert not runner.live_calls


@pytest.mark.parametrize("problem", [None, "moved", "stale-files"])
def test_uploaded_final_head_is_reselected_and_recomputed(correction, monkeypatch, problem):
    runner, snapshot, editable, args, data, _, _ = correction
    pr = {**args.selected_pr, "headRefOid": OLD}

    def upload(*a, **kw):
        runner.heads[editable.path] = OLD
        return True

    monkeypatch.setattr(u, "offer_upload", upload)
    monkeypatch.setattr(
        m, "_select_check_pr", lambda *args: {**pr, "headRefOid": SHA} if problem == "moved" else pr
    )
    monkeypatch.setattr(m, "_release_checkout", lambda *args, **kw: editable)
    final = {**data, "config": {}} if problem == "stale-files" else data
    canonical = Mock(side_effect=[data, {"config": {}}, data, final])
    monkeypatch.setattr(n, "_canonical", canonical)
    if problem:
        with pytest.raises(m.ReleaseError, match=r"PR changed|uploaded release files are outdated"):
            n.check_updates(runner, snapshot, args, Mock(ask=Mock(return_value="yes")))
        return
    assert n.check_updates(runner, snapshot, args, Mock(ask=Mock(return_value="yes"))) == editable
    assert args.selected_pr == pr
    assert canonical.call_args.kwargs == {"refresh": (SHA, OLD)}


@pytest.mark.parametrize("already_open", [True, False])
def test_upload_reselects_original_pr_when_multiple_releases_are_open(
    correction, monkeypatch, already_open
):
    runner, snapshot, editable, args, data, _, _ = correction
    second = release_pr(number=43, headRefName="release/v1.0.2")
    prs = [args.selected_pr, second] if already_open else [args.selected_pr]
    monkeypatch.setattr(
        m,
        "_release_prs",
        lambda runner, repo, state, tag=None: [
            pr for pr in prs if tag is None or pr["headRefName"] == f"release/{tag}"
        ],
    )
    args.pr = args.tag = None
    menu = Mock(ask=Mock(return_value=f"{TAG} | PR #42"))
    args.selected_pr = m._select_check_pr(runner, snapshot, args, menu)
    assert menu.ask.call_count == int(already_open)
    uploaded_pr = {**args.selected_pr, "headRefOid": OLD}

    def upload(*a, **kw):
        runner.heads[editable.path] = OLD
        prs[:] = [uploaded_pr, second]
        return True

    monkeypatch.setattr(u, "offer_upload", upload)
    checkout = Mock(return_value=editable)
    monkeypatch.setattr(m, "_release_checkout", checkout)
    canonical = Mock(side_effect=[data, {"config": {}}, data, data])
    monkeypatch.setattr(n, "_canonical", canonical)
    assert n.check_updates(runner, snapshot, args, Mock(ask=Mock(return_value="yes"))) == editable
    assert args.selected_pr == uploaded_pr
    assert checkout.call_args.kwargs["pr"] == uploaded_pr
    assert canonical.call_args.kwargs == {"refresh": (SHA, OLD)}
    assert args.pr is None and args.tag is None


def test_canonical_uses_base_ancestor_and_provenance_helpers(monkeypatch, capsys):
    config = {
        "summary": "Keep authored summary",
        "version": "1.0.1",
        "provenance": {"previousTag": "v1.0.0", "includedThrough": OLD},
        "compatibility": {
            "stableUpgrade": "supported",
            "upgradesFromAlphaRevisions": [],
            "recovery": "forward-fix",
        },
    }
    git = Mock(return_value=SHA)
    provenance = Mock(
        return_value={"previousTag": "v1.0.0", "includedThrough": SHA, "commits": [SHA]}
    )
    validate = Mock()

    def build():
        return {}

    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda path: {
            "git": git,
            "GIT_COMMIT": m.SHA_PATTERN,
            "ReleaseError": m.ReleaseError,
            "CONFIG_PATH": "release/config.yaml",
            "load_yaml": lambda path: copy.deepcopy(config),
            "validate_previous_tag_at_included_through": validate,
            "provenance_from_git": provenance,
            "build_manifest": build,
            "validate_manifest_schema": lambda value: None,
            "migration_scaffold": lambda *args: "unused scaffold",
            "render_release_notes": lambda path: "## v1.2.3\n\n- Existing fix.",
            "yaml": Mock(safe_dump=lambda value, **kw: json.dumps(value)),
        },
    )
    monkeypatch.setattr(sys, "argv", ["probe", "--refresh", SHA, OLD])
    exec(n.CANONICAL, {})
    result = json.loads(capsys.readouterr().out)
    git.assert_called_once_with("merge-base", "--all", SHA, OLD)
    validate.assert_called_once_with("v1.0.0", SHA)
    provenance.assert_called_once_with("v1.0.0", SHA)
    assert result["config"]["summary"] == config["summary"]
    assert result["config"]["provenance"]["commits"] == [SHA]
    git.return_value = SHA + "\n" + OLD
    with pytest.raises(m.ReleaseError, match="one shared main commit"):
        exec(n.CANONICAL, {})


def test_real_base_recomputes_only_main_changes_in_pr_history(tmp_path, monkeypatch):
    base = os.environ.get("PLATFORM_RELEASE_BASE_FIXTURE")
    if not base:
        pytest.skip("Set PLATFORM_RELEASE_BASE_FIXTURE to a trusted local Base checkout")
    runner = SubprocessRunner()
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test Operator")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "operator@example.test")

    def git(*args):
        return m._checked(runner, ("git", *args), cwd=tmp_path)

    def commit(message):
        git("add", ".")
        git("-c", "commit.gpgsign=false", "commit", "-m", message)
        return git("rev-parse", "HEAD")

    git("init", "-b", "main")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    commit("Previous release")
    git("-c", "tag.gpgsign=false", "tag", "v1.0.0")
    (tmp_path / "application.txt").write_text("first included change\n")
    first = commit("First implementation")
    git("switch", "-c", "release/v1.0.1")
    (tmp_path / "CHANGELOG.md").write_text("Keep these operator-authored notes.\n")
    commit("Release notes")
    git("switch", "main")
    (tmp_path / "application.txt").write_text("second included change\n")
    through = commit("Second implementation")
    git("switch", "release/v1.0.1")
    git("-c", "commit.gpgsign=false", "merge", "--no-ff", "main", "-m", "Refresh release branch")
    head = git("rev-parse", "HEAD")
    git("switch", "main")
    (tmp_path / "application.txt").write_text("not included in release\n")
    main = commit("Later implementation")
    script = n.CANONICAL.replace(
        "config = b['load_yaml'](b['CONFIG_PATH'])",
        """config = b['load_yaml'](b['CONFIG_PATH'])
config['provenance'] = {'previousTag': 'v1.0.0', 'includedThrough': sys.argv[5]}
# Run the real Base Git helpers against fake history, never a runtime release branch.
fixture = Path(sys.argv[4])
b['git'].__kwdefaults__['repository'] = fixture
b['git_is_ancestor'].__defaults__ = (fixture,)
b['validate_previous_tag_at_included_through'].__defaults__ = (fixture,)
""",
    )
    result = m._checked(
        runner,
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            script,
            "--refresh",
            main,
            head,
            str(tmp_path),
            first,
        ),
        cwd=Path(base),
    )
    data = json.loads(result)
    assert data["config"]["provenance"]["includedThrough"] == through
    assert data["config"]["provenance"]["commits"] == [first, through]
    assert git("show", f"{head}:CHANGELOG.md") == "Keep these operator-authored notes."
