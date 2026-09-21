from pathlib import Path
from unittest.mock import Mock

import pytest
import questionary
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from questionary import Separator

from local_ai_installer.downloader.catalog import load_catalog, load_queue, write_queue
from local_ai_installer.downloader.config import Settings
from local_ai_installer.downloader.errors import MediaDownloaderError
from local_ai_installer.downloader.main import _select_models
from local_ai_installer.downloader.models import AvailableCatalog, DownloadQueue, Selection
from local_ai_installer.downloader.selection import model_choices, select_downloads


@pytest.fixture
def catalog():
    source = load_catalog()
    return AvailableCatalog(
        schemaVersion=1,
        models=[
            source.model("granite-docling-258m"),
            source.model("voxcpm2"),
            source.model("chatterbox-multilingual-v3"),
        ],
    )


def _queue(*pairs):
    return DownloadQueue(
        schemaVersion=1,
        selected=[Selection(modelId=model, variantId=variant) for model, variant in pairs],
    )


def _prompts(monkeypatch, checkboxes, actions):
    checkbox = Mock(side_effect=[Mock(unsafe_ask=Mock(return_value=a)) for a in checkboxes])
    select = Mock(side_effect=[Mock(unsafe_ask=Mock(return_value=a)) for a in actions])
    monkeypatch.setattr("local_ai_installer.downloader.selection.questionary.checkbox", checkbox)
    monkeypatch.setattr("local_ai_installer.downloader.selection.questionary.select", select)
    return checkbox, select


def test_keyboard_navigation_skips_headings_and_blank_rows(catalog):
    choices = model_choices(catalog, set())
    assert len([c for c in choices if not isinstance(c, Separator)]) == 3
    assert len([c for c in choices if isinstance(c, Separator) and not c.line.strip()]) == 2
    with create_pipe_input() as keyboard:
        keyboard.send_text(" \x1b[B \r")
        answer = questionary.checkbox(
            "Models", choices=choices, input=keyboard, output=DummyOutput()
        ).unsafe_ask()
    assert answer == ["granite-docling-258m", "chatterbox-multilingual-v3"]


def test_select_multiple_variants_and_skip_single_variant_menu(monkeypatch, catalog):
    checkbox, _ = _prompts(
        monkeypatch,
        [["voxcpm2", "chatterbox-multilingual-v3"], ["gguf-q8-0", "gguf-f16"]],
        ["next", "save"],
    )
    outputs = []
    result = select_downloads(catalog, _queue(), outputs.append)
    assert result == _queue(
        ("voxcpm2", "gguf-q8-0"),
        ("voxcpm2", "gguf-f16"),
        ("chatterbox-multilingual-v3", "pytorch-v3"),
    )
    assert checkbox.call_count == 2
    assert "Total estimated download:" in outputs[-1]
    assert "ChatterboxMultilingualTTS.from_local" in " ".join(outputs)


def test_preselection_and_back_preserve_drafts_across_models(monkeypatch, catalog):
    original = _queue(("voxcpm2", "gguf-q8-0"))
    checkbox, _ = _prompts(
        monkeypatch,
        [
            ["granite-docling-258m", "voxcpm2"],
            ["transformers"],
            ["gguf-q8-0", "gguf-f16"],
            ["transformers", "gguf-bf16"],
            ["gguf-q8-0", "gguf-f16"],
            ["gguf-f16"],
        ],
        ["next", "back", "next", "next", "back", "next", "save"],
    )
    result = select_downloads(catalog, original, Mock())
    model_rows = checkbox.call_args_list[0].kwargs["choices"]
    assert next(c for c in model_rows if c.value == "voxcpm2").checked
    first_vox = checkbox.call_args_list[2].kwargs["choices"]
    assert {c.value for c in first_vox if c.checked} == {"gguf-q8-0"}
    second_vox = checkbox.call_args_list[4].kwargs["choices"]
    assert {c.value for c in second_vox if c.checked} == {"gguf-q8-0", "gguf-f16"}
    assert result == _queue(
        ("granite-docling-258m", "transformers"),
        ("granite-docling-258m", "gguf-bf16"),
        ("voxcpm2", "gguf-f16"),
    )
    assert original == _queue(("voxcpm2", "gguf-q8-0"))


def test_back_to_models_retains_draft_but_deselected_models_are_removed(monkeypatch, catalog):
    checkbox, _ = _prompts(
        monkeypatch,
        [["voxcpm2"], ["gguf-f16"], ["chatterbox-multilingual-v3"]],
        ["back", "save"],
    )
    result = select_downloads(catalog, _queue(), Mock())
    rows = checkbox.call_args_list[2].kwargs["choices"]
    assert next(c for c in rows if c.value == "voxcpm2").checked
    assert result == _queue(("chatterbox-multilingual-v3", "pytorch-v3"))


def test_empty_variants_disable_continue_but_allow_back(monkeypatch, catalog):
    _, actions = _prompts(monkeypatch, [["voxcpm2"], [], []], ["back", "save"])
    assert select_downloads(catalog, _queue(), Mock()).selected == []
    navigation = actions.call_args_list[0].kwargs["choices"]
    assert next(c for c in navigation if c.value == "next").disabled
    assert not next(c for c in navigation if c.value == "back").disabled


def test_edit_reopens_variant_checklist_with_current_selection(monkeypatch, catalog):
    checkbox, _ = _prompts(
        monkeypatch,
        [["voxcpm2"], ["gguf-f16"], ["gguf-q8-0"]],
        ["edit", "next", "save"],
    )
    assert select_downloads(catalog, _queue(), Mock()) == _queue(("voxcpm2", "gguf-q8-0"))
    rows = checkbox.call_args_list[2].kwargs["choices"]
    assert {c.value for c in rows if c.checked} == {"gguf-f16"}


@pytest.mark.parametrize(
    ("checkboxes", "actions"),
    [
        ([None], []),
        ([["voxcpm2"], None], []),
        ([["voxcpm2"], ["gguf-f16"]], ["cancel"]),
        ([["voxcpm2"], ["gguf-f16"]], ["next", "cancel"]),
        ([["voxcpm2"], ["gguf-f16"]], ["next", None]),
    ],
)
def test_cancel_never_writes_existing_queue(monkeypatch, tmp_path, catalog, checkboxes, actions):
    path = tmp_path / "download.yaml"
    write_queue(path, _queue(("granite-docling-258m", "transformers")))
    before = path.read_bytes()
    _prompts(monkeypatch, checkboxes, actions)
    with pytest.raises(KeyboardInterrupt):
        _select_models(catalog, Settings(storage_root=tmp_path, hf_home=tmp_path / "hf"), Mock())
    assert path.read_bytes() == before


def test_save_replaces_queue_only_after_review(monkeypatch, tmp_path: Path, catalog):
    path = tmp_path / "download.yaml"
    original = _queue(("granite-docling-258m", "transformers"))
    write_queue(path, original)
    _prompts(monkeypatch, [["chatterbox-multilingual-v3"]], [])

    def review(*args, **kwargs):
        assert load_queue(path) == original
        return Mock(unsafe_ask=Mock(return_value="save"))

    monkeypatch.setattr("local_ai_installer.downloader.selection.questionary.select", review)
    _select_models(catalog, Settings(storage_root=tmp_path, hf_home=tmp_path / "hf"), Mock())
    assert load_queue(path) == _queue(("chatterbox-multilingual-v3", "pytorch-v3"))


def test_empty_selection_clears_queue_only_on_save(monkeypatch, tmp_path, catalog):
    path = tmp_path / "download.yaml"
    write_queue(path, _queue(("voxcpm2", "pytorch")))
    _prompts(monkeypatch, [[]], ["save"])
    outputs = []
    _select_models(
        catalog, Settings(storage_root=tmp_path, hf_home=tmp_path / "hf"), outputs.append
    )
    assert not load_queue(path).selected
    assert "Saving this selection will clear the download queue." in outputs


def test_declining_gated_confirmation_keeps_existing_queue(monkeypatch, tmp_path, catalog):
    path = tmp_path / "download.yaml"
    original = _queue(("voxcpm2", "pytorch"))
    write_queue(path, original)
    catalog.model("chatterbox-multilingual-v3").gated = True
    _prompts(monkeypatch, [["chatterbox-multilingual-v3"]], ["save"])
    monkeypatch.setattr(
        "local_ai_installer.downloader.main.questionary.confirm",
        Mock(return_value=Mock(unsafe_ask=Mock(return_value=False))),
    )
    with pytest.raises(MediaDownloaderError, match="Gated model selection was not confirmed"):
        _select_models(catalog, Settings(storage_root=tmp_path, hf_home=tmp_path / "hf"), Mock())
    assert load_queue(path) == original
