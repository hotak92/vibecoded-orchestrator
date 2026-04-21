# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 VibeCoded Tools
"""SQLite-backed local event queue for telemetry.

Design:
    - WAL mode for concurrent readers + a single writer.
    - Queue capped at MAX_QUEUE_SIZE events; inserts over the cap drop the
      oldest un-uploaded rows first (FIFO eviction).
    - `events.uploaded_at` is NULL until the uploader marks a batch sent;
      old uploaded rows are purged by cleanup_old().
    - Never raises out of the module — every failure is logged at DEBUG
      and the caller sees a best-effort no-op.

Schema:
    CREATE TABLE events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type   TEXT    NOT NULL,
        payload_json TEXT    NOT NULL,
        created_at   REAL    NOT NULL,   -- epoch seconds
        uploaded_at  REAL             -- NULL until uploaded
    );
    CREATE INDEX idx_events_pending ON events (uploaded_at, id);
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)

DB_DIR = Path.home() / ".vibecoded"
DB_FILE = DB_DIR / "telemetry.db"

MAX_QUEUE_SIZE = 1000
# Rows older than this get purged after upload.
UPLOADED_RETENTION_SECONDS = 7 * 24 * 3600


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    created_at   REAL    NOT NULL,
    uploaded_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_events_pending ON events (uploaded_at, id);
"""


class TelemetryQueue:
    """Thread-safe SQLite queue.

    Methods never raise on I/O errors — they log at DEBUG and fall back to
    a no-op return value (empty list, zero, etc).
    """

    def __init__(self, db_path: Optional[Path] = None, *, max_size: int = MAX_QUEUE_SIZE) -> None:
        self._db_path = Path(db_path) if db_path is not None else DB_FILE
        self._max_size = max_size
        self._lock = threading.Lock()
        self._init_db()

    # ---- setup ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # check_same_thread=False because we serialize access via self._lock.
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                conn = self._connect()
                try:
                    conn.executescript(_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()
        except (OSError, sqlite3.Error) as e:
            log.debug("Telemetry queue init failed: %s", e)

    # ---- write ----------------------------------------------------------

    def enqueue(self, event_type: str, payload: dict) -> bool:
        """Insert a new event. Drops oldest on overflow. Returns True on success."""
        try:
            raw = json.dumps(payload, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as e:
            log.debug("Could not serialize telemetry payload: %s", e)
            return False

        try:
            with self._lock:
                conn = self._connect()
                try:
                    self._enforce_cap(conn)
                    conn.execute(
                        "INSERT INTO events (event_type, payload_json, created_at) "
                        "VALUES (?, ?, ?);",
                        (event_type, raw, time.time()),
                    )
                    conn.commit()
                    return True
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry enqueue failed: %s", e)
            return False

    def _enforce_cap(self, conn: sqlite3.Connection) -> None:
        """If queue at/over capacity, drop oldest rows (prefer already-uploaded)."""
        row = conn.execute("SELECT COUNT(*) FROM events;").fetchone()
        count = int(row[0]) if row else 0
        if count < self._max_size:
            return
        # First purge anything already uploaded.
        conn.execute("DELETE FROM events WHERE uploaded_at IS NOT NULL;")
        row = conn.execute("SELECT COUNT(*) FROM events;").fetchone()
        count = int(row[0]) if row else 0
        if count < self._max_size:
            return
        # Still over capacity — drop the oldest pending rows.
        overflow = count - self._max_size + 1
        conn.execute(
            "DELETE FROM events WHERE id IN "
            "(SELECT id FROM events ORDER BY id ASC LIMIT ?);",
            (overflow,),
        )

    # ---- read ----------------------------------------------------------

    def pending_events(self, limit: int = 100) -> List[dict]:
        """Return up to `limit` oldest un-uploaded events.

        Each dict has keys: id, event_type, payload, created_at.
        """
        try:
            with self._lock:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT id, event_type, payload_json, created_at "
                        "FROM events "
                        "WHERE uploaded_at IS NULL "
                        "ORDER BY id ASC LIMIT ?;",
                        (limit,),
                    ).fetchall()
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry pending fetch failed: %s", e)
            return []

        out: List[dict] = []
        for row_id, etype, raw, created in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {"_parse_error": True}
            out.append({
                "id": int(row_id),
                "event_type": etype,
                "payload": payload,
                "created_at": float(created),
            })
        return out

    def recent_events(self, limit: int = 20, include_uploaded: bool = True) -> List[dict]:
        """Return the most recent events (newest first) for the dashboard CLI."""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    if include_uploaded:
                        rows = conn.execute(
                            "SELECT id, event_type, payload_json, created_at, uploaded_at "
                            "FROM events ORDER BY id DESC LIMIT ?;",
                            (limit,),
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT id, event_type, payload_json, created_at, uploaded_at "
                            "FROM events WHERE uploaded_at IS NULL "
                            "ORDER BY id DESC LIMIT ?;",
                            (limit,),
                        ).fetchall()
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry recent fetch failed: %s", e)
            return []

        out: List[dict] = []
        for row_id, etype, raw, created, uploaded in rows:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {"_parse_error": True}
            out.append({
                "id": int(row_id),
                "event_type": etype,
                "payload": payload,
                "created_at": float(created),
                "uploaded_at": float(uploaded) if uploaded is not None else None,
            })
        return out

    def count_pending(self) -> int:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE uploaded_at IS NULL;"
                    ).fetchone()
                    return int(row[0]) if row else 0
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry count failed: %s", e)
            return 0

    def count_total(self) -> int:
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute("SELECT COUNT(*) FROM events;").fetchone()
                    return int(row[0]) if row else 0
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry count failed: %s", e)
            return 0

    # ---- mutate --------------------------------------------------------

    def mark_uploaded(self, ids: Iterable[int]) -> int:
        ids_list = [int(x) for x in ids]
        if not ids_list:
            return 0
        placeholders = ",".join("?" for _ in ids_list)
        try:
            with self._lock:
                conn = self._connect()
                try:
                    now = time.time()
                    cur = conn.execute(
                        f"UPDATE events SET uploaded_at = ? WHERE id IN ({placeholders});",
                        (now, *ids_list),
                    )
                    conn.commit()
                    return int(cur.rowcount or 0)
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry mark_uploaded failed: %s", e)
            return 0

    def cleanup_old(self, retention_seconds: float = UPLOADED_RETENTION_SECONDS) -> int:
        """Purge rows already uploaded more than `retention_seconds` ago."""
        cutoff = time.time() - retention_seconds
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "DELETE FROM events "
                        "WHERE uploaded_at IS NOT NULL AND uploaded_at < ?;",
                        (cutoff,),
                    )
                    conn.commit()
                    return int(cur.rowcount or 0)
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry cleanup_old failed: %s", e)
            return 0

    def clear(self) -> int:
        """Delete every event. Used by the `vibecoded telemetry clear` CLI."""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute("DELETE FROM events;")
                    conn.commit()
                    return int(cur.rowcount or 0)
                finally:
                    conn.close()
        except sqlite3.Error as e:
            log.debug("Telemetry clear failed: %s", e)
            return 0


_singleton_lock = threading.Lock()
_singleton: Optional[TelemetryQueue] = None


def get_queue() -> TelemetryQueue:
    """Process-wide singleton. Thread-safe first-time init."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = TelemetryQueue()
    return _singleton
