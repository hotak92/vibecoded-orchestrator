#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hook latency probe — measure the SYNCHRONOUS (turn-blocking) cost of hooks.

Why this exists
---------------
A Claude Code turn is blocked only for as long as a hook's process stays
alive: work that a hook detaches into the background (``setsid`` / ``&`` /
``Start-Job``) does NOT block the turn. This probe measures wall-clock time
from ``bash <hook>`` start to hook EXIT — i.e. exactly the portion that
delays the user — for each registered POSIX hook, driven with a
representative stdin JSON envelope per the Claude Code v2.1.x hook I/O
contract (fields the hooks actually parse: ``tool_name``, ``tool_input``,
``session_id``, ``transcript_path``, etc.).

It is a DIAGNOSTIC TOOL, not a CI gate. Run it against a working tree to see
where user-felt latency lives, then drive the async work off the numbers.
The coarse regression GUARD (a real pytest) lives in
``tests/test_hook_sync_latency_guard.py`` — this file is intentionally NOT
collected by pytest (no ``test_`` functions at module import that assert
timings; the ``main`` entry point is invoked manually).

Measurement method
------------------
* ``time.monotonic()`` around ``subprocess.run(["bash", hook], input=...)``.
* N runs per (hook, payload); report p50 + p95 (ms). A short warmup run is
  discarded so filesystem-cache / interpreter-cold-start effects don't skew
  the first sample.
* Each hook runs against a fresh temp ``CLAUDE_PROJECT_DIR`` skeleton so the
  numbers reflect a clean project, not accumulated state.

Caveats (reported honestly — the numbers describe the SHELL portion)
--------------------------------------------------------------------
* Venv-gated heavy work (KG sync flush, RL citation drain embed-compute,
  codegraph analyzer) only runs when a VCO venv resolves. On a bare clone
  with no ``claude_mcp_servers/.venv`` those branches soft-exit, so the
  probe measures the hook's own shell portion (parse + decide + spawn +
  exit). Point ``VCO_VENV_PYTHON`` at a real venv to exercise more of the
  path where a heavier synchronous cost might hide (the probe surfaces
  whichever portion actually blocks).
* Background/detached work is deliberately EXCLUDED from the measurement —
  that is the whole point (it does not block the turn).

Usage::

    python tests/perf/hook_latency_probe.py            # all hooks, N=10
    python tests/perf/hook_latency_probe.py --runs 20
    python tests/perf/hook_latency_probe.py --only post-file-edit.sh
    python tests/perf/hook_latency_probe.py --markdown  # emit a md table
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"

# A representative session id (sanitiser-clean: [A-Za-z0-9_-]).
_SID = "probe_session_0001"


def _skeleton(root: Path) -> None:
    """Create a minimal CLAUDE_PROJECT_DIR the hooks expect to write into."""
    (root / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "concepts").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)


def _small_edit_payload(root: Path, tool: str = "Edit") -> dict:
    """A small code-file edit — the common per-turn case."""
    f = root / "sample.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")
    return {
        "tool_name": tool,
        "tool_input": {"file_path": str(f), "new_string": "def a():\n    return 2\n"},
        "session_id": _SID,
    }


def _large_edit_payload(root: Path, tool: str = "Write") -> dict:
    """A large file write — stresses credscan grep + any content-bearing path."""
    f = root / "big.py"
    # ~400 KB of code-shaped text (no real credentials).
    body = "\n".join(f"def fn_{i}(x):\n    return x + {i}" for i in range(12000))
    f.write_text(body, encoding="utf-8")
    return {
        "tool_name": tool,
        "tool_input": {"file_path": str(f), "content": body},
        "session_id": _SID,
    }


def _knowledge_edit_payload(root: Path) -> dict:
    """An edit under knowledge/ — exercises the KG-sync debounce schedule path."""
    f = root / "knowledge" / "concepts" / "probe_node.md"
    f.write_text("---\ntitle: Probe\ntype: concept\n---\n\nbody\n", encoding="utf-8")
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": str(f), "content": "body"},
        "session_id": _SID,
    }


def _bash_payload(root: Path, cmd: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": _SID,
    }


def _read_payload(root: Path) -> dict:
    f = root / "sample.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")
    return {
        "tool_name": "Read",
        "tool_input": {"file_path": str(f)},
        "session_id": _SID,
    }


def _stop_payload(root: Path) -> dict:
    """Stop-event envelope: session_id + a small transcript file."""
    t = root / ".claude" / "state" / "transcript.jsonl"
    t.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n",
        encoding="utf-8",
    )
    return {"session_id": _SID, "transcript_path": str(t)}


def _prompt_payload(root: Path) -> dict:
    return {"session_id": _SID, "prompt": "please add caching to the API layer"}


# The probe matrix: (hook file, event label, payload-builder).
# Grouped by the Claude Code event that fires them so the report totals
# blocking time PER EVENT (what the user actually waits for).
PROBES: list[tuple[str, str, str]] = [
    # PostToolUse(Edit|Write) — the busy per-turn event.
    ("post-file-edit.sh", "PostToolUse(Edit small)", "small_edit"),
    ("post-file-edit.sh", "PostToolUse(Write large)", "large_edit"),
    ("post-file-edit.sh", "PostToolUse(Write knowledge)", "knowledge_edit"),
    ("post-edit-outcome.sh", "PostToolUse(Edit small)", "small_edit"),
    ("post-tool-security.sh", "PostToolUse(Edit small)", "small_edit"),
    ("post-tool-security.sh", "PostToolUse(Write large)", "large_edit"),
    ("kg-summary-generator.sh", "PostToolUse(Edit small)", "small_edit"),
    ("kg-update-nudge.sh", "PostToolUse(Edit small)", "small_edit"),
    # PreToolUse(Edit) injectors + the universal logger.
    ("pre-tool-use.sh", "PreToolUse(Edit small)", "small_edit"),
    ("pre-tool-use.sh", "PreToolUse(Read code)", "read_code"),
    ("pre-edit-context-inject.sh", "PreToolUse(Edit small)", "small_edit"),
    # PreToolUse(Bash).
    ("pre-tool-use.sh", "PreToolUse(Bash routine)", "bash_routine"),
    ("pre-bash-context-inject.sh", "PreToolUse(Bash routine)", "bash_routine"),
    ("pre-bash-context-inject.sh", "PreToolUse(Bash long)", "bash_long"),
    # Stop event — cost-tracker + notify + RL/codegraph drains.
    ("stop-drain-citations.sh", "Stop", "stop"),
    ("stop-codegraph-drain.sh", "Stop", "stop"),
    ("stop-codegraph-reminder.sh", "Stop", "stop"),
    ("cost-tracker.sh", "Stop", "stop"),
    # UserPromptSubmit.
    ("diff-context-inject.sh", "UserPromptSubmit", "prompt"),
    ("user-prompt-submit-reminder.sh", "UserPromptSubmit", "prompt"),
]


def _build_payload(kind: str, root: Path) -> dict:
    if kind == "small_edit":
        return _small_edit_payload(root, tool="Edit")
    if kind == "large_edit":
        return _large_edit_payload(root, tool="Write")
    if kind == "knowledge_edit":
        return _knowledge_edit_payload(root)
    if kind == "read_code":
        return _read_payload(root)
    if kind == "bash_routine":
        return _bash_payload(root, "git status")
    if kind == "bash_long":
        # >500 chars → trips the KG-inject threshold in pre-bash.
        return _bash_payload(root, "echo " + ("migrate_collections " * 80))
    if kind == "stop":
        return _stop_payload(root)
    if kind == "prompt":
        return _prompt_payload(root)
    raise ValueError(f"unknown payload kind: {kind}")


def _time_one(hook_path: Path, payload: dict, project_root: Path, timeout: float) -> float | None:
    """Run one hook invocation; return ms to exit, or None on error/timeout."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Never inherit an ambient venv pin (wave-1 lesson) or a disable flag.
    env.pop("VCT_VENV", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    t0 = time.monotonic()
    try:
        subprocess.run(
            ["bash", str(hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:  # noqa: BLE001
        return None
    return (time.monotonic() - t0) * 1000.0


def _pct(samples: list[float], q: float) -> float:
    """Percentile (nearest-rank) of a list of floats."""
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[k]


def run_probe(runs: int, only: str | None, timeout: float) -> list[dict]:
    results: list[dict] = []
    for hook_name, event, kind in PROBES:
        if only and only not in hook_name:
            continue
        hook_path = HOOKS_DIR / hook_name
        if not hook_path.exists():
            results.append({"hook": hook_name, "event": event, "kind": kind, "error": "missing"})
            continue
        samples: list[float] = []
        # Fresh skeleton per hook so state doesn't accumulate across probes.
        tmp = Path(tempfile.mkdtemp(prefix="hook_probe_"))
        try:
            _skeleton(tmp)
            payload = _build_payload(kind, tmp)
            # Warmup (discarded): pay interpreter/FS cold-start once.
            _time_one(hook_path, payload, tmp, timeout)
            for _ in range(runs):
                ms = _time_one(hook_path, payload, tmp, timeout)
                if ms is not None:
                    samples.append(ms)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        results.append(
            {
                "hook": hook_name,
                "event": event,
                "kind": kind,
                "n": len(samples),
                "p50": _pct(samples, 0.50),
                "p95": _pct(samples, 0.95),
                "mean": statistics.fmean(samples) if samples else float("nan"),
            }
        )
    return results


def _fmt_ms(v: float) -> str:
    if v != v:  # NaN
        return "  n/a"
    return f"{v:6.1f}"


def print_table(results: list[dict], markdown: bool) -> None:
    if markdown:
        print("| Hook | Event | p50 (ms) | p95 (ms) | mean (ms) | N |")
        print("|------|-------|---------:|---------:|----------:|--:|")
        for r in results:
            if r.get("error"):
                print(f"| {r['hook']} | {r['event']} | — | — | — | (missing) |")
                continue
            print(
                f"| {r['hook']} | {r['event']} | {r['p50']:.1f} | {r['p95']:.1f} "
                f"| {r['mean']:.1f} | {r['n']} |"
            )
    else:
        print(f"{'hook':32} {'event':30} {'p50':>8} {'p95':>8} {'mean':>8} {'N':>3}")
        print("-" * 92)
        for r in results:
            if r.get("error"):
                print(f"{r['hook']:32} {r['event']:30} {'MISSING':>8}")
                continue
            print(
                f"{r['hook']:32} {r['event']:30} {_fmt_ms(r['p50']):>8} "
                f"{_fmt_ms(r['p95']):>8} {_fmt_ms(r['mean']):>8} {r['n']:>3}"
            )

    # Per-event totals (sum of p50 across hooks that fire on that event) —
    # the coarse "how long does the whole event block the turn" figure.
    print()
    print("Per-event total blocking time (sum of per-hook p50):")
    by_event: dict[str, float] = {}
    for r in results:
        if r.get("error"):
            continue
        p50 = r.get("p50", float("nan"))
        if p50 == p50:  # not NaN
            by_event[r["event"]] = by_event.get(r["event"], 0.0) + p50
    for event, total in sorted(by_event.items()):
        print(f"  {event:34} {total:7.1f} ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure synchronous hook latency.")
    parser.add_argument("--runs", type=int, default=10, help="samples per hook (default 10)")
    parser.add_argument("--only", default=None, help="substring filter on hook file name")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-run timeout seconds")
    parser.add_argument("--markdown", action="store_true", help="emit a markdown table")
    parser.add_argument("--json", action="store_true", help="emit raw JSON results")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        print("hook_latency_probe: POSIX-only (bash hooks); use the .ps1 harness on Windows.")
        return 0

    results = run_probe(args.runs, args.only, args.timeout)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results, args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
