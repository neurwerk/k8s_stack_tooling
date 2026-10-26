"""Offline binding of the complete client selection to local source checkouts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

from .git import run_git as _git
from .schema import (
    API_VERSION,
    Addon,
    AddonSelection,
    ContractError,
    Selection,
    SourceRef,
    expand_identities,
    expand_setup,
    load_manifest,
    load_selection,
    parse_yaml,
)
from .validation import (
    CLIENT_SOURCE,
    base_revision,
    check,
    check_base,
    check_client,
    check_database_claims,
    check_secret_deliveries,
    check_verifier_reader_separation,
    inside,
    pod_containers,
    render,
    render_addon_packages,
    render_stage,
    resource_claims,
    stage_source,
)

if TYPE_CHECKING:
    from .helm_images import HelmImageResult


@dataclass(frozen=True)
class VerifiedSource:
    reference: SourceRef
    url: str
    revision: str
    manifest_sha256: str


@dataclass(frozen=True)
class SelectedAddon:
    definition: Addon
    selection: AddonSelection
    source: VerifiedSource
    checkout: Path
    features: Mapping[str, bool]
    declared_definition: Addon
    effective_packages: dict[str, list[dict]] | None = None


@dataclass(frozen=True)
class VerifiedSelection:
    """Source-verified definitions; candidate results must never authorize setup."""

    selection: Selection
    base: VerifiedSource
    addons: tuple[SelectedAddon, ...]
    installable: bool
    projection_verified: bool
    cluster_dir: str
    working_base: bool = False


def _key(ref: SourceRef) -> tuple[str, str, str]:
    return ref.kind, ref.namespace, ref.name


def _verify_source(
    root: Path, reference: SourceRef, resource: dict, *, installable: bool, base: bool = False
) -> VerifiedSource:
    root = root.absolute()
    if root != root.resolve() or _git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ContractError("selected checkout must be a nonsymlink repository root")
    spec = resource["spec"]
    # Filtering changes which files Flux can consume. Fail closed until modeled explicitly.
    if any(spec.get(k) for k in ("ignore", "include", "sparseCheckout", "recurseSubmodules")):
        raise ContractError("filtered or composite source artifacts are unsupported")
    if any((root / name).exists() for name in (".sourceignore", ".gitmodules")):
        raise ContractError("source ignore files and submodules are unsupported")
    url = spec.get("url", "")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in ("https", "ssh")
            and parsed.hostname
            and parsed.path
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.username is None or (parsed.scheme == "ssh" and parsed.username == "git"))
            and not any(c.isspace() or ord(c) < 32 for c in url)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid or _git(root, "config", "--local", "--get", "remote.origin.url") != url:
        raise ContractError("selected source URL does not match checkout origin")
    ref = spec.get("ref", {})
    if base and set(ref) == {"tag"}:
        revision = base_revision(root, ref)
    elif set(ref) == {"commit"} and re.fullmatch(r"[a-f0-9]{40}", ref["commit"]):
        revision = ref["commit"]
    else:
        raise ContractError("selected source requires an immutable revision")
    if _git(root, "rev-parse", "--verify", "HEAD^{commit}") != revision:
        raise ContractError("checkout does not match selected revision")
    if installable and _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ContractError("installable checkout is dirty")
    manifest = root / "release/manifest.yaml"
    if manifest != manifest.resolve() or not manifest.is_file():
        raise ContractError("selected manifest must be a regular file")
    # Check tracked regular-file mode even when candidate content is intentionally dirty.
    entry = _git(root, "ls-tree", revision, "--", "release/manifest.yaml")
    if not entry.startswith("100644 blob "):
        raise ContractError("manifest must be a committed regular file")
    with manifest.open("rb") as stream:
        from .schema import MAX_DOCUMENT_BYTES

        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ContractError("manifest exceeds size limit")
    if (
        installable
        and data != _git(root, "show", f"{revision}:release/manifest.yaml", strip=False).encode()
    ):
        raise ContractError("manifest differs from selected revision")
    return VerifiedSource(reference, url, revision, hashlib.sha256(data).hexdigest())


def verify_selection(
    client: Path,
    checkouts: Mapping[str, Path],
    *,
    cluster_dir: str,
    base: Path,
    installable: bool = True,
    staged_projection: Mapping[str, object] | None = None,
    base_candidate: Path | None = None,
) -> VerifiedSelection:
    """Verify every selected add-on; no acquisition, credentials, or cluster access.

    Setup must require both ``installable`` and ``projection_verified`` on the result.
    Pass the live ConfigMap object as ``staged_projection`` to establish the latter.
    """
    client = client.resolve()
    if base_candidate is not None and (installable or base_candidate.resolve() != base.resolve()):
        raise ContractError(
            "working Base candidate requires candidate mode and the explicit Base checkout"
        )
    selection = load_selection(inside(client, "config/addons.yaml"))
    if set(checkouts) != {item.id for item in selection.addons}:
        raise ContractError("checkouts must cover the complete selection exactly")
    resources = render(inside(client, cluster_dir), root=client)
    sources = {}
    for resource in resources:
        if resource.get("kind") != "GitRepository":
            continue
        metadata = resource["metadata"]
        key = (resource["kind"], metadata.get("namespace", ""), metadata["name"])
        if key in sources:
            raise ContractError("duplicate source identity")
        sources[key] = resource

    def source(ref: SourceRef) -> dict:
        if _key(ref) not in sources:
            raise ContractError("selected namespaced source is missing")
        return sources[_key(ref)]

    base_source = _verify_source(
        base,
        selection.base_source_ref,
        source(selection.base_source_ref),
        installable=installable,
        base=True,
    )
    selected = []
    claimed_records: set[str] = set()
    claimed_roles: dict[str, tuple[str, str, str]] = {}
    claimed_identities: set[str] = set()
    for item in selection.addons:
        root = checkouts[item.id]
        verified = _verify_source(
            root, item.source_ref, source(item.source_ref), installable=installable
        )
        definition = load_manifest(root / "release/manifest.yaml")
        effective_packages = render_addon_packages(
            definition,
            root,
            [obj for obj in resources if obj.get("kind") == "Kustomization"],
            item.source_ref,
        )
        definition = check(root, installable=installable, effective_packages=effective_packages)
        if (
            definition.metadata.name != item.id
            or definition.spec.source_name != item.source_ref.name
        ):
            raise ContractError("selected source and manifest identity disagree")
        check_base(definition, base, None if base_candidate is not None else base_source.revision)
        if not item.features.keys() <= definition.spec.features.keys():
            raise ContractError("selection contains undeclared features")
        features = definition.spec.features | item.features
        setup = expand_setup(definition, item)
        identities = expand_identities(definition, item)
        if sorted(i.model_dump_json() for i in identities) != sorted(
            i.model_dump_json() for i in item.approved_identities
        ):
            raise ContractError("identity requests require exact client approval")
        for identity in identities:
            if identity.client_id in claimed_identities:
                raise ContractError("selected identity clients have overlapping ownership")
            claimed_identities.add(identity.client_id)
        # Consumers receive resolved typed definitions, while provenance hashes the source manifest.
        declared_definition = definition
        definition = definition.model_copy(deep=True)
        definition.spec.setup = setup
        definition.spec.identities = identities
        definition.spec.instance_recipes = []
        readers = definition.spec.setup.readers
        if sorted(r.model_dump_json() for r in readers) != sorted(
            r.model_dump_json() for r in item.approved_readers
        ):
            raise ContractError("reader requests require exact client approval")
        for record in definition.spec.setup.records:
            if record.path in claimed_records:
                raise ContractError("selected credentials have overlapping ownership")
            claimed_records.add(record.path)
        for reader in readers:
            binding = (reader.namespace, reader.service_account, reader.audience)
            if reader.role in claimed_roles and claimed_roles[reader.role] != binding:
                raise ContractError("selected shared reader role has conflicting bindings")
            claimed_roles[reader.role] = binding
        selected.append(
            SelectedAddon(
                definition,
                item,
                verified,
                root.resolve(),
                features,
                declared_definition,
                effective_packages,
            )
        )
    check_database_claims([item.definition for item in selected])
    check_verifier_reader_separation([item.definition for item in selected])
    check_client(
        [(item.checkout, item.definition) for item in selected],
        client,
        installable=installable,
        base=base,
        cluster_dir=cluster_dir,
        base_candidate=base_candidate,
    )
    result = VerifiedSelection(
        selection,
        base_source,
        tuple(selected),
        installable,
        False,
        cluster_dir,
        base_candidate is not None,
    )
    if staged_projection is not None:
        expected = selection_projection(result)
        # Ignore API-server metadata, but bind identity and the complete nonsecret payload.
        metadata = staged_projection.get("metadata", {})
        if (
            not isinstance(metadata, Mapping)
            or staged_projection.get("apiVersion") != "v1"
            or staged_projection.get("kind") != "ConfigMap"
            or metadata.get("name") != "addon-selection"
            or metadata.get("namespace") != "flux-system"
            or staged_projection.get("data") != expected["data"]
        ):
            raise ContractError("staged selection projection does not match verified sources")
        result = VerifiedSelection(
            selection,
            base_source,
            tuple(selected),
            installable,
            True,
            cluster_dir,
            base_candidate is not None,
        )
    return result


def _selected_base_resources(selection: VerifiedSelection, client: Path, base: Path) -> list[dict]:
    client = client.resolve()
    if load_selection(inside(client, "config/addons.yaml")) != selection.selection:
        raise ContractError("client selection changed before composition validation")
    resources = render(inside(client, selection.cluster_dir), root=client)
    reference = selection.base.reference
    source = next(
        (
            obj
            for obj in resources
            if obj.get("kind") == reference.kind
            and obj.get("metadata", {}).get("namespace") == reference.namespace
            and obj.get("metadata", {}).get("name") == reference.name
        ),
        None,
    )
    if (
        source is None
        or _verify_source(base, reference, source, installable=selection.installable, base=True)
        != selection.base
    ):
        raise ContractError("selected Base source changed before store validation")
    stages = []
    for obj in resources:
        if obj.get("kind") != "Kustomization" or stage_source(obj) != _key(reference):
            continue
        stages.append(obj)
    return [obj for stage in stages for obj in render_stage(stage, base)]


def _database_provider_resources(
    resources: list[dict],
    *,
    base_source: SourceRef,
    sources: Mapping[tuple[str, str, str], Path],
    configmaps: Mapping[tuple[str, str], dict],
    kube_version: str,
    api_versions: tuple[str, ...],
) -> list[dict]:
    """Render only the supported Base provider's nonsecret endpoint topology with Helm.

    Use the existing chart confinement and nonsecret values loader. In particular,
    Secret-backed provider values cannot be dropped: they could override any port.
    Do not evaluate unrelated provider Secret/provisioning templates or invent input.
    """
    from .helm_images import _chart, _values

    result = []
    for resource in resources:
        if resource.get("kind") != "HelmRelease":
            continue
        spec = resource["spec"]
        chart = spec.get("chart", {}).get("spec", {})
        if chart.get("chart", "").removeprefix("./") != "charts/postgres/operations":
            continue
        reference = chart.get("sourceRef", {})
        if (
            reference.get("kind"),
            reference.get("namespace", resource["metadata"]["namespace"]),
            reference.get("name"),
        ) != _key(base_source):
            raise ContractError("database provider must come from the selected Base source")
        path = _chart(resource, sources)
        values = _values(resource, configmaps)
        namespace = spec.get("targetNamespace", resource["metadata"]["namespace"])
        name = spec.get("releaseName") or (
            f"{namespace}-{resource['metadata']['name']}"
            if spec.get("targetNamespace")
            else resource["metadata"]["name"]
        )
        # A confined chart slice uses Helm itself, not a second template engine.
        # StatefulSet checksum references config-map.yaml; none of these files
        # materialize provider credentials. Missing/changed interfaces fail closed.
        with tempfile.TemporaryDirectory(prefix="database-endpoints-") as temporary:
            root = Path(temporary)
            for file in (
                "Chart.yaml",
                "values.yaml",
                "templates/_helpers.tpl",
                "templates/service.yaml",
                "templates/stateful-set.yaml",
                "templates/config-map.yaml",
            ):
                destination = root / file
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(path / file, destination)
                except OSError:
                    raise ContractError(
                        "selected Base lacks the database topology interface"
                    ) from None
            command = [
                "helm",
                "template",
                name,
                str(root),
                "--namespace",
                namespace,
                "--kube-version",
                kube_version,
                "--values",
                "-",
            ]
            for version in api_versions:
                command.extend(["--api-versions", version])
            phases = []
            for phase in ([], ["--is-upgrade"]):
                try:
                    process = subprocess.run(
                        command + phase,
                        input=yaml.safe_dump(values),
                        text=True,
                        capture_output=True,
                        timeout=60,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    raise ContractError("could not render database provider topology") from None
                if process.returncode:
                    raise ContractError(
                        "database provider endpoint rendering failed; output suppressed"
                    )
                objects = [obj for obj in parse_yaml(process.stdout.encode(), multiple=True) if obj]
                if any(
                    not isinstance(obj, dict)
                    or obj.get("kind") not in {"Service", "StatefulSet", "ConfigMap"}
                    for obj in objects
                ):
                    raise ContractError("unexpected resource in database provider topology")
                for obj in objects:
                    obj.setdefault("metadata", {}).setdefault("namespace", namespace)
                phases.append(objects)
            if phases[0] != phases[1]:
                raise ContractError(
                    "database provider topology differs between install and upgrade"
                )
            result.extend(phases[0])
    return result


def _check_database_bindings(
    selection: VerifiedSelection,
    packages: Mapping[str, dict[str, list[dict]]],
    results: Mapping[str, HelmImageResult],
    providers: list[dict],
) -> None:
    """Bind every claim to one verified Base provisioner and the actual engine port."""
    try:
        for item in selection.addons:
            addon = item.definition
            result = results[addon.metadata.name]
            for claim in addon.spec.databases:
                releases = []
                for release in packages[addon.metadata.name][claim.package]:
                    if release.get("kind") != "HelmRelease":
                        continue
                    chart = release["spec"].get("chart", {}).get("spec", {})
                    if chart.get("chart", "").removeprefix("./") != claim.base_chart:
                        continue
                    reference = chart.get("sourceRef", {})
                    if (
                        reference.get("kind"),
                        reference.get("namespace", release["metadata"]["namespace"]),
                        reference.get("name"),
                    ) != _key(selection.base.reference):
                        raise ContractError(
                            "database provisioning must use the selected Base source"
                        )
                    releases.append(release)
                if len(releases) != 1:
                    raise ContractError("database claim requires one verified provisioning release")
                release = releases[0]
                key = (claim.package, release["metadata"]["namespace"], release["metadata"]["name"])
                settings = result.effective_values[key]["addon"]
                sql = claim.engine == "postgresql"
                if sql:
                    declarations = settings["databases"]
                    databases = [d for d in declarations if d["name"] == claim.database]
                    if (
                        settings["role"] != claim.role
                        or len(databases) != 1
                        or bool(databases[0].get("vector", False)) != ("vector" in claim.extensions)
                    ):
                        raise ContractError(
                            "database claim differs from effective SQL provisioning"
                        )
                elif (
                    settings.get("enabled") is not True
                    or settings["user"] != claim.role
                    or settings["database"] != claim.database
                ):
                    raise ContractError(
                        "database claim differs from effective DocumentDB provisioning"
                    )
                service_key = (claim.service.namespace, claim.service.name)
                services = [
                    obj
                    for obj in providers
                    if obj["kind"] == "Service"
                    and (obj["metadata"]["namespace"], obj["metadata"]["name"]) == service_key
                ]
                if len(services) != 1:
                    raise ContractError("database claim requires one actual selected Base Service")
                service = services[0]
                ports = {p["name"]: p for p in service["spec"]["ports"]}
                if len(ports) != len(service["spec"]["ports"]):
                    raise ContractError("ambiguous database provider port names")
                if any(
                    type(p["port"]) is not int or not 1 <= p["port"] <= 65535
                    for p in ports.values()
                ) or len({p["port"] for p in ports.values()}) != len(ports):
                    raise ContractError("database provider has invalid or overlapping engine ports")
                engine_port = "postgresql" if sql else "documentdb"
                host = settings["postgresHost" if sql else "host"]
                if host not in {
                    f"{claim.service.name}.{claim.service.namespace}.svc",
                    f"{claim.service.name}.{claim.service.namespace}.svc.cluster.local",
                }:
                    raise ContractError(
                        "database provisioning host differs from its claimed Service"
                    )
                required_ports = {"postgresql": settings["postgresPort"]}
                if not sql:
                    required_ports["documentdb"] = settings["documentdbPort"]
                for name, port in required_ports.items():
                    entry = ports[name]
                    if (
                        type(port) is not int
                        or entry["port"] != port
                        or entry.get("protocol", "TCP") != "TCP"
                        or entry["targetPort"] != name
                    ):
                        raise ContractError(
                            "database provisioning port differs from its engine endpoint"
                        )
                selector = service["spec"]["selector"]
                pods = [
                    obj["spec"]["template"]
                    for obj in providers
                    if obj["kind"] == "StatefulSet"
                    and obj["metadata"]["namespace"] == claim.service.namespace
                    and selector
                    and selector.items()
                    <= obj["spec"]["template"]["metadata"].get("labels", {}).items()
                ]
                if len(pods) != 1:
                    raise ContractError("database Service requires one selected provider workload")
                target = ports[engine_port]["targetPort"]
                targets = [
                    (container, port)
                    for container in pods[0]["spec"]["containers"]
                    for port in container.get("ports", [])
                    if port.get("name") == target
                ]
                if len(targets) != 1:
                    raise ContractError(
                        "database Service target does not identify its engine listener"
                    )
                container, port = targets[0]
                environment = {e["name"]: e.get("value") for e in container.get("env", [])}
                variable = "POSTGRESQL_PORT" if sql else "DOCUMENTDB_PORT"
                if environment.get(variable) != str(port["containerPort"]):
                    raise ContractError(
                        "database Service target differs from the native engine listener"
                    )
                jobs = [obj for obj in result.rendered[key] if obj["kind"] == "Job"]
                if not jobs:
                    raise ContractError("database provisioning Job is absent")
                for job in jobs:
                    containers = [c for _, c in pod_containers(job) if c["name"] == "provision"]
                    if len(containers) != 1:
                        raise ContractError("database provisioning container is ambiguous")
                    env = {e["name"]: e.get("value") for e in containers[0].get("env", [])}
                    if env.get("PGHOST") != host or env.get("PGPORT") != str(
                        settings["postgresPort"]
                    ):
                        raise ContractError(
                            "rendered database connection differs from verified settings"
                        )
                    if not sql and (
                        env.get("DOCUMENTDB_HOST") != host
                        or env.get("DOCUMENTDB_PORT") != str(settings["documentdbPort"])
                    ):
                        raise ContractError(
                            "rendered document endpoint differs from verified settings"
                        )
    except (KeyError, TypeError, AttributeError):
        raise ContractError("incomplete verified database endpoint binding") from None
    check_database_claims([item.definition for item in selection.addons])


def validate_selection_images(
    selection: VerifiedSelection,
    *,
    client: Path,
    base: Path,
    packages: Mapping[str, dict[str, list[dict]]] | None = None,
    configmaps: Mapping[tuple[str, str], dict],
    kube_version: str,
    api_versions: tuple[str, ...],
) -> dict[str, HelmImageResult]:
    """Validate complete effective add-on renders against the source-verified snapshot.

    Callers supply client-patched packages and approved nonsecret ConfigMaps. This
    returns actual sanitized renders; it never upgrades a candidate to setup authority.
    """
    from .helm_images import validate_helm_images

    if packages is None:
        packages = {}
        for item in selection.addons:
            if item.effective_packages is None:
                raise ContractError("source snapshot lacks effective selected Flux resources")
            packages[item.definition.metadata.name] = item.effective_packages
    if set(packages) != {item.definition.metadata.name for item in selection.addons}:
        raise ContractError("effective packages must cover the complete selection")
    sources = {}
    for expected, root in [
        (selection.base, base),
        *((item.source, item.checkout) for item in selection.addons),
    ]:
        actual = _verify_source(
            root,
            expected.reference,
            {"spec": {"url": expected.url, "ref": {"commit": expected.revision}}},
            installable=selection.installable,
        )
        if actual != expected:
            raise ContractError("selected source snapshot changed before effective rendering")
        sources[_key(expected.reference)] = root
    for key, obj in configmaps.items():
        if (
            obj.get("kind") != "ConfigMap"
            or (obj.get("metadata", {}).get("namespace"), obj.get("metadata", {}).get("name"))
            != key
        ):
            raise ContractError("approved nonsecret ConfigMap identity is invalid")
    base_resources = _selected_base_resources(selection, client, base)
    client_root = client.resolve()
    client_resources = list(render(inside(client_root, selection.cluster_dir), root=client_root))
    client_stages = [
        obj
        for obj in client_resources
        if obj.get("kind") == "Kustomization" and stage_source(obj) == CLIENT_SOURCE
    ]
    for stage in client_stages:
        if inside(client_root, stage["spec"]["path"]) != inside(client_root, selection.cluster_dir):
            client_resources.extend(render_stage(stage, client_root))
    check_verifier_reader_separation([item.definition for item in selection.addons])
    results = {}
    claimed = {}
    for owner, objects in (("base", base_resources), ("client", client_resources)):
        for obj in objects:
            for claim in resource_claims(obj):
                if claim in claimed:
                    raise ContractError("Base and client resources have overlapping ownership")
                claimed[claim] = (owner,)
    for item in selection.addons:
        addon = item.definition
        product = addon.metadata.name
        expected_packages = render_addon_packages(
            addon,
            item.checkout,
            [obj for obj in client_resources if obj.get("kind") == "Kustomization"],
            item.selection.source_ref,
        )
        if packages[product] != expected_packages:
            raise ContractError("effective packages differ from the selected Flux patches")
        if set(packages[product]) != {p.name for p in addon.spec.packages}:
            raise ContractError("effective packages omit or add selected deployment packages")
        result = validate_helm_images(
            addon,
            packages[product],
            sources=sources,
            configmaps=configmaps,
            kube_version=kube_version,
            api_versions=api_versions,
        )
        if result is None:
            raise ContractError("effective rendering did not return validated outputs")
        for name, objects in packages[product].items():
            for obj in objects:
                for claim in resource_claims(obj):
                    if claim in claimed:
                        raise ContractError(
                            "effective selected resources have overlapping ownership"
                        )
                    claimed[claim] = (product, name, "direct")
        for release, objects in result.rendered.items():
            owner = (product, release)
            # Install and upgrade may return the same identity from the same producer.
            for claim in {claim for obj in objects for claim in resource_claims(obj)}:
                if claim in claimed and claimed[claim] != owner:
                    raise ContractError("effective selected resources have overlapping ownership")
                claimed[claim] = owner
        check_secret_deliveries(
            addon,
            [obj for objects in packages[product].values() for obj in objects]
            + [obj for objects in result.rendered.values() for obj in objects],
            base_resources,
            client_resources=client_resources,
        )
        results[product] = result
    if any(item.definition.spec.databases for item in selection.addons):
        providers = _database_provider_resources(
            base_resources,
            base_source=selection.base.reference,
            sources=sources,
            configmaps=configmaps,
            kube_version=kube_version,
            api_versions=api_versions,
        )
        _check_database_bindings(selection, packages, results, providers)
    return results


def selection_projection(selection: VerifiedSelection) -> dict[str, object]:
    """Produce nonsecret staging data; source pins are derived, never hand-maintained."""

    def source(value: VerifiedSource):
        return {
            "sourceRef": value.reference.model_dump(by_alias=True),
            "url": value.url,
            "revision": value.revision,
            "manifestSha256": value.manifest_sha256,
        }

    payload = {
        "apiVersion": API_VERSION,
        "clusterDirectory": selection.cluster_dir,
        "installable": selection.installable,
        "workingBase": selection.working_base,
        "base": source(selection.base),
        "selection": selection.selection.model_dump(by_alias=True),
        "addons": [
            {"id": item.definition.metadata.name, **source(item.source)}
            for item in sorted(selection.addons, key=lambda item: item.definition.metadata.name)
        ],
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "addon-selection", "namespace": "flux-system"},
        "data": {"selection.json": json.dumps(payload, sort_keys=True, separators=(",", ":"))},
    }


def projection_main(argv: list[str] | None = None) -> int:
    """Generate nonsecret staging metadata from already acquired sources, offline."""
    from .acquisition import source_checkouts

    parser = argparse.ArgumentParser(prog="addon-project", description=__doc__)
    parser.add_argument("--client", required=True, type=Path)
    parser.add_argument("--cluster-dir", required=True)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-candidate", type=Path)
    parser.add_argument(
        "--candidate",
        action="store_true",
        help="Allow reviewed working candidates; never authorizes setup",
    )
    args = parser.parse_args(argv)
    try:
        base, checkouts = source_checkouts(
            args.client, cluster_dir=args.cluster_dir, cache=args.cache
        )
        if args.base_candidate is not None:
            if not args.candidate:
                raise ContractError("working Base candidate requires --candidate")
            base = args.base_candidate
        verified = verify_selection(
            args.client,
            checkouts,
            base=base,
            cluster_dir=args.cluster_dir,
            installable=not args.candidate,
            base_candidate=args.base_candidate,
        )
        document = yaml.safe_dump(selection_projection(verified), sort_keys=False)
        if args.output is None:
            sys.stdout.write(document)
        else:
            args.output.write_text(document)
    except (ContractError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(
            "Cannot generate selection projection; check selected sources and composition.",
            file=sys.stderr,
        )
        return 1
    return 0
