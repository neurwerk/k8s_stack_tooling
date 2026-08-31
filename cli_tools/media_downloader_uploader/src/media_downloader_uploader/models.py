"""Define curated model catalog, download queue, and installed-state models."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


class LicenseInfo(BaseModel):
    """Describe reviewed licensing information for one curated model.

    Args:
        name: SPDX identifier or reviewed upstream license name.
        url: Canonical license URL or upstream model-card URL.
        commercial_use: Whether the reviewed license permits commercial use.
        notice_required: Whether release notices must be retained.
    """

    name: str
    url: str
    commercial_use: bool = Field(alias="commercialUse")
    notice_required: bool = Field(alias="noticeRequired")


class ModelMetadata(BaseModel):
    """Describe reviewed technical and PII runtime metadata for one catalog model."""

    architecture: str
    parameter_count: str | None = Field(default=None, alias="parameterCount")
    context_length: int | None = Field(default=None, alias="contextLength", ge=1)
    languages: list[str] = Field(default_factory=list)
    intended_use: str = Field(alias="intendedUse")
    pii_alias: str | None = Field(default=None, alias="piiAlias", pattern=_IDENTIFIER)


class ModelVariant(BaseModel):
    """Describe one selectable downloadable representation of a catalog model.

    Args:
        id: Stable variant identifier within the catalog model.
        source: Optional Hugging Face repository overriding the model source.
        revision: Optional revision overriding the model revision.
        format: Local model format.
        quantization: Quantization label, or `none`.
        precision: Numeric weight precision when applicable.
        estimated_download_bytes: Curated approximate required download size.
        include: Hugging Face include patterns; empty means the full repository.
    """

    id: str
    source: str | None = None
    revision: str | None = None
    format: str
    quantization: str = "none"
    precision: str | None = None
    estimated_download_bytes: int = Field(alias="estimatedDownloadBytes", ge=1)
    include: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """Validate a portable lower-case identifier.

        Args:
            value: Catalog identifier.

        Returns:
            Validated identifier.

        Raises:
            ValueError: If the identifier is unsafe for paths and state files.
        """
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier must be a lower-case portable path component")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        """Validate an optional full Hugging Face model name.

        Args:
            value: Optional `owner/name` model source.

        Returns:
            Validated source or None.

        Raises:
            ValueError: If the source is not in `owner/name` format.
        """
        if value is not None and not _SOURCE.fullmatch(value):
            raise ValueError("source must be a Hugging Face repository in owner/name format")
        return value


class CatalogModel(BaseModel):
    """Describe one manually reviewed Hugging Face model available for selection.

    Args:
        id: Stable catalog identifier.
        category: Model category, such as `ner` or `llm`.
        display_name: Human-readable model name.
        description: Short catalog description.
        recommended: Whether the model is recommended by neurwerk.
        source: Canonical Hugging Face repository.
        revision: Curated source revision, tag, branch, or commit SHA.
        gated: Whether Hugging Face requires explicit upstream access approval.
        license: Reviewed license information.
        metadata: Reviewed technical metadata.
        variants: Downloadable local representations.
    """

    id: str
    category: str
    display_name: str = Field(alias="displayName")
    description: str
    recommended: bool = False
    source: str
    revision: str = "main"
    gated: bool = False
    license: LicenseInfo
    metadata: ModelMetadata
    variants: list[ModelVariant]

    @field_validator("id", "category")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """Validate a portable lower-case catalog identifier.

        Args:
            value: Catalog identifier or category.

        Returns:
            Validated value.

        Raises:
            ValueError: If the value is unsafe for paths and state files.
        """
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("identifier must be a lower-case portable path component")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        """Validate a full Hugging Face model name.

        Args:
            value: Model source in `owner/name` format.

        Returns:
            Validated source.

        Raises:
            ValueError: If the source is not in `owner/name` format.
        """
        if not _SOURCE.fullmatch(value):
            raise ValueError("source must be a Hugging Face repository in owner/name format")
        return value

    @model_validator(mode="after")
    def validate_variants(self) -> CatalogModel:
        """Require unique selectable variants.

        Returns:
            Validated catalog model.

        Raises:
            ValueError: If no variants or duplicate variant IDs are configured.
        """
        identifiers = [variant.id for variant in self.variants]
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("model variants must be non-empty and have unique identifiers")
        return self

    def variant(self, variant_id: str) -> ModelVariant:
        """Return a configured variant by ID.

        Args:
            variant_id: Stable variant identifier.

        Returns:
            Matching catalog variant.

        Raises:
            ValueError: If no variant has the requested identifier.
        """
        for variant in self.variants:
            if variant.id == variant_id:
                return variant
        raise ValueError(f"Unknown variant {variant_id!r} for catalog model {self.id!r}")


class AvailableCatalog(BaseModel):
    """Describe the manually maintained curated model catalog.

    Args:
        schema_version: Catalog schema version.
        models: Selectable NER and LLM models.
    """

    schema_version: int = Field(alias="schemaVersion", ge=1)
    models: list[CatalogModel]

    @model_validator(mode="after")
    def validate_models(self) -> AvailableCatalog:
        """Require unique model IDs.

        Returns:
            Validated catalog.

        Raises:
            ValueError: If model IDs are duplicated.
        """
        identifiers = [model.id for model in self.models]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("available catalog contains duplicate model identifiers")
        return self

    def model(self, model_id: str) -> CatalogModel:
        """Return one catalog model by ID.

        Args:
            model_id: Stable catalog model ID.

        Returns:
            Matching catalog model.

        Raises:
            ValueError: If no catalog model has the requested identifier.
        """
        for model in self.models:
            if model.id == model_id:
                return model
        raise ValueError(f"Unknown catalog model {model_id!r}")


class Selection(BaseModel):
    """Reference one selected model variant from the curated catalog.

    Args:
        model_id: Stable catalog model ID.
        variant_id: Stable variant ID belonging to that model.
    """

    model_id: str = Field(alias="modelId")
    variant_id: str = Field(alias="variantId")


class DownloadQueue(BaseModel):
    """Describe selected catalog variants waiting for local download.

    Args:
        schema_version: Queue schema version.
        selected: Selected catalog model variants.
    """

    schema_version: int = Field(alias="schemaVersion", ge=1)
    selected: list[Selection]

    @model_validator(mode="after")
    def validate_selections(self) -> DownloadQueue:
        """Require unique model and variant selection pairs.

        Returns:
            Validated queue.

        Raises:
            ValueError: If a model variant is selected more than once.
        """
        identifiers = [(item.model_id, item.variant_id) for item in self.selected]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("download queue contains duplicate selections")
        return self


class FileChecksum(BaseModel):
    """Record the SHA-256 digest for one artifact file.

    Args:
        path: Relative POSIX path inside the downloaded artifact.
        sha256: Lower-case SHA-256 hexadecimal digest.
        size: Byte size recorded when the checksum was calculated.
    """

    path: str
    sha256: str
    size: int = Field(ge=0)


class ArtifactRequest(BaseModel):
    """Describe a concrete catalog variant download.

    Args:
        model_id: Stable catalog model ID.
        variant_id: Stable catalog variant ID.
        category: Storage category.
        source: Hugging Face repository in `owner/name` format.
        revision: Curated revision to resolve before download.
        include: Optional Hugging Face include patterns.
    """

    model_id: str
    variant_id: str
    category: str
    source: str
    revision: str
    include: list[str] = Field(default_factory=list)

    @property
    def owner(self) -> str:
        """Return the Hugging Face repository owner."""
        return self.source.split("/", maxsplit=1)[0]

    @property
    def name(self) -> str:
        """Return the Hugging Face repository name."""
        return self.source.split("/", maxsplit=1)[1]


class StoredArtifact(BaseModel):
    """Describe one complete immutable local artifact bundle."""

    schema_version: int = Field(alias="schemaVersion", ge=1)
    model_id: str = Field(alias="modelId")
    variant_id: str = Field(alias="variantId")
    category: str
    source: str
    requested_revision: str = Field(alias="requestedRevision")
    revision: str
    files: list[FileChecksum]
    created_at: datetime = Field(alias="createdAt")

    @classmethod
    def create(
        cls, request: ArtifactRequest, revision: str, files: list[FileChecksum]
    ) -> StoredArtifact:
        """Create metadata for a successfully verified artifact."""
        return cls(
            schemaVersion=1,
            modelId=request.model_id,
            variantId=request.variant_id,
            category=request.category,
            source=request.source,
            requestedRevision=request.revision,
            revision=revision,
            files=files,
            createdAt=datetime.now(UTC),
        )

    def to_installed(self, path: Path) -> InstalledModel:
        """Create compact installed-state metadata for this verified artifact."""
        return InstalledModel.create(self, path)


class InstalledModel(BaseModel):
    """Describe one verified artifact available on the external drive."""

    model_id: str = Field(alias="modelId")
    variant_id: str = Field(alias="variantId")
    source: str
    revision: str
    path: str
    installed_at: datetime = Field(alias="installedAt")
    total_bytes: int = Field(alias="totalBytes", ge=0)
    file_count: int = Field(alias="fileCount", ge=0)
    verification: str

    @classmethod
    def create(cls, artifact: StoredArtifact, path: Path) -> InstalledModel:
        """Create compact installed-state metadata from a stored artifact."""
        return cls(
            modelId=artifact.model_id,
            variantId=artifact.variant_id,
            source=artifact.source,
            revision=artifact.revision,
            path=path.as_posix(),
            installedAt=artifact.created_at,
            totalBytes=sum(item.size for item in artifact.files),
            fileCount=len(artifact.files),
            verification="sha256",
        )


class InstalledState(BaseModel):
    """Describe verified model variants installed on one external storage drive."""

    schema_version: int = Field(alias="schemaVersion", ge=1)
    installed: list[InstalledModel]
