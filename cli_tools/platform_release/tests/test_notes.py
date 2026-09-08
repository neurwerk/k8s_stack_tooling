import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_release import SHA, TAG, Fake, release_pr

from platform_release import compact as c
from platform_release import main as m
from platform_release import notes as n
from platform_release.commands import SubprocessRunner


def scaffold():
    return (
        f"# Platform {TAG}\n\n"
        "> TODO: Replace every TODO with reviewed release-specific evidence.\n\n## Support\n\n"
        "- Stable upgrades: Supported.\n- Supported alpha source revisions: None.\n"
        "- Downgrade: Unsupported.\n\n"
        + "".join(
            f"## {heading}\n\n"
            + ("Recovery classification: Forward fix.\n\n" if heading == "Recovery" else "")
            + "TODO.\n\n"
            for heading in sorted(c.HEADINGS)
            if heading != "Support"
        )
    )


CHANGELOG = (
    "## [Unreleased]\nFuture\n## [1.0.1] - 2026-09-08\n"
    "### Fixed\n- Keep reviewed fix.\n"
    "### Compatibility\n- TODO: Describe exact compatibility and recovery behavior.\n\n"
    "## [1.0.0]\nOld\n"
)


def test_conversion_requires_consent_and_preserves_existing_prose(capsys):
    text = scaffold().replace(
        "## Client Actions\n\nTODO.", "## Client Actions\n\nKeep staged order."
    )
    prompt = Mock()
    prompt.ask.return_value = ""
    migration, changelog = n.draft_notes(text, CHANGELOG, scaffold(), "1.0.1", prompt)
    assert "Keep staged order." in migration and "TODO" in migration
    assert changelog == CHANGELOG
    assert "conversion preview" in capsys.readouterr().out
    prompt.ask.side_effect = ["yes", "", ""]
    migration, changelog = n.draft_notes(migration, changelog, scaffold(), "1.0.1", prompt)
    assert not n.TODO.search(migration + changelog)
    assert "Keep staged order." in migration and "Keep reviewed fix." in changelog
    assert changelog.endswith("## [1.0.0]\nOld\n") and "Future" in changelog
    assert "Recovery classification: Forward fix." in migration
    assert "- Supported alpha source revisions: None." in migration


def test_authored_breaking_instructions_contradictions_and_code_are_not_erased():
    authored = (
        "## Breaking Changes\nStop writes before migration.\n"
        "## Recovery\nRecovery classification: Replacement restore.\n"
        "Do not revert the database.\n```md\nn/a\n## Support\n```\n"
        "## Support\nStable upgrades are NOT supported.\n"
    )
    log = "## [1.0.1]\n- Fix routing.\n```md\n## [1.0.0]\nn/a\n```\n"
    prompt = Mock(ask=Mock(return_value=""))
    assert n.draft_notes(authored, log, scaffold(), "1.0.1", prompt) == (authored, log)
    assert prompt.ask.call_count == 2


def test_compact_default_and_optional_none(capsys):
    log = "## [1.0.1]\n- Fix routing.\n"
    prompt = Mock(ask=Mock(side_effect=["", "yes", "None", "."]))
    migration, result = n.draft_notes("Technical metadata\n", log, "", "1.0.1", prompt)
    assert result == log and migration == "Technical metadata\n"
    assert "Add special notes or upgrade instructions? [y/N]: " in [
        call.args[0] for call in prompt.ask.call_args_list
    ]
    assert capsys.readouterr().out.count("- Fix routing.") == 1


def test_explicit_main_edit_and_special_notes():
    prompt = Mock(ask=Mock(side_effect=["yes", "- Reviewed fix.", ".", "yes", "Stop writes.", "."]))
    _, result = n.draft_notes("Metadata", "## [1.0.1]\nOld long notes\n", "", "1.0.1", prompt)
    assert result == "## [1.0.1]\n- Reviewed fix.\n\nStop writes.\n"


def test_cleanup_keeps_raw_code_and_unknown_todos():
    text = "### Compatibility\nn/a\n\n```md\nn/a\n## Recovery\n```\n    n/a\nTODO: review data\n"
    cleaned = c.clean(text)
    assert cleaned == text
    authored = "# Recovery\n## Client Actions\n### Runbook\nPreserve this procedure.\n"
    assert c.clean(authored, migration=True) == authored
    with pytest.raises(m.ReleaseError, match="section is missing"):
        c.section_bounds("## [Unreleased]\n", "1.0.1")


@pytest.mark.parametrize("value", ["NULL", "n/a", "- NULL", "- n/a", "TODO."])
@pytest.mark.parametrize("heading", ["", "## Client Actions\n", "### Compatibility\n"])
def test_cleanup_preserves_literal_values_with_authored_context(value, heading):
    text = heading + "Set database marker to this literal value:\n" + value + "\n"
    assert c.clean(text, migration=heading.startswith("## ")) == text


@pytest.mark.parametrize("value", ["NULL", "n/a", "- NULL", "- n/a", "TODO."])
def test_only_whole_known_placeholder_sections_are_conversion_candidates(value, capsys):
    authored = "Set database marker to this literal value:\nNULL\n"
    body = authored + "### Compatibility\n\n" + value + "\n"
    log = "## [1.0.1]\n" + body
    prompt = Mock(ask=Mock(side_effect=["", "", ""]))
    assert n.draft_notes("Metadata", log, "", "1.0.1", prompt)[1] == log
    preview = capsys.readouterr().out
    assert "conversion preview" in preview
    assert " Set database marker to this literal value:\n NULL\n" in preview
    prompt.ask.side_effect = ["yes", "", ""]
    converted = n.draft_notes("Metadata", log, "", "1.0.1", prompt)[1]
    assert converted == "## [1.0.1]\n" + authored
    assert c.clean("### Operator Values\n" + value + "\n") == (
        "### Operator Values\n" + value + "\n"
    )
    assert c.clean(value + "\n") == value + "\n"


@pytest.mark.parametrize("compact", [False, True])
def test_canonical_probes_renderer_behavior_without_a_version_gate(monkeypatch, capsys, compact):
    config = {
        "version": "8.7.6",
        "summary": "Existing fix",
        "compatibility": {
            "stableUpgrade": "supported",
            "upgradesFromAlphaRevisions": [],
            "recovery": "forward-fix",
        },
    }

    def manifest():
        return {"summary": "Existing fix"}

    def render(root):
        assert (root / "VERSION").read_text() == "1.2.3\n"
        assert "INTERNAL METADATA" in (root / "release/migrations/v1.2.3.md").read_text()
        return "## v1.2.3\n\n- Existing fix.\n" if compact else "# Old\nINTERNAL METADATA"

    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda path: {
            "CONFIG_PATH": "release/config.yaml",
            "load_yaml": lambda path: config,
            "build_manifest": manifest,
            "validate_manifest_schema": lambda value: None,
            "migration_scaffold": lambda *args: "Technical metadata",
            "render_release_notes": render,
            "yaml": Mock(safe_dump=lambda value, **kw: json.dumps(value)),
        },
    )
    monkeypatch.setattr(sys, "argv", ["probe"])
    exec(n.CANONICAL, {})
    result = json.loads(capsys.readouterr().out)
    assert result["compact_renderer"] is compact
    assert result["config"] == config


@pytest.fixture
def local_notes(tmp_path, monkeypatch):
    repo = m.Repository(tmp_path, "example/base", "main")
    runner = Fake()
    pr = release_pr()
    monkeypatch.setattr(m, "_release_prs", lambda *a: [pr])
    (tmp_path / "release/migrations").mkdir(parents=True)
    contents = {
        "VERSION": "1.0.1\n",
        "CHANGELOG.md": CHANGELOG,
        "release/config.yaml": "summary: Reviewed fixes\n# keep formatting\n",
        "release/manifest.yaml": "old inventory\n",
        f"release/migrations/{TAG}.md": scaffold(),
    }
    for name, text in contents.items():
        (tmp_path / name).write_text(text)
    data = {
        "config": {"summary": "Reviewed fixes"},
        "config_text": "summary: new\n",
        "scaffold": scaffold(),
        "manifest": "canonical inventory\n",
    }
    runner.overrides[("uv", "run", "--frozen", "python", "-c", n.CANONICAL)] = json.dumps(data)
    return runner, repo, argparse.Namespace(selected_pr=pr, base_repo=tmp_path), contents, data


def test_save_is_previewed_local_only_and_retry_preserves_edits(local_notes, capsys):
    runner, repo, args, before, _ = local_notes
    unrelated = repo.path / "operator.txt"
    unrelated.write_text("untouched")
    prompt = Mock()
    prompt.ask.side_effect = lambda message: "yes" if "Save this diff" in message else ""
    n.finish_notes(runner, repo, args, prompt)
    output = capsys.readouterr().out
    assert "--- release/manifest.yaml" not in output
    assert output.count("Proposed notes (not actual publication output until Base updated):") == 1
    assert "older Base renderer" in output
    assert not any("Save this diff" in call.args[0] for call in prompt.ask.call_args_list)
    assert "https://github.com/example/base/pull/42" in output
    assert (repo.path / "release/config.yaml").read_text() == before["release/config.yaml"]
    assert (repo.path / "release/manifest.yaml").read_text() == "canonical inventory\n"
    migration = repo.path / f"release/migrations/{TAG}.md"
    migration.write_text(migration.read_text().replace("TODO.", "Keep manual edit.", 1))
    n.finish_notes(runner, repo, args, prompt)
    assert "Keep manual edit." in migration.read_text()
    assert unrelated.read_text() == "untouched"
    assert all(
        a[0] == "uv" or a[1] in ("status", "branch", "rev-parse", "diff", "log")
        for a, _ in runner.calls
    )


def test_finish_notes_command_uses_shared_selection_and_local_save(
    local_notes, monkeypatch, capsys
):
    runner, repo, _, _, _ = local_notes
    checkout = Mock(return_value=repo)
    monkeypatch.setattr(n, "editable_checkout", checkout)
    prompt = Mock()
    prompt.ask.side_effect = lambda message: "yes" if "Save this diff" in message else ""
    assert (
        m.run_cli(
            [
                "--base-repo",
                str(repo.path),
                "--allow-local-preparation",
                "finish-notes",
                "--pr",
                "42",
            ],
            runner=runner,
            prompt=prompt,
        )
        == 0
    )
    assert checkout.call_args.args[-1]["number"] == 42
    assert "NOT UPLOADED" in capsys.readouterr().out


@pytest.mark.parametrize(
    "status", [" M scripts/platform_release.py\0", "?? notes.txt\0", "R  CHANGELOG.md\0old\0"]
)
def test_unrelated_edits_block_generator_without_cleanup(local_notes, status):
    runner, repo, args, before, _ = local_notes
    runner.overrides[("git", "status", "--porcelain=v1", "-z", "--untracked-files=all")] = status
    with pytest.raises(m.ReleaseError, match="non-evidence edits"):
        n.finish_notes(runner, repo, args, Mock())
    assert not any(a[0] == "uv" for a, _ in runner.calls)
    assert (repo.path / "CHANGELOG.md").read_text() == before["CHANGELOG.md"]


@pytest.mark.parametrize("summary", ["TODO", None])
def test_missing_summary_uses_canonical_render_and_preserves_other_inputs(local_notes, summary):
    runner, repo, args, _, data = local_notes
    data["config"]["summary"] = summary
    command = ("uv", "run", "--frozen", "python", "-c", n.CANONICAL)
    runner.overrides[command] = json.dumps(data)
    data = {
        **data,
        "config": {"summary": "Reviewed change"},
        "config_text": "summary: Reviewed change\n",
    }
    runner.overrides[(*command, "Reviewed change")] = json.dumps(data)
    prompt = Mock()
    prompt.ask.side_effect = lambda message: (
        "Reviewed change"
        if message.startswith("Release summary:")
        else "yes"
        if "Save this diff" in message
        else ""
    )
    n.finish_notes(runner, repo, args, prompt)
    assert (repo.path / "release/config.yaml").read_text() == data["config_text"]


@pytest.mark.parametrize("summary", [None, "", "   "])
def test_blank_missing_summary_saves_explicit_todo_not_fake_prose(local_notes, summary, capsys):
    runner, repo, args, _, data = local_notes
    data["config"]["summary"] = summary
    command = ("uv", "run", "--frozen", "python", "-c", n.CANONICAL)
    runner.overrides[command] = json.dumps(data)
    replacement = "TODO: Describe the release changes."
    rendered = {
        **data,
        "config": {"summary": replacement},
        "config_text": f"summary: '{replacement}'\n",
        "manifest": f"summary: '{replacement}'\n",
    }
    runner.overrides[(*command, replacement)] = json.dumps(rendered)
    prompt = Mock()
    prompt.ask.side_effect = lambda message: "yes" if "Save this diff" in message else ""
    n.finish_notes(runner, repo, args, prompt)
    assert any(call.args[0].startswith("Release summary:") for call in prompt.ask.call_args_list)
    for name in ("release/config.yaml", "release/manifest.yaml"):
        assert (repo.path / name).read_text() == f"summary: '{replacement}'\n"
    output = capsys.readouterr().out
    assert '"summary": "None"' not in output and "summary: None" not in output


@pytest.mark.parametrize("summary", [True, False, [], ["change"], {}, 42])
def test_malformed_summary_is_rejected_without_coercion_or_file_writes(local_notes, summary):
    runner, repo, args, before, data = local_notes
    data["config"]["summary"] = summary
    runner.overrides[("uv", "run", "--frozen", "python", "-c", n.CANONICAL)] = json.dumps(data)
    prompt = Mock()
    with pytest.raises(m.ReleaseError, match="summary must be text or null"):
        n.finish_notes(runner, repo, args, prompt)
    prompt.ask.assert_not_called()
    assert all((repo.path / name).read_text() == text for name, text in before.items())


@pytest.mark.parametrize("problem", ["concurrent", "symlink", "version", "remote", "branch"])
def test_save_guards_do_not_overwrite_local_work(local_notes, problem, monkeypatch):
    runner, repo, args, before, _ = local_notes
    path = repo.path / "CHANGELOG.md"
    if problem == "symlink":
        path.unlink()
        path.symlink_to(repo.path / "VERSION")
    if problem == "version":
        (repo.path / "VERSION").write_text("1.0.0\n")
    if problem == "remote":
        monkeypatch.setattr(m, "_check_pr_head", Mock(side_effect=m.StaleTargetError("moved")))

    def answer(message):
        if "Add special notes" in message:
            if problem == "concurrent":
                path.write_text("Concurrent user edit\n")
            if problem == "branch":
                runner.overrides[("git", "branch", "--show-current")] = "another-branch"
            return ""
        return ""

    with pytest.raises(m.ReleaseError):
        n.finish_notes(runner, repo, args, Mock(ask=answer))
    assert (repo.path / "release/manifest.yaml").read_text() == before["release/manifest.yaml"]
    if problem == "concurrent":
        assert path.read_text() == "Concurrent user edit\n"


def test_editable_checkout_is_separate_and_preserves_dirty_retry(tmp_path, monkeypatch):
    runner = Fake()
    repo = m.Repository(tmp_path / "base", "example/base", "main")
    repo.path.mkdir()
    original = repo.path / "user.txt"
    original.write_text("keep")
    pr = release_pr()
    monkeypatch.setattr(m, "_release_prs", lambda *a: [pr])
    root = tmp_path / "worktrees"
    checkout = n.editable_checkout(runner, repo, root, pr)
    runner.overrides[("git", "branch", "--show-current")] = f"release-notes/42-{SHA}"
    edited = checkout.path / "draft.md"
    edited.write_text("unfinished user edit")
    runner.overrides[("git", "status", "--porcelain")] = " M draft.md"
    assert n.editable_checkout(runner, repo, root, pr) == checkout
    assert edited.read_text() == "unfinished user edit" and original.read_text() == "keep"
    additions = [a for a, _ in runner.calls if a[:3] == ("git", "worktree", "add")]
    assert len(additions) == 1 and "-b" in additions[0] and "--detach" not in additions[0]
    runner.heads[checkout.path] = "c" * 40
    runner.overrides[("git", "rev-list", "--parents", "-n", "1", "HEAD")] = "unrelated history"
    runner.overrides[("git", "diff", "--name-only", "--no-renames", "-z", SHA, "HEAD", "--")] = (
        "CHANGELOG.md\0"
    )
    with pytest.raises(m.ReleaseError, match="identity changed"):
        n.editable_checkout(runner, repo, root, pr)


def test_todo_gate_stops_before_expensive_checks(monkeypatch, tmp_path):
    runner = Fake()
    repo = m.Repository(tmp_path, "example/base", "main")
    monkeypatch.setattr(m, "_release_prs", lambda *a: [release_pr()])
    runner.overrides[("git", "show", f"HEAD:release/migrations/{TAG}.md")] = scaffold()
    with pytest.raises(m.ReleaseError, match="Review notes"):
        m.check_release(runner, repo, release_pr())
    assert not runner.live_calls


@pytest.mark.parametrize("failure", [False, True])
def test_validation_summary_keeps_full_private_log_and_bounded_errors(capsys, failure):
    runner = SubprocessRunner()
    code = "print('success detail\\n' * 1500); "
    code += "print('ERROR: TODO migration incomplete'); raise SystemExit(2)" if failure else ""
    result = runner.run_live((sys.executable, "-c", code))
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    log = Path(output.strip().removeprefix("Full validation log: "))
    assert log.stat().st_mode & 0o777 == 0o600
    assert len(log.read_text().splitlines()) > 1500
    assert result.returncode == (2 if failure else 0)
    assert len(result.stderr.splitlines()) <= 60
    if failure:
        assert "TODO migration incomplete" in result.stderr
    else:
        assert result.stderr == ""


def test_base_environment_keeps_signer_but_not_other_virtualenv(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/another/project/.venv")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/operator/agent")
    env = SubprocessRunner._environment()
    assert "VIRTUAL_ENV" not in env and env["SSH_AUTH_SOCK"] == "/operator/agent"
    assert "PLATFORM_RELEASE_ALLOWED_SIGNER" in env


def test_actual_base_generator_and_migration_parser():
    base = os.environ.get("PLATFORM_RELEASE_BASE_FIXTURE")
    if not base:
        pytest.skip("Set PLATFORM_RELEASE_BASE_FIXTURE to a trusted local Base checkout")
    repo = m.Repository(Path(base), "example/base", "main")
    runner = SubprocessRunner()
    data = n._canonical(runner, repo)
    version = data["config"]["version"]
    changelog = f"## [{version}] - {data['config']['releaseDate']}\n- Reviewed fix.\n"
    prompt = Mock()
    prompt.ask.side_effect = lambda message: "yes" if "Remove only these" in message else ""
    migration, changelog = n.draft_notes(
        data["scaffold"], changelog, data["scaffold"], version, prompt
    )
    script = (
        "import json, runpy, sys; b=runpy.run_path('scripts/platform_release.py'); "
        "b['validate_migration_compatibility'](sys.argv[1], json.loads(sys.argv[2])); "
        "assert not b['contains_todo'](sys.argv[1] + sys.argv[3])"
    )
    m._checked(
        runner,
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            script,
            migration,
            json.dumps(data["config"]["compatibility"]),
            changelog,
        ),
        cwd=repo.path,
    )
    updated = n._canonical(runner, repo, "Operator-reviewed summary")
    assert updated["config"]["summary"] == "Operator-reviewed summary"
    assert "Operator-reviewed summary" in updated["manifest"]
    for value in ("False", "[]", "__import__('datetime').date(2026, 9, 8)"):
        # Exercise raw YAML-compatible types before Base or JSON can stringify them.
        script_with_bad_summary = n.CANONICAL.replace(
            "config = b['load_yaml'](b['CONFIG_PATH'])",
            "config = b['load_yaml'](b['CONFIG_PATH'])\nconfig['summary'] = " + value,
        )
        with pytest.raises(m.ReleaseError, match="summary must be text or null"):
            m._checked(
                runner,
                ("uv", "run", "--frozen", "python", "-c", script_with_bad_summary),
                cwd=repo.path,
            )
    # Exercise the actual parser with the current preparation defaults too, even
    # when the fixture's own release declares fresh-install-only recovery.
    migration, changelog = n.draft_notes(scaffold(), CHANGELOG, scaffold(), "1.0.1", prompt)
    compatibility = {
        "stableUpgrade": "supported",
        "upgradesFromAlphaRevisions": [],
        "downgrade": "unsupported",
        "recovery": "forward-fix",
    }
    m._checked(
        runner,
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            script,
            migration,
            json.dumps(compatibility),
            changelog,
        ),
        cwd=repo.path,
    )
