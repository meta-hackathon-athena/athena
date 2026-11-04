"""Which Context Engine events have had their side effects (Episode Writer + Agent).

Context Engine commits an event before acknowledging it; the graph writes and the
agent run after that. If the process dies (or FalkorDB is down) in between, the event
is in SQLite but its side effects are not. The ledger records each event's progress so
``VixyRuntime.recover()`` can finish exactly those events, and a duplicate re-send of a
fully processed event does nothing. Graph writes are idempotent (MERGE on stable ids),
so finishing an event twice is harmless; skipping one is what the ledger prevents.

  status: pending -> episode_done -> done        (failed: stage + error kept, retried)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    event_id    TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ingest_seq  INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'episode_done', 'done', 'failed')),
    stage       TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    decision_ids TEXT,
    updated_ms  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_session_seq ON processed (session_id, ingest_seq);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Ledger:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=FULL")
        self._c.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._c.close()

    def _x(self, sql: str, params=()):
        with self._lock:
            return self._c.execute(sql, params).fetchall()

    def get(self, event_id: str) -> dict | None:
        rows = self._x("SELECT event_id, session_id, kind, ingest_seq, status, stage, error, attempts, decision_ids "
                       "FROM processed WHERE event_id = ?", (event_id,))
        if not rows:
            return None
        keys = ["event_id", "session_id", "kind", "ingest_seq", "status", "stage", "error", "attempts", "decision_ids"]
        row = dict(zip(keys, rows[0]))
        row["decision_ids"] = json.loads(row["decision_ids"] or "[]")
        return row

    def status(self, event_id: str) -> str | None:
        row = self.get(event_id)
        return row["status"] if row else None

    def begin(self, event_id: str, session_id: str, kind: str, ingest_seq: int) -> None:
        self._x("""
            INSERT INTO processed (event_id, session_id, kind, ingest_seq, status, attempts, updated_ms)
            VALUES (?, ?, ?, ?, 'pending', 1, ?)
            ON CONFLICT(event_id) DO UPDATE SET attempts = attempts + 1, updated_ms = excluded.updated_ms
        """, (event_id, session_id, kind, ingest_seq, _now()))

    def mark(self, event_id: str, status: str, stage: str | None = None, error: str | None = None,
             decision_ids: list[str] | None = None) -> None:
        self._x("UPDATE processed SET status = ?, stage = ?, error = ?, decision_ids = COALESCE(?, decision_ids), "
                "updated_ms = ? WHERE event_id = ?",
                (status, stage, error, json.dumps(decision_ids) if decision_ids is not None else None,
                 _now(), event_id))

    def resume_after(self, session_id: str) -> int:
        """Replay CE events with ingest_seq > this to finish everything not yet done."""
        unfinished = self._x("SELECT MIN(ingest_seq) FROM processed WHERE session_id = ? AND status != 'done'",
                             (session_id,))[0][0]
        if unfinished is not None:
            return unfinished - 1
        done = self._x("SELECT MAX(ingest_seq) FROM processed WHERE session_id = ?", (session_id,))[0][0]
        return done or 0

    def counts(self, session_id: str | None = None) -> dict[str, int]:
        sql, params = "SELECT status, COUNT(*) FROM processed", ()
        if session_id:
            sql, params = sql + " WHERE session_id = ?", (session_id,)
        return dict(self._x(sql + " GROUP BY status", params))

    def failures(self) -> list[dict]:
        rows = self._x("SELECT event_id FROM processed WHERE status = 'failed' ORDER BY ingest_seq")
        return [self.get(r[0]) for r in rows]

    def get_kv(self, key: str, default=None):
        rows = self._x("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_kv(self, key: str, value) -> None:
        self._x("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))


def _now() -> int:
    return int(time.time() * 1000)
