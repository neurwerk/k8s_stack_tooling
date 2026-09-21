from __future__ import annotations

from copy import deepcopy
from itertools import count
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from k8s_stack_tooling.api.keycloak_active_directory import (
    LDAP_ENTRY_DN_ATTRIBUTE,
    LDAP_MAPPER_PROVIDER_TYPE,
    MANAGED_PROVIDER_SUBTYPE,
    MAPPING_NAME_PREFIX,
    MAPPING_OWNER_SUBTYPE,
    MAPPING_TRANSITION_SUBTYPE,
    MASKED_SECRET,
    ActiveDirectoryConfig,
    ActiveDirectoryError,
    GroupMapping,
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


@pytest.mark.parametrize(
    "source",
    [
        "",
        " CORP_USERS",
        "CORP_USERS ",
        "CORP\nUSERS",
        "a" * 65,
        "REPLACE_ME",
        "${GROUP}",
        "{{GROUP}}",
        "<GROUP>",
    ],
)
def test_active_directory_config_rejects_invalid_values(source: str) -> None:
    with pytest.raises(ValueError, match="username attribute"):
        _config(username_attribute="mail")
    with pytest.raises(ValueError, match="must be unique"):
        _config(group_names=("neurwerk-studio-users", "neurwerk-studio-users"))
    with pytest.raises(ValueError, match="emailVerified"):
        _config(email_verified=False)
    with pytest.raises(ValueError, match="sourceName"):
        GroupMapping(source, "/access/neurwerk-studio-users")
    with pytest.raises(ValueError, match="targetParent"):
        GroupMapping("CORP_USERS", "/access/custom")
    mapping = GroupMapping("CORP_USERS", "/access/neurwerk-studio-users")
    with pytest.raises(ValueError, match="exactly one"):
        _config(group_mappings=(mapping,))
    with pytest.raises(ValueError, match="exactly one"):
        _config(group_names=())
    with pytest.raises(ValueError, match="must be unique"):
        _config(
            group_names=(),
            group_mappings=(
                mapping,
                GroupMapping("corp_users", "/access/neurwerk-librechat-users"),
            ),
        )
    with pytest.raises(ValueError, match="must be unique"):
        _config(
            group_names=(),
            group_mappings=(mapping, GroupMapping("CORP_OTHER", mapping.target_parent)),
        )


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
        "ldap://corp.example:389",
    ],
)
def test_active_directory_config_requires_explicit_ldaps_636(
    connection_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"explicit ldaps://host:636"):
        _config(connection_url=connection_url)
    if connection_url == "ldap://corp.example:389":
        config = _config(connection_url=connection_url, allow_insecure_ldap=True)
        assert _provider_config(config)["startTls"] == ["false"]
    else:
        with pytest.raises(ValueError, match=r"explicit ldaps://host:636"):
            _config(connection_url=connection_url, allow_insecure_ldap=True)


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


@pytest.mark.parametrize("mapped", [False, True])
def test_provider_config_builds_exact_direct_membership_filter(mapped: bool) -> None:
    config = _config(group_names=("neurwerk-studio-users", "neurwerk-studio.admins"))
    if mapped:
        config = _config(
            group_names=(),
            group_mappings=(
                GroupMapping(r"CORP_Users, Ops+(A)*\B", "/access/neurwerk-studio-users"),
                GroupMapping("CORP_ADMINS", "/access/neurwerk-platform-admins"),
            ),
        )

    provider = _provider_config(config)

    expected = [
        "(|(memberOf=CN=neurwerk-studio-users,OU=Access,DC=example,DC=com)"
        "(memberOf=CN=neurwerk-studio.admins,OU=Access,DC=example,DC=com))"
    ]
    if mapped:
        expected = [
            r"(|(memberOf=CN=CORP_Users\5c, Ops\5c+\28A\29\2a\5c\5cB,"
            r"OU=Access,DC=example,DC=com)"
            r"(memberOf=CN=CORP_ADMINS,OU=Access,DC=example,DC=com))"
        ]
        mapper = _mapper_representations("provider-id", config)[-2]
        assert mapper["config"]["groups.ldap.filter"] == [
            r"(&(cn=CORP_Users, Ops+\28A\29\2a\5cB)"
            r"(distinguishedName=CN=CORP_Users\5c, Ops\5c+\28A\29\2a\5c\5cB,"
            r"OU=Access,DC=example,DC=com))"
        ]
        assert mapper["config"]["groups.path"] == ["/access/neurwerk-studio-users"]
        assert mapper["config"]["mapped.group.attributes"] == []
        assert mapper["config"]["user.roles.retrieve.strategy"] == [
            "LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"
        ]
    assert provider["customUserSearchFilter"] == expected
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
    assert _component_needs_update(existing, {**desired, "subType": MANAGED_PROVIDER_SUBTYPE})
    assert _component_needs_update(
        {**existing, "subType": MAPPING_TRANSITION_SUBTYPE},
        {**desired, "subType": MANAGED_PROVIDER_SUBTYPE},
    )
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

    assert [item.kwargs["json"] for item in session.request.call_args_list] == [
        {
            "action": action,
            "connectionUrl": "ldaps://directory.example.com:636",
            "bindDn": "CN=Keycloak,OU=Service Accounts,DC=example,DC=com",
            "bindCredential": "do-not-log-this",
            "authType": "simple",
            "startTls": "false",
            "useTruststoreSpi": "ldapsOnly",
            "connectionTimeout": "5000",
        }
        for action in ("testConnection", "testAuthentication")
    ]


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
@patch("k8s_stack_tooling.api.keycloak_active_directory._owned_group_mappers", return_value=[])
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
    owned_group_mappers: MagicMock,
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
        allow_missing=False,
    )
    verify_access_group_bindings.assert_called_once_with(
        session,
        "https://keycloak.example.com",
        "platform",
        config,
    )


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "fresh",
        "disabled",
        "missing-source",
        "partial-source",
        "disappearing-source",
        "sync-count",
        "sync-failed",
        "child-parent",
        "child-id",
        "child-path",
        "child-name",
        "missing-parent",
        "parent-changed",
        "create",
        "update",
        "mapper-readback",
        "provider-subtype",
        "provider-subtype-readback",
        "mapper-subtype",
        "mapper-subtype-readback",
        "delete",
        "delete-readback",
        "enable",
        "enable-readback",
        "enable-subtype-readback",
        "manual-overlap",
        "manual-relative",
        "manual-root",
        "reserved-collision",
        "foreign-component",
        "legacy-import",
    ],
)
def test_mapping_reconciliation_transitions_are_scoped_and_fail_closed(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _config(group_names=("neurwerk-studio-users", "neurwerk-librechat-users"))
    config = _config(
        group_names=(),
        group_mappings=(
            GroupMapping("CORP_USERS", "/access/neurwerk-studio-users"),
            GroupMapping("CORP_CHAT", "/access/neurwerk-librechat-users"),
        ),
    )
    provider: dict[str, Any] = {
        "id": "provider-id",
        "name": "microsoft-active-directory",
        "parentId": "realm-id",
        "providerType": "org.keycloak.storage.UserStorageProvider",
        "providerId": "ldap",
        "config": {**_provider_config(legacy), "bindCredential": [MASKED_SECRET]},
    }
    sequence = count()
    components: dict[str, dict[str, Any]] = {
        mapper["name"]: {"id": f"mapper-{next(sequence)}", **mapper}
        for mapper in _mapper_representations("provider-id", legacy)
    }
    components["email"]["config"]["read.only"] = ["false"]
    manual: dict[str, Any] = {
        "id": "manual-id",
        "name": "manual",
        "parentId": "provider-id",
        "providerType": LDAP_MAPPER_PROVIDER_TYPE,
        "providerId": "group-ldap-mapper",
        "config": {"groups.path": ["/other"]},
    }
    components["manual"] = deepcopy(manual)
    provider_exists = failure != "fresh"
    if failure == "fresh":
        components = {}
    if failure == "disabled":
        provider["config"]["enabled"] = ["false"]
    if failure == "provider-subtype":
        provider["subType"] = "native-subtype"
    if failure == "mapper-subtype":
        mapper = _mapper_representations("provider-id", config)[-1]
        components[mapper["name"]] = {"id": "native-mapper", **mapper, "subType": "native-subtype"}
    if failure == "manual-overlap":
        components["manual"]["config"]["groups.path"] = ["/access"]
    if failure == "manual-relative":
        components["manual"]["config"]["groups.path"] = ["access"]
    if failure == "manual-root":
        components["manual"]["config"]["groups.path"] = ["/"]
    if failure == "reserved-collision":
        components[MAPPING_NAME_PREFIX + "neurwerk-studio-users"] = {
            **manual,
            "name": MAPPING_NAME_PREFIX + "neurwerk-studio-users",
        }
    if failure == "legacy-import":
        components["approved groups"]["config"]["mode"] = ["IMPORT"]
    groups: dict[str, dict[str, Any]] = {
        "access-id": {"id": "access-id", "name": "access", "path": "/access"}
    }
    for name in legacy.group_names:
        groups[name] = {
            "id": name,
            "name": name,
            "path": f"/access/{name}",
            "parentId": "access-id",
            "attributes": {LDAP_ENTRY_DN_ATTRIBUTE: [f"CN={name},{legacy.groups_dn}"]},
        }
    if failure == "missing-parent":
        del groups["neurwerk-studio-users"]
    canonical = deepcopy(groups)
    synced = False
    activation_failed = False

    # Keycloak 26.7.2 only exposes/updates registered config properties, not arbitrary markers.
    config_keys = {
        "ldap": set(
            """
            allowKerberosAuthentication authType batchSizeForSync bindCredential bindDn cachePolicy
            changedSyncPeriod connectionPooling connectionUrl connectionTimeout readTimeout
            customUserSearchFilter debug editMode
            enabled fullSyncPeriod importEnabled pagination priority rdnLDAPAttribute searchScope
            startTls syncRegistrations trustEmail useKerberosForPasswordAuthentication
            useTruststoreSpi userObjectClasses usernameLDAPAttribute usersDn uuidLDAPAttribute
            validatePasswordPolicy vendor
        """.split()
        ),
        "group-ldap-mapper": set(
            """
            drop.non.existing.groups.during.sync group.name.ldap.attribute group.object.classes
            groups.dn groups.ldap.filter groups.path ignore.missing.groups mapped.group.attributes
            membership.attribute.type membership.ldap.attribute membership.user.ldap.attribute
            memberof.ldap.attribute mode preserve.group.inheritance user.roles.retrieve.strategy
        """.split()
        ),
        "user-attribute-ldap-mapper": set(
            """
            always.read.value.from.ldap is.mandatory.in.ldap ldap.attribute read.only
            user.model.attribute
        """.split()
        ),
        "msad-user-account-control-mapper": {
            "always.read.enabled.value.from.ldap",
            "ldap.password.policy.hints.enabled",
        },
        "hardcoded-attribute-mapper": {"attribute.value", "user.model.attribute"},
    }

    def persist(item: dict[str, Any], representation: dict[str, Any], *, create: bool) -> None:
        for key in ("name", "parentId", "providerId", "providerType", "subType"):
            if representation.get(key) is not None:
                item[key] = representation[key]
        settings = item.setdefault("config", {})
        for key, values in representation.get("config", {}).items():
            if not create and key not in config_keys[item["providerId"]]:
                continue
            if not values or values[0] is None or not values[0].strip():
                settings.pop(key, None)
                continue
            values = [
                value
                for value in values
                if value and value.strip() and (create or value != MASKED_SECRET)
            ]
            if values:
                settings[key] = values

    def readback(item: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(item)
        result["config"] = {
            key: value
            for key, value in result["config"].items()
            if key in config_keys[item["providerId"]]
        }
        if "bindCredential" in result["config"]:
            result["config"]["bindCredential"] = [MASKED_SECRET]
        return result

    probe: dict[str, Any] = {}
    persist(
        probe,
        {"providerId": "ldap", "subType": "native-subtype", "config": {"unknown": ["stored"]}},
        create=True,
    )
    persist(probe, {"subType": None, "config": {"unknown": ["ignored"]}}, create=False)
    assert probe["config"]["unknown"] == ["stored"]
    assert readback(probe) == {"providerId": "ldap", "subType": "native-subtype", "config": {}}

    def request(method: str, url: str, *, json: Any, params: Any, timeout: int) -> FakeResponse:
        nonlocal synced, activation_failed, provider_exists
        assert timeout == 30
        if url.endswith("/realms/platform"):
            return FakeResponse(200, {"id": "realm-id"})
        if url.endswith("/testLDAPConnection"):
            return FakeResponse(204)
        if url.endswith("/components") and method == "GET":
            if params["type"] == provider["providerType"]:
                if not provider_exists:
                    return FakeResponse(200, [])
                actual = readback(provider)
                if (
                    failure in {"enable-readback", "enable-subtype-readback"}
                    and provider["config"]["enabled"] == ["true"]
                    and synced
                ):
                    activation_failed = True
                    if failure == "enable-readback":
                        actual["config"]["enabled"] = ["false"]
                    else:
                        actual["subType"] = MAPPING_TRANSITION_SUBTYPE
                if failure == "provider-subtype-readback":
                    actual.pop("subType", None)
                return FakeResponse(200, [actual])
            result = [
                readback(item)
                for item in components.values()
                if "name" not in params or item["name"] == params["name"]
            ]
            if (
                failure == "legacy-retry"
                and not result
                and params.get("name", "").startswith(MAPPING_NAME_PREFIX)
            ):
                raise requests.ConnectionError("lost deletion readback")
            if failure == "foreign-component" and result:
                result[0]["parentId"] = "other-provider"
            if failure == "mapper-readback":
                for item in result:
                    if item.get("subType") == MAPPING_OWNER_SUBTYPE:
                        item["config"]["groups.path"] = ["/other"]
            if failure == "mapper-subtype-readback":
                for item in result:
                    item.pop("subType", None)
            return FakeResponse(200, result)
        if url.endswith("/components") and method == "POST" and json["providerId"] == "ldap":
            assert not provider_exists
            provider_exists = True
            persist(provider, json, create=True)
            return FakeResponse(201)
        if url.endswith("/components/provider-id") and method == "PUT":
            if json["config"]["enabled"] == ["true"] and failure == "enable":
                activation_failed = True
                return FakeResponse(500)
            persist(provider, json, create=False)
            return FakeResponse(204)
        if "/components" in url and method in {"POST", "PUT", "DELETE"}:
            assert provider["config"]["enabled"] == ["false"]
            if method == "DELETE":
                name = next(
                    name for name, item in components.items() if url.endswith("/" + item["id"])
                )
                assert name != "manual"
                if failure == "delete":
                    return FakeResponse(500)
                if failure != "delete-readback":
                    del components[name]
                return FakeResponse(204)
            assert json["parentId"] == "provider-id"
            assert json["name"] != "manual"
            if failure == "create" and json["name"].endswith("neurwerk-librechat-users"):
                return FakeResponse(500)
            if failure == "update" and method == "PUT":
                return FakeResponse(500)
            item = (
                components[json["name"]] if method == "PUT" else {"id": f"mapper-{next(sequence)}"}
            )
            persist(item, json, create=method == "POST")
            components[item["name"]] = item
            return FakeResponse(201 if method == "POST" else 204)
        if url.endswith("/sync"):
            assert provider["config"]["enabled"] == ["false"]
            mapper = next(item for item in components.values() if f"/{item['id']}/sync" in url)
            settings = mapper["config"]
            assert settings["mode"] == ["READ_ONLY"]
            assert settings["drop.non.existing.groups.during.sync"] == ["false"]
            assert settings["preserve.group.inheritance"] == ["false"]
            if mapper["name"] == "approved groups":
                return FakeResponse(200, {"added": 0, "updated": 2, "removed": 0, "failed": 0})
            mapping = next(
                item
                for item in config.group_mappings
                if [item.target_parent] == settings["groups.path"]
            )
            if failure == "missing-source" or (
                failure == "partial-source" and mapping.source_name == "CORP_CHAT"
            ):
                synced = True
                return FakeResponse(200, {"added": 0, "updated": 0, "removed": 0, "failed": 0})
            child_id = f"child-{mapping.source_name}"
            groups[child_id] = {
                "id": child_id,
                "name": mapping.source_name,
                "path": f"{mapping.target_parent}/{mapping.source_name}",
                "parentId": mapping.target_parent.removeprefix("/access/"),
            }
            if failure in {"child-id", "child-path", "child-name"}:
                groups[child_id][failure.removeprefix("child-")] = "wrong"
            if failure == "child-parent":
                groups[child_id]["parentId"] = "access-id"
            synced = True
            return FakeResponse(
                200,
                {
                    "added": 1,
                    "updated": 1 if failure == "sync-count" else 0,
                    "removed": 0,
                    "failed": 1 if failure == "sync-failed" else 0,
                },
            )
        if "/groups" in url and method == "GET":
            if url.endswith("/groups"):
                return FakeResponse(200, [deepcopy(groups["access-id"])])
            if url.endswith("/children"):
                parent_id = url.split("/")[-2]
                result = [
                    deepcopy(item)
                    for item in groups.values()
                    if item.get("parentId") == parent_id and item["name"] == params["search"]
                ]
                if failure == "parent-changed" and synced and parent_id == "access-id" and result:
                    result[0]["id"] = "changed-id"
                return FakeResponse(200, result)
            child = groups.get(url.split("/")[-1])
            return FakeResponse(200 if child else 404, deepcopy(child))
        raise AssertionError((method, url))

    session = MagicMock(spec=requests.Session)
    session.request.side_effect = request
    if failure not in {
        "none",
        "fresh",
        "disabled",
        "missing-source",
        "partial-source",
        "disappearing-source",
    }:
        with pytest.raises(ActiveDirectoryError):
            reconcile_active_directory("https://corp.example", "platform", session, config)
        preflight_failure = failure in {
            "manual-overlap",
            "manual-relative",
            "manual-root",
            "reserved-collision",
            "foreign-component",
            "legacy-import",
            "missing-parent",
            "provider-subtype",
            "mapper-subtype",
        }
        assert provider["config"]["enabled"] == (["true"] if preflight_failure else ["false"])
        if failure.startswith("enable"):
            assert activation_failed
        if failure == "provider-subtype":
            assert provider["subType"] == "native-subtype"
        if failure == "mapper-subtype":
            assert components[mapper["name"]]["subType"] == "native-subtype"
        if preflight_failure:
            assert all(
                call.args[0] == "GET" or call.args[1].endswith("/testLDAPConnection")
                for call in session.request.call_args_list
            )
        if not preflight_failure:
            failure = "none"
            reconcile_active_directory("https://corp.example", "platform", session, config)
            assert provider["config"]["enabled"] == ["true"]
        return

    reconcile_active_directory("https://corp.example", "platform", session, config)
    assert provider["config"]["enabled"] == ["true"]
    assert provider["subType"] == MANAGED_PROVIDER_SUBTYPE
    assert "approved groups" not in components
    first_ids = {name: item["id"] for name, item in components.items()}
    if failure in {"missing-source", "partial-source"}:
        missing = {"CORP_USERS", "CORP_CHAT"} if failure == "missing-source" else {"CORP_CHAT"}
        for source in missing:
            assert f"child-{source}" not in groups
            assert f"Active Directory group {source!r} was not found" in caplog.text
        if failure == "partial-source":
            assert "child-CORP_USERS" in groups
            assert "group 'CORP_USERS' was not found" not in caplog.text
        assert len(caplog.records) == len(missing)
        assert config.bind_credential not in caplog.text
        assert config.bind_dn not in caplog.text
        assert (
            provider["config"]["customUserSearchFilter"]
            == _provider_config(config)["customUserSearchFilter"]
        )
        # Creating the source groups later must converge without replacing the mappers.
        failure = "none"
        caplog.clear()
    reconcile_active_directory("https://corp.example", "platform", session, config)
    assert {name: item["id"] for name, item in components.items()} == first_ids
    assert "child-CORP_CHAT" in groups
    assert groups["child-CORP_USERS"].get("attributes") is None
    assert not caplog.records
    if failure == "disappearing-source":
        before = deepcopy(groups)
        failure = "missing-source"
        reconcile_active_directory("https://corp.example", "platform", session, config)
        assert provider["config"]["enabled"] == ["true"]
        assert provider["config"]["cachePolicy"] == ["NO_CACHE"]
        assert (
            provider["config"]["customUserSearchFilter"]
            == _provider_config(config)["customUserSearchFilter"]
        )
        # READ_ONLY mappers query current LDAP membership; do not copy or delete local grants.
        assert groups == before
        for item in components.values():
            if item.get("subType") == MAPPING_OWNER_SUBTYPE:
                assert item["config"]["mode"] == ["READ_ONLY"]
                assert item["config"]["user.roles.retrieve.strategy"] == [
                    "LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"
                ]
        assert len(caplog.records) == 2
        failure = "none"
    config = _config(
        group_names=(),
        group_mappings=(GroupMapping("CORP_EDITORS", "/access/neurwerk-studio-users"),),
    )
    reconcile_active_directory("https://corp.example", "platform", session, config)
    assert MAPPING_NAME_PREFIX + "neurwerk-librechat-users" not in components
    remaining = components[MAPPING_NAME_PREFIX + "neurwerk-studio-users"]
    assert "CORP_EDITORS" in remaining["config"]["groups.ldap.filter"][0]
    assert "CORP_USERS" not in provider["config"]["customUserSearchFilter"][0]
    assert "child-CORP_USERS" in groups and "child-CORP_CHAT" in groups
    config = _config(
        group_names=(),
        group_mappings=(GroupMapping("CORP_EDITORS", "/access/neurwerk-librechat-users"),),
    )
    reconcile_active_directory("https://corp.example", "platform", session, config)
    assert MAPPING_NAME_PREFIX + "neurwerk-studio-users" not in components
    assert MAPPING_NAME_PREFIX + "neurwerk-librechat-users" in components
    had_manual = "manual" in components
    failure = "legacy-retry"
    with pytest.raises(requests.ConnectionError):
        reconcile_active_directory("https://corp.example", "platform", session, legacy)
    assert provider["config"]["enabled"] == ["false"]
    assert provider["subType"] == MAPPING_TRANSITION_SUBTYPE
    assert not any(item.get("subType") == MAPPING_OWNER_SUBTYPE for item in components.values())
    failure = "none"
    reconcile_active_directory("https://corp.example", "platform", session, legacy)
    assert "approved groups" in components
    assert not any(item.get("subType") == MAPPING_OWNER_SUBTYPE for item in components.values())
    assert provider["config"]["enabled"] == ["true"]
    assert provider["subType"] == MANAGED_PROVIDER_SUBTYPE
    if had_manual:
        assert components["manual"] == manual
    assert {key: groups[key] for key in canonical} == canonical


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


@pytest.mark.parametrize("expected,processed", [(2, 1), (1, 0)])
def test_group_sync_requires_every_approved_group_to_be_processed(
    expected: int, processed: int
) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(
        200, {"added": 0, "updated": processed, "removed": 0, "failed": 0}
    )

    with pytest.raises(ActiveDirectoryError, match="group sync failed"):
        _sync_group_mapper(
            session,
            "https://keycloak.example.com",
            "platform",
            "provider-id",
            "mapper-id",
            expected,
        )


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"added": 0, "updated": 0, "removed": 0, "failed": 0}, False),
        ({"added": 1, "updated": 0, "removed": 0, "failed": 0}, True),
        ({"added": 0, "updated": 1, "removed": 0, "failed": 0}, True),
        ({"added": 0, "updated": 0, "removed": 0, "failed": 1}, None),
        ({"added": 0, "updated": 0, "removed": 1, "failed": 0}, None),
        ({"added": 2, "updated": 0, "removed": 0, "failed": 0}, None),
        ({"added": 0, "updated": True, "removed": 0, "failed": 0}, None),
        ({"added": 0, "updated": 0, "removed": -1, "failed": 0}, None),
        ({"added": 0, "updated": 0, "removed": 0}, None),
    ],
)
def test_missing_group_tolerance_preserves_sync_failure_checks(
    result: dict[str, int], expected: bool | None
) -> None:
    session = MagicMock(spec=requests.Session)
    session.request.return_value = FakeResponse(200, result)

    def sync() -> bool:
        return _sync_group_mapper(
            session,
            "https://keycloak.example.com",
            "platform",
            "provider-id",
            "mapper-id",
            1,
            allow_missing=True,
        )

    if expected is None:
        with pytest.raises(ActiveDirectoryError):
            sync()
    else:
        assert sync() is expected


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
