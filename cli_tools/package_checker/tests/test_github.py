"""Test GitHub API response validation."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from package_checker.github import GitHubApiError, GitHubClient


def test_parse_versions_reads_container_tags() -> None:
    response = """[
      {
        "id": 42,
        "name": "sha256:abc",
        "created_at": "2026-08-12T10:00:00Z",
        "updated_at": "2026-08-12T10:00:00Z",
        "metadata": {"container": {"tags": ["1.2.3", "latest"]}}
      }
    ]"""

    versions = GitHubClient._parse_versions(response, "test-package")

    assert versions[0].metadata.container.tags == ["1.2.3", "latest"]


def test_parse_versions_rejects_invalid_response() -> None:
    with pytest.raises(GitHubApiError, match="invalid version data"):
        GitHubClient._parse_versions("{}", "test-package")


def test_parse_runs_reads_workflow_data() -> None:
    response = """{"workflow_runs": [{"id": 12, "status": "in_progress", "html_url": "https://github.com/neurwerk/repo/actions/runs/12"}]}"""

    runs = GitHubClient._parse_runs(response)

    assert runs[0].status == "in_progress"


def test_client_lists_versions_and_checks_workflow_status() -> None:
    versions = """[
      {
        "id": 42,
        "name": "sha256:abc",
        "created_at": "2026-08-12T10:00:00Z",
        "updated_at": "2026-08-12T10:00:00Z",
        "metadata": {"container": {"tags": ["1.2.3"]}}
      }
    ]"""
    no_runs = """{"workflow_runs": []}"""
    queued_run = """{"workflow_runs": [{"id": 7, "status": "queued", "html_url": "https://example.test/7"}]}"""
    responses = [response(versions), response(no_runs), response(queued_run)]
    client = GitHubClient("test-token")
    client._session.get = Mock(side_effect=responses)

    package_versions = client.list_package_versions("example")
    workflow_run = client.get_active_workflow_run("neurwerk/example")

    assert package_versions[0].name == "sha256:abc"
    assert workflow_run is not None
    assert workflow_run.status == "queued"
    client.close()


def test_client_wraps_requests_errors() -> None:
    client = GitHubClient("test-token")
    client._session.get = Mock(side_effect=requests.ConnectionError("offline"))

    with pytest.raises(GitHubApiError, match="GitHub request failed"):
        client.list_package_versions("example")


def test_parse_runs_rejects_invalid_response() -> None:
    with pytest.raises(GitHubApiError, match="invalid workflow-run data"):
        GitHubClient._parse_runs("[]")


def response(text: str) -> Mock:
    mocked_response = Mock()
    mocked_response.text = text
    mocked_response.raise_for_status.return_value = None
    return mocked_response
