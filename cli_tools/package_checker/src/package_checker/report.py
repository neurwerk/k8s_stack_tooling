"""Build and render the package status report."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from package_checker.config import PackageConfig
from package_checker.github import GitHubApiError
from package_checker.models import PackageVersion, WorkflowRun


@dataclass(frozen=True)
class PackageReport:
    """Store the status displayed for one GHCR package.

    Args:
        package: GHCR package name.
        channel: Reserved optional channel field, retained for report compatibility.
        version: Newest non-`latest` tag, or a fallback label.
        published_at: Time this package version was published.
        digest: Image digest reported by GitHub.
        build_status: Active Actions status, unavailable state, or an error state.
        build_url: URL of the active build when one exists.
        error: Package lookup error when no package data is available.
    """

    package: str
    channel: str | None
    version: str | None
    published_at: str | None
    digest: str | None
    build_status: str
    build_url: str | None
    error: str | None


def create_report(
    package: PackageConfig, versions: list[PackageVersion], workflow_run: WorkflowRun | None
) -> PackageReport:
    """Create a display record from package versions and an active workflow run.

    Args:
        package: Configuration for the package being reported.
        versions: Active versions returned by GitHub Packages.
        workflow_run: Current queued or in-progress Actions run, if any.

    Returns:
        Report record for the package.
    """
    latest = newest_version(versions, package.stable_tags_only)
    return PackageReport(
        package=package.package_name,
        channel=None,
        version=version_label(latest, package.stable_tags_only),
        published_at=latest.created_at.isoformat(),
        digest=latest.name,
        build_status=workflow_run.status if workflow_run is not None else "not building",
        build_url=workflow_run.html_url if workflow_run is not None else None,
        error=None,
    )


def failed_report(
    package: PackageConfig,
    error: GitHubApiError,
    workflow_run: WorkflowRun | None = None,
    workflow_failed: bool = True,
) -> PackageReport:
    """Create a report record for a failed package-version request.

    Args:
        package: Configuration for the package that failed.
        error: GitHub API failure returned for the package.
        workflow_run: Active workflow run when that lookup succeeded.
        workflow_failed: Whether the workflow lookup also failed.

    Returns:
        Report record containing the error.
    """
    return PackageReport(
        package=package.package_name,
        channel=None,
        version=None,
        published_at=None,
        digest=None,
        build_status=(
            "unknown"
            if workflow_failed
            else workflow_run.status
            if workflow_run is not None
            else "not building"
        ),
        build_url=workflow_run.html_url if workflow_run is not None else None,
        error=str(error),
    )


def newest_version(
    versions: list[PackageVersion], stable_tags_only: bool = False
) -> PackageVersion:
    """Select the most recently published version.

    Args:
        versions: Versions returned by GitHub Packages.
        stable_tags_only: Require full plain X.Y.Z tags without fallback aliases.

    Returns:
        Version with the newest creation timestamp.

    Raises:
        GitHubApiError: If GitHub has no eligible active versions for the package.
    """
    immutable = [version for version in versions if _immutable_tags(version, stable_tags_only)]
    if immutable:
        return max(immutable, key=lambda version: version.created_at)
    matching = [version for version in versions if _matching_tags(version, stable_tags_only)]
    if matching:
        return max(matching, key=lambda version: version.created_at)
    if not stable_tags_only and versions:
        return max(versions, key=lambda version: version.created_at)
    raise GitHubApiError(_NO_ACTIVE_VERSIONS_MESSAGE)


def version_label(version: PackageVersion, stable_tags_only: bool = False) -> str:
    """Choose a human-readable tag for a package version.

    Args:
        version: Package version whose tags should be displayed.
        stable_tags_only: Restrict the label to full plain X.Y.Z tags.

    Returns:
        First non-`latest` tag, `latest`, or `untagged`.
    """
    immutable = _immutable_tags(version, stable_tags_only)
    if immutable:
        return max(immutable, key=lambda tag: (tag.count("."), len(tag), tag))
    matching = _matching_tags(version, stable_tags_only)
    if matching:
        return matching[0]
    return "untagged"


def _matching_tags(version: PackageVersion, stable_tags_only: bool) -> list[str]:
    """Return tags allowed by the package's version policy."""
    tags = version.metadata.container.tags
    if not stable_tags_only:
        return list(tags)
    return [tag for tag in tags if _STABLE_TAG.fullmatch(tag)]


def _immutable_tags(version: PackageVersion, stable_tags_only: bool) -> list[str]:
    """Return eligible tags that are not moving `latest` aliases."""
    return [
        tag
        for tag in _matching_tags(version, stable_tags_only)
        if tag != "latest" and not tag.startswith("latest-")
    ]


_STABLE_TAG = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_NO_ACTIVE_VERSIONS_MESSAGE = "GitHub reported no active package versions"


def render_table(reports: list[PackageReport]) -> str:
    """Render reports as an aligned plain-text table.

    Args:
        reports: Package report records to display.

    Returns:
        Plain-text status table.
    """
    headers = ("PACKAGE", "CHANNEL", "VERSION", "PUBLISHED AT", "DIGEST", "BUILD")
    rows = [headers, *[table_row(report) for report in reports]]
    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    separator = "  ".join("-" * width for width in widths)
    rendered_rows = [format_row(row, widths) for row in rows]
    return "\n".join([rendered_rows[0], separator, *rendered_rows[1:]])


def render_json(reports: list[PackageReport]) -> str:
    """Render reports as pretty-printed JSON.

    Args:
        reports: Package report records to serialize.

    Returns:
        JSON document ending with a newline.
    """
    return f"{json.dumps([asdict(report) for report in reports], indent=2)}\n"


def table_row(report: PackageReport) -> tuple[str, str, str, str, str, str]:
    """Convert a report to display-ready table cells.

    Args:
        report: Package report record.

    Returns:
        Ordered table cells.
    """
    build = report.build_status
    if report.build_url is not None:
        build = f"{build} ({report.build_url})"
    return (
        report.package,
        report.channel or "-",
        report.version or "-",
        display_timestamp(report.published_at),
        report.digest or "-",
        report.error or build,
    )


def display_timestamp(timestamp: str | None) -> str:
    """Render an ISO timestamp in a concise UTC-friendly form.

    Args:
        timestamp: ISO 8601 timestamp or None.

    Returns:
        Concise display timestamp or a placeholder.
    """
    if timestamp is None:
        return "-"
    return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M:%S %Z")


def format_row(row: tuple[str, ...], widths: list[int]) -> str:
    """Pad a row's cells to their column widths.

    Args:
        row: Cells in column order.
        widths: Display width for each column.

    Returns:
        Padded table row.
    """
    return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
