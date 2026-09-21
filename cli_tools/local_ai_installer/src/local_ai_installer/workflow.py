"""One workstation menu for catalog, downloads, remote files and alias assignments."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import questionary
from rich.console import Console
from rich.table import Table
from rich.text import Text

from local_ai_installer.downloader.catalog import (
    load_catalog,
    load_installed,
    load_queue,
    write_queue,
)
from local_ai_installer.downloader.config import Settings, validate_storage
from local_ai_installer.downloader.main import _confirm_gated
from local_ai_installer.downloader.models import DownloadQueue, Selection
from local_ai_installer.downloader.presentation import CATEGORY_NAMES, format_bytes, show_variants
from local_ai_installer.installer import checks
from local_ai_installer.installer.assignments import (
    ALIAS_CATEGORIES,
    Assignment,
    deployment_slots,
    load_deployment,
    recipe,
    save_deployment,
)
from local_ai_installer.installer.docker import DeploymentSettings, Docker
from local_ai_installer.installer.upload import prepare, prepare_artifact, transfer


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
        ["#", "Model", "Variant", "LocalAI", "Local files", "Download"],
        [
            [
                str(i),
                m.display_name,
                v.id,
                v.localai.title(),
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
                f"{i:>2}  {m.id} / {v.id}  [{v.localai.title()}]",
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
            recipe(entry.model_id, entry.variant_id)
        except ValueError:
            continue
        candidates.append(entry)
    if not candidates:
        print("No downloaded model with a reviewed LocalAI recipe for this alias.")
        print("Unverified models can be downloaded/uploaded, but cannot be activated yet.")
        return
    entry = questionary.select(
        f"Assign {alias}",
        choices=[questionary.Choice(f"{e.model_id} / {e.variant_id}", value=e) for e in candidates],
    ).ask()
    if entry is None:
        return
    # Check actual publication, rather than trusting a previous upload receipt.
    _, manifest = prepare_artifact(root, entry.model_id, entry.variant_id)
    if not Docker(DeploymentSettings()).worker({"action": "inspect", "manifest": manifest})[
        "present"
    ]:
        raise ValueError("Upload this model to the selected target before assigning it")
    enabled = questionary.confirm("Enable when applied?", default=True).ask()
    if enabled is None:
        return
    deployment.assignments[alias] = Assignment(
        model_id=entry.model_id, variant_id=entry.variant_id, enabled=enabled
    )
    save_deployment(root, deployment)
    print("Saved locally. Choose Apply assignments to update the server.")


def deployment_status(remote: bool = False) -> None:
    root = storage()
    configured = deployment_slots(root)
    docker = Docker(DeploymentSettings()) if remote else None
    if docker:
        load_deployment(root, docker.settings.docker_context)
    entries = {
        (e.model_id, e.variant_id): e for e in load_installed(root / "installed.yaml").installed
    }
    rows = []
    for alias in ALIAS_CATEGORIES:
        preset = configured.get(alias)
        if preset is None:
            rows.append([alias, "—", "—", "Not checked", "—", "Not tested"])
            continue
        key = preset["model_id"], preset["variant_id"]
        local = (
            "Downloaded" if key in entries and (root / entries[key].path).is_dir() else "Missing"
        )
        remote_state = "Not checked"
        if docker and local == "Downloaded":
            _, manifest = prepare_artifact(root, *key)
            remote_state = (
                "Verified files"
                if docker.worker({"action": "inspect", "manifest": manifest})["present"]
                else "Missing"
            )
        test = (
            checks.status(docker, root, alias)
            if docker and remote_state == "Verified files"
            else "Not checked"
        )
        rows.append(
            [
                alias,
                "/".join(key),
                local,
                remote_state,
                "Enable" if preset["enabled"] else "Disable",
                test,
            ]
        )
    table(
        "Deployment plan (not live model state)",
        ["Alias", "Assignment", "Local files", "Remote files", "On apply", "Inference"],
        rows,
    )
    print(
        "File checks do not prove inference. Recorded probes verify a response, not quality or GPU fit."
    )


def apply_assignments(execute: Callable[..., None]) -> None:
    root = storage()
    deployment = load_deployment(root, DeploymentSettings().docker_context)
    if not deployment.assignments:
        print(
            "Assign uploaded models first. Bundled defaults are suggestions, not saved assignments."
        )
        return
    deployment_status(remote=True)
    for alias in deployment.assignments:
        prepare(root, alias)
    if questionary.confirm("Apply saved assignments and restart LocalAI?", default=False).ask():
        execute("upload", list(deployment.assignments))


def test_models() -> None:
    root = storage()
    docker = Docker(DeploymentSettings())
    load_deployment(root, docker.settings.docker_context)
    alias = questionary.select(
        "Inference test (other model types: use LocalAI UI)",
        choices=["tts-german", "llm-general"],
    ).ask()
    if alias is None:
        return
    if questionary.confirm(
        f"Run a short inference request for {alias} on the target?", default=False
    ).ask():
        checks.verify(docker, root, alias)
        print("Verified response. Changes to model/config/backend pins invalidate this result.")
