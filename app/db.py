"""SQLite store for the service's own state.

The connector methods never touch this (Appendix A: "methods return results
only"). The service layer around them - jobs, the catalogue, the post ledger
used for frequency caps and idempotency, certification evidence - lives here.
SQLite keeps `docker compose up` to one file volume; every query is short, so a
single connection guarded by a lock is enough for this workload.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS destinations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    platform_key TEXT NOT NULL,
    connector_key TEXT NOT NULL,
    surface TEXT,
    category TEXT,
    audience_tags TEXT DEFAULT '[]',
    language TEXT,
    geo TEXT,
    est_audience INTEGER,
    rules_url TEXT,
    frequency_cap TEXT,
    self_promo_allowed TEXT,
    link_policy TEXT,
    disclosure_required INTEGER DEFAULT 0,
    automation_allowed INTEGER DEFAULT 1,
    relevance_score INTEGER,
    relevance_reason TEXT,
    level TEXT DEFAULT 'L0',
    last_certified_at TEXT,
    status TEXT DEFAULT 'uncertified',
    rules_status TEXT DEFAULT 'confirmed',
    config TEXT DEFAULT '{}',
    connection_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(normalized_url, platform_key)
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    connector_key TEXT NOT NULL,
    name TEXT,
    auth_type TEXT,
    credentials_enc TEXT,
    status TEXT DEFAULT 'active',
    expires_at TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    progress INTEGER DEFAULT 0,
    request TEXT,
    result TEXT,
    error TEXT,
    callback_url TEXT,
    correlation_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT NOT NULL,
    scope TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code INTEGER,
    response TEXT,
    created_at TEXT,
    PRIMARY KEY (key, scope)
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    destination_id TEXT,
    connector_key TEXT,
    idempotency_key TEXT UNIQUE,
    state TEXT,
    platform_post_id TEXT,
    live_url TEXT,
    is_test INTEGER DEFAULT 0,
    promotional INTEGER DEFAULT 0,
    title TEXT,
    fingerprint TEXT,
    rendition TEXT,
    reason TEXT,
    reschedule_at TEXT,
    publication_id TEXT,
    attempts INTEGER DEFAULT 0,
    created_at TEXT,
    published_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS posts_dest ON posts(destination_id, state, published_at);
CREATE TABLE IF NOT EXISTS certifications (
    id TEXT PRIMARY KEY,
    destination_id TEXT,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_s REAL,
    steps TEXT,
    evidence_url TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS rule_proposals (
    id TEXT PRIMARY KEY,
    destination_id TEXT,
    source_url TEXT,
    proposed TEXT,
    evidence TEXT,
    provider TEXT,
    status TEXT,
    decided_by TEXT,
    created_at TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    destination_id TEXT,
    kind TEXT,
    message TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    post_id TEXT,
    fetched_at TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS feed_items (
    destination_id TEXT,
    guid TEXT,
    item TEXT,
    created_at TEXT,
    PRIMARY KEY (destination_id, guid)
);
CREATE TABLE IF NOT EXISTS breaker (
    connector_key TEXT,
    destination_id TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    PRIMARY KEY (connector_key, destination_id)
);
CREATE TABLE IF NOT EXISTS kill_switch (
    connector_key TEXT PRIMARY KEY,
    disabled INTEGER DEFAULT 0,
    reason TEXT
);
"""

JSON_COLUMNS = {
    "audience_tags", "config", "request", "result", "error", "response", "rendition", "steps",
    "proposed", "evidence", "data", "item",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        for k, v in out.items():
            if k in JSON_COLUMNS and isinstance(v, str):
                try:
                    out[k] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        return out

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, bool):
            return int(value)
        return value

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, [self._encode(p) for p in params])

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._decode(self._conn.execute(sql, [self._encode(p) for p in params]).fetchone())

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, [self._encode(p) for p in params]).fetchall()
        return [self._decode(r) for r in rows]  # type: ignore[misc]

    def insert(self, table: str, row: dict[str, Any], or_ignore: bool = False) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        self.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

    def update(self, table: str, key: str, key_value: Any, changes: dict[str, Any]) -> None:
        if not changes:
            return
        sets = ", ".join(f"{c} = ?" for c in changes)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", [*changes.values(), key_value])

    def transaction(self):  # usage: with db.transaction(): ...
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database) -> None:
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().resolved_db_path())
    return _db


def reset_db(path: str | None = None) -> Database:
    """Open a fresh database (used by tests and the batch runner)."""
    global _db
    _db = Database(path or get_settings().resolved_db_path())
    return _db
