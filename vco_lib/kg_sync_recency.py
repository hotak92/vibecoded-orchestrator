# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""When was the orchestrator root's KG last CERTIFIED as synced? (v0.2.101 ⑥)

The READER of ``launcher.db app_state["last_kg_sync_at"]``. Its writers are
:func:`vco_lib.kg_context_triple.record` (every certified whole-tree sync of the
orchestrator root — install's seed, the detached / session-start drivers, the
launcher's Sync button, ``migrate-collections``, a hand-run ``kg-sync --all``)
and install.py's partial-run and empty-diff paths. Until this module nothing
read the row: a promise with no reader. ``vco doctor`` now reports it
(``doctor.probe_last_kg_sync``), in the human report and in ``--json``.

Scope, and why the doctor reports it ON THE ORCHESTRATOR ROOT ONLY: the row is
machine-global and, by the SF-1 rule in ``kg_context_triple``, only a run that
seeded the ROOT's own KG may write it — a registered project's clean ``--all``
deliberately leaves it alone. Shown under any other project it would claim a
sync that project never had, so the probe stays silent there.

Diagnostic only: the age is REPORTED, never graded (the ``last_update_run``
rule — a user whose KG simply has not changed must not be told something is
wrong). ``last_installed_kg_collection`` (the class the stamp is about) rides
along in the summary; ``last_kg_sync_stats`` (install.py's node counts) only in
the JSON detail, as ``install_stats`` — install.py is its sole writer, so after
a launcher Sync it describes an older run than the stamp.

Pure apart from :func:`read_rows`, the one DB read (read-only, soft-fail).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from vco_lib.kg_context_triple import (
    APP_STATE_KEY_LAST_KG_COLLECTION,
    APP_STATE_KEY_LAST_KG_SYNC_AT,
)

#: install.py's ``_APP_STATE_KEY_LAST_KG_SYNC_STATS`` — the row only install.py
#: writes (``kg_context_triple`` deliberately does not invent counts).
#: MUST MATCH install.py (parity-pinned by tests/test_v02101_kg_sync_recency.py).
APP_STATE_KEY_LAST_KG_SYNC_STATS = "last_kg_sync_stats"

#: Outcome states of :func:`describe`.
STATE_RECORDED = "recorded"
STATE_NEVER = "never"
STATE_UNPARSEABLE = "unparseable"
STATE_NO_DB = "no_db"


def read_rows(
    read_key: Optional[Callable[[str], Optional[str]]] = None,
    db_present: Optional[Callable[[], bool]] = None,
) -> dict:
    """The raw rows this report needs. Never raises.

    ``db_present`` separates "the launcher DB does not exist" (nothing could
    have been recorded on this machine) from "the DB exists, the row does not"
    (no certified whole-tree sync yet) — ``read_app_state_value`` returns
    ``None`` for both.
    """
    if read_key is None:
        from vco_lib.launcher_db_reader import read_app_state_value

        read_key = read_app_state_value
    if db_present is None:
        def _present() -> bool:
            from vco_lib.paths import launcher_db_path

            try:
                return Path(launcher_db_path()).is_file()
            except Exception:  # noqa: BLE001 — cannot resolve ⇒ treat as absent
                return False

        db_present = _present
    rows: dict = {"db_present": False}
    try:
        rows["db_present"] = bool(db_present())
    except Exception:  # noqa: BLE001
        rows["db_present"] = False
    for key in (APP_STATE_KEY_LAST_KG_SYNC_AT, APP_STATE_KEY_LAST_KG_COLLECTION,
                APP_STATE_KEY_LAST_KG_SYNC_STATS):
        try:
            rows[key] = read_key(key)
        except Exception:  # noqa: BLE001 — soft-fail: an unreadable row is absent
            rows[key] = None
    return rows


def parse_stamp(value: Any) -> Optional[datetime]:
    """An ``isoformat()`` stamp (both writers) → aware UTC datetime, or None.

    A naive value is read as UTC (both writers stamp UTC; a hand-edited row
    without an offset must not shift by the reader's local zone).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def format_age(seconds: float) -> str:
    """``3d 4h`` / ``5h 12m`` / ``42m`` / ``<1m``; a FUTURE stamp says so."""
    if seconds < 0:
        return "in the future (clock skew?)"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _parse_stats(raw: Any) -> Optional[dict]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def describe(rows: Mapping[str, Any], *, now: Optional[datetime] = None) -> dict:
    """Turn :func:`read_rows`' output into ``{"state", "summary", "detail"}``. PURE."""
    raw = rows.get(APP_STATE_KEY_LAST_KG_SYNC_AT)
    collection = rows.get(APP_STATE_KEY_LAST_KG_COLLECTION) or None
    stats = _parse_stats(rows.get(APP_STATE_KEY_LAST_KG_SYNC_STATS))
    detail: dict = {
        "last_kg_sync_at": raw,
        "age_seconds": None,
        "kg_collection": collection,
        # install.py's node counts. JSON-only, never in the summary: only
        # install.py writes this row, so after a launcher Sync / kg-sync --all
        # it describes an OLDER run than the stamp beside it.
        "install_stats": stats,
        "launcher_db_present": bool(rows.get("db_present")),
    }
    if not rows.get("db_present"):
        return {
            "state": STATE_NO_DB,
            "summary": "last KG sync: unknown — no launcher.db on this machine, "
                       "so no certified sync can have been recorded",
            "detail": detail,
        }
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {
            "state": STATE_NEVER,
            "summary": "last KG sync: never (no certified whole-tree sync recorded yet)",
            "detail": detail,
        }
    when = parse_stamp(raw)
    if when is None:
        return {
            "state": STATE_UNPARSEABLE,
            "summary": f"last KG sync: unreadable timestamp {raw!r} in launcher.db",
            "detail": detail,
        }
    age = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    detail["age_seconds"] = age
    summary = f"last KG sync: {raw} (age {format_age(age)})"
    if collection:
        summary += f", collection {collection}"
    return {"state": STATE_RECORDED, "summary": summary, "detail": detail}


__all__ = [
    "APP_STATE_KEY_LAST_KG_SYNC_STATS",
    "STATE_NEVER",
    "STATE_NO_DB",
    "STATE_RECORDED",
    "STATE_UNPARSEABLE",
    "describe",
    "format_age",
    "parse_stamp",
    "read_rows",
]
