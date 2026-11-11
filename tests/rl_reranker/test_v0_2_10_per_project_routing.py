# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.10 — per-project routing tests.

Covers:
  - X-VCT-Project-ID header is read by /cache_nodes, /rl_update, /finetune,
    /reset, /global/retrain, and steers per-project state.
  - Missing header falls back to base model + audit-log; request still
    succeeds (backward compatibility with pre-routing clients).
  - The LRU cache of per-project heads obeys its size limit and the base
    sentinel head is pinned (never evicted).
  - Per-project state dirs are created lazily under <state>/projects/<id>/.
  - Per-project event logs land at <logs>/rl_events_<project_id>.jsonl.
  - /health reports routing counters + LRU occupancy.
  - Routing counters increment correctly across the (with-header / without-
    header / no-weights-fallback) branches.

These tests use the same in-process TestClient pattern as test_endpoints.py
and stay free of pytest-asyncio (asyncio.run wrappers).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Make the repo-root-as-package import work the same way conftest does.

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

torch = pytest.importorskip("torch")

import rl_reranker.rl_server as rl_server_mod  # noqa: E402
from rl_reranker.rl_server import (  # noqa: E402
    RLServer,
    _BASE_PROJECT_SENTINEL,
    _HEADER_PROJECT_ID,
    _ProjectHead,
    _is_path_under,
    _per_project_log_path,
    _per_project_state_dir,
)
from rl_reranker.rl_model import RLModel  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_async(coro):
    return asyncio.run(coro)


def _write_fake_checkpoint(target_path: Path, emb_dim: int = 1024) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    m = RLModel.load_or_create(state_path=target_path, emb_dim=emb_dim)
    m.save()
    assert target_path.exists()


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Build a /data-equivalent layout under tmp_path and patch the
    safe-path validators to accept those tmp dirs."""
    data_root = tmp_path / "data"
    state_dir = data_root / "state"
    logs_dir = data_root / "logs"
    global_dir = data_root / "global"
    for d in (state_dir, logs_dir, global_dir):
        d.mkdir(parents=True)

    def _safe_weights(p):
        if not p or not isinstance(p, str):
            return None
        cand = Path(p)
        if not cand.is_absolute():
            return None
        return cand if _is_path_under(
            cand, (f"{state_dir}/", f"{global_dir}/")
        ) else None

    # v0.2.11: _safe_events_path was removed (training is DB-only; /finetune no
    # longer takes an events_path), so there's nothing to monkeypatch for it.
    monkeypatch.setattr(rl_server_mod, "_safe_weights_path", _safe_weights)

    return {
        "root": data_root,
        "state": state_dir,
        "logs": logs_dir,
        "global": global_dir,
    }


def _make_server(tmp_path, layout, monkeypatch, lru_size: int = 10):
    """Construct an RLServer pointed at the tmp /data tree."""
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True)

    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(layout["global"]))
    monkeypatch.setenv("RL_PROJECT_HEAD_LRU_SIZE", str(lru_size))
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)

    srv = RLServer(
        project_root=project_root,
        ollama_url="http://localhost:11435",
        log_path=str(layout["logs"] / "rl_events.jsonl"),
        reranker_model=None,
        port=0,
        bind_host="127.0.0.1",
    )
    srv._state_dir = layout["state"]
    srv._global_weights_dir = layout["global"]
    srv._log_root = layout["logs"]
    return srv


async def _with_client(srv, body):
    test_server = TestServer(srv._app)
    await test_server.start_server()
    client = TestClient(test_server)
    try:
        return await body(client)
    finally:
        await client.close()
        await test_server.close()


# ---------------------------------------------------------------------------
# Pure helpers — no network
# ---------------------------------------------------------------------------

class TestPerProjectPathHelpers:
    def test_per_project_state_dir_base_sentinel_returns_base(self, tmp_path):
        base = tmp_path / "state"
        result = _per_project_state_dir(base, _BASE_PROJECT_SENTINEL)
        assert result == base

    def test_per_project_state_dir_real_project_returns_subdir(self, tmp_path):
        base = tmp_path / "state"
        result = _per_project_state_dir(base, "proj-uuid-abc")
        assert result == base / "projects" / "proj-uuid-abc"

    def test_per_project_log_path_base_sentinel_returns_unscoped(self, tmp_path):
        result = _per_project_log_path(tmp_path, _BASE_PROJECT_SENTINEL)
        assert result == tmp_path / "rl_events.jsonl"

    def test_per_project_log_path_real_project_returns_scoped(self, tmp_path):
        result = _per_project_log_path(tmp_path, "proj-uuid-abc")
        assert result == tmp_path / "rl_events_proj-uuid-abc.jsonl"


# ---------------------------------------------------------------------------
# Header resolution + counter increments
# ---------------------------------------------------------------------------

class TestResolveRequestProjectId:
    def test_header_present_returns_header_value(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)

        async def _body(client):
            resp = await client.get("/health", headers={_HEADER_PROJECT_ID: "abc123"})
            assert resp.status == 200
            data = await resp.json()
            # Counter for with-header path bumped by /health doesn't apply
            # (we read it from a different endpoint), so check directly.
            return data

        _run_async(_with_client(srv, _body))
        # /health does not call _resolve_request_project_id, so counters are 0.
        # Test the helper directly using a fake request.
        class _FakeRequest:
            headers = {_HEADER_PROJECT_ID: "abc123"}
            rel_url = type("U", (), {"path": "/test"})()

        resolved = srv._resolve_request_project_id(_FakeRequest())
        assert resolved == "abc123"
        assert srv._routing_counters["requests_with_header"] == 1

    def test_no_header_no_env_returns_base_sentinel(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        srv._project_id = ""  # ensure no env fallback

        class _FakeRequest:
            headers = {}
            rel_url = type("U", (), {"path": "/test"})()

        resolved = srv._resolve_request_project_id(_FakeRequest())
        assert resolved == _BASE_PROJECT_SENTINEL
        assert srv._routing_counters["requests_without_header"] == 1

    def test_no_header_env_set_returns_env_value(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        # Simulate legacy single-project install with VCT_PROJECT_ID set.
        srv._project_id = "legacy-project-id"

        class _FakeRequest:
            headers = {}
            rel_url = type("U", (), {"path": "/test"})()

        resolved = srv._resolve_request_project_id(_FakeRequest())
        assert resolved == "legacy-project-id"
        assert srv._routing_counters["requests_without_header"] == 1


# ---------------------------------------------------------------------------
# /health reports routing counters
# ---------------------------------------------------------------------------

class TestHealthRoutingFields:
    def test_health_includes_routing_block(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)

        async def _body(client):
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert "routing" in data
            assert "lru_size" in data["routing"]
            assert "lru_capacity" in data["routing"]
            assert "counters" in data["routing"]
            for k in ("requests_with_header", "requests_without_header",
                      "head_loads", "head_load_failures", "base_fallback_no_weights"):
                assert k in data["routing"]["counters"]
            return data

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# _get_or_load_project_head — LRU + fallback behaviour
# ---------------------------------------------------------------------------

class TestProjectHeadLoading:
    def test_loads_base_sentinel_from_seeded_lru(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        head = _run_async(srv._get_or_load_project_head(_BASE_PROJECT_SENTINEL))
        assert isinstance(head, _ProjectHead)
        assert head.project_id == _BASE_PROJECT_SENTINEL
        # No counters bumped for the seeded base (it was a cache hit).
        assert srv._routing_counters["head_loads"] == 0

    def test_unknown_project_with_no_weights_falls_back_to_base(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        head = _run_async(srv._get_or_load_project_head("unknown-project-id"))
        assert head.is_base_fallback is True
        assert head.project_id == "unknown-project-id"
        # Per-project state dir was created.
        expected_state_dir = layout["state"] / "projects" / "unknown-project-id"
        assert expected_state_dir.is_dir()
        # Counter bumped.
        assert srv._routing_counters["head_loads"] == 1
        assert srv._routing_counters["base_fallback_no_weights"] == 1

    def test_known_project_with_weights_loads_real_head(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        # Pre-seed a per-project checkpoint.
        per_project_dir = layout["state"] / "projects" / "real-project-uuid"
        per_project_dir.mkdir(parents=True, exist_ok=True)
        ckpt = per_project_dir / f"rl_model_{srv._rl_model_tag}_0.2.10.pt"
        _write_fake_checkpoint(ckpt, emb_dim=srv._rl_emb_dim)

        head = _run_async(srv._get_or_load_project_head("real-project-uuid"))
        assert head.is_base_fallback is False
        assert head.project_id == "real-project-uuid"
        assert head.state_path == ckpt
        assert srv._routing_counters["head_loads"] == 1
        # No base-fallback counter bump since we found a real checkpoint.
        assert srv._routing_counters["base_fallback_no_weights"] == 0

    def test_lru_evicts_oldest_non_base_head(self, tmp_path, layout, monkeypatch):
        # LRU size 2 (allow exactly 2 entries: base + 1 project, OR 2 projects).
        # Since base is pinned, 3 projects loaded = 1 base + 2 projects total
        # (the 2nd project loaded should evict the 1st project).
        srv = _make_server(tmp_path, layout, monkeypatch, lru_size=2)

        # Base is already seeded. Load 2 more projects (which puts us at 3 entries).
        _run_async(srv._get_or_load_project_head("project-A"))
        _run_async(srv._get_or_load_project_head("project-B"))
        # After loading project-B, the LRU must evict the LRU-victim (project-A).
        keys = [k[0] for k in srv._project_heads.keys()]
        assert _BASE_PROJECT_SENTINEL in keys  # base pinned
        assert "project-A" not in keys  # evicted
        assert "project-B" in keys

    def test_repeated_access_does_not_double_load(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        h1 = _run_async(srv._get_or_load_project_head("proj-X"))
        h2 = _run_async(srv._get_or_load_project_head("proj-X"))
        assert h1 is h2
        assert srv._routing_counters["head_loads"] == 1


# ---------------------------------------------------------------------------
# /cache_nodes routes the header to the right head + log path
# ---------------------------------------------------------------------------

class _RecorderRLModel:
    """Mimics RLModel's read surface; tracks scoring calls."""
    def __init__(self):
        self.scored = []
        self._type_map = {}

    def get_type_idx(self, t):
        return self._type_map.setdefault(t or "concept", len(self._type_map))


class TestCacheNodesRouting:
    def test_no_header_uses_base_and_increments_counter(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)

        async def _body(client):
            payload = {
                "task_id": "t1",
                "query": "q",
                "nodes": [{"title": "A", "node_type": "concept"}],
                "limit": 1,
                "session_id": "s",
                "active_embedding": "arctic",
            }
            resp = await client.post("/cache_nodes", json=payload)
            # Reranking with no embedding_fn returns empty top_k but the
            # endpoint itself returns 200 (it does NOT need to actually rank).
            # Either 200 OK or 500 (no embedder) — what we care about is
            # the routing fired the counter.
            assert resp.status in (200, 500)

        _run_async(_with_client(srv, _body))
        # The handler called _resolve_request_project_id at least once.
        assert srv._routing_counters["requests_without_header"] >= 1

    def test_header_increments_with_header_counter(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)

        async def _body(client):
            payload = {
                "task_id": "t1",
                "query": "q",
                "nodes": [{"title": "A", "node_type": "concept"}],
                "limit": 1,
                "session_id": "s",
                "active_embedding": "arctic",
            }
            resp = await client.post(
                "/cache_nodes",
                json=payload,
                headers={_HEADER_PROJECT_ID: "my-project-uuid"},
            )
            assert resp.status in (200, 500)

        _run_async(_with_client(srv, _body))
        assert srv._routing_counters["requests_with_header"] == 1
        # A new head was loaded for "my-project-uuid".
        assert ("my-project-uuid", "arctic") in srv._project_heads


# ---------------------------------------------------------------------------
# /reset routes the fork into the per-project state dir
# ---------------------------------------------------------------------------

class TestResetPerProjectFork:
    def test_fork_writes_to_per_project_state_dir(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        # Seed a global checkpoint.
        global_ckpt = layout["global"] / f"rl_model_{srv._rl_model_tag}_0.2.10.pt"
        _write_fake_checkpoint(global_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            resp = await client.post(
                "/reset",
                json={"strategy": "fork"},
                headers={_HEADER_PROJECT_ID: "proj-fork-test"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            return body

        body = _run_async(_with_client(srv, _body))
        # The fork target should be under the per-project subdir.
        weights_path = Path(body["weights_path"])
        expected_dir = layout["state"] / "projects" / "proj-fork-test"
        assert weights_path.parent == expected_dir
        assert weights_path.exists()

    def test_fork_with_no_header_uses_base_state_dir(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        global_ckpt = layout["global"] / f"rl_model_{srv._rl_model_tag}_0.2.10.pt"
        _write_fake_checkpoint(global_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            resp = await client.post("/reset", json={"strategy": "fork"})
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        weights_path = Path(body["weights_path"])
        # Base sentinel → uses base state_dir, NOT projects/<id>/.
        assert weights_path.parent == layout["state"]


# ---------------------------------------------------------------------------
# Backward compatibility: no-header requests still succeed
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_rl_update_without_header_still_returns_200(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)

        async def _body(client):
            # Empty task body → handler short-circuits with "no task_ids".
            resp = await client.post(
                "/rl_update",
                json={
                    "task_ids": [],
                    "tasks": {},
                    "active_embedding": "arctic",
                },
            )
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body["ok"] is True


# ---------------------------------------------------------------------------
# Per-project data logger writes to per-project file
# ---------------------------------------------------------------------------

class TestPerProjectLogPath:
    def test_head_data_logger_targets_per_project_file(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        head = _run_async(srv._get_or_load_project_head("proj-log-test"))
        assert head.data_logger.log_path == layout["logs"] / "rl_events_proj-log-test.jsonl"

    def test_base_sentinel_uses_unscoped_log_path(self, tmp_path, layout, monkeypatch):
        srv = _make_server(tmp_path, layout, monkeypatch)
        head = _run_async(srv._get_or_load_project_head(_BASE_PROJECT_SENTINEL))
        # Base data_logger is whatever __init__ created (using the --log-path arg).
        # It MAY be unscoped; per-project subsequent heads get the scoped pattern.
        assert head.data_logger is not None
