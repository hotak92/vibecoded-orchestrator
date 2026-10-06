# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.77 Part 9 task 5 — repeat-brief KG injection served from cache.

History: subagent-start-kg-inject.sh cost ~3.8 s/spawn (1793 spawns ~= 113 min
in one fleet session, audit 2026-07-11), so the KG result was served from the
shared TTL cache. v0.2.101 (PLAN-V02101 §C4/§C5) RETIRED that hook's KG half
— the SubagentStart payload carries only agent_id + agent_type, so the old
prompt-keyed query could never fire — and moved the capability to the
parent-side PreToolUse(Agent|Task) hook ``agent-brief-kg-inject.sh`` driving
the router's ``agent`` surface. The COST CONTRACT this file pins is unchanged
in the new home: a repeat of the SAME brief (same session, same prompt_id —
the cache-key scoping the router uses) must be served from the router's
shared TTL cache (~ms) instead of re-paying the KG search, and a genuinely
different brief still queries live. The old shell-side cache is superseded by
the router's (one home, ``claude_mcp_servers/scripts/hook_context_router.py``).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "templates" / "hooks" / "agent-brief-kg-inject.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash hook; .ps1 sibling covered by hook-OS-parity",
)


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    """Sandbox: fake VCO venv (VCT_INSTALL_ROOT) + a COUNTING KG stub exposed
    through the router's VCO_ROUTER_KG_SCRIPT seam. The REAL router resolves
    from VCT_ORCHESTRATOR_ROOT (this checkout)."""
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    vb = root / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(sys.executable, vb / "python")
    marker = tmp_path / "search_calls"
    stub = root / "stub_kg.py"
    stub.write_text(
        "import argparse\n"
        "\n"
        "def main(argv=None):\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('query')\n"
        "    ap.add_argument('--limit', type=int, default=3)\n"
        "    ap.add_argument('--hook-format', action='store_true')\n"
        "    ap.add_argument('--injection-profile')\n"
        "    ap.add_argument('--task-type')\n"
        "    ap.add_argument('--transcript')\n"
        "    ap.parse_args(argv)\n"
        f"    open({str(marker)!r}, 'a').write('x')\n"
        "    print('KG: Subagent Probe Node | concept | score=0.90 | FULL NODE:')\n"
        "    print('probe body')\n",
        encoding="utf-8",
    )
    return root, marker


def _run(root: Path, prompt: str, agent_no: int, session: str = "sess-sa-cache",
         prompt_id: str = "p-sa-cache") -> subprocess.CompletedProcess:
    payload = {
        "tool_name": "Agent",
        "session_id": session,
        "prompt_id": prompt_id,
        "tool_input": {"prompt": prompt, "description": f"lane {agent_no}",
                       "model": "claude-gw/glm-5.3"},
    }
    env = os.environ.copy()
    env.update({
        "CLAUDE_PROJECT_DIR": str(root),
        "VCT_INSTALL_ROOT": str(root),
        "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
        "VCO_ROUTER_KG_SCRIPT": str(root / "stub_kg.py"),
    })
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=str(root),
    )


def test_second_identical_brief_served_from_cache(tmp_path: Path) -> None:
    root, marker = _setup(tmp_path)

    brief = "Task: implement the retrieval reranker with RL scoring"
    first = _run(root, brief, agent_no=1)
    assert first.returncode == 0, first.stderr
    assert "Subagent Probe Node" in first.stdout, (
        f"first dispatch must emit the KG-enriched envelope; stdout={first.stdout!r}")
    assert marker.read_text("utf-8").count("x") == 1, (
        "first dispatch should run the KG search once")

    # Second dispatch, SAME brief text + SAME session/prompt_id (the cache-key
    # scope) → cache hit, no re-query. (The second ENVELOPE is suppressed by
    # the per-session seen-store dedupe — by design: identity dedupe and cost
    # dedupe are separate mechanisms; this test pins the COST one.)
    second = _run(root, brief, agent_no=2)
    assert second.returncode == 0, second.stderr
    calls = marker.read_text("utf-8").count("x")
    assert calls == 1, (
        f"an identical repeat brief must be served from the router's cache "
        f"WITHOUT re-running the search — expected 1 producer call, got {calls}")


def test_different_brief_misses_cache(tmp_path: Path) -> None:
    root, marker = _setup(tmp_path)

    _run(root, "Task: first distinct task about widgets", agent_no=1)
    _run(root, "Task: second entirely different task about auth", agent_no=2)
    calls = marker.read_text("utf-8").count("x") if marker.exists() else 0
    assert calls == 2, (
        f"two DIFFERENT briefs must each run the search (no false cache hit); "
        f"got {calls}")
