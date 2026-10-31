"""Protect existing keys and fail closed across an interrupted ownership handoff."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fake import FakeSession, StoredSecret
from k8s_stack_tooling.addons import Addon, Setup, expand_setup
from k8s_stack_tooling.addons.schema import AddonSelection
from test_reconcile import client, identity, passwords, source, state_values
from test_recovery import initialized_kit
from test_selected_cli import verified

from openbao_stack_setup import main as cli
from openbao_stack_setup.catalog import RECONCILIATION_STATE_PATH
from openbao_stack_setup.client import JsonValue, OpenBaoError
from openbao_stack_setup.declarative import plan_credentials
from openbao_stack_setup.librechat_preservation import preserve_librechat
from openbao_stack_setup.reconcile import _load_state, plan_reconciliation_access, reconcile_openbao
from openbao_stack_setup.selection import SelectedRun


@pytest.fixture
def legacy():
    groups = {
        "runtime": [
            "credsKey",
            "credsIv",
            "jwtSecret",
            "jwtRefreshSecret",
            "meiliMasterKey",
            "openidSessionSecret",
            "valkeyPassword",
            "adminPanelSessionSecret",
            "adminPanelMetricsSecret",
        ],
        "database": ["documentdbUser", "documentdbPassword"],
        "oidc": ["openidClientSecret"],
        "speech": ["sttApiKey", "ttsApiKey"],
    }
    records = []
    internal: dict[str, JsonValue] = {}
    for name, fields in groups.items():
        definitions = {}
        for field in fields:
            size = 16 if field == "credsIv" else 32
            encoding = "hex" if field in ("credsKey", "credsIv") else "base64url"
            definitions[field] = {"generate": {"encoding": encoding, "bytes": size}}
            internal[field] = "ab" * size if encoding == "hex" else "Y" * 42 + "A"
        if name == "database":
            definitions["documentdbUser"] = {"literal": "librechat"}
            internal["documentdbUser"] = "librechat"
        records.append(
            {
                "id": name,
                "path": f"addons/librechat/{'external/speech' if name == 'speech' else name}",
                "fields": definitions,
            }
        )
    session = FakeSession()
    session.secrets = {
        RECONCILIATION_STATE_PATH: StoredSecret(state_values(applied=2)),
        "frontend-librechat/internal": StoredSecret(internal),
        "frontend-librechat/external": StoredSecret(
            {key: internal[key] for key in groups["speech"]}
        ),
        "auth-keycloak/internal": StoredSecret(
            {"librechatOidcClientSecret": internal["openidClientSecret"]}
        ),
        "infra-postgres-operations/internal": StoredSecret(
            {"documentdbPassword": internal["documentdbPassword"]}
        ),
    }
    return session, Setup.model_validate({"records": records})


def test_preserves_keys_and_sources_without_regeneration(tmp_path, legacy):
    session, setup = legacy
    originals = deepcopy(session.secrets)
    api = client(tmp_path, session)
    preserve_librechat(api, identity(), setup, active_setup=setup)
    for path, original in originals.items():
        if path != RECONCILIATION_STATE_PATH:
            assert session.secrets[path] == original
    assert session.secrets[RECONCILIATION_STATE_PATH].values["appliedVersion"] == 2
    assert (
        _load_state(
            api.read_secret(RECONCILIATION_STATE_PATH), identity(), client=api
        ).applied_version
        == 2
    )
    with patch("secrets.token_bytes", side_effect=AssertionError("must not regenerate")):
        assert plan_credentials(api, {"librechat": setup}).writes == ()
    completed = deepcopy(session.secrets)
    preserve_librechat(api, identity(), setup, active_setup=setup)
    assert session.secrets == completed


def test_interruption_retains_state_and_retry_preserves_completed_copy(tmp_path, legacy):
    session, setup = legacy
    api = client(tmp_path, session)
    session.failure = ("POST", "secret/data/addons/librechat/database", 500, {})
    with pytest.raises(OpenBaoError):
        preserve_librechat(api, identity(), setup, active_setup=setup)
    saved = deepcopy(session.secrets["addons/librechat/runtime"])
    assert "librechatPreserved" not in session.secrets[RECONCILIATION_STATE_PATH].values
    with pytest.raises(OpenBaoError, match="no migration"):
        _load_state(api.read_secret(RECONCILIATION_STATE_PATH), identity())
    session.failure = None
    preserve_librechat(api, identity(), setup, active_setup=setup)
    assert session.secrets["addons/librechat/runtime"] == saved


@pytest.mark.parametrize(
    "features", [{}, {"stt-auth": True}, {"tts-auth": True}, {"stt-auth": True, "tts-auth": True}]
)
@pytest.mark.parametrize("retain_inactive", [False, True])
def test_actual_manifest_preservation_then_ordinary_reconciliation(
    tmp_path, legacy, features, retain_inactive
):
    old, _ = legacy
    addon = Addon.model_validate(
        yaml.safe_load(
            Path(__file__).with_name("fixtures").joinpath("librechat-preservation.yaml").read_text()
        )
    )
    selection = AddonSelection.model_validate(
        {
            "id": "librechat",
            "sourceRef": {
                "kind": "GitRepository",
                "namespace": "flux-system",
                "name": "librechat-addon",
            },
            "features": features,
        }
    )
    active = expand_setup(addon, selection)
    speech = old.secrets["frontend-librechat/external"].values
    if not retain_inactive:
        for feature, field in (("stt-auth", "sttApiKey"), ("tts-auth", "ttsApiKey")):
            if not features.get(feature):
                speech.pop(field)
        if not speech:
            del old.secrets["frontend-librechat/external"]
    session = FakeSession()
    api = client(tmp_path, session)
    source(session)
    reconcile_openbao(api, identity(), passwords())
    for path, record in old.secrets.items():
        if path in session.secrets:
            session.secrets[path].values.update(record.values)
        else:
            session.secrets[path] = record
    preserve_librechat(api, identity(), addon.spec.setup, active_setup=active)
    preserved = {
        path: deepcopy(record)
        for path, record in session.secrets.items()
        if path.startswith("addons/librechat/")
    }
    staged = verified(addon_id="librechat")
    effective = addon.model_copy(deep=True)
    effective.spec.setup = active
    item = replace(
        staged.addons[0], definition=effective, declared_definition=addon, selection=selection
    )
    run = SelectedRun(replace(staged, addons=(item,)))
    with (
        patch.object(cli, "_verify_secret_operator"),
        patch.object(cli, "_revoke_other_root_tokens"),
    ):
        cli._prepare_selected(MagicMock(), api, api, initialized_kit(), tmp_path / "kit", run)
    for path, record in preserved.items():
        assert session.secrets[path] == record
    assert session.secrets[RECONCILIATION_STATE_PATH].values["appliedVersion"] == 4
    assert "librechatPreserved" not in session.secrets[RECONCILIATION_STATE_PATH].values
    copied_speech = session.secrets.get("addons/librechat/external/speech")
    assert (copied_speech.values if copied_speech else {}) == speech


@pytest.mark.parametrize("changed", ["frontend-librechat/internal", "addons/librechat/runtime"])
@pytest.mark.parametrize("change_value", [False, True])
def test_change_immediately_before_marker_is_not_accepted_by_reconciliation(
    tmp_path, legacy, changed, change_value
):
    session, setup = legacy
    api = client(tmp_path, session)
    write = api.write_secret

    def change_before_marker(path, values, version):
        if path == RECONCILIATION_STATE_PATH:
            session.secrets[changed].version += 1
            if change_value:
                session.secrets[changed].values["jwtSecret"] = "Z" * 42 + "A"
        write(path, values, version)

    with (
        patch.object(api, "write_secret", side_effect=change_before_marker),
        pytest.raises(OpenBaoError, match="snapshot changed"),
    ):
        preserve_librechat(api, identity(), setup, active_setup=setup)
    assert session.secrets[RECONCILIATION_STATE_PATH].values["appliedVersion"] == 2
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="evidence is stale"):
        plan_reconciliation_access(api, identity(), ())
    with pytest.raises(OpenBaoError, match="evidence is stale"):
        reconcile_openbao(api, identity(), passwords())
    assert not any(
        call.method == "POST" and call.path.startswith("secret/") for call in session.calls
    )
    if change_value:
        with pytest.raises(OpenBaoError, match="destination conflicts"):
            preserve_librechat(api, identity(), setup, active_setup=setup)
    else:
        # An unrelated version change can be re-reviewed without rewriting good copies.
        preserve_librechat(api, identity(), setup, active_setup=setup)
        plan_reconciliation_access(api, identity(), ())


@pytest.mark.parametrize("feature", ["stt-auth", "tts-auth"])
def test_missing_selected_speech_source_fails_before_any_copy(tmp_path, legacy, feature):
    session, _ = legacy
    addon = Addon.model_validate(
        yaml.safe_load(
            Path(__file__).with_name("fixtures").joinpath("librechat-preservation.yaml").read_text()
        )
    )
    selection = AddonSelection.model_validate(
        {
            "id": "librechat",
            "sourceRef": {
                "kind": "GitRepository",
                "namespace": "flux-system",
                "name": "librechat-addon",
            },
            "features": {feature: True},
        }
    )
    del session.secrets["frontend-librechat/external"]
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError, match="missing or invalid"):
        preserve_librechat(
            client(tmp_path, session),
            identity(),
            addon.spec.setup,
            active_setup=expand_setup(addon, selection),
        )
    assert session.secrets == before
    assert not any(call.method == "POST" for call in session.calls)


@pytest.mark.parametrize(
    "failure",
    ["oidc", "database", "missing", "invalid", "destination", "state", "identity", "definition"],
)
def test_preflight_failure_never_writes_or_discloses_values(tmp_path, legacy, failure, capsys):
    session, setup = legacy
    private = "synthetic-private-value"
    if failure == "oidc":
        session.secrets["auth-keycloak/internal"].values["librechatOidcClientSecret"] = private
    elif failure == "database":
        session.secrets["infra-postgres-operations/internal"].values["documentdbPassword"] = private
    elif failure == "missing":
        del session.secrets["frontend-librechat/internal"].values["credsKey"]
    elif failure == "invalid":
        session.secrets["frontend-librechat/internal"].values["credsKey"] = private
    elif failure == "destination":
        session.secrets["addons/librechat/oidc"] = StoredSecret({"openidClientSecret": private})
    elif failure == "state":
        session.secrets[RECONCILIATION_STATE_PATH].values["appliedVersion"] = 5
    elif failure == "identity":
        session.secrets[RECONCILIATION_STATE_PATH].values["client"] = "another"
    else:
        setup.records[-1].fields.pop("ttsApiKey")
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError) as error:
        preserve_librechat(client(tmp_path, session), identity(), setup, active_setup=setup)
    assert private not in str(error.value)
    assert session.secrets == before
    assert not any(call.method == "POST" for call in session.calls)
    assert capsys.readouterr() == ("", "")


def test_concurrent_source_change_stops_before_completion(tmp_path, legacy):
    session, setup = legacy
    api = client(tmp_path, session)
    write = api.write_secret

    def concurrent_write(path, values, version):
        write(path, values, version)
        session.secrets["frontend-librechat/internal"].version += 1

    with (
        patch.object(api, "write_secret", side_effect=concurrent_write),
        pytest.raises(OpenBaoError, match="snapshot changed"),
    ):
        preserve_librechat(api, identity(), setup, active_setup=setup)
    assert "librechatPreserved" not in session.secrets[RECONCILIATION_STATE_PATH].values
