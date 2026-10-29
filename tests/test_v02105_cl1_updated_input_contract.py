# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 CL-1 — pin the ``updatedInput`` contract of the two hooks that
rewrite a tool call's input (read-only check done 2026-10-18, int 5b896ee0:
"OK by construction"; this file makes it a regression test).

Claude Code 2.1.300 validates a PreToolUse ``updatedInput`` more strictly
than earlier versions, so the contract is pinned at the hooks' OBSERVABLE
output — stdout of the real hook script, no internals — and therefore
survives internal rework of either hook (e.g. new decision stages):

* ``updatedInput`` contains NO keys beyond the original ``tool_input``
  (a dropped field breaks the dispatch; an ADDED one is what 2.1.300's
  validation could drop or reject);
* every value's JSON type is unchanged, recursively (a bool flipped to a
  string, a nested dict flattened — same defect class);
* exactly the sanctioned field is modified: ``command`` for
  ``lean-ctx-rewrite`` (rewritten to the tee wrapper, still a string),
  ``prompt`` for ``agent-brief-kg-inject`` (the KG context block is
  appended, original brief leading, still a string).

Both hooks are driven end to end: the lean-ctx legs reuse the behavioural
rig of ``tests/test_v02101_lean_ctx_allowlist_tee.py`` (fake lean-ctx
binary, marked project); the agent-brief legs reuse the sandbox of
``tests/test_v02101_agent_brief_inject.py`` (real router, stubbed KG
producer). The ``.ps1`` siblings run where pwsh is installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash hook; .ps1 sibling driven separately below")
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed")

#: Fields of every JSON shape a real tool_input carries beside the
#: rewritten one — the contract must hold for all of them at once.
EXTRA_FIELDS = {
    "description": "install the dependencies",
    "run_in_background": True,
    "timeout": 120000,
    "ratio": 2.5,
    "meta": {"nested": [1, 2.5, None, True, "étagère"], "flag": False},
    "tags": ["build", "deps"],
    "origin": None,
}

_BRIEF = ("@agent-implementer (Model)\n"
          "Task: implement the widget reranker with RL scoring\n"
          "Context: vco_lib/retrieval, tests under tests/\n"
          "Success Criteria: pytest green\n")


def _type_skeleton(value):
    """The JSON type shape of ``value``, recursively (bool before int —
    ``isinstance(True, int)`` is True in Python)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    if isinstance(value, list):
        return ["list", *(_type_skeleton(v) for v in value)]
    if isinstance(value, dict):
        return {"dict": {k: _type_skeleton(v) for k, v in value.items()}}
    raise AssertionError(f"non-JSON value in tool_input: {value!r}")


def _assert_contract(updated: dict, original: dict, mutated_key: str) -> None:
    """No keys beyond the original; types unchanged; every other value
    verbatim. The mutated key's type is still checked (str in str out)."""
    assert set(updated) == set(original), (
        f"updatedInput keys diverge from tool_input: "
        f"added={sorted(set(updated) - set(original))} "
        f"dropped={sorted(set(original) - set(updated))}")
    for key in original:
        assert _type_skeleton(updated[key]) == _type_skeleton(original[key]), (
            f"field {key!r} changed JSON type: "
            f"{_type_skeleton(original[key])} -> {_type_skeleton(updated[key])}")
        if key != mutated_key:
            assert updated[key] == original[key], f"field {key!r} was altered"
    assert isinstance(updated[mutated_key], str), (
        f"{mutated_key!r} must stay a string")


def _updated_input(stdout: str) -> dict:
    doc = json.loads(stdout)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso, "never auto-approve the call"
    return hso["updatedInput"]


# ─── lean-ctx-rewrite: command replaced, everything else frozen ───────────


@needs_bash
def test_lean_ctx_sh_updated_input_is_the_original_tool_input(tmp_path):
    from tests.test_v02101_lean_ctx_allowlist_tee import _run_sh_hook

    original = {"command": "npm install", **EXTRA_FIELDS}
    res = _run_sh_hook("npm install", tmp_path, extra_tool_input=EXTRA_FIELDS)
    assert res.stdout.strip(), "wrap expected for an allow-listed command"
    updated = _updated_input(res.stdout)
    _assert_contract(updated, original, mutated_key="command")
    assert updated["command"] != original["command"], (
        "the command must be rewritten to the tee wrapper")


@needs_pwsh
def test_lean_ctx_ps1_updated_input_is_the_original_tool_input(tmp_path):
    from tests.test_v02101_lean_ctx_allowlist_tee import _run_ps1_hook

    original = {"command": "npm install", **EXTRA_FIELDS}
    res = _run_ps1_hook("npm install", tmp_path, extra_tool_input=EXTRA_FIELDS)
    assert res.stdout.strip(), "wrap expected for an allow-listed command"
    updated = _updated_input(res.stdout)
    _assert_contract(updated, original, mutated_key="command")
    assert updated["command"] != original["command"]


# ─── agent-brief-kg-inject: prompt extended, everything else frozen ───────


def _agent_brief_run(hook: str, tmp_path: Path) -> dict:
    from tests.test_v02101_agent_brief_inject import _env, _sandbox

    root, stub_out = _sandbox(tmp_path)
    original = {"prompt": _BRIEF, **EXTRA_FIELDS}
    payload = {"session_id": "sess-cl1", "prompt_id": "p-cl1",
               "tool_name": "Agent", "cwd": str(root),
               "tool_input": original}
    argv = (["bash", str(HOOKS / hook)] if hook.endswith(".sh")
            else ["pwsh", *PWSH_BASE_FLAGS, "-File", str(HOOKS / hook)])
    proc = subprocess.run(
        argv, input=json.dumps(payload), capture_output=True, text=True,
        env=_env(root, stub_out), timeout=120, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "expected the updatedInput envelope"
    return _updated_input(proc.stdout), original


@needs_bash
def test_agent_brief_sh_updated_input_is_the_original_tool_input(tmp_path):
    updated, original = _agent_brief_run("agent-brief-kg-inject.sh", tmp_path)
    _assert_contract(updated, original, mutated_key="prompt")
    assert updated["prompt"].startswith(_BRIEF), "the original brief must lead"
    assert "[KG context for this task]:" in updated["prompt"]


@needs_pwsh
def test_agent_brief_ps1_updated_input_is_the_original_tool_input(tmp_path):
    updated, original = _agent_brief_run("agent-brief-kg-inject.ps1", tmp_path)
    _assert_contract(updated, original, mutated_key="prompt")
    assert updated["prompt"].startswith(_BRIEF), "the original brief must lead"
    assert "[KG context for this task]:" in updated["prompt"]
