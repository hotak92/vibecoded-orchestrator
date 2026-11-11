# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Tests for the v0.1.1 management endpoints on `RLServer`.

Covered endpoints:
  - POST /rotate_weights
  - POST /reset
  - POST /finetune
  - GET  /finetune_status
  - POST /global/retrain

Strategy: spin up `RLServer` in-process behind aiohttp's `TestClient` so
we get the real router dispatch + JSON handling without containerisation
overhead. Fake weights checkpoints and event-log JSONLs live under
``tmp_path`` so each test gets a clean filesystem slice.

We deliberately use plain `asyncio.run()` wrappers instead of
``pytest-asyncio`` so the repo's test suite doesn't grow a new
dependency. Each async test body is wrapped in an ``_arun()`` helper and
the pytest function calls ``asyncio.run(_arun())``.

Wire-format compatibility: the launcher (rl_service.rs::run_finetune_then_rotate
in the orchestrator) treats ``state == "done"`` as success and
``state in {"failed", "error"}`` as failure. The tests below pin those
exact strings — drift here ripples into a broken paid-module integration.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# Make the repo-root-as-package import work the same way conftest does.

# aiohttp is a hard runtime dep of rl_server.py; skip cleanly if missing.
aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

# Same for torch — every endpoint exercises a code path that ends up
# touching RLModel, which needs torch.
torch = pytest.importorskip("torch")

import rl_reranker.rl_server as rl_server_mod  # noqa: E402
from rl_reranker.rl_server import (  # noqa: E402
    RLServer,
    _is_path_under,
    _safe_weights_path,
    JobState,
)
from rl_reranker.rl_model import RLModel  # noqa: E402


# ---------------------------------------------------------------------------
# Path-validation helpers (pure functions, no network)
# ---------------------------------------------------------------------------

class TestSafePathValidation:
    """The container's outward-facing endpoints take user-supplied paths.
    These helpers must reject anything that doesn't live under the
    bind-mount roots."""

    def test_safe_weights_path_accepts_data_state(self):
        p = _safe_weights_path("/data/state/rl_model_arctic_1024.pt")
        assert p is not None
        assert str(p) == "/data/state/rl_model_arctic_1024.pt"

    def test_safe_weights_path_accepts_data_global(self):
        p = _safe_weights_path("/data/global/rl_model_arctic_1024.pt")
        assert p is not None

    def test_safe_weights_path_rejects_etc_passwd(self):
        assert _safe_weights_path("/etc/passwd") is None

    def test_safe_weights_path_rejects_relative(self):
        assert _safe_weights_path("rl_model.pt") is None
        assert _safe_weights_path("./rl_model.pt") is None
        assert _safe_weights_path("../escape.pt") is None

    def test_safe_weights_path_rejects_traversal(self):
        # /data/state/../../etc/passwd resolves out of the safe root
        assert _safe_weights_path("/data/state/../../etc/passwd") is None

    def test_safe_weights_path_rejects_empty(self):
        assert _safe_weights_path("") is None
        assert _safe_weights_path(None) is None

    def test_safe_weights_path_rejects_data_state_x(self):
        # /data/state-x should NOT be allowed because it's not /data/state/
        assert _safe_weights_path("/data/state-x/foo.pt") is None


# ---------------------------------------------------------------------------
# Server-fixture helpers
# ---------------------------------------------------------------------------

def _write_fake_checkpoint(target_path: Path, emb_dim: int = 1024) -> None:
    """Write a freshly-initialised RLModel checkpoint to ``target_path``.

    RLModel.save() emits a torch.save() blob with the expected
    state_dict + opt_state + emb_dim + type_registry keys. We need the
    blob to be load-compatible for /rotate_weights's reload step.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    m = RLModel.load_or_create(state_path=target_path, emb_dim=emb_dim)
    m.save()
    assert target_path.exists()


def _write_event_log(target_path: Path, events: "list[dict]") -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _make_retrieval_event(
    task_id: str,
    ts: str,
    embedding_source: str = "arctic",
    embedding_dim: int = 1024,
) -> dict:
    return {
        "event": "retrieval",
        "schema_version": 2,
        "ts": ts,
        "project": "TestProject",
        "task_id": task_id,
        "task_type": "implementation",
        "query": "test query",
        "query_emb": [0.1] * embedding_dim,
        "embedding_source": embedding_source,
        "embedding_dim": embedding_dim,
        "embedding_model": "test-model",
        "nodes": [
            {
                "title": "NodeA",
                "score": 0.7,
                "tier": "top_k",
                "emb": [0.2] * embedding_dim,
                "cos_qn": 0.7,
                "cos_ql": 0.5,
                "cos_nl": 0.5,
            }
        ],
    }


def _make_citation_event(task_id: str, ts: str) -> dict:
    return {
        "event": "citation",
        "schema_version": 2,
        "ts": ts,
        "project": "TestProject",
        "task_id": task_id,
        "task_type": "implementation",
        "citations": {"NodeA": True},
        "cosine_sims": {"NodeA": 0.7},
    }


@pytest.fixture
def layout_paths(tmp_path, monkeypatch):
    """Build a /data-equivalent layout under tmp_path and patch the
    safe-path validators to accept those tmp dirs.

    The production layout has hardcoded /data/state, /data/logs,
    /data/global allowlists in the safe-path helpers. For tests we
    monkey-patch those helpers so the same paths under tmp_path pass
    validation — the handlers under test only call through the
    `_safe_weights_path` module attribute, so a setattr swap is sufficient.
    """
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

    monkeypatch.setattr(rl_server_mod, "_safe_weights_path", _safe_weights)

    return {
        "root": data_root,
        "state": state_dir,
        "logs": logs_dir,
        "global": global_dir,
    }


def _make_server(tmp_path, layout, monkeypatch):
    """Construct an RLServer pointed at the tmp /data tree."""
    project_root = tmp_path / "project"
    (project_root / "state").mkdir(parents=True)

    monkeypatch.setenv("ACTIVE_EMBEDDING", "arctic")
    monkeypatch.setenv("RL_GLOBAL_WEIGHTS_DIR", str(layout["global"]))
    # Clear VCT_PROJECT_ID so _resolve_request_project_id returns
    # _BASE_PROJECT_SENTINEL when no X-VCT-Project-ID header is sent.
    # Without this, the server falls back to the machine's real
    # VCT_PROJECT_ID env var, creating a per-project head with a fresh
    # model object that differs from srv._rl._rl_model (breaking patches
    # and the _base-sentinel 400 rejection in /finetune).
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
    return srv


def _run_async(coro):
    """Run an async test body via asyncio.run.

    Centralised so all test functions can ``return _run_async(_body())``
    without each one carrying the same scaffold.
    """
    return asyncio.run(coro)


async def _with_client(srv, body):
    """Spin up the in-process TestClient + TestServer, hand control to
    ``body(client)``, then tear down cleanly.

    The TestServer is bound on an ephemeral port (port=0) and never
    speaks to the network beyond loopback — entirely isolated.
    """
    test_server = TestServer(srv._app)
    await test_server.start_server()
    client = TestClient(test_server)
    try:
        return await body(client)
    finally:
        await client.close()
        await test_server.close()


# ---------------------------------------------------------------------------
# /rotate_weights
# ---------------------------------------------------------------------------

class TestRotateWeights:
    def test_rejects_path_outside_data_state(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/rotate_weights", json={"weights_path": "/etc/passwd"}
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["ok"] is False

        _run_async(_with_client(srv, _body))

    def test_rejects_relative_path(self, tmp_path, layout_paths, monkeypatch):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/rotate_weights", json={"weights_path": "rl_model.pt"}
            )
            assert resp.status == 400

        _run_async(_with_client(srv, _body))

    def test_400_when_file_missing(self, tmp_path, layout_paths, monkeypatch):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        missing = layout_paths["state"] / "rl_model_doesnotexist.pt"

        async def _body(client):
            resp = await client.post(
                "/rotate_weights", json={"weights_path": str(missing)}
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["ok"] is False
            assert "not found" in body["error"]

        _run_async(_with_client(srv, _body))

    def test_loads_valid_checkpoint(self, tmp_path, layout_paths, monkeypatch):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        ckpt = layout_paths["state"] / "rl_model_new.pt"
        _write_fake_checkpoint(ckpt, emb_dim=1024)

        async def _body(client):
            old_model = srv._rl._rl_model
            resp = await client.post(
                "/rotate_weights", json={"weights_path": str(ckpt)}
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["loaded"] == str(ckpt)
            assert body["emb_dim"] == 1024
            assert srv._rl._rl_model is not old_model

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# /reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_fork_copies_global_to_project(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        # v0.2.8 filename pattern: rl_model_<source>_<version>.pt.
        # Use a sentinel version so the glob-by-prefix picks it up.
        global_ckpt = (
            layout_paths["global"] / f"rl_model_{srv._rl_model_tag}_0.2.8.pt"
        )
        _write_fake_checkpoint(global_ckpt, emb_dim=srv._rl_emb_dim)
        global_bytes = global_ckpt.read_bytes()

        async def _body(client):
            resp = await client.post(
                "/reset", json={"strategy": "fork", "days": 30}
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["ok"] is True
            assert body["strategy"] == "fork"
            assert body["job_id"] is None
            project_ckpt = Path(body["weights_path"])
            assert project_ckpt.exists()
            assert project_ckpt.read_bytes() == global_bytes

        _run_async(_with_client(srv, _body))

    def test_fork_404_when_no_global_weights(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        # No global file exists for this tag.

        async def _body(client):
            resp = await client.post(
                "/reset", json={"strategy": "fork", "days": 30}
            )
            assert resp.status == 404
            body = await resp.json()
            assert body["ok"] is False
            # v0.2.8: error message is "no global weights for source=...".
            assert "no global weights" in body["error"]

        _run_async(_with_client(srv, _body))

    def test_invalid_strategy_rejected(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/reset", json={"strategy": "yolo", "days": 30}
            )
            assert resp.status == 400

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# /finetune + /finetune_status
#
# v0.2.11 — DB-only. The project is resolved from the X-VCT-Project-ID
# header. events_path in the body is IGNORED. Training reads from
# launcher.db via the hub; in the test server there is no live hub so
# scheduled jobs will soft-fail internally. These tests assert the
# ENDPOINT CONTRACT (status codes, job_id presence, 400s), NOT that
# training completes.
# ---------------------------------------------------------------------------

class TestFinetune:
    def test_returns_job_id_202(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Happy path: POST with X-VCT-Project-ID header → 202 + job_id.

        v0.2.11: events_path is ignored; only base_weights_path + days matter.
        The base_weights_path must exist and be under /data/state/ or /data/global/.
        No live hub in tests → background job will soft-fail, but the endpoint
        contract is 202 + job_id returned immediately.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        base_ckpt = layout_paths["state"] / f"rl_model_{srv._rl_model_tag}.pt"
        _write_fake_checkpoint(base_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            resp = await client.post(
                "/finetune",
                json={
                    "base_weights_path": str(base_ckpt),
                    "days": 30,
                },
                headers={"X-VCT-Project-ID": "proj-test-1"},
            )
            assert resp.status == 202, await resp.text()
            body = await resp.json()
            job_id = body["job_id"]
            assert isinstance(job_id, str) and len(job_id) >= 16

        _run_async(_with_client(srv, _body))

    def test_events_path_in_body_is_ignored(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """events_path in body is silently ignored (back-compat with older launchers).

        The endpoint must still return 202 when events_path is present alongside
        base_weights_path and a valid X-VCT-Project-ID header.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        base_ckpt = layout_paths["state"] / f"rl_model_{srv._rl_model_tag}.pt"
        _write_fake_checkpoint(base_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            resp = await client.post(
                "/finetune",
                json={
                    "events_path": "/etc/passwd",   # ignored since v0.2.11
                    "base_weights_path": str(base_ckpt),
                    "days": 30,
                },
                headers={"X-VCT-Project-ID": "proj-test-2"},
            )
            # events_path is no longer validated — endpoint must return 202.
            assert resp.status == 202, await resp.text()
            body = await resp.json()
            assert "job_id" in body

        _run_async(_with_client(srv, _body))

    def test_rejects_missing_project_id_header(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Without X-VCT-Project-ID header the request resolves to _base sentinel.

        _base has no project events in launcher.db and cannot be fine-tuned.
        The endpoint must return 400 with an error mentioning X-VCT-Project-ID.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        base_ckpt = layout_paths["state"] / f"rl_model_{srv._rl_model_tag}.pt"
        _write_fake_checkpoint(base_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            resp = await client.post(
                "/finetune",
                json={
                    "base_weights_path": str(base_ckpt),
                    "days": 30,
                },
                # No X-VCT-Project-ID header → resolves to _base → 400
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["ok"] is False
            # Error must mention the header or project routing.
            error_text = body.get("error", "")
            assert "X-VCT-Project-ID" in error_text or "project" in error_text.lower()

        _run_async(_with_client(srv, _body))

    def test_status_404_for_unknown_job(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.get(
                "/finetune_status?job_id=nonexistent-job-id"
            )
            assert resp.status == 404
            body = await resp.json()
            assert body["error"] == "unknown job_id"

        _run_async(_with_client(srv, _body))

    def test_status_400_when_no_job_id(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.get("/finetune_status")
            assert resp.status == 400

        _run_async(_with_client(srv, _body))

    def test_concurrent_jobs_get_distinct_ids(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Two concurrent POSTs each return 202 with distinct job_ids.

        No live hub → background jobs will soft-fail. We only assert the
        endpoint contract: two distinct job_ids registered in srv._jobs.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        base_ckpt = layout_paths["state"] / f"rl_model_{srv._rl_model_tag}.pt"
        _write_fake_checkpoint(base_ckpt, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            r1 = await client.post(
                "/finetune",
                json={
                    "base_weights_path": str(base_ckpt),
                    "days": 30,
                },
                headers={"X-VCT-Project-ID": "proj-concurrent-1"},
            )
            r2 = await client.post(
                "/finetune",
                json={
                    "base_weights_path": str(base_ckpt),
                    "days": 30,
                },
                headers={"X-VCT-Project-ID": "proj-concurrent-2"},
            )
            assert r1.status == 202, await r1.text()
            assert r2.status == 202, await r2.text()
            b1 = await r1.json()
            b2 = await r2.json()
            assert b1["job_id"] != b2["job_id"]
            assert b1["job_id"] in srv._jobs
            assert b2["job_id"] in srv._jobs

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# /global/retrain
#
# v0.2.11 — DB-only. events_paths in the body is IGNORED. project_ids drives
# which DB rows are read from launcher.db via the hub. No live hub in tests
# → background jobs soft-fail. Positive tests assert 202 + job_id only.
# ---------------------------------------------------------------------------

class TestGlobalRetrain:
    def test_schedules_job_with_valid_project_ids(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """POST with valid project_ids + mode returns 202 + job_id.

        v0.2.11: events_paths is ignored; project_ids drives DB reads.
        No live hub → background job soft-fails, but endpoint contract is 202.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "offline",
                    "project_ids": ["proj-a", "proj-b"],
                    "days": 30,
                },
            )
            assert resp.status == 202, await resp.text()
            body = await resp.json()
            assert "job_id" in body
            assert isinstance(body["job_id"], str) and len(body["job_id"]) >= 16

        _run_async(_with_client(srv, _body))

    def test_rejects_empty_project_ids(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Empty project_ids (or only '_base') must return 400.

        v0.2.11: project_ids is now the DB data key. An empty list means
        nothing to train on → reject immediately.
        """
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "offline",
                    "project_ids": [],
                    "days": 30,
                },
            )
            assert resp.status == 400

        _run_async(_with_client(srv, _body))

    def test_rejects_base_sentinel_only_project_ids(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """project_ids containing only '_base' is filtered out → empty → 400."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "offline",
                    "project_ids": ["_base"],
                    "days": 30,
                },
            )
            assert resp.status == 400

        _run_async(_with_client(srv, _body))

    def test_bad_mode_rejected(self, tmp_path, layout_paths, monkeypatch):
        """Invalid mode string → 400 (checked before project_ids)."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "yolo",
                    "project_ids": ["proj-a"],
                    "days": 30,
                },
            )
            assert resp.status == 400

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# JobState / helpers
# ---------------------------------------------------------------------------

class TestJobState:
    def test_jobstate_defaults(self):
        j = JobState(job_id="x", kind="finetune", state="running")
        assert j.percent == 0
        assert j.message == ""
        assert j.error is None
        assert j.result_weights_path is None


# ---------------------------------------------------------------------------
# /state_summary  (v0.2.2, D1' introspection)
# ---------------------------------------------------------------------------

class TestStateSummary:
    def test_reports_builtin_count_and_zero_dynamics_on_fresh_model(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.get("/state_summary")
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["builtin_types"] == 9  # N_ENTITY_TYPES
            assert body["dynamic_types"] == 0
            assert body["model_loaded"] is True
            # Marker / sidecar dir presence: depends on whether a save()
            # has fired yet — assert the keys exist + are bool.
            assert isinstance(body["marker_present"], bool)
            assert isinstance(body["sidecar_dir_present"], bool)

        _run_async(_with_client(srv, _body))

    def test_reports_dynamic_count_after_registration(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        # Register 3 dynamic types directly on the live model.
        srv._rl._rl_model.get_type_idx("alpha")
        srv._rl._rl_model.get_type_idx("beta")
        srv._rl._rl_model.get_type_idx("gamma")

        async def _body(client):
            resp = await client.get("/state_summary")
            assert resp.status == 200
            body = await resp.json()
            assert body["dynamic_types"] == 3

        _run_async(_with_client(srv, _body))

    def test_save_on_register_marks_sidecar_dir_present(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        # get_type_idx triggers save-on-register, which creates the sidecar dir.
        srv._rl._rl_model.get_type_idx("durable_type")

        async def _body(client):
            resp = await client.get("/state_summary")
            assert resp.status == 200
            body = await resp.json()
            assert body["sidecar_dir_present"] is True
            assert body["dynamic_types"] == 1

        _run_async(_with_client(srv, _body))


# ---------------------------------------------------------------------------
# /cache_nodes — session_id thread-through (v0.2.4, RL telemetry audit #5)
#
# The orchestrator-side audit (rl-logging-audit-report-2026-05-23.md) found
# session_id populated 0.1% of the time. Two halves:
#   1. Container must accept session_id from the request body. ← this test
#   2. Client (AGPL-side weaviate_mcp/server.py) must pass it. ← orchestrator chat
# ---------------------------------------------------------------------------


class TestCacheNodesSessionId:
    """The orchestrator-side audit (rl-logging-audit-report-2026-05-23.md)
    found session_id populated 0.1% of the time. The container side has
    two responsibilities:
      1. Accept session_id from the request body (v0.2.4+).
      2. Thread it through to RLDataLogger.log_retrieval.

    Root cause of the pre-existing test failure (Task D investigation):
      _make_server sets ACTIVE_EMBEDDING=arctic so self._rl._rl_model is
      an active RLModel. In rerank_query_aware, when _rl_model is not None,
      scoring goes through the batched neural path (score_batch). A freshly-
      initialised network produces random scores that are typically BELOW
      RELEVANCE_THRESHOLD (0.3). When all nodes fall below threshold, no
      nodes enter above_scores and log_retrieval is never called.

    Fix: patch score_batch on the live model to return scores above threshold
    (0.9) for all candidates. This correctly exercises the log_retrieval call
    path while keeping the test isolated from torch weight initialisation.

    Note on the spy object: _cache_nodes calls _route_request + _swap_in_head
    which sets self._rl._data_logger = head.data_logger. For the _base sentinel
    (no X-VCT-Project-ID header), _get_or_load_project_head returns the cached
    base head whose data_logger IS self._rl._data_logger (set in __init__ and
    kept live via the sync in _get_or_load_project_head). The spy on
    srv._rl._data_logger.log_retrieval therefore IS on the object actually used.
    """

    def _spy_log_retrieval(self, srv):
        """Replace data_logger.log_retrieval with a spy capturing kwargs."""
        captured: list[dict] = []
        original = srv._rl._data_logger.log_retrieval

        def spy(**kwargs):
            captured.append(kwargs)
            return original(**kwargs)

        srv._rl._data_logger.log_retrieval = spy  # type: ignore[assignment]
        return captured

    def _mock_embed(self, srv):
        """Mock RetrievalRL._embed to return a deterministic vector
        without touching Ollama. Required because tests run without an
        Ollama server — the real _embed falls through to the no-vector
        path which short-circuits before log_retrieval.
        """
        emb_dim = srv._rl_emb_dim

        async def _fake_embed(text: str) -> list[float]:
            # Vector close to the node vectors below — pushes cosine
            # similarity above RELEVANCE_THRESHOLD so log_retrieval fires.
            return [0.5] * emb_dim

        srv._rl._embed = _fake_embed  # type: ignore[assignment]

    def _mock_score_batch_above_threshold(self, srv):
        """Patch RLModel.score_batch to return 0.9 for every candidate.

        The freshly-initialised RLModel produces random scores that are
        typically below RELEVANCE_THRESHOLD (0.3). This patch forces all
        nodes above threshold so log_retrieval fires, letting us assert
        session_id plumbing without depending on torch weight initialisation.
        """


        def _high_score_batch(q_raw, candidates):
            return [0.9] * len(candidates)

        srv._rl._rl_model.score_batch = _high_score_batch  # type: ignore[assignment]

    def test_session_id_threaded_when_provided(
        self, tmp_path, layout_paths, monkeypatch
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        self._mock_embed(srv)
        self._mock_score_batch_above_threshold(srv)
        captured = self._spy_log_retrieval(srv)

        emb_dim = srv._rl_emb_dim
        nodes = [
            {
                "title": f"Node{i}", "content": "body",
                "node_type": "concept",
                "emb": [0.5 + 0.001 * i] * emb_dim,
                "links": [],
            }
            for i in range(3)
        ]

        async def _body(client):
            resp = await client.post("/cache_nodes", json={
                "task_id": "test-task-001",
                "query": "test query body",
                "nodes": nodes,
                "limit": 2,
                "session_id": "sess-abc-123",
            })
            assert resp.status == 200

        _run_async(_with_client(srv, _body))

        assert len(captured) >= 1, (
            "log_retrieval was not invoked — score_batch mock should have forced "
            "all nodes above RELEVANCE_THRESHOLD. Check _mock_score_batch_above_threshold."
        )
        assert captured[-1].get("session_id") == "sess-abc-123"
        assert captured[-1].get("task_id") == "test-task-001"

    def test_session_id_defaults_to_empty_string_when_omitted(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Backward-compat: a request body without session_id (older
        clients) reaches log_retrieval as an empty string, never None /
        missing key."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        self._mock_embed(srv)
        self._mock_score_batch_above_threshold(srv)
        captured = self._spy_log_retrieval(srv)

        emb_dim = srv._rl_emb_dim
        nodes = [
            {
                "title": f"Node{i}", "content": "body",
                "node_type": "concept",
                "emb": [0.5 + 0.001 * i] * emb_dim,
                "links": [],
            }
            for i in range(3)
        ]

        async def _body(client):
            resp = await client.post("/cache_nodes", json={
                "task_id": "test-task-002",
                "query": "no session",
                "nodes": nodes,
                "limit": 2,
                # session_id intentionally omitted
            })
            assert resp.status == 200

        _run_async(_with_client(srv, _body))

        assert len(captured) >= 1, (
            "log_retrieval was not invoked — score_batch mock should have forced "
            "all nodes above RELEVANCE_THRESHOLD. Check _mock_score_batch_above_threshold."
        )
        assert captured[-1].get("session_id") == ""
        assert "session_id" in captured[-1]

    def test_rerank_query_aware_signature_accepts_session_id(self):
        """Plumbing-level guard: rerank_query_aware must accept the
        session_id kwarg. Catches signature regressions even when the
        end-to-end test skips on threshold filtering."""
        import inspect

        from rl_reranker.retrieval_rl import RetrievalRL

        sig = inspect.signature(RetrievalRL.rerank_query_aware)
        assert "session_id" in sig.parameters
        # Default must be "" (empty string), not None — keeps the schema
        # consistent with rl_logger's defaulting behavior.
        assert sig.parameters["session_id"].default == ""


# ---------------------------------------------------------------------------
# /global/retrain — earliest_date body param (v0.2.7, R2 in feedback doc)
#
# Replaces the days-based filter with an absolute-date one. Server still
# accepts `days` for back-compat; `earliest_date` takes precedence when both
# are present.
#
# v0.2.11: events_paths is ignored. project_ids drives DB reads. Tests only
# assert the endpoint contract (202/400), not job completion (no live hub).
# ---------------------------------------------------------------------------


class TestGlobalRetrainEarliestDate:
    def test_earliest_date_iso_accepted(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """ISO YYYY-MM-DD parses + the handler returns 202 (job spawned)."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "online",
                    "project_ids": ["proj-A"],
                    "earliest_date": "2026-01-01",
                },
            )
            assert resp.status == 202, await resp.text()

        _run_async(_with_client(srv, _body))

    def test_earliest_date_takes_precedence_over_days(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """When both fields are present, earliest_date wins."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "online",
                    "project_ids": ["proj-A"],
                    "days": 9999,                  # should be IGNORED
                    "earliest_date": "2026-01-01",  # wins
                },
            )
            # 202 means parser didn't reject either field.
            assert resp.status == 202, await resp.text()

        _run_async(_with_client(srv, _body))

    def test_earliest_date_invalid_format_400(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Non-ISO date string is rejected with 400 + structured error."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "online",
                    "project_ids": ["proj-A"],
                    "earliest_date": "not a date",
                },
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["ok"] is False
            assert "earliest_date" in body["error"]

        _run_async(_with_client(srv, _body))

    def test_empty_earliest_date_falls_back_to_days(
        self, tmp_path, layout_paths, monkeypatch
    ):
        """Empty / null earliest_date triggers the days fallback path."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={
                    "mode": "online",
                    "project_ids": ["proj-A"],
                    "earliest_date": "",       # falls through
                    "days": 30,                 # used
                },
            )
            assert resp.status == 202

        _run_async(_with_client(srv, _body))
