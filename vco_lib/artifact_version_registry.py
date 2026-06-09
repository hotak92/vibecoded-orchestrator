# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reader + writer helpers for the artifact_schema_versions registry.

Sits between callers (install/update flows, V52-AF post-bundle steps, V52-O.2
collection-reset helper) and the SQLite ``artifact_schema_versions`` table
created by launcher.db migration 033.

The contract is intentionally narrow:

  - ``check_artifact_version(...)`` → returns ``ArtifactVersionStatus`` (an
    enum naming the action the caller should take).
  - ``register_artifact_version(...)`` → upsert a version row after a
    successful materialization/recreate.
  - ``unregister_artifact_version(...)`` → delete a row (used by V52-O.2's
    pre-drop step; FK cascade handles project deletion automatically).
  - ``list_artifacts_for_project(...)`` → diagnostic / GUI surface.

Callers DO NOT compute the canonical version themselves — they read it from
``vco_lib.schema_versions.canonical_version(artifact_type)``. The registry
just compares stored-vs-canonical and tells the caller what to do.

State classification (derived vs user_curated) is honored automatically:
``check_artifact_version`` returns ``RECREATE_NEEDED`` for derived state and
``UPGRADE_IN_PLACE_NEEDED`` for user-curated state — the caller chooses the
helper accordingly.

See ``v0.2.52`` backlog ``§ V52-AG`` for the full 4-layer design.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

from . import schema_versions as sv

logger = logging.getLogger(__name__)


class ArtifactVersionStatus(Enum):
    """Action the caller should take after a version check.

    The status names what's NEEDED, not what happened. Callers do the
    actual drop+recreate / upgrade-in-place themselves.
    """

    #: No row for this artifact exists. The artifact was never registered
    #: (fresh install, or pre-V52-AG project being touched for the first
    #: time). Caller should materialize from scratch.
    NEVER_MATERIALIZED = "never_materialized"

    #: Stored version matches canonical. No action needed.
    UP_TO_DATE = "up_to_date"

    #: Stored version < canonical, and artifact is DERIVED. Caller should
    #: drop + recreate cleanly (no user-state lives here).
    RECREATE_NEEDED = "recreate_needed"

    #: Stored version < canonical, and artifact is USER_CURATED. Caller
    #: should run a forward-only upgrade-in-place migration.
    UPGRADE_IN_PLACE_NEEDED = "upgrade_in_place_needed"

    #: Stored version > canonical. Means the launcher.db was written by a
    #: newer orchestrator version than the one running now. Refuse to act
    #: — the caller surfaces a hard error so the user upgrades the
    #: orchestrator instead of mangling state.
    REFUSE_DOWNGRADE = "refuse_downgrade"


@dataclass(frozen=True)
class ArtifactVersionRow:
    """One row of the registry."""

    project_id: Optional[str]  # NULL = orchestrator-wide
    artifact_type: str
    artifact_name: str
    schema_version: int
    materialized_at: int


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


@contextmanager
def _conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with FK enforcement enabled."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_artifact_version(
    db_path: Path,
    *,
    project_id: Optional[str],
    artifact_type: str,
    artifact_name: str,
) -> ArtifactVersionStatus:
    """Look up the artifact's stored version + compare against canonical.

    Returns one of the ``ArtifactVersionStatus`` values. Never raises on
    the version-comparison logic — caller bugs (unknown ``artifact_type``)
    surface as ``KeyError`` from ``sv.canonical_version``.

    On any SQLite error reading the row, returns ``NEVER_MATERIALIZED`` —
    conservative default: caller will recreate, which is idempotent for
    derived state and skipped-with-warning for user-curated state.
    """
    canonical = sv.canonical_version(artifact_type)
    derived = sv.is_derived(artifact_type)

    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "SELECT schema_version FROM artifact_schema_versions "
                "WHERE COALESCE(project_id, '') = COALESCE(?, '') "
                "  AND artifact_type = ? "
                "  AND artifact_name = ?",
                (project_id, artifact_type, artifact_name),
            )
            row = cur.fetchone()
    except sqlite3.Error as exc:
        logger.debug(
            "check_artifact_version: SQLite read failed (%s); treating as NEVER_MATERIALIZED",
            exc,
        )
        return ArtifactVersionStatus.NEVER_MATERIALIZED

    if row is None:
        return ArtifactVersionStatus.NEVER_MATERIALIZED

    stored = int(row[0])
    if stored == canonical:
        return ArtifactVersionStatus.UP_TO_DATE
    if stored > canonical:
        return ArtifactVersionStatus.REFUSE_DOWNGRADE
    # stored < canonical → recreate or upgrade-in-place per classification
    return (
        ArtifactVersionStatus.RECREATE_NEEDED
        if derived
        else ArtifactVersionStatus.UPGRADE_IN_PLACE_NEEDED
    )


def register_artifact_version(
    db_path: Path,
    *,
    project_id: Optional[str],
    artifact_type: str,
    artifact_name: str,
    schema_version: int,
    materialized_at: int,
) -> bool:
    """Upsert a row in ``artifact_schema_versions``.

    Caller invokes this AFTER a successful materialization or recreate
    of the artifact. Idempotent (PRIMARY KEY REPLACE on conflict).

    Returns True on success, False on SQLite error (logged at DEBUG).
    Telemetry/visibility issues should not crash the install flow.

    ``artifact_type`` MUST be a known constant from
    ``vco_lib.schema_versions.CANONICAL_VERSIONS`` — raises ``KeyError`` if
    not. ``schema_version`` MUST equal the canonical version for that type
    at the time of write (asserted) so the registry can never store an
    older version than what was just materialized.
    """
    canonical = sv.canonical_version(artifact_type)
    if schema_version != canonical:
        raise ValueError(
            f"register_artifact_version: schema_version={schema_version} "
            f"!= canonical_version({artifact_type!r})={canonical}. "
            f"Pass the canonical version constant, not a literal — "
            f"callers should write `sv.canonical_version({artifact_type!r})` "
            f"rather than hardcoding."
        )
    try:
        with _conn(db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO artifact_schema_versions "
                "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, artifact_type, artifact_name, schema_version, materialized_at),
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.debug(
            "register_artifact_version: SQLite write failed (%s); telemetry only",
            exc,
        )
        return False


def unregister_artifact_version(
    db_path: Path,
    *,
    project_id: Optional[str],
    artifact_type: str,
    artifact_name: str,
) -> bool:
    """Delete the row for an artifact that's about to be dropped (V52-O.2).

    Idempotent — deleting a nonexistent row is a no-op. Returns True on
    success or row-absent; False only on SQLite error.

    Note: project deletion cascades automatically via the FK ON DELETE
    CASCADE in migration 033, so callers don't need to call this when
    deleting a project — only when individually dropping a single
    collection (e.g. V52-O.2's reset of the 5 codegraph classes).
    """
    try:
        with _conn(db_path) as conn:
            conn.execute(
                "DELETE FROM artifact_schema_versions "
                "WHERE COALESCE(project_id, '') = COALESCE(?, '') "
                "  AND artifact_type = ? "
                "  AND artifact_name = ?",
                (project_id, artifact_type, artifact_name),
            )
            conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.debug(
            "unregister_artifact_version: SQLite delete failed (%s)",
            exc,
        )
        return False


def list_artifacts_for_project(
    db_path: Path,
    *,
    project_id: Optional[str],
) -> list[ArtifactVersionRow]:
    """Return every registered artifact for a project (or orchestrator-wide).

    Diagnostic + GUI use case: the launcher's per-project Settings page
    can render "Schema state: 12 artifacts up-to-date, 1 recreate needed
    (kg_collection v2 → v3)". Read-only; never touches the DB
    other than SELECT.

    Empty list on missing project, never raises.
    """
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "SELECT project_id, artifact_type, artifact_name, "
                "       schema_version, materialized_at "
                "FROM artifact_schema_versions "
                "WHERE COALESCE(project_id, '') = COALESCE(?, '') "
                "ORDER BY artifact_type, artifact_name",
                (project_id,),
            )
            return [
                ArtifactVersionRow(
                    project_id=r[0],
                    artifact_type=r[1],
                    artifact_name=r[2],
                    schema_version=int(r[3]),
                    materialized_at=int(r[4]),
                )
                for r in cur.fetchall()
            ]
    except sqlite3.Error as exc:
        logger.debug("list_artifacts_for_project: SQLite read failed (%s)", exc)
        return []


def stale_artifacts_for_project(
    db_path: Path,
    *,
    project_id: Optional[str],
) -> list[tuple[ArtifactVersionRow, ArtifactVersionStatus]]:
    """Find every registered artifact whose stored version != canonical.

    Returns ``(row, status)`` pairs for artifacts needing action. Used by
    V52-AF's apply_post_bundle_steps to decide what to recreate/upgrade
    on a per-project update.

    ``NEVER_MATERIALIZED`` artifacts don't appear here (they have no row).
    Callers needing that signal should iterate ``sv.all_artifact_types()``
    and call ``check_artifact_version`` per type.
    """
    rows = list_artifacts_for_project(db_path, project_id=project_id)
    stale: list[tuple[ArtifactVersionRow, ArtifactVersionStatus]] = []
    for row in rows:
        try:
            status = check_artifact_version(
                db_path,
                project_id=row.project_id,
                artifact_type=row.artifact_type,
                artifact_name=row.artifact_name,
            )
        except KeyError:
            # artifact_type no longer registered in schema_versions.py.
            # Surface as "unknown — caller decides". Skipping here so the
            # main consumers (V52-AF + V52-O.2) don't crash on the legacy
            # type; they can list_artifacts_for_project for visibility.
            logger.debug(
                "stale_artifacts_for_project: unknown artifact_type "
                "%r in registry; skipping",
                row.artifact_type,
            )
            continue
        if status != ArtifactVersionStatus.UP_TO_DATE:
            stale.append((row, status))
    return stale
