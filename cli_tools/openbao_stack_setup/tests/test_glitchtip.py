from __future__ import annotations

import json
import re
from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fake import FakeSession, StoredSecret
from test_reconcile import client, identity, passwords, source

from openbao_stack_setup import glitchtip
from openbao_stack_setup import main as cli
from openbao_stack_setup.client import OpenBaoError
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
