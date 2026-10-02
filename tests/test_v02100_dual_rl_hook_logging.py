# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 — dual-RL-log on the hook/CLI paths, twin-record parity, loss
visibility, and the per-project identity of the hook KG search.

Investigation: the dual-RL-log fan-out (a ``<task_id>:arctic`` twin of every
KG retrieval, so the second embedding net gets a corpus) existed only inside the
two MCP tool bodies, while ~99% of retrievals come from the hooks through
``rl_kg_search.py`` — which never read the flag. The arctic twins also lacked
``shown_rank`` / ``chunks_matched`` / ``best_chunk_number``; lost hub POSTs were
DEBUG-only; and in every non-root project the hook KG leg never ran, because the
hooks looked for ``rl_kg_search.py`` under the PROJECT root.

All tests here are behavioural (they run the real flow against fakes from
``tests/common/rl_kg_search_harness.py``); none reads source text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = REPO_ROOT / "claude_mcp_servers"
for _p in (str(REPO_ROOT), str(MCP_DIR), str(REPO_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from common import rl_kg_search_harness as H  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402

pytest.importorskip("weaviate_mcp.server")


def _live(name: str):
    """The module object currently in ``sys.modules`` — other suites purge and
    re-import ``weaviate_mcp.server``, so a module-level binding can go stale
    while the code under test imports the live one."""
    import importlib

    return importlib.import_module(name)


class _LiveModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr):
        return getattr(_live(self._name), attr)


srv = _LiveModule("weaviate_mcp.server")
sp = _LiveModule("claude_mcp_servers.rl_client.search_pipeline")

DUAL_ENV = {
    "DUAL_RL_LOG_ENABLED": "true",
    "DUAL_EMBEDDING_WRITE_ALL_SLOTS": "true",
    "DUAL_EMBEDDING_ARCTIC_SECONDARY": "true",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj_a"
    (proj / ".claude").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("VCT_SESSION_ID", "sess-dual-1")
    return proj


@pytest.fixture
def world(project, monkeypatch):
    """In-process fakes + the rl_kg_search module. Returns (mod, state)."""
    for k, v in DUAL_ENV.items():
        monkeypatch.setenv(k, v)
    from vco_lib import rl_telemetry_loss

    rl_telemetry_loss._reset_warned_for_test()
    mod = H.import_rl_kg_search()
    state: dict = {}
    H.install_fakes(
        _live("weaviate_mcp.server"), _live("claude_mcp_servers.rl_client.search_pipeline"),
        cfg={"collections": ["ProjA_KnowledgeGraph"]}, state=state,
        patch=monkeypatch.setattr,
    )
    return mod, state


def _loss_lines():
    from vco_lib.rl_telemetry_loss import loss_log_path

    p = loss_log_path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def _clean_ledger():
    from vco_lib.rl_telemetry_loss import loss_log_path

    p = loss_log_path()
    if p.exists():
        p.unlink()
    yield


# ---------------------------------------------------------------------------
# F1 — the hook path emits the twin
# ---------------------------------------------------------------------------


def test_hook_path_dual_on_posts_primary_and_arctic_twin(world, project):
    mod, state = world
    H.run_main(mod)

    emitted = state["emitted"]
    assert [e["task_id"].endswith(":arctic") for e in emitted] == [False, True], emitted
    primary, twin = emitted
    assert twin["task_id"] == primary["task_id"] + ":arctic"
    assert primary["task_type"] == twin["task_type"] == "pre_edit_kg_search"
    assert primary["embedding_source"] == "qwen3"
    assert twin["embedding_source"] == "arctic"
    assert twin["embedding_dim"] == 1024
    # The twin's query vector is the ARCTIC one, never the active one.
    assert twin["query_emb_head"] == H.ARCTIC_VEC[0]
    assert twin["query_emb_len"] == 1024
    assert primary["query_emb_head"] == H.ACTIVE_VEC[0]
    # Every twin node carries the arctic per-node vector, not the active one.
    assert twin["nodes"], "the twin must carry nodes"
    for n in twin["nodes"]:
        assert n["emb"] == H._node_vec(n["title"], H.OTHER_SLOT)[0]
        assert n["n_emb"] == n["emb"]
    # Hook-shaped call: budgeted embed, NO lazy node backfill.
    other_calls = [c for c in state["enrich_calls"] if c.get("other_slot")]
    assert other_calls and other_calls[0]["backfill_other"] is False
    # Primary is posted before the twin.
    assert primary["t"] <= twin["t"]


def test_hook_path_stages_dual_ctx_and_the_drain_writes_both_citations(world, project, monkeypatch):
    mod, state = world
    H.run_main(mod)
    primary_tid = state["emitted"][0]["task_id"]

    from claude_mcp_servers.rl_client.citation_pending import (
        list_pending_for_session,
        read_pending,
    )

    pend = list_pending_for_session("sess-dual-1", project)
    assert len(pend) == 1
    staged = read_pending(pend[0])
    ctx = staged["ctx"]
    assert ctx["dual_log"] is True
    assert ctx["other_embedding_source"] == "arctic"
    assert ctx["other_query_emb"][0] == H.ARCTIC_VEC[0]
    assert ctx["other_nodes"] and all(n["n_emb"] for n in ctx["other_nodes"])

    # The drain's compute (citation_compute.compute_citation is the drain's
    # default compute_fn) must write BOTH citations: T and T:arctic.
    import claude_mcp_servers.weaviate_mcp.server as csrv
    from claude_mcp_servers.rl_client import citation_compute

    written = []

    class _W:
        def log_citations(self, *, task_id, **_kw):
            written.append(task_id)

    class _Svc:
        def embed_text(self, text):
            return [0.3] * 1024

    monkeypatch.setattr(csrv, "_get_embedding_service", lambda: _Svc())
    monkeypatch.setattr(csrv, "_get_rl_telemetry_writer", lambda *a, **k: _W())
    monkeypatch.setattr(csrv, "_get_rl_telemetry_writer_for", lambda *a, **k: _W())
    monkeypatch.setattr(csrv, "_embed_text_in_other_model", lambda *a, **k: [0.6] * 1024)
    res = citation_compute.compute_citation(
        primary_tid, "The answer uses the node about dual logging.", ctx
    )
    assert res is not None
    assert written == [primary_tid, primary_tid + ":arctic"]


@pytest.mark.parametrize("off_env", [
    {"DUAL_RL_LOG_ENABLED": "false"},
    {"DUAL_EMBEDDING_WRITE_ALL_SLOTS": "false"},
])
def test_hook_path_dual_off_posts_exactly_one(world, monkeypatch, off_env):
    mod, state = world
    for k, v in off_env.items():
        monkeypatch.setenv(k, v)
    H.run_main(mod)
    assert [e["task_id"] for e in state["emitted"]] == [state["emitted"][0]["task_id"]]
    assert not state["emitted"][0]["task_id"].endswith(":arctic")
    assert state["svc"].calls == 0, "no secondary embed when the gate is off"
    assert _loss_lines() == [], "a closed gate is not a loss"


def test_hook_path_slow_secondary_keeps_primary_and_counts_the_skip(world, monkeypatch):
    mod, state = world
    state["svc"].delay_s = 3.0
    monkeypatch.setattr(mod, "HOOK_DUAL_EMBED_BUDGET_S", 0.2)
    t0 = time.monotonic()
    H.run_main(mod)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"main() must return within budget + epsilon, took {elapsed:.2f}s"
    assert len(state["emitted"]) == 1 and not state["emitted"][0]["task_id"].endswith(":arctic")
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [("dual_skip", "secondary_embed_timeout")]
    assert lines[0]["task_type"] == "pre_edit_kg_search"


def test_hook_process_latency_holds_with_a_hung_secondary(tmp_path):
    """The budget must hold at PROCESS level: a hung embed thread must not keep
    the hook's interpreter alive (``asyncio.run`` joins its default executor;
    the budgeted path uses a daemon thread for exactly this reason)."""
    timings = {}
    for label, delay, dual in (("off", 0.0, "false"), ("fast", 0.05, "true"), ("hung", 30.0, "true")):
        res_path = tmp_path / f"res_{label}.json"
        cfg = {
            "collections": ["ProjA_KnowledgeGraph"],
            "embed_delay_s": delay,
            "result_path": str(res_path),
        }
        cfg_path = tmp_path / f"cfg_{label}.json"
        cfg_path.write_text(json.dumps(cfg))
        env = child_env(**{**DUAL_ENV, "CLAUDE_PROJECT_DIR": str(tmp_path),
                           "DUAL_RL_LOG_ENABLED": dual})
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests/common/rl_kg_search_harness.py"), str(cfg_path)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        timings[label] = time.monotonic() - t0
        assert proc.returncode == 0, proc.stderr[-2000:]
        out = json.loads(res_path.read_text())
        timings[label + "_main"] = out["main_elapsed_s"]
        if label == "fast":
            assert [e["task_id"].endswith(":arctic") for e in out["emitted"]] == [False, True]
        else:  # off: single-log by the gate; hung: twin skipped by the budget
            assert [e["task_id"].endswith(":arctic") for e in out["emitted"]] == [False]
    # The hung run costs at most the 1 s budget over the fast one, end to end.
    assert timings["hung"] - timings["fast"] < 1.5, timings
    print(f"\nHOOK-LATENCY {json.dumps({k: round(v, 3) for k, v in timings.items()})}")


def test_oversized_hook_query_skips_dual_and_records_it(world, monkeypatch):
    mod, state = world
    from claude_mcp_servers.rl_client import query_chunking as qc

    monkeypatch.setattr(qc, "is_oversized", lambda text, model: True)
    monkeypatch.setattr(qc, "chunk_query", lambda text, model: [text])
    H.run_main(mod)
    assert len(state["emitted"]) == 1
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [("dual_skip", "oversized_query")]


# ---------------------------------------------------------------------------
# F1 — the MCP tools and search_knowledge.py go through the same helper
# ---------------------------------------------------------------------------


def test_mcp_cache_and_rerank_threads_dual_inputs(monkeypatch):
    captured = []

    async def _fake_rerank(req):
        captured.append(req)
        return sp.RerankResult(ranked=[], task_id=req.task_id, rl_used=False, emit_success=True)

    monkeypatch.setattr(_live("claude_mcp_servers.rl_client.search_pipeline"), "rerank_and_emit", _fake_rerank)
    dual = {
        "other_slot": H.OTHER_SLOT, "other_source": "arctic", "other_dim": 1024,
        "other_model": "snowflake-arctic-embed2:latest", "other_query_emb": list(H.ARCTIC_VEC),
    }
    asyncio.run(srv._rl_cache_and_rerank("t1", "q", [{"title": "x"}], 1, dual_log_inputs=dual))
    req = captured[0]
    assert req.dual_log is True
    assert req.other_embedding_source == "arctic"
    assert req.other_embedding_dim == 1024
    assert req.other_query_emb == H.ARCTIC_VEC


def test_shared_helper_mcp_shape_backfills_and_is_unbudgeted(monkeypatch, project):
    for k, v in DUAL_ENV.items():
        monkeypatch.setenv(k, v)
    state: dict = {}
    H.install_fakes(
        _live("weaviate_mcp.server"), _live("claude_mcp_servers.rl_client.search_pipeline"),
        cfg={}, state=state, patch=monkeypatch.setattr,
    )
    nodes = [{"title": "n1"}]
    dual = asyncio.run(srv.resolve_and_enrich_dual(
        nodes, query="q", query_vector=list(H.ACTIVE_VEC), active_slot=H.ACTIVE_SLOT,
        model_name="qwen3-embedding:0.6b", task_type="mcp_interactive",
    ))
    assert dual["other_source"] == "arctic"
    assert state["enrich_calls"][0]["backfill_other"] is True
    assert nodes[0]["emb_other"][0] == H._node_vec("n1", H.OTHER_SLOT)[0]


def test_search_knowledge_cli_emits_the_twin(monkeypatch, project, tmp_path):
    """templates/scripts/search_knowledge.py routes through the same helper."""
    for k, v in DUAL_ENV.items():
        monkeypatch.setenv(k, v)
    state: dict = {}
    H.install_fakes(
        _live("weaviate_mcp.server"), _live("claude_mcp_servers.rl_client.search_pipeline"),
        cfg={}, state=state, patch=monkeypatch.setattr,
    )
    sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))
    import importlib

    sys.modules.pop("search_knowledge", None)  # fresh: bind the LIVE pipeline/server modules
    sk = importlib.import_module("search_knowledge")
    if not sk.HAS_RL_PIPELINE or sk._resolve_and_enrich_dual is None:
        pytest.fail("search_knowledge must import the pipeline and the dual helper")
    monkeypatch.setattr(sk, "get_embedding", lambda q: list(H.ACTIVE_VEC))
    monkeypatch.setattr(sk, "get_weaviate_client", lambda: H.FakeClient(state.setdefault("queried", [])))
    monkeypatch.setattr(sk, "_get_target_vector_slot", lambda: H.ACTIVE_SLOT)
    monkeypatch.setattr(sk, "_ACTIVE_EMBEDDING_SOURCE", "qwen3")
    monkeypatch.setattr(sk, "_ACTIVE_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    sk.search_knowledge("dual log query", limit=1, detail="titles",
                        collections=["ProjA_KnowledgeGraph"])
    ids = [e["task_id"] for e in state["emitted"]]
    assert len(ids) == 2 and ids[1] == ids[0] + ":arctic", ids
    assert state["emitted"][1]["task_type"] == "kg_search_cli"


# ---------------------------------------------------------------------------
# F2 — the twin record carries the same node fields
# ---------------------------------------------------------------------------


def test_twin_node_keys_match_primary_node_keys():
    cands = [
        {
            "title": f"n{i}", "score": 0.9 - i * 0.1, "node_type": "concept",
            "emb": [0.1] * 4, "n_emb": [0.1] * 4, "cos_qn": 0.7, "emb_truncated": False,
            "chunks_matched": 3, "best_chunk_number": 2,
            "emb_other": [0.2] * 4, "cos_qn_other": 0.6, "emb_other_truncated": False,
        }
        for i in range(3)
    ]
    ranked = [cands[2], cands[0]]
    primary = sp._build_log_nodes(cands, 2)
    sp._stamp_shown_ranks(primary, ranked)
    twin = sp._build_other_slot_log_nodes(cands, 2, ranked)
    assert [set(r) for r in twin] == [set(r) for r in primary]
    for p, t in zip(primary, twin):
        for k in ("shown_rank", "chunks_matched", "best_chunk_number", "tier", "title", "score"):
            assert p.get(k) == t.get(k), k
        assert t["emb"] == [0.2] * 4 and t["n_emb"] == [0.2] * 4 and t["cos_qn"] == 0.6
    assert {r.get("shown_rank") for r in twin} == {0, 1, None}


def test_twin_never_carries_active_space_link_features():
    cand = {
        "title": "n", "score": 0.5, "emb": [0.1] * 4, "linked_embs": [[0.1] * 4],
        "linked_type_names": ["concept"], "cos_ql": 0.3, "cos_nl": 0.2,
        "emb_other": [0.2] * 4,
    }
    (twin,) = sp._build_other_slot_log_nodes([cand], 1)
    for k in ("linked_embs", "linked_type_names", "cos_ql", "cos_nl"):
        assert k not in twin


def test_pipeline_twin_event_carries_shown_rank(monkeypatch):
    emitted = []
    monkeypatch.setattr(_live("claude_mcp_servers.rl_client.search_pipeline"), "emit_rl_event", lambda ev, writer_factory=None: emitted.append(ev) or True)
    monkeypatch.setattr(_live("claude_mcp_servers.rl_client.search_pipeline"), "_resolve_rl_enabled", lambda: False)
    monkeypatch.setattr(_live("claude_mcp_servers.rl_client.search_pipeline"), "_retrieval_emit_has_consumer", lambda: True)
    monkeypatch.setattr(_live("claude_mcp_servers.rl_client.search_pipeline"), "_populate_citation_cache", lambda **k: None)
    req = sp.RerankRequest(
        query="q",
        candidates=[{"title": "a", "score": 0.9, "emb": [0.1] * 4, "emb_other": [0.2] * 4,
                     "chunks_matched": 2, "best_chunk_number": 1}],
        limit=1, query_emb=[0.1] * 4, embedding_source="qwen3", embedding_dim=4,
        embedding_model="qwen3-embedding:0.6b", task_id="T", spawn_answer_monitor=False,
        **sp.dual_log_request_fields({"other_source": "arctic", "other_dim": 4,
                                      "other_model": "m", "other_query_emb": [0.2] * 4}),
    )
    asyncio.run(sp.rerank_and_emit(req))
    assert [e.task_id for e in emitted] == ["T", "T:arctic"]
    twin_node = emitted[1].nodes[0]
    assert twin_node["shown_rank"] == 0
    assert twin_node["chunks_matched"] == 2 and twin_node["best_chunk_number"] == 1


# ---------------------------------------------------------------------------
# F4 — hub POST failures are visible
# ---------------------------------------------------------------------------


@pytest.fixture
def live_poster(monkeypatch, tmp_path):
    from claude_mcp_servers.rl_client import hub_writer

    monkeypatch.setenv("VCT_HUB_ALLOW_TEST_POST", "1")
    monkeypatch.setattr(hub_writer, "_read_hub_port", lambda: 9)
    monkeypatch.setattr(hub_writer, "_RETRY_PAUSE_S", 0.0)
    from vco_lib import rl_telemetry_loss

    rl_telemetry_loss._reset_warned_for_test()
    return hub_writer


EV = {"event_type": "retrieval", "task_id": "T1", "task_type": "pre_edit_kg_search",
      "embedding_source": "arctic", "payload_json": "{}"}


def test_hub_not_running_is_recorded(live_poster, monkeypatch, caplog):
    monkeypatch.setattr(live_poster, "_read_hub_token", lambda: None)
    with caplog.at_level(logging.WARNING):
        assert live_poster.post_rl_event(dict(EV)) is False
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [("hub_post_failed", "hub_not_running")]
    assert lines[0]["task_id"] == "T1" and lines[0]["embedding_source"] == "arctic"
    assert any("RL telemetry loss" in r.getMessage() for r in caplog.records)


def test_refused_connection_is_retried_once_then_recorded(live_poster, monkeypatch):
    monkeypatch.setattr(live_poster, "_read_hub_token", lambda: "tok")
    calls = []

    def _refuse(req, timeout=None):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(live_poster.urllib.request, "urlopen", _refuse)
    assert live_poster.post_rl_event(dict(EV)) is False
    assert len(calls) == 2, "exactly one bounded retry"
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [("hub_post_failed", "connection_refused")]


def test_http_error_is_recorded_without_retry(live_poster, monkeypatch):
    monkeypatch.setattr(live_poster, "_read_hub_token", lambda: "tok")
    calls = []

    def _reject(req, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(live_poster.urllib.request, "urlopen", _reject)
    assert live_poster.post_rl_event(dict(EV)) is False
    assert len(calls) == 1
    assert [(x["kind"], x["reason"]) for x in _loss_lines()] == [("hub_post_failed", "http_500")]


def test_doctor_surfaces_the_loss_ledger():
    from vco_lib import doctor
    from vco_lib.rl_telemetry_loss import record_loss

    record_loss("hub_post_failed", "hub_not_running")
    record_loss("dual_skip", "secondary_embed_timeout")
    res = doctor.DoctorResolvers()
    (f,) = doctor.probe_rl_telemetry_loss(Path("/tmp/x"), res, {})
    assert f.status == doctor.STATUS_OK
    assert "2 RL training event(s) lost" in f.summary
    assert "hub_not_running x1" in f.summary and "secondary_embed_timeout x1" in f.summary
    assert "rl_telemetry_loss" in doctor.PROBES

    unreadable = doctor.DoctorResolvers(rl_loss_summary=lambda since: None)
    (u,) = doctor.probe_rl_telemetry_loss(Path("/tmp/x"), unreadable, {})
    assert u.status == doctor.STATUS_UNKNOWN
