"""Exercise source verification and credential-only workstation setup."""

import json
from contextlib import contextmanager
from dataclasses import replace
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from fake import FakeSession
from k8s_stack_tooling.addons import Addon, VerifiedSelection
from k8s_stack_tooling.addons.schema import AddonSelection, Selection, SourceRef
from k8s_stack_tooling.addons.sources import SelectedAddon, VerifiedSource
from test_reconcile import client, source
from test_recovery import initialized_kit

from openbao_stack_setup import main as cli
from openbao_stack_setup import selection as selected
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.cluster import StackIdentity
from openbao_stack_setup.providers import PROVIDERS
from openbao_stack_setup.recovery import load, with_checkpoint, write_new
from openbao_stack_setup.selection import SelectedRun


def verified(*, native=False, addon_id="sample", stack_setup=None):
    setup: dict[str, Any] = {
        "records": [
            {
                "id": "runtime",
                "path": f"addons/{addon_id}/runtime",
                "fields": {
                    "key": {"generate": {"encoding": "base64url", "bytes": 32}},
                    "password": {"generate": {"encoding": "base64url", "bytes": 32}},
                },
            }
        ],
        "handover": [
            {
                "id": "admin",
                "label": "Sample admin",
                "value": {"record": "runtime", "field": "password"},
            }
        ],
        "readers": [
            {
                "role": "auth-keycloak",
                "namespace": "auth-keycloak",
                "serviceAccount": "auth-keycloak-external-secrets",
                "audience": "openbao",
                "records": ["runtime"],
            }
        ],
        "deliveries": [
            {
                "namespace": addon_id,
                "name": "runtime",
                "secretStore": "bao",
                "targetSecret": "runtime",
                "records": ["runtime"],
            }
        ],
    }
    spec = {
        "version": "1.0.0",
        "status": "ready",
        "sourceName": addon_id,
        "configurationKey": addon_id,
        "prerequisites": {
            "base": "0.4.0",
            "tooling": "0.15.0",
            "stackSetup": stack_setup or distribution_version("openbao-stack-setup"),
        },
        "packages": [{"name": "app", "path": "app"}],
        "setup": setup,
    }
    if native:
        cast(dict[str, object], setup["records"][0]["fields"])["token"] = {
            "preparedOutput": "native-token"
        }
        spec["artifacts"] = [
            {
                "id": "native",
                "role": "native",
                "image": "registry.example/native:1.0.0@sha256:" + "a" * 64,
            }
        ]
        spec["nativeOutputs"] = [
            {
                "id": "native-token",
                "artifact": "native",
                "argv": ["/usr/local/bin/native", "generate"],
                "idleArgv": ["/bin/sleep", "300"],
                "outputKey": "token",
                "validation": {},
            }
        ]
    definition = Addon.model_validate(
        {
            "apiVersion": "addons.neurwerk.com/v1alpha2",
            "kind": "AddonRelease",
            "metadata": {"name": addon_id},
            "spec": spec,
        }
    )
    ref = SourceRef(kind="GitRepository", namespace="flux-system", name=addon_id)
    choice = AddonSelection(
        id=addon_id, sourceRef=ref, approvedReaders=definition.spec.setup.readers
    )
    base_ref = SourceRef(kind="GitRepository", namespace="flux-system", name="base")
    selection = Selection(
        apiVersion="addons.neurwerk.com/v1alpha2",
        kind="AddonSelection",
        baseSourceRef=base_ref,
        addons=[choice],
    )
    origin = VerifiedSource(ref, f"https://example.invalid/{addon_id}", "a" * 40, "b" * 64)
    base = VerifiedSource(base_ref, "https://example.invalid/base", "c" * 40, "d" * 64)
    return VerifiedSelection(
        selection,
        base,
        (SelectedAddon(definition, choice, origin, Path("/") / addon_id, {}, definition),),
        True,
        True,
        "clusters/example",
    )


def arguments(command="reconcile"):
    return [
        command,
        "--context",
        "test",
        "--client",
        "client",
        "--custodian-package",
        "/one",
        "--custodian-package",
        "/two",
    ]


def test_cli_needs_no_source_ceremony_and_has_no_candidate_or_plan_escape():
    parsed = cli._arguments().parse_args(arguments())
    assert parsed.context == "test" and parsed.client == "client"
    for argv in (
        [*arguments(), "--operation-id", "obsolete"],
        [*arguments(), "--source-cache", "/cache"],
        [*arguments(), "--candidate"],
        [*arguments(), "--credential-plan", "/plan.json"],
    ):
        with pytest.raises(SystemExit) as error:
            cli._arguments().parse_args(argv)
        assert error.value.code == 2


@pytest.mark.parametrize("flag", ["installable", "projection_verified"])
def test_failed_verification_flag_stops_cli_before_custody_or_root(monkeypatch, tmp_path, flag):
    monkeypatch.setattr("sys.argv", ["stack-setup", *arguments()])
    cluster = MagicMock()
    (tmp_path / ".git").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/addons.yaml").touch()
    monkeypatch.chdir(tmp_path)
    cluster.selection_projection.return_value = {
        "data": {"selection.json": json.dumps({"clusterDirectory": "clusters/example"})}
    }
    with (
        patch.object(cli, "Cluster", return_value=cluster),
        patch.object(selected, "acquire_selection"),
        patch.object(
            selected, "source_checkouts", return_value=(Path("/base"), {"sample": Path("/sample")})
        ),
        patch.object(
            selected, "verify_selection", return_value=replace(verified(), **{flag: False})
        ),
        patch.object(cli, "prepare_custody_paths") as custody,
        patch.object(cli, "_openbao") as openbao,
        pytest.raises(SystemExit) as error,
    ):
        cli.main()
    assert error.value.code == 1
    custody.assert_not_called()
    openbao.assert_not_called()


def test_complete_source_verifier_gets_live_projection_and_installable_mode(tmp_path, monkeypatch):
    cluster = MagicMock()
    (tmp_path / ".git").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/addons.yaml").touch()
    monkeypatch.chdir(tmp_path)
    projection = {
        "kind": "ConfigMap",
        "data": {"selection.json": json.dumps({"clusterDirectory": "clusters/example"})},
    }
    cluster.selection_projection.return_value = projection
    checkouts = {"sample": Path("/sample")}
    with (
        patch.object(selected, "acquire_selection") as acquire,
        patch.object(
            selected, "source_checkouts", return_value=(Path("/base"), checkouts)
        ) as discover,
        patch.object(selected, "verify_selection", return_value=verified()) as verify,
    ):
        selected.verify_client_sources(cluster)
    cache = acquire.call_args.kwargs["cache"]
    acquire.assert_called_once_with(tmp_path, cluster_dir="clusters/example", cache=cache)
    discover.assert_called_once_with(tmp_path, cluster_dir="clusters/example", cache=cache)
    verify.assert_called_once_with(
        tmp_path,
        checkouts,
        cluster_dir="clusters/example",
        base=Path("/base"),
        installable=True,
        staged_projection=projection,
    )


@pytest.mark.parametrize(
    ("required", "installed"),
    [
        ("0.6.6", "0.7.0"),
        ("0.7.1", "0.7.0"),
        ("1.0.0", "0.7.0"),
        ("0.7.0", "0.6.6"),
        ("0.7.0", "0.8.0"),
        ("0.8.1", "0.8.0"),
        ("0.8.0", "0.9.0"),
    ],
)
def test_incompatible_setup_versions_rejected_before_privilege(
    monkeypatch, tmp_path, capsys, required, installed
):
    first = verified(native=True, stack_setup=installed)
    second = verified(addon_id="other", stack_setup=required)
    combined = replace(
        first,
        addons=(*first.addons, *second.addons),
        selection=first.selection.model_copy(
            update={"addons": [*first.selection.addons, *second.selection.addons]}
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["stack-setup", *arguments(), "--confirm", "client"],
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config/addons.yaml").touch()
    monkeypatch.chdir(tmp_path)
    cluster = MagicMock()
    cluster.selection_projection.return_value = {
        "data": {"selection.json": json.dumps({"clusterDirectory": "clusters/example"})}
    }
    with (
        patch.object(cli, "Cluster", return_value=cluster),
        patch.object(selected, "acquire_selection"),
        patch.object(
            selected,
            "source_checkouts",
            return_value=(Path("/base"), {"sample": Path("/sample"), "other": Path("/other")}),
        ),
        patch.object(selected, "verify_selection", return_value=combined),
        patch.object(selected, "distribution_version", return_value=installed) as package_version,
        patch.object(selected, "prepare_native_output") as native,
        patch.object(cli, "prepare_custody_paths") as custody,
        patch.object(cli, "_openbao") as openbao,
        patch.object(cli, "_reconcile") as reconcile,
        pytest.raises(SystemExit) as error,
    ):
        cli.main()
    assert error.value.code == 1
    package_version.assert_called_once_with("openbao-stack-setup")
    assert (
        f"requires stack-setup {required}; installed package is {installed}"
        in capsys.readouterr().out
    )
    native.assert_not_called()
    custody.assert_not_called()
    openbao.assert_not_called()
    reconcile.assert_not_called()


@pytest.mark.parametrize(
    ("required", "installed"),
    [("0.8.0", "0.8.0"), ("0.8.0", "0.8.1"), ("0.8.9", "0.8.10")],
)
def test_setup_accepts_exact_or_newer_patch_for_every_selected_addon(required, installed):
    first = verified(stack_setup=required)
    second = verified(addon_id="other", stack_setup=installed)
    combined = replace(
        first,
        addons=(*first.addons, *second.addons),
        selection=first.selection.model_copy(
            update={"addons": [*first.selection.addons, *second.selection.addons]}
        ),
    )
    run = SelectedRun(combined)
    fingerprint = run.fingerprint
    with patch.object(selected, "distribution_version", return_value=installed):
        run.require_verified()
        assert run.fingerprint == fingerprint
    assert run.verified == combined


@pytest.mark.parametrize("installed", ["0.8.1rc1", "0.8.1.dev1", "0.8.1+local", "0.8", "00.8.1"])
def test_setup_rejects_invalid_installed_version(installed):
    with (
        patch.object(selected, "distribution_version", return_value=installed),
        pytest.raises(OpenBaoError, match="package version is invalid"),
    ):
        SelectedRun(verified(stack_setup="0.8.0")).require_verified()


def test_setup_version_guard_also_protects_direct_native_preparation():
    definition = verified(native=True, stack_setup="0.8.0")
    with (
        patch.object(selected, "distribution_version", return_value="0.7.0"),
        patch.object(selected, "prepare_native_output") as native,
        pytest.raises(OpenBaoError, match=r"requires stack-setup 0\.8\.0"),
    ):
        selected.prepare_native_outputs(
            MagicMock(), SelectedRun(definition), {("sample", "native-token")}
        )
    native.assert_not_called()


def test_missing_installed_setup_metadata_fails_closed_before_native():
    definition = verified(native=True)
    with (
        patch.object(selected, "distribution_version", side_effect=PackageNotFoundError),
        patch.object(selected, "prepare_native_output") as native,
        pytest.raises(OpenBaoError, match="package metadata is unavailable"),
    ):
        selected.prepare_native_outputs(
            MagicMock(), SelectedRun(definition), {("sample", "native-token")}
        )
    native.assert_not_called()


def test_native_preparation_uses_declared_delivery_namespace():
    run = SelectedRun(verified(native=True))
    with (
        patch.object(
            selected,
            "prepare_native_output",
            return_value={"token": "synthetic-token"},
        ) as native,
    ):
        selected.prepare_native_outputs(MagicMock(), run, {("sample", "native-token")})
    assert native.call_args.kwargs["namespace"] == "sample"
    assert run.prepared == {"sample": {"native-token": "synthetic-token"}}


@pytest.mark.parametrize("initial", [False, True])
def test_complete_fake_client_preserves_keys_and_revokes_without_lifecycle(tmp_path, initial):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    for method in (
        "tailscale_enabled",
        "docling_enabled",
        "glitchtip_enabled",
        "rejection_debug_enabled",
    ):
        getattr(cluster, method).return_value = False
    cluster.contextforge_admin_email.return_value = None
    cluster.studio_mcp_ids.return_value = None
    kit = initialized_kit()
    if not initial:
        kit = with_checkpoint(with_checkpoint(kit, "seeded"), "complete")
    recovery = tmp_path / "recovery.json"
    write_new(recovery, kit)
    paths = MagicMock(seal_file=recovery)

    @contextmanager
    def opened(_):
        yield api

    with (
        patch.object(cli, "Cluster", return_value=cluster),
        patch.object(cli, "prepare_custody_paths", return_value=paths),
        patch.object(cli, "_required_package_paths", return_value=(Path("/one"), Path("/two"))),
        patch.object(cli, "_decrypt_custodian_packages", return_value=("share-one", "share-two")),
        patch.object(api, "create_recovery_root_token", return_value="synthetic-root"),
        patch.object(cli, "_openbao", side_effect=opened),
        patch.object(cli, "_deliver_bootstrap_passwords"),
        patch.object(cli, "_verify_secret_operator"),
        patch.object(cli, "_revoke_other_root_tokens"),
        patch.object(
            cli,
            "_prompt_bootstrap_credentials",
            return_value=(
                {
                    name: dict.fromkeys(provider.fields, "synthetic-value")
                    for name, provider in PROVIDERS.items()
                },
                {"username": "smtp-user", "password": "smtp-password"},
                {},
            ),
        ),
        patch.object(
            selected, "prepare_native_output", return_value={"token": "synthetic-token"}
        ) as native,
    ):
        run = SelectedRun(verified(native=True))
        if initial:
            cli._seed_and_finish(cluster, api, "synthetic-root", kit, recovery, False, run=run)
        else:
            cli._reconcile("test", "client", tmp_path, [], "client", run=run)
        values = dict(session.secrets["addons/sample/runtime"].values)
        policies = session.kubernetes_roles["auth-keycloak"]
        assert isinstance(policies, dict)
        attached = policies["token_policies"]
        assert isinstance(attached, list) and len(attached) == 2
        assert attached[0] == "auth-keycloak"
        assert "addons/sample/runtime" in str(session.policies[str(attached[1])])
        cli._reconcile("test", "client", tmp_path, [], "client", run=SelectedRun(run.verified))
    assert session.secrets["addons/sample/runtime"].values == values
    assert session.kubernetes_roles["auth-keycloak"] == policies
    assert load(recovery).pending_bootstrap_passwords is None
    assert session.calls[-1].path == "auth/token/revoke-self"
    native.assert_called_once()
    cluster.force_external_secret_refresh.assert_not_called()
    cluster.force_reconcile.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_preservation_revokes_root_without_running_reconciliation(tmp_path, fails):
    session = FakeSession()
    api = client(tmp_path, session)
    cluster = MagicMock()
    cluster.identity.return_value = StackIdentity("client", "cluster", "namespace")
    kit = with_checkpoint(with_checkpoint(initialized_kit(), "seeded"), "complete")
    run = SelectedRun(verified(addon_id="librechat"))
    with (
        patch.object(cli, "Cluster", return_value=cluster),
        patch.object(cli, "prepare_custody_paths"),
        patch.object(cli, "_bound_kit", return_value=kit),
        patch.object(cli, "_required_package_paths", return_value=(Path("/one"), Path("/two"))),
        patch.object(cli, "_decrypt_custodian_packages", return_value=("share-one", "share-two")),
        patch.object(cli, "_openbao") as opened,
        patch.object(cli, "OpenBaoClient", return_value=api),
        patch.object(api, "create_recovery_root_token", return_value="synthetic-root"),
        patch.object(cli, "preserve_librechat") as preserve,
        patch.object(cli, "_prepare_selected") as reconcile,
    ):
        opened.return_value.__enter__.return_value = api
        if fails:
            preserve.side_effect = OpenBaoError("preservation conflict")
            with pytest.raises(OpenBaoError, match="preservation conflict"):
                cli._reconcile(
                    "test", "client", tmp_path, [], "client", run=run, preserve_only=True
                )
        else:
            cli._reconcile("test", "client", tmp_path, [], "client", run=run, preserve_only=True)
    preserve.assert_called_once()
    reconcile.assert_not_called()
    assert session.calls[-1].path == "auth/token/revoke-self"
