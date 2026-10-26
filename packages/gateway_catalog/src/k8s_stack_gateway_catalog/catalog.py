"""Strict public catalog v2 contract consumed by gateway observation."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, field_validator, model_validator

from k8s_stack_gateway_catalog.document import Document

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]
Description = Annotated[str, Field(max_length=2000)]
Permissions = Annotated[list[Identifier], Field(max_length=128)]
MAX_ENDPOINTS = 256


class CatalogError(ValueError):
    """Invalid public catalog; never include input values in diagnostics."""


class GatewayEndpoint(Document):
    id: Identifier
    url: str = Field(min_length=1, max_length=2048)
    entry_mode: Literal["api", "forward_proxy"]
    caller_auth_modes: list[Literal["oidc_bearer", "neurwerk_bearer", "proxy_basic"]] = Field(
        min_length=1, max_length=3
    )

    @field_validator("caller_auth_modes")
    @classmethod
    def unique_auth(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise CatalogError
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        _ = parsed.port
        local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            (parsed.scheme != "https" and not local)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or c == "\\" or ord(c) < 32 for c in value)
        ):
            raise CatalogError
        return value


class GatewayCredentialMode(Document):
    source: Literal["company", "caller_supplied", "none"]
    required_permissions: Permissions

    @field_validator("required_permissions")
    @classmethod
    def unique_permissions(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise CatalogError
        return value


class GatewayDestination(Document):
    id: Identifier
    kind: Literal["model", "mcp", "application"]
    display_name: Identifier
    endpoint_ids: list[Identifier] = Field(min_length=1, max_length=256)
    profile_ids: list[Identifier] = Field(max_length=128)
    required_permissions: Permissions = Field(min_length=1)
    credential_modes: list[GatewayCredentialMode] = Field(max_length=3)
    provider_id: Identifier | None = None
    model: Identifier | None = None

    @model_validator(mode="after")
    def unique_modes(self) -> Self:
        if len({mode.source for mode in self.credential_modes}) != len(self.credential_modes):
            raise CatalogError
        for values in (self.endpoint_ids, self.profile_ids, self.required_permissions):
            if len(set(values)) != len(values):
                raise CatalogError
        return self


class GatewayOperation(Document):
    id: Identifier
    support: Literal["supported", "unsupported", "unknown"]
    restoration_scope: Literal["request", "persistent", "none"]
    limitations: list[Description] = Field(max_length=32)
    evidence_ref: Identifier | None = None
    verified_at: AwareDatetime | None = None


class GatewayProfile(Document):
    id: Identifier
    revision: Identifier
    operations: list[GatewayOperation] = Field(max_length=64)
    limitations: list[Description] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_operations(self) -> Self:
        if len({item.id for item in self.operations}) != len(self.operations):
            raise CatalogError
        return self


class GatewayEntry(Document):
    id: Identifier
    provider_id: Identifier | None
    display_name: Identifier
    routes: list[GatewayEndpoint] = Field(max_length=MAX_ENDPOINTS)
    destinations: list[GatewayDestination] = Field(max_length=500)


class GatewayCatalog(Document):
    schema_version: Literal[2]
    catalog_revision: Identifier
    policy_revision: Identifier
    profiles: list[GatewayProfile] = Field(max_length=128)
    self_hosted_api_providers: list[GatewayEntry] = Field(
        alias="self-hosted-api-providers", max_length=256
    )
    external_api_providers: list[GatewayEntry] = Field(
        alias="external-api-providers", max_length=256
    )
    external_subscription_providers: list[GatewayEntry] = Field(
        alias="external-subscription-providers", max_length=256
    )
    mcp_servers: list[GatewayEntry] = Field(alias="mcp-servers", max_length=200)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema(cls, value: object) -> int:
        if type(value) is not int or value != 2:
            raise CatalogError
        return value

    def groups(self) -> tuple[list[GatewayEntry], ...]:
        return (
            self.self_hosted_api_providers,
            self.external_api_providers,
            self.external_subscription_providers,
            self.mcp_servers,
        )

    @property
    def endpoints(self) -> list[GatewayEndpoint]:
        return list(
            {
                route.id: route
                for group in self.groups()
                for entry in group
                for route in entry.routes
            }.values()
        )

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        entries = [entry for group in self.groups() for entry in group]
        providers = (
            self.self_hosted_api_providers
            + self.external_api_providers
            + self.external_subscription_providers
        )
        if len(providers) > 256 or len({entry.id for entry in entries}) != len(entries):
            raise CatalogError
        routes: dict[str, GatewayEndpoint] = {}
        for entry in entries:
            owned = {route.id for route in entry.routes}
            if len(owned) != len(entry.routes):
                raise CatalogError
            for route in entry.routes:
                previous = routes.get(route.id)
                if previous is not None and (
                    route.entry_mode != "forward_proxy" or previous != route
                ):
                    raise CatalogError
                routes[route.id] = route
            if sum(item.kind == "model" for item in entry.destinations) > 32:
                raise CatalogError
            if any(
                not set(item.endpoint_ids) <= owned or item.provider_id != entry.provider_id
                for item in entry.destinations
            ):
                raise CatalogError
        profiles = {item.id for item in self.profiles}
        destinations = [item for entry in entries for item in entry.destinations]
        if (
            len(routes) > MAX_ENDPOINTS
            or len(destinations) > 500
            or sum(item.kind == "model" for item in destinations) > 256
            or sum(item.kind == "mcp" for item in destinations) > 200
            or len(profiles) != len(self.profiles)
            or len({(item.kind, item.id) for item in destinations}) != len(destinations)
            or any(not set(item.profile_ids) <= profiles for item in destinations)
        ):
            raise CatalogError
        return self
