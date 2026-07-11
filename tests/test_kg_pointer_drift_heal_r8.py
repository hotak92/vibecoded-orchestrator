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
    """A minimal launcher.db-shaped SQLite with the three tables the heal uses."""
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE app_state (
            key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
        );
        CREATE TABLE project_kg_bindings (
            project_id TEXT, role TEXT, collection_name TEXT, updated_at INTEGER,
            PRIMARY KEY (project_id, role)
        );
        CREATE TABLE kg_collection_access (
            project_id TEXT, collection_name TEXT, access_level TEXT,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, collection_name)
        );
        """
    )
    conn.commit()
    conn.close()
    return db


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
    cur.execute(
        "INSERT INTO project_kg_bindings VALUES ('p1', 'shared', ?, 1)", (LIVE,)
    )
    # seed-authored access row at the dead pointer name (created==updated).
    ts = 1000
    cur.execute(
        "INSERT INTO kg_collection_access VALUES ('p1', ?, 'read', ?, ?)",
        (DEAD, ts, ts),
    )
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
    cur.execute("INSERT INTO project_kg_bindings VALUES ('p1', 'shared', ?, 1)", (LIVE,))
    # user-configured row (created != updated).
    cur.execute(
        "INSERT INTO kg_collection_access VALUES ('p1', ?, 'write', 100, 200)", (DEAD,)
    )
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
    cur.execute("INSERT INTO project_kg_bindings VALUES ('p1', 'shared', ?, 1)", (LIVE,))
    ts = 1000
    cur.execute(
        "INSERT INTO kg_collection_access VALUES ('p1', ?, 'read', ?, ?)", (DEAD, ts, ts)
    )
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
    cur.execute("INSERT INTO project_kg_bindings VALUES ('p1', 'shared', ?, 1)", (LIVE,))
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
    """launcher.db shape including a `projects` table (host column), so the
    root-scoped sweep can identify the orchestrator-root project."""
    db = _make_db(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT, host TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    ts = 1000
    # Dead seed-authored own-primary row (created == updated).
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'write', ?, ?)", (DEAD, ts, ts))
    # A live own-dev row must be LEFT alone.
    cur.execute(
        "INSERT INTO kg_collection_access VALUES ('root', 'VibeCodedOrchestrator_Development', 'write', ?, ?)",
        (ts, ts),
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    ts = 1000
    # Canonical row already present at 'read'; dead row at 'write' (higher).
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'read', ?, ?)", (CANON, ts, ts))
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'write', ?, ?)", (DEAD, ts, ts))
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    cur.execute("INSERT INTO projects VALUES ('p1', 'Acme', 'base')")
    ts = 1000
    # A base project's own KG not yet in Weaviate — the LEAVE-ALONE case.
    cur.execute("INSERT INTO kg_collection_access VALUES ('p1', 'Acme_KnowledgeGraph', 'write', ?, ?)", (ts, ts))
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    ts = 1000
    # Root granted read access to ClientA's KG via the GUI (fresh insert,
    # created == updated). ClientA's class is NOT in existing_classes.
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'read', ?, ?)", (PEER, ts, ts))
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    # user-configured (created != updated).
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'write', 100, 200)", (DEAD,))
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    ts = 1000
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'write', ?, ?)", (DEAD, ts, ts))
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
    cur.execute("INSERT INTO projects VALUES ('root', 'VibeCoded Orchestrator', 'orchestrator_root')")
    ts = 1000
    cur.execute("INSERT INTO kg_collection_access VALUES ('root', ?, 'write', ?, ?)", (DEAD, ts, ts))
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
