"""Select main models, then their variants, before reviewing the complete queue."""

from __future__ import annotations

from collections.abc import Callable

import questionary
from questionary import Choice, Separator

from local_ai_installer.downloader.errors import MediaDownloaderError
from local_ai_installer.downloader.models import (
    AvailableCatalog,
    CatalogModel,
    DownloadQueue,
    Selection,
)
from local_ai_installer.downloader.presentation import CATEGORY_NAMES, show_queue, show_variants


def model_choices(catalog: AvailableCatalog, selected: set[str]) -> list[Choice]:
    """List each main model once with non-selectable headings and blank separators."""
    choices: list[Choice] = []
    category = None
    for model in sorted(catalog.models, key=lambda item: (item.category, item.display_name)):
        if choices:
            choices.append(Separator(" "))
        if model.category != category:
            category = model.category
            heading = CATEGORY_NAMES.get(category, category.upper())
            choices.append(Separator(f"── {heading} ({category.upper()}) ──"))
        tags = []
        if model.recommended:
            tags.append("recommended")
        if model.gated:
            tags.append("gated")
        suffix = f" ({', '.join(tags)})" if tags else ""
        choices.append(
            Choice(
                model.display_name + suffix,
                value=model.id,
                checked=model.id in selected,
                description=model.description,
            )
        )
    return choices


def _ask_models(catalog: AvailableCatalog, selected: set[str]) -> list[str]:
    """Choose main models, permitting an empty selection to clear the queue on save."""
    answer = questionary.checkbox(
        "Select models for download",
        choices=model_choices(catalog, selected),
        instruction="Arrows, Space to select, Enter to continue; Ctrl+C cancels.",
    ).unsafe_ask()
    if answer is None:
        raise KeyboardInterrupt
    return list(answer)


def _ask_action(message: str, choices: list[Choice]) -> str:
    """Ask for navigation and consistently treat cancellation as an aborted edit."""
    answer = questionary.select(message, choices=choices).unsafe_ask()
    if answer is None or answer == "cancel":
        raise KeyboardInterrupt
    if not isinstance(answer, str):
        raise TypeError("Expected a navigation action")
    return answer


def _variant_step(
    model: CatalogModel, selected: set[str], output: Callable[[str], None]
) -> tuple[str, set[str]]:
    """Edit one model's variants and retain choices when going back or editing again."""
    show_variants(model, output)
    while True:
        answer = questionary.checkbox(
            f"{model.display_name} — select variant(s)",
            choices=[
                Choice(
                    variant.id,
                    value=variant.id,
                    checked=variant.id in selected,
                    description=variant.runtime_notes or model.description,
                )
                for variant in model.variants
            ],
            instruction="Arrows, Space; Enter for Continue / Edit / Back / Cancel.",
        ).unsafe_ask()
        if answer is None:
            raise KeyboardInterrupt
        selected = set(answer)
        action = _ask_action(
            "Variant selection",
            [
                Choice("Continue", value="next", disabled=None if selected else "Select a variant"),
                Choice("Edit variants", value="edit"),
                Choice("Back", value="back"),
                Choice("Cancel", value="cancel"),
            ],
        )
        if action == "back" or (action == "next" and selected):
            return action, selected


def _draft_queue(
    catalog: AvailableCatalog, models: list[str], variants: dict[str, set[str]]
) -> DownloadQueue:
    """Flatten chosen variants into the existing queue format in catalog order."""
    selections = []
    for model_id in models:
        model = catalog.model(model_id)
        chosen = [variant for variant in model.variants if variant.id in variants[model_id]]
        if not chosen:
            raise ValueError(f"Select at least one variant for {model.display_name}")
        selections.extend(Selection(modelId=model_id, variantId=variant.id) for variant in chosen)
    return DownloadQueue(schemaVersion=1, selected=selections)


def select_downloads(
    catalog: AvailableCatalog, queue: DownloadQueue, output: Callable[[str], None]
) -> DownloadQueue:
    """Run the model/variant wizard entirely in memory until Save queue is chosen."""
    if not catalog.models:
        raise MediaDownloaderError("The model catalog is empty.")
    models = list(dict.fromkeys(item.model_id for item in queue.selected))
    variants: dict[str, set[str]] = {
        model.id: {item.variant_id for item in queue.selected if item.model_id == model.id}
        for model in catalog.models
    }
    configurable: list[CatalogModel] = []
    step = -1
    while True:
        if step == -1:
            models = _ask_models(catalog, set(models))
            configurable = []
            for model_id in models:
                model = catalog.model(model_id)
                if len(model.variants) == 1:
                    variants[model_id] = {model.variants[0].id}
                else:
                    configurable.append(model)
            step = 0
        elif step < len(configurable):
            model = configurable[step]
            action, variants[model.id] = _variant_step(model, variants[model.id], output)
            step += -1 if action == "back" else 1
        else:
            draft = _draft_queue(catalog, models, variants)
            show_queue(draft, catalog, output)
            if not draft.selected:
                output("Saving this selection will clear the download queue.")
            action = _ask_action(
                "Review download selection",
                [
                    Choice("Save queue", value="save"),
                    Choice("Back", value="back"),
                    Choice("Cancel", value="cancel"),
                ],
            )
            if action == "save":
                return draft
            step = len(configurable) - 1
