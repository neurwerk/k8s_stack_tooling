"""Validate addon release packages and their selected client dependencies offline."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

import yaml

from .git import run_git
from .schema import (
    MAX_DOCUMENT_BYTES,
    Addon,
    CommandConsumer,
    ContractError,
    SourceRef,
    _unique_json,
    load,
    load_manifest,
    load_selection,
    parse_yaml,
)


def inside(root: Path, path: str) -> Path:
    """Reject absolute paths, traversal and symlinks outside a repository."""
    result = (root / path).resolve()
    if (
        Path(path).is_absolute()
        or ".." in Path(path).parts
        or not result.is_relative_to(root.resolve())
    ):
        raise ContractError("package path leaves its repository")
    return result


def render(path: Path, *, root: Path | None = None) -> list[dict]:
    """Build Kubernetes objects without showing configuration or Secret values."""
    confine(path, root or path)
    return _kustomize_build(path)


def _kustomize_build(path: Path) -> list[dict]:
    result = subprocess.run(
        ["kustomize", "build", "--load-restrictor", "LoadRestrictionsNone", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise ContractError("Kustomize build failed")
    try:
        documents = [item for item in yaml.safe_load_all(result.stdout) if item]
        if any(not isinstance(item, dict) for item in documents):
            raise ContractError("package contains a non-object document")
        return documents
    except yaml.YAMLError:
        raise ContractError("invalid rendered YAML") from None


def render_stage(stage: dict, root: Path) -> list[dict]:
    """Apply selected Flux inline patches with Kustomize, never a custom patch engine."""
    root = root.resolve()
    spec = stage["spec"]
    path = inside(root, spec["path"])
    if any(
        spec.get(field)
        for field in (
            "images",
            "components",
            "postBuild",
            "decryption",
            "targetNamespace",
            "namePrefix",
            "nameSuffix",
            "kubeConfig",
        )
    ):
        raise ContractError("unsupported Flux render transform")
    patches = spec.get("patches", [])
    if not patches:
        return render(path, root=root)
    if not isinstance(patches, list) or len(patches) > 100:
        raise ContractError("Flux patches must be a bounded inline list")
    for patch in patches:
        if (
            not isinstance(patch, dict)
            or set(patch) - {"patch", "target"}
            or not isinstance(patch.get("patch"), str)
        ):
            raise ContractError("Flux patches must be inline and cannot reference files")
        documents = parse_yaml(patch["patch"].encode(), multiple=True)
        if not documents or any(not isinstance(document, (dict, list)) for document in documents):
            raise ContractError("invalid inline Flux patch")
        target = patch.get("target", {})
        if (
            not isinstance(target, dict)
            or set(target)
            - {
                "group",
                "version",
                "kind",
                "name",
                "namespace",
                "labelSelector",
                "annotationSelector",
            }
            or any(not isinstance(value, str) or len(value) > 1024 for value in target.values())
        ):
            raise ContractError("invalid inline Flux patch target")
    confine(path, root)
    with tempfile.TemporaryDirectory(prefix="addon-flux-render-") as directory:
        wrapper = Path(directory)
        (wrapper / "kustomization.yaml").write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "kustomize.config.k8s.io/v1beta1",
                    "kind": "Kustomization",
                    "resources": [os.path.relpath(path, wrapper)],
                    "patches": patches,
                }
            )
        )
        return _kustomize_build(wrapper)


def confine(path: Path, root: Path, seen: set[Path] | None = None) -> None:
    """Check local Kustomize references before invoking the renderer."""
    root = root.resolve()
    path = inside(root, str(path.absolute().relative_to(root)))
    if ".git" in path.relative_to(root).parts or any(
        (parent / ".git").exists()
        for parent in (path, *path.parents)
        if parent != root and parent.is_relative_to(root)
    ):
        raise ContractError("rendering input crosses a nested checkout or repository metadata")
    seen = set() if seen is None else seen
    if path in seen:
        raise ContractError("cyclic Kustomize reference")
    if not path.is_dir():
        if not path.is_file():
            raise ContractError("missing local rendering input")
        return
    seen.add(path)
    candidates = [
        path / name for name in ("kustomization.yaml", "kustomization.yml", "Kustomization")
    ]
    files = [candidate for candidate in candidates if candidate.is_file()]
    if len(files) != 1:
        raise ContractError("expected one local Kustomization")
    document = load(inside(root, str(files[0].relative_to(root))))
    if not isinstance(document, dict):
        raise ContractError("invalid Kustomization")
    if any(
        document.get(key) for key in ("helmCharts", "helmGlobals", "generators", "transformers")
    ):
        raise ContractError("external rendering generators are unsupported")
    references = []
    for key in (
        "resources",
        "bases",
        "components",
        "crds",
        "configurations",
        "patchesStrategicMerge",
    ):
        references.extend(document.get(key, []))
    for key in ("patches", "patchesJson6902", "replacements"):
        references.extend(item["path"] for item in document.get(key, []) if "path" in item)
    for key in ("configMapGenerator", "secretGenerator"):
        for generator in document.get(key, []):
            references.extend(item.split("=", 1)[-1] for item in generator.get("files", []))
            references.extend(generator.get("envs", []))
            if generator.get("env"):
                references.append(generator["env"])
    if document.get("openapi", {}).get("path"):
        references.append(document["openapi"]["path"])
    for reference in references:
        if (
            not isinstance(reference, str)
            or ":" in reference
            or "?" in reference
            or "//" in reference
        ):
            raise ContractError("remote rendering input is undeclared")
        target = (path / reference).resolve()
        if Path(reference).is_absolute() or not target.is_relative_to(root):
            raise ContractError("rendering input leaves its repository")
        confine(target, root, seen)
    seen.remove(path)


def identity(resource: dict) -> tuple[str, str, str, str]:
    """Compare resource identities independently of API version."""
    metadata = resource["metadata"]
    api = resource["apiVersion"]
    group = api.split("/")[0] if "/" in api else ""
    return group, resource["kind"], metadata.get("namespace", ""), metadata["name"]


def resource_claims(resource: dict) -> set[tuple[str, str, str, str]]:
    """Include controller-produced Secrets in the same ownership index as objects."""
    claims = {identity(resource)}
    if resource["kind"] == "ExternalSecret":
        metadata = resource["metadata"]
        target = resource["spec"].get("target", {}).get("name", metadata["name"])
        claims.add(("", "Secret", metadata.get("namespace", ""), target))
    return claims


def check(
    root: Path,
    *,
    installable: bool = False,
    base: Path | None = None,
    effective_packages: dict[str, list[dict]] | None = None,
) -> Addon:
    """Check the release graph, resource ownership and required Base charts."""
    root = root.resolve()
    addon = load_manifest(root / "release/manifest.yaml")
    check_compatibility(addon, installable=installable)
    check_artifacts(addon, installable=installable)
    check_database_claims([addon])
    if not re.fullmatch(r"[a-z][a-z0-9-]*", addon.metadata.name):
        raise ContractError("invalid addon ID")
    if not re.fullmatch(r"\d+\.\d+\.\d+", addon.spec.version):
        raise ContractError("invalid addon version")
    if not re.fullmatch(r"[a-z][a-zA-Z0-9]*", addon.spec.configuration_key):
        raise ContractError("invalid configuration key")
    if installable and addon.spec.status != "ready":
        raise ContractError("addon is staged; publication prerequisites are incomplete")
    packages = {package.name: package for package in addon.spec.packages}
    if not packages or len(packages) != len(addon.spec.packages):
        raise ContractError("package names must be nonempty and unique")
    if effective_packages is not None and effective_packages.keys() != packages.keys():
        raise ContractError("effective resources must cover every declared package")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name not in packages or name in visiting:
            raise ContractError("missing or cyclic package dependency")
        if name in visited:
            return
        visiting.add(name)
        for dependency in packages[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    owners: set[tuple[str, str, str, str]] = set()
    outputs: dict[tuple[str, str, str], str | None] = {
        (item.kind, item.namespace, item.name): None for item in addon.spec.inputs
    }
    generated: dict[tuple[str, str, str], tuple[str, str]] = {}
    if len(outputs) != len(addon.spec.inputs):
        raise ContractError("duplicate input")
    for name, package in packages.items():
        for output in package.helm_outputs:
            key = (output.kind, output.namespace, output.name)
            if key in outputs:
                raise ContractError("duplicate input or generated producer")
            outputs[key] = name
            generated[key] = (name, output.helm_release)
    helm_releases: dict[tuple[str, str, str], dict] = {}
    consumers: list[tuple[str, str, str, dict]] = []
    rendered_packages: dict[str, list[dict]] = {}
    for name, package in packages.items():
        visit(name)
        package_path = inside(root, package.path)
        resources = (
            render(package_path, root=root)
            if effective_packages is None
            else effective_packages[name]
        )
        rendered_packages[name] = resources
        if not resources:
            raise ContractError("package rendered no resources")
        for resource in resources:
            key = identity(resource)
            if key in owners:
                raise ContractError("resource is owned by multiple packages")
            owners.add(key)
            kind, namespace, resource_name = key[1:]
            output = None
            if kind == "ConfigMap":
                output = (kind, namespace, resource_name)
            elif kind == "ExternalSecret":
                target_name = resource["spec"].get("target", {}).get("name", resource_name)
                output = ("Secret", namespace, target_name)
            if output:
                if output in outputs:
                    raise ContractError("duplicate input or secret producer")
                outputs[output] = name
            if resource["kind"] == "Secret":
                raise ContractError("release packages must not contain Secret values")
            if resource["kind"] != "HelmRelease":
                continue
            helm_releases[(name, namespace, resource_name)] = resource
            for reference in resource["spec"].get("valuesFrom", []):
                if not reference.get("optional", False):
                    consumers.append((name, namespace, resource_name, reference))
            password_secret = (
                resource["spec"].get("values", {}).get("addon", {}).get("passwordSecret")
            )
            if password_secret:
                consumers.append(
                    (name, namespace, resource_name, {"kind": "Secret", "name": password_secret})
                )
            chart = resource["spec"]["chart"]["spec"]
            source = chart["sourceRef"]["name"]
            if source == addon.spec.source_name:
                if not (inside(root, chart["chart"]) / "Chart.yaml").is_file():
                    raise ContractError("addon chart does not exist")
            elif source != "k8s-stack" or chart["chart"].removeprefix("./") not in (
                addon.spec.prerequisites.base_charts
            ):
                raise ContractError("undeclared chart source or Base prerequisite")

    def ancestors(name: str) -> set[str]:
        result = set(packages[name].depends_on)
        for dependency in packages[name].depends_on:
            result.update(ancestors(dependency))
        return result

    for (_, namespace, _), (package, release) in generated.items():
        if (package, namespace, release) not in helm_releases:
            raise ContractError("missing namespace-local Helm output producer")
    for consumer, namespace, release, reference in consumers:
        key = (reference["kind"], namespace, reference["name"])
        if key not in outputs:
            raise ContractError("missing values or secret producer")
        producer = outputs[key]
        if producer is not None and producer != consumer and producer not in ancestors(consumer):
            raise ContractError("missing producer readiness dependency")
        if key in generated and producer == consumer:
            producer_release = generated[key][1]
            if release == producer_release:
                raise ContractError("HelmRelease cannot consume its own generated output")
            dependencies = helm_releases[(consumer, namespace, release)]["spec"].get(
                "dependsOn", []
            )
            if not any(
                item["name"] == producer_release and item.get("namespace", namespace) == namespace
                for item in dependencies
            ):
                raise ContractError("missing Helm output readiness dependency")
    check_consumers(addon, rendered_packages)
    check_browser_bindings(addon, rendered_packages)
    if base is not None:
        check_base(addon, base)
    return addon


def check_compatibility(addon: Addon, *, installable: bool) -> None:
    """Enforce the clean baseline and the exact validator SDK for installation."""
    prerequisites = addon.spec.prerequisites
    for value, floor in (
        (prerequisites.base, (0, 4, 0)),
        (prerequisites.tooling, (0, 14, 0)),
        (prerequisites.stack_setup, (0, 7, 0)),
    ):
        if tuple(map(int, value.split("."))) < floor:
            raise ContractError("declared prerequisites predate the supported standalone baseline")
    if installable:
        try:
            installed = distribution_version("k8s-stack-tooling")
        except PackageNotFoundError:
            raise ContractError(
                "installed SDK metadata is required for installable validation"
            ) from None
        if prerequisites.tooling != installed:
            raise ContractError("declared SDK does not match the installed SDK version")


def check_artifacts(addon: Addon, *, installable: bool) -> None:
    """Validate declared immutable image references; publication is verified at release time."""
    for artifact in addon.spec.artifacts:
        match = re.fullmatch(
            r"[a-z0-9][a-z0-9./_-]*:(?:[a-z][a-z0-9]*-)?v?[0-9]+(?:\.[0-9]+){1,3}"
            r"(?:[-._][A-Za-z0-9][A-Za-z0-9._-]*)?(?:@sha256:([a-f0-9]{64}))?",
            artifact.image,
        )
        if (
            not match
            or (installable and match[1] is None)
            or (match[1] and len(set(match[1])) == 1)
        ):
            raise ContractError(
                "artifact requires an exact version and a non-placeholder digest when installable"
            )


def pod_containers(resource: dict):
    """Yield ordinary Pod container slots, including Job/CronJob templates."""
    kind = resource["kind"]
    if kind not in (
        "Pod",
        "Deployment",
        "StatefulSet",
        "Job",
        "CronJob",
        "DaemonSet",
        "ReplicaSet",
        "ReplicationController",
    ):
        return
    spec = resource["spec"]
    if kind == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    if kind != "Pod":
        spec = spec["template"]["spec"]
    for location in ("containers", "initContainers"):
        for container in spec.get(location, []):
            yield location, container


def check_consumers(
    addon: Addon,
    packages: dict[str, list[dict]],
    *,
    helm_resources: dict[tuple[str, str, str], list[dict]] | None = None,
) -> None:
    """Check images, including hooks/init containers in complete effective Helm renders.

    Render keys are (package, HelmRelease namespace, HelmRelease name). Supplying
    renders requires every selected HelmRelease, including settings-only charts.
    Without renders this checks declarations/direct workloads, not effective Helm images.
    """
    images = {artifact.id: artifact.image for artifact in addon.spec.artifacts}
    declarations = {}
    for consumer in addon.spec.consumers:
        if isinstance(consumer, CommandConsumer):
            continue
        key = (
            consumer.package,
            consumer.kind,
            consumer.namespace,
            consumer.name,
            consumer.location,
            consumer.container,
            consumer.template_key,
        )
        if key in declarations:
            raise ContractError("duplicate image consumer")
        declarations[key] = consumer
    found = set()
    releases = set()

    def check_container(prefix, location, container, template_key=None):
        key = (*prefix, location, container["name"], template_key)
        consumer = declarations.get(key)
        if consumer is None or container.get("image") != images[consumer.artifact]:
            raise ContractError("effective container image lacks a matching declaration")
        found.add(key)

    def check_job_templates(package, resource):
        if resource.get("kind") != "ConfigMap":
            return
        metadata = resource["metadata"]
        prefix = (package, "JobTemplate", metadata.get("namespace", ""), metadata["name"])
        keys = {key[-1] for key in declarations if key[:4] == prefix}
        if "job-template.json" in resource.get("data", {}) and "job-template.json" not in keys:
            raise ContractError("inert Job template requires explicit image consumers")
        for key in keys:
            text = resource.get("data", {}).get(key)
            if text is None:
                continue
            try:
                if not isinstance(text, str) or len(text.encode()) > MAX_DOCUMENT_BYTES:
                    raise ContractError("Job template exceeds its input bound")
                job = json.loads(text, object_pairs_hook=_unique_json)
                if (
                    not isinstance(job, dict)
                    or job.get("apiVersion") != "batch/v1"
                    or job.get("kind") != "Job"
                    or job.get("metadata", {}).get("namespace", prefix[2]) != prefix[2]
                ):
                    raise ContractError("expected a namespace-local inert Job object")
                for location, container in pod_containers(job):
                    check_container(prefix, location, container, key)
            except (ValueError, KeyError, TypeError, RecursionError, AttributeError):
                raise ContractError("invalid inert Job template or image declaration") from None

    for package, resources in packages.items():
        for resource in resources:
            kind, namespace, name = identity(resource)[1:]
            if kind == "HelmRelease":
                release = (package, namespace, name)
                releases.add(release)
                if helm_resources is None:
                    found.update(
                        key for key in declarations if key[:4] == (package, kind, namespace, name)
                    )
                else:
                    if release not in helm_resources:
                        raise ContractError("effective Helm render is missing")
                    for rendered in helm_resources[release]:
                        check_job_templates(package, rendered)
                        for location, container in pod_containers(rendered):
                            check_container((package, kind, namespace, name), location, container)
                continue
            check_job_templates(package, resource)
            for location, container in pod_containers(resource):
                check_container((package, kind, namespace, name), location, container)
    if helm_resources is not None:
        if releases != helm_resources.keys():
            raise ContractError("effective Helm renders do not match selected releases")
        for package in addon.spec.packages:
            for output in package.helm_outputs:
                objects = helm_resources[(package.name, output.namespace, output.helm_release)]
                if ("", output.kind, output.namespace, output.name) not in {
                    claim for resource in objects for claim in resource_claims(resource)
                }:
                    raise ContractError("declared Helm output is absent from the effective render")
    if not {key for key, consumer in declarations.items() if not consumer.optional} <= found:
        raise ContractError("declared image consumer is missing")


def check_browser_bindings(addon: Addon, packages: dict[str, list[dict]]) -> None:
    """Require settings and trusted approval to feed the existing Base OIDC consumer."""
    identities = [
        *addon.spec.identities,
        *(i for recipe in addon.spec.instance_recipes for i in recipe.identities),
    ]
    for identity_request in identities:
        binding = identity_request.browser_settings
        if binding is None:
            continue
        matched = False
        for resources in packages.values():
            for resource in resources:
                if (
                    resource.get("kind") != "HelmRelease"
                    or resource["metadata"].get("namespace") != binding.namespace
                ):
                    continue
                spec = resource["spec"]
                chart = spec.get("chart", {}).get("spec", {})
                if chart.get("chart", "").removeprefix("./") != "charts/keycloak/addon-oidc":
                    continue
                refs = {
                    r["name"]
                    for r in spec.get("valuesFrom", [])
                    if r.get("kind") == "ConfigMap" and not r.get("optional", False)
                }
                if {binding.settings, binding.approval} <= refs:
                    matched = True
        if not matched:
            raise ContractError(
                "browser settings and client approval must feed the Base OIDC consumer"
            )


def check_database_claims(addons: list[Addon]) -> None:
    """Check declared ownership; source composition additionally binds actual endpoints."""
    databases = set()
    roles = {}
    endpoints = {}
    for addon in addons:
        for claim in addon.spec.databases:
            expected_chart = {
                "postgresql": "charts/postgres/addon",
                "documentdb": "charts/postgres/documentdb-addon",
            }[claim.engine]
            if claim.base_chart != expected_chart:
                raise ContractError("database engine does not match its provisioning interface")
            service = (claim.service.namespace, claim.service.name)
            # Base exposes PostgreSQL and DocumentDB on distinct ports of one Service.
            endpoint = (*service, claim.engine)
            previous = endpoints.get(endpoint)
            if previous and (previous.exclusive or claim.exclusive):
                raise ContractError("database endpoint has exclusive consumers")
            endpoints[endpoint] = claim
            key = (*endpoint, claim.database)
            # DocumentDB provisioning also owns a PostgreSQL role on this Service.
            role = (*service, claim.role)
            owner = (addon.metadata.name, claim.owner)
            if key in databases or (role in roles and roles[role] != owner):
                raise ContractError("database or role has conflicting ownership")
            databases.add(key)
            roles[role] = owner


def check_secret_deliveries(
    addon: Addon,
    objects: list[dict],
    base_resources: list[dict],
    *,
    client_resources: list[dict] | None = None,
) -> None:
    """Check active ESO definitions against own or approved Base/client stores."""
    try:
        _check_secret_deliveries(addon, objects, base_resources, client_resources or [])
    except (KeyError, TypeError, AttributeError):
        raise ContractError("invalid effective delivery or Base store definition") from None


def _check_secret_deliveries(
    addon: Addon, objects: list[dict], base_resources: list[dict], client_resources: list[dict]
) -> None:
    """Match actual ESO deliveries to active definitions and verified Base-only stores.

    Inline target templates are not interpreted. Only explicit current-record field
    reads through namespace-local Kubernetes-authenticated KV-v2 stores are supported.
    """
    setup = addon.spec.setup
    if any(
        record.when or any(field.when for field in record.fields.values())
        for record in setup.records
    ) or any(delivery.when for delivery in setup.deliveries):
        raise ContractError("secret delivery checks require active expanded setup")
    records = {record.id: record for record in setup.records}
    paths = {record.path: record for record in setup.records}
    readers = {reader.role: reader for reader in setup.readers}
    stores = {}
    accounts = set()
    declared = {(item.kind, item.namespace, item.name): item.owner for item in addon.spec.inputs}
    owners = {}
    for owner, resources in (
        ("base", base_resources),
        ("client", client_resources),
        ("addon", objects),
    ):
        for resource in resources:
            kind = resource["kind"]
            if kind not in ("SecretStore", "ServiceAccount"):
                continue
            api = resource.get("apiVersion", "")
            if (kind == "SecretStore" and not api.startswith("external-secrets.io/")) or (
                kind == "ServiceAccount" and api != "v1"
            ):
                raise ContractError("store or ServiceAccount has an unsupported API identity")
            metadata = resource["metadata"]
            key = (metadata.get("namespace", ""), metadata["name"])
            identity_key = (kind, *key)
            expected = declared.get(identity_key)
            if owner == "client" and expected != "client":
                continue
            if expected is not None and expected != owner:
                raise ContractError("store or ServiceAccount differs from its declared owner")
            if identity_key in owners and owners[identity_key] != owner:
                raise ContractError("store or ServiceAccount has overlapping owners")
            owners[identity_key] = owner
            if kind == "SecretStore":
                if key in stores and stores[key].get("spec") != resource.get("spec"):
                    raise ContractError("SecretStore binding differs between rendered phases")
                stores[key] = resource
            else:
                accounts.add(key)
    deliveries = {(delivery.namespace, delivery.name): delivery for delivery in setup.deliveries}
    if len(deliveries) != len(setup.deliveries):
        raise ContractError("duplicate active delivery identity")
    found = {}
    delivered_fields = {
        record_id: set() for delivery in setup.deliveries for record_id in delivery.records
    }
    for resource in objects:
        if resource.get("kind") != "ExternalSecret":
            continue
        if not resource.get("apiVersion", "").startswith("external-secrets.io/"):
            raise ContractError("ExternalSecret has an unsupported API identity")
        metadata, spec = resource["metadata"], resource["spec"]
        key = (metadata.get("namespace", ""), metadata["name"])
        delivery = deliveries.get(key)
        if delivery is None:
            raise ContractError("rendered ExternalSecret is not an active declared delivery")
        target = spec.get("target", {})
        store_ref = spec.get("secretStoreRef", {})
        if (
            target.get("name", key[1]) != delivery.target_secret
            or store_ref.get("kind", "SecretStore") != "SecretStore"
            or store_ref.get("name") != delivery.secret_store
        ):
            raise ContractError("rendered delivery target or store differs from active definition")
        if spec.get("dataFrom") or target.get("template", {}).get("templateFrom"):
            raise ContractError(
                "dataFrom and external target templates are unsupported for active field proof"
            )
        store = stores.get((delivery.namespace, delivery.secret_store))
        if store is None:
            raise ContractError("delivery requires an owned or declared Base/client SecretStore")
        provider = store["spec"].get("provider", {})
        vault = provider.get("vault", {})
        auth = vault.get("auth", {})
        kubernetes = auth.get("kubernetes", {})
        account = kubernetes.get("serviceAccountRef", {})
        reader = readers.get(kubernetes.get("role"))
        if (
            set(provider) != {"vault"}
            or vault.get("path") != "secret"
            or vault.get("version") != "v2"
            or vault.get("namespace")
            or set(auth) != {"kubernetes"}
            or set(kubernetes) - {"mountPath", "role", "serviceAccountRef"}
            or kubernetes.get("mountPath") != "kubernetes"
            or set(account) - {"name", "namespace", "audiences"}
            or reader is None
            or reader.namespace != delivery.namespace
            or account.get("namespace", delivery.namespace) != delivery.namespace
            or account.get("name") != reader.service_account
            or account.get("audiences") != [reader.audience]
            or (delivery.namespace, reader.service_account) not in accounts
            or not set(delivery.records) <= set(reader.records)
        ):
            raise ContractError("SecretStore binding does not match the approved reader")
        references = []
        secret_keys = set()
        used_records = set()
        for entry in spec.get("data", []):
            remote = entry.get("remoteRef", {})
            record = paths.get(remote.get("key"))
            field = remote.get("property")
            secret_key = entry.get("secretKey")
            if (
                set(entry) != {"secretKey", "remoteRef"}
                or set(remote)
                - {"key", "property", "decodingStrategy", "conversionStrategy", "metadataPolicy"}
                or remote.get("decodingStrategy", "None") != "None"
                or remote.get("conversionStrategy", "Default") != "Default"
                or remote.get("metadataPolicy", "None") != "None"
                or record is None
                or record.id not in delivery.records
                or field not in record.fields
                or not isinstance(secret_key, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,253}", secret_key)
                or secret_key in secret_keys
            ):
                raise ContractError(
                    "ExternalSecret field read is not an allowed active record field"
                )
            secret_keys.add(secret_key)
            used_records.add(record.id)
            delivered_fields[record.id].add(field)
            references.append((record.path, field, secret_key))
        if used_records != set(delivery.records):
            raise ContractError("rendered delivery omits an active declared record")
        signature = tuple(sorted(references))
        if key in found and found[key] != signature:
            raise ContractError("delivery fields differ between install and upgrade")
        found[key] = signature
    if found.keys() != deliveries.keys():
        raise ContractError("active declared delivery is missing from effective resources")
    for record_id, fields in delivered_fields.items():
        if fields != records[record_id].fields.keys():
            raise ContractError("effective deliveries omit active credential fields")


def check_verifier_reader_separation(addons: list[Addon]) -> None:
    """Composed reader roles may not combine raw key and verifier record access."""
    raw, verifiers, roles = set(), set(), {}
    for addon in addons:
        setup = addon.spec.setup
        records = {record.id: record for record in setup.records}
        for record in setup.records:
            for field in record.fields.values():
                if field.sha256 is not None:
                    raw.add(records[field.sha256.record].path)
                    verifiers.add(record.path)
        for invariant in setup.invariants:
            raw.add(records[invariant.source.record].path)
            verifiers.update(
                records[ref.record].path for ref in (invariant.primary, invariant.secondary)
            )
        for reader in setup.readers:
            roles.setdefault(reader.role, set()).update(
                records[name].path for name in reader.records
            )
    if any(paths & raw and paths & verifiers for paths in roles.values()):
        raise ContractError("composed verifier reader can access raw key records")


def check_base(addon: Addon, base: Path, revision: str | None = None) -> None:
    """Check chart prerequisites in the candidate tree or a resolved local Git commit."""
    base = base.resolve()

    def document(path: str):
        local = inside(base, path)
        if revision is None:
            return load(local)
        try:
            content = run_git(base, "show", f"{revision}:{path}", strip=False)
        except ContractError:
            raise ContractError("required Base file is missing at the selected revision")
        try:
            return parse_yaml(content.encode())
        except yaml.YAMLError:
            raise ContractError("invalid Base prerequisite YAML") from None

    prerequisites = addon.spec.prerequisites
    if document("release/manifest.yaml")["spec"]["version"] != prerequisites.base:
        raise ContractError("Base revision is not the reviewed prerequisite")
    for path in prerequisites.base_charts:
        chart = document(f"{path}/Chart.yaml")
        if path in prerequisites.base_chart_versions and (
            not isinstance(chart, dict)
            or chart.get("version") != prerequisites.base_chart_versions[path]
        ):
            raise ContractError("Base chart version is not the reviewed prerequisite")


def base_revision(base: Path, ref: dict) -> str:
    """Resolve the selected Flux ref using existing local refs only."""
    if len(ref) != 1:
        raise ContractError("Base source must select one tag, commit or branch")
    if "tag" in ref:
        candidates = ["refs/tags/" + ref["tag"]]
    elif "branch" in ref:
        candidates = ["refs/remotes/origin/" + ref["branch"], "refs/heads/" + ref["branch"]]
    elif "commit" in ref and re.fullmatch(r"[a-f0-9]{40}", ref["commit"]):
        candidates = [ref["commit"]]
    else:
        raise ContractError("Base source must select one tag, commit or branch")
    for candidate in candidates:
        if "commit" not in ref:
            try:
                run_git(base, "check-ref-format", candidate)
            except ContractError:
                raise ContractError("invalid Base source branch or tag ref") from None
        try:
            revision = run_git(
                base,
                "rev-parse",
                "--verify",
                "--end-of-options",
                candidate + "^{commit}",
            )
        except ContractError:
            continue
        if re.fullmatch(r"[a-f0-9]{40}", revision):
            return revision
    raise ContractError("selected Base source revision cannot be resolved locally")


CLIENT_SOURCE = ("GitRepository", "flux-system", "flux-system")


def stage_source(stage: dict) -> tuple[str, str, str]:
    ref = stage["spec"]["sourceRef"]
    return (
        ref.get("kind", "GitRepository"),
        ref.get("namespace", stage["metadata"].get("namespace", "")),
        ref["name"],
    )


def render_addon_packages(
    addon: Addon, root: Path, stages: list[dict], source: SourceRef
) -> dict[str, list[dict]]:
    selected = [
        stage
        for stage in stages
        if stage_source(stage) == (source.kind, source.namespace, source.name)
    ]
    by_path = {stage["spec"]["path"].removeprefix("./"): stage for stage in selected}
    if len(by_path) != len(selected) or set(by_path) != {
        package.path.removeprefix("./") for package in addon.spec.packages
    }:
        raise ContractError("client must select every addon package exactly once")
    return {
        package.name: render_stage(by_path[package.path.removeprefix("./")], root)
        for package in addon.spec.packages
    }


def check_client(
    addons: list[tuple[Path, Addon]],
    client: Path,
    *,
    installable: bool = False,
    base: Path | None = None,
    cluster_dir: str = "clusters/prod-eu-1",
    base_candidate: Path | None = None,
) -> None:
    """Require enabled packages and their dependency edges in the effective client graph."""
    client = client.resolve()
    if base_candidate is not None and (
        installable or base is None or base_candidate.resolve() != base.resolve()
    ):
        raise ContractError(
            "working Base candidate requires candidate mode and the explicit Base checkout"
        )
    resources = render(inside(client, cluster_dir), root=client)
    sources = {
        (r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
        for r in resources
        if r["kind"] == "GitRepository"
    }
    stages = {
        (r["metadata"].get("namespace", ""), r["metadata"]["name"]): r
        for r in resources
        if r["kind"] == "Kustomization"
    }
    selection = load_selection(client / "config/addons.yaml")
    selected = {item.id: item for item in selection.addons}
    if {addon.metadata.name for _, addon in addons} != set(selected) or len(addons) != len(
        selected
    ):
        raise ContractError("validation requires the complete selection")
    allowed_sources = {
        (entry.source_ref.kind, entry.source_ref.namespace, entry.source_ref.name)
        for entry in selection.addons
    }
    allowed_sources.add(
        (
            selection.base_source_ref.kind,
            selection.base_source_ref.namespace,
            selection.base_source_ref.name,
        )
    )
    for stage in stages.values():
        source_identity = stage_source(stage)
        if source_identity[0] != "GitRepository":
            raise ContractError("unsupported deployment source kind")
        if source_identity != CLIENT_SOURCE and source_identity not in allowed_sources:
            raise ContractError("deployment source is absent from complete selection")
    external = {"client": list(resources), "base": []}
    for stage in stages.values():
        if stage_source(stage) != CLIENT_SOURCE:
            continue
        if inside(client, stage["spec"]["path"]) == inside(client, cluster_dir):
            continue  # Flux bootstrap may point at its own already-rendered cluster directory.
        external["client"].extend(render_stage(stage, client))
    base_ref = selection.base_source_ref
    base_source = (base_ref.kind, base_ref.namespace, base_ref.name)
    base_stages = [stage for stage in stages.values() if stage_source(stage) == base_source]
    if base_stages and base is None:
        raise ContractError("selected Base validation requires --base with --client")
    if base is not None:
        for stage in base_stages:
            external["base"].extend(render_stage(stage, base))
    claimed: set[tuple[str, str, str, str]] = set()
    external_values = {"client": set(), "base": set()}
    for owner, documents in external.items():
        for resource in documents:
            claims = resource_claims(resource)
            if claimed & claims:
                raise ContractError("Base and client resources have overlapping ownership")
            claimed.update(claims)
            external_values[owner].update(
                key[1:] for key in claims if key[0] == "" or key[1] == "SecretStore"
            )
    authentication_clients: set[str] = set()
    for root, addon in addons:
        entry = selected[addon.metadata.name]
        source_key = (entry.source_ref.namespace, entry.source_ref.name)
        source_name = addon.spec.source_name
        selected_stages = {
            name: stage
            for name, stage in stages.items()
            if stage_source(stage) == (entry.source_ref.kind, *source_key)
        }
        if source_name != entry.source_ref.name or source_key not in sources:
            raise ContractError("selected addon requires its namespaced source")
        if addon.spec.authentication is not None:
            client_id = addon.spec.authentication.client_id
            if client_id in authentication_clients:
                raise ContractError("selected addons have duplicate authentication client IDs")
            authentication_clients.add(client_id)
        for item in addon.spec.inputs:
            if (item.kind, item.namespace, item.name) not in external_values[item.owner]:
                raise ContractError(
                    "required Base or client values are missing from their declared owner"
                )
        ref = sources[source_key]["spec"]["ref"]
        if set(ref) != {"commit"} or not re.fullmatch(r"[a-f0-9]{40}", ref["commit"]):
            raise ContractError("addon source must select an exact commit")
        base_ref = selection.base_source_ref
        platform_ref = (
            sources.get((base_ref.namespace, base_ref.name), {}).get("spec", {}).get("ref", {})
        )
        if (
            base_candidate is None
            and "tag" in platform_ref
            and platform_ref["tag"] != "v" + addon.spec.prerequisites.base
        ):
            raise ContractError("client Base release is not the reviewed prerequisite")
        if base is None:
            raise ContractError("selected Base validation requires --base with --client")
        check_base(
            addon, base, None if base_candidate is not None else base_revision(base, platform_ref)
        )
        if installable:
            revision = run_git(root, "rev-parse", "HEAD")
            dirty = run_git(root, "status", "--porcelain")
            if revision != ref["commit"] or dirty:
                raise ContractError("addon checkout does not match the client's exact revision")
        by_path = {s["spec"]["path"].removeprefix("./"): n for n, s in selected_stages.items()}
        if len(by_path) != len(selected_stages) or set(by_path) != {
            p.path for p in addon.spec.packages
        }:
            raise ContractError("client must select every addon package exactly once")
        paths = {p.name: p.path for p in addon.spec.packages}
        effective_packages = render_addon_packages(
            addon, root, list(stages.values()), entry.source_ref
        )
        check(root, installable=installable, effective_packages=effective_packages)
        for package in addon.spec.packages:
            stage = selected_stages[by_path[package.path]]
            dependencies = {
                (d.get("namespace", stage["metadata"].get("namespace", "")), d["name"])
                for d in stage["spec"].get("dependsOn", [])
            }
            if not {by_path[paths[d]] for d in package.depends_on} <= dependencies:
                raise ContractError("client is missing an addon readiness dependency")
            if not stage["spec"].get("wait") and not stage["spec"].get("healthChecks"):
                raise ContractError("addon stage must check readiness")
            for resource in effective_packages[package.name]:
                claims = resource_claims(resource)
                if claimed & claims:
                    raise ContractError(
                        "selected resources or generated Secrets have overlapping ownership"
                    )
                claimed.update(claims)
            for output in package.helm_outputs:
                resource_id = ("", output.kind, output.namespace, output.name)
                if resource_id in claimed:
                    raise ContractError("selected addons have overlapping ownership")
                claimed.add(resource_id)


def main() -> None:
    """Run local validation; never access a cluster or retrieve credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--client", type=Path)
    parser.add_argument("--installable", action="store_true")
    parser.add_argument("--cluster-dir", required=False)
    parser.add_argument("--base-candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.base_candidate is not None and (args.installable or args.client is None):
            raise ContractError(
                "working Base candidate requires complete candidate client validation"
            )
        addons = [
            (
                root,
                load_manifest(root / "release/manifest.yaml")
                if args.client
                else check(root, installable=args.installable, base=args.base),
            )
            for root in args.roots
        ]
        if args.client:
            from .sources import verify_selection

            if args.base is None or args.cluster_dir is None:
                raise ContractError("client validation requires --base and --cluster-dir")
            roots = {addon.metadata.name: root for root, addon in addons}
            if len(roots) != len(addons):
                raise ContractError("duplicate supplied add-on identity")
            verify_selection(
                args.client,
                roots,
                base=args.base,
                cluster_dir=args.cluster_dir,
                installable=args.installable,
                base_candidate=args.base_candidate,
            )
    except (ContractError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ContractError) else "invalid addon inputs"
        parser.exit(1, f"Addon check failed: {message}\n")
    print(f"Validated {len(addons)} addon release contract(s).")
