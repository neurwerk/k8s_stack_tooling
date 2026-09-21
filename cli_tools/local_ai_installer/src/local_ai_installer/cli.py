"""Workstation entry point for downloads and explicit LocalAI installation jobs."""

from __future__ import annotations

import argparse
import subprocess

import questionary
from pydantic import ValidationError

from local_ai_installer.downloader.config import Settings
from local_ai_installer.downloader.main import main as downloads
from local_ai_installer.installer.docker import DeploymentSettings, Docker, slots
from local_ai_installer.installer.images import download_images
from local_ai_installer.installer.upload import provision


def execute(action: str, aliases: list[str] | None = None, dry_run: bool = False) -> None:
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
    selected = aliases or [name for name, slot in slots().items() if slot["enabled"]]
    if action == "upload":
        if len(set(selected)) != len(selected) or any(name not in slots() for name in selected):
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
        questionary.Choice("Download/select model files", value="downloads"),
        questionary.Choice("Download offline Docker/backend bundle", value="images"),
        questionary.Choice("View deployment slots", value="slots"),
        questionary.Choice("Install/update stock LocalAI (maintenance restart)", value="install"),
        questionary.Choice("Upload/apply model selections (maintenance restart)", value="upload"),
        questionary.Choice("Server status", value="status"),
        questionary.Choice("Exit", value="exit"),
    ]
    while True:
        action = questionary.select("LocalAI Installer", choices=choices).ask()
        if action in (None, "exit"):
            return
        aliases = None
        if action == "upload":
            aliases = questionary.checkbox(
                "Select model slots to provision",
                choices=[
                    questionary.Choice(name, checked=p["enabled"]) for name, p in slots().items()
                ],
            ).ask()
            if not aliases:
                continue
        try:
            execute(action, aliases)
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
        choices=["downloads", "images", "slots", "install", "upload", "status", "config"],
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
