from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from k8s_stack_tooling.api.keycloak_active_directory import (
    LDAP_ENTRY_DN_ATTRIBUTE,
    LDAP_MAPPER_PROVIDER_TYPE,
    MASKED_SECRET,
    ActiveDirectoryConfig,
    ActiveDirectoryError,
    _component_needs_update,
    _create_component,
    _escape_dn_value,
    _mapper_representations,
    _preflight_connection,
    _provider_config,
    _remove_conflicting_full_name_mapper,
    _request_json,
    _sync_group_mapper,
    _upsert_mapper,
    _verify_access_group_bindings,
    _verify_access_groups,
    reconcile_active_directory,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"" if payload is None else b"json"

    def json(self) -> Any:
        return self._payload


def _config(**overrides: Any) -> ActiveDirectoryConfig:
    values: dict[str, Any] = {
        "connection_url": "ldaps://directory.example.com:636",
        "users_dn": "OU=People,DC=example,DC=com",
        "groups_dn": "OU=Access,DC=example,DC=com",
        "username_attribute": "sAMAccountName",
        "group_names": ("neurwerk-studio-users", "neurwerk-studio-admins"),
        "bind_dn": "CN=Keycloak,OU=Service Accounts,DC=example,DC=com",
        "bind_credential": "do-not-log-this",
        "email_verified": True,
    }
    values.update(overrides)
    return ActiveDirectoryConfig(**values)


def test_active_directory_config_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="username attribute"):
        _config(username_attribute="mail")
    with pytest.raises(ValueError, match="must be unique"):
        _config(group_names=("neurwerk-studio-users", "neurwerk-studio-users"))
    with pytest.raises(ValueError, match="emailVerified"):
        _config(email_verified=False)


@pytest.mark.parametrize(
    "connection_url",
    [
        "ldap://directory.example.com:636",
        "ldaps://directory.example.com",
        "ldaps://directory.example.com:389",
        "ldaps://directory.example.com:not-a-port",
        "ldaps://user:password@directory.example.com:636",
        "ldaps://directory.example.com:636/",
        "ldaps://directory.example.com:636/base",
        "ldaps://directory.example.com:636?scope=subtree",
        "ldaps://directory.example.com:636#fragment",
        "ldaps://directory.example.com:636\n",
        "ldaps://[invalid:636",
    ],
)
def test_active_directory_config_requires_explicit_ldaps_636(
    connection_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"explicit ldaps://host:636"):
        _config(connection_url=connection_url)


@pytest.mark.parametrize(
    "bind_principal",
    [
        "CN=Keycloak,OU=Service Accounts,DC=example,DC=com",
        "keycloak-bind@example.com",
    ],
)
def test_active_directory_config_accepts_dn_or_upn_bind_principal(
    bind_principal: str,
) -> None:
    assert _config(bind_dn=bind_principal).bind_dn == bind_principal


@pytest.mark.parametrize(
    "bind_principal",
    [
        "keycloak-bind",
        "@example.com",
        "keycloak-bind@",
        "a@b@example.com",
        "key cloak@example.com",
        "CN=Keycloak\n",
    ],
)
def test_active_directory_config_rejects_invalid_bind_principal(
    bind_principal: str,
) -> None:
    with pytest.raises(ValueError, match="DN-like or UPN-like"):
        _config(bind_dn=bind_principal)


@pytest.mark.parametrize(
    "group_name",
    [
        "studio-users",
        "Neurwerk-studio-users",
        "neurwerk-studio_users",
        "neurwerk-studio-users-",
        "neurwerk-.studio",
        "neurwerk-a/child",
        f"neurwerk-{'a' * 56}",
    ],
)
def test_active_directory_config_rejects_unsafe_group_names(group_name: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        _config(group_names=(group_name,))


def test_active_directory_config_accepts_max_length_approved_group_name() -> None:
    group_name = f"neurwerk-{'a' * 55}"

    assert len(group_name) == 64
    assert _config(group_names=(group_name,)).group_names == (group_name,)


def test_active_directory_config_accepts_existing_agreed_group_name() -> None:
    assert _config(group_names=("neurwerk-llm-all-users",)).group_names == (
        "neurwerk-llm-all-users",
    )


def test_provider_config_builds_exact_direct_membership_filter() -> None:
    config = _config(group_names=("neurwerk-studio-users", "neurwerk-studio.admins"))

    provider = _provider_config(config)

    assert provider["customUserSearchFilter"] == [
        "(|(memberOf=CN=neurwerk-studio-users,OU=Access,DC=example,DC=com)"
        "(memberOf=CN=neurwerk-studio.admins,OU=Access,DC=example,DC=com))"
    ]
    assert provider["searchScope"] == ["2"]
    assert provider["trustEmail"] == ["true"]
    assert provider["bindCredential"] == ["do-not-log-this"]
    assert provider["cachePolicy"] == ["NO_CACHE"]
    assert provider["changedSyncPeriod"] == ["-1"]
    assert provider["fullSyncPeriod"] == ["-1"]


def test_dn_values_are_escaped_before_filter_escaping() -> None:
    assert _escape_dn_value(" admins,prod ") == r"\ admins\,prod\ "


def test_mapper_representations_are_scoped_and_read_only() -> None:
    mappers = _mapper_representations("provider-id", _config())

    assert {mapper["name"] for mapper in mappers} == {
        "username",
        "first name",
        "last name",
        "email",
        "MSAD account controls",
        "approved groups",
        "verified email",
    }
    assert all(mapper["parentId"] == "provider-id" for mapper in mappers)
    assert all(mapper["providerType"] == LDAP_MAPPER_PROVIDER_TYPE for mapper in mappers)
    group_mapper = next(mapper for mapper in mappers if mapper["name"] == "approved groups")
    assert group_mapper["config"]["mode"] == ["READ_ONLY"]
    assert group_mapper["config"]["groups.path"] == ["/access"]
    assert group_mapper["config"]["groups.ldap.filter"] == [
        "(|(cn=neurwerk-studio-users)(cn=neurwerk-studio-admins))"
    ]
    assert group_mapper["config"]["mapped.group.attributes"] == [LDAP_ENTRY_DN_ATTRIBUTE]
    attributes = {mapper["name"]: mapper for mapper in mappers}
    assert attributes["username"]["config"]["always.read.value.from.ldap"] == ["false"]
    for name in ("first name", "last name", "email"):
        assert attributes[name]["config"]["always.read.value.from.ldap"] == ["true"]
    verified_mapper = next(mapper for mapper in mappers if mapper["name"] == "verified email")
    assert verified_mapper["providerId"] == "hardcoded-attribute-mapper"
    assert verified_mapper["config"] == {
        "attribute.value": ["true"],
        "user.model.attribute": ["emailVerified"],
    }


def test_masked_bind_credential_matches_without_exposing_secret() -> None:
    desired = {
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": {"enabled": ["true"], "bindCredential": ["new-secret"]},
    }
    existing = {
        **desired,
        "config": {"enabled": ["true"], "bindCredential": [MASKED_SECRET]},
    }

    assert not _component_needs_update(existing, desired)
    assert "new-secret" not in repr(_config(bind_credential="new-secret"))


def test_provider_create_accepts_only_masked_secret_in_readback() -> None:
    config = _config()
    desired = {
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": _provider_config(config),
    }
    created = {
        "id": "provider-id",
        **desired,
        "config": {**desired["config"], "bindCredential": [MASKED_SECRET]},
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [FakeResponse(201), FakeResponse(200, [created])]

    readback = _create_component(
        session,
        "https://keycloak.example.com/admin/realms/platform/components",
        desired,
    )

    assert readback == created


def test_provider_create_rejects_non_secret_masking_or_missing_config() -> None:
    config = _config()
    desired = {
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": _provider_config(config),
    }
    incomplete = {
        "id": "provider-id",
        **desired,
        "config": {
            **desired["config"],
            "bindCredential": [MASKED_SECRET],
            "trustEmail": [MASKED_SECRET],
        },
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [FakeResponse(201), FakeResponse(200, [incomplete])]

    with pytest.raises(ActiveDirectoryError, match="readback did not match"):
        _create_component(
            session,
            "https://keycloak.example.com/admin/realms/platform/components",
            desired,
        )


def test_preflight_uses_keycloak_26_ldap_test_actions() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [FakeResponse(204), FakeResponse(204)]

    _preflight_connection(
        session,
        "https://keycloak.example.com",
        "platform",
        _config(),
    )

    calls = session.request.call_args_list
    assert [item.kwargs["json"]["action"] for item in calls] == [
        "testConnection",
        "testAuthentication",
    ]
    assert all(item.kwargs["json"]["bindCredential"] == "do-not-log-this" for item in calls)
    assert all(
        item.args[1] == "https://keycloak.example.com/admin/realms/platform/testLDAPConnection"
        for item in calls
    )


def test_access_group_verification_reads_existing_group_hierarchy() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [{"id": "access-id", "name": "access"}]),
        FakeResponse(200, [{"id": "users-id", "name": "neurwerk-studio-users"}]),
    ]

    _verify_access_groups(
        session,
        "https://keycloak.example.com",
        "platform",
        ("neurwerk-studio-users",),
    )

    assert session.request.call_args_list[0].args[1].endswith("/admin/realms/platform/groups")
    assert (
        session.request.call_args_list[1]
        .args[1]
        .endswith("/admin/realms/platform/groups/access-id/children")
    )
    assert session.request.call_args_list[0].kwargs["params"] == {
        "briefRepresentation": "true",
        "populateHierarchy": "false",
        "max": "1000",
    }
    assert session.request.call_args_list[1].kwargs["params"] == {
        "search": "neurwerk-studio-users",
        "exact": "true",
        "briefRepresentation": "true",
        "max": "2",
    }


def test_access_group_verification_never_creates_missing_groups() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(200, [])

    with pytest.raises(ActiveDirectoryError, match="does not exist"):
        _verify_access_groups(
            session,
            "https://keycloak.example.com",
            "platform",
            ("neurwerk-studio-users",),
        )

    assert all(item.args[0] == "GET" for item in session.request.call_args_list)


def test_access_group_binding_verification_reads_non_brief_ldap_entry_dn() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [{"id": "access-id", "name": "access"}]),
        FakeResponse(
            200,
            [
                {
                    "id": "users-id",
                    "name": "neurwerk-studio-users",
                    "attributes": {
                        LDAP_ENTRY_DN_ATTRIBUTE: [
                            "cn=NEURWERK-STUDIO-USERS,ou=access,dc=EXAMPLE,dc=COM"
                        ]
                    },
                }
            ],
        ),
    ]

    _verify_access_group_bindings(
        session,
        "https://keycloak.example.com",
        "platform",
        _config(group_names=("neurwerk-studio-users",)),
    )

    assert session.request.call_args_list[1].kwargs["params"] == {
        "search": "neurwerk-studio-users",
        "exact": "true",
        "briefRepresentation": "false",
        "max": "2",
    }


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({}, "no verifiable LDAP entry DN"),
        ({LDAP_ENTRY_DN_ATTRIBUTE: []}, "no verifiable LDAP entry DN"),
        (
            {LDAP_ENTRY_DN_ATTRIBUTE: ["CN=other,OU=Access,DC=example,DC=com"]},
            "unexpected LDAP entry DN",
        ),
        (
            {LDAP_ENTRY_DN_ATTRIBUTE: ["CN=neurwerk-studio-users, OU=Access,DC=example,DC=com"]},
            "unexpected LDAP entry DN",
        ),
        (
            {
                LDAP_ENTRY_DN_ATTRIBUTE: [
                    "CN=neurwerk-studio-users,OU=Access,DC=example,DC=com",
                    "CN=other,OU=Access,DC=example,DC=com",
                ]
            },
            "no verifiable LDAP entry DN",
        ),
    ],
)
def test_access_group_binding_verification_fails_closed(
    attributes: dict[str, list[str]], message: str
) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [{"id": "access-id", "name": "access"}]),
        FakeResponse(
            200,
            [
                {
                    "id": "users-id",
                    "name": "neurwerk-studio-users",
                    "attributes": attributes,
                }
            ],
        ),
    ]

    with pytest.raises(ActiveDirectoryError, match=message):
        _verify_access_group_bindings(
            session,
            "https://keycloak.example.com",
            "platform",
            _config(group_names=("neurwerk-studio-users",)),
        )


def test_disabled_reconciliation_is_a_noop_when_provider_is_absent() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, {"id": "realm-id"}),
        FakeResponse(200, []),
    ]

    reconcile_active_directory("https://keycloak.example.com", "platform", session, None)

    assert session.request.call_count == 2


def test_disabled_reconciliation_preserves_provider_state_and_secret() -> None:
    provider = {
        "id": "provider-id",
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": {
            "enabled": ["true"],
            "bindCredential": [MASKED_SECRET],
            "lastSync": ["123"],
        },
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, {"id": "realm-id"}),
        FakeResponse(200, [provider]),
        FakeResponse(204),
        FakeResponse(
            200,
            [
                {
                    **provider,
                    "config": {**provider["config"], "enabled": ["false"]},
                }
            ],
        ),
    ]

    reconcile_active_directory("https://keycloak.example.com", "platform", session, None)

    update = session.request.call_args_list[2]
    assert update.args[:2] == (
        "PUT",
        "https://keycloak.example.com/admin/realms/platform/components/provider-id",
    )
    assert update.kwargs["json"]["config"] == {
        "enabled": ["false"],
        "bindCredential": [MASKED_SECRET],
        "lastSync": ["123"],
    }


def test_disabled_reconciliation_rejects_incomplete_readback() -> None:
    provider = {
        "id": "provider-id",
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": {"enabled": ["true"], "bindCredential": [MASKED_SECRET]},
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, {"id": "realm-id"}),
        FakeResponse(200, [provider]),
        FakeResponse(204),
        FakeResponse(200, [provider]),
    ]

    with pytest.raises(ActiveDirectoryError, match="readback did not match"):
        reconcile_active_directory("https://keycloak.example.com", "platform", session, None)


def test_mapper_create_verifies_complete_readback() -> None:
    desired = next(
        mapper
        for mapper in _mapper_representations("provider-id", _config())
        if mapper["name"] == "first name"
    )
    created = {"id": "first-name-id", **desired}
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, []),
        FakeResponse(201),
        FakeResponse(200, [created]),
    ]

    assert (
        _upsert_mapper(
            session,
            "https://keycloak.example.com/admin/realms/platform/components",
            "provider-id",
            desired,
        )
        == "first-name-id"
    )
    assert [item.args[0] for item in session.request.call_args_list] == [
        "GET",
        "POST",
        "GET",
    ]


def test_mapper_update_rejects_incomplete_readback() -> None:
    desired = next(
        mapper
        for mapper in _mapper_representations("provider-id", _config())
        if mapper["name"] == "email"
    )
    stale = {
        "id": "email-id",
        **desired,
        "config": {
            **desired["config"],
            "always.read.value.from.ldap": ["false"],
        },
    }
    incomplete = {"id": "email-id", **desired, "config": {}}
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [stale]),
        FakeResponse(204),
        FakeResponse(200, [incomplete]),
    ]

    with pytest.raises(ActiveDirectoryError, match="readback did not match"):
        _upsert_mapper(
            session,
            "https://keycloak.example.com/admin/realms/platform/components",
            "provider-id",
            desired,
        )


def test_conflicting_full_name_mapper_is_deleted_and_absence_verified() -> None:
    conflicting = {
        "id": "full-name-id",
        "name": "full name",
        "parentId": "provider-id",
        "providerId": "full-name-ldap-mapper",
        "providerType": LDAP_MAPPER_PROVIDER_TYPE,
        "config": {},
    }
    custom = {
        "id": "custom-id",
        "name": "full name",
        "parentId": "provider-id",
        "providerId": "custom-mapper",
        "providerType": LDAP_MAPPER_PROVIDER_TYPE,
        "config": {},
    }
    timestamp = {
        "id": "creation-date-id",
        "name": "creation date",
        "parentId": "provider-id",
        "providerId": "user-attribute-ldap-mapper",
        "providerType": LDAP_MAPPER_PROVIDER_TYPE,
        "config": {},
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [conflicting, custom, timestamp]),
        FakeResponse(204),
        FakeResponse(200, [custom, timestamp]),
    ]

    _remove_conflicting_full_name_mapper(
        session,
        "https://keycloak.example.com/admin/realms/platform/components",
        "provider-id",
    )

    delete = session.request.call_args_list[1]
    assert delete.args[:2] == (
        "DELETE",
        "https://keycloak.example.com/admin/realms/platform/components/full-name-id",
    )
    assert all("custom-id" not in item.args[1] for item in session.request.call_args_list)
    assert all("creation-date-id" not in item.args[1] for item in session.request.call_args_list)


def test_conflicting_full_name_mapper_delete_requires_absent_readback() -> None:
    conflicting = {
        "id": "full-name-id",
        "name": "full name",
        "parentId": "provider-id",
        "providerId": "full-name-ldap-mapper",
        "providerType": LDAP_MAPPER_PROVIDER_TYPE,
        "config": {},
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, [conflicting]),
        FakeResponse(204),
        FakeResponse(200, [conflicting]),
    ]

    with pytest.raises(ActiveDirectoryError, match="deletion was not persisted"):
        _remove_conflicting_full_name_mapper(
            session,
            "https://keycloak.example.com/admin/realms/platform/components",
            "provider-id",
        )


@patch("k8s_stack_tooling.api.keycloak_active_directory._verify_access_group_bindings")
@patch("k8s_stack_tooling.api.keycloak_active_directory._sync_group_mapper")
@patch("k8s_stack_tooling.api.keycloak_active_directory._upsert_mapper")
@patch("k8s_stack_tooling.api.keycloak_active_directory._remove_conflicting_full_name_mapper")
@patch("k8s_stack_tooling.api.keycloak_active_directory._preflight_connection")
@patch("k8s_stack_tooling.api.keycloak_active_directory._verify_access_groups")
def test_enabled_reconciliation_updates_masked_secret_and_syncs_groups(
    verify_access_groups: MagicMock,
    preflight_connection: MagicMock,
    remove_full_name_mapper: MagicMock,
    upsert_mapper: MagicMock,
    sync_group_mapper: MagicMock,
    verify_access_group_bindings: MagicMock,
) -> None:
    config = _config()
    provider = {
        "id": "provider-id",
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerId": "ldap",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "config": {
            **_provider_config(config),
            "bindCredential": [MASKED_SECRET],
        },
    }
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        FakeResponse(200, {"id": "realm-id"}),
        FakeResponse(200, [provider]),
        FakeResponse(204),
        FakeResponse(200, [provider]),
    ]
    upsert_mapper.side_effect = [
        "username-id",
        "first-name-id",
        "last-name-id",
        "email-id",
        "account-control-id",
        "group-mapper-id",
        "verified-email-id",
    ]

    reconcile_active_directory("https://keycloak.example.com", "platform", session, config)

    verify_access_groups.assert_called_once_with(
        session,
        "https://keycloak.example.com",
        "platform",
        ("neurwerk-studio-admins", "neurwerk-studio-users"),
    )
    preflight_connection.assert_called_once_with(
        session, "https://keycloak.example.com", "platform", config
    )
    update_body = session.request.call_args_list[2].kwargs["json"]
    assert update_body["config"]["bindCredential"] == ["do-not-log-this"]
    assert upsert_mapper.call_count == 7
    remove_full_name_mapper.assert_called_once_with(
        session,
        "https://keycloak.example.com/admin/realms/platform/components",
        "provider-id",
    )
    sync_group_mapper.assert_called_once_with(
        session,
        "https://keycloak.example.com",
        "platform",
        "provider-id",
        "group-mapper-id",
        2,
    )
    verify_access_group_bindings.assert_called_once_with(
        session,
        "https://keycloak.example.com",
        "platform",
        config,
    )


def test_group_sync_rejects_failed_entries() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(
        200, {"added": 1, "updated": 0, "removed": 0, "failed": 1}
    )

    with pytest.raises(ActiveDirectoryError, match="group sync failed"):
        _sync_group_mapper(
            session,
            "https://keycloak.example.com",
            "platform",
            "provider-id",
            "mapper-id",
            2,
        )

    session.request.assert_called_once_with(
        "POST",
        "https://keycloak.example.com/admin/realms/platform/user-storage/"
        "provider-id/mappers/mapper-id/sync",
        json=None,
        params={"direction": "fedToKeycloak"},
        timeout=30,
    )


def test_group_sync_requires_every_approved_group_to_be_processed() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(
        200, {"added": 0, "updated": 1, "removed": 0, "failed": 0}
    )

    with pytest.raises(ActiveDirectoryError, match="group sync failed"):
        _sync_group_mapper(
            session,
            "https://keycloak.example.com",
            "platform",
            "provider-id",
            "mapper-id",
            2,
        )


def test_group_sync_accepts_exact_approved_group_count() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(
        200, {"added": 1, "updated": 1, "removed": 0, "failed": 0}
    )

    _sync_group_mapper(
        session,
        "https://keycloak.example.com",
        "platform",
        "provider-id",
        "mapper-id",
        2,
    )


def test_failed_request_does_not_include_response_or_secret() -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(
        400, {"error": "bind credential do-not-log-this was rejected"}
    )

    with pytest.raises(ActiveDirectoryError) as error:
        _request_json(
            session,
            "POST",
            "https://keycloak.example.com/test",
            expected_statuses={204},
            json={"bindCredential": "do-not-log-this"},
        )

    assert "do-not-log-this" not in str(error.value)
    assert str(error.value) == "Keycloak POST request failed with status 400"
