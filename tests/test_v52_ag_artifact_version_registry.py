# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AG layer 2 — reader/writer helpers for artifact_schema_versions.

Covers:

1. ``check_artifact_version`` returns the right status for each
   (stored, canonical, derived/user_curated) combination.
2. ``register_artifact_version`` upserts correctly + enforces the
   "schema_version must equal canonical" contract.
3. ``unregister_artifact_version`` idempotent delete.
4. ``list_artifacts_for_project`` returns the right rows.
5. ``stale_artifacts_for_project`` excludes UP_TO_DATE + skips unknown
   artifact_types defensively.
6. NULL project_id is supported (orchestrator-wide artifacts).
7. FK ON DELETE CASCADE wired correctly when a project is deleted.

See v0.2.52 backlog § V52-AG.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import schema_versions as sv  # noqa: E402
from vco_lib.artifact_version_registry import (  # noqa: E402
    ArtifactVersionRow,
    ArtifactVersionStatus,
    check_artifact_version,
    list_artifacts_for_project,
    register_artifact_version,
    stale_artifacts_for_project,
    unregister_artifact_version,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_v033(tmp_path):
    """Apply migrations 1..33 against a fresh sqlite DB."""
    db_path = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE _schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  description TEXT NOT NULL,"
        "  applied_at INTEGER NOT NULL"
        ")"
    )
    migrations_dir = (
        _REPO / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db" / "migrations"
    )
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    for f in files:
        conn.executescript(f.read_text(encoding="utf-8"))
    # Register a project so FK references work.
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at, rl_port) "
        "VALUES ('p1', 'test', '/tmp/p1', 'base', 'p1', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test 1 — check_artifact_version status logic
# ---------------------------------------------------------------------------


def test_check_never_materialized_returns_status(db_with_v033) -> None:
    """No row in the registry → NEVER_MATERIALIZED."""
    status = check_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
    )
    assert status == ArtifactVersionStatus.NEVER_MATERIALIZED


def test_check_up_to_date(db_with_v033) -> None:
    """Stored == canonical → UP_TO_DATE."""
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
        schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1234567890,
    )
    status = check_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
    )
    assert status == ArtifactVersionStatus.UP_TO_DATE


def test_check_recreate_needed_for_derived(db_with_v033) -> None:
    """Stored < canonical AND artifact is derived → RECREATE_NEEDED.

    We can't legally write an old version via register_artifact_version
    (it enforces canonical), so we insert directly with raw SQL to
    simulate a pre-bump state.
    """
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'kg_collection', 'P1_KnowledgeGraph', 1, 1234567890)"
    )
    conn.commit()
    conn.close()

    # kg_collection canonical is 3; stored is 1; classification is derived.
    assert sv.canonical_version("kg_collection") == 3
    assert sv.is_derived("kg_collection") is True

    status = check_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
    )
    assert status == ArtifactVersionStatus.RECREATE_NEEDED


def test_check_upgrade_in_place_needed_for_user_curated(db_with_v033) -> None:
    """Stored < canonical AND artifact is user_curated → UPGRADE_IN_PLACE_NEEDED."""
    # KG node frontmatter is user_curated, canonical v1. We simulate a
    # hypothetical v0 stored row (not a real path; just enforce the
    # branching logic).
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'kg_node_frontmatter', '*', 0, 1234567890)"
    )
    conn.commit()
    conn.close()

    assert sv.is_derived("kg_node_frontmatter") is False

    status = check_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_node_frontmatter",
        artifact_name="*",
    )
    assert status == ArtifactVersionStatus.UPGRADE_IN_PLACE_NEEDED


def test_check_refuse_downgrade(db_with_v033) -> None:
    """Stored > canonical → REFUSE_DOWNGRADE (user downgraded
    orchestrator while running on newer DB; refuse to mangle state)."""
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'kg_collection', 'P1_KnowledgeGraph', 99, 1234567890)"
    )
    conn.commit()
    conn.close()

    status = check_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
    )
    assert status == ArtifactVersionStatus.REFUSE_DOWNGRADE


def test_check_null_project_id_orchestrator_wide(db_with_v033) -> None:
    """NULL project_id = orchestrator-wide artifact (rl_events shape,
    launcher_db_table_set version)."""
    register_artifact_version(
        db_with_v033,
        project_id=None,
        artifact_type="rl_events_payload_shape",
        artifact_name="*",
        schema_version=sv.canonical_version("rl_events_payload_shape"),
        materialized_at=1234567890,
    )
    status = check_artifact_version(
        db_with_v033,
        project_id=None,
        artifact_type="rl_events_payload_shape",
        artifact_name="*",
    )
    assert status == ArtifactVersionStatus.UP_TO_DATE


# ---------------------------------------------------------------------------
# Test 2 — register_artifact_version contract
# ---------------------------------------------------------------------------


def test_register_returns_true_on_success(db_with_v033) -> None:
    ok = register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="P1_CodeFunction",
        schema_version=sv.canonical_version("codegraph_collection"),
        materialized_at=1234567890,
    )
    assert ok is True


def test_register_idempotent_upsert(db_with_v033) -> None:
    """Calling register twice for the same key must overwrite, not duplicate."""
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="P1_CodeFunction",
        schema_version=sv.canonical_version("codegraph_collection"),
        materialized_at=1111111111,
    )
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="P1_CodeFunction",
        schema_version=sv.canonical_version("codegraph_collection"),
        materialized_at=2222222222,
    )
    rows = list_artifacts_for_project(db_with_v033, project_id="p1")
    matches = [r for r in rows if r.artifact_type == "codegraph_collection"]
    assert len(matches) == 1
    assert matches[0].materialized_at == 2222222222


def test_register_rejects_non_canonical_version(db_with_v033) -> None:
    """register_artifact_version refuses to write a non-canonical version
    so callers can't accidentally store a stale value after a successful
    materialization."""
    with pytest.raises(ValueError, match="canonical_version"):
        register_artifact_version(
            db_with_v033,
            project_id="p1",
            artifact_type="kg_collection",
            artifact_name="P1_KnowledgeGraph",
            schema_version=1,  # not the canonical 3
            materialized_at=1234567890,
        )


def test_register_raises_keyerror_for_unknown_artifact_type(db_with_v033) -> None:
    """Unknown artifact_type → KeyError from canonical_version. Surfaces
    the caller bug rather than silently no-op'ing."""
    with pytest.raises(KeyError):
        register_artifact_version(
            db_with_v033,
            project_id="p1",
            artifact_type="not_a_real_type",
            artifact_name="x",
            schema_version=1,
            materialized_at=1234567890,
        )


# ---------------------------------------------------------------------------
# Test 3 — unregister_artifact_version
# ---------------------------------------------------------------------------


def test_unregister_removes_row(db_with_v033) -> None:
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="P1_CodeFunction",
        schema_version=sv.canonical_version("codegraph_collection"),
        materialized_at=1234567890,
    )
    assert (
        check_artifact_version(
            db_with_v033,
            project_id="p1",
            artifact_type="codegraph_collection",
            artifact_name="P1_CodeFunction",
        )
        == ArtifactVersionStatus.UP_TO_DATE
    )
    unregister_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="P1_CodeFunction",
    )
    assert (
        check_artifact_version(
            db_with_v033,
            project_id="p1",
            artifact_type="codegraph_collection",
            artifact_name="P1_CodeFunction",
        )
        == ArtifactVersionStatus.NEVER_MATERIALIZED
    )


def test_unregister_idempotent_on_missing_row(db_with_v033) -> None:
    """Deleting a row that doesn't exist is a no-op."""
    ok = unregister_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="codegraph_collection",
        artifact_name="never-existed",
    )
    assert ok is True


# ---------------------------------------------------------------------------
# Test 4 — list_artifacts_for_project
# ---------------------------------------------------------------------------


def test_list_returns_only_matching_project(db_with_v033) -> None:
    """Make a second project to confirm scoping."""
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at, rl_port) "
        "VALUES ('p2', 'other', '/tmp/p2', 'base', 'p2', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()

    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KG",
        schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1111,
    )
    register_artifact_version(
        db_with_v033,
        project_id="p2",
        artifact_type="kg_collection",
        artifact_name="P2_KG",
        schema_version=sv.canonical_version("kg_collection"),
        materialized_at=2222,
    )

    p1_rows = list_artifacts_for_project(db_with_v033, project_id="p1")
    p2_rows = list_artifacts_for_project(db_with_v033, project_id="p2")
    assert len(p1_rows) == 1 and p1_rows[0].project_id == "p1"
    assert len(p2_rows) == 1 and p2_rows[0].project_id == "p2"


def test_list_returns_empty_for_unknown_project(db_with_v033) -> None:
    rows = list_artifacts_for_project(db_with_v033, project_id="no-such-id")
    assert rows == []


def test_list_orchestrator_wide_rows_with_null_project_id(db_with_v033) -> None:
    """NULL project_id returns the orchestrator-wide rows."""
    register_artifact_version(
        db_with_v033,
        project_id=None,
        artifact_type="rl_events_payload_shape",
        artifact_name="*",
        schema_version=sv.canonical_version("rl_events_payload_shape"),
        materialized_at=3333,
    )
    rows = list_artifacts_for_project(db_with_v033, project_id=None)
    assert len(rows) == 1
    assert rows[0].project_id is None
    assert rows[0].artifact_type == "rl_events_payload_shape"


# ---------------------------------------------------------------------------
# Test 5 — stale_artifacts_for_project filtering
# ---------------------------------------------------------------------------


def test_stale_excludes_up_to_date_rows(db_with_v033) -> None:
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KG",
        schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1111,
    )
    stale = stale_artifacts_for_project(db_with_v033, project_id="p1")
    assert stale == []


def test_stale_returns_recreate_for_derived(db_with_v033) -> None:
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'kg_collection', 'P1_KG', 1, 1234567890)"
    )
    conn.commit()
    conn.close()

    stale = stale_artifacts_for_project(db_with_v033, project_id="p1")
    assert len(stale) == 1
    row, status = stale[0]
    assert row.artifact_type == "kg_collection"
    assert status == ArtifactVersionStatus.RECREATE_NEEDED


def test_stale_skips_unknown_artifact_types(db_with_v033) -> None:
    """A legacy artifact_type in the registry that's no longer in
    schema_versions.py should NOT crash stale_artifacts_for_project."""
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'legacy_dropped_type', '*', 1, 1234567890)"
    )
    conn.commit()
    conn.close()

    # Must not raise; should return empty (no canonical version for this type).
    stale = stale_artifacts_for_project(db_with_v033, project_id="p1")
    assert stale == []


# ---------------------------------------------------------------------------
# Test 6 — FK cascade integration
# ---------------------------------------------------------------------------


def test_project_delete_cascades_via_registry(db_with_v033) -> None:
    """Deleting a project removes its version rows (FK ON DELETE CASCADE).
    This is exercised through the public registry API to confirm the API
    surface sees the cascade correctly."""
    register_artifact_version(
        db_with_v033,
        project_id="p1",
        artifact_type="kg_collection",
        artifact_name="P1_KG",
        schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1234567890,
    )
    assert len(list_artifacts_for_project(db_with_v033, project_id="p1")) == 1

    conn = sqlite3.connect(str(db_with_v033))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM projects WHERE id='p1'")
    conn.commit()
    conn.close()

    assert list_artifacts_for_project(db_with_v033, project_id="p1") == []
