# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""G4 (answer-embedding persistence) + G5 (soft labels) — 2026-07-22.

G4: the answer-capture path already chunks + embeds the answer and computes cosine
citations, but historically persisted only the derived cosine scalars. This
persists the answer-chunk EMBEDDINGS themselves (+ chunk-text hashes for dedup),
tagged by the event's embedding triple, so citation labels can be re-derived
offline for a DIFFERENT embedding profile (the second RL net) or a retuned target
formula. Under dual-log the OTHER slot's answer embeddings are persisted too, so
BOTH nets are fully trainable-with-labels.

G5: a below-terminal-floor answer window is emitted as a SOFT label
(soft_label=True) rather than dropped, so a short-answer retrieval still gets a
(down-weightable) label instead of dying unlabeled at the TTL sweep.

Red-proof: pre-fix, the citation event carried NO answer_chunk_embs — a second
embedding profile could never re-derive labels for a historical event. These
tests assert the field is now present and populated in the event actually built.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402

pytest.importorskip(
    "weaviate_mcp.server",
    reason="weaviate_mcp.server must be importable for the G4/G5 tests",
)


def _capture_writer(captured: list) -> RLTelemetryWriter:
    def _post(envelope, timeout: float = 2.0) -> bool:
        captured.append(envelope)
        return True

    return RLTelemetryWriter(
        project="sample-project",
        embedding_source="qwen3",
        embedding_dim=8,
        embedding_model="qwen3-embedding:0.6b",
        hub_post_fn=_post,
    )


class _Svc:
    """Fake embedding service returning a fixed 8-dim vector per call."""

    def __init__(self):
        self.text_calls = []

    def embed_text(self, text):
        self.text_calls.append(text)
        return [1.0] + [0.0] * 7

    def embed_code(self, text):  # pragma: no cover - not used by KG ctx
        raise AssertionError("KG ctx must not embed_code")


def _srv():
    return importlib.import_module("claude_mcp_servers.weaviate_mcp.server")


def _kg_ctx():
    return {
        "nodes": [
            {"title": "Alpha", "n_emb": [1.0] + [0.0] * 7},
            {"title": "Beta", "n_emb": [0.0, 1.0] + [0.0] * 6},
        ],
        "active_model": "qwen3-embedding:0.6b",
        "embedding_source": "qwen3",
        "embedding_dim": 8,
        "task_type": "mcp_interactive",
        "session_id": "sess-g4",
    }


def _emitted_citation(captured: list) -> dict:
    assert captured, "expected a citation event to be posted"
    return json.loads(captured[0]["payload_json"])


def test_g4_answer_chunk_embeddings_persisted(monkeypatch):
    from claude_mcp_servers.rl_client.citation_compute import compute_citation

    svc = _Svc()
    captured: list = []
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: svc)
    monkeypatch.setattr(
        _srv(), "_get_rl_telemetry_writer", lambda: _capture_writer(captured)
    )

    result = compute_citation(
        "task-g4",
        "The answer references Alpha and Beta with detail. " * 3,
        _kg_ctx(),
        write=True,
    )
    assert result is not None
    ev = _emitted_citation(captured)
    # G4: the answer-chunk embeddings + hashes are persisted in the event.
    assert ev.get("answer_chunk_embs"), (
        "citation event must carry answer_chunk_embs (G4) — without them labels "
        "cannot be re-derived for a second embedding profile"
    )
    assert ev.get("answer_chunk_hashes"), "answer_chunk_hashes must be present (G4 dedup)"
    assert len(ev["answer_chunk_embs"]) == len(ev["answer_chunk_hashes"]), (
        "embeddings and hashes must be index-aligned"
    )
    # Embeddings are rounded 8-dim vectors matching the fake service output.
    assert len(ev["answer_chunk_embs"][0]) == 8
    # cosine_sims still present (unchanged behaviour).
    assert ev.get("cosine_sims")


def test_g4_pre_fix_had_no_answer_embeddings():
    """RED-PROOF: a citation event built WITHOUT the new args carries no
    answer_chunk_embs — the pre-fix state where labels were frozen in the active
    embedding space."""
    w = RLTelemetryWriter(
        project="p", embedding_source="qwen3", embedding_dim=8,
        embedding_model="qwen3-embedding:0.6b", hub_post_fn=lambda *a, **k: True,
    )
    ev = w._build_v3_citation_event(
        task_id="t", task_type="mcp_interactive",
        citations={"Alpha": True}, cosine_sims={"Alpha": 0.9},
    )
    assert "answer_chunk_embs" not in ev, (
        "pre-fix / no-arg build must omit answer_chunk_embs (proves the field is "
        "opt-in and the fix is what populates it)"
    )
    # And WITH the args it appears.
    ev2 = w._build_v3_citation_event(
        task_id="t", task_type="mcp_interactive",
        citations={"Alpha": True}, cosine_sims={"Alpha": 0.9},
        answer_chunk_embs=[[0.1] * 8, [0.2] * 8],
        answer_chunk_hashes=["h1", "h2"],
    )
    assert ev2["answer_chunk_embs"] == [[0.1] * 8, [0.2] * 8]
    assert ev2["answer_chunk_hashes"] == ["h1", "h2"]


def test_g5_soft_label_propagates_to_event():
    """G5: the soft_label marker the drain stamps onto ctx must reach the stored
    citation event so the trainer can down-weight it."""
    w = RLTelemetryWriter(
        project="p", embedding_source="qwen3", embedding_dim=8,
        embedding_model="qwen3-embedding:0.6b", hub_post_fn=lambda *a, **k: True,
    )
    ev = w._build_v3_citation_event(
        task_id="t", task_type="mcp_interactive",
        citations={"Alpha": True}, cosine_sims={"Alpha": 0.6},
        fire_reason="soft_terminal", soft_label=True,
    )
    assert ev.get("soft_label") is True
    assert ev.get("fire_reason") == "soft_terminal"
    # Normal (non-soft) path omits the flag.
    ev2 = w._build_v3_citation_event(
        task_id="t", task_type="mcp_interactive",
        citations={"Alpha": True}, cosine_sims={"Alpha": 0.9},
    )
    assert "soft_label" not in ev2


def test_g5_drain_soft_labels_below_terminal_floor(monkeypatch, tmp_path):
    """G5 end-to-end at the drain: an AGING pending file whose window is below the
    terminal floor but above the soft floor is computed as a soft label rather
    than left to die."""
    import time as _time
    from claude_mcp_servers.scripts import rl_drain_citations as drain
    from claude_mcp_servers.rl_client import citation_pending

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # Force the floors: gate high, terminal floor high, soft floor low, age=0.
    monkeypatch.setenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "25000")
    monkeypatch.setenv("RL_TERMINAL_CITATION_MIN_TOKENS", "2000")
    monkeypatch.setenv("RL_SOFT_CITATION_MIN_TOKENS", "10")
    monkeypatch.setenv("RL_TERMINAL_CITATION_AGE_SECONDS", "0")

    # Stage a pending file aged 1800s (older than terminal age 0, younger than
    # the 3600s TTL so the sweep-first step does not reclaim it).
    ctx = {
        "nodes": [{"title": "Alpha", "n_emb": [1.0, 0.0]}],
        "active_model": "qwen3-embedding:0.6b",
        "embedding_source": "qwen3", "embedding_dim": 2,
        "task_type": "mcp_interactive", "session_id": "sess-soft",
    }
    # Control ts_ms by patching time.time inside citation_pending during staging.
    old_wall = _time.time() - 1800
    monkeypatch.setattr(
        citation_pending.time, "time", lambda: old_wall, raising=False
    )
    citation_pending.stage_pending(
        session_id="sess-soft", task_id="task-soft", seq=None,
        query="short answer query", ctx=ctx, source="mcp",
    )
    # Restore real time for the drain's age computation (uses time.time directly).
    monkeypatch.setattr(citation_pending.time, "time", _time.time, raising=False)

    seen = {}

    def _fake_compute(task_id, answer, c, write=True):
        seen["ctx"] = dict(c)
        seen["answer"] = answer
        return {"cosine_sims": {"Alpha": 0.5}}

    # A tiny answer window (~15 tokens) — below terminal (2000) but above soft (10).
    def _fake_token_count(text):
        return 15

    # Patch the answer_window helpers used inside drain_session so the tiny
    # window is returned for our task regardless of transcript contents.
    import claude_mcp_servers.rl_client.answer_window as aw

    monkeypatch.setattr(aw, "load_messages_cached", lambda p: [{"role": "assistant"}])
    monkeypatch.setattr(aw, "find_kg_positions", lambda m: [])
    monkeypatch.setattr(aw, "match_position_for_query", lambda *a, **k: None)
    monkeypatch.setattr(aw, "match_position_by_timestamp", lambda *a, **k: (0, 0))
    monkeypatch.setattr(
        aw, "extract_answer_window", lambda *a, **k: ("a short but real answer", True)
    )

    summary = drain.drain_session(
        "sess-soft", transcript_path="/fake/transcript.jsonl",
        project_root=str(tmp_path),
        compute_fn=_fake_compute, token_count_fn=_fake_token_count,
    )

    assert summary["computed"] == 1, "soft-label path must compute the citation"
    assert seen["ctx"].get("soft_label") is True, "ctx must carry soft_label"
    assert seen["ctx"].get("fire_reason") == "soft_terminal"
