# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.75 NEW-2 — retention must not be inert for opted-out users.

The RL-5 retention driver (v0.2.73) ran ONLY on the writer's hub-write cadence
— inside the ``not _local_logging_disabled()`` branch of ``log_retrieval``.
Two consequences for an opted-out user:

  1. Logging disabled but writer still reached (upload consent on) → the
     prune never ran even though rows pre-dating the opt-out still existed.
  2. Fully opted out (no consumer at all) → ``rerank_and_emit`` skips the
     writer entirely (Concern-A), so the prune driver had NO trigger and the
     user's pre-existing rl_events rows grew immortal.

v0.2.75: prune is consumer-independent HOUSEKEEPING ("stop recording me" is
not "keep my old rows forever"):

  * ``log_retrieval`` drives the throttled prune in BOTH branches.
  * ``rerank_and_emit``'s no-consumer skip branch drives the same single-home
    ``maybe_run_retention`` (hourly throttle + 6-h in-flight floor identical).
  * rl-doctor grows ``--prune`` — the explicit, throttle-bypassing trigger
    (the tool's single mutating exception).

Rejected alternative (recorded in the plan): a hub-side cron — the hub stays
a resolver, it does not own schedules.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import rl_retention  # noqa: E402
from claude_mcp_servers.rl_client import search_pipeline  # noqa: E402
from claude_mcp_servers.rl_client import telemetry_writer  # noqa: E402
from claude_mcp_servers.rl_client import hub_writer  # noqa: E402
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_retention_state(monkeypatch):
    """Reset the per-process throttle + neutralise machine-local env/consent."""
    rl_retention._reset_throttle_for_test()
    for k in (
        "RL_LOCAL_LOGGING_DISABLED",
        "RL_LOCAL_LOGGING_DISABLED_GLOBAL",
        "RL_ONLINE_TRAINING_DISABLED",
        "RL_ONLINE_TRAINING_DISABLED_GLOBAL",
        "RL_EVENTS_RETENTION_MAX_AGE_DAYS",
        "RL_EVENTS_RETENTION_MAX_ROWS",
        "RL_EVENTS_RETENTION_DISABLED",
        "RL_EVENTS_RETENTION_MIN_INTERVAL_S",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(telemetry_writer, "_upload_consent_granted", lambda: False)
    yield
    rl_retention._reset_throttle_for_test()


def _writer() -> RLTelemetryWriter:
    return RLTelemetryWriter(
        project="sample-project",
        project_id="00000000-0000-0000-0000-000000000000",
        embedding_source="qwen3",
        embedding_dim=8,
        embedding_model="sample-embed",
        hub_post_fn=lambda envelope, timeout=2.0: True,
    )


def _log_one(w: RLTelemetryWriter) -> None:
    w.log_retrieval(
        task_id="task-retention",
        task_type="mcp_interactive",
        query="q",
        nodes=[{"title": "A", "score": 0.5}],
    )


# ── writer path drives the prune in BOTH branches ───────────────────────


def test_logging_disabled_writer_path_still_prunes(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    calls: list = []
    monkeypatch.setattr(
        rl_retention, "maybe_run_retention",
        lambda **kw: calls.append(kw) or {"ran": True},
    )
    _log_one(_writer())
    assert len(calls) == 1, "prune must fire on the logging-DISABLED branch too"
    assert calls[0]["project_id"] == "00000000-0000-0000-0000-000000000000"


def test_logging_enabled_writer_path_still_prunes(monkeypatch):
    # Leave-alone: the pre-NEW-2 behaviour is preserved on the enabled branch.
    calls: list = []
    monkeypatch.setattr(
        rl_retention, "maybe_run_retention",
        lambda **kw: calls.append(kw) or {"ran": True},
    )
    _log_one(_writer())
    assert len(calls) == 1


# ── fully-opted-out skip branch in rerank_and_emit drives retention ─────


def _fully_opted_out_request():
    return search_pipeline.RerankRequest(
        query="q",
        candidates=[{"title": "A", "score": 0.5}],
        limit=1,
        embedding_source="qwen3",
        embedding_dim=8,
        embedding_model="sample-embed",
        task_id="task-optout-retention",
        spawn_answer_monitor=False,
    )


def test_opted_out_skip_branch_drives_retention(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    calls: list = []
    monkeypatch.setattr(
        rl_retention, "maybe_run_retention",
        lambda **kw: calls.append(kw) or {"ran": True},
    )
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
         patch("claude_mcp_servers.weaviate_mcp.server._try_resolve_project_config",
               return_value=None):
        result = _run(search_pipeline.rerank_and_emit(_fully_opted_out_request()))
    assert result.emit_success is False, "sanity: the no-consumer branch ran"
    assert len(calls) == 1, "the skip branch must drive the retention pass"


# ── the 6-h in-flight clamp is identical through both drivers ───────────


def _clamp_capture(monkeypatch) -> list:
    """Force the clamp to bind (protect window > configured age bound) and
    capture what cutoff the hub prune route would receive."""
    monkeypatch.setenv("RL_EVENTS_RETENTION_MAX_AGE_DAYS", "90")
    # Protect window of 100 days (seconds) — the 90-day cutoff is more recent
    # than the floor, so the plan must clamp it down to now - 100d.
    monkeypatch.setattr(rl_retention, "_INFLIGHT_PROTECT_SECONDS", 100 * 86400.0)
    captured: list = []

    def _fake_prune(*, cutoff_ms, max_rows, project_id):
        captured.append({"cutoff_ms": cutoff_ms, "max_rows": max_rows})
        return {"deleted": 0}

    monkeypatch.setattr(hub_writer, "post_rl_prune", _fake_prune)
    return captured


def _assert_clamped(captured, t0: float, t1: float) -> None:
    assert len(captured) == 1
    cutoff = captured[0]["cutoff_ms"]
    lo = int((t0 - 100 * 86400.0) * 1000)
    hi = int((t1 - 100 * 86400.0) * 1000)
    assert lo <= cutoff <= hi, (
        f"cutoff {cutoff} must sit on the clamped 100-day floor, not the "
        f"90-day age bound"
    )


def test_inflight_clamp_respected_on_writer_branch(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    captured = _clamp_capture(monkeypatch)
    t0 = time.time()
    _log_one(_writer())
    _assert_clamped(captured, t0, time.time())


def test_inflight_clamp_respected_on_skip_branch(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    captured = _clamp_capture(monkeypatch)
    t0 = time.time()
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
         patch("claude_mcp_servers.weaviate_mcp.server._try_resolve_project_config",
               return_value=None):
        _run(search_pipeline.rerank_and_emit(_fully_opted_out_request()))
    _assert_clamped(captured, t0, time.time())


# ── integration: an opted-out user's pre-existing rows age out ──────────


def test_opted_out_preexisting_rows_age_out(monkeypatch):
    """End-to-end through the Python side: a fully-opted-out user's search
    drives the prune, and their pre-opt-out rows older than the age bound are
    deleted while recent rows survive (leave-alone)."""
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    monkeypatch.setenv("RL_EVENTS_RETENTION_MAX_AGE_DAYS", "30")

    now_ms = int(time.time() * 1000)
    store = [
        {"task_id": "ancient", "ts": now_ms - 40 * 86_400_000},   # pre-opt-out
        {"task_id": "recent", "ts": now_ms - 3_600_000},          # 1h old
    ]

    def _fake_prune(*, cutoff_ms, max_rows, project_id):
        before = len(store)
        if cutoff_ms is not None:
            store[:] = [r for r in store if r["ts"] >= cutoff_ms]
        return {"deleted": before - len(store)}

    monkeypatch.setattr(hub_writer, "post_rl_prune", _fake_prune)

    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
         patch("claude_mcp_servers.weaviate_mcp.server._try_resolve_project_config",
               return_value=None):
        _run(search_pipeline.rerank_and_emit(_fully_opted_out_request()))

    assert [r["task_id"] for r in store] == ["recent"], (
        "pre-existing rows past the age bound must age out; recent rows stay"
    )


def test_throttle_still_applies_across_opted_out_searches(monkeypatch):
    monkeypatch.setenv("RL_LOCAL_LOGGING_DISABLED", "true")
    calls: list = []
    monkeypatch.setattr(hub_writer, "post_rl_prune",
                        lambda **kw: calls.append(kw) or {"deleted": 0})
    with patch.object(search_pipeline, "_resolve_rl_enabled", return_value=False), \
         patch("claude_mcp_servers.weaviate_mcp.server._try_resolve_project_config",
               return_value=None):
        _run(search_pipeline.rerank_and_emit(_fully_opted_out_request()))
        _run(search_pipeline.rerank_and_emit(_fully_opted_out_request()))
    assert len(calls) == 1, "second search inside the hourly window must be throttled"


# ── rl-doctor --prune: explicit, throttle-bypassing trigger ─────────────


def test_rl_doctor_prune_flag_forces_a_pass(monkeypatch, capsys, tmp_path):
    import importlib

    rl_doctor = importlib.import_module("rl_doctor") if "rl_doctor" in sys.modules else None
    if rl_doctor is None:
        scripts_dir = str(PROJECT_ROOT / "claude_mcp_servers" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        rl_doctor = importlib.import_module("rl_doctor")

    calls: list = []
    monkeypatch.setattr(
        rl_retention, "maybe_run_retention",
        lambda **kw: calls.append(kw) or {
            "ran": True, "skipped": None, "deleted": 7, "reason": "age>90d",
        },
    )
    rc = rl_doctor.main(["--json", "--prune", "--project-root", str(tmp_path)])
    out = capsys.readouterr().out
    report = json.loads(out)
    assert calls and calls[0].get("force") is True, "--prune must bypass the throttle"
    assert report["prune_run"]["deleted"] == 7
    assert rc in (0, 1)  # health verdict is machine-dependent; flag must not crash


def test_rl_doctor_without_prune_flag_never_prunes(monkeypatch, capsys, tmp_path):
    import importlib

    scripts_dir = str(PROJECT_ROOT / "claude_mcp_servers" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    rl_doctor = importlib.import_module("rl_doctor")

    calls: list = []
    monkeypatch.setattr(
        rl_retention, "maybe_run_retention",
        lambda **kw: calls.append(kw) or {"ran": True},
    )
    rl_doctor.main(["--json", "--project-root", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert calls == [], "read-only default must not mutate"
    assert "prune_run" not in report
