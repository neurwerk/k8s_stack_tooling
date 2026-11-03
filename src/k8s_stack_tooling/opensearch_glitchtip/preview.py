"""Read-only policy comparison over an approved OpenSearch/Studio JSON export."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .policy import Decision, Policy, canonical, identity_parts, read_json
from .settings import MAX_RESPONSE_BYTES, Scope, identifier, load_scopes, utc
from .transport import parse_record, selected


def export_rows(data: Any) -> list[dict[str, Any]]:
    rows = data.get("hits", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("hits")
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("expected a bounded JSON log export")
    result: list[dict[str, Any]] = []
    for position, item in enumerate(rows):
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise ValueError("invalid log export row")
        row = cast(dict[str, Any], item)
        if "_source" in row:
            result.append(row)
        else:
            # opensearch-logs exports have no document ID; preview-local IDs are
            # never persisted or sent to ingestion.
            result.append(
                {
                    "_index": row.get("index", "fluent-bit-preview"),
                    "_id": f"preview-{position}",
                    "_source": {
                        "@timestamp": row["timestamp"],
                        "log": row["log"],
                        "kubernetes": {
                            "namespace_name": row["namespace"],
                            "container_name": row["container"],
                        },
                        "stack_log": {
                            "failure_type": row.get("failure_type"),
                            "level": row.get("level"),
                        },
                    },
                }
            )
    return result


def preview(
    rows: list[dict[str, Any]],
    scopes: tuple[Scope, ...],
    environment: str,
    policy: Policy,
    active: Policy,
    lower: datetime,
    upper: datetime,
) -> dict[str, Any]:
    policy.validate_scopes(environment, scopes)
    active.validate_scopes(environment, scopes)
    groups: dict[str, dict[str, Any]] = {}
    differences: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    field_occurrences: dict[str, Counter[str]] = defaultdict(Counter)
    excluded: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    seen = set()
    for position, row in enumerate(rows):
        try:
            candidates = [scope for scope in scopes if selected(row, scope, lower, upper)]
            if not candidates:
                excluded.append({"row": position, "reason": "outside_scope_or_window"})
                continue
            (scope,) = candidates
            record = parse_record(row, scope, lower, upper)
        except (KeyError, TypeError, ValueError, AttributeError):
            excluded.append({"row": position, "reason": "invalid_source_record"})
            continue
        source = (scope.id, record.index, record.id)
        if source in seen:
            excluded.append({"row": position, "reason": "duplicate_source"})
            continue
        seen.add(source)
        proposed = policy.evaluate(environment, scope, record)
        previous = active.evaluate(environment, scope, record)

        def fingerprint(decision: Decision) -> str:
            # Local comparison keys, deliberately not the durable adapter's keyed fingerprints.
            return hashlib.sha256(
                canonical(identity_parts(environment, scope, record, decision)).encode()
            ).hexdigest()

        group_id, before = fingerprint(proposed), fingerprint(previous)
        if group_id != before or proposed.action != previous.action or proposed != previous:
            changes.append(
                {
                    "row": position,
                    "previous_group": before,
                    "proposed_group": group_id,
                    "previous_action": previous.action,
                    "proposed_action": proposed.action,
                    "identity_changed": group_id != before,
                }
            )
        if proposed.action == "log_only":
            excluded.append({"row": position, "reason": "log_only", "rule_id": proposed.rule_id})
            continue
        group = groups.setdefault(
            group_id,
            {
                "group": group_id,
                "rule_id": proposed.rule_id,
                "count_unit": proposed.count_unit,
                "title": proposed.title,
                "explanation": proposed.explanation,
                "occurrences": 0,
                "rows": [],
                "differing_fields": [],
            },
        )
        group["occurrences"] += 1
        group["rows"].append(position)
        for field, value in proposed.fields.items():
            differences[group_id][field].add(canonical(value))
            field_occurrences[group_id][field] += 1
    for group_id, group in groups.items():
        group["differing_fields"] = sorted(
            field
            for field, values in differences[group_id].items()
            if len(values) > 1 or field_occurrences[group_id][field] < group["occurrences"]
        )
    return {
        "rows": len(rows),
        "groups": list(groups.values()),
        "exclusions": excluded,
        "changes": changes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Approved JSON log export")
    parser.add_argument("--scopes", required=True, type=Path)
    parser.add_argument("--environment", required=True, type=identifier)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument(
        "--active-policy", type=Path, help="Last valid active policy; default: exact fallback"
    )
    parser.add_argument("--starts-at-utc", required=True, type=utc)
    parser.add_argument("--ends-at-utc", required=True, type=utc, help="Exclusive upper bound")
    args = parser.parse_args()
    try:
        if args.starts_at_utc >= args.ends_at_utc:
            raise ValueError("invalid preview range")
        with args.input.open("rb") as stream:
            data = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("export exceeds byte limit")
        result = preview(
            export_rows(read_json(data.decode("utf-8"))),
            load_scopes(args.scopes),
            args.environment,
            Policy.load(args.policy),
            Policy.load(args.active_policy) if args.active_policy else Policy(),
            args.starts_at_utc,
            args.ends_at_utc,
        )
    except (OSError, UnicodeError, ValueError, KeyError, RecursionError):
        parser.exit(1, "preview_failed reason=invalid_policy_scopes_or_export\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
