# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 RL-5 — bounded retention / GC for the ``rl_events`` store.

The ``rl_events`` launcher.db table is append-only (``insert_rl_event`` never
deletes). This suite covers the Python-side retention DRIVER
(``rl_client.rl_retention``) that decides when/what to prune and drives the
hub prune route (``hub_writer.post_rl_prune``):

  * age + row-count cutoff resolution from env, with sane defaults;
  * the in-flight-citation protection floor (the cutoff is never more recent
    than now - 6h, so a too-aggressive age setting can't orphan a pending
    citation);
  * cadence throttle (≤1 pass/interval per process);
  * opt-out env;
  * graceful degrade when the hub prune route is absent (older hub binary);
  * soft-fail: a raising prune fn never propagates;
  * the writer path drives retention after a successful retrieval write.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.rl_client import rl_retention
from claude_mcp_servers.rl_client.telemetry_writer import RLTelemetryWriter

_NOW_MS = 1_700_000_000_000
_DAY_MS = 86_400_000


@pytest.fixture(autouse=True)
def _reset_throttle():
    rl_retention._reset_throttle_for_test()
    yield
    rl_retention._reset_throttle_for_test()


@pytest.fixture
def _clean_env():
    keys = [
        "RL_EVENTS_RETENTION_MAX_AGE_DAYS",
        "RL_EVENTS_RETENTION_MAX_ROWS",
        "RL_EVENTS_RETENTION_DISABLED",
        "RL_EVENTS_RETENTION_MIN_INTERVAL_S",
    ]
    with patch.dict(os.environ, {}, clear=False):
        for k in keys:
            os.environ.pop(k, None)
        yield


# ── plan computation ────────────────────────────────────────────────────


def test_default_plan_is_age_90_days(_clean_env):
    plan = rl_retention.compute_retention_plan(now_ms=_NOW_MS)
    assert plan.cutoff_ms == _NOW_MS - 90 * _DAY_MS
    assert plan.max_rows is None
    assert "age>90d" in plan.reason


def test_age_zero_disables_age_bound(_clean_env):
    os.environ["RL_EVENTS_RETENTION_MAX_AGE_DAYS"] = "0"
    plan = rl_retention.compute_retention_plan(now_ms=_NOW_MS)
    assert plan.cutoff_ms is None
    assert plan.is_noop()


def test_row_bound_when_configured(_clean_env):
    os.environ["RL_EVENTS_RETENTION_MAX_AGE_DAYS"] = "0"
    os.environ["RL_EVENTS_RETENTION_MAX_ROWS"] = "50000"
    plan = rl_retention.compute_retention_plan(now_ms=_NOW_MS)
    assert plan.cutoff_ms is None
    assert plan.max_rows == 50000
    assert "rows>50000" in plan.reason
    assert not plan.is_noop()


def test_age_older_than_floor_is_not_clamped(_clean_env):
    # A 1-day cutoff (now - 24h) is OLDER than the 6h in-flight floor, so it is
    # already safe and must pass through un-clamped.
    os.environ["RL_EVENTS_RETENTION_MAX_AGE_DAYS"] = "1"
    plan = rl_retention.compute_retention_plan(now_ms=_NOW_MS)
    assert plan.cutoff_ms == _NOW_MS - 1 * _DAY_MS
    assert "clamped" not in plan.reason


def test_inflight_floor_clamps_subfloor_age():
    # Directly exercise the clamp: patch the floor to a LARGE window so a
    # 1-day cutoff falls inside it and must be clamped back.
    with patch.dict(os.environ, {"RL_EVENTS_RETENTION_MAX_AGE_DAYS": "1"}, clear=False):
        with patch.object(rl_retention, "_INFLIGHT_PROTECT_SECONDS", 10 * 86_400.0):
            plan = rl_retention.compute_retention_plan(now_ms=_NOW_MS)
            # cutoff clamped to now - 10 days (the floor), NOT now - 1 day.
            assert plan.cutoff_ms == _NOW_MS - 10 * _DAY_MS
            assert "clamped-to-inflight-floor" in plan.reason


# ── driver behaviour ────────────────────────────────────────────────────


def test_disabled_env_skips(_clean_env):
    os.environ["RL_EVENTS_RETENTION_DISABLED"] = "true"
    calls = []
    res = rl_retention.maybe_run_retention(prune_fn=lambda **k: calls.append(k))
    assert res["ran"] is False
    assert res["skipped"] == "disabled"
    assert calls == []


def test_prune_called_with_resolved_cutoff(_clean_env):
    captured = {}

    def _fake_prune(**kw):
        captured.update(kw)
        return {"ok": True, "deleted": 42}

    res = rl_retention.maybe_run_retention(
        project_id="proj-1", force=True, now_ms=_NOW_MS, prune_fn=_fake_prune
    )
    assert res["ran"] is True
    assert res["deleted"] == 42
    assert captured["cutoff_ms"] == _NOW_MS - 90 * _DAY_MS
    assert captured["project_id"] == "proj-1"


def test_route_absent_returns_unsupported(_clean_env):
    # prune_fn returns None → route missing on this hub binary.
    res = rl_retention.maybe_run_retention(force=True, prune_fn=lambda **k: None)
    assert res["ran"] is False
    assert res["skipped"] == "route_unsupported"


def test_prune_error_is_soft_fail(_clean_env):
    def _boom(**kw):
        raise RuntimeError("hub wedged")

    res = rl_retention.maybe_run_retention(force=True, prune_fn=_boom)
    assert res["ran"] is False
    assert res["skipped"] == "prune_error"


def test_throttle_blocks_second_pass(_clean_env):
    calls = []

    def _fake_prune(**kw):
        calls.append(kw)
        return {"deleted": 0}

    # First pass (fresh process) allowed; second within the interval blocked.
    r1 = rl_retention.maybe_run_retention(now_ms=_NOW_MS, prune_fn=_fake_prune)
    r2 = rl_retention.maybe_run_retention(now_ms=_NOW_MS + 1000, prune_fn=_fake_prune)
    assert r1["ran"] is True
    assert r2["skipped"] == "throttled"
    assert len(calls) == 1


def test_noop_plan_consumes_throttle_but_does_not_call(_clean_env):
    os.environ["RL_EVENTS_RETENTION_MAX_AGE_DAYS"] = "0"  # no age, no rows → noop
    calls = []
    res = rl_retention.maybe_run_retention(
        now_ms=_NOW_MS, prune_fn=lambda **k: calls.append(k)
    )
    assert res["ran"] is False
    assert res["skipped"] == "noop"
    assert calls == []


# ── writer-path integration ─────────────────────────────────────────────


def test_writer_drives_retention_after_retrieval(_clean_env):
    posted = []
    driven = {"n": 0}

    def _hub_post(env):
        posted.append(env)
        return True

    def _fake_maybe(**kw):
        driven["n"] += 1
        return {"ran": False, "skipped": "noop"}

    writer = RLTelemetryWriter(project="P", project_id="pid-1", hub_post_fn=_hub_post)
    with patch(
        "claude_mcp_servers.rl_client.rl_retention.maybe_run_retention", _fake_maybe
    ):
        writer.log_retrieval(
            task_id="t1",
            task_type="mcp_interactive",
            query="q",
            nodes=[{"title": "N", "score": 0.5}],
        )
    assert len(posted) == 1  # the retrieval event was written
    assert driven["n"] == 1  # retention was driven exactly once


def test_writer_retention_soft_fails(_clean_env):
    def _hub_post(env):
        return True

    def _boom(**kw):
        raise RuntimeError("driver blew up")

    writer = RLTelemetryWriter(project="P", hub_post_fn=_hub_post)
    with patch(
        "claude_mcp_servers.rl_client.rl_retention.maybe_run_retention", _boom
    ):
        # Must NOT raise — retention failure never breaks a telemetry write.
        writer.log_retrieval(
            task_id="t2", task_type="mcp_interactive", query="q", nodes=[]
        )
