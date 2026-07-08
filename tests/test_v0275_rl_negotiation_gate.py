# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 — RL-10 completion: negotiate() wired into the LIVE rerank gate.

v0.2.73 shipped ``RLClient.negotiate()`` (4-way pairing classification incl.
``embedding_space_mismatch``) but its only caller was rl-doctor — the rerank
pipeline itself never consulted the verdict, so a mismatched container was
still POSTed to on every search. v0.2.75 wires the verdict into
``search_pipeline._do_rerank``:

  * ``embedding_space_mismatch`` / ``incompatible_new_server`` → refuse the
    rerank (fallback to cosine, ``rl_used=False``), WARN once per process via
    the RL-3 voice, and record the reason in the persistent fallback counter
    so rl-doctor and the counter agree.
  * ``degraded_old_server`` (old container / absent /health fields) → keep
    reranking (correct-by-design extra=allow tolerance).
  * Negotiation transport failure / unreachable → fall OPEN to the existing
    per-call fallback path; negotiation never blocks retrieval.
  * The verdict is memoised per client instance — exactly ONE /health probe
    per (active_embedding, project_id) client per process.

Async pattern: asyncio.run inline (repo RL-test convention; pytest-asyncio is
not a hard test dep — mirrors tests/test_v0273_rl_version_negotiation.py).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

search_pipeline = pytest.importorskip(
    "claude_mcp_servers.rl_client.search_pipeline",
    reason="search_pipeline must be importable for the negotiation-gate tests",
)
from claude_mcp_servers.rl_client.client import PROTOCOL_VERSION, RLClient  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── fake httpx transport (counts /health GETs + /cache_nodes POSTs) ─────


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Transport:
    """Injected in place of httpx.AsyncClient. GET → /health payload;
    POST → echoes the request's nodes as ``top_k`` (a "successful" rerank)."""

    def __init__(self, health_payload: dict, *, get_raises: bool = False):
        self.health_payload = health_payload
        self.get_raises = get_raises
        self.get_calls = 0
        self.post_calls = 0

    async def get(self, url, timeout=None):
        self.get_calls += 1
        if self.get_raises:
            raise ConnectionError("boom — health unreachable")
        return _Resp(200, self.health_payload)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls += 1
        nodes = (json or {}).get("nodes") or []
        limit = (json or {}).get("limit") or len(nodes)
        return _Resp(200, {"top_k": list(nodes[:limit])})

    async def aclose(self):  # pragma: no cover — lifecycle symmetry
        pass


def _client_with(health_payload: dict, **kw) -> tuple[RLClient, _Transport]:
    transport = _Transport(health_payload, **kw)
    client = RLClient(
        base_url="http://127.0.0.1:65535",
        text_dim=1024,
        active_embedding="qwen3",
        client=transport,
    )
    return client, transport


_CANDIDATES = [
    {"title": "NodeA", "score": 0.9},
    {"title": "NodeB", "score": 0.7},
]


async def _rerank_with(client) -> "list | None":
    with patch(
        "claude_mcp_servers.weaviate_mcp.server._get_rl_client",
        return_value=client,
    ):
        return await search_pipeline._do_rerank(
            query="negotiation gate test",
            candidates=list(_CANDIDATES),
            limit=2,
            task_id="tid-negotiation",
            session_id="sess-negotiation",
        )


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch):
    """Route the persistent fallback counter into tmp + reset the WARN-once."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(search_pipeline, "_WARNED_RL_FALLBACK", False)
    yield tmp_path


def _counter(tmp_path) -> dict:
    path = tmp_path / ".claude" / "state" / "rl_fallback_counter.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ── refuse: embedding-space mismatch (RL-2b hazard) ─────────────────────


def test_dim_mismatch_refuses_rerank_and_counts_reason(tmp_path):
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": 2048, "embedding_space": "qwen3"},
    )
    ranked = _run(_rerank_with(client))
    assert ranked is None, "mismatched dim must refuse the rerank"
    assert transport.post_calls == 0, "no /cache_nodes POST on a refused pairing"
    data = _counter(tmp_path)
    assert data.get("count") == 1
    assert "embedding_space_mismatch" in (data.get("last_reason") or "")


def test_space_tag_mismatch_refuses_rerank(tmp_path):
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": 1024, "embedding_space": "arctic"},
    )
    ranked = _run(_rerank_with(client))
    assert ranked is None
    assert transport.post_calls == 0
    assert "embedding_space_mismatch" in (_counter(tmp_path).get("last_reason") or "")


def test_newer_server_protocol_refuses_rerank(tmp_path):
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION + 1,
         "embedding_dim": 1024, "embedding_space": "qwen3"},
    )
    ranked = _run(_rerank_with(client))
    assert ranked is None
    assert transport.post_calls == 0
    assert "incompatible_new_server" in (_counter(tmp_path).get("last_reason") or "")


# ── keep reranking: degraded old server (absent /health fields) ─────────


def test_absent_health_fields_keeps_reranking(tmp_path):
    # Pre-RL-10 container: /health returns only {ok, model}. Classified
    # degraded_old_server — correct-by-design, rerank proceeds.
    client, transport = _client_with({"ok": True, "model": "rl"})
    ranked = _run(_rerank_with(client))
    assert ranked is not None and len(ranked) == 2
    assert transport.post_calls == 1
    # NEW-3 sibling records the SUCCESS in the same file; the load-bearing
    # assertion is that no FALLBACK was recorded for a working pairing.
    assert _counter(tmp_path).get("count", 0) == 0


def test_compatible_pairing_keeps_reranking(tmp_path):
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": 1024, "embedding_space": "qwen3"},
    )
    ranked = _run(_rerank_with(client))
    assert ranked is not None and len(ranked) == 2
    assert transport.post_calls == 1


# ── caching: exactly one /health probe per client instance ──────────────


def test_negotiation_cached_one_health_probe_per_client_instance():
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": 1024, "embedding_space": "qwen3"},
    )

    async def _twice():
        r1 = await _rerank_with(client)
        r2 = await _rerank_with(client)
        return r1, r2

    r1, r2 = _run(_twice())
    assert r1 is not None and r2 is not None
    assert transport.get_calls == 1, "verdict must be memoised on the instance"
    assert transport.post_calls == 2, "both reranks still hit /cache_nodes"


def test_refused_verdict_also_cached_one_probe():
    client, transport = _client_with(
        {"ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": 2048, "embedding_space": "qwen3"},
    )

    async def _twice():
        await _rerank_with(client)
        await _rerank_with(client)

    _run(_twice())
    assert transport.get_calls == 1
    assert transport.post_calls == 0


# ── fall open: negotiation transport failure never blocks retrieval ─────


def test_health_transport_error_falls_open_to_per_call_path(tmp_path):
    # GET /health raises → negotiate() classifies unreachable → NOT a refuse
    # status → the per-call cache_nodes path runs unchanged (and succeeds
    # here, since only the GET is broken).
    client, transport = _client_with(
        {"ok": True, "model": "rl"}, get_raises=True,
    )
    ranked = _run(_rerank_with(client))
    assert ranked is not None and len(ranked) == 2
    assert transport.get_calls == 1
    assert transport.post_calls == 1
    assert _counter(tmp_path).get("count", 0) == 0


def test_negotiate_raising_entirely_falls_open():
    # Defensive arm: negotiate() is contractually non-raising, but if a test
    # double / future edit breaks that, the gate must still fall open.
    class _BoomNegotiationClient:
        last_call_ok = True
        last_error = None

        async def negotiate(self):
            raise RuntimeError("negotiate exploded")

        async def cache_nodes(self, query, nodes, top_k, *, task_id, session_id=""):
            self.last_call_ok = True
            return list(nodes[:top_k])

    client = _BoomNegotiationClient()
    ranked = _run(_rerank_with(client))
    assert ranked is not None and len(ranked) == 2
    # Verdict (None) is cached — a second call must not re-raise/probe again.
    assert getattr(client, search_pipeline._NEGOTIATION_CACHE_ATTR) is None
