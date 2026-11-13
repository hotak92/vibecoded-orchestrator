# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 (owner ruling 2026-11-12, "backfill once"): a newly enabled
SECONDARY text-embedding slot is backfilled into the stored KG rows ONCE, in
the background, with completion stamped per (collection, slot); a down
secondary is recorded as owed (auto-retryable ledger row) and retried — never
a whole-KG re-embed per sync.

In-file doubles only: a fake Weaviate client, a REAL ``EmbeddingService`` on a
fake Ollama adapter that records which MODEL embedded what, a disposable
project folder for the stamp and the ledger. No live service is contacted.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "claude_mcp_servers"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from vco_lib import embed_slot_fill as esf  # noqa: E402
from vco_lib import embedding_enrichment as ee  # noqa: E402
from vco_lib import kg_embed_model as kem  # noqa: E402
from vco_lib import kg_secondary_backfill as ksb  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402
from vco_lib.embedding_service import EmbeddingService  # noqa: E402

QWEN3 = "qwen3-embedding:0.6b"
ARCTIC = "snowflake-arctic-embed2:latest"
COLL = "Acme_KnowledgeGraph"
DEV = "Acme_Development"
KG_SLOTS = {"qwen3_embed", "ollama_embed", "openai_embed", "arctic2_embed", "openai_text_embed"}
DENSE = "x" * 6000  # over arctic's 4 096-token window (byte bound), inside qwen3's


class FakeOllama:
    """Records (model, text); ``down`` = models that fail; a text longer than
    ``refuse_over`` chars is refused by arctic with Ollama's overflow 400."""

    def __init__(self, down: "set[str] | None" = None, refuse_over: int = 5000):
        self.down = set(down or ())
        self.refuse_over = refuse_over
        self.calls: list[tuple[str, str]] = []

    def is_reachable(self) -> bool:
        return True

    def embed(self, model, text):
        if model in self.down:
            raise ConnectionError(f"{model} not found")
        if model == ARCTIC and len(text) > self.refuse_over:
            raise RuntimeError("HTTP 400: the input length exceeds the context length")
        self.calls.append((model, text))
        return [float(len(model)), 0.5, 0.25]

    def embed_batch(self, model, texts):
        return [self.embed(model, t) for t in texts]

    def arctic_calls(self) -> list[str]:
        return [t for m, t in self.calls if m == ARCTIC and t != ksb._PROBE_TEXT]


class _Obj:
    def __init__(self, vector: dict, content: str, file_path: str):
        self.uuid = str(uuid.uuid4())
        self.vector = dict(vector)
        self.properties = {"content": content, "file_path": file_path}


class _Data:
    def __init__(self, coll):
        self.coll = coll

    def update(self, uuid, vector=None, properties=None):  # noqa: A002 — client kwarg name
        for o in self.coll.objs:
            if o.uuid == uuid:
                o.vector.update(vector or {})
                o.properties.update(properties or {})


class _Coll:
    def __init__(self, objs):
        self.objs = objs
        self.data = _Data(self)

    def iterator(self, include_vector=True, return_properties=None):  # noqa: ARG002
        return iter(list(self.objs))


class FakeClient:
    def __init__(self, state):
        self.state = state
        state.opened += 1
        self.collections = SimpleNamespace(
            exists=lambda name: name in state.colls,
            get=lambda name: state.colls[name],
        )

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.setenv("EMBEDDING_MODEL", QWEN3)
    monkeypatch.setenv("DUAL_EMBEDDING_WRITE_ALL_SLOTS", "1")
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "1")
    kem._RECORD_CACHE.clear()
    kem._LIVE_CACHE.clear()
    ksb.reset_run_state()
    folder = tmp_path / "proj"
    (folder / ".claude" / "context").mkdir(parents=True)
    state = SimpleNamespace(colls={}, folder=folder, opened=0)
    monkeypatch.setattr(esf, "declared_slots", lambda name, url: set(KG_SLOTS) if name in state.colls else None)
    monkeypatch.setattr(esf, "object_count", lambda name, url: len(state.colls[name].objs) if name in state.colls else 0)
    monkeypatch.setattr(ee, "_http_get_schema", lambda name, url: {"vectorConfig": {s: {} for s in KG_SLOTS}} if name in state.colls else None)
    monkeypatch.setattr(ee, "_estimate_object_count", lambda name: len(state.colls[name].objs))
    state.client = lambda: FakeClient(state)
    yield state
    ksb.reset_run_state()
    kem._RECORD_CACHE.clear()
    kem._LIVE_CACHE.clear()


def _svc(ollama: FakeOllama, root=None) -> EmbeddingService:
    return EmbeddingService(
        project_root=root, ollama_url="http://127.0.0.1:9", code_embed_url="http://127.0.0.1:9",
        text_model_id=QWEN3, code_model_id="codesage-large-v2", openai_api_key="",
        ollama_adapter=ollama,
    )


def _seed(state, name=COLL, bodies=("alpha", "beta", "gamma")):
    objs = [_Obj({"qwen3_embed": [0.1, 0.2]}, b, f"knowledge/n{i}.md") for i, b in enumerate(bodies)]
    state.colls[name] = _Coll(objs)
    return state.colls[name]


def _run(state, svc, names=(COLL,)):
    out = ksb.backfill_before_walk(list(names), svc, state.folder, log=lambda _m: None,
                                   client_factory=state.client)
    return out


def _ledger_has(folder) -> bool:
    report = DeferralReport.read(Path(folder))
    return bool(report) and report.has_condition(ksb.DEFERRAL_CID)


# ─── configuration ──────────────────────────────────────────────────────


def test_the_secondary_set_is_the_fan_out_decision_minus_the_active_slot(env):
    assert ksb.secondary_write_models(_svc(FakeOllama())) == {"arctic2_embed": ARCTIC}
    assert ksb.secondary_models_for_env({
        "EMBEDDING_MODEL": QWEN3, "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "true",
        "DUAL_EMBEDDING_ARCTIC_SECONDARY": "true"}) == {"arctic2_embed": ARCTIC}
    assert ksb.secondary_models_for_env({"EMBEDDING_MODEL": QWEN3}) == {}


# ─── backfill once ──────────────────────────────────────────────────────


def test_every_row_gets_the_secondary_vector_once_and_a_second_run_embeds_nothing(env):
    coll = _seed(env)
    ollama = FakeOllama()
    svc = _svc(ollama)

    assert _run(env, svc) == {(COLL, "arctic2_embed"): ksb.COMPLETE}
    assert sorted(ollama.arctic_calls()) == ["alpha", "beta", "gamma"]
    assert all(o.vector["qwen3_embed"] and o.vector["arctic2_embed"] for o in coll.objs)
    assert ksb.is_complete(ksb.read_state(env.folder), COLL, "arctic2_embed", ARCTIC)

    ollama.calls.clear()
    opened = env.opened
    assert _run(env, svc) == {}, "a stamped pair is not owed"
    assert ollama.calls == [], "no probe, no embed on a stamped collection"
    assert env.opened == opened, "no Weaviate scan on a stamped collection"


def test_rows_already_holding_the_secondary_are_kept(env):
    coll = _seed(env)
    coll.objs[0].vector["arctic2_embed"] = [7.0, 7.0]
    ollama = FakeOllama()
    _run(env, _svc(ollama))
    assert sorted(ollama.arctic_calls()) == ["beta", "gamma"]
    assert coll.objs[0].vector["arctic2_embed"] == [7.0, 7.0]


def test_a_refused_dense_row_is_relaid_by_the_walk_then_settled(env):
    coll = _seed(env, bodies=("alpha", DENSE))
    ollama = FakeOllama()
    svc = _svc(ollama)

    assert _run(env, svc) == {(COLL, "arctic2_embed"): ksb.RELAY}
    assert not coll.objs[1].vector.get("arctic2_embed"), "never a cut or averaged vector"
    assert not ksb.is_complete(ksb.read_state(env.folder), COLL, "arctic2_embed", ARCTIC)

    # The walk's gate: fires for the dense node once, never for the other.
    assert ksb.relay_reason(COLL, [coll.objs[0]]) is None
    assert "refused whole" in (ksb.relay_reason(COLL, [coll.objs[1]]) or "")
    assert ksb.relay_reason(COLL, [coll.objs[1]]) is None, "once per node per run"

    # The writer re-lays the node into rows both models accept (simulated:
    # two finer rows, each with both vectors, replace the dense one).
    coll.objs[1:] = [_Obj({"qwen3_embed": [1.0], "arctic2_embed": [2.0]}, DENSE[:3000],
                          "knowledge/n1.md") for _ in range(2)]
    ksb.after_walk([COLL], env.folder, svc, full_tree=True, client_factory=env.client,
                   log=lambda _m: None)
    assert ksb.is_complete(ksb.read_state(env.folder), COLL, "arctic2_embed", ARCTIC)
    assert not _ledger_has(env.folder)


def test_a_relay_the_walk_did_not_complete_stays_owed(env):
    _seed(env, bodies=(DENSE,))
    svc = _svc(FakeOllama())
    _run(env, svc)
    ksb.after_walk([COLL], env.folder, svc, full_tree=True, client_factory=env.client,
                   log=lambda _m: None)
    assert not ksb.is_complete(ksb.read_state(env.folder), COLL, "arctic2_embed", ARCTIC)
    assert _ledger_has(env.folder)


# ─── down backend ───────────────────────────────────────────────────────


def test_a_down_secondary_is_owed_retried_and_never_scanned(env):
    coll = _seed(env)
    _seed(env, name=DEV)
    ollama = FakeOllama(down={ARCTIC})
    svc = _svc(ollama)
    opened = env.opened

    out = _run(env, svc, names=(COLL, DEV))
    assert set(out.values()) == {ksb.DOWN}
    assert env.opened == opened, "nothing scanned while the secondary is down"
    assert ollama.calls == [], "nothing embedded"
    ksb.after_walk([COLL, DEV], env.folder, svc, full_tree=True, client_factory=env.client,
                   log=lambda _m: None)
    assert _ledger_has(env.folder)
    row = next(e for e in DeferralReport.read(env.folder).entries if e.condition_id == ksb.DEFERRAL_CID)
    assert "did not answer" in row.detected and DEV in row.detected

    # Back up: the retry completes and the row clears itself.
    ksb.reset_run_state()
    ollama.down.clear()
    assert set(_run(env, svc, names=(COLL, DEV)).values()) == {ksb.COMPLETE}
    ksb.after_walk([COLL, DEV], env.folder, svc, full_tree=True, client_factory=env.client,
                   log=lambda _m: None)
    assert not _ledger_has(env.folder)
    assert all(o.vector.get("arctic2_embed") for o in coll.objs)


def test_the_down_probe_runs_once_per_model_per_run(env, monkeypatch):
    _seed(env)
    _seed(env, name=DEV)
    probes: list[str] = []
    real = ksb._probe
    monkeypatch.setattr(ksb, "_probe", lambda sib: probes.append(sib.text_model_id) or real(sib))
    _run(env, _svc(FakeOllama(down={ARCTIC})), names=(COLL, DEV))
    assert probes == [ARCTIC]


# ─── the stamp stays true ───────────────────────────────────────────────


def test_a_write_that_omitted_the_secondary_invalidates_the_stamp(env):
    _seed(env)
    svc = _svc(FakeOllama())
    _run(env, svc)
    assert ksb.owed_pairs(env.folder, [COLL], {"arctic2_embed": ARCTIC}) == []

    ksb.observe_document_write(env.folder, {"arctic2_embed": ARCTIC}, {"qwen3_embed"})
    assert ksb.owed_pairs(env.folder, [COLL], {"arctic2_embed": ARCTIC}) == [
        (COLL, "arctic2_embed", ARCTIC)]


def test_a_complete_write_leaves_the_stamp_and_the_file_untouched(env):
    _seed(env)
    _run(env, _svc(FakeOllama()))
    before = ksb.state_path(env.folder).read_bytes()
    ksb.observe_document_write(env.folder, {"arctic2_embed": ARCTIC}, {"qwen3_embed", "arctic2_embed"})
    assert ksb.state_path(env.folder).read_bytes() == before


def test_the_fan_out_reports_an_omitted_secondary(env, monkeypatch):
    """The real document fan-out (EmbeddingService) is what notices."""
    _seed(env)
    _run(env, _svc(FakeOllama()))
    ksb.reset_run_state()
    writer = _svc(FakeOllama(down={ARCTIC}), root=env.folder)
    slots = writer.embed_text_all_configured("a new node")
    assert set(slots) == {"qwen3_embed"}
    assert ksb.owed_pairs(env.folder, [COLL], {"arctic2_embed": ARCTIC}), \
        "the stamp no longer claims a slot this write left empty"


def test_turning_the_secondary_off_keeps_vectors_and_on_again_resumes(env, monkeypatch):
    coll = _seed(env)
    ollama = FakeOllama()
    _run(env, _svc(ollama))
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "0")
    off = _svc(ollama)
    assert ksb.secondary_write_models(off) == {}
    ksb.after_walk([COLL], env.folder, off, full_tree=True, client_factory=env.client,
                   log=lambda _m: None)
    assert all(o.vector.get("arctic2_embed") for o in coll.objs), "no vector cleared"
    assert not ksb.read_state(env.folder)["complete"], "the stamp no longer holds"
    assert not _ledger_has(env.folder)

    # Rows written meanwhile lack it; turning it on again fills only those.
    coll.objs.append(_Obj({"qwen3_embed": [0.3]}, "delta", "knowledge/n9.md"))
    monkeypatch.setenv("DUAL_EMBEDDING_ARCTIC_SECONDARY", "1")
    ollama.calls.clear()
    ksb.reset_run_state()
    assert _run(env, _svc(ollama)) == {(COLL, "arctic2_embed"): ksb.COMPLETE}
    assert ollama.arctic_calls() == ["delta"]


# ─── scheduling without a service ───────────────────────────────────────


def test_the_service_free_scheduler_emits_the_row_and_never_resolves_it(env):
    on = {"EMBEDDING_MODEL": QWEN3, "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "1",
          "DUAL_EMBEDDING_ARCTIC_SECONDARY": "1"}
    assert ksb.schedule_for_folder(env.folder, [COLL], env=on) == [(COLL, "arctic2_embed", ARCTIC)]
    assert _ledger_has(env.folder)
    assert ksb.schedule_for_folder(env.folder, [COLL], env={"EMBEDDING_MODEL": QWEN3}) == []
    assert _ledger_has(env.folder), "only a run holding a live service may resolve"


def test_an_unconfigured_project_schedules_nothing(env):
    assert ksb.schedule_for_folder(env.folder, [COLL], env={"EMBEDDING_MODEL": QWEN3}) == []
    assert not _ledger_has(env.folder)
    assert not ksb.state_path(env.folder).exists()


# ─── wiring into kg-sync's legs ─────────────────────────────────────────


def test_kg_sync_pre_walk_leg_runs_the_backfill(env, monkeypatch):
    coll = _seed(env)
    monkeypatch.setattr(esf, "_default_client", env.client)
    monkeypatch.setattr(ksb, "_default_client", env.client)
    monkeypatch.setattr(ee, "_connect_weaviate", lambda base_url: env.client())
    ollama = FakeOllama()
    log: list[str] = []
    kem.fill_before_walk([COLL], _svc(ollama), log=log.append, folder=env.folder)
    assert not [line for line in log if line.startswith("❌")], log
    assert all(o.vector.get("arctic2_embed") for o in coll.objs)
    assert ksb.is_complete(ksb.read_state(env.folder), COLL, "arctic2_embed", ARCTIC)


def test_kg_sync_post_walk_leg_keeps_the_ledger_row(env, monkeypatch):
    _seed(env)
    monkeypatch.setattr(esf, "_default_client", env.client)
    monkeypatch.setattr(ksb, "_default_client", env.client)
    kem.after_walk([COLL], env.folder, _svc(FakeOllama()), full_tree=False)
    assert _ledger_has(env.folder), "a scoped run schedules the owed backfill"


def test_the_bundle_self_heal_schedules_the_backfill(env, monkeypatch):
    monkeypatch.setattr(kem, "folder_collections", lambda folder: ([COLL], QWEN3))
    monkeypatch.setattr(kem, "_project_env", lambda folder: {
        "EMBEDDING_MODEL": QWEN3, "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "1",
        "DUAL_EMBEDDING_ARCTIC_SECONDARY": "1"})
    monkeypatch.setattr(kem, "_ensure_record", lambda name, target: None)
    monkeypatch.setattr(kem, "_owed_list", lambda names, target: [])
    kem.reconcile_folder_kg_embedding(env.folder)
    assert _ledger_has(env.folder)


def test_session_start_schedules_the_backfill(env, monkeypatch):
    from vco_lib import deferral_retry as dr

    monkeypatch.setattr(kem, "folder_collections", lambda folder: ([COLL], QWEN3))
    monkeypatch.setattr(kem, "_project_env", lambda folder: {
        "EMBEDDING_MODEL": QWEN3, "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "1",
        "DUAL_EMBEDDING_ARCTIC_SECONDARY": "1"})
    spawned: list[Path] = []
    monkeypatch.setattr(dr, "spawn_detached", lambda folder, python="": spawned.append(folder) or True)
    owed = dr.session_start_owed_check(env.folder)
    assert ksb.DEFERRAL_CID in owed
    assert spawned == [env.folder], "the same spawn backfills it"


def test_the_env_projection_schedules_the_backfill(env, monkeypatch):
    from vco_lib import config_projection as cp

    calls: list[Path] = []
    monkeypatch.setattr(ksb, "schedule_for_folder", lambda folder: calls.append(folder) or [])
    monkeypatch.setattr(cp, "project_env_from_db", lambda *a, **k: {"project_root": env.folder})
    monkeypatch.setattr(cp, "apply_project_env", lambda bundle, surfaces=None: {})
    args = SimpleNamespace(project_id="p1", db_path=None, orchestrator_root=None, weaviate_url=None,
                           weaviate_port=None, ollama_port=None, code_embed_port=None, surfaces=None)
    assert cp._cli_apply(args) == 0
    assert calls == [env.folder]


def test_the_retry_driver_reruns_it_through_the_kg_seed_handler():
    import tomllib

    rows = tomllib.loads((REPO_ROOT / "vco_lib" / "deferral_conditions.toml").read_text())["conditions"]
    row = rows[ksb.DEFERRAL_CID]
    assert row["class"] == "auto_retryable"
    assert row["retry_action"] == "retry:py:kg_seed"


# ─── kg-sync's re-embed gate ────────────────────────────────────────────


def _load_sync(tmp_path):
    for key, value in {
        "KG_BASE_DIR": str(tmp_path), "KG_COLLECTION": COLL, "DEVELOPMENT_COLLECTION": DEV,
        "VCT_DISABLE_HUB_RESOLVER": "1", "VCT_STATE_DIR": str(tmp_path / ".vct-state"),
        "WEAVIATE_URL": "http://127.0.0.1:9", "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
    }.items():
        os.environ[key] = value
    name = f"_sync_kg_secbf_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_kg_sync_gate_relays_a_node_the_backfill_found_refused(env, tmp_path, monkeypatch):
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION", "VCT_DISABLE_HUB_RESOLVER",
                "VCT_STATE_DIR", "WEAVIATE_URL", "VCT_ORCHESTRATOR_ROOT"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    mod = _load_sync(tmp_path)
    ksb._RELAY[COLL] = {"knowledge/dense.md"}
    row = _Obj({"qwen3_embed": [0.1]}, DENSE, "knowledge/dense.md")
    reason = mod._split_rows_heal_reason(None, COLL, [row], DENSE, "qwen3_embed")
    assert reason and "secondary" in reason
    other = _Obj({"qwen3_embed": [0.1]}, "x", "knowledge/other.md")
    monkeypatch.setattr(mod, "_chunker_resync_pending", lambda: False)
    assert mod._split_rows_heal_reason(None, COLL, [other], "x", "qwen3_embed") is None
