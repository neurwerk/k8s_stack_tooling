"""Live external input shape is checked before native execution or custody, never inferred."""

import copy
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from k8s_stack_tooling.addons import ExternalInput, selection_projection
from k8s_stack_tooling.addons.schema import Input
from kubernetes.client.exceptions import ApiException
from test_cluster import cluster
from test_selected_cli import arguments, verified

from openbao_stack_setup import main as cli
from openbao_stack_setup import native, selection
from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.cluster import ClusterError

CANARY = "synthetic-private-pull-content-never-log"


def required_input(name="tooling-pull"):
    return ExternalInput(
        kind="Secret",
        namespace="scratch",
        name=name,
        type="kubernetes.io/dockerconfigjson",
        keys=[".dockerconfigjson"],
    )


def live_resource():
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"namespace": "scratch", "name": "tooling-pull"},
        "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": CANARY},
    }


def selected_inputs():
    snapshot = verified(native=True)
    snapshot.addons[0].definition.spec.inputs.append(
        Input(
            kind="Secret",
            namespace="scratch",
            name="tooling-pull",
            owner="client",
            type="kubernetes.io/dockerconfigjson",
            keys=[".dockerconfigjson"],
        )
    )
    return replace(
        snapshot,
        selection=snapshot.selection.model_copy(
            update={
                "external_inputs": [required_input(), required_input("unrelated-pull")],
            }
        ),
    )


@pytest.mark.parametrize("command", ["bootstrap", "reconcile"])
@pytest.mark.parametrize("failure", ["missing", "namespace", "name", "type", "keys", "api-body"])
def test_invalid_live_input_stops_cli_before_native_custody_or_root(
    command, failure, monkeypatch, capsys
):
    args = cli._arguments().parse_args(arguments())
    args.command = command
    args.confirm = "client"
    args.native_namespace = "scratch"
    snapshot = selected_inputs()
    target = cluster()
    value = live_resource()
    if failure in ("namespace", "name"):
        value["metadata"][failure] = "wrong"
    elif failure == "type":
        value["type"] = "Opaque"
    elif failure == "keys":
        value["data"] = {}
    if failure in ("missing", "api-body"):
        target.core.read_namespaced_secret.side_effect = ApiException(
            status=404 if failure == "missing" else 403, reason=CANARY, http_resp=None
        )
        target.core.read_namespaced_secret.side_effect.body = CANARY
    else:
        target.core.read_namespaced_secret.return_value = value
    with (
        patch.object(cli, "_arguments") as parser,
        patch.object(cli, "Cluster", return_value=target),
        patch.object(target, "identity"),
        patch.object(selection, "source_checkouts", return_value=(MagicMock(), {})),
        patch.object(selection, "verify_selection", return_value=snapshot),
        patch.object(selection, "prepare_native_output") as prepare,
        patch.object(cli, "prepare_custody_paths") as custody,
        patch.object(cli, "_openbao") as root,
        pytest.raises(SystemExit) as error,
    ):
        parser.return_value.parse_args.return_value = args
        cli.main()
    assert error.value.code == 1
    assert CANARY not in capsys.readouterr().out
    prepare.assert_not_called()
    custody.assert_not_called()
    root.assert_not_called()


def test_good_input_is_checked_before_native_and_only_product_approved_pull_is_attached():
    args = cli._arguments().parse_args([*arguments(), "--native-namespace", "scratch"])
    snapshot = selected_inputs()
    target = cluster()
    events = []

    def read(name, namespace, **kwargs):
        assert (name, namespace) == ("tooling-pull", "scratch")
        events.append("read-input")
        value = live_resource()
        value["metadata"]["name"] = name
        return value

    target.core.read_namespaced_secret.side_effect = read

    def prepare(_cluster, *, namespace, definition, artifact, pull_inputs):
        assert events and all(event == "read-input" for event in events)
        assert pull_inputs == (required_input(),)
        pod = native._pod("synthetic", namespace, definition, artifact, pull_inputs)
        assert pod["spec"]["imagePullSecrets"] == [{"name": "tooling-pull"}]
        assert "volumes" not in pod["spec"]
        events.append("native")
        return {definition.output_key: "synthetic-token"}

    with patch.object(selection, "prepare_native_output", side_effect=prepare):
        run = selection.selected_run(target, snapshot, args)
    assert run.prepared == {"sample": {"native-token": "synthetic-token"}}
    assert events[-1] == "native"
    target.core.read_namespaced_secret.assert_called_once_with(
        "tooling-pull", "scratch", _request_timeout=(5, 30)
    )
    # The declaration remains in the verified projection and binds the retry fingerprint.
    data = selection_projection(snapshot)["data"]
    assert isinstance(data, dict)
    assert "externalInputs" in data["selection.json"]
    assert run.fingerprint != selection.SelectedRun(verified(native=True), "").fingerprint


@pytest.mark.parametrize("failure", ["undeclared", "namespace", "type", "keys", "owner"])
def test_native_pull_cannot_use_unapproved_or_cross_namespace_input(failure):
    snapshot = selected_inputs()
    product_input = snapshot.addons[0].definition.spec.inputs[0]
    if failure == "undeclared":
        product_input.name = "not-approved"
    elif failure == "namespace":
        product_input.namespace = "another-namespace"
    elif failure == "type":
        snapshot.selection.external_inputs[0].secret_type = "Opaque"
    elif failure == "keys":
        product_input.keys = ["not-approved"]
    else:
        product_input.owner = "base"
    args = cli._arguments().parse_args([*arguments(), "--native-namespace", "scratch"])
    target = cluster()

    def read(name, namespace, **kwargs):
        resource = live_resource()
        resource["metadata"] = {"name": name, "namespace": namespace}
        if name == "tooling-pull":
            resource["type"] = snapshot.selection.external_inputs[0].secret_type
        return resource

    target.core.read_namespaced_secret.side_effect = read
    with (
        patch.object(selection, "prepare_native_output") as prepare,
        pytest.raises(OpenBaoError, match="exact product/client approval"),
    ):
        selection.selected_run(target, snapshot, args)
    prepare.assert_not_called()
    target.core.read_namespaced_secret.assert_not_called()


@pytest.mark.parametrize("command", ["recovery", "secret", "addon"])
def test_recovery_provider_update_and_other_product_refresh_do_not_read_registry_inputs(command):
    args = cli._arguments().parse_args(arguments())
    args.command = command
    args.confirm = "client"
    args.secret_command = "set"
    args.provider = "brave"
    args.addon_id = "other"
    args.addon_command = "refresh"
    snapshot = selected_inputs()
    other = verified(addon_id="other")
    snapshot = replace(
        snapshot,
        addons=(*snapshot.addons, *other.addons),
        selection=snapshot.selection.model_copy(
            update={
                "addons": [*snapshot.selection.addons, *other.selection.addons],
            }
        ),
    )
    target = MagicMock()
    actual = cluster()
    actual.core.read_namespaced_secret.side_effect = ApiException(status=404)
    target.require_external_inputs.side_effect = actual.require_external_inputs
    with (
        patch.object(cli, "_arguments") as parser,
        patch.object(cli, "Cluster", return_value=target),
        patch.object(selection, "source_checkouts", return_value=(MagicMock(), {})),
        patch.object(selection, "verify_selection", return_value=snapshot),
        patch.object(cli, "_verify_recovery") as recover,
        patch.object(cli, "_set_provider") as provider,
        patch.object(selection, "prepare_native_output") as native_prepare,
    ):
        parser.return_value.parse_args.return_value = args
        cli.main()
    actual.core.read_namespaced_secret.assert_not_called()
    native_prepare.assert_not_called()
    if command == "recovery":
        recover.assert_called_once()
    elif command == "secret":
        provider.assert_called_once()
    else:
        target.force_external_secret_refresh.assert_called_once()


@pytest.mark.parametrize("deselected", [False, True])
def test_reconcile_without_native_work_ignores_unused_external_declarations(deselected):
    args = cli._arguments().parse_args(arguments())
    args.confirm = "client"
    snapshot = selected_inputs()
    snapshot.addons[0].definition.spec.setup.records[0].fields.pop("token")
    if deselected:
        snapshot = replace(
            snapshot,
            addons=(),
            selection=snapshot.selection.model_copy(update={"addons": []}),
        )
    actual = cluster()
    actual.core.read_namespaced_secret.side_effect = ApiException(status=404)
    with (
        patch.object(cli, "Cluster", return_value=actual),
        patch.object(cli, "_reconcile") as reconcile,
        patch.object(selection, "prepare_native_output") as prepare,
    ):
        cli._dispatch(args, snapshot)
    reconcile.assert_called_once()
    prepare.assert_not_called()
    actual.core.read_namespaced_secret.assert_not_called()


@pytest.mark.parametrize("mode", ["refresh", "provision", "restart"])
def test_lifecycle_ignores_inputs_outside_selected_operation_targets(mode):
    args = cli._arguments().parse_args(arguments())
    args.addon_id = "sample"
    args.addon_command = mode
    args.confirm = "client"
    snapshot = selected_inputs()  # Registry input is in scratch; these targets are in sample.
    target = MagicMock()
    actual = cluster()
    actual.core.read_namespaced_secret.side_effect = ApiException(status=404)
    target.require_external_inputs.side_effect = actual.require_external_inputs
    with (
        patch.object(cli, "Cluster", return_value=target),
        patch.object(cli, "_force_helm_release"),
        patch.object(cli, "_restart_target"),
    ):
        cli._addon_lifecycle(args, snapshot)
    actual.core.read_namespaced_secret.assert_not_called()


def test_selected_provision_input_failure_stops_only_its_consumers():
    args = cli._arguments().parse_args(arguments())
    args.addon_id = "sample"
    args.addon_command = "provision"
    args.confirm = "client"
    snapshot = selected_inputs()
    snapshot.addons[0].definition.spec.setup.lifecycle.provision[0].namespace = "scratch"
    actual = cluster()
    actual.core.read_namespaced_secret.side_effect = ApiException(status=404)
    target = MagicMock()
    target.require_external_inputs.side_effect = actual.require_external_inputs
    with (
        patch.object(cli, "Cluster", return_value=target),
        patch.object(cli, "_force_helm_release") as provision,
        pytest.raises(ClusterError),
    ):
        cli._addon_lifecycle(args, snapshot)
    provision.assert_not_called()
    target.force_external_secret_refresh.assert_not_called()
    actual.core.read_namespaced_secret.assert_called_once_with(
        "tooling-pull", "scratch", _request_timeout=(5, 30)
    )


def test_live_input_errors_are_redacted_without_decoding_credentials(capsys):
    target = cluster()
    target.core.read_namespaced_secret.side_effect = RuntimeError(CANARY)
    with pytest.raises(ClusterError) as error:
        target.require_external_inputs([required_input()])
    assert CANARY not in str(error.value)
    assert CANARY not in repr(error.value)
    assert capsys.readouterr() == ("", "")


def test_native_admission_cannot_replace_the_verified_pull_secret():
    snapshot = selected_inputs()
    output = snapshot.addons[0].definition.spec.native_outputs[0]
    artifact = snapshot.addons[0].definition.spec.artifacts[0]
    expected = native._pod("synthetic", "scratch", output, artifact, (required_input(),))
    actual = copy.deepcopy(expected)
    actual["metadata"]["uid"] = "synthetic-uid"
    actual["spec"]["imagePullSecrets"] = [{"name": "unrelated-pull"}]
    with pytest.raises(native.NativeError, match="security or execution settings changed"):
        native._verify_pod(actual, expected, "synthetic-uid")
