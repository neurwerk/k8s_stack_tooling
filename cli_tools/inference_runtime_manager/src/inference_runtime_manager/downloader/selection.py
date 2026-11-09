"""Browse catalogue hierarchy and review a queue before saving."""

from __future__ import annotations

from collections.abc import Callable

import questionary

from inference_runtime_manager.downloader.hierarchy import browse
from inference_runtime_manager.downloader.models import AvailableCatalog, DownloadQueue, Selection
from inference_runtime_manager.downloader.presentation import show_queue


def partition_queue(
    catalog: AvailableCatalog, queue: DownloadQueue
) -> tuple[DownloadQueue, list[tuple[Selection, str]]]:
    models = {model.id: model for model in catalog.models}
    current: list[Selection] = []
    obsolete: list[tuple[Selection, str]] = []
    for selection in queue.selected:
        model = models.get(selection.model_id)
        if model is None:
            obsolete.append((selection, "model is not in the current catalog"))
        elif not any(variant.id == selection.variant_id for variant in model.variants):
            obsolete.append((selection, "variant is not in the current catalog"))
        else:
            current.append(selection)
    return DownloadQueue(schemaVersion=1, selected=current), obsolete


def review_obsolete_queue(
    catalog: AvailableCatalog, queue: DownloadQueue, output: Callable[[str], None]
) -> DownloadQueue | None:
    current, obsolete = partition_queue(catalog, queue)
    if not obsolete:
        return current
    output("Obsolete download queue entries:")
    for selection, reason in obsolete:
        output(f"- {selection.model_id} / {selection.variant_id}: {reason}")
    if (
        questionary.confirm("Remove obsolete entries and continue editing?", default=False).ask()
        is not True
    ):
        output("Download queue unchanged.")
        return None
    return current


def select_downloads(
    catalog: AvailableCatalog, queue: DownloadQueue, output: Callable[[str], None]
) -> DownloadQueue:
    selected = {(item.model_id, item.variant_id) for item in queue.selected}
    while True:
        result = browse(catalog, selected=selected, multiple=True)
        if result is None:
            raise KeyboardInterrupt
        draft = DownloadQueue(
            schemaVersion=1,
            selected=[Selection(modelId=m, variantId=v) for m, v in sorted(result)],
        )
        show_queue(draft, catalog, output)
        if not draft.selected:
            output("Saving this selection will clear the download queue.")
        action = questionary.select(
            "Review download selection", choices=["Save queue", "Back", "Cancel"]
        ).ask()
        if action == "Save queue":
            return draft
        if action != "Back":
            raise KeyboardInterrupt
        selected = result
