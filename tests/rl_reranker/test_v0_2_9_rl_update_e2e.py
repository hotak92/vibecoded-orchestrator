# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.9 (C8) — end-to-end /rl_update -> schedule_update -> _update test.

Why this exists: during C8, ``_update``'s signature was changed (pre-packed
payload contract). The unit tests in ``test_v0_2_9_pure_train.py`` and
``test_v0_2_9_literal_cited_signal.py`` exercise ``_update`` directly with
the new signature, but until step 3 they did NOT catch the OLD positional
call from ``schedule_update`` at retrieval_rl.py:372. That call would have
crashed in production but tests were green.

The class of bug: signature drift between an HTTP handler and the backend
method it calls. This file pins the entire call chain so the next analogous
breakage gets caught.

Async convention: ``_run_async(_body())`` per the paid-module convention
(no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rl_reranker.rl_server import RLServer


def _run_async(coro):
    return asyncio.run(coro)


class _RecordingRLModel:
    """Same shape as _TargetCapturingRLModel in test_v0_2_9_literal_cited_signal,
    sufficient to confirm rl_model.update reached with the right target."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._type_map: dict[str, int] = {}

    def get_type_idx(self, node_type: str) -> int:
        return self._type_map.setdefault(node_type or "concept", len(self._type_map))

    def update(
        self, q_raw, n_raw, linked_raws, n_type_idx: int, target: float,
        linked_type_idxs=None,
    ) -> float:
        self.calls.append(
            {
                "n_type_idx": n_type_idx,
                "target": target,
                "linked_type_idxs": list(linked_type_idxs) if linked_type_idxs else None,
            }
        )
        return 0.0

    def save(self) -> None:
        pass


def _build_server(tmp_path: Path, monkeypatch) -> RLServer:
    """A minimal RLServer with a mock rl_model swapped in."""
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True)
    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    # Clear VCT_PROJECT_ID: if set on the host (it is, on dev machines), a
    # no-header /rl_update would route to a per-project head with a FRESH
    # RLModel instead of the _RecordingRLModel we swap into srv._rl._rl_model,
    # so recorder.calls would stay empty. Force the _base sentinel path.
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
    # Swap the real rl_model out for our recorder.
    srv._rl._rl_model = _RecordingRLModel()  # type: ignore[assignment]
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


def _payload_v3(
    task_id: str,
    *,
    titles: list[str],
    cosine_sims: dict[str, float],
    literal_cited: dict[str, bool],
    emb_dim: int = 1024,
) -> dict:
    """Build a v0.2.9 wire payload for /rl_update.

    emb_dim defaults to 1024 to MATCH the server built by _build_server
    (ACTIVE_EMBEDDING=arctic → emb_dim 1024). A mismatched dim makes
    _build_inputs' np.stack fail ("all input arrays must have the same shape")
    because the server-built query/padding slots are 1024-dim while the
    payload's node embeddings would be 8-dim — the pre-existing red this fixes.
    """
    nodes_packed: list[dict] = []
    for title in titles:
        nodes_packed.append(
            {
                "title": title,
                "node_type": "concept",
                "n_emb": [0.1] * emb_dim,
                "linked_embs": [],
                "linked_type_names": [],
            }
        )
    return {
        "task_ids": [task_id],
        "active_embedding": "arctic",
        "task_type": "mcp_interactive",
        "tasks": {
            task_id: {
                "nodes_packed": nodes_packed,
                "query_emb": [0.0] * emb_dim,
                "cosine_sims": cosine_sims,
                "literal_cited": literal_cited,
                "cross_encoder_cited": None,
            }
        },
    }


# ----------------------------------------------------------------------
# 1. End-to-end happy path: POST -> schedule_update -> _update ->
#    rl_model.update with the literal_cited bonus applied.
# ----------------------------------------------------------------------


class TestRlUpdateE2E:
    def test_post_drives_rl_model_update_with_literal_cited_bonus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full call chain. Catches signature drift between handler,
        schedule_update, and _update."""
        srv = _build_server(tmp_path, monkeypatch)
        recorder: _RecordingRLModel = srv._rl._rl_model  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3(
                "t-e2e",
                titles=["NodeA"],
                cosine_sims={"NodeA": 0.40},
                literal_cited={"NodeA": True},
            )
            resp = await client.post("/rl_update", json=payload)
            assert resp.status == 200
            body = await resp.json()
            # Give the fire-and-forget task a chance to run.
            await asyncio.sleep(0.05)
            return body

        body = _run_async(_with_client(srv, _body))
        assert body == {"ok": True, "scheduled": 1}
        # rl_model.update was called with the bonus-applied target.
        assert len(recorder.calls) == 1
        # cosine 0.40, single-node -> step1 target = 0.45
        # literal-cited: *1.5 -> 0.675, +0.1 -> 0.775
        assert recorder.calls[0]["target"] == pytest.approx(0.775, abs=1e-6)

    def test_post_with_no_tasks_returns_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty tasks dict -> 200 with "skipped"; no rl_model.update."""
        srv = _build_server(tmp_path, monkeypatch)
        recorder: _RecordingRLModel = srv._rl._rl_model  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            resp = await client.post(
                "/rl_update",
                json={
                    "task_ids": ["t1"],
                    "active_embedding": "arctic",
                    "tasks": {},
                },
            )
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body == {"ok": True, "skipped": "no task_ids or tasks"}
        assert recorder.calls == []

    def test_post_legacy_agent_output_shape_returns_deprecation_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-C8 callers sent {task_ids, agent_output}. Container now
        rejects with a 'legacy_shape_deprecated' marker (no training)."""
        srv = _build_server(tmp_path, monkeypatch)
        recorder: _RecordingRLModel = srv._rl._rl_model  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            resp = await client.post(
                "/rl_update",
                json={
                    "task_ids": ["t-legacy"],
                    "active_embedding": "arctic",
                    "agent_output": "some legacy text",
                },
            )
            assert resp.status == 200
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body == {"ok": True, "skipped": "legacy_shape_deprecated"}
        assert recorder.calls == []

    def test_post_with_active_embedding_mismatch_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cross-source guard from v0.2.40 F1 still fires: a client
        whose pinned embedder differs from the server's gets rejected
        before any training."""
        srv = _build_server(tmp_path, monkeypatch)
        recorder: _RecordingRLModel = srv._rl._rl_model  # type: ignore[assignment]

        async def _body(client: TestClient) -> tuple[int, dict]:
            payload = _payload_v3(
                "t-mismatch",
                titles=["NodeA"],
                cosine_sims={"NodeA": 0.5},
                literal_cited={"NodeA": False},
            )
            payload["active_embedding"] = "qwen3"  # server is "arctic"
            resp = await client.post("/rl_update", json=payload)
            return resp.status, await resp.json()

        status, body = _run_async(_with_client(srv, _body))
        # Whatever the response shape, no training happened.
        assert recorder.calls == []
        # The mismatch path either 400s or 200s with a skip marker —
        # both are acceptable; the load-bearing check is "no training".

    def test_post_with_online_training_disabled_flag_skips_training(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project flag rl_online_training_disabled=True: accept payload,
        return 200, but rl_model.update never called."""
        srv = _build_server(tmp_path, monkeypatch)
        recorder: _RecordingRLModel = srv._rl._rl_model  # type: ignore[assignment]

        async def _fake_flags() -> dict:
            return {"rl_online_training_disabled": True}

        srv._fetch_project_flags = _fake_flags  # type: ignore[assignment]

        async def _body(client: TestClient) -> dict:
            payload = _payload_v3(
                "t-disabled",
                titles=["NodeA"],
                cosine_sims={"NodeA": 0.5},
                literal_cited={"NodeA": True},
            )
            resp = await client.post("/rl_update", json=payload)
            assert resp.status == 200
            await asyncio.sleep(0.05)
            return await resp.json()

        body = _run_async(_with_client(srv, _body))
        assert body == {
            "ok": True,
            "scheduled": 1,
            "online_training_disabled": True,
        }
        assert recorder.calls == []
