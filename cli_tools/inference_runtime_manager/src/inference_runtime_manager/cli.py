"""Workstation entry point for private model downloads and inference runtimes."""

from __future__ import annotations

import argparse
import subprocess

import questionary
from pydantic import ValidationError

from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.downloader.main import main as downloads
from inference_runtime_manager.installer.api_tests import manual_menu as manual_tests
from inference_runtime_manager.installer.api_tests import menu as api_tests
from inference_runtime_manager.installer.api_tests import tts_menu
from inference_runtime_manager.installer.assignments import deployment_services, load_deployment
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker, services
from inference_runtime_manager.installer.images import download_images
from inference_runtime_manager.installer.upload import provision
from inference_runtime_manager.installer.voice import menu as voice


def execute(action: str, aliases: list[str] | None = None, dry_run: bool = False) -> None:
    from inference_runtime_manager import workflow

    local_actions = {
        "select": workflow.select_models,
        "stage": workflow.upload_models,
        "assign": workflow.assign_models,
        "plan": workflow.deployment_status,
        "remote": lambda: workflow.deployment_status(remote=True),
        "toggle": workflow.toggle_service,
        "apply": workflow.apply_assignments,
        "manual-tests": manual_tests,
        "test": api_tests,
        "tts-test": tts_menu,
        "voice": voice,
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
        for alias, preset in services().items():
            state = "enabled" if preset["enabled"] else "disabled"
            runtime = preset["runtime"]
            print(f"{alias}: {preset['model_id']}/{preset['variant_id']} [{runtime}] ({state})")
        return
    configured = services()
    selected = aliases or [name for name, slot in configured.items() if slot["enabled"]]
    if action == "upload":
        root = Settings().storage_root
        deployment = load_deployment(root)
        configured = deployment_services(root)
        selected = aliases or [
            name for name, assignment in deployment.assignments.items() if assignment.enabled
        ]
        if len(set(selected)) != len(selected) or any(
            name not in deployment.assignments for name in selected
        ):
            raise ValueError("Select each assigned model slot at most once")
        if dry_run:
            provision(None, root, selected, dry_run=True)
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
        questionary.Choice("5. Enable / disable a service", value="toggle"),
        questionary.Choice("6. Review / apply assignments", value="apply"),
        questionary.Separator(" "),
        questionary.Separator("── Manual Tests ──"),
        questionary.Choice("7. Test endpoints", value="manual-tests"),
        questionary.Separator(" "),
        questionary.Separator("── Inventory ──"),
        questionary.Choice("Deployment table (offline)", value="plan"),
        questionary.Choice("Verify remote model files", value="remote"),
        questionary.Choice("Download queue", value="queue"),
        questionary.Choice("Downloaded inventory", value="inventory"),
        questionary.Separator(" "),
        questionary.Separator("── Setup ──"),
        questionary.Choice("Hugging Face login", value="login"),
        questionary.Choice("Storage status", value="storage"),
        questionary.Choice("Download runtime image bundle", value="images"),
        questionary.Choice("Install/update runtime images", value="install"),
        questionary.Choice("Server status", value="status"),
        questionary.Choice("Record and provision default TTS voice", value="voice"),
        questionary.Separator(" "),
        questionary.Choice("Exit", value="exit"),
    ]
    while True:
        action = questionary.select("Inference Runtime Manager", choices=choices).ask()
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
            "toggle",
            "plan",
            "remote",
            "apply",
            "manual-tests",
            "test",
            "tts-test",
            "voice",
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
