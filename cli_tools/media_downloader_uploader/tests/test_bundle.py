import hashlib
from pathlib import Path

import pytest
import yaml

from media_downloader_uploader.bundle import (
    BundleError,
    build_pii_bundle,
    pii_installed_selections,
)
from media_downloader_uploader.models import (
    AvailableCatalog,
    CatalogModel,
    InstalledModel,
    InstalledState,
    LicenseInfo,
    ModelMetadata,
    ModelVariant,
    Selection,
    StoredArtifact,
)

_MODELS = (
    ("ai4privacy-multilingual-pii", "multilingual-pii", ["en", "de", "nl"]),
    ("ai4privacy-english-pii", "english-pii", ["en"]),
    ("openmed-german-pii", "german-pii", ["de"]),
    ("openmed-dutch-pii", "dutch-pii", ["nl"]),
)


def _catalog() -> AvailableCatalog:
    return AvailableCatalog(
        schemaVersion=1,
        models=[
            CatalogModel(
                id=model_id,
                category="ner",
                displayName=model_id,
                description="test",
                source=f"owner/{model_id}",
                revision="revision",
                license=LicenseInfo(
                    name="MIT", url="https://example.test", commercialUse=True, noticeRequired=False
                ),
                metadata=ModelMetadata(
                    architecture="token-classification",
                    languages=languages,
                    intendedUse="pii-detection",
                    piiAlias=alias,
                ),
                variants=[
                    ModelVariant(
                        id="transformers",
                        format="transformers",
                        estimatedDownloadBytes=1,
                        revision="revision",
                    )
                ],
            )
            for model_id, alias, languages in _MODELS
        ],
    )


def _installed(root: Path) -> InstalledState:
    entries: list[InstalledModel] = []
    for index, (model_id, _, _) in enumerate(_MODELS):
        artifact_root = root / "models" / "ner" / "owner" / model_id / "revision" / "transformers"
        artifact_root.mkdir(parents=True)
        (artifact_root / "config.json").write_text(f"model-{index}", encoding="utf-8")
        entries.append(
            InstalledModel(
                modelId=model_id,
                variantId="transformers",
                source=f"owner/{model_id}",
                revision="revision",
                path=artifact_root.relative_to(root).as_posix(),
                installedAt="2026-08-13T00:00:00Z",
                totalBytes=7,
                fileCount=1,
                verification="sha256",
            )
        )
    return InstalledState(schemaVersion=1, installed=entries)


def _write_artifacts(root: Path, state: InstalledState) -> None:
    from media_downloader_uploader.integrity import calculate_checksums, write_checksums

    for item in state.installed:
        artifact_root = root / item.path
        checksums = calculate_checksums(artifact_root)
        write_checksums(artifact_root / "checksums.sha256", checksums)
        artifact = StoredArtifact(
            schemaVersion=1,
            modelId=item.model_id,
            variantId=item.variant_id,
            category="ner",
            source=item.source,
            requestedRevision="revision",
            revision=item.revision,
            files=checksums,
            createdAt=item.installed_at,
        )
        (artifact_root / "artifact.yaml").write_text(
            yaml.safe_dump(artifact.model_dump(by_alias=True, mode="json"), sort_keys=False),
            encoding="utf-8",
        )


def _selection(index: int) -> Selection:
    return Selection(modelId=_MODELS[index][0], variantId="transformers")


def test_build_pii_bundle_contains_only_selected_alias_and_languages(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    staging = tmp_path / "staging"
    state = _installed(storage)
    _write_artifacts(storage, state)

    bundle = build_pii_bundle(staging, storage, _catalog(), state, [_selection(2)], "0.1.2")
    manifest = yaml.safe_load(bundle.manifest_path.read_text(encoding="utf-8"))

    assert list(manifest["models"]) == ["german-pii"]
    assert manifest["models"]["german-pii"]["supportedLanguages"] == ["de"]
    assert manifest["models"]["german-pii"]["variantId"] == "transformers"
    assert not (bundle.root / "multilingual-pii").exists()
    assert bundle.checksums_path.is_file()
    assert manifest["schemaVersion"] == 2
    checksum_sha256 = hashlib.sha256(bundle.checksums_path.read_bytes()).hexdigest()
    assert manifest["checksumSha256"] == checksum_sha256
    assert manifest["checksumSize"] == bundle.checksums_path.stat().st_size
    assert manifest["fileCount"] == len(bundle.checksums)
    assert manifest["totalModelBytes"] == sum(item.size for item in bundle.checksums)


def test_build_pii_bundle_hard_links_original_verified_file(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    state = _installed(storage)
    _write_artifacts(storage, state)
    source = storage / state.installed[0].path / "config.json"

    bundle = build_pii_bundle(
        tmp_path / "staging", storage, _catalog(), state, [_selection(0)], "0.1.2"
    )
    staged = bundle.root / "multilingual-pii" / "config.json"

    assert staged.stat().st_ino == source.stat().st_ino
    assert staged.read_text(encoding="utf-8") == "model-0"


def test_pii_installed_selections_returns_only_installed_eligible_variants(tmp_path: Path) -> None:
    state = _installed(tmp_path)
    state = state.model_copy(update={"installed": [state.installed[1], state.installed[3]]})

    selections = pii_installed_selections(_catalog(), state)

    assert [(item.model_id, item.variant_id) for item in selections] == [
        ("ai4privacy-english-pii", "transformers"),
        ("openmed-dutch-pii", "transformers"),
    ]


def test_build_pii_bundle_rejects_uninstalled_selection(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    state = _installed(storage)
    _write_artifacts(storage, state)
    state = state.model_copy(update={"installed": state.installed[:-1]})

    with pytest.raises(BundleError, match="missing or ambiguous"):
        build_pii_bundle(tmp_path / "staging", storage, _catalog(), state, [_selection(3)], "0.1.2")


def test_build_pii_bundle_rejects_empty_selection(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="Select at least one"):
        build_pii_bundle(
            tmp_path / "staging",
            tmp_path / "storage",
            _catalog(),
            InstalledState(schemaVersion=1, installed=[]),
            [],
            "0.1.2",
        )


def test_build_pii_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    state = _installed(storage)
    _write_artifacts(storage, state)
    state.installed[0] = state.installed[0].model_copy(update={"path": "../outside"})

    with pytest.raises(BundleError, match="escapes storage root"):
        build_pii_bundle(tmp_path / "staging", storage, _catalog(), state, [_selection(0)], "0.1.2")


@pytest.mark.parametrize("version", ["", ".hidden", "../escape", "nested/version"])
def test_build_pii_bundle_rejects_unsafe_version(tmp_path: Path, version: str) -> None:
    with pytest.raises(BundleError, match="Invalid bundle version"):
        build_pii_bundle(
            tmp_path / "staging",
            tmp_path / "storage",
            _catalog(),
            InstalledState(schemaVersion=1, installed=[]),
            [],
            version,
        )
