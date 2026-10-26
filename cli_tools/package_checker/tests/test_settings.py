"""Test local package-checker configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from package_checker.config import PackageConfig, Settings, load_inventory


def test_settings_loads_token_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_CHECKER_GITHUB_PAT", "ghp_test_token")

    settings = Settings(_env_file=None)

    assert settings.github_pat.get_secret_value() == "ghp_test_token"


def test_settings_rejects_placeholder_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKAGE_CHECKER_GITHUB_PAT", "EXAMPLE")

    with pytest.raises(ValidationError, match="valid GitHub credentials"):
        Settings(_env_file=None)


def test_empty_inventory_selects_no_packages(tmp_path) -> None:
    path = tmp_path / "packages.json"
    path.write_text("[]")
    assert load_inventory(path) == ()


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
