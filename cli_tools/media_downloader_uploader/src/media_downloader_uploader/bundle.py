"""Assemble and verify a selected PII model release."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from media_downloader_uploader.errors import IntegrityError, MediaDownloaderError
from media_downloader_uploader.integrity import (
    calculate_checksums,
    sha256_file,
    verify_checksums,
    write_checksums,
)
from media_downloader_uploader.models import (
    AvailableCatalog,
    CatalogModel,
    FileChecksum,
    InstalledModel,
    InstalledState,
    ModelVariant,
    Selection,
    StoredArtifact,
)

_MANIFEST_NAME = "manifest.yaml"
_CHECKSUMS_NAME = "checksums.sha256"
_PII_USE = "pii-detection"
_TRANSFORMERS_FORMAT = "transformers"

_BASE_LABELS = {
    "ACCOUNTNAME": "BANK_ACCOUNT",
    "BANKACCOUNT": "BANK_ACCOUNT",
    "BUILDINGNUM": "STREET_ADDRESS",
    "BUILDINGNUMBER": "STREET_ADDRESS",
    "CITY": "CITY",
    "CREDITCARD": "CREDIT_CARD_NUMBER",
    "CREDITCARDNUMBER": "CREDIT_CARD_NUMBER",
    "DATE": "DATE_OF_BIRTH",
    "DATEOFBIRTH": "DATE_OF_BIRTH",
    "DRIVERLICENSENUM": "DRIVERS_LICENSE_NUMBER",
    "EMAIL": "EMAIL_ADDRESS",
    "FIRSTNAME": "PERSON_NAME",
    "GIVENNAME": "PERSON_NAME",
    "IBAN": "IBAN",
    "IDCARDNUM": "NATIONAL_ID_NUMBER",
    "IPADDRESS": "IP_ADDRESS",
    "LASTNAME": "PERSON_NAME",
    "PASSPORTNUM": "PASSPORT_NUMBER",
    "PASSWORD": "PASSWORD_OR_SECRET",
    "PHONE": "PHONE_NUMBER",
    "SOCIALNUM": "NATIONAL_ID_NUMBER",
    "SSN": "NATIONAL_ID_NUMBER",
    "STREET": "STREET_ADDRESS",
    "SURNAME": "PERSON_NAME",
    "TAXNUM": "TAX_ID",
    "TELEPHONENUM": "PHONE_NUMBER",
    "PRIVATE": "SENSITIVE_TEXT",
    "USERNAME": "USERNAME",
    "VIN": "VEHICLE_REGISTRATION",
    "VRM": "VEHICLE_REGISTRATION",
    "ZIPCODE": "POSTAL_CODE",
}
_LABEL_MAPPING = {
    label: entity
    for source, entity in _BASE_LABELS.items()
    for label in (source, f"B-{source}", f"I-{source}")
}


class BundleError(MediaDownloaderError):
    """Indicate that a PII model release cannot be created or verified."""


@dataclass(frozen=True)
class PiiBundle:
    """Describe one complete staged PII model release."""

    version: str
    root: Path
    checksums: tuple[FileChecksum, ...]

    @property
    def manifest_path(self) -> Path:
        """Return the completion manifest path."""
        return self.root / _MANIFEST_NAME

    @property
    def checksums_path(self) -> Path:
        """Return the checksum file path."""
        return self.root / _CHECKSUMS_NAME

    @property
    def manifest_sha256(self) -> str:
        """Return the exact manifest digest that must be pinned in Git."""
        return sha256_file(self.manifest_path)

    @property
    def checksum_sha256(self) -> str:
        """Return the exact authenticated checksum-index digest."""
        return sha256_file(self.checksums_path)


@dataclass(frozen=True)
class _SelectedModel:
    """Hold one validated installed model selected for a release."""

    model: CatalogModel
    variant: ModelVariant
    installed: InstalledModel
    alias: str


def build_pii_bundle(
    staging_root: Path,
    storage_root: Path,
    catalog: AvailableCatalog,
    installed: InstalledState,
    selections: list[Selection],
    version: str,
) -> PiiBundle:
    """Stage explicitly selected installed PII transformer variants."""
    _validate_version(version)
    selected_models = _resolve_selected_models(catalog, installed, selections)
    destination = staging_root / version
    if destination.exists():
        raise BundleError(f"PII staging destination already exists: {destination}")
    destination.mkdir(parents=True)
    try:
        manifest_models = {
            selected.alias: _stage_model(storage_root, destination, selected)
            for selected in selected_models
        }
        checksums = tuple(calculate_checksums(destination))
        _require_checksums(checksums)
        write_checksums(destination / _CHECKSUMS_NAME, list(checksums))
        _write_manifest(destination, version, manifest_models, checksums)
        verify_checksums(destination, list(checksums))
    except (OSError, ValueError, KeyError, IntegrityError, yaml.YAMLError, BundleError):
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return PiiBundle(version=version, root=destination, checksums=checksums)


def pii_installed_selections(
    catalog: AvailableCatalog, installed: InstalledState
) -> list[Selection]:
    """Return installed variants eligible for explicit PII release selection."""
    eligible: list[Selection] = []
    for item in installed.installed:
        try:
            model = catalog.model(item.model_id)
            variant = model.variant(item.variant_id)
        except ValueError:
            continue
        if _is_pii_transformer(model, variant):
            eligible.append(Selection(modelId=item.model_id, variantId=item.variant_id))
    return eligible


def _resolve_selected_models(
    catalog: AvailableCatalog, installed: InstalledState, selections: list[Selection]
) -> list[_SelectedModel]:
    """Resolve and validate the exact selected installed variants."""
    if not selections:
        raise BundleError("Select at least one installed PII transformer model.")
    resolved: list[_SelectedModel] = []
    aliases: set[str] = set()
    for selection in selections:
        model = catalog.model(selection.model_id)
        variant = model.variant(selection.variant_id)
        if not _is_pii_transformer(model, variant) or model.metadata.pii_alias is None:
            raise BundleError(f"Model is not a configured PII transformer: {model.id}/{variant.id}")
        alias = model.metadata.pii_alias
        if alias in aliases:
            raise BundleError(f"Selected PII models use duplicate alias: {alias}")
        aliases.add(alias)
        resolved.append(
            _SelectedModel(model, variant, _find_installed(installed, selection), alias)
        )
    return resolved


def _is_pii_transformer(model: CatalogModel, variant: ModelVariant) -> bool:
    """Return whether a catalog variant can be included in a PII release."""
    return (
        model.metadata.intended_use == _PII_USE
        and model.metadata.pii_alias is not None
        and variant.format == _TRANSFORMERS_FORMAT
    )


def _find_installed(installed: InstalledState, selection: Selection) -> InstalledModel:
    """Find exactly one installed artifact for a selected variant."""
    matches = [
        item
        for item in installed.installed
        if (item.model_id, item.variant_id) == (selection.model_id, selection.variant_id)
    ]
    if len(matches) != 1:
        raise BundleError(
            "Installed verified model is missing or ambiguous: "
            f"{selection.model_id}/{selection.variant_id}"
        )
    return matches[0]


def _stage_model(
    storage_root: Path, destination: Path, selected: _SelectedModel
) -> dict[str, object]:
    """Verify and hard-link one selected artifact below its stable alias."""
    artifact_root = _safe_relative_path(storage_root, selected.installed.path)
    artifact = _load_artifact(artifact_root)
    source = selected.variant.source or selected.model.source
    revision = selected.variant.revision or selected.model.revision
    _validate_artifact(artifact, selected.model.id, selected.variant.id, source, revision)
    _link_model_files(artifact_root, destination / selected.alias, artifact.files)
    return {
        "catalogId": selected.model.id,
        "variantId": selected.variant.id,
        "upstream": artifact.source,
        "revision": artifact.revision,
        "path": selected.alias,
        "license": selected.model.license.name,
        "licenseUrl": selected.model.license.url,
        "supportedLanguages": selected.model.metadata.languages,
    }


def _write_manifest(
    destination: Path,
    version: str,
    models: dict[str, dict[str, object]],
    checksums: tuple[FileChecksum, ...],
) -> None:
    """Write the completion manifest that authenticates the checksum index."""
    checksum_path = destination / _CHECKSUMS_NAME
    manifest = {
        "schemaVersion": 2,
        "bundleVersion": version,
        "models": models,
        "runtime": {
            "labelsToIgnore": ["O"],
            "aggregationStrategy": "simple",
            "stride": 64,
            "modelToPresidioEntityMapping": _LABEL_MAPPING,
        },
        "checksumFile": _CHECKSUMS_NAME,
        "checksumSha256": sha256_file(checksum_path),
        "checksumSize": checksum_path.stat().st_size,
        "fileCount": len(checksums),
        "totalModelBytes": sum(item.size for item in checksums),
    }
    (destination / _MANIFEST_NAME).write_text(
        yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False), encoding="utf-8"
    )


def _require_checksums(checksums: tuple[FileChecksum, ...]) -> None:
    """Require at least one model file before publishing a release."""
    if not checksums:
        raise BundleError("PII release contains no model files")


def _safe_relative_path(root: Path, value: str) -> Path:
    """Resolve an installed-state path while preventing path traversal."""
    relative = Path(value)
    if relative.is_absolute():
        raise BundleError(f"Installed model path must be relative: {value}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise BundleError(f"Installed model path escapes storage root: {value}") from error
    return resolved


def _load_artifact(root: Path) -> StoredArtifact:
    """Load local completion metadata and verify all original model files."""
    metadata = root / "artifact.yaml"
    if not metadata.is_file():
        raise BundleError(f"Artifact metadata is missing: {metadata}")
    try:
        artifact = StoredArtifact.model_validate(
            yaml.safe_load(metadata.read_text(encoding="utf-8"))
        )
        verify_checksums(root, artifact.files)
    except (OSError, ValueError, TypeError, yaml.YAMLError, IntegrityError) as error:
        raise BundleError(f"Installed artifact is invalid: {root}") from error
    return artifact


def _validate_artifact(
    artifact: StoredArtifact,
    model_id: str,
    variant_id: str,
    source: str,
    requested_revision: str,
) -> None:
    """Ensure a local artifact is the selected catalog entry."""
    if artifact.model_id != model_id or artifact.variant_id != variant_id:
        raise BundleError(f"Installed artifact identity mismatch for {model_id}/{variant_id}")
    if artifact.source != source or artifact.requested_revision != requested_revision:
        raise BundleError(f"Installed artifact source or revision mismatch for {model_id}")


def _link_model_files(root: Path, destination: Path, files: list[FileChecksum]) -> None:
    """Hard-link verified files below an alias without duplicating model data."""
    for item in files:
        source = (root / Path(item.path)).resolve()
        try:
            source.relative_to(root.resolve())
        except ValueError as error:
            raise BundleError(f"Artifact file escapes its root: {item.path}") from error
        if source.is_symlink() or not source.is_file():
            raise BundleError(f"Artifact file is not a regular file: {item.path}")
        target = destination / Path(item.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.hardlink_to(source)


def _validate_version(version: str) -> None:
    """Require a portable release version suitable for an object prefix."""
    if not version or version.startswith(".") or "/" in version or "\\" in version:
        raise BundleError(f"Invalid bundle version: {version!r}")
