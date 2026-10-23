# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Review V02103-FINAL2 L-1 — the Identity-tab shared-KG pick
(``app_state['shared_kg.collection_name']``) is read by the root's KG decision
and by the Python shared-name resolver the same way the hub
(``config_api.rs``) and the launcher (``resolve_shared_kg_collection``) read it:
FIRST. The pick is explicit consent, under the embedding-profile rule ``adopt``
applies; a refused pick is a deferral, never a silent rebind.

Red-proofs (mutation, restored from a saved copy): drop the pick branch in
``decide`` → the follow tests fail; drop the profile check → the refusal test
fails; drop tier 0 in ``resolve_shared_name`` → the resolver test fails.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.common.launcher_db_fixture import (
    create_empty_launcher_db,
    seed_launcher_db,
    set_app_state,
)
from vco_lib import project_init
from vco_lib import root_kg_identity as rki
from vco_lib.deferral_report import DeferralReport

from tests.common.env import process_state_fixture

# install.main() step 7b writes the project-identity env (KG_COLLECTION & co.) into
# os.environ for its child processes; restore the whole process state per test so the
# conftest W-IDENTITY guard (and every later test) sees the environment untouched.
_isolate_process_state = process_state_fixture("function")

ROOT = Path("/x/vibecoded-orchestrator")
DERIVED = "VibecodedOrchestrator_KnowledgeGraph"
CONST = "VibeCodedOrchestrator_KnowledgeGraph"
MODEL = "qwen3-embedding:0.6b"
OTHER = "snowflake-arctic-embed2"


def test_the_picker_key_is_the_one_every_python_reader_uses():
    from vco_lib import config_projection, kg_binding_heal

    assert rki.PICKER_KEY == "shared_kg.collection_name"
    assert config_projection.APP_STATE_KEY_SHARED_KG_NAME is rki.PICKER_KEY
    assert kg_binding_heal._APP_STATE_PICKER_SHARED_KG is rki.PICKER_KEY


def test_a_pick_written_with_the_current_model_is_followed():
    d = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL, MODEL, "",
                   picked=CONST, picked_model=MODEL)
    assert (d.name, d.source, d.persistable, d.picked_refused) == (CONST, rki.SOURCE_PICKED, True, None)


def test_an_empty_or_absent_pick_is_followed_and_blocked_only_by_a_case_sibling():
    d = rki.decide(ROOT, None, {}, None, MODEL, "", picked="Acme_KnowledgeGraph")
    assert (d.name, d.source, d.blocked_by) == ("Acme_KnowledgeGraph", rki.SOURCE_PICKED, None)
    d = rki.decide(ROOT, None, {"ACME_KnowledgeGraph": 3}, None, MODEL, "",
                   picked="Acme_KnowledgeGraph")
    assert d.blocked_by == "ACME_KnowledgeGraph" and not d.persistable


def test_a_pick_with_another_models_vectors_is_refused_and_reported():
    d = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL, MODEL, "",
                   picked=CONST, picked_model=OTHER)
    assert (d.name, d.source, d.picked_refused) == (DERIVED, rki.SOURCE_KEPT, CONST)
    assert "not followed" in d.reason
    entry = rki.pick_refused_entry(d)
    assert entry.condition_id == "root_kg_pick_wrong_embedding"
    assert CONST in entry.detected and DERIVED in entry.command_to_apply


def test_an_unknown_model_on_a_populated_pick_is_refused_like_adopt():
    d = rki.decide(ROOT, None, {CONST: 900}, None, MODEL, "", picked=CONST)
    assert d.picked_refused == CONST and d.name == DERIVED
    # R31 M-2: the refusal is UNVERIFIABLE, not a wrong-model claim — the
    # entry says the model could not be determined.
    assert d.pick_unverifiable is True and d.profile_unresolved is False
    entry = rki.pick_refused_entry(d)
    assert "could not be determined" in entry.title
    assert "different embedding model" not in entry.detected


def test_a_pending_fill_toward_the_profile_makes_the_pick_followable():
    d = rki.decide(ROOT, None, {CONST: 900}, None, MODEL, "",
                   picked=CONST, picked_model=OTHER, picked_pending=True)
    assert d.name == CONST and d.source == rki.SOURCE_PICKED


def test_the_override_still_outranks_the_pick():
    d = rki.decide(ROOT, None, {}, None, MODEL, "Acme_KnowledgeGraph", picked=CONST)
    assert d.source == rki.SOURCE_OVERRIDE


def test_an_unknown_weaviate_keeps_the_pick_unpersisted():
    d = rki.decide(ROOT, DERIVED, None, None, MODEL, "", picked=CONST)
    assert d.name == CONST and not d.persistable


def test_gather_reads_the_pick_and_its_model(monkeypatch):
    monkeypatch.setattr(rki, "_model_facts",
                        lambda cls, target, url: (MODEL if cls == CONST else OTHER, False))
    monkeypatch.setattr("vco_lib.kg_embed_model.target_text_model", lambda: MODEL)
    d = rki.gather(ROOT, live_names=[CONST, DERIVED], count=lambda u, n: 5,
                   bound=DERIVED, picked_read=lambda: CONST, env={})
    assert d.name == CONST and d.source == rki.SOURCE_PICKED


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "state" / "launcher.db"
    path.parent.mkdir()
    seed_launcher_db(path, [{
        "project_id": "root", "name": "Orchestrator", "folder_path": "/tmp/orch",
        "slug": "orch", "host": "orchestrator_root", "kg_primary": DERIVED,
    }])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(path))
    return path


def test_the_resolver_returns_the_pick_first(db):
    assert project_init._resolve_shared_kg_name(None) == DERIVED  # no pick: the root
    set_app_state(db, rki.PICKER_KEY, CONST)
    assert rki.picked_shared_name() == CONST
    assert project_init._resolve_shared_kg_name(None) == CONST


def test_an_empty_pick_is_no_pick(db):
    set_app_state(db, rki.PICKER_KEY, "   ")
    assert rki.picked_shared_name() is None
    assert project_init._resolve_shared_kg_name(None) == DERIVED


def test_an_adoption_updates_a_standing_pick_of_another_class(db, monkeypatch):
    set_app_state(db, rki.PICKER_KEY, CONST)
    monkeypatch.setattr("vco_lib.launcher_db_reader._discover_db_path", lambda: db)
    assert rki._write_adoption(DERIVED, out=lambda *_: None) == 0
    conn = sqlite3.connect(str(db))
    try:
        (pick,) = conn.execute("SELECT value FROM app_state WHERE key = ?",
                               (rki.PICKER_KEY,)).fetchone()
    finally:
        conn.close()
    assert pick == DERIVED


def test_an_adoption_does_not_create_a_pick(tmp_path, monkeypatch):
    path = tmp_path / "launcher.db"
    create_empty_launcher_db(path)
    monkeypatch.setattr("vco_lib.launcher_db_reader._discover_db_path", lambda: path)
    monkeypatch.setattr("vco_lib.launcher_db_reader.get_orchestrator_root_project_id", lambda: None)
    rki._write_adoption(DERIVED, out=lambda *_: None)
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (rki.PICKER_KEY,)).fetchone()
    finally:
        conn.close()
    assert row is None


@pytest.fixture
def root_install(monkeypatch, tmp_path):
    import install

    root = tmp_path / "vibecoded-orchestrator"
    root.mkdir()
    monkeypatch.setattr(install, "PROJECT_ROOT", root)
    monkeypatch.setattr(install, "_is_orchestrator_root_install", lambda: True)
    monkeypatch.setattr(install, "_wait_for_weaviate_ready", lambda *a, **k: True)
    for k in ("KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION", rki.OVERRIDE_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(install._wh, "http_request",
                        lambda m, url, body=None, timeout=None, **k:
                        (200, b'{"classes": []}') if m == "GET" else (200, b"{}"))
    return install, root


def test_install_step_7b_records_a_refused_pick(root_install, monkeypatch):
    install, _root = root_install
    refused = rki.RootKgDecision(DERIVED, rki.SOURCE_KEPT, DERIVED, "x", True, None, CONST)
    monkeypatch.setattr(install, "_root_kg_decision", lambda *a, **k: refused)
    report = DeferralReport()
    install._ensure_collections({}, deferral_report=report)
    assert [e.condition_id for e in report.entries].count("root_kg_pick_wrong_embedding") == 1


def test_install_step_7c_records_a_refused_pick(root_install, monkeypatch):
    install, root = root_install
    both = rki.RootKgDecision(DERIVED, rki.SOURCE_DERIVED, DERIVED, "x", True, CONST.upper(), CONST)
    monkeypatch.setattr(install, "_root_kg_decision", lambda *a, **k: both)
    venv_py = root / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#")
    report = DeferralReport()

    class _Args:
        update = True
        skip_seed = False

    assert install._seed_weaviate(_Args(), deferral_report=report) == install._install_weaviate.SKIPPED
    ids = [e.condition_id for e in report.entries]
    assert "root_kg_pick_wrong_embedding" in ids and "root_kg_blocked_by_case_sibling" in ids
