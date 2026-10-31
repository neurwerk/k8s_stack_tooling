"""One-time, fixed legacy LibreChat credential ownership handoff."""

from k8s_stack_tooling.addons import Setup

from openbao_stack_setup.catalog import RECONCILIATION_STATE_PATH
from openbao_stack_setup.client import JsonValue, OpenBaoClient, OpenBaoError, SecretRecord
from openbao_stack_setup.declarative import _validate_field
from openbao_stack_setup.reconcile import ReconciliationIdentity, _load_state

_INTERNAL = "frontend-librechat/internal"
_RUNTIME = (
    "credsKey",
    "credsIv",
    "jwtSecret",
    "jwtRefreshSecret",
    "meiliMasterKey",
    "openidSessionSecret",
    "valkeyPassword",
    "adminPanelSessionSecret",
    "adminPanelMetricsSecret",
)
_MAPPING = {
    "runtime": (_INTERNAL, _RUNTIME),
    "database": (_INTERNAL, ("documentdbUser", "documentdbPassword")),
    "oidc": (_INTERNAL, ("openidClientSecret",)),
    "speech": ("frontend-librechat/external", ("sttApiKey", "ttsApiKey")),
}


def preserve_librechat(
    client: OpenBaoClient, identity: ReconciliationIdentity, setup: Setup, *, active_setup: Setup
) -> None:
    """Copy validated existing keys with CAS; mark completion only after readback."""
    state = client.read_secret(RECONCILIATION_STATE_PATH)
    loaded = _load_state(state, identity, preserving_librechat=True)
    if state is None or loaded.applied_version not in (2, 3, 4):
        raise OpenBaoError(
            "LibreChat preservation requires existing reconciliation state 2, 3 or 4"
        )
    records = {record.id: record for record in setup.records}
    if set(records) != set(_MAPPING):
        raise OpenBaoError("LibreChat preservation requires the reviewed four-record definition")
    sources = {
        path: (
            client.read_secret(path) or SecretRecord({}, 0)
            if path == "frontend-librechat/external"
            else _required(client, path)
        )
        for path in (
            _INTERNAL,
            "frontend-librechat/external",
            "auth-keycloak/internal",
            "infra-postgres-operations/internal",
        )
    }
    internal = sources[_INTERNAL].values
    if internal.get("openidClientSecret") != sources["auth-keycloak/internal"].values.get(
        "librechatOidcClientSecret"
    ) or internal.get("documentdbPassword") != sources[
        "infra-postgres-operations/internal"
    ].values.get("documentdbPassword"):
        raise OpenBaoError("Legacy LibreChat duplicate credentials disagree")
    destinations, expected = _destinations(client, setup, active_setup, sources)
    snapshots = {**sources, **destinations, RECONCILIATION_STATE_PATH: state}
    _unchanged(client, snapshots)
    for path, values in expected.items():
        current = destinations[path]
        if all(current.values.get(field) == value for field, value in values.items()):
            continue
        _unchanged(client, snapshots)
        client.write_secret(path, {**current.values, **values}, current.version)
        snapshots[path] = SecretRecord({**current.values, **values}, current.version + 1)
    _unchanged(client, snapshots)
    # Keep appliedVersion unchanged: normal reconciliation must still apply its catalog.
    evidence: dict[str, JsonValue] = {
        path: record.version
        for path, record in snapshots.items()
        if path != RECONCILIATION_STATE_PATH
    }
    if state.values.get("librechatPreserved") != evidence:
        values = {**state.values, "librechatPreserved": evidence}
        client.write_secret(RECONCILIATION_STATE_PATH, values, state.version)
        snapshots[RECONCILIATION_STATE_PATH] = SecretRecord(values, state.version + 1)
    # Evidence is not an atomic multi-record lock. Recheck after its CAS write;
    # ordinary reconciliation must independently verify it before accepting state 2.
    _unchanged(client, snapshots)


def _destinations(
    client: OpenBaoClient, setup: Setup, active_setup: Setup, sources: dict[str, SecretRecord]
) -> tuple[dict[str, SecretRecord], dict[str, dict[str, str]]]:
    records = {record.id: record for record in setup.records}
    active = {(record.id, field) for record in active_setup.records for field in record.fields}
    destinations: dict[str, SecretRecord] = {}
    expected: dict[str, dict[str, str]] = {}
    for name, (source, fields) in _MAPPING.items():
        record = records[name]
        if record.path != f"addons/librechat/{'external/speech' if name == 'speech' else name}":
            raise OpenBaoError("Unexpected LibreChat credential destination")
        if set(record.fields) != set(fields):
            raise OpenBaoError("LibreChat preservation requires all reviewed credential fields")
        values = {}
        for field in fields:
            if (
                field not in sources[source].values
                and name == "speech"
                and (name, field) not in active
            ):
                continue
            value = sources[source].values.get(field)
            definition = record.fields[field]
            if (
                not isinstance(value, str)
                or not value
                or (definition.literal is not None and value != definition.literal)
            ):
                raise OpenBaoError("Legacy LibreChat credential is missing or invalid")
            _validate_field(value, definition)
            values[field] = value
        current = client.read_secret(record.path) or SecretRecord({}, 0)
        if any(
            field in current.values and current.values[field] != value
            for field, value in values.items()
        ):
            raise OpenBaoError("Existing LibreChat destination conflicts with legacy credentials")
        destinations[record.path] = current
        expected[record.path] = values
    return destinations, expected


def _required(client: OpenBaoClient, path: str) -> SecretRecord:
    record = client.read_secret(path)
    if record is None:
        raise OpenBaoError("Legacy LibreChat credential source is missing")
    return record


def _unchanged(client: OpenBaoClient, snapshots: dict[str, SecretRecord]) -> None:
    for path, expected in snapshots.items():
        actual = client.read_secret(path) or SecretRecord({}, 0)
        if actual.version != expected.version or actual.values != expected.values:
            raise OpenBaoError("LibreChat preservation snapshot changed; retry after review")
