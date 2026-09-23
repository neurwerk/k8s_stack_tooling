"""Render model details and external-drive state as width-aware terminal tables."""

from __future__ import annotations

from collections.abc import Callable

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from inference_runtime_manager.downloader.models import (
    AvailableCatalog,
    CatalogModel,
    DownloadQueue,
    InstalledState,
)

CATEGORY_NAMES = {
    "image-generation": "Image generation",
    "vad": "Voice activity detection",
    "asr": "Speech recognition",
    "llm": "Language models",
    "ner": "Named-entity recognition",
    "ocr": "Document OCR / parsing",
    "tts": "Text-to-speech",
    "vlm": "General vision",
}


def format_bytes(value: int) -> str:
    """Render a byte count as a concise binary unit string."""
    if value < 1024**3:
        return f"{value / 1024**2:.0f} MiB"
    return f"{value / 1024**3:.1f} GiB"


def _table(title: str, *columns: str) -> Table:
    """Create a consistently styled table whose data is rendered literally."""
    table = Table(title=Text(title), box=box.SIMPLE, padding=(0, 1))
    for column in columns:
        table.add_column(
            column,
            justify="right" if column == "Download" else "left",
            min_width=8 if column == "Download" else None,
        )
    return table


def _emit(console: Console, table: Table, output: Callable[[str], None]) -> None:
    """Send captured Rich output through the application's injectable output sink."""
    with console.capture() as capture:
        console.print(table)
    output(capture.get().rstrip("\n"))


def _notes(notes: list[tuple[str, str]], console: Console, output: Callable[[str], None]) -> None:
    """Wrap full runtime notes below compact tables on narrow terminals."""
    if not notes:
        return
    with console.capture() as capture:
        for label, note in notes:
            console.print(Text(f"{label}: {note}"))
            console.print()
    output(capture.get().rstrip("\n"))


def show_variants(model: CatalogModel, output: Callable[[str], None]) -> None:
    """Display downloadable variants, licences, and relevant runtime requirements."""
    output(model.description)
    output(f"License: {model.license.name}")
    if model.license.notes:
        output(model.license.notes)
    console = Console()
    wide = console.width >= 110
    columns = ["Variant", "Format / stored weights", "Runtime", "Download"]
    if wide:
        columns.append("Runtime notes")
    table = _table(model.display_name, *columns)
    notes = []
    for variant in model.variants:
        weights = variant.quantization if variant.quantization != "none" else "unquantized"
        if variant.precision:
            weights += f" / {variant.precision.upper()}"
        row = [
            variant.id,
            f"{variant.format}\n{weights}",
            ", ".join(variant.runtimes) or "Download only",
            format_bytes(variant.estimated_download_bytes),
        ]
        if wide:
            row.append(variant.runtime_notes or "Not assessed for this GPU.")
        elif variant.runtime_notes:
            notes.append((variant.id, variant.runtime_notes))
        table.add_row(*(Text(cell) for cell in row))
    _emit(console, table, output)
    _notes(notes, console, output)
    _notes([(v.id, v.compatibility_notes) for v in model.variants], console, output)


def show_queue(
    queue: DownloadQueue, catalog: AvailableCatalog, output: Callable[[str], None]
) -> None:
    """Display selected variants with runtime guidance and aggregate download size."""
    if not queue.selected:
        output("Download queue is empty.")
        return
    console = Console()
    table = _table("Download queue", "Model / category / license", "Variant", "Runtime", "Download")
    notes = []
    licences: dict[str, str] = {}
    total = 0
    for selection in queue.selected:
        model = catalog.model(selection.model_id)
        variant = model.variant(selection.variant_id)
        total += variant.estimated_download_bytes
        table.add_row(
            Text(f"{model.display_name}\n{model.category.upper()} | {model.license.name}"),
            Text(variant.id),
            Text(", ".join(variant.runtimes) or "Download only"),
            Text(format_bytes(variant.estimated_download_bytes)),
        )
        if variant.runtime_notes:
            notes.append((f"{model.display_name} / {variant.id}", variant.runtime_notes))
        if model.license.notes:
            licences[model.display_name] = model.license.notes
    _emit(console, table, output)
    _notes(notes, console, output)
    _notes(list(licences.items()), console, output)
    output(f"Total estimated download: {format_bytes(total)}")


def show_installed(
    state: InstalledState, catalog: AvailableCatalog, output: Callable[[str], None]
) -> None:
    """Display verified external-drive installs with actual stored sizes."""
    if not state.installed:
        output("No models are installed.")
        return
    console = Console()
    table = _table("Installed models", "Model / category", "Variant", "Stored size")
    models = {model.id: model for model in catalog.models}
    for item in state.installed:
        model = models.get(item.model_id)
        label = (
            f"{model.display_name}\n{model.category.upper()}"
            if model is not None
            else f"{item.model_id}\nNot in current catalog"
        )
        variant = item.variant_id
        if model is not None and not any(v.id == item.variant_id for v in model.variants):
            variant += "\nNot in current catalog"
        table.add_row(
            Text(label),
            Text(variant),
            Text(format_bytes(item.total_bytes)),
        )
    _emit(console, table, output)
