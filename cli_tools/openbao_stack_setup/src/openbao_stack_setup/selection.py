"""Bind workstation setup to the shared complete-selection verifier, without acquisition."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
from k8s_stack_tooling.addons.acquisition import source_checkouts
from k8s_stack_tooling.addons.schema import Input

from openbao_stack_setup.client import OpenBaoError
from openbao_stack_setup.cluster import Cluster
from openbao_stack_setup.native import REGISTRY_TYPE, prepare_native_output


@dataclass(frozen=True)
class SelectedRun:
    """Verified nonsecret authority and privately captured pre-custody native outputs."""

    verified: VerifiedSelection
    operation_id: str
    resume: bool = False
    previous_process_stopped: bool = False
    input_groups: tuple[str, ...] = ()
    prepared: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)

    def require_verified(self) -> None:
        """Require verified sources and the exact installed CLI requested by every add-on."""
        if self.verified.installable is not True or self.verified.projection_verified is not True:
            raise OpenBaoError("Setup requires installable sources and verified staged selection")
        try:
            installed = distribution_version("openbao-stack-setup")
        except PackageNotFoundError:
            raise OpenBaoError("Installed stack-setup package metadata is unavailable") from None
        for item in self.verified.addons:
            required = item.declared_definition.spec.prerequisites.stack_setup
            if required != installed:
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
        """Bind retries and handover to sources, approval, features, and instance selection."""
        payload = json.dumps(selection_projection(self.verified), sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()


def verify_client_sources(
    cluster: Cluster, client_root: Path, source_cache: Path, cluster_dir: str
) -> VerifiedSelection:
    """Discover existing local checkouts offline and compare the live staged ConfigMap."""
    base, checkouts = source_checkouts(client_root, cluster_dir=cluster_dir, cache=source_cache)
    result = verify_selection(
        client_root,
        checkouts,
        cluster_dir=cluster_dir,
        base=base,
        installable=True,
        staged_projection=cluster.selection_projection(),
    )
    SelectedRun(result, "").require_verified()
    return result


def selected_run(
    cluster: Cluster, verified: VerifiedSelection, args: argparse.Namespace
) -> SelectedRun:
    """Prepare declared native outputs before the caller loads custody or root authority."""
    run = SelectedRun(
        verified,
        args.operation_id,
        args.resume,
        args.previous_process_stopped,
        tuple(args.input_group or ()),
    )
    run.require_verified()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run.operation_id):
        raise OpenBaoError("Operation ID must be a bounded nonsecret identifier")
    if args.native_namespace is not None and not re.fullmatch(
        r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", args.native_namespace
    ):
        raise OpenBaoError("Invalid native scratch namespace")
    if run.resume != run.previous_process_stopped:
        raise OpenBaoError("Resume requires explicit confirmation that the former process stopped")
    allowed = {
        f"{addon}/{field.supplied.group}"
        for addon, setup in run.setups.items()
        for record in setup.records
        for field in record.fields.values()
        if field.supplied is not None
    }
    if not set(run.input_groups) <= allowed or len(set(run.input_groups)) != len(run.input_groups):
        raise OpenBaoError("Input groups must be unique active selected groups")
    native_requests = []
    for item in verified.addons:
        addon = item.definition.metadata.name
        outputs = {output.id: output for output in item.definition.spec.native_outputs}
        artifacts = {artifact.id: artifact for artifact in item.definition.spec.artifacts}
        requested = {
            field.prepared_output
            for record in item.definition.spec.setup.records
            for field in record.fields.values()
            if field.prepared_output is not None
        }
        if requested and not args.native_namespace:
            raise OpenBaoError(
                "Selected native outputs require an explicit scratch --native-namespace"
            )
        for output_id in sorted(requested):
            output = outputs[output_id]
            pull_inputs = approved_pull_inputs(
                item.definition.spec.inputs, verified, args.native_namespace
            )
            native_requests.append((addon, output, artifacts[output.artifact], pull_inputs))
    # Preflight only inputs actually attached to this operation's native Pods.
    needed = {
        (item.namespace, item.name): item for _, _, _, inputs in native_requests for item in inputs
    }
    cluster.require_external_inputs(list(needed.values()))
    for addon, output, artifact, pull_inputs in native_requests:
        values = prepare_native_output(
            cluster,
            namespace=args.native_namespace,
            definition=output,
            artifact=artifact,
            pull_inputs=pull_inputs,
        )
        run.prepared.setdefault(addon, {})[output.id] = values[output.output_key]
    return run


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
