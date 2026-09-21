"""Explicit-context Docker operations, with package-relative Compose resources."""

from __future__ import annotations

import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentSettings(BaseSettings):
    """Deployment settings do not require mounted model storage or cluster access."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    docker_context: str = Field(validation_alias="LOCAL_AI_INSTALLER_DOCKER_CONTEXT", min_length=1)
    api_key: SecretStr = Field(validation_alias="LOCALAI_API_KEY", min_length=1)
    bind_address: str = Field(default="127.0.0.1", validation_alias="LOCALAI_BIND_ADDRESS")
    port: int = Field(default=8080, ge=1, le=65535, validation_alias="LOCALAI_PORT")
    max_backends: int = Field(default=3, ge=1, validation_alias="LOCALAI_MAX_ACTIVE_BACKENDS")
    vram_budget: str = Field(default="14GiB", validation_alias="LOCALAI_VRAM_BUDGET")
    threads: int = Field(default=4, ge=1, validation_alias="LOCALAI_THREADS")


def resource(name: str) -> str:
    """Read deployment resources from the installed wheel as well as a checkout."""
    return files("local_ai_installer.resources").joinpath(name).read_text(encoding="utf-8")


def slots() -> dict[str, Any]:
    return yaml.safe_load(resource("slots.yaml"))


class Docker:
    """A workstation-side helper; no SSH command or Docker socket inside containers."""

    def __init__(self, settings: DeploymentSettings):
        from local_ai_installer.installer.images import image_specs

        runtime = {image.name: image.tag for image in image_specs() if not image.backend}
        self.settings = settings
        self.environment = {
            **os.environ,
            "LOCALAI_API_KEY": settings.api_key.get_secret_value(),
            "LOCALAI_BIND_ADDRESS": settings.bind_address,
            "LOCALAI_PORT": str(settings.port),
            "LOCALAI_MAX_ACTIVE_BACKENDS": str(settings.max_backends),
            "LOCALAI_VRAM_BUDGET": settings.vram_budget,
            "LOCALAI_THREADS": str(settings.threads),
            "LOCALAI_IMAGE": runtime["localai"],
            "LOCALAI_SETUP_IMAGE": runtime["setup"],
        }
        # Hatch installs resources as real files. The package has no host bind
        # mounts, so this path is only read by the workstation's Compose CLI.
        compose_file = str(files("local_ai_installer.resources").joinpath("compose.yaml"))
        self.docker_command = ["docker", "--context", settings.docker_context]
        self.command = self.docker_command + [
            "compose",
            "--project-name",
            "local-ai",
            "--env-file",
            os.devnull,
            "-f",
            compose_file,
            "--profile",
            "setup",
        ]

    def run(self, *args: str, capture: bool = False, **kwargs):
        return subprocess.run(
            self.command + list(args),
            env=self.environment,
            check=True,
            stdout=subprocess.PIPE if capture else None,
            **kwargs,
        )

    def worker_command(self) -> list[str]:
        source = Path(__file__).with_name("worker.py").read_text(encoding="utf-8")
        return self.command + ["run", "--pull", "never", "--rm", "-T", "--no-deps", "setup", source]

    def worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        import json

        result = subprocess.run(
            self.worker_command(),
            env=self.environment,
            check=True,
            input=json.dumps(payload) + "\n",
            text=True,
            stdout=subprocess.PIPE,
        )
        return json.loads(result.stdout)

    def install(self, storage: Path) -> None:
        """Stage verified offline assets before entering the maintenance window."""
        from local_ai_installer.installer.images import stage_bundle

        packages, auxiliary = stage_bundle(self, storage)
        self.run("stop", "localai")
        self.worker({"action": "pkuseg-cache", "manifest": auxiliary})
        for name, uri in packages:
            self.run(
                "run",
                "--pull",
                "never",
                "--rm",
                "--no-deps",
                "backend-install",
                "backends",
                "install",
                uri,
                name,
            )
        definitions = {}
        for alias, preset in slots().items():
            config = preset["config"]
            config.update(name=alias, disabled=True)
            config.setdefault("parameters", {})["model"] = preset.get(
                "cache_repo", f"not-staged/{alias}/{preset.get('model_file', 'snapshot')}"
            )
            config["limits"] = {"max_concurrent": 1, "retry_after_seconds": 2}
            definitions[alias] = yaml.safe_dump(config, sort_keys=False)
        self.worker({"action": "seed", "definitions": definitions})
        self.run("up", "--pull", "never", "-d", "--wait", "--wait-timeout", "180", "localai")
