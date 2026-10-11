# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-I — a KG embedding-profile switch fills the new model's vectors
for every node (additive, automatic); searches stay on the recorded model
until the new slot is complete, then flip.

Everything here runs on in-file doubles: a disposable launcher.db (sqlite,
``VCT_LAUNCHER_DB_PATH``), a fake Weaviate client (collections / iterator /
data.update), and a REAL ``EmbeddingService`` whose Ollama adapter is a fake
that records which MODEL embedded what. No live service is contacted (the
suite pins ``WEAVIATE_URL`` at the unroutable sentinel).
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT, REPO_ROOT / "claude_mcp_servers"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from vco_lib import embed_slot_fill as esf  # noqa: E402
from vco_lib import embedding_enrichment as ee  # noqa: E402
from vco_lib import kg_embed_model as kem  # noqa: E402
from vco_lib.embedding_service import EmbeddingService, NoEmbeddingBackendError  # noqa: E402

QWEN3 = "qwen3-embedding:0.6b"
ARCTIC = "snowflake-arctic-embed2:latest"
COLL = "Acme_KnowledgeGraph"
KG_SLOTS = {"qwen3_embed", "ollama_embed", "openai_embed", "arctic2_embed", "openai_text_embed"}


# ─── doubles ────────────────────────────────────────────────────────────


class FakeOllama:
    """Records (model, text) for every embed; ``up=False`` = backend down."""

    def __init__(self, up: bool = True):
        self.up = up
        self.calls: list[tuple[str, str]] = []

    def is_reachable(self) -> bool:
        return self.up

    def embed(self, model, text):
        if not self.up:
            raise ConnectionError("ollama down")
        self.calls.append((model, text))
        return [float(len(model)), 0.5, 0.25]

    def embed_batch(self, model, texts):
        return [self.embed(model, t) for t in texts]

    def models(self) -> set:
        return {m for m, _t in self.calls}


class _Obj:
    def __init__(self, vector: dict, content: str):
        self.uuid = str(uuid.uuid4())
        self.vector = dict(vector)
        self.properties = {"content": content, "title": content}


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
    def __init__(self, colls: dict):
        self._colls = colls
        self.collections = SimpleNamespace(
            exists=lambda name: name in self._colls,
            get=lambda name: self._colls[name],
        )

    def close(self):
        pass


@pytest.fixture
def kgenv(tmp_path, monkeypatch):
    """Disposable launcher.db + a target model (arctic) + clean caches + the
    REST seams (schema / counts) pointed at the fake collection."""
    db = tmp_path / "launcher.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    monkeypatch.setenv("EMBEDDING_MODEL", ARCTIC)
    kem._RECORD_CACHE.clear()
    kem._LIVE_CACHE.clear()
    state = SimpleNamespace(colls={}, db=db)
    monkeypatch.setattr(esf, "declared_slots", lambda name, url: set(KG_SLOTS) if name in state.colls else None)
    monkeypatch.setattr(esf, "object_count", lambda name, url: len(state.colls[name].objs) if name in state.colls else 0)
    monkeypatch.setattr(esf, "sample_common_vectors", lambda name, url, limit: {})
    # W6-J: the per-slot population seam (sample_common_vectors delegates to
    # it). raising=False so this fixture also runs against the pre-W6-J source
    # (red-proofs), where the seam does not exist yet.
    monkeypatch.setattr(esf, "sample_slot_population", lambda name, url, limit: ({}, 0), raising=False)
    monkeypatch.setattr(ee, "_http_get_schema", lambda name, url: {"vectorConfig": {s: {} for s in KG_SLOTS}})
    monkeypatch.setattr(ee, "_estimate_object_count", lambda name: len(state.colls[name].objs))
    state.client = lambda: FakeClient(state.colls)
    yield state
    kem._RECORD_CACHE.clear()
    kem._LIVE_CACHE.clear()


def _svc(model: str, ollama: FakeOllama) -> EmbeddingService:
    return EmbeddingService(
        project_root=None, ollama_url="http://127.0.0.1:9", code_embed_url="http://127.0.0.1:9",
        text_model_id=model, code_model_id="codesage-large-v2", openai_api_key="",
        ollama_adapter=ollama,
    )


def _qwen3_collection(state, n: int = 3, arctic_on: int = 0):
    objs = []
    for i in range(n):
        vec = {"qwen3_embed": [0.1, 0.2, 0.3]}
        if i < arctic_on:
            vec["arctic2_embed"] = [0.9, 0.9, 0.9]
        objs.append(_Obj(vec, f"node body {i}"))
    state.colls[COLL] = _Coll(objs)
    return state.colls[COLL]


def _record(model: str) -> kem.KgModelRecord:
    slot, dim = kem.text_model_slot(model)
    return kem.KgModelRecord(model, dim, slot)


# ─── switch → fill → flip ──────────────────────────────────────────────


def test_profile_switch_fills_the_new_slot_then_flips(kgenv):
    coll = _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    ollama = FakeOllama()

    before = kem.resolve_kg_model(COLL, role="query")
    assert before.model == QWEN3 and before.filling, "searches stay on the recorded model"
    write = kem.resolve_kg_model(COLL, role="write")
    assert write.model == ARCTIC and write.carry == QWEN3

    log: list[str] = []
    verdict = kem.fill_collection(COLL, _svc(ARCTIC, ollama), log=log.append, client_factory=kgenv.client)

    assert verdict == "complete"
    assert ollama.models() == {ARCTIC}, "the fill embeds ONLY with the selected model"
    assert len(ollama.calls) == 3
    assert all(o.vector.get("qwen3_embed") for o in coll.objs), "no existing vector is dropped"
    assert all(o.vector.get("arctic2_embed") for o in coll.objs)
    assert any("filling up to 3 object(s)" in line for line in log)
    assert kem.read_record(COLL).model == ARCTIC
    kem._RECORD_CACHE.clear()
    assert kem.resolve_kg_model(COLL, role="query").model == ARCTIC, "searches flip once complete"


def test_partial_fill_keeps_queries_on_the_recorded_slot(kgenv):
    _qwen3_collection(kgenv, n=3, arctic_on=2)
    kem.write_record(COLL, _record(QWEN3))

    assert kem.settle(COLL, ARCTIC, client_factory=kgenv.client) == "filling"
    assert kem.read_record(COLL).model == QWEN3, "an incomplete slot never flips the record"
    fill = kem.read_fill(COLL)
    assert (fill["filled"], fill["total"], fill["target"]) == (2, 3, ARCTIC)
    kem._RECORD_CACHE.clear()
    assert kem.recorded_query_model(COLL) == QWEN3
    assert kem.recorded_query_slot(COLL) == "qwen3_embed"

    # The near_object / dup-scan decision targets the recorded slot too.
    from vco_lib.kg_vector_slot import kg_query_target_vector

    fake = SimpleNamespace(
        name=COLL,
        config=SimpleNamespace(get=lambda: SimpleNamespace(vector_config={s: None for s in KG_SLOTS})),
    )
    assert kg_query_target_vector(fake) == "qwen3_embed"


def test_same_content_means_zero_reembeds(kgenv):
    _qwen3_collection(kgenv, n=3, arctic_on=3)
    kem.write_record(COLL, _record(ARCTIC))
    ollama = FakeOllama()

    def _must_not_run(*_a, **_k):
        raise AssertionError("no fill is owed — the enrichment must not run")

    assert kem.fill_collection(COLL, _svc(ARCTIC, ollama), log=lambda _m: None,
                               enrich=_must_not_run, client_factory=kgenv.client) is None
    slots = kem.complete_write_vectors(_svc(ARCTIC, ollama), {"arctic2_embed": [1.0]}, "t", COLL)
    assert set(slots) == {"arctic2_embed"}, "steady state: no carry"
    assert ollama.calls == []


def test_switch_back_to_a_still_complete_slot_is_instant(kgenv):
    coll = _qwen3_collection(kgenv, n=3, arctic_on=3)
    kem.write_record(COLL, _record(ARCTIC))  # the previous fill flipped to arctic
    os.environ["EMBEDDING_MODEL"] = QWEN3     # the user switches back (kgenv restores it)
    ollama = FakeOllama()

    assert kem.fill_collection(COLL, _svc(QWEN3, ollama), log=lambda _m: None,
                               client_factory=kgenv.client) == "complete"
    assert ollama.calls == [], "qwen3 vectors were kept (carried) — nothing to embed"
    assert kem.read_record(COLL).model == QWEN3
    assert all(o.vector.get("arctic2_embed") for o in coll.objs)


# ─── loud, never a substitute ──────────────────────────────────────────


def test_target_backend_down_is_loud_and_never_substitutes(kgenv):
    coll = _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    ollama = FakeOllama(up=False)
    svc = _svc(ARCTIC, ollama)

    with pytest.raises(NoEmbeddingBackendError):
        kem.fill_collection(COLL, svc, log=lambda _m: None, client_factory=kgenv.client)
    out: list[str] = []
    kem.fill_before_walk([COLL], svc, log=out.append)
    assert any(line.startswith("❌ kg-embed: filling") for line in out)
    assert not any(o.vector.get("arctic2_embed") for o in coll.objs)
    assert kem.read_record(COLL).model == QWEN3


def test_a_write_without_the_target_vector_fails_instead_of_storing_a_fallback_row(kgenv):
    """W6-H's "fallback-only write": the active backend failed and only a
    secondary slot came back. Never stored now — the write fails loudly."""
    svc = _svc(ARCTIC, FakeOllama())
    with pytest.raises(kem.KgWriteVectorMissing):
        kem.complete_write_vectors(svc, {"qwen3_embed": [0.1]}, "text", COLL)
    with pytest.raises(kem.KgWriteVectorMissing):
        kem.complete_write_vectors(svc, {}, "text", "")


def test_a_write_during_a_fill_carries_the_recorded_vector(kgenv):
    _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    ollama = FakeOllama()
    truncated: list[str] = []
    slots = kem.complete_write_vectors(_svc(ARCTIC, ollama), {"arctic2_embed": [1.0]}, "body", COLL, truncated)
    assert set(slots) == {"arctic2_embed", "qwen3_embed"}
    assert ollama.calls == [(QWEN3, "body")], "the carry embeds with the RECORDED model"
    assert truncated == []


def test_openai_target_needs_the_embeddings_consent_and_logs_the_count_first(kgenv, monkeypatch):
    _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    order: list[str] = []

    def _enrich(collection, slot, **_k):
        order.append(f"enrich:{slot}")
        return SimpleNamespace(failed=0)

    svc = SimpleNamespace(text_model_id="text-embedding-3-small")
    assert kem.fill_collection(COLL, svc, log=order.append, enrich=_enrich,
                               client_factory=kgenv.client) is None
    assert any("not allowed" in line for line in order) and not any(o.startswith("enrich") for o in order)

    conn = sqlite3.connect(kgenv.db)
    conn.execute("INSERT INTO app_state VALUES ('openai_embeddings_consent', 'true', 0)")
    conn.commit()
    conn.close()
    order.clear()
    kem.fill_collection(COLL, svc, log=order.append, enrich=_enrich, client_factory=kgenv.client)
    assert "OpenAI API (cost-bearing)" in order[0] and "up to 3 object(s)" in order[0]
    assert order[1] == "enrich:openai_text_embed"


def test_no_record_reads_the_complete_slot_off_live_data(kgenv, monkeypatch):
    """A collection embedded before v0.2.102 has no record; a reader samples
    it, so a switch made before this release still searches the populated
    slot instead of the empty new one."""
    monkeypatch.setattr(esf, "sample_slot_population",
                        lambda name, url, limit: ({"qwen3_embed": (1024, 3)}, 3), raising=False)
    monkeypatch.setattr(esf, "sample_common_vectors", lambda name, url, limit: {"qwen3_embed": 1024})
    res = kem.resolve_kg_model(COLL, role="query")
    assert res.model == QWEN3 and res.source == kem.SOURCE_LIVE


def test_a_project_env_selects_the_target():
    from vco_lib.embedding_service import resolve_active_text_model_id

    assert resolve_active_text_model_id({"ACTIVE_EMBEDDING": "arctic"}) == ARCTIC
    assert resolve_active_text_model_id({"EMBEDDING_MODEL": "nomic-embed-text"}) == "nomic-embed-text"


# ─── bundle self-heal + the ledger row ─────────────────────────────────


def test_bundle_self_heal_emits_then_resolves_the_fill_row(kgenv, tmp_path, monkeypatch):
    from vco_lib.deferral_report import DeferralReport

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    coll = _qwen3_collection(kgenv, n=2, arctic_on=1)
    kem.write_record(COLL, _record(QWEN3))
    monkeypatch.setattr(kem, "folder_collections", lambda f: ([COLL], ARCTIC))
    monkeypatch.setattr(esf, "_default_client", kgenv.client)

    assert kem.reconcile_folder_kg_embedding(folder) == {COLL: "filling"}
    assert DeferralReport.read(folder).has_condition(kem.DEFERRAL_CID)

    coll.objs[1].vector["arctic2_embed"] = [0.9]
    kem._RECORD_CACHE.clear()
    assert kem.reconcile_folder_kg_embedding(folder) == {COLL: "complete"}
    report = DeferralReport.read(folder)
    assert not (report and report.has_condition(kem.DEFERRAL_CID))


def test_bundle_self_heal_stamps_a_missing_record_and_never_embeds(kgenv, monkeypatch, tmp_path):
    _qwen3_collection(kgenv, n=2)
    monkeypatch.setattr(kem, "folder_collections", lambda f: ([COLL], ARCTIC))
    monkeypatch.setattr(esf, "_default_client", kgenv.client)
    assert kem.reconcile_folder_kg_embedding(tmp_path) == {COLL: "recorded"}
    assert kem.read_record(COLL).model == QWEN3


def test_the_fill_row_is_declared_with_the_kg_seed_retry():
    from vco_lib.deferral_registry import retry_handler_for

    assert retry_handler_for(kem.DEFERRAL_CID) == "kg_seed"


def test_bundle_runs_call_both_self_heals(monkeypatch):
    import vco_lib.code_embed_model as cem

    seen: list[str] = []
    monkeypatch.setattr(cem, "reconcile_folder_code_embedding", lambda f, dry_run=False: seen.append("code"))
    monkeypatch.setattr(kem, "reconcile_folder_kg_embedding", lambda f, dry_run=False: seen.append("kg"))
    esf.reconcile_folder_embeddings(Path("."))
    assert seen == ["code", "kg"]
    src = (REPO_ROOT / "vco_lib" / "project_init.py").read_text(encoding="utf-8")
    assert "reconcile_folder_embeddings(folder, dry_run=dry_run)" in src


# ─── the readers: MCP + hook ───────────────────────────────────────────


def test_mcp_kg_query_embeds_with_the_recorded_model_while_filling(kgenv, monkeypatch):
    import weaviate_mcp.server as srv

    _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    ollama = FakeOllama()
    svc = _svc(ARCTIC, ollama)
    monkeypatch.setattr(srv, "_get_embedding_service", lambda: svc)
    steady_calls: list[str] = []

    async def _steady(text, **_k):
        steady_calls.append(text)
        return [0.0], "arctic2_embed"

    vec, slot = asyncio.run(srv._kg_search_vector("find me", COLL, steady=_steady))
    assert slot == "qwen3_embed"
    assert steady_calls == []
    assert {m for m, _t in ollama.calls} == {QWEN3}

    kem.write_record(COLL, _record(ARCTIC))  # complete → flipped
    vec, slot = asyncio.run(srv._kg_search_vector("find me", COLL, steady=_steady))
    assert (slot, steady_calls) == ("arctic2_embed", ["find me"]), "steady state = the old path"


def test_rl_label_follows_the_query_model_only_while_filling(kgenv):
    from claude_mcp_servers.scripts import rl_kg_search

    assert rl_kg_search._rl_model_name(COLL, "env-model") == "env-model"
    kem.write_record(COLL, _record(QWEN3))
    assert rl_kg_search._rl_model_name(COLL, "env-model") == QWEN3


# ─── kg-sync wiring (the shipped script, driven) ───────────────────────


def _load_sync(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KG_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("KG_COLLECTION", COLL)
    monkeypatch.setenv("SHARED_KG_COLLECTION", "")
    monkeypatch.setenv("DEVELOPMENT_COLLECTION", "")
    monkeypatch.setenv("DUAL_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("VCT_DISABLE_HUB_RESOLVER", "1")
    monkeypatch.delenv("KG_SYNC_PROJECT_ROOT", raising=False)
    (tmp_path / "knowledge").mkdir(exist_ok=True)
    name = f"_sync_w6i_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.HAS_LOGGER = False
    return mod


def test_kg_sync_writes_carry_the_recorded_vector(kgenv, tmp_path, monkeypatch):
    mod = _load_sync(tmp_path, monkeypatch)
    _qwen3_collection(kgenv)
    kem.write_record(COLL, _record(QWEN3))
    ollama = FakeOllama()
    server = SimpleNamespace(
        embedding_service=_svc(ARCTIC, ollama), text_vector_slot="arctic2_embed",
        _get_all_kg_embeddings_tagged=lambda text: ({"arctic2_embed": [1.0]}, []),
    )
    vec_arg, slots, truncated = mod._build_vector_arg(server, "node text", COLL)
    assert set(vec_arg) == {"arctic2_embed", "qwen3_embed"}
    assert ollama.calls == [(QWEN3, "node text")]

    server._get_all_kg_embeddings_tagged = lambda text: ({"qwen3_embed": [1.0]}, [])
    with pytest.raises(kem.KgWriteVectorMissing):
        mod._build_vector_arg(server, "node text", COLL)


def test_kg_sync_all_fills_before_the_walk_and_settles_after(tmp_path, monkeypatch):
    mod = _load_sync(tmp_path, monkeypatch)
    order: list[object] = []
    mod._kg_embed_model = SimpleNamespace(
        fill_before_walk=lambda colls, svc: order.append(("fill", tuple(colls))),
        after_walk=lambda colls, root, svc, full_tree: order.append(("after", full_tree)),
    )
    mod.EmbeddingService = SimpleNamespace(for_project=lambda root: SimpleNamespace(text_model_id=ARCTIC, close=lambda: None))
    mod.WeaviateMCPServer = lambda **kw: SimpleNamespace(close=lambda: None, embedding_service=kw.get("embedding_service"))
    mod.ensure_collection_exists = lambda srv: True
    mod.ensure_dev_collection_exists = lambda srv: None
    mod._regen_node_formats_after_full_sync = lambda: None
    mod._shared_write_matrix_allows = lambda name: (True, "")

    def _nodes(_srv):
        order.append("walk")
        return mod.SyncTally()

    mod.sync_all_nodes = _nodes
    mod.sync_all_docs = lambda _srv: mod.SyncTally()
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["sync", "--all"]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        mod.main()
    assert order == [("fill", (COLL,)), "walk", ("after", True)]

    order.clear()
    node = tmp_path / "knowledge" / "a.md"
    node.write_text("---\ntitle: A\ntype: concept\n---\nbody\n", encoding="utf-8")
    mod.sync_node = lambda _srv, _p: (order.append("node"), mod.SyncOutcome(mod.OUTCOME_SYNCED, "a"))[1]
    with mock.patch.object(sys, "argv", ["sync", str(node)]), contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()), pytest.raises(SystemExit):
        mod.main()
    assert order == ["node", ("after", False)], "a scoped run never embeds a whole collection"


# ─── W6-J F1: the no-record window (review 2026-10-11) ─────────────────
#
# A collection indexed before v0.2.102 has no record, and the bundle self-heal
# that would stamp one soft-fails when Weaviate is down at update time. If the
# user then switches profile and edits a node before any whole-tree sync, the
# scoped run must STILL keep the node in the slot searches read (carry off the
# live sample) and leave the ledger row that schedules the fill.


def test_no_record_scoped_edit_carries_the_sampled_slot_and_emits_the_row(kgenv, tmp_path, monkeypatch):
    """The exact F1 scenario: no record (pre-v0.2.102 collection, stamp
    skipped) + a profile switch + a scoped edit. Pre-fix the rewrite stored
    the TARGET's vector only (dropping the node from the slot searches read)
    and no `kg_embed_fill_pending` row was emitted, so nothing scheduled the
    fill."""
    from vco_lib.deferral_report import DeferralReport

    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    _qwen3_collection(kgenv)  # 3 objects, qwen3_embed only; NO record stamped
    # The live sample (paid once per process) sees qwen3 on every object.
    monkeypatch.setattr(esf, "sample_slot_population",
                        lambda name, url, limit: ({"qwen3_embed": (1024, 3)}, 3), raising=False)
    monkeypatch.setattr(esf, "sample_common_vectors", lambda name, url, limit: {"qwen3_embed": 1024})
    ollama = FakeOllama()

    slots = kem.complete_write_vectors(_svc(ARCTIC, ollama), {"arctic2_embed": [1.0]}, "body", COLL)
    assert set(slots) == {"arctic2_embed", "qwen3_embed"}, \
        "a scoped rewrite with no record must carry the slot the sample shows"
    assert (QWEN3, "body") in ollama.calls, "the carry embeds with the recorded (sampled) model"
    assert kem.read_record(COLL) is None, "a sample is never stamped as the record"

    kem.after_walk([COLL], folder, _svc(ARCTIC, ollama), full_tree=False)
    report = DeferralReport.read(folder)
    assert report and report.has_condition(kem.DEFERRAL_CID), \
        "a scoped run that finds the fill owed (live sample) must emit the ledger row"


def test_one_target_only_rewrite_does_not_blind_the_live_read(kgenv, monkeypatch):
    """F1's second half: after ONE node was rewritten target-only, the
    sample's every-object intersection is empty. The live read must follow
    the slot a MAJORITY of the sample still holds — falling to the target
    would search the new, nearly-empty slot (the defect W6-I exists to kill)."""
    _qwen3_collection(kgenv)
    # Post-rewrite sample: 2 objects qwen3-only, 1 object arctic2-only.
    monkeypatch.setattr(
        esf, "sample_slot_population",
        lambda name, url, limit: ({"qwen3_embed": (1024, 2), "arctic2_embed": (1024, 1)}, 3),
        raising=False)
    # The pre-fix seam reads an empty intersection for the same sample.
    monkeypatch.setattr(esf, "sample_common_vectors", lambda name, url, limit: {})

    rec = kem.record_from_live(COLL, ARCTIC)
    assert rec is not None and rec.model == QWEN3, "the majority slot is the live record"
    res = kem.resolve_kg_model(COLL, role="query")
    assert res.model == QWEN3 and res.source == kem.SOURCE_LIVE, \
        "searches must NOT fall to the target off an empty intersection"


def test_live_read_still_prefers_a_complete_target_and_refuses_a_split_sample(kgenv, monkeypatch):
    """The quorum changes nothing when the target's slot really is populated
    on every sampled object (a finished switch), and a sample with NO majority
    slot gives no verdict (the pre-fix behaviour — never a guess)."""
    monkeypatch.setattr(esf, "sample_slot_population",
                        lambda name, url, limit: ({"arctic2_embed": (1024, 3),
                                                   "qwen3_embed": (1024, 3)}, 3), raising=False)
    monkeypatch.setattr(esf, "sample_common_vectors",
                        lambda name, url, limit: {"arctic2_embed": 1024, "qwen3_embed": 1024})
    rec = kem.record_from_live(COLL, ARCTIC)
    assert rec is not None and rec.model == ARCTIC, "a complete target slot stays preferred"

    kem._LIVE_CACHE.clear()
    monkeypatch.setattr(esf, "sample_slot_population",
                        lambda name, url, limit: ({"arctic2_embed": (1024, 1),
                                                   "qwen3_embed": (1024, 1)}, 3), raising=False)
    monkeypatch.setattr(esf, "sample_common_vectors", lambda name, url, limit: {})
    assert kem.record_from_live(COLL, ARCTIC) is None, "no majority → no verdict (no guess)"


def test_inline_fallback_labels_the_vector_with_the_model_that_made_it(monkeypatch):
    """F2 (W6-I review): with no EmbeddingService in hand, the inline leg of
    the MCP's `_get_all_kg_embeddings` stored the vector
    `server.get_ollama_embedding` returned under the hardcoded label
    "qwen3_embed" — but that call embeds `server.EMBEDDING_MODEL`, so on a
    non-qwen3 profile another model's vector was stored under the qwen3 slot.
    The label now follows the model that PRODUCED the vector."""
    import weaviate_mcp.server as srv
    from vco_lib.embedding_service import DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV

    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    monkeypatch.delenv(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, raising=False)

    async def _fake_ollama(text):  # embeds server.EMBEDDING_MODEL, as the real one does
        return [0.7, 0.7, 0.7]

    monkeypatch.setattr(srv, "get_ollama_embedding", _fake_ollama)

    monkeypatch.setattr(srv, "EMBEDDING_MODEL", ARCTIC)
    vectors = asyncio.run(srv._get_all_kg_embeddings("node text"))
    assert list(vectors) == ["arctic2_embed"], "an arctic vector is never labelled qwen3"

    monkeypatch.setattr(srv, "EMBEDDING_MODEL", QWEN3)
    vectors = asyncio.run(srv._get_all_kg_embeddings("node text"))
    assert list(vectors) == ["qwen3_embed"], "the default profile's label is unchanged"


def test_inline_fallback_secondary_never_overwrites_the_active_slot(monkeypatch):
    """F2 follow-through: with dual-write ON, a profile whose model maps to
    the legacy `ollama_embed` slot (nomic, mxbai) must keep ITS vector there —
    the legacy arctic vector the same leg gathers is not allowed to replace
    it (the row would then hold another model's vector under the active
    model's slot, the exact mislabel F2 closes)."""
    import weaviate_mcp.server as srv
    from vco_lib.embedding_service import DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV

    monkeypatch.setattr(srv, "_get_embedding_service", lambda: None)
    monkeypatch.setenv(DUAL_EMBEDDING_WRITE_ALL_SLOTS_ENV, "1")

    async def _active(text):
        return [0.1, 0.1]

    async def _legacy(text):
        return [0.9, 0.9]

    async def _openai(text):
        return None

    monkeypatch.setattr(srv, "get_ollama_embedding", _active)
    monkeypatch.setattr(srv, "get_legacy_text_embedding", _legacy)
    monkeypatch.setattr(srv, "get_openai_embedding", _openai)
    monkeypatch.setattr(srv, "EMBEDDING_MODEL", "nomic-embed-text")
    vectors = asyncio.run(srv._get_all_kg_embeddings("node text"))
    assert vectors == {"ollama_embed": [0.1, 0.1]}, \
        "the active model's vector keeps its slot; the legacy one does not replace it"


def test_status_counts_objects_only_for_an_unrecorded_collection(kgenv):
    """W6-J: the launcher's re-scan alert must tell a stored-but-unrecorded KG
    (its stamp failed, or no slot is complete) from an empty/absent one (a
    fresh project, an unused development collection) — "no record" alone
    describes both, and an alert on the empty one would never clear."""
    _qwen3_collection(kgenv)  # 3 objects, no record
    st = kem.collection_status(COLL, ARCTIC)
    assert st["recorded_model"] is None and st["objects"] == 3

    empty = kem.collection_status("Acme_Development", ARCTIC)
    assert empty["recorded_model"] is None and empty["objects"] == 0

    kem.write_record(COLL, kem.KgModelRecord(QWEN3, 1024, "qwen3_embed"))
    assert kem.collection_status(COLL, ARCTIC)["objects"] is None, \
        "a recorded collection is not counted for this field"
    assert kem.collection_status("Acme_Development", ARCTIC, count=False)["objects"] is None
