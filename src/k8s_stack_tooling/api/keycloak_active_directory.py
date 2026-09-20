from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests

MANAGED_PROVIDER_NAME = "microsoft-active-directory"
USER_STORAGE_PROVIDER_TYPE = "org.keycloak.storage.UserStorageProvider"
LDAP_MAPPER_PROVIDER_TYPE = "org.keycloak.storage.ldap.mappers.LDAPStorageMapper"
MASKED_SECRET = "**********"
REQUEST_TIMEOUT_SECONDS = 30
MAX_APPROVED_GROUP_NAME_LENGTH = 64
APPROVED_GROUP_NAME_PATTERN = re.compile(r"^neurwerk-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
CONFLICTING_FULL_NAME_MAPPER = "full name"
FULL_NAME_MAPPER_PROVIDER_ID = "full-name-ldap-mapper"
LDAP_ENTRY_DN_ATTRIBUTE = "LDAP_ENTRY_DN"
MAPPING_OWNER_SUBTYPE = "k8s-stack-tooling.group-mapping.v1"
MAPPING_TRANSITION_SUBTYPE = "k8s-stack-tooling.group-reconciliation-pending.v1"
MANAGED_PROVIDER_SUBTYPE = "k8s-stack-tooling.active-directory.v1"
MAPPING_NAME_PREFIX = "approved group: "
CANONICAL_GROUP_PATHS = frozenset(
    f"/access/neurwerk-{name}"
    for name in (
        "api-key-admins",
        "dify-admins",
        "dify-users",
        "keycloak-admins",
        "langfuse-admins",
        "librechat-admins",
        "librechat-users",
        "opensearch-admins",
        "pii-admins",
        "platform-admins",
        "studio-users",
        "llm-all-users",
        "mcp-all-users",
        "forgejo-users",
        "forgejo-admins",
    )
)


@dataclass(frozen=True)
class GroupMapping:
    source_name: str
    target_parent: str

    def __post_init__(self) -> None:
        name = self.source_name
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 64
            or name != name.strip()
            or _contains_control_character(name)
            or any(token in name.lower() for token in ("replace", "placeholder", "changeme"))
            or "${" in name
            or "{{" in name
            or "<" in name
            or ">" in name
        ):
            raise ValueError("mapping sourceName must be a real AD CN of 1-64 characters")
        if self.target_parent not in CANONICAL_GROUP_PATHS:
            raise ValueError("mapping targetParent must be an exact canonical /access group path")


class ActiveDirectoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveDirectoryConfig:
    connection_url: str
    users_dn: str
    groups_dn: str
    username_attribute: str
    group_names: tuple[str, ...]
    bind_dn: str
    bind_credential: str = field(repr=False)
    email_verified: bool = True
    group_mappings: tuple[GroupMapping, ...] = ()
    allow_insecure_ldap: bool = False
    connection_timeout_ms: int = 5000
    read_timeout_ms: int = 10000

    def __post_init__(self) -> None:
        _validate_config(self)


def _validate_config(config: ActiveDirectoryConfig) -> None:
    parsed_url = None
    try:
        parsed_url = urlsplit(config.connection_url)
        hostname = parsed_url.hostname
        port = parsed_url.port
    except ValueError:
        hostname = None
        port = None
    if (
        parsed_url is None
        or (parsed_url.scheme, port)
        not in (
            {("ldaps", 636), ("ldap", 389)}
            if config.allow_insecure_ldap is True
            else {("ldaps", 636)}
        )
        or not hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or bool(parsed_url.path)
        or bool(parsed_url.query)
        or bool(parsed_url.fragment)
        or "?" in config.connection_url
        or "#" in config.connection_url
        or config.connection_url != config.connection_url.strip()
        or any(character.isspace() for character in config.connection_url)
        or _contains_control_character(config.connection_url)
    ):
        raise ValueError(
            "connection URL must be an explicit ldaps://host:636 URL (or ldap://host:389 "
            "with allowInsecureLdap true) without credentials, path, query, or fragment"
        )
    if type(config.allow_insecure_ldap) is not bool:
        raise ValueError("allowInsecureLdap must be a boolean")
    for name, value in (
        ("connection timeout", config.connection_timeout_ms),
        ("read timeout", config.read_timeout_ms),
    ):
        if type(value) is not int or not 1 <= value <= 10000:
            raise ValueError(f"{name} must be an integer from 1 to 10000 milliseconds")

    for label, value in (("users DN", config.users_dn), ("groups DN", config.groups_dn)):
        if (
            not value.strip()
            or value != value.strip()
            or "=" not in value
            or _contains_control_character(value)
        ):
            raise ValueError(f"{label} must be a non-empty distinguished name")

    bind_principal = config.bind_dn
    upn_parts = bind_principal.split("@")
    is_dn_like = "=" in bind_principal
    is_upn_like = (
        len(upn_parts) == 2
        and all(upn_parts)
        and not any(character.isspace() for character in bind_principal)
    )
    if (
        not bind_principal.strip()
        or bind_principal != bind_principal.strip()
        or _contains_control_character(bind_principal)
        or not (is_dn_like or is_upn_like)
    ):
        raise ValueError("bind DN must be a DN-like or UPN-like Active Directory principal")

    if config.username_attribute not in {"sAMAccountName", "userPrincipalName"}:
        raise ValueError("username attribute must be sAMAccountName or userPrincipalName")
    if bool(config.group_names) == bool(config.group_mappings):
        raise ValueError("exactly one nonempty groupNames or groupMappings list is required")
    if len({mapping.source_name.casefold() for mapping in config.group_mappings}) != len(
        config.group_mappings
    ) or len({mapping.target_parent for mapping in config.group_mappings}) != len(
        config.group_mappings
    ):
        raise ValueError("mapping sources (case-insensitive) and targets must be unique")
    if len(set(config.group_names)) != len(config.group_names):
        raise ValueError("Active Directory group names must be unique")
    if any(
        len(name) > MAX_APPROVED_GROUP_NAME_LENGTH
        or APPROVED_GROUP_NAME_PATTERN.fullmatch(name) is None
        for name in config.group_names
    ):
        raise ValueError(
            "Active Directory group names must match "
            "^neurwerk-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$ and be at most 64 characters"
        )
    if not config.bind_credential.strip():
        raise ValueError("bind credential must not be empty")
    if not config.email_verified:
        raise ValueError("emailVerified must be true for Active Directory users")


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _escape_ldap_filter(value: str) -> str:
    return "".join(
        {
            "\\": r"\5c",
            "*": r"\2a",
            "(": r"\28",
            ")": r"\29",
            "\x00": r"\00",
        }.get(character, character)
        for character in value
    )


def _escape_dn_value(value: str) -> str:
    escaped = value.replace("\\", r"\\").replace(",", r"\,")
    escaped = escaped.replace("+", r"\+").replace('"', r"\"")
    escaped = escaped.replace("<", r"\<").replace(">", r"\>")
    escaped = escaped.replace(";", r"\;").replace("=", r"\=")
    if escaped.startswith(" ") or escaped.startswith("#"):
        escaped = f"\\{escaped}"
    if escaped.endswith(" "):
        escaped = f"{escaped[:-1]}\\ "
    return escaped


def _group_dns(config: ActiveDirectoryConfig) -> tuple[str, ...]:
    names = tuple(mapping.source_name for mapping in config.group_mappings) or config.group_names
    return tuple(f"CN={_escape_dn_value(group_name)},{config.groups_dn}" for group_name in names)


def _or_filter(attribute: str, values: tuple[str, ...]) -> str:
    clauses = "".join(f"({attribute}={_escape_ldap_filter(value)})" for value in values)
    return clauses if len(values) == 1 else f"(|{clauses})"


def _provider_config(config: ActiveDirectoryConfig) -> dict[str, list[str]]:
    return {
        "allowKerberosAuthentication": ["false"],
        "authType": ["simple"],
        "batchSizeForSync": ["1000"],
        "bindCredential": [config.bind_credential],
        "bindDn": [config.bind_dn],
        "cachePolicy": ["NO_CACHE"],
        "changedSyncPeriod": ["-1"],
        "connectionPooling": ["true"],
        "connectionUrl": [config.connection_url],
        "connectionTimeout": [str(config.connection_timeout_ms)],
        "readTimeout": [str(config.read_timeout_ms)],
        "customUserSearchFilter": [_or_filter("memberOf", _group_dns(config))],
        "debug": ["false"],
        "editMode": ["READ_ONLY"],
        "enabled": ["true"],
        "fullSyncPeriod": ["-1"],
        "importEnabled": ["true"],
        "pagination": ["true"],
        "priority": ["0"],
        "rdnLDAPAttribute": ["cn"],
        "searchScope": ["2"],
        "startTls": ["false"],
        "syncRegistrations": ["false"],
        "trustEmail": ["true"],
        "useKerberosForPasswordAuthentication": ["false"],
        "useTruststoreSpi": ["ldapsOnly"],
        "userObjectClasses": ["person, organizationalPerson, user"],
        "usernameLDAPAttribute": [config.username_attribute],
        "usersDn": [config.users_dn],
        "uuidLDAPAttribute": ["objectGUID"],
        "validatePasswordPolicy": ["false"],
        "vendor": ["ad"],
    }


def _mapper_representations(
    provider_id: str, config: ActiveDirectoryConfig
) -> tuple[dict[str, Any], ...]:
    attribute_mappers = (
        ("username", "username", config.username_attribute, "true", "false"),
        ("first name", "firstName", "givenName", "false", "true"),
        ("last name", "lastName", "sn", "false", "true"),
        ("email", "email", "mail", "false", "true"),
    )
    mappers: list[dict[str, Any]] = [
        {
            "name": name,
            "parentId": provider_id,
            "providerId": "user-attribute-ldap-mapper",
            "providerType": LDAP_MAPPER_PROVIDER_TYPE,
            "config": {
                "always.read.value.from.ldap": [always_read],
                "is.mandatory.in.ldap": [mandatory],
                "ldap.attribute": [ldap_attribute],
                "read.only": ["true"],
                "user.model.attribute": [model_attribute],
            },
        }
        for name, model_attribute, ldap_attribute, mandatory, always_read in attribute_mappers
    ]
    mappers.extend(
        (
            {
                "name": "MSAD account controls",
                "parentId": provider_id,
                "providerId": "msad-user-account-control-mapper",
                "providerType": LDAP_MAPPER_PROVIDER_TYPE,
                "config": {
                    "always.read.enabled.value.from.ldap": ["true"],
                    "ldap.password.policy.hints.enabled": ["false"],
                },
            },
            {
                "name": "approved groups",
                "parentId": provider_id,
                "providerId": "group-ldap-mapper",
                "providerType": LDAP_MAPPER_PROVIDER_TYPE,
                "config": {
                    "drop.non.existing.groups.during.sync": ["false"],
                    "group.name.ldap.attribute": ["cn"],
                    "group.object.classes": ["group"],
                    "groups.dn": [config.groups_dn],
                    "groups.ldap.filter": [_or_filter("cn", tuple(config.group_names))],
                    "groups.path": ["/access"],
                    "ignore.missing.groups": ["false"],
                    "mapped.group.attributes": [LDAP_ENTRY_DN_ATTRIBUTE],
                    "membership.attribute.type": ["DN"],
                    "membership.ldap.attribute": ["member"],
                    "membership.user.ldap.attribute": [config.username_attribute],
                    "memberof.ldap.attribute": ["memberOf"],
                    "mode": ["READ_ONLY"],
                    "preserve.group.inheritance": ["false"],
                    "user.roles.retrieve.strategy": ["GET_GROUPS_FROM_USER_MEMBEROF_ATTRIBUTE"],
                },
            },
            {
                # This maps a Keycloak user-model property. The similarly named
                # hardcoded-ldap mapper writes an attribute back to LDAP instead.
                "name": "verified email",
                "parentId": provider_id,
                "providerId": "hardcoded-attribute-mapper",
                "providerType": LDAP_MAPPER_PROVIDER_TYPE,
                "config": {
                    "attribute.value": ["true"],
                    "user.model.attribute": ["emailVerified"],
                },
            },
        )
    )
    if config.group_mappings:
        legacy = next(mapper for mapper in mappers if mapper["name"] == "approved groups")
        mappers.remove(legacy)
        for mapping, source_dn in zip(config.group_mappings, _group_dns(config), strict=True):
            mappers.append(
                {
                    **legacy,
                    "name": MAPPING_NAME_PREFIX + mapping.target_parent.removeprefix("/access/"),
                    "subType": MAPPING_OWNER_SUBTYPE,
                    "config": {
                        **legacy["config"],
                        "groups.path": [mapping.target_parent],
                        "groups.ldap.filter": [
                            f"(&(cn={_escape_ldap_filter(mapping.source_name)})"
                            f"(distinguishedName={_escape_ldap_filter(source_dn)}))"
                        ],
                        "mapped.group.attributes": [],
                        # Unlike memberOf's CN lookup, this cannot confuse equal CNs in other OUs.
                        "user.roles.retrieve.strategy": ["LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"],
                    },
                }
            )
    return tuple(mappers)


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_statuses: set[int],
    json: Any = None,
    params: dict[str, str] | None = None,
) -> Any:
    response = session.request(
        method,
        url,
        json=json,
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code not in expected_statuses:
        raise ActiveDirectoryError(
            f"Keycloak {method} request failed with status {response.status_code}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ActiveDirectoryError("Keycloak returned an invalid JSON response") from exc


def _component_url(keycloak_url: str, realm_name: str) -> str:
    realm = quote(realm_name, safe="")
    return f"{keycloak_url.rstrip('/')}/admin/realms/{realm}/components"


def _get_components(
    session: requests.Session,
    components_url: str,
    *,
    name: str | None,
    parent: str,
    component_type: str,
) -> list[dict[str, Any]]:
    payload = _request_json(
        session,
        "GET",
        components_url,
        expected_statuses={200},
        params={"parent": parent, "type": component_type, **({"name": name} if name else {})},
    )
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ActiveDirectoryError("Keycloak returned invalid component data")
    if any(
        item.get("parentId") != parent or item.get("providerType") != component_type
        for item in payload
    ):
        raise ActiveDirectoryError("Keycloak returned components outside the requested scope")
    return payload


def _find_managed_component(components: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matching = [component for component in components if component.get("name") == name]
    if len(matching) > 1:
        raise ActiveDirectoryError(f"multiple managed Keycloak components named {name!r}")
    return matching[0] if matching else None


def _component_needs_update(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
    for key in ("name", "parentId", "providerId", "providerType", "subType"):
        if existing.get(key) != desired.get(key):
            return True

    existing_config = existing.get("config")
    desired_config = desired.get("config")
    if not isinstance(existing_config, dict) or not isinstance(desired_config, dict):
        return True
    for key, value in desired_config.items():
        if key == "bindCredential" and existing_config.get(key) == [MASKED_SECRET]:
            continue
        if existing_config.get(key, [] if value == [] else None) != value:
            return True
    return False


def _verify_component_readback(
    session: requests.Session,
    components_url: str,
    desired: dict[str, Any],
    *,
    expected_id: str | None = None,
) -> dict[str, Any]:
    name = desired.get("name")
    parent_id = desired.get("parentId")
    component_type = desired.get("providerType")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(parent_id, str)
        or not parent_id
        or not isinstance(component_type, str)
        or not component_type
    ):
        raise ActiveDirectoryError("desired Keycloak component identity is invalid")
    actual = _find_managed_component(
        _get_components(
            session,
            components_url,
            name=name,
            parent=parent_id,
            component_type=component_type,
        ),
        name,
    )
    if actual is None:
        raise ActiveDirectoryError(f"Keycloak component {name!r} readback is missing")
    actual_id = actual.get("id")
    if not isinstance(actual_id, str) or not actual_id:
        raise ActiveDirectoryError(f"Keycloak component {name!r} readback is incomplete")
    if expected_id is not None and actual_id != expected_id:
        raise ActiveDirectoryError(f"Keycloak component {name!r} readback has an unexpected ID")
    for key in ("name", "parentId", "providerId", "providerType", "subType"):
        if actual.get(key) != desired.get(key):
            raise ActiveDirectoryError(
                f"Keycloak component {name!r} readback did not match desired state"
            )
    actual_config = actual.get("config")
    desired_config = desired.get("config")
    if not isinstance(actual_config, dict) or not isinstance(desired_config, dict):
        raise ActiveDirectoryError(f"Keycloak component {name!r} readback is incomplete")
    for key, desired_value in desired_config.items():
        actual_value = actual_config.get(key, [] if desired_value == [] else None)
        if key == "bindCredential" and actual_value == [MASKED_SECRET]:
            continue
        if actual_value != desired_value:
            raise ActiveDirectoryError(
                f"Keycloak component {name!r} readback did not match desired state"
            )
    return actual


def _create_component(
    session: requests.Session, components_url: str, representation: dict[str, Any]
) -> dict[str, Any]:
    _request_json(
        session,
        "POST",
        components_url,
        expected_statuses={201},
        json=representation,
    )
    return _verify_component_readback(session, components_url, representation)


def _update_component(
    session: requests.Session,
    components_url: str,
    component_id: str,
    representation: dict[str, Any],
) -> dict[str, Any]:
    _request_json(
        session,
        "PUT",
        f"{components_url}/{quote(component_id, safe='')}",
        expected_statuses={204},
        json={"id": component_id, **representation},
    )
    return _verify_component_readback(
        session,
        components_url,
        representation,
        expected_id=component_id,
    )


def _upsert_mapper(
    session: requests.Session,
    components_url: str,
    provider_id: str,
    desired: dict[str, Any],
) -> str:
    name = str(desired["name"])
    existing = _find_managed_component(
        _get_components(
            session,
            components_url,
            name=name,
            parent=provider_id,
            component_type=LDAP_MAPPER_PROVIDER_TYPE,
        ),
        name,
    )
    if existing is None:
        existing = _create_component(session, components_url, desired)
    else:
        if existing.get("providerId") != desired["providerId"] or existing.get(
            "subType"
        ) != desired.get("subType"):
            raise ActiveDirectoryError(f"unmanaged mapper conflicts with reserved name {name!r}")
        if _component_needs_update(existing, desired):
            component_id = existing.get("id")
            if not isinstance(component_id, str) or not component_id:
                raise ActiveDirectoryError(f"managed mapper {name!r} has no component ID")
            existing = _update_component(session, components_url, component_id, desired)

    component_id = existing.get("id")
    if not isinstance(component_id, str) or not component_id:
        raise ActiveDirectoryError(f"managed mapper {name!r} has no component ID")
    return component_id


def _remove_conflicting_full_name_mapper(
    session: requests.Session,
    components_url: str,
    provider_id: str,
) -> None:
    components = _get_components(
        session,
        components_url,
        name=CONFLICTING_FULL_NAME_MAPPER,
        parent=provider_id,
        component_type=LDAP_MAPPER_PROVIDER_TYPE,
    )
    conflicting = [
        component
        for component in components
        if component.get("name") == CONFLICTING_FULL_NAME_MAPPER
        and component.get("providerId") == FULL_NAME_MAPPER_PROVIDER_ID
    ]
    if len(conflicting) > 1:
        raise ActiveDirectoryError("multiple conflicting full name mappers exist")
    if not conflicting:
        return
    if conflicting[0].get("subType") is not None:
        raise ActiveDirectoryError("conflicting full name mapper has an unmanaged subtype")
    mapper_id = conflicting[0].get("id")
    if not isinstance(mapper_id, str) or not mapper_id:
        raise ActiveDirectoryError("conflicting full name mapper has no component ID")
    _request_json(
        session,
        "DELETE",
        f"{components_url}/{quote(mapper_id, safe='')}",
        expected_statuses={204},
    )
    readback = _get_components(
        session,
        components_url,
        name=CONFLICTING_FULL_NAME_MAPPER,
        parent=provider_id,
        component_type=LDAP_MAPPER_PROVIDER_TYPE,
    )
    if any(
        component.get("name") == CONFLICTING_FULL_NAME_MAPPER
        and component.get("providerId") == FULL_NAME_MAPPER_PROVIDER_ID
        for component in readback
    ):
        raise ActiveDirectoryError("conflicting full name mapper deletion was not persisted")


def _preflight_connection(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    config: ActiveDirectoryConfig,
) -> None:
    endpoint = (
        f"{keycloak_url.rstrip('/')}/admin/realms/{quote(realm_name, safe='')}/testLDAPConnection"
    )
    connection = {
        "connectionUrl": config.connection_url,
        "bindDn": config.bind_dn,
        "bindCredential": config.bind_credential,
        "authType": "simple",
        "startTls": "false",
        "useTruststoreSpi": "ldapsOnly",
        "connectionTimeout": str(config.connection_timeout_ms),
    }
    for action in ("testConnection", "testAuthentication"):
        _request_json(
            session,
            "POST",
            endpoint,
            expected_statuses={204},
            json={"action": action, **connection},
        )


def _verify_access_groups(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    group_names: tuple[str, ...],
) -> None:
    _access_group_representations(
        session,
        keycloak_url,
        realm_name,
        group_names,
        brief_representation=True,
    )


def _access_group_representations(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    group_names: tuple[str, ...],
    *,
    brief_representation: bool,
    verify_paths: bool = False,
) -> dict[str, dict[str, Any]]:
    realm = quote(realm_name, safe="")
    groups_url = f"{keycloak_url.rstrip('/')}/admin/realms/{realm}/groups"
    top_level_groups = _request_json(
        session,
        "GET",
        groups_url,
        expected_statuses={200},
        params={
            "briefRepresentation": "true",
            "populateHierarchy": "false",
            "max": "1000",
        },
    )
    access_group = _select_exact_group(top_level_groups, "access")
    access_id = access_group["id"]
    if verify_paths and (
        access_group.get("path") != "/access" or access_group.get("parentId") is not None
    ):
        raise ActiveDirectoryError("Keycloak /access group has an unexpected path or parent")
    children_url = f"{groups_url}/{quote(access_id, safe='')}/children"
    groups = {
        group_name: _get_exact_group(
            session,
            children_url,
            group_name,
            brief_representation=brief_representation,
        )
        for group_name in group_names
    }
    if verify_paths and (
        len({access_id, *(group["id"] for group in groups.values())}) != len(groups) + 1
        or any(
            group.get("path") != f"/access/{name}" or group.get("parentId") != access_id
            for name, group in groups.items()
        )
    ):
        raise ActiveDirectoryError("Keycloak canonical group has an unexpected ID, path or parent")
    return groups


def _get_exact_group(
    session: requests.Session,
    endpoint: str,
    group_name: str,
    *,
    brief_representation: bool,
) -> dict[str, Any]:
    groups = _request_json(
        session,
        "GET",
        endpoint,
        expected_statuses={200},
        params={
            "search": group_name,
            "exact": "true",
            "briefRepresentation": str(brief_representation).lower(),
            "max": "2",
        },
    )
    return _select_exact_group(groups, group_name)


def _select_exact_group(groups: Any, group_name: str) -> dict[str, Any]:
    if not isinstance(groups, list):
        raise ActiveDirectoryError("Keycloak returned invalid group data")
    matching = [
        group for group in groups if isinstance(group, dict) and group.get("name") == group_name
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("id"), str) or not matching[0]["id"]:
        path = "/access" if group_name == "access" else f"/access/{group_name}"
        raise ActiveDirectoryError(f"required Keycloak group does not exist: {path}")
    return matching[0]


def _verify_access_group_bindings(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    config: ActiveDirectoryConfig,
) -> None:
    groups = _access_group_representations(
        session,
        keycloak_url,
        realm_name,
        tuple(sorted(config.group_names)),
        brief_representation=False,
    )
    expected_dns = dict(zip(config.group_names, _group_dns(config), strict=True))
    for group_name, group in groups.items():
        attributes = group.get("attributes")
        ldap_entry_dns = (
            attributes.get(LDAP_ENTRY_DN_ATTRIBUTE) if isinstance(attributes, dict) else None
        )
        if (
            not isinstance(ldap_entry_dns, list)
            or len(ldap_entry_dns) != 1
            or not isinstance(ldap_entry_dns[0], str)
            or not ldap_entry_dns[0]
        ):
            raise ActiveDirectoryError(
                f"Keycloak group /access/{group_name} has no verifiable LDAP entry DN"
            )
        if ldap_entry_dns[0].casefold() != expected_dns[group_name].casefold():
            raise ActiveDirectoryError(
                f"Keycloak group /access/{group_name} is bound to an unexpected LDAP entry DN"
            )


def _sync_group_mapper(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    provider_id: str,
    mapper_id: str,
    approved_group_count: int,
) -> None:
    endpoint = (
        f"{keycloak_url.rstrip('/')}/admin/realms/{quote(realm_name, safe='')}"
        f"/user-storage/{quote(provider_id, safe='')}/mappers/"
        f"{quote(mapper_id, safe='')}/sync"
    )
    result = _request_json(
        session,
        "POST",
        endpoint,
        expected_statuses={200},
        params={"direction": "fedToKeycloak"},
    )
    count_fields = ("added", "updated", "removed", "failed")
    if not isinstance(result, dict) or any(
        type(result.get(field)) is not int or result[field] < 0 for field in count_fields
    ):
        raise ActiveDirectoryError("Keycloak returned invalid group sync data")
    if (
        result["failed"] != 0
        or result["removed"] != 0
        or result["added"] + result["updated"] != approved_group_count
    ):
        raise ActiveDirectoryError("Keycloak Active Directory group sync failed")


def _verify_mapping_groups(
    session: requests.Session,
    keycloak_url: str,
    realm_name: str,
    config: ActiveDirectoryConfig,
    parent_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    parents = _access_group_representations(
        session,
        keycloak_url,
        realm_name,
        tuple(mapping.target_parent.removeprefix("/access/") for mapping in config.group_mappings),
        brief_representation=False,
        verify_paths=True,
    )
    actual_ids = {group["path"]: group["id"] for group in parents.values()}
    if parent_ids is not None:
        if actual_ids != parent_ids:
            raise ActiveDirectoryError("Keycloak mapping parents changed during reconciliation")
        groups_url = f"{keycloak_url.rstrip('/')}/admin/realms/{quote(realm_name, safe='')}/groups"
        seen_ids = set(parent_ids.values())
        for mapping in config.group_mappings:
            parent_id = parent_ids[mapping.target_parent]
            child = _get_exact_group(
                session,
                f"{groups_url}/{quote(parent_id, safe='')}/children",
                mapping.source_name,
                brief_representation=False,
            )
            child_id = child["id"]
            actual = _request_json(
                session,
                "GET",
                f"{groups_url}/{quote(child_id, safe='')}",
                expected_statuses={200},
            )
            expected = {
                "id": child_id,
                "name": mapping.source_name,
                "path": f"{mapping.target_parent}/{mapping.source_name}",
                "parentId": parent_id,
            }
            if (
                child_id in seen_ids
                or not isinstance(actual, dict)
                or any(
                    child.get(key) != value or actual.get(key) != value
                    for key, value in expected.items()
                )
            ):
                raise ActiveDirectoryError(
                    "Keycloak mapped child has an unexpected ID, name or path"
                )
            seen_ids.add(child_id)
    return actual_ids


def _owned_group_mappers(
    session: requests.Session,
    components_url: str,
    provider_id: str,
    *,
    mapping_mode: bool,
) -> list[dict[str, Any]]:
    components = _get_components(
        session,
        components_url,
        name=None,
        parent=provider_id,
        component_type=LDAP_MAPPER_PROVIDER_TYPE,
    )
    owned = []
    manual = []
    for component in components:
        name = component.get("name", "")
        settings = component.get("config")
        if not isinstance(name, str) or not isinstance(settings, dict):
            raise ActiveDirectoryError("Keycloak mapper has invalid identity or configuration")
        is_mapping = (
            name.startswith(MAPPING_NAME_PREFIX)
            or component.get("subType") == MAPPING_OWNER_SUBTYPE
        )
        if name == "approved groups" or is_mapping:
            if (
                component.get("providerId") != "group-ldap-mapper"
                or not isinstance(component.get("id"), str)
                or not component["id"]
                or settings.get("mode") != ["READ_ONLY"]
                or (
                    is_mapping
                    and (
                        component.get("subType") != MAPPING_OWNER_SUBTYPE
                        or "/access/" + name.removeprefix(MAPPING_NAME_PREFIX)
                        not in CANONICAL_GROUP_PATHS
                    )
                )
                or (
                    not is_mapping
                    and (
                        component.get("subType") is not None
                        or settings.get("groups.path") != ["/access"]
                    )
                )
            ):
                raise ActiveDirectoryError(
                    "reserved group mapper ownership or READ_ONLY mode is invalid"
                )
            if any(item["name"] == name for item in owned):
                raise ActiveDirectoryError("duplicate managed group mappers exist")
            owned.append(component)
            mapping_mode |= is_mapping
        elif component.get("providerId") == "group-ldap-mapper":
            manual.append(component)
    if mapping_mode:
        for component in manual:
            paths = component["config"].get("groups.path", ["/"])
            if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str):
                raise ActiveDirectoryError("manual group mapper has an unverifiable groups path")
            path = "/" + paths[0].strip().strip("/")
            if path == "/" or path == "/access" or path.startswith("/access/"):
                raise ActiveDirectoryError(
                    "manual group mapper overlaps /access; review it before retrying"
                )
    return owned


def reconcile_active_directory(
    keycloak_url: str,
    realm_name: str,
    session: requests.Session,
    config: ActiveDirectoryConfig | None,
) -> None:
    components_url = _component_url(keycloak_url, realm_name)
    realm_payload = _request_json(
        session,
        "GET",
        f"{keycloak_url.rstrip('/')}/admin/realms/{quote(realm_name, safe='')}",
        expected_statuses={200},
    )
    realm_id = realm_payload.get("id") if isinstance(realm_payload, dict) else None
    if not isinstance(realm_id, str) or not realm_id:
        raise ActiveDirectoryError("Keycloak realm has no ID")

    existing = _find_managed_component(
        _get_components(
            session,
            components_url,
            name=MANAGED_PROVIDER_NAME,
            parent=realm_id,
            component_type=USER_STORAGE_PROVIDER_TYPE,
        ),
        MANAGED_PROVIDER_NAME,
    )
    if existing is not None and existing.get("providerId") != "ldap":
        raise ActiveDirectoryError(
            "reserved Active Directory provider name belongs to another type"
        )
    if config is None:
        if existing is None:
            return
        existing_config = existing.get("config")
        if not isinstance(existing_config, dict):
            raise ActiveDirectoryError("managed Active Directory provider has invalid config")
        if existing_config.get("enabled") == ["false"]:
            return
        provider_id = existing.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ActiveDirectoryError("managed Active Directory provider has no ID")
        disabled = {**existing, "config": {**existing_config, "enabled": ["false"]}}
        _update_component(session, components_url, provider_id, disabled)
        return

    if existing is not None and existing.get("subType") not in (
        None,
        MANAGED_PROVIDER_SUBTYPE,
        MAPPING_TRANSITION_SUBTYPE,
    ):
        raise ActiveDirectoryError("managed Active Directory provider has an unmanaged subtype")

    parent_ids = None
    if config.group_mappings:
        parent_ids = _verify_mapping_groups(session, keycloak_url, realm_name, config)
    else:
        _verify_access_groups(session, keycloak_url, realm_name, tuple(sorted(config.group_names)))
    _preflight_connection(session, keycloak_url, realm_name, config)

    mapping_transition = bool(config.group_mappings) or (
        existing is not None and existing.get("subType") == MAPPING_TRANSITION_SUBTYPE
    )
    owned_groups = []
    if existing is not None:
        if not isinstance(existing.get("id"), str) or not existing["id"]:
            raise ActiveDirectoryError("managed Active Directory provider has no ID")
        owned_groups = _owned_group_mappers(
            session,
            components_url,
            existing["id"],
            mapping_mode=mapping_transition,
        )
    mapping_transition |= any(mapper["name"] != "approved groups" for mapper in owned_groups)

    desired_provider: dict[str, Any] = {
        "name": MANAGED_PROVIDER_NAME,
        "parentId": realm_id,
        "providerId": "ldap",
        "providerType": USER_STORAGE_PROVIDER_TYPE,
        "subType": existing.get("subType") if existing is not None else None,
        "config": _provider_config(config),
    }
    if mapping_transition:
        # Disabled providers reject imported-user authentication without deleting users.
        # Admin mapper sync still works in 26.7.2. Never expose a partially replaced grant set.
        # subType roundtrips; arbitrary config is filtered and null cannot clear a subtype.
        enabled_provider = {
            **desired_provider,
            "subType": MANAGED_PROVIDER_SUBTYPE,
        }
        desired_provider = {
            **desired_provider,
            "subType": MAPPING_TRANSITION_SUBTYPE,
            "config": {
                **desired_provider["config"],
                "enabled": ["false"],
            },
        }
    if existing is None:
        existing = _create_component(session, components_url, desired_provider)
    elif _component_needs_update(existing, desired_provider) or existing.get("config", {}).get(
        "bindCredential"
    ) == [MASKED_SECRET]:
        # Keycloak masks component secrets on read, so always re-apply the supplied
        # credential to make rotations converge without exposing either value.
        provider_id = existing.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ActiveDirectoryError("managed Active Directory provider has no ID")
        existing = _update_component(session, components_url, provider_id, desired_provider)

    provider_id = existing.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise ActiveDirectoryError("managed Active Directory provider has no ID")
    _remove_conflicting_full_name_mapper(session, components_url, provider_id)
    mappers = _mapper_representations(provider_id, config)
    desired_names = {mapper["name"] for mapper in mappers}
    for obsolete in owned_groups:
        if obsolete["name"] in desired_names:
            continue
        _request_json(
            session,
            "DELETE",
            f"{components_url}/{quote(obsolete['id'], safe='')}",
            expected_statuses={204},
        )
        if (
            _find_managed_component(
                _get_components(
                    session,
                    components_url,
                    name=obsolete["name"],
                    parent=provider_id,
                    component_type=LDAP_MAPPER_PROVIDER_TYPE,
                ),
                obsolete["name"],
            )
            is not None
        ):
            raise ActiveDirectoryError("obsolete group mapper deletion was not persisted")

    group_mappers = []
    for mapper in mappers:
        mapper_id = _upsert_mapper(session, components_url, provider_id, mapper)
        if mapper["providerId"] == "group-ldap-mapper":
            group_mappers.append((mapper_id, mapper))
    if not group_mappers:
        raise ActiveDirectoryError("managed Active Directory group mapper is missing")
    for mapper_id, mapper in group_mappers:
        _sync_group_mapper(
            session,
            keycloak_url,
            realm_name,
            provider_id,
            mapper_id,
            1 if config.group_mappings else len(config.group_names),
        )
        if mapping_transition:
            _verify_component_readback(session, components_url, mapper, expected_id=mapper_id)
    if config.group_mappings:
        _verify_mapping_groups(session, keycloak_url, realm_name, config, parent_ids)
    else:
        _verify_access_group_bindings(session, keycloak_url, realm_name, config)
    if mapping_transition:
        remaining = _owned_group_mappers(
            session,
            components_url,
            provider_id,
            mapping_mode=True,
        )
        if {mapper["id"] for mapper in remaining} != {item[0] for item in group_mappers}:
            raise ActiveDirectoryError("managed group mapper set changed during reconciliation")
        try:
            _update_component(session, components_url, provider_id, enabled_provider)
        except (ActiveDirectoryError, requests.RequestException):
            # A PUT can succeed even if its response/readback is lost. Re-close the gate.
            try:
                _update_component(session, components_url, provider_id, desired_provider)
            except (ActiveDirectoryError, requests.RequestException) as exc:
                raise ActiveDirectoryError(
                    "provider activation failed and disabling could not be verified; "
                    "operator review required"
                ) from exc
            raise
