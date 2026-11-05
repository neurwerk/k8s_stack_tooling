"""Workstation entry point for private model downloads and inference runtimes."""

from __future__ import annotations

import argparse
import subprocess

import questionary
from pydantic import ValidationError

from inference_runtime_manager.configuration import management_state
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
from inference_runtime_manager.installer.voice import voice_cloning_available


def execute(action: str, aliases: list[str] | None = None, dry_run: bool = False) -> None:
    from inference_runtime_manager import workflow

    local_actions = {
        "guided": workflow.guided_models,
        "runtimes": workflow.runtime_updates,
        "configuration": workflow.configuration_menu,
        "select": workflow.select_models,
        "stage": workflow.upload_models,
        "assign": workflow.assign_models,
        "plan": workflow.deployment_status,
        "live": lambda: workflow.deployment_status(remote=True),
        "remote": lambda: workflow.deployment_status(remote=True, verify_remote=True),
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
        settings = Settings()
        root = settings.storage_root
        state_root = management_state(root)
        deployment = load_deployment(state_root)
        configured = deployment_services(state_root)
        selected = aliases or [
            name for name, assignment in deployment.assignments.items() if assignment.enabled
        ]
        if len(set(selected)) != len(selected) or any(
            name not in deployment.assignments for name in selected
        ):
            raise ValueError("Select each assigned model slot at most once")
        if dry_run:
            provision(None, root, state_root, selected, dry_run=True)
            return
    docker = Docker(DeploymentSettings())
    if action == "install":
        docker.install(Settings())
    elif action == "upload":
        settings = Settings()
        provision(
            docker,
            settings.storage_root,
            management_state(settings.storage_root),
            selected,
            dry_run=dry_run,
        )
    elif action == "status":
        docker.run("ps", "--all")
    elif action == "config":
        # Quiet validation avoids printing the interpolated API key.
        docker.run("config", "--quiet")


def menu() -> None:
    from inference_runtime_manager import workflow

    while True:
        choices = [
            questionary.Separator("── Guided Setup ──"),
            questionary.Choice("1. Choose and configure a model", value="guided"),
            questionary.Separator(" "),
            questionary.Choice("2. Test endpoints manually", value="manual-tests"),
            questionary.Choice("3. Live deployment status", value="live"),
            questionary.Separator(" "),
            questionary.Choice("5. Advanced operations", value="advanced"),
            questionary.Choice("6. Setup", value="setup"),
            *(
                [questionary.Choice("Record and provision default TTS voice", value="voice")]
                if voice_cloning_available()
                else []
            ),
            questionary.Separator(" "),
            *(
                [questionary.Choice([("fg:ansigreen", "Apply saved changes")], value="apply")]
                if workflow.pending_changes()
                else []
            ),
            questionary.Choice("Exit", value="exit"),
        ]
        action = questionary.select("Inference Runtime Manager", choices=choices).ask()
        if action in (None, "exit"):
            return
        try:
            if action == "guided":
                from inference_runtime_manager import workflow

                workflow.guided_models()
            elif action == "configuration":
                from inference_runtime_manager import workflow

                workflow.configuration_menu()
            elif action == "advanced":
                advanced_menu()
            elif action == "setup":
                from inference_runtime_manager import workflow

                workflow.configuration_menu()
            else:
                execute(action)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            # Pydantic validation can include input values. Never print it here.
            print(
                "Invalid or incomplete settings; use Setup."
                if isinstance(exc, ValidationError)
                else f"Failed: {exc}"
            )


def advanced_menu() -> None:
    while True:
        action = questionary.select(
            "Advanced operations",
            choices=[
                questionary.Choice("Manage downloads", value="manage-downloads"),
                questionary.Choice("Downloaded inventory", value="inventory"),
                questionary.Choice("Enable / disable a service", value="toggle"),
                questionary.Choice("Prepare or update runtime images", value="runtimes"),
                questionary.Choice("Verify remote model files (full SHA-256)", value="remote"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action in (None, "back"):
            return
        if action != "manage-downloads":
            execute(action)
            continue
        while True:
            action = questionary.select(
                "Manage downloads",
                choices=[
                    questionary.Choice("View download queue", value="queue"),
                    questionary.Choice("Edit download queue", value="select"),
                    questionary.Choice("Download queued models", value="download"),
                    questionary.Choice("Back", value="back"),
                ],
            ).ask()
            if action is None:
                return
            if action == "back":
                break
            execute(action)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=[
            "downloads",
            "guided",
            "runtimes",
            "configuration",
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
            "live",
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
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(
            "Invalid or incomplete settings; use Setup."
            if isinstance(exc, ValidationError)
            else f"Failed: {exc}"
        )
        raise SystemExit(1) from None
