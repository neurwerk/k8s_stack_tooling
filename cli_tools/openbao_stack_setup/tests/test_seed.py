from __future__ import annotations

from pathlib import Path

import pytest
from fake import FakeSession

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import BOOTSTRAP_PASSWORDS
from openbao_stack_setup.reconcile import ReconciliationIdentity
from openbao_stack_setup.seed import seed_bootstrap


def client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "root", ca, session)


def providers() -> dict[str, dict[str, str]]:
    return {
        "openrouter": {"openrouterApiKey": "openrouter"},
        "deepseek": {"deepseekApiKey": "deepseek"},
        "brave": {"braveApiKey": "brave"},
        "route53": {"accessKeyId": "access", "secretAccessKey": "secret"},
    }


def bootstrap_passwords() -> dict[str, str]:
    return {password.key: f"{password.key}-password" for password in BOOTSTRAP_PASSWORDS}


def identity() -> ReconciliationIdentity:
    return ReconciliationIdentity("client", "cluster", "namespace")


def test_seed_requires_complete_provider_set(tmp_path: Path) -> None:
    with pytest.raises(OpenBaoError, match="provider set is incomplete"):
        seed_bootstrap(client(tmp_path, FakeSession()), {}, {}, {}, identity())


def test_seed_rejects_invalid_provider_fields(tmp_path: Path) -> None:
    values = providers()
    values["brave"] = {"braveApiKey": ""}
    with pytest.raises(OpenBaoError, match="invalid for brave"):
        seed_bootstrap(client(tmp_path, FakeSession()), values, {}, {}, identity())


def test_seed_accepts_paired_smtp_credentials(tmp_path: Path) -> None:
    session = FakeSession()
    report = seed_bootstrap(
        client(tmp_path, session),
        providers(),
        {"username": "user", "password": "password"},
        bootstrap_passwords(),
        identity(),
    )
    assert report.external_records_changed == 5
    assert report.internal_records_changed == 13
    assert session.secrets["stack-setup/providers/smtp"].values == {
        "smtpUsername": "user",
        "smtpPassword": "password",
    }
    assert session.secrets["auth-keycloak/external"].values == {
        "smtpUsername": "user",
        "smtpPassword": "password",
    }
    assert session.secrets["monitor-kube-prometheus-stack/external"].values == {
        "smtpUsername": "user",
        "smtpPassword": "password",
    }
    assert (
        session.secrets["infra-postgres-auth/internal"].values["keycloakPassword"]
        == (session.secrets["auth-keycloak/internal"].values["dbPassword"])
    )
    assert session.secrets["infra-postgres-operations/internal"].values == {
        "adminPassword": session.secrets["infra-postgres-operations/internal"].values[
            "adminPassword"
        ],
        "documentdbPassword": session.secrets["frontend-librechat/internal"].values[
            "documentdbPassword"
        ],
        "difyPassword": session.secrets["frontend-dify/internal"].values["postgresPassword"],
        "langfusePassword": session.secrets["monitor-langfuse/internal"].values[
            "postgresqlPassword"
        ],
        "librechatRagPassword": session.secrets["frontend-librechat/internal"].values[
            "ragPostgresqlPassword"
        ],
    }
    reconciled = seed_bootstrap(
        client(tmp_path, session),
        providers(),
        {"username": "user", "password": "password"},
        {},
        identity(),
    )
    assert reconciled.external_records_changed == 0


def test_seed_merges_active_directory_with_smtp_siblings(tmp_path: Path) -> None:
    session = FakeSession()
    active_directory = {
        "activeDirectoryBindDn": "CN=Keycloak,OU=Services,DC=example,DC=com",
        "activeDirectoryBindCredential": "bind-password",
    }

    report = seed_bootstrap(
        client(tmp_path, session),
        providers(),
        {"username": "smtp-user", "password": "smtp-password"},
        bootstrap_passwords(),
        identity(),
        active_directory=active_directory,
    )

    assert report.external_records_changed == 5
    assert "stack-setup/providers/active-directory" not in session.secrets
    assert session.secrets["auth-keycloak/external"].values == {
        "smtpUsername": "smtp-user",
        "smtpPassword": "smtp-password",
        **active_directory,
    }


def test_seed_rejects_partial_active_directory_credentials(tmp_path: Path) -> None:
    with pytest.raises(OpenBaoError, match="Active Directory credentials"):
        seed_bootstrap(
            client(tmp_path, FakeSession()),
            providers(),
            {},
            bootstrap_passwords(),
            identity(),
            active_directory={"activeDirectoryBindDn": "bind-dn"},
        )
