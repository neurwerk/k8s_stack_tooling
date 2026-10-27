"""Small target-first workstation UI over Base and the two existing Python release workflows."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import questionary
from dotenv import load_dotenv

from platform_release import main as base
from platform_release.commands import CommandRunner, SubprocessRunner
from platform_release.python_releases import (
    check_python,
    publish_python,
    release_spec,
    reviewed_checkout,
    status_python,
    verify_python,
)

ACTIONS = ("status", "check", "publish", "verify")
TARGETS = {"base": "Base", "tooling": "Tooling", "setup": "Setup"}


def parser() -> argparse.ArgumentParser:
    """Build offline help; no repository, network, signing or SDK import is involved."""
    result = argparse.ArgumentParser(
        prog="release-manager",
        description="Release manager for Base, Tooling and setup.",
        epilog="Run here: tooling/cli_tools/platform_release. Select a target for its menu.",
    )
    targets = result.add_subparsers(dest="target")
    for target in ("base", "tooling", "setup"):
        command = targets.add_parser(
            target, help=f"{target} release status, checks, publication and verification"
        )
        command.add_argument("action", nargs="?", choices=ACTIONS)
        command.add_argument(
            "--repo", type=Path, help="Trusted target repository; original edits are preserved."
        )
        command.add_argument(
            "--commit",
            help="Reviewed full SHA for Python check/publish/verify or Base publish.",
        )
        command.add_argument(
            "--tag", help="Base release tag; Python tags come from committed project metadata."
        )
        command.add_argument("--pr", type=int, help="Base check: existing release PR number.")
        command.add_argument(
            "--worktree-root", type=Path, help="Parent for isolated exact-commit checkouts."
        )
        command.add_argument("--signing-public-key", type=Path, default=base._default_public_key())
        command.add_argument(
            "--confirm", help="Noninteractive publication: PUBLISH OWNER/REPO TAG COMMIT."
        )
        command.add_argument(
            "--verbose", action="store_true", help="Also show live validation output."
        )
    return result


def load_cli_env() -> None:
    """Load only the optional .env in the explicitly selected CLI project working directory."""
    project = Path.cwd() / "pyproject.toml"
    if not project.is_file():
        return
    if tomllib.loads(project.read_text()).get("project", {}).get("name") != "platform-release":
        return
    config = project.parent / ".env"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise base.ReleaseError("the CLI project's .env must be a regular local file")
    load_dotenv(dotenv_path=config, override=False)


def repository_path(args: argparse.Namespace, prompt: base.Prompt | None = None) -> Path:
    """Use explicit flags, existing process environment, then local dotenv; never guess a repo."""
    if args.repo is not None:
        return cast(Path, args.repo).expanduser()
    workspace = os.environ.get("WORKSPACE_ROOT")
    if workspace:
        root = Path(workspace).expanduser()
        if not root.is_absolute():
            raise base.ReleaseError("WORKSPACE_ROOT must be an absolute workspace path")
        return root / ("base" if args.target == "base" else "tooling")
    if prompt is not None:
        path = prompt.ask(f"{TARGETS[args.target]} repository path: ")
        if path:
            args.repo = Path(path).expanduser()
            return args.repo
    raise base.ReleaseError(
        "pass --repo or set absolute WORKSPACE_ROOT in the CLI project's .env or environment"
    )


def _reviewed_commit(args: argparse.Namespace, prompt: base.Prompt, interactive: bool) -> str:
    value = args.commit
    if value is None and interactive:
        value = prompt.ask("Reviewed full commit SHA (no moving-branch default): ")
    if not isinstance(value, str) or base.SHA_PATTERN.fullmatch(value) is None:
        raise base.ReleaseError("--commit must supply the reviewed full 40-character SHA")
    print(f"Selected reviewed commit: {value}")
    return value


def _base_action(
    args: argparse.Namespace, runner: CommandRunner, prompt: base.Prompt, interactive: bool
) -> int:
    """Delegate the Base lifecycle unchanged, including its protected staging workflow."""
    path = repository_path(args, prompt if interactive else None)
    action = "continue" if args.action == "verify" else args.action
    if action == "publish":
        commit = _reviewed_commit(args, prompt, interactive)
        args.commit = commit
        repo = base._repository(runner, path)
        tip = base._remote_ref(runner, repo, f"refs/heads/{repo.default_branch}")
        if commit != tip:
            raise base.ReleaseError(
                "Base's existing publication flow requires the reviewed remote main tip"
            )
    tag = args.tag
    if action in {"publish", "continue"} and tag is None and interactive:
        tag = prompt.ask("Exact Base release tag (vX.Y.Z): ")
    if action in {"publish", "continue"} and tag is None:
        raise base.ReleaseError("Base publish/verify requires --tag vX.Y.Z")
    arguments = _base_arguments(args, path, action, tag)
    return base.run_cli(arguments, runner=runner, prompt=prompt, isatty=interactive)


def _base_arguments(
    args: argparse.Namespace, path: Path, action: str, tag: str | None
) -> list[str]:
    arguments: list[str] = ["--base-repo", str(path)]
    if args.worktree_root is not None:
        arguments.extend(("--worktree-root", str(args.worktree_root)))
    if args.verbose:
        arguments.append("--verbose")
    if action == "publish":
        arguments.append("--allow-local-preparation")
        arguments.extend(("--reviewed-commit", args.commit))
    arguments.append(action)
    if tag is not None:
        arguments.extend(("--tag", tag))
    if action == "check" and args.pr is not None:
        arguments.extend(("--pr", str(args.pr)))
    if action == "publish":
        arguments.extend(("--signing-public-key", str(args.signing_public_key)))
        if args.confirm is not None:
            arguments.extend(("--confirm", args.confirm))
    return arguments


def _python_action(
    args: argparse.Namespace, runner: CommandRunner, prompt: base.Prompt, interactive: bool
) -> int:
    repo = base._repository(runner, repository_path(args, prompt if interactive else None))
    if repo.slug != "neurwerk/k8s_stack_tooling":
        raise base.ReleaseError("tooling/setup releases require neurwerk/k8s_stack_tooling")
    if args.tag is not None or args.pr is not None:
        raise base.ReleaseError(
            "Python release tags are derived from committed metadata; --tag/--pr are Base-only"
        )
    if args.action == "status":
        commit = args.commit or base._checked(runner, ("git", "rev-parse", "HEAD"), cwd=repo.path)
    else:
        commit = _reviewed_commit(args, prompt, interactive)
        repo = reviewed_checkout(runner, repo, args.target, commit, args.worktree_root)
    release = release_spec(runner, repo, args.target, commit)
    if args.action == "status":
        status_python(runner, repo, release)
    elif args.action == "check":
        check_python(runner, repo, release)
    elif args.action == "publish":
        if not interactive and args.confirm is None:
            raise base.ReleaseError(
                "noninteractive publish requires --confirm with repository/tag/commit"
            )
        publish_python(runner, repo, release, args.signing_public_key, prompt, args.confirm)
    else:
        verify_python(runner, repo, release, args.signing_public_key)
    return 0


def menu_action(
    command_parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.Namespace | None:
    """Select a target/action; Back discards target-specific arguments before returning."""
    if args.target is None:
        print("Release manager\nChoose a target, then an action.")
        target = questionary.select(
            "Release target",
            choices=[
                *(questionary.Choice(label, value=value) for value, label in TARGETS.items()),
                questionary.Choice("Exit", value="exit"),
            ],
        ).ask()
        if target in (None, "exit"):
            return None
        args = command_parser.parse_args([target])
    choice = questionary.select(
        f"{TARGETS[args.target]} release",
        choices=[
            questionary.Choice("Show status", value="status"),
            questionary.Choice("Check", value="check"),
            questionary.Choice("Sign and publish", value="publish"),
            questionary.Choice("Verify publication", value="verify"),
            questionary.Choice("Back", value="back"),
        ],
        default="status",
    ).ask()
    if choice is None:
        return None
    if choice == "back":
        return command_parser.parse_args([])
    args.action = choice
    return args


def run_cli(
    arguments: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    prompt: base.Prompt | None = None,
    isatty: bool | None = None,
) -> int:
    """Show offline help outside a terminal, otherwise offer targets and simple action menus."""
    command_parser = parser()
    args = command_parser.parse_args(arguments)
    interactive = (sys.stdin.isatty() and sys.stdout.isatty()) if isatty is None else isatty
    if args.target is None and not interactive:
        command_parser.print_help()
        return 0
    if getattr(args, "action", None) is None and not interactive:
        command_parser.parse_args([args.target, "--help"])
        return 0
    active_runner = runner or SubprocessRunner(verbose=getattr(args, "verbose", False))
    active_prompt = prompt or base.TerminalPrompt()
    menu = args.target is None or args.action is None
    try:
        load_cli_env()
        while True:
            if menu:
                selected = menu_action(command_parser, args)
                if selected is None:
                    return 0
                args = selected
                if args.target is None:
                    continue
            result = (
                _base_action(args, active_runner, active_prompt, interactive)
                if args.target == "base"
                else _python_action(args, active_runner, active_prompt, interactive)
            )
            if not menu or args.action == "publish" or result:
                return result
            args.action = None
    except KeyboardInterrupt:
        print(
            "Release operation cancelled; inspect partial tag state before retrying.",
            file=sys.stderr,
        )
        return 130
    except (base.ReleaseError, ValueError, TypeError, KeyError, OSError, EOFError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def main() -> None:
    """Run the installed release-manager entry point."""
    raise SystemExit(run_cli())
