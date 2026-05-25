# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""End-to-end tests for templates/hooks/subagent-start-suggest.sh.

The SubagentStart hook mirrors the UserPromptSubmit-side
`agent-skill-keyword-suggest.sh` (covered by test_hook_keyword_suggest.py)
but for subagent spawn time. We test the WRAPPER behavior here — the
matching algorithm itself is exercised by tests/test_keyword_match.py.

What this file specifically guards:
- SubagentStart JSON input shape parsing (`prompt`/`task`/`description`
  synonyms; `tools`/`allowed_tools`/`tool_list` synonyms; tools as either
  list-of-strings or whitespace/comma-delimited string).
- Agent suggestions are SUPPRESSED when the subagent's tool list does
  NOT include `Agent` or `Task` (the user vetoed pointlessly suggesting
  agents to a subagent that can't spawn them).
- Agent suggestions are INCLUDED when Agent/Task IS in the tool list,
  AND when the tools field is absent or empty (default: over-suggest,
  per user direction "cost of MISSING is higher than over-suggest").
- The envelope's `hookEventName` is `SubagentStart` (not the
  UserPromptSubmit value the older sibling emits).

POSIX-only (the hook is bash). PowerShell sibling parity is exercised by
the existing hook-parity gate (.github/scripts/check_hook_parity.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "templates" / "hooks" / "subagent-start-suggest.sh"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook is POSIX-only; .ps1 sibling covers Windows.",
)


def _write_agent(root: Path, slug: str, name: str, keywords: list[str],
                 short_desc: str = "") -> None:
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    kw_inline = "[" + ", ".join(keywords) + "]"
    sd_line = f"short_desc: {short_desc}\n" if short_desc else ""
    body = f"---\nname: {name}\nkeywords: {kw_inline}\n{sd_line}---\n# {name}\n"
    (agents_dir / f"{slug}.md").write_text(body, encoding="utf-8")


def _write_skill(root: Path, slug: str, name: str, keywords: list[str],
                 short_desc: str = "") -> None:
    skill_dir = root / ".claude" / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    kw_inline = "[" + ", ".join(keywords) + "]"
    sd_line = f"short_desc: {short_desc}\n" if short_desc else ""
    body = f"---\nname: {name}\nkeywords: {kw_inline}\n{sd_line}---\n# {name}\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _run_hook(payload: dict, project_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Scope per-session dedup state to this test's tmp_path so a hand-run
    # pytest doesn't leak state outside of it.
    env["VCT_KEYWORD_DEDUP_DIR"] = str(project_root / ".claude" / "state")
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------


def test_emits_subagent_start_envelope_when_agent_tool_present(tmp_path):
    """When Agent is in the subagent's tool list, both agents and skills
    are surfaced in the envelope."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes", "Helm"], short_desc="review k8s manifests")
    _write_skill(tmp_path, "tdd-skill", "tdd-skill",
                 ["TDD"], short_desc="drive via tests")
    payload = {
        "prompt": "review my Kubernetes manifest using TDD",
        "session_id": "s-1",
        "tools": ["Read", "Edit", "Agent", "Bash"],
    }
    result = _run_hook(payload, tmp_path)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}")
    assert result.stdout.strip(), (
        f"expected envelope on stdout; stderr={result.stderr!r}")
    envelope = json.loads(result.stdout)
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "SubagentStart"
    ctx = hso["additionalContext"]
    assert "kubernetes-agent" in ctx
    assert "tdd-skill" in ctx
    # Singular agreement when each group has exactly one match.
    assert "this agent" in ctx
    assert "this skill" in ctx


def test_suppresses_agents_when_agent_tool_absent(tmp_path):
    """When Agent/Task is NOT in the subagent's tool list, agent
    suggestions are suppressed — only skills surface. Logic: a subagent
    without Agent/Task has no way to spawn sub-subagents, so suggesting
    them is noise."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="review k8s manifests")
    _write_skill(tmp_path, "tdd-skill", "tdd-skill",
                 ["TDD"], short_desc="drive via tests")
    payload = {
        "prompt": "review my Kubernetes manifest using TDD",
        "session_id": "s-2",
        "tools": ["Read", "Edit", "Bash"],   # NO Agent / NO Task
    }
    result = _run_hook(payload, tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip(), "expected at least the skill suggestion"
    envelope = json.loads(result.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" not in ctx, (
        "agent must be suppressed when subagent lacks Agent/Task tool")
    assert "tdd-skill" in ctx, "skill must still surface"
    # No agents header should appear.
    assert "You might want to use this agent" not in ctx
    assert "You might want to use these agents" not in ctx


def test_task_tool_also_unlocks_agent_suggestions(tmp_path):
    """`Task` is a legacy alias for `Agent` (see CLAUDE.md). Either one
    in the tool list should unlock agent suggestions."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {
        "prompt": "review Kubernetes",
        "session_id": "s-3",
        "tools": ["Read", "Task"],   # Task, not Agent
    }
    result = _run_hook(payload, tmp_path)

    assert result.returncode == 0
    envelope = json.loads(result.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" in ctx, (
        "Task alias should unlock agent suggestions, same as Agent")


def test_empty_tools_list_defaults_to_agents_allowed(tmp_path):
    """Empty/absent tool list → default to over-suggesting (per user
    direction: cost of MISSING is higher than over-suggesting). An empty
    list is treated as "we don't know what the subagent has", not as
    "the subagent has nothing"."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {
        "prompt": "review Kubernetes",
        "session_id": "s-4",
        "tools": [],   # empty
    }
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    envelope = json.loads(result.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" in ctx


def test_missing_tools_field_defaults_to_agents_allowed(tmp_path):
    """Same as above but for the truly-absent case: no `tools` key at
    all in the payload."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {"prompt": "review Kubernetes", "session_id": "s-5"}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    envelope = json.loads(result.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" in ctx


# ---------------------------------------------------------------------------
# Field-name synonyms (defensive against Claude Code contract phrasing)
# ---------------------------------------------------------------------------


def test_accepts_task_field_synonym_for_prompt(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    # `task` instead of `prompt`.
    payload = {"task": "review Kubernetes", "session_id": "s-6",
               "tools": ["Agent"]}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip(), (
        "expected envelope when prompt is provided via `task` synonym")


def test_accepts_description_field_synonym_for_prompt(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    # `description` instead of `prompt`.
    payload = {"description": "review Kubernetes", "session_id": "s-7",
               "tools": ["Agent"]}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip(), (
        "expected envelope when prompt is provided via `description` synonym")


def test_accepts_tools_as_whitespace_delimited_string(tmp_path):
    """Some hook contracts pass tools as a string ("Read Edit Agent")
    rather than an array. We tolerate both."""
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {
        "prompt": "review Kubernetes",
        "session_id": "s-8",
        "tools": "Read Edit Agent Bash",
    }
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    envelope = json.loads(result.stdout)
    ctx = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" in ctx


def test_accepts_allowed_tools_synonym(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {
        "prompt": "review Kubernetes",
        "session_id": "s-9",
        "allowed_tools": ["Read", "Edit"],  # no Agent / no Task
    }
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    # Skills-only because allowed_tools lacks Agent/Task. Agent is
    # filtered, so no envelope is emitted (the only matchable item was
    # an agent).
    assert result.stdout.strip() == "", (
        f"expected silent exit when only matchable item is filtered out; "
        f"got stdout={result.stdout!r}")


# ---------------------------------------------------------------------------
# Silent no-op paths (the hook MUST never block a spawn)
# ---------------------------------------------------------------------------


def test_silent_when_no_match(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {"prompt": "write a haiku about pasta", "session_id": "s-n1",
               "tools": ["Agent"]}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_silent_when_disabled(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env["VCT_DISABLE_HOOKS"] = "1"
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps({"prompt": "Kubernetes", "tools": ["Agent"]}),
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_silent_on_malformed_json(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env.pop("VCT_DISABLE_HOOKS", None)
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input="not valid json at all",
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_silent_on_empty_prompt(tmp_path):
    _write_agent(tmp_path, "k8s", "kubernetes-agent",
                 ["Kubernetes"], short_desc="hint")
    payload = {"prompt": "", "session_id": "s-n2", "tools": ["Agent"]}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_silent_when_no_claude_dir(tmp_path):
    """Project root exists but has no `.claude/agents/` or `.claude/skills/`.
    Hook must exit silently (no envelope) rather than emit a header-only
    block."""
    payload = {"prompt": "review Kubernetes", "session_id": "s-n3",
               "tools": ["Agent"]}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
