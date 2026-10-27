"""Exercise finite instance expansion and product-required resource boundaries."""

import base64
import copy
import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from k8s_stack_tooling.addons import (
    ContractError,
    decode_native_output,
    expand_setup,
    resolve_browser_identity,
    valid_value,
    validate_verifier_slots,
)
from k8s_stack_tooling.addons.schema import Addon, AddonSelection, ValueValidation
from k8s_stack_tooling.addons.validation import (
    check_artifacts,
    check_browser_bindings,
    check_consumers,
    check_database_claims,
)


def manifest(name="synthetic"):
    return {
        "apiVersion": "addons.neurwerk.com/v1alpha2",
        "kind": "AddonRelease",
        "metadata": {"name": name},
        "spec": {
            "version": "1.0.0",
            "status": "staged",
            "sourceName": name,
            "configurationKey": name,
            "prerequisites": {
                "base": "0.4.0",
                "tooling": "0.14.0",
                "stackSetup": "0.7.0",
                "baseCharts": ["charts/postgres/addon"],
            },
            "packages": [{"name": "app", "path": "releases/app"}],
        },
    }


def selected(instances=None):
    return AddonSelection.model_validate(
        {
            "id": "synthetic",
            "sourceRef": {"kind": "GitRepository", "namespace": "flux-system", "name": "synthetic"},
            "instances": [] if instances is None else instances,
        }
    )


def bot_manifest():
    data = manifest()
    bot: dict[str, Any] = {
        "id": "bot",
        "path": "addons/synthetic/bots/{instance}",
        "fields": {
            "key": {"when": "gateway", "generate": {"encoding": "base64url", "bytes": 32}},
            "stt": {"when": "stt", "supplied": {"group": "stt", "key": "token"}},
            "tts": {"when": "tts", "supplied": {"group": "tts", "key": "token"}},
        },
    }
    machine = {
        "id": "machine",
        "path": "addons/synthetic/machines/{instance}",
        "when": "gateway",
        "fields": {"secret": {"generate": {"encoding": "hex", "bytes": 32}}},
    }
    verifier = {
        "id": "verifier",
        "path": "addons/synthetic/verifiers/{instance}",
        "when": "gateway",
        "fields": {"hash": {"sha256": {"record": "bot", "field": "key"}}},
    }
    groups = [
        {"id": feature, "when": feature, "fields": {"token": {}}} for feature in ("stt", "tts")
    ]
    protected = copy.deepcopy(bot)
    del protected["fields"]["key"]
    data["spec"]["instanceRecipes"] = [
        {
            "id": "anonymous",
            "features": {"gateway": True, "stt": False, "tts": False, "sessions": False},
            "setup": {"records": [bot, machine, verifier], "inputGroups": groups},
        },
        {
            "id": "protected",
            "features": {"stt": False, "tts": False},
            "setup": {"records": [protected], "inputGroups": groups},
        },
    ]
    data["spec"]["instanceRecipes"][0]["setup"].update(
        {
            "readers": [
                {
                    "role": "verifier-{instance}",
                    "namespace": "gateway",
                    "serviceAccount": "verifier-{instance}",
                    "audience": "openbao",
                    "records": ["verifier"],
                    "when": "gateway",
                }
            ],
            "deliveries": [
                {
                    "namespace": "gateway",
                    "name": "verifier-{instance}",
                    "secretStore": "shared-secret-store",
                    "targetSecret": "verifier-{instance}",
                    "records": ["verifier"],
                    "when": "gateway",
                }
            ],
        }
    )
    return data


def test_instances_select_exact_credential_shape_without_sessions_gate():
    addon = Addon.model_validate(bot_manifest())
    assert expand_setup(addon, selected()).records == []
    setup = expand_setup(
        addon,
        selected(
            [
                {"id": "public", "recipe": "anonymous"},
                {
                    "id": "speech",
                    "recipe": "anonymous",
                    "features": {"gateway": False, "stt": True},
                },
                {"id": "private", "recipe": "protected", "features": {"tts": True}},
            ]
        ),
    )
    records = {record.id: record for record in setup.records}
    assert set(records) == {
        "public-bot",
        "public-machine",
        "public-verifier",
        "speech-bot",
        "private-bot",
    }
    assert set(records["speech-bot"].fields) == {"stt"}
    assert set(records["private-bot"].fields) == {"tts"}
    reference = records["public-verifier"].fields["hash"].sha256
    assert reference is not None and reference.record == "public-bot"
    assert records["speech-bot"].path == "addons/synthetic/bots/speech"
    supplied = records["speech-bot"].fields["stt"].supplied
    assert supplied is not None and supplied.group == "speech-stt"
    assert {group.id for group in setup.input_groups} == {"speech-stt", "private-tts"}
    assert [(r.role, r.namespace, r.service_account, r.records) for r in setup.readers] == [
        ("verifier-public", "gateway", "verifier-public", ["public-verifier"])
    ]
    assert setup.deliveries[0].secret_store == "shared-secret-store"
    with pytest.raises(ContractError, match="undeclared"):
        expand_setup(
            addon,
            selected([{"id": "private", "recipe": "protected", "features": {"gateway": True}}]),
        )


def test_instance_limits_and_collision_rejection():
    data = bot_manifest()
    rows = [{"id": f"bot-{n}", "recipe": "anonymous"} for n in range(32)]
    assert len(expand_setup(Addon.model_validate(data), selected(rows)).records) == 96
    with pytest.raises(ValidationError):
        selected(rows + [{"id": "extra", "recipe": "anonymous"}])
    with pytest.raises(ValidationError):
        selected([rows[0], rows[0]])
    with pytest.raises(ValidationError):
        selected([{"id": "../escape", "recipe": "anonymous"}])
    data["spec"]["setup"] = {
        "records": [
            {
                "id": "bot-0-bot",
                "path": "addons/synthetic/other",
                "fields": {"fixed": {"literal": "constant"}},
            }
        ]
    }
    with pytest.raises(ContractError, match="collisions"):
        expand_setup(Addon.model_validate(data), selected(rows[:1]))


def test_field_condition_reference_integrity():
    data = bot_manifest()
    fields = data["spec"]["instanceRecipes"][0]["setup"]["records"][0]["fields"]
    fields["unsafe"] = {"ref": {"record": "bot", "field": "stt"}}
    with pytest.raises(ValidationError, match="activation"):
        Addon.model_validate(data)


def test_slot_secondary_match_is_preserved_and_utf8_hashed():
    data = manifest()
    data["spec"]["setup"] = {
        "records": [
            {
                "id": "key",
                "path": "addons/synthetic/gateway/key",
                "fields": {"raw": {"generate": {"encoding": "hex", "bytes": 32}}},
            },
            {
                "id": "verifiers",
                "path": "addons/synthetic/gateway/verifiers",
                "fields": {
                    "primary": {"sha256": {"record": "key", "field": "raw"}},
                    "secondary": {
                        "literal": "",
                        "validation": {"minLength": 0, "format": "sha256"},
                    },
                },
            },
        ],
        "invariants": [
            {
                "kind": "accepted-verifier-slots",
                "source": {"record": "key", "field": "raw"},
                "primary": {"record": "verifiers", "field": "primary"},
                "secondary": {"record": "verifiers", "field": "secondary"},
            }
        ],
    }
    setup = Addon.model_validate(data).spec.setup
    raw = "synthetic-κλειδί"
    values = {
        "key": {"raw": raw},
        "verifiers": {"primary": "a" * 64, "secondary": hashlib.sha256(raw.encode()).hexdigest()},
    }
    before = copy.deepcopy(values)
    validate_verifier_slots(setup, values)
    assert values == before
    values["verifiers"]["secondary"] = ""
    with pytest.raises(ContractError, match="accepted verifier"):
        validate_verifier_slots(setup, values)


def claim(owner, name, engine="postgresql", service="postgres"):
    return {
        "id": name.replace("_", "-"),
        "service": {"namespace": "database", "name": service},
        "engine": engine,
        "database": name,
        "role": name,
        "owner": owner,
        "package": "app",
        "baseChart": "charts/postgres/documentdb-addon"
        if engine == "documentdb"
        else "charts/postgres/addon",
        "exclusive": engine == "documentdb",
    }


def test_sql_claims_and_documentdb_exclusivity():
    data = manifest()
    data["spec"]["databases"] = [
        claim("synthetic", name) for name in ("app", "app_plugin", "app_vector")
    ]
    data["spec"]["databases"][-1]["extensions"] = ["vector"]
    addon = Addon.model_validate(data)
    check_database_claims([addon])
    other = manifest("other")
    other["spec"]["databases"] = [claim("other", "app")]
    with pytest.raises(ContractError, match="ownership"):
        check_database_claims([addon, Addon.model_validate(other)])
    data["spec"]["databases"] = [claim("synthetic", "chat", "documentdb", "documents")]
    other["spec"]["databases"] = [claim("other", "other", "documentdb", "documents")]
    for item in (data, other):
        item["spec"]["prerequisites"]["baseCharts"].append("charts/postgres/documentdb-addon")
    with pytest.raises(ContractError, match="exclusive"):
        check_database_claims([Addon.model_validate(data), Addon.model_validate(other)])
    other["spec"]["databases"][0]["service"]["name"] = "separate-documents"
    check_database_claims([Addon.model_validate(data), Addon.model_validate(other)])


def test_sql_role_owner_is_namespaced_by_addon_identity():
    data = manifest()
    data["spec"]["databases"] = [claim("synthetic", "first")]
    first = Addon.model_validate(data)
    second = first.model_copy(deep=True)
    second.metadata.name = "different-addon"
    second.spec.databases[0].database = "second"
    # Exercise the helper boundary with identical owner strings and distinct product IDs.
    with pytest.raises(ContractError, match="conflicting ownership"):
        check_database_claims([first, second])
    second.metadata.name = first.metadata.name
    check_database_claims([first, second])


def shared_endpoint_addon(owner, name, engine="postgresql"):
    data = manifest(owner)
    database = claim(owner, name, engine, "postgres-operations")
    database["service"]["namespace"] = "infra-postgres-operations"
    if engine == "documentdb":
        data["spec"]["prerequisites"]["baseCharts"].append("charts/postgres/documentdb-addon")
        database["baseChart"] = "charts/postgres/documentdb-addon"
    data["spec"]["databases"] = [database]
    return Addon.model_validate(data)


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_service_accepts_postgresql_and_exclusive_documentdb_endpoints(reverse):
    sql = shared_endpoint_addon("synthetic", "sql_app")
    documents = shared_endpoint_addon("documents", "document_app", "documentdb")
    # These are the fixed PostgreSQL 5432 / DocumentDB 10260 endpoints on one Base Service.
    assert sql.spec.databases[0].service == documents.spec.databases[0].service
    selected = [sql, documents]
    check_database_claims(list(reversed(selected)) if reverse else selected)


def test_document_store_cannot_escape_exclusivity_by_relabelling_its_engine():
    first = shared_endpoint_addon("documents", "first", "documentdb")
    second = shared_endpoint_addon("other", "second", "documentdb").model_dump(by_alias=True)
    second["spec"]["databases"][0]["engine"] = "postgresql"
    with pytest.raises(ContractError, match="engine.*provisioning interface"):
        check_database_claims([first, Addon.model_validate(second)])


@pytest.mark.parametrize("sql_position", [0, 1, 2])
def test_second_documentdb_consumer_is_rejected_even_with_sql_on_shared_service(sql_position):
    selected = [
        shared_endpoint_addon("documents", "first", "documentdb"),
        shared_endpoint_addon("other", "second", "documentdb"),
    ]
    selected.insert(sql_position, shared_endpoint_addon("synthetic", "sql_app"))
    with pytest.raises(ContractError, match="endpoint has exclusive consumers"):
        check_database_claims(selected)


def test_same_sql_endpoint_database_collision_and_cross_engine_role_collision_are_rejected():
    first = shared_endpoint_addon("synthetic", "sql_app")
    second = shared_endpoint_addon("other", "other_app")
    second.spec.databases[0].database = first.spec.databases[0].database
    with pytest.raises(ContractError, match="database or role has conflicting ownership"):
        check_database_claims([first, second])
    documents = shared_endpoint_addon("documents", "document_app", "documentdb")
    documents.spec.databases[0].role = first.spec.databases[0].role
    with pytest.raises(ContractError, match="database or role has conflicting ownership"):
        check_database_claims([first, documents])


@pytest.mark.parametrize("change", ["exclusive", "extensions", "owner", "prerequisite"])
def test_database_claims_require_valid_ownership_and_capability(change):
    data = manifest()
    data["spec"]["prerequisites"]["baseCharts"].append("charts/postgres/documentdb-addon")
    database = claim("synthetic", "chat", "documentdb", "documents")
    data["spec"]["databases"] = [database]
    if change == "exclusive":
        database["exclusive"] = False
    elif change == "extensions":
        database["extensions"] = ["vector"]
    elif change == "owner":
        database["owner"] = "foreign"
    else:
        data["spec"]["prerequisites"]["baseCharts"] = []
    with pytest.raises(ValidationError):
        Addon.model_validate(data)


def artifact_manifest():
    data = manifest()
    data["spec"]["artifacts"] = [
        {"id": "tooling", "role": "product", "image": "ghcr.io/example/tooling:1.2.3"}
    ]
    data["spec"]["consumers"] = [
        {
            "artifact": "tooling",
            "package": "app",
            "kind": "Job",
            "namespace": "app",
            "name": "prepare",
            "container": "stage",
            "location": "initContainers",
        }
    ]
    return data


def test_staging_consumer_matches_effective_image_and_requires_installable_digest():
    data = artifact_manifest()
    addon = Addon.model_validate(data)
    check_artifacts(addon, installable=False)
    with pytest.raises(ContractError, match="digest"):
        check_artifacts(addon, installable=True)
    resource = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"namespace": "app", "name": "prepare"},
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [{"name": "stage", "image": addon.spec.artifacts[0].image}]
                }
            }
        },
    }
    check_consumers(addon, {"app": [resource]})
    resource["spec"]["template"]["spec"]["initContainers"][0]["image"] = "unreviewed:latest"
    with pytest.raises(ContractError, match="effective container"):
        check_consumers(addon, {"app": [resource]})
    data["spec"]["artifacts"][0]["image"] += "@sha256:" + "0" * 64
    with pytest.raises(ContractError, match="digest"):
        check_artifacts(Addon.model_validate(data), installable=True)


def test_effective_helm_consumers_check_overrides_hooks_and_missing_renders():
    data = artifact_manifest()
    data["spec"]["consumers"][0].update({"kind": "HelmRelease", "name": "application"})
    addon = Addon.model_validate(data)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "application"},
    }
    hook = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "namespace": "app",
            "name": "hook",
            "annotations": {"helm.sh/hook": "pre-install"},
        },
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [{"name": "stage", "image": addon.spec.artifacts[0].image}]
                }
            }
        },
    }
    packages = {"app": [release]}
    renders = {("app", "app", "application"): [hook]}
    check_consumers(addon, packages, helm_resources=renders)
    with pytest.raises(ContractError, match="render is missing"):
        check_consumers(addon, packages, helm_resources={})
    hook["spec"]["template"]["spec"]["initContainers"][0]["image"] = "unreviewed:latest"
    with pytest.raises(ContractError, match="effective container image"):
        check_consumers(addon, packages, helm_resources=renders)


def test_declared_helm_output_must_exist_in_effective_render():
    data = manifest()
    data["spec"]["packages"][0]["helmOutputs"] = [
        {"kind": "ConfigMap", "namespace": "app", "name": "settings", "helmRelease": "settings"}
    ]
    addon = Addon.model_validate(data)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "settings"},
    }
    packages = {"app": [release]}
    renders = {("app", "app", "settings"): []}
    with pytest.raises(ContractError, match="absent from the effective render"):
        check_consumers(addon, packages, helm_resources=renders)
    renders[("app", "app", "settings")] = [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": "app", "name": "settings"},
        }
    ]
    check_consumers(addon, packages, helm_resources=renders)


def test_helm_externalsecret_target_is_a_generated_secret_output():
    data = manifest()
    data["spec"]["packages"][0]["helmOutputs"] = [
        {"kind": "Secret", "namespace": "app", "name": "generated", "helmRelease": "settings"}
    ]
    addon = Addon.model_validate(data)
    release = {
        "apiVersion": "helm.toolkit.fluxcd.io/v2",
        "kind": "HelmRelease",
        "metadata": {"namespace": "app", "name": "settings"},
    }
    external = {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"namespace": "app", "name": "delivery"},
        "spec": {"target": {"name": "generated"}},
    }
    check_consumers(
        addon, {"app": [release]}, helm_resources={("app", "app", "settings"): [external]}
    )


def test_optional_consumer_allows_absence_but_never_wrong_or_unknown_images():
    data = artifact_manifest()
    data["spec"]["consumers"][0]["optional"] = True
    addon = Addon.model_validate(data)
    check_consumers(addon, {})
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"namespace": "app", "name": "prepare"},
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [{"name": "stage", "image": addon.spec.artifacts[0].image}]
                }
            }
        },
    }
    check_consumers(addon, {"app": [job]})
    job["spec"]["template"]["spec"]["initContainers"][0]["image"] = "unknown:latest"
    with pytest.raises(ContractError, match="matching declaration"):
        check_consumers(addon, {"app": [job]})
    job["spec"]["template"]["spec"]["initContainers"][0]["name"] = "unlisted"
    with pytest.raises(ContractError, match="matching declaration"):
        check_consumers(addon, {"app": [job]})


def test_inert_job_template_resolves_both_image_slots_without_execution(tmp_path):
    data = artifact_manifest()
    data["spec"]["artifacts"].append(
        {"id": "native", "role": "native", "image": "example.test/native:1.2.3"}
    )
    data["spec"]["consumers"] = [
        {
            "artifact": "tooling",
            "package": "app",
            "kind": "JobTemplate",
            "namespace": "app",
            "name": "manual",
            "templateKey": "job-template.json",
            "container": "stage",
            "location": "initContainers",
        },
        {
            "artifact": "native",
            "package": "app",
            "kind": "JobTemplate",
            "namespace": "app",
            "name": "manual",
            "templateKey": "job-template.json",
            "container": "execute",
        },
    ]
    addon = Addon.model_validate(data)
    marker = tmp_path / "never-created"
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"generateName": "manual-", "namespace": "app"},
        "spec": {
            "template": {
                "spec": {
                    "initContainers": [{"name": "stage", "image": addon.spec.artifacts[0].image}],
                    "containers": [
                        {
                            "name": "execute",
                            "image": addon.spec.artifacts[1].image,
                            "command": ["touch", str(marker)],
                        }
                    ],
                }
            }
        },
    }
    resource = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"namespace": "app", "name": "manual"},
        "data": {"job-template.json": json.dumps(job)},
    }
    check_consumers(addon, {"app": [resource]})
    assert not marker.exists()
    job["spec"]["template"]["spec"]["containers"][0]["image"] = "unknown:latest"
    resource["data"]["job-template.json"] = json.dumps(job)
    with pytest.raises(ContractError, match="Job template"):
        check_consumers(addon, {"app": [resource]})
    resource["data"]["job-template.json"] = '{"kind":"Job","kind":"Pod"}'
    with pytest.raises(ContractError, match="Job template"):
        check_consumers(addon, {"app": [resource]})


def native_manifest():
    data = artifact_manifest()
    data["spec"]["consumers"] = []
    validation = {"format": "jwt-hs256-nbf", "minLength": 32, "maxLength": 2048}
    data["spec"]["nativeOutputs"] = [
        {
            "id": "token",
            "artifact": "tooling",
            "argv": ["/app/.venv/bin/synthetic-token"],
            "idleArgv": ["/usr/bin/sleep", "300"],
            "outputKey": "token",
            "validation": validation,
        }
    ]
    data["spec"]["setup"] = {
        "records": [
            {
                "id": "runtime",
                "path": "addons/synthetic/runtime",
                "fields": {"token": {"preparedOutput": "token", "validation": validation}},
            }
        ]
    }
    return data


def test_native_output_binds_artifact_command_and_validation():
    data = native_manifest()
    addon = Addon.model_validate(data)
    assert addon.spec.native_outputs[0].transport == "private-exec-json"
    data["spec"]["nativeOutputs"][0]["argv"] = ["/bin/sh", "-c", "untrusted"]
    with pytest.raises(ValidationError, match="executable"):
        Addon.model_validate(data)
    data = native_manifest()
    data["spec"]["setup"]["records"][0]["fields"]["token"]["validation"] = {
        "format": "jwt-hs256-nbf",
        "minLength": 32,
        "maxLength": 4096,
    }
    with pytest.raises(ValidationError, match="matching declared validation"):
        Addon.model_validate(data)


@pytest.mark.parametrize(
    "idle", [None, [], ["sleep", "300"], ["/bin/sh", "-c", "sleep 300"], ["/bin/sleep", "x" * 1025]]
)
def test_native_output_requires_bounded_non_shell_idle_command(idle):
    data = native_manifest()
    output = data["spec"]["nativeOutputs"][0]
    if idle is None:
        del output["idleArgv"]
    else:
        output["idleArgv"] = idle
    with pytest.raises(ValidationError):
        Addon.model_validate(data)


@pytest.mark.parametrize(
    "header,payload,signature,accepted",
    [
        ({"alg": "HS256"}, {"nbf": 0}, b"s" * 32, True),
        ({"alg": "none"}, {"nbf": 0}, b"s" * 32, False),
        ({"alg": "HS256"}, {}, b"s" * 32, False),
        ({"alg": "HS256"}, {"nbf": True}, b"s" * 32, False),
        ({"alg": "HS256"}, {"nbf": 0.0}, b"s" * 32, False),
        ({"alg": "HS256"}, {"nbf": 0}, b"short", False),
    ],
)
def test_native_jwt_structure_is_bounded(header, payload, signature, accepted):
    parts = [json.dumps(header).encode(), json.dumps(payload).encode(), signature]
    value = ".".join(base64.urlsafe_b64encode(part).decode().rstrip("=") for part in parts)
    assert valid_value(value, ValueValidation(format="jwt-hs256-nbf")) is accepted
    assert not valid_value(value, ValueValidation(format="jwt-hs256-nbf", maxLength=10))


@pytest.mark.parametrize("tag", ["v0.12.3", "pg18-0.116.0"])
def test_upstream_tag_and_command_consumer_without_kubernetes_workload(tag):
    data = artifact_manifest()
    # Synthetic digest tests syntax only, not publication or registry existence.
    data["spec"]["artifacts"][0]["image"] = (
        f"ghcr.io/example/tooling:{tag}@sha256:" + hashlib.sha256(b"synthetic-image").hexdigest()
    )
    data["spec"]["consumers"] = [
        {"kind": "Command", "artifact": "tooling", "argv": ["synthetic-check", "--offline"]}
    ]
    addon = Addon.model_validate(data)
    check_artifacts(addon, installable=True)
    check_consumers(addon, {})
    for tag in ("latest", "stable", "main", "PLACEHOLDER"):
        data["spec"]["artifacts"][0]["image"] = f"ghcr.io/example/tooling:{tag}"
        with pytest.raises(ContractError):
            check_artifacts(Addon.model_validate(data), installable=True)


def test_documentdb_keeps_mixed_case_name_and_sql_stays_strict():
    data = manifest()
    data["spec"]["prerequisites"]["baseCharts"].append("charts/postgres/documentdb-addon")
    database = claim("synthetic", "chat", "documentdb", "postgres-operations")
    database["database"] = "LibreChat"
    database["service"]["namespace"] = "infra-postgres-operations"
    data["spec"]["databases"] = [database]
    addon = Addon.model_validate(data)
    check_database_claims([addon])
    assert addon.spec.databases[0].database == "LibreChat"
    database["engine"] = "postgresql"
    with pytest.raises(ValidationError, match="database name"):
        Addon.model_validate(data)


def test_instance_markers_preserve_shared_stores_accounts_and_canonical_names():
    data = bot_manifest()
    setup = data["spec"]["instanceRecipes"][0]["setup"]
    setup["readers"][0].update({"role": "shared-reader", "serviceAccount": "external-secrets"})
    setup["deliveries"] = [
        {
            "namespace": "pipecat",
            "name": "pipecat-bot-{instance}",
            "targetSecret": "pipecat-bot-{instance}",
            "secretStore": "pipecat-openbao-secret-store",
            "records": ["bot"],
        },
        {
            "namespace": "auth-keycloak",
            "name": "pipecat-bot-{instance}-oidc",
            "targetSecret": "pipecat-bot-{instance}-oidc",
            "secretStore": "auth-keycloak-openbao-secret-store",
            "records": ["machine"],
            "when": "gateway",
        },
        {
            "namespace": "auth-keycloak-api-key-bridge",
            "name": "pipecat-bot-{instance}-verifier",
            "targetSecret": "pipecat-bot-{instance}-verifier",
            "secretStore": "auth-keycloak-api-key-bridge-openbao-secret-store",
            "records": ["verifier"],
            "when": "gateway",
        },
    ]
    result = expand_setup(
        Addon.model_validate(data),
        selected(
            [
                {"id": "alpha", "recipe": "anonymous"},
                {"id": "beta", "recipe": "anonymous"},
            ]
        ),
    )
    assert [d.name for d in result.deliveries[:3]] == [
        "pipecat-bot-alpha",
        "pipecat-bot-alpha-oidc",
        "pipecat-bot-alpha-verifier",
    ]
    assert result.deliveries[1].secret_store == "auth-keycloak-openbao-secret-store"
    assert len(result.readers) == 1
    assert result.readers[0].service_account == "external-secrets"
    assert result.readers[0].records == ["alpha-verifier", "beta-verifier"]
    setup["records"][0]["path"] = "addons/synthetic/bots/{unexpected}"
    with pytest.raises(ValidationError):
        Addon.model_validate(data)


def test_browser_uses_effective_settings_and_client_approval_without_manifest_hosts():
    data = manifest()
    data["spec"]["inputs"] = [
        {
            "kind": "ConfigMap",
            "namespace": "identity",
            "name": "client-settings",
            "owner": "client",
        },
        {
            "kind": "ConfigMap",
            "namespace": "identity",
            "name": "client-approval",
            "owner": "client",
        },
    ]
    data["spec"]["setup"] = {
        "records": [
            {
                "id": "browser",
                "path": "addons/synthetic/oidc/browser",
                "fields": {"secret": {"generate": {"encoding": "hex", "bytes": 32}}},
            }
        ]
    }
    data["spec"]["identities"] = [
        {
            "id": "browser",
            "mode": "browser",
            "clientId": "synthetic",
            "secret": {"record": "browser", "field": "secret"},
            "browserSettings": {
                "namespace": "identity",
                "settings": "client-settings",
                "approval": "client-approval",
            },
        }
    ]
    addon = Addon.model_validate(data)
    request = addon.spec.identities[0]
    consumer: dict[str, Any] = {
        "kind": "HelmRelease",
        "metadata": {"namespace": "identity", "name": "oidc"},
        "spec": {
            "chart": {"spec": {"chart": "./charts/keycloak/addon-oidc"}},
            "valuesFrom": [
                {"kind": "ConfigMap", "name": "client-settings", "optional": False},
                {"kind": "ConfigMap", "name": "client-approval", "optional": False},
            ],
        },
    }
    check_browser_bindings(addon, {"app": [consumer]})
    consumer["spec"]["valuesFrom"][1]["optional"] = True
    with pytest.raises(ContractError, match="feed the Base OIDC"):
        check_browser_bindings(addon, {"app": [consumer]})
    authentication = {
        "clientId": "synthetic",
        "redirectUris": ["https://client.example/oauth/callback"],
        "webOrigins": ["https://client.example"],
        "postLogoutRedirectUris": ["https://client.example/auth"],
        "realmRoles": ["synthetic-user"],
        "roleClaim": "realm_access.roles",
    }
    approval = {key: value for key, value in authentication.items() if key != "roleClaim"}
    effective = {"authentication": authentication, "clientApproval": copy.deepcopy(approval)}
    resolved = resolve_browser_identity(request, effective)
    assert resolved.browser is not None and resolved.browser.logout_uris == [
        "https://client.example/auth"
    ]
    assert resolved.browser_settings is None
    authentication["redirectUris"] = ["https://unapproved.example/oauth/callback"]
    with pytest.raises(ContractError, match="exceeds client approval"):
        resolve_browser_identity(request, effective)
    data["spec"]["inputs"][1]["owner"] = "base"
    with pytest.raises(ValidationError, match="client approval inputs"):
        Addon.model_validate(data)


def test_raw_native_jwt_envelope_and_canonical_encoding():
    data = native_manifest()
    data["spec"]["nativeOutputs"][0]["transport"] = "private-exec-text"
    output = Addon.model_validate(data).spec.native_outputs[0]
    segments = [b'{"alg":"HS256"}', b'{"nbf":123}', b"s" * 32]
    token = ".".join(base64.urlsafe_b64encode(part).decode().rstrip("=") for part in segments)
    assert decode_native_output(output, (token + "\n").encode()) == {"token": token}
    for invalid in (
        (token + "\nnoise").encode(),
        (token + "=").encode(),
        b"x" * (output.max_bytes + 1),
    ):
        with pytest.raises(ContractError):
            decode_native_output(output, invalid)
