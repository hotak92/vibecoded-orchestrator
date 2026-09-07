# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.76 (R8): shared-KG canonical pointer-drift heal.

`app_state.orchestrator_root_kg_collection` (read by the access seeder) can
diverge from `app_state.last_installed_shared_kg_collection` (the real
canonical shared collection) on a white-label / rebind install. The seeder
then mints `kg_collection_access` rows for a class that doesn't exist in
Weaviate — a dead peer in every hybrid_search fan-out, re-broken by each update.

`heal_shared_kg_pointer_drift` converges the pointer (divergence + TRIPLE
agreement) and rewrites stale access rows. launcher.db metadata ONLY — the heal
makes NO Weaviate calls itself (existence is passed in as a set) and NEVER
writes Weaviate objects / enqueues syncs / touches embed_revision.

Tests:
  * ACT   divergent + triple agreement → converged, seed rows rewritten, KPI>0.
  * LEAVE default install (ptr == last) → strict no-op.
  * LEAVE white-label custom name that EXISTS (ptr == last) → no-op.
  * DEFER divergent WITHOUT agreement (dead last-value class) → untouched.
  * LEAVE user-configured access row (created_at != updated_at) → not rewritten.
  * IDEMPOTENT re-run on an already-converged DB → 0 changes.
  * NO-WEAVIATE: the heal issues zero DB/HTTP Weaviate mutations.
  * WIRING: install.py --update reaches the heal; bundle-update wires it too.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import (  # noqa: E402
    create_empty_launcher_db,
)
from vco_lib.kg_binding_heal import heal_shared_kg_pointer_drift  # noqa: E402


class _Sink:
    def __init__(self):
        self.entries = []

    def add_entry(self, entry):
        self.entries.append(entry)


class _Entry:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _log_collector():
    logs = []

    def _log(step, level, msg, **kw):
        logs.append((level, msg))

    return logs, _log


def _make_db(tmp_path) -> Path:
    """A REAL-schema launcher.db (shipped migrations), zero rows.

    The three tables the heal touches — ``app_state``,
    ``project_kg_bindings``, ``kg_collection_access`` — now have their true
    shape rather than the four-column guess this replaced; ``projects`` is
    present but EMPTY, which is what the ptr != last tests below need (the
    root-scoped sweep returns 0 for "no orchestrator_root row" exactly as it
    did for "no projects table").
    """
    return create_empty_launcher_db(tmp_path / "launcher.db")


def _add_project(cur, pid: str, name: str, host: str) -> None:
    """One ``projects`` row on the CALLER'S open cursor.

    Named-column INSERT: the real table has nine columns, not the three the
    old inline DDL declared, so a positional ``VALUES (...)`` no longer
    matches. ``folder_path`` is UNIQUE and is never dereferenced by the heal
    (it only reads ``id`` / ``host``), so a per-pid synthetic path is enough.

    Takes a cursor, not a path, because every call site is mid-transaction on
    a connection it commits itself — a second connection from the shared
    ``add_project`` helper would contend with that open write.
    """
    cur.execute(
        "INSERT INTO projects (id, name, folder_path, host, slug, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1)",
        (pid, name, f"/vct-heal-fixture/{pid}", host, pid),
    )


def _add_kg_binding(cur, pid: str, role: str, collection: str) -> None:
    """One ``project_kg_bindings`` row on the caller's open cursor (named
    columns — the real table has nine, the old guess had four)."""
    cur.execute(
        "INSERT INTO project_kg_bindings "
        "(project_id, role, collection_name, updated_at) VALUES (?, ?, ?, 1)",
        (pid, role, collection),
    )


def _add_access(
    cur, pid: str, collection: str, level: str, created: int, updated: int,
) -> None:
    """One ``kg_collection_access`` row on the caller's open cursor.

    ``created_at == updated_at`` marks a SEED-authored row; differing values
    mark a user-configured one. That distinction is the thing several tests
    below turn on, so both are always passed explicitly.
    """
    cur.execute(
        "INSERT INTO kg_collection_access (project_id, collection_name, "
        "access_level, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (pid, collection, level, created, updated),
    )


def _set_state(cur, key, value):
    cur.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _get_state(cur, key):
    cur.execute("SELECT value FROM app_state WHERE key = ?", (key,))
    r = cur.fetchone()
    return r[0] if r else None


def test_divergent_triple_agreement_converges(tmp_path):
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"  # machine default (ptr)
    LIVE = "AcmeCorp_KnowledgeGraph"               # canonical (last)
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    # shared-binding consensus agrees with LIVE.
    _add_kg_binding(cur, "p1", "shared", LIVE)
    # seed-authored access row at the dead pointer name (created==updated).
    ts = 1000
    _add_access(cur, "p1", DEAD, "read", ts, ts)
    conn.commit()

    logs, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()

    assert changed >= 2  # pointer + at least one access rewrite
    assert _get_state(cur, "orchestrator_root_kg_collection") == LIVE
    # The dead access row is gone; a LIVE-named row exists.
    cur.execute("SELECT collection_name FROM kg_collection_access WHERE project_id='p1'")
    names = {r[0] for r in cur.fetchall()}
    assert DEAD not in names and LIVE in names
    assert any("[kg-heal] converged" in m for _, m in logs)
    conn.close()


def test_default_install_no_op(tmp_path):
    """ptr == last (default install) → strict no-op, no deferral."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEFAULT = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEFAULT)
    _set_state(cur, "last_installed_shared_kg_collection", DEFAULT)
    conn.commit()

    sink = _Sink()
    logs, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={DEFAULT}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    assert changed == 0
    assert sink.entries == []
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEFAULT
    conn.close()


def test_white_label_matching_no_op(tmp_path):
    """A white-label custom name where ptr == last → no-op (keys agree)."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    NAME = "AcmeCorp_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", NAME)
    _set_state(cur, "last_installed_shared_kg_collection", NAME)
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={NAME}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    assert changed == 0 and sink.entries == []
    conn.close()


def test_divergent_without_agreement_defers(tmp_path):
    """Divergent but the last-value class does NOT exist in Weaviate → touch
    nothing + emit a deferral."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD_PTR = "VibeCodedOrchestrator_KnowledgeGraph"
    GHOST_LAST = "TypoName_KnowledgeGraph"  # not in existing_classes
    _set_state(cur, "orchestrator_root_kg_collection", DEAD_PTR)
    _set_state(cur, "last_installed_shared_kg_collection", GHOST_LAST)
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={DEAD_PTR}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    assert changed == 0
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEAD_PTR
    assert len(sink.entries) == 1
    assert sink.entries[0].condition_id == "shared_kg_pointer_drift_unresolved"
    conn.close()


def test_zero_shared_rows_is_not_no_consensus(tmp_path):
    """v0.2.92 (m-R6-4): a root-only machine — the orchestrator root binds
    role='primary' only, so before a SECOND project is registered there are
    ZERO role='shared' rows — must not read "no single-collection consensus
    (found [])" as drift. Absence is not disagreement: `last` recorded and
    its class live carry the convergence.

    MUT target: restore the `shared_consensus is None` reason leg without
    the `and shared_names` guard and this goes red (a fresh install nags
    again)."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEFAULT = "VibeCodedOrchestrator_KnowledgeGraph"  # migration 028's seed
    LIVE = "VCODev_KnowledgeGraph"                    # recorded canonical
    _set_state(cur, "orchestrator_root_kg_collection", DEFAULT)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    # NO project_kg_bindings rows at all — the fresh root-only shape.
    conn.commit()

    sink = _Sink()
    logs, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={DEFAULT, LIVE}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert sink.entries == [], (
        f"zero shared rows must not nag: {[e.__dict__ for e in sink.entries]}"
    )
    assert _get_state(cur, "orchestrator_root_kg_collection") == LIVE
    assert changed >= 1
    assert any("[kg-heal] converged" in m for _, m in logs)
    conn.close()


def test_absent_last_is_not_drift(tmp_path):
    """v0.2.92 (m-R6-4): migration 028 seeds `ptr` with the machine default
    on EVERY fresh DB while only a successful KG-seed step writes `last` —
    ptr-set/last-absent is a brand-new machine's ordinary shape (sync failed
    to launch, rebind deferred, Weaviate down at seed time). Nothing is
    recorded to diverge FROM, so: leave-alone, no nag.

    MUT target: restore the "last_installed_shared_kg_collection is empty"
    reason leg and this goes red."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEFAULT = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEFAULT)
    # last_installed_shared_kg_collection: deliberately UNSET.
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={DEFAULT}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert changed == 0
    assert sink.entries == []
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEFAULT
    conn.close()


def test_absent_ptr_converges_to_recorded_last(tmp_path):
    """The symmetric absence: `last` recorded + live, `ptr` never written
    (pre-028 DB, or the converge write soft-failed). With zero shared rows
    the two remaining legs agree → converge, no nag."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    LIVE = "AcmeCorp_KnowledgeGraph"
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    # orchestrator_root_kg_collection: deliberately UNSET.
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert sink.entries == []
    assert _get_state(cur, "orchestrator_root_kg_collection") == LIVE
    assert changed >= 1
    conn.close()


def test_genuine_ambiguity_still_defers(tmp_path):
    """The guard that remains: two DIFFERENT shared-binding names is real
    ambiguity, with or without this fix — defer to the user."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    LIVE = "AcmeCorp_KnowledgeGraph"
    OTHER = "WhiteLabel_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    _add_project(cur, "p1", "One", "base")
    _add_project(cur, "p2", "Two", "base")
    _add_kg_binding(cur, "p1", "shared", LIVE)
    _add_kg_binding(cur, "p2", "shared", OTHER)
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE, OTHER}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert changed == 0
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEAD
    assert len(sink.entries) == 1
    assert sink.entries[0].condition_id == "shared_kg_pointer_drift_unresolved"
    conn.close()


def test_consensus_disagreeing_with_last_still_defers(tmp_path):
    """The other retained guard: a single-name consensus that does NOT match
    `last` is evidence AGAINST converging — defer."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    LIVE = "AcmeCorp_KnowledgeGraph"
    CONSENSUS = "WhiteLabel_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    _add_project(cur, "p1", "One", "base")
    _add_kg_binding(cur, "p1", "shared", CONSENSUS)
    conn.commit()

    sink = _Sink()
    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE, CONSENSUS}, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert changed == 0
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEAD
    assert [e.condition_id for e in sink.entries] == [
        "shared_kg_pointer_drift_unresolved",
    ]
    conn.close()


def test_user_configured_access_row_left(tmp_path):
    """A user-configured access row (created_at != updated_at) at the dead name
    is NOT rewritten; the pointer still converges."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    LIVE = "AcmeCorp_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    _add_kg_binding(cur, "p1", "shared", LIVE)
    # user-configured row (created != updated).
    _add_access(cur, "p1", DEAD, "write", 100, 200)
    conn.commit()

    _, log = _log_collector()
    heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert _get_state(cur, "orchestrator_root_kg_collection") == LIVE
    # The user-configured dead row survives (not auto-rewritten).
    cur.execute(
        "SELECT COUNT(*) FROM kg_collection_access "
        "WHERE project_id='p1' AND collection_name=?", (DEAD,)
    )
    assert cur.fetchone()[0] == 1
    conn.close()


def test_idempotent_on_already_converged(tmp_path):
    """Running twice: the second run (already converged, ptr==last) → 0."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    LIVE = "AcmeCorp_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    _add_kg_binding(cur, "p1", "shared", LIVE)
    ts = 1000
    _add_access(cur, "p1", DEAD, "read", ts, ts)
    conn.commit()
    _, log = _log_collector()
    heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()
    second = heal_shared_kg_pointer_drift(
        cur, existing_classes={LIVE}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    assert second == 0
    conn.close()


def test_heal_makes_zero_weaviate_calls(tmp_path):
    """No-re-embed / no-Weaviate-mutation guard: the heal takes existing_classes
    as a plain set and must issue ZERO Weaviate HTTP calls. We assert this by
    patching urllib.request.urlopen to fail loudly if the heal calls it."""
    import urllib.request

    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    LIVE = "AcmeCorp_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", LIVE)
    _add_kg_binding(cur, "p1", "shared", LIVE)
    conn.commit()

    calls = []
    orig = urllib.request.urlopen

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("heal must not touch Weaviate over HTTP")

    urllib.request.urlopen = _boom
    try:
        _, log = _log_collector()
        heal_shared_kg_pointer_drift(
            cur, existing_classes={LIVE}, log_event=log,
            deferral_report=_Sink(), deferral_entry_cls=_Entry,
        )
    finally:
        urllib.request.urlopen = orig
    assert calls == []
    conn.close()


# ─── Wiring guards (source-level: the heal is reachable on both surfaces) ────


# ─── v0.2.77 Part 2 (5b): converged-pointer dead-row sweep ───────────────────
#
# Even when the shared-KG pointer is fully converged (ptr == last, canonical
# class live), a dead orchestrator-root OWN-PRIMARY access row can survive from
# pre-fix launcher builds that seeded the literal
# `sanitize(ORCHESTRATOR_ROOT_NAME)_KnowledgeGraph`. The `ptr != last` branch
# never reaches it. These tests pin the scoped sweep.


def _make_db_with_projects(tmp_path) -> Path:
    """Same DB as :func:`_make_db`.

    Kept as a distinct name because it marks INTENT at the call site: these
    tests go on to seed an orchestrator-root ``projects`` row so the
    root-scoped sweep has something to find. Under the real schema
    ``projects`` always exists, so there is no longer a second CREATE to do —
    the only difference between the two groups is the row, not the table.
    """
    return _make_db(tmp_path)


def test_converged_pointer_sweeps_dead_root_own_primary_row(tmp_path):
    """ACT: ptr == last (converged) but the ROOT project holds a seed-authored
    own-primary row at the DEAD literal name → it is rewritten to canonical."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"        # live canonical (ptr == last)
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"  # dead literal-derived name
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    ts = 1000
    # Dead seed-authored own-primary row (created == updated).
    _add_access(cur, "root", DEAD, "write", ts, ts)
    # A live own-dev row must be LEFT alone.
    _add_access(
        cur, "root", "VibeCodedOrchestrator_Development", "write", ts, ts,
    )
    conn.commit()

    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON, "VibeCodedOrchestrator_Development"}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()

    assert changed == 1  # exactly the dead own-primary row rewritten
    cur.execute("SELECT collection_name FROM kg_collection_access WHERE project_id='root'")
    names = {r[0] for r in cur.fetchall()}
    assert DEAD not in names
    assert CANON in names
    # Dev row untouched (it's a live class).
    assert "VibeCodedOrchestrator_Development" in names
    conn.close()


def test_converged_pointer_sweep_pk_conflict_keeps_higher_privilege(tmp_path):
    """The root already has a canonical-named seed row AND a dead one → merge
    keeps the HIGHER privilege, deletes the dead row (PK-conflict path)."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    ts = 1000
    # Canonical row already present at 'read'; dead row at 'write' (higher).
    _add_access(cur, "root", CANON, "read", ts, ts)
    _add_access(cur, "root", DEAD, "write", ts, ts)
    conn.commit()

    _, log = _log_collector()
    heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()

    cur.execute("SELECT access_level FROM kg_collection_access WHERE project_id='root' AND collection_name=?", (CANON,))
    assert cur.fetchone()[0] == "write"  # higher privilege kept
    cur.execute("SELECT COUNT(*) FROM kg_collection_access WHERE project_id='root' AND collection_name=?", (DEAD,))
    assert cur.fetchone()[0] == 0  # dead row deleted
    conn.close()


def test_converged_pointer_sweep_leaves_non_root_dead_row(tmp_path):
    """LEAVE-ALONE: a NON-root project's own collection that is legitimately
    absent from Weaviate (e.g. not yet bootstrapped) must NOT be swept."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    _add_project(cur, "p1", "Acme", "base")
    ts = 1000
    # A base project's own KG not yet in Weaviate — the LEAVE-ALONE case.
    _add_access(cur, "p1", "Acme_KnowledgeGraph", "write", ts, ts)
    conn.commit()

    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    assert changed == 0  # base project's absent own-KG untouched
    cur.execute("SELECT COUNT(*) FROM kg_collection_access WHERE project_id='p1' AND collection_name='Acme_KnowledgeGraph'")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_converged_pointer_sweep_leaves_root_grant_to_absent_peer(tmp_path):
    """LEAVE-ALONE (v0.2.77 L2-1): the ROOT project may hold a cross-project
    GRANT to a peer collection (e.g. ClientA_KnowledgeGraph) with fresh
    timestamps (created_at == updated_at — a GUI insert, indistinguishable
    from a seed row). If that peer collection is merely absent from Weaviate
    at heal time, the sweep must NOT consume the grant — only the exact
    known-dead own-primary literal (VibeCodedOrchestrator_KnowledgeGraph) is
    swept. Pre-fix, the endswith('_KnowledgeGraph') match would have deleted
    this grant with no undo trail."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"
    PEER = "ClientA_KnowledgeGraph"  # a peer project's KG, absent from Weaviate
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    ts = 1000
    # Root granted read access to ClientA's KG via the GUI (fresh insert,
    # created == updated). ClientA's class is NOT in existing_classes.
    _add_access(cur, "root", PEER, "read", ts, ts)
    conn.commit()

    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()

    assert changed == 0, "a root grant to an absent peer collection must not be swept"
    cur.execute(
        "SELECT collection_name, access_level FROM kg_collection_access "
        "WHERE project_id='root' AND collection_name=?",
        (PEER,),
    )
    row = cur.fetchone()
    assert row is not None, "the peer grant must survive"
    assert row[1] == "read", "the grant's access level must be intact"
    conn.close()


def test_converged_pointer_sweep_leaves_user_configured_root_row(tmp_path):
    """LEAVE-ALONE: a USER-configured (created_at != updated_at) dead root row
    is reported but NOT auto-rewritten."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    # user-configured (created != updated).
    _add_access(cur, "root", DEAD, "write", 100, 200)
    conn.commit()

    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    assert changed == 0
    cur.execute("SELECT COUNT(*) FROM kg_collection_access WHERE project_id='root' AND collection_name=?", (DEAD,))
    assert cur.fetchone()[0] == 1  # user row survives
    conn.close()


def test_converged_pointer_sweep_idempotent(tmp_path):
    """IDEMPOTENT: after the sweep heals the dead root row, a second run finds
    none → 0 changes, no writes."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    ts = 1000
    _add_access(cur, "root", DEAD, "write", ts, ts)
    conn.commit()

    _, log = _log_collector()
    heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    conn.commit()
    second = heal_shared_kg_pointer_drift(
        cur, existing_classes={CANON}, log_event=log,
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    assert second == 0
    conn.close()


def test_converged_pointer_sweep_no_op_when_canonical_class_absent(tmp_path):
    """LEAVE-ALONE: ptr == last but the canonical class is NOT live → the sweep
    does not run (can't safely rewrite onto a dead target), dead row survives."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CANON = "VCODev_KnowledgeGraph"  # NOT in existing_classes below
    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CANON)
    _set_state(cur, "last_installed_shared_kg_collection", CANON)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    ts = 1000
    _add_access(cur, "root", DEAD, "write", ts, ts)
    conn.commit()

    _, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes=set(), log_event=log,  # canonical NOT live
        deferral_report=_Sink(), deferral_entry_cls=_Entry,
    )
    assert changed == 0
    cur.execute("SELECT COUNT(*) FROM kg_collection_access WHERE project_id='root' AND collection_name=?", (DEAD,))
    assert cur.fetchone()[0] == 1
    conn.close()


def test_install_update_wires_pointer_heal():
    """install.py's --update self-heal detects pointer divergence AND the RW
    pass calls the pointer heal (via self_heal_kg_bindings pass 5)."""
    install_src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    # The RO-detection triggers the RW pass on app_state divergence.
    assert "last_installed_shared_kg_collection" in install_src
    assert "_converge_orchestrator_root_kg_pointer" in install_src
    heal_src = (REPO_ROOT / "vco_lib" / "kg_binding_heal.py").read_text(encoding="utf-8")
    assert "heal_shared_kg_pointer_drift(" in heal_src
    # self_heal_kg_bindings invokes it as pass 5.
    assert heal_src.count("heal_shared_kg_pointer_drift(") >= 2


def test_bundle_update_wires_pointer_heal():
    """install-bundle --update reaches the machine-wide pointer heal."""
    src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
    assert "_bundle_update_pointer_heal" in src
    assert "heal_shared_kg_pointer_drift" in src


# ─── v0.2.92 remedy fix: following the printed remedy clears the entry ───────
#
# The drift deferral's printed remedy named the Identity-tab picker, but the
# picker command writes ONLY app_state['shared_kg.collection_name'] — a THIRD
# key that nothing in the convergence path read. A user could follow the
# remedy to the letter and the entry would stand until an install run
# happened to converge via triple agreement (instance #12: a remedy that
# cannot work for the population it is printed for). The picker leg added to
# `heal_shared_kg_pointer_drift` makes the pick — when it names a LIVE class —
# the explicit canonical choice. These tests EXERCISE the remedy end to end.


def test_printed_remedy_resolves_the_entry(tmp_path):
    """EXERCISE the printed remedy: drift → entry emitted → user picks in the
    Identity tab → re-runs install --update (the heal) → entry is GONE and the
    convergence survives a third run.

    The drift shape is deliberately one where triple agreement FAILS (a
    nonempty shared-binding set with no single `last`-matching consensus), so
    pre-fix, no install run would ever auto-converge it: the pick is the ONLY
    thing that clears this state. MUT target: delete the picker leg from
    `heal_shared_kg_pointer_drift` and step 3 re-emits the entry instead of
    converging — this test goes red.
    """
    from vco_lib.kg_binding_heal import pointer_drift_needs_rw

    DEAD = "VibeCodedOrchestrator_KnowledgeGraph"   # ptr (machine default)
    OLD = "OldCanonical_KnowledgeGraph"             # last (live, stale)
    OTHER = "OtherProject_KnowledgeGraph"           # breaks the consensus leg
    NEW = "AcmeCorp_KnowledgeGraph"                 # the user's pick (live)

    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    _set_state(cur, "orchestrator_root_kg_collection", DEAD)
    _set_state(cur, "last_installed_shared_kg_collection", OLD)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    _add_kg_binding(cur, "root", "shared", OLD)   # root's shared binding
    _add_kg_binding(cur, "p1", "shared", OTHER)   # → consensus {OLD, OTHER}
    ts = 1000
    _add_access(cur, "root", DEAD, "read", ts, ts)   # seed-authored at dead ptr
    _add_access(cur, "p1", DEAD, "write", 100, 200)  # user-configured (PK
    #                                                 is (project, collection))
    conn.commit()

    # 1. Install run on the drifted DB → the deferral fires with the remedy.
    sink1 = _Sink()
    heal_shared_kg_pointer_drift(
        cur, existing_classes={OLD, OTHER, NEW, DEAD}, log_event=_log_collector()[1],
        deferral_report=sink1, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert [e.condition_id for e in sink1.entries] == [
        "shared_kg_pointer_drift_unresolved",
    ]
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEAD

    # 2. The user follows the printed remedy: picks the canonical shared
    #    collection in the Identity tab. That command — Rust
    #    `set_shared_kg_collection_name` (project_identity.rs) — writes ONE
    #    app_state key and nothing else; mirror it exactly.
    _set_state(cur, "shared_kg.collection_name", NEW)
    conn.commit()

    # 3. Re-run `python install.py --update` → its step 7e/10 heal runs on a
    #    still-drifted DB (the pick did not touch the compared keys).
    assert pointer_drift_needs_rw(cur) is True  # RW pass is still owed
    sink2 = _Sink()
    logs, log = _log_collector()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={OLD, OTHER, NEW, DEAD}, log_event=log,
        deferral_report=sink2, deferral_entry_cls=_Entry,
    )
    conn.commit()

    #    The pick cleared it: no entry, all three pointers converged.
    assert sink2.entries == [], (
        f"following the remedy must clear the entry: "
        f"{[e.__dict__ for e in sink2.entries]}"
    )
    assert changed >= 3  # ptr + last + root shared binding (+ access rewrite)
    assert _get_state(cur, "orchestrator_root_kg_collection") == NEW
    assert _get_state(cur, "last_installed_shared_kg_collection") == NEW
    cur.execute(
        "SELECT collection_name FROM project_kg_bindings "
        "WHERE project_id='root' AND role='shared'"
    )
    assert cur.fetchone()[0] == NEW
    # Seed-authored dead-ptr row rewritten onto the pick; user row untouched.
    cur.execute(
        "SELECT collection_name FROM kg_collection_access "
        "WHERE project_id='root' AND created_at=updated_at"
    )
    assert cur.fetchone()[0] == NEW
    cur.execute(
        "SELECT COUNT(*) FROM kg_collection_access "
        "WHERE project_id='p1' AND collection_name=? AND created_at!=updated_at",
        (DEAD,),
    )
    assert cur.fetchone()[0] == 1
    assert any("Identity pick" in m for _, m in logs)

    # 4. The convergence SURVIVES the next install run: probe says no RW pass
    #    is owed, and a third heal is a no-op that emits nothing.
    assert pointer_drift_needs_rw(cur) is False
    sink3 = _Sink()
    third = heal_shared_kg_pointer_drift(
        cur, existing_classes={OLD, OTHER, NEW, DEAD}, log_event=log,
        deferral_report=sink3, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert third == 0
    assert sink3.entries == []
    conn.close()


def test_dead_pick_still_defers_and_names_the_pick(tmp_path):
    """A pick naming a class that is NOT live in Weaviate cannot be converged
    onto (the access seeder would seed rows for a nonexistent class): the
    entry stands, and its reasons NAME the dead pick so the user learns why
    following the remedy did not take."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    DEAD_PTR = "VibeCodedOrchestrator_KnowledgeGraph"
    OLD = "OldCanonical_KnowledgeGraph"
    PICK = "NotYetCreated_KnowledgeGraph"  # not in existing_classes
    _set_state(cur, "orchestrator_root_kg_collection", DEAD_PTR)
    _set_state(cur, "last_installed_shared_kg_collection", OLD)
    _set_state(cur, "shared_kg.collection_name", PICK)
    _add_kg_binding(cur, "p1", "shared", OLD)
    _add_kg_binding(cur, "p2", "shared", "Third_KnowledgeGraph")
    conn.commit()

    sink = _Sink()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={DEAD_PTR, OLD, "Third_KnowledgeGraph"},
        log_event=_log_collector()[1],
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert changed == 0
    assert [e.condition_id for e in sink.entries] == [
        "shared_kg_pointer_drift_unresolved",
    ]
    # The reason names the dead pick — the honest disclosure.
    assert PICK in sink.entries[0].detected
    assert _get_state(cur, "orchestrator_root_kg_collection") == DEAD_PTR
    assert _get_state(cur, "last_installed_shared_kg_collection") == OLD
    conn.close()


def test_pick_does_not_fire_without_drift(tmp_path):
    """Scope boundary: the picker leg lives in the DIVERGENT region only.
    With ptr == last there is no entry, and in production the RW pass is not
    even opened (pointer_drift_needs_rw gates it), so a divergent pick with
    no drift stays a no-op here. Locking the boundary so the leg cannot
    silently widen into an always-on write path."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    CURRENT = "AcmeCorp_KnowledgeGraph"
    NEW = "RePicked_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", CURRENT)
    _set_state(cur, "last_installed_shared_kg_collection", CURRENT)
    _set_state(cur, "shared_kg.collection_name", NEW)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    _add_kg_binding(cur, "root", "shared", CURRENT)
    conn.commit()

    sink = _Sink()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={CURRENT, NEW}, log_event=_log_collector()[1],
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert changed == 0
    assert sink.entries == []
    assert _get_state(cur, "orchestrator_root_kg_collection") == CURRENT
    conn.close()


def test_pick_equals_ptr_converges_last_and_binding(tmp_path):
    """The env-override shape: ptr already equals the pick (a white-label run
    set VCT_ORCHESTRATOR_ROOT_KG_COLLECTION) but `last` still diverges. The
    pick converges last + the root binding; no access rewrite runs (ptr was
    never dead)."""
    db = _make_db_with_projects(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    PICK = "WhiteLabel_KnowledgeGraph"
    OLD = "OldCanonical_KnowledgeGraph"
    _set_state(cur, "orchestrator_root_kg_collection", PICK)
    _set_state(cur, "last_installed_shared_kg_collection", OLD)
    _set_state(cur, "shared_kg.collection_name", PICK)
    _add_project(cur, "root", "VibeCoded Orchestrator", "orchestrator_root")
    _add_kg_binding(cur, "root", "shared", OLD)
    conn.commit()

    sink = _Sink()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes={PICK, OLD}, log_event=_log_collector()[1],
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    assert sink.entries == []
    assert _get_state(cur, "orchestrator_root_kg_collection") == PICK
    assert _get_state(cur, "last_installed_shared_kg_collection") == PICK
    cur.execute(
        "SELECT collection_name FROM project_kg_bindings "
        "WHERE project_id='root' AND role='shared'"
    )
    assert cur.fetchone()[0] == PICK
    assert changed == 3  # ptr-write + last-write + one binding rebind
    conn.close()


# ─── The RO gate's contract: True IFF the heal owes work ────────────────────
#
# `pointer_drift_needs_rw` is ORed into install.py's `needs_rebind`, so a
# False positive is not a wrong answer to the user — it is a writer-lock open
# (5s timeout when vct-hub holds it) to reach a branch that returns 0. The
# gate and the heal must agree on the SAME four shapes, so they are pinned
# together here: each case asserts the gate's verdict AND what the heal
# actually does under it.


def _gate_and_heal(tmp_path, ptr, last, *, existing):
    """(gate_verdict, changed, entry_ids) for one (ptr, last) shape."""
    from vco_lib.kg_binding_heal import pointer_drift_needs_rw

    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    if ptr is not None:
        _set_state(cur, "orchestrator_root_kg_collection", ptr)
    if last is not None:
        _set_state(cur, "last_installed_shared_kg_collection", last)
    conn.commit()
    gate = pointer_drift_needs_rw(cur)
    sink = _Sink()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes=existing, log_event=_log_collector()[1],
        deferral_report=sink, deferral_entry_cls=_Entry,
    )
    conn.commit()
    conn.close()
    return gate, changed, [e.condition_id for e in sink.entries]


def test_gate_false_when_last_absent_because_the_heal_no_ops(tmp_path):
    """ptr set, `last` absent — the fresh-machine shape (migration 028 seeds
    the pointer; only a successful KG seed records `last`). Since m-R6-4 the
    heal returns 0 there with no write and no deferral, so the gate must not
    open the writer lock for it. MUT: restore `bool(ptr or last)` and this
    goes red while the heal still returns 0 — the gate claiming owed work."""
    gate, changed, entries = _gate_and_heal(
        tmp_path, "VibeCodedOrchestrator_KnowledgeGraph", None,
        existing={"VibeCodedOrchestrator_KnowledgeGraph"},
    )
    assert changed == 0 and entries == []
    assert gate is False


def test_gate_true_when_ptr_absent_and_last_recorded(tmp_path):
    """The INVERSE shape still owes work: the heal converges the pointer onto
    the recorded canonical, so narrowing the gate must not swallow it."""
    gate, changed, entries = _gate_and_heal(
        tmp_path, None, "AcmeCorp_KnowledgeGraph",
        existing={"AcmeCorp_KnowledgeGraph"},
    )
    assert gate is True
    assert changed >= 1 and entries == []


def test_gate_true_on_a_genuine_divergence(tmp_path):
    gate, _changed, _entries = _gate_and_heal(
        tmp_path, "Old_KnowledgeGraph", "AcmeCorp_KnowledgeGraph",
        existing={"Old_KnowledgeGraph", "AcmeCorp_KnowledgeGraph"},
    )
    assert gate is True


def test_gate_false_on_an_untouched_fresh_launcher_db(tmp_path):
    """The population this tightening is FOR, taken verbatim from a real
    fresh DB: `create_empty_launcher_db` applies the shipped migrations, and
    they seed `orchestrator_root_kg_collection` with the machine default
    while nothing writes `last`. So EVERY first `install.py --update` on a
    machine whose KG seed has not yet succeeded hit the old gate's True and
    opened the writer lock to reach a branch that returns 0."""
    from vco_lib.kg_binding_heal import pointer_drift_needs_rw

    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    seeded = _get_state(cur, "orchestrator_root_kg_collection")
    assert seeded, "fixture drifted: migrations no longer seed the pointer"
    assert not _get_state(cur, "last_installed_shared_kg_collection")
    assert pointer_drift_needs_rw(cur) is False
    conn.close()


def test_gate_false_when_nothing_is_recorded_or_when_they_agree(tmp_path):
    """Truly-empty app_state (older schema) and the converged default install
    are both leave-alone — unchanged by the tightening, pinned so a future
    edit cannot trade one no-op for another."""
    from vco_lib.kg_binding_heal import pointer_drift_needs_rw

    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("DELETE FROM app_state")
    conn.commit()
    assert pointer_drift_needs_rw(cur) is False
    conn.close()

    agree, changed2, entries2 = _gate_and_heal(
        tmp_path, "AcmeCorp_KnowledgeGraph", "AcmeCorp_KnowledgeGraph",
        existing={"AcmeCorp_KnowledgeGraph"},
    )
    assert agree is False and changed2 == 0 and entries2 == []
