# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""v0.2.102 W6-FIN — the MCP answer monitor takes the drains' pending claim.

The W6-C2 fix round made both Stop-hook drains (parent and subagent) CLAIM a
pending citation file (rename to ``.draining.<pid>``) before computing it.
The in-process MCP answer monitor still computed, wrote, and only THEN
deleted its pending file, so a drain that ran during that compute scored the
same citation a second time. These tests drive the REAL monitor and the REAL
drain against one staged pending file and count citation writes:

* a drain run DURING the monitor's compute writes nothing (monitor first);
* a monitor fired DURING a drain's compute writes nothing (drain first);
* leave-alone: a monitor that cannot score hands the file back, and the
  drain then scores it exactly once;
* a claim left behind by a dead monitor (its MCP process gone) is
  re-adopted by the next drain and scored once;
* the populate path records the staged path in the in-memory ctx only.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402
from claude_mcp_servers.rl_client import citation_pending  # noqa: E402
from claude_mcp_servers.weaviate_mcp import rl_enrichment  # noqa: E402
from tests.common.child_env import child_env  # noqa: E402

SID = "sess-w6fin"
TASK = "w6fintask"
QUERY = "w6fin probe query"
ANSWER = "an answer long enough for the lowered test gate " * 20


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], env=child_env())
    proc.wait()
    return proc.pid


def _transcript(tmp_path: Path) -> str:
    """A transcript the DRAIN can match: the KG tool_use carrying QUERY, then
    the answer text (the monitor's loader is patched to the same messages)."""
    rows = [
        {"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "tu1", "name": "hybrid_search",
              "input": {"query": QUERY}}]}},
        {"type": "assistant", "timestamp": "2099-01-01T00:00:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": ANSWER}]}},
    ]
    p = tmp_path / "parent.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _monitor_messages() -> list[dict]:
    return [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "hybrid_search", "input": {"query": QUERY}},
        {"type": "text", "text": ANSWER},
    ]}}]


def _stage(tmp_path: Path) -> Path:
    path = citation_pending.stage_pending(
        session_id=SID, task_id=TASK, seq=1, query=QUERY,
        ctx={"nodes": [{"title": "n"}], "query_emb": [0.1]}, source="mcp",
        project_root=tmp_path,
    )
    assert path is not None and path.exists()
    return path


def _drain(tmp_path: Path, tp: str, compute_fn) -> dict:
    import rl_drain_citations as drain

    return drain.drain_session(SID, tp, project_root=str(tmp_path),
                               compute_fn=compute_fn, token_count_fn=lambda t: 30_000)


def _run_monitor(monkeypatch, tmp_path: Path, *, staged: "Path | None", compute) -> None:
    """Fire the real ``_rl_answer_monitor`` once (force-flush, gate 1)."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 30.0)
    monkeypatch.setattr(srv, "_RL_MIN_ANSWER_TOKENS_FOR_CITATION", 1)
    monkeypatch.setattr(srv, "_rl_check_force_flush", lambda: True)
    monkeypatch.setattr(srv, "_rl_clear_force_flush", lambda: None)

    async def _transcripts():
        return [Path("fake.jsonl")]

    monkeypatch.setattr(srv, "_rl_find_all_transcripts", _transcripts)
    monkeypatch.setattr(srv, "_rl_load_messages", lambda c: _monitor_messages())
    monkeypatch.setattr(srv, "_rl_compute_and_write_citations", compute)
    monkeypatch.setattr(srv, "_get_rl_client", lambda: None)
    entry: dict = {"nodes": [{"title": "n"}], "query_emb": [0.1]}
    if staged is not None:
        entry[citation_pending.CTX_PENDING_PATH_KEY] = str(staged)
    srv._rl_node_content_cache[TASK] = entry
    try:
        asyncio.run(rl_enrichment._rl_answer_monitor(TASK, 1, QUERY))
    finally:
        srv._rl_node_content_cache.pop(TASK, None)


def _leftovers(staged: Path) -> list[str]:
    return sorted(p.name for p in staged.parent.iterdir())


# ── exactly one write, in both orders ────────────────────────────────────


@pytest.mark.parametrize("record_path", [True, False], ids=["staged-path", "suffix-lookup"])
def test_drain_during_monitor_compute_writes_once(tmp_path, monkeypatch, record_path):
    """Monitor first: a Stop drain that runs while the monitor computes finds
    no pending file to score. RED before W6-FIN: the drain saw the still
    unclaimed file and wrote the same citation a second time."""
    staged = _stage(tmp_path)
    tp = _transcript(tmp_path)
    writes: list[str] = []

    def drain_compute(task_id, answer, ctx, write=True):
        writes.append("drain")
        return {"ok": True}

    async def monitor_compute(task_id, answer, ctx):
        _drain(tmp_path, tp, drain_compute)  # a Stop hook fires mid-compute
        writes.append("monitor")
        assert citation_pending.CTX_PENDING_PATH_KEY not in ctx
        return {"cosine_sims": {}, "literal_cited": {}}

    _run_monitor(monkeypatch, tmp_path, staged=staged if record_path else None,
                 compute=monitor_compute)
    assert writes == ["monitor"]
    assert _leftovers(staged) == [], "a landed write consumes the claimed file"


def test_monitor_during_drain_compute_writes_once(tmp_path, monkeypatch):
    """Drain first: the monitor fires while a drain holds the claim, and
    stands down. RED before W6-FIN: the monitor ignored the drain's claim and
    wrote its own citation too."""
    staged = _stage(tmp_path)
    tp = _transcript(tmp_path)
    writes: list[str] = []

    async def monitor_compute(task_id, answer, ctx):
        writes.append("monitor")
        return {"cosine_sims": {}, "literal_cited": {}}

    def drain_compute(task_id, answer, ctx, write=True):
        _run_monitor(monkeypatch, tmp_path, staged=staged, compute=monitor_compute)
        writes.append("drain")
        return {"ok": True}

    summary = _drain(tmp_path, tp, drain_compute)
    assert writes == ["drain"]
    assert summary["computed"] == 1
    assert _leftovers(staged) == []


def test_monitor_after_drain_consumed_does_not_rescore(tmp_path, monkeypatch):
    """A drain that already scored and deleted the file: the monitor's staged
    path is gone, which means "scored" — never "compute again"."""
    staged = _stage(tmp_path)
    tp = _transcript(tmp_path)
    writes: list[str] = []
    _drain(tmp_path, tp, lambda *a, **k: writes.append("drain") or {"ok": True})

    async def monitor_compute(task_id, answer, ctx):
        writes.append("monitor")
        return {"cosine_sims": {}, "literal_cited": {}}

    _run_monitor(monkeypatch, tmp_path, staged=staged, compute=monitor_compute)
    assert writes == ["drain"]


# ── leave-alone: a monitor that cannot score hands the file back ─────────


@pytest.mark.parametrize("outcome", ["none", "raises"])
def test_monitor_that_cannot_score_releases_then_drain_scores_once(
    tmp_path, monkeypatch, outcome,
):
    staged = _stage(tmp_path)
    tp = _transcript(tmp_path)
    writes: list[str] = []
    held_during_compute: list[bool] = []

    async def monitor_compute(task_id, answer, ctx):
        # While the monitor computes it holds the claim: the file is renamed.
        # (Recorded, not asserted here: the monitor swallows compute errors.)
        held_during_compute.append(
            not staged.exists() and any(".draining." in n for n in _leftovers(staged))
        )
        if outcome == "raises":
            raise RuntimeError("embedding service down")
        return None

    _run_monitor(monkeypatch, tmp_path, staged=staged, compute=monitor_compute)
    assert held_during_compute == [True], "the monitor must hold the claim while computing"
    assert staged.exists(), "the claim must be handed back on every leave path"
    assert _leftovers(staged) == [staged.name]

    _drain(tmp_path, tp, lambda *a, **k: writes.append("drain") or {"ok": True})
    assert writes == ["drain"]
    assert _leftovers(staged) == []


def test_cancelled_monitor_releases_its_claim(tmp_path, monkeypatch):
    """A monitor cancelled mid-compute (MCP shutdown) still hands the file
    back — the claim is released in ``finally``, not only on return."""
    staged = _stage(tmp_path)

    async def monitor_compute(task_id, answer, ctx):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run_monitor(monkeypatch, tmp_path, staged=staged, compute=monitor_compute)
    assert _leftovers(staged) == [staged.name]


def test_unstaged_retrieval_is_still_computed(tmp_path, monkeypatch):
    """No pending file was ever staged (stage soft-failed / no project root):
    no drain can see it, so the monitor computes it — skipping would lose it."""
    writes: list[str] = []

    async def monitor_compute(task_id, answer, ctx):
        writes.append("monitor")
        return {"cosine_sims": {}, "literal_cited": {}}

    _run_monitor(monkeypatch, tmp_path, staged=None, compute=monitor_compute)
    assert writes == ["monitor"]


# ── a stale claim left by a dead monitor is re-adopted ───────────────────


def test_dead_monitor_claim_is_readopted_and_scored_once(tmp_path, monkeypatch):
    """The MCP process died mid-compute: its claim names a dead pid. The next
    drain re-adopts it and scores it once — the crash costs a retry only."""
    staged = _stage(tmp_path)
    tp = _transcript(tmp_path)
    claim = staged.with_name(f"{staged.name}{citation_pending.CLAIM_MARK}{_dead_pid()}")
    staged.replace(claim)
    writes: list[str] = []
    summary = _drain(tmp_path, tp, lambda *a, **k: writes.append("drain") or {"ok": True})
    assert summary["readopted"] == 1
    assert writes == ["drain"]
    assert _leftovers(staged) == []


def test_monitor_stands_down_on_a_live_claim_found_by_suffix(tmp_path, monkeypatch):
    """Without a recorded staged path, a live claim of this task (another
    process mid-compute) is still recognised as taken."""
    staged = _stage(tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              env=child_env())
    try:
        staged.replace(staged.with_name(f"{staged.name}{citation_pending.CLAIM_MARK}{holder.pid}"))
        state, claimed, path = citation_pending.claim_task_pending(TASK, project_root=tmp_path)
        assert (state, claimed, path) == ("taken", None, staged)
    finally:
        holder.kill()
        holder.wait()


# ── the populate path records the staged path in memory only ─────────────


def test_populate_records_staged_path_in_memory_only(tmp_path, monkeypatch):
    from unittest.mock import patch

    from claude_mcp_servers.rl_client import search_pipeline
    from claude_mcp_servers.weaviate_mcp import rl_state

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    with patch.object(srv, "_rl_node_content_cache", {}), \
         patch.object(rl_state, "_rl_node_content_cache_sizes", {}), \
         patch.object(srv, "_try_resolve_project_config", return_value=None):
        search_pipeline._populate_citation_cache(
            task_id="tid-pop", candidates=[{"title": "A", "score": 0.9}], limit=5,
            query_emb=[0.1, 0.2], embedding_source="ollama", embedding_dim=2,
            embedding_model="m", task_type="mcp_interactive", session_id=SID,
            query=QUERY, stage_pending_file=True,
        )
        entry = srv._rl_node_content_cache["tid-pop"]
    files = citation_pending.list_pending_for_session(SID, tmp_path)
    assert len(files) == 1
    assert entry[citation_pending.CTX_PENDING_PATH_KEY] == str(files[0])
    on_disk = json.loads(files[0].read_text())
    assert citation_pending.CTX_PENDING_PATH_KEY not in on_disk["ctx"]
