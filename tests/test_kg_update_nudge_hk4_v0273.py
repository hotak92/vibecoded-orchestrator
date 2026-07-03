# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 HK-4 / D-7 — kg-update-nudge state hygiene.

- HK-4/D-7: the state read-modify-write is guarded by a flock on a STABLE
  sidecar lockfile (the pre-v0.2.73 fcntl.flock on a private mkstemp fd
  gave zero mutual exclusion). Concurrent invocations no longer clobber
  sibling-session rows or lose a baseline reset.
- HK-4: stale-session GC drops rows older than KG_NUDGE_GC_DAYS.

Hook-spawning tests MUST clear ambient VCT_VENV (wave-1 lesson).
POSIX-only for the .sh body under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NUDGE_SH = REPO_ROOT / "templates" / "hooks" / "kg-update-nudge.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the .sh nudge body is POSIX-only.",
)


def _env(home: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("VCT_VENV", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("KG_NUDGE_OFF", None)
    return env


def _run(home: Path, payload: dict, extra: dict | None = None):
    env = _env(home)
    if extra:
        env.update(extra)
    return subprocess.run(
        ["bash", str(NUDGE_SH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        cwd=str(home),
    )


def _metrics_path(home: Path) -> Path:
    return home / ".claude" / "metrics" / "kg_update_tokens.jsonl"


def _read_state(home: Path) -> dict:
    p = _metrics_path(home)
    out: dict = {}
    if not p.exists():
        return out
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        e = json.loads(ln)
        out[e["session_id"]] = e
    return out


def test_post_tool_kg_write_creates_state_and_lockfile_released(tmp_path):
    """A PostToolUse KG-write payload writes a state row; the lockfile is
    released (removed on the ps1 path / left as an empty flock-file on the
    sh path — either way not held)."""
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    kg_file = tmp_path / "knowledge" / "node.md"
    kg_file.write_text("# node\n")
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-A",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(kg_file)},
    }
    result = _run(tmp_path, payload)
    assert result.returncode == 0, result.stderr
    state = _read_state(tmp_path)
    assert "sess-A" in state
    # A second invocation must not deadlock (lock was released).
    result2 = _run(tmp_path, payload)
    assert result2.returncode == 0, result2.stderr


def test_stale_session_rows_are_gced(tmp_path):
    """Rows older than KG_NUDGE_GC_DAYS are dropped on the next write; the
    active session's row survives."""
    metrics = _metrics_path(tmp_path)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    rows = [
        {"session_id": "old-1", "baseline": 0, "updated_at": old_ts,
         "metric_version": "v10"},
        {"session_id": "fresh-1", "baseline": 0, "updated_at": fresh_ts,
         "metric_version": "v10"},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    kg_file = tmp_path / "knowledge" / "node.md"
    kg_file.write_text("# node\n")
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-active",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(kg_file)},
    }
    result = _run(tmp_path, payload, {"KG_NUDGE_GC_DAYS": "14"})
    assert result.returncode == 0, result.stderr

    state = _read_state(tmp_path)
    assert "old-1" not in state, "stale row should have been GC'd"
    assert "fresh-1" in state, "recent row should survive"
    assert "sess-active" in state, "active session row must be written"


def test_gc_keeps_undateable_rows(tmp_path):
    """A row with a malformed updated_at is kept (conservative — never GC
    a row we can't date)."""
    metrics = _metrics_path(tmp_path)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"session_id": "weird", "baseline": 0, "updated_at": "not-a-date",
         "metric_version": "v10"},
    ]
    metrics.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    kg_file = tmp_path / "knowledge" / "node.md"
    kg_file.write_text("# node\n")
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "sess-active",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(kg_file)},
    }
    result = _run(tmp_path, payload, {"KG_NUDGE_GC_DAYS": "14"})
    assert result.returncode == 0, result.stderr
    state = _read_state(tmp_path)
    assert "weird" in state


def test_concurrent_writes_do_not_lose_sibling_rows(tmp_path):
    """Fire two KG-write invocations for DIFFERENT sessions concurrently;
    both rows must survive (the stable lock serialises the RMW, so neither
    clobbers the other — the lost-update D-7 bug)."""
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    kg_file = tmp_path / "knowledge" / "node.md"
    kg_file.write_text("# node\n")

    def _payload(sid):
        return {
            "hook_event_name": "PostToolUse",
            "session_id": sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": str(kg_file)},
        }

    procs = []
    for sid in ("cc-1", "cc-2", "cc-3", "cc-4"):
        env = _env(tmp_path)
        procs.append(subprocess.Popen(
            ["bash", str(NUDGE_SH)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path),
        ))
    for p, sid in zip(procs, ("cc-1", "cc-2", "cc-3", "cc-4")):
        p.communicate(input=json.dumps(_payload(sid)), timeout=25)

    state = _read_state(tmp_path)
    for sid in ("cc-1", "cc-2", "cc-3", "cc-4"):
        assert sid in state, (
            f"{sid} row lost — concurrent RMW clobbered a sibling "
            f"(D-7 lost-update). Present: {sorted(state)}"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
