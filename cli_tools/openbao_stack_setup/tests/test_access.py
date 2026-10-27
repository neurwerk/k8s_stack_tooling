"""Exercise policy ownership and interrupted operations against CAS-enforcing storage."""

from dataclasses import replace
from pathlib import Path

import pytest
from fake import FakeSession, StoredSecret
from test_reconcile import client, identity, passwords, source, state_values

from openbao_stack_setup.access import AccessReader, apply_access, complete_access, reserve_access
from openbao_stack_setup.catalog import RECONCILIATION_STATE_PATH
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.reconcile import reconcile_openbao, reserve_reconciliation

READER = AccessReader(
    "sample-reader", "sample", "sample-eso", "openbao", ("addons/sample/verifier",)
)


def mutations(session):
    return [
        call
        for call in session.calls
        if call.method in {"POST", "DELETE"}
        and call.path.startswith(("sys/policies/acl/", "auth/kubernetes/role/"))
    ]


def test_stale_cas_loses_before_any_grants_change(tmp_path: Path):
    session = FakeSession()
    session.secrets[RECONCILIATION_STATE_PATH] = StoredSecret(state_values())
    api = client(tmp_path, session)
    snapshot = api.read_secret(RECONCILIATION_STATE_PATH)
    assert snapshot is not None
    first = reserve_access(api, snapshot, "first", "selection-a", (READER,))
    with pytest.raises(OpenBaoError, match="HTTP 400"):
        reserve_access(api, snapshot, "second", "selection-b", (READER,))
    assert mutations(session) == []
    apply_access(api, first)
    assert first.intended[0].policy in session.policies


@pytest.mark.parametrize(
    ("operation", "selection", "readers", "resume", "stopped"),
    [
        ("operation", "selection", (READER,), False, False),
        ("operation", "selection", (READER,), True, False),
        ("other", "selection", (READER,), True, True),
        ("operation", "other", (READER,), True, True),
        ("operation", "selection", (), True, True),
    ],
)
def test_pending_operation_conflicts_before_grants(
    tmp_path, operation, selection, readers, resume, stopped
):
    session = FakeSession()
    api = client(tmp_path, session)
    reserve_reconciliation(api, identity(), "operation", "selection", (READER,))
    version = session.secrets[RECONCILIATION_STATE_PATH].version
    with pytest.raises(OpenBaoError, match=r"pending|conflicts"):
        reserve_reconciliation(
            api,
            identity(),
            operation,
            selection,
            readers,
            resume=resume,
            previous_process_stopped=stopped,
        )
    assert session.secrets[RECONCILIATION_STATE_PATH].version == version
    assert mutations(session) == []


def test_interrupted_grants_resume_same_policy_and_invalidate_old_handle(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "operation", "selection", (READER,))
    session.failure = ("POST", "auth/kubernetes/role/sample-reader", 500, {})
    with pytest.raises(OpenBaoError, match="HTTP 500"):
        apply_access(api, original)
    assert original.intended[0].policy in session.policies
    assert READER.role not in session.kubernetes_roles
    session.failure = None
    resumed = reserve_reconciliation(
        api,
        identity(),
        "operation",
        "selection",
        (READER,),
        resume=True,
        previous_process_stopped=True,
    )
    assert resumed.intended == original.intended
    session.calls.clear()
    for operation in (apply_access, complete_access):
        with pytest.raises(OpenBaoError, match="reservation changed"):
            operation(api, original)
    assert mutations(session) == []
    apply_access(api, resumed)
    complete_access(api, resumed)
    assert READER.role in session.kubernetes_roles


def test_stable_reader_rerun_has_no_policy_or_role_churn(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selection", (READER,))
    apply_access(api, original)
    complete_access(api, original)
    session.calls.clear()
    again = reserve_reconciliation(api, identity(), "two", "selection", (READER,))
    apply_access(api, again)
    complete_access(api, again)
    assert again.intended == original.intended
    assert mutations(session) == []
    policy = str(session.policies[again.intended[0].policy])
    assert "addons/sample/verifier" in policy
    assert "addons/sample/raw-key" not in policy
    assert "*" not in policy


def test_deselection_shrinks_before_role_removal_and_reactivation_cannot_revive_policy(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    shared = replace(READER, records=("addons/one/database", "addons/two/database"))
    other = replace(READER, role="other-reader")
    original = reserve_reconciliation(api, identity(), "one", "both", (shared, other))
    apply_access(api, original)
    complete_access(api, original)
    original_policies = {item.reader.role: item.policy for item in original.intended}
    session.secrets["addons/sample/verifier"] = StoredSecret({"primary": "synthetic-verifier"})
    retained = replace(shared, records=("addons/two/database",))
    removal = reserve_reconciliation(api, identity(), "two", "one-product", (retained,))
    session.calls.clear()
    apply_access(api, removal)
    complete_access(api, removal)
    actions = mutations(session)
    role_index = next(i for i, call in enumerate(actions) if "auth/kubernetes/role/" in call.path)
    assert all(call.path.startswith("sys/policies/acl/") for call in actions[:role_index])
    assert actions[role_index].method == "DELETE"
    assert original_policies[other.role] not in session.policies
    assert "addons/one/database" not in str(session.policies[original_policies[shared.role]])
    assert "addons/two/database" in str(session.policies[original_policies[shared.role]])
    assert session.secrets["addons/sample/verifier"].version == 1
    reactivation = reserve_reconciliation(api, identity(), "three", "both-again", (retained, other))
    policies = {item.reader.role: item.policy for item in reactivation.intended}
    assert policies[shared.role] == original_policies[shared.role]
    assert policies[other.role] != original_policies[other.role]
    apply_access(api, reactivation)
    complete_access(api, reactivation)
    # A token carrying the old removed policy still has no policy to grant it access.
    assert original_policies[other.role] not in session.policies


def test_policy_removal_failure_preserves_role_and_pending_intent(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selected", (READER,))
    apply_access(api, original)
    complete_access(api, original)
    removal = reserve_reconciliation(api, identity(), "two", "removed", ())
    session.failure = ("DELETE", f"sys/policies/acl/{original.intended[0].policy}", 500, {})
    with pytest.raises(OpenBaoError, match="HTTP 500"):
        apply_access(api, removal)
    assert READER.role in session.kubernetes_roles
    session.failure = None
    resumed = reserve_reconciliation(
        api, identity(), "two", "removed", (), resume=True, previous_process_stopped=True
    )
    apply_access(api, resumed)
    complete_access(api, resumed)
    assert READER.role not in session.kubernetes_roles


def test_base_reconciliation_preserves_pending_ownership_and_requires_handle(tmp_path):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reserved = reserve_reconciliation(api, identity(), "one", "selection", (READER,))
    with pytest.raises(OpenBaoError, match="explicit reservation"):
        reconcile_openbao(api, identity(), passwords())
    assert mutations(session) == []
    report = reconcile_openbao(api, identity(), passwords(), reservation=reserved)
    assert session.secrets[RECONCILIATION_STATE_PATH].version == reserved.version
    apply_access(api, reserved)
    complete_access(api, reserved, applied_version=report.applied_version)
    assert session.secrets[RECONCILIATION_STATE_PATH].values["appliedVersion"] == 4
    again = reserve_reconciliation(api, identity(), "two", "selection", (READER,))
    report = reconcile_openbao(api, identity(), reservation=again)
    assert report.internal_fields_added == 0
    complete_access(api, again, applied_version=report.applied_version)


def test_changed_binding_and_wrong_cluster_cannot_reserve(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selection", (READER,))
    apply_access(api, original)
    complete_access(api, original)
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="binding changed"):
        reserve_reconciliation(
            api, identity(), "two", "changed", (replace(READER, namespace="other"),)
        )
    with pytest.raises(OpenBaoError, match="does not belong"):
        reserve_reconciliation(api, replace(identity(), cluster_id="other"), "two", "same", ())
    assert mutations(session) == []


def test_base_role_collision_fails_before_reservation(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    with pytest.raises(OpenBaoError, match="collides with Base"):
        reserve_reconciliation(
            api, identity(), "one", "selection", (replace(READER, role="secret-operator"),)
        )
    assert all(call.method == "GET" for call in session.calls)


def live_role(session, name):
    role = session.kubernetes_roles[name]
    assert isinstance(role, dict)
    return role


@pytest.mark.parametrize("role", ["auth-keycloak", "infra-postgres-operations"])
def test_shared_base_role_reattaches_on_rerun_and_retains_base_on_deselection(tmp_path, role):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    base_policy = session.policies[role]
    reader = AccessReader(role, role, f"{role}-external-secrets", "openbao", READER.records)
    original = reserve_reconciliation(api, identity(), "one", "selected", (reader,))
    apply_access(api, original)
    complete_access(api, original)
    policy = original.intended[0].policy
    assert live_role(session, role)["token_policies"] == [role, policy]

    again = reserve_reconciliation(api, identity(), "two", "selected", (reader,))
    reconcile_openbao(api, identity(), reservation=again)
    assert live_role(session, role)["token_policies"] == [role]
    session.calls.clear()
    apply_access(api, again)
    complete_access(api, again)
    assert again.intended == original.intended
    assert live_role(session, role)["token_policies"] == [role, policy]
    assert not any(call.path == f"sys/policies/acl/{policy}" for call in mutations(session))

    removal = reserve_reconciliation(api, identity(), "three", "removed", ())
    session.calls.clear()
    apply_access(api, removal)
    complete_access(api, removal)
    assert session.policies[role] == base_policy
    assert live_role(session, role)["token_policies"] == [role]
    assert policy not in session.policies
    assert not any(
        call.method == "DELETE" and "auth/kubernetes/role/" in call.path for call in session.calls
    )
    reactivated = reserve_reconciliation(api, identity(), "four", "selected", (reader,))
    assert reactivated.intended[0].policy != policy


@pytest.mark.parametrize("policy", ["foreign-policy", "stack-setup-addon-" + "a" * 32])
def test_unowned_existing_nonbase_role_is_rejected_before_any_write(tmp_path, policy):
    session = FakeSession()
    api = client(tmp_path, session)
    session.kubernetes_roles[READER.role] = {
        "bound_service_account_names": [READER.service_account],
        "bound_service_account_namespaces": [READER.namespace],
        "audience": READER.audience,
        "token_policies": [policy],
    }
    with pytest.raises(OpenBaoError, match="not owned"):
        reserve_reconciliation(api, identity(), "one", "selected", (READER,))
    assert all(call.method == "GET" for call in session.calls)
    assert RECONCILIATION_STATE_PATH not in session.secrets
    assert live_role(session, READER.role)["token_policies"] == [policy]


def test_pending_creation_can_resume_exact_live_intent_but_not_foreign_attachment(tmp_path):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selected", (READER,))
    apply_access(api, original)
    policy = original.intended[0].policy
    live_role(session, READER.role)["token_policies"] = [policy, "foreign-policy"]
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="ownership conflicts"):
        reserve_reconciliation(
            api,
            identity(),
            "one",
            "selected",
            (READER,),
            resume=True,
            previous_process_stopped=True,
        )
    assert all(call.method == "GET" for call in session.calls)
    live_role(session, READER.role)["token_policies"] = [policy]
    resumed = reserve_reconciliation(
        api,
        identity(),
        "one",
        "selected",
        (READER,),
        resume=True,
        previous_process_stopped=True,
    )
    apply_access(api, resumed)
    complete_access(api, resumed)
    assert resumed.intended == original.intended


@pytest.mark.parametrize("drift", ["namespace", "policy", "selector"])
def test_owned_role_drift_blocks_deselection_before_writes(tmp_path, drift):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selected", (READER,))
    apply_access(api, original)
    complete_access(api, original)
    role = live_role(session, READER.role)
    if drift == "namespace":
        role["bound_service_account_namespaces"] = ["another-namespace"]
    elif drift == "policy":
        role["token_policies"] = ["foreign-policy"]
    else:
        role["bound_service_account_namespace_selector"] = "matchLabels: {}"
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="ownership conflicts"):
        reserve_reconciliation(api, identity(), "two", "removed", ())
    assert all(call.method == "GET" for call in session.calls)
    assert original.intended[0].policy in session.policies


def test_role_ownership_is_rechecked_immediately_before_deletion(tmp_path, monkeypatch):
    session = FakeSession()
    api = client(tmp_path, session)
    original = reserve_reconciliation(api, identity(), "one", "selected", (READER,))
    apply_access(api, original)
    complete_access(api, original)
    removal = reserve_reconciliation(api, identity(), "two", "removed", ())
    delete_policy = api.delete_policy

    def replace_role_after_policy_removal(name):
        delete_policy(name)
        live_role(session, READER.role)["bound_service_account_names"] = ["foreign-account"]

    monkeypatch.setattr(api, "delete_policy", replace_role_after_policy_removal)
    session.calls.clear()
    with pytest.raises(OpenBaoError, match="ownership conflicts"):
        apply_access(api, removal)
    assert live_role(session, READER.role)["bound_service_account_names"] == ["foreign-account"]
    assert not any(
        call.method == "DELETE" and "auth/kubernetes/role/" in call.path for call in session.calls
    )


@pytest.mark.parametrize("wrong", ["namespace", "service_account", "audience", "policies"])
def test_base_identity_and_attachments_are_fixed_not_discovered(tmp_path, wrong):
    session = FakeSession()
    api = client(tmp_path, session)
    api.write_kubernetes_role("auth-keycloak")
    reader = AccessReader(
        "auth-keycloak",
        "auth-keycloak",
        "auth-keycloak-external-secrets",
        "openbao",
        READER.records,
    )
    if wrong == "policies":
        live_role(session, reader.role)["token_policies"] = ["auth-keycloak", "foreign-policy"]
    else:
        reader = replace(reader, **{wrong: "foreign"})
    session.calls.clear()
    with pytest.raises(OpenBaoError, match=r"fixed binding|ownership conflicts"):
        reserve_reconciliation(api, identity(), "one", "selected", (reader,))
    assert all(call.method == "GET" for call in session.calls)
