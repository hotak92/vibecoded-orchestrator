# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 §C4 (PLAN-V02101) — the Agent-brief KG injection hook.

``templates/hooks/agent-brief-kg-inject.{sh,ps1}`` runs the router's
``agent`` surface on PreToolUse(Agent|Task) and passes the router's
``updatedInput`` envelope through VERBATIM. The envelope must:

* echo EVERY original ``tool_input`` field (``updatedInput`` REPLACES the
  whole object — a dropped field breaks the dispatch);
* modify ONLY ``prompt`` (appending the ``[KG context for this task]:``
  block built from the brief's TASK section);
* NEVER carry ``permissionDecision`` (that would auto-approve the spawn);
* emit nothing at all for a briefless prompt (input untouched, exit 0).

The KG producer is stubbed through the router's ``VCO_ROUTER_KG_SCRIPT``
seam; the stub RECORDS the argv it was pinned to, so the test also pins
the WP-D contract: the query is the TASK section and the leg carries
``--injection-profile agent_brief`` + ``--task-type agent_brief_kg_search``
(the bound itself — ≤1 500 chars, floor 0.65 — is enforced inside the
producer via the profile and pinned by test_v02101_inject_gates.py).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"
ROUTER_REL = "claude_mcp_servers/scripts/hook_context_router.py"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash hook; .ps1 sibling driven separately below")
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed")


# --------------------------------------------------------------------------- #
# TASK-section extraction (vco_lib.inject_intent.agent_task_section)
# --------------------------------------------------------------------------- #

def test_task_line_section_wins():
    from vco_lib.inject_intent import agent_task_section
    prompt = ("@agent-implementer (Model)\n"
              "Task: One sentence goal\n"
              "Context: File paths, patterns, constraints\n"
              "Success Criteria: What done looks like\n")
    assert agent_task_section(prompt) == "One sentence goal"


def test_first_sentence_after_preamble():
    from vco_lib.inject_intent import agent_task_section
    prompt = ("Effort medium. FIRST ACTION: read the plan.\n"
              "Implement the reranker without hurting recall. Other text.")
    assert agent_task_section(prompt) == "Implement the reranker without hurting recall."


def test_empty_prompt_is_briefless():
    from vco_lib.inject_intent import agent_task_section
    assert agent_task_section("") == ""
    assert agent_task_section("   \n  ") == ""


# --------------------------------------------------------------------------- #
# Sandbox: fake venv (VCT_INSTALL_ROOT) + real router (VCT_ORCHESTRATOR_ROOT)
# + recording KG stub (VCO_ROUTER_KG_SCRIPT).
# --------------------------------------------------------------------------- #

_STUB_KG = '''\
import argparse, json, os

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hook-format", action="store_true")
    ap.add_argument("--injection-profile")
    ap.add_argument("--task-type")
    ap.add_argument("--transcript")
    a = ap.parse_args(argv)
    with open(os.environ["VCO_AGENT_STUB_OUT"], "w", encoding="utf-8") as fh:
        json.dump({"query": a.query, "limit": a.limit,
                   "profile": a.injection_profile,
                   "task_type": a.task_type}, fh)
    print("KG: Widget Reranker Pattern | concept | score=0.91 | "
          "rerank with RL scoring before capping")
'''


def _sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """(project_root, stub_out_path) with a fake VCO venv + stub producer."""
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    (root / ".claude" / "scripts").mkdir(parents=True)
    vb = root / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(sys.executable, vb / "python")
    stub_out = tmp_path / "stub_argv.json"
    stub = root / "stub_kg.py"
    stub.write_text(_STUB_KG, encoding="utf-8")
    return root, stub_out


def _env(root: Path, stub_out: Path) -> dict:
    from tests.common.child_env import child_env

    env = child_env(
        CLAUDE_PROJECT_DIR=str(root),
        VCO_ROUTER_KG_SCRIPT=str(root / "stub_kg.py"),
        VCO_AGENT_STUB_OUT=str(stub_out),
    )
    # child_env pins VCT_INSTALL_ROOT at the repo (no .venv there) — repoint
    # it at the sandbox so the wrapper's venv tier-2 resolves the fake venv.
    # VCT_ORCHESTRATOR_ROOT stays pinned at the real checkout (child_env),
    # so the REAL router runs; only the producer is stubbed.
    env["VCT_INSTALL_ROOT"] = str(root)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    return env


_BRIEF = ("@agent-implementer (Model)\n"
          "Task: implement the widget reranker with RL scoring\n"
          "Context: vco_lib/retrieval, tests under tests/\n"
          "Success Criteria: pytest green\n")
_TOOL_INPUT = {
    "prompt": _BRIEF,
    "description": "implementer lane",
    "model": "claude-gw/glm-5.3",
    "subagent_type": "implementer",
}


def _payload() -> dict:
    return {"session_id": "sess-agent-1", "prompt_id": "p-agent-1",
            "tool_name": "Agent", "cwd": "/tmp",
            "tool_input": dict(_TOOL_INPUT)}


def _parse_envelope(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert out, "expected the updatedInput envelope on stdout"
    return json.loads(out)


# --------------------------------------------------------------------------- #
# The .sh wrapper, end to end
# --------------------------------------------------------------------------- #

@needs_bash
def test_sh_wrapper_envelope_echoes_all_fields_and_modifies_only_prompt(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
        input=json.dumps(_payload()), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=60, cwd=str(root))
    env_doc = _parse_envelope(proc)

    hso = env_doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso, "never auto-approve the spawn"
    updated = hso["updatedInput"]
    # EVERY original field survives verbatim; only prompt is modified.
    for key, val in _TOOL_INPUT.items():
        if key == "prompt":
            continue
        assert updated.get(key) == val, f"field {key!r} was dropped or altered"
    assert updated["prompt"].startswith(_BRIEF), "original brief must lead"
    assert "[KG context for this task]:" in updated["prompt"]
    assert "Widget Reranker Pattern" in updated["prompt"]


@needs_bash
def test_sh_wrapper_query_is_the_task_section_with_agent_profile(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
        input=json.dumps(_payload()), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=60, cwd=str(root))
    _parse_envelope(proc)  # exit 0 + envelope before inspecting the stub
    called = json.loads(stub_out.read_text("utf-8"))
    assert called["query"] == "implement the widget reranker with RL scoring"
    assert called["profile"] == "agent_brief"
    assert called["task_type"] == "agent_brief_kg_search", (
        "WP-D: the retrieval event must be partitioned by the agent surface")


@needs_bash
def test_sh_wrapper_briefless_prompt_emits_nothing(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    payload = _payload()
    # NOTE: a non-empty `description` is a sanctioned query fallback
    # (router GLM-review nit-3), so "briefless" here means BOTH the prompt
    # and the description carry no task text.
    payload["tool_input"] = {"prompt": "", "description": "", "model": "m"}
    proc = subprocess.run(
        ["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "no envelope → the input is untouched"
    assert not stub_out.exists(), "a briefless prompt must not query the KG"


@needs_bash
def test_sh_wrapper_kill_switch_spawns_nothing(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    env = _env(root, stub_out)
    env["VCO_INJECT_PROFILE"] = "off"
    proc = subprocess.run(
        ["bash", str(HOOKS / "agent-brief-kg-inject.sh")],
        input=json.dumps(_payload()), capture_output=True, text=True,
        env=env, timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    assert not stub_out.exists()


# --------------------------------------------------------------------------- #
# The .ps1 sibling
# --------------------------------------------------------------------------- #

@needs_pwsh
def test_ps1_wrapper_envelope_echoes_all_fields_and_modifies_only_prompt(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "agent-brief-kg-inject.ps1")],
        input=json.dumps(_payload()), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=120, cwd=str(root))
    env_doc = _parse_envelope(proc)
    hso = env_doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    updated = hso["updatedInput"]
    for key, val in _TOOL_INPUT.items():
        if key == "prompt":
            continue
        assert updated.get(key) == val
    assert "[KG context for this task]:" in updated["prompt"]


@needs_pwsh
def test_ps1_wrapper_briefless_prompt_emits_nothing(tmp_path):
    root, stub_out = _sandbox(tmp_path)
    payload = _payload()
    # NOTE: a non-empty `description` is a sanctioned query fallback
    # (router GLM-review nit-3), so "briefless" here means BOTH the prompt
    # and the description carry no task text.
    payload["tool_input"] = {"prompt": "", "description": "", "model": "m"}
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "agent-brief-kg-inject.ps1")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=120, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
