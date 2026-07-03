# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 RL-10 — container protocol version negotiation.

The MCP RL client and the paid vct-rl-reranker container speak an HTTP wire
contract. Pre-RL-10 the only compatibility signal was ``extra="allow"`` on both
schemas, so an incompatible container degraded silently to cosine order. RL-10
adds an explicit handshake: the container advertises ``protocol_version`` +
``embedding_dim`` / ``embedding_space`` on ``/health``, the client advertises
``X-VCT-RL-Protocol`` on every POST, and ``RLClient.negotiate()`` classifies the
pairing so a paying user gets a signal instead of silence.

Also covers the RL-2b hazard: a container whose head expects 1024-dim qwen3
must NOT be fed a 2048-dim CodeSage query (or vice-versa) — the negotiation
refuses that pairing rather than train/query the wrong network.

Uses ``asyncio.run`` inline (the repo's RL-test convention — pytest-asyncio is
not a hard test dep).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client.client import (  # noqa: E402
    MIN_SERVER_PROTOCOL,
    PROTOCOL_VERSION,
    RLClient,
)
from claude_mcp_servers.rl_client.schemas import HealthResponse  # noqa: E402


def _client(text_dim=1024, active_embedding="qwen3"):
    # base_url set → enabled mode; no real network calls (we pass health= in).
    return RLClient(
        base_url="http://127.0.0.1:65535",
        text_dim=text_dim,
        active_embedding=active_embedding,
    )


def test_disabled_mode_negotiates_incompatible():
    c = RLClient(base_url=None)  # disabled
    res = asyncio.run(c.negotiate())
    assert res.compatible is False
    assert res.status == "disabled"


def test_health_not_ok_is_unreachable():
    c = _client()
    res = asyncio.run(c.negotiate(health=HealthResponse(ok=False, model="unreachable")))
    assert res.compatible is False
    assert res.status == "unreachable"


def test_matching_protocol_is_compatible():
    c = _client()
    h = HealthResponse(
        ok=True,
        model="rl",
        protocol_version=PROTOCOL_VERSION,
        embedding_dim=1024,
        embedding_space="qwen3",
    )
    res = asyncio.run(c.negotiate(health=h))
    assert res.compatible is True
    assert res.status == "compatible"
    assert res.server_protocol == PROTOCOL_VERSION


def test_missing_protocol_is_degraded_but_usable():
    # Pre-RL-10 container: /health omits protocol_version entirely.
    c = _client()
    res = asyncio.run(c.negotiate(health=HealthResponse(ok=True, model="rl")))
    assert res.compatible is True  # still engaged via extra=allow tolerance
    assert res.status == "degraded_old_server"
    assert res.server_protocol is None


def test_newer_server_is_incompatible():
    c = _client()
    h = HealthResponse(ok=True, model="rl", protocol_version=PROTOCOL_VERSION + 5)
    res = asyncio.run(c.negotiate(health=h))
    assert res.compatible is False
    assert res.status == "incompatible_new_server"
    assert "newer" in res.detail


def test_embedding_dim_mismatch_refused_rl2b():
    # Container head expects 2048-dim CodeSage; client is 1024-dim qwen3.
    c = _client(text_dim=1024, active_embedding="qwen3")
    h = HealthResponse(
        ok=True,
        model="rl",
        protocol_version=PROTOCOL_VERSION,
        embedding_dim=2048,
        embedding_space="codesage",
    )
    res = asyncio.run(c.negotiate(health=h))
    assert res.compatible is False
    assert res.status == "embedding_space_mismatch"
    assert "2048" in res.detail and "1024" in res.detail


def test_embedding_space_tag_mismatch_refused():
    # Same dim would pass, but the source tag disagrees → still refuse.
    c = _client(text_dim=1024, active_embedding="qwen3")
    h = HealthResponse(
        ok=True,
        model="rl",
        protocol_version=PROTOCOL_VERSION,
        embedding_dim=1024,
        embedding_space="arctic",
    )
    res = asyncio.run(c.negotiate(health=h))
    assert res.compatible is False
    assert res.status == "embedding_space_mismatch"


def test_matching_embedding_space_is_compatible():
    c = _client(text_dim=1024, active_embedding="qwen3")
    h = HealthResponse(
        ok=True,
        model="rl",
        protocol_version=PROTOCOL_VERSION,
        embedding_dim=1024,
        embedding_space="qwen3",
    )
    res = asyncio.run(c.negotiate(health=h))
    assert res.compatible is True
    assert res.status == "compatible"


def test_negotiate_never_raises_on_health_probe_failure():
    # A client whose transport raises: negotiate must still return unreachable,
    # never propagate (health() swallows to HealthResponse(ok=False)).
    class _BoomClient:
        async def get(self, *a, **k):
            raise RuntimeError("network exploded")

    c = RLClient(base_url="http://127.0.0.1:65535", client=_BoomClient())
    res = asyncio.run(c.negotiate())  # no health= → real health() → soft-fail
    assert res.compatible is False
    assert res.status == "unreachable"


def test_post_advertises_protocol_header():
    # The client must advertise X-VCT-RL-Protocol on every POST so a
    # negotiating container can pick a compatible response shape.
    captured = {}

    class _CaptureClient:
        async def post(self, url, json=None, headers=None, timeout=None):
            captured["headers"] = headers or {}

            class _Resp:
                status_code = 200

                @staticmethod
                def json():
                    return {"ok": True, "task_id": "t", "top_k": []}

            return _Resp()

    c = RLClient(base_url="http://127.0.0.1:65535", client=_CaptureClient())
    asyncio.run(
        c.cache_nodes(query="q", nodes=[{"title": "N", "score": 1.0}], top_k=1, task_id="t")
    )
    assert captured["headers"].get("X-VCT-RL-Protocol") == str(PROTOCOL_VERSION)
