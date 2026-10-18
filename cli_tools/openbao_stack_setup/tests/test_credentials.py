from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fake import FakeSession, StoredSecret

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import (
    plan_bootstrap_passwords,
    reconcile_internal_credentials,
)


def client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "root", ca, session)


def test_internal_credentials_are_complete_and_idempotent(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)

    base_passwords = plan_bootstrap_passwords(api)
    assert "dify" not in base_passwords
    reconcile_internal_credentials(api, base_passwords)
    assert "frontend-dify/internal" not in session.secrets
    assert "frontend-librechat/internal" not in session.secrets
    assert "librechat-code-interpreter/internal" not in session.secrets
    assert "librechatOidcClientSecret" not in session.secrets["auth-keycloak/internal"].values
    assert "difyOidcClientSecret" not in session.secrets["auth-keycloak/internal"].values
    assert "difyAgentgatewayClientSecret" not in session.secrets["auth-keycloak/internal"].values
    assert (
        "difyAgentgatewayPrimaryVerifierSha256"
        not in session.secrets["auth-keycloak-api-key-bridge/internal"].values
    )
    assert (
        "difyAgentgatewaySecondaryVerifierSha256"
        not in session.secrets["auth-keycloak-api-key-bridge/internal"].values
    )
    assert "difyPassword" not in session.secrets["infra-postgres-operations/internal"].values

    planned = plan_bootstrap_passwords(api, addons=("dify",))
    first = reconcile_internal_credentials(api, planned, addons=("dify", "librechat"))
    original = {path: dict(record.values) for path, record in session.secrets.items()}
    second = reconcile_internal_credentials(api, {})

    assert set(first.changed_paths) == {
        "frontend-dify/internal",
        "auth-keycloak/internal",
        "auth-keycloak-api-key-bridge/internal",
        "infra-postgres-operations/internal",
        "frontend-librechat/internal",
    }
    assert first.added_fields > 0
    assert set(planned) == {"dify"}
    assert second.changed_paths == ()
    assert second.added_fields == 0
    assert {path: record.values for path, record in session.secrets.items()} == original

    dify = original["frontend-dify/internal"]
    assert all(
        dify[field]
        for field in ("difyAgentApiToken", "difyAgentServerSecretKey", "difyAgentSandboxAuthToken")
    )
    assert (
        len(
            {
                dify[field]
                for field in (
                    "difyAgentApiToken",
                    "difyAgentServerSecretKey",
                    "difyAgentSandboxAuthToken",
                )
            }
        )
        == 3
    )

    librechat = original["frontend-librechat/internal"]
    assert {
        "valkeyPassword",
        "documentdbUser",
        "documentdbPassword",
        "adminPanelSessionSecret",
        "adminPanelMetricsSecret",
    }.issubset(librechat)
    assert librechat["documentdbUser"] == "librechat"
    assert "librechat-code-interpreter/internal" not in original
    assert not {"ragPostgresqlPassword", "codeInterpreterJwtPrivateKey"} & librechat.keys()
    bridge = original["auth-keycloak-api-key-bridge/internal"]
    dify_key = dify["agentgatewayApiKey"]
    assert isinstance(dify_key, str)
    assert (
        bridge["difyAgentgatewayPrimaryVerifierSha256"]
        == hashlib.sha256(dify_key.encode()).hexdigest()
    )
    assert bridge["difyAgentgatewaySecondaryVerifierSha256"] == ""
    assert isinstance(bridge["postgresqlPassword"], str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", bridge["postgresqlPassword"])
    agentgateway = original["infra-agentgateway/internal"]
    assert set(agentgateway) == {"postgresqlPassword"}
    agentgateway_password = agentgateway["postgresqlPassword"]
    assert isinstance(agentgateway_password, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", agentgateway_password)
    opensearch = original["monitor-opensearch/internal"]
    assert set(opensearch) == {
        "adminPassword",
        "dashboardCookieSecret",
        "dashboardPassword",
        "fluentBitPassword",
        "provisionerPassword",
        "queryDatasourceEncryptionKey",
        "reportReaderPassword",
        "studioPassword",
    }
    query_encryption_key = opensearch["queryDatasourceEncryptionKey"]
    assert isinstance(query_encryption_key, str)
    assert len(query_encryption_key) == 32
    studio = original["frontend-studio/internal"]
    assert set(studio) == {
        "opensearchPassword",
        "postgresqlPassword",
        "llmLogsPublicKey",
        "llmLogsSecretKey",
    }
    langfuse = original["monitor-langfuse/internal"]
    assert studio["llmLogsPublicKey"] == langfuse["initProjectPublicKey"]
    assert studio["llmLogsSecretKey"] == langfuse["initProjectSecretKey"]
    assert isinstance(studio["postgresqlPassword"], str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", studio["postgresqlPassword"])
    postgres_auth = original["infra-postgres-auth/internal"]
    postgres_operations = original["infra-postgres-operations/internal"]
    assert set(postgres_auth) == {"adminPassword", "keycloakPassword"}
    assert set(postgres_operations) == {
        "adminPassword",
        "agentgatewayPassword",
        "apiKeyBridgePassword",
        "studioPassword",
        "difyPassword",
        "langfusePassword",
        "documentdbPassword",
    }
    assert postgres_auth["adminPassword"]
    assert postgres_operations["adminPassword"]
    assert postgres_operations["agentgatewayPassword"] == agentgateway_password
    assert postgres_operations["apiKeyBridgePassword"] == bridge["postgresqlPassword"]
    assert postgres_operations["studioPassword"] == studio["postgresqlPassword"]
    assert postgres_auth["keycloakPassword"] == original["auth-keycloak/internal"]["dbPassword"]
    assert postgres_operations["documentdbPassword"] == librechat["documentdbPassword"]
    assert (
        postgres_operations["difyPassword"]
        == original["frontend-dify/internal"]["postgresPassword"]
    )
    assert (
        postgres_operations["langfusePassword"]
        == original["monitor-langfuse/internal"]["postgresqlPassword"]
    )


def test_internal_fixed_value_mismatch_is_rejected(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets["frontend-studio/internal"].values["opensearchPassword"] = "wrong"

    with pytest.raises(OpenBaoError, match="Internal credential mismatch"):
        reconcile_internal_credentials(api, {})
    session.secrets["frontend-studio/internal"].values["opensearchPassword"] = session.secrets[
        "monitor-opensearch/internal"
    ].values["studioPassword"]
    session.secrets["frontend-studio/internal"].values["llmLogsPublicKey"] = "stale"
    with pytest.raises(OpenBaoError, match="Internal credential mismatch"):
        reconcile_internal_credentials(api, {})


def test_invalid_opensearch_query_encryption_key_is_rejected_without_rotation(
    tmp_path: Path,
) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets["monitor-opensearch/internal"].values["queryDatasourceEncryptionKey"] = (
        "too-short"
    )

    with pytest.raises(OpenBaoError, match="must be 32 characters"):
        reconcile_internal_credentials(api, {})

    assert (
        session.secrets["monitor-opensearch/internal"].values["queryDatasourceEncryptionKey"]
        == "too-short"
    )


def test_existing_studio_langfuse_fields_are_preserved(tmp_path: Path) -> None:
    session = FakeSession()
    session.secrets["frontend-studio/internal"] = StoredSecret(
        {
            "langfusePublicKey": "legacy-public-key",
            "langfuseSecretKey": "legacy-secret-key",
        }
    )
    api = client(tmp_path, session)

    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))

    assert session.secrets["frontend-studio/internal"].values == {
        "langfusePublicKey": "legacy-public-key",
        "langfuseSecretKey": "legacy-secret-key",
        "llmLogsPublicKey": session.secrets["monitor-langfuse/internal"].values[
            "initProjectPublicKey"
        ],
        "llmLogsSecretKey": session.secrets["monitor-langfuse/internal"].values[
            "initProjectSecretKey"
        ],
        "opensearchPassword": session.secrets["monitor-opensearch/internal"].values[
            "studioPassword"
        ],
        "postgresqlPassword": session.secrets["infra-postgres-operations/internal"].values[
            "studioPassword"
        ],
    }


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("infra-postgres-auth/internal", "keycloakPassword"),
        ("infra-postgres-operations/internal", "agentgatewayPassword"),
        ("infra-postgres-operations/internal", "apiKeyBridgePassword"),
        ("infra-postgres-operations/internal", "studioPassword"),
        ("infra-postgres-operations/internal", "documentdbPassword"),
        ("infra-postgres-operations/internal", "difyPassword"),
        ("infra-postgres-operations/internal", "langfusePassword"),
    ],
)
def test_postgres_consumer_conflicts_are_rejected(tmp_path: Path, path: str, field: str) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(
        api, plan_bootstrap_passwords(api, addons=("dify",)), addons=("dify", "librechat")
    )
    session.secrets[path].values[field] = "conflict"
    existing = dict(session.secrets[path].values)

    with pytest.raises(OpenBaoError, match=rf"credential mismatch at {path}/{field}"):
        reconcile_internal_credentials(api, {}, addons=("dify", "librechat"))

    assert session.secrets[path].values == existing


def test_bridge_verifier_must_match_managed_key(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    session.secrets["auth-keycloak-api-key-bridge/internal"] = StoredSecret(
        {
            "difyAgentgatewayPrimaryVerifierSha256": "wrong-primary",
            "difyAgentgatewaySecondaryVerifierSha256": "wrong-secondary",
        }
    )

    with pytest.raises(OpenBaoError, match="managed verifier slot"):
        reconcile_internal_credentials(
            api, plan_bootstrap_passwords(api, addons=("dify",)), addons=("dify",)
        )


def test_existing_bootstrap_passwords_are_not_planned(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    planned = plan_bootstrap_passwords(api)
    reconcile_internal_credentials(api, planned)

    assert plan_bootstrap_passwords(api) == {}


def test_invalid_bootstrap_passwords_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(OpenBaoError, match="Bootstrap passwords are invalid"):
        reconcile_internal_credentials(client(tmp_path, FakeSession()), {"machine": "secret"})
