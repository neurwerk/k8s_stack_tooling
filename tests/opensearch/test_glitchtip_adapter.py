"""Synthetic behavioral checks reviewed by Astra; no client log fixtures."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests
from pydantic import SecretStr

from k8s_stack_tooling.opensearch_glitchtip import main
from k8s_stack_tooling.opensearch_glitchtip import transport as transport_module
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
                        "stack_log": {"failure_type": r.failure_type},
                    },
                }
                for r in records
            ],
        },
    }


def test_distinct_occurrences_overlap_and_restart(settings: Settings) -> None:
    state = State(settings, [SCOPE])
    records = [record("one"), record("two")]
    upper = START + timedelta(minutes=5)
    assert state.enqueue(SCOPE, records, {records[0].message: "request <*> failed"}, upper) == 2
    events = payloads(state)
    assert len({event["event_id"] for event in events}) == 2
    assert len({tuple(event["fingerprint"]) for event in events}) == 1
    assert events[0]["timestamp"] == stamp(START)
    assert events[0]["tags"]["application"] == "chat"
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
    state.enqueue(SCOPE, [record("one")], {"request 1 failed": "request <*> failed"}, START)
    main.deliver_due(state, transport, 0)
    assert state.pending_count() == 1
    assert not state.due(1)
    state.close()
    state = State(settings, [SCOPE])
    try:
        # A changed template during overlap must not rewrite the pending payload.
        state.enqueue(SCOPE, [record("one")], {"request 1 failed": "changed"}, START)
        main.deliver_due(state, transport, 10**10)
        assert sent[0] == sent[1]
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
    monkeypatch.setattr(transport, "request", lambda *_: {"id": "different"})
    try:
        with pytest.raises(AdapterError, match="acceptance uncertain"):
            transport.deliver('{"event_id":"expected"}')
    finally:
        transport.close()


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
        actual, labels = transport.window(SCOPE, START, START + timedelta(minutes=1))
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
            rows, labels = transport.window(SCOPE, START, START + timedelta(minutes=1))
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
        records, _ = transport.window(SCOPE, START, START + timedelta(minutes=1))
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


@pytest.mark.parametrize("stage", ["search", "ppl"])
@pytest.mark.parametrize("unsplittable", [False, True])
def test_query_byte_cap_splits_complete_windows_or_stalls_without_advancing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
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
        is_ppl = url.endswith("/_plugins/_ppl")
        if is_ppl:
            lower, upper = [
                datetime.fromisoformat(value)
                for value in re.findall(r"timestamp\('([^']+)'\)", body["query"])
            ]
        else:
            bounds = body["query"]["bool"]["filter"][0]["range"]["@timestamp"]
            lower, upper = (
                datetime.fromisoformat(bounds["gte"]),
                datetime.fromisoformat(bounds["lt"]),
            )
        rows = [r for r in occurrences if lower <= datetime.fromisoformat(r.timestamp) < upper]
        response = ppl([r.message for r in rows]) if is_ppl else search(rows)
        if is_ppl == (stage == "ppl"):
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
