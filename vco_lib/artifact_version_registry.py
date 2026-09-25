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
  - ``record_bundle_materialization(...)`` → the bundle's own write, read from
    the manifest it just wrote (CLI verb ``record-bundle-materialization``,
    spawned by the launcher's post-bundle pipeline; called in-process by
    install.py for the root).

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

import argparse
import json
import logging
import sqlite3
import sys
import time
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
            # NULL-safe upsert. `INSERT OR REPLACE` resolves conflicts via the
            # PRIMARY KEY (project_id, artifact_type, artifact_name) — but in
            # SQLite NULL is DISTINCT from NULL for uniqueness, so an
            # orchestrator-wide row (project_id IS NULL) never "conflicts" and
            # OR REPLACE would APPEND a duplicate instead of replacing. The
            # read side (`check_artifact_version`) matches on
            # COALESCE(project_id,'') — so a stale duplicate could shadow the
            # fresh row. DELETE-then-INSERT with the same COALESCE match makes
            # the upsert correct for both NULL and non-NULL project_id.
            conn.execute(
                "DELETE FROM artifact_schema_versions "
                "WHERE COALESCE(project_id, '') = COALESCE(?, '') "
                "  AND artifact_type = ? AND artifact_name = ?",
                (project_id, artifact_type, artifact_name),
            )
            conn.execute(
                "INSERT INTO artifact_schema_versions "
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

    Returns ``(row, status)`` pairs for artifacts needing action — a
    diagnostic view. The recreate/upgrade DECISION on a per-project update
    was planned here (V52-AF) and is made by
    ``schema_migration_runner.run_schema_migrations`` instead (v0.2.60),
    which the post-bundle pipeline runs through ``project_init
    migrate-schema`` and which also sees ``NEVER_MATERIALIZED``.

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
            # Surface as "unknown — caller decides". Skipping here so a
            # caller does not crash on the legacy type; it can
            # list_artifacts_for_project for visibility.
            logger.debug(
                "stale_artifacts_for_project: unknown artifact_type "
                "%r in registry; skipping",
                row.artifact_type,
            )
            continue
        if status != ArtifactVersionStatus.UP_TO_DATE:
            stale.append((row, status))
    return stale


# ---------------------------------------------------------------------------
# bundle_materialization — the bundle's own registry write (V52-AG layer 3)
# ---------------------------------------------------------------------------

#: The row name of every artifact that is not a named Weaviate class.
#: ``schema_migration_runner._resolve_artifact_names`` resolves THIS constant,
#: so the row written here is the row the runner reads.
DEFAULT_ARTIFACT_NAME = "default"

BUNDLE_MATERIALIZATION = "bundle_materialization"


def _not_recorded(code: str, error: str, **extra: object) -> dict:
    return {"ok": False, "code": code, "error": error, **extra}


def record_bundle_materialization(
    db_path: Path,
    *,
    project_id: str,
    folder: Path,
    now_ms: Optional[int] = None,
) -> dict:
    """Record the bundle schema version ``folder`` now holds.

    Evidence, not intent: the version is the ``schema_version`` the bundle
    engine wrote into ``<folder>/.claude/.vco-manifest.json`` when it finished,
    recorded only when it equals the canonical ``bundle_materialization``
    version. Both callers run this straight after a bundle install/update and
    BEFORE the schema-migration runner, so the runner (the registry's reader)
    sees ``UP_TO_DATE`` for a bundle that was just re-materialized, and a row
    still behind canonical means the bundle did not reach the current version
    — the drift the registry exists to show.

    Never raises. ``{"ok": True, "action": "registered", ...}`` on a write;
    otherwise ``{"ok": False, "code": ..., "error": <sentence>}`` and the
    registry is untouched. A missing launcher.db is reported, never created
    (``sqlite3.connect`` would create an empty file).
    """
    from vco_lib.manifest_paths import manifest_path

    canonical = sv.canonical_version(BUNDLE_MATERIALIZATION)
    manifest = manifest_path(folder)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _not_recorded("no_manifest", f"{manifest} does not exist: no bundle "
                             "was materialized in this folder")
    except (OSError, ValueError) as exc:
        return _not_recorded("manifest_unreadable", f"{manifest} could not be read "
                             f"({type(exc).__name__}: {exc})")
    applied = data.get("schema_version") if isinstance(data, dict) else None
    if not isinstance(applied, int) or isinstance(applied, bool):
        return _not_recorded("manifest_unreadable",
                             f"{manifest} carries no integer `schema_version`")
    if applied != canonical:
        return _not_recorded(
            "manifest_version_mismatch",
            f"{manifest} records bundle schema v{applied}, but this orchestrator "
            f"materializes v{canonical}: the bundle update did not rewrite it",
            schema_version=applied, canonical=canonical)
    if not Path(db_path).is_file():
        return _not_recorded("no_launcher_db", f"{db_path} does not exist")
    ok = register_artifact_version(
        Path(db_path),
        project_id=project_id,
        artifact_type=BUNDLE_MATERIALIZATION,
        artifact_name=DEFAULT_ARTIFACT_NAME,
        schema_version=canonical,
        materialized_at=int(now_ms if now_ms is not None else time.time() * 1000),
    )
    if not ok:
        return _not_recorded(
            "registry_write_failed",
            f"the artifact_schema_versions write to {db_path} failed (locked, "
            f"read-only, no such table, or project {project_id!r} not registered)",
            schema_version=applied, canonical=canonical)
    return {"ok": True, "action": "registered", "project_id": project_id,
            "artifact_type": BUNDLE_MATERIALIZATION, "schema_version": applied}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.artifact_version_registry",
        description="artifact_schema_versions registry writes. Machine "
        "interface: one JSON object on stdout; exit 0 only when recorded.",
    )
    sub = parser.add_subparsers(dest="op", required=True)
    rec = sub.add_parser(
        "record-bundle-materialization",
        help="Record the bundle schema version a project folder now holds.",
    )
    rec.add_argument("--folder", required=True)
    rec.add_argument("--project-id", required=True)
    rec.add_argument("--db", help="launcher.db (default: the resolved one)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from vco_lib.paths import launcher_db_path

        db_path = launcher_db_path()
    result = record_bundle_materialization(
        db_path, project_id=args.project_id, folder=Path(args.folder))
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
