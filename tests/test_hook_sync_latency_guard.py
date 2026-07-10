# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.76 P5 — coarse regression guard for hook SYNCHRONOUS latency.

A Claude Code turn is blocked only while a hook process stays alive; work a
hook detaches into the background (setsid / ``&`` / Start-Process) does NOT
block the turn. v0.2.76 Part 5 measured the per-hook blocking cost and cut
the biggest offenders (pre-tool-use.sh, post-tool-security.sh — redundant
multi-python stdin parses; stop-drain-citations.sh — a blocking ``& wait``
around the RL embed drain, now detached).

This guard stops a FUTURE change from silently re-introducing heavy
synchronous work on the hot path. It drives the three highest-traffic
fire-and-forget hooks on a small edit and asserts each returns under a
GENEROUS bound. The bound is deliberately loose: the point is to catch a
re-introduced BLOCKING embed / network / analyzer call (which would add
hundreds of ms to seconds), NOT to police normal shell + one-python-parse
jitter (tens of ms). It must never flake CI.

Flake protection:
  * Generous default bound (VCT_HOOK_SYNC_BOUND_MS, default 500ms).
  * A warmup run is discarded (interpreter / FS cold-start).
  * best-of-N sampling (min of a few runs) so one scheduler hiccup on a
    loaded runner doesn't trip it.
  * The strict timing assertion is SKIPPED when VCT_SKIP_LATENCY_GUARD=1 is
    set (an escape hatch for a pathologically slow/over-subscribed runner);
    the functional part (exit 0, no crash) still runs.

POSIX-only (bash hooks). The synchronous portion measured here is the shell
work + a single stdin decode; venv-gated heavy paths (KG-sync flush, RL
drain, codegraph analyzer) soft-exit on a tree with no MCP venv, which is
exactly the SYNCHRONOUS portion we want to bound (background work is
excluded by design).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hooks are POSIX-only; the .ps1 latency profile is not guarded here.",
)

# Generous bound — a re-introduced blocking embed/network call is >>500ms,
# while the intended synchronous portion (parse + decide + spawn + exit) is
# tens of ms. Env-overridable for tuning on unusual hardware.
_BOUND_MS = float(os.environ.get("VCT_HOOK_SYNC_BOUND_MS", "500"))
_SKIP_TIMING = os.environ.get("VCT_SKIP_LATENCY_GUARD") == "1"

# The hot-path fire-and-forget hooks whose synchronous portion must stay small.
# (Injection hooks like pre-edit-context-inject are inherently synchronous —
# their stdout must reach the model before the tool runs — so they are NOT
# guarded here; their cost is a retrieval-tuning concern, tracked separately.)
_GUARDED_HOOKS = [
    "post-file-edit.sh",
    "post-tool-security.sh",
    "pre-tool-use.sh",
]


def _skeleton(root: Path) -> None:
    (root / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "concepts").mkdir(parents=True, exist_ok=True)


def _small_edit_payload(root: Path) -> dict:
    f = root / "sample.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(f), "new_string": "def a():\n    return 2\n"},
        "session_id": "guard_session_0001",
    }


def _run_once(hook_path: Path, payload: dict, project_root: Path) -> tuple[int, float]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env.pop("VCT_VENV", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    t0 = time.monotonic()
    result = subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result.returncode, (time.monotonic() - t0) * 1000.0


@pytest.mark.parametrize("hook_name", _GUARDED_HOOKS)
def test_hook_sync_portion_under_bound(hook_name: str, tmp_path: Path) -> None:
    """The synchronous portion of a hot-path hook stays under the bound.

    Runs the hook on a small edit N times (best-of-N to absorb scheduler
    jitter) after a discarded warmup; asserts the fastest run exits 0 and
    completes under VCT_HOOK_SYNC_BOUND_MS. Functional exit-0 is always
    checked; the timing assertion is skipped under VCT_SKIP_LATENCY_GUARD=1.
    """
    hook_path = HOOKS_DIR / hook_name
    assert hook_path.exists(), f"{hook_name} missing from {HOOKS_DIR}"

    _skeleton(tmp_path)
    payload = _small_edit_payload(tmp_path)

    # Warmup (discarded): pay interpreter/FS cold-start once.
    _run_once(hook_path, payload, tmp_path)

    best_ms = float("inf")
    for _ in range(5):
        rc, ms = _run_once(hook_path, payload, tmp_path)
        assert rc == 0, f"{hook_name}: exit {rc} (hooks must soft-fail exit 0)"
        best_ms = min(best_ms, ms)

    if _SKIP_TIMING:
        pytest.skip("VCT_SKIP_LATENCY_GUARD=1 — timing assertion disabled for this runner")

    assert best_ms < _BOUND_MS, (
        f"{hook_name}: synchronous portion {best_ms:.0f}ms exceeds the "
        f"{_BOUND_MS:.0f}ms bound (best of 5). A re-introduced blocking "
        f"embed/network/analyzer call on the hot path is the likely cause — "
        f"move it into the existing background-detach pattern, or (if the "
        f"regression is real hardware) raise VCT_HOOK_SYNC_BOUND_MS."
    )


def test_guard_covers_the_expected_hooks() -> None:
    """Guard against silently dropping a hook from the guarded set (a glob or
    rename could make the parametrize list empty and the guard vacuous)."""
    for name in _GUARDED_HOOKS:
        assert (HOOKS_DIR / name).exists(), f"guarded hook {name} not found"
    assert len(_GUARDED_HOOKS) >= 3
