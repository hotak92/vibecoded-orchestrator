# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""RL-10 completion (v0.2.75, tightened v0.2.106): negotiate() gates the LIVE
rerank in ``search_pipeline._do_rerank``.

v0.2.106: the container is called ONLY when the verdict is ``compatible``
(feature protocol v2, matching embedding space, trained weights). Every
other verdict skips the ``/cache_nodes`` POST:

  * ``embedding_space_mismatch`` / ``incompatible_new_server`` /
    ``degraded_old_server`` (a pre-v0.2.106 container cannot score) /
    ``unreachable`` / no verdict at all → no POST, WARN once per session via
    the RL-3 voice, the reason recorded in the persistent fallback counter.
  * ``not_trained`` → no POST and NOTHING counted (a legitimate off state).
  * The verdict is memoised per client instance — one /health probe per
    (active_embedding, project_id) client — except the verdicts that can
    change while the process lives, which are re-probed after
    ``_NEGOTIATION_REPROBE_S``.

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

DIM = 8


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
    POST → a scored answer in the request's order (a "successful" rerank)."""

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
        n = len((json or {}).get("nodes") or [])
        return _Resp(200, {"scored": True, "order": list(range(n)), "scores": [0.5] * n})

    async def aclose(self):  # pragma: no cover — lifecycle symmetry
        pass


def _health(**over) -> dict:
    h = {"ok": True, "model": "RLModel loaded", "protocol_version": PROTOCOL_VERSION,
         "embedding_dim": DIM, "embedding_space": "qwen3", "trained": True}
    h.update(over)
    return h


def _client_with(health_payload: dict, **kw) -> tuple[RLClient, _Transport]:
    transport = _Transport(health_payload, **kw)
    client = RLClient(
        base_url="http://127.0.0.1:65535",
        text_dim=DIM,
        active_embedding="qwen3",
        client=transport,
    )
    return client, transport


_CANDIDATES = [
    {"title": "NodeA", "score": 0.9, "emb": [1.0] * DIM},
    {"title": "NodeB", "score": 0.7, "emb": [0.5] * DIM},
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
            query_emb=[1.0] * DIM,
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


# ── refuse + count: faults ──────────────────────────────────────────────


@pytest.mark.parametrize("health, reason", [
    (_health(embedding_dim=2048), "embedding_space_mismatch"),
    (_health(embedding_space="arctic"), "embedding_space_mismatch"),
    (_health(protocol_version=PROTOCOL_VERSION + 1), "incompatible_new_server"),
    # pre-RL-10 container: /health returns only {ok, model}; it cannot score
    ({"ok": True, "model": "rl"}, "degraded_old_server"),
    (_health(protocol_version=1), "degraded_old_server"),
])
def test_faulty_pairing_refuses_rerank_and_counts_reason(tmp_path, health, reason):
    client, transport = _client_with(health)
    ranked = _run(_rerank_with(client))
    assert ranked is None
    assert transport.post_calls == 0, "no /cache_nodes POST on a refused pairing"
    data = _counter(tmp_path)
    assert data.get("count") == 1
    assert reason in (data.get("last_reason") or "")


def test_unreachable_health_refuses_without_a_post(tmp_path):
    client, transport = _client_with(_health(), get_raises=True)
    ranked = _run(_rerank_with(client))
    assert ranked is None
    assert transport.get_calls == 1 and transport.post_calls == 0
    assert "unreachable" in (_counter(tmp_path).get("last_reason") or "")


def test_negotiate_raising_entirely_does_not_rerank(tmp_path):
    # Defensive arm: negotiate() is contractually non-raising; if it raises,
    # nothing confirms the container scores, so it is not called.
    class _BoomNegotiationClient:
        enabled = True
        last_call_ok = True
        last_error = None
        posts = 0

        async def negotiate(self):
            raise RuntimeError("negotiate exploded")

        async def cache_nodes(self, *a, **k):  # pragma: no cover — must not run
            self.posts += 1
            return []

    client = _BoomNegotiationClient()
    assert _run(_rerank_with(client)) is None
    assert client.posts == 0
    # Verdict (None) is cached — a second call must not re-raise/probe again.
    assert getattr(client, search_pipeline._NEGOTIATION_CACHE_ATTR) is None
    assert "no verdict" in (_counter(tmp_path).get("last_reason") or "")


# ── refuse, count nothing: legitimate off state ─────────────────────────


@pytest.mark.parametrize("trained", [False, None])
def test_not_trained_refuses_and_counts_nothing(tmp_path, trained):
    health = _health(model="not trained")
    if trained is None:
        health.pop("trained")
    else:
        health["trained"] = trained
    client, transport = _client_with(health)
    assert _run(_rerank_with(client)) is None
    assert transport.post_calls == 0
    assert _counter(tmp_path) == {}


# ── rerank ───────────────────────────────────────────────────────────────


def test_compatible_pairing_reranks(tmp_path):
    client, transport = _client_with(_health())
    ranked = _run(_rerank_with(client))
    assert ranked is not None and [n["title"] for n in ranked] == ["NodeA", "NodeB"]
    assert transport.post_calls == 1
    data = _counter(tmp_path)
    assert data.get("count", 0) == 0 and data.get("success_count") == 1


# ── caching ──────────────────────────────────────────────────────────────


def test_negotiation_cached_one_health_probe_per_client_instance():
    client, transport = _client_with(_health())

    async def _twice():
        return await _rerank_with(client), await _rerank_with(client)

    r1, r2 = _run(_twice())
    assert r1 is not None and r2 is not None
    assert transport.get_calls == 1, "verdict must be memoised on the instance"
    assert transport.post_calls == 2, "both reranks still hit /cache_nodes"


def test_refused_verdict_also_cached_one_probe():
    client, transport = _client_with(_health(embedding_dim=2048))

    async def _twice():
        await _rerank_with(client)
        await _rerank_with(client)

    _run(_twice())
    assert transport.get_calls == 1
    assert transport.post_calls == 0


def test_not_trained_verdict_is_reprobed_after_the_ttl():
    """Weights rotated in mid-process: the next probe after the TTL sees them."""
    client, transport = _client_with(_health(trained=False))
    assert _run(_rerank_with(client)) is None
    transport.health_payload = _health()
    # within the TTL: still the cached not_trained verdict
    assert _run(_rerank_with(client)) is None
    assert transport.get_calls == 1
    # age the verdict past the TTL
    setattr(client, search_pipeline._NEGOTIATION_CACHE_AT_ATTR,
            getattr(client, search_pipeline._NEGOTIATION_CACHE_AT_ATTR)
            - search_pipeline._NEGOTIATION_REPROBE_S - 1)
    assert _run(_rerank_with(client)) is not None
    assert transport.get_calls == 2 and transport.post_calls == 1


def test_mismatch_verdict_is_not_reprobed():
    client, transport = _client_with(_health(embedding_dim=2048))
    _run(_rerank_with(client))
    setattr(client, search_pipeline._NEGOTIATION_CACHE_AT_ATTR, -1e9)
    _run(_rerank_with(client))
    assert transport.get_calls == 1
