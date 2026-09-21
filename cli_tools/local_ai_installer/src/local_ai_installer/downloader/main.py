"""Run the interactive curated-model downloader application."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import questionary
from pydantic import ValidationError
from questionary import Choice

from local_ai_installer.downloader.catalog import (
    load_catalog,
    load_installed,
    load_queue,
    upsert_installed,
    write_installed,
    write_queue,
)
from local_ai_installer.downloader.config import Settings, huggingface_environment, validate_storage
from local_ai_installer.downloader.errors import MediaDownloaderError
from local_ai_installer.downloader.huggingface import HuggingFaceClient
from local_ai_installer.downloader.models import (
    ArtifactRequest,
    AvailableCatalog,
    DownloadQueue,
    Selection,
)
from local_ai_installer.downloader.presentation import (
    format_bytes as _format_bytes,
)
from local_ai_installer.downloader.presentation import (
    show_installed as _show_installed,
)
from local_ai_installer.downloader.presentation import (
    show_queue as _show_queue,
)
from local_ai_installer.downloader.selection import select_downloads
from local_ai_installer.downloader.store import ArtifactStore

_logger = logging.getLogger(__name__)
_MENU_CHOICES = [
    Choice("Select models for download", value="select_models"),
    Choice("View download queue", value="view_queue"),
    Choice("Download selected models", value="download"),
    Choice("View installed models", value="view_installed"),
    Choice("Hugging Face authentication", value="authentication"),
    Choice("Storage status", value="storage_status"),
    Choice("Exit", value="exit"),
]


def main() -> None:
    """Open the interactive curated-model downloader menu."""
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
    try:
        settings = Settings()
        validate_storage(settings)
        catalog = load_catalog()
    except (OSError, ValidationError, ValueError, TypeError) as error:
        _logger.error("%s", error)  # noqa: TRY400 -- CLI needs concise user-facing output.
        raise SystemExit(1) from error
    client = HuggingFaceClient(huggingface_environment(settings))
    store = ArtifactStore(settings.storage_root.resolve(), client)
    _run_menu(store, client, settings, catalog, _ask_text, print)


def _select_menu_option() -> str | None:
    """Prompt for one main-menu action with arrow-key navigation."""
    selection: object = questionary.select(
        "LocalAI Installer — Model downloads",
        choices=_MENU_CHOICES,
        instruction="Use arrows, then Enter.",
    ).unsafe_ask()
    return selection if isinstance(selection, str) else None


def _ask_text(message: str) -> str:
    """Prompt for text and treat cancellation as an operation failure."""
    try:
        answer = questionary.text(message).ask()
    except (EOFError, KeyboardInterrupt):
        raise MediaDownloaderError("Interactive prompt was cancelled") from None
    if not isinstance(answer, str):
        raise MediaDownloaderError("Interactive prompt was cancelled")
    return answer


def _run_menu(
    store: ArtifactStore,
    client: HuggingFaceClient,
    settings: Settings,
    catalog: AvailableCatalog,
    prompt: Callable[[str], str],
    output: Callable[[str], None],
    menu_prompt: Callable[[], str | None] = _select_menu_option,
) -> None:
    """Run the interactive menu until the user exits or closes standard input."""
    while True:
        try:
            selection = menu_prompt()
        except (EOFError, KeyboardInterrupt):
            output("\nExiting.")
            return
        if selection is None or selection == "exit":
            output("Exiting.")
            return
        _run_selection(selection, store, client, settings, catalog, output, prompt)


def _run_selection(
    selection: str,
    store: ArtifactStore,
    client: HuggingFaceClient,
    settings: Settings,
    catalog: AvailableCatalog,
    output: Callable[[str], None],
    prompt: Callable[[str], str] = input,
) -> None:
    """Run one menu operation and preserve the interactive session on failure."""
    try:
        if selection == "select_models":
            _select_models(catalog, settings, output)
        elif selection == "view_queue":
            _show_queue(load_queue(_queue_path(settings)), catalog, output)
        elif selection == "download":
            _require_authentication(client)
            _download_queue(store, settings, catalog, output)
        elif selection == "view_installed":
            _show_installed(load_installed(_installed_path(settings)), catalog, output)
        elif selection == "authentication":
            _manage_authentication(client, output)
        elif selection == "storage_status":
            _show_storage_status(settings, output)
        else:
            output("Choose a menu action.")
    except (EOFError, KeyboardInterrupt):
        output("Operation cancelled.")
    except (MediaDownloaderError, OSError, ValidationError, ValueError, TypeError) as error:
        output(f"Operation failed: {error}")


def _select_models(
    catalog: AvailableCatalog, settings: Settings, output: Callable[[str], None]
) -> None:
    """Review model and variant choices before atomically saving the external queue."""
    queue = load_queue(_queue_path(settings))
    updated = select_downloads(catalog, queue, output)
    _confirm_gated(catalog, updated.selected)
    write_queue(_queue_path(settings), updated)
    output(f"Saved {len(updated.selected)} selected model variants to {_queue_path(settings)}.")


def _confirm_gated(catalog: AvailableCatalog, selections: list[Selection]) -> None:
    """Require explicit confirmation before queueing gated upstream models."""
    gated = [
        catalog.model(item.model_id) for item in selections if catalog.model(item.model_id).gated
    ]
    if not gated:
        return
    names = ", ".join(model.display_name for model in gated)
    confirmed = questionary.confirm(
        f"{names} requires upstream access and its listed license. Add to queue?", default=False
    ).unsafe_ask()
    if confirmed is None:
        raise KeyboardInterrupt
    if not confirmed:
        raise MediaDownloaderError("Gated model selection was not confirmed.")


def _download_queue(
    store: ArtifactStore,
    settings: Settings,
    catalog: AvailableCatalog,
    output: Callable[[str], None],
) -> None:
    """Download queued model variants and retain only failed selections in the queue."""
    queue_path = _queue_path(settings)
    queue = load_queue(queue_path)
    if not queue.selected:
        output("Download queue is empty. Select models first.")
        return
    installed_path = _installed_path(settings)
    installed = load_installed(installed_path)
    remaining: list[Selection] = []
    for selection in queue.selected:
        try:
            request = _request_from_selection(catalog, selection)
            artifact = store.synchronize(request)
            destination = store.destination(request, artifact.revision)
            relative_path = destination.relative_to(settings.storage_root.resolve())
            installed = upsert_installed(installed, artifact.to_installed(relative_path))
            write_installed(installed_path, installed)
            output(f"Downloaded {selection.model_id}/{selection.variant_id}.")
        except (MediaDownloaderError, OSError, ValidationError, ValueError, TypeError) as error:
            output(f"Failed {selection.model_id}/{selection.variant_id}: {error}")
            remaining.append(selection)
    write_queue(queue_path, DownloadQueue(schemaVersion=1, selected=remaining))


def _request_from_selection(catalog: AvailableCatalog, selection: Selection) -> ArtifactRequest:
    """Resolve one queue selection to its curated concrete download request."""
    model = catalog.model(selection.model_id)
    variant = model.variant(selection.variant_id)
    return ArtifactRequest(
        model_id=model.id,
        variant_id=variant.id,
        category=model.category,
        source=variant.source or model.source,
        revision=variant.revision or model.revision,
        include=variant.include,
    )


def _require_authentication(client: HuggingFaceClient) -> None:
    """Require a valid token in the configured external Hugging Face home."""
    if client.authenticated_user() is None:
        raise MediaDownloaderError(
            "Select option 5 to log in to Hugging Face on the external drive."
        )


def _manage_authentication(client: HuggingFaceClient, output: Callable[[str], None]) -> None:
    """Show external-cache authentication state and start login when necessary."""
    user = client.authenticated_user()
    if user is not None:
        output(f"Authenticated for this external drive: {user}")
        return
    output("Opening Hugging Face login for this external drive.")
    client.login()
    user = client.authenticated_user()
    if user is None:
        raise MediaDownloaderError("Hugging Face login did not create a usable session.")
    output(f"Authenticated for this external drive: {user}")


def _show_storage_status(settings: Settings, output: Callable[[str], None]) -> None:
    """Display external storage paths, available capacity, and state-file locations."""
    usage = shutil.disk_usage(settings.storage_root)
    output(f"Storage root: {settings.storage_root}")
    output(f"Hugging Face home: {settings.hf_home}")
    output(f"Download queue: {_queue_path(settings)}")
    output(f"Installed state: {_installed_path(settings)}")
    output(f"Available space: {_format_bytes(usage.free)} of {_format_bytes(usage.total)}")


def _queue_path(settings: Settings) -> Path:
    """Return the external-drive download queue path."""
    return settings.storage_root / "download.yaml"


def _installed_path(settings: Settings) -> Path:
    """Return the external-drive installed-state path."""
    return settings.storage_root / "installed.yaml"
