"""Acquire and verify the current client's pinned definitions with the shared parser."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

from k8s_stack_tooling.addons import (
    ExternalInput,
    Setup,
    VerifiedSelection,
    selection_projection,
    verify_selection,
)
from k8s_stack_tooling.addons.acquisition import acquire_selection, source_checkouts
from k8s_stack_tooling.addons.schema import Input, Version
from pydantic import TypeAdapter, ValidationError

from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.cluster import Cluster
from openbao_stack_setup.native import REGISTRY_TYPE, prepare_native_output


@dataclass(frozen=True)
class SelectedRun:
    """Verified definitions and private credential inputs for one reconciliation."""

    verified: VerifiedSelection
    input_groups: tuple[str, ...] = ()
    prepared: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    def require_verified(self) -> None:
        """Require verified sources and compatible installed CLI patches for every add-on."""
        if self.verified.installable is not True or self.verified.projection_verified is not True:
            raise OpenBaoError("Setup requires installable sources and verified staged selection")
        try:
            installed = distribution_version("openbao-stack-setup")
        except PackageNotFoundError:
            raise OpenBaoError("Installed stack-setup package metadata is unavailable") from None
        try:
            installed = TypeAdapter(Version).validate_python(installed)
        except ValidationError:
            raise OpenBaoError("Installed stack-setup package version is invalid") from None
        installed_parts = tuple(map(int, installed.split(".")))
        for item in self.verified.addons:
            required = item.declared_definition.spec.prerequisites.stack_setup
            required_parts = tuple(map(int, required.split(".")))
            if installed_parts[:2] != required_parts[:2] or installed_parts[2] < required_parts[2]:
                raise OpenBaoError(
                    f"Add-on {item.definition.metadata.name} requires stack-setup {required}; "
                    f"installed package is {installed}"
                )

    @property
    def setups(self) -> dict[str, Setup]:
        """Return canonical active-expanded models, without another expansion or loader."""
        return {
            item.definition.metadata.name: item.definition.spec.setup
            for item in self.verified.addons
        }

    @property
    def fingerprint(self) -> str:
        """Bind pending password handover to the verified definitions and selection."""
        payload = json.dumps(selection_projection(self.verified), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()


def verify_client_sources(cluster: Cluster) -> VerifiedSelection:
    """Acquire immutable sources automatically and compare the complete staged selection."""
    client_root = Path.cwd().resolve()
    while not (client_root / ".git").exists():
        if client_root.parent == client_root:
            raise OpenBaoError("Run setup from the selected client Git checkout")
        client_root = client_root.parent
    if not (client_root / "config/addons.yaml").is_file():
        raise OpenBaoError("Run setup from the selected client Git checkout")
    projection = cluster.selection_projection()
    data = projection.get("data")
    if not isinstance(data, dict):
        raise OpenBaoError("Staged selection has no valid cluster directory")
    try:
        cluster_dir = json.loads(data["selection.json"])["clusterDirectory"]
    except (KeyError, TypeError, ValueError):
        raise OpenBaoError("Staged selection has no valid cluster directory") from None
    if not isinstance(cluster_dir, str) or not cluster_dir:
        raise OpenBaoError("Staged selection has no valid cluster directory")
    with tempfile.TemporaryDirectory(prefix="stack-setup-sources-") as temporary:
        cache = Path(temporary).resolve()
        acquire_selection(client_root, cluster_dir=cluster_dir, cache=cache)
        base, checkouts = source_checkouts(client_root, cluster_dir=cluster_dir, cache=cache)
        result = verify_selection(
            client_root,
            checkouts,
            cluster_dir=cluster_dir,
            base=base,
            installable=True,
            staged_projection=projection,
        )
    SelectedRun(result).require_verified()
    return result


def selected_run(verified: VerifiedSelection, args: argparse.Namespace) -> SelectedRun:
    """Validate selected credential input groups without a separate operation lifecycle."""
    run = SelectedRun(verified, tuple(args.input_group or ()))
    run.require_verified()
    allowed = {
        f"{addon}/{field.supplied.group}"
        for addon, setup in run.setups.items()
        for record in setup.records
        for field in record.fields.values()
        if field.supplied is not None
    }
    if not set(run.input_groups) <= allowed or len(set(run.input_groups)) != len(run.input_groups):
        raise OpenBaoError("Input groups must be unique active selected groups")
    return run


def prepare_native_outputs(
    cluster: Cluster, run: SelectedRun, needed: set[tuple[str, str]]
) -> None:
    """Prepare only missing native credentials, without passing OpenBao authority to native code."""
    run.require_verified()
    native_requests = []
    for item in run.verified.addons:
        addon = item.definition.metadata.name
        outputs = {output.id: output for output in item.definition.spec.native_outputs}
        artifacts = {artifact.id: artifact for artifact in item.definition.spec.artifacts}
        requested = {
            field.prepared_output
            for record in item.definition.spec.setup.records
            for field in record.fields.values()
            if field.prepared_output is not None and (addon, field.prepared_output) in needed
        }
        for output_id in sorted(requested):
            records = {
                record.id
                for record in item.definition.spec.setup.records
                if any(field.prepared_output == output_id for field in record.fields.values())
            }
            namespaces = {
                delivery.namespace
                for delivery in item.definition.spec.setup.deliveries
                if records.intersection(delivery.records)
            }
            if len(namespaces) != 1:
                raise OpenBaoError(
                    "Native credential preparation requires one unambiguous delivery namespace"
                )
            namespace = namespaces.pop()
            output = outputs[output_id]
            pull_inputs = approved_pull_inputs(item.definition.spec.inputs, run.verified, namespace)
            native_requests.append(
                (addon, namespace, output, artifacts[output.artifact], pull_inputs)
            )
    # Preflight only inputs actually attached to this operation's native Pods.
    pull_inputs = {
        (item.namespace, item.name): item
        for _, _, _, _, inputs in native_requests
        for item in inputs
    }
    cluster.require_external_inputs(list(pull_inputs.values()))
    for addon, namespace, output, artifact, pull_inputs in native_requests:
        values = prepare_native_output(
            cluster,
            namespace=namespace,
            definition=output,
            artifact=artifact,
            pull_inputs=pull_inputs,
        )
        run.prepared.setdefault(addon, {})[output.id] = values[output.output_key]


def approved_pull_inputs(
    inputs: list[Input], verified: VerifiedSelection, namespace: str
) -> tuple[ExternalInput, ...]:
    """Select only product-requested, client-approved registry inputs in the Pod namespace."""
    requested = {
        (item.namespace, item.name): item
        for item in inputs
        if item.kind == "Secret" and item.secret_type == REGISTRY_TYPE
    }
    approved = {(item.namespace, item.name): item for item in verified.selection.external_inputs}
    result = []
    for key, item in requested.items():
        allowed = approved.get(key)
        if (
            item.owner != "client"
            or allowed is None
            or allowed.secret_type != REGISTRY_TYPE
            or not item.keys
            or not set(item.keys) <= set(allowed.keys)
            or item.namespace != namespace
        ):
            raise OpenBaoError(
                "Native pull input requires exact product/client approval in its namespace"
            )
        result.append(allowed)
    return tuple(result)
