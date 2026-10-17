"""Validate addon release packages and their selected client dependencies offline."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from k8s_stack_tooling.keycloak.scoped_oidc import RESERVED_CLIENT_IDS


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Prerequisites(Contract):
    base: str
    tooling: str
    stack_setup: str = Field(alias="stackSetup")
    base_charts: list[str] = Field(alias="baseCharts")

    @field_validator("base", "tooling", "stack_setup")
    @classmethod
    def version(cls, value: str) -> str:
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise ValueError("expected an exact release version")
        return value


class HelmOutput(Contract):
    """A ConfigMap produced by a package's namespace-local HelmRelease."""

    kind: Literal["ConfigMap"]
    namespace: str
    name: str
    helm_release: str = Field(alias="helmRelease")


class Package(Contract):
    name: str
    path: str
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")
    helm_outputs: list[HelmOutput] = Field(default_factory=list, alias="helmOutputs")


class Retention(Contract):
    databases: Literal["retain"]
    volumes: Literal["retain"]
    credentials: Literal["retain"]


class Input(Contract):
    kind: Literal["ConfigMap", "Secret"]
    namespace: str
    name: str
    owner: Literal["base", "client"]


class Authentication(Contract):
    client_id: str = Field(alias="clientId", pattern=r"^[a-z][a-z0-9-]*$")
    redirect_uris: list[str] = Field(alias="redirectUris", min_length=1)
    web_origins: list[str] = Field(alias="webOrigins", min_length=1)
    realm_roles: list[str] = Field(alias="realmRoles", min_length=1)
    role_claim: str = Field(alias="roleClaim", pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
    audiences: list[str] = Field(default_factory=list)
    refresh_token_rotation: bool = Field(default=True, alias="refreshTokenRotation")
    mode: Literal["login-only", "login+gateway"] = "login-only"
    user_token_forwarding: bool = Field(default=False, alias="userTokenForwarding")
    gateway_permissions: list[str] = Field(default_factory=list, alias="gatewayPermissions")

    @field_validator("client_id")
    @classmethod
    def addon_client(cls, value: str) -> str:
        if value in RESERVED_CLIENT_IDS:
            raise ValueError("reserved authentication client ID")
        return value

    @field_validator(
        "redirect_uris", "web_origins", "realm_roles", "audiences", "gateway_permissions"
    )
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("authentication lists must be unique")
        return value

    @model_validator(mode="after")
    def authentication_contract(self):
        for values, origin in ((self.redirect_uris, False), (self.web_origins, True)):
            for value in values:
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or any(c.isspace() or c in "*\\" for c in value)
                    or (origin and (parsed.path or parsed.query))
                ):
                    raise ValueError("authentication requires exact HTTPS URLs")
                _ = parsed.port
        for values, pattern in (
            (self.realm_roles, r"[a-z][a-z0-9:-]*"),
            (self.audiences, r"[a-z][a-z0-9-]*"),
            (self.gateway_permissions, r"[a-z][A-Za-z0-9:._/-]{0,255}"),
        ):
            if any(not re.fullmatch(pattern, value) for value in values):
                raise ValueError("invalid authentication scope")
        if self.role_claim.split(".")[0] in {
            "sub",
            "aud",
            "azp",
            "iss",
            "exp",
            "iat",
            "nbf",
            "resource_access",
        }:
            raise ValueError("reserved authentication claim")
        if self.mode == "login-only":
            if self.gateway_permissions or "agentgateway" in self.audiences:
                raise ValueError("login-only cannot request gateway access")
        elif not self.user_token_forwarding or "agentgateway" not in self.audiences:
            raise ValueError("gateway mode requires user-token forwarding and audience")
        return self


class Spec(Contract):
    version: str
    status: Literal["staged", "ready"]
    source_name: str = Field(alias="sourceName")
    configuration_key: str = Field(alias="configurationKey")
    prerequisites: Prerequisites
    packages: list[Package]
    inputs: list[Input] = Field(default_factory=list)
    retention: Retention
    authentication: Authentication | None = None


class Metadata(Contract):
    name: str


class Addon(Contract):
    api_version: Literal["addons.neurwerk.com/v1alpha1"] = Field(alias="apiVersion")
    kind: Literal["AddonRelease"]
    metadata: Metadata
    spec: Spec


class ContractError(ValueError):
    """An invalid release or client composition, without supplied values in errors."""


def load(path: Path):
    """Read a YAML document without including input snippets in errors."""
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError):
        raise ContractError("cannot read contract YAML") from None


def inside(root: Path, path: str) -> Path:
    """Reject absolute paths, traversal and symlinks outside a repository."""
    result = (root / path).resolve()
    if Path(path).is_absolute() or ".." in Path(path).parts or not result.is_relative_to(root):
        raise ContractError("package path leaves its repository")
    return result


def render(path: Path) -> list[dict]:
    """Build Kubernetes objects without showing configuration or Secret values."""
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


def identity(resource: dict) -> tuple[str, str, str, str]:
    """Compare resource identities independently of API version."""
    metadata = resource["metadata"]
    api = resource["apiVersion"]
    group = api.split("/")[0] if "/" in api else ""
    return group, resource["kind"], metadata.get("namespace", ""), metadata["name"]


def check(root: Path, *, installable: bool = False, base: Path | None = None) -> Addon:
    """Check the release graph, resource ownership and required Base charts."""
    root = root.resolve()
    try:
        addon = Addon.model_validate(load(root / "release/manifest.yaml"))
    except ValidationError:
        raise ContractError("invalid addon release contract") from None
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
    for name, package in packages.items():
        for output in package.helm_outputs:
            key = (output.kind, output.namespace, output.name)
            if key in outputs:
                raise ContractError("duplicate input or generated producer")
            outputs[key] = name
            generated[key] = (name, output.helm_release)
    helm_releases: dict[tuple[str, str, str], dict] = {}
    consumers: list[tuple[str, str, str, dict]] = []
    for name, package in packages.items():
        visit(name)
        resources = render(inside(root, package.path))
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
            if installable and source == addon.spec.source_name:
                chart_values = load(inside(root, chart["chart"]) / "values.yaml") or {}
                image = chart_values.get("k8sTools", {}).get("image")
                if image and not re.fullmatch(
                    r"ghcr.io/neurwerk/k8s-stack-tooling:"
                    + re.escape(addon.spec.prerequisites.tooling)
                    + r"@sha256:[a-f0-9]{64}",
                    image,
                ):
                    raise ContractError("Tooling image does not match the release prerequisite")

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
    if base is not None:
        base = base.resolve()
        manifest = load(base / "release/manifest.yaml")
        if manifest["spec"]["version"] != addon.spec.prerequisites.base:
            raise ContractError("Base revision is not the reviewed prerequisite")
        for path in addon.spec.prerequisites.base_charts:
            if not (inside(base, path) / "Chart.yaml").is_file():
                raise ContractError("required Base chart is missing")
    return addon


def check_client(
    addons: list[tuple[Path, Addon]], client: Path, *, installable: bool = False
) -> None:
    """Require enabled packages and their dependency edges in the effective client graph."""
    resources = render(client.resolve() / "clusters/prod-eu-1")
    sources = {r["metadata"]["name"]: r for r in resources if r["kind"] == "GitRepository"}
    stages = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Kustomization"}
    values = load(client / "config/client.yaml")
    value_paths = {
        stage["spec"]["path"]
        for stage in stages.values()
        if stage["spec"]["sourceRef"]["name"] == "flux-system"
    }
    client_values = {
        identity(resource)[1:]
        for path in value_paths
        for resource in render(inside(client.resolve(), path))
    }
    claimed: set[tuple[str, str, str, str]] = set()
    authentication_clients: set[str] = set()
    for root, addon in addons:
        key = addon.spec.configuration_key
        selected = values.get(key, {})
        enabled = selected.get("enabled", False)
        if type(enabled) is not bool:
            raise ContractError("addon selection must be a boolean")
        source_name = addon.spec.source_name
        selected_stages = {
            name: stage
            for name, stage in stages.items()
            if stage["spec"]["sourceRef"]["name"] == source_name
        }
        if not enabled:
            if selected_stages:
                raise ContractError("disabled addon still has selected deployment packages")
            continue
        if not selected.get("hostname") or source_name not in sources:
            raise ContractError("enabled addon requires a hostname and source")
        if addon.spec.authentication is not None:
            client_id = addon.spec.authentication.client_id
            if client_id in authentication_clients:
                raise ContractError("selected addons have duplicate authentication client IDs")
            authentication_clients.add(client_id)
        for item in addon.spec.inputs:
            if (
                item.owner == "client"
                and (item.kind, item.namespace, item.name) not in client_values
            ):
                raise ContractError("required client values are missing")
        ref = sources[source_name]["spec"]["ref"]
        if set(ref) != {"commit"} or not re.fullmatch(r"[a-f0-9]{40}", ref["commit"]):
            raise ContractError("addon source must select an exact commit")
        platform_ref = sources.get("k8s-stack", {}).get("spec", {}).get("ref", {})
        if "tag" in platform_ref and platform_ref["tag"] != "v" + addon.spec.prerequisites.base:
            raise ContractError("client Base release is not the reviewed prerequisite")
        if installable:
            revision = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            if revision != ref["commit"] or dirty:
                raise ContractError("addon checkout does not match the client's exact revision")
        by_path = {s["spec"]["path"].removeprefix("./"): n for n, s in selected_stages.items()}
        if len(by_path) != len(selected_stages) or set(by_path) != {
            p.path for p in addon.spec.packages
        }:
            raise ContractError("client must select every addon package exactly once")
        paths = {p.name: p.path for p in addon.spec.packages}
        for package in addon.spec.packages:
            stage = selected_stages[by_path[package.path]]
            dependencies = {d["name"] for d in stage["spec"].get("dependsOn", [])}
            if not {by_path[paths[d]] for d in package.depends_on} <= dependencies:
                raise ContractError("client is missing an addon readiness dependency")
            if not stage["spec"].get("wait") and not stage["spec"].get("healthChecks"):
                raise ContractError("addon stage must check readiness")
            for resource in render(inside(root.resolve(), package.path)):
                resource_id = identity(resource)
                if resource_id in claimed:
                    raise ContractError("selected addons have overlapping ownership")
                claimed.add(resource_id)
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
    args = parser.parse_args()
    try:
        addons = [
            (root, check(root, installable=args.installable, base=args.base)) for root in args.roots
        ]
        if args.client:
            check_client(addons, args.client, installable=args.installable)
    except (ContractError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, ContractError) else "invalid addon inputs"
        parser.exit(1, f"Addon check failed: {message}\n")
    print(f"Validated {len(addons)} addon release contract(s).")
