"""Define application settings and the monitored package inventory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PackageConfigErrorReason = Literal["package", "repository"]


class InvalidPackageConfigError(ValueError):
    """Indicate that a monitored package violates naming rules."""

    def __init__(self, reason: PackageConfigErrorReason) -> None:
        """Initialize a stable validation message for one configuration field."""
        messages = {
            "package": "package name does not follow the container naming convention",
            "repository": "repository does not follow the source naming convention",
        }
        super().__init__(messages[reason])


@dataclass(frozen=True)
class PackageConfig:
    """Identify a GHCR package and the repository that builds it.

    Args:
        package_name: Organization-level GitHub Container Registry package name.
        repository: GitHub repository in `owner/name` format.
        stable_tags_only: Require full plain X.Y.Z tags without fallback aliases.
    """

    package_name: str
    repository: str
    stable_tags_only: bool = False

    def __post_init__(self) -> None:
        """Reject package and repository names outside project conventions."""
        if _PACKAGE_NAME.fullmatch(self.package_name) is None:
            raise InvalidPackageConfigError("package")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise InvalidPackageConfigError("repository")


class InvalidGitHubCredentialsError(ValueError):
    """Indicate that the configured GitHub credential is empty or a placeholder."""

    def __init__(self) -> None:
        """Initialize the actionable configuration error message."""
        super().__init__("Set PACKAGE_CHECKER_GITHUB_PAT in .env to valid GitHub credentials.")


class Settings(BaseSettings):
    """Load the GitHub credential from the local environment file.

    Args:
        github_pat: Personal access token with package and Actions read access.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PACKAGE_CHECKER_",
        extra="ignore",
    )

    github_pat: SecretStr

    @field_validator("github_pat")
    @classmethod
    def reject_placeholder_token(cls, value: SecretStr) -> SecretStr:
        """Reject the committed placeholder before an HTTP request is made.

        Args:
            value: Token loaded from `PACKAGE_CHECKER_GITHUB_PAT`.

        Returns:
            The validated token.

        Raises:
            ValueError: If the token is the placeholder or empty.
        """
        if value.get_secret_value() in {"", "EXAMPLE"}:
            raise InvalidGitHubCredentialsError
        return value


_PACKAGE_NAME = re.compile(r"^k8s-stack-[a-z0-9]+(?:-[a-z0-9]+)*$")
_REPOSITORY = re.compile(r"^neurwerk/k8s_stack_[a-z0-9_]+$")


def load_inventory(path: Path) -> tuple[PackageConfig, ...]:
    """Load a bounded client-owned JSON list, without a compiled package selection."""
    with path.open("rb") as source:
        raw = source.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("Package inventory exceeds 1 MiB")
    entries = json.loads(raw)
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("Package inventory must be a list of at most 1000 packages")
    packages = []
    names: set[str] = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not {"package_name", "repository"} <= entry.keys()
            or entry.keys() - {"package_name", "repository", "stable_tags_only"}
            or not isinstance(entry["package_name"], str)
            or not isinstance(entry["repository"], str)
            or not isinstance(entry.get("stable_tags_only", False), bool)
        ):
            raise ValueError("Invalid package inventory entry")
        package = PackageConfig(**entry)
        if package.package_name in names:
            raise ValueError("Duplicate package in inventory")
        names.add(package.package_name)
        packages.append(package)
    return tuple(packages)
