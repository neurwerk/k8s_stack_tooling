"""Workstation entry point for downloads and explicit LocalAI installation jobs."""

from __future__ import annotations

import argparse
import subprocess

import questionary
from pydantic import ValidationError

from local_ai_installer.downloader.config import Settings
from local_ai_installer.downloader.main import main as downloads
from local_ai_installer.installer.api_tests import menu as api_tests
from local_ai_installer.installer.assignments import deployment_slots
from local_ai_installer.installer.docker import DeploymentSettings, Docker, slots
from local_ai_installer.installer.images import download_images
from local_ai_installer.installer.upload import provision


def execute(action: str, aliases: list[str] | None = None, dry_run: bool = False) -> None:
    from local_ai_installer import workflow

    local_actions = {
        "select": workflow.select_models,
        "stage": workflow.upload_models,
        "assign": workflow.assign_models,
        "plan": workflow.deployment_status,
        "remote": lambda: workflow.deployment_status(remote=True),
        "apply": lambda: workflow.apply_assignments(execute),
        "test": api_tests,
        "verify": workflow.test_models,
    }
    if action in local_actions:
        local_actions[action]()
        return
    download_actions = {
        "download": "download",
        "inventory": "view_installed",
        "queue": "view_queue",
        "login": "authentication",
        "storage": "storage_status",
    }
    if action in download_actions:
        if action == "download":
            download_images(Settings())
        downloads(download_actions[action])
        return
    if action == "downloads":
        downloads()
        return
    if action == "images":
        download_images(Settings())
        return
    if action == "slots":
        for alias, preset in slots().items():
            state = "enabled after upload" if preset["enabled"] else "disabled backup"
            print(f"{alias}: {preset['model_id']}/{preset['variant_id']} ({state})")
        return
    configured = deployment_slots(Settings().storage_root) if action == "upload" else slots()
    selected = aliases or [name for name, slot in configured.items() if slot["enabled"]]
    if action == "upload":
        if len(set(selected)) != len(selected) or any(name not in configured for name in selected):
            raise ValueError("Select each known model slot at most once")
        if dry_run:
            provision(None, Settings().storage_root, selected, dry_run=True)
            return
    docker = Docker(DeploymentSettings())
    if action == "install":
        docker.install(Settings().storage_root)
    elif action == "upload":
        provision(docker, Settings().storage_root, selected, dry_run=dry_run)
    elif action == "status":
        docker.run("ps", "--all")
    elif action == "config":
        # Quiet validation avoids printing the interpolated API key.
        docker.run("config", "--quiet")


def menu() -> None:
    choices = [
        questionary.Separator("── Models ──"),
        questionary.Choice("1. Browse catalog / select downloads", value="select"),
        questionary.Choice("2. Download selected models", value="download"),
        questionary.Choice("3. Upload downloaded models (files only)", value="stage"),
        questionary.Choice("4. Assign uploaded models to aliases", value="assign"),
        questionary.Choice("5. Review / apply assignments (restart)", value="apply"),
        questionary.Choice("6. Test API endpoints", value="test"),
        questionary.Choice("Record model verification (TTS / chat)", value="verify"),
        questionary.Separator("── Inventory ──"),
        questionary.Choice("Deployment table (offline)", value="plan"),
        questionary.Choice("Verify remote model files", value="remote"),
        questionary.Choice("Download queue", value="queue"),
        questionary.Choice("Downloaded inventory", value="inventory"),
        questionary.Separator("── Setup ──"),
        questionary.Choice("Hugging Face login", value="login"),
        questionary.Choice("Storage status", value="storage"),
        questionary.Choice("Download Docker/backend/cache bundle", value="images"),
        questionary.Choice("Install/update LocalAI runtime (restart)", value="install"),
        questionary.Choice("Server status", value="status"),
        questionary.Choice("Exit", value="exit"),
    ]
    while True:
        action = questionary.select("LocalAI Installer", choices=choices).ask()
        if action in (None, "exit"):
            return
        try:
            if (
                action != "install"
                or questionary.confirm(
                    "Install/update runtime on the configured target?", default=False
                ).ask()
            ):
                execute(action)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            # Pydantic validation can include input values. Never print it here.
            print(
                "Invalid settings; check .env."
                if isinstance(exc, ValidationError)
                else f"Failed: {exc}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "downloads",
            "images",
            "slots",
            "install",
            "upload",
            "status",
            "config",
            "select",
            "download",
            "stage",
            "assign",
            "plan",
            "remote",
            "apply",
            "test",
            "verify",
            "inventory",
            "queue",
            "login",
            "storage",
        ],
    )
    parser.add_argument("slots", nargs="*")
    parser.add_argument(
        "--dry-run", action="store_true", help="Verify local upload inputs without server access"
    )
    args = parser.parse_intermixed_args()
    if args.dry_run and args.action != "upload":
        parser.error("--dry-run is only valid for upload")
    if args.slots and args.action != "upload":
        parser.error("Model slot arguments are only valid for upload")
    try:
        if args.action:
            execute(args.action, args.slots, args.dry_run)
        else:
            menu()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(
            "Invalid settings; check .env."
            if isinstance(exc, ValidationError)
            else f"Failed: {exc}"
        )
        raise SystemExit(1) from None
