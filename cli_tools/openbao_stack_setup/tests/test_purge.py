"""Destructive cleanup stays scoped, revokes grants first, and supports partial retries."""

import argparse
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from fake import FakeSession, StoredSecret
from test_main import kit, opened
from test_reconcile import client, identity, state_values

from openbao_stack_setup import main
from openbao_stack_setup.access import AccessReader, apply_access, complete_access
from openbao_stack_setup.catalog import RECONCILIATION_STATE_PATH
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.custody import CustodyPaths
from openbao_stack_setup.purge import purge_addon, removed_addon_prefix
from openbao_stack_setup.reconcile import plan_reconciliation_access


@pytest.fixture
def stored(tmp_path):
    session = FakeSession()
    session.secrets[RECONCILIATION_STATE_PATH] = StoredSecret(state_values())
    api = client(tmp_path, session)
    readers = (
        AccessReader("sample", "sample", "sample-eso", "openbao", ("addons/sample/runtime",)),
        AccessReader(
            "auth-keycloak",
            "auth-keycloak",
            "auth-keycloak-external-secrets",
            "openbao",
            ("addons/other/oidc", "addons/sample/oidc"),
        ),
    )
    plan = plan_reconciliation_access(api, identity(), readers)
    apply_access(api, plan)
    complete_access(api, plan)
    for path in (
        "addons/sample/runtime",
        "addons/sample/oidc",
        "addons/sample/nested/token",
        "addons/sample-extra/runtime",
        "addons/other/oidc",
    ):
        session.secrets[path] = StoredSecret({"value": "synthetic"}, version=3)
    session.calls.clear()
    return api, session, plan


def run(api, **kwargs):
    return purge_addon(
        api, identity(), "sample", ("other",), verify_selection=MagicMock(), **kwargs
    )


def test_purge_all_versions_preserves_shared_grants_and_prefix_neighbours(stored):
    api, session, plan = stored
    assert run(api) == 3
    assert "sample" not in session.kubernetes_roles
    assert "auth-keycloak" in session.kubernetes_roles
    shared = next(item for item in plan.intended if item.reader.role == "auth-keycloak")
    assert "addons/other/oidc" in str(session.policies[shared.policy])
    assert "addons/sample/oidc" not in str(session.policies[shared.policy])
    assert session.secrets["addons/other/oidc"].version == 3
    assert session.secrets["addons/sample-extra/runtime"].version == 3
    deleted = [call.path for call in session.calls if call.method == "DELETE"]
    assert "secret/metadata/addons/sample/nested/token" in deleted
    assert not any(path.startswith("secret/data/") for path in deleted)
    assert run(api) == 0


@pytest.mark.parametrize("stage", ["plan_removed_addon_access", "apply_access", "complete_access"])
def test_grant_failure_never_deletes_records(stored, stage):
    api, session, _ = stored
    with (
        patch(f"openbao_stack_setup.purge.{stage}", side_effect=OpenBaoError("failed")),
        pytest.raises(OpenBaoError),
    ):
        run(api)
    assert not any(c.method == "DELETE" and c.path.startswith("secret/") for c in session.calls)


def test_partial_delete_retry_and_empty_target(stored):
    api, session, _ = stored
    session.failure = ("DELETE", "secret/metadata/addons/sample/oidc", 500, {})
    with pytest.raises(OpenBaoError, match="HTTP 500"):
        run(api)
    assert "addons/sample/nested/token" not in session.secrets
    session.failure = None
    assert run(api) == 2
    assert purge_addon(api, identity(), "empty", (), verify_selection=MagicMock()) == 0


@pytest.mark.parametrize("addon", ["../other", "sample/../other", "/sample", "sample?x", ""])
def test_invalid_target_rejected(addon):
    with pytest.raises(OpenBaoError):
        removed_addon_prefix(addon, ())


def test_selected_target_and_wrong_cluster_cannot_mutate(stored):
    api, session, _ = stored
    with pytest.raises(OpenBaoError, match="deployed selection"):
        purge_addon(api, identity(), "sample", ("sample",), verify_selection=MagicMock())
    with pytest.raises(OpenBaoError, match="selected cluster"):
        purge_addon(
            api, replace(identity(), client="other"), "sample", (), verify_selection=MagicMock()
        )
    assert not any(c.method in {"POST", "DELETE"} for c in session.calls)


@pytest.mark.parametrize("key", ["../other", "/other", "nested/../other", "a?b", "a#b", ".", 1])
def test_bad_metadata_listing_prevents_mutation(stored, key):
    api, session, _ = stored
    session.failure = ("LIST", "secret/metadata/addons/sample", 200, {"data": {"keys": [key]}})
    with pytest.raises(OpenBaoError, match="invalid KV"):
        run(api)
    assert not any(c.method in {"POST", "DELETE"} for c in session.calls)


def test_listing_failure_is_not_treated_as_empty(stored):
    api, session, _ = stored
    session.failure = ("LIST", "secret/metadata/addons/sample", 403, {})
    with pytest.raises(OpenBaoError, match="HTTP 403"):
        run(api)
    assert not any(c.method in {"POST", "DELETE"} for c in session.calls)


def test_selection_change_before_deletion_preserves_records(stored):
    api, session, _ = stored
    verify = MagicMock(side_effect=[None, OpenBaoError("selection changed")])
    with pytest.raises(OpenBaoError, match="selection changed"):
        purge_addon(api, identity(), "sample", (), verify_selection=verify)
    assert not any(c.method == "DELETE" and c.path.startswith("secret/") for c in session.calls)


@pytest.mark.parametrize("failure", [False, True])
def test_cli_always_revokes_temporary_root(tmp_path, monkeypatch, failure):
    run = MagicMock()
    run.setups = {}
    cluster = MagicMock()
    cluster.selection_projection.return_value = {"data": {"selection.json": "{}"}}
    api, root = MagicMock(), MagicMock()
    monkeypatch.setattr(main, "selection_projection", lambda _: {})
    monkeypatch.setattr(main, "Cluster", lambda _: cluster)
    monkeypatch.setattr(main, "_confirm", MagicMock())
    monkeypatch.setattr(
        main, "prepare_custody_paths", lambda _: CustodyPaths(tmp_path, tmp_path / "seal", tmp_path)
    )
    monkeypatch.setattr(main, "_bound_kit", lambda *_: kit("complete"))
    monkeypatch.setattr(main, "_required_package_paths", lambda paths: tuple(paths))
    monkeypatch.setattr(main, "_decrypt_custodian_packages", lambda *_: ("one", "two"))
    monkeypatch.setattr(main, "_openbao", lambda _: opened(api))
    monkeypatch.setattr(main, "OpenBaoClient", lambda *_: root)
    purge = MagicMock(return_value=2, side_effect=OpenBaoError("failed") if failure else None)
    monkeypatch.setattr(main, "purge_addon", purge)
    args = argparse.Namespace(
        addon="sample",
        context="ctx",
        client="client",
        confirm="client",
        custody_root=tmp_path,
        custodian_package=[tmp_path / "one", tmp_path / "two"],
    )
    if failure:
        with pytest.raises(OpenBaoError):
            main._purge_addon(args, run)
    else:
        main._purge_addon(args, run)
    root.revoke_self.assert_called_once_with()
    # A pending handover must stop before decrypting shares or opening privileged access.
    monkeypatch.setattr(
        main,
        "_bound_kit",
        lambda *_: replace(kit("complete"), pending_bootstrap_passwords={"sample/admin": "test"}),
    )
    api.reset_mock()
    with pytest.raises(main.SetupError, match="handover"):
        main._purge_addon(args, run)
    api.create_recovery_root_token.assert_not_called()


@pytest.mark.parametrize("selected,live", [(True, "{}"), (False, "invalid"), (False, "[]")])
def test_cli_refuses_selected_or_changed_projection_before_custody(monkeypatch, selected, live):
    run = MagicMock()
    run.setups = {"sample": MagicMock()} if selected else {}
    cluster = MagicMock()
    cluster.selection_projection.return_value = {"data": {"selection.json": live}}
    custody = MagicMock()
    monkeypatch.setattr(main, "selection_projection", lambda _: {})
    monkeypatch.setattr(main, "Cluster", lambda _: cluster)
    monkeypatch.setattr(main, "prepare_custody_paths", custody)
    args = argparse.Namespace(addon="sample", context="ctx", client="client")
    with pytest.raises((OpenBaoError, main.SetupError)):
        main._purge_addon(args, run)
    custody.assert_not_called()
