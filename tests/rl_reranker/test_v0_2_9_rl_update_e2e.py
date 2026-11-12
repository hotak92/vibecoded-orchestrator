# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""End-to-end ``POST /rl_update`` -> the routed project's online learner.

Why this exists (v0.2.9 C8): a signature drift between the HTTP handler and
the backend it calls once crashed in production with green unit tests. This
file pins the whole chain. v0.2.106: the chain is ``_rl_update`` ->
``ProjectLearner.ingest`` (per project, never the shared base) -> rounds;
the pre-v0.2.106 ``schedule_update`` task ran after the routing lock was
released and could train whichever project's model was swapped in by then.

Async convention: ``_run_async(_body())`` (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("torch")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from rl_reranker.rl_server import RLServer  # noqa: E402

D = 16
PID = "p-e2e"
HDR = {"X-VCT-Project-ID": PID}


def _run_async(coro):
    return asyncio.run(coro)


def _build_server(tmp_path: Path, monkeypatch) -> RLServer:
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    monkeypatch.setenv("RL_MODEL_TAG", "arctic")
    monkeypatch.setenv("RL_EMB_DIM", str(D))
    # A host VCT_PROJECT_ID would route header-less requests to a project.
    monkeypatch.delenv("VCT_PROJECT_ID", raising=False)
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(tmp_path / "global"))
    (tmp_path / "global").mkdir(exist_ok=True)
    srv = RLServer(
        project_root=project_root,
        ollama_url="http://localhost:11435",
        log_path=str(tmp_path / "rl_events.jsonl"),
        reranker_model=None,
        port=0,
        bind_host="127.0.0.1",
    )
    srv._hub_client = None  # never contact a live hub from a test
    return srv


async def _with_client(srv: RLServer, body):
    test_server = TestServer(srv._app)
    await test_server.start_server()
    client = TestClient(test_server)
    try:
        return await body(client)
    finally:
        await client.close()
        await test_server.close()


def _payload_v3(task_id: str, *, titles: list[str], cosine_sims: dict[str, float],
                literal_cited: dict[str, bool]) -> dict:
    return {
        "task_ids": [task_id],
        "active_embedding": "arctic",
        "task_type": "mcp_interactive",
        "tasks": {
            task_id: {
                "nodes_packed": [{"title": t, "node_type": "concept", "n_emb": [0.1] * D,
                                  "linked_embs": [], "linked_type_names": []} for t in titles],
                "query_emb": [0.2] * D,
                "cosine_sims": cosine_sims,
                "literal_cited": literal_cited,
                "cross_encoder_cited": None,
            }
        },
    }


def _learner(srv: RLServer):
    head = srv._project_heads.get((PID, "arctic"))
    return head.learner if head is not None else None


class TestRlUpdateE2E:
    def test_post_reaches_the_project_learner_with_the_literal_bonus(self, tmp_path, monkeypatch):
        srv = _build_server(tmp_path, monkeypatch)

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3("t-e2e", titles=["NodeA"], cosine_sims={"NodeA": 0.40},
                                  literal_cited={"NodeA": True})
            resp = await client.post("/rl_update", json=payload, headers=HDR)
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body["ok"] is True and body["scheduled"] == 1 and body["serving"] is False
        lr = _learner(srv)
        assert lr is not None and lr.counters["events_seen"] == 1
        (q,) = list(lr._queue)
        # cosine 0.40, single node -> 0.45; literal: *1.5 -> 0.675, +0.1 -> 0.775
        assert float(q.target[0]) == pytest.approx(0.775, abs=1e-6)
        assert bool(q.literal[0]) is True

    def test_post_without_a_project_id_trains_nothing(self, tmp_path, monkeypatch):
        """Per-project models only: no header (and no VCT_PROJECT_ID) = skip."""
        srv = _build_server(tmp_path, monkeypatch)

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3("t-nohdr", titles=["NodeA"], cosine_sims={"NodeA": 0.4},
                                  literal_cited={})
            resp = await client.post("/rl_update", json=payload)
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body == {"ok": True, "scheduled": 0, "skipped": "no_project_id"}
        assert all(h.learner is None for h in srv._project_heads.values())

    def test_post_with_no_tasks_returns_skipped(self, tmp_path, monkeypatch):
        srv = _build_server(tmp_path, monkeypatch)

        async def _body(client: TestClient) -> dict:
            resp = await client.post("/rl_update", headers=HDR, json={
                "task_ids": ["t1"], "active_embedding": "arctic", "tasks": {}})
            assert resp.status == 200
            return await resp.json()

        assert _run_async(_with_client(srv, _body)) == {"ok": True, "skipped": "no task_ids or tasks"}
        assert _learner(srv) is None

    def test_post_legacy_agent_output_shape_returns_deprecation_marker(self, tmp_path, monkeypatch):
        srv = _build_server(tmp_path, monkeypatch)

        async def _body(client: TestClient) -> dict:
            resp = await client.post("/rl_update", headers=HDR, json={
                "task_ids": ["t-legacy"], "active_embedding": "arctic",
                "agent_output": "some legacy text"})
            assert resp.status == 200
            return await resp.json()

        assert _run_async(_with_client(srv, _body)) == {"ok": True, "skipped": "legacy_shape_deprecated"}
        assert _learner(srv) is None

    def test_post_with_active_embedding_mismatch_rejected(self, tmp_path, monkeypatch):
        srv = _build_server(tmp_path, monkeypatch)

        async def _body(client: TestClient) -> int:
            payload = _payload_v3("t-mismatch", titles=["NodeA"], cosine_sims={"NodeA": 0.5},
                                  literal_cited={})
            payload["active_embedding"] = "qwen3"  # server is "arctic"
            resp = await client.post("/rl_update", json=payload, headers=HDR)
            return resp.status

        assert _run_async(_with_client(srv, _body)) == 409
        assert _learner(srv) is None  # rejected before routing: nothing learned

    def test_post_with_online_training_disabled_flag_skips_training(self, tmp_path, monkeypatch):
        """``rl_online_training_disabled``: accepted, judged, never trained."""
        srv = _build_server(tmp_path, monkeypatch)

        async def _fake_flags() -> dict:
            return {"rl_online_training_disabled": True}

        srv._fetch_project_flags = _fake_flags  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3("t-disabled", titles=["NodeA"], cosine_sims={"NodeA": 0.5},
                                  literal_cited={"NodeA": True})
            resp = await client.post("/rl_update", json=payload, headers=HDR)
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body["scheduled"] == 1 and body["online_training_disabled"] is True
        lr = _learner(srv)
        assert lr is not None and lr.training_enabled is False
        assert lr.counters["queries_trained"] == 0

    def test_use_global_is_read_only_for_the_project(self, tmp_path, monkeypatch):
        """``rl_use_global``: the shared model is read-only — no training."""
        srv = _build_server(tmp_path, monkeypatch)

        async def _fake_flags() -> dict:
            return {"rl_use_global": True}

        srv._fetch_project_flags = _fake_flags  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3("t-glob", titles=["NodeA"], cosine_sims={"NodeA": 0.5},
                                  literal_cited={})
            resp = await client.post("/rl_update", json=payload, headers=HDR)
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body["online_training_disabled"] is True
        assert _learner(srv).training_enabled is False
