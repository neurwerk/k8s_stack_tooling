"""Define application settings and the monitored package inventory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PackageConfigErrorReason = Literal["package", "repository", "pair", "channel", "suffix"]


class InvalidPackageConfigError(ValueError):
    """Indicate that a monitored package violates naming or channel rules."""

    def __init__(self, reason: PackageConfigErrorReason) -> None:
        """Initialize a stable validation message for one configuration field."""
        messages = {
            "package": "package name does not follow the container naming convention",
            "repository": "repository does not follow the source naming convention",
            "pair": "package channel and tag suffix must be configured together",
            "channel": "package channel is invalid",
            "suffix": "package tag suffix must match its channel",
        }
        super().__init__(messages[reason])


@dataclass(frozen=True)
class PackageConfig:
    """Identify a GHCR package and the repository that builds it.

    Args:
        package_name: Organization-level GitHub Container Registry package name.
        repository: GitHub repository in `owner/name` format.
        channel: Optional image channel displayed separately in reports.
        tag_suffix: Tag suffix selecting one channel from a shared package.
    """

    package_name: str
    repository: str
    channel: str | None = None
    tag_suffix: str | None = None

    def __post_init__(self) -> None:
        """Reject package and repository names outside project conventions."""
        if _PACKAGE_NAME.fullmatch(self.package_name) is None:
            raise InvalidPackageConfigError("package")
        if _REPOSITORY.fullmatch(self.repository) is None:
            raise InvalidPackageConfigError("repository")
        if (self.channel is None) != (self.tag_suffix is None):
            raise InvalidPackageConfigError("pair")
        if self.channel is not None and _CHANNEL.fullmatch(self.channel) is None:
            raise InvalidPackageConfigError("channel")
        if self.tag_suffix is not None and self.tag_suffix != f"-{self.channel}":
            raise InvalidPackageConfigError("suffix")


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


_PACKAGE_NAME = re.compile(r"^(?:k8s-stack|addon)-[a-z0-9]+(?:-[a-z0-9]+)*$")
_REPOSITORY = re.compile(r"^neurwerk/(?:k8s_stack_[a-z0-9_]+|addon_dify_ce_builder)$")
_CHANNEL = re.compile(r"^[a-z0-9]+$")

PACKAGES: tuple[PackageConfig, ...] = (
    PackageConfig("k8s-stack-studio-api", "neurwerk/k8s_stack_studio"),
    PackageConfig("k8s-stack-studio-web", "neurwerk/k8s_stack_studio"),
    PackageConfig("k8s-stack-agentgateway-extproc", "neurwerk/k8s_stack_agentgateway_extproc"),
    PackageConfig("k8s-stack-pii-engine", "neurwerk/k8s_stack_pii_engine", "cpu", "-cpu"),
    PackageConfig("k8s-stack-pii-engine", "neurwerk/k8s_stack_pii_engine", "cu124", "-cu124"),
    PackageConfig(
        "k8s-stack-keycloak-api-key-bridge", "neurwerk/k8s_stack_keycloak_api_key_bridge"
    ),
    PackageConfig("addon-dify-ce-builder-api", "neurwerk/addon_dify_ce_builder"),
    PackageConfig("k8s-stack-tooling", "neurwerk/k8s_stack_tooling"),
    PackageConfig("addon-dify-ce-builder-web", "neurwerk/addon_dify_ce_builder"),
)
