# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 RL-12 — rl-doctor diagnosability command.

rl-doctor is a READ-ONLY diagnostic that reports every gate + outcome on the RL
retrieval path so a user (or support) can see WHY RL isn't reranking:
scoring lock + per-project toggle, container reachability + negotiated
version, last rerank outcome (fallback counter), telemetry hub reachability,
retention status.

This suite verifies: the aggregate healthy/enabled logic, the fallback-counter
reader, the opted-out exit-0 path, the enabled-but-degraded exit-1 path, that
--json is machine-parseable, that no probe mutates state, and (v0.2.106) that
there is no licence gate left in the report.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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


_UNLOCKED = {"status": "unlocked", "locked": False, "detail": ""}


def _run_with_probes(*, toggle, container, telemetry, retention, fallback=None, tmp_path=None, lock=None):
    # The scoring lock is pinned UNLOCKED here so these cases keep exercising
    # the toggle/container logic beneath it; the locked case is below.
    fallback = fallback or {"status": "none", "count": 0, "detail": ""}
    root = str(tmp_path) if tmp_path else "/tmp/x"
    with patch.object(rl_doctor, "_probe_scoring_lock", return_value=lock or _UNLOCKED), patch.object(
        rl_doctor, "_probe_per_project_toggle", return_value=toggle
    ), patch.object(rl_doctor, "_probe_container", return_value=container), patch.object(
        rl_doctor, "_probe_telemetry_hub", return_value=telemetry
    ), patch.object(
        rl_doctor, "_probe_retention", return_value=retention
    ), patch.object(
        rl_doctor, "_probe_fallback_counter", return_value=fallback
    ):
        return rl_doctor.run_diagnostics(root)


def test_no_licence_is_needed_to_be_enabled():
    """v0.2.106: unlocked + toggle not off = RL enabled, with no licence
    anywhere in the inputs or the report (the reranker is free)."""
    report = _run_with_probes(
        toggle={"status": "unknown", "enabled": None, "detail": ""},
        container={"status": "compatible", "compatible": True, "reachable": True, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is True
    assert "license" not in report
    assert not hasattr(rl_doctor, "_probe_license")
    assert "license" not in rl_doctor.format_human(report).lower()


def test_enabled_and_compatible_is_healthy():
    report = _run_with_probes(
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "compatible", "compatible": True, "reachable": True, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is True


def test_enabled_but_container_incompatible_is_unhealthy():
    report = _run_with_probes(
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "incompatible_new_server", "compatible": False, "reachable": True, "detail": ""},
        telemetry={"status": "hub_up", "reachable": True, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is False


def test_enabled_but_hub_down_is_unhealthy():
    report = _run_with_probes(
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "compatible", "compatible": True, "reachable": True, "detail": ""},
        telemetry={"status": "hub_down", "reachable": False, "detail": ""},
        retention={"status": "active", "detail": ""},
    )
    assert report["healthy"] is False


def test_per_project_disabled_treated_as_not_enabled():
    report = _run_with_probes(
        toggle={"status": "disabled_for_project", "enabled": False, "detail": ""},
        container={"status": "disabled", "compatible": False, "reachable": False, "detail": ""},
        telemetry={"status": "hub_down", "reachable": False, "detail": ""},
        retention={"status": "noop", "detail": ""},
    )
    # Toggle off → not enabled → healthy (legitimately cosine).
    assert report["rl_enabled"] is False
    assert report["healthy"] is True


def test_scoring_lock_overrides_the_toggle():
    """v0.2.100 W5R-02: while the shipped lock is set, RL is not enabled even
    with an explicit per-project ON, and a down container is therefore
    nothing to fix."""
    report = _run_with_probes(
        toggle={"status": "enabled", "enabled": True, "detail": ""},
        container={"status": "disabled", "compatible": False, "reachable": False, "detail": ""},
        telemetry={"status": "hub_down", "reachable": False, "detail": ""},
        retention={"status": "noop", "detail": ""},
        lock={"status": "locked", "locked": True, "detail": "locked"},
    )
    assert report["rl_enabled"] is False
    assert report["healthy"] is True


def test_the_real_lock_probe_reports_the_shipped_lock():
    res = rl_doctor._probe_scoring_lock()
    assert res["status"] == "locked" and res["locked"] is True
    assert "until the model is trained" in res["detail"]


# ── CLI surface ─────────────────────────────────────────────────────────


def test_main_json_output_parses(capsys, tmp_path):
    report = {
        "rl_enabled": False,
        "healthy": True,
        "project_root": str(tmp_path),
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
        rl_doctor, "_probe_per_project_toggle", return_value={"status": "unknown", "enabled": None, "detail": ""}
    ), patch.object(
        rl_doctor, "_probe_retention", return_value={"status": "noop", "detail": ""}
    ):
        rl_doctor.run_diagnostics(str(tmp_path))
    after = set(p.name for p in tmp_path.rglob("*"))
    assert before == after


# ── v0.2.106: the "scoring state" line ──────────────────────────────────

_ENABLED = {"status": "enabled", "enabled": True, "detail": ""}
_HUB_UP = {"status": "hub_up", "reachable": True, "detail": ""}


@pytest.mark.parametrize("lock, toggle, container, status, needle", [
    ({"locked": True, "detail": "until trained"}, _ENABLED, {"status": "compatible"},
     "off_locked", "until trained"),
    (_UNLOCKED, {"enabled": False}, {"status": "compatible"},
     "off_project_disabled", "disabled for this project"),
    (_UNLOCKED, _ENABLED, {"status": "disabled"}, "off_disabled", "no container wired"),
    (_UNLOCKED, _ENABLED, {"status": "not_trained"}, "off_not_trained", "no trained weights"),
    (_UNLOCKED, _ENABLED, {"status": "unreachable"}, "off_unreachable", "does not answer"),
    (_UNLOCKED, _ENABLED, {"status": "degraded_old_server"}, "off_degraded_old_server",
     "too old to score"),
    (_UNLOCKED, _ENABLED, {"status": "embedding_space_mismatch"},
     "off_embedding_space_mismatch", "embedding space"),
    (_UNLOCKED, _ENABLED, {"status": "compatible", "weights_version": "1.2.3"}, "on", "1.2.3"),
])
def test_scoring_state_names_the_gate(lock, toggle, container, status, needle):
    state = rl_doctor._scoring_state(lock, toggle, container)
    assert state["status"] == status
    assert state["scoring"] is (status == "on")
    assert needle in state["detail"]


def test_not_trained_container_is_a_legitimate_off_state():
    report = _run_with_probes(
        toggle=_ENABLED,
        container={"status": "not_trained", "compatible": False, "reachable": True, "detail": ""},
        telemetry=_HUB_UP,
        retention={"status": "active", "detail": ""},
    )
    assert report["rl_enabled"] is True
    assert report["healthy"] is True
    assert report["scoring"]["status"] == "off_not_trained"
    assert "[scoring state] off_not_trained" in rl_doctor.format_human(report)


def test_unwired_container_on_an_enabled_install_still_needs_attention():
    report = _run_with_probes(
        toggle=_ENABLED,
        container={"status": "disabled", "compatible": False, "reachable": False, "detail": ""},
        telemetry=_HUB_UP,
        retention={"status": "active", "detail": ""},
    )
    assert report["healthy"] is False
    assert report["scoring"]["status"] == "off_disabled"
