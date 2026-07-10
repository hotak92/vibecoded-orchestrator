# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read + write helpers for the launcher's ``app_state`` key/value table.

WHY a separate module from ``vco_lib.launcher_db_reader``:
``launcher_db_reader`` is deliberately READ-ONLY — every one of its
connections uses the ``file:<path>?mode=ro&immutable=1`` URI so a resolver
running inside a hook / MCP / script can never acquire a writer lock or
mutate launcher state it doesn't own. Keeping the WRITE path in its own
module preserves that guarantee at the import boundary: a module that only
needs to read cannot accidentally reach a writer by importing the reader.

Canonical home (v0.2.77 Part 7a cluster D convergence) for the
``app_state`` accessors that install.py used to inline:
  - ``read_app_state_key``  — readonly (mode=ro&immutable=1), None on miss.
  - ``write_app_state_key`` — INSERT ... ON CONFLICT upsert.

Both take an EXPLICIT ``db_path`` (no discovery here) so the module is pure
and unit-testable, and so install.py can keep its own
``_discover_app_state_db_path`` wrapper (which many tests patch) as the
path oracle. install.py's ``_read_app_state_key`` / ``_write_app_state_key``
are thin name-stable wrappers that thread the discovered path in.

Soft-fail contract (matches install.py's historical behaviour): these
helpers NEVER raise. A read miss / unreachable DB returns None; a write
failure is swallowed (callers use app_state for telemetry / cache — a
missed write just means the next run re-scans).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional


def read_app_state_key(db_path: Path, key: str) -> Optional[str]:
    """Read a single key from ``launcher.db``'s ``app_state`` table.

    Returns the string value, or None when the DB file is absent, the key
    is missing, or any error occurs (soft-fail: callers use None as
    "unknown / first run").

    Uses the sqlite3 URI form ``file:<path>?mode=ro&immutable=1`` — the same
    readonly pattern as :func:`vco_lib.launcher_db_reader._open_db_readonly`.
    The ro+immutable URI does NOT acquire a writer lock and never blocks
    regardless of what the launcher is doing (pre-v0.2.53 this used
    ``sqlite3.connect(timeout=5.0)`` which could block for up to 5s on
    Windows when the launcher held a write lock).
    """
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{db_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def write_app_state_key(db_path: Path, key: str, value: str) -> None:
    """Write/overwrite a single key in ``launcher.db``'s ``app_state`` table.

    Soft-fail: any sqlite error (missing file, locked DB, missing table,
    permission denied) is silently swallowed. Callers use this for
    telemetry / cache; a missed write just means the next run re-scans.
    """
    if not db_path.is_file():
        return
    try:
        now_ms = int(time.time() * 1000)
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now_ms),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
