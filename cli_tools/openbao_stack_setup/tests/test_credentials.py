from __future__ import annotations

import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fake import FakeSession, StoredSecret

from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import (
    BOOTSTRAP_PASSWORDS,
    INTERNAL_PATHS,
    plan_bootstrap_passwords,
    reconcile_internal_credentials,
)


def client(tmp_path: Path, session: FakeSession) -> OpenBaoClient:
    ca = tmp_path / "ca.crt"
    ca.write_text("CA", encoding="utf-8")
    return OpenBaoClient("https://bao.test", "root", ca, session)


def assert_ed25519_pair(
    values: dict[str, JsonValue], private_field: str, public_field: str
) -> None:
    private_pem = values[private_field]
    public_pem = values[public_field]
    assert isinstance(private_pem, str)
    assert isinstance(public_pem, str)
    assert private_pem.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----\n")

    private_key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
    assert isinstance(private_key, Ed25519PrivateKey)
    assert isinstance(public_key, Ed25519PublicKey)
    assert private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def unrelated_public_key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def test_internal_credentials_are_complete_and_idempotent(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)

    planned = plan_bootstrap_passwords(api)
    first = reconcile_internal_credentials(api, planned)
    original = {path: dict(record.values) for path, record in session.secrets.items()}
    second = reconcile_internal_credentials(api, {})

    assert set(first.changed_paths) == set(INTERNAL_PATHS)
    assert first.added_fields > len(INTERNAL_PATHS)
    assert set(planned) == {password.key for password in BOOTSTRAP_PASSWORDS}
    assert second.changed_paths == ()
    assert second.added_fields == 0
    assert {path: record.values for path, record in session.secrets.items()} == original

    librechat = original["frontend-librechat/internal"]
    code_interpreter = original["librechat-code-interpreter/internal"]
    assert {
        "valkeyPassword",
        "ragPostgresqlUser",
        "ragPostgresqlPassword",
        "ragOpenaiApiKey",
        "adminPanelSessionSecret",
        "adminPanelMetricsSecret",
        "codeInterpreterJwtPrivateKey",
        "codeInterpreterJwtPublicKey",
    }.issubset(librechat)
    assert librechat["ragPostgresqlUser"] == "librechat_rag"
    assert set(code_interpreter) == {
        "internalServiceToken",
        "valkeyPassword",
        "egressGrantSecret",
        "executionManifestPrivateKey",
        "executionManifestPublicKey",
        "jwtPublicKey",
    }
    assert_ed25519_pair(librechat, "codeInterpreterJwtPrivateKey", "codeInterpreterJwtPublicKey")
    assert_ed25519_pair(
        code_interpreter, "executionManifestPrivateKey", "executionManifestPublicKey"
    )
    assert code_interpreter["jwtPublicKey"] == librechat["codeInterpreterJwtPublicKey"]
    assert code_interpreter["valkeyPassword"] != librechat["valkeyPassword"]
    agentgateway = original["infra-agentgateway/internal"]
    assert set(agentgateway) == {"postgresqlPassword"}
    agentgateway_password = agentgateway["postgresqlPassword"]
    assert isinstance(agentgateway_password, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", agentgateway_password)
    assert set(original["frontend-studio/internal"]) == {"opensearchPassword"}
    postgres_auth = original["infra-postgres-auth/internal"]
    postgres_operations = original["infra-postgres-operations/internal"]
    assert set(postgres_auth) == {"adminPassword", "keycloakPassword"}
    assert set(postgres_operations) == {
        "adminPassword",
        "agentgatewayPassword",
        "documentdbPassword",
        "difyPassword",
        "langfusePassword",
        "librechatRagPassword",
    }
    assert postgres_auth["adminPassword"]
    assert postgres_operations["adminPassword"]
    assert postgres_operations["agentgatewayPassword"] == agentgateway_password
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
    assert postgres_operations["librechatRagPassword"] == librechat["ragPostgresqlPassword"]


def test_internal_fixed_value_mismatch_is_rejected(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets["frontend-studio/internal"].values["opensearchPassword"] = "wrong"

    with pytest.raises(OpenBaoError, match="Internal credential mismatch"):
        reconcile_internal_credentials(api, {})


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
        "opensearchPassword": session.secrets["monitor-opensearch/internal"].values[
            "studioPassword"
        ],
    }


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("infra-postgres-auth/internal", "keycloakPassword"),
        ("infra-postgres-operations/internal", "agentgatewayPassword"),
        ("infra-postgres-operations/internal", "documentdbPassword"),
        ("infra-postgres-operations/internal", "difyPassword"),
        ("infra-postgres-operations/internal", "langfusePassword"),
        ("infra-postgres-operations/internal", "librechatRagPassword"),
    ],
)
def test_postgres_consumer_conflicts_are_rejected(tmp_path: Path, path: str, field: str) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets[path].values[field] = "conflict"
    existing = dict(session.secrets[path].values)

    with pytest.raises(OpenBaoError, match=rf"credential mismatch at {path}/{field}"):
        reconcile_internal_credentials(api, {})

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
        reconcile_internal_credentials(api, plan_bootstrap_passwords(api))


@pytest.mark.parametrize(
    ("path", "private_field", "public_field", "missing_field"),
    [
        (
            "frontend-librechat/internal",
            "codeInterpreterJwtPrivateKey",
            "codeInterpreterJwtPublicKey",
            "codeInterpreterJwtPrivateKey",
        ),
        (
            "frontend-librechat/internal",
            "codeInterpreterJwtPrivateKey",
            "codeInterpreterJwtPublicKey",
            "codeInterpreterJwtPublicKey",
        ),
        (
            "librechat-code-interpreter/internal",
            "executionManifestPrivateKey",
            "executionManifestPublicKey",
            "executionManifestPrivateKey",
        ),
        (
            "librechat-code-interpreter/internal",
            "executionManifestPrivateKey",
            "executionManifestPublicKey",
            "executionManifestPublicKey",
        ),
    ],
)
def test_incomplete_ed25519_pair_is_rejected_without_rotation(
    tmp_path: Path,
    path: str,
    private_field: str,
    public_field: str,
    missing_field: str,
) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    del session.secrets[path].values[missing_field]
    existing = dict(session.secrets[path].values)

    with pytest.raises(OpenBaoError, match="key pair is incomplete"):
        reconcile_internal_credentials(api, {})

    assert session.secrets[path].values == existing
    assert (private_field in existing) != (public_field in existing)


@pytest.mark.parametrize(
    ("path", "public_field"),
    [
        ("frontend-librechat/internal", "codeInterpreterJwtPublicKey"),
        ("librechat-code-interpreter/internal", "executionManifestPublicKey"),
    ],
)
def test_mismatched_ed25519_pair_is_rejected_without_rotation(
    tmp_path: Path, path: str, public_field: str
) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets[path].values[public_field] = unrelated_public_key()
    existing = dict(session.secrets[path].values)

    with pytest.raises(OpenBaoError, match="key pair does not match"):
        reconcile_internal_credentials(api, {})

    assert session.secrets[path].values == existing


def test_code_interpreter_jwt_verifier_mismatch_is_rejected(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    reconcile_internal_credentials(api, plan_bootstrap_passwords(api))
    session.secrets["librechat-code-interpreter/internal"].values["jwtPublicKey"] = (
        unrelated_public_key()
    )

    with pytest.raises(OpenBaoError, match=r"credential mismatch.*jwtPublicKey"):
        reconcile_internal_credentials(api, {})


def test_existing_bootstrap_passwords_are_not_planned(tmp_path: Path) -> None:
    session = FakeSession()
    api = client(tmp_path, session)
    planned = plan_bootstrap_passwords(api)
    reconcile_internal_credentials(api, planned)

    assert plan_bootstrap_passwords(api) == {}


def test_invalid_bootstrap_passwords_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(OpenBaoError, match="Bootstrap passwords are invalid"):
        reconcile_internal_credentials(client(tmp_path, FakeSession()), {"machine": "secret"})
