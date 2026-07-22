# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R2-11 / WP-Q item 2 — client-side payload size guard (pathological backstop).

The hub's rl_events POST handler (vct-hub/src/rl_events_api.rs) now sets an
EXPLICIT 16 MiB ``DefaultBodyLimit`` on the ingest route (raised from axum's
2 MB default per the user rule "move the limit, never the data" — embedding-heavy
events can legitimately exceed 2 MB and their labels must not be lost). A body
over the 16 MiB cap is still rejected with 413.

This client-side guard is a PATHOLOGICAL-CASE BACKSTOP that sits JUST UNDER the
hub's 16 MiB cap (``_HUB_PAYLOAD_MAX_BYTES_DEFAULT``): normal events never
approach it, so the trim never fires in steady state. When a genuinely runaway
event does exceed the cap, ``_wrap_for_hub`` measures the serialized event and
drops the OPTIONAL heavy embedding fields in a documented priority order —
per-node near-chunk ``linked_embs`` first, then event-level
``answer_chunk_embs`` — NEVER the core label fields NOR the core net inputs
(``query_emb`` / per-node ``emb``). A trimmed event is logged at WARNING.

These tests assert the boundary behaviour directly against the trimming helper
and the ``_wrap_for_hub`` chokepoint. They pass explicit small caps
(``max_bytes`` / ``RL_HUB_PAYLOAD_MAX_BYTES``) so the trim path is exercised
without building 16 MiB fixtures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import telemetry_writer as tw  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402


def _writer() -> RLTelemetryWriter:
    def _post(envelope, timeout: float = 2.0) -> bool:  # never actually posted here
        return True

    return RLTelemetryWriter(
        project="sample-project",
        embedding_source="qwen3",
        embedding_dim=8,
        embedding_model="qwen3-embedding:0.6b",
        hub_post_fn=_post,
    )


def _big_vec(dim: int = 1024) -> list:
    # A realistic 1024-dim rounded embedding (~7 KB serialized at 4 dp).
    return [round(0.123456 + (i % 7) * 0.01, 4) for i in range(dim)]


def _core_label_fields(event: dict) -> dict:
    """The fields that must survive any trim (the trainable label)."""
    return {k: event.get(k) for k in ("citations", "cosine_sims", "task_id", "event")}


def test_small_event_is_not_trimmed():
    """An under-cap event passes through untouched (no trimming, no log)."""
    event = {
        "event": "citation",
        "schema_version": 3,
        "task_id": "t-small",
        "citations": {"NodeA": True},
        "cosine_sims": {"NodeA": 0.8},
        "answer_chunk_embs": [_big_vec()],  # one vec — well under cap
    }
    max_bytes = tw._resolve_hub_payload_max_bytes()
    trimmed, dropped = tw._trim_event_to_payload_cap(event, max_bytes)
    assert dropped == []
    assert "answer_chunk_embs" in trimmed


def test_oversized_drops_linked_embs_first_never_node_emb():
    """When over-cap, per-node ``linked_embs`` are dropped FIRST (whole-field);
    the node's own ``emb`` is a CORE NET INPUT and is NEVER trimmed — a
    well-formed-but-untrainable event is worse than a loud 413."""
    max_bytes = 60_000
    nodes = [
        {"title": f"N{i}", "score": 0.5, "emb": _big_vec(),
         "linked_embs": [_big_vec()], "linked_type_names": ["relatedTo"]}
        for i in range(20)
    ]
    event = {
        "event": "retrieval",
        "schema_version": 3,
        "task_id": "t-big",
        "citations": {"N0": True},
        "cosine_sims": {"N0": 0.7},
        "query_emb": _big_vec(),
        "nodes": nodes,
        "answer_chunk_embs": [_big_vec() for _ in range(3)],
    }
    before = _core_label_fields(event)
    trimmed, dropped = tw._trim_event_to_payload_cap(event, max_bytes)

    # linked_embs dropped (whole-field); answer embs next.
    assert "nodes.linked_embs" in dropped, f"expected linked_embs drop, got {dropped}"
    assert "nodes.emb" not in dropped, "node emb is a core net input — never trimmed"
    assert "query_emb" not in dropped, "query_emb is a core net input — never trimmed"
    for rec in trimmed["nodes"]:
        assert "emb" in rec, "core net input nodes[].emb must survive trimming"
        assert "linked_embs" not in rec
    assert "query_emb" in trimmed
    # 20 retained node embs keep this fixture over-cap by design: the guard
    # posts it anyway and the hub's 413 surfaces loudly (documented fallback).
    assert tw._serialized_len(trimmed) > max_bytes
    # The CORE label is intact.
    assert _core_label_fields(trimmed) == before
    assert trimmed["citations"] == {"N0": True}
    assert trimmed["cosine_sims"] == {"N0": 0.7}


def test_answer_embs_dropped_when_node_trim_insufficient():
    """If dropping node embeddings is not enough, answer_chunk_embs go next."""
    max_bytes = 30_000
    # A single node (small) but MANY answer embeddings (the bulk here).
    event = {
        "event": "citation",
        "schema_version": 3,
        "task_id": "t-answer-heavy",
        "citations": {"N0": True},
        "cosine_sims": {"N0": 0.9},
        "nodes": [{"title": "N0", "score": 0.5, "emb": _big_vec()}],
        "answer_chunk_embs": [_big_vec() for _ in range(10)],  # dominates size
    }
    trimmed, dropped = tw._trim_event_to_payload_cap(event, max_bytes)
    assert "answer_chunk_embs" in dropped
    assert "answer_chunk_embs" not in trimmed
    assert tw._serialized_len(trimmed) <= max_bytes
    # Core label survives, and the node's own emb (core net input) survives.
    assert trimmed["citations"] == {"N0": True}
    assert trimmed["nodes"][0]["emb"], "node emb must survive answer-emb trim"


def test_wrap_for_hub_trims_and_warns(monkeypatch, caplog):
    """The chokepoint ``_wrap_for_hub`` applies the guard and logs at WARNING."""
    import logging
    monkeypatch.setenv("RL_HUB_PAYLOAD_MAX_BYTES", "40000")
    w = _writer()
    # Bulk lives in the TRIMMABLE field (linked_embs); the 4 core node embs
    # (~30 KB) stay under the 40 KB cap after the trim, so this pins the
    # trim+warn+fit path. Core net inputs are never trimmed (see the
    # never-trim tests above).
    nodes = [
        {"title": f"N{i}", "score": 0.5, "emb": _big_vec(),
         "linked_embs": [_big_vec() for _ in range(8)]}
        for i in range(4)
    ]
    event = {
        "event": "retrieval",
        "schema_version": 3,
        "task_id": "t-wrap",
        "citations": {"N0": True},
        "cosine_sims": {"N0": 0.6},
        "nodes": nodes,
    }
    with caplog.at_level(logging.WARNING, logger=tw.logger.name):
        envelope = w._wrap_for_hub("rl_retrieval", "t-wrap", "code_hook", event)

    # The serialized payload_json in the envelope fits the cap.
    assert len(envelope["payload_json"].encode("utf-8")) <= 40_000
    # WARNING logged mentioning the drop.
    assert any("exceeded" in r.message and "hub payload cap" in r.message
               for r in caplog.records), "a trimmed event must warn"
    # Core label survives in the serialized payload.
    payload = json.loads(envelope["payload_json"])
    assert payload["citations"] == {"N0": True}
    assert payload["cosine_sims"] == {"N0": 0.6}


def test_env_override_cap_is_honored(monkeypatch):
    """RL_HUB_PAYLOAD_MAX_BYTES tunes the cap; a malformed value falls back."""
    monkeypatch.setenv("RL_HUB_PAYLOAD_MAX_BYTES", "12345")
    assert tw._resolve_hub_payload_max_bytes() == 12345
    monkeypatch.setenv("RL_HUB_PAYLOAD_MAX_BYTES", "not-a-number")
    assert tw._resolve_hub_payload_max_bytes() == tw._HUB_PAYLOAD_MAX_BYTES_DEFAULT
    monkeypatch.setenv("RL_HUB_PAYLOAD_MAX_BYTES", "-5")
    assert tw._resolve_hub_payload_max_bytes() == tw._HUB_PAYLOAD_MAX_BYTES_DEFAULT


def test_envelope_dim_measured_from_payload_vector(caplog):
    """WP-R defect-2: a PRESENT query_emb is ground truth for the envelope's
    denormalized embedding_dim — a config/actual disagreement stores the
    measured length and warns; a vectorless event falls back to config dim."""
    import logging
    w = _writer()  # config dim = 8
    ev = {
        "event": "retrieval", "schema_version": 3, "task_id": "t-dim",
        "citations": {}, "cosine_sims": {},
        "query_emb": [0.1, 0.2, 0.3],  # measured 3 != config 8
    }
    with caplog.at_level(logging.WARNING, logger=tw.logger.name):
        env = w._wrap_for_hub("rl_retrieval", "t-dim", "code_hook", ev)
    assert env["embedding_dim"] == 3, "payload vector length is ground truth"
    assert any("measured" in r.message for r in caplog.records)

    ev2 = {"event": "citation", "schema_version": 3, "task_id": "t-nodim",
           "citations": {"N0": True}, "cosine_sims": {"N0": 0.5}}
    env2 = w._wrap_for_hub("rl_citation", "t-nodim", "code_hook", ev2)
    assert env2["embedding_dim"] == 8, "vectorless event falls back to config dim"


def test_payload_inner_dim_measured_from_query_emb():
    """R3-7 step 2: the PAYLOAD-INNER embedding_dim (inside payload_json) must
    also match the stored query_emb length — not just the indexed envelope
    column. The historical escaper wrote embedding_dim: 2048 beside a len-3
    query_emb one level deeper; readers of payload_json saw the disagreement."""
    w = _writer()  # config dim = 8
    event = w._build_v3_retrieval_event(
        task_id="t-inner",
        task_type="code_hook",
        query="q",
        nodes=[],
        session_id="s",
        query_emb=[0.1, 0.2, 0.3],  # measured 3 != config 8
    )
    assert event["embedding_dim"] == 3, (
        "the payload-inner embedding_dim must be measured from the stored "
        "query_emb (ground truth), not the config dim"
    )
    assert len(event["query_emb"]) == event["embedding_dim"], (
        "payload_json must be self-consistent: query_emb length == embedding_dim"
    )

    # A retrieval event with NO query_emb falls back to the config dim.
    event2 = w._build_v3_retrieval_event(
        task_id="t-inner-nodim",
        task_type="code_hook",
        query="q",
        nodes=[],
        session_id="s",
        query_emb=None,
    )
    assert event2["embedding_dim"] == 8, "vectorless retrieval falls back to config dim"
    assert "query_emb" not in event2


def test_citation_inner_dim_is_config_dim():
    """R3-7 step 3: a citation event carries no query vector to measure, so its
    payload-inner embedding_dim is the config dim BY DESIGN (documented)."""
    w = _writer()  # config dim = 8
    event = w._build_v3_citation_event(
        task_id="t-cite",
        task_type="code_hook",
        citations={"N0": True},
        cosine_sims={"N0": 0.5},
    )
    assert event["embedding_dim"] == 8, "citation uses the config dim (no vector)"
