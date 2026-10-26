"""One-shot submission and safe observation protect against duplicate native work."""

import json
from types import SimpleNamespace as Ns
from unittest.mock import Mock

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import HTTPError, NewConnectionError, ProtocolError

from k8s_stack_tooling.api.jobs import (
    ConfigMapVersion,
    JobError,
    job_api_client,
    read_job_summary,
    submit_snapshot_job,
    wait_for_job,
)


@pytest.mark.parametrize("error_type", [NewConnectionError, ProtocolError])
def test_client_transport_does_not_retry_job_creation(
    tmp_path, monkeypatch, capsys, caplog, error_type
):
    # Keep the real Kubernetes serialization and urllib3 retry loop, replacing
    # only the socket request. No cluster/network/credential access is involved.
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(variable, raising=False)
    kubeconfig = tmp_path / "config.json"
    kubeconfig.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "not-selected",
                "contexts": [
                    {"name": "explicit", "context": {"cluster": "synthetic", "user": "test"}}
                ],
                "clusters": [{"name": "synthetic", "cluster": {"server": "http://127.0.0.1:9"}}],
                "users": [{"name": "test", "user": {"token": "synthetic-private-token"}}],
            }
        )
    )
    original = kubeconfig.read_bytes()
    attempts = []

    def failed_request(pool, connection, method, url, *args, **kwargs):
        attempts.append((method, url))
        if error_type is NewConnectionError:
            raise NewConnectionError(connection, "synthetic-private-response")
        raise ProtocolError("synthetic-private-response")

    monkeypatch.setattr(HTTPConnectionPool, "_make_request", failed_request)
    core, _ = clients()
    with job_api_client(context="explicit", config_file=str(kubeconfig)) as api:
        close = Mock(wraps=api.close)
        monkeypatch.setattr(api, "close", close)
        result = submit_snapshot_job(
            core,
            client.BatchV1Api(api),
            job=job(),
            snapshot_data={},
        )
    assert result.status == "uncertain" and result.phase == "job"
    assert attempts == [("POST", "/apis/batch/v1/namespaces/example/jobs")]
    close.assert_called_once()
    assert kubeconfig.read_bytes() == original
    assert "synthetic-private" not in repr(result) + caplog.text
    assert capsys.readouterr() == ("", "")


def test_client_requires_context_before_loading_config(monkeypatch):
    loader = Mock()
    monkeypatch.setattr("k8s_stack_tooling.api.jobs.config.new_client_from_config", loader)
    with pytest.raises(JobError, match="explicit nonempty"):
        with job_api_client(context=" \t"):
            pytest.fail("invalid context accepted")
    loader.assert_not_called()


def test_client_configuration_errors_are_redacted(monkeypatch, capsys):
    monkeypatch.setattr(
        "k8s_stack_tooling.api.jobs.config.new_client_from_config",
        Mock(side_effect=ValueError("synthetic-private-configuration")),
    )
    with pytest.raises(JobError) as caught:
        with job_api_client(context="explicit"):
            pytest.fail("invalid configuration accepted")
    assert "synthetic-private" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert capsys.readouterr() == ("", "")


def clients():
    core, batch = Mock(), Mock()
    core.create_namespaced_config_map.return_value = Ns(
        metadata=Ns(uid="snapshot-uid", resource_version="7")
    )
    batch.create_namespaced_job.return_value = Ns(metadata=Ns(uid="job-uid"))
    return core, batch


def job():
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "example-run", "namespace": "example"},
    }


def test_snapshot_is_immutable_and_ownership_patch_is_conditional():
    core, batch = clients()
    template = job()
    result = submit_snapshot_job(core, batch, job=template, snapshot_data={"input.json": "{}"})
    assert result.status == "submitted" and result.uid == "job-uid"
    snapshot = core.create_namespaced_config_map.call_args.args[1]
    assert snapshot["immutable"] is True
    patch = core.patch_namespaced_config_map.call_args.args[2]["metadata"]
    assert patch["uid"] == "snapshot-uid" and patch["resourceVersion"] == "7"
    assert patch["ownerReferences"][0]["uid"] == "job-uid"
    assert template == job()


@pytest.mark.parametrize("phase", ["snapshot", "job", "ownership"])
@pytest.mark.parametrize("error", [ApiException(reason="sensitive-body"), HTTPError("secret")])
def test_uncertain_mutations_are_never_retried_or_rolled_back(phase, error, capsys):
    core, batch = clients()
    operations = {
        "snapshot": core.create_namespaced_config_map,
        "job": batch.create_namespaced_job,
        "ownership": core.patch_namespaced_config_map,
    }
    operations[phase].side_effect = error
    result = submit_snapshot_job(core, batch, job=job(), snapshot_data={})
    assert result.status == "uncertain" and result.phase == phase
    assert operations[phase].call_count == 1
    assert not core.delete_namespaced_config_map.called
    assert not batch.delete_namespaced_job.called
    if phase == "snapshot":
        batch.create_namespaced_job.assert_not_called()
    if phase == "job":
        core.patch_namespaced_config_map.assert_not_called()
    assert "sensitive" not in repr(result) and "secret" not in repr(result)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("uid,version", [("replaced", "3"), ("input-uid", "4")])
def test_stale_inputs_stop_before_any_write(uid, version):
    core, batch = clients()
    core.read_namespaced_config_map.return_value = Ns(
        metadata=Ns(uid=uid, resource_version=version)
    )
    with pytest.raises(JobError, match="inputs changed"):
        submit_snapshot_job(
            core,
            batch,
            job=job(),
            snapshot_data={},
            inputs=[ConfigMapVersion("example", "input", "input-uid", "3")],
        )
    core.create_namespaced_config_map.assert_not_called()
    batch.create_namespaced_job.assert_not_called()


def test_precondition_api_error_is_redacted():
    core, batch = clients()
    core.read_namespaced_config_map.side_effect = ApiException(reason="secret-response")
    with pytest.raises(JobError) as caught:
        submit_snapshot_job(
            core,
            batch,
            job=job(),
            snapshot_data={},
            inputs=[ConfigMapVersion("example", "input", "uid", "3")],
        )
    assert "secret" not in str(caught.value) and caught.value.__suppress_context__
    core.create_namespaced_config_map.assert_not_called()


def test_wait_is_bounded_and_detects_replaced_job():
    batch = Mock()
    batch.read_namespaced_job_status.return_value = Ns(
        metadata=Ns(uid="job-uid"), status=Ns(conditions=[])
    )
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    assert (
        wait_for_job(
            batch,
            namespace="example",
            name="run",
            uid="job-uid",
            seconds=3,
            clock=lambda: now[0],
            sleep=sleep,
        )
        == "timeout"
    )
    assert now[0] == 3 and batch.read_namespaced_job_status.call_count == 2
    assert batch.read_namespaced_job_status.call_args.kwargs["_request_timeout"] == (1, 1)
    batch.read_namespaced_job_status.return_value.metadata.uid = "replacement"
    assert (
        wait_for_job(
            batch,
            namespace="example",
            name="run",
            uid="job-uid",
            seconds=1,
        )
        == "uncertain"
    )


@pytest.mark.parametrize("condition,expected", [("Complete", "complete"), ("Failed", "failed")])
def test_wait_terminal_conditions(condition, expected):
    batch = Mock()
    batch.read_namespaced_job_status.return_value = Ns(
        metadata=Ns(uid="uid"), status=Ns(conditions=[Ns(type=condition, status="True")])
    )
    assert (
        wait_for_job(
            batch,
            namespace="example",
            name="run",
            uid="uid",
            seconds=1,
        )
        == expected
    )


def test_wait_api_failure_is_uncertain_without_retry(capsys):
    batch = Mock()
    batch.read_namespaced_job_status.side_effect = ApiException(reason="secret-response")
    assert (
        wait_for_job(
            batch,
            namespace="example",
            name="run",
            uid="uid",
            seconds=1,
        )
        == "uncertain"
    )
    batch.read_namespaced_job_status.assert_called_once()
    assert capsys.readouterr() == ("", "")


def log_client(raw=b'{"created": 2, "secret": "never-print"}'):
    core = Mock()
    core.list_namespaced_pod.return_value = Ns(
        items=[
            Ns(
                metadata=Ns(
                    name="pod",
                    owner_references=[Ns(uid="uid", kind="Job", controller=True)],
                )
            )
        ]
    )
    response = core.read_namespaced_pod_log.return_value
    response.read.return_value = raw
    return core, response


def test_log_callback_only_returns_allowlisted_summary(capsys):
    core, response = log_client()
    result = read_job_summary(
        core,
        namespace="example",
        name="run",
        uid="uid",
        container="native",
        summarize=lambda text: {"created": json.loads(text)["created"]},
    )
    assert result.summary == {"created": 2}
    assert "secret" not in repr(result)
    response.read.assert_called_once_with(16385)
    response.close.assert_called_once()
    response.release_conn.assert_called_once()
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("raw", [b"x" * 17, b"\xff"])
def test_local_log_budget_and_encoding_are_enforced(raw):
    core, response = log_client(raw)
    summarize = Mock()
    result = read_job_summary(
        core,
        namespace="example",
        name="run",
        uid="uid",
        container="native",
        summarize=summarize,
        max_bytes=16,
    )
    assert result.status == "unavailable"
    summarize.assert_not_called()
    response.release_conn.assert_called_once()


@pytest.mark.parametrize("failure", ["read", "callback", "close"])
def test_log_failures_never_expose_exception_bodies(failure, capsys):
    core, response = log_client()
    summarize = Mock(return_value={"created": 2})
    {"read": response.read, "callback": summarize, "close": response.close}[
        failure
    ].side_effect = RuntimeError("secret-content")
    result = read_job_summary(
        core,
        namespace="example",
        name="run",
        uid="uid",
        container="native",
        summarize=summarize,
    )
    assert result.status == "unavailable" and result.summary is None
    assert "secret" not in repr(result)
    response.release_conn.assert_called_once()
    assert capsys.readouterr() == ("", "")


def test_logs_of_wrong_owner_are_not_read():
    core, _ = log_client()
    core.list_namespaced_pod.return_value.items[0].metadata.owner_references[0].uid = "other"
    assert (
        read_job_summary(
            core,
            namespace="example",
            name="run",
            uid="uid",
            container="native",
            summarize=lambda _: {},
        ).status
        == "unavailable"
    )
    core.read_namespaced_pod_log.assert_not_called()
