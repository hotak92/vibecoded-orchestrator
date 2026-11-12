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

    The production layout has hardcoded /data/state, /data/global roots
    (``_WEIGHTS_ROOTS``). For tests we point that attribute at the same
    dirs under tmp_path, so the REAL validators (containment + the
    listing-rebuilt path a checkpoint is read from) run on every request.
    """
    data_root = tmp_path / "data"
    state_dir = data_root / "state"
    logs_dir = data_root / "logs"
    global_dir = data_root / "global"
    for d in (state_dir, logs_dir, global_dir):
        d.mkdir(parents=True)

    # The real validators run; only their roots move to the tmp tree.
    monkeypatch.setattr(
        rl_server_mod, "_WEIGHTS_ROOTS", (f"{state_dir}/", f"{global_dir}/"),
    )

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
# /cache_nodes — session_id is accepted; the container writes no retrieval
# event (v0.2.106)
#
# v0.2.4 threaded session_id into a container-side retrieval JSONL. That
# write sat after the never-wired query embedding and so never ran; the MCP
# logs every retrieval event (with session_id and the same vectors) to
# rl_events, which is what training reads. The container scores only.
# ---------------------------------------------------------------------------


class TestCacheNodesSessionId:
    def _payload(self, srv, **extra):
        from rl_client.rerank_wire import encode_rerank_features

        dim = srv._rl_emb_dim
        nodes = [
            {"title": f"Node{i}", "node_type": "concept", "emb": [0.5 + 0.001 * i] * dim}
            for i in range(3)
        ]
        body = {
            "task_id": "test-task-001",
            "query": "q",
            "nodes": [{"title": n["title"], "node_type": "concept"} for n in nodes],
            "limit": 2,
            "features": encode_rerank_features([0.5] * dim, nodes, dim=dim),
        }
        body.update(extra)
        return body

    @pytest.mark.parametrize("extra", [{"session_id": "sess-abc-123"}, {}])
    def test_session_id_accepted_and_no_container_retrieval_event(
        self, tmp_path, layout_paths, monkeypatch, extra,
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        calls: list = []
        srv._rl._data_logger.log_retrieval = lambda **kw: calls.append(kw)  # type: ignore[assignment]

        async def _body(client):
            resp = await client.post("/cache_nodes", json=self._payload(srv, **extra))
            assert resp.status == 200
            data = await resp.json()
            # v0.2.106: a request without a project id is answered with its
            # own search order (models are per project) — still a 200.
            assert data["ok"] is True and sorted(data["order"]) == [0, 1, 2]
            assert data["serving"] == "search_order" and data["scored"] is False

        _run_async(_with_client(srv, _body))
        assert calls == []
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


# ---------------------------------------------------------------------------
# v0.2.106 CodeQL hardening: request-supplied paths and project ids
# ---------------------------------------------------------------------------

#: Every route; the project-id header is refused before any handler runs.
_ALL_ROUTES = (
    ("GET", "/health"), ("POST", "/cache_nodes"), ("POST", "/rl_update"),
    ("POST", "/rotate_weights"), ("POST", "/reset"), ("POST", "/finetune"),
    ("GET", "/finetune_status"), ("POST", "/global/retrain"),
    ("GET", "/state_summary"), ("GET", "/project_status"),
)

_HOSTILE_IDS = ("../../escape", "..", "a/b", "proj\nFAKE", "x" * 65, "proj id")


class TestRequestSuppliedPathsContained:
    """RED-PROOF (CodeQL py/path-injection + py/unsafe-deserialization): no
    request value names a file outside the /data roots, and a checkpoint a
    request names is read through the path rebuilt from the roots' listing."""

    @pytest.mark.parametrize("hostile", [h for h in _HOSTILE_IDS if "\n" not in h])
    def test_hostile_project_header_is_400_on_every_route(
        self, tmp_path, layout_paths, monkeypatch, hostile,
    ):
        # (A header value cannot carry a newline on the wire; the newline
        # case is pinned on the path helpers below.)
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        before = sorted(tmp_path.rglob("*"))

        async def _body(client):
            for method, route in _ALL_ROUTES:
                resp = await client.request(
                    method, route, json={}, headers={"X-VCT-Project-ID": hostile},
                )
                assert resp.status == 400, (route, resp.status)
                body = await resp.json()
                assert "invalid project id" in body["error"]

        _run_async(_with_client(srv, _body))
        # Nothing was created anywhere (pre-fix: ``../../escape`` made
        # ``<data>/escape`` beside the state root).
        assert sorted(tmp_path.rglob("*")) == before
        assert not (layout_paths["root"] / "escape").exists()

    def test_hostile_project_id_never_builds_a_path(self, tmp_path):
        base = tmp_path / "state"
        base.mkdir()
        for hostile in _HOSTILE_IDS:
            with pytest.raises(rl_server_mod.InvalidProjectId):
                rl_server_mod._per_project_state_dir(base, hostile)
            with pytest.raises(rl_server_mod.InvalidProjectId):
                rl_server_mod._per_project_log_path(tmp_path, hostile)
            with pytest.raises(rl_server_mod.InvalidProjectId):
                rl_server_mod._ensure_project_state_dir(base, hostile)
        assert sorted(tmp_path.rglob("*")) == [base]

    def test_a_project_dir_symlinked_out_of_the_state_dir_is_not_used(self, tmp_path):
        base = tmp_path / "state"
        (base / "projects").mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (base / "projects" / "p1").symlink_to(outside, target_is_directory=True)
        assert rl_server_mod._existing_project_state_dir(base, "p1") is None
        with pytest.raises(rl_server_mod.InvalidProjectId):
            rl_server_mod._ensure_project_state_dir(base, "p1")
        assert rl_server_mod._global_retrain_seed(base, "p1", "qwen3") is None
        # A real project dir is returned as the listing names it.
        (base / "projects" / "p2").mkdir()
        assert rl_server_mod._ensure_project_state_dir(base, "p2") == base / "projects" / "p2"

    def test_global_retrain_refuses_a_hostile_project_id_in_the_body(
        self, tmp_path, layout_paths, monkeypatch,
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)

        async def _body(client):
            resp = await client.post(
                "/global/retrain",
                json={"mode": "offline", "project_ids": ["ok-id", "../../x"]},
            )
            assert resp.status == 400
            assert "invalid project id" in (await resp.json())["error"]

        _run_async(_with_client(srv, _body))

    def test_rotate_refuses_a_symlink_that_escapes_the_roots(
        self, tmp_path, layout_paths, monkeypatch,
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        outside = tmp_path / "outside.pt"
        _write_fake_checkpoint(outside, emb_dim=srv._rl_emb_dim)
        link = layout_paths["state"] / "rl_model_link.pt"
        link.symlink_to(outside)
        sibling = layout_paths["root"] / "state-x" / "a.pt"
        _write_fake_checkpoint(sibling, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            for path in (str(link), str(layout_paths["state"] / ".." / ".." / "outside.pt"),
                         str(sibling)):
                resp = await client.post("/rotate_weights", json={"weights_path": path})
                assert resp.status == 400, path

        _run_async(_with_client(srv, _body))

    def test_finetune_refuses_a_base_outside_the_roots(
        self, tmp_path, layout_paths, monkeypatch,
    ):
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        outside = tmp_path / "outside.pt"
        _write_fake_checkpoint(outside, emb_dim=srv._rl_emb_dim)

        async def _body(client):
            for path in (str(outside), "/etc/passwd",
                         str(layout_paths["state"] / ".." / ".." / "outside.pt")):
                resp = await client.post(
                    "/finetune", json={"base_weights_path": path, "days": 30},
                    headers={"X-VCT-Project-ID": "proj-ok"},
                )
                assert resp.status == 400, path

        _run_async(_with_client(srv, _body))

    def test_rotate_reads_the_listing_rebuilt_path(
        self, tmp_path, layout_paths, monkeypatch,
    ):
        """A request path spelled with ``.`` and ``..`` that stays inside the
        roots still loads, and what is loaded/reported is the resolved file."""
        srv = _make_server(tmp_path, layout_paths, monkeypatch)
        ckpt = layout_paths["state"] / "sub" / "rl_model_new.pt"
        _write_fake_checkpoint(ckpt, emb_dim=1024)
        spelled = layout_paths["state"] / "sub" / ".." / "sub" / "." / "rl_model_new.pt"

        async def _body(client):
            resp = await client.post("/rotate_weights", json={"weights_path": str(spelled)})
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["loaded"] == str(ckpt)
            assert srv._state_path == ckpt

        _run_async(_with_client(srv, _body))
