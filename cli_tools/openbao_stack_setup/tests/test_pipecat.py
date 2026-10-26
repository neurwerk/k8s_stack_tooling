from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from fake import FakeSession, StoredSecret
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster
from test_main import kit

from openbao_stack_setup.client import OpenBaoClient, OpenBaoError
from openbao_stack_setup.cluster import ClusterError
from openbao_stack_setup.main import (
    _bootstrap,
    _reconcile,
    _refresh_addon,
    _refresh_pipecat_delivery,
    _seed_and_finish,
)
from openbao_stack_setup.pipecat import reconcile_pipecat, select_pipecat, select_pipecat_values

PRODUCT: dict[str, Any] = {
    "enabled": True,
    "secretSync": {"enabled": True},
    "oidc": {"enabled": True},
    "turn": {"enabled": True},
    "bots": [
        {
            "id": "voice-bot",
            "urlSlug": "voice_bot",
            "anonymous": True,
            "sessionsEnabled": False,
            "modelId": "example-model",
            "gatewayPermissions": [
                "llm:invoke",
                "model:example-model:invoke",
                "provider:example:company-use",
            ],
            "stt": {"secretRef": {"name": "pipecat-bot-voice-bot", "key": "sttApiKey"}},
        },
        {"id": "protected-bot", "anonymous": False, "sessionsEnabled": False},
    ],
}


def api(session: FakeSession) -> OpenBaoClient:
    return OpenBaoClient("https://openbao", "root", Path("/ca"), session)


@pytest.mark.parametrize("identity", ["../escape", "a/b", "a*", "voice_bot", "A", "a-", "a" * 41])
def test_bot_ids_cannot_escape_exact_paths(identity: str) -> None:
    product = deepcopy(PRODUCT)
    product["bots"][0]["id"] = identity
    with pytest.raises(OpenBaoError, match="Pipecat"):
        select_pipecat(product)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "protected-bot"},
        {"urlSlug": "protected-bot"},
        {"urlSlug": "assets"},
        {"anonymous": "true"},
        {"sessionsEnabled": "true"},
        {"gatewayPermissions": ["llm:invoke", "model:other:invoke"]},
        {"gatewayPermissions": ["llm:invoke", "model:example-model:invoke", "admin"]},
        {"gatewayPermissions": ["llm:invoke", "llm:invoke", "model:example-model:invoke"]},
        {"gatewayPermissions": ["llm:invoke", "model:example-model:invoke", "model:*:invoke"]},
        {"gatewaySecretRef": {"name": "pipecat-bot-other", "key": "gatewayApiKey"}},
        {"stt": {"secretRef": {"name": "pipecat-bot-voice-bot", "key": "gatewayApiKey"}}},
    ],
)
def test_invalid_or_broader_bot_selections_fail_closed(change: dict[str, Any]) -> None:
    product = deepcopy(PRODUCT)
    product["bots"][0].update(change)
    with pytest.raises(OpenBaoError, match="Pipecat"):
        select_pipecat(product)


def test_selection_matches_chart_refs_and_does_not_import_external_secrets() -> None:
    selected = select_pipecat(PRODUCT)
    assert asdict(selected.bots[0]) == {
        "id": "voice-bot",
        "gateway": True,
        "speech": ("sttApiKey",),
    }
    assert selected.bots[1].gateway is False
    product = deepcopy(PRODUCT)
    product["bots"][0]["gatewaySecretRef"] = {"name": "existing-scoped-key", "key": "key"}
    product["bots"][0]["stt"]["secretRef"] = {"name": "existing-speech", "key": "token"}
    external = select_pipecat(product)
    session = FakeSession()
    reconcile_pipecat(api(session), external, MagicMock(side_effect=AssertionError))
    assert set(session.secrets) == {"addons/pipecat/oidc", "addons/pipecat/turn"}
    disabled = select_pipecat({"enabled": True, "bots": []})
    empty = FakeSession()
    reconcile_pipecat(api(empty), disabled, MagicMock(side_effect=AssertionError))
    assert not empty.secrets
    target = MagicMock()
    _refresh_pipecat_delivery(target, disabled)
    assert not target.mock_calls
    page_only = select_pipecat(
        {
            "enabled": True,
            "secretSync": {"enabled": True},
            "bots": [{"id": "page-only", "anonymous": True, "sessionsEnabled": False}],
        }
    )
    assert not page_only.catalog().external_secrets


def test_namespace_grants_are_exact_and_no_raw_key_reaches_bridge_or_keycloak() -> None:
    selected = select_pipecat(PRODUCT)
    assert selected.paths("pipecat") == (
        "addons/pipecat/oidc",
        "addons/pipecat/turn",
        "addons/pipecat/bots/voice-bot",
    )
    assert selected.paths("auth-keycloak") == (
        "addons/pipecat/oidc",
        "addons/pipecat/machines/voice-bot",
    )
    assert selected.paths("auth-keycloak-api-key-bridge") == ("addons/pipecat/verifiers/voice-bot",)
    session = FakeSession()
    prompt = MagicMock(return_value="synthetic-speech-provider-key")
    reconcile_pipecat(api(session), selected, prompt)
    assert set(session.secrets) == {
        "addons/pipecat/oidc",
        "addons/pipecat/turn",
        "addons/pipecat/bots/voice-bot",
        "addons/pipecat/verifiers/voice-bot",
        "addons/pipecat/machines/voice-bot",
    }
    assert set(session.secrets["addons/pipecat/oidc"].values) == {"clientSecret"}
    turn = session.secrets["addons/pipecat/turn"].values["sharedSecret"]
    assert isinstance(turn, str) and len(turn) == 64 and set(turn) <= set("0123456789abcdef")
    assert set(session.secrets["addons/pipecat/verifiers/voice-bot"].values) == {
        "gatewayApiKeySha256"
    }
    for namespace in ("auth-keycloak", "auth-keycloak-api-key-bridge"):
        policy = str(session.policies[namespace])
        assert "addons/pipecat/bots" not in policy
        assert "addons/pipecat/turn" not in policy
    bridge_policy = str(session.policies["auth-keycloak-api-key-bridge"])
    assert "addons/pipecat/verifiers/voice-bot" in bridge_policy
    assert "addons/pipecat/oidc" not in bridge_policy
    runtime_policy = str(session.policies["pipecat"])
    assert "addons/pipecat/bots/voice-bot" in runtime_policy
    assert "verifiers" not in runtime_policy and "*" not in runtime_policy
    assert "machines" not in runtime_policy
    assert "machines" not in bridge_policy
    assert "addons/pipecat/machines/voice-bot" in str(session.policies["auth-keycloak"])
    snapshot = deepcopy(session.secrets)
    reconcile_pipecat(api(session), selected, prompt)
    assert session.secrets == snapshot
    prompt.assert_called_once_with("voice-bot", "sttApiKey")
    reconcile_pipecat(api(session), None, prompt)
    assert session.secrets == snapshot
    assert "pipecat" not in session.policies
    assert "addons/pipecat" not in str(session.policies["auth-keycloak"])
    assert "addons/pipecat" not in str(session.policies["auth-keycloak-api-key-bridge"])


def test_disabling_secret_sync_preserves_existing_secrets_without_prompt_or_delivery() -> None:
    product = deepcopy(PRODUCT)
    product["secretSync"]["enabled"] = False
    selected = select_pipecat(product)
    session = FakeSession()
    path = "addons/pipecat/oidc"
    session.secrets[path] = StoredSecret({"clientSecret": "synthetic-existing-value"})
    reconcile_pipecat(api(session), selected, MagicMock(side_effect=AssertionError))
    assert list(session.secrets) == [path]
    assert session.secrets[path].version == 1
    assert not selected.catalog().external_secrets
    assert all(not selected.paths(ns) for ns in ("pipecat", "auth-keycloak"))


@pytest.mark.parametrize("failure", [False, True])
def test_pipecat_preparation_revokes_root_before_delivery_even_on_failure(
    tmp_path: Path, failure: bool
) -> None:
    selected = select_pipecat(PRODUCT)
    root = MagicMock()
    events = []
    root.revoke_self.side_effect = lambda: events.append("revoke")

    def prepare(*args: object) -> None:
        events.append("prepare")
        if failure:
            raise OpenBaoError("Pipecat verifier conflict")

    with (
        patch("openbao_stack_setup.main.OpenBaoClient", return_value=root),
        patch("openbao_stack_setup.main.reconcile_openbao"),
        patch("openbao_stack_setup.main._prepare_pipecat_credentials", side_effect=prepare) as prep,
        patch("openbao_stack_setup.main._verify_secret_operator"),
        patch("openbao_stack_setup.main._revoke_other_root_tokens"),
        patch("openbao_stack_setup.main.update"),
        patch(
            "openbao_stack_setup.main._converge_runtime",
            side_effect=lambda *args, **kwargs: events.append("delivery"),
        ) as converge,
    ):
        if failure:
            with pytest.raises(OpenBaoError, match="Pipecat"):
                _seed_and_finish(
                    MagicMock(),
                    MagicMock(),
                    "temporary",
                    kit("seeded"),
                    tmp_path / "kit",
                    False,
                    addons=("pipecat",),
                    pipecat=selected,
                )
            converge.assert_not_called()
        else:
            _seed_and_finish(
                MagicMock(),
                MagicMock(),
                "temporary",
                kit("seeded"),
                tmp_path / "kit",
                False,
                addons=("pipecat",),
                pipecat=selected,
            )
        prep.assert_called_once_with(root, selected)
    assert events == ["prepare", "revoke"] + ([] if failure else ["delivery"])


def test_partial_write_retry_preserves_key_and_rejects_conflicting_verifier() -> None:
    selected = select_pipecat(PRODUCT)
    session = FakeSession()
    client = api(session)
    verifier_path = "addons/pipecat/verifiers/voice-bot"
    session.failure = ("POST", f"secret/data/{verifier_path}", 400, {"errors": ["CAS mismatch"]})
    prompt = MagicMock(return_value="synthetic-speech-provider-key")
    with pytest.raises(OpenBaoError):
        reconcile_pipecat(client, selected, prompt)
    original = deepcopy(session.secrets)
    session.failure = None
    reconcile_pipecat(client, selected, prompt)
    assert all(session.secrets[path] == record for path, record in original.items())
    prompt.assert_called_once()
    session.secrets[verifier_path] = StoredSecret({"gatewayApiKeySha256": "0" * 64})
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError, match="conflict"):
        reconcile_pipecat(client, selected, prompt)
    assert session.secrets == before
    del session.secrets["addons/pipecat/bots/voice-bot"]
    with pytest.raises(OpenBaoError, match="conflict"):
        reconcile_pipecat(client, selected, prompt)
    assert "addons/pipecat/bots/voice-bot" not in session.secrets


@pytest.mark.parametrize(
    "path,values",
    [
        ("addons/pipecat/oidc", {"clientSecret": "", "sharedSecret": "unexpected"}),
        ("addons/pipecat/turn", {"sharedSecret": "A" * 64}),
        ("addons/pipecat/bots/voice-bot", {"gatewayApiKey": "invalid\nkey"}),
        ("addons/pipecat/bots/voice-bot", {"arbitrary": "unexpected"}),
    ],
)
def test_invalid_existing_credentials_are_not_silently_rotated(
    path: str, values: dict[str, Any]
) -> None:
    session = FakeSession()
    session.secrets[path] = StoredSecret(values)
    with pytest.raises(OpenBaoError, match="Pipecat"):
        reconcile_pipecat(api(session), select_pipecat(PRODUCT), MagicMock())
    assert session.secrets[path].values == values


def test_cluster_reads_only_fixed_configmaps_and_rejects_before_custody() -> None:
    target = cluster()
    target.core.read_namespaced_config_map.side_effect = [
        SimpleNamespace(data={"values.yaml": yaml.safe_dump({"pipecat": {"enabled": True}})}),
        SimpleNamespace(data={"values.yaml": yaml.safe_dump({"pipecat": PRODUCT})}),
    ]
    assert target.pipecat_selection() == select_pipecat(PRODUCT)
    assert target.core.read_namespaced_config_map.call_args_list == [
        call("client-values", "pipecat"),
        call("pipecat-product-values", "pipecat"),
    ]
    target.core.read_namespaced_config_map.side_effect = ApiException(status=403)
    with pytest.raises(ClusterError, match="Pipecat"):
        target.pipecat_selection()
    target = MagicMock()
    target.selected_addons.return_value = ("pipecat",)
    target.pipecat_selection.side_effect = ClusterError("Invalid Pipecat catalog")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch("openbao_stack_setup.main._confirm") as confirm,
        patch("openbao_stack_setup.main._openbao") as openbao,
    ):
        for operation in (
            _bootstrap,
            lambda ctx, name, custody: _reconcile(ctx, name, custody, None),
        ):
            with pytest.raises(ClusterError, match="Pipecat"):
                operation("context", "client", None)
        confirm.assert_not_called()
        openbao.assert_not_called()
    assert select_pipecat_values(
        {"pipecat": {"enabled": True, "oidc": {"clientId": "pipecat"}}},
        {"pipecat": PRODUCT},
    ) == select_pipecat(PRODUCT)
    split = deepcopy(PRODUCT)
    split["secretSync"] = {"stores": {"pipecat": "pipecat-openbao-secret-store"}}
    assert select_pipecat_values(
        {"pipecat": {"secretSync": {"enabled": True}}}, {"pipecat": split}
    ) == select_pipecat(PRODUCT)


def test_refresh_delivers_before_consumers_and_restarts_only_derived_bot_workloads() -> None:
    target = MagicMock()
    selected = select_pipecat(PRODUCT)
    target.selected_addons.return_value = ("pipecat",)
    target.pipecat_selection.return_value = selected
    events = MagicMock()
    events.attach_mock(target, "cluster")
    with (
        patch("openbao_stack_setup.main.Cluster", return_value=target),
        patch("openbao_stack_setup.main._confirm"),
        patch("openbao_stack_setup.main._kubectl_executable", return_value="/tools/kubectl"),
        patch("openbao_stack_setup.main.subprocess.run") as rollout,
    ):
        events.attach_mock(rollout, "rollout")
        _refresh_addon("context", "client", "pipecat")
    calls = events.mock_calls
    secret_indexes = [
        i for i, item in enumerate(calls) if item[0].endswith("force_external_secret_refresh")
    ]
    consumer_indexes = [i for i, item in enumerate(calls) if item[0].endswith("force_reconcile")]
    rollout_indexes = [i for i, item in enumerate(calls) if item[0] == "rollout"]
    assert (
        max(secret_indexes) < min(consumer_indexes) < max(consumer_indexes) < min(rollout_indexes)
    )
    assert all("postgres" not in str(item) for item in target.force_reconcile.call_args_list)
    assert {item.args[0][7] for item in rollout.call_args_list} == {
        "deployment/pipecat-voice-bot",
        "deployment/pipecat-protected-bot",
        "deployment/pipecat-turn",
    }
