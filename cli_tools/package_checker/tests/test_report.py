"""Test package status report construction and rendering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from package_checker.config import PackageConfig
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


def test_pii_channels_are_selected_independently() -> None:
    created_at = datetime(2026, 8, 1, tzinfo=UTC)
    cpu = make_version(["0.1.0-cpu", "0.1-cpu"], created_at)
    cuda = make_version(["0.1.0-cu124", "0.1-cu124"], created_at)

    assert newest_version([cpu, cuda], "-cpu") == cpu
    assert newest_version([cpu, cuda], "-cu124") == cuda
    assert version_label(cpu, "-cpu") == "0.1.0-cpu"


def test_renderers_include_package_status() -> None:
    report = failed_report(
        PackageConfig("k8s-stack-example", "neurwerk/k8s_stack_example"),
        GitHubApiError("not found"),
    )

    assert "example" in render_table([report])
    assert '"error": "not found"' in render_json([report])
