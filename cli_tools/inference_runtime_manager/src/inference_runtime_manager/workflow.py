"""One workstation menu for catalog, downloads, remote files and alias assignments."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import questionary
from rich.console import Console
from rich.table import Table
from rich.text import Text

from inference_runtime_manager.downloader.catalog import (
    load_catalog,
    load_installed,
    load_queue,
    write_queue,
)
from inference_runtime_manager.downloader.config import Settings, validate_storage
from inference_runtime_manager.downloader.main import _confirm_gated
from inference_runtime_manager.downloader.models import DownloadQueue, Selection
from inference_runtime_manager.downloader.presentation import (
    CATEGORY_NAMES,
    format_bytes,
    show_variants,
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
from inference_runtime_manager.installer.docker import DeploymentSettings, Docker
from inference_runtime_manager.installer.upload import prepare_artifact, transfer


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


def select_models() -> None:
    root = storage()
    catalog = load_catalog()
    category = questionary.select(
        "Category",
        choices=[
            questionary.Choice(CATEGORY_NAMES.get(c, c), value=c)
            for c in sorted({m.category for m in catalog.models})
        ],
    ).ask()
    if category is None:
        return
    variants = [(m, v) for m in catalog.models if m.category == category for v in m.variants]
    installed = {
        (i.model_id, i.variant_id): i for i in load_installed(root / "installed.yaml").installed
    }
    queue = load_queue(root / "download.yaml")
    selected = {(s.model_id, s.variant_id) for s in queue.selected}
    table(
        CATEGORY_NAMES.get(category, category),
        ["#", "Model", "Variant", "Runtime", "Local files", "Download"],
        [
            [
                str(i),
                m.display_name,
                v.id,
                ", ".join(v.runtimes) or "Download only",
                "Downloaded"
                if (m.id, v.id) in installed and (root / installed[m.id, v.id].path).is_dir()
                else "Queued"
                if (m.id, v.id) in selected
                else "Missing",
                format_bytes(v.estimated_download_bytes),
            ]
            for i, (m, v) in enumerate(variants, 1)
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
                if m.category == category
            ],
        ).ask()
        if model is not None:
            show_variants(model, print)
        return
    if action != "Select downloads":
        return
    chosen = questionary.checkbox(
        "Select rows (Space toggles, Enter saves this category)",
        choices=[
            questionary.Choice(
                f"{i:>2}  {m.id} / {v.id}  [{', '.join(v.runtimes) or 'download only'}]",
                value=(m.id, v.id),
                checked=(m.id, v.id) in selected,
                description=v.compatibility_notes,
            )
            for i, (m, v) in enumerate(variants, 1)
        ],
    ).ask()
    if chosen is None:
        return
    kept = [s for s in queue.selected if catalog.model(s.model_id).category != category]
    draft = DownloadQueue(
        schemaVersion=1, selected=kept + [Selection(modelId=m, variantId=v) for m, v in chosen]
    )
    _confirm_gated(catalog, draft.selected)
    write_queue(root / "download.yaml", draft)
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
    target = DeploymentSettings().docker_context
    deployment = load_deployment(root, target)
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
    save_deployment(root, deployment)
    print(f"Saved {alias} locally for {target}.")
    if questionary.confirm(f"Apply {alias} to {target} now?", default=False).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(DeploymentSettings()), root, [alias])
    else:
        print("Choose Review / apply assignments when ready to update the server.")


def deployment_status(remote: bool = False, verify_remote: bool = False) -> None:
    root = storage()
    if verify_remote and not remote:
        raise ValueError("Remote verification requires a Docker context")
    configured = deployment_services(root)
    docker = Docker(DeploymentSettings()) if remote else None
    deployment = load_deployment(root, docker.settings.docker_context if docker else None)
    states: dict[str, tuple[str, str, str]] = {}
    if docker:
        states = docker.service_status()
    entries = {
        (e.model_id, e.variant_id): e for e in load_installed(root / "installed.yaml").installed
    }
    rows = []
    for alias in ALIAS_CATEGORIES:
        assigned = alias in deployment.assignments
        preset = configured.get(alias) if assigned else None
        key = (preset["model_id"], preset["variant_id"]) if preset else None
        local = (
            "Downloaded"
            if key in entries and (root / entries[key].path).is_dir()
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
    settings = DeploymentSettings()
    deployment = load_deployment(root, settings.docker_context)
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
    save_deployment(root, deployment)
    if questionary.confirm("Apply this service state now?", default=False).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(settings), root, [alias])


def warn_vram(root: Path) -> None:
    deployment = load_deployment(root)
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
    deployment = load_deployment(root, DeploymentSettings().docker_context)
    if not deployment.assignments:
        print(
            "Assign uploaded models first. Bundled defaults are suggestions, not saved assignments."
        )
        return
    deployment_status(remote=True)
    warn_vram(root)
    if questionary.confirm(
        "Apply saved assignments and recreate affected services?", default=False
    ).ask():
        from inference_runtime_manager.installer.upload import provision

        provision(Docker(DeploymentSettings()), root, list(deployment.assignments))
        save_deployment(root, deployment)
