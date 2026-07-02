# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-AG schema-version tracking — unit + integration tests.

Covers:

1. ``vco_lib/schema_versions.py`` constants module (CANONICAL_VERSIONS +
   ARTIFACT_STATE_CLASSIFICATION + canonical_version() + is_derived()).
2. ``vco_lib/schema_versions.json`` ↔ Python parity (the JSON included by
   Rust at compile time MUST match what Python would produce — drift
   means Rust is using stale constants).
3. ``scripts/regen_schema_versions_json.py`` regen + --check semantics.
4. Migration 033's ``artifact_schema_versions`` table shape (column set,
   PRIMARY KEY, FK cascade behavior).
5. Existing ``_MANIFEST_SCHEMA_VERSION`` in project_init.py agrees with
   the new ``BUNDLE_MATERIALIZATION_SCHEMA_VERSION`` constant.

See v0.2.52 backlog § V52-AG.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import schema_versions as sv  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1 — Constants module shape
# ---------------------------------------------------------------------------


def test_canonical_versions_dict_populated() -> None:
    """The CANONICAL_VERSIONS dict must contain every artifact_type the
    backlog enumerates. Missing entries break the version-check helpers
    silently."""
    expected = {
        # Layer 1 — Weaviate collections
        "kg_collection",
        "shared_kg_collection",
        "diagrams_collection",
        "development_collection",
        "codegraph_collection",
        # Layer 2 — KG content
        "kg_node_frontmatter",
        "kg_node_formats",  # v0.2.57: .node_formats.json regen-cache (derived)
        # Layer 3 — Bundle
        "bundle_materialization",
        # Layer 4 — launcher.db row content
        "project_kg_bindings_shape",
        "project_codegraph_bindings_shape",
        "module_installs_shape",
        "module_settings_shape",
        "codegraph_access_vocabulary",
        "kg_collection_access_vocabulary",
        "code_graph_builds_status_vocabulary",
        "project_bootstrap_version",
        # Layer 5 — Orchestrator-wide
        "rl_events_payload_shape",
        "launcher_db_table_set",
    }
    assert set(sv.CANONICAL_VERSIONS) == expected, (
        "CANONICAL_VERSIONS keys diverged from the V52-AG backlog spec. "
        "If you intentionally added/removed an artifact_type, update both "
        "this test AND v0.2.52-backlog.md § V52-AG."
    )


def test_state_classification_covers_every_artifact_type() -> None:
    """Every artifact_type in CANONICAL_VERSIONS must have a matching
    entry in ARTIFACT_STATE_CLASSIFICATION. Otherwise the version-check
    helper can't decide between drop+recreate vs upgrade-in-place."""
    assert set(sv.ARTIFACT_STATE_CLASSIFICATION) == set(sv.CANONICAL_VERSIONS)


def test_state_classification_values_are_valid() -> None:
    """The only valid classifications are 'derived' or 'user_curated'.
    Anything else means the helper can't handle the mismatch."""
    valid = {"derived", "user_curated"}
    for k, v in sv.ARTIFACT_STATE_CLASSIFICATION.items():
        assert v in valid, (
            f"ARTIFACT_STATE_CLASSIFICATION[{k!r}] = {v!r} — must be "
            f"'derived' (drop+recreate) or 'user_curated' (upgrade in place)."
        )


def test_canonical_version_helper() -> None:
    assert sv.canonical_version("kg_collection") == 3
    # v0.2.72 P3/P7: codegraph classes gained chunk_num/total_chunks/embed_revision
    # → bumped 4→5 with the preserving migrations/codegraph_collection/4_to_5.py edge.
    assert sv.canonical_version("codegraph_collection") == 5
    assert sv.canonical_version("rl_events_payload_shape") == 3
    with pytest.raises(KeyError):
        sv.canonical_version("not_a_real_artifact_type")


def test_is_derived_helper() -> None:
    assert sv.is_derived("kg_collection") is True
    assert sv.is_derived("codegraph_collection") is True
    assert sv.is_derived("bundle_materialization") is True
    # User-curated
    assert sv.is_derived("kg_node_frontmatter") is False
    assert sv.is_derived("module_settings_shape") is False
    assert sv.is_derived("codegraph_access_vocabulary") is False
    with pytest.raises(KeyError):
        sv.is_derived("not_a_real_artifact_type")


def test_all_artifact_types_is_sorted() -> None:
    types = sv.all_artifact_types()
    assert list(types) == sorted(types)


# ---------------------------------------------------------------------------
# Test 2 — Python ↔ JSON parity (the Rust-side payload)
# ---------------------------------------------------------------------------


def test_schema_versions_json_matches_python() -> None:
    """vco_lib/schema_versions.json must be regenerable from the Python
    module without changes. If this fails, run
    ``scripts/regen_schema_versions_json.py``."""
    json_path = _REPO / "vco_lib" / "schema_versions.json"
    assert json_path.exists(), (
        "vco_lib/schema_versions.json missing — generate it via "
        "scripts/regen_schema_versions_json.py"
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["canonical_versions"] == dict(
        sorted(sv.CANONICAL_VERSIONS.items())
    ), (
        "vco_lib/schema_versions.json canonical_versions is OUT OF DATE "
        "relative to Python constants. Run regen_schema_versions_json.py."
    )
    assert payload["state_classification"] == dict(
        sorted(sv.ARTIFACT_STATE_CLASSIFICATION.items())
    ), (
        "vco_lib/schema_versions.json state_classification is OUT OF DATE. "
        "Run regen_schema_versions_json.py."
    )


def test_regen_script_check_mode_succeeds_on_clean_state() -> None:
    """The regen script's --check mode must agree with the committed JSON.
    If this fails AFTER bumping a constant, regen + commit the JSON."""
    script = _REPO / "scripts" / "regen_schema_versions_json.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"regen_schema_versions_json.py --check failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Bundle-version parity (the only existing schema-version constant)
# ---------------------------------------------------------------------------


def test_bundle_materialization_version_matches_project_init() -> None:
    """The existing ``_MANIFEST_SCHEMA_VERSION`` in vco_lib/project_init.py
    must agree with the new ``BUNDLE_MATERIALIZATION_SCHEMA_VERSION``.

    If they ever drift, the registry will lie about bundle state — and the
    install/update flow's recreate-or-upgrade decision will fire incorrectly.
    """
    from vco_lib import project_init
    assert (
        project_init._MANIFEST_SCHEMA_VERSION
        == sv.BUNDLE_MATERIALIZATION_SCHEMA_VERSION
    ), (
        f"_MANIFEST_SCHEMA_VERSION ({project_init._MANIFEST_SCHEMA_VERSION}) "
        f"!= BUNDLE_MATERIALIZATION_SCHEMA_VERSION "
        f"({sv.BUNDLE_MATERIALIZATION_SCHEMA_VERSION}). Bump both together."
    )


# ---------------------------------------------------------------------------
# Test 4 — Migration 033 table shape (smoke test against an in-memory DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db_with_migrations(tmp_path):
    """Apply migrations 1..33 against a fresh sqlite DB.

    Approximates what the Rust migration runner does (sequentially executes
    each migration's SQL). We don't use the Rust runner here because this
    test is Python-side; a separate Rust integration test covers the runner.
    """
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
        try:
            conn.executescript(f.read_text(encoding="utf-8"))
        except sqlite3.Error as exc:
            pytest.fail(f"Migration {f.name} failed: {exc}")
    conn.commit()
    yield conn
    conn.close()


def test_migration_033_creates_artifact_schema_versions_table(
    fresh_db_with_migrations,
) -> None:
    cur = fresh_db_with_migrations.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='artifact_schema_versions'"
    )
    assert cur.fetchone() is not None, (
        "Migration 033 did not create artifact_schema_versions table. "
        "Check launcher/src-tauri/vct-launcher-core/src/db/migrations/"
        "033_artifact_schema_versions.sql."
    )


def test_migration_033_column_set(fresh_db_with_migrations) -> None:
    cur = fresh_db_with_migrations.execute(
        "PRAGMA table_info(artifact_schema_versions)"
    )
    cols = {r[1] for r in cur.fetchall()}
    assert cols == {
        "project_id",
        "artifact_type",
        "artifact_name",
        "schema_version",
        "materialized_at",
    }, f"unexpected column set: {cols}"


def test_migration_033_primary_key_shape(fresh_db_with_migrations) -> None:
    cur = fresh_db_with_migrations.execute(
        "PRAGMA table_info(artifact_schema_versions)"
    )
    pk_cols = [r[1] for r in cur.fetchall() if r[5] > 0]
    assert pk_cols == ["project_id", "artifact_type", "artifact_name"], (
        f"PRIMARY KEY shape diverged: {pk_cols}. The (project_id, "
        f"artifact_type, artifact_name) composite key is load-bearing for "
        f"the install/update upsert path."
    )


def test_migration_033_indexes_exist(fresh_db_with_migrations) -> None:
    cur = fresh_db_with_migrations.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='artifact_schema_versions'"
    )
    names = {r[0] for r in cur.fetchall()}
    assert "idx_artifact_schema_versions_type" in names
    assert "idx_artifact_schema_versions_project" in names


def test_migration_033_null_project_id_supported(
    fresh_db_with_migrations,
) -> None:
    """NULL project_id = orchestrator-wide artifacts (rl_events shape,
    launcher_db_table_set version). PRIMARY KEY must allow NULL."""
    conn = fresh_db_with_migrations
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES (NULL, 'rl_events_payload_shape', '*', 3, 1234567890)"
    )
    conn.commit()
    cur = conn.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE project_id IS NULL AND artifact_type='rl_events_payload_shape'"
    )
    row = cur.fetchone()
    assert row is not None and row[0] == 3


def test_migration_033_cascade_on_project_delete(
    fresh_db_with_migrations,
) -> None:
    """When a project is deleted, its artifact_schema_versions rows
    must vanish (FK ON DELETE CASCADE)."""
    conn = fresh_db_with_migrations
    conn.execute("PRAGMA foreign_keys = ON")
    # Insert a project
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at, rl_port) "
        "VALUES ('p1', 'test', '/tmp/p1', 'base', 'p1', 1, 1, NULL)"
    )
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES ('p1', 'kg_collection', 'P1_KnowledgeGraph', 3, 1234567890)"
    )
    conn.commit()

    cur = conn.execute(
        "SELECT COUNT(*) FROM artifact_schema_versions WHERE project_id='p1'"
    )
    assert cur.fetchone()[0] == 1

    # Delete the project — cascade must remove the version row.
    conn.execute("DELETE FROM projects WHERE id='p1'")
    conn.commit()

    cur = conn.execute(
        "SELECT COUNT(*) FROM artifact_schema_versions WHERE project_id='p1'"
    )
    assert cur.fetchone()[0] == 0, (
        "FK ON DELETE CASCADE did not fire — stale version rows would "
        "accumulate on every project deletion."
    )


# ---------------------------------------------------------------------------
# Test 5 — launcher_db_table_set version matches actual MIGRATIONS list
# ---------------------------------------------------------------------------


def test_launcher_db_table_set_version_matches_migration_count() -> None:
    """``LAUNCHER_DB_TABLE_SET_VERSION`` MUST equal the highest migration
    version registered in migrations.rs. If we add a new migration, this
    test reminds us to bump the constant."""
    migrations_rs = (
        _REPO
        / "launcher"
        / "src-tauri"
        / "vct-launcher-core"
        / "src"
        / "db"
        / "migrations.rs"
    )
    src = migrations_rs.read_text(encoding="utf-8")
    # Find every `version: N,` literal in the MIGRATIONS array.
    import re
    versions = [int(m.group(1)) for m in re.finditer(r"version:\s*(\d+),", src)]
    # The MIGRATIONS array is the first chunk; defensively cap at the first
    # `];` so the test doesn't pick up versions in test fixtures further
    # down the file.
    end = src.find("];")
    src_head = src[:end] if end > 0 else src
    versions_in_array = [
        int(m.group(1)) for m in re.finditer(r"version:\s*(\d+),", src_head)
    ]
    assert versions_in_array, "no Migration entries found in MIGRATIONS array"
    max_version = max(versions_in_array)
    assert sv.LAUNCHER_DB_TABLE_SET_VERSION == max_version, (
        f"LAUNCHER_DB_TABLE_SET_VERSION = {sv.LAUNCHER_DB_TABLE_SET_VERSION} "
        f"but the last migration in migrations.rs is version {max_version}. "
        f"Bump LAUNCHER_DB_TABLE_SET_VERSION in vco_lib/schema_versions.py "
        f"and regen the JSON."
    )
