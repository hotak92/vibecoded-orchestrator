# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 X-4 — the RL enrichment fan-out gets a consumer-gated skip predicate.

``_rl_enrich_nodes_with_linked_embs`` ran one batched Weaviate
``fetch_objects(include_vector=True)`` per collection on EVERY
hybrid_search / semantic_graph_search / CLI kg-search — unconditionally.
The fetches hit WEAVIATE (an RL-down install makes them pointless, not
failing) and feed exactly two consumers: the RL rerank/online-training path
and the retrieval/citation telemetry. v0.2.75 adds a per-process TTL-cached
predicate: enrich only when (rerank will run: license tier + per-project
toggle, refined by the RL-10 cached negotiation verdict) OR (a retrieval
telemetry consumer exists).

PRECISION pinned here: free tier deliberately still emits telemetry (the
historical corpus accumulates), so enrichment is NOT dead weight there —
the predicate keeps it running for a telemetry-on free-tier install.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import search_pipeline  # noqa: E402
from claude_mcp_servers.rl_client import telemetry_writer  # noqa: E402

pytest.importorskip(
    "weaviate_mcp.server",
    reason="weaviate_mcp.server must be importable for the enrich-gate tests",
)


def _srv():
    """Resolve the CURRENT server module at call time (repo convention).

    A module-level binding goes STALE when a sibling test (e.g.
    test_v0273_rl_enrichment_reimport_safety) purges + re-imports the
    weaviate_mcp package mid-suite: rl_enrichment's lazy proxy always
    resolves the LIVE sys.modules entry, so patches on a stale object
    are invisible to the code under test.
    """
    import importlib

    return importlib.import_module("weaviate_mcp.server")


def _enr():
    import importlib

    return importlib.import_module("weaviate_mcp.rl_enrichment")


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    """Reset the TTL cache around every test + neutralise machine-local env."""
    _enr()._rl_enrich_gate_reset_for_test()
    for k in (
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ENRICH_GATE_TTL_S",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: False)
    yield
    _enr()._rl_enrich_gate_reset_for_test()


class _CountingResolver:
    """coll_resolver stand-in: counts how many collections were fetched."""

    def __init__(self):
        self.calls = 0

    def __call__(self, name: str):
        self.calls += 1

        class _Query:
            @staticmethod
            def fetch_objects(**kw):
                return types.SimpleNamespace(objects=[])

        return types.SimpleNamespace(query=_Query())


def _nodes():
    return [
        {
            "title": "NodeA",
            "collection": "Sample_KnowledgeGraph",
            "source_id": "uuid-a",
            "emb": [0.5, 0.5],
        }
    ]


def _enrich(resolver) -> None:
    _srv()._rl_enrich_nodes_with_linked_embs(
        _nodes(), query_emb=[1.0, 0.0], active_slot="qwen3_embed",
        coll_resolver=resolver,
    )


# ── gated off: no consumer at all → zero Weaviate fetches ───────────────


def test_gated_off_no_consumer_zero_enrichment_queries(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    resolver = _CountingResolver()
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
        _enrich(resolver)
    assert resolver.calls == 0, "no consumer → the fan-out must not touch Weaviate"


# ── free tier with telemetry on → enrichment still runs ─────────────────


def test_free_tier_with_telemetry_still_enriches(monkeypatch):
    # Local logging enabled (default) — the free-tier corpus accumulates,
    # so the enrichment fields must keep flowing into the events.
    resolver = _CountingResolver()
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False):
        _enrich(resolver)
    assert resolver.calls == 1, "telemetry-on free tier must still enrich"


def test_licensed_rerank_only_still_enriches(monkeypatch):
    # Telemetry fully off, but rerank will run → candidates need n_emb.
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    resolver = _CountingResolver()
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
         patch.object(_srv(), "_rl_client_instances", {}):
        _enrich(resolver)
    assert resolver.calls == 1


# ── RL-10 negotiation fold: hard-refused pairing kills the rerank term ──


def _verdict(status: str):
    return types.SimpleNamespace(status=status, detail="test verdict")


def _client_with_verdict(status):
    c = types.SimpleNamespace()
    setattr(c, search_pipeline._NEGOTIATION_CACHE_ATTR, _verdict(status))
    return c


def test_refused_negotiation_closes_gate_when_no_telemetry(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    resolver = _CountingResolver()
    instances = {("qwen3", None): _client_with_verdict("embedding_space_mismatch")}
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
         patch.object(_srv(), "_rl_client_instances", instances):
        _enrich(resolver)
    assert resolver.calls == 0, (
        "licensed but hard-refused pairing + no telemetry consumer → dead fan-out"
    )


def test_compatible_negotiation_keeps_gate_open(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    resolver = _CountingResolver()
    instances = {("qwen3", None): _client_with_verdict("compatible")}
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
         patch.object(_srv(), "_rl_client_instances", instances):
        _enrich(resolver)
    assert resolver.calls == 1


def test_no_verdict_yet_falls_open(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    resolver = _CountingResolver()
    c = types.SimpleNamespace()  # client cached, never negotiated yet
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=True), \
         patch.object(_srv(), "_rl_client_instances", {("qwen3", None): c}):
        _enrich(resolver)
    assert resolver.calls == 1, "no verdict yet → the rerank may run → enrich"


# ── TTL cache ────────────────────────────────────────────────────────────


def test_gate_verdict_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return True

    monkeypatch.setattr(_srv(), "_rl_enrichment_consumer_exists", _probe)
    assert _enr()._rl_enrichment_gate_open() is True
    assert _enr()._rl_enrichment_gate_open() is True
    assert calls["n"] == 1, "second call within the TTL must hit the memo"


def test_gate_ttl_expiry_reprobes(monkeypatch):
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return True

    monkeypatch.setattr(_srv(), "_rl_enrichment_consumer_exists", _probe)
    monkeypatch.setenv("RL_ENRICH_GATE_TTL_S", "0")
    _enr()._rl_enrichment_gate_open()
    _enr()._rl_enrichment_gate_open()
    assert calls["n"] == 2, "TTL=0 must re-probe on every call"


def test_gate_probe_error_falls_open(monkeypatch):
    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(_srv(), "_rl_enrichment_consumer_exists", _boom)
    assert _enr()._rl_enrichment_gate_open() is True


def test_gate_error_in_enrich_falls_open(monkeypatch):
    def _boom():
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(_srv(), "_rl_enrichment_gate_open", _boom)
    resolver = _CountingResolver()
    _enrich(resolver)
    assert resolver.calls == 1, "a broken gate must fall open to enrich"
