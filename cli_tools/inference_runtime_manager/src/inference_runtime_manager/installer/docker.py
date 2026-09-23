"""Explicit-context Docker operations with package-owned Compose resources."""

from __future__ import annotations

import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    docker_context: str = Field(
        validation_alias=AliasChoices(
            "INFERENCE_RUNTIME_MANAGER_DOCKER_CONTEXT", "LOCAL_AI_INSTALLER_DOCKER_CONTEXT"
        ),
        min_length=1,
    )
    api_key: SecretStr = Field(
        validation_alias=AliasChoices("INFERENCE_RUNTIME_MANAGER_API_KEY", "LOCALAI_API_KEY"),
        min_length=1,
    )
    bind_address: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices(
            "INFERENCE_RUNTIME_MANAGER_BIND_ADDRESS", "LOCALAI_BIND_ADDRESS"
        ),
    )
    vlm_documents_port: int = Field(default=8000, ge=1, le=65535, alias="VLM_DOCUMENTS_PORT")
    llm_general_port: int = Field(default=8001, ge=1, le=65535, alias="LLM_GENERAL_PORT")
    vlm_general_port: int = Field(default=8002, ge=1, le=65535, alias="VLM_GENERAL_PORT")
    stt_general_port: int = Field(default=8003, ge=1, le=65535, alias="STT_GENERAL_PORT")
    tts_german_port: int = Field(default=8004, ge=1, le=65535, alias="TTS_GERMAN_PORT")
    vad_general_port: int = Field(default=8005, ge=1, le=65535, alias="VAD_GENERAL_PORT")
    ner_german_port: int = Field(default=8006, ge=1, le=65535, alias="NER_GERMAN_PORT")
    vllm_granite_dtype: str = Field(default="float32", alias="VLLM_GRANITE_DTYPE")
    vllm_granite_gpu_memory_utilization: float = Field(
        default=0.25, gt=0, le=1, alias="VLLM_GRANITE_GPU_MEMORY_UTILIZATION"
    )


def resource(name: str) -> str:
    return files("inference_runtime_manager.resources").joinpath(name).read_text(encoding="utf-8")


def services() -> dict[str, Any]:
    return yaml.safe_load(resource("services.yaml"))


class Docker:
    """Workstation-side Docker helper; target access is always explicit."""

    def __init__(self, settings: DeploymentSettings):
        from inference_runtime_manager.installer.images import image_specs

        images = {image.name: image.tag for image in image_specs()}
        self.settings = settings
        self.environment = {
            **os.environ,
            "INFERENCE_API_KEY": settings.api_key.get_secret_value(),
            "INFERENCE_BIND_ADDRESS": settings.bind_address,
            "VLM_DOCUMENTS_PORT": str(settings.vlm_documents_port),
            "LLM_GENERAL_PORT": str(settings.llm_general_port),
            "VLM_GENERAL_PORT": str(settings.vlm_general_port),
            "STT_GENERAL_PORT": str(settings.stt_general_port),
            "TTS_GERMAN_PORT": str(settings.tts_german_port),
            "VAD_GENERAL_PORT": str(settings.vad_general_port),
            "NER_GERMAN_PORT": str(settings.ner_german_port),
            "VLLM_GRANITE_DTYPE": settings.vllm_granite_dtype,
            "VLLM_GRANITE_GPU_MEMORY_UTILIZATION": str(
                settings.vllm_granite_gpu_memory_utilization
            ),
            "SETUP_IMAGE": images["setup"],
            "VLLM_IMAGE": images["vllm"],
            "LLAMACPP_IMAGE": images["llamacpp"],
            "SPEACHES_IMAGE": images["speaches"],
            "CHATTERBOX_IMAGE": images["chatterbox"],
            "KSERVE_IMAGE": images["kserve"],
        }
        compose_file = str(files("inference_runtime_manager.resources").joinpath("compose.yaml"))
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
        """Load verified images and remove containers from the superseded Compose definition."""
        from inference_runtime_manager.installer.images import stage_bundle

        stage_bundle(self, storage)
        self.worker({"action": "configure"})
        self.run("up", "--pull", "never", "--remove-orphans", "--no-start", "setup")
        self.run("rm", "-f", "setup")
