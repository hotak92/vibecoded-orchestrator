# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 RL-12 — rl-doctor diagnosability command.

rl-doctor is a READ-ONLY diagnostic that reports every gate + outcome on the RL
retrieval path so a Pro user (or support) can see WHY RL isn't reranking:
license/feature gate, container reachability + negotiated version, last rerank
outcome (fallback counter), telemetry hub reachability, retention status.

This suite verifies: the aggregate healthy/enabled logic, the fallback-counter
reader, the free-tier exit-0 path, the enabled-but-degraded exit-1 path, that
--json is machine-parseable, and that no probe mutates state.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from claude_mcp_servers.scripts import rl_doctor  # noqa: E402


# ── fallback-counter reader ─────────────────────────────────────────────


def test_fallback_counter_absent(tmp_path):
    res = rl_doctor._probe_fallback_counter(str(tmp_path))
    assert res["status"] == "none"
    assert res["count"] == 0


def test_fallback_counter_present(tmp_path):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "rl_fallback_counter.json").write_text(
        json.dumps({"count": 7, "last_reason": "HTTP 409 env-pin", "last_ts": "2026-07-03T10:00:00+0000"})
    )
    res = rl_doctor._probe_fallback_counter(str(tmp_path))
    assert res["status"] == "fallbacks_recorded"
    assert res["count"] == 7
    assert "409" in res["last_reason"]


def test_fallback_counter_corrupt(tmp_path):
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (state / "rl_fallback_counter.json").write_text("{ not json")
    res = rl_doctor._probe_fallback_counter(str(tmp_path))
    assert res["status"] == "unreadable"


# ── aggregate health logic ──────────────────────────────────────────────


def _run_with_probes(*, lic, toggle, container, telemetry, retention, fallback=None, tmp_path=None):
    fallback = fallback or {"status": "none", "count": 0, "detail": ""}
    root = str(tmp_path) if tmp_path else "/tmp/x"
    with patch.object(rl_doctor, "_probe_license", return_value=lic), patch.object(
        rl_doctor, "_probe_per_project_toggle", return_value=toggle
    ), patch.object(rl_doctor, "_probe_container", return_value=container), patch.object(
        rl_doctor, "_probe_telemetry_hub", return_value=telemetry
    ), patch.object(
        rl_doctor, "_probe_retention", return_value=retention
    ), patch.object(
        rl_doctor, "_probe_fallback_counter", return_value=fallback
    ):
        return rl_doctor.run_diagnostics(root)


def test_free_tier_is_healthy():
    report = _run_with_probes(
        lic={"status": "free_tier", "enabled": False, "detail": ""},
        toggle={"status": "unknown", "enabled": None, "detail": ""},
        container={"status": "disabled", "compatible": False, "reachable": False, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is False
    assert report["healthy"] is True  # free-tier → nothing to fix


def test_enabled_and_compatible_is_healthy():
    report = _run_with_probes(
        lic={"status": "enabled", "enabled": True, "detail": ""},
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "compatible", "compatible": True, "reachable": True, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is True


def test_enabled_but_container_incompatible_is_unhealthy():
    report = _run_with_probes(
        lic={"status": "enabled", "enabled": True, "detail": ""},
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "incompatible_new_server", "compatible": False, "reachable": True, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is False


def test_enabled_but_hub_down_is_unhealthy():
    report = _run_with_probes(
        lic={"status": "enabled", "enabled": True, "detail": ""},
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "compatible", "compatible": True, "reachable": True, "detail": ""},
        telemetry={"status": "hub_down", "reachable": False, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["healthy"] is False


def test_per_project_disabled_treated_as_not_enabled():
    report = _run_with_probes(
        lic={"status": "enabled", "enabled": True, "detail": ""},
        toggle={"status": "disabled_for_project", "enabled": False, "detail": ""},
        container={"status": "disabled", "compatible": False, "reachable": False, "detail": ""},
        telemetry={"status": "hub_down", "reachable": False, "detail": ""},
        retention={"status": "noop", "detail": ""},
    )
    # Toggle off → not enabled → healthy (legitimately cosine).
    assert report["rl_enabled"] is False
    assert report["healthy"] is True


# ── CLI surface ─────────────────────────────────────────────────────────


def test_main_json_output_parses(capsys, tmp_path):
    report = {
        "rl_enabled": False,
        "healthy": True,
        "project_root": str(tmp_path),
        "license": {"status": "free_tier", "detail": ""},
        "per_project_toggle": {"status": "unknown", "detail": ""},
        "container": {"status": "disabled", "detail": ""},
        "last_fallback": {"status": "none", "detail": ""},
        "telemetry_hub": {"status": "hub_up", "detail": ""},
        "retention": {"status": "active", "detail": ""},
    }
    with patch.object(rl_doctor, "run_diagnostics", return_value=report):
        rc = rl_doctor.main(["--json", "--project-root", str(tmp_path)])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["healthy"] is True
    assert rc == 0  # healthy → exit 0


def test_main_exit_1_when_unhealthy(capsys, tmp_path):
    report = {
        "rl_enabled": True,
        "healthy": False,
        "project_root": str(tmp_path),
        "license": {"status": "enabled", "detail": ""},
        "per_project_toggle": {"status": "enabled", "detail": ""},
        "container": {"status": "unreachable", "detail": "down"},
        "last_fallback": {"status": "fallbacks_recorded", "detail": ""},
        "telemetry_hub": {"status": "hub_down", "detail": ""},
        "retention": {"status": "active", "detail": ""},
    }
    with patch.object(rl_doctor, "run_diagnostics", return_value=report):
        rc = rl_doctor.main(["--project-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "NEEDS ATTENTION" in out
    assert rc == 1


def test_run_diagnostics_is_read_only(tmp_path):
    # Running against a clean tmp root must create NO files (read-only).
    before = set(p.name for p in tmp_path.rglob("*"))
    # Force container/telemetry probes to safe stubs so no real network/hub.
    with patch.object(
        rl_doctor, "_probe_container", return_value={"status": "disabled", "compatible": False, "reachable": False, "detail": ""}
    ), patch.object(
        rl_doctor, "_probe_telemetry_hub", return_value={"status": "hub_down", "reachable": False, "detail": ""}
    ), patch.object(
        rl_doctor, "_probe_license", return_value={"status": "free_tier", "enabled": False, "detail": ""}
    ), patch.object(
        rl_doctor, "_probe_per_project_toggle", return_value={"status": "unknown", "enabled": None, "detail": ""}
    ), patch.object(
        rl_doctor, "_probe_retention", return_value={"status": "noop", "detail": ""}
    ):
        rl_doctor.run_diagnostics(str(tmp_path))
    after = set(p.name for p in tmp_path.rglob("*"))
    assert before == after
