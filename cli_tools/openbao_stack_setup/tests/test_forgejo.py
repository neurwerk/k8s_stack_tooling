from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession

from openbao_stack_setup.catalog import (
    BOOTSTRAP_SECRET_STORES,
    FORGEJO_EXTERNAL_SECRETS,
    FORGEJO_SECRET_STORE,
    RECONCILIATION_STATE_PATH,
)
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import ClusterError
from openbao_stack_setup.credentials import _forgejo_internal_token, plan_bootstrap_passwords
from openbao_stack_setup.main import _converge_runtime
from openbao_stack_setup.providers import PROVIDERS
from openbao_stack_setup.reconcile import ReconciliationIdentity, reconcile_openbao
from openbao_stack_setup.seed import seed_bootstrap


@pytest.mark.parametrize("initially_enabled", [False, True])
def test_forgejo_bootstrap_onboarding_and_retry(
    tmp_path: Path, initially_enabled: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    seed_bootstrap(
        api,
        {
            name: dict.fromkeys(provider.fields, secrets.token_urlsafe(32))
            for name, provider in PROVIDERS.items()
        },
        {},
        plan_bootstrap_passwords(api),
        identity,
        forgejo_enabled=initially_enabled,
    )
    before = deepcopy(session.secrets)
    if not initially_enabled:
        assert "forgejo/internal" not in before
        assert "forgejoPassword" not in before["infra-postgres-operations/internal"].values
        assert "forgejoClientSecret" not in before["auth-keycloak/internal"].values
        assert not any("forgejo" in request.path for request in session.calls)
        # Existing schema-4 installations need no global migration or state rewrite.
        session.secrets[RECONCILIATION_STATE_PATH].values["packageVersion"] = "0.2.11"
    state = deepcopy(session.secrets[RECONCILIATION_STATE_PATH])
    report = reconcile_openbao(api, identity, forgejo_enabled=True)
    assert report.applied_version == report.previous_version == 4
    assert report.internal_fields_added == (0 if initially_enabled else 9)
    assert session.secrets[RECONCILIATION_STATE_PATH] == state
    record = session.secrets["forgejo/internal"].values
    assert set(record) == {
        "dbPassword",
        "oidcClientSecret",
        "secretKey",
        "internalToken",
        "oauth2JwtSecret",
        "lfsJwtSecret",
        "adminPassword",
    }
    assert len(set(record.values())) == 7
    for field in (
        "dbPassword",
        "oidcClientSecret",
        "oauth2JwtSecret",
        "lfsJwtSecret",
        "adminPassword",
    ):
        value = record[field]
        assert isinstance(value, str)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
        assert len(base64.urlsafe_b64decode(value + "=")) == 32
    assert isinstance(record["secretKey"], str)
    assert re.fullmatch(r"[0-9a-f]{64}", record["secretKey"])
    assert (
        session.secrets["infra-postgres-operations/internal"].values["forgejoPassword"]
        == record["dbPassword"]
    )
    assert (
        session.secrets["auth-keycloak/internal"].values["forgejoClientSecret"]
        == record["oidcClientSecret"]
    )
    for path, old in before.items():
        if path != RECONCILIATION_STATE_PATH:
            assert old.values.items() <= session.secrets[path].values.items()
    for namespace, service_account in (
        ("forgejo", "forgejo-external-secrets"),
        ("infra-postgres-operations", "infra-postgres-operations-external-secrets"),
        ("auth-keycloak", "auth-keycloak-external-secrets"),
    ):
        role = next(
            request
            for request in session.calls
            if request.path == f"auth/kubernetes/role/{namespace}"
        )
        assert role.body == {
            "bound_service_account_names": [service_account],
            "bound_service_account_namespaces": [namespace],
            "audience": "openbao",
            "token_policies": [namespace],
            "token_ttl": "10m",
            "token_max_ttl": "30m",
        }
    after = deepcopy(session.secrets)
    assert reconcile_openbao(api, identity, forgejo_enabled=True).internal_fields_added == 0
    session.calls.clear()
    reconcile_openbao(api, identity)
    assert session.secrets == after
    assert not any("forgejo" in request.path for request in session.calls)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("path", "field", "source"),
    [
        ("infra-postgres-operations/internal", "forgejoPassword", "dbPassword"),
        ("auth-keycloak/internal", "forgejoClientSecret", "oidcClientSecret"),
    ],
)
def test_forgejo_copy_conflict_preserves_generated_keys_and_state(
    tmp_path: Path, path: str, field: str, source: str
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api))
    session.secrets[path].values[field] = secrets.token_urlsafe(32)
    state = deepcopy(session.secrets[RECONCILIATION_STATE_PATH])
    with pytest.raises(OpenBaoError, match="credential mismatch"):
        reconcile_openbao(api, identity, forgejo_enabled=True)
    generated = deepcopy(session.secrets["forgejo/internal"])
    assert session.secrets[RECONCILIATION_STATE_PATH] == state
    with pytest.raises(OpenBaoError, match="credential mismatch"):
        reconcile_openbao(api, identity, forgejo_enabled=True)
    assert session.secrets["forgejo/internal"] == generated
    session.secrets[path].values[field] = generated.values[source]
    reconcile_openbao(api, identity, forgejo_enabled=True)
    assert session.secrets["forgejo/internal"] == generated


def test_internal_token_matches_upstream_hs256_generation() -> None:
    key = secrets.token_urlsafe(32)
    with patch("openbao_stack_setup.credentials._random_secret", return_value=key):
        token = _forgejo_internal_token()
    header, payload, signature = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(header)) == {"alg": "HS256", "typ": "JWT"}
    assert isinstance(json.loads(base64.urlsafe_b64decode(payload + "=="))["nbf"], int)
    assert base64.urlsafe_b64decode(signature + "=") == hmac.digest(
        key.encode("ascii"), f"{header}.{payload}".encode("ascii"), hashlib.sha256
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_convergence_is_selected_and_orders_credentials_before_postgres(
    enabled: bool,
) -> None:
    cluster = MagicMock()
    _converge_runtime(cluster, False, enabled)
    stores = list(BOOTSTRAP_SECRET_STORES) + ([FORGEJO_SECRET_STORE] if enabled else [])
    assert cluster.ensure_secret_store_ready.call_args_list == [
        call(target.name, target.namespace) for target in stores
    ]
    # Pin Base's resource identities independently of the CLI catalog constants.
    store_calls = cluster.ensure_secret_store_ready.call_args_list
    assert (call("forgejo-openbao-secret-store", "forgejo") in store_calls) is enabled
    assert call("forgejo-secret-store", "forgejo") not in store_calls
    assert (
        call("infra-postgres-operations-openbao-secret-store", "infra-postgres-operations")
        in store_calls
    )
    assert call("auth-keycloak-openbao-secret-store", "auth-keycloak") in store_calls
    for target in FORGEJO_EXTERNAL_SECRETS:
        refresh = call.force_external_secret_refresh(
            target.name, target.namespace, target.target_secret
        )
        if enabled:
            assert cluster.mock_calls.index(refresh) < cluster.mock_calls.index(
                call.force_reconcile("postgres-operations", "infra-postgres-operations")
            )
        else:
            assert refresh not in cluster.mock_calls


def test_selected_missing_runtime_resource_fails_visibly_and_can_retry() -> None:
    cluster = MagicMock()

    def ready(name: str, namespace: str) -> None:
        if namespace == "forgejo":
            raise ClusterError("Selected Forgejo SecretStore is not available yet")

    cluster.ensure_secret_store_ready.side_effect = ready
    with pytest.raises(ClusterError, match="not available yet"):
        _converge_runtime(cluster, False, True)
    cluster.force_reconcile.assert_not_called()
    cluster.ensure_secret_store_ready.side_effect = None
    _converge_runtime(cluster, False, True)
    cluster.reconcile_kustomization.assert_called_once_with("infrastructure")
