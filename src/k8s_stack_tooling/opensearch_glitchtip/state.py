"""Durable occurrence delivery; remote acceptance and local ack are not atomic."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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

    def enqueue(
        self, scope: Scope, records: Sequence[Record], labels: dict[str, str], upper: datetime
    ) -> int:
        """Commit all identities, immutable payloads and the window cursor together."""
        added = 0
        with self.db:
            known: dict[str, str] = {}
            candidates: dict[str, set[str]] = {}
            prior_templates: dict[str, set[str]] = {}
            for record in records:
                message_digest = self.digest("message", scope.id, record.message)
                row = self.db.execute(
                    "SELECT identity FROM messages WHERE scope=? AND digest=?",
                    (scope.id, message_digest),
                ).fetchone()
                template_digest = self.digest("template", scope.id, labels[record.message])
                if template_digest not in candidates:
                    candidates[template_digest] = {
                        row[0]
                        for row in self.db.execute(
                            "SELECT identity FROM templates WHERE scope=? AND digest=?",
                            (scope.id, template_digest),
                        )
                    }
                    prior_templates[template_digest] = set(candidates[template_digest])
                if row:
                    known[message_digest] = row[0]
                    candidates[template_digest].add(row[0])
            for record in records:
                message_digest = self.digest("message", scope.id, record.message)
                template_digest = self.digest("template", scope.id, labels[record.message])
                identity = known.get(message_digest)
                if identity is None:
                    prior = prior_templates[template_digest]
                    # A novel generalization never inherits an old message's identity.
                    # Ambiguous templates isolate new messages rather than bridging issues.
                    if len(prior) == 1 and len(candidates[template_digest] | prior) == 1:
                        identity = next(iter(prior))
                    elif len(candidates[template_digest]) > 1:
                        identity = self.digest("isolated", scope.id, message_digest)
                    else:
                        identity = self.digest("issue", scope.id, template_digest)
                    known[message_digest] = identity
                self.db.execute(
                    "INSERT OR IGNORE INTO messages VALUES (?, ?, ?)",
                    (scope.id, message_digest, identity),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO templates VALUES (?, ?, ?)",
                    (scope.id, template_digest, identity),
                )
                source = self.digest("source", scope.id, record.index, record.id)
                event_id = self.digest("event", source)[:32]
                payload: dict[str, Any] = {
                    "event_id": event_id,
                    "message": record.message,
                    "timestamp": record.timestamp,
                    "platform": "other",
                    "level": "error",
                    "environment": self.environment,
                    "fingerprint": [identity],
                    "tags": {
                        "application": scope.application,
                        "service": scope.service,
                        "namespace": record.namespace,
                        "container": record.container,
                        "failure_type": record.failure_type,
                    },
                }
                release = scope.release or record.image
                if release:
                    payload["release"] = release
                inserted = self.db.execute(
                    "INSERT OR IGNORE INTO records(source, event_id, payload) VALUES (?, ?, ?)",
                    (source, event_id, json.dumps(payload, ensure_ascii=False)),
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
