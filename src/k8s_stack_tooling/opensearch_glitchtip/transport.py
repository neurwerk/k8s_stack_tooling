"""Bounded requests and independently checked search / native Brain populations."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

import requests

from .settings import (
    INDEX,
    MAX_MESSAGE_BYTES,
    MAX_RECORDS,
    MAX_RESPONSE_BYTES,
    Scope,
    Settings,
    destination,
    stamp,
    utc,
)
from .state import Record


class AdapterError(Exception):
    """Only fixed, payload-free diagnostic strings may cross this boundary."""


class WindowFullError(AdapterError):
    pass


class ResponseTooLargeError(AdapterError):
    pass


class PopulationChangedError(AdapterError):
    pass


def literal(value: str) -> str:
    # Scope identifiers are validated separately, but timestamps use ':' and '+'.
    if not value or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:+TZ"
        for char in value
    ):
        raise ValueError("unsafe PPL literal")
    return "'" + value + "'"


def selection(scope: Scope, lower: datetime, upper: datetime) -> dict[str, Any]:
    return {
        "bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": stamp(lower), "lt": stamp(upper)}}},
                {"terms": {"kubernetes.namespace_name.keyword": list(scope.namespaces)}},
                {"terms": {"kubernetes.container_name.keyword": list(scope.containers)}},
                {"terms": {"stack_log.failure_type.keyword": list(scope.failure_types)}},
            ]
        }
    }


def brain_query(scope: Scope, lower: datetime, upper: datetime) -> str:
    clauses = [
        f"`@timestamp` >= timestamp({literal(stamp(lower))})",
        f"`@timestamp` < timestamp({literal(stamp(upper))})",
    ]
    for field, values in (
        ("kubernetes.namespace_name", scope.namespaces),
        ("kubernetes.container_name", scope.containers),
        ("stack_log.failure_type", scope.failure_types),
    ):
        clauses.append("(" + " or ".join(f"`{field}` = {literal(v)}" for v in values) + ")")
    return (
        f"source = `{INDEX}` | where "
        + " and ".join(clauses)
        + f" | head {MAX_RECORDS + 1}"
        + " | patterns log method=brain mode=label buffer_limit=50000 show_numbered_token=false"
        + " | fields log, patterns_field"
    )


class Transport:
    def __init__(self, settings: Settings):
        self.source_url = settings.opensearch_url.rstrip("/")
        self.source = requests.Session()
        self.source.trust_env = False
        self.source.auth = (
            settings.opensearch_username,
            settings.opensearch_password.get_secret_value(),
        )
        self.source.verify = str(settings.opensearch_ca_file)
        self.sink = requests.Session()
        self.sink.trust_env = False
        self.sink.verify = str(settings.glitchtip_ca_file) if settings.glitchtip_ca_file else True
        self.store_url, public_key, _ = destination(settings.glitchtip_dsn.get_secret_value())
        self.sink.headers["X-Sentry-Auth"] = (
            "Sentry sentry_version=7, sentry_client=opensearch-glitchtip/1, "
            f"sentry_key={public_key}"
        )

    def close(self) -> None:
        self.source.close()
        self.sink.close()

    @staticmethod
    def request(session: requests.Session, url: str, body: dict[str, Any] | str) -> dict[str, Any]:
        try:
            with session.post(
                url,
                data=body if isinstance(body, str) else json.dumps(body),
                headers={"Content-Type": "application/json"},
                timeout=(5, 60),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    raise AdapterError(f"upstream HTTP status {response.status_code}")
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ResponseTooLargeError("upstream response exceeds byte limit")
                    chunks.append(chunk)
                data = json.loads(b"".join(chunks))
        except (requests.RequestException, ValueError):
            raise AdapterError("upstream request or JSON response failed") from None
        if not isinstance(data, dict):
            raise AdapterError("upstream response must be an object")
        return data

    def window(
        self, scope: Scope, lower: datetime, upper: datetime
    ) -> tuple[list[Record], dict[str, str]]:
        for _ in range(3):
            try:
                records = self.raw_window(scope, lower, upper)
                result = self.request(
                    self.source,
                    f"{self.source_url}/_plugins/_ppl",
                    {"query": brain_query(scope, lower, upper), "fetch_size": MAX_RECORDS + 1},
                )
                labels = parse_labels(result, records)
                after = self.raw_window(scope, lower, upper)
                if Counter((r.index, r.id, r.message) for r in records) != Counter(
                    (r.index, r.id, r.message) for r in after
                ):
                    raise PopulationChangedError("source population changed during Brain query")
                return records, labels
            except PopulationChangedError:
                continue
            except ResponseTooLargeError:
                # Only query responses are splittable; ingestion must keep its immutable retry.
                raise WindowFullError("query response exceeds byte limit") from None
        raise AdapterError("source and Brain populations unstable after three attempts")

    def raw_window(self, scope: Scope, lower: datetime, upper: datetime) -> list[Record]:
        raw = self.request(
            self.source,
            f"{self.source_url}/{INDEX}/_search?allow_partial_search_results=false",
            {
                "query": selection(scope, lower, upper),
                "size": MAX_RECORDS + 1,
                "track_total_hits": True,
                "sort": [{"@timestamp": "asc"}],
                "_source": [
                    "@timestamp",
                    "log",
                    "kubernetes.namespace_name",
                    "kubernetes.container_name",
                    "kubernetes.container_image",
                    "stack_log.failure_type",
                ],
            },
        )
        if raw.get("timed_out") is not False:
            raise AdapterError("search timeout or missing completion status")
        shards = raw.get("_shards")
        if (
            not isinstance(shards, dict)
            or shards.get("failed") != 0
            or not isinstance(shards.get("total"), int)
            or shards.get("successful") != shards["total"]
        ):
            raise AdapterError("search partial or missing shard completion")
        hits = raw.get("hits")
        if not isinstance(hits, dict):
            raise AdapterError("invalid search hits")
        total, rows = hits.get("total"), hits.get("hits")
        if (
            not isinstance(total, dict)
            or total.get("relation") != "eq"
            or type(total.get("value")) is not int
            or not isinstance(rows, list)
        ):
            raise AdapterError("search total must be exact")
        if total["value"] > MAX_RECORDS:
            raise WindowFullError("search window exceeds occurrence limit")
        if len(rows) != total["value"]:
            raise AdapterError("search population incomplete")
        try:
            records = [parse_record(row, scope, lower, upper) for row in rows]
        except (KeyError, TypeError, ValueError):
            raise AdapterError("invalid source record") from None
        if len({(record.index, record.id) for record in records}) != len(records):
            raise AdapterError("duplicate source identities in window")
        return records

    def deliver(self, payload: str) -> None:
        result = self.request(self.sink, self.store_url, payload)
        if result.get("id") != json.loads(payload)["event_id"]:
            # The server may already have accepted it. Preserve payload and event ID on retry.
            raise AdapterError("ingestion acknowledgement mismatch; acceptance uncertain")


def parse_record(row: Any, scope: Scope, lower: datetime, upper: datetime) -> Record:
    source = row["_source"]
    metadata = source["kubernetes"]
    namespace, container = metadata["namespace_name"], metadata["container_name"]
    failure = source["stack_log"]["failure_type"]
    timestamp = source["@timestamp"]
    message = source["log"]
    if (
        not isinstance(message, str)
        or not message
        or len(message.encode()) > MAX_MESSAGE_BYTES
        or not isinstance(timestamp, str)
        or not lower <= utc(timestamp) < upper
        or namespace not in scope.namespaces
        or container not in scope.containers
        or failure not in scope.failure_types
        or not isinstance(row["_id"], str)
        or not row["_id"]
        or len(row["_id"]) > 1024
        or not isinstance(row["_index"], str)
        or not row["_index"].startswith("fluent-bit-")
        or len(row["_index"]) > 255
    ):
        raise ValueError("invalid selected source record")
    image = metadata.get("container_image")
    if image is not None and (
        not isinstance(image, str) or len(image) > 200 or any(ord(c) < 32 for c in image)
    ):
        raise ValueError("invalid release metadata")
    return Record(
        row["_index"],
        row["_id"],
        stamp(utc(timestamp)),
        message,
        namespace,
        container,
        failure,
        image,
    )


def parse_labels(result: dict[str, Any], records: list[Record]) -> dict[str, str]:
    if any(key in result for key in ("error", "_shards", "timed_out", "cursor")):
        raise AdapterError("unexpected PPL error, partial status or cursor")
    schema, rows = result.get("schema"), result.get("datarows")
    if not isinstance(schema, list) or not isinstance(rows, list):
        raise AdapterError("invalid PPL schema or rows")
    if (
        len(schema) != 2
        or any(not isinstance(field, dict) for field in schema)
        or {field.get("name") for field in schema} != {"log", "patterns_field"}
        or any(field.get("type") != "string" for field in schema)
        or type(result.get("total")) is not int
        or type(result.get("size")) is not int
        or result["total"] != len(rows)
        or result["size"] != len(rows)
    ):
        raise AdapterError("PPL schema or population size mismatch")
    if len(rows) > MAX_RECORDS:
        raise WindowFullError("PPL window exceeds occurrence limit")
    message_column = 0 if isinstance(schema[0], dict) and schema[0]["name"] == "log" else 1
    labels: dict[str, str] = {}
    messages: Counter[str] = Counter()
    for row in rows:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or not all(
                isinstance(value, str) and value and len(value.encode()) <= MAX_MESSAGE_BYTES
                for value in row
            )
        ):
            raise AdapterError("invalid PPL label row")
        message, template = row[message_column], row[1 - message_column]
        if message in labels and labels[message] != template:
            raise AdapterError("conflicting Brain labels for identical messages")
        labels[message] = template
        messages[message] += 1
    if messages != Counter(record.message for record in records):
        raise PopulationChangedError("search and Brain occurrence populations differ")
    return labels
