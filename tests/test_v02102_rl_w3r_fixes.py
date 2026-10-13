# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W4-A1 — W3-R audit fixes for the RL training-log pipeline.

Covers (audit: V02102-RL-AUDIT-2026-10):

* W3R-1 — a force-flush (sub-gate) monitor fire DELETED the citation pending
  file before the token gate and skipped with a DEBUG line: the citation was
  permanently lost with no drain recovery. The pending file now survives
  unless the citation write actually landed.
* W3R-2 — NB-06 hub-down coalescing was per-PROCESS, so hook/CLI producers
  (one process per search) wrote one ledger line per search, and ``timeout``
  was never coalesced. Episode state is now a cross-process file; repeats
  bump the episode line's ``suppressed`` count in place.
* W3R-5 — writer-unavailable drops (emit_rl_event factory-raise / None,
  deferred_emit._serialize drop, hand_off inline discard) left no ledger
  line. All now record ``writer_unavailable`` / ``deferred_unsent`` without
  double-counting.
* W3R-6a — a zero-result hook-KG search left NO retrieval event ("no event"
  indistinguishable from "no search"). A degraded ``failure_mode="no_results"``
  event now goes through the same consumer gate + defer machinery.
* W3R-8 — outcome events were stamped ``schema_version=3`` from a mirrored
  constant while ``RLDataLogger.SCHEMA_VERSION`` is 4; now imported, one home.
* W3R-9 — a transient hub-config miss stamped the env display name with
  project_id=NULL for the rest of the process (live split-brain cohorts:
  display casing beside slug, one project). The last RESOLVED identity
  (in-process, then a cross-process cache keyed by project root) is reused;
  the env fallback now warns once.
* W3R-11 — a SIGKILLed deferred-emit child left its payload (and events)
  orphaned with no ledger line; the next ``hand_off`` reaps stale payloads.

W3R-7's dedupe is pinned in Rust (``rl_events.rs`` /
``rl_events_api.rs`` tests); W3R-12 is record-only (no defect).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from claude_mcp_servers.rl_client import deferred_emit as de
from claude_mcp_servers.rl_client import hub_writer, outcome_emit, telemetry_emit
from claude_mcp_servers.rl_client.rl_logger import RLDataLogger
from claude_mcp_servers.weaviate_mcp import rl_enrichment
from claude_mcp_servers.weaviate_mcp import server as srv
from vco_lib import rl_telemetry_loss

REPO_ROOT = Path(__file__).resolve().parent.parent

EV = {
    "event_type": "retrieval",
    "task_id": "W3R-T1",
    "task_type": "pre_edit_kg_search",
    "embedding_source": "qwen3",
    "payload_json": "{}",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _loss_lines():
    p = rl_telemetry_loss.loss_log_path()
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def _clean_state():
    hub_writer._reset_hub_down_bookkeeping_for_test()
    rl_telemetry_loss._reset_warned_for_test()
    rl_enrichment._reset_rl_telemetry_writers()
    p = rl_telemetry_loss.loss_log_path()
    if p.exists():
        p.unlink()
    # Absent on pre-fix sources (the W3R-9 identity cache is new); tolerant so
    # red-proof runs fail on ASSERTIONS, not on fixture AttributeErrors.
    idc_path = None
    idc_fn = getattr(rl_enrichment, "_identity_cache_path", None)
    if idc_fn is not None:
        idc_path = idc_fn()
        if idc_path.exists():
            idc_path.unlink()
    yield
    hub_writer._reset_hub_down_bookkeeping_for_test()
    rl_enrichment._reset_rl_telemetry_writers()
    if p.exists():
        p.unlink()
    if idc_path is not None and idc_path.exists():
        idc_path.unlink()


def _event(task_id: str = "w3r-ev") -> telemetry_emit.RetrievalEvent:
    return telemetry_emit.RetrievalEvent(
        query="q",
        query_emb=[0.1, 0.2],
        embedding_source="qwen3",
        embedding_dim=2,
        embedding_model="m",
        nodes=[{"title": "n"}],
        task_id=task_id,
        task_type="pre_edit_kg_search",
    )


# ---------------------------------------------------------------------------
# W3R-8 — outcome schema_version tracks the one home
# ---------------------------------------------------------------------------


def test_outcome_schema_version_matches_rl_logger():
    """W3R-8: outcome events must carry the SAME schema version as every
    other RL event — a mirrored constant drifted to 3 while the home moved
    to 4, which any reader-side ``schema_version >= 4`` gate would drop."""
    assert outcome_emit._schema_version() == RLDataLogger.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# W3R-5 — writer-unavailable drops are ledgered (never silently)
# ---------------------------------------------------------------------------


def test_writer_factory_raise_is_ledgered():
    def _boom():
        raise RuntimeError("broken install")

    assert telemetry_emit.emit_rl_event(_event("w5-raise"), writer_factory=_boom) is False
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("writer_unavailable", "writer_factory_raised")
    assert line["task_id"] == "w5-raise"


def test_writer_factory_none_is_ledgered():
    assert (
        telemetry_emit.emit_rl_event(_event("w5-none"), writer_factory=lambda: None) is False
    )
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("writer_unavailable", "writer_unavailable")
    assert line["task_id"] == "w5-none"


def test_serialize_drop_is_ledgered(monkeypatch):
    """W3R-5(b): an item whose writer params cannot resolve is dropped from
    the child's batch — the drop must leave a ledger line (the child never
    sees the event, so nothing else would name it)."""
    monkeypatch.setattr(telemetry_emit, "_default_writer_factory", lambda: None)
    payload = de._serialize([de.DeferredEmit(_event("w5-ser"))])
    assert json.loads(payload)["items"] == []
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("writer_unavailable", "serialize_drop")
    assert line["task_id"] == "w5-ser"


def test_hand_off_inline_records_unsent(monkeypatch):
    """W3R-5(c): the inline fallback used to DISCARD per-item results — an
    event the emitter never sent left no line on this path."""
    monkeypatch.setenv(de.INLINE_ENV, "1")
    item = de.DeferredEmit(
        _event("w5-inline"),
        writer_params={"project": "p", "project_id": "pid", "embedding_source": "qwen3",
                       "embedding_dim": 2, "embedding_model": "m"},
    )
    result = de.hand_off([item], emit=lambda ev, writer_factory=None: False)
    assert result == "inline"
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("deferred_unsent", "deferred_emit_not_sent")
    assert line["task_ids"] == "w5-inline"


def test_hand_off_inline_does_not_double_count_writer_unavailable(monkeypatch):
    """W3R-5 invariant: an event whose emit failed for an UNAVAILABLE writer
    is ledgered by emit_rl_event (``writer_unavailable``) — the batch line
    must not count it again."""
    monkeypatch.setenv(de.INLINE_ENV, "1")
    monkeypatch.setattr(telemetry_emit, "_default_writer_factory", lambda: None)
    result = de.hand_off([de.DeferredEmit(_event("w5-dbl"))])
    assert result == "inline"
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [
        ("writer_unavailable", "writer_unavailable")
    ], "one event, one line — never both kinds"


# ---------------------------------------------------------------------------
# W3R-11 — orphaned deferred-emit payloads are reaped + ledgered
# ---------------------------------------------------------------------------


def _orphan_payload(dirpath: Path, name: str, items: int, age_s: float) -> Path:
    p = dirpath / name
    payload = {
        "items": [
            {
                "event": {
                    "query": "q", "query_emb": [0.1], "embedding_source": "qwen3",
                    "embedding_dim": 1, "embedding_model": "m", "nodes": [],
                    "task_id": f"orphan-{i}", "task_type": "pre_edit_kg_search",
                },
                "writer": {},
            }
            for i in range(items)
        ]
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - age_s
    os.utime(p, (old, old))
    return p


def test_orphaned_payload_reaped_and_ledgered(tmp_path):
    stale = _orphan_payload(tmp_path, "vco-rl-emit-dead.json", items=2, age_s=7200)
    fresh = _orphan_payload(tmp_path, "vco-rl-emit-live.json", items=1, age_s=5)
    reaped = de._reap_orphaned_payloads(tmp_dir=tmp_path)
    assert reaped == 1
    assert not stale.exists(), "the stale payload is deleted"
    assert fresh.exists(), "a young payload (its child may be mid-send) is untouched"
    (line,) = _loss_lines()
    assert (line["kind"], line["reason"]) == ("deferred_unsent", "orphaned_payload")
    assert line["unsent"] == 2


def test_hand_off_reaps_orphans_on_the_next_call(tmp_path, monkeypatch):
    """Wiring: the reaper runs from hand_off itself — a SIGKILLed child's
    payload is caught by the NEXT hand-off, not by a separate cron."""
    monkeypatch.setenv(de.INLINE_ENV, "1")
    calls = []
    monkeypatch.setattr(de, "_reap_orphaned_payloads", lambda: calls.append(1) or 0)
    de.hand_off([de.DeferredEmit(_event("w5-reap"))], emit=lambda ev, writer_factory=None: True)
    assert calls == [1], "hand_off must run the orphan reaper"


# ---------------------------------------------------------------------------
# W3R-2 — hub-down episode coalescing is cross-process
# ---------------------------------------------------------------------------


@pytest.fixture
def hub_down(monkeypatch):
    """The real poster, hermetic: test-posts allowed, port 9 (nothing
    listens), no token file under the redirected VCT_STATE_DIR."""
    monkeypatch.setenv("VCT_HUB_ALLOW_TEST_POST", "1")
    monkeypatch.setattr(hub_writer, "_read_hub_port", lambda: 9)
    monkeypatch.setattr(hub_writer, "_RETRY_PAUSE_S", 0.0)
    hub_writer._reset_hub_down_bookkeeping_for_test()
    return hub_writer


def test_timeout_lines_are_coalesced_too(hub_down, monkeypatch):
    """W3R-2: the dominant live failure (``timeout``) was NEVER coalesced —
    only ``hub_not_running`` was. All hub-unreachable reasons now share the
    episode."""
    monkeypatch.setattr(hub_down, "_read_hub_token", lambda: "tok")

    def _timeout(req, timeout=None):
        raise TimeoutError("hub wedged")

    monkeypatch.setattr(hub_down.urllib.request, "urlopen", _timeout)
    for _ in range(3):
        assert hub_down.post_rl_event(dict(EV)) is False
    lines = _loss_lines()
    assert [(x["kind"], x["reason"]) for x in lines] == [("hub_post_failed", "timeout")]
    assert lines[0]["suppressed"] == 2, "repeats bump the ONE episode line in place"


def test_episode_survives_across_processes(tmp_path):
    """W3R-2 red-proof shape: two SEQUENTIAL PROCESSES posting with the hub
    down against one VCT_STATE_DIR must yield ONE ledger line whose
    suppressed count is 1 (pre-fix: one line per process, no suppression)."""
    from tests.common.child_env import child_env

    state = tmp_path / "vct-state"
    child_src = (
        "from claude_mcp_servers.rl_client.hub_writer import post_rl_event;"
        "post_rl_event({'event_type': 'retrieval', 'task_id': 'xp',"
        " 'task_type': 'cli_kg_search', 'payload_json': '{}'})"
    )
    # child_env pins the checkout's import roots (a site-packages shadow would
    # measure the wrong tree); the overrides isolate the state dir, allow the
    # test-context post through the WP-R chokepoint, and pin the unroutable
    # Weaviate sentinel so a stray import can never reach a live backend.
    env = child_env(
        VCT_STATE_DIR=str(state),
        VCT_HUB_ALLOW_TEST_POST="1",
        WEAVIATE_URL="http://127.0.0.1:9",
    )
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, "-c", child_src],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr[-500:]
    ledger = state / "metrics" / "rl_telemetry_loss.jsonl"
    lines = [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]
    assert [(x["kind"], x["reason"]) for x in lines] == [
        ("hub_post_failed", "hub_not_running")
    ], "two hub-down PROCESSES must coalesce to ONE ledger line"
    assert lines[0].get("suppressed") == 1, "the second process's loss rides on the one line"


# ---------------------------------------------------------------------------
# W3R-9 — writer project identity is stable across hub-config misses
# ---------------------------------------------------------------------------


def _fake_cfg(slug: str, project_id: str):
    return types.SimpleNamespace(
        project_slug=slug,
        code_graph_project="",
        project_display_name="",
        project_id=project_id,
    )


def test_resolve_miss_reuses_last_resolved_identity_in_process(tmp_path, monkeypatch):
    """W3R-9: after one successful resolve, a transient miss must stamp the
    SAME identity (an MCP process serves exactly one project) — not the env
    display name with project_id=NULL."""
    # The identity machinery honours the resolver's own hermeticity flag
    # (conftest sets it session-wide), so opt back in for this test.
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_NAME", "Env Display Name")  # the env fallback, distinguishable
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: _fake_cfg("resolved-project", "pid-1"))
    w1 = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert (w1._project, w1._project_id) == ("resolved-project", "pid-1")

    # Hub config now misses (transient). A DIFFERENT slot forces a cache miss
    # so the identity resolution runs again.
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    w2 = rl_enrichment._get_rl_telemetry_writer_for("arctic", embedding_dim=8, embedding_model="m")
    assert (w2._project, w2._project_id) == ("resolved-project", "pid-1"), (
        "a transient resolve miss must never stamp a different identity"
    )


def test_resolve_miss_reuses_cross_process_identity_cache(tmp_path, monkeypatch):
    """W3R-9: the remembered identity is on disk keyed by project root, so a
    process whose FIRST resolve misses (hub down at startup) still stamps the
    identity a resolved process on this root stamped earlier."""
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_NAME", "Env Display Name")
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: _fake_cfg("resolved-project", "pid-9"))
    rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")

    # Simulate a NEW process: in-process memory gone, resolve still failing.
    rl_enrichment._reset_rl_telemetry_writers()
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    w = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert (w._project, w._project_id) == ("resolved-project", "pid-9")


def test_env_fallback_warns_once_when_no_identity_is_known(tmp_path, monkeypatch, caplog):
    """W3R-9: the env display-name fallback stamps project_id=NULL — that
    must be a visible WARNING, not a silent identity change."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_NAME", "Env Display Name")
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    import logging

    with caplog.at_level(logging.WARNING):
        w = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert w._project_id is None
    assert any("project_id=NULL" in r.message for r in caplog.records), (
        "falling back to a NULL-project_id writer must warn"
    )


def test_last_good_identity_is_keyed_by_project_root(tmp_path, monkeypatch):
    """Batch-8 follow-up 1: the in-process last-good memory is keyed by
    project root — a transient hub-config miss on root B must never stamp
    root A's remembered identity."""
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setenv("PROJECT_NAME", "Env Display Name")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root_a))
    monkeypatch.setattr(
        srv, "_try_resolve_project_config", lambda: _fake_cfg("resolved-a", "pid-a")
    )
    wa = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert (wa._project, wa._project_id) == ("resolved-a", "pid-a")

    # Root B, transient miss: B has no remembered identity, so the writer
    # must fall back to B's OWN sources (the env fallback), never A's.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root_b))
    monkeypatch.setattr(srv, "_try_resolve_project_config", lambda: None)
    wb = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert wb._project_id is None, "root B must not inherit root A's project_id"
    assert wb._project != "resolved-a", "root B must not inherit root A's identity"


def test_stale_cached_identity_dropped_and_event_lands_on_422(tmp_path, monkeypatch):
    """Batch-8 follow-up 2: when the hub rejects an event 422
    ``unknown_project_id`` (the rl_events FK fired — the project row behind
    a cached identity is gone), the cached identity is dropped from ALL
    three caches and the event is retried once with project_id=None so it
    lands instead of being lost for the rest of the cache TTL."""
    import io
    import urllib.error

    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("PROJECT_NAME", "Env Display Name")

    # 1. Resolve once so the identity is cached (in-process + on disk) and a
    #    writer is built from it.
    monkeypatch.setattr(
        srv, "_try_resolve_project_config", lambda: _fake_cfg("cached-proj", "stale-id")
    )
    w1 = rl_enrichment._get_rl_telemetry_writer_for("qwen3", embedding_dim=8, embedding_model="m")
    assert w1._project_id == "stale-id"
    assert ("cached-proj", "qwen3") in srv._rl_telemetry_writers

    # 2. The hub rejects the stale id (FK fired) and accepts the NULL retry.
    monkeypatch.setenv("VCT_HUB_ALLOW_TEST_POST", "1")
    monkeypatch.setattr(hub_writer, "_read_hub_token", lambda: "tok")
    monkeypatch.setattr(hub_writer, "_read_hub_port", lambda: 9)
    seen_bodies: list = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        seen_bodies.append(body)
        if body.get("project_id") == "stale-id":
            raise urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable Content", {},
                io.BytesIO(b'{"error":{"code":"unknown_project_id","message":"x"}}'),
            )
        return _Resp()

    monkeypatch.setattr(hub_writer.urllib.request, "urlopen", _urlopen)

    ok = hub_writer.post_rl_event({
        "event_type": "retrieval", "task_id": "w9-stale",
        "task_type": "mcp_interactive", "project_id": "stale-id",
        "project_name": "cached-proj", "payload_json": "{}",
    })
    assert ok is True, "the retry with project_id=None must land"
    assert seen_bodies[0]["project_id"] == "stale-id"
    assert seen_bodies[1]["project_id"] is None

    # 3. The stale identity is gone from all three caches, so the NEXT writer
    #    resolution re-resolves from the hub instead of reusing it.
    assert all(
        ident.get("project_id") != "stale-id"
        for ident in rl_enrichment._LAST_GOOD_BY_ROOT.values()
    ), "in-process last-good must drop the rejected identity"
    assert ("cached-proj", "qwen3") not in srv._rl_telemetry_writers, (
        "the writer built from the stale identity must be evicted"
    )
    cache_file = rl_enrichment._identity_cache_path()
    if cache_file.exists():
        roots = json.loads(cache_file.read_text()).get("roots", {})
        assert all(e.get("project_id") != "stale-id" for e in roots.values()), (
            "the on-disk identity cache must drop the rejected identity"
        )


# ---------------------------------------------------------------------------
# W3R-6a — a zero-result hook-KG search emits a degraded retrieval event
# ---------------------------------------------------------------------------


def _args(**over):
    base = types.SimpleNamespace(query="probe query", limit=3, hook_format=False)
    for k, v in over.items():
        setattr(base, k, v)
    return base


def test_no_results_event_emitted_inline_on_cli_path(monkeypatch):
    from claude_mcp_servers.scripts import rl_kg_search

    sent = []
    monkeypatch.setattr(telemetry_emit, "emit_rl_event", lambda ev: sent.append(ev) or True)
    out = rl_kg_search.stage_no_results_event(
        _args(), "pre_edit_kg_search", [0.1, 0.2], (),
        embedding_source="qwen3", embedding_model="m", embedding_dim=2,
    )
    assert out == ()
    (ev,) = sent
    assert ev.failure_mode == "no_results"
    assert ev.nodes == []
    assert ev.task_type == "pre_edit_kg_search"
    assert ev.task_id.startswith("pre_edit_")


def test_no_results_event_deferred_on_hook_path(monkeypatch):
    from claude_mcp_servers.scripts import rl_kg_search

    out = rl_kg_search.stage_no_results_event(
        _args(hook_format=True), "pre_bash_kg_search", None, (),
        embedding_source="qwen3", embedding_model="m", embedding_dim=2,
    )
    assert len(out) == 1
    ev = out[0].event
    assert ev.failure_mode == "no_results" and ev.nodes == []
    assert ev.query_emb is None, "a failed embed stays distinguishable from a zero-length one"


def test_no_results_event_respects_the_consumer_gate(monkeypatch):
    """Suppress arm: local logging off AND no upload consent ⇒ no event,
    exactly as for a populated search."""
    from claude_mcp_servers.rl_client import search_pipeline
    from claude_mcp_servers.scripts import rl_kg_search

    monkeypatch.setattr(search_pipeline, "_retrieval_emit_has_consumer", lambda: False)
    sent = []
    monkeypatch.setattr(telemetry_emit, "emit_rl_event", lambda ev: sent.append(ev) or True)
    out = rl_kg_search.stage_no_results_event(
        _args(), "pre_edit_kg_search", [0.1], (),
        embedding_source="qwen3", embedding_model="m", embedding_dim=2,
    )
    assert out == () and sent == []


def test_main_emits_no_results_event_on_empty_search(monkeypatch):
    """Wiring proof (not a source scan): drive main() to the empty-results
    branch and assert the degraded event actually leaves the process via the
    deferred hand-off."""
    from claude_mcp_servers.rl_client import search_pipeline
    from claude_mcp_servers.scripts import rl_kg_search

    # rl_kg_search imports the MCP server as TOP-LEVEL ``weaviate_mcp.server``
    # (it puts claude_mcp_servers/ on sys.path at import), a different module
    # object than ``claude_mcp_servers.weaviate_mcp.server`` — patch the view
    # the script actually uses.
    import weaviate_mcp.server as script_srv

    class _Client:
        def close(self):
            pass

    monkeypatch.setattr(script_srv, "get_weaviate_client", lambda: _Client())
    monkeypatch.setattr(script_srv, "_kg_collections_to_search", lambda include_dev=False: [])

    async def _no_embed(text):
        return None, ""

    monkeypatch.setattr(rl_kg_search, "embed_hook_query", _no_embed)
    monkeypatch.setenv(de.INLINE_ENV, "1")
    sent = []
    monkeypatch.setattr(
        search_pipeline, "emit_rl_event", lambda ev, writer_factory=None: sent.append(ev) or True
    )
    monkeypatch.setattr(
        sys, "argv",
        ["rl_kg_search", "probe query", "--hook-format", "--task-type", "pre_edit_kg_search"],
    )
    asyncio.run(rl_kg_search.main())
    assert len(sent) == 1, "an empty hook-KG search must emit exactly one degraded event"
    assert sent[0].failure_mode == "no_results" and sent[0].nodes == []


# ---------------------------------------------------------------------------
# W3R-1 — sub-gate monitor fire keeps the pending file for the drain
# ---------------------------------------------------------------------------


def _kg_messages(query: str, answer: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "hybrid_search", "input": {"query": query}},
                    {"type": "text", "text": answer},
                ]
            },
        },
    ]


def _drive_monitor(monkeypatch, tmp_path, *, answer: str, gate_tokens: int, write_result):
    """Drive one _rl_answer_monitor fire. Returns (pending_path, compute_calls)."""
    from claude_mcp_servers.rl_client import citation_pending

    task_id = "w3r1task"
    query = "w3r1 probe query"
    staged = citation_pending.stage_pending(
        session_id="s1", task_id=task_id, seq=1, query=query,
        ctx={"nodes": [{"title": "n"}], "query_emb": [0.1]}, source="mcp",
        project_root=tmp_path,
    )
    assert staged is not None and staged.exists()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    monkeypatch.setattr(srv, "_RL_MONITOR_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(srv, "_RL_MONITOR_TIMEOUT", 30.0)
    monkeypatch.setattr(srv, "_RL_MIN_ANSWER_TOKENS_FOR_CITATION", gate_tokens)
    monkeypatch.setattr(srv, "_rl_check_force_flush", lambda: True)
    cleared = []
    monkeypatch.setattr(srv, "_rl_clear_force_flush", lambda: cleared.append(1))

    async def _transcripts():
        return [Path("fake.jsonl")]

    monkeypatch.setattr(srv, "_rl_find_all_transcripts", _transcripts)
    monkeypatch.setattr(srv, "_rl_load_messages", lambda c: _kg_messages(query, answer))
    srv._rl_node_content_cache[task_id] = {"nodes": [{"title": "n"}], "query_emb": [0.1]}

    compute_calls = []

    async def _compute(tid, ans, ctx):
        compute_calls.append(tid)
        return write_result

    monkeypatch.setattr(srv, "_rl_compute_and_write_citations", _compute)
    monkeypatch.setattr(srv, "_get_rl_client", lambda: None)

    try:
        asyncio.run(rl_enrichment._rl_answer_monitor(task_id, 1, query))
    finally:
        srv._rl_node_content_cache.pop(task_id, None)
    return staged, compute_calls


def test_sub_gate_force_flush_keeps_pending_file(tmp_path, monkeypatch):
    """W3R-1: a force-flush fire below the token gate must NOT delete the
    pending file — the Stop-hook drain recovers the citation at session end
    (terminal_floor / soft_terminal). Pre-fix the file was deleted BEFORE the
    gate and the skip was a DEBUG line: permanent silent loss."""
    staged, compute_calls = _drive_monitor(
        monkeypatch, tmp_path, answer="a short answer", gate_tokens=25_000,
        write_result=None,
    )
    assert compute_calls == [], "a sub-gate window is never computed"
    assert staged.exists(), "the pending file must survive a sub-gate fire"


def test_landed_write_deletes_pending_file(tmp_path, monkeypatch):
    """F-QUEUE intact: once the citation write LANDS the pending file is
    deleted, so the drain never double-writes it."""
    staged, compute_calls = _drive_monitor(
        monkeypatch, tmp_path, answer="an answer long enough for the lowered test gate",
        gate_tokens=1, write_result={"cosine_sims": {}, "literal_cited": {}},
    )
    assert compute_calls == ["w3r1task"]
    assert not staged.exists(), "a landed write still deletes its own pending file"


def test_failed_write_keeps_pending_file(tmp_path, monkeypatch):
    """W3R-1 companion: a citation write that soft-failed (None) keeps the
    pending file so the drain retries at session end."""
    staged, compute_calls = _drive_monitor(
        monkeypatch, tmp_path, answer="an answer long enough for the lowered test gate",
        gate_tokens=1, write_result=None,
    )
    assert compute_calls == ["w3r1task"]
    assert staged.exists(), "a failed write must leave the pending file for the drain"
