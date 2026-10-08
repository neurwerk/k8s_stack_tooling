"""Test package status report construction and rendering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from package_checker.config import PACKAGES, PackageConfig
from package_checker.github import GitHubApiError
from package_checker.models import ContainerMetadata, PackageMetadata, PackageVersion, WorkflowRun
from package_checker.report import (
    create_report,
    failed_report,
    newest_version,
    render_json,
    render_table,
    version_label,
)


def make_version(tags: list[str], created_at: datetime) -> PackageVersion:
    return PackageVersion(
        id=1,
        name="sha256:abc",
        created_at=created_at,
        updated_at=created_at,
        metadata=PackageMetadata(container=ContainerMetadata(tags=tags)),
    )


def test_create_report_uses_newest_version_and_active_workflow() -> None:
    package = PackageConfig("k8s-stack-example", "neurwerk/k8s_stack_example")
    older = make_version(["1.0.0"], datetime(2026, 8, 1, tzinfo=UTC))
    newest = make_version(["1.1.0", "latest"], datetime(2026, 8, 2, tzinfo=UTC))
    workflow = WorkflowRun(id=2, status="in_progress", html_url="https://example.test/run/2")

    report = create_report(package, [older, newest], workflow)

    assert report.version == "1.1.0"
    assert report.build_status == "in_progress"
    assert report.published_at == "2026-08-02T00:00:00+00:00"


def test_newest_version_rejects_empty_list() -> None:
    with pytest.raises(GitHubApiError, match="no active package versions"):
        newest_version([])


def test_version_label_falls_back_to_latest_and_untagged() -> None:
    created_at = datetime(2026, 8, 1, tzinfo=UTC)

    assert version_label(make_version(["latest"], created_at)) == "latest"
    assert version_label(make_version([], created_at)) == "untagged"


def test_newest_version_prefers_immutable_release_over_newer_latest() -> None:
    release = make_version(["0.2.13"], datetime(2026, 8, 1, tzinfo=UTC))
    moving = make_version(["latest"], datetime(2026, 8, 2, tzinfo=UTC))

    assert newest_version([release, moving]) == release


def test_newest_version_retains_latest_then_untagged_fallback() -> None:
    moving = make_version(["latest"], datetime(2026, 8, 1, tzinfo=UTC))
    untagged = make_version([], datetime(2026, 8, 2, tzinfo=UTC))

    assert newest_version([moving, untagged]) == moving
    assert newest_version([untagged]) == untagged


def test_pii_report_ignores_later_suffixed_publications() -> None:
    package = next(
        package for package in PACKAGES if package.package_name == "k8s-stack-pii-engine"
    )
    older = make_version(["0.1.0"], datetime(2026, 8, 1, tzinfo=UTC))
    release = make_version(["0.2.0", "latest", "9.9.9-custom"], datetime(2026, 8, 2, tzinfo=UTC))
    later = make_version(["0.1.0-custom", "0.1.0-preview"], datetime(2026, 8, 3, tzinfo=UTC))

    report = create_report(package, [older, release, later], None)

    assert report.version == "0.2.0"
    assert report.published_at == release.created_at.isoformat()
    assert report.channel is None


@pytest.mark.parametrize(
    "tags",
    [
        ["0.1.0-custom", "0.1.0-preview"],
        ["latest"],
        [],
        ["0.2", "v0.2.0", "0.2.0-rc.1", "0.2.0+build", "00.2.0", "0.2.0\n"],
    ],
)
def test_pii_report_rejects_versions_without_plain_stable_tags(tags: list[str]) -> None:
    package = next(
        package for package in PACKAGES if package.package_name == "k8s-stack-pii-engine"
    )
    version = make_version(tags, datetime(2026, 8, 1, tzinfo=UTC))

    with pytest.raises(GitHubApiError, match="no active package versions"):
        create_report(package, [version], None)


def test_dify_report_retains_suffixed_release_tag() -> None:
    package = next(
        package for package in PACKAGES if package.package_name == "k8s-stack-addon-dify-api"
    )
    release = make_version(["1.17.1-kc-v1"], datetime(2026, 8, 1, tzinfo=UTC))
    moving = make_version(["latest"], datetime(2026, 8, 2, tzinfo=UTC))

    assert create_report(package, [release, moving], None).version == "1.17.1-kc-v1"


def test_renderers_include_package_status() -> None:
    report = failed_report(
        PackageConfig("k8s-stack-example", "neurwerk/k8s_stack_example"),
        GitHubApiError("not found"),
    )

    assert "example" in render_table([report])
    assert '"error": "not found"' in render_json([report])
