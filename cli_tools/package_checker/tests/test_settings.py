"""Test local package-checker configuration validation."""

from __future__ import annotations

import re
from pathlib import Path

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
        ("addon-dify-ce-builder-api", "neurwerk/dify_ce_builder"),
        ("k8s-stack-tooling", "neurwerk/k8s_stack_tooling"),
        ("addon-dify-ce-builder-web", "neurwerk/dify_ce_builder"),
    ]


def test_package_inventory_matches_base_chart_references() -> None:
    workspace = next(
        parent for parent in Path(__file__).resolve().parents if (parent / "base/charts").is_dir()
    )
    pattern = re.compile(r"ghcr\.io/neurwerk/([a-z0-9-]+):")
    referenced = {
        match.group(1)
        for values_file in (workspace / "base/charts").rglob("values.yaml")
        for match in pattern.finditer(values_file.read_text(encoding="utf-8"))
    }
    configured = {package.package_name for package in PACKAGES}

    assert configured == referenced


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
