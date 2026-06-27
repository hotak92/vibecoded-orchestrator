# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.60 version-gated schema-migration runner.

Covers the SPEC §7 test list (Piece 2 scope) PLUS the audit-driven rework
(2026-06-16): per-project vs orchestrator-wide keying, codegraph multi-class
resolution, atomic .sql edges (C1), register-failed accounting (C4).

  T3  layout discovery + malformed-name skip + OS dispatch
  T4  contiguity / abort-on-gap
  T5  RECREATE on a real (monkeypatched) canonical bump, derived
  T6  abort-without-half-migrate + retry (R3)
  T7  NO-OP today: empty migrations/ + all NEVER_MATERIALIZED → all
      registered at canonical, apply_edge spy count == 0
  T8  NO-OP up-to-date: all pre-registered at canonical + live-drift stub
      "no drift" → apply count == 0, zero registry writes, zero deferrals
  T9  REFUSE_DOWNGRADE
  T10 POLICY STEP 3 — stale + no preserving script → pending_regenerate, no drop
  T-preserving  POLICY STEP 2 wins over STEP 3
  T11 classification cross-check abort (R2)
  T12 --check dry-run produces a plan with zero mutation
  + per-project scoping: orchestrator-wide skipped on non-root update;
    keyed NULL on root update
  + codegraph: 5 classes resolved; UP_TO_DATE no-fire; stale-probe drives policy
  + C1: atomic .sql edge — stmt-2 failure rolls back stmt-1
  + C4: register failure → register_failed, not registered
  + CONCERN-1: the per-project CLI apply WRITES the schema_regenerate_or_defer
    entry to UPDATE_DEFERRED.md (durable), not just JSON

The ``live_drift_probe`` injection seam lets every test run with NO live
Weaviate — it is stubbed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import project_init as pinit  # noqa: E402
from vco_lib import schema_migration_runner as smr  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_v033(tmp_path):
    """Apply migrations 1..33 against a fresh sqlite DB + register a project."""
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
        _REPO
        / "launcher"
        / "src-tauri"
        / "vct-launcher-core"
        / "src"
        / "db"
        / "migrations"
    )
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    for f in files:
        conn.executescript(f.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, "
        "created_at, updated_at, rl_port) "
        "VALUES ('p1', 'test', '/tmp/p1', 'base', 'p1', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def empty_migrations(tmp_path):
    """An empty migrations/ directory (the shipped v0.2.60 state)."""
    d = tmp_path / "migrations"
    d.mkdir()
    return d


def _no_drift(weaviate_url, artifact_name):
    """live_drift_probe stub: nothing is ever stale."""
    return (False, [])


def _always_stale(weaviate_url, artifact_name):
    """live_drift_probe stub: every collection is stale."""
    return (True, ["indexNullState"])


def _env_with_collections():
    """Env exposing live class names for the Weaviate-derived types."""
    return {
        "KG_COLLECTION": "P1_KnowledgeGraph",
        "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
        "DEVELOPMENT_COLLECTION": "P1_Development",
        "DIAGRAMS_COLLECTION": "P1_Diagrams",
        "CODE_GRAPH_PROJECT": "P1",
    }


class _ApplyEdgeSpy:
    """Wrap smr._apply_edge to count calls; default to success."""

    def __init__(self, *, return_value=True):
        self.calls = []
        self.return_value = return_value

    def __call__(self, edge, *, project_root, launcher_db, weaviate_url, env):
        self.calls.append(edge)
        return self.return_value

    @property
    def count(self):
        return len(self.calls)


def _write_edge(
    migrations_dir: Path,
    artifact_type: str,
    name: str,
    *,
    destructive: str = "no",
    classification: str = "derived",
    body: str = "exit 0\n",
):
    """Write a fake .sh edge file with the mandatory header block."""
    type_dir = migrations_dir / artifact_type
    type_dir.mkdir(parents=True, exist_ok=True)
    path = type_dir / name
    path.write_text(
        f"#!/usr/bin/env bash\n"
        f"# @idempotent: yes\n"
        f"# @destructive: {destructive}\n"
        f"# @classification: {classification}\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def _insert_row(db_path, project_id, atype, name, version):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO artifact_schema_versions "
        "(project_id, artifact_type, artifact_name, schema_version, materialized_at) "
        "VALUES (?, ?, ?, ?, 1)",
        (project_id, atype, name, version),
    )
    conn.commit()
    conn.close()


def _count_registry_rows(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM artifact_schema_versions"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T3 — layout discovery
# ---------------------------------------------------------------------------


def test_t3_discover_edges_sorts_and_skips_malformed(tmp_path):
    migrations = tmp_path / "migrations"
    _write_edge(migrations, "development_collection", "2_to_3.sh")
    _write_edge(migrations, "development_collection", "1_to_2.sh")
    (migrations / "development_collection" / "garbage.sh").write_text("x")
    (migrations / "development_collection" / "3_to_5.sh").write_text("x")

    edges = smr.discover_edges(migrations, "development_collection", platform="linux")
    versions = [(e.from_version, e.to_version) for e in edges]
    assert versions == [(1, 2), (2, 3), (3, 5)]


def test_t3_discover_edges_os_dispatch(tmp_path):
    migrations = tmp_path / "migrations"
    _write_edge(migrations, "kg_collection", "1_to_2.sh")
    _write_edge(migrations, "kg_collection", "1_to_2.ps1")
    linux_edges = smr.discover_edges(migrations, "kg_collection", platform="linux")
    win_edges = smr.discover_edges(migrations, "kg_collection", platform="win32")
    assert [e.ext for e in linux_edges] == ["sh"]
    assert [e.ext for e in win_edges] == ["ps1"]


# ---------------------------------------------------------------------------
# T4 — contiguity / abort-on-gap
# ---------------------------------------------------------------------------


def test_t4_contiguity_gap_aborts_no_mutation(db_with_v033, tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    atype = "kg_node_frontmatter"  # user_curated → §2.7 path
    assert not sv.is_derived(atype)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, 3)
    _insert_row(db_with_v033, "p1", atype, "default", 1)
    _write_edge(migrations, atype, "2_to_3.sh", classification="user_curated")  # gap

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env={}, weaviate_url="http://x", live_drift_probe=_no_drift, now_ms=1,
    )
    assert spy.count == 0
    assert any(
        "schema_migration_script_missing" in d for (_, _, d) in report.errors
    )
    status = avr.check_artifact_version(
        db_with_v033, project_id="p1", artifact_type=atype, artifact_name="default"
    )
    assert status == avr.ArtifactVersionStatus.UPGRADE_IN_PLACE_NEEDED


# ---------------------------------------------------------------------------
# T5 — RECREATE on a real bump (derived, per-project: development_collection)
# ---------------------------------------------------------------------------


def test_t5_recreate_on_real_bump_applies_edge(db_with_v033, tmp_path, monkeypatch):
    atype = "development_collection"  # per-project derived
    name = "P1_Development"
    base = sv.canonical_version(atype)  # 2 today
    _insert_row(db_with_v033, "p1", atype, name, base)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, base + 1)

    migrations = tmp_path / "migrations"
    _write_edge(migrations, atype, f"{base}_to_{base + 1}.sh", classification="derived")

    spy = _ApplyEdgeSpy(return_value=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
    )
    applied_for = [a for a in report.applied if a[0] == atype and a[1] == name]
    assert len(applied_for) == 1
    assert spy.count == 1
    status = avr.check_artifact_version(
        db_with_v033, project_id="p1", artifact_type=atype, artifact_name=name
    )
    assert status == avr.ArtifactVersionStatus.UP_TO_DATE


# ---------------------------------------------------------------------------
# T6 — abort-without-half-migrate + retry (R3)
# ---------------------------------------------------------------------------


def test_t6_edge_failure_aborts_and_retries(db_with_v033, tmp_path, monkeypatch):
    atype = "development_collection"
    name = "P1_Development"
    base = sv.canonical_version(atype)
    _insert_row(db_with_v033, "p1", atype, name, base)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, base + 1)

    migrations = tmp_path / "migrations"
    _write_edge(migrations, atype, f"{base}_to_{base + 1}.sh", classification="derived")

    failing_spy = _ApplyEdgeSpy(return_value=False)
    monkeypatch.setattr(smr, "_apply_edge", failing_spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
    )
    assert failing_spy.count == 1
    assert not any(a[0] == atype and a[1] == name for a in report.applied)
    assert any("failed" in d for (a, n, d) in report.errors if a == atype)
    status = avr.check_artifact_version(
        db_with_v033, project_id="p1", artifact_type=atype, artifact_name=name
    )
    assert status == avr.ArtifactVersionStatus.RECREATE_NEEDED

    succeeding_spy = _ApplyEdgeSpy(return_value=True)
    monkeypatch.setattr(smr, "_apply_edge", succeeding_spy)
    report2 = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=2,
    )
    assert succeeding_spy.count == 1
    assert any(a[0] == atype and a[1] == name for a in report2.applied)


# ---------------------------------------------------------------------------
# T7 — NO-OP today: empty migrations/ + all NEVER_MATERIALIZED
# ---------------------------------------------------------------------------


def test_t7_noop_empty_migrations_registers_all_no_apply(
    db_with_v033, empty_migrations, monkeypatch
):
    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
        include_orchestrator_wide=True,  # root update path
    )
    # CRITICAL: zero edges applied — the verified no-op bar.
    assert spy.count == 0
    assert report.apply_edge_call_count() == 0
    assert report.pending_regenerate == []
    assert report.register_failed == []
    assert len(report.registered) > 0
    # Per-project artifact keyed by p1 → UP_TO_DATE.
    assert avr.check_artifact_version(
        db_with_v033, project_id="p1", artifact_type="kg_collection",
        artifact_name="P1_KnowledgeGraph",
    ) == avr.ArtifactVersionStatus.UP_TO_DATE
    # Orchestrator-wide artifact keyed NULL → UP_TO_DATE.
    assert avr.check_artifact_version(
        db_with_v033, project_id=None, artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
    ) == avr.ArtifactVersionStatus.UP_TO_DATE
    # Codegraph: all 5 classes registered (per-project).
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        assert avr.check_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"P1_{suffix}",
        ) == avr.ArtifactVersionStatus.UP_TO_DATE


# ---------------------------------------------------------------------------
# T8 — NO-OP up-to-date: all pre-registered at canonical + no live drift
# ---------------------------------------------------------------------------


def test_t8_noop_up_to_date_no_apply_no_writes_no_deferrals(
    db_with_v033, empty_migrations, monkeypatch
):
    env = _env_with_collections()
    smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=env, weaviate_url="http://x", live_drift_probe=_no_drift, now_ms=1,
        include_orchestrator_wide=True,
    )
    rows_before = _count_registry_rows(db_with_v033)

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    register_calls = []
    orig_register = avr.register_artifact_version

    def _reg_spy(*a, **k):
        register_calls.append(k)
        return orig_register(*a, **k)

    monkeypatch.setattr(smr.avr, "register_artifact_version", _reg_spy)

    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=env, weaviate_url="http://x", live_drift_probe=_no_drift, now_ms=2,
        codegraph_drift_probe=_no_drift, include_orchestrator_wide=True,
    )
    assert spy.count == 0
    assert register_calls == []  # zero registry writes on the up-to-date pass
    assert report.errors == []
    assert report.pending_regenerate == []
    assert report.applied == []
    assert report.register_failed == []
    assert _count_registry_rows(db_with_v033) == rows_before


# ---------------------------------------------------------------------------
# T9 — REFUSE_DOWNGRADE
# ---------------------------------------------------------------------------


def test_t9_refuse_downgrade(db_with_v033, empty_migrations, monkeypatch):
    atype = "kg_node_frontmatter"  # user_curated, canonical 1, per-project
    name = "default"
    _insert_row(db_with_v033, "p1", atype, name, 99)

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
    )
    assert spy.count == 0
    assert any(a == atype and n == name for (a, n, _) in report.refused)
    conn = sqlite3.connect(str(db_with_v033))
    v = conn.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type=? AND artifact_name=?",
        (atype, name),
    ).fetchone()[0]
    conn.close()
    assert v == 99


# ---------------------------------------------------------------------------
# T10 — POLICY STEP 3: stale + no preserving script → pending_regenerate
# (shared_kg_collection is orchestrator-wide → keyed NULL + root-update only)
# ---------------------------------------------------------------------------


def test_t10_stale_no_preserving_script_pending_regenerate_no_drop(
    db_with_v033, empty_migrations, monkeypatch
):
    atype = "shared_kg_collection"
    name = "VibeCodedOrchestrator_KnowledgeGraph"
    # Orchestrator-wide → register at NULL project_id.
    avr.register_artifact_version(
        db_with_v033, project_id=None, artifact_type=atype, artifact_name=name,
        schema_version=sv.canonical_version(atype), materialized_at=1,
    )

    def _stale_only_shared(weaviate_url, artifact_name):
        return (True, ["indexNullState"]) if artifact_name == name else (False, [])

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_stale_only_shared, now_ms=1,
        include_orchestrator_wide=True,  # root update sees the shared KG
    )
    assert spy.count == 0
    pend = [p for p in report.pending_regenerate if p["artifact_name"] == name]
    assert len(pend) == 1
    assert pend[0]["artifact_type"] == atype
    assert "indexNullState" in pend[0]["changed_fields"]
    # Recorded version unchanged (NULL-keyed row still canonical).
    assert avr.check_artifact_version(
        db_with_v033, project_id=None, artifact_type=atype, artifact_name=name
    ) == avr.ArtifactVersionStatus.UP_TO_DATE


# ---------------------------------------------------------------------------
# T-preserving — POLICY STEP 2 wins over STEP 3 (shared KG, root update)
# ---------------------------------------------------------------------------


def test_t_preserving_script_preferred_over_recreate(
    db_with_v033, tmp_path, monkeypatch
):
    atype = "shared_kg_collection"
    name = "VibeCodedOrchestrator_KnowledgeGraph"
    canonical = sv.canonical_version(atype)  # 3 today
    avr.register_artifact_version(
        db_with_v033, project_id=None, artifact_type=atype, artifact_name=name,
        schema_version=canonical, materialized_at=1,
    )
    migrations = tmp_path / "migrations"
    _write_edge(
        migrations, atype, f"{canonical - 1}_to_{canonical}.sh",
        destructive="no", classification="derived",
    )

    spy = _ApplyEdgeSpy(return_value=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_always_stale, now_ms=1,
        include_orchestrator_wide=True,
    )
    assert any(a[0] == atype and a[1] == name for a in report.applied)
    assert not any(p["artifact_name"] == name for p in report.pending_regenerate)


# ---------------------------------------------------------------------------
# T11 — classification cross-check abort (R2)
# ---------------------------------------------------------------------------


def test_t11_classification_mismatch_aborts_no_mutation(
    db_with_v033, tmp_path, monkeypatch
):
    atype = "development_collection"  # derived, per-project
    name = "P1_Development"
    base = sv.canonical_version(atype)
    _insert_row(db_with_v033, "p1", atype, name, base)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, base + 1)

    migrations = tmp_path / "migrations"
    _write_edge(
        migrations, atype, f"{base}_to_{base + 1}.sh",
        destructive="no", classification="user_curated",  # lies
    )

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
    )
    assert spy.count == 0
    assert any(
        "schema_migration_classification" in d for (_, _, d) in report.errors
    )
    assert avr.check_artifact_version(
        db_with_v033, project_id="p1", artifact_type=atype, artifact_name=name
    ) == avr.ArtifactVersionStatus.RECREATE_NEEDED


def test_t11_destructive_preserving_edge_rejected(
    db_with_v033, tmp_path, monkeypatch
):
    """A @destructive: yes script offered as a STEP-2 preserving edge for a
    DERIVED collection is rejected (R2). Per-project derived collection."""
    atype = "development_collection"
    name = "P1_Development"
    canonical = sv.canonical_version(atype)
    avr.register_artifact_version(
        db_with_v033, project_id="p1", artifact_type=atype, artifact_name=name,
        schema_version=canonical, materialized_at=1,
    )
    migrations = tmp_path / "migrations"
    _write_edge(
        migrations, atype, f"{canonical - 1}_to_{canonical}.sh",
        destructive="yes", classification="derived",
    )

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_always_stale, now_ms=1,
    )
    assert spy.count == 0
    assert any(
        "schema_migration_classification" in d
        for (a, n, d) in report.errors if a == atype
    )


# ---------------------------------------------------------------------------
# T12 — --check dry-run
# ---------------------------------------------------------------------------


def test_t12_check_dry_run_plans_no_mutation(db_with_v033, tmp_path, monkeypatch):
    atype = "development_collection"
    name = "P1_Development"
    base = sv.canonical_version(atype)
    _insert_row(db_with_v033, "p1", atype, name, base)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, base + 1)
    migrations = tmp_path / "migrations"
    _write_edge(migrations, atype, f"{base}_to_{base + 1}.sh", classification="derived")

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    rows_before = _count_registry_rows(db_with_v033)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, check=True, now_ms=1,
    )
    assert spy.count == 0
    assert any(p[0] == atype and p[1] == name for p in report.planned)
    assert _count_registry_rows(db_with_v033) == rows_before


# ---------------------------------------------------------------------------
# Per-project scoping (audit C2/C3 structural fix)
# ---------------------------------------------------------------------------


def test_orchestrator_wide_skipped_on_non_root_update(
    db_with_v033, empty_migrations, monkeypatch
):
    """A non-root project's bundle update (include_orchestrator_wide=False)
    NEVER touches the shared KG / Layer-5 shapes → no NULL-keyed rows written
    for them, and they are not in any report list."""
    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
        include_orchestrator_wide=False,  # non-root bundle update
    )
    assert not any(a == "shared_kg_collection" for (a, _, _) in report.registered)
    assert avr.check_artifact_version(
        db_with_v033, project_id=None, artifact_type="shared_kg_collection",
        artifact_name="VibeCodedOrchestrator_KnowledgeGraph",
    ) == avr.ArtifactVersionStatus.NEVER_MATERIALIZED
    assert any(a == "kg_collection" for (a, _, _) in report.registered)


def test_per_project_collections_keyed_by_project_not_null(
    db_with_v033, empty_migrations, monkeypatch
):
    """C2: per-project collections are keyed by the real project_id, not NULL,
    so they never collide with the NULL-keyed orchestrator-wide rows."""
    monkeypatch.setattr(smr, "_apply_edge", _ApplyEdgeSpy())
    smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1, include_orchestrator_wide=True,
    )
    conn = sqlite3.connect(str(db_with_v033))
    dev_pid = conn.execute(
        "SELECT project_id FROM artifact_schema_versions "
        "WHERE artifact_type='development_collection'"
    ).fetchone()
    shared_pid = conn.execute(
        "SELECT project_id FROM artifact_schema_versions "
        "WHERE artifact_type='shared_kg_collection'"
    ).fetchone()
    conn.close()
    assert dev_pid is not None and dev_pid[0] == "p1"
    assert shared_pid is not None and shared_pid[0] is None


# ---------------------------------------------------------------------------
# Codegraph (NIT-1 + structural: codegraph is migratable, 5 classes)
# ---------------------------------------------------------------------------


def test_codegraph_resolves_five_classes():
    env = {"CODE_GRAPH_PROJECT": "MyProj"}
    names = smr._resolve_artifact_names("codegraph_collection", env, None)
    assert names == [
        "MyProj_CodeModule", "MyProj_CodeClass", "MyProj_CodeFunction",
        "MyProj_CodeAPI", "MyProj_CodeInteraction",
    ]


def test_codegraph_prefix_from_project_name_normalized():
    env = {"PROJECT_NAME": "My Project"}
    names = smr._resolve_artifact_names("codegraph_collection", env, None)
    assert names[0] == "MyProject_CodeModule"  # space-normalized prefix


def test_codegraph_no_prefix_skips():
    assert smr._resolve_artifact_names("codegraph_collection", {}, None) == []


def test_codegraph_up_to_date_does_not_misfire(
    db_with_v033, empty_migrations, monkeypatch
):
    """NIT-1: codegraph at UP_TO_DATE with resolved real names + the default
    codegraph probe (never-stale) → no apply, no pending_regenerate, no error.
    live_drift_probe is always-stale to PROVE codegraph uses its OWN probe."""
    canonical = sv.canonical_version("codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"P1_{suffix}", schema_version=canonical, materialized_at=1,
        )
    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_always_stale, now_ms=1,
    )
    cg_pending = [p for p in report.pending_regenerate
                  if p["artifact_type"] == "codegraph_collection"]
    assert cg_pending == []
    cg_errors = [e for e in report.errors if e[0] == "codegraph_collection"]
    assert cg_errors == []


def test_codegraph_stale_probe_drives_policy(
    db_with_v033, empty_migrations, monkeypatch
):
    """A codegraph-specific stale probe (no preserving script) → STEP 3
    pending_regenerate for each stale class, no drop."""
    canonical = sv.canonical_version("codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"P1_{suffix}", schema_version=canonical, materialized_at=1,
        )
    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)

    def _cg_stale(weaviate_url, artifact_name):
        return (True, ["function_body dataType"])

    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, codegraph_drift_probe=_cg_stale, now_ms=1,
    )
    assert spy.count == 0  # never dropped
    cg_pending = [p for p in report.pending_regenerate
                  if p["artifact_type"] == "codegraph_collection"]
    assert len(cg_pending) == len(smr._CODEGRAPH_CLASS_SUFFIXES)


# ---------------------------------------------------------------------------
# C1 — atomic .sql edge (executescript bug fix)
# ---------------------------------------------------------------------------


def test_c1_sql_edge_atomic_rollback_on_second_statement(tmp_path):
    """A 2-statement .sql edge whose 2nd statement fails leaves the 1st ROLLED
    BACK — proving the transaction is atomic (not executescript's auto-commit)."""
    db = tmp_path / "target.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()

    edge_path = tmp_path / "0_to_1.sql"
    edge_path.write_text(
        "-- @db: launcher\n"
        "-- @destructive: no\n"
        "-- @classification: derived\n"
        "INSERT INTO t (id, v) VALUES (1, 'first');\n"
        "INSERT INTO t (nonexistent_col) VALUES ('boom');\n",  # stmt 2 fails
        encoding="utf-8",
    )
    edge = smr.MigrationEdge(
        artifact_type="launcher_db_table_set", from_version=0, to_version=1,
        path=edge_path, ext="sql",
    )
    ok = smr._apply_sql_edge(edge, launcher_db=db)
    assert ok is False
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert count == 0  # stmt-1 rolled back (atomic) — the C1 guarantee


def test_c1_sql_edge_success_commits_all(tmp_path):
    db = tmp_path / "target.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()
    edge_path = tmp_path / "0_to_1.sql"
    edge_path.write_text(
        "INSERT INTO t (id, v) VALUES (1, 'a');\n"
        "INSERT INTO t (id, v) VALUES (2, 'b; not a separator');\n",
        encoding="utf-8",
    )
    edge = smr.MigrationEdge(
        artifact_type="launcher_db_table_set", from_version=0, to_version=1,
        path=edge_path, ext="sql",
    )
    assert smr._apply_sql_edge(edge, launcher_db=db) is True
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    conn.close()
    assert rows == [(1, "a"), (2, "b; not a separator")]


def test_split_sql_respects_literals_and_comments():
    sql = (
        "INSERT INTO t VALUES ('a;b');\n"
        "-- a comment with ; in it\n"
        "/* block ; comment */\n"
        "UPDATE t SET v = 'x;y' WHERE id = 1;\n"
    )
    stmts = smr._split_sql_statements(sql)
    assert len(stmts) == 2
    assert "'a;b'" in stmts[0]
    assert "'x;y'" in stmts[1]


# ---------------------------------------------------------------------------
# C4 — register failure accounting
# ---------------------------------------------------------------------------


def test_c4_register_failure_not_counted_as_registered(
    db_with_v033, empty_migrations, monkeypatch
):
    """When register_artifact_version returns False (DB locked), the artifact
    is recorded in register_failed, NOT registered (C4)."""
    monkeypatch.setattr(smr, "_apply_edge", _ApplyEdgeSpy())

    def _always_false(*a, **k):
        return False

    monkeypatch.setattr(smr.avr, "register_artifact_version", _always_false)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1, include_orchestrator_wide=True,
    )
    assert report.registered == []  # nothing falsely reported as registered
    assert len(report.register_failed) > 0


# ---------------------------------------------------------------------------
# Runner-level unknown-type skip (defensive — no crash)
# ---------------------------------------------------------------------------


def test_runner_skips_unknown_artifact_type_dir(db_with_v033, tmp_path):
    migrations = tmp_path / "migrations"
    _write_edge(migrations, "totally_unknown_type", "0_to_1.sh")
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, now_ms=1,
    )
    assert isinstance(report, smr.MigrationRunReport)


# ---------------------------------------------------------------------------
# build_deferral_entries — shared report→DeferralEntry mapping
# ---------------------------------------------------------------------------


def test_build_deferral_entries_maps_each_outcome():
    report = smr.MigrationRunReport()
    report.refused.append(("kg_node_frontmatter", "default", "stored newer"))
    report.pending_regenerate.append({
        "artifact_type": "shared_kg_collection",
        "artifact_name": "VibeCodedOrchestrator_KnowledgeGraph",
        "changed_fields": ["indexNullState"],
    })
    report.errors.append((
        "development_collection", "P1_Development",
        "edge 2_to_3.sh failed [schema_migration_failed_2_to_3]",
    ))
    entries = smr.build_deferral_entries(report)
    cids = {e.condition_id for e in entries}
    assert "schema_migration_refuse_downgrade" in cids
    assert any(c.startswith("schema_regenerate_or_defer_") for c in cids)
    assert "schema_migration_failed_2_to_3" in cids


def test_build_deferral_entries_skips_needs_choice_error_row():
    """needs_choice errors are covered by pending_regenerate; the error-row
    duplicate is skipped so the deferral file stays clean."""
    report = smr.MigrationRunReport()
    report.errors.append((
        "shared_kg_collection", "X",
        "stale, no preserving script [schema_migration_needs_choice]",
    ))
    entries = smr.build_deferral_entries(report)
    assert entries == []


# ---------------------------------------------------------------------------
# CONCERN-1 — the per-project CLI apply WRITES the durable deferral
# ---------------------------------------------------------------------------


def _migrate_schema_args(folder, db_path, **over):
    """Build an argparse.Namespace for _cmd_migrate_schema."""
    ns = argparse.Namespace(
        folder=str(folder), db=str(db_path), project_id="p1",
        migrations_dir=None, include_orchestrator_wide=False,
        check=False, regenerate=None, artifact_name=None, strict=False,
        now_ms=1,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_concern1_cli_apply_writes_deferral_file(
    db_with_v033, tmp_path, empty_migrations, monkeypatch, capsys
):
    """A stale-derived per-project apply run (live_drift_probe stale, empty
    migrations/) WRITES a schema_regenerate_or_defer_* entry to the project's
    UPDATE_DEFERRED.md file — not just JSON. This is what makes the Rust
    toast's 'deferral written' claim TRUE and survives to session-start."""
    project_folder = tmp_path / "proj"
    project_folder.mkdir()

    # Pre-register the per-project KG at canonical so status is UP_TO_DATE →
    # the live-drift net runs and (stubbed) reports stale.
    name = "P1_KnowledgeGraph"
    avr.register_artifact_version(
        db_with_v033, project_id="p1", artifact_type="kg_collection",
        artifact_name=name, schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1,
    )

    # Force the runner's default live-drift probe to report the KG stale, and
    # point migrations/ at the empty dir (no preserving edge → POLICY STEP 3).
    def _stale_kg(weaviate_url, artifact_name):
        return (True, ["indexNullState"]) if artifact_name == name else (False, [])

    monkeypatch.setattr(smr, "_default_live_drift_probe", _stale_kg)
    monkeypatch.setenv("KG_COLLECTION", name)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "P1")

    args = _migrate_schema_args(
        project_folder, db_with_v033, migrations_dir=str(empty_migrations),
    )
    rc = pinit._cmd_migrate_schema(args)
    assert rc == 0

    # The durable deferral file now exists with the regenerate-or-defer entry.
    deferred = project_folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert deferred.exists()
    persisted = DeferralReport.read(project_folder)
    cids = {e.condition_id for e in persisted.entries}
    assert any(c.startswith("schema_regenerate_or_defer_") for c in cids)
    # JSON also reports it.
    out = capsys.readouterr().out
    assert '"deferral_written": true' in out


def test_concern1_cli_check_writes_no_deferral(
    db_with_v033, tmp_path, empty_migrations, monkeypatch, capsys
):
    """--check stays no-write even when a stale collection is detected."""
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    name = "P1_KnowledgeGraph"
    avr.register_artifact_version(
        db_with_v033, project_id="p1", artifact_type="kg_collection",
        artifact_name=name, schema_version=sv.canonical_version("kg_collection"),
        materialized_at=1,
    )

    def _stale_kg(weaviate_url, artifact_name):
        return (True, ["indexNullState"]) if artifact_name == name else (False, [])

    monkeypatch.setattr(smr, "_default_live_drift_probe", _stale_kg)
    monkeypatch.setenv("KG_COLLECTION", name)
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "P1")

    args = _migrate_schema_args(
        project_folder, db_with_v033, migrations_dir=str(empty_migrations),
        check=True,
    )
    rc = pinit._cmd_migrate_schema(args)
    assert rc == 0
    deferred = project_folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert not deferred.exists()  # dry-run wrote nothing
    out = capsys.readouterr().out
    assert '"deferral_written": false' in out


def test_concern1_cli_clean_run_writes_no_deferral(
    db_with_v033, tmp_path, empty_migrations, monkeypatch, capsys
):
    """A clean per-project run (nothing stale, empty migrations/) writes NO
    deferral file — the no-op-today guarantee at the CLI surface."""
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    monkeypatch.setattr(smr, "_default_live_drift_probe", _no_drift)
    monkeypatch.setenv("KG_COLLECTION", "P1_KnowledgeGraph")
    monkeypatch.setenv("CODE_GRAPH_PROJECT", "P1")

    args = _migrate_schema_args(
        project_folder, db_with_v033, migrations_dir=str(empty_migrations),
    )
    rc = pinit._cmd_migrate_schema(args)
    assert rc == 0
    deferred = project_folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert not deferred.exists()
    out = capsys.readouterr().out
    assert '"deferral_written": false' in out


# ---------------------------------------------------------------------------
# RUST_OWNED_TYPES — Rust/_schema_migrations-owned artifacts do a REGISTER-ONLY
# version advance on a stored<canonical gap, with NO Python edge demanded and
# NO schema_migration_script_missing deferral.
#
# Regression for the latent false-deferral that surfaced when a launcher.db
# migration bump advanced LAUNCHER_DB_TABLE_SET_VERSION (e.g. 34→35) with no
# Python edge shipped (no launcher.db bump ever ships one — the real schema
# lives in migrations.rs::MIGRATIONS, applied at launcher startup). Before the
# fix this hit the user_curated no-edge error branch and re-fired a
# schema_migration_script_missing deferral on EVERY update.
#
# These exercise the LIVE gap path (seed stored=canonical-1 → run the runner),
# filling the coverage gap in test_v52_ag_schema_versions.py's
# test_launcher_db_table_set_version_matches_migration_count (which asserts only
# const==MIGRATIONS-count, never the runner outcome).
# ---------------------------------------------------------------------------


def test_rust_owned_register_only_advance_no_deferral(
    db_with_v033, empty_migrations, monkeypatch
):
    """launcher_db_table_set at stored=canonical-1 → clean register-only
    advance to canonical, NO error, NO edge applied, NO deferral.

    Seeds at ``canonical_version - 1`` (not a hardcoded 34) so the test stays
    correct across future LAUNCHER_DB_TABLE_SET_VERSION bumps."""
    atype = "launcher_db_table_set"
    name = "default"  # the sentinel _resolve_artifact_names uses for non-class types
    canonical = sv.canonical_version(atype)
    # Orchestrator-wide → keyed NULL. Seed one version behind canonical.
    _insert_row(db_with_v033, None, atype, name, canonical - 1)

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, codegraph_drift_probe=_no_drift, now_ms=1,
        include_orchestrator_wide=True,  # root update sees orchestrator-wide types
    )

    # No edge ran, and CRITICALLY no error / no pending_regenerate.
    assert spy.count == 0
    assert report.pending_regenerate == []
    assert report.register_failed == []
    # The decisive assertion: NO schema_migration_script_missing error.
    assert report.errors == [], (
        f"expected a clean register-only advance, got errors: {report.errors}"
    )
    # It landed in the success accumulator.
    advanced = [
        (a, n, d) for (a, n, d) in report.registered
        if a == atype and n == name
    ]
    assert len(advanced) == 1, report.registered

    # The stored version is now == canonical (advanced, not stuck).
    assert avr.check_artifact_version(
        db_with_v033, project_id=None, artifact_type=atype, artifact_name=name
    ) == avr.ArtifactVersionStatus.UP_TO_DATE
    conn = sqlite3.connect(str(db_with_v033))
    v = conn.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE project_id IS NULL AND artifact_type=? AND artifact_name=?",
        (atype, name),
    ).fetchone()[0]
    conn.close()
    assert v == canonical

    # And the report→deferral mapping yields NO entry for this outcome.
    assert smr.build_deferral_entries(report) == []


def test_rust_owned_register_only_advance_check_is_dry_run(
    db_with_v033, empty_migrations, monkeypatch
):
    """--check on a Rust-owned gap PLANS the advance but mutates nothing."""
    atype = "launcher_db_table_set"
    name = "default"
    canonical = sv.canonical_version(atype)
    _insert_row(db_with_v033, None, atype, name, canonical - 1)

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, codegraph_drift_probe=_no_drift, now_ms=1,
        check=True, include_orchestrator_wide=True,
    )
    assert spy.count == 0
    assert report.errors == []
    # Recorded as a planned/would-advance success, not an error.
    assert any(a == atype and n == name for (a, n, _) in report.registered)
    # NO mutation: the stored version is still canonical-1.
    conn = sqlite3.connect(str(db_with_v033))
    v = conn.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE project_id IS NULL AND artifact_type=? AND artifact_name=?",
        (atype, name),
    ).fetchone()[0]
    conn.close()
    assert v == canonical - 1


def test_rl_events_payload_shape_NOT_rust_owned(
    db_with_v033, empty_migrations, monkeypatch
):
    """rl_events_payload_shape is user_curated (its JSON payload shape is
    migrated by the Python RL telemetry layer, not Rust). It must NOT be in
    RUST_OWNED_TYPES — a register-only advance there would silently skip a
    genuinely-needed payload migration. A stored<canonical gap MUST surface a
    schema_migration_script_missing error (until a real Python edge ships),
    NOT a silent advance.

    Uses a monkeypatched canonical bump so the assertion holds regardless of
    the constant's current value."""
    assert "rl_events_payload_shape" not in smr.RUST_OWNED_TYPES
    atype = "rl_events_payload_shape"
    name = "default"
    base = sv.canonical_version(atype)
    monkeypatch.setitem(sv.CANONICAL_VERSIONS, atype, base + 1)
    _insert_row(db_with_v033, None, atype, name, base)

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_no_drift, codegraph_drift_probe=_no_drift, now_ms=1,
        include_orchestrator_wide=True,
    )
    assert spy.count == 0
    # It is NOT silently advanced; it surfaces the missing-script error.
    assert not any(a == atype for (a, n, _) in report.registered)
    assert any(
        a == atype and "schema_migration_script_missing" in d
        for (a, n, d) in report.errors
    ), report.errors
    # Stored version unchanged (the runner never advanced it).
    conn = sqlite3.connect(str(db_with_v033))
    v = conn.execute(
        "SELECT schema_version FROM artifact_schema_versions "
        "WHERE project_id IS NULL AND artifact_type=? AND artifact_name=?",
        (atype, name),
    ).fetchone()[0]
    conn.close()
    assert v == base


def test_rust_owned_types_membership_is_justified():
    """Guard the membership of RUST_OWNED_TYPES: every member must be a
    derived-but-not-Weaviate artifact_type that exists in the registry. This
    catches an accidental future addition of a Weaviate-derived type (which
    has its own BINDING POLICY) or a typo'd artifact_type."""
    for atype in smr.RUST_OWNED_TYPES:
        assert atype in sv.CANONICAL_VERSIONS, f"unknown artifact_type {atype!r}"
        assert atype not in smr.WEAVIATE_DERIVED_TYPES, (
            f"{atype!r} is a Weaviate collection — it follows the BINDING "
            f"POLICY, not the register-only path"
        )
    # The one member shipped today.
    assert smr.RUST_OWNED_TYPES == frozenset({"launcher_db_table_set"})


# ---------------------------------------------------------------------------
# v0.2.70 (Bug A) — dim-mismatch parity: a same-name/different-dim vector slot
# must NEVER enter the auto-applied `copy` bucket. `_schema_delta` compares
# slot NAME sets only, so a same-name slot (even with a different dim) is a
# `noop` there (never `copy`); dim-mismatch is owned by THIS subsystem
# (live_fingerprint_stale → schema_migration_runner), which DEFERS via the
# preserving-vs-recreate policy. This pins the boundary so the v0.2.70
# auto-apply of additive `copy` can never accidentally cover a lossy
# dim-mismatch.
# ---------------------------------------------------------------------------


def test_v0270_dim_mismatch_never_classifies_as_copy():
    """A same-name vector slot with a DIFFERENT dimension is invisible to
    `_schema_delta` (name-only comparison) → classifies `noop`, never `copy`.
    So the auto-applied additive `copy` path can never receive a lossy
    dim-mismatch."""
    # actual + target share the SAME slot name but a different vector dim.
    actual = {
        "vectorConfig": {
            "qwen3_embed": {"vectorizer": {}, "vectorIndexConfig": {"dimensions": 1024}},
        },
        "properties": [],
        "invertedIndexConfig": {},
    }
    target = {
        "vectorConfig": {
            # Same NAME, different dim (e.g. swapped embedder). _schema_delta
            # compares slot-name sets only → no missing slot detected.
            "qwen3_embed": {"vectorizer": {}, "vectorIndexConfig": {"dimensions": 2048}},
        },
        "properties": [],
        "invertedIndexConfig": {},
    }
    delta = pinit._schema_delta(actual, target)
    assert not delta.missing_vec_slots, (
        "same-name slot must NOT register as a missing slot (name-only delta)"
    )
    action = pinit._classify_action(delta)
    assert action == "noop", (
        f"dim-mismatch (same-name slot) must classify `noop`, not `copy`; "
        f"got {action!r}"
    )
    assert action != "copy", "dim-mismatch must never enter the copy bucket"


def test_v0270_dim_mismatch_stale_probe_still_defers_not_auto_applied(
    db_with_v033, empty_migrations, monkeypatch
):
    """The schema_migration_runner OWNS dim-mismatch (via the live drift
    probe). A stale (e.g. dim-changed) collection with no preserving script
    maps to `pending_regenerate` — a DEFERRING outcome — and applies NO edge.
    It is never silently auto-ported as an additive `copy`."""
    name = "VibeCodedOrchestrator_KnowledgeGraph"
    atype = "shared_kg_collection"
    avr.register_artifact_version(
        db_with_v033, project_id=None, artifact_type=atype, artifact_name=name,
        schema_version=sv.canonical_version(atype), materialized_at=1,
    )

    def _dim_stale(weaviate_url, artifact_name):
        # Simulate a dim-mismatch surfaced by the live fingerprint probe.
        return (True, ["vector_dim"]) if artifact_name == name else (False, [])

    spy = _ApplyEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033, project_id="p1", migrations_dir=empty_migrations,
        env=_env_with_collections(), weaviate_url="http://x",
        live_drift_probe=_dim_stale, now_ms=1,
        include_orchestrator_wide=True,
    )
    # No edge applied (nothing auto-ported); the stale collection is deferred
    # to the preserving-vs-recreate policy (pending_regenerate).
    assert spy.count == 0, "dim-mismatch must NOT auto-apply a migration edge"
    pend = [p for p in report.pending_regenerate if p["artifact_name"] == name]
    assert len(pend) == 1, "dim-mismatch must surface a pending_regenerate (deferring)"
