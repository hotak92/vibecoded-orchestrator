# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 NEW-3 — rl-doctor resolves text_dim like the live pipeline
(+ sibling: rerank-SUCCESS counter so the fallback RATE is on disk).

Pre-fix ``rl_doctor._probe_container`` hardcoded ``text_dim = 1024``: on an
openai-1536 (or any non-1024) install the doctor's RL-10 negotiation probe
reported ``embedding_space_mismatch`` against a perfectly-matched container
(false alarm) and would have reported ``compatible`` for a genuinely
mismatched 1024 container. The fix mirrors the resolution used by
``rl_enrichment._get_rl_client`` — the client the pipeline actually pairs
with the container: ACTIVE_EMBEDDING env → server constant → qwen3 for the
tag; ``EmbeddingService.for_project().text_dim`` → 1024 floor for the dim.

Sibling one-liner: ``_do_rerank`` now counts SUCCESSFUL reranks in the same
``.claude/state/rl_fallback_counter.json`` file (``success_count``), so the
fallback rate — count / (count + success_count) — is computable from disk.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
SCRIPTS_DIR = MCP_DIR / "scripts"
for _p in (str(PROJECT_ROOT), str(MCP_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

rl_doctor = importlib.import_module("rl_doctor")
from claude_mcp_servers.rl_client import client as rl_client_mod  # noqa: E402
from claude_mcp_servers.rl_client import search_pipeline  # noqa: E402
from claude_mcp_servers.rl_client.client import PROTOCOL_VERSION, RLClient  # noqa: E402
import vco_lib.embedding_service as embedding_service_mod  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ── fakes ────────────────────────────────────────────────────────────────


class _FakeSvc:
    def __init__(self, dim: int, short: str):
        self._dim = dim
        self._short = short
        self.text_dim = dim
        self.text_model_id = f"{short}-model-id"

    def text_model_short_id(self) -> str:
        return self._short

    def close(self) -> None:
        pass


class _FakeEmbeddingService:
    """Stands in for vco_lib.embedding_service.EmbeddingService."""

    dim = 1536
    short = "openai"

    @classmethod
    def for_project(cls):
        return _FakeSvc(cls.dim, cls.short)


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Transport:
    def __init__(self, health_payload: dict):
        self.health_payload = health_payload
        self.post_calls = 0

    async def get(self, url, timeout=None):
        return _Resp(200, self.health_payload)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls += 1
        nodes = (json or {}).get("nodes") or []
        limit = (json or {}).get("limit") or len(nodes)
        return _Resp(200, {"top_k": list(nodes[:limit])})

    async def aclose(self):  # pragma: no cover
        pass


@pytest.fixture()
def _service_1536(monkeypatch):
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)
    monkeypatch.setattr(embedding_service_mod, "EmbeddingService", _FakeEmbeddingService)
    return _FakeEmbeddingService


# ── the resolution helper mirrors the live pipeline ─────────────────────


def test_resolution_prefers_embedding_service(_service_1536):
    active, dim = rl_doctor._resolve_active_text_embedding()
    assert (active, dim) == ("openai", 1536)


def test_resolution_falls_back_to_qwen3_floor(monkeypatch):
    monkeypatch.delenv("ACTIVE_EMBEDDING", raising=False)

    class _Boom:
        @classmethod
        def for_project(cls):
            raise RuntimeError("no service")

    monkeypatch.setattr(embedding_service_mod, "EmbeddingService", _Boom)
    active, dim = rl_doctor._resolve_active_text_embedding()
    assert dim == 1024
    assert active  # qwen3 (or the server constant) — never empty


# ── container probe uses the resolved dim, not a 1024 literal ───────────


def _patched_probe(monkeypatch, health_payload: dict):
    """Run _probe_container with a real RLClient wired to a fake transport."""

    def _factory(**kw):
        return RLClient(
            base_url="http://127.0.0.1:65535",
            client=_Transport(health_payload),
            **kw,
        )

    monkeypatch.setattr(rl_client_mod, "RLClient", _factory)
    return rl_doctor._probe_container()


def test_probe_compatible_on_matching_1536_container(_service_1536, monkeypatch):
    out = _patched_probe(monkeypatch, {
        "ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
        "embedding_dim": 1536, "embedding_space": "openai",
    })
    assert out["client_text_dim"] == 1536
    assert out["client_active_embedding"] == "openai"
    assert out["status"] == "compatible", (
        "a matched 1536 container must NOT be flagged as a mismatch "
        f"(pre-NEW-3 hardcoded 1024 did): {out}"
    )


def test_probe_mismatch_on_1024_container_when_active_is_1536(_service_1536, monkeypatch):
    out = _patched_probe(monkeypatch, {
        "ok": True, "model": "rl", "protocol_version": PROTOCOL_VERSION,
        "embedding_dim": 1024, "embedding_space": "openai",
    })
    assert out["status"] == "embedding_space_mismatch", (
        "a 1024 container against a 1536 active embedding is the RL-2b "
        f"hazard and must be flagged: {out}"
    )


# ── success counter: fallback RATE computable from disk ─────────────────


def _counter(tmp_path) -> dict:
    path = tmp_path / ".claude" / "state" / "rl_fallback_counter.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_success_counter_increments_on_successful_rerank(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    client = RLClient(
        base_url="http://127.0.0.1:65535",
        text_dim=1024,
        active_embedding="qwen3",
        client=_Transport({"ok": True, "model": "rl"}),
    )

    async def _once():
        with patch(
            "claude_mcp_servers.weaviate_mcp.server._get_rl_client",
            return_value=client,
        ):
            return await search_pipeline._do_rerank(
                query="q",
                candidates=[{"title": "A", "score": 0.9}],
                limit=1,
                task_id="tid-success",
                session_id=None,
            )

    assert _run(_once()) is not None
    assert _counter(tmp_path).get("success_count") == 1
    assert _run(_once()) is not None
    assert _counter(tmp_path).get("success_count") == 2
    assert _counter(tmp_path).get("count", 0) == 0, "no fallbacks were recorded"


def test_doctor_reports_success_count_and_rate(tmp_path):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "rl_fallback_counter.json").write_text(
        json.dumps({"count": 1, "success_count": 3, "last_reason": "x"}),
        encoding="utf-8",
    )
    out = rl_doctor._probe_fallback_counter(str(tmp_path))
    assert out["count"] == 1
    assert out["success_count"] == 3
    assert out["fallback_rate"] == pytest.approx(0.25)


def test_doctor_rate_none_when_no_events(tmp_path):
    out = rl_doctor._probe_fallback_counter(str(tmp_path))
    assert out["count"] == 0
    assert out["success_count"] == 0
    assert out["fallback_rate"] is None
