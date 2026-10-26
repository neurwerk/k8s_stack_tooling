"""Prove ESO's bounded literal ConfigMap subset without evaluating Go templates."""

import copy

import pytest

from k8s_stack_tooling.addons import ContractError, secret_template_keys
from k8s_stack_tooling.addons.schema import Input
from k8s_stack_tooling.addons.validation import check, check_secret_deliveries
from tests import test_addons as package_fixtures
from tests.test_addon_composition import deliveries

package = package_fixtures.package


def template_delivery():
    addon, external, base = deliveries(stt=False)
    external["spec"]["data"] = [
        {
            "secretKey": f"raw{index}",
            "remoteRef": {"key": "addons/synthetic/runtime", "property": "key"},
        }
        for index in range(8)
    ]
    external["spec"]["target"]["template"] = {
        "engineVersion": "v2",
        "mergePolicy": "Replace",
        "type": "Opaque",
        "data": {f"INLINE_{index}": "{{ .raw" + str(index) + " }}" for index in range(8)},
        "templateFrom": [
            {
                "target": "Data",
                "configMap": {
                    "name": "runtime-template",
                    "items": [
                        {"key": f"SETTING_{index}", "templateAs": "Values"} for index in range(3)
                    ],
                },
            }
        ],
    }
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"namespace": "app", "name": "runtime-template"},
        "data": {f"SETTING_{index}": f"nonsecret-{index}" for index in range(3)},
    }
    return addon, external, base, configmap


def test_three_literal_settings_and_eight_inline_keys_produce_eleven_final_keys():
    addon, external, base, configmap = template_delivery()
    objects = [configmap, external]
    check_secret_deliveries(addon, objects, base)
    assert secret_template_keys(external, objects) == {
        *(f"INLINE_{index}" for index in range(8)),
        *(f"SETTING_{index}" for index in range(3)),
    }


def test_webui_metadata_only_merge_preserves_its_actual_five_direct_keys():
    expected = {
        "DATABASE_PASSWORD",
        "OAUTH_CLIENT_SECRET",
        "WEBUI_SECRET_KEY",
        "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY",
        "OAUTH_CLIENT_INFO_ENCRYPTION_KEY",
    }
    resource = {
        "metadata": {"namespace": "frontend-open-webui"},
        "spec": {
            "data": [{"secretKey": key} for key in sorted(expected)],
            "target": {
                "template": {
                    "engineVersion": "v2",
                    "mergePolicy": "Merge",
                    "type": "Opaque",
                    "metadata": {"labels": {"reconcile.fluxcd.io/watch": "Enabled"}},
                }
            },
        },
    }
    assert secret_template_keys(resource, []) == expected


def test_dify_replace_retains_exact_actual_eleven_target_keys_not_fetch_aliases():
    _, external, _, configmap = template_delivery()
    inline = {
        "SECRET_KEY",
        "DB_PASSWORD",
        "REDIS_PASSWORD",
        "SANDBOX_API_KEY",
        "PLUGIN_DAEMON_KEY",
        "DIFY_AGENT_API_TOKEN",
        "DIFY_AGENT_SERVER_SECRET_KEY",
        "DIFY_AGENT_SANDBOX_AUTH_TOKEN",
    }
    settings = {"KEYCLOAK_OIDC_ISSUER_URL", "KEYCLOAK_OIDC_CLIENT_ID", "LLM_PROXY_ENDPOINT"}
    template = external["spec"]["target"]["template"]
    template["data"] = {key: "{{ .raw0 }}" for key in inline}
    template["templateFrom"][0]["configMap"]["items"] = [
        {"key": key, "templateAs": "Values"} for key in sorted(settings)
    ]
    configmap["data"] = dict.fromkeys(settings, "nonsecret-setting")
    assert secret_template_keys(external, [configmap]) == inline | settings


@pytest.mark.parametrize("policy", [None, "Replace", "Merge"])
def test_metadata_type_only_templates_keep_direct_data_without_a_data_template(policy):
    resource = {
        "metadata": {"namespace": "app"},
        "spec": {
            "data": [{"secretKey": "direct"}],
            "target": {"template": {"type": "Opaque"}},
        },
    }
    if policy is not None:
        resource["spec"]["target"]["template"]["mergePolicy"] = policy
    assert secret_template_keys(resource, []) == {"direct"}


def test_merge_accounts_for_template_precedence_on_direct_key_overlap_without_execution():
    addon, external, base, configmap = template_delivery()
    external["spec"]["target"]["template"]["mergePolicy"] = "Merge"
    external["spec"]["target"]["template"]["data"]["raw0"] = "{{ .raw0 }}"
    before = copy.deepcopy(external)
    check_secret_deliveries(addon, [external, configmap], base)
    assert secret_template_keys(external, [configmap]) == {
        *(f"raw{index}" for index in range(8)),
        *(f"INLINE_{index}" for index in range(8)),
        *(f"SETTING_{index}" for index in range(3)),
    }
    # The overlapping template has precedence in ESO, not an extra key or a local evaluation.
    assert external == before


@pytest.mark.parametrize("field,value", [("mergePolicy", "Unknown"), ("engineVersion", "v1")])
def test_template_key_accounting_rejects_unsupported_modes(field, value):
    _, external, _, configmap = template_delivery()
    external["spec"]["target"]["template"][field] = value
    with pytest.raises(ContractError, match="v2 Merge or Replace"):
        secret_template_keys(external, [configmap])


@pytest.mark.parametrize("owner", ["base", "client"])
def test_template_configmap_from_other_owner_requires_explicit_input(owner):
    addon, external, base, configmap = template_delivery()
    sources = {"base": base, "client": []}
    sources[owner].append(configmap)
    with pytest.raises(ContractError, match="template ConfigMap is missing"):
        check_secret_deliveries(
            addon, [external], sources["base"], client_resources=sources["client"]
        )
    addon.spec.inputs.append(
        Input(kind="ConfigMap", namespace="app", name="runtime-template", owner=owner)
    )
    check_secret_deliveries(addon, [external], sources["base"], client_resources=sources["client"])


@pytest.mark.parametrize(
    "failure",
    [
        "namespace",
        "missing-map",
        "missing-key",
        "collision",
        "secret",
        "literal",
        "dynamic",
        "keys-values",
        "target",
        "reference-namespace",
        "duplicate-producer",
        "reader",
        "field",
        "final-target",
    ],
)
def test_unsupported_template_sources_or_delivery_changes_fail_closed(failure):
    addon, external, base, configmap = template_delivery()
    source = external["spec"]["target"]["template"]["templateFrom"][0]
    objects = [configmap, external]
    if failure == "namespace":
        configmap["metadata"]["namespace"] = "other"
    elif failure == "missing-map":
        objects.remove(configmap)
    elif failure == "missing-key":
        del configmap["data"]["SETTING_0"]
    elif failure == "collision":
        external["spec"]["target"]["template"]["data"]["SETTING_0"] = "inline"
    elif failure == "secret":
        source["secret"] = source.pop("configMap")
    elif failure == "literal":
        source["literal"] = "unreviewed"
        del source["configMap"]
    elif failure == "dynamic":
        configmap["data"]["SETTING_0"] = "{{ .unreviewed }}"
    elif failure == "keys-values":
        source["configMap"]["items"][0]["templateAs"] = "KeysAndValues"
    elif failure == "target":
        source["target"] = "Annotations"
    elif failure == "reference-namespace":
        source["configMap"]["namespace"] = "other"
    elif failure == "duplicate-producer":
        other = copy.deepcopy(configmap)
        other["data"]["SETTING_0"] = "different"
        objects.append(other)
    elif failure == "reader":
        base[1]["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"] = "unapproved"
    elif failure == "field":
        external["spec"]["data"][0]["remoteRef"]["property"] = "unapproved"
    elif failure == "final-target":
        external["spec"]["target"]["name"] = "different-secret"
    with pytest.raises(ContractError):
        check_secret_deliveries(addon, objects, base)


@pytest.mark.parametrize("same_package", [False, True])
def test_externalsecret_template_from_generated_configmap_uses_package_not_helm_dependencies(
    package, same_package
):
    root, manifest, documents, save = package
    producer = copy.deepcopy(documents["app"][0])
    producer["metadata"]["name"] = "settings"
    producer["spec"].pop("valuesFrom")
    external = copy.deepcopy(documents["secrets"][0])
    external["spec"]["target"]["template"] = {
        "templateFrom": [
            {
                "target": "Data",
                "configMap": {
                    "name": "settings",
                    "items": [{"key": "SETTING", "templateAs": "Values"}],
                },
            }
        ]
    }
    output = {
        "kind": "ConfigMap",
        "namespace": "example",
        "name": "settings",
        "helmRelease": "settings",
    }
    documents["app"] = [external]
    if same_package:
        documents["app"].append(producer)
        documents["secrets"] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "example"}}
        ]
        manifest["spec"]["packages"][1]["helmOutputs"] = [output]
    else:
        documents["secrets"] = [producer]
        manifest["spec"]["packages"][0]["helmOutputs"] = [output]
    save()
    check(root)
    if not same_package:
        manifest["spec"]["packages"][1]["dependsOn"] = []
        save()
        with pytest.raises(ContractError, match="producer readiness dependency"):
            check(root)
