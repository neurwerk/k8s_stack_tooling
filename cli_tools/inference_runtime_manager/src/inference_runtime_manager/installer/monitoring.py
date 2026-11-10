"""Host-scoped CPU and GPU exporters for an external Prometheus scraper."""

from __future__ import annotations

import os
import subprocess
from importlib.resources import files

import questionary

from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.images import (
    install_local_images,
    prepare_local_images,
    selected_specs,
)

EXPORTERS = ["node-exporter", "dcgm-exporter"]


def run(action: str) -> None:
    settings = Settings()
    if action == "prepare":
        prepare_local_images(settings, EXPORTERS)
        return
    docker = Docker(DeploymentSettings())
    if action not in {"status", "apply", "stop"}:
        raise ValueError("Unknown monitoring action")
    hostname = subprocess.run(
        docker.docker_command + ["info", "--format", "{{.Name}}"],
        env=docker.environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not hostname:
        raise ValueError("Docker target did not report its hostname")
    if action == "apply":
        install_local_images(docker, settings, EXPORTERS)
    image_tags = {spec.name: spec.tag for spec in selected_specs(EXPORTERS)}
    environment = {
        **docker.environment,
        "NODE_EXPORTER_IMAGE": image_tags["node-exporter"],
        "DCGM_EXPORTER_IMAGE": image_tags["dcgm-exporter"],
        "MONITORING_HOSTNAME": hostname,
        "MONITORING_BIND_ADDRESS": docker.settings.bind_address,
    }
    arguments = {
        "status": ["ps", "--all"],
        "apply": ["up", "-d", "--pull", "never"],
        "stop": ["stop"],
    }[action]
    subprocess.run(
        docker.docker_command
        + [
            "compose",
            "--project-name",
            "local-ai-monitoring",
            "--env-file",
            os.devnull,
            "-f",
            str(files("inference_runtime_manager.resources").joinpath("monitoring.yaml")),
            *arguments,
        ],
        env=environment,
        check=True,
    )


def menu() -> None:
    while True:
        action = questionary.select(
            "Host monitoring exporters (metrics are stored by your Prometheus server)",
            choices=[
                questionary.Choice("Show exporter status", value="status"),
                questionary.Choice("Prepare exporter images on workstation", value="prepare"),
                questionary.Choice("Install / update exporters on selected target", value="apply"),
                questionary.Choice("Stop exporters on selected target", value="stop"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action in (None, "back"):
            return
        run(action)
