"""Tests for SOPS-free OpenBao bootstrap primitives."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fake import FakeSession

from openbao_stack_setup.catalog import ROLE_NAMESPACES
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import BOOTSTRAP_PASSWORDS
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, update_provider
from openbao_stack_setup.reconcile import ReconciliationIdentity
from openbao_stack_setup.recovery import (
    RecoveryKitError,
    load,
    new_kit,
    new_static_seal,
    update,
    with_checkpoint,
    with_initialization_material,
    write_new,
)
from openbao_stack_setup.seed import _secret_operator_policy, seed_bootstrap


def _client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("test CA", encoding="utf-8")
    return OpenBaoClient("https://openbao.example.test:8200", "root", ca, session)


def _providers() -> dict[str, dict[str, str]]:
    return {
        "openrouter": {"openrouterApiKey": "openrouter"},
        "deepseek": {"deepseekApiKey": "deepseek"},
        "brave": {"braveApiKey": "brave"},
        "route53": {"accessKeyId": "access", "secretAccessKey": "secret"},
    }


def _bootstrap_passwords() -> dict[str, str]:
    return {password.key: f"{password.key}-password" for password in BOOTSTRAP_PASSWORDS}


def _identity() -> ReconciliationIdentity:
    return ReconciliationIdentity("client", "cluster", "namespace")


def test_bootstrap_seeds_exact_roles_and_provider_records(tmp_path: Path) -> None:
    session = FakeSession()
    report = seed_bootstrap(
        _client(tmp_path, session), _providers(), {}, _bootstrap_passwords(), _identity()
    )

    assert report.external_records_changed == 5
    assert report.internal_records_changed == 13
    assert "librechat-code-interpreter" in ROLE_NAMESPACES
    assert {"infra-postgres-auth", "infra-postgres-operations"}.issubset(ROLE_NAMESPACES)
    role_calls = [call for call in session.calls if call.path.startswith("auth/kubernetes/role/")]
    assert len(role_calls) == len(ROLE_NAMESPACES) + 1
    assert any(
        call.path == "auth/kubernetes/role/librechat-code-interpreter" for call in role_calls
    )
    policy_calls = [call for call in session.calls if call.path.startswith("sys/policies/acl/")]
    assert len(policy_calls) == len(ROLE_NAMESPACES) + 1
    assert any(call.path == "sys/policies/acl/librechat-code-interpreter" for call in policy_calls)
    for namespace in ("infra-postgres-auth", "infra-postgres-operations"):
        assert any(call.path == f"auth/kubernetes/role/{namespace}" for call in role_calls)
        assert any(call.path == f"sys/policies/acl/{namespace}" for call in policy_calls)


def test_bootstrap_refuses_changed_external_record(tmp_path: Path) -> None:
    session = FakeSession()
    client = _client(tmp_path, session)
    seed_bootstrap(client, _providers(), {}, _bootstrap_passwords(), _identity())
    values = _providers()
    values["openrouter"] = {"openrouterApiKey": "different"}

    with pytest.raises(OpenBaoError, match="external record differs"):
        seed_bootstrap(client, values, {}, {}, _identity())


def test_provider_update_preserves_siblings_and_uses_cas(tmp_path: Path) -> None:
    session = FakeSession()
    client = _client(tmp_path, session)
    seed_bootstrap(client, _providers(), {}, _bootstrap_passwords(), _identity())

    update_provider(client, PROVIDERS["openrouter"], {"openrouterApiKey": "replacement"})

    assert session.secrets["infra-agentgateway/external"].values == {
        "openrouterApiKey": "replacement",
        "deepseekApiKey": "deepseek",
        "braveApiKey": "brave",
    }


def test_smtp_update_replaces_the_managed_record(tmp_path: Path) -> None:
    session = FakeSession()
    client = _client(tmp_path, session)
    seed_bootstrap(
        client,
        _providers(),
        {"username": "old", "password": "old-secret"},
        _bootstrap_passwords(),
        _identity(),
    )

    update_provider(
        client,
        MANAGED_CREDENTIALS["smtp"],
        {"smtpUsername": "new", "smtpPassword": "new-secret"},
    )

    assert session.secrets["auth-keycloak/external"].values == {
        "smtpUsername": "new",
        "smtpPassword": "new-secret",
    }
    assert session.secrets["monitor-kube-prometheus-stack/external"].values == {
        "smtpUsername": "new",
        "smtpPassword": "new-secret",
    }


def test_secret_operator_policy_allows_only_managed_records() -> None:
    assert set(re.findall(r'^path "([^"]+)"', _secret_operator_policy(), re.MULTILINE)) == {
        "secret/data/auth-keycloak/external",
        "secret/metadata/auth-keycloak/external",
        "secret/data/infra-agentgateway/external",
        "secret/metadata/infra-agentgateway/external",
        "secret/data/infra-cert-manager/external",
        "secret/metadata/infra-cert-manager/external",
        "secret/data/monitor-kube-prometheus-stack/external",
        "secret/metadata/monitor-kube-prometheus-stack/external",
        "secret/data/stack-setup/providers/smtp",
        "secret/metadata/stack-setup/providers/smtp",
    }


def test_active_directory_update_preserves_smtp_siblings(tmp_path: Path) -> None:
    session = FakeSession()
    client = _client(tmp_path, session)
    seed_bootstrap(
        client,
        _providers(),
        {"username": "smtp-user", "password": "smtp-password"},
        _bootstrap_passwords(),
        _identity(),
    )

    update_provider(
        client,
        MANAGED_CREDENTIALS["active-directory"],
        {
            "activeDirectoryBindDn": "CN=Keycloak,OU=Services,DC=example,DC=com",
            "activeDirectoryBindCredential": "bind-password",
        },
    )

    assert session.secrets["auth-keycloak/external"].values == {
        "smtpUsername": "smtp-user",
        "smtpPassword": "smtp-password",
        "activeDirectoryBindDn": "CN=Keycloak,OU=Services,DC=example,DC=com",
        "activeDirectoryBindCredential": "bind-password",
    }


def test_recovery_kit_is_private_atomic_and_bound_to_checkpoints(tmp_path: Path) -> None:
    key, key_id = new_static_seal()
    kit = new_kit(
        "client",
        "cluster",
        "namespace",
        key,
        key_id,
        ("One", "Two", "Three"),
        ("fingerprint-1", "fingerprint-2", "fingerprint-3"),
        ("public-1", "public-2", "public-3"),
        ("private-1", "private-2", "private-3"),
    )
    path = tmp_path / "recovery.json"
    write_new(path, kit)

    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RecoveryKitError, match="already exists"):
        write_new(path, kit)
    staged = with_initialization_material(load(path), "root", ("one", "two", "three"))
    update(path, staged)
    initialized = with_checkpoint(load(path), "initialized")
    update(path, initialized)
    seeded = with_checkpoint(load(path), "seeded")
    update(path, seeded)
    complete = with_checkpoint(load(path), "complete")
    update(path, complete)
    assert load(path).checkpoint == "complete"
