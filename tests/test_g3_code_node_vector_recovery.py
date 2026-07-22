# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""G3 (2026-07-22) — code-retrieval events must carry per-node embeddings.

Root cause: the CLI/hook code path (query_code_graph.py, out of the MCP's scope)
fetches candidates WITHOUT include_vector, so its survivors carry code TEXT but
no ``n_emb``. Result: code_hook (8 772 rows) + code_cli (1 505 rows) events had
ZERO node embeddings and the offline trainer skipped every one — ~40% of the
retrieval corpus was dead weight.

Fix (in the SHARED emitter, one home): ``_rl_recover_code_node_vectors`` re-embeds
each vector-less node's stored code text in the active code slot, so the SAME
emitter serves BOTH the MCP path (already has n_emb → no-op) and the CLI/hook
path (recovers). The v3 serializer then promotes ``n_emb`` → ``emb`` so the
trainer's ``node.get("emb")`` read finds it.

Red-proof: a code survivor with text but NO ``n_emb`` produces an event whose
node carries ``emb`` AFTER the fix; with the recovery service unavailable it does
NOT (the pre-fix behaviour), and the trainer would skip it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import telemetry_writer  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402

pytest.importorskip(
    "weaviate_mcp.server",
    reason="weaviate_mcp.server must be importable for the G3 recovery tests",
)


def _srv():
    import importlib
    return importlib.import_module("weaviate_mcp.server")


def _writer(captured: list) -> RLTelemetryWriter:
    def _post(envelope, timeout: float = 2.0) -> bool:
        captured.append(envelope)
        return True

    return RLTelemetryWriter(
        project="sample-project",
        embedding_source="codesage",
        embedding_dim=8,
        embedding_model="sample-code-model",
        hub_post_fn=_post,
    )


class _FakeEmbedService:
    """Deterministic code embedder: returns a fixed 8-dim vector for any text."""

    def __init__(self):
        self.calls = []

    def embed_code(self, text: str):
        self.calls.append(text)
        return [0.25] * 8


def _code_survivor_text_no_vector():
    """A CLI/hook-shaped survivor: code text present, NO ``n_emb``."""
    return [
        {
            "_c": "CodeFunction",
            "_s": 0.61,
            "_p": {
                "full_name": "alpha.processing.process_batch",
                "file_path": "src/alpha/processing.py",
                "function_body": "def process_batch(items):\n    return [f(i) for i in items]",
            },
            "_tier": "three_chunks",
            # NOTE: no "n_emb" — mirrors the CLI fetch (no include_vector).
        },
    ]


@pytest.fixture()
def _env(tmp_path, monkeypatch):
    for k in (
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ONLINE_TRAINING_DISABLED",
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("VCT_SESSION_ID", "sess-g3")
    monkeypatch.setenv("CODE_EMBED_MODEL", "sample-code-model")
    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: False)
    monkeypatch.setattr(_srv(), "_try_resolve_project_config", lambda: None)
    return tmp_path


def _emitted_nodes(captured: list) -> list:
    assert captured, "expected a retrieval event to be posted"
    payload = json.loads(captured[0]["payload_json"])
    return payload.get("nodes", [])


def test_code_node_vector_recovered_via_reembed(_env, monkeypatch):
    """FIX: a vector-less code survivor with text gets ``emb`` on the emitted
    event (recovered by re-embedding its code text)."""
    fake = _FakeEmbedService()
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: fake)
    captured: list = []
    monkeypatch.setattr(
        _srv(), "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )

    ok = _srv()._emit_code_retrieval_telemetry(
        query="where are batches processed?",
        query_emb=[0.1] * 8,
        survivors=_code_survivor_text_no_vector(),
        limit=1,
        slot="codesage_embed",
        task_id="task-g3-recover",
    )
    assert ok is True
    nodes = _emitted_nodes(captured)
    assert len(nodes) == 1
    # The v3 serializer promotes recovered n_emb -> emb (trainer reads `emb`).
    assert nodes[0].get("emb"), (
        "recovered code node must carry `emb` on the event (G3 fix); the "
        "trainer skips any node without emb/n_emb"
    )
    assert fake.calls, "the recovery must have re-embedded the node's code text"


def test_pre_fix_no_service_leaves_node_vectorless(_env, monkeypatch):
    """RED-PROOF: with NO embedding service (recovery impossible), the same
    text-only survivor produces an event whose node has NO emb — exactly the
    pre-fix state the trainer skipped."""
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: None)
    captured: list = []
    monkeypatch.setattr(
        _srv(), "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )

    ok = _srv()._emit_code_retrieval_telemetry(
        query="where are batches processed?",
        query_emb=[0.1] * 8,
        survivors=_code_survivor_text_no_vector(),
        limit=1,
        slot="codesage_embed",
        task_id="task-g3-nofix",
    )
    assert ok is True
    nodes = _emitted_nodes(captured)
    assert len(nodes) == 1
    assert not nodes[0].get("emb") and not nodes[0].get("n_emb"), (
        "RED-PROOF: without the recovery service the code node stays "
        "vector-less (the pre-fix state the trainer dropped)"
    )


def test_mcp_path_with_vector_is_noop(_env, monkeypatch):
    """A survivor that ALREADY carries n_emb (the MCP include_vector path) must
    NOT be re-embedded — the recovery is a no-op and the original vector wins."""
    fake = _FakeEmbedService()
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: fake)
    captured: list = []
    monkeypatch.setattr(
        _srv(), "_get_rl_telemetry_writer_for", lambda *a, **k: _writer(captured)
    )

    surv = _code_survivor_text_no_vector()
    surv[0]["n_emb"] = [0.9] * 8  # already has a real vector

    ok = _srv()._emit_code_retrieval_telemetry(
        query="q",
        query_emb=[0.1] * 8,
        survivors=surv,
        limit=1,
        slot="codesage_embed",
        task_id="task-g3-noop",
    )
    assert ok is True
    nodes = _emitted_nodes(captured)
    assert nodes[0].get("emb") == [0.9] * 8, "existing vector must be preserved"
    assert not fake.calls, "recovery must NOT re-embed a node that already has n_emb"


# ── R2-6: per-emit budget (bounded count + wall-clock deadline) ──────────────
#
# The recovery runs on the SYNCHRONOUS hook/CLI path. A cold/hung backend must
# never let it exceed a hard per-emit budget — else the "lock on retrieval"
# class returns on the code-emit path. These pin the two budget legs.


def _survivors_text_no_vector(n: int) -> list:
    """N CLI/hook-shaped survivors: code text present, NO ``n_emb``."""
    out = []
    for i in range(n):
        out.append({
            "_c": "CodeFunction",
            "_s": 0.6,
            "_p": {
                "full_name": f"pkg.mod.fn_{i}",
                "file_path": f"src/pkg/mod_{i}.py",
                "function_body": f"def fn_{i}(x):\n    return x + {i}",
            },
            "_tier": "single_chunk",
        })
    return out


def _nodes_for(survivors: list) -> list:
    """Emitter-shaped per-node records aligned index-for-index with survivors
    (mirrors what _emit_code_retrieval_telemetry builds before recovery)."""
    return [
        {"title": s["_p"]["full_name"], "score": s["_s"], "shown_rank": i}
        for i, s in enumerate(survivors)
    ]


def _import_rl_enrichment():
    import importlib
    return importlib.import_module("weaviate_mcp.rl_enrichment")


def test_r2_6_recovery_count_cap_bounds_reembeds(_env, monkeypatch):
    """The recovery re-embeds AT MOST RL_CODE_RECOVER_MAX_NODES nodes; the rest
    are skipped-and-left-vectorless (not stalled). Red-proof: removing the count
    gate would re-embed all N."""
    rle = _import_rl_enrichment()
    monkeypatch.setenv("RL_CODE_RECOVER_MAX_NODES", "3")
    monkeypatch.delenv("RL_CODE_RECOVER_DEADLINE_S", raising=False)
    fake = _FakeEmbedService()
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: fake)

    survivors = _survivors_text_no_vector(10)
    nodes = _nodes_for(survivors)
    recovered = rle._rl_recover_code_node_vectors(nodes, survivors)

    assert recovered == 3, "count cap must bound recovery to max_nodes"
    assert len(fake.calls) == 3, "no more than max_nodes embeds may fire"
    # The remaining 7 nodes stay vector-less (skipped by budget), not stalled.
    with_vec = [n for n in nodes if n.get("n_emb")]
    assert len(with_vec) == 3


def test_r2_6_recovery_deadline_bounds_wall_clock(_env, monkeypatch):
    """A SLOW backend cannot exceed the wall-clock deadline: with a per-embed
    sleep and a tight deadline, the loop stops well before N×sleep. This is the
    latency-bound the reviewer asked for — the synchronous hook can never lock."""
    import time as _t
    rle = _import_rl_enrichment()
    # Deadline 0.25s; each embed sleeps 0.1s → at most ~3 embeds fit, and the
    # loop must return in well under 10×0.1s = 1.0s (the unbounded worst case).
    monkeypatch.setenv("RL_CODE_RECOVER_DEADLINE_S", "0.25")
    monkeypatch.setenv("RL_CODE_RECOVER_MAX_NODES", "1000")  # count gate not the bound here

    class _SlowEmbed:
        def __init__(self):
            self.calls = 0

        def embed_code(self, text):
            self.calls += 1
            _t.sleep(0.1)
            return [0.25] * 8

    slow = _SlowEmbed()
    monkeypatch.setattr(_srv(), "_get_embedding_service", lambda: slow)

    survivors = _survivors_text_no_vector(10)
    nodes = _nodes_for(survivors)

    start = _t.monotonic()
    rle._rl_recover_code_node_vectors(nodes, survivors)
    elapsed = _t.monotonic() - start

    # Hard latency bound: the deadline (0.25s) + one in-flight embed (0.1s) of
    # slack; MUST be far below the unbounded 10×0.1s = 1.0s worst case.
    assert elapsed < 0.6, (
        f"recovery took {elapsed:.2f}s — the per-emit deadline must cap the "
        f"synchronous hook well below the unbounded {10 * 0.1:.1f}s worst case"
    )
    # And it did NOT try to embed all 10 nodes.
    assert slow.calls < 10, "deadline must have short-circuited the loop"
