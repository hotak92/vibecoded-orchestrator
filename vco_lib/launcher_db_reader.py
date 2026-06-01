# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Read-only launcher.db helper for install.py (v0.2.44).

Discovers ``~/.vct/launcher.db`` (env override: ``VCT_LAUNCHER_DB_PATH``).
Soft-fails on any error (returns ``None`` or empty). Never raises.

Used by ``install.py`` to resolve canonical KG collection names from
``project_kg_bindings`` (the source-of-truth) instead of from env vars
(a projection). This closes the architectural-debt phase 1 of the
v0.2.43 V0243-0 recurring bug — env-derived collection names drift
from the launcher DB silently, the DB binding is authoritative.

Surface
~~~~~~~

* :func:`get_orchestrator_root_project_id` — lookup the
  ``host='orchestrator_root'`` row in ``projects``.
* :func:`get_kg_binding` — lookup ``collection_name`` from
  ``project_kg_bindings`` for a given ``(project_id, role)`` pair where
  ``role`` is ``"primary"`` or ``"shared"``.
* :func:`get_orchestrator_root_bindings` — convenience: return both
  primary + shared bindings for orchestrator-root in one call.

Soft-fail semantics
~~~~~~~~~~~~~~~~~~~

All public functions return ``None`` (or ``(None, None)`` for the tuple
helper) when the DB is unavailable, malformed, the row is missing, or
SQLite raises. They never propagate exceptions. This matches the rest
of ``install.py``'s "Weaviate-down / launcher-down must not block the
install" discipline (see CHANGELOG v0.2.41 + v0.2.42 hardening notes).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple


def _discover_db_path() -> Optional[Path]:
    """Return ``launcher.db`` path or ``None`` if not found.

    Resolution priority:
      1. ``VCT_LAUNCHER_DB_PATH`` env override (must point at an existing file).
      2. :func:`vco_lib.paths.launcher_db_path` — honours ``VCT_STATE_DIR``
         env override and falls back to ``~/.vct/launcher.db``.

    Returns ``None`` when neither yields an existing regular file.

    v0.2.44 V44-E: delegates the default-path branch to
    :func:`vco_lib.paths.launcher_db_path` so the single source-of-truth
    helper in ``vco_lib.paths`` owns cross-OS path resolution + the
    ``VCT_STATE_DIR`` override (the previous inline form bypassed the
    env override silently). Satisfies
    ``tests/test_vct_root_dir_consolidation::test_no_inline_reconstructions_outside_paths_module``.
    """
    override = os.environ.get("VCT_LAUNCHER_DB_PATH", "").strip()
    if override:
        p = Path(override)
        return p if p.is_file() else None
    try:
        from vco_lib.paths import launcher_db_path
        default = launcher_db_path()
        return default if default.is_file() else None
    except Exception:
        return None


def _open_db_readonly() -> Optional[sqlite3.Connection]:
    """Open ``launcher.db`` in read-only mode, or ``None`` on any failure.

    Uses SQLite's ``file:...?mode=ro`` URI form so concurrent launcher
    writes are never blocked by us. ``timeout=5.0`` guards against a
    locked-DB hang on Windows. Row factory is set to :class:`sqlite3.Row`
    so callers can use column-name access.
    """
    p = _discover_db_path()
    if p is None:
        return None
    try:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def get_orchestrator_root_project_id() -> Optional[str]:
    """Return ``project_id`` of the orchestrator-root project.

    Looks up the row in ``projects`` whose ``host`` column equals
    ``'orchestrator_root'``. Returns ``None`` when the DB is unavailable,
    the table/column is missing, or no such row exists.
    """
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM projects WHERE host = 'orchestrator_root' LIMIT 1"
        ).fetchone()
        return row["id"] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_kg_binding(project_id: str, role: str) -> Optional[str]:
    """Return ``collection_name`` from ``project_kg_bindings`` for ``(project_id, role)``.

    :param project_id: the ``projects.id`` value to look up.
    :param role: must be ``"primary"`` or ``"shared"`` — any other value
        returns ``None`` without touching the DB (defensive: prevents
        a typo from silently matching some other role string).

    Returns ``None`` when DB unavailable / row missing / SQLite errors.
    """
    if role not in ("primary", "shared"):
        return None
    conn = _open_db_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT collection_name FROM project_kg_bindings "
            "WHERE project_id = ? AND role = ? LIMIT 1",
            (project_id, role),
        ).fetchone()
        return row["collection_name"] if row else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_orchestrator_root_bindings() -> Tuple[Optional[str], Optional[str]]:
    """Return ``(primary_collection_name, shared_collection_name)`` for orchestrator-root.

    Convenience wrapper that combines :func:`get_orchestrator_root_project_id`
    with two :func:`get_kg_binding` calls. Either or both elements of the
    returned tuple can be ``None`` (DB unavailable, project row missing,
    or a binding row missing).
    """
    pid = get_orchestrator_root_project_id()
    if pid is None:
        return (None, None)
    return (get_kg_binding(pid, "primary"), get_kg_binding(pid, "shared"))
