# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the orchestrator root's KG identity is ONE decision
(``vco_lib.root_kg_identity``, owner ruling 2026-10-15).

The root owns the FOLDER-DERIVED name its install creates; an existing
populated binding is kept; a case-only sibling is never adopted on the fly
(it blocks creation and becomes a consent deferral); the white-label override
keeps top priority. install.py's step 7b, step 7c and the launcher.db pointer
persist all use it; the hub serves the bound name byte-for-byte
(``config_api.rs::root_primary_kg_is_never_case_rebound``).

Red-proofs (mutation, restored from a saved copy): in ``decide``, make the
KEEP branch always keep → the stale-model test fails; drop the blocked check →
the case-sibling tests fail; in install.py, revert step 7b to the env /
case-adopted name → the 7b test fails; revert the persist to override-only →
the persist test fails.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import install
from vco_lib import root_kg_identity as rki
from vco_lib.deferral_report import DeferralReport

ROOT = Path("/x/vibecoded-orchestrator")
DERIVED = "VibecodedOrchestrator_KnowledgeGraph"
CONST = "VibeCodedOrchestrator_KnowledgeGraph"


# ── decide (pure) ───────────────────────────────────────────────────────────


def test_fresh_install_takes_the_folder_derived_name():
    d = rki.decide(ROOT, None, {}, None, "qwen3-embedding:0.6b", "")
    assert (d.name, d.source, d.blocked_by, d.persistable) == (DERIVED, rki.SOURCE_DERIVED, None, True)


def test_the_override_wins_over_everything():
    d = rki.decide(ROOT, CONST, {CONST: 900}, None, None, "  Acme_KnowledgeGraph \n")
    assert (d.name, d.source) == ("Acme_KnowledgeGraph", rki.SOURCE_OVERRIDE)


def test_a_populated_bound_class_is_kept_even_when_it_is_not_the_derived_name():
    """An existing install bound to the shipped constant keeps its data."""
    d = rki.decide(ROOT, CONST, {CONST: 892, DERIVED: None}, "qwen3-embedding:0.6b",
                   "qwen3-embedding:0.6b", "")
    assert (d.name, d.source, d.persistable) == (CONST, rki.SOURCE_KEPT, True)


def test_unknown_model_keeps_the_bound_class():
    d = rki.decide(ROOT, CONST, {CONST: 5}, None, "qwen3-embedding:0.6b", "")
    assert d.name == CONST and d.source == rki.SOURCE_KEPT


def test_a_class_positively_written_with_another_model_is_left_for_the_derived_name():
    d = rki.decide(ROOT, CONST, {CONST: 5}, "snowflake-arctic-embed2", "qwen3-embedding:0.6b", "")
    assert (d.name, d.source) == (DERIVED, rki.SOURCE_DERIVED)
    assert "written with snowflake-arctic-embed2" in d.reason


def test_a_pending_fill_toward_the_profile_keeps_the_bound_class():
    d = rki.decide(ROOT, CONST, {CONST: 5}, "snowflake-arctic-embed2", "qwen3-embedding:0.6b",
                   "", pending_fill=True)
    assert (d.name, d.source) == (CONST, rki.SOURCE_KEPT)


def test_an_empty_bound_class_yields_the_derived_name():
    d = rki.decide(ROOT, CONST, {CONST: 0}, None, None, "")
    assert d.name == DERIVED and "holds no rows" in d.reason


def test_a_blind_read_keeps_the_bound_name_and_is_not_persistable():
    d = rki.decide(ROOT, CONST, None, None, None, "")
    assert (d.name, d.source, d.state_known, d.persistable) == (CONST, rki.SOURCE_UNKNOWN, False, False)
    d2 = rki.decide(ROOT, CONST, {CONST: None}, None, None, "")
    assert d2.source == rki.SOURCE_UNKNOWN and not d2.persistable


def test_a_case_only_sibling_blocks_the_derived_name_and_is_never_adopted():
    d = rki.decide(ROOT, None, {CONST: 892}, None, None, "")
    assert d.name == DERIVED, "the sibling is NOT adopted"
    assert d.blocked_by == CONST and not d.persistable


def test_a_case_only_sibling_also_blocks_the_override():
    d = rki.decide(ROOT, None, {"ACME_KG": 1}, None, None, "Acme_KG")
    assert d.blocked_by == "ACME_KG"


def test_the_exact_class_existing_is_not_blocked():
    d = rki.decide(ROOT, None, {DERIVED: 0, CONST: 10}, None, None, "")
    assert d.name == DERIVED and d.blocked_by is None


# ── gather (I/O edges injected) ─────────────────────────────────────────────


def test_gather_reads_the_binding_and_counts_only_the_bound_class(monkeypatch):
    counted: list = []
    monkeypatch.setattr(rki, "_model_facts", lambda b, t, u: (None, False))
    d = rki.gather(ROOT, weaviate_url="http://w", live_names=[CONST, "Other_KnowledgeGraph"],
                   count=lambda u, n: counted.append(n) or 42, bound=CONST, env={})
    assert counted == [CONST]
    assert d.name == CONST and d.source == rki.SOURCE_KEPT


def test_gather_without_a_schema_is_a_blind_read():
    d = rki.gather(ROOT, weaviate_url="http://127.0.0.1:9", live_names=None, bound=None, env={})
    assert not d.state_known and d.name == DERIVED


# ── the consent path ────────────────────────────────────────────────────────


def test_adopt_refuses_without_yes(capsys):
    assert rki.main(["adopt", "--collection", CONST]) == 2
    assert "explicit consent" in capsys.readouterr().err


def test_adopt_refuses_a_class_written_with_another_model(monkeypatch):
    monkeypatch.setattr(rki, "_live_class_names", lambda url: [CONST])
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: 9)
    monkeypatch.setattr("vco_lib.kg_embed_model.target_text_model", lambda env=None: "qwen3-embedding:0.6b")
    monkeypatch.setattr(rki, "_model_facts", lambda b, t, u: ("snowflake-arctic-embed2", False))
    monkeypatch.setattr(rki, "_write_adoption", lambda c, out=print: pytest.fail("adopted"))
    lines: list = []
    assert rki.adopt(ROOT, CONST, weaviate_url="http://w", out=lines.append) == 1
    assert "wrong vectors" in lines[0]


@pytest.mark.parametrize("names,count,why", [
    (None, 9, "did not answer"),
    (["Other"], 9, "no class named exactly"),
    ([CONST], 0, "holds no rows"),
])
def test_adopt_refuses_what_it_cannot_verify(monkeypatch, names, count, why):
    monkeypatch.setattr(rki, "_live_class_names", lambda url: names)
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: count)
    lines: list = []
    assert rki.adopt(ROOT, CONST, weaviate_url="http://w", out=lines.append) == 1
    assert why in lines[0]


def test_adopt_writes_the_pointer_and_both_root_bindings(monkeypatch, tmp_path):
    from tests.common.launcher_db_fixture import create_empty_launcher_db

    db = tmp_path / "launcher.db"
    create_empty_launcher_db(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at) "
                 "VALUES ('root', 'VCO', '/x', 'orchestrator_root', 'orchestrator-root', 0, 0)")
    for role in ("primary", "shared"):
        conn.execute("INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, "
                     "updated_at) VALUES ('root', ?, 'Old_KnowledgeGraph', '{}', 0)", (role,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.setattr(rki, "_live_class_names", lambda url: [CONST])
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: 9)
    monkeypatch.setattr("vco_lib.kg_embed_model.target_text_model", lambda env=None: "qwen3-embedding:0.6b")
    monkeypatch.setattr(rki, "_model_facts", lambda b, t, u: ("qwen3-embedding:0.6b", False))
    assert rki.adopt(ROOT, CONST, weaviate_url="http://w", out=lambda m: None) == 0
    conn = sqlite3.connect(str(db))
    try:
        ptr = conn.execute("SELECT value FROM app_state WHERE key = ?", (rki.POINTER_KEY,)).fetchone()
        rows = conn.execute("SELECT role, collection_name FROM project_kg_bindings "
                            "WHERE project_id = 'root' ORDER BY role").fetchall()
    finally:
        conn.close()
    assert ptr == (CONST,)
    assert rows == [("primary", CONST), ("shared", CONST)]


# ── install.py wiring ───────────────────────────────────────────────────────


class _FakeWeaviate:
    """Answers /v1/schema from ``classes`` and records schema POSTs."""

    def __init__(self, classes):
        self.classes, self.posted = list(classes), []

    def __call__(self, method, url, body=None, timeout=None, **_k):
        if method == "GET" and url.endswith("/v1/schema"):
            return 200, json.dumps({"classes": [{"class": c} for c in self.classes]}).encode()
        if method == "POST" and url.endswith("/v1/schema"):
            self.posted.append((body or {}).get("class"))
            return 200, b"{}"
        return 404, b""


@pytest.fixture
def root_install(monkeypatch, tmp_path):
    root = tmp_path / "vibecoded-orchestrator"
    root.mkdir()
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "_wait_for_weaviate_ready", lambda *a, **k: True)
    monkeypatch.setattr("vco_lib.launcher_db_reader.get_orchestrator_root_bindings", lambda: (None, None))
    for k in ("KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION", rki.OVERRIDE_ENV):
        monkeypatch.delenv(k, raising=False)
    # A polluted shell names another project's KG — the root must not take it.
    monkeypatch.setenv("KG_COLLECTION", "Other_KnowledgeGraph")
    monkeypatch.setenv("SHARED_KG_COLLECTION", CONST)
    return root


def test_step_7b_creates_the_folder_derived_root_class_as_both_roles(root_install, monkeypatch):
    fake = _FakeWeaviate([])
    monkeypatch.setattr(install._wh, "http_request", fake)
    monkeypatch.setattr(rki, "_live_class_names", lambda url: list(fake.classes))
    install._ensure_collections({})
    assert DERIVED in fake.posted and CONST not in fake.posted
    assert "Other_KnowledgeGraph" not in fake.posted
    assert os.environ["KG_COLLECTION"] == os.environ["SHARED_KG_COLLECTION"] == DERIVED


def test_step_7b_never_creates_beside_a_case_sibling_and_7c_defers(root_install, monkeypatch):
    fake = _FakeWeaviate([CONST])
    monkeypatch.setattr(install._wh, "http_request", fake)
    monkeypatch.setattr(rki, "_live_class_names", lambda url: list(fake.classes))
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: 892)
    install._ensure_collections({})
    assert DERIVED not in fake.posted and CONST not in fake.posted
    # The root's name stays the decided one — never the case-sibling's.
    assert os.environ["KG_COLLECTION"] == os.environ["SHARED_KG_COLLECTION"] == DERIVED

    report = DeferralReport()
    venv_py = root_install / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#")

    class _Args:
        update = True
        skip_seed = False

    status = install._seed_weaviate(_Args(), deferral_report=report)
    assert status == install._install_weaviate.SKIPPED
    (entry,) = [e for e in report.entries if e.condition_id == "root_kg_blocked_by_case_sibling"]
    assert f"adopt --collection {CONST} --yes" in entry.command_to_apply


def test_the_pointer_persist_writes_the_decided_name_over_the_028_default(root_install, monkeypatch, tmp_path):
    from tests.common.launcher_db_fixture import create_empty_launcher_db, set_app_state

    db = tmp_path / "state" / "launcher.db"
    db.parent.mkdir()
    create_empty_launcher_db(db)
    set_app_state(db, rki.POINTER_KEY, CONST)  # migration 028's default
    monkeypatch.setenv("VCT_STATE_DIR", str(db.parent))
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    monkeypatch.setattr(rki, "_live_class_names", lambda url: [DERIVED])
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: 0)
    install._persist_orchestrator_root_kg_collection(DeferralReport())
    conn = sqlite3.connect(str(db))
    try:
        (value,) = conn.execute("SELECT value FROM app_state WHERE key = ?", (rki.POINTER_KEY,)).fetchone()
    finally:
        conn.close()
    assert value == DERIVED


def test_the_root_rows_are_never_case_rebound_by_the_heal(tmp_path):
    """`kg_binding_heal` skips the root project's rows; a per-project row
    with the same shape is still healed (legacy behaviour kept). The helper
    leg; the wiring through install.py's heal entry is pinned below."""
    from vco_lib.kg_binding_heal import _rebind_collection_names_to_on_disk_casing, root_project_ids

    db = tmp_path / "launcher.db"
    _root_launcher_db(db, [("root", "primary", CONST), ("p", "primary", CONST)])
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    ids = root_project_ids(cur)
    assert ids == {"root"}
    done: list = []
    _rebind_collection_names_to_on_disk_casing(
        cur, table="project_kg_bindings", project_id_col="project_id",
        collection_name_col="collection_name",
        existing_classes={DERIVED}, existing_by_lower={DERIVED.lower(): DERIVED},
        extra_select_cols=("role",),
        do_rebind=lambda cur, **k: done.append(k["project_id"]),
        skip_row=lambda row: row[0] in ids)
    conn.close()
    assert done == ["p"]


def _root_launcher_db(db, rows, access=()):
    """A real-schema launcher.db whose project ``root`` is the orchestrator
    root (``projects.host``) and ``p`` an ordinary project; ``access`` seeds
    ``kg_collection_access`` (project_id, collection_name, access_level)."""
    from tests.common.launcher_db_fixture import create_empty_launcher_db

    create_empty_launcher_db(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO projects (id, name, folder_path, host, slug, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, 0)",
            [("root", "VCO", "/x/root", "orchestrator_root", "orchestrator-root"),
             ("p", "P", "/x/p", "base", "p")])
        conn.executemany(
            "INSERT INTO project_kg_bindings (project_id, role, collection_name, config_json, "
            "updated_at) VALUES (?, ?, ?, '{}', 0)", rows)
        conn.executemany(
            "INSERT INTO kg_collection_access (project_id, collection_name, access_level) "
            "VALUES (?, ?, ?)", access)
        conn.commit()
    finally:
        conn.close()


def _bindings(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT project_id, role, collection_name FROM project_kg_bindings "
                            "ORDER BY project_id, role").fetchall()
    finally:
        conn.close()


def _access(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT project_id, collection_name, access_level "
                            "FROM kg_collection_access ORDER BY project_id, collection_name").fetchall()
    finally:
        conn.close()


def _heal_against(monkeypatch, tmp_path, classes, counts, rows, access=()):
    """Run install.py's real heal entry (``_self_heal_kg_bindings_on_update``)
    against a launcher.db and a Weaviate stub."""
    db = tmp_path / "state" / "launcher.db"
    db.parent.mkdir()
    _root_launcher_db(db, rows, access)
    monkeypatch.setenv("VCT_STATE_DIR", str(db.parent))
    monkeypatch.delenv("VCT_LAUNCHER_DB_PATH", raising=False)
    from tests.test_self_heal_cross_prefix_adopt import _start_stub_weaviate

    server, port = _start_stub_weaviate(classes=classes, counts=counts)
    try:
        monkeypatch.setenv("WEAVIATE_URL", f"http://127.0.0.1:{port}")
        install._self_heal_kg_bindings_on_update(DeferralReport())
    finally:
        server.shutdown()
    return _bindings(db)


def test_the_update_heal_never_case_rebinds_the_root_rows(monkeypatch, tmp_path):
    """Wiring, through install.py's heal entry: the root's row naming a class
    of which only a case-sibling exists stays as bound; an ordinary
    project's identical row is still case-rebound (legacy heal kept).
    Red-proof: drop ``skip_row=`` at the binding call site in
    ``self_heal_kg_bindings`` → the root row is rebound."""
    rows = [("root", "primary", CONST), ("p", "shared", CONST)]
    assert _heal_against(monkeypatch, tmp_path, [DERIVED], {DERIVED: 5}, rows) == [
        ("p", "shared", DERIVED), ("root", "primary", CONST)]


def test_the_update_heal_never_prefix_adopts_onto_the_root_rows(monkeypatch, tmp_path):
    """Wiring: the root's row naming a missing class is NOT prefix-adopted onto
    the lone populated ``*_KnowledgeGraph`` (a class the root did not write);
    an ordinary project's identical row still is. Red-proof: drop
    ``skip_project_ids=`` at the prefix-adopt call site → the root row is
    adopted."""
    rows = [("root", "primary", "Missing_KnowledgeGraph"), ("p", "primary", "Gone_KnowledgeGraph")]
    got = _heal_against(monkeypatch, tmp_path, ["Other_KnowledgeGraph"],
                        {"Other_KnowledgeGraph": 40}, rows)
    assert got == [("p", "primary", "Other_KnowledgeGraph"),
                   ("root", "primary", "Missing_KnowledgeGraph")]


def test_the_access_heal_skips_only_the_roots_grant_on_its_own_class(monkeypatch, tmp_path):
    """R24 MEDIUM-1 — both arms, through install.py's heal entry. The root's
    grant on its OWN class (named by its binding rows) is its identity and is
    never case-rebound; the root's grant on ANOTHER project's collection whose
    on-disk casing differs is case-healed exactly like any other project's
    (pre-v0.2.103 behaviour). Red-proof: revert the access-pass ``skip_row``
    to ``row[0] in _root_ids`` → the root's ``Widget_KnowledgeGraph`` grant
    stays stale."""
    rows = [("root", "primary", CONST), ("root", "shared", CONST)]
    access = [("root", CONST, "write"), ("root", "Widget_KnowledgeGraph", "read"),
              ("p", "Widget_KnowledgeGraph", "read")]
    db = tmp_path / "state" / "launcher.db"
    _heal_against(monkeypatch, tmp_path, [DERIVED, "WIDGET_KnowledgeGraph"],
                  {DERIVED: 5, "WIDGET_KnowledgeGraph": 7}, rows, access)
    got = sorted(r for r in _access(db) if r[1].endswith("_KnowledgeGraph"))
    assert got == sorted([
        ("p", "WIDGET_KnowledgeGraph", "read"),
        ("root", "WIDGET_KnowledgeGraph", "read"),   # another project's class: healed
        ("root", CONST, "write"),                    # the root's own class: kept as bound
    ])


def test_step_7b_alone_records_the_blocked_deferral(root_install, monkeypatch):
    """R24 LOW-1: step 7b records `root_kg_blocked_by_case_sibling` itself, so
    a run whose seed leg returns before step 7c's decision (missing venv
    python, gated sync) still reports a still-blocked root instead of letting
    the owned-drop-when-absent row drop. The main() call site threads the run
    report into 7b (``deferral_report=_deferral_report`` on the
    ``collections_and_seed`` lambda). Red-proof: remove the 7b ``add_entry``
    → fails."""
    fake = _FakeWeaviate([CONST])
    monkeypatch.setattr(install._wh, "http_request", fake)
    monkeypatch.setattr(rki, "_live_class_names", lambda url: list(fake.classes))
    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects", lambda u, n: 892)
    report = DeferralReport()
    install._ensure_collections({}, deferral_report=report)
    (entry,) = [e for e in report.entries if e.condition_id == "root_kg_blocked_by_case_sibling"]
    assert f"adopt --collection {CONST} --yes" in entry.command_to_apply
    assert DERIVED not in fake.posted
