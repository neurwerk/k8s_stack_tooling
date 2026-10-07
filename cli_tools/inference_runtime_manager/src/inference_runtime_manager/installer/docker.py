"""Explicit-context Docker operations with package-owned Compose resources."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import yaml
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from inference_runtime_manager.configuration import load_workstation_config


def _configured_docker_context() -> str:
    value = load_workstation_config().docker_context
    if value is None:
        raise ValueError("Missing workstation configuration: docker_context")
    return value


class DeploymentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
    docker_context: str = Field(
        default_factory=_configured_docker_context,
        validation_alias=AliasChoices(
            "INFERENCE_RUNTIME_MANAGER_DOCKER_CONTEXT", "LOCAL_AI_INSTALLER_DOCKER_CONTEXT"
        ),
        min_length=1,
    )
    vlm_documents_api_key: SecretStr | None = Field(default=None, alias="VLM_DOCUMENTS_API_KEY")
    llm_general_api_key: SecretStr | None = Field(default=None, alias="LLM_GENERAL_API_KEY")
    vlm_general_api_key: SecretStr | None = Field(default=None, alias="VLM_GENERAL_API_KEY")
    stt_general_api_key: SecretStr | None = Field(default=None, alias="STT_GENERAL_API_KEY")
    tts_german_api_key: SecretStr | None = Field(default=None, alias="TTS_GERMAN_API_KEY")
    vad_general_api_key: SecretStr | None = Field(default=None, alias="VAD_GENERAL_API_KEY")
    ner_german_api_key: SecretStr | None = Field(default=None, alias="NER_GERMAN_API_KEY")
    ner_english_api_key: SecretStr | None = Field(default=None, alias="NER_ENGLISH_API_KEY")
    ner_multilingual_api_key: SecretStr | None = Field(
        default=None, alias="NER_MULTILINGUAL_API_KEY"
    )
    vlm_images_api_key: SecretStr | None = Field(default=None, alias="VLM_IMAGES_API_KEY")
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
    ner_english_port: int = Field(default=8008, ge=1, le=65535, alias="NER_ENGLISH_PORT")
    ner_multilingual_port: int = Field(default=8009, ge=1, le=65535, alias="NER_MULTILINGUAL_PORT")
    ner_multilingual_dtype: Literal["float32", "float16"] = Field(
        default="float32", alias="NER_MULTILINGUAL_DTYPE"
    )
    ner_german_dtype: Literal["float32", "float16"] = Field(
        default="float32", alias="NER_GERMAN_DTYPE"
    )
    ner_english_dtype: Literal["float32", "float16"] = Field(
        default="float32", alias="NER_ENGLISH_DTYPE"
    )
    gliner_threshold: float = Field(default=0.5, gt=0, lt=1, alias="GLINER_THRESHOLD")
    gliner_labels: list[str] = Field(
        default_factory=lambda: [
            "person",
            "email",
            "phone number",
            "address",
            "date of birth",
            "iban",
            "credit card number",
            "passport number",
            "medical condition",
            "medication",
        ],
        min_length=1,
        max_length=25,
        alias="GLINER_LABELS",
    )
    vlm_images_port: int = Field(default=8007, ge=1, le=65535, alias="VLM_IMAGES_PORT")
    vllm_granite_dtype: str = Field(default="float32", alias="VLLM_GRANITE_DTYPE")
    vllm_granite_gpu_memory_utilization: float = Field(
        default=0.25, gt=0, le=1, alias="VLLM_GRANITE_GPU_MEMORY_UTILIZATION"
    )
    vllm_images_gpu_memory_utilization: float = Field(
        default=0.36, gt=0, le=1, alias="VLLM_IMAGES_GPU_MEMORY_UTILIZATION"
    )
    vllm_nanonets_gpu_memory_utilization: float = Field(
        default=0.65, gt=0, le=1, alias="VLLM_NANONETS_GPU_MEMORY_UTILIZATION"
    )

    @model_validator(mode="after")
    def apply_workstation_config(self) -> DeploymentSettings:
        configured = load_workstation_config()
        if configured.docker_context is not None:
            self.docker_context = configured.docker_context
        for name, value in configured.runtime.model_dump(exclude_none=True).items():
            setattr(self, name, value)
        return self

    def api_key_for(self, alias: str) -> str | None:
        """Return the independently configured key for one stable service alias."""
        value = cast(SecretStr | None, getattr(self, alias.replace("-", "_") + "_api_key"))
        return value.get_secret_value() if value is not None else None


def resource(name: str) -> str:
    return files("inference_runtime_manager.resources").joinpath(name).read_text(encoding="utf-8")


def services() -> dict[str, Any]:
    """Return the package-default recipe for every stable alias."""
    return {
        alias: copy.deepcopy(next(recipe for recipe in recipes if recipe.get("default", False)))
        for alias, recipes in service_recipes().items()
    }


def service_recipes() -> dict[str, list[dict[str, Any]]]:
    """Return every reviewed recipe, accepting legacy single-recipe entries."""
    configured = yaml.safe_load(resource("services.yaml"))
    result: dict[str, list[dict[str, Any]]] = {}
    for alias, value in configured.items():
        recipes = value.get("recipes") if isinstance(value, dict) else None
        if recipes is None:
            recipes = [{**value, "default": True}]
        if not isinstance(recipes, list) or not recipes:
            raise ValueError(f"Service {alias} must define at least one recipe")
        defaults = [recipe for recipe in recipes if recipe.get("default", False)]
        identities = {(recipe.get("model_id"), recipe.get("variant_id")) for recipe in recipes}
        service_names = {recipe.get("service") for recipe in recipes}
        if len(defaults) != 1:
            raise ValueError(f"Service {alias} must define exactly one default recipe")
        if len(identities) != len(recipes) or len(service_names) != len(recipes):
            raise ValueError(f"Service {alias} has duplicate recipe identities or service names")
        result[alias] = copy.deepcopy(recipes)
    return result


class Docker:
    """Workstation-side Docker helper; target access is always explicit."""

    def __init__(self, settings: DeploymentSettings):
        from inference_runtime_manager.installer.images import image_specs

        images = {image.name: image.tag for image in image_specs()}
        self.settings = settings
        self.environment = {
            **os.environ,
            **{
                alias.upper().replace("-", "_") + "_API_KEY": settings.api_key_for(alias) or ""
                for alias in service_recipes()
            },
            "INFERENCE_BIND_ADDRESS": settings.bind_address,
            "VLM_DOCUMENTS_PORT": str(settings.vlm_documents_port),
            "LLM_GENERAL_PORT": str(settings.llm_general_port),
            "VLM_GENERAL_PORT": str(settings.vlm_general_port),
            "STT_GENERAL_PORT": str(settings.stt_general_port),
            "TTS_GERMAN_PORT": str(settings.tts_german_port),
            "VAD_GENERAL_PORT": str(settings.vad_general_port),
            "NER_GERMAN_PORT": str(settings.ner_german_port),
            "NER_ENGLISH_PORT": str(settings.ner_english_port),
            "NER_MULTILINGUAL_PORT": str(settings.ner_multilingual_port),
            "NER_MULTILINGUAL_DTYPE": settings.ner_multilingual_dtype,
            "NER_GERMAN_DTYPE": settings.ner_german_dtype,
            "NER_ENGLISH_DTYPE": settings.ner_english_dtype,
            "GLINER_THRESHOLD": str(settings.gliner_threshold),
            "GLINER_LABELS": json.dumps(settings.gliner_labels),
            "VLM_IMAGES_PORT": str(settings.vlm_images_port),
            "VLLM_GRANITE_DTYPE": settings.vllm_granite_dtype,
            "VLLM_GRANITE_GPU_MEMORY_UTILIZATION": str(
                settings.vllm_granite_gpu_memory_utilization
            ),
            "VLLM_IMAGES_GPU_MEMORY_UTILIZATION": str(settings.vllm_images_gpu_memory_utilization),
            "VLLM_NANONETS_GPU_MEMORY_UTILIZATION": str(
                settings.vllm_nanonets_gpu_memory_utilization
            ),
            "SETUP_IMAGE": images["setup"],
            "VLLM_IMAGE": images["vllm"],
            "LLAMACPP_IMAGE": images["llamacpp"],
            "VLLM_OCR_PROXY_IMAGE": images["vllm-ocr-proxy"],
            "LLAMACPP_OCR_PROXY_IMAGE": images["llamacpp-ocr-proxy"],
            "SPEACHES_IMAGE": images["speaches"],
            "CHATTERBOX_IMAGE": images["chatterbox"],
            "KOKORO_IMAGE": images["kokoro"],
            "KSERVE_IMAGE": images["kserve"],
            "GLINER_IMAGE": images["gliner"],
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

    def running_services(self) -> set[str]:
        return set(
            self.run(
                "ps", "--status", "running", "--services", capture=True, text=True
            ).stdout.splitlines()
        )

    def service_logs(self, service: str) -> str:
        """Read a bounded tail of one service's logs from the configured target."""
        result = subprocess.run(
            self.command + ["logs", "--no-color", "--tail", "100", service],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return result.stdout[-32768:]

    def service_status(self) -> dict[str, tuple[str, str, str]]:
        """Return Compose service state, health and image in a single remote query."""
        output = self.run("ps", "--all", "--format", "json", capture=True, text=True).stdout
        if not output.strip():
            return {}
        parsed = (
            json.loads(output)
            if output.lstrip().startswith("[")
            else [json.loads(line) for line in output.splitlines() if line.strip()]
        )
        missing_images = [item["ID"] for item in parsed if not item.get("Image")]
        inspected = {}
        if missing_images:
            result = subprocess.run(
                self.docker_command + ["inspect", *missing_images],
                env=self.environment,
                check=True,
                capture_output=True,
                text=True,
            )
            inspected = {
                container["Id"]: container["Config"]["Image"]
                for container in json.loads(result.stdout)
            }
        return {
            item["Service"]: (
                item.get("State", "unknown"),
                item.get("Health") or "unknown",
                item.get("Image") or inspected.get(item["ID"], ""),
            )
            for item in parsed
        }

    def image_environment_key(self, runtime: str) -> str:
        return {
            "vllm": "VLLM_IMAGE",
            "vllm-ocr-proxy": "VLLM_OCR_PROXY_IMAGE",
            "llama.cpp": "LLAMACPP_IMAGE",
            "llama.cpp-ocr-proxy": "LLAMACPP_OCR_PROXY_IMAGE",
            "speaches": "SPEACHES_IMAGE",
            "chatterbox": "CHATTERBOX_IMAGE",
            "kokoro-onnx": "KOKORO_IMAGE",
            "kserve": "KSERVE_IMAGE",
            "gliner": "GLINER_IMAGE",
        }[runtime]

    def image_tag_for(self, runtime: str) -> str:
        return self.environment[self.image_environment_key(runtime)]

    def image_matches(self, runtime: str, image: str) -> bool:
        return image.removeprefix("docker.io/") == self.image_tag_for(runtime).removeprefix(
            "docker.io/"
        )

    def service_container_image_id(self, service: str) -> str:
        container = self.run("ps", "-q", service, capture=True, text=True).stdout.strip()
        if not container:
            raise ValueError(f"Service has no container: {service}")
        return subprocess.run(
            self.docker_command + ["inspect", "--format", "{{.Image}}", container],
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def tag_image(self, image: str, tag: str) -> None:
        subprocess.run(
            self.docker_command + ["image", "tag", image, tag],
            env=self.environment,
            check=True,
        )

    def run_with_image(self, runtime: str, image: str, *args: str) -> None:
        subprocess.run(
            self.command + list(args),
            env={**self.environment, self.image_environment_key(runtime): image},
            check=True,
        )

    def runtime_settings_match(self, service: str) -> bool:
        """Do not skip application when a saved NER/Docling setting changed."""
        if service in {"ner-german-gliner", "ner-english-gliner", "ner-multilingual-gliner"}:
            values = json.loads(
                subprocess.run(
                    self.docker_command
                    + ["inspect", f"local-ai-{service}-1", "--format", "{{json .Config.Env}}"],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            environment = dict(value.split("=", 1) for value in values)
            alias = service.removesuffix("-gliner").replace("-", "_")
            return bool(
                environment.get("GLINER_DTYPE") == getattr(self.settings, alias + "_dtype")
                and environment.get("GLINER_THRESHOLD") == self.environment["GLINER_THRESHOLD"]
                and environment.get("GLINER_LABELS") == self.environment["GLINER_LABELS"]
            )
        if service not in {"ner-german", "ner-english", "vlm-documents"}:
            return True
        command = json.loads(
            subprocess.run(
                self.docker_command
                + ["inspect", f"local-ai-{service}-1", "--format", "{{json .Config.Cmd}}"],
                env=self.environment,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        if service.startswith("ner-"):
            dtype = getattr(self.settings, service.replace("-", "_") + "_dtype")
            return f"--dtype={dtype}" in command
        try:
            value = command[command.index("--gpu-memory-utilization") + 1]
            return float(value) == self.settings.vllm_granite_gpu_memory_utilization
        except (ValueError, IndexError, TypeError):
            return False

    def active_identity(self, alias: str, service: str) -> tuple[str, str] | None:
        """Read the active model link from an already running service, without a setup container."""
        link = self.run(
            "exec", "-T", service, "readlink", f"/models/active/{alias}", capture=True, text=True
        ).stdout.strip()
        parts = PurePosixPath(link).parts
        if len(parts) != 7 or parts[:3] != ("/", "models", "library"):
            return None
        return parts[3], parts[5]

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

    def install(self, settings: Any, names: list[str] | None = None) -> None:
        """Stream workstation images and remove containers from superseded definitions."""
        from inference_runtime_manager.installer.images import stage_bundle

        stage_bundle(self, settings, names)
        self.worker({"action": "configure"})
        self.run("up", "--pull", "never", "--remove-orphans", "--no-start", "setup")
        self.run("rm", "-f", "setup")
