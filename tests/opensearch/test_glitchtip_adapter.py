"""Synthetic behavioral checks reviewed by Astra; no client log fixtures."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from pydantic import SecretStr

from k8s_stack_tooling.opensearch_glitchtip import main
from k8s_stack_tooling.opensearch_glitchtip import transport as transport_module
from k8s_stack_tooling.opensearch_glitchtip.health import Health
from k8s_stack_tooling.opensearch_glitchtip.policy import Policy, identity_parts
from k8s_stack_tooling.opensearch_glitchtip.preview import preview
from k8s_stack_tooling.opensearch_glitchtip.settings import (
    MAX_RECORDS,
    Scope,
    Settings,
    destination,
    load_scopes,
    stamp,
)
from k8s_stack_tooling.opensearch_glitchtip.state import Record, State, state_lock
from k8s_stack_tooling.opensearch_glitchtip.transport import (
    AdapterError,
    ResponseTooLargeError,
    Transport,
    WindowFullError,
    brain_query,
    literal,
    parse_labels,
    parse_record,
    selection,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
SCOPE = Scope("chat", "chat", "api", ("chat",), ("api",), ("application_error",))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    ca = tmp_path / "ca.crt"
    ca.write_text("synthetic CA; no network in these checks")
    scopes = tmp_path / "scopes.json"
    scopes.write_text(json.dumps([SCOPE.binding()]))
    return Settings(
        opensearch_url="https://search.example.test:9200",
        opensearch_username="reader",
        opensearch_password=SecretStr("secret-reader-password"),
        opensearch_ca_file=ca,
        glitchtip_dsn=SecretStr("https://project-public-key@errors.example.test/1"),
        environment="test",
        scopes_file=scopes,
        state_path=tmp_path / "state.sqlite3",
        initial_start_at=stamp(START),
    )


def record(id: str, message: str = "request 1 failed", scope: Scope = SCOPE) -> Record:
    return Record(
        "fluent-bit-2026.01.01",
        id,
        stamp(START),
        message,
        scope.namespaces[0],
        scope.containers[0],
        scope.failure_types[0],
        "api:1.0",
    )


def payloads(state: State) -> list[dict[str, Any]]:
    return [json.loads(payload) for _, payload, _ in state.due(0)]


def tracing(
    body: str = "request failed",
    timestamp: str = "2026-01-01T00:00:00Z",
    module: str = "example::api",
    level: str = "ERROR",
    color: str = "",
) -> str:
    return (
        f"{timestamp} {color}{level}\x1b[0m {module}: {body}"
        if color
        else (f"{timestamp} {level} {module}: {body}")
    )


def ppl(messages: list[str], template: str = "request <*> failed") -> dict[str, Any]:
    # Reverse row order intentionally; association must not use row positions.
    return {
        "schema": [{"name": "patterns_field", "type": "string"}, {"name": "log", "type": "string"}],
        "datarows": [[template, message] for message in reversed(messages)],
        "total": len(messages),
        "size": len(messages),
    }


def search(records: list[Record]) -> dict[str, Any]:
    return {
        "timed_out": False,
        "_shards": {"total": 2, "successful": 2, "failed": 0},
        "hits": {
            "total": {"relation": "eq", "value": len(records)},
            "hits": [
                {
                    "_index": r.index,
                    "_id": r.id,
                    "_source": {
                        "@timestamp": r.timestamp,
                        "log": r.message,
                        "kubernetes": {
                            "namespace_name": r.namespace,
                            "container_name": r.container,
                            "container_image": r.image,
                        },
                        "stack_log": {
                            "failure_type": r.failure_type,
                            **({"level": r.original_level} if r.original_level is not None else {}),
                        },
                    },
                }
                for r in records
            ],
        },
    }


def test_distinct_occurrences_overlap_and_restart(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    raw = "\x1b[31m2026-01-01T00:00:00Z ERROR example::api: request 1 failed\x1b[0m"
    records = [record("one", raw), record("two", raw)]
    upper = START + timedelta(minutes=5)
    assert state.enqueue(SCOPE, records, {records[0].message: "request <*> failed"}, upper) == 2
    events = payloads(state)
    assert len({event["event_id"] for event in events}) == 2
    assert len({tuple(event["fingerprint"]) for event in events}) == 1
    assert all(event["message"] == "request 1 failed" for event in events)
    source = state.digest("source", SCOPE.id, records[0].index, records[0].id)
    assert events[0]["event_id"] == state.digest("event", source)[:32]
    assert events[0]["timestamp"] == stamp(START)
    assert events[0]["tags"] == {
        "application": "chat",
        "service": "api",
        "namespace": "chat",
        "container": "api",
        "failure_type": "application_error",
    }
    assert events[0]["environment"] == "test"
    assert events[0]["level"] == "error"
    assert events[0]["platform"] == "other"
    assert events[0]["release"] == "api:1.0"
    assert "user" not in events[0]
    for source, _, _ in state.due(0):
        state.acknowledge(source)
    state.close()
    state = State(settings, [SCOPE])
    try:
        assert state.cursor(SCOPE, START) == upper
        assert state.enqueue(SCOPE, records, {records[0].message: "changed"}, upper) == 0
        assert state.pending_count() == 0
        assert state.db.execute("SELECT payload FROM records").fetchall() == [(None,), (None,)]
    finally:
        state.close()


def test_tracing_repeats_group_across_sparse_windows_restart_and_overlap(
    settings: Settings,
) -> None:
    first = record("one", tracing(color="\x1b[31m"))
    second = record("two", tracing(timestamp="2026-01-01T01:00:00Z", color="\x1b[2;32m"))
    third = record("three", tracing(timestamp="2026-01-01T02:00:00Z"))
    upper = START + timedelta(hours=3)
    state = State(settings, [SCOPE])
    state.enqueue(SCOPE, [first], {first.message: first.message}, START)
    original = state.due(0)[0][1]
    state.close()
    state = State(settings, [SCOPE])
    try:
        assert (
            state.enqueue(
                SCOPE,
                [first, second],
                dict.fromkeys([first.message, second.message], "different Brain label"),
                upper,
            )
            == 1
        )
        assert state.enqueue(SCOPE, [third], {third.message: "another label"}, upper) == 1
        events = payloads(state)
        assert len(events) == 3
        assert len({event["event_id"] for event in events}) == 3
        assert len({tuple(event["fingerprint"]) for event in events}) == 1
        assert state.due(0)[0][1] == original
        assert state.cursor(SCOPE, START) == upper
    finally:
        state.close()


@pytest.mark.parametrize("later_window", [False, True])
@pytest.mark.parametrize(
    "difference",
    [
        "cause",
        "code",
        "body_time",
        "url",
        "request_id",
        "whitespace",
        "indentation",
        "module",
        "level",
        "namespace",
        "container",
        "failure_type",
    ],
)
def test_same_brain_label_cannot_merge_different_content_or_context(
    settings: Settings, difference: str, later_window: bool
) -> None:
    scope = replace(
        SCOPE,
        namespaces=("chat", "other"),
        containers=("api", "worker"),
        failure_types=("application_error", "connection_error"),
    )
    body = (
        "request failed code=301 cause=reset body_time=2026-01-01T00:00:00Z "
        "url=https://db.example.test request_id=abc\n detail  "
    )
    first = record("one", tracing(body), scope)
    changed = {
        "cause": body.replace("cause=reset", "cause=certificate expired"),
        "code": body.replace("code=301", "code=302"),
        "body_time": body.replace("00:00:00Z", "00:00:01Z"),
        "url": body.replace("db.example.test", "other.example.test"),
        "request_id": body.replace("request_id=abc", "request_id=def"),
        "whitespace": body.rstrip(),
        "indentation": " " + body,
    }
    second = record("two", tracing(changed.get(difference, body)), scope)
    if difference in ("module", "level"):
        second = replace(
            second,
            message=tracing(
                body, **{difference: "example::worker" if difference == "module" else "WARN"}
            ),
        )
    elif difference in ("namespace", "container", "failure_type"):
        second = replace(
            second,
            **{
                difference: {
                    "namespace": "other",
                    "container": "worker",
                    "failure_type": "connection_error",
                }[difference]
            },
        )
    labels = dict.fromkeys([first.message, second.message], "<*> failed")
    state = State(settings, [scope])
    try:
        if later_window:
            state.enqueue(scope, [first], labels, START)
            state.enqueue(scope, [second], labels, START + timedelta(minutes=1))
        else:
            state.enqueue(scope, [first, second], labels, START)
        assert len({tuple(event["fingerprint"]) for event in payloads(state)}) == 2
    finally:
        state.close()


@pytest.mark.parametrize(
    "prefix",
    [
        "2026-13-01T00:00:00Z ERROR example::api: ",
        "2026-01-01T00:00:00+00:99 ERROR example::api: ",
        "2026-01-01T00:00:00Z NOTICE example::api: ",
        "2026-01-01T00:00:00Z ERROR example.api: ",
        "context: 2026-01-01T00:00:00Z ERROR example::api: ",
        "\x1b[31" + "2026-01-01T00:00:00Z ERROR example::api: ",
        "2026-01-01T00:00:00Z ERROR example::api: \x1b]0;title\x07",
        "2026-01-01T00:00:00Z ERROR example::api: \x1b[2J",
        "2026-01-01T00:00:00Z ERROR example::api: \x1b[8m",
        "2026-01-01T00:00:00Z ERROR example::api: \x07",
    ],
)
def test_unknown_envelopes_and_terminal_controls_keep_exact_grouping(
    settings: Settings, prefix: str
) -> None:
    raw = [prefix + "failed", prefix.replace("T00:00:00", "T00:00:01") + "failed"]
    state = State(settings, [SCOPE])
    try:
        state.enqueue(
            SCOPE,
            [record(str(i), message) for i, message in enumerate(raw)],
            dict.fromkeys(raw, "same broad label"),
            START,
        )
        assert len({tuple(event["fingerprint"]) for event in payloads(state)}) == 2
    finally:
        state.close()


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_outbox_is_preserved_without_inheriting_historical_groups(
    settings: Settings, reverse: bool
) -> None:
    # Freeze the v1 schema and identity recipe rather than enqueueing with new code.
    key = b"synthetic legacy identity key!!!"
    assert len(key) == 32

    def digest(*parts: str) -> str:
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()

    records = [
        record("old-one", tracing()),
        record("old-two", tracing(timestamp="2026-01-01T01:00:00Z", color="\x1b[31m")),
    ]
    identities = ["a" * 64, "b" * 64]
    sources = [digest("source", SCOPE.id, r.index, r.id) for r in records]
    pending = json.dumps(
        {
            "event_id": digest("event", sources[0])[:32],
            "message": records[0].message,
            "fingerprint": [identities[0]],
        },
        indent=2,
    )
    binding = json.dumps(
        {
            "version": 1,
            "destination": destination(settings.glitchtip_dsn.get_secret_value())[2],
            "source": settings.opensearch_url.rstrip("/"),
            "environment": settings.environment,
            "initial_start_at": stamp(datetime.fromisoformat(settings.initial_start_at)),
            "scopes": [SCOPE.binding()],
        },
        sort_keys=True,
    )
    with sqlite3.connect(settings.state_path) as db:
        db.executescript("""
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB);
            CREATE TABLE cursors (scope TEXT PRIMARY KEY, upper TEXT);
            CREATE TABLE messages (scope TEXT, digest TEXT, identity TEXT,
                PRIMARY KEY(scope, digest));
            CREATE TABLE templates (scope TEXT, digest TEXT, identity TEXT,
                PRIMARY KEY(scope, digest, identity));
            CREATE TABLE records (source TEXT PRIMARY KEY, event_id TEXT UNIQUE,
                payload TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
            CREATE INDEX pending_records ON records(delivered, next_attempt);
        """)
        db.executemany(
            "INSERT INTO metadata VALUES (?, ?)", [("binding", binding), ("identity_key", key)]
        )
        db.execute("INSERT INTO cursors VALUES (?, ?)", (SCOPE.id, stamp(START)))
        legacy_mappings = [
            (SCOPE.id, digest("message", SCOPE.id, r.message), identity)
            for r, identity in zip(records, identities, strict=True)
        ]
        db.executemany("INSERT INTO messages VALUES (?, ?, ?)", legacy_mappings)
        db.executemany(
            "INSERT INTO templates VALUES (?, ?, ?)",
            [
                (SCOPE.id, digest("template", SCOPE.id, r.message), identity)
                for r, identity in zip(records, identities, strict=True)
            ],
        )
        db.executemany(
            "INSERT INTO records(source, event_id, payload, delivered) VALUES (?, ?, ?, ?)",
            [
                (source, digest("event", source)[:32], pending if i == 0 else None, i)
                for i, source in enumerate(sources)
            ],
        )
    state = State(settings, [SCOPE])
    ordered = list(reversed(records)) if reverse else records
    try:
        assert state.enqueue(SCOPE, ordered, {r.message: "label" for r in records}, START) == 0
        assert state.due(0) == [(sources[0], pending, 0)]
        assert state.db.execute(
            "SELECT delivered, payload FROM records WHERE source=?", (sources[1],)
        ).fetchone() == (1, None)
        for i, old in enumerate(ordered):
            fresh = replace(old, id=f"new-{i}")
            state.enqueue(SCOPE, [fresh], {fresh.message: "changed label"}, START)
        state.close()
        state = State(settings, [SCOPE])
        fresh = record("new-variant", tracing(timestamp="2026-01-01T02:00:00Z"))
        state.enqueue(SCOPE, [fresh], {fresh.message: "another label"}, START)
        events = payloads(state)
        new_identities = {tuple(event["fingerprint"]) for event in events[1:]}
        assert len(events) == 4
        assert len(new_identities) == 1
        assert new_identities.isdisjoint({(identity,) for identity in identities})
        assert state.due(0)[0][1] == pending
        for scope_id, message_digest, identity in legacy_mappings:
            assert state.db.execute(
                "SELECT identity FROM messages WHERE scope=? AND digest=?",
                (scope_id, message_digest),
            ).fetchone() == (identity,)
    finally:
        state.close()


def test_atomic_window_rollback(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    try:
        state.db.execute(
            "CREATE TRIGGER fail_cursor BEFORE INSERT ON cursors "
            "BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END"
        )
        with pytest.raises(Exception, match="synthetic crash"):
            state.enqueue(
                SCOPE,
                [record("one")],
                {"request 1 failed": "request <*> failed"},
                START + timedelta(minutes=1),
            )
        for table in ("records", "messages", "templates", "cursors"):
            assert state.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        state.db.execute("DROP TRIGGER fail_cursor")
        assert (
            state.enqueue(
                SCOPE,
                [record("one")],
                {"request 1 failed": "request <*> failed"},
                START + timedelta(minutes=1),
            )
            == 1
        )
    finally:
        state.close()


def test_lost_ack_retry_is_immutable_and_pending_never_expires(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    sent: list[str] = []

    def deliver(payload: str) -> None:
        sent.append(payload)
        if len(sent) == 1:
            raise AdapterError("ingestion acknowledgement mismatch; acceptance uncertain")

    monkeypatch.setattr(transport, "deliver", deliver)
    raw = "\x1b[31m2026-01-01T00:00:00Z ERROR example::api: request 1 failed\x1b[0m"
    legacy_record = record("one", raw)
    # Persist the genuine old payload contract before restoring the new
    # formatter. Retry must deliver these original bytes, not clean them.
    with monkeypatch.context() as legacy:
        legacy.setattr(
            "k8s_stack_tooling.opensearch_glitchtip.policy.title_text", lambda message: message
        )
        state.enqueue(SCOPE, [legacy_record], {raw: "request <*> failed"}, START)
    legacy_payload = state.due(0)[0][1]
    assert json.loads(legacy_payload)["message"] == raw
    main.deliver_due(state, transport, 0)
    assert state.pending_count() == 1
    assert not state.due(1)
    state.close()
    state = State(settings, [SCOPE])
    try:
        # A changed template during overlap must not rewrite the pending payload.
        assert state.enqueue(SCOPE, [legacy_record], {raw: "changed"}, START) == 0
        main.deliver_due(state, transport, 10**10)
        assert sent[0] == sent[1] == legacy_payload
        assert state.pending_count() == 0
        assert "acceptance uncertain" in capsys.readouterr().out
    finally:
        transport.close()
        state.close()


def test_backoff_is_bounded_without_expiring_pending(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    try:
        state.enqueue(SCOPE, [record("one")], {"request 1 failed": "template"}, START)
        source = state.due(0)[0][0]
        state.defer(source, 10000, 100)
        assert not state.due(399)
        assert state.due(400)
        assert state.pending_count() == 1
    finally:
        state.close()


def test_generalization_preserves_known_and_isolates_new(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    try:
        state.enqueue(
            SCOPE,
            [record("one", "alpha failed"), record("two", "beta failed")],
            {"alpha failed": "alpha failed", "beta failed": "beta failed"},
            START,
        )
        old = {event["message"]: event["fingerprint"] for event in payloads(state)}
        state.enqueue(
            SCOPE,
            [
                record("three", "alpha failed"),
                record("four", "beta failed"),
                record("five", "gamma failed"),
                record("six", "delta failed"),
            ],
            dict.fromkeys(
                ["alpha failed", "beta failed", "gamma failed", "delta failed"], "<*> failed"
            ),
            START,
        )
        events = payloads(state)
        assert all(
            event["fingerprint"] == old[event["message"]]
            for event in events
            if event["message"] in old
        )
        identities = {tuple(event["fingerprint"]) for event in events}
        assert len(identities) == 4
        state.close()
        state = State(settings, [SCOPE])
        state.enqueue(
            SCOPE, [record("seven", "gamma failed")], {"gamma failed": "another template"}, START
        )
        gamma = [
            event["fingerprint"] for event in payloads(state) if event["message"] == "gamma failed"
        ]
        assert gamma[0] == gamma[1]
    finally:
        state.close()


def test_single_known_message_does_not_bridge_novel_generalization(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    try:
        state.enqueue(
            SCOPE, [record("one", "alpha failed")], {"alpha failed": "alpha failed"}, START
        )
        state.enqueue(
            SCOPE,
            [record("two", "alpha failed"), record("three", "beta failed")],
            {"alpha failed": "<*> failed", "beta failed": "<*> failed"},
            START,
        )
        identities = {event["message"]: event["fingerprint"] for event in payloads(state)}
        assert identities["alpha failed"] != identities["beta failed"]
    finally:
        state.close()


def test_state_binding_scope_environment_destination_and_missing_key(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    state.close()
    for field, value in (
        ("environment", "other"),
        ("glitchtip_dsn", "https://key@errors.example.test/2"),
    ):
        changed = Settings.model_validate({**settings.model_dump(), field: value})
        with pytest.raises(ValueError, match="binding mismatch"):
            State(changed, [SCOPE])
    with pytest.raises(ValueError, match="binding mismatch"):
        State(settings, [replace(SCOPE, service="worker")])
    state = State(settings, [SCOPE])
    with state.db:
        state.db.execute("DELETE FROM metadata WHERE name='identity_key'")
    state.close()
    with pytest.raises(ValueError, match="identity key missing"):
        State(settings, [SCOPE])


def test_scope_isolation_and_process_lifetime_lock(settings: Settings) -> None:
    other = replace(SCOPE, id="other", namespaces=("other",))
    with state_lock(settings.state_path):
        with pytest.raises(ValueError, match="already owned"):
            with state_lock(settings.state_path):
                pass
        state = State(settings, [SCOPE, other])
        try:
            for scope in (SCOPE, other):
                state.enqueue(
                    scope,
                    [record("one", scope=scope)],
                    {"request 1 failed": "request <*> failed"},
                    START,
                )
            assert len({tuple(event["fingerprint"]) for event in payloads(state)}) == 2
        finally:
            state.close()
    with state_lock(settings.state_path):
        pass


def test_population_multiset_not_order_and_duplicate_multiplicity() -> None:
    records = [record("one", "a"), record("two", "a"), record("three", "b")]
    assert parse_labels(ppl(["a", "b", "a"]), records) == {
        "a": "request <*> failed",
        "b": "request <*> failed",
    }
    with pytest.raises(AdapterError, match="populations differ"):
        parse_labels(ppl(["a", "b", "b"]), records)
    with pytest.raises(AdapterError, match="populations differ"):
        parse_labels(ppl(["a", "b"]), records)
    conflict = ppl(["a", "a"])
    conflict["datarows"][1][0] = "conflicting template"
    with pytest.raises(AdapterError, match="conflicting Brain labels"):
        parse_labels(conflict, records[:2])


@pytest.mark.parametrize(
    "field,value", [("cursor", "next"), ("timed_out", True), ("total", 4), ("size", 0)]
)
def test_ppl_partial_truncated_or_paged_is_rejected(field: str, value: Any) -> None:
    response = ppl(["a"])
    response[field] = value
    with pytest.raises(AdapterError):
        parse_labels(response, [record("one", "a")])


@pytest.mark.parametrize("failure", ["timeout", "partial", "truncated", "inexact", "cap"])
def test_raw_search_completion_required(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    response = search([record("one")])
    if failure == "timeout":
        response["timed_out"] = True
    elif failure == "partial":
        response["_shards"]["successful"] = 1
    elif failure == "truncated":
        response["hits"]["total"]["value"] = 2
    elif failure == "inexact":
        response["hits"]["total"]["relation"] = "gte"
    else:
        response["hits"]["total"]["value"] = MAX_RECORDS + 1
    transport = Transport(settings)
    monkeypatch.setattr(transport, "request", lambda *_: response)
    try:
        with pytest.raises(AdapterError):
            transport.window(SCOPE, START, START + timedelta(minutes=1))
    finally:
        transport.close()


def test_half_open_boundaries_and_late_arrival(settings: Settings) -> None:
    upper = START + timedelta(minutes=1)
    assert selection(SCOPE, START, upper)["bool"]["filter"][0] == {
        "range": {"@timestamp": {"gte": stamp(START), "lt": stamp(upper)}}
    }
    row = search([record("one")])["hits"]["hits"][0]
    assert parse_record(row, SCOPE, START, upper).timestamp == stamp(START)
    row["_source"]["@timestamp"] = stamp(upper)
    with pytest.raises(ValueError):
        parse_record(row, SCOPE, START, upper)
    state = State(settings, [SCOPE])
    try:
        state.enqueue(SCOPE, [], {}, upper)
        late = replace(record("late"), timestamp=stamp(START + timedelta(seconds=30)))
        assert state.enqueue(SCOPE, [late], {late.message: "request <*> failed"}, upper) == 1
        assert state.cursor(SCOPE, START) == upper
    finally:
        state.close()


def test_same_timestamp_cap_terminates_and_initial_overlap_is_clamped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stop = threading.Event()
    calls: list[tuple[datetime, datetime]] = []

    class FullTransport:
        def __init__(self, _settings: Settings):
            pass

        def window(self, _scope: Scope, lower: datetime, upper: datetime):
            calls.append((lower, upper))
            raise WindowFullError("synthetic cap")

        def close(self):
            pass

    monkeypatch.setattr(main, "Transport", FullTransport)
    monkeypatch.setattr(stop, "wait", lambda _: stop.set())
    main.run(settings, stop)
    assert 1 < len(calls) < 30
    assert all(lower == START for lower, _ in calls)
    assert "same_timestamp_or_minimum_window_cap" in capsys.readouterr().out
    state = State(settings, [SCOPE])
    try:
        assert state.cursor(SCOPE, START) == START
        assert state.pending_count() == 0
    finally:
        state.close()


def test_bounded_subwindows_preserve_every_occurrence(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    occurrences = [
        replace(record(str(i)), timestamp=stamp(START + timedelta(seconds=i)))
        for i in (0, 60, 120, 180, 240)
    ]

    class SplitTransport:
        def __init__(self, _settings: Settings):
            pass

        def window(self, _scope: Scope, lower: datetime, upper: datetime):
            rows = [r for r in occurrences if lower <= datetime.fromisoformat(r.timestamp) < upper]
            if len(rows) > 1:
                raise WindowFullError("synthetic bounded split")
            return rows, {r.message: "template" for r in rows}

        def close(self):
            pass

        def deliver(self, _payload: str):
            pass

    monkeypatch.setattr(main, "Transport", SplitTransport)
    monkeypatch.setattr(stop, "wait", lambda _: stop.set())
    main.run(settings, stop)
    state = State(settings, [SCOPE])
    try:
        assert state.pending_count() == 0
        assert state.db.execute("SELECT count(*) FROM records WHERE delivered=1").fetchone()[0] == 5
        assert state.cursor(SCOPE, START) == START + timedelta(minutes=5)
    finally:
        state.close()


def test_query_values_and_scope_overlaps_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="unsafe PPL literal"):
        literal("chat' or true | head 1")
    scopes = [SCOPE.binding(), {**SCOPE.binding(), "id": "other"}]
    settings.scopes_file.write_text(json.dumps(scopes))
    with pytest.raises(ValueError, match="overlapping"):
        load_scopes(settings.scopes_file)
    scopes[0]["namespaces"] = ["chat` | head 1"]
    settings.scopes_file.write_text(json.dumps(scopes[:1]))
    with pytest.raises(ValueError, match="invalid scope identifier"):
        load_scopes(settings.scopes_file)
    query = brain_query(SCOPE, START, START + timedelta(minutes=1))
    assert "method=brain mode=label" in query
    assert "`@timestamp` < timestamp(" in query
    with pytest.raises(ValueError):
        destination("http://public.example.test/1")
    connection = replace(SCOPE, id="connections", failure_types=("connection_error",))
    settings.scopes_file.write_text(json.dumps([SCOPE.binding(), connection.binding()]))
    partitioned = load_scopes(settings.scopes_file)
    policy = Policy.parse(
        json.dumps(
            {
                "version": 1,
                "defaults": [
                    {"id": "connections", "match": {"failure_type": ["connection_error"]}}
                ],
                "products": [
                    {
                        "id": "inactive-product",
                        "match": {
                            "application": ["unselected"],
                            "failure_type": ["unselected_failure"],
                        },
                    }
                ],
            }
        )
    )
    policy.validate_scopes("test", partitioned)
    unrelated = replace(SCOPE, id="other", service="worker", namespaces=("other",))
    with pytest.raises(ValueError, match="outside its source scopes"):
        policy.validate_scopes("test", (*partitioned, unrelated))
    with pytest.raises(ValueError, match="outside its source scopes"):
        policy.validate_scopes("test", (SCOPE,))


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_ingestion_override_preserves_native_identity(
    settings: Settings, scheme: str, suffix: str
) -> None:
    dsn = settings.glitchtip_dsn.get_secret_value()
    canonical = "https://errors.example.test"
    assert destination(dsn) == (f"{canonical}/api/1/store/", "project-public-key", f"{canonical}/1")
    default = Transport(settings)
    try:
        assert default.store_url == f"{canonical}/api/1/store/"
    finally:
        default.close()
    state = State(settings, [SCOPE])
    try:
        state.enqueue(SCOPE, [record("one")], {"request 1 failed": "request <*> failed"}, START)
        pending = state.due(0)
        identity = state.key
    finally:
        state.close()
    base = f"{scheme}://glitchtip.monitor-errors.svc.cluster.local:8000"
    settings.glitchtip_ingest_base_url = base + suffix
    settings.validate_runtime()
    assert destination(dsn, base + suffix) == (
        f"{base}/api/1/store/",
        "project-public-key",
        f"{canonical}/1",
    )
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    try:
        assert state.key == identity
        assert state.due(0) == pending
        assert (
            state.enqueue(SCOPE, [record("one")], {"request 1 failed": "request <*> failed"}, START)
            == 0
        )
        assert settings.glitchtip_dsn.get_secret_value() == dsn
        assert transport.store_url == f"{base}/api/1/store/"
        assert transport.sink.verify is True
        assert transport.sink.headers["X-Sentry-Auth"].endswith("sentry_key=project-public-key")
    finally:
        transport.close()
        state.close()


@pytest.mark.parametrize(
    "base",
    [
        "http://key@glitchtip.monitor-errors.svc.cluster.local:8000",
        "http://key:password@glitchtip.monitor-errors.svc.cluster.local:8000",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000?query=1",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000#fragment",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000/api/2/store/",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000/../",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000/%2f",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000//",
        "http://glitchtip.monitor-errors.svc.cluster.local:80",
        "http://glitchtip.monitor-errors.svc.cluster.local",
        "http://glitchtip.monitor-errors.svc.cluster.local.:8000",
        "http://glitchtip.monitor-errors.svc.cluster.local.attacker.test:8000",
        "https://errors.example.test:8000",
        "http://127.0.0.1:8000",
        "http://glitch_tip.monitor-errors.svc.cluster.local:8000",
        "http://glitchtip.monitor_errors.svc.cluster.local:8000",
        "http://-glitchtip.monitor-errors.svc.cluster.local:8000",
        "http://glitchtip.monitor-errors-.svc.cluster.local:8000",
        "http://glitchtip.extra.monitor-errors.svc.cluster.local:8000",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000\n",
        "http://glitchtip.monitor-errors.svc.cluster.local:8000\\@attacker.test",
        "http://" + "a" * 64 + ".monitor-errors.svc.cluster.local:8000",
        "http://glitchtip." + "a" * 64 + ".svc.cluster.local:8000",
        "",
    ],
)
def test_unsafe_ingestion_override_rejected(settings: Settings, base: str) -> None:
    settings.glitchtip_ingest_base_url = base
    with pytest.raises(ValueError, match="Service origin on port 8000"):
        settings.validate_runtime()
    with pytest.raises(ValueError, match="Service origin on port 8000"):
        Transport(settings)


@pytest.mark.parametrize("status", [200, 302, 307])
def test_internal_ingestion_http_and_redirect_retains_pending(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    host = "glitchtip.monitor-errors.svc.cluster.local"
    settings.glitchtip_ingest_base_url = f"http://{host}:8000"
    settings.validate_runtime()
    seen: list[tuple[str, dict[str, str], str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, dict(self.headers), ""))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen.append((self.path, dict(self.headers), body))
            # A followed redirect would succeed, making the request-count assertion fail.
            self.send_response(status if self.path == "/api/1/store/" else 200)
            self.send_header("Location", f"http://{host}:8000/redirected")
            response = json.dumps({"event_id": json.loads(body)["event_id"]}).encode()
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    resolve = socket.getaddrinfo

    def local_service(hostname: str, port: int, *args: Any, **kwargs: Any):
        assert (hostname, port) == (host, 8000)
        return resolve("127.0.0.1", server.server_port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_service)
    monkeypatch.setenv("HTTP_PROXY", "http://unreachable.example.test:9999")
    monkeypatch.setenv("ALL_PROXY", "http://unreachable.example.test:9999")
    monkeypatch.setenv("NO_PROXY", "")
    netrc = settings.state_path.parent / "synthetic.netrc"
    netrc.write_text(f"machine {host} login unexpected password unexpected\n")
    monkeypatch.setenv("NETRC", str(netrc))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    try:
        state.enqueue(SCOPE, [record("one")], {"request 1 failed": "request <*> failed"}, START)
        payload = state.due(0)[0][1]
        assert transport.sink.verify is True
        assert not transport.sink.trust_env
        main.deliver_due(state, transport, 0)
        assert len(seen) == 1
        path, headers, body = seen[0]
        assert path == "/api/1/store/"
        assert headers["Host"] == f"{host}:8000"
        assert headers["X-Sentry-Auth"] == (
            "Sentry sentry_version=7, sentry_client=opensearch-glitchtip/1, "
            "sentry_key=project-public-key"
        )
        assert "Authorization" not in headers
        assert "Proxy-Authorization" not in headers
        assert body == payload
        assert state.pending_count() == (0 if status == 200 else 1)
        if status != 200:
            assert state.due(3600)[0][1] == payload
    finally:
        transport.close()
        state.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_tls_and_request_errors_never_expose_secrets_or_payload(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport(settings)
    assert transport.source.verify == str(settings.opensearch_ca_file)
    assert transport.sink.verify is True
    assert not transport.source.trust_env
    assert not transport.sink.trust_env

    def fail(*_args: Any, **_kwargs: Any):
        raise requests.exceptions.SSLError("secret-reader-password private log payload")

    monkeypatch.setattr(transport.source, "post", fail)
    try:
        with pytest.raises(AdapterError) as error:
            transport.window(SCOPE, START, START + timedelta(minutes=1))
        assert "secret" not in str(error.value)
        assert "payload" not in str(error.value)
    finally:
        transport.close()


def test_ingestion_acknowledgement_requires_matching_event_id(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport(settings)
    monkeypatch.setattr(transport, "request", lambda *_: {"event_id": "different"})
    try:
        with pytest.raises(AdapterError, match="acceptance uncertain"):
            transport.deliver('{"event_id":"expected"}')
    finally:
        transport.close()


@pytest.mark.parametrize(
    "acknowledgement",
    [
        "native",
        "missing",
        "null",
        "number",
        "object",
        "malformed",
        "foreign",
        "hyphenated",
        "id_only",
    ],
)
def test_store_native_acknowledgement_and_immutable_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, acknowledgement: str
) -> None:
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    sent: list[str] = []
    try:
        state.enqueue(SCOPE, [record("one")], {"request 1 failed": "request <*> failed"}, START)
        source, payload, _ = state.due(0)[0]
        event_id = json.loads(payload)["event_id"]
        responses = {
            "native": {"event_id": event_id, "task_id": None},
            "missing": {},
            "null": {"event_id": None},
            "number": {"event_id": 123},
            "object": {"event_id": {"id": event_id}},
            "malformed": {"event_id": "not-a-uuid"},
            "foreign": {"event_id": ("0" if event_id[0] != "0" else "1") + event_id[1:]},
            "hyphenated": {
                "event_id": "-".join(
                    [event_id[:8], event_id[8:12], event_id[12:16], event_id[16:20], event_id[20:]]
                )
            },
            "id_only": {"id": event_id},
        }

        def request(_session: requests.Session, _url: str, body: str) -> dict[str, Any]:
            sent.append(body)
            return responses[acknowledgement] if len(sent) == 1 else {"event_id": event_id}

        monkeypatch.setattr(transport, "request", request)
        main.deliver_due(state, transport, 0)
        if acknowledgement == "native":
            assert state.pending_count() == 0
            assert sent == [payload]
        else:
            assert state.pending_count() == 1
            assert state.due(3600) == [(source, payload, 1)]
            main.deliver_due(state, transport, 3600)
            assert state.pending_count() == 0
            assert sent == [payload, payload]
    finally:
        transport.close()
        state.close()


def test_native_contract_and_raw_recheck(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [record("one"), record("two")]
    responses = iter([search(records), ppl([r.message for r in records]), search(records)])
    requests_seen: list[tuple[str, dict[str, Any]]] = []
    transport = Transport(settings)

    def request(_session: requests.Session, url: str, body: dict[str, Any]):
        requests_seen.append((url, body))
        return next(responses)

    monkeypatch.setattr(transport, "request", request)
    try:
        actual, labels = transport.brain_window(SCOPE, START, START + timedelta(minutes=1))
        assert actual == records
        assert labels == {records[0].message: "request <*> failed"}
        assert len(requests_seen) == 3
        raw_url, raw_body = requests_seen[0]
        assert raw_url.endswith("allow_partial_search_results=false")
        assert raw_body["size"] == 1001 and raw_body["track_total_hits"] is True
        assert requests_seen[2] == requests_seen[0]
        ppl_url, ppl_body = requests_seen[1]
        assert ppl_url.endswith("/_plugins/_ppl")
        assert ppl_body["fetch_size"] == 1001
        assert "| head 1001 | patterns log method=brain mode=label " in ppl_body["query"]
        assert "buffer_limit=50000 show_numbered_token=false" in ppl_body["query"]
    finally:
        transport.close()


@pytest.mark.parametrize("changed_ids", [True, False])
def test_population_races_retry_bounded_without_commit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, changed_ids: bool
) -> None:
    records = [record("one")]
    calls = 0
    transport = Transport(settings)
    state = State(settings, [SCOPE])

    def request(_session: requests.Session, url: str, _body: dict[str, Any]):
        nonlocal calls
        calls += 1
        if url.endswith("/_plugins/_ppl"):
            return ppl([records[0].message if changed_ids else "racing new message"])
        if changed_ids and calls % 3 == 0:
            return search([replace(records[0], id="replacement")])
        return search(records)

    monkeypatch.setattr(transport, "request", request)
    try:
        with pytest.raises(AdapterError, match="unstable after three attempts"):
            rows, labels = transport.brain_window(SCOPE, START, START + timedelta(minutes=1))
            state.enqueue(SCOPE, rows, labels, START + timedelta(minutes=1))
        assert calls == (9 if changed_ids else 6)
        assert state.pending_count() == 0
        assert state.cursor(SCOPE, START) == START
    finally:
        state.close()
        transport.close()


def test_population_race_can_settle_on_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial, late = record("one"), record("late", "request 2 failed")
    responses = iter(
        [
            search([initial]),
            ppl([initial.message, late.message]),
            search([initial, late]),
            ppl([initial.message, late.message]),
            search([initial, late]),
        ]
    )
    transport = Transport(settings)
    monkeypatch.setattr(transport, "request", lambda *_: next(responses))
    try:
        records, _ = transport.brain_window(SCOPE, START, START + timedelta(minutes=1))
        assert records == [initial, late]
    finally:
        transport.close()


def test_http_error_body_redirects_and_credentials_are_not_exposed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport(settings)
    kwargs_seen: dict[str, Any] = {}

    class Response:
        status_code = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_content(self, **_kwargs):
            raise AssertionError("error body must never be read")

    def post(_url: str, **kwargs: Any):
        kwargs_seen.update(kwargs)
        return Response()

    monkeypatch.setattr(transport.source, "post", post)
    try:
        with pytest.raises(AdapterError, match="^upstream HTTP status 403$"):
            transport.window(SCOPE, START, START + timedelta(minutes=1))
        assert kwargs_seen["allow_redirects"] is False
        assert kwargs_seen["stream"] is True
        assert kwargs_seen["timeout"] == (5, 60)
    finally:
        transport.close()


@pytest.mark.parametrize("unsplittable", [False, True])
def test_query_byte_cap_splits_complete_windows_or_stalls_without_advancing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unsplittable: bool,
) -> None:
    # Scale only the wire-byte budget, not the occurrence cap, to exercise the real
    # streaming response reader and query-specific oversized-response conversion.
    monkeypatch.setattr(transport_module, "MAX_RESPONSE_BYTES", 4096)
    occurrences = [
        replace(
            record(str(i)), timestamp=stamp(START + timedelta(seconds=0 if unsplittable else i))
        )
        for i in (0, 60, 120, 180, 240)
    ]
    transport = Transport(settings)
    sent: list[str] = []
    stop = threading.Event()
    capped = 0

    class Response:
        status_code = 200

        def __init__(self, body: dict[str, Any]):
            self.data = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_content(self, **_kwargs):
            for i in range(0, len(self.data), 512):
                yield self.data[i : i + 512]

    def post(url: str, **kwargs: Any):
        nonlocal capped
        body = json.loads(kwargs["data"])
        assert not url.endswith("/_plugins/_ppl")
        bounds = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
        lower, upper = (
            datetime.fromisoformat(bounds["gte"]),
            datetime.fromisoformat(bounds["lt"]),
        )
        rows = [r for r in occurrences if lower <= datetime.fromisoformat(r.timestamp) < upper]
        response = search(rows)
        response["ignored_padding"] = "x" * (1024 * len(rows))
        result = Response(response)
        capped += len(result.data) > 4096
        return result

    monkeypatch.setattr(transport.source, "post", post)
    monkeypatch.setattr(transport, "deliver", sent.append)
    monkeypatch.setattr(main, "Transport", lambda _: transport)
    monkeypatch.setattr(stop, "wait", lambda _: stop.set())
    main.run(settings, stop)
    state = State(settings, [SCOPE])
    try:
        assert capped > 0
        if unsplittable:
            assert capped < 30
            assert state.cursor(SCOPE, START) == START
            assert state.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
            assert not sent
            output = capsys.readouterr().out
            assert "same_timestamp_or_minimum_window_cap" in output
            assert "query response exceeds byte limit" in output
        else:
            assert len(sent) == len(occurrences)
            assert len({json.loads(payload)["event_id"] for payload in sent}) == len(occurrences)
            assert state.cursor(SCOPE, START) == START + timedelta(minutes=5)
            assert state.pending_count() == 0
    finally:
        state.close()


def test_oversized_ingestion_response_remains_immutable_pending_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    state.enqueue(SCOPE, [record("one")], {"request 1 failed": "template"}, START)
    original = state.due(0)[0][1]

    def oversized(*_args):
        raise ResponseTooLargeError("upstream response exceeds byte limit")

    monkeypatch.setattr(transport, "request", oversized)
    try:
        with pytest.raises(ResponseTooLargeError):
            transport.deliver(original)
        main.deliver_due(state, transport, 0)
        assert state.pending_count() == 1
        assert state.due(2)[0][1] == original
        assert state.cursor(SCOPE, START) == START
    finally:
        transport.close()
        state.close()


def test_persistently_failing_older_records_cannot_starve_new_healthy_records(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    sent: list[str] = []
    state.enqueue(
        SCOPE, [record(f"bad-{i}", "failing") for i in range(500)], {"failing": "failing"}, START
    )
    for source, _, _ in state.due(0, 500):
        state.defer(source, 9, 0)
    # A short cycle budget recreates the starvation that rowid-only ordering caused.
    monkeypatch.setattr(main, "DELIVERY_CYCLE_ATTEMPTS", 100)

    def deliver(payload: str):
        if json.loads(payload)["message"] == "failing":
            raise AdapterError("synthetic persistent failure")
        sent.append(payload)

    monkeypatch.setattr(transport, "deliver", deliver)
    try:
        for cycle in range(20):
            state.enqueue(
                SCOPE, [record(f"healthy-{cycle}", "healthy")], {"healthy": "healthy"}, START
            )
            main.deliver_due(state, transport, 1000 + cycle * 300)
            assert len(sent) == cycle + 1
        assert state.pending_count() == 500
    finally:
        transport.close()
        state.close()


def test_healthy_backlog_drains_multiple_batches_before_poll_sleep(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Future activation avoids source calls while checking the worker's delivery/sleep order.
    settings.initial_start_at = stamp(datetime.now(UTC) + timedelta(days=1))
    initial = datetime.fromisoformat(settings.initial_start_at)
    state = State(settings, [SCOPE])
    state.enqueue(
        SCOPE, [record(str(i)) for i in range(350)], {"request 1 failed": "template"}, initial
    )
    state.close()
    transport = Transport(settings)
    sent: list[str] = []
    stop = threading.Event()
    monkeypatch.setattr(transport, "deliver", sent.append)
    monkeypatch.setattr(main, "Transport", lambda _: transport)

    def sleep(_seconds: float):
        assert len(sent) == 350
        stop.set()

    monkeypatch.setattr(stop, "wait", sleep)
    main.run(settings, stop)
    state = State(settings, [SCOPE])
    try:
        assert state.pending_count() == 0
    finally:
        state.close()


@pytest.mark.parametrize("bound", ["stop", "time", "attempts"])
def test_delivery_work_budget_and_stop_checks_leave_remaining_payloads_durable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    state = State(settings, [SCOPE])
    transport = Transport(settings)
    state.enqueue(
        SCOPE, [record(str(i)) for i in range(4)], {"request 1 failed": "template"}, START
    )
    stop = threading.Event()
    clock = 0
    sent: list[str] = []

    def deliver(payload: str):
        nonlocal clock
        sent.append(payload)
        if bound == "stop":
            stop.set()
        elif bound == "time":
            clock = main.DELIVERY_CYCLE_SECONDS + 1

    monkeypatch.setattr(main.time, "monotonic", lambda: clock)
    monkeypatch.setattr(transport, "deliver", deliver)
    if bound == "attempts":
        monkeypatch.setattr(main, "DELIVERY_CYCLE_ATTEMPTS", 1)
    try:
        main.deliver_due(state, transport, 0, stop)
        assert len(sent) == 1
        assert state.pending_count() == 3
        assert len(state.due(0)) == 3
    finally:
        transport.close()
        state.close()


def reporting_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "defaults": [
            {
                "id": "dependency-failure",
                "match": {
                    "failure_type": ["application_error"],
                    "json.event": ["dependency_failed"],
                },
                "title": "{service}: {json.target} authentication rejected ({json.code})",
                "explanation": "The dependency rejected authentication.",
                "drop_fields": ["timestamp", "request_id", "operation_id"],
            },
            {
                "id": "successful-debug",
                "match": {
                    "json.event": ["operation_succeeded"],
                    "level": ["debug"],
                    "json.error": [None],
                    "json.outcome.status": ["success"],
                },
                "action": "log_only",
            },
            {
                "id": "collector-failure",
                "match": {"json.event": ["collector_failed"]},
                "group_by": ["level"],
            },
        ],
    }


def diagnostic(**changes: Any) -> str:
    return json.dumps(
        {
            "level": "ERROR",
            "event": "dependency_failed",
            "message": "Dependency rejected authentication",
            "operation": "initialize",
            "target": "server-a",
            "stage": "authenticate",
            "code": 403,
            "cause": "permission denied",
            "error": None,
            "cache_enabled": True,
            "timestamp": stamp(START),
            "request_id": "request-a",
            "operation_id": "operation-a",
            **changes,
        }
    )


def test_reporting_synthetic_groups_preserve_diagnostics_and_conservative_fallback(
    settings: Settings,
) -> None:
    settings.reporting_policy_file = settings.state_path.parent / "policy.json"
    settings.reporting_policy_file.write_text(json.dumps(reporting_policy()))
    state = State(settings, [SCOPE])
    records = [
        record(
            str(i),
            diagnostic(
                request_id=f"request-{i}",
                operation_id=f"operation-{i}",
                timestamp=stamp(START + timedelta(seconds=i)),
            ),
        )
        for i in range(100)
    ]
    variations = {
        "operation": "refresh",
        "target": "server-b",
        "stage": "connect",
        "code": 401,
        "cause": "certificate expired",
        "message": "Distinct failure detail",
        "cache_enabled": False,
    }
    records += [record(field, diagnostic(**{field: value})) for field, value in variations.items()]
    records += [
        record(f"missing-{kind}-{i}", diagnostic(cause=cause, request_id=f"missing-{kind}-{i}"))
        for kind, cause in enumerate((None, {}, []))
        for i in range(2)
    ]
    records += [record("unknown", '{"message":"Unknown error","request_id":"unknown"}')]
    collision = {"outcome": {"status": "failed"}, "outcome.status": "success"}
    records += [
        record(
            f"ambiguous-{i}",
            diagnostic(
                event="operation_succeeded",
                level="DEBUG",
                **fields,
            ),
        )
        for i, fields in enumerate((collision, dict(reversed(list(collision.items())))))
    ]
    collector_message = '{"event":"collector_failed","message":"Upstream failed"}'
    records += [
        replace(record(f"collector-{level}", collector_message), original_level=level)
        for level in ("ERROR", "WARNING")
    ]
    records += [
        record(
            "success",
            diagnostic(
                event="operation_succeeded",
                level="DEBUG",
                outcome={"status": "success"},
            ),
        )
    ]
    try:
        assert state.enqueue(SCOPE, records, {}, START + timedelta(minutes=5)) == len(records) - 1
        events = [json.loads(payload) for _, payload, _ in state.due(0, 1000)]
        first = events[0]
        assert len({tuple(event["fingerprint"]) for event in events[:100]}) == 1
        # All diagnostic, incomplete, ambiguous and collector-level variants stay separate.
        assert len({tuple(event["fingerprint"]) for event in events}) == 1 + len(events) - 100
        assert first["message"] == "api: server-a authentication rejected (403)"
        assert first["extra"]["diagnostics"]["cache_enabled"] is True
        assert first["extra"]["diagnostics"]["error"] is None
        assert all(event["extra"]["reporting"]["count_unit"] == "error_events" for event in events)
        decision = state.policy.evaluate("test", SCOPE, records[0])
        base = identity_parts("test", SCOPE, records[0], decision)
        assert base != identity_parts("other", SCOPE, records[0], decision)
        assert base != identity_parts(
            "test", replace(SCOPE, service="worker"), records[0], decision
        )
        untuned_document = reporting_policy()
        del untuned_document["defaults"][0]["drop_fields"]
        untuned = Policy.parse(json.dumps(untuned_document))
        assert (
            untuned.evaluate(
                "test", SCOPE, record("duration-a", diagnostic(time=30))
            ).identity_parts
            != untuned.evaluate(
                "test", SCOPE, record("duration-b", diagnostic(time=60))
            ).identity_parts
        )
        rows = search(records)["hits"]["hits"]
        rows.append(
            search([record("outside", scope=replace(SCOPE, namespaces=("other",)))])["hits"][
                "hits"
            ][0]
        )
        result = preview(
            rows, (SCOPE,), "test", state.policy, Policy(), START, START + timedelta(minutes=5)
        )
        assert result["groups"][0]["occurrences"] == 100
        assert "json.request_id" in result["groups"][0]["differing_fields"]
        assert {item["reason"] for item in result["exclusions"]} == {
            "log_only",
            "outside_scope_or_window",
        }
        assert result["changes"][0]["identity_changed"] is True
    finally:
        state.close()


def test_reporting_overrides_last_valid_restart_and_metadata_preserve_pending(
    settings: Settings,
) -> None:
    settings.reporting_policy_file = settings.state_path.parent / "policy.json"
    settings.studio_url = "https://studio.example.test"
    document = reporting_policy()
    document["products"] = [{"id": "dependency-failure", "title": "Product title"}]
    override: dict[str, Any] = {
        "id": "dependency-failure",
        "title": "Client title",
        "level": "warning",
    }
    document["overrides"] = [override]
    settings.reporting_policy_file.write_text(json.dumps(document))
    state = State(settings, [SCOPE])
    first = record("original", diagnostic())
    state.enqueue(SCOPE, [first], {}, START)
    pending = state.due(0)[0]
    key = state.key
    event = json.loads(pending[1])
    assert event["message"] == "Client title"
    assert event["level"] == "warning"
    assert event["extra"]["original_level"] == "ERROR"
    reporting = event["extra"]["reporting"]
    assert reporting["original_timestamp"] == event["timestamp"] == stamp(START)
    matching = parse_qs(urlsplit(reporting["matching_logs_url"]).query)
    surrounding = parse_qs(urlsplit(reporting["surrounding_logs_url"]).query)
    assert matching["namespace"] == surrounding["namespace"] == ["chat"]
    assert matching["start"] == surrounding["start"] == [stamp(START - timedelta(minutes=10))]
    assert matching["end"] == surrounding["end"] == [stamp(START + timedelta(minutes=5))]
    assert '_id:"original"' in matching["q"][0]
    assert surrounding["q"] == ['kubernetes.container_name.keyword:"api"']
    document["overrides"][0].update(
        title="Reworded\n\x1b[31mnotice\x1b[0m",
        explanation="Updated wording",
        level="error",
        group_by=["json.cause", "json.code", "json.stage", "json.target", "json.operation"],
    )
    settings.reporting_policy_file.write_text(json.dumps(document))
    assert state.reload_policy()
    second = record("second", diagnostic(request_id="second"))
    state.enqueue(SCOPE, [second], {}, START)
    assert state.due(0)[0] == pending
    updated = payloads(state)[1]
    assert updated["message"] == "Reworded notice"
    assert updated["fingerprint"] == event["fingerprint"]
    # Reject a dimension that normalization would remove, keeping immutable pending events.
    override["group_by"] = ["json.request_id"]
    settings.reporting_policy_file.write_text(json.dumps(document))
    assert not state.reload_policy()
    assert state.due(0)[0] == pending
    override["group_by"] = ["level"]
    settings.reporting_policy_file.write_text(json.dumps(document))
    assert state.reload_policy()
    transitioned = record("grouping-transition", diagnostic(request_id="transition"))
    state.enqueue(SCOPE, [transitioned], {}, START)
    assert payloads(state)[2]["fingerprint"] != event["fingerprint"]
    override.pop("group_by")
    settings.reporting_policy_file.write_text(json.dumps(document))
    assert state.reload_policy()
    # Reject an attempted blanket diagnostic removal and retain the last valid policy on disk.
    override["drop_fields"] = ["cause", "cache_enabled"]
    settings.reporting_policy_file.write_text(json.dumps(document))
    assert not state.reload_policy()
    state.close()
    state = State(settings, [SCOPE])
    try:
        assert state.key == key and not state.policy_valid
        assert state.due(0)[0] == pending
        assert state.enqueue(SCOPE, [first], {}, START) == 0
        third = record("late", diagnostic(request_id="late"))
        state.enqueue(SCOPE, [third], {}, START + timedelta(days=1))
        late = payloads(state)[3]
        assert late["message"] == "Reworded notice"
        assert late["fingerprint"] == event["fingerprint"]
        assert late["timestamp"] == late["extra"]["reporting"]["original_timestamp"] == stamp(START)
        health = Health((SCOPE.id,))
        health.update(state)
        assert b"reporting_adapter_policy_valid 0" in health.render()
        assert b"reporting_adapter_pending_events 4" in health.render()
    finally:
        state.close()


def gateway_message(**changes: str | None) -> str:
    fields = {
        "gateway": "gateway/gateway",
        "listener": "api-bindings",
        "route": "gateway/mcp-tools-route",
        "src.addr": "192.0.2.1:12345",
        "http.method": "POST",
        "http.host": "tools.example.test:443",
        "http.path": "/mcp/tools",
        "http.version": "HTTP/1.1",
        "http.status": "403",
        "trace.id": "a" * 32,
        "span.id": "b" * 16,
        "jwt.sub": "caller-a",
        "protocol": "http",
        "error": "authorization failed",
        "reason": "Authorization",
        "duration": "1ms",
        "neurwerk.request_id": "00000000-0000-0000-0000-000000000001",
        **changes,
    }
    timestamp = fields.pop("timestamp", "2026-01-01T00:00:00Z")
    return f"{timestamp}\terror\trequest " + " ".join(
        f"{k}={json.dumps(v) if ' ' in v else v}" for k, v in fields.items() if v is not None
    )


def gateway_noise(**changes: str | None) -> str:
    return gateway_message(
        **{
            "http.method": "GET",
            "http.status": "405",
            "protocol": "mcp",
            "reason": "MCP",
            "error": "mcp: method not allowed; must be GET, POST, or DELETE",
            "neurwerk.billing_classification": "unknown",
            "neurwerk.cost_known": "false",
            "neurwerk.credential_mode": "none",
            "neurwerk.destination_kind": "mcp",
            "neurwerk.dispatch_attempted": "true",
            "neurwerk.entry_mode": "api",
            "neurwerk.ignored_proxy_credential_headers": "[]",
            "neurwerk.operation_id": "exchange",
            "neurwerk.protocol_profile": "mcp-streamable-http-v1",
            "neurwerk.provider_credential_source": "none",
            "neurwerk.request_outcome": "gateway_error",
            "neurwerk.request_text_inspection": "unknown",
            "neurwerk.stripped_credential_headers": "[]",
            "neurwerk.target_credential_mode": "unknown",
            "neurwerk.token_usage_known": "false",
            **changes,
        }
    )


def gateway_policy() -> Policy:
    return Policy.parse(
        json.dumps(
            {
                "version": 1,
                "defaults": [
                    {
                        "id": "noise",
                        "priority": 100,
                        "source_format": "agentgateway-mcp-v1",
                        "match": {"json.http.status": ["405"]},
                        "group_by": ["json.http.path"],
                        "action": "log_only",
                    },
                    {
                        "id": "auth",
                        "source_format": "agentgateway-mcp-v1",
                        "match": {"json.http.status": ["403"]},
                        "group_by": [
                            "json.jwt.sub",
                            "json.http.path",
                            "json.route",
                            "json.http.host",
                        ],
                    },
                ],
            }
        )
    )


def test_reviewed_gateway_grouping_boundaries() -> None:
    policy = gateway_policy()

    def signature(message: str) -> tuple[str, ...]:
        item = record("sample", message)
        return identity_parts("test", SCOPE, item, policy.evaluate("test", SCOPE, item))

    original = signature(gateway_message())
    assert signature(gateway_message(**{"src.addr": "[2001:db8::1]:12345"})) == signature(
        gateway_message(**{"src.addr": "[2001:db8::1]:54321"})
    )
    assert (
        signature(
            gateway_message(
                **{
                    "timestamp": "2026-01-02T00:00:00Z",
                    "trace.id": "c" * 32,
                    "span.id": "d" * 16,
                    "duration": "123ms",
                    "src.addr": "192.0.2.1:54321",
                    "neurwerk.request_id": "00000000-0000-0000-0000-000000000002",
                }
            )
        )
        == original
    )
    for field, value in {
        "jwt.sub": "caller-b",
        "http.path": "/mcp/other",
        "route": "gateway/other-route",
        "http.host": "tools.example.test:8443",
        "http.status": "401",
        "reason": "Other",
        "error": "other authorization error",
        "src.addr": "192.0.2.2:12345",
        "future.diagnostic": "new cause",
        "http.method": "DELETE",
        "neurwerk.operation_id": "tools/call",
    }.items():
        assert signature(gateway_message(**{field: value})) != original, field
    assert signature(gateway_message(**{"future.diagnostic": "a"})) != signature(
        gateway_message(**{"future.diagnostic": "b"})
    )


@pytest.mark.parametrize(
    "message",
    [
        gateway_noise() + " reason=MCP",
        gateway_noise() + " trailing-garbage",
        gateway_noise() + ' extra="unfinished',
        gateway_noise() + " http=collision",
        gateway_noise(**{"timestamp": "2026-99-99T00:00:00Z"}),
        gateway_noise(**{"trace.id": "invalid"}),
        gateway_noise(**{"span.id": "invalid"}),
        gateway_noise(**{"src.addr": "192.0.2.1:99999"}),
        gateway_noise(**{"src.addr": "[192.0.2.1]:12345"}),
        gateway_noise(**{"src.addr": "[2001:db8::1:12345"}),
        gateway_noise(**{"duration": "soon"}),
        gateway_noise(**{"http.host": None}),
        gateway_noise() + "\x1b[31m",
        gateway_noise(**{"neurwerk.request_id": "invalid"}),
    ],
)
def test_reviewed_gateway_invalid_input_never_excludes(message: str) -> None:
    item = record("sample", message)
    decision = gateway_policy().evaluate("test", SCOPE, item)
    assert decision.action == "report" and decision.identity_parts is None
    assert identity_parts("test", SCOPE, item, decision) == identity_parts(
        "test", SCOPE, item, Policy().evaluate("test", SCOPE, item)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"http.method": "POST"},
        {"http.method": "DELETE"},
        {"http.status": "500"},
        {"reason": "Authorization"},
        {"protocol": "http"},
        {"error": "upstream unavailable"},
        {"listener": "other"},
        {"route": "gateway/mcp-other-route"},
        {"future.error": "upstream unavailable"},
        {"neurwerk.dispatch_attempted": "false"},
        {"neurwerk.protocol_profile": None},
        {"neurwerk.token_usage_known": None},
    ],
)
def test_reviewed_gateway_get_near_misses_report(changes: dict[str, str | None]) -> None:
    assert (
        gateway_policy().evaluate("test", SCOPE, record("sample", gateway_noise(**changes))).action
        == "report"
    )


def test_reviewed_gateway_opt_in_persistence_and_preview(settings: Settings) -> None:
    policy = gateway_policy()
    first = record("first", gateway_message())
    second = record("second", gateway_message(duration="5ms"))
    noise = record("noise", gateway_noise())
    assert policy.evaluate("test", SCOPE, noise).action == "log_only"
    disabled = Policy.parse(policy.serialize().replace('"enabled":true', '"enabled":false'))
    for candidate in (Policy(), disabled):
        assert candidate.evaluate("test", SCOPE, first).identity_parts is None
        assert candidate.evaluate("test", SCOPE, noise).action == "report"
    assert (
        policy.evaluate("test", SCOPE, record("json", '{"http.status":"405"}')).action == "report"
    )
    settings.reporting_policy_file = settings.state_path.parent / "policy.json"
    settings.reporting_policy_file.write_text(policy.serialize())
    state = State(settings, [SCOPE])
    try:
        assert state.enqueue(SCOPE, [first, second, noise], {}, START) == 2
        pending = state.due(0)
        events = [json.loads(row[1]) for row in pending]
        assert events[0]["fingerprint"] == events[1]["fingerprint"]
        assert events[0]["event_id"] != events[1]["event_id"]
        assert events[0]["extra"]["diagnostics"]["duration"] == "1ms"
        settings.reporting_policy_file.write_text(
            policy.serialize().replace("agentgateway-mcp-v1", "unknown-format")
        )
        assert not state.reload_policy()
    finally:
        state.close()
    state = State(settings, [SCOPE])
    try:
        assert not state.policy_valid and state.due(0) == pending
        settings.reporting_policy_file.write_text(Policy().serialize())
        assert state.reload_policy()
        assert state.enqueue(SCOPE, [noise], {}, START) == 0
        assert state.due(0) == pending
    finally:
        state.close()
    result = preview(
        search([first, second, noise])["hits"]["hits"],
        (SCOPE,),
        "test",
        policy,
        Policy(),
        START,
        START + timedelta(minutes=5),
    )
    assert result["groups"][0]["occurrences"] == 2
    assert "json.duration" in result["groups"][0]["differing_fields"]
    assert result["exclusions"][0]["reason"] == "log_only"


def test_brain_unavailable_still_polls_commits_and_delivers(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = Transport(settings)
    stop = threading.Event()
    health = Health((SCOPE.id,))
    sent: list[dict[str, Any]] = []

    def request(_session: requests.Session, url: str, body: Any) -> dict[str, Any]:
        if url.endswith("/_plugins/_ppl"):
            raise AdapterError("BRAIN unavailable")
        if url.endswith("/store/"):
            event = json.loads(body)
            sent.append(event)
            return {"event_id": event["event_id"]}
        return search([record("one")])

    monkeypatch.setattr(transport, "request", request)
    monkeypatch.setattr(main, "Transport", lambda _: transport)
    monkeypatch.setattr(stop, "wait", lambda _: stop.set())
    # Advisory failure is real, but cannot enter the worker's critical path.
    with pytest.raises(AdapterError, match="BRAIN unavailable"):
        transport.brain_window(SCOPE, START, START + timedelta(minutes=1))
    main.run(settings, stop, health)
    state = State(settings, [SCOPE])
    try:
        assert len(sent) == 1
        assert state.pending_count() == 0
        assert state.cursor(SCOPE, START) == START + timedelta(minutes=5)
        assert state.db.execute("SELECT delivered FROM records").fetchone() == (1,)
        assert health.values[f'last_poll_success_timestamp_seconds{{scope="{SCOPE.id}"}}'] > 0
        assert health.values["last_delivery_success_timestamp_seconds"] > 0
    finally:
        state.close()
