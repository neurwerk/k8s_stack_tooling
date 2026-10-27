"""Protect declarative credential preservation, failure order, and reader boundaries."""

import base64
import hashlib
from dataclasses import replace
from unittest.mock import patch

import pytest
from fake import FakeSession, StoredSecret
from k8s_stack_tooling.addons import Addon, Setup, expand_setup
from k8s_stack_tooling.addons.schema import AddonSelection
from test_reconcile import client, identity, passwords, source
from test_recovery import initialized_kit

from openbao_stack_setup.access import apply_access, complete_access
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.declarative import (
    apply_credentials,
    complete_handover,
    compose_readers,
    handover_definitions,
    plan_credentials,
)
from openbao_stack_setup.main import _prepare_bootstrap_passwords
from openbao_stack_setup.reconcile import plan_reconciliation_access, reconcile_openbao
from openbao_stack_setup.recovery import load, write_new


def record(name, fields, addon="sample", **kwargs):
    return {"id": name, "path": f"addons/{addon}/{name}", "fields": fields, **kwargs}


def generated(encoding="base64url", size=32):
    return {"generate": {"encoding": encoding, "bytes": size}}


def credential_writes(session):
    return [
        call
        for call in session.calls
        if call.method == "POST" and call.path.startswith("secret/data/addons/")
    ]


@pytest.mark.parametrize(
    "encoding,size", [("hex", 32), ("hex", 16), ("base64url", 32), ("base64", 32)]
)
def test_generated_encoding_round_trip_and_existing_size_protection(tmp_path, encoding, size):
    setup = Setup.model_validate(
        {"records": [record("runtime", {"key": generated(encoding, size)})]}
    )
    session = FakeSession()
    api = client(tmp_path, session)
    selected = {"sample": setup}
    plan = plan_credentials(api, selected)
    assert credential_writes(session) == []
    apply_credentials(api, plan)
    value = session.secrets["addons/sample/runtime"].values["key"]
    assert isinstance(value, str)
    if encoding == "hex":
        decoded = bytes.fromhex(value)
    else:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_")
    assert len(decoded) == size
    session.calls.clear()
    again = plan_credentials(api, selected)
    assert again.writes == ()
    assert value not in repr(plan)
    session.secrets["addons/sample/runtime"] = StoredSecret({"key": "00"})
    with pytest.raises(OpenBaoError, match="invalid encoding or size"):
        plan_credentials(api, selected)
    assert credential_writes(session) == []


def test_complete_selection_conflict_precedes_all_writes_and_is_redacted(tmp_path, capsys):
    setup = Setup.model_validate({"records": [record("runtime", {"key": generated()})]})
    other = Setup.model_validate(
        {"records": [record("config", {"name": {"literal": "expected"}}, "other")]}
    )
    session = FakeSession()
    api = client(tmp_path, session)
    session.secrets["addons/other/config"] = StoredSecret({"name": "synthetic-private-value"})
    with pytest.raises(OpenBaoError) as error:
        plan_credentials(api, {"sample": setup, "other": other})
    assert "synthetic-private-value" not in str(error.value)
    assert credential_writes(session) == []
    assert capsys.readouterr() == ("", "")


def test_interrupted_reference_writes_retry_from_persisted_canonical_key(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record("copy", {"key": {"ref": {"record": "source", "field": "key"}}}),
                record("source", {"key": generated()}),
            ]
        }
    )
    session = FakeSession()
    api = client(tmp_path, session)
    selected = {"sample": setup}
    session.failure = ("POST", "secret/data/addons/sample/copy", 400, {})
    with pytest.raises(OpenBaoError, match="HTTP 400"):
        apply_credentials(api, plan_credentials(api, selected))
    original = session.secrets["addons/sample/source"].values["key"]
    assert "addons/sample/copy" not in session.secrets
    session.failure = None
    apply_credentials(api, plan_credentials(api, selected))
    assert session.secrets["addons/sample/source"].values["key"] == original
    assert session.secrets["addons/sample/source"].version == 1
    assert session.secrets["addons/sample/copy"].values["key"] == original


def test_acyclic_fields_can_cross_records_in_both_directions(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record(
                    "first",
                    {"source": generated(), "last": {"ref": {"record": "second", "field": "copy"}}},
                ),
                record("second", {"copy": {"ref": {"record": "first", "field": "source"}}}),
            ]
        }
    )
    session = FakeSession()
    api = client(tmp_path, session)
    selected = {"sample": setup}
    apply_credentials(api, plan_credentials(api, selected))
    first = session.secrets["addons/sample/first"]
    assert first.values["source"] == first.values["last"]
    assert first.version == 2
    assert plan_credentials(api, selected).writes == ()


def test_stale_read_only_source_stops_plan_before_dependent_write(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record("source", {"value": {"supplied": {"group": "provider", "key": "token"}}}),
                record("copy", {"value": {"ref": {"record": "source", "field": "value"}}}),
            ],
            "inputGroups": [{"id": "provider", "fields": {"token": {}}}],
        }
    )
    session = FakeSession()
    api = client(tmp_path, session)
    session.secrets["addons/sample/source"] = StoredSecret({"value": "before"})
    selected = {"sample": setup}
    plan = plan_credentials(api, selected)
    session.secrets["addons/sample/source"] = StoredSecret({"value": "concurrent"}, 2)
    with pytest.raises(OpenBaoError, match="snapshot changed"):
        apply_credentials(api, plan)
    assert credential_writes(session) == []


def speech_setup(stt, tts):
    addon = Addon.model_validate(
        {
            "apiVersion": "addons.neurwerk.com/v1alpha2",
            "kind": "AddonRelease",
            "metadata": {"name": "sample"},
            "spec": {
                "version": "1.0.0",
                "status": "staged",
                "sourceName": "sample",
                "configurationKey": "sample",
                "packages": [{"name": "app", "path": "app"}],
                "prerequisites": {"base": "0.4.0", "tooling": "0.14.0", "stackSetup": "0.7.0"},
                "features": {"stt": False, "tts": False},
                "setup": {
                    "records": [
                        record(
                            "speech",
                            {
                                direction: {
                                    "supplied": {"group": direction, "key": "token"},
                                    "when": direction,
                                }
                                for direction in ("stt", "tts")
                            },
                        )
                    ],
                    "inputGroups": [
                        {"id": direction, "when": direction, "fields": {"token": {}}}
                        for direction in ("stt", "tts")
                    ],
                },
            },
        }
    )
    selection = AddonSelection.model_validate(
        {
            "id": "sample",
            "sourceRef": {"kind": "GitRepository", "namespace": "flux-system", "name": "sample"},
            "features": {"stt": stt, "tts": tts},
        }
    )
    return expand_setup(addon, selection)


def test_disabled_fields_do_not_read_or_require_inputs_and_updates_preserve_siblings(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    assert plan_credentials(api, {"sample": speech_setup(False, False)}).writes == ()
    assert session.calls == []
    session.secrets["addons/sample/speech"] = StoredSecret(
        {"tts": "retained-tts", "unrelated": "retained"}
    )
    selected = {"sample": speech_setup(True, False)}
    plan = plan_credentials(api, selected, supplied={"sample": {"stt": {"token": "new-stt"}}})
    apply_credentials(api, plan)
    assert session.secrets["addons/sample/speech"].values == {
        "stt": "new-stt",
        "tts": "retained-tts",
        "unrelated": "retained",
    }
    assert plan_credentials(api, selected).writes == ()
    with pytest.raises(OpenBaoError, match="inactive input group"):
        plan_credentials(api, selected, supplied={"sample": {"tts": {"token": "not-enabled"}}})


def test_group_is_all_or_none_before_any_secret_read(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record(
                    "provider",
                    {
                        key: {"supplied": {"group": "provider", "key": key}}
                        for key in ("user", "password")
                    },
                )
            ],
            "inputGroups": [{"id": "provider", "fields": {"user": {}, "password": {}}}],
        }
    )
    session = FakeSession()
    with pytest.raises(OpenBaoError, match="incomplete or invalid"):
        plan_credentials(
            client(tmp_path, session),
            {"sample": setup},
            supplied={"sample": {"provider": {"user": "only-user"}}},
        )
    assert session.calls == []


def verifier_setup():
    return Setup.model_validate(
        {
            "records": [
                record("runtime", {"key": generated()}),
                record(
                    "verifiers",
                    {
                        "primary": {
                            "sha256": {"record": "runtime", "field": "key"},
                            "validation": {"format": "sha256"},
                        },
                        "secondary": {
                            "literal": "",
                            "validation": {"format": "sha256", "minLength": 0},
                        },
                    },
                ),
            ],
            "invariants": [
                {
                    "kind": "accepted-verifier-slots",
                    "source": {"record": "runtime", "field": "key"},
                    "primary": {"record": "verifiers", "field": "primary"},
                    "secondary": {"record": "verifiers", "field": "secondary"},
                }
            ],
        }
    )


@pytest.mark.parametrize("slot", ["primary", "secondary", "missing-primary"])
def test_existing_key_and_accepted_slots_are_preserved(tmp_path, slot):
    session = FakeSession()
    api = client(tmp_path, session)
    key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
    digest = hashlib.sha256(key.encode()).hexdigest()
    values = {"primary": "a" * 64, "secondary": ""}
    if slot == "missing-primary":
        del values["primary"]
    else:
        values[slot] = digest
    session.secrets["addons/sample/runtime"] = StoredSecret({"key": key})
    session.secrets["addons/sample/verifiers"] = StoredSecret(dict(values))
    selected = {"sample": verifier_setup()}
    apply_credentials(api, plan_credentials(api, selected))
    assert session.secrets["addons/sample/runtime"].values["key"] == key
    expected = {**values, **({"primary": digest} if slot == "missing-primary" else {})}
    assert session.secrets["addons/sample/verifiers"].values == expected
    assert plan_credentials(api, selected).writes == ()


def test_mismatched_slots_stop_all_writes_without_exposing_key(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    session.secrets["addons/sample/verifiers"] = StoredSecret(
        {"primary": "a" * 64, "secondary": ""}
    )
    with pytest.raises(OpenBaoError, match="accepted verifier slot"):
        plan_credentials(api, {"sample": verifier_setup()})
    assert credential_writes(session) == []


def test_sha256_encoding_failure_is_redacted_before_mutation(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record("source", {"value": {"supplied": {"group": "provider", "key": "token"}}}),
                record("digest", {"value": {"sha256": {"record": "source", "field": "value"}}}),
            ],
            "inputGroups": [{"id": "provider", "fields": {"token": {}}}],
        }
    )
    session = FakeSession()
    session.secrets["addons/sample/source"] = StoredSecret({"value": "synthetic-private-\ud800"})
    with pytest.raises(OpenBaoError, match="not valid UTF-8") as error:
        plan_credentials(client(tmp_path, session), {"sample": setup})
    assert "synthetic-private" not in str(error.value)
    assert credential_writes(session) == []


def test_prepared_output_is_create_only_and_missing_or_invalid_values_fail(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [
                record(
                    "native",
                    {"token": {"preparedOutput": "native-token", "validation": {"minLength": 5}}},
                )
            ]
        }
    )
    session = FakeSession()
    api = client(tmp_path, session)
    selected = {"sample": setup}
    with pytest.raises(OpenBaoError, match="prevalidated prepared output"):
        plan_credentials(api, selected)
    plan = plan_credentials(api, selected, prepared={"sample": {"native-token": "synthetic-token"}})
    apply_credentials(api, plan)
    assert (
        plan_credentials(
            api, selected, prepared={"sample": {"native-token": "another-valid-token"}}
        ).writes
        == ()
    )
    session.secrets["addons/sample/native"] = StoredSecret({"token": "bad"})
    with pytest.raises(OpenBaoError, match="constraints"):
        plan_credentials(api, selected, prepared={"sample": {"native-token": "replacement"}})


def test_acknowledged_handover_persists_before_clear_and_conflicts_stop(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [record("admin", {"password": generated()})],
            "handover": [
                {
                    "id": "admin",
                    "label": "Sample admin",
                    "value": {"record": "admin", "field": "password"},
                }
            ],
        }
    )
    selected = {"sample": setup}
    definitions = {"sample": "verified-definition"}
    session = FakeSession()
    api = client(tmp_path, session)
    recovery_file = tmp_path / "recovery.json"
    kit = initialized_kit()
    write_new(recovery_file, kit)
    with patch("openbao_stack_setup.main._deliver_bootstrap_passwords"):
        kit, pending = _prepare_bootstrap_passwords(
            api, kit, recovery_file, definitions=handover_definitions(selected, definitions)
        )
    with pytest.raises(OpenBaoError, match="not persisted"):
        complete_handover(api, kit, recovery_file)
    assert load(recovery_file).pending_bootstrap_passwords == pending
    with pytest.raises(OpenBaoError, match="acknowledgement"):
        plan_credentials(
            api,
            selected,
            handover=replace(kit, bootstrap_passwords_acknowledged=False),
            definitions=definitions,
        )
    session.secrets["addons/sample/admin"] = StoredSecret({"password": "conflicting-value"})
    with pytest.raises(OpenBaoError, match="conflicts"):
        plan_credentials(api, selected, handover=kit, definitions=definitions)
    del session.secrets["addons/sample/admin"]
    plan = plan_credentials(api, selected, handover=kit, definitions=definitions)
    apply_credentials(api, plan)
    complete_handover(api, kit, recovery_file)
    assert load(recovery_file).pending_bootstrap_passwords is None
    assert session.secrets["addons/sample/admin"].values["password"] == pending["sample/admin"]


def test_base_reader_union_survives_reconciliation_and_addon_deselection(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [record("oidc", {"secret": generated()})],
            "readers": [
                {
                    "role": "auth-keycloak",
                    "namespace": "auth-keycloak",
                    "serviceAccount": "auth-keycloak-external-secrets",
                    "audience": "openbao",
                    "records": ["oidc"],
                }
            ],
        }
    )
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    readers = compose_readers({"sample": setup})
    access = plan_reconciliation_access(api, identity(), readers)
    reconcile_openbao(api, identity(), passwords())
    base_policy = session.policies["auth-keycloak"]
    apply_access(api, access)
    complete_access(api, access)
    policy = access.intended[0].policy
    assert all(path in str(session.policies[policy]) for path in readers[0].records)
    role = api.read_kubernetes_role("auth-keycloak")
    assert role is not None and role["token_policies"] == ["auth-keycloak", policy]
    removed = plan_reconciliation_access(api, identity(), ())
    reconcile_openbao(api, identity())
    apply_access(api, removed)
    complete_access(api, removed)
    assert policy not in session.policies
    assert session.policies["auth-keycloak"] == base_policy
    role = api.read_kubernetes_role("auth-keycloak")
    assert role is not None and role["token_policies"] == ["auth-keycloak"]


def test_unsupported_handover_recipe_stops_before_secret_reads(tmp_path):
    setup = Setup.model_validate(
        {
            "records": [record("admin", {"password": generated("hex")})],
            "handover": [
                {
                    "id": "admin",
                    "label": "Sample admin",
                    "value": {"record": "admin", "field": "password"},
                }
            ],
        }
    )
    session = FakeSession()
    with pytest.raises(OpenBaoError, match="32-byte base64url generation"):
        plan_credentials(
            client(tmp_path, session), {"sample": setup}, definitions={"sample": "verified"}
        )
    assert session.calls == []
