"""Protect the pre-import environment gate; native connection checks run in the image."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "native_guard", Path(__file__).parent / "glitchtip_native/guard.py"
)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def environment():
    return dict(guard.REQUIRED_ENV) | {
        "GLITCHTIP_NATIVE_TEST_DB_HOST": "disposable-pg",
        "GLITCHTIP_NATIVE_TEST_DB_NAME": "glitchtip_provisioning_test_synthetic",
        "GLITCHTIP_NATIVE_TEST_DB_USER": "synthetic",
        "GLITCHTIP_NATIVE_TEST_DB_PORT": "5432",
        "DATABASE_URL": "postgresql://synthetic:synthetic-password@disposable-pg:5432/glitchtip_provisioning_test_synthetic",
        "GLITCHTIP_URL": "https://native-test.example.invalid",
        "SECRET_KEY": "synthetic-only",
    }


def test_explicit_isolated_environment_is_accepted_without_importing_django():
    expected = guard.validate_environment(environment())
    assert expected == {
        "HOST": "disposable-pg",
        "NAME": "glitchtip_provisioning_test_synthetic",
        "USER": "synthetic",
        "PORT": "5432",
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("MAINTENANCE_DATABASE_URL", "postgresql://sensitive-other-target"),
        ("READ_ONLY_DATABASE_URL", "postgresql://sensitive-other-target"),
        ("DATABASE_HOST", "sensitive-other-target"),
        ("PGSERVICE", "sensitive-other-target"),
        ("VALKEY_URL", "redis://sensitive-other-target"),
        ("VALKEY_HOST", "sensitive-other-target"),
        ("TESTING", "true"),
        ("IS_WORKER", "true"),
        ("GLITCHTIP_EMBED_WORKER", "true"),
        ("GLITCHTIP_ENABLE_UPTIME", "false"),
        ("VTASKS_BACKEND", "immediate"),
        ("OPENBAO_TOKEN", "sensitive-test-token"),
        ("OIDC_CLIENT_SECRET", "sensitive-test-secret"),
        ("GLITCHTIP_OWNER_PASSWORD", "sensitive-test-password"),
        ("EMAIL_URL", "smtp://sensitive-other-target"),
        ("GLITCHTIP_NATIVE_TEST_DB_HOST", "sensitive-other-target"),
    ],
)
def test_unsafe_environment_fails_before_any_connection_and_redacts_values(key, value):
    config = environment() | {key: value}
    with pytest.raises(guard.GuardError) as error:
        guard.validate_environment(config)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "key", ["VALKEY_URL", "REDIS_URL", "GLITCHTIP_NATIVE_TEST_DB_HOST", "TESTING"]
)
def test_implicit_defaults_are_not_accepted(key):
    config = environment()
    del config[key]
    with pytest.raises(guard.GuardError):
        guard.validate_environment(config)
