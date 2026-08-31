"""Call GitHub Packages and Actions REST APIs."""

from __future__ import annotations

import requests
from pydantic import TypeAdapter, ValidationError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from package_checker.config import PackageConfig
from package_checker.models import PackageVersion, WorkflowRun, WorkflowRunList

_API_URL = "https://api.github.com"
_TIMEOUT_SECONDS = 20
_VERSIONS_ADAPTER: TypeAdapter[list[PackageVersion]] = TypeAdapter(list[PackageVersion])


class GitHubApiError(Exception):
    """Indicate that GitHub could not provide the requested API data."""

    def __init__(self, message: str) -> None:
        """Initialize the API error with a safe user-facing message.

        Args:
            message: Error description that does not contain credentials.
        """
        super().__init__(message)


class GitHubClient:
    """Retrieve package versions and active GitHub Actions runs.

    Args:
        token: GitHub personal access token used for every API request.
    """

    def __init__(self, token: str) -> None:
        """Initialize an authenticated, retrying GitHub HTTP session.

        Args:
            token: GitHub personal access token used for every API request.
        """
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)

    def close(self) -> None:
        """Close the HTTP session used for GitHub requests."""
        self._session.close()

    def __enter__(self) -> GitHubClient:
        """Open the client as a context manager.

        Returns:
            This open client.
        """
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Close the session after context-manager use.

        Args:
            exc_type: Exception type raised in the context, if any.
            exc_value: Exception value raised in the context, if any.
            traceback: Exception traceback raised in the context, if any.
        """
        self.close()

    def list_package_versions(self, package_name: str) -> list[PackageVersion]:
        """List active container versions for one neurwerk package.

        Args:
            package_name: GHCR package name without the organization prefix.

        Returns:
            Package versions reported by GitHub.

        Raises:
            GitHubApiError: If GitHub rejects the request or returns invalid data.
        """
        path = (
            f"/orgs/neurwerk/packages/container/{package_name}/versions?state=active&per_page=100"
        )
        return self._parse_versions(self._get(path), package_name)

    def get_active_workflow_run(self, repository: str) -> WorkflowRun | None:
        """Return the first queued or in-progress workflow run for a repository.

        Args:
            repository: GitHub repository in `owner/name` format.

        Returns:
            The active workflow run, or None when no build is active.

        Raises:
            GitHubApiError: If GitHub rejects the request or returns invalid data.
        """
        for status in ("in_progress", "queued"):
            path = f"/repos/{repository}/actions/runs?status={status}&per_page=1"
            runs = self._parse_runs(self._get(path))
            if runs:
                return runs[0]
        return None

    def _get(self, path: str) -> str:
        """Request a GitHub API path and return its response body.

        Args:
            path: API path beginning with a slash.

        Returns:
            Successful response body.

        Raises:
            GitHubApiError: If the request fails or GitHub returns an error status.
        """
        try:
            response = self._session.get(f"{_API_URL}{path}", timeout=_TIMEOUT_SECONDS)
            response.raise_for_status()
        except requests.RequestException as error:
            raise GitHubApiError(_request_error_message(path, error)) from error
        return response.text

    @staticmethod
    def _parse_versions(response_text: str, package_name: str) -> list[PackageVersion]:
        """Validate the version-list payload returned by GitHub.

        Args:
            response_text: Raw JSON response body.
            package_name: Package associated with the response for error reporting.

        Returns:
            Validated package versions.

        Raises:
            GitHubApiError: If the payload does not match GitHub's expected schema.
        """
        try:
            return _VERSIONS_ADAPTER.validate_json(response_text)
        except ValidationError as error:
            raise GitHubApiError(_invalid_version_message(package_name)) from error

    @staticmethod
    def _parse_runs(response_text: str) -> list[WorkflowRun]:
        """Validate the workflow-run payload returned by GitHub.

        Args:
            response_text: Raw JSON response body.

        Returns:
            Validated workflow runs.

        Raises:
            GitHubApiError: If the payload does not match GitHub's expected schema.
        """
        try:
            return WorkflowRunList.model_validate_json(response_text).workflow_runs
        except ValidationError as error:
            raise GitHubApiError(_INVALID_WORKFLOW_MESSAGE) from error


def _request_error_message(path: str, error: requests.RequestException) -> str:
    """Build a safe error message for a failed GitHub request.

    Args:
        path: GitHub API path that failed.
        error: Underlying requests error.

    Returns:
        Error description without authentication headers.
    """
    return f"GitHub request failed for {path}: {error}"


def _invalid_version_message(package_name: str) -> str:
    """Build an error message for malformed package-version data.

    Args:
        package_name: Package whose payload was invalid.

    Returns:
        Error description for the package.
    """
    return f"GitHub returned invalid version data for {package_name}"


_INVALID_WORKFLOW_MESSAGE = "GitHub returned invalid workflow-run data"


def unique_repositories(packages: tuple[PackageConfig, ...]) -> list[str]:
    """Yield repository names once while preserving their configured order.

    Args:
        packages: Package configurations exposing a `repository` attribute.

    Returns:
        Distinct repository names in their configured order.
    """
    seen: set[str] = set()
    repositories: list[str] = []
    for package in packages:
        if package.repository in seen:
            continue
        seen.add(package.repository)
        repositories.append(package.repository)
    return repositories
