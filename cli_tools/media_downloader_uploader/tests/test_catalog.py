from importlib.resources import files
from pathlib import Path

import pytest

from media_downloader_uploader.catalog import (
    load_catalog,
    load_installed,
    load_queue,
    upsert_installed,
    write_installed,
    write_queue,
)
from media_downloader_uploader.models import (
    ArtifactRequest,
    DownloadQueue,
    InstalledModel,
    InstalledState,
    Selection,
    StoredArtifact,
)


def test_load_catalog_reads_curated_model_variants(tmp_path: Path) -> None:
    catalog_path = tmp_path / "available.yaml"
    catalog_path.write_text(
        "schemaVersion: 1\nmodels:\n"
        "  - id: model\n    category: llm\n    displayName: Model\n    description: Test\n"
        "    source: owner/model\n    gated: false\n"
        "    license:\n      name: MIT\n      url: https://example.test\n"
        "      commercialUse: true\n      noticeRequired: true\n"
        "    metadata:\n      architecture: causal-language-model\n      intendedUse: test\n"
        "    variants:\n      - id: transformers\n        format: transformers\n"
        "        estimatedDownloadBytes: 1\n"
    )

    catalog = load_catalog(catalog_path)

    assert catalog.model("model").variant("transformers").format == "transformers"


def test_load_default_catalog_uses_packaged_resource_outside_project_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    catalog = load_catalog()
    resource = files("media_downloader_uploader").joinpath("available.yaml")

    assert catalog.models
    assert resource.is_file()
    assert resource.read_text(encoding="utf-8").startswith("schemaVersion:")


def test_load_queue_uses_empty_state_when_absent(tmp_path: Path) -> None:
    assert load_queue(tmp_path / "download.yaml").selected == []


def test_write_queue_persists_selected_variants(tmp_path: Path) -> None:
    path = tmp_path / "download.yaml"
    queue = DownloadQueue(schemaVersion=1, selected=[Selection(modelId="model", variantId="gguf")])

    write_queue(path, queue)

    assert load_queue(path) == queue


def test_upsert_installed_replaces_matching_variant(tmp_path: Path) -> None:
    request = ArtifactRequest(
        model_id="model",
        variant_id="transformers",
        category="llm",
        source="owner/model",
        revision="main",
    )
    first = InstalledModel.create(
        StoredArtifact.create(request, "a" * 40, []), Path("models/first")
    )
    replacement = InstalledModel.create(
        StoredArtifact.create(request, "b" * 40, []), Path("models/replacement")
    )
    state = upsert_installed(InstalledState(schemaVersion=1, installed=[first]), replacement)
    path = tmp_path / "installed.yaml"

    write_installed(path, state)

    assert load_installed(path).installed == [replacement]


def test_load_catalog_rejects_duplicate_model_ids(tmp_path: Path) -> None:
    catalog_path = tmp_path / "available.yaml"
    catalog_path.write_text(
        "schemaVersion: 1\nmodels:\n"
        "  - id: repeated\n    category: llm\n    displayName: One\n    description: Test\n"
        "    source: owner/one\n    gated: false\n"
        "    license: {name: MIT, url: https://example.test, commercialUse: true, "
        "noticeRequired: true}\n"
        "    metadata: {architecture: causal-language-model, intendedUse: test}\n"
        "    variants: [{id: transformers, format: transformers, estimatedDownloadBytes: 1}]\n"
        "  - id: repeated\n    category: llm\n    displayName: Two\n    description: Test\n"
        "    source: owner/two\n    gated: false\n"
        "    license: {name: MIT, url: https://example.test, commercialUse: true, "
        "noticeRequired: true}\n"
        "    metadata: {architecture: causal-language-model, intendedUse: test}\n"
        "    variants: [{id: transformers, format: transformers, estimatedDownloadBytes: 1}]\n"
    )

    with pytest.raises(ValueError, match="duplicate"):
        load_catalog(catalog_path)
