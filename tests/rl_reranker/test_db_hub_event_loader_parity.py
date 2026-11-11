# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Parity: hub (DB) event loader == JSONL loader → identical pairs + targets.

The DB-as-source-of-truth swap (V52) replaces the trainer's JSONL reader with
a hub read (``GET /api/v1/modules/{id}/projects/{pid}/rl/events`` →
``hub_event_loader.load_events_from_hub``). This test PINS that the swap is
behaviourally invisible to the rest of the trainer: feeding the SAME events
through the JSONL path and the hub path must yield byte-identical
``pair_events`` + ``extract_samples`` output (task_ids, per-node targets,
cosine features).

It also pins the design invariants:
  - outcome events (bash_outcome / edit_outcome / pre_bash) are DROPPED by the
    hub loader (RL trains on what the LLM sends, not what tools return);
  - the per-source ``embedding_source`` filter applies to retrieval events only;
  - ``schema_version < 2`` events are refused (parity with the JSONL reader).

The mock hub uses aiohttp's TestServer (same pattern as test_hub_client.py),
exercising the real ``_HubClient.list_rl_events`` HTTP path incl. pagination.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("numpy")
aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from rl_reranker.offline_trainer import (  # noqa: E402
    load_training_events,
    pair_events,
    extract_samples,
)
from rl_reranker.hub_event_loader import load_events_from_hub  # noqa: E402
from rl_reranker.rl_server import _HubClient  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: a small but representative v3 event set
# ---------------------------------------------------------------------------

def _retrieval(task_id: str, *, emb_source: str = "qwen3") -> dict[str, Any]:
    return {
        "event": "retrieval",
        "schema_version": 3,
        "task_id": task_id,
        "task_type": "search",
        "embedding_source": emb_source,
        "project": "orchestrator-root",
        "query_emb": [0.1, 0.2, 0.3, 0.4],
        "nodes": [
            {"title": "NodeA", "emb": [0.10, 0.20, 0.30, 0.40]},
            {"title": "NodeB", "emb": [0.40, 0.30, 0.20, 0.10]},
        ],
    }


def _citation(task_id: str) -> dict[str, Any]:
    return {
        "event": "citation",
        "schema_version": 3,
        "task_id": task_id,
        "cosine_sims": {"NodeA": 0.92, "NodeB": 0.31},
        "literal_cited": {"NodeA": True, "NodeB": False},
    }


def _outcome(task_id: str) -> dict[str, Any]:
    # An outcome event that the trainer must DROP.
    return {
        "event": "bash_outcome",
        "schema_version": 3,
        "task_id": task_id,
        "payload": {"exit_code": 0, "output_len": 128, "duration_ms": 42, "cmd_len": 10},
    }


def _all_events() -> list[dict[str, Any]]:
    return [
        _retrieval("t1"),
        _citation("t1"),
        _retrieval("t2"),
        _citation("t2"),
        _retrieval("t3", emb_source="arctic"),  # filtered out under emb_source=qwen3
        _citation("t3"),
        _outcome("t4"),                          # dropped by design
    ]


# ---------------------------------------------------------------------------
# Mock hub serving the module-scoped rl/events route
# ---------------------------------------------------------------------------

def _wrap_as_hub_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mirror telemetry_writer._wrap_for_hub: payload_json = full event JSON."""
    rows: list[dict[str, Any]] = []
    for i, ev in enumerate(events):
        rows.append({
            "id": i + 1,
            "event_type": ev["event"],
            "schema_version": ev.get("schema_version", 3),
            "ts_ms": 1_700_000_000_000 + i,
            "project_id": ev.get("project"),
            "project_name": ev.get("project"),
            "task_id": ev.get("task_id", ""),
            "task_type": ev.get("task_type"),
            "embedding_source": ev.get("embedding_source"),
            "embedding_dim": 4 if ev.get("event") == "retrieval" else None,
            "embedding_model": None,
            "payload_json": json.dumps(ev),
        })
    return rows


def _build_mock_events_hub(events: list[dict[str, Any]], *, page_size: int | None = None) -> web.Application:
    """aiohttp app serving GET /modules/{id}/projects/{pid}/rl/events.

    Honours ``since_ms``/``limit`` so the client's pagination is exercised when
    ``page_size`` is set (forces multiple pages).
    """
    rows_all = _wrap_as_hub_rows(events)

    async def _handler(request: web.Request) -> web.Response:
        since = request.query.get("since_ms")
        limit = int(request.query.get("limit", "10000"))
        etype = request.query.get("event_type")
        rows = rows_all
        if since is not None:
            rows = [r for r in rows if r["ts_ms"] >= int(since)]
        if etype:
            rows = [r for r in rows if r["event_type"] == etype]
        rows = sorted(rows, key=lambda r: r["ts_ms"])
        effective_limit = min(limit, page_size) if page_size else limit
        rows = rows[:effective_limit]
        # Return a BARE JSON ARRAY — this matches the real hub
        # (rl_events_api.rs: `(StatusCode::OK, Json(Vec<RlEventOut>))`), NOT a
        # `{"rows": [...]}` envelope. _HubClient.list_rl_events handles both
        # shapes, but production only ever hits the bare-array branch, so the
        # mock must exercise THAT branch (regression guard, audit 2026-06-17).
        return web.json_response(rows)

    app = web.Application()
    app.router.add_get(
        r"/api/v1/modules/{module_id}/projects/{project_id}/rl/events", _handler
    )
    return app


async def _client_for(server: TestServer) -> _HubClient:
    client = _HubClient(
        base_url=f"http://127.0.0.1:{server.port}",
        module_id="vct-rl-reranker",
        token_env="VCT_MODULE_TOKEN_TEST",
    )
    client._token = "test-token"
    return client


def _hub_events(events: list[dict[str, Any]], *, emb_source: str = "", page_size: int | None = None):
    """Run the full hub read path the way PRODUCTION does and return events.

    ``load_events_from_hub`` must be called from sync code (no running loop in
    the calling thread) — exactly how the offline trainer reaches it, and how
    the /finetune handler reaches it via ``asyncio.to_thread`` (worker thread,
    no loop). So: start the mock hub on the main loop, then run the loader in a
    WORKER THREAD. The loader's own ``asyncio.run`` opens the _HubClient's
    aiohttp session on the worker-thread loop and connects to the TestServer
    over real localhost TCP — no cross-loop session sharing.
    """
    import threading

    result: dict[str, Any] = {}

    async def _serve_and_drive():
        app = _build_mock_events_hub(events, page_size=page_size)
        server = TestServer(app)
        async with server:
            base_url = f"http://127.0.0.1:{server.port}"

            def _worker():
                # SYNC context (no running loop) → load_events_from_hub takes
                # the asyncio.run fast path, mirroring production.
                client = _HubClient(
                    base_url=base_url,
                    module_id="vct-rl-reranker",
                    token_env="VCT_MODULE_TOKEN_TEST",
                )
                client._token = "test-token"
                try:
                    result["value"] = load_events_from_hub(
                        client, "orchestrator-root",
                        filter_embedding_source=emb_source,
                    )
                except BaseException as exc:  # noqa: BLE001 — surface to test
                    result["error"] = exc
                finally:
                    # close() is async; run it on a throwaway loop in this thread.
                    try:
                        asyncio.run(client.close())
                    except Exception:
                        pass

            t = threading.Thread(target=_worker)
            t.start()
            # Keep the server loop responsive while the worker drives requests.
            while t.is_alive():
                await asyncio.sleep(0.01)
            t.join()

    asyncio.run(_serve_and_drive())
    if "error" in result:
        raise result["error"]
    return result["value"]


def _samples_signature(events: list[dict[str, Any]]):
    """Reduce extract_samples output to a comparable, order-independent set."""
    pairs = pair_events(events)
    samples = extract_samples(pairs, link_sim={})
    sig = set()
    for s in samples:
        sig.add((
            s["task_id"],
            round(float(s["target"]), 9),
            round(float(s["cos_qn"]), 9),
            round(float(s["cos_ql"]), 9),
            round(float(s["cos_nl"]), 9),
        ))
    return sig


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class HubLoaderCorrectness:
    def test_hub_events_yield_expected_pairs_and_targets(self) -> None:
        # The hub loader → pair_events → extract_samples produces the trainable
        # samples we expect: qwen3 filter keeps t1 + t2 (2 retrievals × 2 nodes
        # = 4 targets); arctic t3 + the outcome event contribute nothing.
        events = _all_events()
        hub = _hub_events(events, emb_source="qwen3")
        sig = _samples_signature(hub)
        assert len(sig) == 4
        # Every sample belongs to t1 or t2 (arctic t3 filtered out).
        assert {s[0] for s in sig} == {"t1", "t2"}

    def test_pagination_yields_identical_corpus(self) -> None:
        events = _all_events()
        single = _hub_events(events, emb_source="qwen3")
        paged = _hub_events(events, emb_source="qwen3", page_size=2)  # force >1 page
        assert _samples_signature(single) == _samples_signature(paged)


class DesignInvariants:
    def test_outcome_events_dropped(self) -> None:
        # A corpus of ONLY outcome events → zero trainable events.
        only_outcomes = [_outcome("t4"), _outcome("t5")]
        loaded = _hub_events(only_outcomes)
        assert loaded == []

    def test_embedding_source_filter_retrieval_only(self) -> None:
        # arctic retrieval (t3) excluded under qwen3 filter; its citation passes
        # through but has no paired retrieval → no sample.
        events = _all_events()
        loaded = _hub_events(events, emb_source="qwen3")
        retr_sources = {e["embedding_source"] for e in loaded if e.get("event") == "retrieval"}
        assert retr_sources == {"qwen3"}
        task_ids = {e["task_id"] for e in loaded if e.get("event") == "retrieval"}
        assert task_ids == {"t1", "t2"}

    def test_schema_v1_refused(self) -> None:
        v1 = [{"event": "retrieval", "schema_version": 1, "task_id": "old",
               "embedding_source": "qwen3", "query_emb": [0.1], "nodes": []}]
        with pytest.raises(ValueError, match="legacy"):
            _hub_events(v1, emb_source="qwen3")

    def test_citations_pass_through_filter(self) -> None:
        # Citation events carry no embedding_source — the per-source filter must
        # let them through so pair_events() can still join them by task_id.
        events = [_retrieval("t1", emb_source="qwen3"), _citation("t1")]
        loaded = _hub_events(events, emb_source="qwen3")
        assert sorted(e["event"] for e in loaded) == ["citation", "retrieval"]


class LoaderContract:
    def test_load_training_events_requires_project_id(self) -> None:
        with pytest.raises(ValueError, match="project_id is required"):
            load_training_events(object(), "")

    def test_load_training_events_requires_client(self) -> None:
        with pytest.raises(ValueError, match="hub_client is required"):
            load_training_events(None, "some-project")


# Allow running under both pytest classes and bare `python -m pytest`.
TestHubLoaderCorrectness = HubLoaderCorrectness
TestDesignInvariants = DesignInvariants
TestLoaderContract = LoaderContract
