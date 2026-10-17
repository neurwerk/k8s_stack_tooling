"""Exercise addon composition failures with synthetic packages."""

import copy
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from k8s_stack_tooling.addons import Authentication, ContractError, check, check_client


@pytest.fixture
def package(tmp_path: Path):
    root = tmp_path / "addon"
    (root / "release").mkdir(parents=True)
    manifest = {
        "apiVersion": "addons.neurwerk.com/v1alpha1",
        "kind": "AddonRelease",
        "metadata": {"name": "example"},
        "spec": {
            "version": "0.1.0",
            "status": "ready",
            "sourceName": "example",
            "configurationKey": "example",
            "prerequisites": {
                "base": "0.3.26",
                "tooling": "0.9.0",
                "stackSetup": "0.3.0",
                "baseCharts": ["charts/postgres/addon"],
            },
            "packages": [
                {"name": "secrets", "path": "releases/secrets"},
                {"name": "app", "path": "releases/app", "dependsOn": ["secrets"]},
            ],
            "retention": {"databases": "retain", "volumes": "retain", "credentials": "retain"},
        },
    }
    secret = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": "password", "namespace": "example"},
        "spec": {"target": {"name": "password"}},
    }
    app = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"name": "app", "namespace": "example"},
        "spec": {
            "chart": {
                "spec": {"chart": "./charts/postgres/addon", "sourceRef": {"name": "k8s-stack"}}
            },
            "valuesFrom": [{"kind": "Secret", "name": "password"}],
        },
    }
    documents = {"secrets": [secret], "app": [app]}

    def save():
        (root / "release/manifest.yaml").write_text(yaml.safe_dump(manifest))

    save()
    with patch("k8s_stack_tooling.addons.render", side_effect=lambda path: documents[path.name]):
        yield root, manifest, documents, save


def test_valid_package_and_staged_release_gate(package):
    root, manifest, _, save = package
    assert check(root, installable=True).metadata.name == "example"
    manifest["spec"]["status"] = "staged"
    save()
    check(root)
    with pytest.raises(ContractError, match="staged"):
        check(root, installable=True)


@pytest.mark.parametrize("path", ["../outside", "/outside"])
def test_path_escape_is_rejected(package, path):
    root, manifest, _, save = package
    manifest["spec"]["packages"][0]["path"] = path
    save()
    with pytest.raises(ContractError, match="leaves its repository"):
        check(root)


def test_symlink_escape_is_rejected(package, tmp_path):
    root, manifest, _, save = package
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    manifest["spec"]["packages"][0]["path"] = "link/secrets"
    save()
    with pytest.raises(ContractError, match="leaves its repository"):
        check(root)


def test_unknown_contract_field(package):
    root, manifest, _, save = package
    manifest["spec"]["unexpected"] = True
    save()
    with pytest.raises(ContractError, match="invalid addon release contract"):
        check(root)


def test_duplicate_resource_owner(package):
    root, _, documents, _ = package
    documents["app"].append(copy.deepcopy(documents["secrets"][0]))
    with pytest.raises(ContractError, match="multiple packages"):
        check(root)


def test_missing_secret_producer(package):
    root, _, documents, _ = package
    documents["app"][0]["spec"]["valuesFrom"][0]["name"] = "missing"
    with pytest.raises(ContractError, match="missing values or secret producer"):
        check(root)


def test_missing_readiness_dependency(package):
    root, manifest, _, save = package
    manifest["spec"]["packages"][1]["dependsOn"] = []
    save()
    with pytest.raises(ContractError, match="missing producer readiness dependency"):
        check(root)


@pytest.fixture
def helm_output(package):
    root, manifest, documents, save = package
    producer = copy.deepcopy(documents["app"][0])
    producer["metadata"]["name"] = "settings"
    producer["spec"]["valuesFrom"] = []
    documents["app"].append(producer)
    consumer = documents["app"][0]
    consumer["spec"]["valuesFrom"].append({"kind": "ConfigMap", "name": "settings-values"})
    consumer["spec"]["dependsOn"] = [{"name": "settings"}]
    manifest["spec"]["packages"][1]["helmOutputs"] = [
        {
            "kind": "ConfigMap",
            "namespace": "example",
            "name": "settings-values",
            "helmRelease": "settings",
        }
    ]
    save()
    return root, manifest, documents, save


def test_chart_generated_values_have_a_real_helm_producer(helm_output):
    root, _, documents, _ = helm_output
    check(root)
    documents["app"][0]["spec"]["dependsOn"][0]["namespace"] = "example"
    check(root)


@pytest.mark.parametrize("failure", ["missing", "namespace", "package", "dependency", "self"])
def test_chart_generated_values_reject_invalid_producer_ordering(helm_output, failure):
    root, manifest, documents, save = helm_output
    if failure == "missing":
        documents["app"].pop()
    elif failure == "namespace":
        documents["app"][-1]["metadata"]["namespace"] = "other"
    elif failure == "package":
        documents["secrets"].append(documents["app"].pop())
    elif failure == "dependency":
        documents["app"][0]["spec"]["dependsOn"][0]["namespace"] = "other"
    else:
        manifest["spec"]["packages"][1]["helmOutputs"][0]["helmRelease"] = "app"
    save()
    with pytest.raises(ContractError, match="Helm|own generated"):
        check(root)


@pytest.mark.parametrize("duplicate", ["input", "rendered", "declaration", "other-package"])
@pytest.mark.parametrize("reverse", [False, True])
def test_chart_generated_values_reject_conflicting_ownership(helm_output, duplicate, reverse):
    root, manifest, documents, save = helm_output
    output = manifest["spec"]["packages"][1]["helmOutputs"][0]
    if duplicate == "input":
        manifest["spec"]["inputs"] = [
            {
                "kind": "ConfigMap",
                "namespace": "example",
                "name": "settings-values",
                "owner": "base",
            }
        ]
    elif duplicate == "rendered":
        documents["secrets"].append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"namespace": "example", "name": "settings-values"},
            }
        )
    elif duplicate == "declaration":
        manifest["spec"]["packages"][1]["helmOutputs"].append(copy.deepcopy(output))
    else:
        manifest["spec"]["packages"][0]["helmOutputs"] = [copy.deepcopy(output)]
    if reverse:
        manifest["spec"]["packages"].reverse()
    save()
    with pytest.raises(ContractError, match="duplicate"):
        check(root)


def test_chart_generated_values_require_cross_package_readiness(helm_output):
    root, manifest, documents, save = helm_output
    documents["secrets"].append(documents["app"].pop())
    packages = manifest["spec"]["packages"]
    packages[0]["helmOutputs"] = packages[1].pop("helmOutputs")
    documents["app"][0]["spec"]["dependsOn"] = []
    save()
    check(root)
    packages[1]["dependsOn"] = []
    save()
    with pytest.raises(ContractError, match="missing producer readiness dependency"):
        check(root)


def test_cycle_and_undeclared_chart(package):
    root, manifest, documents, save = package
    manifest["spec"]["packages"][0]["dependsOn"] = ["app"]
    save()
    with pytest.raises(ContractError, match="cyclic"):
        check(root)
    manifest["spec"]["packages"][0]["dependsOn"] = []
    save()
    documents["app"][0]["spec"]["chart"]["spec"]["sourceRef"]["name"] = "other"
    with pytest.raises(ContractError, match="undeclared chart source"):
        check(root)


def test_incompatible_base(package, tmp_path):
    root, _, _, _ = package
    base = tmp_path / "base"
    (base / "release").mkdir(parents=True)
    (base / "release/manifest.yaml").write_text("spec:\n  version: 0.1.0\n")
    with pytest.raises(ContractError, match="reviewed prerequisite"):
        check(root, base=base)


def test_client_requires_dependency_and_single_owner(package, tmp_path):
    root, _, documents, _ = package
    addon = check(root)
    client = tmp_path / "client"
    (client / "config").mkdir(parents=True)
    (client / "config/client.yaml").write_text("example: {enabled: true, hostname: example.test}\n")
    source = {
        "kind": "GitRepository",
        "metadata": {"name": "example"},
        "spec": {"ref": {"commit": "a" * 40}},
    }

    def stage(name, dependency):
        return {
            "kind": "Kustomization",
            "metadata": {"name": name},
            "spec": {
                "path": f"./releases/{name}",
                "sourceRef": {"name": "example"},
                "wait": True,
                "dependsOn": [{"name": d} for d in dependency],
            },
        }

    resources: list[dict] = [source, stage("secrets", []), stage("app", ["secrets"])]

    def rendered(path):
        if path.name == "prod-eu-1":
            return resources
        if path.parent == root / "releases":
            return documents[path.name]
        return []

    with patch("k8s_stack_tooling.addons.render", side_effect=rendered):
        check_client([(root, addon)], client)
        authenticated = addon.model_copy(deep=True)
        authenticated.spec.authentication = Authentication.model_validate(authentication())
        with pytest.raises(ContractError, match="duplicate authentication client IDs"):
            check_client([(root, authenticated), (root, authenticated)], client)
        resources[-1]["spec"]["dependsOn"] = []
        with pytest.raises(ContractError, match="readiness dependency"):
            check_client([(root, addon)], client)
        resources[-1]["spec"]["dependsOn"] = [{"name": "secrets"}]
        with pytest.raises(ContractError, match="overlapping ownership"):
            check_client([(root, addon), (root, addon)], client)
        resources.append(
            {
                "kind": "GitRepository",
                "metadata": {"name": "k8s-stack"},
                "spec": {"ref": {"tag": "v0.1.0"}},
            }
        )
        with pytest.raises(ContractError, match="client Base release"):
            check_client([(root, addon)], client)


def authentication():
    return {
        "clientId": "example",
        "redirectUris": ["https://example.test/oauth/callback"],
        "webOrigins": ["https://example.test"],
        "realmRoles": ["example-user"],
        "roleClaim": "example_roles",
    }


@pytest.mark.parametrize(
    "client_id",
    [
        "agentgateway",
        "studio",
        "keycloak-api-key-bridge",
        "account",
        "account-console",
        "admin-cli",
        "broker",
        "realm-management",
        "security-admin-console",
    ],
)
def test_reserved_authentication_client(package, client_id):
    root, manifest, _, save = package
    manifest["spec"]["authentication"] = authentication() | {"clientId": client_id}
    save()
    with pytest.raises(ContractError, match="invalid addon release contract"):
        check(root)


@pytest.mark.parametrize("client_id", ["forgejo", "dify", "librechat"])
def test_supported_application_authentication_client(package, client_id):
    root, manifest, _, save = package
    manifest["spec"]["authentication"] = authentication() | {"clientId": client_id}
    save()
    auth = check(root).spec.authentication
    assert auth is not None
    assert auth.client_id == client_id


@pytest.mark.parametrize(
    "change",
    [
        {"redirectUris": ["https://example.test/*"]},
        {"redirectUris": ["http://example.test/callback"]},
        {"redirectUris": ["https://user:pass@example.test/callback"]},
        {"redirectUris": ["https://example.test/callback#fragment"]},
        {"redirectUris": ["https://example.test/callback\\other"]},
        {"redirectUris": ["https://example.test/call back"]},
        {"webOrigins": ["https://example.test/"]},
        {"webOrigins": ["https://example.test?query=yes"]},
        {"realmRoles": []},
        {"realmRoles": ["example-user", "example-user"]},
        {"roleClaim": "sub"},
        {"roleClaim": "resource_access.roles"},
        {"audiences": ["agentgateway"]},
        {"gatewayPermissions": ["llm:invoke"]},
        {"mode": "login+gateway", "audiences": ["agentgateway"]},
        {"mode": "login+gateway", "userTokenForwarding": True},
        {"clientApproval": {"realmRoles": ["example-user"]}},
    ],
)
def test_invalid_authentication_declaration(package, change):
    root, manifest, _, save = package
    manifest["spec"]["authentication"] = authentication() | change
    save()
    with pytest.raises(ContractError, match="invalid addon release contract"):
        check(root)


def test_optional_authentication_and_explicit_forwarding(package):
    root, manifest, _, save = package
    assert check(root).spec.authentication is None
    manifest["spec"]["authentication"] = authentication()
    save()
    auth = check(root).spec.authentication
    assert auth is not None
    assert auth.mode == "login-only"
    assert auth.refresh_token_rotation is True
    manifest["spec"]["authentication"].update(
        {
            "mode": "login+gateway",
            "userTokenForwarding": True,
            "audiences": ["agentgateway"],
            "gatewayPermissions": ["model:vendor/Model-1.2:invoke"],
        }
    )
    save()
    auth = check(root).spec.authentication
    assert auth is not None
    assert auth.gateway_permissions == ["model:vendor/Model-1.2:invoke"]
