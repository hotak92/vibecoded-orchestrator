# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Review V02103-R29 N-5 + N-8(b): ONE pick-profile rule for decide AND heal.

N-5: a pick refused because the embedding profile could not be resolved
(target model unknown) keeps the refusal — symmetric with ``adopt`` — but the
``root_kg_pick_wrong_embedding`` deferral must not claim the pick "was
written with a different embedding model": with an unresolvable profile the
pick is UNVERIFIABLE, not wrong. ``RootKgDecision.profile_unresolved`` carries
that corner and ``pick_refused_entry`` words the two cases differently.

N-8(b): ``heal_shared_kg_pointer_drift`` converged the shared-KG pointers
onto ANY live Identity-tab pick, while ``root_kg_identity.decide`` refuses a
wrong-model pick — across runs the pointers flipped between the pick and the
root's own name. The rule is now ONE function, ``pick_follows_profile``,
applied by ``decide`` and by the heal's injectable ``pick_ok`` predicate; the
heal's two production callers pass ``root_kg_identity.live_pick_ok(url)``
(the same live gathers the decide path uses). The heal's DEFAULT predicate
refuses every pick (this module holds no profile facts and must not guess).

Red-proofs (cp-aside → mutate → red → restore → md5): dropping
``profile_unresolved`` from the decision fails the N-5 wording tests; making
the heal ignore ``pick_ok`` (always converge onto a live pick) fails the
wrong-model and agreement tests; making ``live_pick_ok`` ignore the count
fails the live-predicate test.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.launcher_db_fixture import create_empty_launcher_db  # noqa: E402
from vco_lib import root_kg_identity as rki  # noqa: E402
from vco_lib.kg_binding_heal import heal_shared_kg_pointer_drift  # noqa: E402

ROOT = Path("/x/vibecoded-orchestrator")
DERIVED = "VibecodedOrchestrator_KnowledgeGraph"
CONST = "VibeCodedOrchestrator_KnowledgeGraph"
MODEL = "qwen3-embedding:0.6b"
OTHER = "snowflake-arctic-embed2"


# ─── N-5: the deferral says WHY the pick was not followed, truthfully ────────


def test_n5_unresolvable_profile_refuses_but_words_the_entry_truthfully():
    d = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL, "", "",
                   picked=CONST, picked_model=MODEL)
    assert d.picked_refused == CONST and d.name == DERIVED
    assert d.profile_unresolved is True
    entry = rki.pick_refused_entry(d)
    assert entry.condition_id == "root_kg_pick_wrong_embedding"
    assert "could not be resolved" in entry.title
    assert "could not be resolved" in entry.detected
    # The overstated claim of the pre-fix wording must be gone: with an
    # unknown profile VCO cannot know the models differ.
    assert "different embedding model" not in entry.detected
    assert "Resolve the embedding profile" in entry.command_to_apply


def test_n5_resolved_profile_keeps_the_wrong_model_wording():
    d = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL, MODEL, "",
                   picked=CONST, picked_model=OTHER)
    assert d.picked_refused == CONST
    assert d.profile_unresolved is False
    entry = rki.pick_refused_entry(d)
    assert "another embedding model" in entry.title
    assert "written with a different embedding model" in entry.detected
    assert "switch Preferences" in entry.command_to_apply


def test_n5_the_flag_is_defaulted_so_unrelated_decisions_are_unaffected():
    d = rki.decide(ROOT, None, {}, None, MODEL, "", picked="Acme_KnowledgeGraph")
    assert d.profile_unresolved is False  # followed picks never set it


# ─── N-8(b): the ONE predicate ───────────────────────────────────────────────


@pytest.mark.parametrize("rows,picked_model,target,pending,expected", [
    (0, None, MODEL, False, True),            # empty pick: nothing to mismatch
    (0, None, "", False, True),               # …even with an unresolvable profile
    (900, MODEL, MODEL, False, True),         # same model → follow
    (900, OTHER, MODEL, False, False),        # wrong model → refuse
    (900, MODEL, "", False, False),           # profile unknown → refuse (N-5 corner)
    (900, None, MODEL, False, False),         # pick's model unknown → refuse
    (900, OTHER, MODEL, True, True),          # fill toward the profile pending
    (None, MODEL, MODEL, False, False),       # count unknown → refuse (heal arm)
])
def test_pick_follows_profile_is_the_one_rule(rows, picked_model, target,
                                              pending, expected):
    assert rki.pick_follows_profile(
        rows=rows, picked_model=picked_model, target_model=target,
        pending_fill=pending) is expected


def test_decide_uses_the_predicate_for_every_branch():
    # Same fixtures as the parametrized predicate test, through decide: a
    # populated same-model pick is followed, a wrong-model one refused.
    followed = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL,
                          MODEL, "", picked=CONST, picked_model=MODEL)
    assert followed.source == rki.SOURCE_PICKED and followed.picked_refused is None
    refused = rki.decide(ROOT, DERIVED, {DERIVED: 10, CONST: 900}, MODEL,
                         MODEL, "", picked=CONST, picked_model=OTHER)
    assert refused.picked_refused == CONST and refused.source != rki.SOURCE_PICKED


# ─── N-8(b): the heal applies the same rule via pick_ok ─────────────────────


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


PTR = "VibeCodedOrchestrator_KnowledgeGraph"   # drifted pointer (machine default)
LAST = "OldCanonical_KnowledgeGraph"           # recorded `last`, live


def _drifted_db_with_pick(tmp_path, pick):
    """ptr != last with a live Identity-tab ``pick`` and no shared-binding
    consensus, so the pick leg is the ONLY thing that can converge."""
    db = create_empty_launcher_db(tmp_path / "launcher.db")
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("INSERT INTO projects (id, name, folder_path, host, slug, "
                "created_at, updated_at) VALUES ('root', 'Orchestrator', "
                "'/fx/root', 'orchestrator_root', 'root', 1, 1)")
    cur.execute("INSERT INTO project_kg_bindings (project_id, role, "
                "collection_name, updated_at) VALUES "
                "('root', 'shared', ?, 1)", (LAST,))
    cur.execute("INSERT INTO project_kg_bindings (project_id, role, "
                "collection_name, updated_at) VALUES "
                "('p1', 'shared', 'Other_KnowledgeGraph', 1)")
    for k, v in (("orchestrator_root_kg_collection", PTR),
                 ("last_installed_shared_kg_collection", LAST),
                 ("shared_kg.collection_name", pick)):
        cur.execute("INSERT INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value", (k, v))
    conn.commit()
    return conn, cur


PICK = "AcmeCorp_KnowledgeGraph"
EXISTING = {PTR, LAST, PICK, "Other_KnowledgeGraph"}


def _heal(cur, pick_ok):
    logs, log = _log_collector()
    sink = _Sink()
    changed = heal_shared_kg_pointer_drift(
        cur, existing_classes=EXISTING, log_event=log,
        deferral_report=sink, deferral_entry_cls=_Entry, pick_ok=pick_ok)
    return changed, sink, logs


def _ptr(conn_or_cur):
    row = conn_or_cur.execute(
        "SELECT value FROM app_state WHERE key = "
        "'orchestrator_root_kg_collection'").fetchone()
    return row[0]


def test_heal_converges_onto_a_same_model_pick(tmp_path):
    conn, cur = _drifted_db_with_pick(tmp_path, PICK)
    try:
        pick_ok = lambda name: rki.pick_follows_profile(  # noqa: E731
            rows=900, picked_model=MODEL, target_model=MODEL, pending_fill=False)
        changed, sink, _logs = _heal(cur, pick_ok)
        assert sink.entries == []
        assert _ptr(cur) == PICK and changed >= 3
        conn.commit()
    finally:
        conn.close()


def test_heal_leaves_a_wrong_model_populated_pick_alone(tmp_path):
    conn, cur = _drifted_db_with_pick(tmp_path, PICK)
    try:
        pick_ok = lambda name: rki.pick_follows_profile(  # noqa: E731
            rows=900, picked_model=OTHER, target_model=MODEL, pending_fill=False)
        changed, sink, logs = _heal(cur, pick_ok)
        assert changed == 0
        assert _ptr(cur) == PTR  # untouched
        assert [e.condition_id for e in sink.entries] == [
            "shared_kg_pointer_drift_unresolved"]
        # The entry names the refused pick — the honest disclosure.
        assert PICK in sink.entries[0].detected
        assert any("not converged onto" in m for _, m in logs)
        # No loop: a second run changes nothing and re-emits the SAME entry.
        changed2, sink2, _ = _heal(cur, pick_ok)
        assert changed2 == 0 and _ptr(cur) == PTR
        assert [e.condition_id for e in sink2.entries] == [
            "shared_kg_pointer_drift_unresolved"]
        conn.commit()
    finally:
        conn.close()


def test_heal_default_predicate_refuses_without_profile_facts(tmp_path):
    """The DEFAULT is the conservative arm: no caller-supplied predicate, no
    convergence onto a pick — this module cannot prove the pick's vectors
    match the current profile (N-8b: guessing is the divergence bug)."""
    conn, cur = _drifted_db_with_pick(tmp_path, PICK)
    try:
        changed, sink, _logs = _heal(cur, None)
        assert changed == 0 and _ptr(cur) == PTR
        assert [e.condition_id for e in sink.entries] == [
            "shared_kg_pointer_drift_unresolved"]
        assert PICK in sink.entries[0].detected
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("rows,picked_model,target,pending,followed", [
    (0, None, MODEL, False, True),
    (900, MODEL, MODEL, False, True),
    (900, OTHER, MODEL, False, False),
    (900, MODEL, "", False, False),
    (900, OTHER, MODEL, True, True),
])
def test_decide_and_heal_agree_on_the_same_fixtures(
        tmp_path, rows, picked_model, target, pending, followed):
    """ONE parametrized fixture drives BOTH surfaces: ``decide`` follows the
    pick exactly when the heal (with a ``pick_ok`` built from the same facts)
    converges onto it — the N-8b invariant, pinned end to end."""
    conn, cur = _drifted_db_with_pick(tmp_path, PICK)
    try:
        counts = {PTR: 10, LAST: 5, "Other_KnowledgeGraph": 5}
        if rows:
            counts[PICK] = rows
        decision = rki.decide(ROOT, DERIVED, counts, MODEL, target, "",
                              picked=PICK, picked_model=picked_model,
                              picked_pending=pending)
        decision_follows = decision.source == rki.SOURCE_PICKED
        assert decision_follows is followed
        if followed:
            assert decision.picked_refused is None

        pick_ok = lambda name: rki.pick_follows_profile(  # noqa: E731
            rows=rows, picked_model=picked_model, target_model=target,
            pending_fill=pending)
        _changed, sink, _logs = _heal(cur, pick_ok)
        heal_converged = _ptr(cur) == PICK
        assert heal_converged is followed, (
            f"heal {'converged' if heal_converged else 'refused'} while decide "
            f"{'followed' if decision_follows else 'refused'} the same pick")
        if not followed:
            assert [e.condition_id for e in sink.entries] == [
                "shared_kg_pointer_drift_unresolved"]
        conn.commit()
    finally:
        conn.close()


# ─── live_pick_ok: the caller-side predicate from root_kg_identity ─────────


def test_live_pick_ok_gathers_the_same_facts_decide_uses(monkeypatch):
    counts: "dict[str, object]" = {"n": 900}

    def _count(url, name):
        return counts["n"] if name == PICK else 0

    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects",
                        _count)
    monkeypatch.setattr("vco_lib.kg_embed_model.target_text_model", lambda: MODEL)
    facts = {"model": MODEL}
    monkeypatch.setattr(rki, "_model_facts",
                        lambda cls, target, url: (facts["model"], False))
    # A FRESH predicate per verdict: live_pick_ok binds its gathers at build
    # time, so re-patching the module attribute needs a rebuild to matter.
    assert rki.live_pick_ok("http://127.0.0.1:9")(PICK) is True  # same model
    counts["n"] = None
    assert rki.live_pick_ok("http://127.0.0.1:9")(PICK) is False  # count unknown
    counts["n"] = 0
    assert rki.live_pick_ok("http://127.0.0.1:9")(PICK) is True  # empty pick
    counts["n"] = 900
    facts["model"] = OTHER
    assert rki.live_pick_ok("http://127.0.0.1:9")(PICK) is False  # wrong model


def test_live_pick_ok_refuses_an_unresolvable_profile(monkeypatch):
    def _boom():
        raise RuntimeError("profile unreadable")

    monkeypatch.setattr("vco_lib.kg_binding_heal._count_weaviate_class_objects",
                        lambda url, name: 900)
    monkeypatch.setattr("vco_lib.kg_embed_model.target_text_model", _boom)
    ok = rki.live_pick_ok("http://127.0.0.1:9")
    assert ok(PICK) is False


# ─── the two production callers pass the predicate (behavioural) ────────────


def _run_self_heal(db, *, existing, predicate):
    """Drive ``self_heal_kg_bindings`` (pass 5 calls the heal) with
    ``root_kg_identity.live_pick_ok`` replaced by ``predicate``."""
    from vco_lib import kg_binding_heal as kbh

    recorded = {}

    def _fake_live_pick_ok(url):
        recorded["url"] = url
        return predicate

    entries = _Sink()
    logs, log = _log_collector()

    import unittest.mock as mock

    with mock.patch.object(rki, "live_pick_ok", _fake_live_pick_ok):
        kbh.self_heal_kg_bindings(
            entries,
            db_path=str(db),
            weaviate_url="http://127.0.0.1:9",
            existing_classes=existing,
            existing_by_lower={n.lower(): n for n in existing},
            log_event=log,
            connect_rw=lambda path, label: sqlite3.connect(str(path)),
            deferral_entry_cls=_Entry,
        )
    return recorded, entries, logs


def test_self_heal_kg_bindings_passes_the_root_kg_identity_predicate(tmp_path):
    """kg_binding_heal's own call site (the install --update path) builds its
    ``pick_ok`` from ``root_kg_identity.live_pick_ok`` and the heal FOLLOWS
    it: the refused verdict leaves the pointer alone; the accepted verdict
    converges. Behavioural — nothing inspects the source."""
    # Refused pick → pointer untouched, drift deferral stands.
    _conn, _cur = _drifted_db_with_pick(tmp_path, PICK)
    _conn.close()
    recorded, entries, _logs = _run_self_heal(
        tmp_path / "launcher.db", existing=EXISTING, predicate=lambda n: False)
    assert recorded.get("url") == "http://127.0.0.1:9"
    c = sqlite3.connect(str(tmp_path / "launcher.db"))
    try:
        assert _ptr(c) == PTR
    finally:
        c.close()
    assert any(e.condition_id == "shared_kg_pointer_drift_unresolved"
               for e in entries.entries)

    # Accepted pick → converged onto the pick (same DB, second run).
    _recorded2, entries2, _logs2 = _run_self_heal(
        tmp_path / "launcher.db", existing=EXISTING, predicate=lambda n: True)
    c = sqlite3.connect(str(tmp_path / "launcher.db"))
    try:
        assert _ptr(c) == PICK
    finally:
        c.close()
    assert not any(e.condition_id == "shared_kg_pointer_drift_unresolved"
                   for e in entries2.entries)


def test_bundle_update_pointer_heal_passes_the_predicate(tmp_path, monkeypatch):
    """project_init's call site (the install-bundle --update path) builds its
    ``pick_ok`` from ``root_kg_identity.live_pick_ok`` too, and its heal
    FOLLOWS the verdict."""
    import http.server
    import json as _json
    import threading

    from vco_lib import project_init

    db = tmp_path / "launcher.db"
    conn, _cur = _drifted_db_with_pick(tmp_path, PICK)
    conn.commit()
    conn.close()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _json.dumps(
                {"classes": [{"class": n} for n in EXISTING]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **k):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("WEAVIATE_URL",
                       f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr("vco_lib.paths.vct_root_dir", lambda: tmp_path)

    verdicts = []
    monkeypatch.setattr(rki, "live_pick_ok",
                        lambda url: (lambda n: verdicts.append(n) or False))
    try:
        project_init._bundle_update_pointer_heal()
        c = sqlite3.connect(str(db))
        try:
            assert _ptr(c) == PTR  # refused → untouched
        finally:
            c.close()
        assert verdicts == [PICK]  # the predicate was actually consulted

        verdicts.clear()
        monkeypatch.setattr(rki, "live_pick_ok",
                            lambda url: (lambda n: verdicts.append(n) or True))
        project_init._bundle_update_pointer_heal()
        c = sqlite3.connect(str(db))
        try:
            assert _ptr(c) == PICK  # accepted → converged
        finally:
            c.close()
        assert verdicts == [PICK]
    finally:
        server.shutdown()
