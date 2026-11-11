"""Profile-scoped multi-model routing with immutable model bundle references."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from inference_runtime_manager.configuration import state_root
from inference_runtime_manager.downloader.config import Settings
from inference_runtime_manager.downloader.ollama_archive import (
    ARCHIVE_RECIPES,
    prepare_archived_bundle,
)
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.images import install_local_images, selected_specs
from inference_runtime_manager.installer.upload import prepare_artifact, transfer

MAX_TEMPLATE_BYTES = 1024 * 1024


class RouterDocker(Protocol):
    docker_command: list[str]
    environment: dict[str, str]

    def worker(self, payload: dict[str, Any]) -> dict[str, Any]: ...


# Runtime filenames are reviewed alongside the pinned model catalog.
ROUTER_RECIPES = {
    ("mistral-small32-24b", "gguf-q4-k-m"): (
        "Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf",
        "mmproj-F16.gguf",
    ),
    ("qwen38-27b", "gguf-ud-q6-k"): ("Qwen3.8-27B-UD-Q6_K.gguf", None),
    ("qwen38-27b", "gguf-ud-q6-k-vision"): ("Qwen3.8-27B-UD-Q6_K.gguf", "mmproj-F16.gguf"),
    ("qwen38-27b-turbo-fable-cold-fusion-neo-coder-max-mtp", "gguf-mtp-q6-k-vision"): (
        "Qwen3.8-27B-TurboFCFusion-735-882-Here-Uncen-NEO-CODER-MAX-MTP-Q6_K.gguf",
        "mmproj-F16.gguf",
    ),
    ("gemma3-4b-it", "gguf-q4-k-m"): ("gemma-3-4b-it-Q4_K_M.gguf", "mmproj-model-f16.gguf"),
    ("qwen36-35b-a3b", "gguf-ud-q4-k-m"): ("Qwen3.6-35B-A3B-UD-Q4_K_M.gguf", "mmproj-F16.gguf"),
    **{
        (model, variant): ("sha256-" + digest, None)
        for model, variant, digest in ARCHIVE_RECIPES.values()
    },
}


class RouterModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str
    variant_id: str
    serving_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")
    context_tokens: int = Field(ge=256, le=1048576)
    cache_type: Literal["f16", "q8_0", "q4_0"] = "q8_0"
    template: Path | None = None
    archive_model: Literal["deepseek-r1/32b", "deepseek-r1/70b", "llama3/latest"] | None = None

    @model_validator(mode="after")
    def reviewed_recipe(self) -> RouterModel:
        if (self.model_id, self.variant_id) not in ROUTER_RECIPES:
            raise ValueError("No reviewed router recipe for the selected model")
        archive_identity = next(
            (
                name
                for name, recipe in ARCHIVE_RECIPES.items()
                if recipe[:2] == (self.model_id, self.variant_id)
            ),
            None,
        )
        if self.archive_model != archive_identity:
            raise ValueError("Archive selection does not match the reviewed model/variant")
        return self


class RouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1)
    bind_address: str
    port: int = Field(default=11434, ge=1024, le=65535)
    max_loaded: int = Field(default=1, ge=1, le=8)
    models: list[RouterModel] = Field(min_length=1, max_length=32)
    archive_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]*$")

    @field_validator("bind_address")
    @classmethod
    def explicit_address(cls, value: str) -> str:
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Router requires an explicit unicast IPv4 bind address")
        return str(address)

    @model_validator(mode="after")
    def unique_models(self) -> RouterConfig:
        if any(model.archive_model for model in self.models) and self.archive_profile is None:
            raise ValueError("Archived models require an explicit archive profile")
        identities = [(m.model_id, m.variant_id) for m in self.models]
        aliases = [m.serving_id for m in self.models]
        if len(set(identities)) != len(identities) or len(set(aliases)) != len(aliases):
            raise ValueError("Router models and serving IDs must be unique")
        # Preset names are internal catalog IDs; aliases must not collide with them.
        if set(aliases) & {m.model_id for m in self.models}:
            raise ValueError("Serving IDs must differ from internal preset names")
        return self


def load_router(target: str) -> RouterConfig:
    """Load only the selected profile and reject a different deployment target."""
    config = RouterConfig.model_validate_json((state_root() / "router.json").read_text())
    if config.target != target:
        raise ValueError("Saved router belongs to a different Docker context")
    return config


def save_router(config: RouterConfig) -> None:
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "router.json.tmp"
    temporary.write_text(config.model_dump_json(indent=2) + "\n")
    temporary.replace(root / "router.json")


def read_template(path: Path) -> str:
    """Read an explicitly chosen private template without following symlinks."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Template path must be absolute without traversal")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("Template path must not contain symlinks")
    with path.open("rb") as stream:
        content = stream.read(MAX_TEMPLATE_BYTES + 1)
    if not content or len(content) > MAX_TEMPLATE_BYTES:
        raise ValueError("Template must be nonempty and at most 1 MiB")
    return content.decode("utf-8")


def prepare_router(
    config: RouterConfig, target: str, model_root: Path, *, verify: bool = True
) -> tuple[dict[str, str], list[tuple[Path, dict[str, Any]]]]:
    """Validate local bundle identities and render config, optionally hashing contents."""
    if config.target != target:
        raise ValueError("Saved router belongs to a different Docker context")
    artifacts = []
    files: dict[str, str] = {}
    lines = [
        "version = 1",
        "",
        "[*]",
        "parallel = 1",
        "jinja = true",
        "offline = true",
        "cache-prompt = true",
        "n-gpu-layers = 999",
        "",
    ]
    for index, model in enumerate(config.models):
        if model.archive_model is not None:
            assert config.archive_profile is not None
            source, manifest = prepare_archived_bundle(
                model_root, config.archive_profile, model.archive_model, verify=verify
            )
        else:
            source, manifest = prepare_artifact(
                model_root, model.model_id, model.variant_id, verify=verify
            )
        weight, projector = ROUTER_RECIPES[(model.model_id, model.variant_id)]
        required = {weight} | ({projector} if projector else set())
        if not required <= {record["path"] for record in manifest["files"]}:
            raise ValueError("Router bundle is missing required model/projector files")
        directory = "/models/" + manifest["destination"]
        lines.extend(
            [
                f"[{model.model_id}]",
                f"alias = {model.serving_id}",
                f"model = {directory}/{weight}",
                f"ctx-size = {model.context_tokens}",
                f"cache-type-k = {model.cache_type}",
                f"cache-type-v = {model.cache_type}",
                "flash-attn = on",
                f"load-on-startup = {'true' if index == 0 else 'false'}",
            ]
        )
        if projector:
            lines.append(f"mmproj = {directory}/{projector}")
        if model.template is not None:
            filename = f"{model.model_id}.jinja"
            files[filename] = read_template(model.template)
            lines.append(f"chat-template-file = /runtime-config/router/current/{filename}")
        lines.append("")
        artifacts.append((source, manifest))
    files["models.ini"] = "\n".join(lines)
    return files, artifacts


def config_identity(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def publish_config(docker: RouterDocker, files: dict[str, str]) -> str:
    """Stage an immutable configuration without changing the active router."""
    result = docker.worker({"action": "router_stage", "files": files})
    version = config_identity(files)
    if result.get("version") != version:
        raise ValueError("Remote router configuration identity differs")
    return version


def activate_router(
    docker: RouterDocker,
    version: str,
    previous_container: str | None,
    start: Callable[[], None],
    accept: Callable[[], None],
    stop_candidate: Callable[[], None],
) -> None:
    """Retain the prior container and restore its config on any failed acceptance."""
    previous_version = docker.worker({"action": "router_current"})["version"]
    stopped = False
    try:
        if previous_container:
            subprocess.run(
                docker.docker_command + ["stop", previous_container],
                env=docker.environment,
                check=True,
            )
            stopped = True
        docker.worker({"action": "router_select", "version": version})
        start()
        accept()
    except Exception as failure:
        errors = []
        for operation in [
            stop_candidate,
            lambda: docker.worker({"action": "router_select", "version": previous_version}),
        ]:
            try:
                operation()
            except Exception as error:
                errors.append(type(error).__name__)
        if stopped and previous_container:
            try:
                subprocess.run(
                    docker.docker_command + ["start", previous_container],
                    env=docker.environment,
                    check=True,
                )
            except Exception as error:
                errors.append(type(error).__name__)
        if errors:
            raise RuntimeError(
                "Router deployment failed and rollback needs attention: " + ", ".join(errors)
            ) from failure
        raise


def applied_router(target: str) -> dict[str, str] | None:
    path = state_root() / "router-applied.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key]
        for key in ("target", "container", "version", "project")
    ):
        raise ValueError("Invalid applied router state")
    if data.get("target") != target:
        raise ValueError("Applied router belongs to a different Docker context")
    if not isinstance(data.get("container"), str) or not data["container"]:
        raise ValueError("Applied router container is missing")
    return {key: str(data[key]) for key in ("target", "container", "version", "project")}


def accept_router(docker: Docker, container: str, config: RouterConfig) -> None:
    """Exercise exact aliases inside the target, including switching back to the first."""
    choices = config.models + (config.models[:1] if len(config.models) > 1 else [])
    for model in choices:
        payload = {
            "model": model.serving_id,
            "messages": [{"role": "user", "content": "Reply with exactly READY."}],
            "max_tokens": 1024
            if model.archive_model in {"deepseek-r1/32b", "deepseek-r1/70b"}
            else 128,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        result = subprocess.run(
            docker.docker_command
            + [
                "exec",
                "-i",
                container,
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "300",
                "-H",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
                "http://127.0.0.1:8000/v1/chat/completions",
            ],
            env=docker.environment,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
            timeout=310,
        )
        response = json.loads(result.stdout)
        content = response["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Router acceptance returned no text for {model.serving_id}")
        print(f"Router acceptance passed: {model.serving_id}")


def apply_router(legacy_container: str | None = None) -> None:
    """Verify and adopt a router with a retained previous-container rollback path."""
    deployment = DeploymentSettings()
    config = load_router(deployment.docker_context)
    previous = applied_router(config.target)
    if previous and legacy_container:
        raise ValueError("Router is already managed; omit the legacy container")
    if legacy_container and (legacy_container.startswith("-") or "/" in legacy_container):
        raise ValueError("Invalid legacy container name")
    settings = Settings()
    files, artifacts = prepare_router(
        config, deployment.docker_context, settings.storage_root, verify=False
    )
    docker = Docker(deployment)
    install_local_images(docker, settings, ["setup", "llamacpp"])
    for model, (source, manifest) in zip(config.models, artifacts, strict=True):
        if not docker.worker({"action": "present", "manifest": manifest})["present"]:
            if model.archive_model is not None:
                assert config.archive_profile is not None
                verified = prepare_archived_bundle(
                    settings.storage_root, config.archive_profile, model.archive_model
                )
            else:
                verified = prepare_artifact(settings.storage_root, model.model_id, model.variant_id)
            if verified != (source, manifest):
                raise ValueError("Local model bundle identity changed during preparation")
            transfer(docker, source, manifest)
        # Receipts only decide whether to upload; always hash the serving bytes before switching.
        if not docker.worker({"action": "inspect", "manifest": manifest})["present"]:
            raise ValueError("Remote model bundle did not pass full verification")
    version = publish_config(docker, files)
    image = selected_specs(["llamacpp"])[0].tag
    generation = hashlib.sha256((config.model_dump_json() + version + image).encode()).hexdigest()[
        :16
    ]
    project = "local-ai-router-" + generation
    container = project + "-chat-router-1"
    previous_container = previous["container"] if previous else legacy_container
    docker.environment.update(
        ROUTER_BIND_ADDRESS=config.bind_address,
        ROUTER_PORT=str(config.port),
        ROUTER_MAX_LOADED=str(config.max_loaded),
    )
    command = list(docker.command)
    command[command.index("--project-name") + 1] = project
    if previous_container == container:
        if docker.worker({"action": "router_current"})["version"] != version:
            raise ValueError("Active router configuration drifted; restore it before applying")
        accept_router(docker, container, config)
        print("Router configuration is already applied.")
        return
    if previous_container:
        info = json.loads(
            subprocess.run(
                docker.docker_command + ["inspect", previous_container],
                env=docker.environment,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )[0]
        if not info["State"]["Running"]:
            raise ValueError("Previous router must be running before adoption")
        bindings = info["HostConfig"].get("PortBindings") or {}
        if not any(
            str(p.get("HostPort")) == str(config.port)
            for ports in bindings.values()
            for p in ports or []
        ):
            raise ValueError("Previous container does not publish the configured router port")

    def start() -> None:
        subprocess.run(
            command
            + ["up", "-d", "--pull", "never", "--wait", "--wait-timeout", "300", "chat-router"],
            env=docker.environment,
            check=True,
        )

    def stop() -> None:
        subprocess.run(command + ["stop", "chat-router"], env=docker.environment, check=True)

    def accept() -> None:
        accept_router(docker, container, config)
        record = {
            "target": config.target,
            "container": container,
            "version": version,
            "project": project,
        }
        path = state_root() / "router-applied.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n")
        temporary.replace(path)

    activate_router(docker, version, previous_container, start, accept, stop)
    print("Router adopted successfully; previous container retained stopped for rollback.")


def add_catalogue_model(target: str) -> None:
    """Save one reviewed model choice without changing a live deployment."""
    import questionary

    from inference_runtime_manager.configuration import active_profile
    from inference_runtime_manager.downloader.catalog import load_catalog
    from inference_runtime_manager.downloader.hierarchy import browse

    config = load_router(target)
    catalog = load_catalog()
    configured = {(model.model_id, model.variant_id) for model in config.models}
    available = {
        (model.id, variant.id)
        for model in catalog.models
        for variant in model.variants
        if (model.id, variant.id) in ROUTER_RECIPES and (model.id, variant.id) not in configured
    }
    if not available:
        print("All reviewed catalogue router models are already configured.")
        return
    selected = browse(catalog, available)
    if not selected:
        return
    model_id, variant_id = next(iter(selected))
    if (model_id, variant_id) not in available:
        raise ValueError("Selected model is not an available router recipe")
    model = catalog.model(model_id)
    variant = model.variant(variant_id)
    alias = questionary.text(
        "Exact serving alias used by clients",
        default=variant.archive_model.replace("/", ":")
        if variant.archive_model
        else model.id + ":local",
    ).ask()
    if alias is None:
        return
    context = questionary.text(
        "Context tokens (must fit target memory)",
        default=str(min(model.metadata.context_length or 32768, 32768)),
    ).ask()
    if context is None:
        return
    archive_profile = config.archive_profile
    if variant.archive_model:
        print(
            "Archive-backed model: original blobs are verified; no Hugging Face download is used."
        )
        if archive_profile is None:
            archive_profile = questionary.text(
                "Retained archive profile", default=active_profile()
            ).ask()
            if archive_profile is None:
                return
    choice = RouterModel(
        model_id=model_id,
        variant_id=variant_id,
        serving_id=alias,
        context_tokens=int(context),
        archive_model=variant.archive_model,
    )
    if model.metadata.context_length and choice.context_tokens > model.metadata.context_length:
        raise ValueError("Context exceeds the model's declared limit")
    draft = config.model_dump()
    draft["models"].append(choice.model_dump())
    draft["archive_profile"] = archive_profile
    save_router(RouterConfig.model_validate(draft))
    print("Router model saved. Choose Apply router configuration to deploy and verify it.")


def menu() -> None:
    """Manage a multi-model endpoint independently of single-model slots."""
    import questionary

    from inference_runtime_manager.installer.images import prepare_local_images

    while True:
        action = questionary.select(
            "Multi-model router",
            choices=[
                questionary.Choice("Show configuration and container status", value="status"),
                questionary.Choice("Add catalogue model", value="add-model"),
                questionary.Choice("Import router configuration JSON", value="configure"),
                questionary.Choice("Prepare router images on workstation", value="images"),
                questionary.Choice("Apply router configuration", value="apply"),
                questionary.Choice("Adopt an existing router container", value="adopt"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action in {None, "back"}:
            return
        deployment = DeploymentSettings()
        if action == "add-model":
            add_catalogue_model(deployment.docker_context)
        elif action == "configure":
            filename = questionary.path("Router JSON file").ask()
            if filename:
                config = RouterConfig.model_validate_json(Path(filename).expanduser().read_text())
                if config.target != deployment.docker_context:
                    raise ValueError("Router configuration belongs to a different Docker context")
                save_router(config)
        elif action == "images":
            prepare_local_images(Settings(), ["setup", "llamacpp"])
        elif action in {"apply", "adopt"}:
            legacy = (
                questionary.text("Existing router container name").ask()
                if action == "adopt"
                else None
            )
            if action == "adopt" and not legacy:
                continue
            apply_router(legacy)
        else:
            config = load_router(deployment.docker_context)
            print(f"Target: {config.target}; endpoint: {config.bind_address}:{config.port}")
            for model in config.models:
                print(f"{model.serving_id}: {model.model_id}/{model.variant_id}")
            applied = applied_router(config.target)
            if applied:
                docker = Docker(deployment)
                subprocess.run(
                    docker.docker_command
                    + ["ps", "--all", "--filter", "name=^/" + applied["container"] + "$"],
                    env=docker.environment,
                    check=True,
                )
            else:
                print("Router has not been adopted yet.")
