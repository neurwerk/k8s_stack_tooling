from pathlib import Path
from unittest.mock import Mock

import pytest

from media_downloader_uploader.config import Settings
from media_downloader_uploader.errors import MediaDownloaderError
from media_downloader_uploader.main import (
    _ask_text,
    _confirm_prefix_cleanup,
    _download_queue,
    _format_bytes,
    _manage_authentication,
    _request_from_selection,
    _run_menu,
    _run_selection,
    _select_installed_pii_models,
    _select_menu_option,
    _show_installed,
    _show_queue,
    _show_storage_status,
    _upload_pii_bundle,
)
from media_downloader_uploader.models import (
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
from media_downloader_uploader.rgw import KubectlUnavailableError


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
    monkeypatch.setattr("media_downloader_uploader.main.questionary.select", select)

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
    monkeypatch.setattr("media_downloader_uploader.main.questionary.text", text)

    assert _ask_text("Bundle version:") == "0.1.2"
    text.assert_called_once_with("Bundle version:")


def test_text_prompt_fails_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "media_downloader_uploader.main.questionary.text",
        Mock(return_value=Mock(ask=Mock(return_value=None))),
    )

    with pytest.raises(MediaDownloaderError, match="cancelled"):
        _ask_text("Bundle version:")


def test_prefix_cleanup_confirmation_is_bound_to_selected_context() -> None:
    prompt = Mock(return_value="example-cluster")

    assert _confirm_prefix_cleanup(prompt)("example-cluster", "pii-models", "0.1.2/")
    assert "example-cluster" in prompt.call_args.args[0]
    assert "s3://pii-models/0.1.2/" in prompt.call_args.args[0]


def test_run_selection_requires_authentication_for_download(tmp_path: Path) -> None:
    outputs: list[str] = []
    client = Mock()
    client.authenticated_user.return_value = None
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")

    _run_selection("download", Mock(), client, settings, _catalog(), outputs.append)

    assert outputs == [
        "Operation failed: Select option 5 to log in to Hugging Face on the external drive."
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


def test_upload_preflight_runs_before_bundle_version_prompt(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")
    prompt = Mock()
    monkeypatch.setattr(
        "media_downloader_uploader.main.check_kubernetes",
        Mock(side_effect=KubectlUnavailableError("cluster unavailable")),
    )

    with pytest.raises(KubectlUnavailableError, match="cluster unavailable"):
        _upload_pii_bundle(settings, _catalog(), prompt, Mock())

    prompt.assert_not_called()


def test_select_installed_pii_models_requires_explicit_selection(monkeypatch) -> None:
    model = (
        _catalog()
        .models[0]
        .model_copy(
            update={
                "category": "ner",
                "metadata": ModelMetadata(
                    architecture="token-classification",
                    languages=["en"],
                    intendedUse="pii-detection",
                    piiAlias="english-pii",
                ),
            }
        )
    )
    catalog = AvailableCatalog(schemaVersion=1, models=[model])
    installed = InstalledState(
        schemaVersion=1,
        installed=[
            {
                "modelId": "model",
                "variantId": "transformers",
                "source": "owner/model",
                "revision": "revision",
                "path": "models/model",
                "installedAt": "2026-08-17T00:00:00Z",
                "totalBytes": 100,
                "fileCount": 1,
                "verification": "sha256",
            }
        ],
    )
    ask = Mock(return_value=[])
    monkeypatch.setattr(
        "media_downloader_uploader.main.questionary.checkbox",
        Mock(return_value=Mock(unsafe_ask=ask)),
    )

    with pytest.raises(MediaDownloaderError, match="Select at least one"):
        _select_installed_pii_models(catalog, installed)


def test_upload_cleans_temporary_staging_after_success(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")
    bundle_roots: list[Path] = []
    monkeypatch.setattr("media_downloader_uploader.main.check_kubernetes", Mock())
    monkeypatch.setattr(
        "media_downloader_uploader.main.load_installed",
        Mock(return_value=InstalledState(schemaVersion=1, installed=[])),
    )
    monkeypatch.setattr(
        "media_downloader_uploader.main._select_installed_pii_models",
        Mock(return_value=[Selection(modelId="model", variantId="transformers")]),
    )

    def build(staging, storage, catalog, installed, selections, version):
        root = staging / version
        root.mkdir()
        bundle_roots.append(root)
        return Mock(version=version, root=root)

    monkeypatch.setattr("media_downloader_uploader.main.build_pii_bundle", build)
    publisher = Mock()
    publisher.publish.return_value = "0.1.2/"
    monkeypatch.setattr("media_downloader_uploader.main.RgwPublisher", Mock(return_value=publisher))

    _upload_pii_bundle(settings, _catalog(), lambda message: "0.1.2", Mock())

    assert len(bundle_roots) == 1
    assert not bundle_roots[0].exists()


def test_upload_cleans_temporary_staging_after_failure(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path, hf_home=tmp_path / "huggingface")
    bundle_roots: list[Path] = []
    monkeypatch.setattr("media_downloader_uploader.main.check_kubernetes", Mock())
    monkeypatch.setattr(
        "media_downloader_uploader.main.load_installed",
        Mock(return_value=InstalledState(schemaVersion=1, installed=[])),
    )
    monkeypatch.setattr(
        "media_downloader_uploader.main._select_installed_pii_models",
        Mock(return_value=[Selection(modelId="model", variantId="transformers")]),
    )

    def build(staging, storage, catalog, installed, selections, version):
        root = staging / version
        root.mkdir()
        bundle_roots.append(root)
        return Mock(version=version, root=root)

    monkeypatch.setattr("media_downloader_uploader.main.build_pii_bundle", build)
    publisher = Mock()
    publisher.publish.side_effect = OSError("upload failed")
    monkeypatch.setattr("media_downloader_uploader.main.RgwPublisher", Mock(return_value=publisher))

    with pytest.raises(OSError, match="upload failed"):
        _upload_pii_bundle(settings, _catalog(), lambda message: "0.1.2", Mock())

    assert len(bundle_roots) == 1
    assert not bundle_roots[0].exists()


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
    monkeypatch.setattr("media_downloader_uploader.main._queue_path", lambda settings: queue_path)
    monkeypatch.setattr(
        "media_downloader_uploader.main._installed_path",
        lambda settings: tmp_path / "installed.yaml",
    )
    outputs: list[str] = []

    _download_queue(store, settings, _catalog(), outputs.append)

    assert "Downloaded model/transformers." in outputs
    assert "selected: []" in queue_path.read_text()
