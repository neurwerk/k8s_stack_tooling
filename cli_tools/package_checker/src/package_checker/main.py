"""Run the package-checker command-line application."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from package_checker.config import PACKAGES, Settings
from package_checker.github import GitHubApiError, GitHubClient, unique_repositories
from package_checker.models import WorkflowRun
from package_checker.report import (
    PackageReport,
    create_report,
    failed_report,
    render_json,
    render_table,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowCheck:
    """Store the result of checking a repository's active workflow runs.

    Args:
        workflow_run: Active workflow run, or None if no run is active or the check failed.
        failed: Whether GitHub could not complete the workflow-status lookup.
    """

    workflow_run: WorkflowRun | None
    failed: bool


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse package-checker command-line arguments.

    Args:
        arguments: Arguments to parse, excluding the command name. Uses `sys.argv` when None.

    Returns:
        Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Report newest neurwerk GHCR package versions.")
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of an aligned table."
    )
    return parser.parse_args(arguments)


def fetch_active_workflows(client: GitHubClient) -> dict[str, WorkflowCheck]:
    """Fetch active workflow runs once per repository.

    Args:
        client: Authenticated GitHub API client.

    Returns:
        Active run keyed by repository; missing entries indicate a lookup failure.
    """
    workflows: dict[str, WorkflowCheck] = {}
    for repository in unique_repositories(PACKAGES):
        try:
            workflows[repository] = WorkflowCheck(
                workflow_run=client.get_active_workflow_run(repository), failed=False
            )
        except GitHubApiError as error:
            _logger.warning("Could not check active workflow for %s: %s", repository, error)
            workflows[repository] = WorkflowCheck(workflow_run=None, failed=True)
    return workflows


def fetch_reports(client: GitHubClient) -> list[PackageReport]:
    """Fetch every configured package and build its status report.

    Args:
        client: Authenticated GitHub API client.

    Returns:
        One report record for every configured package.
    """
    workflows = fetch_active_workflows(client)
    reports: list[PackageReport] = []
    for package in PACKAGES:
        workflow = workflows[package.repository]
        try:
            versions = client.list_package_versions(package.package_name)
            report = create_report(package, versions, workflow.workflow_run)
            reports.append(
                PackageReport(
                    package=report.package,
                    channel=report.channel,
                    version=report.version,
                    published_at=report.published_at,
                    digest=report.digest,
                    build_status="unknown" if workflow.failed else report.build_status,
                    build_url=report.build_url,
                    error=report.error,
                )
            )
        except GitHubApiError as error:
            reports.append(failed_report(package, error, workflow.workflow_run, workflow.failed))
    return reports


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the CLI and exit nonzero when one or more package requests fail.

    Args:
        arguments: Arguments to parse, excluding the command name. Uses `sys.argv` when None.
    """
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
    args = parse_arguments(arguments)
    try:
        settings = Settings()
    except ValidationError as error:
        _logger.error(  # noqa: TRY400 -- Invalid local configuration is an expected CLI error.
            "Invalid configuration: %s", error.errors()[0]["msg"]
        )
        raise SystemExit(2) from error
    with GitHubClient(settings.github_pat.get_secret_value()) as client:
        reports = fetch_reports(client)
    output = render_json(reports) if args.json else f"{render_table(reports)}\n"
    print(output, end="")  # noqa: T201 -- CLI output is the command's explicit result.
    if any(report.error is not None for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
