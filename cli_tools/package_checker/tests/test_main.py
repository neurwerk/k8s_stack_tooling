"""Test command-line report collection and rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from pydantic import SecretStr, ValidationError

from package_checker import main
from package_checker.config import PackageConfig
from package_checker.github import GitHubApiError
from package_checker.models import ContainerMetadata, PackageMetadata, PackageVersion

PACKAGES = (PackageConfig("k8s-stack-example", "neurwerk/k8s_stack_example"),)


@pytest.fixture
def inventory(tmp_path):
    path = tmp_path / "packages.json"
    path.write_text(
        json.dumps(
            [
                {
                    "package_name": "k8s-stack-example",
                    "repository": "neurwerk/k8s_stack_example",
                }
            ]
        )
    )
    return str(path)


def test_fetch_reports_keeps_package_data_when_workflow_check_fails() -> None:
    client = Mock()
    client.get_active_workflow_run.side_effect = GitHubApiError("Actions unavailable")
    client.list_package_versions.side_effect = package_versions

    reports = main.fetch_reports(client, PACKAGES)

    assert len(reports) == len(PACKAGES)
    assert all(report.build_status == "unknown" for report in reports)


def test_fetch_reports_records_package_errors() -> None:
    client = Mock()
    client.get_active_workflow_run.return_value = None
    client.list_package_versions.side_effect = GitHubApiError("Packages unavailable")

    reports = main.fetch_reports(client, PACKAGES)

    assert all(report.error == "Packages unavailable" for report in reports)


def test_fetch_reports_retains_active_build_for_a_new_package() -> None:
    client = Mock()
    client.get_active_workflow_run.return_value = Mock(
        status="in_progress", html_url="https://example.test/build"
    )
    client.list_package_versions.side_effect = GitHubApiError("Package not created yet")

    reports = main.fetch_reports(client, PACKAGES)

    assert all(report.build_status == "in_progress" for report in reports)
    assert all(report.build_url == "https://example.test/build" for report in reports)


def test_main_outputs_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], inventory
) -> None:
    settings = Mock(github_pat=SecretStr("test-token"))
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.get_active_workflow_run.return_value = None
    client.list_package_versions.side_effect = package_versions
    monkeypatch.setattr(main, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(main, "GitHubClient", Mock(return_value=client))

    main.main(["--inventory", inventory, "--json"])

    assert '"package": "k8s-stack-example"' in capsys.readouterr().out
    client.list_package_versions.assert_called_once_with("k8s-stack-example")
    client.get_active_workflow_run.assert_called_once_with("neurwerk/k8s_stack_example")


def test_main_exits_for_invalid_settings(monkeypatch: pytest.MonkeyPatch, inventory) -> None:
    monkeypatch.setattr(main, "Settings", Mock(side_effect=invalid_settings_error()))

    with pytest.raises(SystemExit, match="2"):
        main.main(["--inventory", inventory])


def test_main_reports_missing_plain_pii_version_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], inventory
) -> None:
    settings = Mock(github_pat=SecretStr("test-token"))
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.get_active_workflow_run.return_value = None
    version = package_version()
    version.metadata.container.tags = ["0.1.0-custom", "latest"]
    client.list_package_versions.return_value = [version]
    monkeypatch.setattr(main, "Settings", Mock(return_value=settings))
    monkeypatch.setattr(main, "GitHubClient", Mock(return_value=client))
    monkeypatch.setattr(
        main,
        "load_inventory",
        Mock(
            return_value=(
                PackageConfig(
                    "k8s-stack-pii-engine", "neurwerk/k8s_stack_pii_engine", stable_tags_only=True
                ),
            )
        ),
    )

    with pytest.raises(SystemExit, match="1"):
        main.main(["--inventory", inventory, "--json"])

    output = capsys.readouterr().out
    assert '"version": null' in output
    assert '"error": "GitHub reported no active package versions"' in output


def package_version() -> PackageVersion:
    created_at = datetime(2026, 8, 12, tzinfo=UTC)
    return PackageVersion(
        id=1,
        name="sha256:abc",
        created_at=created_at,
        updated_at=created_at,
        metadata=PackageMetadata(container=ContainerMetadata(tags=["1.2.3"])),
    )


def package_versions(package_name: str) -> list[PackageVersion]:
    return [package_version()]


def invalid_settings_error() -> Exception:
    try:
        from package_checker.config import Settings

        Settings(github_pat="EXAMPLE")
    except ValidationError as error:
        return error
    raise RuntimeError("Expected Settings validation to fail")
