# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Net-input completeness end-to-end (2026-07-22).

Drives a KG retrieval + citation through the REAL emit code with a fake hub sink,
then asserts the persisted (retrieval, citation) pair carries EVERY input the v2
RL net's forward pass consumes — mapped one-to-one:

  net input (rl_model._build_inputs / forward)   →  log field (persisted event)
  --------------------------------------------------------------------------
  q_raw   (entities[:,0,:])   query embedding     →  retrieval.query_emb
  n_raw   (entities[:,1,:])   target node emb     →  retrieval.nodes[].emb
  linked_raws (entities[:,2:7]) 5 linked slots    →  retrieval.nodes[].linked_embs
  n_type_idx  (type_idxs[:,1])                    →  retrieval.nodes[].node_type
  linked_type_idxs (type_idxs[:,2:7])            →  retrieval.nodes[].linked_type_names
  slot cosines (derived from the raw embeddings)  →  (recomputed offline — inputs above suffice)
  near-chunk context (three_chunks/full tiers)    →  retrieval.nodes[].linked_embs (extra chunks)
  training label (BCE target)                     →  citation.cosine_sims / literal_cited
  answer artifacts (re-derive labels/2nd space)   →  citation.answer_chunk_embs / answer_chunk_hashes
  embedding-space attribution                     →  event.embedding_source/dim/model

The retrieval node record is built by the REAL serialize_node_record; the citation
by the REAL compute_citation. Verification of LIVE new rows still requires an MCP
restart (session reload) — this proves the EMIT CODE produces a complete event.
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

from claude_mcp_servers.rl_client.rl_logger import serialize_node_record  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402

pytest.importorskip("weaviate_mcp.server")

_DIM = 8


def _vec(seed):
    import numpy as np
    return np.random.default_rng(seed).random(_DIM).astype(float).round(4).tolist()


def _srv():
    return importlib.import_module("claude_mcp_servers.weaviate_mcp.server")


def test_retrieval_and_citation_carry_all_net_inputs(monkeypatch):
    captured: list = []

    def _post(envelope, timeout: float = 2.0):
        captured.append(envelope)
        return True

    writer = RLTelemetryWriter(
        project="p", embedding_source="qwen3", embedding_dim=_DIM,
        embedding_model="qwen3-embedding:0.6b", hub_post_fn=_post,
    )

    # --- 1) RETRIEVAL: full net-input node record via the real serializer ---
    node = serialize_node_record(
        {
            "title": "Alpha",
            "score": 0.9,
            "tier": "three_chunks",  # near-chunk tier
            "n_emb": _vec(2),                      # n_raw
            "linked_embs": [_vec(3), _vec(4)],     # linked_raws (near-chunk + link)
            "linked_type_names": ["tool", "concept"],  # linked_type_idxs source
            "node_type": "concept",                # n_type_idx source
            "links": ["Beta"],
            "chunks_matched": 3,
            "best_chunk_number": 2,
        },
        include_links=True, include_shown_rank=True, include_chunks_matched=True,
        include_best_chunk_number=True, include_code_path_fields=True,
    )
    writer.log_retrieval(
        task_id="t-e2e", task_type="mcp_interactive", query="q",
        nodes=[node], query_emb=_vec(1),  # q_raw
    )
    ret_env = next(e for e in captured if e["event_type"] == "retrieval")
    ret = json.loads(ret_env["payload_json"])

    # Map each net input to its persisted field.
    assert ret.get("query_emb"), "q_raw missing (query_emb)"
    n0 = ret["nodes"][0]
    assert n0.get("emb"), "n_raw missing (node emb — serializer promotes n_emb→emb)"
    assert n0.get("linked_embs") and len(n0["linked_embs"]) == 2, "linked_raws missing"
    assert n0.get("node_type") == "concept", "n_type_idx source missing"
    assert n0.get("linked_type_names") == ["tool", "concept"], "linked_type_idxs source missing"
    # near-chunk context: chunks_matched / best_chunk_number recorded, extra
    # chunks travel as linked_embs.
    assert n0.get("chunks_matched") == 3
    assert n0.get("best_chunk_number") == 2
    # embedding-space attribution present on the event.
    assert ret["embedding_source"] == "qwen3" and ret["embedding_dim"] == _DIM

    # --- 2) CITATION: labels + answer artifacts via the real compute path ---
    class _Svc:
        def embed_text(self, text):
            return _vec(hash(text) % 1000)

    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: _Svc())
    monkeypatch.setattr(_srv(), "_get_rl_telemetry_writer", lambda: writer)

    from claude_mcp_servers.rl_client.citation_compute import compute_citation

    ctx = {
        "nodes": [{"title": "Alpha", "n_emb": n0["emb"]}],
        "active_model": "qwen3-embedding:0.6b",
        "embedding_source": "qwen3", "embedding_dim": _DIM,
        "task_type": "mcp_interactive", "session_id": "sess-e2e",
        "fire_reason": "threshold", "window_tokens": 30000,
    }
    result = compute_citation(
        "t-e2e", "The answer references Alpha in detail. " * 5, ctx, write=True,
    )
    assert result is not None
    cit_env = next(e for e in captured if e["event_type"] == "citation")
    cit = json.loads(cit_env["payload_json"])

    # Label inputs.
    assert cit.get("cosine_sims"), "training label (cosine_sims) missing"
    assert "literal_cited" in cit, "literal_cited target-boost missing"
    # Answer artifacts (G4) — re-derivable labels for a 2nd space / retuned formula.
    assert cit.get("answer_chunk_embs"), "answer_chunk_embs missing (G4)"
    assert cit.get("answer_chunk_hashes"), "answer_chunk_hashes missing (G4)"
    # Pairing key + space attribution.
    assert cit["task_id"] == "t-e2e" == ret["task_id"]
    assert cit["embedding_source"] == "qwen3"

    # FULL net-input coverage confirmed for the emitted pair.
