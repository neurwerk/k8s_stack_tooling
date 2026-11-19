from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest
from fake import FakeSession, StoredSecret
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster
from test_main import selected_run
from test_recovery import initialized_kit

from openbao_stack_setup.catalog import DOCLING_NAMESPACES
from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import ClusterError, StackIdentity
from openbao_stack_setup.credentials import plan_bootstrap_passwords
from openbao_stack_setup.main import _bootstrap, _reconcile, main
from openbao_stack_setup.reconcile import ReconciliationIdentity, reconcile_openbao
from openbao_stack_setup.recovery import with_checkpoint, write_new


def test_selection_fails_before_confirmation_or_secret_access() -> None:
    target = cluster()
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch.object(target, "identity"),
        patch.object(target, "require_bootstrap_prerequisites"),
        patch.object(target, "active_directory_required"),
        patch.object(target, "tailscale_enabled", return_value=False),
        patch("openbao_stack_setup.main._confirm") as confirm,
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as openbao,
    ):
        for text in [
            "docling: {enabled: 1}",
            'docling: {enabled: "true"}',
            "docling: null",
            "[]",
            "[",
            "",
            "docling: {enabled: true, inference: {mode: private-standard}}",
            "docling: {enabled: false, inference: null}",
            *[
                f"docling: {{enabled: true, mode: {mode}}}"
                for mode in (
                    "null",
                    "true",
                    "1",
                    "[]",
                    "{}",
                    "other",
                    "internal-standard",
                    "private-standard",
                    "private-vlm",
                )
            ],
        ]:
            target.core.read_namespaced_config_map.return_value = SimpleNamespace(
                data={"values.yaml": text}
            )
            for operation in (
                target.docling_enabled,
                lambda: _bootstrap("ctx", "client", None, run=selected_run()),
                lambda: _reconcile("ctx", "client", None, None, run=selected_run()),
            ):
                with pytest.raises(ClusterError, match="Docling"):
                    operation()
        target.core.read_namespaced_config_map.side_effect = ApiException(status=403)
        with pytest.raises(ClusterError, match="Docling"):
            target.docling_enabled()
        target.core.read_namespaced_config_map.side_effect = ApiException(status=404)
        assert target.docling_enabled() is False
        confirm.assert_not_called()
        prompt.assert_not_called()
        openbao.assert_not_called()
        target.core.create_namespaced_service_account_token.assert_not_called()
    assert all(
        c == call("client-values", "monitor-agentgateway-extproc")
        for c in target.core.read_namespaced_config_map.call_args_list
    )


@pytest.mark.parametrize("mode", [None, "internal", "external"])
@pytest.mark.parametrize("enabled", [True, False])
def test_modes_and_shared_selection(mode: str | None, enabled: bool) -> None:
    target = cluster()
    text = f"docling:\n  enabled: {str(enabled).lower()}\n"
    if mode is not None:
        text += f"  mode: {mode}\n"
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": text}
    )
    expected = mode or "internal"
    assert target.docling_inference_mode() == (expected if enabled else None)
    assert target.docling_enabled() is enabled
    assert target.core.mock_calls == [
        call.read_namespaced_config_map("client-values", "monitor-agentgateway-extproc"),
        call.read_namespaced_config_map("client-values", "monitor-agentgateway-extproc"),
    ]


def test_generation_mirror_retry_conflict_and_optional_roles(tmp_path: Path) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api))
    assert not any("docling/internal" in r.path for r in session.calls)
    source = "docling/internal"
    destination = "monitor-agentgateway-extproc/internal"
    session.secrets["docling/external"] = StoredSecret(
        {"inferenceToken": "retired-token", "sibling": "keep"}
    )
    legacy = deepcopy(session.secrets["docling/external"])
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
    assert session.secrets["docling/external"] == legacy
    assert not any("docling/external" in r.path for r in session.calls)
    for namespace in DOCLING_NAMESPACES:
        role = next(r for r in session.calls if r.path == f"auth/kubernetes/role/{namespace}")
        assert isinstance(role.body, dict)
        assert role.body["bound_service_account_names"] == [f"{namespace}-external-secrets"]
        assert role.body["bound_service_account_namespaces"] == [namespace]
    session.calls.clear()
    reconcile_openbao(api, identity)
    assert not any(any(ns in r.path for ns in DOCLING_NAMESPACES) for r in session.calls)
    assert session.secrets[source] == saved
    assert session.secrets["docling/external"] == legacy
    for value in ("", " ", "key\r", "key\n"):
        session.secrets[source].values["apiKey"] = value
        before = deepcopy(session.secrets)
        with pytest.raises(OpenBaoError):
            reconcile_openbao(api, identity, docling_enabled=True)
        assert session.secrets == before


def test_retired_token_setter_fails_before_cluster_or_secret_access() -> None:
    with (
        patch(
            "sys.argv",
            [
                "stack-setup",
                "secret",
                "set",
                "docling-inference",
                "--context",
                "ctx",
                "--client",
                "client",
            ],
        ),
        patch("openbao_stack_setup.main.Cluster") as target,
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as openbao,
        pytest.raises(SystemExit) as error,
    ):
        main()
    assert error.value.code == 2
    target.assert_not_called()
    prompt.assert_not_called()
    openbao.assert_not_called()


@pytest.mark.parametrize("mode", ["internal", "external"])
def test_native_key_workflow_preserves_keys_without_local_secret_dependency(
    tmp_path: Path, mode: str
) -> None:
    target = cluster()
    target.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": f"docling: {{enabled: true, mode: {mode}}}"}
    )
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca", session)
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    session.secrets["docling/internal"] = StoredSecret({"apiKey": "native-key", "sibling": "keep"})
    session.secrets["docling/external"] = StoredSecret({"inferenceToken": "retired-token"})
    before = deepcopy(session.secrets)
    kit = with_checkpoint(with_checkpoint(initialized_kit(), "seeded"), "complete")
    recovery = tmp_path / "recovery.json"
    write_new(recovery, kit)
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch.object(
            target, "identity", return_value=StackIdentity("client", "cluster", "namespace")
        ),
        patch.object(target, "tailscale_enabled", return_value=False),
        patch.object(target, "glitchtip_enabled", return_value=False),
        patch.object(target, "rejection_debug_enabled", return_value=False),
        patch.object(target, "contextforge_admin_email", return_value=None),
        patch.object(target, "studio_mcp_ids", return_value=None),
        patch.object(target, "wait_openbao_endpoint"),
        patch.object(target, "force_external_secret_refresh") as refresh,
        patch.object(target, "force_reconcile") as reconcile,
        patch(
            "openbao_stack_setup.main.prepare_custody_paths",
            return_value=SimpleNamespace(seal_file=recovery),
        ),
        patch(
            "openbao_stack_setup.main._required_package_paths",
            return_value=(Path("one"), Path("two")),
        ),
        patch("openbao_stack_setup.main._decrypt_custodian_packages", return_value=("one", "two")),
        patch.object(api, "create_recovery_root_token", return_value="synthetic-root"),
        patch("openbao_stack_setup.main._openbao", return_value=nullcontext(api)),
        patch("openbao_stack_setup.main._deliver_bootstrap_passwords"),
        patch("openbao_stack_setup.main._verify_secret_operator"),
        patch("openbao_stack_setup.main._revoke_other_root_tokens"),
    ):
        _reconcile("ctx", "client", tmp_path, [], "client", run=selected_run())
        mirrored = deepcopy(session.secrets["monitor-agentgateway-extproc/internal"])
        _reconcile("ctx", "client", tmp_path, [], "client", run=selected_run())
    assert session.secrets["docling/internal"] == before["docling/internal"]
    assert session.secrets["docling/external"] == before["docling/external"]
    assert mirrored.values == {"doclingApiKey": "native-key"}
    assert session.secrets["monitor-agentgateway-extproc/internal"] == mirrored
    assert not any("docling/external" in r.path for r in session.calls)
    assert session.calls[-1].path == "auth/token/revoke-self"
    assert target.core.mock_calls == [
        call.read_namespaced_config_map("client-values", "monitor-agentgateway-extproc"),
        call.read_namespaced_config_map("client-values", "monitor-agentgateway-extproc"),
    ]
    refresh.assert_not_called()
    reconcile.assert_not_called()
