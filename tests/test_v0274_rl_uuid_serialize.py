# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 T5-1b (BLOCKER-2): RL cache_nodes POST must survive a uuid.UUID
embedded inside a candidate node dict.

The bug: ``_do_rerank`` passes candidate node dicts VERBATIM into
``cache_nodes`` → ``_post_json`` → ``client.post(url, json=json_body)``.
httpx's default JSON encoder raises ``TypeError: Object of type UUID is not
JSON serializable`` on any embedded ``uuid.UUID`` (a links / wikilink /
enrichment field), the POST fails, and RLClient silently falls back to cosine
order per query.

The fix serializes the body JSON-safe with ``json.dumps(..., default=str)``
(round-tripped through ``json.loads`` so httpx still receives a plain dict via
``json=``) at the single ``_post_json`` choke point — covering EVERY POST body.

These tests also pin:
  * The already-string wire fields (session_id / task_id / embedding_source)
    are unaffected — they were already strings and stay byte-identical.
  * The absent-module path is a clean silent no-op (disabled RLClient returns
    the input order, no POST attempted, no exception).
"""
from __future__ import annotations

import asyncio
import json
import json as _json  # module-level alias — the fake client's ``json=`` param shadows the name
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, payload=None):
        self._payload = payload or {"top_k": [{"title": "N1", "score": 0.9}]}

    def json(self):
        return self._payload


class _CapturingClient:
    """Mimics httpx.AsyncClient.post but ALSO enforces JSON-serializability.

    The real httpx would raise ``TypeError`` on a non-serializable ``json=``
    body; we reproduce that contract here by running ``json.dumps`` on the
    body the client hands us. If the client sanitized correctly, this passes;
    if it forwarded a raw UUID, this raises exactly like httpx.
    """

    def __init__(self, captured, payload=None):
        self._captured = captured
        self._payload = payload

    async def post(self, url, json=None, headers=None, timeout=None):
        # NOTE: the ``json`` param name here shadows the stdlib module inside
        # this method; use the module-level ``_json`` alias for dumps.
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["headers"] = headers
        # Reproduce httpx's own serialization contract — must not raise.
        _json.dumps(json)
        return _FakeResp(self._payload)

    async def get(self, url, timeout=None):
        return _FakeResp(self._payload)

    async def aclose(self):
        pass


def _make_client(captured, payload=None):
    from claude_mcp_servers.rl_client.client import RLClient

    return RLClient(
        text_dim=1024,
        active_embedding="qwen3",
        base_url="http://127.0.0.1:0",
        client=_CapturingClient(captured, payload=payload),
    )


def test_uuid_inside_candidate_node_serializes_and_posts():
    """A uuid.UUID buried in a candidate node dict must not break the POST."""
    captured = {}
    rl = _make_client(captured)

    node_uuid = uuid.uuid4()
    candidates = [
        {
            "title": "N1",
            "score": 0.5,
            # UUID embedded in an enrichment / links field — the real hazard.
            "links": [{"target": "Other", "uuid": node_uuid}],
            "enrichment_id": node_uuid,
        }
    ]

    result = _run(
        rl.cache_nodes(
            query="q",
            nodes=candidates,
            top_k=1,
            task_id="task-xyz",
            session_id="sess-1",
        )
    )

    # The rerank succeeded (server's top_k returned) — no cosine fallback.
    assert result == [{"title": "N1", "score": 0.9}]
    # The body was posted and is fully JSON-safe.
    body = captured["json"]
    assert body is not None
    # UUID coerced to its str form (not a raw UUID object).
    assert body["nodes"][0]["enrichment_id"] == str(node_uuid)
    assert body["nodes"][0]["links"][0]["uuid"] == str(node_uuid)
    # The whole body is genuinely serializable (would have raised otherwise).
    json.dumps(body)


def test_already_string_wire_fields_unaffected():
    """session_id / task_id / embedding_source were strings — stay identical."""
    captured = {}
    rl = _make_client(captured)

    _run(
        rl.cache_nodes(
            query="q",
            nodes=[{"title": "N1", "score": 0.5}],
            top_k=1,
            task_id="task-abc",
            session_id="claude-session-xyz",
        )
    )
    body = captured["json"]
    assert body["session_id"] == "claude-session-xyz"
    assert body["task_id"] == "task-abc"
    assert body["embedding_source"] == "qwen3"
    assert body["active_embedding"] == "qwen3"
    # These are the SAME strings, not stringified-from-something-else.
    assert isinstance(body["session_id"], str)
    assert isinstance(body["task_id"], str)


def test_rl_update_v3_uuid_safe():
    """The fix covers rl_update_v3's POST body too (single choke point)."""
    captured = {}
    rl = _make_client(captured, payload={"ok": True})

    node_uuid = uuid.uuid4()
    _run(
        rl.rl_update_v3(
            task_id="t1",
            nodes_packed=[
                {"title": "N1", "n_emb": [0.1, 0.2], "wikilink_uuid": node_uuid}
            ],
            query_emb=[0.1, 0.2],
            cosine_sims={"N1": 0.5},
            literal_cited={"N1": True},
        )
    )
    body = captured["json"]
    packed = body["tasks"]["t1"]["nodes_packed"][0]
    assert packed["wikilink_uuid"] == str(node_uuid)
    json.dumps(body)  # fully serializable


def test_disabled_client_is_silent_no_op_no_post():
    """Absent-module path: disabled RLClient returns input order, NO POST."""
    from claude_mcp_servers.rl_client.client import RLClient

    posted = {"called": False}

    class _NoPostClient:
        async def post(self, *a, **k):
            posted["called"] = True
            raise AssertionError("disabled client must NOT POST")

        async def get(self, *a, **k):
            raise AssertionError("disabled client must NOT GET")

        async def aclose(self):
            pass

    # base_url=None → disabled mode (no RL_SERVER_URL/PORT).
    rl = RLClient(text_dim=1024, active_embedding="qwen3", base_url=None,
                  client=_NoPostClient())
    candidates = [{"title": "A", "score": 0.9}, {"title": "B", "score": 0.5}]
    result = _run(
        rl.cache_nodes(query="q", nodes=candidates, top_k=2, task_id="t")
    )
    # Input order returned unchanged; no POST attempted; no exception.
    assert result == candidates
    assert posted["called"] is False
    assert rl.last_error == "disabled"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
