"""Model GitHub Packages and Actions API responses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ContainerMetadata(BaseModel):
    """Store container-specific fields returned by GitHub Packages.

    Args:
        tags: Tags associated with the package version.
    """

    tags: list[str] = Field(default_factory=list)


class PackageMetadata(BaseModel):
    """Store package metadata returned by GitHub Packages.

    Args:
        container: Container-specific metadata for this package version.
    """

    container: ContainerMetadata = Field(default_factory=ContainerMetadata)


class PackageVersion(BaseModel):
    """Represent one version returned by the GitHub Packages API.

    Args:
        id: GitHub package-version identifier.
        name: Package-version name, usually the image digest.
        created_at: Time the version was published to GitHub Packages.
        updated_at: Time GitHub last updated the version metadata.
        metadata: Container metadata associated with this version.
    """

    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    metadata: PackageMetadata = Field(default_factory=PackageMetadata)


class WorkflowRun(BaseModel):
    """Represent an active GitHub Actions workflow run.

    Args:
        id: GitHub Actions workflow-run identifier.
        status: Current GitHub Actions run state.
        html_url: Browser URL for the workflow run.
    """

    id: int
    status: str
    html_url: str


class WorkflowRunList(BaseModel):
    """Represent the paginated GitHub Actions workflow-run response.

    Args:
        workflow_runs: Workflow runs returned by GitHub.
    """

    workflow_runs: list[WorkflowRun] = Field(default_factory=list)
