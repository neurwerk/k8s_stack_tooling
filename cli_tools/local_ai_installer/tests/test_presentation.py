from io import StringIO

import pytest
from rich.console import Console

from local_ai_installer.downloader.catalog import load_catalog
from local_ai_installer.downloader.models import DownloadQueue, InstalledState, Selection
from local_ai_installer.downloader.presentation import show_installed, show_queue, show_variants


@pytest.mark.parametrize("width", [40, 80, 140])
def test_variant_tables_wrap_without_losing_runtime_notes(monkeypatch, width):
    monkeypatch.setattr(
        "local_ai_installer.downloader.presentation.Console",
        lambda: Console(file=StringIO(), width=width, color_system=None),
    )
    model = load_catalog().model("voxcpm2").model_copy(deep=True)
    model.display_name = "Vox [bold]literal[/bold]"
    model.description = "Description"
    model.variants[0].runtime_notes = "Use FP16. GPU_RUNTIME_MARKER"
    output = []
    show_variants(model, output.append)
    rendered = "\n".join(output)
    assert "[bold]literal[/bold]" in rendered
    assert "GPU_RUNTIME_MARKER" in rendered
    assert "\x1b[" not in rendered
    # Headers and table data must fit the available terminal width.
    table = next(part for part in output if "Download" in part)
    assert all(len(line) <= width for line in table.splitlines())


def test_review_shows_single_variant_hardware_and_licensing_notes(monkeypatch):
    monkeypatch.setattr(
        "local_ai_installer.downloader.presentation.Console",
        lambda: Console(file=StringIO(), width=100, color_system=None),
    )
    catalog = load_catalog()
    queue = DownloadQueue(
        schemaVersion=1,
        selected=[Selection(modelId="qwen38-flash-next", variantId="transformers-bf16")],
    )
    outputs = []
    show_queue(queue, catalog, outputs.append)
    text = " ".join(" ".join(outputs).split())
    assert "Larger hardware required" in text
    assert "Commercial Model-as-a-Service" in text
    assert "Total estimated download: 335.3 GiB" in text


def test_installed_table_uses_actual_sizes(monkeypatch):
    monkeypatch.setattr(
        "local_ai_installer.downloader.presentation.Console",
        lambda: Console(file=StringIO(), width=120, color_system=None),
    )
    state = InstalledState.model_validate(
        {
            "schemaVersion": 1,
            "installed": [
                {
                    "modelId": "voxcpm2",
                    "variantId": "gguf-f16",
                    "source": "DennisHuang648/VoxCPM2-GGUF",
                    "revision": "a" * 40,
                    "path": "models/tts/voxcpm2",
                    "installedAt": "2026-09-16T00:00:00Z",
                    "totalBytes": 1024**3,
                    "fileCount": 3,
                    "verification": "sha256",
                }
            ],
        }
    )
    outputs = []
    show_installed(state, load_catalog(), outputs.append)
    text = "\n".join(outputs)
    assert "Installed models" in text
    assert "gguf-f16" in text
    assert "1.0 GiB" in text
