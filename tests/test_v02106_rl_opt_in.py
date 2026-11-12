# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — RL scoring is OPT-IN per project (owner rulings 2026-11-12).

The scoring lock (``vco_lib/rl_scoring_lock.toml``) is lifted; only a project
whose toggle is positively ON calls the container. The hub serves the
effective toggle (the user's choice, else the bundled-weights default —
``vco_lib/rl_scoring_default.py``, off while no trained model ships; pinned in
``tests/test_v02106_rl_scoring_default.py`` and the hub/launcher Rust tests). Every reader of "may I call the container?" is pinned here:

* ``search_pipeline._resolve_rl_enabled`` — falls CLOSED when the toggle
  cannot be read (it fell open before v0.2.106);
* ``project_config`` — an absent field is off;
* the answer monitor's ``/rl_update`` — gated by the same rule (it fired
  whenever a container was wired before v0.2.106);
* ``claude_md_sections._rl_renders`` — the same default as the pipeline;
* ``rl_doctor`` — an unknown toggle is not "enabled";
* the client: a container with the per-project gate is called even without
  trained weights, and its "search order" answer is a healthy non-rerank.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from claude_mcp_servers.rl_client import search_pipeline
from claude_mcp_servers.rl_client.client import RLClient
from claude_mcp_servers.scripts import rl_doctor
from claude_mcp_servers.weaviate_mcp import rl_enrichment
from claude_mcp_servers.weaviate_mcp import server as srv
from vco_lib import claude_md_sections, rl_scoring_lock

httpx = pytest.importorskip("httpx")


# ─── the shipped table ────────────────────────────────────────────────


def test_the_lock_is_lifted_with_the_owner_ruling_recorded():
    assert rl_scoring_lock.rl_scoring_lock_reason() is None
    text = Path(rl_scoring_lock.__file__).with_name("rl_scoring_lock.toml").read_text()
    assert "2026-11-12" in text and "OPT-IN" in text and "+2%" in text


# ─── the pipeline gate ────────────────────────────────────────────────


def _cfg(**kw):
    return SimpleNamespace(**kw)


@pytest.mark.parametrize("resolver, expected", [
    (lambda: None, False),                                         # hub down / no config
    (lambda: _cfg(), False),                                       # field absent
    (lambda: _cfg(rl_reranker_enabled_for_project=False), False),  # the default
    (lambda: _cfg(rl_reranker_enabled_for_project="yes"), False),  # not a positive bool
    (lambda: _cfg(rl_reranker_enabled_for_project=True), True),    # the user's opt-in
])
def test_rl_scoring_runs_only_on_a_positive_opt_in(resolver, expected):
    with patch.object(search_pipeline, "_rl_scoring_lock_reason", return_value=None), \
            patch.object(srv, "_try_resolve_project_config", side_effect=resolver):
        assert search_pipeline._resolve_rl_enabled() is expected


def test_a_toggle_probe_error_is_off():
    def boom():
        raise RuntimeError("hub exploded")

    with patch.object(search_pipeline, "_rl_scoring_lock_reason", return_value=None), \
            patch.object(srv, "_try_resolve_project_config", side_effect=boom):
        assert search_pipeline._resolve_rl_enabled() is False


def test_toggle_off_means_no_container_call_at_all():
    """locked=false + toggle off: rerank_and_emit never builds an RL client
    and returns the search order unchanged."""
    nodes = [{"title": "b", "score": 0.9}, {"title": "a", "score": 0.8}]
    with patch.object(search_pipeline, "_rl_scoring_lock_reason", return_value=None), \
            patch.object(srv, "_try_resolve_project_config",
                         return_value=_cfg(rl_reranker_enabled_for_project=False)), \
            patch.object(search_pipeline, "_retrieval_emit_has_consumer", return_value=False), \
            patch.object(search_pipeline, "_should_capture_citations", return_value=False), \
            patch.object(search_pipeline, "_drive_retention_housekeeping", return_value=None), \
            patch.object(srv, "_get_rl_client") as getter:
        res = asyncio.run(search_pipeline.rerank_and_emit(search_pipeline.RerankRequest(
            query="q", candidates=nodes, limit=2, query_emb=[0.1] * 4, task_id="t")))
    getter.assert_not_called()
    assert res.rl_used is False and res.ranked == nodes


# ─── project_config default ───────────────────────────────────────────


def test_project_config_dataclass_default_is_off():
    from vco_lib.project_config import ProjectConfig

    field = ProjectConfig.__dataclass_fields__["rl_reranker_enabled_for_project"]
    assert field.default is False


# ─── the answer monitor's /rl_update ──────────────────────────────────


def _drive_monitor(monkeypatch, tmp_path, *, scoring_on: bool):
    """One monitor fire that reaches the /rl_update branch; returns the
    fake client's rl_update_v3 mock."""
    from claude_mcp_servers.rl_client import citation_pending

    task_id = "optin-task"
    query = "opt-in probe query"
    ctx = {"nodes": [{"title": "n", "n_emb": [0.1] * 4}], "query_emb": [0.1] * 4}
    citation_pending.stage_pending(session_id="s1", task_id=task_id, seq=1, query=query,
                                   ctx=ctx, source="mcp", project_root=tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    for k in ("RL_ONLINE_TRAINING_DISABLED", "RL_ONLINE_TRAINING_DISABLED_GLOBAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 30.0)
    monkeypatch.setattr(srv, "_RL_MIN_ANSWER_TOKENS_FOR_CITATION", 1)
    monkeypatch.setattr(srv, "_rl_check_force_flush", lambda: True)
    monkeypatch.setattr(srv, "_rl_clear_force_flush", lambda: None)

    async def _transcripts():
        return [Path("fake.jsonl")]

    monkeypatch.setattr(srv, "_rl_find_all_transcripts", _transcripts)
    monkeypatch.setattr(srv, "_rl_load_messages", lambda c: [{
        "type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "hybrid_search", "input": {"query": query}},
            {"type": "text", "text": "an answer long enough for the lowered test gate"},
        ]}}])
    srv._rl_node_content_cache[task_id] = ctx

    async def _compute(tid, ans, c):
        return {"cosine_sims": {"n": 0.5}, "literal_cited": {"n": False}}

    monkeypatch.setattr(srv, "_rl_compute_and_write_citations", _compute)
    client = SimpleNamespace(enabled=True, rl_update_v3=AsyncMock(
        return_value=SimpleNamespace(ok=True, error=None, skipped=None)))
    monkeypatch.setattr(srv, "_get_rl_client", lambda: client)
    monkeypatch.setattr(search_pipeline, "_resolve_rl_enabled", lambda: scoring_on)
    try:
        asyncio.run(rl_enrichment._rl_answer_monitor(task_id, 1, query))
    finally:
        srv._rl_node_content_cache.pop(task_id, None)
    return client.rl_update_v3


def test_the_monitor_sends_no_citation_to_the_container_when_scoring_is_off(tmp_path,
                                                                            monkeypatch):
    assert _drive_monitor(monkeypatch, tmp_path, scoring_on=False).await_count == 0


def test_the_monitor_sends_the_citation_when_the_project_opted_in(tmp_path, monkeypatch):
    sent = _drive_monitor(monkeypatch, tmp_path, scoring_on=True)
    assert sent.await_count == 1
    assert sent.await_args.kwargs["cosine_sims"] == {"n": 0.5}


# ─── the CLAUDE.md render gate ────────────────────────────────────────


@pytest.mark.parametrize("cfg, renders", [
    (_cfg(), False),
    (_cfg(rl_reranker_enabled_for_project=False), False),
    (_cfg(rl_reranker_enabled_for_project=True), True),
])
def test_the_rl_section_follows_the_opt_in(tmp_path, monkeypatch, cfg, renders):
    from vco_lib import project_config

    monkeypatch.setattr(rl_scoring_lock, "rl_scoring_lock_reason", lambda: None)
    monkeypatch.setattr(project_config, "resolve", lambda folder: cfg)
    assert claude_md_sections._rl_renders(tmp_path) is renders


# ─── rl_doctor ────────────────────────────────────────────────────────


_UNLOCKED = {"status": "unlocked", "locked": False, "detail": ""}


def _diag(toggle, container, online=None):
    with patch.object(rl_doctor, "_probe_scoring_lock", return_value=_UNLOCKED), \
            patch.object(rl_doctor, "_probe_per_project_toggle", return_value=toggle), \
            patch.object(rl_doctor, "_probe_container", return_value=container), \
            patch.object(rl_doctor, "_probe_project_online",
                         return_value=online or {"status": "not_checked", "detail": ""}), \
            patch.object(rl_doctor, "_probe_telemetry_hub",
                         return_value={"status": "hub_up", "reachable": True, "detail": ""}), \
            patch.object(rl_doctor, "_probe_retention", return_value={"status": "active", "detail": ""}), \
            patch.object(rl_doctor, "_probe_fallback_counter",
                         return_value={"status": "none", "count": 0, "detail": ""}):
        return rl_doctor.run_diagnostics("/tmp/x")


_COMPAT = {"status": "compatible", "compatible": True, "reachable": True,
           "per_project_gate": True, "detail": ""}


def test_doctor_an_unknown_toggle_is_not_enabled():
    rep = _diag({"status": "unknown", "enabled": None, "detail": ""}, _COMPAT)
    assert rep["rl_enabled"] is False
    assert rep["scoring"]["status"] == "off_toggle_unknown"


def test_doctor_names_a_project_whose_model_is_not_served():
    rep = _diag({"status": "enabled", "enabled": True, "project_id": "p", "detail": ""}, _COMPAT,
                {"status": "not_serving", "reason": "NDCG@10 +0.40% vs plain cosine; needs +2.00%",
                 "detail": "x"})
    assert rep["healthy"] is True and rep["scoring"]["scoring"] is False
    assert rep["scoring"]["status"] == "off_not_served"
    assert "needs +2.00%" in rep["scoring"]["detail"]
    assert "[per-project online learning] not_serving" in rl_doctor.format_human(rep)


def test_doctor_on_names_the_projects_own_model():
    rep = _diag({"status": "enabled", "enabled": True, "project_id": "p", "detail": ""}, _COMPAT,
                {"status": "serving", "reason": "NDCG@10 +2.40% over plain cosine", "detail": "x"})
    assert rep["scoring"]["status"] == "on" and "this project's own model" in rep["scoring"]["detail"]


def test_doctor_reads_project_status_through_the_client():
    class _T:
        def __init__(self):
            self.headers = None

        async def get(self, url, timeout=None, headers=None):
            self.headers = headers
            assert url.endswith("/project_status")
            return httpx.Response(200, json={"ok": True, "serving": False, "reason": "collecting",
                                             "online": {"counters": {"events_seen": 3},
                                                        "record": {"queries": 3,
                                                                   "cited_queries": 1},
                                                        "bar": {"min_queries": 300,
                                                                "min_cited_queries": 50},
                                                        "checks": 0,
                                                        "served_searches": {"queries": 0}}})

        async def aclose(self):
            pass

    t = _T()
    real = RLClient

    def _client(**kw):
        return real(client=t, **kw)

    with patch("claude_mcp_servers.rl_client.client.RLClient", _client):
        got = rl_doctor._probe_project_online(
            {**_COMPAT, "client_base_url": "http://127.0.0.1:9", "client_text_dim": 4,
             "client_active_embedding": "qwen3"},
            {"enabled": True, "project_id": "proj-1"})
    assert got["status"] == "not_serving" and got["reason"] == "collecting"
    assert "prequential record 3 queries (1 citing; a verdict needs 300 / 50)" in got["detail"]
    assert "0 checks" in got["detail"] and "no search served by the model yet" in got["detail"]
    assert t.headers["X-VCT-Project-ID"] == "proj-1"


# ─── the client ───────────────────────────────────────────────────────


class _Transport:
    def __init__(self, health: dict, answer: dict):
        self.health, self.answer, self.posts = health, answer, 0

    async def get(self, url, timeout=None):
        return httpx.Response(200, json=self.health)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posts += 1
        return httpx.Response(200, json=self.answer)

    async def aclose(self):
        pass


_HEALTH = {"ok": True, "model": "not trained", "trained": False, "protocol_version": 2,
           "embedding_dim": 4, "embedding_space": "qwen3", "linked_slots_masked": False,
           "per_project_gate": True}
_NOT_SERVED = {"ok": True, "scored": False, "serving": "search_order",
               "reason": "collecting queries scored before training (3/300 queries, 1/50 citing a node)", "order": [0, 1],
               "scores": [None, None], "top_k": []}


def _nodes():
    return [{"title": "b", "emb": [0.1, 0.2, 0.3, 0.4]}, {"title": "a", "emb": [0.4, 0.3, 0.2, 0.1]}]


def test_a_gated_container_is_called_without_trained_weights():
    c = RLClient(base_url="http://127.0.0.1:9", text_dim=4, active_embedding="qwen3",
                 client=_Transport(_HEALTH, _NOT_SERVED))
    neg = asyncio.run(c.negotiate())
    assert neg.compatible is True and neg.status == "compatible" and neg.per_project_gate is True
    assert "per-project gate" in neg.detail


def test_without_the_gate_an_untrained_container_stays_uncalled():
    h = {**_HEALTH, "per_project_gate": None}
    c = RLClient(base_url="http://127.0.0.1:9", text_dim=4, active_embedding="qwen3",
                 client=_Transport(h, _NOT_SERVED))
    assert asyncio.run(c.negotiate()).status == "not_trained"


def test_a_search_order_answer_is_healthy_and_not_resent_for_a_minute(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(search_pipeline, "_WARNED_RL_FALLBACK", False)
    t = _Transport(_HEALTH, _NOT_SERVED)
    c = RLClient(base_url="http://127.0.0.1:9", text_dim=4, active_embedding="qwen3", client=t)
    nodes = _nodes()

    async def run():
        with patch("claude_mcp_servers.weaviate_mcp.server._get_rl_client", return_value=c):
            a = await search_pipeline._do_rerank(query="q", candidates=nodes, limit=2,
                                                 task_id="t", session_id="s", query_emb=[0.1] * 4)
            b = await search_pipeline._do_rerank(query="q", candidates=nodes, limit=2,
                                                 task_id="t2", session_id="s", query_emb=[0.1] * 4)
            return a, b

    a, b = asyncio.run(run())
    assert a is None and b is None  # not reranked: the caller keeps its order
    assert c.last_call_ok is True and c.last_served is False
    assert c.last_not_served_reason == "collecting queries scored before training (3/300 queries, 1/50 citing a node)"
    assert t.posts == 1  # the second search did not resend the vectors
    counter = tmp_path / ".claude" / "state" / "rl_fallback_counter.json"
    assert not counter.exists() or json.loads(counter.read_text()).get("count", 0) == 0
