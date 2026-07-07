# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 migration-delivery (THEME 2) — A1 / HIGH-2 / A2 / A3.

These pin the fixes that carry v0.2.73's code-graph cleanup + the .claude/state
purge to EXISTING users on ``install.py --update``:

  A1     env-independent codegraph name resolution + augmented edge env: the
         runner iterates the codegraph loop AND each edge subprocess receives
         CODE_GRAPH_PROJECT even when the caller env lacks it.
  HIGH-2 an edge that exits rc=0 with the EDGE_NOOP_NO_PREFIX sentinel does NOT
         advance the recorded version + emits a deferral (defense-in-depth
         against the false-advance trap).
  A2     NEVER_MATERIALIZED + collection-has-rows → the ladder runs from earliest
         (not a stamp-to-canonical).
  A3     one-time reconcile over a fake launcher.db with a stale registry row →
         replays the ladder + registers canonical.

Mocked throughout — NO live Weaviate, NO live launcher. The Weaviate existence
probe + edge apply are monkeypatched. Fixtures mirror
``tests/test_schema_migration_runner.py``.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from vco_lib import artifact_version_registry as avr  # noqa: E402
from vco_lib import codegraph_registry_reconcile as cgrr  # noqa: E402
from vco_lib import schema_migration_runner as smr  # noqa: E402
from vco_lib import schema_versions as sv  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_schema_migration_runner.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_with_v033(tmp_path):
    """Apply launcher.db migrations 1..N against a fresh sqlite DB + register a
    project + a codegraph binding for it."""
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
        "VALUES ('p1', 'MyProj', '/tmp/p1', 'base', 'myproj', 1, 1, NULL)"
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_codegraph_binding(db_path, project_id, prefix, *, enabled=1):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO project_codegraph_bindings "
            "(project_id, collection_prefix, enabled, config_json, updated_at) "
            "VALUES (?, ?, ?, '{}', 1)",
            (project_id, prefix, enabled),
        )
        conn.commit()
    finally:
        conn.close()


def _real_migrations_dir():
    """The SHIPPED migrations/ dir with the real codegraph 4→5→6→7 ladder."""
    return _REPO / "migrations"


class _EnvCapturingEdgeSpy:
    """Stand in for smr._apply_edge: capture the env of every edge call and
    return a configurable EdgeResult (default: applied)."""

    def __init__(self, *, stdout=smr._EDGE_SENTINEL_APPLIED, ok=True):
        self.calls = []  # list of (edge, env dict)
        self.stdout = stdout
        self.ok = ok

    def __call__(self, edge, *, project_root, launcher_db, weaviate_url, env):
        self.calls.append((edge, dict(env)))
        return smr.EdgeResult(ok=self.ok, stdout=self.stdout)

    @property
    def count(self):
        return len(self.calls)


# ---------------------------------------------------------------------------
# A1 — env-independent codegraph name resolution + augmented edge env
# ---------------------------------------------------------------------------


def test_a1_resolve_inputs_reads_prefix_from_launcher_db(db_with_v033):
    """resolve_codegraph_migration_inputs reads the SSOT prefix from
    project_codegraph_bindings when the env has NO CODE_GRAPH_PROJECT."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    artifact_names, aug_env, prefix = smr.resolve_codegraph_migration_inputs(
        {},  # env WITHOUT CODE_GRAPH_PROJECT / PROJECT_NAME
        db_path=db_with_v033,
        project_id="p1",
    )
    assert prefix == "MyProj"
    assert artifact_names == {
        "codegraph_collection": [
            "MyProj_CodeModule",
            "MyProj_CodeClass",
            "MyProj_CodeFunction",
            "MyProj_CodeAPI",
            "MyProj_CodeInteraction",
        ]
    }
    assert aug_env["CODE_GRAPH_PROJECT"] == "MyProj"


def test_a1_resolve_inputs_by_project_name(db_with_v033):
    """Falls back to a name/slug lookup when project_id resolution misses."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    _, _, prefix = smr.resolve_codegraph_migration_inputs(
        {},
        db_path=db_with_v033,
        project_id=None,
        project_name="MyProj",  # matches projects.name
    )
    assert prefix == "MyProj"


def test_a1_resolve_inputs_soft_fail_no_binding(db_with_v033):
    """No binding + no env → ({}, dict(env), None): the runner's own env
    resolution stays in charge and no bogus override is injected."""
    names, aug_env, prefix = smr.resolve_codegraph_migration_inputs(
        {},
        db_path=db_with_v033,
        project_id="p1",  # no codegraph binding inserted
    )
    assert prefix is None
    assert names == {}
    assert "CODE_GRAPH_PROJECT" not in aug_env


def test_a1_loop_iterates_and_edge_receives_code_graph_project(
    db_with_v033, monkeypatch
):
    """THE PRIMARY A1 ASSERTION: with an env lacking CODE_GRAPH_PROJECT but the
    resolved artifact_names + augmented env passed, the codegraph loop iterates
    AND the edge subprocess receives CODE_GRAPH_PROJECT (spy the sub_env)."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    # Register the codegraph collection BEHIND canonical so the runner reaches
    # the edge-apply path (RECREATE_NEEDED for a derived collection).
    canonical = sv.canonical_version("codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    # Now force them stale: rewrite the stored version to the earliest edge's
    # from_version so a full ladder is owed.
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    assert edges, "shipped codegraph ladder must exist"
    earliest = edges[0].from_version
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type = 'codegraph_collection'",
        (earliest,),
    )
    conn.commit()
    conn.close()

    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)

    artifact_names, aug_env, prefix = smr.resolve_codegraph_migration_inputs(
        {},  # env WITHOUT CODE_GRAPH_PROJECT
        db_path=db_with_v033,
        project_id="p1",
    )
    assert prefix == "MyProj"

    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env=aug_env,
        artifact_names=artifact_names,
        weaviate_url="http://localhost:8081",
        include_orchestrator_wide=False,
        now_ms=1,
    )

    # The loop iterated: at least the 3 ladder edges were applied.
    assert spy.count >= 1, "codegraph edge loop must have iterated"
    # Every edge subprocess received CODE_GRAPH_PROJECT in its env.
    for _edge, env in spy.calls:
        assert env.get("CODE_GRAPH_PROJECT") == "MyProj", (
            "edge env must carry the resolved CODE_GRAPH_PROJECT (A1)"
        )
    # The recorded version advanced to canonical (registered on final edge).
    assert not report.errors, report.errors
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(canonical,)]


@pytest.mark.parametrize("offset", [1, 2])
def test_b1_stored_above_earliest_still_applies_ladder(
    db_with_v033, monkeypatch, offset
):
    """B-1 REGRESSION: a codegraph collection recorded at a version ABOVE the
    earliest retained edge (stored=earliest+1 / +2, e.g. v5/v6 against a retained
    [4→5,5→6,6→7] ladder) must STILL apply the owed edges and advance to canonical.

    Before the B-1 fix, `_apply_edges_preserving` asserted contiguity on the WHOLE
    retained ladder from `stored`, so `_assert_contiguous(start=5)` saw `4_to_5`
    first → 'version gap' → ZERO edges applied → permanent defer. The fix slices
    the ladder to `from_version >= stored`. This is latent today (nothing writes a
    codegraph v5/v6 row yet) but a guaranteed deadlock at the next codegraph bump,
    so it's pinned now. The pre-existing test above only covers stored=earliest —
    the one value that masks the bug.
    """
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    assert edges, "shipped codegraph ladder must exist"
    earliest = edges[0].from_version
    stored = earliest + offset
    # Only meaningful when the ladder is deep enough that stored is still < canonical
    # AND an edge starts exactly at `stored` (mid-ladder boundary).
    if stored >= canonical or not any(e.from_version == stored for e in edges):
        pytest.skip(
            f"ladder too shallow for stored=earliest+{offset} "
            f"(earliest={earliest}, canonical={canonical})"
        )

    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type = 'codegraph_collection'",
        (stored,),
    )
    conn.commit()
    conn.close()

    # Edge applies successfully + reports it did real work (prefix resolved).
    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_APPLIED, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)

    artifact_names = {
        "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
    }
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names=artifact_names,
        weaviate_url="http://localhost:8081",
        include_orchestrator_wide=False,
        now_ms=1,
    )

    # The owed edges from `stored` forward MUST have applied (NOT a gap error).
    assert spy.count >= 1, (
        f"stored=earliest+{offset}={stored}: ladder must apply the owed edges, "
        f"not defer on a spurious contiguity gap. report.errors={report.errors}"
    )
    assert not any("script_missing" in str(e) for e in report.errors), (
        f"stored={stored}: must NOT report a missing-script gap (B-1). "
        f"errors={report.errors}"
    )
    # Advanced to canonical.
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(canonical,)], (
        f"stored={stored}: must advance to canonical v{canonical}, got {ver}"
    )


# ---------------------------------------------------------------------------
# HIGH-2 — no-prefix sentinel refuses the false-advance
# ---------------------------------------------------------------------------


def test_high2_no_prefix_sentinel_does_not_advance(db_with_v033, monkeypatch):
    """An edge that returns rc=0 with EDGE_NOOP_NO_PREFIX → the runner does NOT
    advance the version + emits an error (deferral). This is the exact
    false-advance the A1 second-order trap describes."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    earliest = edges[0].from_version
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type = 'codegraph_collection'",
        (earliest,),
    )
    conn.commit()
    conn.close()

    # Edge exits rc=0 but reports it touched NOTHING (no prefix in ITS env).
    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_NOOP_NO_PREFIX, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)

    artifact_names = {
        "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
    }
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names=artifact_names,
        include_orchestrator_wide=False,
        now_ms=1,
    )

    # An error was recorded (the deferral surface); NOTHING applied.
    assert report.errors, "no-prefix sentinel must surface a deferral error"
    assert any("EDGE_NOOP_NO_PREFIX" in d for (_t, _n, d) in report.errors)
    assert not report.applied, "must NOT count as applied"
    # The recorded version did NOT advance past the earliest edge's from.
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(earliest,)], "version must stay at stored, not advance"


def test_high2_applied_sentinel_advances(db_with_v033, monkeypatch):
    """The positive control: EDGE_APPLIED sentinel → the version advances."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    earliest = edges[0].from_version
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type='codegraph_collection'",
        (earliest,),
    )
    conn.commit()
    conn.close()

    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_APPLIED, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names={
            "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
        },
        include_orchestrator_wide=False,
        now_ms=1,
    )
    assert not report.errors, report.errors
    assert report.applied
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(canonical,)]


def test_h1_no_sentinel_does_not_advance(db_with_v033, monkeypatch):
    """H1 REGRESSION: a codegraph edge that exits rc=0 but prints NEITHER
    EDGE_APPLIED nor EDGE_NOOP_NO_PREFIX (blank/unexpected stdout — a future edge
    author forgetting the sentinel, a truncated pipe) must NOT advance the
    version. The false-advance guard was a denylist (only checked the negative
    sentinel); a positive EDGE_APPLIED is now REQUIRED as proof-of-work for
    codegraph edges."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    earliest = edges[0].from_version
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type='codegraph_collection'",
        (earliest,),
    )
    conn.commit()
    conn.close()

    # rc=0 but BLANK stdout → no positive sentinel → must defer, not advance.
    spy = _EnvCapturingEdgeSpy(stdout="", ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names={
            "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
        },
        include_orchestrator_wide=False,
        now_ms=1,
    )
    assert report.errors, "a no-sentinel rc=0 edge must surface a deferral"
    assert any("EDGE_APPLIED" in d for (_t, _n, d) in report.errors), (
        f"deferral must cite the missing EDGE_APPLIED sentinel; got {report.errors}"
    )
    assert not report.applied, "must NOT count as applied on ambiguous stdout"
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(earliest,)], "version must stay at stored (no false-advance)"


# ---------------------------------------------------------------------------
# A2 — NEVER_MATERIALIZED + collection-has-rows → ladder from earliest
# ---------------------------------------------------------------------------


def test_a2_never_materialized_with_rows_runs_ladder(db_with_v033, monkeypatch):
    """A codegraph collection with NO registry row but that EXISTS WITH DATA →
    the ladder replays from earliest (NOT a stamp-to-canonical with zero
    edges)."""
    # No artifact_schema_versions rows exist → NEVER_MATERIALIZED.
    # Force the existence probe to report "has rows".
    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: True
    )
    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_APPLIED, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)

    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names={
            "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
        },
        include_orchestrator_wide=False,
        now_ms=1,
    )
    # The ladder ran (edges applied), NOT a zero-edge stamp.
    assert spy.count >= 1, "the ladder must replay from earliest"
    assert report.applied, "edges must be recorded as applied"
    # Ladder ran ONCE for the whole codegraph type (not 5x for 5 class names).
    ladder_len = len(
        smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    )
    assert spy.count == ladder_len, (
        f"ladder must run exactly once ({ladder_len} edges), not per class"
    )
    canonical = sv.canonical_version("codegraph_collection")
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(canonical,)]


def test_a2_never_materialized_no_rows_stamps_canonical(db_with_v033, monkeypatch):
    """A born-fresh / empty collection (no rows) → the ORIGINAL born-at-canonical
    stamp, NO ladder replay."""
    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: False
    )
    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names={
            "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
        },
        include_orchestrator_wide=False,
        now_ms=1,
    )
    assert spy.count == 0, "no ladder replay for an empty collection"
    # Stamped straight to canonical (5 class names registered).
    assert len(report.registered) >= 5


def test_a2_never_materialized_unknown_existence_skips_ladder_and_stamp(
    db_with_v033, monkeypatch
):
    """F1 REGRESSION (Fable update-process review): Weaviate down / unknown
    existence (probe returns None) → conservative BOTH ways: NO ladder replay
    AND — critically — NO born-at-canonical stamp. Pre-fix, None fell through
    to the stamp, permanently masking an existing collection as v7-migrated
    (the purge would never run; every future update short-circuits on
    UP_TO_DATE). Now: skip + deferral error → genuinely retried next update."""
    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: None
    )
    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    report = smr.run_schema_migrations(
        db_path=db_with_v033,
        project_id="p1",
        migrations_dir=_real_migrations_dir(),
        env={"CODE_GRAPH_PROJECT": "MyProj"},
        artifact_names={
            "codegraph_collection": smr.codegraph_class_names_for_prefix("MyProj")
        },
        include_orchestrator_wide=False,
        now_ms=1,
    )
    assert spy.count == 0, "unknown existence must NOT replay the ladder"
    # THE F1 assertion: nothing may be registered — a stamp here would be
    # permanent (UP_TO_DATE short-circuits all future runs + the reconcile).
    conn = sqlite3.connect(str(db_with_v033))
    rows = conn.execute(
        "SELECT COUNT(*) FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchone()[0]
    conn.close()
    assert rows == 0, (
        "unknown existence must NOT born-at-canonical stamp (permanent mask); "
        f"found {rows} registered codegraph rows"
    )
    # And the skip is surfaced as a deferral-shaped error so the next update
    # retries it visibly.
    assert any(
        "probe_unreachable" in d or "unreachable" in d.lower()
        for (_t, _n, d) in report.errors
    ), f"expected an unreachable-probe deferral error; got {report.errors}"


# ---------------------------------------------------------------------------
# A3 — one-time reconcile over a fake launcher.db
# ---------------------------------------------------------------------------


def test_a3_reconcile_stale_row_runs_ladder(db_with_v033, monkeypatch):
    """A stale registry row + collection-has-rows → reconcile replays the ladder
    from earliest + registers canonical."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    earliest = edges[0].from_version
    # Stale registry row (behind canonical) for the codegraph collection.
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type='codegraph_collection'",
        (earliest,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: True
    )
    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_APPLIED, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)

    outcome = cgrr.reconcile_codegraph_registry(
        None,  # no deferral report
        db_path=db_with_v033,
        weaviate_url="http://localhost:8081",
        migrations_dir=_real_migrations_dir(),
        env={},  # NO CODE_GRAPH_PROJECT — reconcile injects the SSOT prefix
        now_ms=1,
    )

    assert outcome.reconciled == [("p1", "MyProj")]
    assert not outcome.deferred
    # Each edge subprocess got CODE_GRAPH_PROJECT=MyProj (SSOT-injected).
    for _edge, env in spy.calls:
        assert env.get("CODE_GRAPH_PROJECT") == "MyProj"
    # Registry advanced to canonical.
    conn = sqlite3.connect(str(db_with_v033))
    ver = conn.execute(
        "SELECT DISTINCT schema_version FROM artifact_schema_versions "
        "WHERE artifact_type='codegraph_collection'"
    ).fetchall()
    conn.close()
    assert ver == [(canonical,)]


def test_a3_reconcile_already_current_is_noop(db_with_v033, monkeypatch):
    """A project already at canonical → already_current, no ladder replay."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    outcome = cgrr.reconcile_codegraph_registry(
        None,
        db_path=db_with_v033,
        migrations_dir=_real_migrations_dir(),
        env={},
        now_ms=1,
    )
    assert outcome.already_current == [("p1", "MyProj")]
    assert spy.count == 0


def test_a3_reconcile_no_data_skips(db_with_v033, monkeypatch):
    """A stale row but the collection has no data → skipped_no_data (nothing to
    reconcile; the forward NEVER_MATERIALIZED path handles a born-fresh one)."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type='codegraph_collection'",
        (edges[0].from_version,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: False
    )
    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    outcome = cgrr.reconcile_codegraph_registry(
        None,
        db_path=db_with_v033,
        migrations_dir=_real_migrations_dir(),
        env={},
        now_ms=1,
    )
    assert outcome.skipped_no_data == [("p1", "MyProj")]
    assert spy.count == 0


def test_a3_reconcile_disabled_binding_skipped(db_with_v033, monkeypatch):
    """A codegraph binding with enabled=0 (user turned code-graph off) is NOT
    reconciled."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj", enabled=0)
    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: True
    )
    spy = _EnvCapturingEdgeSpy()
    monkeypatch.setattr(smr, "_apply_edge", spy)
    outcome = cgrr.reconcile_codegraph_registry(
        None,
        db_path=db_with_v033,
        migrations_dir=_real_migrations_dir(),
        env={},
        now_ms=1,
    )
    assert outcome.reconciled == []
    assert spy.count == 0


def test_a3_reconcile_deferred_on_no_prefix_sentinel(db_with_v033, monkeypatch):
    """If an edge reports EDGE_NOOP_NO_PREFIX during reconcile, the project is
    DEFERRED (not reconciled) — the HIGH-2 gate protects the reconcile too."""
    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    canonical = sv.canonical_version("codegraph_collection")
    edges = smr.discover_edges(_real_migrations_dir(), "codegraph_collection")
    for suffix in smr._CODEGRAPH_CLASS_SUFFIXES:
        avr.register_artifact_version(
            db_with_v033, project_id="p1", artifact_type="codegraph_collection",
            artifact_name=f"MyProj_{suffix}", schema_version=canonical,
            materialized_at=1,
        )
    conn = sqlite3.connect(str(db_with_v033))
    conn.execute(
        "UPDATE artifact_schema_versions SET schema_version = ? "
        "WHERE artifact_type='codegraph_collection'",
        (edges[0].from_version,),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        smr, "_codegraph_collection_has_rows", lambda url, names: True
    )
    spy = _EnvCapturingEdgeSpy(stdout=smr._EDGE_SENTINEL_NOOP_NO_PREFIX, ok=True)
    monkeypatch.setattr(smr, "_apply_edge", spy)

    outcome = cgrr.reconcile_codegraph_registry(
        None,
        db_path=db_with_v033,
        migrations_dir=_real_migrations_dir(),
        env={},
        now_ms=1,
    )
    assert outcome.reconciled == []
    assert len(outcome.deferred) == 1
    assert outcome.deferred[0][0] == "p1"


# ---------------------------------------------------------------------------
# launcher_db_reader.get_codegraph_prefix (A1 SSOT read)
# ---------------------------------------------------------------------------


def test_get_codegraph_prefix_by_id_and_name(db_with_v033, monkeypatch):
    from vco_lib import launcher_db_reader as ldr

    _insert_codegraph_binding(db_with_v033, "p1", "MyProj")
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db_with_v033))
    assert ldr.get_codegraph_prefix("p1") == "MyProj"
    assert ldr.get_codegraph_prefix("MyProj") == "MyProj"  # by name
    assert ldr.get_codegraph_prefix("myproj") == "MyProj"  # by slug
    assert ldr.get_codegraph_prefix("nope") is None
