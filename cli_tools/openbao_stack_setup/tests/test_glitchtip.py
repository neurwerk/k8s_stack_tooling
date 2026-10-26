from __future__ import annotations

import json
import re
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from fake import FakeSession, StoredSecret
from test_cluster import cluster as configured_cluster
from test_reconcile import client, identity, passwords, source

from openbao_stack_setup import glitchtip
from openbao_stack_setup import main as cli
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.cluster import Cluster, ClusterError
from openbao_stack_setup.reconcile import reconcile_openbao

DSN = "https://0123456789abcdef0123456789abcdef@glitchtip.test/7"


def test_dsn_handoff_changes_only_approved_field_and_preserves_siblings_and_version(tmp_path):
    session = FakeSession()
    session.secrets[glitchtip.ADAPTER_PATH] = StoredSecret(
        {
            "dsn": "",
            "opensearchUsername": "glitchtip-adapter",
            "opensearchPassword": "synthetic-password",
            "futureSibling": "keep",
        },
        version=9,
    )
    session.secrets["unrelated/record"] = StoredSecret({"doNotChange": "keep"})
    api = client(tmp_path, session)
    before = deepcopy(session.secrets)
    glitchtip.store_project_dsn(api, DSN)
    expected = deepcopy(before)
    expected[glitchtip.ADAPTER_PATH].values["dsn"] = DSN
    expected[glitchtip.ADAPTER_PATH].version += 1
    assert session.secrets == expected
    glitchtip.store_project_dsn(api, DSN)
    assert session.secrets == expected
    posts = [call for call in session.calls if call.method == "POST"]
    assert len(posts) == 1
    assert posts[0].path == "secret/data/glitchtip/adapter"
    assert isinstance(posts[0].body, dict)
    assert posts[0].body["options"] == {"cas": 9}


@pytest.mark.parametrize("record", [None, {"dsn": "another-existing-dsn"}, {"sibling": "keep"}])
def test_missing_catalog_or_different_existing_dsn_never_mutates_records(tmp_path, record):
    session = FakeSession()
    if record is not None:
        session.secrets[glitchtip.ADAPTER_PATH] = StoredSecret(record)
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError):
        glitchtip.store_project_dsn(client(tmp_path, session), DSN)
    assert session.secrets == before
    assert all(call.method == "GET" for call in session.calls)


def test_selected_catalog_is_additive_preserves_credentials_and_deselect_never_deletes(tmp_path):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords(), glitchtip_enabled=True)
    assert (
        session.secrets[glitchtip.ADAPTER_PATH].values["opensearchUsername"] == "glitchtip-adapter"
    )
    assert session.secrets[glitchtip.ADAPTER_PATH].values["dsn"] == ""
    assert (
        session.secrets["glitchtip/internal"].values["databasePassword"]
        == session.secrets["infra-postgres-operations/internal"].values["glitchtipPassword"]
    )
    assert session.secrets["glitchtip/owner"].values == {"email": "", "password": ""}
    admission = session.secrets["glitchtip/admission"].values
    assert (
        admission["clientSecret"]
        == session.secrets["auth-keycloak/glitchtip"].values["clientSecret"]
    )
    assert admission["cookieSecret"] != admission["clientSecret"]
    assert "glitchtip/provisioning" not in session.secrets
    session.secrets[glitchtip.ADAPTER_PATH].values["dsn"] = DSN
    before = deepcopy(session.secrets)
    reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets == before
    session.calls.clear()
    reconcile_openbao(api, identity(), glitchtip_enabled=False)
    assert session.secrets == before
    assert not any(
        call.method == "DELETE" and call.path.startswith("secret/") for call in session.calls
    )


@pytest.mark.parametrize("existing", ["app", "postgres", "both"])
def test_database_password_copies_are_stable_and_preserve_all_siblings(tmp_path, existing):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    if existing in {"app", "both"}:
        session.secrets["glitchtip/internal"] = StoredSecret(
            {"databasePassword": "synthetic-stable", "sibling": "keep"}
        )
    postgres = session.secrets["infra-postgres-operations/internal"]
    if existing in {"postgres", "both"}:
        postgres.values["glitchtipPassword"] = "synthetic-stable"
    postgres_siblings = dict(postgres.values)
    reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets["glitchtip/internal"].values["databasePassword"] == "synthetic-stable"
    assert session.secrets["infra-postgres-operations/internal"].values == postgres_siblings | {
        "glitchtipPassword": "synthetic-stable"
    }
    if existing in {"app", "both"}:
        assert session.secrets["glitchtip/internal"].values["sibling"] == "keep"
    before = deepcopy(session.secrets)
    reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets == before
    policy = session.policies["glitchtip"]
    assert isinstance(policy, dict) and isinstance(policy["policy"], str)
    paths = set(re.findall(r'path "([^"]+)"', policy["policy"]))
    assert paths == {
        "secret/data/glitchtip/*",
        "secret/metadata/glitchtip",
        "secret/metadata/glitchtip/*",
    }
    assert "infra-postgres-operations" not in policy["policy"]


def test_conflicting_database_copies_fail_before_changing_either_record(tmp_path):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords(), glitchtip_enabled=True)
    session.secrets["glitchtip/internal"].values["databasePassword"] = "different-synthetic-value"
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError, match="copies conflict"):
        reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets == before


@pytest.mark.parametrize("survivor", ["glitchtip/admission", "auth-keycloak/glitchtip"])
def test_admission_copy_retry_preserves_credentials_and_conflict_never_rotates(tmp_path, survivor):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords(), glitchtip_enabled=True)
    before = deepcopy(session.secrets)
    missing = (
        "auth-keycloak/glitchtip" if survivor == "glitchtip/admission" else "glitchtip/admission"
    )
    del session.secrets[missing]
    reconcile_openbao(api, identity(), glitchtip_enabled=True)
    for path in ("glitchtip/admission", "auth-keycloak/glitchtip"):
        assert (
            session.secrets[path].values["clientSecret"] == before[survivor].values["clientSecret"]
        )
    session.secrets[missing].values["clientSecret"] = "conflicting-synthetic-secret"
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError, match="admission credential copies conflict"):
        reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets == before


def test_interrupted_database_copy_retry_preserves_the_first_generated_password(tmp_path):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords())
    original_postgres = deepcopy(session.secrets["infra-postgres-operations/internal"])
    session.failure = (
        "POST",
        "secret/data/infra-postgres-operations/internal",
        503,
        {"errors": ["synthetic interruption"]},
    )
    with pytest.raises(OpenBaoError, match="HTTP 503"):
        reconcile_openbao(api, identity(), glitchtip_enabled=True)
    original_app = deepcopy(session.secrets["glitchtip/internal"])
    assert session.secrets["infra-postgres-operations/internal"] == original_postgres
    session.failure = None
    reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets["glitchtip/internal"] == original_app
    assert session.secrets[
        "infra-postgres-operations/internal"
    ].values == original_postgres.values | {
        "glitchtipPassword": original_app.values["databasePassword"]
    }


@pytest.mark.parametrize("selected", [False, True])
def test_staged_convergence_delivers_generated_database_without_waiting_for_blank_inputs(selected):
    target = MagicMock(spec=Cluster)
    with (
        patch("openbao_stack_setup.main._converge_bootstrap_secret_stores"),
        patch("openbao_stack_setup.main._refresh_bootstrap_external_secrets"),
        patch("openbao_stack_setup.main._reconcile_bootstrap_helm_releases"),
    ):
        cli._converge_runtime(target, False, glitchtip_enabled=selected)
    assert target.ensure_secret_store_ready.call_args_list == (
        [call("glitchtip-openbao-secret-store", "glitchtip")] if selected else []
    )
    assert target.force_external_secret_refresh.call_args_list == (
        [
            call(
                "glitchtip-postgres-values",
                "infra-postgres-operations",
                "glitchtip-postgres-values",
            )
        ]
        if selected
        else []
    )
    target.token_request.assert_not_called()
    target.wait_helm_release.assert_not_called()
    target.glitchtip_application_pod.assert_not_called()


@pytest.mark.parametrize(
    "order", [("glitchtip-owner", "glitchtip-smtp"), ("glitchtip-smtp", "glitchtip-owner")]
)
def test_setters_independently_retry_delivery_after_revocation_in_either_order(
    monkeypatch, tmp_path, order
):
    session = FakeSession()
    source(session)
    operator = client(tmp_path, session)
    reconcile_openbao(operator, identity(), passwords(), glitchtip_enabled=True)
    original_database = deepcopy(session.secrets["infra-postgres-operations/internal"])
    values = {
        "glitchtip-owner": {"email": "synthetic@example.invalid", "password": "synthetic-owner"},
        "glitchtip-smtp": {
            "smtpUsername": "synthetic-smtp",
            "smtpPassword": "synthetic-smtp-password",
        },
    }
    own_targets = {"glitchtip-owner": "glitchtip-owner", "glitchtip-smtp": "glitchtip-runtime"}
    target = MagicMock(spec=Cluster)
    target.glitchtip_enabled.return_value = True
    connection = MagicMock()
    connection.__enter__.return_value.kubernetes_login.return_value = "synthetic-operator-token"
    monkeypatch.setattr(cli, "Cluster", lambda context: target)
    monkeypatch.setattr(cli, "_confirm", lambda *args: None)
    monkeypatch.setattr(cli, "_prompt_provider", lambda provider: values[provider.name])
    monkeypatch.setattr(cli, "_openbao", lambda cluster: connection)
    monkeypatch.setattr(cli, "OpenBaoClient", lambda *args: operator)

    def after_revocation(*args):
        assert session.calls[-1].path == "auth/token/revoke-self"

    target.ensure_secret_store_ready.side_effect = after_revocation
    target.force_external_secret_refresh.side_effect = after_revocation
    for name in order:
        target.reset_mock()
        session.calls.clear()
        cli._set_provider("context", "client", name)
        assert session.secrets["infra-postgres-operations/internal"] == original_database
        target.ensure_secret_store_ready.assert_called_once_with(
            "glitchtip-openbao-secret-store", "glitchtip"
        )
        assert target.force_external_secret_refresh.call_args_list == [
            call(
                "glitchtip-postgres-values",
                "infra-postgres-operations",
                "glitchtip-postgres-values",
            ),
            call(own_targets[name], "glitchtip", own_targets[name]),
        ]
        assert sum(request.path == "auth/token/revoke-self" for request in session.calls) == 1
        other = "glitchtip-smtp" if name == "glitchtip-owner" else "glitchtip-owner"
        if name == order[0]:
            path = "glitchtip/external" if other == "glitchtip-smtp" else "glitchtip/owner"
            assert all(value == "" for value in session.secrets[path].values.values())
        target.glitchtip_application_pod.assert_not_called()
        target.wait_helm_release.assert_not_called()


@pytest.mark.parametrize("selected", [False, True])
def test_preflight_reports_actual_glitchtip_selection(monkeypatch, capsys, selected):
    selector = configured_cluster()
    selector.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": "glitchtip: {enabled: " + str(selected).lower() + "}"}
    )
    target = MagicMock()
    target.glitchtip_enabled = selector.glitchtip_enabled
    target.studio_mcp_ids.return_value = ()
    monkeypatch.setattr(cli, "Cluster", lambda context: target)
    cli._preflight("context", "client")
    assert f"GlitchTip staged credential catalog selected={selected}" in capsys.readouterr().out
    target.token_request.assert_not_called()


def test_malformed_glitchtip_selection_stops_before_credentials_or_custody(monkeypatch):
    selector = configured_cluster()
    selector.core.read_namespaced_config_map.return_value = SimpleNamespace(
        data={"values.yaml": 'glitchtip: {enabled: "true"}'}
    )
    target = MagicMock()
    target.glitchtip_enabled = selector.glitchtip_enabled
    monkeypatch.setattr(cli, "Cluster", lambda context: target)
    with (
        patch("openbao_stack_setup.main._prompt_provider") as prompt,
        patch("openbao_stack_setup.main._openbao") as login,
        patch("openbao_stack_setup.main.prepare_custody_paths") as custody,
        pytest.raises(ClusterError, match="GlitchTip"),
    ):
        cli._preflight("context", "client")
    prompt.assert_not_called()
    login.assert_not_called()
    custody.assert_not_called()
    target.token_request.assert_not_called()


def test_blank_existing_internal_secret_is_not_silently_replaced(tmp_path):
    session = FakeSession()
    source(session)
    api = client(tmp_path, session)
    reconcile_openbao(api, identity(), passwords(), glitchtip_enabled=True)
    session.secrets["glitchtip/internal"].values["secretKey"] = "  "
    before = deepcopy(session.secrets)
    with pytest.raises(OpenBaoError, match="invalid"):
        reconcile_openbao(api, identity(), glitchtip_enabled=True)
    assert session.secrets == before


def test_handoff_revokes_operator_and_preserves_concurrent_siblings_when_cas_write_fails(
    monkeypatch, tmp_path
):
    args = cli._arguments().parse_args(
        [
            "secret",
            "import-glitchtip-dsn",
            "--context",
            "test",
            "--client",
            "test",
            "--organization",
            "operations",
            "--project",
            "services",
            "--project-name",
            "Services",
            "--team",
            "platform",
        ]
    )
    cluster = MagicMock()
    session = FakeSession()
    session.secrets[glitchtip.ADAPTER_PATH] = StoredSecret(
        {"dsn": "", "opensearchPassword": "synthetic-password", "sibling": "before"}, version=9
    )
    session.secrets["unrelated/record"] = StoredSecret({"keep": "unrelated"})
    operator = client(tmp_path, session)
    original_request = session.request

    def concurrent_request(method, url, **kwargs):
        if method == "POST" and url.endswith("/v1/secret/data/glitchtip/adapter"):
            session.secrets[glitchtip.ADAPTER_PATH].values["sibling"] = "concurrent-update"
            session.secrets[glitchtip.ADAPTER_PATH].version += 1
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(session, "request", concurrent_request)
    connection = MagicMock()
    api = connection.__enter__.return_value
    monkeypatch.setattr(cli, "Cluster", lambda context: cluster)
    monkeypatch.setattr(cli, "_confirm", lambda *args: None)
    monkeypatch.setattr(cli, "_read_glitchtip_dsn", lambda *args: DSN)
    monkeypatch.setattr(cli, "_openbao", lambda cluster: connection)
    monkeypatch.setattr(cli, "OpenBaoClient", lambda *args: operator)
    with pytest.raises(OpenBaoError, match="HTTP 400"):
        cli._import_glitchtip_dsn(args)
    assert session.secrets[glitchtip.ADAPTER_PATH] == StoredSecret(
        {"dsn": "", "opensearchPassword": "synthetic-password", "sibling": "concurrent-update"},
        version=10,
    )
    assert session.secrets["unrelated/record"].values == {"keep": "unrelated"}
    writes = [
        call
        for call in session.calls
        if call.path == "secret/data/glitchtip/adapter" and call.method == "POST"
    ]
    assert len(writes) == 1 and isinstance(writes[0].body, dict)
    assert writes[0].body["options"] == {"cas": 9}
    assert sum(call.path == "auth/token/revoke-self" for call in session.calls) == 1
    cluster.force_external_secret_refresh.assert_not_called()
    cluster.identity.assert_called_once_with("test")
    api.kubernetes_login.assert_called_once()


def test_private_export_exec_uses_stdin_without_forwarding_sensitive_stdout(monkeypatch, capsys):
    args = MagicMock(
        organization="operations", project="services", project_name="Services", team="platform"
    )
    cluster = MagicMock(context="test")
    cluster.glitchtip_application_pod.return_value = "glitchtip-ready"
    result = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {
                "schemaVersion": 1,
                "organization": "operations",
                "project": "services",
                "projectId": 7,
                "dsn": DSN,
            }
        ).encode(),
    )
    run = MagicMock(return_value=result)
    monkeypatch.setattr(cli, "_kubectl_executable", lambda: "/trusted/kubectl")
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._read_glitchtip_dsn(cluster, args) == DSN
    call = run.call_args
    assert call.kwargs["input"] == glitchtip.provisioning_payload()
    assert call.kwargs["stdout"] == cli.subprocess.PIPE
    assert call.kwargs["stderr"] == cli.subprocess.DEVNULL
    assert DSN not in repr(call.args)
    assert "export-dsn" in call.args[0]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "envelope",
    [
        {"schemaVersion": 1},
        {
            "schemaVersion": 1,
            "organization": "other",
            "project": "services",
            "projectId": 7,
            "dsn": DSN,
        },
        {
            "schemaVersion": 1,
            "organization": "operations",
            "project": "services",
            "projectId": 8,
            "dsn": DSN,
        },
    ],
)
def test_invalid_private_exports_fail_without_secret_in_error(envelope):
    with pytest.raises(OpenBaoError) as error:
        glitchtip.parse_dsn_export(json.dumps(envelope).encode(), "operations", "services")
    assert DSN not in str(error.value)
