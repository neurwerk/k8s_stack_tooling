from pathlib import Path
from unittest.mock import Mock

import pytest

from local_ai_installer.downloader.config import Settings
from local_ai_installer.downloader.errors import MediaDownloaderError
from local_ai_installer.downloader.main import (
    _ask_text,
    _download_queue,
    _format_bytes,
    _manage_authentication,
    _request_from_selection,
    _run_menu,
    _run_selection,
    _select_menu_option,
    _show_installed,
    _show_queue,
    _show_storage_status,
)
from local_ai_installer.downloader.models import (
    AvailableCatalog,
    CatalogModel,
    DownloadQueue,
    InstalledState,
    LicenseInfo,
    ModelMetadata,
    ModelVariant,
    Selection,
    StoredArtifact,
)


def _catalog() -> AvailableCatalog:
    return AvailableCatalog(
        schemaVersion=1,
        models=[
            CatalogModel(
                id="model",
                category="llm",
                displayName="Model",
                description="Test model",
                source="owner/model",
                gated=False,
                license=LicenseInfo(
                    name="MIT",
                    url="https://example.test",
                    commercialUse=True,
                    noticeRequired=True,
                ),
                metadata=ModelMetadata(architecture="causal-language-model", intendedUse="test"),
                variants=[
                    ModelVariant(
                        id="transformers",
                        format="transformers",
                        estimatedDownloadBytes=100,
                    )
                ],
            )
        ],
    )


def test_menu_exits_when_user_selects_exit(tmp_path: Path) -> None:
    outputs: list[str] = []
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    _run_menu(
        Mock(),
        Mock(),
        settings,
        _catalog(),
        Mock(),
        outputs.append,
        lambda: "exit",
    )

    assert outputs[-1] == "Exiting."


def test_menu_selection_uses_questionary_select(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = Mock(return_value="exit")
    select = Mock(return_value=Mock(unsafe_ask=ask))
    monkeypatch.setattr("local_ai_installer.downloader.main.questionary.select", select)

    assert _select_menu_option() == "exit"
    select.assert_called_once()
    ask.assert_called_once_with()


def test_menu_exits_when_selection_is_cancelled(tmp_path: Path) -> None:
    outputs: list[str] = []
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    _run_menu(Mock(), Mock(), settings, _catalog(), Mock(), outputs.append, lambda: None)

    assert outputs == ["Exiting."]


def test_text_prompt_uses_questionary(monkeypatch: pytest.MonkeyPatch) -> None:
    ask = Mock(return_value="0.1.2")
    text = Mock(return_value=Mock(ask=ask))
    monkeypatch.setattr("local_ai_installer.downloader.main.questionary.text", text)

    assert _ask_text("Bundle version:") == "0.1.2"
    text.assert_called_once_with("Bundle version:")


def test_text_prompt_fails_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_ai_installer.downloader.main.questionary.text",
        Mock(return_value=Mock(ask=Mock(return_value=None))),
    )

    with pytest.raises(MediaDownloaderError, match="cancelled"):
        _ask_text("Bundle version:")


def test_run_selection_requires_authentication_for_download(tmp_path: Path) -> None:
    outputs: list[str] = []
    client = Mock()
    client.authenticated_user.return_value = None
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    _run_selection("download", Mock(), client, settings, _catalog(), outputs.append)

    assert outputs == [
        "Operation failed: Choose Hugging Face login/authentication for this external drive first."
    ]


def test_request_from_selection_uses_variant_source_and_patterns() -> None:
    model = _catalog().model("model")
    variant = ModelVariant(
        id="gguf",
        source="owner/model-gguf",
        format="gguf",
        quantization="Q4_K_M",
        estimatedDownloadBytes=100,
        include=["*Q4_K_M.gguf"],
    )
    catalog = _catalog().model_copy(
        update={"models": [model.model_copy(update={"variants": [variant]})]}
    )

    request = _request_from_selection(catalog, Selection(modelId="model", variantId="gguf"))

    assert request.source == "owner/model-gguf"
    assert request.include == ["*Q4_K_M.gguf"]


def test_show_queue_reports_total_estimated_size() -> None:
    outputs: list[str] = []

    _show_queue(
        DownloadQueue(
            schemaVersion=1, selected=[Selection(modelId="model", variantId="transformers")]
        ),
        _catalog(),
        outputs.append,
    )

    assert outputs[-1] == "Total estimated download: 0 MiB"


def test_show_queue_reports_empty_queue() -> None:
    outputs: list[str] = []

    _show_queue(DownloadQueue(schemaVersion=1, selected=[]), _catalog(), outputs.append)

    assert outputs == ["Download queue is empty."]


def test_show_installed_reports_empty_state() -> None:
    outputs: list[str] = []

    _show_installed(InstalledState(schemaVersion=1, installed=[]), _catalog(), outputs.append)

    assert outputs == ["No models are installed."]


def test_manage_authentication_reports_existing_external_user() -> None:
    client = Mock()
    client.authenticated_user.return_value = "user=example-user"
    outputs: list[str] = []

    _manage_authentication(client, outputs.append)

    assert outputs == ["Authenticated for this external drive: user=example-user"]
    client.login.assert_not_called()


def test_show_storage_status_reports_external_state_paths(tmp_path: Path) -> None:
    outputs: list[str] = []
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    _show_storage_status(settings, outputs.append)

    assert f"Download queue: {tmp_path / 'download.yaml'}" in outputs
    assert any(line.startswith("Available space:") for line in outputs)


def test_format_bytes_uses_mib_and_gib() -> None:
    assert _format_bytes(1024**2) == "1 MiB"
    assert _format_bytes(1024**3) == "1.0 GiB"


def test_download_queue_updates_installed_and_clears_successful_selection(
    monkeypatch, tmp_path: Path
) -> None:
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")
    queue_path = tmp_path / "download.yaml"
    queue_path.write_text(
        "schemaVersion: 1\nselected:\n  - modelId: model\n    variantId: transformers\n"
    )
    request = _request_from_selection(
        _catalog(), Selection(modelId="model", variantId="transformers")
    )
    artifact = StoredArtifact.create(request, "a" * 40, [])
    store = Mock()
    store.synchronize.return_value = artifact
    store.destination.return_value = (
        tmp_path / "models" / "llm" / "owner" / "model" / "a" / "transformers"
    )
    monkeypatch.setattr(
        "local_ai_installer.downloader.main._queue_path", lambda settings: queue_path
    )
    monkeypatch.setattr(
        "local_ai_installer.downloader.main._installed_path",
        lambda settings: tmp_path / "installed.yaml",
    )
    outputs: list[str] = []

    _download_queue(store, settings, _catalog(), outputs.append)

    assert "Downloaded model/transformers." in outputs
    assert "selected: []" in queue_path.read_text()
