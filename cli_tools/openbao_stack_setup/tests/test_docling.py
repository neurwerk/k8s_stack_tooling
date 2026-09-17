from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession, StoredSecret
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster

from openbao_stack_setup.catalog import DOCLING_NAMESPACES
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import ClusterError
from openbao_stack_setup.credentials import plan_bootstrap_passwords
from openbao_stack_setup.main import (
    SetupError,
    _bootstrap,
    _converge_runtime,
    _reconcile,
    _refresh_provider,
    _set_provider,
)
from openbao_stack_setup.providers import MANAGED_CREDENTIALS, PROVIDERS, update_provider
from openbao_stack_setup.reconcile import ReconciliationIdentity, reconcile_openbao

SELECTED = """docling:
  enabled: true
  apiKeySecretRef: {name: docling-api, key: api-key}
  inference:
    tokenSecretRef: {name: docling-inference, key: token}
"""


def test_selection_fails_before_confirmation_or_secret_access() -> None:
    target = cluster()
    cpu = SELECTED.replace("tokenSecretRef: {name: docling-inference, key: token}", "mode: cpu")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch.object(target, "identity"),
        patch.object(target, "require_bootstrap_prerequisites"),
        patch.object(target, "active_directory_required"),
        patch.object(target, "forgejo_enabled"),
        patch.object(target, "wireguard_enabled"),
        patch("openbao_stack_setup.main._confirm") as confirm,
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as openbao,
    ):
        for text in [
            "{}",
            "docling: {enabled: false}",
            "docling: {enabled: 1}",
            'docling: {enabled: "true"}',
            "docling: null",
            "[]",
            "[",
            "",
            "docling: {enabled: true}",
            SELECTED.replace("key: token", "key: wrong"),
            SELECTED.replace("name: docling-api", "name: other"),
            SELECTED.replace("key: api-key", "key: api-key, extra: value"),
            SELECTED.replace("tokenSecretRef: {name: docling-inference, key: token}", "null"),
            cpu,
            cpu.replace("name: docling-api", "name: other"),
            *[
                cpu.replace("mode: cpu", f"mode: {mode}")
                for mode in ("null", "true", "1", "[]", "{}", "other")
            ],
        ]:
            target.core.read_namespaced_config_map.return_value = SimpleNamespace(
                data={"values.yaml": text}
            )
            if text not in ("{}", "docling: {enabled: false}", cpu):
                with pytest.raises(ClusterError, match="Docling"):
                    target.docling_enabled()
            with pytest.raises((SetupError, ClusterError), match="Docling"):
                _set_provider("ctx", "client", "docling-inference")
        for status in (404, 403):
            target.core.read_namespaced_config_map.side_effect = ApiException(status=status)
            with pytest.raises((SetupError, ClusterError), match="Docling"):
                _set_provider("ctx", "client", "docling-inference")
        for operation in (
            lambda: _bootstrap("ctx", "client", None),
            lambda: _reconcile("ctx", "client", None, None),
        ):
            with pytest.raises(ClusterError, match="Docling"):
                operation()
        confirm.assert_not_called()
        prompt.assert_not_called()
        openbao.assert_not_called()
        target.core.create_namespaced_service_account_token.assert_not_called()
    target.core.read_namespaced_config_map.side_effect = None
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": SELECTED}
    )
    assert target.docling_enabled() is True
    for text, mode in (
        (SELECTED, "remote"),
        (SELECTED.replace("inference:", "inference:\n    mode: remote"), "remote"),
        (cpu, "cpu"),
    ):
        target.core.read_namespaced_config_map.return_value = SimpleNamespace(
            data={"values.yaml": text}
        )
        before = target.core.read_namespaced_config_map.call_count
        assert target.docling_inference_mode() == mode
        assert target.core.read_namespaced_config_map.call_count == before + 1
        assert target.docling_enabled() is True
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": cpu.replace("name: docling-api", "name: other")}
    )
    with pytest.raises(ClusterError, match="Docling"):
        target.docling_enabled()
    assert all(
        c == call("docling-product-values", "docling")
        for c in target.core.read_namespaced_config_map.call_args_list
    )


def test_generation_mirror_retry_conflict_and_optional_roles(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api))
    assert not any("docling/internal" in r.path for r in session.calls)
    source = "docling/internal"
    destination = "monitor-agentgateway-extproc/internal"
    session.secrets[source] = StoredSecret({"sibling": "keep"})
    session.secrets[destination] = StoredSecret({"sibling": "keep", "doclingApiKey": "conflict"})
    state = deepcopy(session.secrets["stack-setup/reconciliation-state"])
    with patch(
        "openbao_stack_setup.credentials.secrets.token_urlsafe", return_value="generated"
    ) as gen:
        for _ in range(2):
            with pytest.raises(OpenBaoError, match="Internal credential mismatch"):
                reconcile_openbao(api, identity, docling_enabled=True)
        gen.assert_called_once_with(32)
    saved = deepcopy(session.secrets[source])
    assert saved.values == {"sibling": "keep", "apiKey": "generated"}
    assert session.secrets[destination].values["doclingApiKey"] == "conflict"
    del session.secrets[destination].values["doclingApiKey"]
    assert reconcile_openbao(api, identity, docling_enabled=True).internal_fields_added == 1
    assert reconcile_openbao(api, identity, docling_enabled=True).internal_fields_added == 0
    assert session.secrets[source] == saved
    assert session.secrets[destination].values == {"sibling": "keep", "doclingApiKey": "generated"}
    assert session.secrets["stack-setup/reconciliation-state"] == state
    assert "docling/external" not in session.secrets
    for namespace in DOCLING_NAMESPACES:
        role = next(r for r in session.calls if r.path == f"auth/kubernetes/role/{namespace}")
        assert isinstance(role.body, dict)
        assert role.body["bound_service_account_names"] == [f"{namespace}-external-secrets"]
        assert role.body["bound_service_account_namespaces"] == [namespace]
    session.calls.clear()
    reconcile_openbao(api, identity)
    assert not any(any(ns in r.path for ns in DOCLING_NAMESPACES) for r in session.calls)
    assert session.secrets[source] == saved
    for value in ("", " ", "key\r", "key\n"):
        session.secrets[source].values["apiKey"] = value
        before = deepcopy(session.secrets)
        with pytest.raises(OpenBaoError):
            reconcile_openbao(api, identity, docling_enabled=True)
        assert session.secrets == before


def test_provider_cas_siblings_and_header_safety(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "operator", tmp_path / "ca", session)
    assert "docling-inference" not in PROVIDERS
    provider = MANAGED_CREDENTIALS["docling-inference"]
    assert provider.paths == ("docling/external",)
    assert provider.fields == ("inferenceToken",)
    for value in ("", "token\r", "token\n"):
        with pytest.raises(OpenBaoError):
            update_provider(api, provider, {"inferenceToken": value})
    assert session.calls == []
    update_provider(api, provider, {"inferenceToken": "first"})
    session.secrets["docling/external"].values["sibling"] = "keep"
    update_provider(api, provider, {"inferenceToken": "second"})
    assert session.secrets["docling/external"] == StoredSecret(
        {"inferenceToken": "second", "sibling": "keep"}, 2
    )


def test_consumer_refresh_never_waits_for_external_token_or_application() -> None:
    target = MagicMock()
    _converge_runtime(target, False)
    assert not any("docling" in str(c) for c in target.mock_calls)
    target.reset_mock()
    _converge_runtime(target, False, docling_enabled=True)
    for namespace, name in (
        ("docling", "docling-api"),
        ("monitor-agentgateway-extproc", "monitor-agentgateway-extproc-docling-secret"),
    ):
        store = call.ensure_secret_store_ready(f"{namespace}-openbao-secret-store", namespace)
        refresh = call.force_external_secret_refresh(name, namespace, name)
        assert target.mock_calls.index(store) < target.mock_calls.index(refresh)
    assert not any("docling-inference" in str(c) for c in target.mock_calls)
    assert not any("docling" in str(c) for c in target.force_reconcile.call_args_list)
    target.reset_mock()
    _refresh_provider(target, MANAGED_CREDENTIALS["docling-inference"])
    assert target.mock_calls == [
        call.force_external_secret_refresh("docling-inference", "docling", "docling-inference")
    ]
