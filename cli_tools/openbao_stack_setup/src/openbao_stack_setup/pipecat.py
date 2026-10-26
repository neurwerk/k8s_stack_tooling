"""Prepare the reviewed Pipecat credential interface, never caller-supplied paths."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import cast

from openbao_stack_setup.catalog import (
    ADDONS,
    AddonCatalog,
    ExternalSecretTarget,
    HelmReleaseTarget,
    namespace_policy,
)
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError, SecretRecord

_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?")
_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_PROVIDER_GRANT = re.compile(r"provider:[a-z0-9][a-z0-9-]{0,47}:company-use")
_PREFIX = "addons/pipecat"
_BRIDGE = "auth-keycloak-api-key-bridge"


@dataclass(frozen=True)
class PipecatBot:
    """Identify only the credentials selected for one validated Kubernetes bot ID."""

    id: str
    gateway: bool
    speech: tuple[str, ...]

    def __post_init__(self) -> None:
        """Prevent invalid IDs from becoming resource names or ACL paths."""
        if not _ID.fullmatch(self.id) or any(
            field not in ("sttApiKey", "ttsApiKey") for field in self.speech
        ):
            raise OpenBaoError("Invalid Pipecat bot credential selection")


@dataclass(frozen=True)
class PipecatSelection:
    """Hold a validated selection without paths, commands, or secret values."""

    oidc: bool
    turn: bool
    bots: tuple[PipecatBot, ...]

    def paths(self, namespace: str) -> tuple[str, ...]:
        """Derive namespace-exact record reads; bridge never reads raw bot keys."""
        if namespace == "auth-keycloak":
            return ((f"{_PREFIX}/oidc",) if self.oidc else ()) + tuple(
                f"{_PREFIX}/machines/{bot.id}" for bot in self.bots if bot.gateway
            )
        if namespace == _BRIDGE:
            return tuple(f"{_PREFIX}/verifiers/{bot.id}" for bot in self.bots if bot.gateway)
        if namespace == "pipecat":
            return (
                ((f"{_PREFIX}/oidc",) if self.oidc else ())
                + ((f"{_PREFIX}/turn",) if self.turn else ())
                + tuple(
                    f"{_PREFIX}/bots/{bot.id}" for bot in self.bots if bot.gateway or bot.speech
                )
            )
        raise OpenBaoError("Invalid Pipecat delivery namespace")

    def catalog(self) -> AddonCatalog:
        """Derive only compiled resource names, never workloads from configuration."""
        targets: list[ExternalSecretTarget] = []
        if self.oidc:
            targets.extend(
                _target("pipecat-oidc-secret", ns) for ns in ("auth-keycloak", "pipecat")
            )
        if self.turn:
            targets.append(_target("pipecat-turn-secret", "pipecat"))
        for bot in self.bots:
            name = f"pipecat-bot-{bot.id}"
            if bot.gateway or bot.speech:
                targets.append(_target(name, "pipecat"))
            if bot.gateway:
                targets.append(_target(f"{name}-verifier", _BRIDGE))
                targets.append(_target(f"{name}-oidc", "auth-keycloak"))
        consumers = ()
        if self.oidc:
            consumers += (
                HelmReleaseTarget("pipecat-oidc-settings", "auth-keycloak"),
                HelmReleaseTarget("keycloak-pipecat-access", "auth-keycloak"),
                HelmReleaseTarget("keycloak-pipecat-oidc", "auth-keycloak"),
            )
        if any(bot.gateway for bot in self.bots):
            consumers += (
                HelmReleaseTarget("keycloak-pipecat-machines", "auth-keycloak"),
                HelmReleaseTarget("pipecat-gateway-registrations", _BRIDGE),
            )
        consumers += (HelmReleaseTarget("pipecat-shared", "pipecat"),)
        workloads = tuple(f"deployment/pipecat-{bot.id}" for bot in self.bots)
        workloads += ("deployment/pipecat-turn",) if self.turn else ()
        return replace(
            ADDONS["pipecat"],
            external_secrets=tuple(targets),
            consumers=consumers,
            workloads=workloads,
        )


def select_pipecat_values(client_values: object, product_values: object) -> PipecatSelection:
    """Merge the two fixed value sources in the same order as the add-on releases."""
    if not isinstance(client_values, dict) or not isinstance(product_values, dict):
        raise OpenBaoError("Invalid Pipecat values")
    shared = _settings(client_values, "pipecat")
    overrides = _settings(product_values, "pipecat")
    combined = {**shared, **overrides}
    for feature in ("oidc", "turn", "secretSync"):
        defaults = _settings(shared, feature)
        settings = _settings(overrides, feature)
        merged = {**defaults, **settings}
        if feature == "secretSync":
            merged["stores"] = {**_settings(defaults, "stores"), **_settings(settings, "stores")}
        combined[feature] = merged
    return select_pipecat(combined)


def select_pipecat(product: object) -> PipecatSelection:
    """Validate bot IDs, independent URL slugs, exact managed refs, and narrow grants."""
    if not isinstance(product, dict) or product.get("enabled") is not True:
        raise OpenBaoError("Pipecat must be explicitly enabled")
    sync = _settings(product, "secretSync")
    managed = _enabled(sync, False)
    stores = _settings(sync, "stores")
    if managed and any(
        stores.get(namespace, f"{namespace}-openbao-secret-store")
        != f"{namespace}-openbao-secret-store"
        for namespace in ("pipecat", "auth-keycloak", _BRIDGE)
    ):
        raise OpenBaoError("Pipecat delivery requires the reviewed namespace-local SecretStores")
    rows = product.get("bots", [])
    if not isinstance(rows, list) or len(rows) > 32:
        raise OpenBaoError("Invalid Pipecat bot catalog")
    identities: set[str] = set()
    slugs: set[str] = set()
    bots: list[PipecatBot] = []
    protected = False
    for row in rows:
        if not isinstance(row, dict):
            raise OpenBaoError("Invalid Pipecat bot catalog")
        identity = row.get("id")
        slug = row.get("urlSlug", identity)
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or identity in identities
            or not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or slug in slugs
            or slug in ("healthz", "assets")
            or type(row.get("anonymous")) is not bool
            or type(row.get("sessionsEnabled", False)) is not bool
        ):
            raise OpenBaoError("Invalid or duplicate Pipecat bot ID or URL slug")
        identities.add(identity)
        slugs.add(slug)
        anonymous = row["anonymous"]
        protected |= not anonymous
        bots.append(_select_bot(row, identity, anonymous, managed))
    oidc = _settings(product, "oidc")
    oidc_enabled = _enabled(oidc, protected)
    if oidc_enabled != protected:
        raise OpenBaoError("Pipecat OIDC selection must match the protected bot catalog")
    if (
        managed
        and oidc_enabled
        and (
            oidc.get("clientId", "pipecat") != "pipecat"
            or oidc.get("secretName", "pipecat-oidc-secret") != "pipecat-oidc-secret"
            or oidc.get("secretKey", "clientSecret") != "clientSecret"
        )
    ):
        raise OpenBaoError("Pipecat OIDC requires the reviewed client and Secret reference")
    turn = _settings(product, "turn")
    turn_enabled = _enabled(turn, False)
    if (
        managed
        and turn_enabled
        and turn.get("secretName", "pipecat-turn-secret") != "pipecat-turn-secret"
    ):
        raise OpenBaoError("Managed Pipecat TURN requires the reviewed Secret reference")
    return PipecatSelection(oidc_enabled and managed, turn_enabled and managed, tuple(bots))


def _select_bot(
    row: dict[str, object], identity: str, anonymous: bool, managed: bool
) -> PipecatBot:
    name = f"pipecat-bot-{identity}"
    if not anonymous and "gatewaySecretRef" in row:
        raise OpenBaoError("Protected Pipecat bots cannot select a gateway key")
    gateway = (
        anonymous
        and _managed_ref(row.get("gatewaySecretRef"), name, "gatewayApiKey", True)
        and managed
    )
    if gateway:
        permissions = row.get("gatewayPermissions", [])
        if not isinstance(permissions, list):
            raise OpenBaoError("Invalid Pipecat gateway permissions")
        gateway = bool(permissions)
        if gateway:
            _validate_grants(row)
        elif row.get("sessionsEnabled", False):
            raise OpenBaoError("Enabled anonymous Pipecat sessions require explicit gateway grants")
    fields = []
    for direction in ("stt", "tts"):
        speech = _settings(row, direction)
        field = f"{direction}ApiKey"
        if _managed_ref(speech.get("secretRef"), name, field, False) and managed:
            fields.append(field)
    return PipecatBot(identity, gateway, tuple(fields))


def _validate_grants(row: dict[str, object]) -> None:
    model = row.get("modelId")
    permissions = row.get("gatewayPermissions")
    if (
        not isinstance(model, str)
        or not _MODEL.fullmatch(model)
        or not isinstance(permissions, list)
        or not all(isinstance(item, str) for item in permissions)
    ):
        raise OpenBaoError("Managed Pipecat bots require explicit canonical model grants")
    required = {"llm:invoke", f"model:{model}:invoke"}
    if (
        len(permissions) != len(set(permissions))
        or not required.issubset(permissions)
        or any(item not in required and not _PROVIDER_GRANT.fullmatch(item) for item in permissions)
    ):
        raise OpenBaoError("Pipecat grants must include only invocation and explicit company-use")


def _managed_ref(value: object, name: str, key: str, default: bool) -> bool:
    if value is None:
        return default
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "key"}
        or any(not isinstance(item, str) or not item for item in value.values())
    ):
        raise OpenBaoError("Invalid Pipecat Secret reference")
    if value == {"name": name, "key": key}:
        return True
    # Existing external credentials are never read, imported, or provisioned.
    if value["name"].startswith("pipecat-bot-"):
        raise OpenBaoError("Pipecat managed Secret reference does not match its bot and field")
    return False


def _settings(product: dict[str, object], name: str) -> dict[str, object]:
    value = product.get(name, {})
    if not isinstance(value, dict):
        raise OpenBaoError("Invalid Pipecat feature settings")
    if any(not isinstance(key, str) for key in value):
        raise OpenBaoError("Invalid Pipecat feature settings")
    return cast(dict[str, object], value)


def _enabled(settings: dict[str, object], default: bool) -> bool:
    enabled = settings.get("enabled", default)
    if type(enabled) is not bool:
        raise OpenBaoError("Pipecat feature enabled value must be a boolean")
    return enabled


def _target(name: str, namespace: str) -> ExternalSecretTarget:
    return ExternalSecretTarget(name, namespace, name)


def reconcile_pipecat(
    client: OpenBaoClient,
    selection: PipecatSelection | None,
    speech_prompt: Callable[[str, str], str],
) -> None:
    """Create missing credentials with CAS; retain valid values and reject verifier conflicts."""
    for namespace in ("auth-keycloak", _BRIDGE):
        client.write_policy(namespace, namespace_policy(namespace))
    if selection is None:
        client.delete_policy("pipecat")
        client.delete_kubernetes_role("pipecat")
        return
    if selection.oidc:
        _single_record(client, "oidc", "clientSecret", lambda: secrets.token_urlsafe(32))
    if selection.turn:
        _single_record(client, "turn", "sharedSecret", lambda: secrets.token_hex(32))
    for bot in selection.bots:
        if bot.gateway or bot.speech:
            _reconcile_bot(client, bot, speech_prompt)
        if bot.gateway:
            _single_record(
                client, f"machines/{bot.id}", "clientSecret", lambda: secrets.token_urlsafe(32)
            )
    for namespace in ("pipecat", "auth-keycloak", _BRIDGE):
        paths = selection.paths(namespace)
        policy = "\n".join(
            f'path "secret/{kind}/{path}" {{ capabilities = ["read"] }}'
            for path in paths
            for kind in ("data", "metadata")
        )
        if namespace != "pipecat":
            policy = namespace_policy(namespace) + policy
        else:
            policy = policy or namespace_policy(namespace)
            client.write_kubernetes_role(namespace)
        client.write_policy(namespace, policy)


def _single_record(
    client: OpenBaoClient, suffix: str, field: str, generate: Callable[[], str]
) -> None:
    path = f"{_PREFIX}/{suffix}"
    record = client.read_secret(path)
    if record is None:
        client.write_secret(path, {field: generate()}, 0)
    elif set(record.values) != {field} or not _valid_value(record.values[field]):
        raise OpenBaoError("Invalid existing Pipecat credential; refusing replacement")
    elif suffix == "turn" and not re.fullmatch(r"[0-9a-f]{64}", str(record.values[field])):
        raise OpenBaoError("Invalid existing Pipecat TURN credential; refusing replacement")


def _reconcile_bot(
    client: OpenBaoClient, bot: PipecatBot, speech_prompt: Callable[[str, str], str]
) -> None:
    path = f"{_PREFIX}/bots/{bot.id}"
    current = client.read_secret(path)
    values = dict(current.values) if current else {}
    if set(values) - {"gatewayApiKey", "sttApiKey", "ttsApiKey"} or any(
        not _valid_value(value) for value in values.values()
    ):
        raise OpenBaoError("Invalid existing Pipecat bot credentials; refusing replacement")
    verifier_path = f"{_PREFIX}/verifiers/{bot.id}"
    verifier = client.read_secret(verifier_path) if bot.gateway else None
    if verifier is not None:
        _verify_key(values, verifier)
    if bot.gateway and "gatewayApiKey" not in values:
        values["gatewayApiKey"] = secrets.token_urlsafe(32)
    for field in bot.speech:
        if field not in values:
            value = speech_prompt(bot.id, field)
            if not _valid_value(value):
                raise OpenBaoError("Invalid Pipecat speech credential; values are hidden")
            values[field] = value
    if current is None or values != current.values:
        client.write_secret(path, values, current.version if current else 0)
    if bot.gateway and verifier is None:
        digest = hashlib.sha256(str(values["gatewayApiKey"]).encode()).hexdigest()
        client.write_secret(verifier_path, {"gatewayApiKeySha256": digest}, 0)


def _verify_key(values: dict[str, JsonValue], verifier: SecretRecord) -> None:
    key = values.get("gatewayApiKey")
    if (
        not isinstance(key, str)
        or set(verifier.values) != {"gatewayApiKeySha256"}
        or verifier.values["gatewayApiKeySha256"] != hashlib.sha256(key.encode()).hexdigest()
    ):
        raise OpenBaoError("Pipecat gateway key and verifier conflict; refusing replacement")


def _valid_value(value: JsonValue) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= 16384
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )
