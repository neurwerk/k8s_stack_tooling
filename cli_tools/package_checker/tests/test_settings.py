"""Test local package-checker configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from package_checker.config import PACKAGES, PackageConfig, Settings


def test_settings_loads_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_CHECKER_GITHUB_PAT", "ghp_test_token")

    settings = Settings(_env_file=None)

    assert settings.github_pat.get_secret_value() == "ghp_test_token"


def test_settings_rejects_placeholder_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_CHECKER_GITHUB_PAT", "EXAMPLE")

    with pytest.raises(ValidationError, match="valid GitHub credentials"):
        Settings(_env_file=None)


def test_available_packages_are_registered() -> None:
    assert [(package.package_name, package.repository) for package in PACKAGES] == [
        ("k8s-stack-studio-api", "neurwerk/k8s_stack_studio"),
        ("k8s-stack-studio-web", "neurwerk/k8s_stack_studio"),
        ("k8s-stack-agentgateway-extproc", "neurwerk/k8s_stack_agentgateway_extproc"),
        ("k8s-stack-pii-engine", "neurwerk/k8s_stack_pii_engine"),
        ("k8s-stack-pii-engine", "neurwerk/k8s_stack_pii_engine"),
        (
            "k8s-stack-keycloak-api-key-bridge",
            "neurwerk/k8s_stack_keycloak_api_key_bridge",
        ),
        ("k8s-stack-keycloak-theme", "neurwerk/k8s_stack_keycloak_theme"),
        (
            "k8s-stack-opensearch-reporting-cli",
            "neurwerk/k8s_stack_opensearch_reporting_cli",
        ),
        ("k8s-stack-addon-dify-api", "neurwerk/k8s_stack_addon_dify"),
        ("k8s-stack-tooling", "neurwerk/k8s_stack_tooling"),
        ("k8s-stack-addon-dify-web", "neurwerk/k8s_stack_addon_dify"),
    ]


def test_dify_packages_use_standalone_source_not_legacy_builder() -> None:
    dify = [package for package in PACKAGES if "dify" in package.package_name]

    assert {package.package_name for package in dify} == {
        "k8s-stack-addon-dify-api",
        "k8s-stack-addon-dify-web",
    }
    assert {package.repository for package in dify} == {"neurwerk/k8s_stack_addon_dify"}


@pytest.mark.parametrize(
    ("package_name", "repository"),
    [
        ("k8s_stack_invalid", "neurwerk/k8s_stack_invalid"),
        ("k8s-stack-valid", "neurwerk/k8s-stack-invalid"),
    ],
)
def test_package_configuration_rejects_invalid_names(package_name: str, repository: str) -> None:
    with pytest.raises(ValueError, match="naming convention"):
        PackageConfig(package_name, repository)
