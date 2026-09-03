from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fake import FakeSession, StoredSecret

from openbao_stack_setup.catalog import RECONCILIATION_STATE_PATH, ROLE_NAMESPACES
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import BOOTSTRAP_PASSWORDS
from openbao_stack_setup.reconcile import (
    CURRENT_RECONCILIATION_VERSION,
    ReconciliationIdentity,
    reconcile_openbao,
)


def client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "root", ca, session)


def identity() -> ReconciliationIdentity:
    return ReconciliationIdentity("client", "cluster", "namespace")


def passwords() -> dict[str, str]:
    return {password.key: f"{password.key}-password" for password in BOOTSTRAP_PASSWORDS}


def source(session: FakeSession) -> None:
    session.secrets["auth-keycloak/external"] = StoredSecret(
        {"smtpUsername": "smtp-user", "smtpPassword": "smtp-password"}
    )


def state_values(
    *,
    applied: int = 4,
    client_name: str = "client",
    package_version: str = "0.2.11",
) -> dict[str, JsonValue]:
    return {
        "schemaVersion": 1,
        "appliedVersion": applied,
        "client": client_name,
        "clusterId": "cluster",
        "namespaceUid": "namespace",
        "packageVersion": package_version,
    }


def test_reconcile_applies_catalog_and_cluster_bound_state(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)

    report = reconcile_openbao(client(tmp_path, session), identity(), passwords())

    assert report.previous_version == 0
    assert report.applied_version == CURRENT_RECONCILIATION_VERSION
    assert report.replicated_records == 2
    assert session.secrets["stack-setup/providers/smtp"].values == {
        "smtpUsername": "smtp-user",
        "smtpPassword": "smtp-password",
    }
    assert session.secrets["monitor-kube-prometheus-stack/external"].values == {
        "smtpUsername": "smtp-user",
        "smtpPassword": "smtp-password",
    }
    assert session.secrets[RECONCILIATION_STATE_PATH].values == state_values()
    role_calls = [call for call in session.calls if call.path.startswith("auth/kubernetes/role/")]
    assert len(role_calls) == len(ROLE_NAMESPACES) + 1


def test_reconcile_is_idempotent_and_preserves_state_cas_version(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    state_version = session.secrets[RECONCILIATION_STATE_PATH].version

    report = reconcile_openbao(api, identity())

    assert report.previous_version == CURRENT_RECONCILIATION_VERSION
    assert report.replicated_records == 0
    assert report.internal_fields_added == 0
    assert session.secrets[RECONCILIATION_STATE_PATH].version == state_version


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"schemaVersion": 1}, "invalid schema"),
        (state_values(applied=5), "newer"),
        (state_values(client_name="other"), "does not belong"),
    ],
)
def test_reconcile_rejects_invalid_or_unbound_state(
    tmp_path: Path, values: dict[str, JsonValue], message: str
) -> None:
    session = FakeSession()
    source(session)
    session.secrets[RECONCILIATION_STATE_PATH] = StoredSecret(values)

    with pytest.raises(OpenBaoError, match=message):
        reconcile_openbao(client(tmp_path, session), identity(), passwords())


def test_reconcile_fails_closed_on_missing_password_or_conflicting_replica(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)

    with pytest.raises(OpenBaoError, match="missing a bootstrap password"):
        reconcile_openbao(api, identity())
    assert RECONCILIATION_STATE_PATH not in session.secrets

    reconcile_openbao(api, identity(), passwords())
    del session.secrets[RECONCILIATION_STATE_PATH]
    session.secrets["monitor-kube-prometheus-stack/external"] = StoredSecret(
        {"smtpUsername": "other", "smtpPassword": "other-password"}
    )
    with pytest.raises(OpenBaoError, match="conflicts"):
        reconcile_openbao(api, identity())
    assert RECONCILIATION_STATE_PATH not in session.secrets


def test_postgres_conflict_does_not_advance_state_and_retry_converges(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)
    session.secrets[RECONCILIATION_STATE_PATH] = StoredSecret(state_values(applied=2))
    session.secrets["infra-postgres-auth/internal"] = StoredSecret(
        {"adminPassword": "postgres-admin", "keycloakPassword": "conflict"}
    )
    api = client(tmp_path, session)

    with pytest.raises(OpenBaoError, match="credential mismatch"):
        reconcile_openbao(api, identity(), passwords())

    assert session.secrets[RECONCILIATION_STATE_PATH].values == state_values(applied=2)
    canonical = session.secrets["auth-keycloak/internal"].values["dbPassword"]
    session.secrets["infra-postgres-auth/internal"].values["keycloakPassword"] = canonical

    report = reconcile_openbao(api, identity())

    assert report.previous_version == 2
    assert report.applied_version == CURRENT_RECONCILIATION_VERSION
    assert session.secrets[RECONCILIATION_STATE_PATH].values == state_values()
    assert session.secrets["infra-postgres-auth/internal"].values == {
        "adminPassword": "postgres-admin",
        "keycloakPassword": canonical,
    }


def test_schema_4_agentgateway_conflict_does_not_advance_and_retry_converges(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    previous_state = state_values(applied=3, package_version="0.2.10")
    session.secrets[RECONCILIATION_STATE_PATH].values = previous_state
    del session.secrets["infra-agentgateway/internal"]
    operations = session.secrets["infra-postgres-operations/internal"].values
    operations["agentgatewayPassword"] = "conflict"

    with pytest.raises(
        OpenBaoError,
        match=r"credential mismatch.*infra-postgres-operations/internal/agentgatewayPassword",
    ):
        reconcile_openbao(api, identity())

    assert session.secrets[RECONCILIATION_STATE_PATH].values == previous_state
    canonical = session.secrets["infra-agentgateway/internal"].values["postgresqlPassword"]
    operations["agentgatewayPassword"] = canonical

    report = reconcile_openbao(api, identity())

    assert report.previous_version == 3
    assert report.applied_version == CURRENT_RECONCILIATION_VERSION
    assert session.secrets[RECONCILIATION_STATE_PATH].values == state_values()
    assert operations["agentgatewayPassword"] == canonical


def test_schema_3_adds_only_agentgateway_database_credentials(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    session.secrets[RECONCILIATION_STATE_PATH].values = state_values(
        applied=3, package_version="0.2.10"
    )
    del session.secrets["infra-agentgateway/internal"]
    del session.secrets["infra-postgres-operations/internal"].values["agentgatewayPassword"]
    session.secrets["frontend-studio/internal"].values.update(
        langfusePublicKey="legacy-public", langfuseSecretKey="legacy-secret"
    )
    previous = {
        path: deepcopy(secret.values)
        for path, secret in session.secrets.items()
        if path != RECONCILIATION_STATE_PATH
    }

    report = reconcile_openbao(api, identity())

    canonical = session.secrets["infra-agentgateway/internal"].values["postgresqlPassword"]
    assert report.previous_version == 3
    assert (
        session.secrets["infra-postgres-operations/internal"].values["agentgatewayPassword"]
        == canonical
    )
    for path, values in previous.items():
        for field, value in values.items():
            assert session.secrets[path].values[field] == value
    assert reconcile_openbao(api, identity()).internal_fields_added == 0


def test_schema_2_librechat_credentials_migrate_exactly_once(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    session.secrets[RECONCILIATION_STATE_PATH].values = state_values(applied=2)
    librechat = session.secrets["frontend-librechat/internal"].values
    librechat.update(
        {
            "ragPostgresqlUser": "librechat",
            "ferretdbPassword": "retired-password",
            "ferretdbUser": "librechat",
            "weaviateApiKey": "retired-api-key",
        }
    )
    del session.secrets["infra-postgres-auth/internal"]
    del session.secrets["infra-postgres-operations/internal"]

    report = reconcile_openbao(api, identity())

    assert report.previous_version == 2
    migrated = dict(session.secrets["frontend-librechat/internal"].values)
    assert migrated["ragPostgresqlUser"] == "librechat_rag"
    assert "ferretdbPassword" not in migrated
    assert "ferretdbUser" not in migrated
    assert "weaviateApiKey" not in migrated
    reconcile_openbao(api, identity())
    assert session.secrets["frontend-librechat/internal"].values == migrated


def test_schema_2_librechat_username_migration_fails_closed(tmp_path: Path) -> None:
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    session.secrets[RECONCILIATION_STATE_PATH].values = state_values(applied=2)
    session.secrets["frontend-librechat/internal"].values["ragPostgresqlUser"] = "other"

    with pytest.raises(OpenBaoError, match=r"credential mismatch.*ragPostgresqlUser"):
        reconcile_openbao(api, identity())

    assert session.secrets[RECONCILIATION_STATE_PATH].values == state_values(applied=2)


def test_reconcile_preserves_complete_active_directory_siblings(tmp_path: Path) -> None:
    session = FakeSession()
    session.secrets["auth-keycloak/external"] = StoredSecret(
        {
            "smtpUsername": "smtp-user",
            "smtpPassword": "smtp-password",
            "activeDirectoryBindDn": "CN=Keycloak,OU=Services,DC=example,DC=com",
            "activeDirectoryBindCredential": "bind-password",
        }
    )

    reconcile_openbao(client(tmp_path, session), identity(), passwords())

    assert (
        session.secrets["auth-keycloak/external"].values["activeDirectoryBindCredential"]
        == "bind-password"
    )


@pytest.mark.parametrize(
    "extra_values",
    [
        {"activeDirectoryBindDn": "bind-dn"},
        {"unknown": "value"},
    ],
)
def test_reconcile_rejects_partial_or_unknown_siblings(
    tmp_path: Path, extra_values: dict[str, str]
) -> None:
    session = FakeSession()
    session.secrets["auth-keycloak/external"] = StoredSecret(
        {
            "smtpUsername": "smtp-user",
            "smtpPassword": "smtp-password",
            **extra_values,
        }
    )

    with pytest.raises(OpenBaoError, match=r"field set|sibling"):
        reconcile_openbao(client(tmp_path, session), identity(), passwords())
