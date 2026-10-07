"""One workstation menu for catalog, downloads, remote files and alias assignments."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console
from rich.table import Table
from rich.text import Text

from inference_runtime_manager.configuration import (
    RuntimeConfig,
    WorkstationConfig,
    config_path,
    load_workstation_config,
    management_state,
    save_workstation_config,
)
from inference_runtime_manager.configuration import state_root as workstation_state_root
from inference_runtime_manager.downloader.catalog import (
    load_catalog,
    load_installed,
    load_queue,
    write_queue,
)
from inference_runtime_manager.downloader.config import Settings, validate_storage
from inference_runtime_manager.downloader.errors import StorageUnavailableError
from inference_runtime_manager.downloader.main import _confirm_gated, download_selections
from inference_runtime_manager.downloader.models import DownloadQueue, Selection
from inference_runtime_manager.downloader.presentation import (
    CATEGORY_NAMES,
    format_bytes,
    show_queue,
    show_variants,
)
from inference_runtime_manager.downloader.selection import (
    partition_queue,
    review_obsolete_queue,
)
from inference_runtime_manager.installer.assignments import (
    ALIAS_CATEGORIES,
    Assignment,
    compose_services,
    deployment_services,
    load_deployment,
    recipe,
    save_deployment,
)
from inference_runtime_manager.installer.docker import (
    DeploymentSettings,
    Docker,
    service_recipes,
)
from inference_runtime_manager.installer.images import (
    image_names_for_runtime,
    local_images_ready,
    prepare_local_images,
)
from inference_runtime_manager.installer.upload import (
    prepare_artifact,
    prepare_recipe,
    provision,
    transfer,
)


def table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    view = Table(title=Text(title), show_lines=False)
    for column in columns:
        view.add_column(column)
    for row in rows:
        view.add_row(*(Text(cell) for cell in row))
    Console().print(view)


def storage() -> Path:
    settings = Settings()
    validate_storage(settings)
    return settings.storage_root


def state() -> Path:
    settings = Settings()
    return management_state(settings.storage_root)


CATEGORY_GUIDES = {
    "image-ocr": (
        "Image OCR",
        "Reads text from images for private attachment inspection.",
        "vlm-images",
    ),
    "document-parsing": (
        "Structured document parsing",
        "Extracts document text, tables and layout as structured DocTags.",
        "vlm-documents",
    ),
    "asr": ("Speech recognition", "Converts spoken audio into text.", "stt-general"),
    "tts": ("Text-to-speech", "Generates spoken audio from text.", "tts-german"),
    "vad": (
        "Voice activity detection",
        "Finds the parts of an audio recording that contain speech.",
        "vad-general",
    ),
    "llm": (
        "Language models",
        "Generate text and answer text-based questions.",
        "llm-general",
    ),
    "vlm": (
        "General vision",
        "Interprets image content and answers questions about it.",
        "vlm-general",
    ),
    "ner": (
        "German named-entity recognition",
        "Detects potentially identifying information in German text on the GPU.",
        "ner-german",
    ),
    "ner-english": (
        "English named-entity recognition",
        "Detects potentially identifying information in English text on the GPU.",
        "ner-english",
    ),
    "ner-multilingual": (
        "Multilingual named-entity recognition (GLiNER)",
        "Detects PII and PHI spans in multiple languages using one GPU model.",
        "ner-multilingual",
    ),
    "image-generation": (
        "Image generation",
        "Creates or edits images from instructions; no runtime is configured yet.",
        "image-generation-general",
    ),
}

RUNTIME_NAMES = {
    "vllm": "vLLM",
    "llama.cpp": "llama.cpp",
    "speaches": "Speaches",
    "kokoro": "Kokoro ONNX",
    "chatterbox": "Chatterbox",
    "kserve": "KServe",
    "gliner": "GLiNER",
}

TARGET_CONTEXT_PLACEHOLDER = "ai-server"
STORAGE_ROOT_PLACEHOLDER = "/Volumes/ExternalDrive/inference-runtime-manager"


def runtime_family(runtime: str) -> str:
    if runtime.startswith("vllm"):
        return "vllm"
    if runtime.startswith("llama.cpp"):
        return "llama.cpp"
    if runtime == "kokoro-onnx":
        return "kokoro"
    return runtime


def configured_storage(settings: Settings) -> bool:
    try:
        validate_storage(settings)
    except (OSError, ValueError, StorageUnavailableError):
        return False
    return True


def configured_environment() -> tuple[Settings, DeploymentSettings] | None:
    """Open first-run configuration when required workstation choices are missing."""
    try:
        return Settings(), DeploymentSettings()
    except ValueError:
        print("Complete the workstation configuration before continuing.")
        configuration_menu()
    try:
        return Settings(), DeploymentSettings()
    except ValueError:
        print("Configuration is incomplete; returning to the main menu.")
        return None


def add_to_download_queue(state_root: Path, selection: Selection) -> None:
    catalog = load_catalog()
    current, obsolete = partition_queue(catalog, load_queue(state_root / "download.yaml"))
    if obsolete:
        reviewed = review_obsolete_queue(catalog, load_queue(state_root / "download.yaml"), print)
        if reviewed is None:
            return
        current = reviewed
    selected = [
        item
        for item in current.selected
        if (item.model_id, item.variant_id) != (selection.model_id, selection.variant_id)
    ]
    selected.append(selection)
    write_queue(state_root / "download.yaml", DownloadQueue(schemaVersion=1, selected=selected))
    print(f"Added {selection.model_id}/{selection.variant_id} to the download queue.")


def select_recipe(alias: str, catalog: Any) -> dict[str, Any] | None:
    recipes = service_recipes().get(alias, [])
    families = list(dict.fromkeys(runtime_family(item["runtime"]) for item in recipes))
    family = questionary.select(
        "Runtime",
        choices=[
            questionary.Choice(RUNTIME_NAMES.get(item, item), value=item) for item in families
        ],
    ).ask()
    if family is None:
        return None
    compatible = [item for item in recipes if runtime_family(item["runtime"]) == family]
    model_ids = list(dict.fromkeys(item["model_id"] for item in compatible))
    model_id = questionary.select(
        "Model",
        choices=[
            questionary.Choice(
                catalog.model(item).display_name,
                value=item,
                description=catalog.model(item).description,
            )
            for item in model_ids
        ],
    ).ask()
    if model_id is None:
        return None
    variants = [item for item in compatible if item["model_id"] == model_id]
    if len(variants) == 1:
        return variants[0]
    variant_id = questionary.select(
        "Variant",
        choices=[
            questionary.Choice(
                item["variant_id"],
                value=item["variant_id"],
                description=catalog.model(model_id).variant(item["variant_id"]).runtime_notes,
            )
            for item in variants
        ],
    ).ask()
    return next((item for item in variants if item["variant_id"] == variant_id), None)


def show_current_category_status(
    alias: str,
    title: str,
    catalog: Any,
    settings: Settings,
    docker_settings: DeploymentSettings,
    state_root: Path,
) -> bool:
    """Show saved, workstation and live target state before changing a category."""
    deployment = load_deployment(state_root, docker_settings.docker_context)
    choice = deployment.assignments.get(alias)
    preset = recipe(alias, choice.model_id, choice.variant_id) if choice is not None else None
    model_name = (
        catalog.model(choice.model_id).display_name if choice is not None else "Not configured"
    )
    variant = choice.variant_id if choice is not None else "—"
    runtime = (
        RUNTIME_NAMES.get(runtime_family(preset["runtime"]), preset["runtime"])
        if preset is not None
        else "Not configured"
    )
    desired = "Not configured"
    if choice is not None:
        desired = "Enabled" if choice.enabled else "Disabled"
    model_files = "—"
    runtime_image = "—"
    if choice is not None and preset is not None:
        if configured_storage(settings):
            installed = load_installed(settings.storage_root / "installed.yaml")
            entry = next(
                (
                    item
                    for item in installed.installed
                    if (item.model_id, item.variant_id) == (choice.model_id, choice.variant_id)
                ),
                None,
            )
            model_files = (
                "Downloaded"
                if entry is not None and (settings.storage_root / entry.path).is_dir()
                else "Missing"
            )
        else:
            model_files = "Storage disconnected"
        runtime_image = (
            "Ready"
            if local_images_ready(settings, image_names_for_runtime(preset["runtime"]))
            else "Missing"
        )

    target_service = "Target unavailable"
    active_model = "Unknown"
    manual_test_ready = False
    try:
        docker = Docker(docker_settings)
        states = docker.service_status()
        recipes = service_recipes()[alias]
        running = [
            item for item in recipes if states.get(item["service"], ("", "", ""))[0] == "running"
        ]
        if len(running) > 1:
            target_service = "Multiple mutually exclusive services are running"
            active_model = "Ambiguous"
        elif running:
            running_preset = running[0]
            _, health, _ = states[running_preset["service"]]
            running_runtime = RUNTIME_NAMES.get(
                runtime_family(running_preset["runtime"]), running_preset["runtime"]
            )
            target_service = f"{running_preset['service']}: running / {health} ({running_runtime})"
            identity = docker.active_identity(alias, running_preset["service"])
            if identity is None:
                active_model = "Active model link unavailable"
            else:
                active_name = catalog.model(identity[0]).display_name
                active_model = f"{active_name} / {identity[1]}"
                if choice is not None and identity != (choice.model_id, choice.variant_id):
                    active_model += " (differs from saved assignment)"
            manual_test_ready = bool(
                choice is not None
                and choice.enabled
                and preset is not None
                and running_preset["service"] == preset["service"]
                and health == "healthy"
                and identity == (choice.model_id, choice.variant_id)
            )
        else:
            known = [
                f"{item['service']}: {states[item['service']][0]}"
                for item in recipes
                if item["service"] in states
            ]
            target_service = ", ".join(known) if known else "No container"
            active_model = "None running"
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    table(
        f"Current {alias} status",
        ["Component", "Current state"],
        [
            ["Saved model", model_name],
            ["Saved variant", variant],
            ["Saved runtime", runtime],
            ["Desired service state", desired],
            ["Model files", model_files],
            ["Workstation runtime image", runtime_image],
            [f"Target ({docker_settings.docker_context})", target_service],
            ["Active target model", active_model],
        ],
    )
    return manual_test_ready


def guided_models() -> None:
    environment = configured_environment()
    if environment is None:
        return
    settings, docker_settings = environment
    catalog = load_catalog()
    available_groups = {model.browse_group for model in catalog.models}
    deployable_aliases = set(service_recipes())
    group = questionary.select(
        "Which service alias would you like to configure?",
        choices=[
            questionary.Choice(
                f"{CATEGORY_GUIDES[key][2]} — {CATEGORY_GUIDES[key][0]}",
                value=key,
                description=CATEGORY_GUIDES[key][1],
                disabled=(
                    None
                    if CATEGORY_GUIDES[key][2] in deployable_aliases
                    else "No deployable runtime is configured"
                ),
            )
            for key in CATEGORY_GUIDES
            if key in available_groups or (key.startswith("ner-") and "ner" in available_groups)
        ],
    ).ask()
    if group is None:
        return
    title, description, alias = CATEGORY_GUIDES[group]
    print(f"\n{title}: {description}")
    if alias not in deployable_aliases:
        print("This category is catalog-only; no deployable runtime is configured.")
        return
    local_state_root = workstation_state_root()
    status_state_root = (
        local_state_root
        if (local_state_root / "deployment.json").is_file()
        else settings.storage_root
        if (settings.storage_root / "deployment.json").is_file()
        else local_state_root
    )
    manual_test_ready = show_current_category_status(
        alias, title, catalog, settings, docker_settings, status_state_root
    )
    action = questionary.select(
        "What would you like to do?",
        choices=[
            questionary.Choice("Change model or runtime", value="change"),
            questionary.Choice(
                "Run manual endpoint test",
                value="test",
                disabled=None if manual_test_ready else "No matching healthy service is running",
            ),
            questionary.Choice("Back", value="back"),
        ],
    ).ask()
    if action == "test":
        from inference_runtime_manager.installer.api_tests import test_individual

        test_individual(alias)
        return
    if action != "change":
        return
    state_root = management_state(settings.storage_root)
    preset = select_recipe(alias, catalog)
    if preset is None:
        return
    model = catalog.model(preset["model_id"])
    variant = model.variant(preset["variant_id"])
    selection = Selection(modelId=model.id, variantId=variant.id)
    storage_ready = configured_storage(settings)
    installed = load_installed(settings.storage_root / "installed.yaml") if storage_ready else None
    local_model = bool(
        installed
        and any(
            (entry.model_id, entry.variant_id) == (model.id, variant.id)
            and (settings.storage_root / entry.path).is_dir()
            for entry in installed.installed
        )
    )
    runtime_images = image_names_for_runtime(preset["runtime"])
    runtime_ready = local_images_ready(settings, runtime_images)
    table(
        f"{title} selection",
        ["Component", "Selection", "Status"],
        [
            [
                "Runtime",
                RUNTIME_NAMES.get(runtime_family(preset["runtime"]), preset["runtime"]),
                "Ready" if runtime_ready else "Missing",
            ],
            ["Model", model.display_name, "Downloaded" if local_model else "Missing"],
            ["Variant", variant.id, format_bytes(variant.estimated_download_bytes)],
            [
                "Staging storage",
                str(settings.storage_root),
                "Available" if storage_ready else "Disconnected",
            ],
            ["Target", docker_settings.docker_context, alias],
        ],
    )
    if not local_model:
        action = questionary.select(
            "Model files are not available locally",
            choices=[
                questionary.Choice(
                    "Download now",
                    value="download",
                    disabled=None if storage_ready else "Staging storage is disconnected",
                ),
                questionary.Choice("Add to download queue for later", value="queue"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action == "queue":
            add_to_download_queue(state_root, selection)
            return
        if action != "download":
            return
        download_selections([selection])
        local_model = True
    if not runtime_ready:
        if not questionary.confirm(
            f"Prepare the required {RUNTIME_NAMES.get(runtime_family(preset['runtime']), preset['runtime'])} runtime on this workstation?",
            default=True,
        ).ask():
            print("Model retained for later; runtime was not prepared.")
            return
        prepare_local_images(settings, runtime_images)
    if not questionary.confirm(
        f"Install the runtime and activate {model.display_name} on {docker_settings.docker_context}?",
        default=False,
    ).ask():
        print("Model and runtime retained for later activation.")
        return
    docker = Docker(docker_settings)
    deployment = load_deployment(state_root, docker_settings.docker_context)
    previous = deployment.model_copy(deep=True)
    deployment.assignments[alias] = Assignment(
        model_id=model.id,
        variant_id=variant.id,
        enabled=True,
        runtime=preset["runtime"],
    )
    try:
        prepare_recipe(settings.storage_root, alias, preset)
        save_deployment(state_root, deployment)
        docker.install(settings, runtime_images)
        provision(docker, settings.storage_root, state_root, [alias])
    except BaseException:
        save_deployment(state_root, previous)
        raise
    print(
        f"{title} is ready: {model.display_name} on {RUNTIME_NAMES.get(runtime_family(preset['runtime']), preset['runtime'])}."
    )
    if questionary.confirm("Run the manual endpoint test now?", default=True).ask():
        from inference_runtime_manager.installer.api_tests import test_individual

        test_individual(alias)


def runtime_updates() -> None:
    environment = configured_environment()
    if environment is None:
        return
    settings, docker_settings = environment
    recipes = [item for values in service_recipes().values() for item in values]
    families = list(dict.fromkeys(runtime_family(item["runtime"]) for item in recipes))
    family = questionary.select(
        "Runtime to prepare or update",
        choices=[
            questionary.Choice(RUNTIME_NAMES.get(item, item), value=item) for item in families
        ],
    ).ask()
    if family is None:
        return
    matching = [item for item in recipes if runtime_family(item["runtime"]) == family]
    names = list(
        dict.fromkeys(
            name for item in matching for name in image_names_for_runtime(item["runtime"])
        )
    )
    if not questionary.confirm(
        f"Prepare {RUNTIME_NAMES.get(family, family)} images on this workstation?", default=True
    ).ask():
        return
    prepare_local_images(settings, names)
    state_root = management_state(settings.storage_root)
    deployment = load_deployment(state_root, docker_settings.docker_context)
    affected = [
        (alias, recipe(alias, choice.model_id, choice.variant_id))
        for alias, choice in deployment.assignments.items()
        if choice.enabled
        and runtime_family(recipe(alias, choice.model_id, choice.variant_id)["runtime"]) == family
    ]
    if not questionary.confirm(
        f"Stream these images to offline target {docker_settings.docker_context}?", default=False
    ).ask():
        return
    docker = Docker(docker_settings)
    docker.install(settings, names)
    if not affected:
        print("Runtime images are installed; no enabled saved assignment uses this runtime.")
        return
    if not questionary.confirm(
        f"Recreate {len(affected)} enabled service(s) with the updated runtime now?",
        default=False,
    ).ask():
        print("Runtime images are installed. Existing services remain unchanged.")
        return
    states = docker.service_status()
    recreate = []
    for alias, preset in affected:
        state_name = states.get(preset["service"], ("", "", ""))[0]
        if state_name != "running":
            print(f"{alias}: assigned service is not running; skipped.")
            continue
        if docker.active_identity(alias, preset["service"]) != (
            preset["model_id"],
            preset["variant_id"],
        ):
            raise ValueError(f"{alias}: active model does not match the saved assignment")
        tag = docker.image_tag_for(preset["runtime"])
        recreate.append(
            (
                alias,
                preset,
                docker.service_container_image_id(preset["service"]),
                tag,
            )
        )
    updated = []
    current = None
    try:
        for current in recreate:
            alias, preset, _, _ = current
            docker.run(
                "up",
                "--pull",
                "never",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "300",
                preset["service"],
            )
            updated.append(current)
            current = None
            print(f"{alias}: runtime updated and service is healthy.")
    except (OSError, subprocess.SubprocessError, ValueError, KeyboardInterrupt) as exc:
        rollback = ([current] if current is not None else []) + list(reversed(updated))
        try:
            for alias, preset, previous_image, tag in rollback:
                repository = tag.rsplit(":", 1)[0]
                rollback_tag = (
                    f"{repository}:rollback-{previous_image.removeprefix('sha256:')[:16]}"
                )
                docker.tag_image(previous_image, rollback_tag)
                docker.run_with_image(
                    preset["runtime"],
                    rollback_tag,
                    "up",
                    "--pull",
                    "never",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "300",
                    preset["service"],
                )
                print(f"{alias}: previous runtime restored.")
        except (OSError, subprocess.SubprocessError, ValueError) as rollback_exc:
            raise RuntimeError("Runtime update and rollback failed") from rollback_exc
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise RuntimeError("Runtime update failed; previous services restored") from exc


def configuration_menu() -> None:
    while True:
        configured = load_workstation_config()
        try:
            settings = Settings()
        except ValueError:
            settings = None
        try:
            deployment = DeploymentSettings()
        except ValueError:
            deployment = None
        storage_root = configured.storage_root or (
            settings.storage_root if settings is not None else None
        )
        hf_home = configured.hf_home or (settings.hf_home if settings is not None else None)
        build_context = configured.build_docker_context or (
            settings.build_docker_context if settings is not None else "desktop-linux"
        )
        target_context = configured.docker_context or (
            deployment.docker_context if deployment is not None else None
        )
        table(
            "Configuration",
            ["Setting", "Value"],
            [
                ["Target Docker context", target_context or "Not configured"],
                ["Model staging storage", str(storage_root or "Not configured")],
                ["Hugging Face home", str(hf_home or "Not configured")],
                ["Workstation Docker context", build_context],
                ["Local configuration", str(config_path())],
            ],
        )
        action = questionary.select(
            "Configuration",
            choices=[
                questionary.Choice("Select model staging storage", value="storage"),
                questionary.Choice("Set target Docker context", value="target"),
                questionary.Choice("Set workstation Docker context", value="build"),
                questionary.Choice("NER precision / Docling GPU memory", value="runtime"),
                questionary.Choice("Back", value="back"),
            ],
        ).ask()
        if action in (None, "back"):
            return
        if action == "runtime":
            runtime_configuration()
            continue
        if action == "storage":
            value = questionary.path(
                "Model staging directory:",
                default=str(storage_root or ""),
                placeholder=STORAGE_ROOT_PLACEHOLDER if storage_root is None else None,
            ).ask()
            if value is None:
                continue
            path = Path(value).expanduser()
            if not path.is_dir():
                print("Select an existing mounted directory.")
                continue
            configured.storage_root = path
            configured.hf_home = path / "huggingface"
        elif action == "target":
            value = questionary.text(
                "Target Docker context:",
                default=target_context or "",
                placeholder=TARGET_CONTEXT_PLACEHOLDER if target_context is None else None,
            ).ask()
            if not value:
                continue
            configured.docker_context = value.strip()
        elif action == "build":
            value = questionary.text("Workstation Docker context:", default=build_context).ask()
            if not value:
                continue
            configured.build_docker_context = value.strip()
        save_workstation_config(WorkstationConfig.model_validate(configured.model_dump()))
        print("Configuration saved.")


def runtime_configuration() -> None:
    configured = load_workstation_config()
    settings = DeploymentSettings()
    options = {
        "ner_german_dtype": ("German NER precision", "ner-german"),
        "ner_english_dtype": ("English NER precision", "ner-english"),
        "ner_multilingual_dtype": ("Multilingual GLiNER precision", "ner-multilingual"),
        "vllm_granite_gpu_memory_utilization": ("Docling GPU memory fraction", "vlm-documents"),
        "gliner_threshold": ("GLiNER confidence threshold", "gliner"),
        "gliner_labels": ("GLiNER entity labels", "gliner"),
    }
    table(
        "Runtime settings",
        ["Setting", "Effective value"],
        [[label, str(getattr(settings, name))] for name, (label, _) in options.items()],
    )
    print("FP16 uses less memory; verify recognition quality after changing precision.")
    print("The GPU fraction is a vLLM allocation target, not a hard memory limit.")
    name = questionary.select(
        "Change a runtime setting",
        choices=[questionary.Choice(label, value=name) for name, (label, _) in options.items()]
        + [questionary.Choice("Back", value="back")],
    ).ask()
    if name in (None, "back"):
        return
    if name.endswith("dtype"):
        value = questionary.select(
            options[name][0],
            choices=[
                questionary.Choice("FP16 — recommended after target-GPU checks", value="float16"),
                questionary.Choice("FP32 — conservative default", value="float32"),
            ],
            default=getattr(settings, name),
        ).ask()
    elif name == "gliner_labels":
        value = questionary.text(
            "GLiNER labels (comma separated, at most 25):",
            default=", ".join(settings.gliner_labels),
        ).ask()
        if value is not None:
            value = [label.strip() for label in value.split(",")]
    elif name == "gliner_threshold":
        value = questionary.text(
            "GLiNER confidence threshold (0 < value < 1):",
            default=str(settings.gliner_threshold),
        ).ask()
    else:
        value = questionary.text(
            "GPU memory fraction (0 < value <= 1; recommended starting point: 0.18):",
            default=str(getattr(settings, name)),
        ).ask()
    if value is None:
        return
    previous = configured.model_copy(deep=True)
    draft = configured.runtime.model_dump()
    draft[name] = value
    configured.runtime = RuntimeConfig.model_validate(draft)
    save_workstation_config(configured)
    print("Runtime setting saved. Only services using this setting need a restart.")
    alias = options[name][1]
    if not questionary.confirm(f"Apply {alias} now (brief downtime)?", default=False).ask():
        print("Choose Review / apply assignments later to apply the saved setting.")
        return
    root = state()
    assignments = load_deployment(root, settings.docker_context).assignments
    targets = (
        [alias]
        if alias != "gliner"
        else [
            name
            for name, assignment in assignments.items()
            if assignment.runtime == "gliner" and assignment.enabled
        ]
    )
    targets = [name for name in targets if name in assignments and assignments[name].enabled]
    if not targets:
        print("Service is not enabled; the saved setting will be used when activated.")
        return
    model_root = storage()
    try:
        provision(Docker(DeploymentSettings()), model_root, root, targets)
    except BaseException:
        save_workstation_config(previous)
        for target in targets:
            assignment = assignments[target]
            preset = recipe(target, assignment.model_id, assignment.variant_id)
            Docker(settings).run(
                "up",
                "--pull",
                "never",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "300",
                preset["service"],
            )
        print("Previous runtime setting restored.")
        raise


def select_models() -> None:
    settings = Settings()
    root = settings.storage_root
    storage_ready = configured_storage(settings)
    catalog = load_catalog()
    category = questionary.select(
        "Category",
        choices=[
            questionary.Choice(CATEGORY_NAMES.get(c, c), value=c)
            for c in sorted({m.browse_group for m in catalog.models})
        ],
    ).ask()
    if category is None:
        return
    models = [model for model in catalog.models if model.browse_group == category]
    installed = (
        {(i.model_id, i.variant_id): i for i in load_installed(root / "installed.yaml").installed}
        if storage_ready
        else {}
    )
    queue_path = management_state(root) / "download.yaml"
    queue = load_queue(queue_path)
    queue = review_obsolete_queue(catalog, queue, print)
    if queue is None:
        return
    selected = {(s.model_id, s.variant_id) for s in queue.selected}
    table(
        CATEGORY_NAMES.get(category, category),
        ["#", "Model", "Variants", "Selected", "Local files"],
        [
            [
                str(i),
                model.display_name,
                str(len(model.variants)),
                ", ".join(
                    variant.id for variant in model.variants if (model.id, variant.id) in selected
                )
                or "—",
                (
                    f"{sum(1 for variant in model.variants if (model.id, variant.id) in installed and (root / installed[model.id, variant.id].path).is_dir())}/{len(model.variants)} downloaded"
                    if storage_ready
                    else "Storage disconnected"
                ),
            ]
            for i, model in enumerate(models, 1)
        ],
    )
    action = questionary.select(
        "Catalog", choices=["Select downloads", "Model details", "Back"]
    ).ask()
    if action == "Model details":
        model = questionary.select(
            "Model details",
            choices=[
                questionary.Choice(m.display_name, value=m)
                for m in catalog.models
                if m.browse_group == category
            ],
        ).ask()
        if model is not None:
            show_variants(model, print)
        return
    if action != "Select downloads":
        return
    chosen = questionary.checkbox(
        "Select models (Space toggles, Enter chooses variants)",
        choices=[
            questionary.Choice(
                f"{i:>2}  {model.display_name}",
                value=model.id,
                checked=any((model.id, variant.id) in selected for variant in model.variants),
                description=model.description,
            )
            for i, model in enumerate(models, 1)
        ],
    ).ask()
    if chosen is None:
        return
    kept = [s for s in queue.selected if catalog.model(s.model_id).browse_group != category]
    category_selections = []
    for model in models:
        if model.id not in chosen:
            continue
        if len(model.variants) == 1:
            variant_ids = [model.variants[0].id]
        else:
            show_variants(model, print)
            variant_ids = questionary.checkbox(
                f"{model.display_name} — select variant(s)",
                choices=[
                    questionary.Choice(
                        f"{variant.id} [{', '.join(variant.runtimes) or 'download only'}] — {format_bytes(variant.estimated_download_bytes)}",
                        value=variant.id,
                        checked=(model.id, variant.id) in selected,
                        description=variant.compatibility_notes,
                    )
                    for variant in model.variants
                ],
                instruction="Arrows, Space to select, Enter to continue; Ctrl+C cancels.",
            ).ask()
            if variant_ids is None:
                return
            if not variant_ids:
                print(f"Select at least one variant for {model.display_name}; queue unchanged.")
                return
        category_selections.extend(
            Selection(modelId=model.id, variantId=variant_id) for variant_id in variant_ids
        )
    draft = DownloadQueue(schemaVersion=1, selected=kept + category_selections)
    show_queue(draft, catalog, print)
    if not questionary.confirm("Save this download queue?", default=False).ask():
        print("Download queue unchanged.")
        return
    _confirm_gated(catalog, draft.selected)
    write_queue(queue_path, draft)
    print(f"Saved {len(draft.selected)} variants. Choose Download selected models next.")


def upload_models() -> None:
    root = storage()
    entries = load_installed(root / "installed.yaml").installed
    if not entries:
        print("No downloaded models. Select and download models first.")
        return
    choices = questionary.checkbox(
        "Upload downloaded models (files only; does not enable models)",
        choices=[questionary.Choice(f"{e.model_id} / {e.variant_id}", value=e) for e in entries],
    ).ask()
    if not choices:
        return
    prepared = [prepare_artifact(root, e.model_id, e.variant_id) for e in choices]
    docker = Docker(DeploymentSettings())
    if not questionary.confirm(
        f"Upload {len(prepared)} bundles to {docker.settings.docker_context}?", default=False
    ).ask():
        return
    for source, manifest in prepared:
        transfer(docker, source, manifest)
    print("Upload complete. Assign aliases, then apply when ready.")


def assign_models() -> None:
    root = storage()
    state_root = state()
    target = DeploymentSettings().docker_context
    deployment = load_deployment(state_root, target)
    alias = questionary.select("Stable model name", choices=list(ALIAS_CATEGORIES)).ask()
    if alias is None:
        return
    catalog = load_catalog()
    candidates = []
    models = {model.id: model for model in catalog.models}
    for entry in load_installed(root / "installed.yaml").installed:
        model = models.get(entry.model_id)
        if model is None or model.category != ALIAS_CATEGORIES[alias]:
            continue
        try:
            preset = recipe(alias, entry.model_id, entry.variant_id)
        except ValueError:
            continue
        candidates.append((entry, preset["runtime"]))
    if not candidates:
        print("No downloaded model with a reviewed runtime recipe for this alias.")
        print("Unverified models can be downloaded/uploaded, but cannot be activated yet.")
        return
    selected = questionary.select(
        f"Assign {alias}",
        choices=[
            questionary.Choice(f"{e.model_id} / {e.variant_id} [{runtime}]", value=(e, runtime))
            for e, runtime in candidates
        ],
    ).ask()
    if selected is None:
        return
    entry, runtime = selected
    # Check actual publication, rather than trusting a previous upload receipt.
    _, manifest = prepare_artifact(root, entry.model_id, entry.variant_id, verify=False)
    if not Docker(DeploymentSettings()).worker({"action": "present", "manifest": manifest})[
        "present"
    ]:
        raise ValueError("Upload this model to the selected target before assigning it")
    enabled = questionary.confirm("Enable when applied?", default=True).ask()
    if enabled is None:
        return
    deployment.assignments[alias] = Assignment(
        model_id=entry.model_id,
        variant_id=entry.variant_id,
        enabled=enabled,
        runtime=runtime,
    )
    save_deployment(state_root, deployment)
    print(f"Saved {alias} locally for {target}.")
    if questionary.confirm(f"Apply {alias} to {target} now?", default=False).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(DeploymentSettings()), root, state_root, [alias])
    else:
        print("Choose Review / apply assignments when ready to update the server.")


def deployment_status(remote: bool = False, verify_remote: bool = False) -> None:
    settings = Settings()
    root = settings.storage_root
    storage_ready = configured_storage(settings)
    state_root = management_state(root)
    if verify_remote and not remote:
        raise ValueError("Remote verification requires a Docker context")
    if verify_remote and not storage_ready:
        raise ValueError("Connect the configured model staging storage before full verification")
    configured = deployment_services(state_root)
    docker = Docker(DeploymentSettings()) if remote else None
    deployment = load_deployment(state_root, docker.settings.docker_context if docker else None)
    states: dict[str, tuple[str, str, str]] = {}
    if docker:
        states = docker.service_status()
    entries = (
        {(e.model_id, e.variant_id): e for e in load_installed(root / "installed.yaml").installed}
        if storage_ready
        else {}
    )
    rows = []
    for alias in ALIAS_CATEGORIES:
        assigned = alias in deployment.assignments
        preset = configured.get(alias) if assigned else None
        key = (preset["model_id"], preset["variant_id"]) if preset else None
        local = (
            "Downloaded"
            if key in entries and (root / entries[key].path).is_dir()
            else "Storage disconnected"
            if key is not None and not storage_ready
            else "Missing"
            if key is not None
            else "—"
        )
        remote_state = "Not checked"
        if docker and verify_remote and key is not None and local == "Downloaded":
            print(f"Fully verifying {alias} locally and on {docker.settings.docker_context}...")
            started = time.monotonic()
            _, manifest = prepare_artifact(root, *key)
            remote_state = (
                "Verified files"
                if docker.worker({"action": "inspect", "manifest": manifest})["present"]
                else "Missing"
            )
            print(f"{alias}: full verification completed in {time.monotonic() - started:.1f}s")
        elif docker and verify_remote and key is not None:
            remote_state = "Local files missing"
        observed = (
            next(
                (
                    service
                    for service in compose_services(alias)
                    if states.get(service, ("", "", ""))[0] == "running"
                ),
                None,
            )
            if docker and alias in configured
            else None
        )
        active = "—"
        if observed and docker:
            try:
                identity = docker.active_identity(alias, observed)
            except (OSError, ValueError, subprocess.SubprocessError):
                identity = None
            active = (
                "/".join(identity) + (" (matches)" if identity == key else " (differs)")
                if identity is not None
                else "Unknown"
            )
        service = preset["service"] if preset else "—"
        state, health, image = states.get(observed or service, ("not created", "—", ""))
        if observed and observed != service:
            service = f"{observed} (other recipe)"
        elif docker:
            service = f"{service} ({state})"
        if preset and observed == preset["service"] and image and docker:
            if not docker.image_matches(preset["runtime"], image):
                health += "; old image"
        desired = (
            f"{'ON' if preset['enabled'] else 'OFF'} {key[0]}/{key[1]} [{preset['runtime']}]"
            if preset and key
            else "Not assigned"
        )
        files = local if not verify_remote else f"Local: {local}; remote: {remote_state}"
        rows.append(
            [
                alias,
                desired,
                active,
                f"{service} / {health}" if docker else "Not checked",
                files,
            ]
        )
    table(
        f"Deployment on {docker.settings.docker_context}" if docker else "Saved deployment plan",
        [
            "Alias",
            "Desired assignment",
            "Active model files",
            "Service / health",
            "Files",
        ],
        rows,
    )
    if docker:
        print(f"Docker context: {docker.settings.docker_context}")
        for service, (state, _, image) in states.items():
            if state == "running":
                print(f"Running image for {service}: {image or 'unknown'}")
        if (
            "tts-german" in deployment.assignments
            and configured["tts-german"]["runtime"] == "kokoro-onnx"
        ):
            print(
                "Packaged Kokoro setup: CPUExecutionProvider, 4 intra-op / 1 inter-op threads; "
                f"host {docker.settings.bind_address}:{docker.settings.tts_german_port}, "
                "24 kHz mono PCM WAV output."
            )
    print(
        "Active files and container health do not prove inference; use Test endpoints to check it."
    )


def toggle_service() -> None:
    root = storage()
    state_root = state()
    settings = DeploymentSettings()
    deployment = load_deployment(state_root, settings.docker_context)
    if not deployment.assignments:
        print("Assign uploaded models before enabling a service.")
        return
    alias = questionary.select("Service", choices=list(deployment.assignments)).ask()
    if alias is None:
        return
    choice = deployment.assignments[alias]
    enabled = questionary.confirm("Enable this service?", default=not choice.enabled).ask()
    if enabled is None or enabled == choice.enabled:
        return
    choice.enabled = enabled
    save_deployment(state_root, deployment)
    if questionary.confirm("Apply this service state now?", default=False).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(settings), root, state_root, [alias])


def warn_vram(state_root: Path) -> None:
    deployment = load_deployment(state_root)
    enabled = [
        recipe(alias, choice.model_id, choice.variant_id)
        for alias, choice in deployment.assignments.items()
        if choice.enabled
    ]
    total = sum(float(preset.get("estimated_vram_gib", 0)) for preset in enabled)
    if total > 14:
        print(
            f"Warning: enabled GPU services estimate {total:g} GiB VRAM. "
            "Docker/NVIDIA cannot reserve hard per-service VRAM limits on this GPU."
        )


def apply_assignments() -> None:
    root = storage()
    state_root = state()
    deployment = load_deployment(state_root, DeploymentSettings().docker_context)
    if not deployment.assignments:
        print(
            "Assign uploaded models first. Bundled defaults are suggestions, not saved assignments."
        )
        return
    deployment_status(remote=True)
    warn_vram(state_root)
    if questionary.confirm(
        "Apply saved assignments and recreate affected services?", default=False
    ).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(DeploymentSettings()), root, state_root, list(deployment.assignments))
        save_deployment(state_root, deployment)
