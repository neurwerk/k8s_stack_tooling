"""Durable occurrence delivery; remote acceptance and local ack are not atomic."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .policy import Policy, identity_parts
from .reporting import log_links
from .settings import Scope, Settings, destination, stamp


@dataclass(frozen=True)
class Record:
    index: str
    id: str
    timestamp: str
    message: str
    namespace: str
    container: str
    failure_type: str
    image: str | None
    original_level: str | None = None


@contextmanager
def state_lock(path: Path):
    """Hold a separate inode for the full process lifetime, including DB setup."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("state is already owned by another process") from None
        yield
    finally:
        os.close(fd)


class State:
    def __init__(self, settings: Settings, scopes: Sequence[Scope]):
        path = settings.state_path
        existed = path.exists()
        self.environment = settings.environment
        self.settings = settings
        self.scopes = tuple(scopes)
        self.policy = Policy()
        self.policy_valid = True
        self.policy_rejections = 0
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            binding = json.dumps(
                {
                    "version": 1,
                    "destination": destination(settings.glitchtip_dsn.get_secret_value())[2],
                    "source": settings.opensearch_url.rstrip("/"),
                    "environment": settings.environment,
                    "initial_start_at": stamp(datetime.fromisoformat(settings.initial_start_at)),
                    "scopes": sorted((scope.binding() for scope in scopes), key=lambda s: s["id"]),
                },
                sort_keys=True,
            )
            if existed:
                metadata = dict(self.db.execute("SELECT name, value FROM metadata"))
                if metadata.get("binding") != binding:
                    raise ValueError("state configuration binding mismatch")
                key = metadata.get("identity_key")
                if not isinstance(key, bytes) or len(key) != 32:
                    raise ValueError("state identity key missing or invalid")
                self.key = key
            else:
                self.key = os.urandom(32)
                with self.db:
                    self.db.execute("CREATE TABLE metadata (name TEXT PRIMARY KEY, value BLOB)")
                    self.db.executemany(
                        "INSERT INTO metadata VALUES (?, ?)",
                        [("binding", binding), ("identity_key", self.key)],
                    )
                    self.db.execute("CREATE TABLE cursors (scope TEXT PRIMARY KEY, upper TEXT)")
                    self.db.execute(
                        "CREATE TABLE messages (scope TEXT, digest TEXT, identity TEXT, "
                        "PRIMARY KEY(scope, digest))"
                    )
                    self.db.execute(
                        "CREATE TABLE templates (scope TEXT, digest TEXT, identity TEXT, "
                        "PRIMARY KEY(scope, digest, identity))"
                    )
                    self.db.execute(
                        "CREATE TABLE records (source TEXT PRIMARY KEY, event_id TEXT UNIQUE, "
                        "payload TEXT, delivered INTEGER NOT NULL DEFAULT 0, "
                        "attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)"
                    )
                    self.db.execute(
                        "CREATE INDEX pending_records ON records(delivered, next_attempt)"
                    )
            with self.db:
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(records)")}
                if "queued_at" not in columns:
                    self.db.execute(
                        "ALTER TABLE records ADD COLUMN queued_at REAL NOT NULL DEFAULT 0"
                    )
                    self.db.execute(
                        "UPDATE records SET queued_at=? WHERE delivered=0", (time.time(),)
                    )
            saved_policy = self.db.execute(
                "SELECT value FROM metadata WHERE name='reporting_policy'"
            ).fetchone()
            if saved_policy:
                self.policy = Policy.parse(saved_policy[0])
            self.reload_policy()
        except Exception:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def digest(self, *parts: str) -> str:
        encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
        return hmac.new(self.key, encoded, hashlib.sha256).hexdigest()

    def cursor(self, scope: Scope, initial: datetime) -> datetime:
        row = self.db.execute("SELECT upper FROM cursors WHERE scope=?", (scope.id,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else initial

    def pending_count(self) -> int:
        return self.db.execute("SELECT count(*) FROM records WHERE delivered=0").fetchone()[0]

    def reload_policy(self) -> bool:
        """Commit a complete valid policy before activation; keep the last good one on error."""
        try:
            candidate = (
                Policy.load(self.settings.reporting_policy_file)
                if self.settings.reporting_policy_file is not None
                else Policy()
            )
            candidate.validate_scopes(self.environment, self.scopes)
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.policy_valid = False
            self.policy_rejections += 1
            return False
        serialized = candidate.serialize()
        with self.db:
            self.db.execute(
                "INSERT INTO metadata VALUES ('reporting_policy', ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (serialized,),
            )
        self.policy = candidate
        self.policy_valid = True
        return True

    def enqueue(
        self, scope: Scope, records: Sequence[Record], labels: dict[str, str], upper: datetime
    ) -> int:
        """Commit all identities, immutable payloads and the window cursor together."""
        added = 0
        with self.db:
            for record in records:
                source = self.digest("source", scope.id, record.index, record.id)
                if self.db.execute("SELECT 1 FROM records WHERE source=?", (source,)).fetchone():
                    continue
                event_id = self.digest("event", source)[:32]
                decision = self.policy.evaluate(self.environment, scope, record)
                if decision.action == "log_only":
                    # An explicit exclusion is handled, not a successful remote delivery.
                    # Retain its occurrence identity so configuration changes cannot replay history.
                    self.db.execute(
                        "INSERT INTO records(source, event_id, payload, delivered) "
                        "VALUES (?, ?, NULL, 2)",
                        (source, event_id),
                    )
                    continue
                context = (scope.id, record.namespace, record.container, record.failure_type)
                # Policy grouping applies only to new occurrences; fallback keeps v2.
                # Existing outbox bytes, source IDs and historical mappings stay intact.
                parts = identity_parts(self.environment, scope, record, decision)
                message_digest = (
                    self.digest("message-v2", *context, record.message)
                    if decision.identity_parts is None
                    else self.digest("message-policy-v1", *parts, record.message)
                )
                row = self.db.execute(
                    "SELECT identity FROM messages WHERE scope=? AND digest=?",
                    (scope.id, message_digest),
                ).fetchone()
                identity = row[0] if row else self.digest(*parts)
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?)",
                    (scope.id, message_digest, identity),
                )
                # Retain Brain observations for comparison, not as merge authority.
                if record.message in labels:
                    template_digest = self.digest("template-v2", *context, labels[record.message])
                    self.db.execute(
                        "INSERT OR IGNORE INTO templates VALUES (?, ?, ?)",
                        (scope.id, template_digest, identity),
                    )
                matching_url, surrounding_url = log_links(self.settings.studio_url, record)
                payload: dict[str, Any] = {
                    "event_id": event_id,
                    "message": decision.title,
                    "timestamp": record.timestamp,
                    "platform": "other",
                    "level": decision.level,
                    "environment": self.environment,
                    "fingerprint": [identity],
                    "tags": {
                        "application": scope.application,
                        "service": scope.service,
                        "namespace": record.namespace,
                        "container": record.container,
                        "failure_type": record.failure_type,
                    },
                    "extra": {
                        "reporting": {
                            "rule_id": decision.rule_id,
                            "count_unit": decision.count_unit,
                            "explanation": decision.explanation,
                            "matching_logs_url": matching_url,
                            "surrounding_logs_url": surrounding_url,
                            "original_timestamp": record.timestamp,
                        },
                        "original_level": decision.original_level,
                        "original_log": record.message,
                        "diagnostics": decision.diagnostics,
                    },
                }
                release = scope.release or record.image
                if release:
                    payload["release"] = release
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO records(source, event_id, payload, queued_at) "
                    "VALUES (?, ?, ?, ?)",
                    (source, event_id, json.dumps(payload, ensure_ascii=False), time.time()),
                )
                added += inserted.rowcount
            self.db.execute(
                "INSERT INTO cursors VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET upper="
                "MAX(upper, excluded.upper)",
                (scope.id, stamp(upper)),
            )
        return added

    def due(self, now: float, limit: int = 100) -> list[tuple[str, str, int]]:
        return self.db.execute(
            "SELECT source, payload, attempts FROM records WHERE delivered=0 AND "
            "next_attempt<=? ORDER BY next_attempt, rowid LIMIT ?",
            (now, limit),
        ).fetchall()

    def acknowledge(self, source: str) -> None:
        with self.db:
            # Retain permanent occurrence tracking, but remove delivered message contents.
            self.db.execute(
                "UPDATE records SET delivered=1, payload=NULL WHERE source=?", (source,)
            )

    def defer(self, source: str, attempts: int, now: float) -> None:
        delay = min(300, 2 ** min(attempts + 1, 9))
        with self.db:
            self.db.execute(
                "UPDATE records SET attempts=attempts+1, next_attempt=? WHERE source=?",
                (now + delay, source),
            )
