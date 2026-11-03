# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 (Windows field: 19 of 20 projects had the SHARED KG class bound
as their PRIMARY KG collection).

* CAUSE — the update-time cross-prefix adoption
  (``kg_binding_heal._prefix_adopt_kg_bindings_pass``) re-pointed an unsynced
  project's missing primary class to the ONE populated ``*_KnowledgeGraph``
  class, which was the shared one. It now never adopts, as a primary, the
  shared class or a class another project binds; an unbound populated class
  (the renamed-project shape the pass exists for) is still adopted.
* DETECTION — ``vco_lib.kg_primary_shared`` finds the rows the defect already
  wrote (root never flagged), the doctor emits/clears
  ``kg_primary_bound_to_shared``, and the registry clear probe reads the same
  function.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import add_project, create_empty_launcher_db, set_app_state
from vco_lib import doctor as d
from vco_lib import kg_binding_heal as heal
from vco_lib import kg_primary_shared as kps
from vco_lib.deferral_probes import PROBES, ProbeContext
from vco_lib.deferral_registry import clear_probe_for, is_action_required

SHARED = "VibeCodedOrchestrator_KnowledgeGraph"


def _db(tmp_path: Path, *projects: dict, pointer: str = SHARED) -> Path:
    db = create_empty_launcher_db(tmp_path / "launcher.db")
    set_app_state(db, "orchestrator_root_kg_collection", pointer)
    for spec in projects:
        add_project(db, folder_path=tmp_path / spec["project_id"], **spec)
    return db


ROOT = {"project_id": "root", "name": "VibeCoded Orchestrator", "host": "orchestrator_root",
        "slug": "orchestrator-root", "kg_primary": SHARED, "kg_shared": SHARED}


# ── the cause: cross-prefix adoption ─────────────────────────────────────────


def _adopt(db: Path, populated: dict, monkeypatch) -> dict:
    monkeypatch.setattr(heal, "_count_weaviate_class_objects",
                        lambda _url, name: populated.get(name, 0))
    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        existing = set(populated)
        out = heal._prefix_adopt_kg_bindings_pass(
            cur, existing_classes=existing,
            existing_by_lower={c.lower(): c for c in existing},
            weaviate_url="http://127.0.0.1:9", skip_project_ids={"root"})
        conn.commit()
    finally:
        conn.close()
    return out


def _primary(db: Path, pid: str) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT collection_name FROM project_kg_bindings "
                            "WHERE project_id = ? AND role = 'primary'", (pid,)).fetchone()[0]
    finally:
        conn.close()


def test_an_unsynced_project_never_adopts_the_shared_class(tmp_path, monkeypatch):
    """Pre-fix: Alpha's missing class had exactly one populated candidate —
    the shared class — and was re-pointed to it."""
    db = _db(tmp_path, ROOT, {"project_id": "a", "name": "Alpha", "kg_primary": "Alpha_KnowledgeGraph",
                              "kg_shared": SHARED})
    out = _adopt(db, {SHARED: 120}, monkeypatch)
    assert out["adopts"] == [] and out["multi_candidates"] == []
    assert _primary(db, "a") == "Alpha_KnowledgeGraph"


def test_a_custom_shared_pointer_is_never_adopted_either(tmp_path, monkeypatch):
    db = _db(tmp_path, {"project_id": "a", "name": "Alpha", "kg_primary": "Alpha_KnowledgeGraph"},
             pointer="Canon_KnowledgeGraph")
    _adopt(db, {"Canon_KnowledgeGraph": 50}, monkeypatch)
    assert _primary(db, "a") == "Alpha_KnowledgeGraph"


def test_another_projects_class_is_never_adopted(tmp_path, monkeypatch):
    db = _db(tmp_path, {"project_id": "a", "name": "Alpha", "kg_primary": "Alpha_KnowledgeGraph"},
             {"project_id": "b", "name": "Bystander", "kg_primary": "Bystander_KnowledgeGraph"})
    _adopt(db, {"Bystander_KnowledgeGraph": 9}, monkeypatch)
    assert _primary(db, "a") == "Alpha_KnowledgeGraph"


def test_an_unbound_populated_class_is_still_adopted(tmp_path, monkeypatch):
    """The shape the pass exists for (a renamed project's data under its old
    prefix) keeps working beside the shared class."""
    db = _db(tmp_path, ROOT, {"project_id": "a", "name": "Alpha", "kg_primary": "Alpha_KnowledgeGraph"})
    out = _adopt(db, {SHARED: 120, "Existing_KnowledgeGraph": 7}, monkeypatch)
    assert [a[3] for a in out["adopts"]] == ["Existing_KnowledgeGraph"]
    assert _primary(db, "a") == "Existing_KnowledgeGraph"


# ── detection ────────────────────────────────────────────────────────────────


def test_find_flags_non_root_projects_bound_to_the_shared_class(tmp_path):
    db = _db(tmp_path, ROOT,
             {"project_id": "a", "name": "Alpha", "kg_primary": SHARED, "kg_shared": SHARED},
             {"project_id": "b", "name": "Beta", "kg_primary": "Beta_KnowledgeGraph",
              "kg_shared": SHARED},
             {"project_id": "c", "name": "Gamma", "kg_primary": "vibecodedorchestrator_knowledgegraph"})
    found = kps.find(db)
    assert [(f.project_name, f.expected) for f in found] == [
        ("Alpha", "Alpha_KnowledgeGraph"), ("Gamma", "Gamma_KnowledgeGraph")]
    assert not any(f.project_id == "root" for f in found)


def test_find_flags_a_primary_equal_to_the_projects_own_shared_row(tmp_path):
    db = _db(tmp_path, {"project_id": "a", "name": "Alpha", "kg_primary": "Canon_KnowledgeGraph",
                        "kg_shared": "Canon_KnowledgeGraph"})
    assert [f.project_name for f in kps.find(db)] == ["Alpha"]


def test_a_healthy_machine_finds_nothing(tmp_path):
    db = _db(tmp_path, ROOT, {"project_id": "a", "name": "Alpha",
                              "kg_primary": "Alpha_KnowledgeGraph", "kg_shared": SHARED})
    assert kps.find(db) == []
    assert kps.still_bound(db) is False


def test_an_unreadable_db_is_unknown_not_none_found(tmp_path):
    bogus = tmp_path / "launcher.db"
    bogus.write_bytes(b"not a database at all" * 10)
    assert kps.find(bogus) is None
    assert kps.still_bound(bogus) is None


# ── doctor + registry wiring ─────────────────────────────────────────────────


def _res(findings):
    return d.DoctorResolvers(kg_primary_shared=lambda: findings)


def test_doctor_problem_finding_carries_the_gui_repair(tmp_path):
    f = kps.PrimaryBoundToShared("a", "Alpha", "/p/a", SHARED, "Alpha_KnowledgeGraph")
    findings = d.PROBES["kg_primary_shared"][0](tmp_path, _res([f]), {})
    assert len(findings) == 1 and findings[0].status == d.STATUS_PROBLEM
    assert findings[0].condition_id == kps.CID_PRIMARY_BOUND_TO_SHARED
    assert "Repair primary KG binding" in findings[0].command
    assert "Alpha_KnowledgeGraph" in findings[0].command
    entry = kps.doctor_entry(findings[0])
    assert entry.disposition == "action_required"
    assert entry.condition_id == "kg_primary_bound_to_shared"
    assert entry.dismiss_fields == {"projects": ["Alpha"]}


def test_doctor_clean_reading_carries_the_cid_to_clear(tmp_path):
    findings = d.PROBES["kg_primary_shared"][0](tmp_path, _res([]), {})
    assert [x.status for x in findings] == [d.STATUS_OK]
    assert findings[0].condition_id == kps.CID_PRIMARY_BOUND_TO_SHARED
    assert d.PROBES["kg_primary_shared"][0](tmp_path, _res(None), {}) == []


def test_the_condition_is_doctor_owned_self_resolving_and_registered():
    assert kps.CID_PRIMARY_BOUND_TO_SHARED in d.DOCTOR_OWNED_CIDS
    assert kps.CID_PRIMARY_BOUND_TO_SHARED in d.DOCTOR_SELF_RESOLVING_CIDS
    assert is_action_required("kg_primary_bound_to_shared")
    assert clear_probe_for("kg_primary_bound_to_shared") == \
        "probe:py:kg_primary_still_bound_to_shared"


def test_the_clear_probe_reads_the_same_function(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(kps, "still_bound", lambda db_path=None: calls.append(1) or True)
    assert PROBES["kg_primary_still_bound_to_shared"](ProbeContext(folder=tmp_path)) is True
    assert calls == [1]


@pytest.mark.parametrize("expected_is_shared", [False, True])
def test_remedy_text_handles_a_project_named_like_the_orchestrator(expected_is_shared):
    expected = SHARED if expected_is_shared else "Alpha_KnowledgeGraph"
    text = kps.remedy([kps.PrimaryBoundToShared("a", "Alpha", "/p", SHARED, expected)])
    if expected_is_shared:
        assert "type a KG collection name of its own" in text
    else:
        assert "re-points its primary KG binding to Alpha_KnowledgeGraph" in text


# ── the launcher's reading (the Identity tab calls this CLI) ─────────────────


def _cli(args: list) -> tuple:
    import json
    import subprocess
    import sys

    from tests.common.child_env import child_env

    res = subprocess.run([sys.executable, "-m", "vco_lib.kg_primary_shared", *args],
                         capture_output=True, text=True, timeout=60, env=child_env(),
                         cwd=str(Path(__file__).resolve().parent.parent))
    return res.returncode, json.loads(res.stdout)


def test_status_cli_names_the_repair_target_for_a_bound_project(tmp_path):
    db = _db(tmp_path, ROOT, {"project_id": "a", "name": "My App", "kg_primary": SHARED},
             {"project_id": "b", "name": "Other", "kg_primary": "Other_KnowledgeGraph"})
    rc, reply = _cli(["status", "--project-id", "a", "--db", str(db), "--json"])
    assert rc == 0 and reply == {"ok": True, "bound_to_shared": True, "bound": SHARED,
                                 "expected": "MyApp_KnowledgeGraph", "expected_is_shared": False}
    rc, reply = _cli(["status", "--project-id", "b", "--db", str(db), "--json"])
    assert rc == 0 and reply["bound_to_shared"] is False
    rc, reply = _cli(["status", "--project-id", "root", "--db", str(db), "--json"])
    assert rc == 0 and reply["bound_to_shared"] is False  # the root is never flagged


def test_candidate_check_refuses_the_shared_class_for_a_project_but_not_the_root(tmp_path):
    """The launcher's binding writers ask this before writing a primary."""
    db = _db(tmp_path, ROOT, {"project_id": "a", "name": "My App",
                              "kg_primary": "MyApp_KnowledgeGraph", "kg_shared": SHARED})
    assert kps.candidate_is_shared("a", SHARED.lower(), db) is True
    assert kps.candidate_is_shared("a", "MyApp_KnowledgeGraph", db) is False
    assert kps.candidate_is_shared("root", SHARED, db) is False
    rc, reply = _cli(["status", "--project-id", "a", "--candidate", SHARED, "--db", str(db), "--json"])
    assert rc == 0 and reply == {"ok": True, "candidate": SHARED, "candidate_is_shared": True}


def test_status_cli_reports_an_unreadable_db_as_not_ok(tmp_path):
    bogus = tmp_path / "launcher.db"
    bogus.write_bytes(b"not a database" * 20)
    rc, reply = _cli(["status", "--project-id", "a", "--db", str(bogus), "--json"])
    assert rc == 1 and reply["ok"] is False
