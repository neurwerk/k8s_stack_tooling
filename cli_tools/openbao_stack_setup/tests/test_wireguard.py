from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.credentials import plan_bootstrap_passwords
from openbao_stack_setup.main import _converge_runtime
from openbao_stack_setup.reconcile import ReconciliationIdentity, reconcile_openbao


def test_selection_uses_only_optional_product_values() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = ApiException(status=404)
    assert target.wireguard_enabled() is False
    target.core.read_namespaced_config_map.assert_called_once_with(
        "wireguard-product-values", "wireguard"
    )
    target.core.read_namespaced_config_map.side_effect = None
    for text, selected in [
        ("{}", False),
        ("wireguard: {enabled: false}", False),
        ("wireguard: {enabled: true, serverKeySecret: wireguard-server-key}", True),
    ]:
        target.core.read_namespaced_config_map.return_value = SimpleNamespace(
            data={"values.yaml": text}
        )
        assert target.wireguard_enabled() is selected
    for text in ["wireguard: null", 'wireguard: {enabled: "true"}', "wireguard: {enabled: true}"]:
        target.core.read_namespaced_config_map.return_value = SimpleNamespace(
            data={"values.yaml": text}
        )
        with pytest.raises(RuntimeError, match="WireGuard"):
            target.wireguard_enabled()
    target.core.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        target.wireguard_enabled()


def test_key_is_selected_persistent_and_never_replaced_when_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = FakeSession()
    api = OpenBaoClient("https://bao.test", "root", tmp_path / "ca.crt", session)
    identity = ReconciliationIdentity("client", "cluster", "namespace")
    api.write_secret("stack-setup/providers/smtp", {"smtpUsername": "", "smtpPassword": ""}, 0)
    reconcile_openbao(api, identity, plan_bootstrap_passwords(api))
    assert not any("wireguard" in request.path for request in session.calls)
    generated = MagicMock()
    generated.private_bytes_raw.return_value = bytes(range(32))
    with patch(
        "openbao_stack_setup.credentials.X25519PrivateKey.generate", return_value=generated
    ) as generate:
        assert reconcile_openbao(api, identity, wireguard_enabled=True).internal_fields_added == 1
        saved = deepcopy(session.secrets["wireguard/internal"])
        assert set(saved.values) == {"privateKey"}
        assert reconcile_openbao(api, identity, wireguard_enabled=True).internal_fields_added == 0
        generate.assert_called_once_with()
    assert session.secrets["wireguard/internal"] == saved
    role = next(r for r in session.calls if r.path == "auth/kubernetes/role/wireguard")
    assert isinstance(role.body, dict)
    assert role.body["bound_service_account_names"] == ["wireguard-external-secrets"]
    assert role.body["bound_service_account_namespaces"] == ["wireguard"]
    session.calls.clear()
    reconcile_openbao(api, identity)
    assert not any("wireguard" in request.path for request in session.calls)
    assert session.secrets["wireguard/internal"] == saved
    session.secrets["wireguard/internal"].values["privateKey"] = "invalid"
    with pytest.raises(OpenBaoError, match="WireGuard server key is invalid"):
        reconcile_openbao(api, identity, wireguard_enabled=True)
    assert session.secrets["wireguard/internal"].values["privateKey"] == "invalid"
    assert capsys.readouterr() == ("", "")


def test_consumer_refresh_is_selected_and_never_starts_gateway() -> None:
    target = MagicMock()
    _converge_runtime(target, False)
    assert not any("wireguard" in str(c) for c in target.mock_calls)
    target.reset_mock()
    _converge_runtime(target, False, wireguard_enabled=True)
    store = call.ensure_secret_store_ready("wireguard-openbao-secret-store", "wireguard")
    refresh = call.force_external_secret_refresh(
        "wireguard-server-key", "wireguard", "wireguard-server-key"
    )
    assert target.mock_calls.index(store) < target.mock_calls.index(refresh)
    assert not any("wireguard" in str(c) for c in target.force_reconcile.call_args_list)
