# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 5 — subagent-start KG injection served from cache.

subagent-start-kg-inject.sh costs ~3.8 s/spawn (1793 spawns ~= 113 min in one
fleet session, audit 2026-07-11). This test drives the real hook end-to-end
with a stub venv + a COUNTING rl_kg_search.py and asserts that a SECOND spawn
with the same prompt is served from the shared TTL cache — the search launches
exactly ONCE across two spawns, and BOTH spawns still emit the injection.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "templates" / "hooks" / "subagent-start-kg-inject.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash hook; .ps1 sibling covered by hook-OS-parity",
)


def _setup(tmp_path: Path) -> Path:
    """Build a sandbox where the hook's venv + rl_kg_search resolve, with a
    counting stub producer."""
    root = tmp_path / "proj"
    (root / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    (root / ".claude" / "state").mkdir(parents=True)
    (root / ".claude" / "logs").mkdir(parents=True)
    # Fake venv → system python3 (resolve-vco-venv.sh accepts VCT_INSTALL_ROOT/.venv).
    vb = root / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(shutil.which("python3") or sys.executable, vb / "python")
    return root


def _write_counting_producer(root: Path, marker: Path) -> None:
    rl = root / "claude_mcp_servers" / "scripts" / "rl_kg_search.py"
    rl.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, argparse\n"
        "ap = argparse.ArgumentParser()\n"
        "ap.add_argument('query')\n"
        "ap.add_argument('--limit', type=int, default=3)\n"
        "ap.add_argument('--hook-format', action='store_true')\n"
        "args = ap.parse_args()\n"
        f"open({str(marker)!r}, 'a').write('x')\n"
        "print('KG: Subagent Probe Node | concept | score=0.90 | FULL NODE:')\n"
        "print('probe body')\n",
        encoding="utf-8",
    )
    rl.chmod(rl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(root: Path, prompt: str, agent_id: str) -> subprocess.CompletedProcess:
    payload = {
        "prompt": prompt,
        "session_id": "sess-sa-cache",
        "agent_id": agent_id,
        "agent_type": "@agent-coder",
    }
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(root)
    env["VCT_INSTALL_ROOT"] = str(root)
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_second_spawn_served_from_cache(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    marker = tmp_path / "search_calls"
    _write_counting_producer(root, marker)

    prompt = "implement the retrieval reranker with RL scoring"
    first = _run(root, prompt, "agent-1")
    assert first.returncode == 0, first.stderr
    assert "Subagent Probe Node" in first.stdout, (
        f"first spawn must emit the KG injection; stdout={first.stdout!r}")
    calls_1 = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert calls_1 == 1, f"first spawn should run the search once, got {calls_1}"

    # Second spawn, SAME prompt, DIFFERENT agent id → cache hit.
    second = _run(root, prompt, "agent-2")
    assert second.returncode == 0, second.stderr
    assert "Subagent Probe Node" in second.stdout, (
        f"second spawn must STILL emit the injection (from cache); "
        f"stdout={second.stdout!r}")
    calls_2 = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert calls_2 == 1, (
        f"second identical spawn must be served from cache WITHOUT re-running "
        f"the search — expected the marker to stay at 1, got {calls_2}")


def test_different_prompt_misses_cache(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    marker = tmp_path / "search_calls"
    _write_counting_producer(root, marker)

    _run(root, "first distinct task about widgets", "agent-a")
    _run(root, "second entirely different task about auth", "agent-b")
    calls = len(marker.read_text("utf-8")) if marker.exists() else 0
    assert calls == 2, (
        f"two DIFFERENT prompts must each run the search (no false cache hit); "
        f"got {calls}")
