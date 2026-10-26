"""Offline effective-image validation for local charts with approved nonsecret values."""

from __future__ import annotations

import copy
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from k8s_stack_tooling.gateway_catalog.values import merge_values

from .schema import (
    MAX_DOCUMENT_BYTES,
    Addon,
    ContractError,
    _unique_json,
    load,
    parse_yaml,
    resolve_service_identity,
)
from .validation import check_consumers, inside, pod_containers, resource_claims


def _chart(release: dict, sources: Mapping[tuple[str, str, str], Path]) -> Path:
    spec = release["spec"]
    if any(spec.get(field) for field in ("chartRef", "kubeConfig", "postRenderers")):
        raise ContractError("effective images require a local chart without rendering transforms")
    chart = spec.get("chart", {}).get("spec", {})
    if any(chart.get(field) for field in ("valuesFiles", "ignoreMissingValuesFiles")):
        raise ContractError("alternative chart default values are unsupported")
    source = chart.get("sourceRef", {})
    key = (
        source.get("kind"),
        source.get("namespace", release["metadata"]["namespace"]),
        source.get("name"),
    )
    if key[0] != "GitRepository" or key not in sources:
        raise ContractError("chart source is not an exact verified local source")
    root = sources[key].resolve()
    path = inside(root, chart.get("chart", ""))
    if any(
        (parent / ".git").exists()
        for parent in (path, *path.parents)
        if parent != root and parent.is_relative_to(root)
    ):
        raise ContractError("chart path crosses a nested checkout")
    if not (path / "Chart.yaml").is_file():
        raise ContractError("selected source does not contain the requested chart")
    # Helm must not follow symlinks, nested checkouts or packaged dependencies outside
    # this bounded adapter. Acquisition and source revision verification belong upstream.
    for item in path.rglob("*"):
        if item.is_symlink() or item.name == ".git":
            raise ContractError("chart contains a symlink or nested checkout")
        if item.is_file() and item.suffix in (".yaml", ".yml", ".tpl", ".json"):
            text = item.read_text()
            if re.search(r"\blookup\b|\.Release\.Revision", text):
                raise ContractError("chart requires runtime state for rendering")
            if item.name == "values.schema.json":

                def refs(value):
                    if isinstance(value, dict):
                        for name, child in value.items():
                            if name == "$ref" and (
                                not isinstance(child, str) or not child.startswith("#")
                            ):
                                raise ContractError(
                                    "external chart schema references are unsupported"
                                )
                            refs(child)
                    elif isinstance(value, list):
                        for child in value:
                            refs(child)

                try:
                    refs(json.loads(text))
                except ValueError:
                    raise ContractError("invalid or external chart schema") from None
    metadata = load(path / "Chart.yaml")
    if metadata.get("dependencies") or (path / "charts").exists():
        raise ContractError("chart dependencies require a separately supported acquired input")
    return path


def _values(release: dict, configmaps: Mapping[tuple[str, str], dict]) -> dict:
    namespace = release["metadata"]["namespace"]
    result: dict[str, Any] = {}
    for reference in release["spec"].get("valuesFrom", []):
        if reference.get("kind") != "ConfigMap":
            raise ContractError("Secret-dependent Helm values cannot prove effective images")
        if reference.get("targetPath"):
            raise ContractError("valuesFrom targetPath is unsupported for effective image proof")
        key = (namespace, reference["name"])
        if key not in configmaps:
            # Even optional missing objects may exist at reconciliation time. The caller
            # must provide the complete approved input inventory, not assume live absence.
            raise ContractError("approved ConfigMap values input is unavailable")
        resource = configmaps[key]
        if (
            resource.get("kind") != "ConfigMap"
            or resource.get("metadata", {}).get("namespace") != namespace
            or resource.get("metadata", {}).get("name") != reference["name"]
        ):
            raise ContractError("approved ConfigMap identity does not match valuesFrom")
        text = resource.get("data", {}).get(reference.get("valuesKey", "values.yaml"))
        if not isinstance(text, str):
            raise ContractError("approved ConfigMap values key is unavailable")
        values = parse_yaml(text.encode())
        if not isinstance(values, dict):
            raise ContractError("Helm values must be mappings")
        merge_values(result, copy.deepcopy(values))
    inline = release["spec"].get("values", {})
    if not isinstance(inline, dict):
        raise ContractError("inline Helm values must be a mapping")
    merge_values(result, copy.deepcopy(inline))
    if re.search(r"\blookup\b|\.Release\.Revision", yaml.safe_dump(result)):
        raise ContractError("Helm values contain runtime-dependent template input")
    return result


@dataclass(frozen=True)
class HelmImageResult:
    """Actual renders with Secret payloads removed and materialized nonsecret settings."""

    rendered: dict[tuple[str, str, str], list[dict]]
    configmaps: dict[tuple[str, str], dict]
    effective_values: dict[tuple[str, str, str], dict]


def _check_service_settings(addon, packages, rendered, configmaps):
    releases = {
        (package, obj["metadata"]["namespace"], obj["metadata"]["name"]): obj
        for package, objects in packages.items()
        for obj in objects
        if obj.get("kind") == "HelmRelease"
    }
    for request in addon.spec.identities:
        binding = request.service_settings
        if binding is None:
            continue
        matched = False
        for key, objects in rendered.items():
            if key[1] != binding.namespace:
                continue
            references = {
                r["name"]: r
                for r in releases[key]["spec"].get("valuesFrom", [])
                if r.get("kind") == "ConfigMap"
            }
            if not {binding.settings, binding.approval} <= references.keys():
                continue
            approval_map = configmaps.get((binding.namespace, binding.approval))
            if approval_map is None:
                raise ContractError("approved service settings input is missing")
            document = parse_yaml(
                approval_map["data"][
                    references[binding.approval].get("valuesKey", "values.yaml")
                ].encode()
            )
            if not isinstance(document, dict):
                raise ContractError("approved service values must be a mapping")
            for obj in objects:
                for _, container in pod_containers(obj):
                    command = container.get("command", [])
                    if not command or Path(command[0]).name != "upsert-scoped-service-client":
                        continue
                    environment = container.get("env", [])
                    env = {entry["name"]: entry for entry in environment}
                    if len(env) != len(environment):
                        raise ContractError("ambiguous service command environment")
                    if env.get("KC_CLIENT_ID", {}).get("value") != request.client_id:
                        continue

                    def json_env(name, default=None):
                        text = env.get(name, {}).get("value", default)
                        if not isinstance(text, str) or len(text.encode()) > MAX_DOCUMENT_BYTES:
                            raise ContractError("service scope must be bounded nonsecret JSON")
                        try:
                            return json.loads(text, object_pairs_hook=_unique_json)
                        except (ValueError, RecursionError):
                            raise ContractError("invalid generic service scope JSON") from None

                    service = {
                        "clientId": request.client_id,
                        "realmRoles": json_env("KC_REALM_ROLE_SCOPE", "[]"),
                        "gatewayPermissions": json_env("KC_GATEWAY_ROLE_SCOPE"),
                        "audiences": json_env("KC_CLIENT_AUDIENCES"),
                    }
                    resolve_service_identity(
                        request,
                        {"service": service, "serviceApproval": json_env("KC_CLIENT_APPROVAL")},
                    )
                    resolve_service_identity(
                        request,
                        {"service": service, "serviceApproval": document.get("serviceApproval")},
                    )
                    matched = True
        if not matched:
            raise ContractError(
                "service settings do not resolve to a declared scoped service command"
            )


def _ordered_releases(addon: Addon, packages: dict[str, list[dict]]):
    releases = {}
    by_identity = {}
    for package, resources in packages.items():
        for release in resources:
            if release.get("kind") != "HelmRelease":
                continue
            identity = (release["metadata"]["namespace"], release["metadata"]["name"])
            if identity in by_identity:
                raise ContractError("duplicate effective HelmRelease")
            key = (package, *identity)
            releases[key] = release
            by_identity[identity] = key
    producers = {}
    for package in addon.spec.packages:
        for output in package.helm_outputs:
            if output.kind != "ConfigMap":
                continue
            key = (output.namespace, output.name)
            producer = (package.name, output.namespace, output.helm_release)
            if key in producers or producer not in releases:
                raise ContractError("missing or duplicate generated settings producer")
            producers[key] = producer
    dependencies = {}
    for key, release in releases.items():
        spec = release["spec"]
        required = set()
        for reference in spec.get("valuesFrom", []):
            producer = producers.get((key[1], reference["name"]))
            if reference.get("kind") == "ConfigMap" and producer is not None:
                required.add(producer)
        for reference in spec.get("dependsOn", []):
            producer = by_identity.get((reference.get("namespace", key[1]), reference["name"]))
            if producer is not None:
                required.add(producer)
        dependencies[key] = required
    while dependencies:
        ready = sorted(
            key for key, required in dependencies.items() if not required.intersection(dependencies)
        )
        if not ready:
            raise ContractError("cyclic effective Helm settings dependencies")
        for key in ready:
            yield key, releases[key], producers
            del dependencies[key]


def validate_helm_images(
    addon: Addon,
    packages: dict[str, list[dict]],
    *,
    sources: Mapping[tuple[str, str, str], Path],
    configmaps: Mapping[tuple[str, str], dict],
    kube_version: str,
    api_versions: tuple[str, ...],
) -> HelmImageResult:
    """Render effective install/upgrade images and reuse the shared consumer checker.

    Call after source/ownership verification. ``sources`` maps exact namespaced Flux
    identities to their verified checkouts (including the selected Base revision).
    ``packages`` must contain the effective, client-patched HelmReleases, and
    ``configmaps`` only the approved, fully materialized nonsecret values objects.
    Supply reviewed target capabilities explicitly. This helper never acquires charts,
    reads live state, accepts Secret values, logs Helm output or returns rendered Secrets.
    Generated settings are rendered before consumers; conflicting install/upgrade
    settings fail. Unsupported inputs fail; success is not publication or setup authority.
    """
    if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", kube_version):
        raise ContractError("an explicit target Kubernetes version is required")
    rendered = {}
    approved = copy.deepcopy(dict(configmaps))
    generated = {}
    effective_values = {}
    try:
        for key, release, producers in _ordered_releases(addon, packages):
            metadata, spec = release["metadata"], release["spec"]
            chart = _chart(release, sources)
            values = _values(release, approved)
            effective = load(chart / "values.yaml") or {}
            if not isinstance(effective, dict):
                raise ContractError("chart defaults must be a mapping")
            merge_values(effective, copy.deepcopy(values))
            effective_values[key] = effective
            namespace = spec.get("targetNamespace", metadata["namespace"])
            name = spec.get("releaseName") or (
                f"{spec['targetNamespace']}-{metadata['name']}"
                if spec.get("targetNamespace")
                else metadata["name"]
            )
            if len(name) > 53:
                raise ContractError("long derived release names require an explicit releaseName")
            command = [
                "helm",
                "template",
                name,
                str(chart),
                "--namespace",
                namespace,
                "--kube-version",
                kube_version,
                "--values",
                "-",
            ]
            for version in api_versions:
                command.extend(["--api-versions", version])
            objects = []
            for phase in ([], ["--is-upgrade"]):
                process = subprocess.run(
                    command + phase,
                    input=yaml.safe_dump(values),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if process.returncode:
                    raise ContractError("effective Helm rendering failed; output suppressed")
                try:
                    documents = [obj for obj in yaml.safe_load_all(process.stdout) if obj]
                except yaml.YAMLError:
                    raise ContractError("invalid Helm output; output suppressed") from None
                if any(not isinstance(obj, dict) or "kind" not in obj for obj in documents):
                    raise ContractError("invalid Helm object; output suppressed")
                if any(obj["kind"] == "List" for obj in documents):
                    raise ContractError("Helm List objects require explicit workload expansion")
                # Only identities are needed for Secret producer checks.
                phase_claims = set()
                for obj in documents:
                    if obj["kind"] in {
                        "ConfigMap",
                        "Secret",
                        "ExternalSecret",
                        "Pod",
                        "Job",
                        "Deployment",
                        "StatefulSet",
                        "CronJob",
                        "DaemonSet",
                        "Service",
                        "ServiceAccount",
                        "Role",
                        "RoleBinding",
                        "PersistentVolumeClaim",
                    }:
                        obj.setdefault("metadata", {}).setdefault("namespace", namespace)
                    if obj["kind"] == "Secret":
                        obj.pop("data", None)
                        obj.pop("stringData", None)
                    claims = resource_claims(obj)
                    if phase_claims & claims:
                        raise ContractError("effective Helm resources have overlapping ownership")
                    phase_claims.update(claims)
                expected_maps = {
                    identity for identity, producer in producers.items() if producer == key
                }
                actual_maps = {
                    (obj["metadata"]["namespace"], obj["metadata"]["name"])
                    for obj in documents
                    if obj["kind"] == "ConfigMap"
                }
                if not expected_maps <= actual_maps:
                    raise ContractError(
                        "declared generated settings are missing from install or upgrade"
                    )
                objects.extend(documents)
            rendered[key] = objects
            output_maps = {}
            for obj in objects:
                if obj["kind"] != "ConfigMap":
                    continue
                identity = (obj["metadata"]["namespace"], obj["metadata"]["name"])
                if producers.get(identity) != key:
                    continue
                previous = output_maps.get(identity, approved.get(identity))
                if previous is not None and any(
                    previous.get(field, {}) != obj.get(field, {})
                    for field in ("data", "binaryData")
                ):
                    raise ContractError(
                        "generated settings conflict with approved inputs or install/upgrade output"
                    )
                output_maps[identity] = obj
            for identity, producer in producers.items():
                if producer == key and identity not in output_maps:
                    raise ContractError("declared generated settings are missing")
            approved.update(output_maps)
            generated.update(output_maps)
        check_consumers(addon, packages, helm_resources=rendered)
        _check_service_settings(addon, packages, rendered, approved)
        return HelmImageResult(rendered, generated, effective_values)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, UnicodeError):
        raise ContractError(
            "effective Helm image inputs or execution failed; details suppressed"
        ) from None
