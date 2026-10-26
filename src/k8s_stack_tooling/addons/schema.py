"""One bounded, strict parser for release definitions and client selections."""

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from yaml.resolver import BaseResolver

from k8s_stack_tooling.keycloak.scoped_oidc import RESERVED_CLIENT_IDS
from k8s_stack_tooling.keycloak.scoped_service import names as service_permissions

API_VERSION = "addons.neurwerk.com/v1alpha2"
MAX_DOCUMENT_BYTES = 262144
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


def _instance_name(value: str) -> str:
    if value.count("{instance}") > 1 or not re.fullmatch(
        r"[a-z][a-z0-9-]{0,62}", value.replace("{instance}", "x")
    ):
        raise ValueError("expected a literal name or one instance marker")
    return value


InstanceName = Annotated[str, AfterValidator(_instance_name)]
FieldName = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
Version = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]


class ContractError(ValueError):
    """Invalid nonsecret authority; messages never include supplied values."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Prerequisites(Contract):
    base: Version
    tooling: Version
    stack_setup: Version = Field(alias="stackSetup")
    base_charts: list[str] = Field(default_factory=list, alias="baseCharts", max_length=100)
    base_chart_versions: dict[str, str] = Field(default_factory=dict, alias="baseChartVersions")

    @model_validator(mode="after")
    def chart_versions(self):
        import re

        number = r"(?:0|[1-9][0-9]*)"
        identifier = rf"(?:{number}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
        semver = (
            rf"{number}\.{number}\.{number}(?:-{identifier}(?:\.{identifier})*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        )
        if not self.base_chart_versions.keys() <= set(self.base_charts) or any(
            not re.fullmatch(semver, version) for version in self.base_chart_versions.values()
        ):
            raise ValueError("expected declared Base charts and exact chart versions")
        return self


class HelmOutput(Contract):
    kind: Literal["ConfigMap", "Secret"]
    namespace: Name
    name: Name
    helm_release: Name = Field(alias="helmRelease")


class Package(Contract):
    name: Name
    path: str = Field(min_length=1, max_length=256)
    depends_on: list[Name] = Field(default_factory=list, alias="dependsOn", max_length=100)
    helm_outputs: list[HelmOutput] = Field(
        default_factory=list, alias="helmOutputs", max_length=100
    )


class Retention(Contract):
    databases: Literal["retain"] = "retain"
    volumes: Literal["retain"] = "retain"
    credentials: Literal["retain"] = "retain"


class Input(Contract):
    kind: Literal["ConfigMap", "Secret", "SecretStore", "ServiceAccount"]
    namespace: Name
    name: Name
    owner: Literal["base", "client"]
    secret_type: str | None = Field(default=None, alias="type", min_length=1, max_length=128)
    keys: list[str] = Field(default_factory=list, max_length=100)


class ExternalInput(Contract):
    """An operator-provided prerequisite; never a generated resource or presence proof."""

    kind: Literal["Secret"]
    namespace: Name
    name: Name
    secret_type: str = Field(alias="type", min_length=1, max_length=128)
    keys: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,253}$")]] = Field(
        min_length=1, max_length=100
    )

    @model_validator(mode="after")
    def unique_keys(self):
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("external input keys must be unique")
        return self


class Authentication(Contract):
    client_id: Name = Field(alias="clientId")
    redirect_uris: list[str] = Field(alias="redirectUris", min_length=1, max_length=100)
    web_origins: list[str] = Field(alias="webOrigins", min_length=1, max_length=100)
    logout_uris: list[str] = Field(
        default_factory=list, alias="postLogoutRedirectUris", max_length=100
    )
    realm_roles: list[str] = Field(alias="realmRoles", min_length=1, max_length=100)
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
        "redirect_uris",
        "web_origins",
        "logout_uris",
        "realm_roles",
        "audiences",
        "gateway_permissions",
    )
    @classmethod
    def unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("authentication lists must be unique")
        return value

    @model_validator(mode="after")
    def authentication_contract(self):
        import re

        for values, origin in (
            (self.redirect_uris, False),
            (self.web_origins, True),
            (self.logout_uris, False),
        ):
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


class FieldRef(Contract):
    record: Name
    field: FieldName


class Generate(Contract):
    encoding: Literal["hex", "base64url", "base64"]
    bytes: int = Field(ge=16, le=4096)


class ValueValidation(Contract):
    min_length: int = Field(default=1, alias="minLength", ge=0, le=16384)
    max_length: int = Field(default=16384, alias="maxLength", ge=1, le=16384)
    format: Literal["text", "hex", "base64url", "base64", "sha256", "jwt-hs256-nbf"] = "text"

    @model_validator(mode="after")
    def bounds(self):
        if self.min_length > self.max_length:
            raise ValueError("invalid value bounds")
        return self


class Supplied(Contract):
    group: Name
    key: FieldName


class CredentialField(Contract):
    generate: Generate | None = None
    literal: str | None = Field(default=None, max_length=16384)
    ref: FieldRef | None = None
    sha256: FieldRef | None = None
    supplied: Supplied | None = None
    prepared_output: Name | None = Field(default=None, alias="preparedOutput")
    validation: ValueValidation = Field(default_factory=ValueValidation)
    when: Name | None = None

    @model_validator(mode="after")
    def one_operation(self):
        operations = (
            self.generate,
            self.literal,
            self.ref,
            self.sha256,
            self.supplied,
            self.prepared_output,
        )
        names = {"generate", "literal", "ref", "sha256", "supplied", "prepared_output"}
        if (
            sum(value is not None for value in operations) != 1
            or len(self.model_fields_set & names) != 1
        ):
            raise ValueError("credential field requires exactly one operation")
        if self.literal is not None and not valid_value(self.literal, self.validation):
            raise ValueError("literal does not satisfy value constraints")
        return self


class Record(Contract):
    id: Name
    path: str = Field(
        pattern=r"^addons/[a-z][a-z0-9-]*/(?:[a-zA-Z0-9_-]+|\{instance\})(?:/(?:[a-zA-Z0-9_-]+|\{instance\}))*$",
        max_length=256,
    )
    fields: dict[FieldName, CredentialField] = Field(min_length=1, max_length=100)
    when: Name | None = None


class Reader(Contract):
    role: InstanceName
    namespace: Name
    service_account: InstanceName = Field(alias="serviceAccount")
    audience: str = Field(min_length=1, max_length=256)
    records: list[Name] = Field(min_length=1, max_length=100)
    when: Name | None = None


class InputGroup(Contract):
    id: Name
    when: Name | None = None
    fields: dict[FieldName, ValueValidation] = Field(min_length=1, max_length=100)


class Target(Contract):
    kind: Literal[
        "SecretStore", "ExternalSecret", "HelmRelease", "Deployment", "StatefulSet", "Job"
    ]
    namespace: Name
    name: InstanceName
    when: Name | None = None


class Delivery(Contract):
    namespace: Name
    name: InstanceName
    secret_store: InstanceName = Field(alias="secretStore")
    target_secret: InstanceName = Field(alias="targetSecret")
    records: list[Name] = Field(min_length=1, max_length=100)
    when: Name | None = None


class Handover(Contract):
    id: Name
    label: str = Field(min_length=1, max_length=120)
    value: FieldRef
    when: Name | None = None


class VerifierSlots(Contract):
    kind: Literal["accepted-verifier-slots"]
    source: FieldRef
    primary: FieldRef
    secondary: FieldRef
    when: Name | None = None


class Lifecycle(Contract):
    delivery: list[Target] = Field(default_factory=list, max_length=100)
    provision: list[Target] = Field(default_factory=list, max_length=100)
    restart: list[Target] = Field(default_factory=list, max_length=100)
    rotate: list[Target] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def operation_targets(self):
        for targets, kinds in (
            (self.delivery, {"SecretStore", "ExternalSecret"}),
            (self.provision, {"HelmRelease", "Job"}),
            (self.restart, {"Deployment", "StatefulSet"}),
        ):
            keys = [(target.kind, target.namespace, target.name) for target in targets]
            if len(set(keys)) != len(keys) or any(target.kind not in kinds for target in targets):
                raise ValueError("invalid lifecycle target")
        return self


class Setup(Contract):
    records: list[Record] = Field(default_factory=list, max_length=100)
    readers: list[Reader] = Field(default_factory=list, max_length=100)
    input_groups: list[InputGroup] = Field(
        default_factory=list, alias="inputGroups", max_length=100
    )
    deliveries: list[Delivery] = Field(default_factory=list, max_length=100)
    handover: list[Handover] = Field(default_factory=list, max_length=100)
    invariants: list[VerifierSlots] = Field(default_factory=list, max_length=100)
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)

    @model_validator(mode="after")
    def references(self):
        records = {record.id: record for record in self.records}
        groups = {group.id: group for group in self.input_groups}
        if (
            len(records) != len(self.records)
            or len(groups) != len(self.input_groups)
            or len({r.path for r in self.records}) != len(self.records)
            or len({r.role for r in self.readers}) != len(self.readers)
            or len({h.id for h in self.handover}) != len(self.handover)
        ):
            raise ValueError("duplicate setup identity")
        edges: dict[tuple[str, str], set[tuple[str, str]]] = {}

        def reference(ref: FieldRef, conditions: set[str]):
            record = records.get(ref.record)
            if record is None or ref.field not in record.fields:
                raise ValueError("missing credential field reference")
            required = {c for c in (record.when, record.fields[ref.field].when) if c}
            if not required <= conditions:
                raise ValueError("credential reference crosses activation boundaries")
            return ref.record, ref.field

        for record in self.records:
            for name, field in record.fields.items():
                ref = field.ref or field.sha256
                conditions = {c for c in (record.when, field.when) if c}
                edges[(record.id, name)] = {reference(ref, conditions)} if ref else set()
                if field.supplied:
                    group = groups.get(field.supplied.group)
                    if (
                        group is None
                        or field.supplied.key not in group.fields
                        or (group.when is not None and group.when not in conditions)
                    ):
                        raise ValueError("invalid supplied input reference")
        pending = dict(edges)
        while pending:
            ready = {key for key, refs in pending.items() if not refs.intersection(pending)}
            if not ready:
                raise ValueError("cyclic credential field references")
            pending = {key: refs for key, refs in pending.items() if key not in ready}
        for reader in self.readers:
            if (
                len(set(reader.records)) != len(reader.records)
                or not set(reader.records) <= records.keys()
            ):
                raise ValueError("invalid reader records")
        for delivery in self.deliveries:
            for name in delivery.records:
                if name not in records or records[name].when not in (None, delivery.when):
                    raise ValueError("invalid delivery records")
        for handover in self.handover:
            reference(handover.value, {handover.when} if handover.when else set())
        for invariant in self.invariants:
            for ref in (invariant.source, invariant.primary, invariant.secondary):
                reference(ref, {invariant.when} if invariant.when else set())
            if (
                invariant.source.record in (invariant.primary.record, invariant.secondary.record)
                or invariant.primary == invariant.secondary
            ):
                raise ValueError("verifier slots require distinct fields and isolated raw keys")
        return self


def valid_value(value: str, validation: ValueValidation) -> bool:
    """Validate bounded encodings; JWT checks structure, not signature authenticity."""
    if (
        not isinstance(value, str)
        or not validation.min_length <= len(value) <= validation.max_length
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        return False
    if value == "":
        return validation.min_length == 0
    kind = validation.format
    if kind == "text":
        return bool(value.strip())
    if kind in ("hex", "sha256"):
        return (
            bool(re.fullmatch(r"[0-9a-f]+", value))
            and len(value) % 2 == 0
            and (kind != "sha256" or len(value) == 64)
        )
    try:
        if kind == "base64":
            return base64.b64encode(base64.b64decode(value, validate=True)).decode() == value
        if kind == "base64url":
            return (
                bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))
                and base64.urlsafe_b64encode(
                    base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                )
                .decode()
                .rstrip("=")
                == value
            )
        parts = value.split(".")
        if len(parts) != 3 or any(
            not valid_value(p, ValueValidation(format="base64url")) for p in parts
        ):
            return False
        decoded = [base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)) for p in parts]
        header = json.loads(decoded[0].decode("utf-8"), object_pairs_hook=_unique_json)
        payload = json.loads(decoded[1].decode("utf-8"), object_pairs_hook=_unique_json)
        nbf = payload.get("nbf") if isinstance(payload, dict) else None
        return (
            isinstance(header, dict)
            and header.get("alg") == "HS256"
            and type(nbf) is int
            and len(decoded[2]) == 32
        )
    except (ValueError, UnicodeError, binascii.Error, RecursionError, OverflowError):
        return False


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate_verifier_slots(setup: Setup, values: Mapping[str, Mapping[str, str]]) -> None:
    """Accept either SHA-256 UTF-8 slot without modifying primary or secondary state."""
    for invariant in setup.invariants:
        try:
            raw = values[invariant.source.record][invariant.source.field]
            slots = [
                values[ref.record][ref.field] for ref in (invariant.primary, invariant.secondary)
            ]
            if (
                not isinstance(raw, str)
                or not raw
                or any(
                    not isinstance(slot, str) or (slot and not re.fullmatch(r"[a-f0-9]{64}", slot))
                    for slot in slots
                )
            ):
                raise ContractError("invalid verifier slot state")
            if hashlib.sha256(raw.encode("utf-8")).hexdigest() not in slots:
                raise ContractError("credential does not match an accepted verifier slot")
        except (KeyError, UnicodeError):
            raise ContractError("incomplete or invalid verifier slot state") from None


class Artifact(Contract):
    id: Name
    image: str = Field(min_length=1, max_length=512)
    role: Literal["shared", "product", "native"]


class Consumer(Contract):
    artifact: Name
    package: Name
    kind: Literal[
        "Deployment", "StatefulSet", "Job", "CronJob", "Pod", "HelmRelease", "JobTemplate"
    ]
    namespace: Name
    name: Name
    container: Name
    location: Literal["containers", "initContainers"] = "containers"
    optional: bool = False
    template_key: str | None = Field(
        default=None, alias="templateKey", pattern=r"^[A-Za-z0-9._-]{1,253}$"
    )

    @model_validator(mode="after")
    def template_binding(self):
        if (self.kind == "JobTemplate") != (self.template_key is not None):
            raise ValueError("JobTemplate consumers require exactly one explicit templateKey")
        return self


class CommandConsumer(Contract):
    artifact: Name
    kind: Literal["Command"]
    argv: list[str] = Field(min_length=1, max_length=32)

    @field_validator("argv")
    @classmethod
    def command(cls, value: list[str]) -> list[str]:
        if any(not arg or len(arg) > 1024 or any(ord(c) < 32 for c in arg) for arg in value):
            raise ValueError("command consumer requires bounded argv")
        return value


class NativeOutput(Contract):
    """Executor must use a tokenless, PV-free, network-denied Pod before root custody."""

    id: Name
    artifact: Name
    argv: list[str] = Field(min_length=1, max_length=32)
    idle_argv: list[str] = Field(alias="idleArgv", min_length=1, max_length=32)
    output_key: FieldName = Field(alias="outputKey")
    transport: Literal["private-exec-json", "private-exec-text"] = "private-exec-json"
    timeout_seconds: int = Field(default=60, alias="timeoutSeconds", ge=1, le=300)
    max_bytes: int = Field(default=16384, alias="maxBytes", ge=1, le=65536)
    validation: ValueValidation

    @model_validator(mode="after")
    def command(self):
        for argv in (self.argv, self.idle_argv):
            if (
                not argv[0].startswith("/")
                or Path(argv[0]).name in {"sh", "bash", "dash", "zsh", "ash", "env"}
                or any(not arg or len(arg) > 1024 or any(ord(c) < 32 for c in arg) for arg in argv)
            ):
                raise ValueError("native output requires fixed bounded executable commands")
        if self.validation.max_length > self.max_bytes:
            raise ValueError("native output validation exceeds its output bound")
        return self


def decode_native_output(definition: NativeOutput, stdout: bytes) -> dict[str, str]:
    """Decode one bounded output; the trusted runner supplies the text envelope."""
    if len(stdout) > definition.max_bytes:
        raise ContractError("native output exceeds size limit")
    try:
        text = stdout.decode("utf-8")
        if definition.transport == "private-exec-text":
            value = text.removesuffix("\r\n") if text.endswith("\r\n") else text.removesuffix("\n")
        else:
            document = json.loads(text, object_pairs_hook=_unique_json)
            if not isinstance(document, dict) or set(document) != {definition.output_key}:
                raise ContractError("native output has unexpected fields")
            value = document[definition.output_key]
        if not valid_value(value, definition.validation):
            raise ContractError("native output does not satisfy declared validation")
        return {definition.output_key: value}
    except (ValueError, UnicodeError, RecursionError):
        raise ContractError("invalid native output") from None


class ServiceIdentity(Contract):
    namespace: Name
    name: Name


class DatabaseClaim(Contract):
    id: Name
    service: ServiceIdentity
    engine: Literal["postgresql", "documentdb"]
    database: str = Field(min_length=1, max_length=63)
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    owner: Name
    package: Name
    base_chart: str = Field(alias="baseChart")
    exclusive: bool = False
    extensions: list[Literal["vector"]] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def capability(self):
        pattern = (
            r"[a-z][a-z0-9_]{0,62}"
            if self.engine == "postgresql"
            else r"[A-Za-z][A-Za-z0-9_-]{0,62}"
        )
        if not re.fullmatch(pattern, self.database):
            raise ValueError("invalid database name for the selected engine")
        if self.engine == "documentdb" and (not self.exclusive or self.extensions):
            raise ValueError("DocumentDB requires an exclusive service without SQL extensions")
        return self


class BrowserSettings(Contract):
    namespace: Name
    settings: Name
    approval: Name


class ServiceSettings(Contract):
    namespace: Name
    settings: Name
    approval: Name


class IdentityRequest(Contract):
    id: Name
    mode: Literal["browser", "service"]
    client_id: InstanceName = Field(alias="clientId")
    secret: FieldRef
    browser: Authentication | None = None
    browser_settings: BrowserSettings | None = Field(default=None, alias="browserSettings")
    service_settings: ServiceSettings | None = Field(default=None, alias="serviceSettings")
    realm_roles: list[str] = Field(default_factory=list, alias="realmRoles", max_length=100)
    gateway_permissions: list[str] = Field(
        default_factory=list, alias="gatewayPermissions", max_length=100
    )
    audiences: list[Name] = Field(default_factory=list, max_length=100)
    when: Name | None = None

    @model_validator(mode="after")
    def scope(self):
        if self.client_id in RESERVED_CLIENT_IDS:
            raise ValueError("reserved identity client")
        if self.mode == "browser":
            if (
                self.service_settings is not None
                or (self.browser is None) == (self.browser_settings is None)
                or (self.browser is not None and self.browser.client_id != self.client_id)
                or self.realm_roles
                or self.gateway_permissions
                or self.audiences
            ):
                raise ValueError("browser identity requires one consistent browser scope")
        elif self.service_settings is not None:
            if (
                self.browser is not None
                or self.browser_settings is not None
                or self.realm_roles
                or self.gateway_permissions
                or self.audiences
            ):
                raise ValueError("service settings cannot be combined with literal identity scopes")
        elif (
            self.browser is not None
            or self.browser_settings is not None
            or self.audiences != ["agentgateway"]
            or not self.gateway_permissions
        ):
            raise ValueError("service identity requires explicit gateway scope")
        if self.mode == "service" and self.service_settings is None:
            service_permissions(self.realm_roles)
            service_permissions(self.gateway_permissions)
            if "llm:invoke" not in self.gateway_permissions or not any(
                p.startswith("model:") for p in self.gateway_permissions
            ):
                raise ValueError("service identity requires explicit model invocation scope")
        for values in (self.realm_roles, self.gateway_permissions, self.audiences):
            if len(values) != len(set(values)) or any(
                not re.fullmatch(r"[a-z][A-Za-z0-9:._/-]{0,255}", value) for value in values
            ):
                raise ValueError("invalid identity scope")
        return self


class InstanceRecipe(Contract):
    id: Name
    features: dict[Name, bool] = Field(default_factory=dict, max_length=16)
    setup: Setup
    identities: list[IdentityRequest] = Field(default_factory=list, max_length=8)


def resolve_service_identity(
    request: IdentityRequest, effective_values: Mapping[str, object]
) -> IdentityRequest:
    """Resolve generic service request/approval fields, never product-specific settings."""
    if request.mode != "service" or request.service_settings is None:
        raise ContractError("service settings reference is required")
    service = effective_values.get("service")
    approval = effective_values.get("serviceApproval")
    fields = {"clientId", "realmRoles", "gatewayPermissions", "audiences"}
    if (
        not isinstance(service, dict)
        or set(service) != fields
        or service.get("clientId") != request.client_id
        or not isinstance(approval, dict)
        or set(approval) != fields
        or approval.get("clientId") != request.client_id
    ):
        raise ContractError("effective service identity does not match client approval")
    try:
        values = request.model_dump(by_alias=True)
        values.update(service)
        values["serviceSettings"] = None
        resolved = IdentityRequest.model_validate(values)
        approved = cast(dict[str, object], approval)
        for key, requested in (
            ("realmRoles", resolved.realm_roles),
            ("gatewayPermissions", resolved.gateway_permissions),
            ("audiences", resolved.audiences),
        ):
            allowed = approved.get(key)
            if (
                not isinstance(allowed, list)
                or any(not isinstance(value, str) for value in allowed)
                or len(allowed) != len(set(allowed))
                or set(requested) != set(allowed)
            ):
                raise ContractError("effective service scope differs from client approval")
        return resolved
    except (ValidationError, TypeError):
        raise ContractError("invalid effective service settings") from None


def resolve_browser_identity(
    request: IdentityRequest, effective_values: Mapping[str, object]
) -> IdentityRequest:
    """Validate final merged Helm values against clientApproval, without URL templates."""
    if request.mode != "browser" or request.browser_settings is None:
        raise ContractError("browser settings reference is required")
    try:
        browser = Authentication.model_validate(effective_values.get("authentication"))
        approval = effective_values.get("clientApproval")
        if (
            not isinstance(approval, dict)
            or approval.get("clientId") != request.client_id
            or browser.client_id != request.client_id
        ):
            raise ContractError("effective browser identity does not match client approval")
        for key, requested in (
            ("redirectUris", browser.redirect_uris),
            ("webOrigins", browser.web_origins),
            ("postLogoutRedirectUris", browser.logout_uris),
            ("realmRoles", browser.realm_roles),
            ("audiences", browser.audiences),
            ("gatewayPermissions", browser.gateway_permissions),
        ):
            allowed = approval.get(key, [])
            if (
                not isinstance(allowed, list)
                or len(allowed) > 100
                or any(not isinstance(value, str) for value in allowed)
                or len(set(allowed)) != len(allowed)
                or not set(requested) <= set(allowed)
            ):
                raise ContractError("effective browser scope exceeds client approval")
        resolved = request.model_dump(by_alias=True)
        resolved["browserSettings"] = None
        resolved["browser"] = browser.model_dump(by_alias=True)
        return IdentityRequest.model_validate(resolved)
    except ValidationError:
        raise ContractError("invalid effective browser settings") from None


class Spec(Contract):
    version: Version
    status: Literal["staged", "ready"]
    source_name: Name = Field(alias="sourceName")
    configuration_key: str = Field(alias="configurationKey", pattern=r"^[a-z][a-zA-Z0-9]*$")
    prerequisites: Prerequisites
    packages: list[Package] = Field(min_length=1, max_length=100)
    inputs: list[Input] = Field(default_factory=list, max_length=100)
    retention: Retention | None = None
    authentication: Authentication | None = None
    features: dict[Name, bool] = Field(default_factory=dict, max_length=100)
    setup: Setup = Field(default_factory=Setup)
    artifacts: list[Artifact] = Field(default_factory=list, max_length=100)
    consumers: list[Consumer | CommandConsumer] = Field(default_factory=list, max_length=200)
    native_outputs: list[NativeOutput] = Field(
        default_factory=list, alias="nativeOutputs", max_length=16
    )
    databases: list[DatabaseClaim] = Field(default_factory=list, max_length=32)
    identities: list[IdentityRequest] = Field(default_factory=list, max_length=100)
    instance_recipes: list[InstanceRecipe] = Field(
        default_factory=list, alias="instanceRecipes", max_length=8
    )

    @model_validator(mode="after")
    def conditions(self):
        _conditions(self.setup, self.identities, self.features)
        for recipe in self.instance_recipes:
            _conditions(recipe.setup, recipe.identities, recipe.features, instance_recipe=True)
        for items in (
            self.artifacts,
            self.native_outputs,
            self.databases,
            self.identities,
            self.instance_recipes,
        ):
            if len({item.id for item in items}) != len(items):
                raise ValueError("duplicate declaration identity")
        artifacts = {a.id for a in self.artifacts}
        outputs = {o.id: o for o in self.native_outputs}
        packages = {p.name for p in self.packages}
        settings_inputs = {(i.namespace, i.name) for i in self.inputs if i.kind == "ConfigMap"}
        settings_inputs.update(
            (o.namespace, o.name)
            for p in self.packages
            for o in p.helm_outputs
            if o.kind == "ConfigMap"
        )
        approval_inputs = {
            (i.namespace, i.name)
            for i in self.inputs
            if i.kind == "ConfigMap" and i.owner == "client"
        }
        for identity in [
            *self.identities,
            *(i for recipe in self.instance_recipes for i in recipe.identities),
        ]:
            binding = identity.browser_settings or identity.service_settings
            if binding is not None and (
                (binding.namespace, binding.settings) not in settings_inputs
                or (binding.namespace, binding.approval) not in approval_inputs
            ):
                raise ValueError(
                    "identity settings require declared settings and client approval inputs"
                )
        if any(
            c.artifact not in artifacts or (isinstance(c, Consumer) and c.package not in packages)
            for c in self.consumers
        ):
            raise ValueError("consumer references an undeclared artifact or package")
        if any(o.artifact not in artifacts for o in self.native_outputs):
            raise ValueError("native output references an undeclared artifact")
        used_artifacts = {c.artifact for c in self.consumers} | {
            o.artifact for o in self.native_outputs
        }
        if artifacts != used_artifacts:
            raise ValueError("artifact requires an explicit consumer")
        for setup in [self.setup, *(recipe.setup for recipe in self.instance_recipes)]:
            for record in setup.records:
                for field in record.fields.values():
                    if field.prepared_output is not None:
                        output = outputs.get(field.prepared_output)
                        if output is None or output.validation != field.validation:
                            raise ValueError(
                                "prepared output requires matching declared validation"
                            )
        if any(
            d.package not in packages or d.base_chart not in self.prerequisites.base_charts
            for d in self.databases
        ):
            raise ValueError(
                "database claim requires a provisioning package and Base chart prerequisite"
            )
        if self.authentication is not None and any(i.mode == "browser" for i in self.identities):
            raise ValueError("browser scope must have one declaration")
        return self


def _conditions(
    setup: Setup,
    identities: list[IdentityRequest],
    features: dict[str, bool],
    *,
    instance_recipe: bool = False,
) -> None:
    strings = [r.path for r in setup.records]
    if instance_recipe and any("{instance}" not in r.path for r in setup.records):
        raise ValueError("instance record paths require one explicit instance marker")
    strings.extend(value for r in setup.readers for value in (r.role, r.service_account))
    strings.extend(
        value for d in setup.deliveries for value in (d.name, d.secret_store, d.target_secret)
    )
    strings.extend(
        t.name
        for targets in (
            setup.lifecycle.delivery,
            setup.lifecycle.provision,
            setup.lifecycle.restart,
            setup.lifecycle.rotate,
        )
        for t in targets
    )
    strings.extend(i.client_id for i in identities)
    if any(
        value.count("{instance}") > 1 or ("{instance}" in value and not instance_recipe)
        for value in strings
    ):
        raise ValueError("instance markers are confined to instance recipe names and paths")
    objects = [
        *setup.records,
        *setup.readers,
        *setup.input_groups,
        *setup.deliveries,
        *setup.handover,
        *setup.invariants,
        *identities,
        *(field for record in setup.records for field in record.fields.values()),
        *setup.lifecycle.delivery,
        *setup.lifecycle.provision,
        *setup.lifecycle.restart,
        *setup.lifecycle.rotate,
    ]
    if any(item.when is not None and item.when not in features for item in objects):
        raise ValueError("undeclared activation feature")
    records = {record.id: record for record in setup.records}
    for identity in identities:
        record = records.get(identity.secret.record)
        if record is None or identity.secret.field not in record.fields:
            raise ValueError("identity secret is undeclared")
        required = {c for c in (record.when, record.fields[identity.secret.field].when) if c}
        if not required <= ({identity.when} if identity.when else set()):
            raise ValueError("identity secret crosses activation boundaries")


class Metadata(Contract):
    name: Name


class Addon(Contract):
    api_version: Literal["addons.neurwerk.com/v1alpha2"] = Field(alias="apiVersion")
    kind: Literal["AddonRelease"]
    metadata: Metadata
    spec: Spec

    @model_validator(mode="after")
    def record_owner(self):
        if any(
            not r.path.startswith(f"addons/{self.metadata.name}/")
            for setup in [self.spec.setup, *(recipe.setup for recipe in self.spec.instance_recipes)]
            for r in setup.records
        ):
            raise ValueError("credential path belongs to another add-on")
        if any(d.owner != self.metadata.name for d in self.spec.databases):
            raise ValueError("database claim belongs to another add-on")
        return self


class SourceRef(Contract):
    kind: Literal["GitRepository"]
    namespace: Name
    name: Name


class InstanceSelection(Contract):
    id: Name
    recipe: Name
    features: dict[Name, bool] = Field(default_factory=dict, max_length=16)


class AddonSelection(Contract):
    id: Name
    source_ref: SourceRef = Field(alias="sourceRef")
    features: dict[Name, bool] = Field(default_factory=dict, max_length=100)
    approved_readers: list[Reader] = Field(
        default_factory=list, alias="approvedReaders", max_length=100
    )
    instances: list[InstanceSelection] = Field(default_factory=list, max_length=32)
    approved_identities: list[IdentityRequest] = Field(
        default_factory=list, alias="approvedIdentities", max_length=100
    )

    @model_validator(mode="after")
    def instances_unique(self):
        if len({row.id for row in self.instances}) != len(self.instances):
            raise ValueError("instance IDs must be unique")
        if any(
            "{instance}" in value
            for r in self.approved_readers
            for value in (r.role, r.service_account)
        ) or any("{instance}" in i.client_id for i in self.approved_identities):
            raise ValueError("approvals require resolved identities")
        return self


class Selection(Contract):
    api_version: Literal["addons.neurwerk.com/v1alpha2"] = Field(alias="apiVersion")
    kind: Literal["AddonSelection"]
    base_source_ref: SourceRef = Field(alias="baseSourceRef")
    addons: list[AddonSelection] = Field(max_length=100)
    external_inputs: list[ExternalInput] = Field(
        default_factory=list, alias="externalInputs", max_length=100
    )

    @model_validator(mode="after")
    def identities(self):
        external = [(row.kind, row.namespace, row.name) for row in self.external_inputs]
        if len(set(external)) != len(external):
            raise ValueError("duplicate external input identity")
        refs = [(a.source_ref.kind, a.source_ref.namespace, a.source_ref.name) for a in self.addons]
        if len({a.id for a in self.addons}) != len(self.addons) or len(set(refs)) != len(refs):
            raise ValueError("duplicate selected identity")
        base = self.base_source_ref
        if (base.kind, base.namespace, base.name) in refs:
            raise ValueError("Base source cannot be an add-on source")
        return self


def _active_setup(setup: Setup, features: dict[str, bool]) -> dict:
    """Select fixed boolean conditions; no expression evaluation or secret access."""
    data = setup.model_dump(by_alias=True, exclude_none=True)

    def active(row):
        condition = row.pop("when", None)
        return condition is None or features[condition]

    data["records"] = [r for r in data["records"] if active(r)]
    for record in data["records"]:
        record["fields"] = {key: value for key, value in record["fields"].items() if active(value)}
    data["records"] = [r for r in data["records"] if r["fields"]]
    record_ids = {r["id"] for r in data["records"]}
    for key in ("readers", "inputGroups", "deliveries", "handover", "invariants"):
        data[key] = [row for row in data[key] if active(row)]
    supplied = {}
    for record in data["records"]:
        for field in record["fields"].values():
            if "supplied" in field:
                source = field["supplied"]
                supplied.setdefault(source["group"], set()).add(source["key"])
    data["inputGroups"] = [group for group in data["inputGroups"] if group["id"] in supplied]
    for group in data["inputGroups"]:
        group["fields"] = {
            key: value for key, value in group["fields"].items() if key in supplied[group["id"]]
        }
    for key in ("readers", "deliveries"):
        for row in data[key]:
            row["records"] = [name for name in row["records"] if name in record_ids]
        data[key] = [row for row in data[key] if row["records"]]
    for operation, targets in data["lifecycle"].items():
        data["lifecycle"][operation] = [target for target in targets if active(target)]
    return data


def _instance_setup(data: dict, instance: str) -> dict:
    """Apply the finite ID/path/name rules only to declared structural fields."""

    def prefix(name):
        return f"{instance}-{name}"

    def substitute(name):
        return name.replace("{instance}", instance)

    def reference(ref):
        ref["record"] = prefix(ref["record"])

    for record in data["records"]:
        record["id"] = prefix(record["id"])
        record["path"] = substitute(record["path"])
        for field in record["fields"].values():
            for kind in ("ref", "sha256"):
                if kind in field:
                    reference(field[kind])
            if "supplied" in field:
                field["supplied"]["group"] = prefix(field["supplied"]["group"])
    for reader in data["readers"]:
        reader["records"] = [prefix(name) for name in reader["records"]]
        for key in ("role", "serviceAccount"):
            reader[key] = substitute(reader[key])
    for group in data["inputGroups"]:
        group["id"] = prefix(group["id"])
    for delivery in data["deliveries"]:
        delivery["records"] = [prefix(name) for name in delivery["records"]]
        for key in ("name", "secretStore", "targetSecret"):
            delivery[key] = substitute(delivery[key])
    for handover in data["handover"]:
        handover["id"] = prefix(handover["id"])
        reference(handover["value"])
    for invariant in data["invariants"]:
        for key in ("source", "primary", "secondary"):
            reference(invariant[key])
    for targets in data["lifecycle"].values():
        for target in targets:
            target["name"] = substitute(target["name"])
    return data


def _selected_recipes(addon: Addon, selection: AddonSelection):
    recipes = {recipe.id: recipe for recipe in addon.spec.instance_recipes}
    for instance in selection.instances:
        recipe = recipes.get(instance.recipe)
        if recipe is None or not instance.features.keys() <= recipe.features.keys():
            raise ContractError("instance selects an undeclared recipe or feature")
        yield instance, recipe, recipe.features | instance.features


def expand_setup(addon: Addon, selection: AddonSelection) -> Setup:
    """Resolve up to 32 explicit instances and boolean gates into ordinary setup data."""
    if (
        selection.id != addon.metadata.name
        or not selection.features.keys() <= addon.spec.features.keys()
    ):
        raise ContractError("selection does not match add-on features or identity")
    data = _active_setup(addon.spec.setup, addon.spec.features | selection.features)
    for instance, recipe, features in _selected_recipes(addon, selection):
        expanded = _instance_setup(_active_setup(recipe.setup, features), instance.id)
        for key, values in expanded.items():
            if key == "lifecycle":
                for operation, targets in values.items():
                    data[key][operation].extend(targets)
            else:
                data[key].extend(values)
    try:
        readers = {}
        for row in data["readers"]:
            previous = readers.get(row["role"])
            if previous is None:
                readers[row["role"]] = row
                continue
            if {k: v for k, v in previous.items() if k != "records"} != {
                k: v for k, v in row.items() if k != "records"
            }:
                raise ContractError("shared reader has conflicting bindings")
            previous["records"] = sorted(set(previous["records"]) | set(row["records"]))
        data["readers"] = list(readers.values())
        for operation, targets in data["lifecycle"].items():
            unique = []
            for target in targets:
                if target not in unique:
                    unique.append(target)
            data["lifecycle"][operation] = unique
        return Setup.model_validate(data)
    except ValidationError:
        raise ContractError(
            "expanded setup has invalid names, references, or ownership collisions"
        ) from None


def expand_identities(addon: Addon, selection: AddonSelection) -> list[IdentityRequest]:
    features = addon.spec.features | selection.features
    rows = []
    for identity in addon.spec.identities:
        if identity.when is None or features[identity.when]:
            rows.append(identity.model_dump(by_alias=True, exclude={"when"}))
    for instance, recipe, features in _selected_recipes(addon, selection):
        for identity in recipe.identities:
            if identity.when is not None and not features[identity.when]:
                continue
            row = identity.model_dump(by_alias=True, exclude={"when"})
            row["id"] = f"{instance.id}-{row['id']}"
            row["clientId"] = row["clientId"].replace("{instance}", instance.id)
            row["secret"]["record"] = f"{instance.id}-{row['secret']['record']}"
            if row["browser"] is not None:
                row["browser"]["clientId"] = row["clientId"]
            rows.append(row)
    try:
        result = [IdentityRequest.model_validate(row) for row in rows]
        if len({i.client_id for i in result}) != len(result):
            raise ContractError("expanded identities have overlapping client IDs")
        return result
    except ValidationError:
        raise ContractError("expanded identity is invalid") from None


class _Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ContractError("YAML aliases are not supported")
        if len(self.states) > 64:
            raise ContractError("YAML nesting exceeds the limit")
        return super().compose_node(parent, index)


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ContractError("YAML keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


_Loader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_yaml(data: bytes, *, multiple: bool = False):
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ContractError("definition exceeds the size limit")
    try:
        return (
            list(yaml.load_all(data, Loader=_Loader))
            if multiple
            else yaml.load(data, Loader=_Loader)
        )
    except (ValueError, yaml.YAMLError, RecursionError):
        raise ContractError("invalid definition YAML") from None


def load(path: Path):
    try:
        with path.open("rb") as stream:
            return parse_yaml(stream.read(MAX_DOCUMENT_BYTES + 1))
    except OSError:
        raise ContractError("cannot read definition YAML") from None


def _load_model[Model: Contract](path: Path, model: type[Model]) -> Model:
    try:
        return model.model_validate(load(path))
    except ValidationError:
        raise ContractError("invalid add-on definition") from None


def load_manifest(path: Path) -> Addon:
    return _load_model(path, Addon)


def load_selection(path: Path) -> Selection:
    return _load_model(path, Selection)
