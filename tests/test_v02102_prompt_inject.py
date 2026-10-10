# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-F item 1 — the shipped "search the KG on every prompt"
promise gets a real surface.

Pre-fix (research report 2026-10-10, item 1 — verified UNWIRED): README.md
("On every prompt (UserPromptSubmit hook): Searches the Knowledge Graph …
Injects matches") and docs/GETTING_STARTED.md promise prompt-time KG
injection, but no UserPromptSubmit hook queried anything and the context
router's SURFACES had no prompt entry.

Post-fix contract pinned here:

* ``vco_lib.inject_intent.prompt_query`` (the ONE home for the query rule):
  the user's prompt text, whitespace-collapsed and capped; "" for empty /
  trivially short / slash-command / bang-passthrough prompts.
* The router grows a ``prompt`` surface running the KG leg under the
  ``prompt`` §2.1 profile (floor + titles-only tier below 0.85, like the
  other weak free-text surfaces), the RL ``prompt_kg_search`` task type,
  the seen-store dedupe, the per-injection cap and the kill switches — the
  same machinery as every other surface (no second injection path).
* ``templates/hooks/prompt-context-inject.{sh,ps1}`` are thin wrappers like
  the read/edit ones, registered on UserPromptSubmit in BOTH settings
  templates.
* A repeated identical prompt in one session injects once (seen-store).

RED on the pre-fix tree: the wrappers and the profile do not exist.
"""
from __future__ import annotations

from tests.common.child_env import child_env
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="bash hook")
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed")

_KG_STUB = '''\
import argparse, json, os

def main():
    rec = os.environ.get("VCO_STUB_KG_RECORD", "")
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    ns = p.parse_args()
    if rec:
        with open(rec, "a", encoding="utf-8") as fh:
            json.dump(ns.rest, fh)
            fh.write("\\n")
    body = "KG: Stub Node | concept | score=0.90 | TITLES"
    if os.environ.get("VCO_STUB_KG_LONG"):
        body += "\\n" + ("long body line\\n" * 400)
    print(body)
    return 0
'''


class Rig:
    """Fake project + fake venv whose python runs the REAL router
    (VCT_ORCHESTRATOR_ROOT=checkout) with a STUB KG producer — the same rig
    shape as tests/test_v02101_router_surfaces.py."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.proj = tmp_path / "proj"
        (self.proj / ".claude" / "state").mkdir(parents=True)
        venv = tmp_path / "orch" / ".venv"
        (venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, venv / "bin" / "python")
        self.kg_stub = tmp_path / "kg_stub.py"
        self.kg_stub.write_text(_KG_STUB, encoding="utf-8")
        self.kg_record = tmp_path / "kg_records.jsonl"

    def env(self, **overrides: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("VCT_DISABLE_HOOKS", "VCT_INSTALL_ROOT",
                            "VCT_ORCHESTRATOR_ROOT", "VCO_INJECT_PROFILE",
                            "VCO_RL_TASK_TYPE", "VCT_VENV", "VCO_STUB_KG_LONG")}
        env.update({
            "CLAUDE_PROJECT_DIR": str(self.proj),
            "VCT_ORCHESTRATOR_ROOT": str(REPO_ROOT),
            "VCT_VENV": str(self.tmp / "orch" / ".venv"),
            "VCO_ROUTER_KG_SCRIPT": str(self.kg_stub),
            "VCO_STUB_KG_RECORD": str(self.kg_record),
            "VCT_STATE_DIR": str(self.tmp / "vctstate"),
            "HOME": str(self.tmp / "home"),
            "KG_COLLECTION": "PromptProjKG",
            "RL_HUB_POST_DISABLED": "1",
        })
        env.update(overrides)
        return env

    def run(self, prompt: str, session: str = "sess-pi-1", shell: str = "sh",
            env_overrides: dict | None = None) -> subprocess.CompletedProcess:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "cwd": str(self.proj),
            "prompt": prompt,
        }
        env = self.env(**(env_overrides or {}))
        if shell == "ps1":
            argv = ["pwsh", *PWSH_BASE_FLAGS, "-File",
                    str(HOOKS / "prompt-context-inject.ps1")]
        else:
            argv = ["bash", str(HOOKS / "prompt-context-inject.sh")]
        return subprocess.run(
            argv, input=json.dumps(payload), capture_output=True, text=True,
            timeout=120, cwd=str(self.proj), env=env,
        )

    def kg_records(self) -> list:
        if not self.kg_record.exists():
            return []
        return [json.loads(ln)
                for ln in self.kg_record.read_text("utf-8").splitlines()
                if ln.strip()]


def _envelope(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON envelope in stdout: {stdout!r}")


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- the query rule (pure core) -------------------------------------------------


def test_prompt_query_rule():
    from vco_lib.inject_intent import prompt_query

    assert prompt_query("") == ""
    assert prompt_query("   \n  ") == ""
    assert prompt_query("/compact focus on hooks") == "", "slash commands skip"
    assert prompt_query("!ls -la") == "", "bang shell passthroughs skip"
    assert prompt_query("ok") == "", "trivially short prompts skip"
    assert prompt_query("yes, continue") == ""
    q = prompt_query("How does the KG nudge threshold interact with compaction?")
    assert q.startswith("How does the KG nudge")
    # whitespace collapses to single spaces; the query is capped
    q2 = prompt_query("line one\n\nline two\twith\tspaces " + "x" * 500)
    assert "\n" not in q2 and "\t" not in q2
    assert len(q2) <= 400


def test_prompt_profile_is_registered():
    from vco_lib.inject_intent import cg_policy, kg_gate, task_type_for

    gate = kg_gate("prompt")
    assert gate is not None, "the prompt surface needs a KG gate (§2.1 row)"
    assert gate.max_rows == 3
    assert not cg_policy("prompt").enabled, (
        "no semantic code-graph queries from injection surfaces (§2.1)"
    )
    assert task_type_for("prompt") == "prompt_kg_search"


# --- the driven wrapper ---------------------------------------------------------


@needs_bash
class TestPromptWrapperSh:
    def test_prompt_injects_kg_context(self, rig: Rig) -> None:
        prompt = "How does the KG nudge threshold interact with compaction?"
        r = rig.run(prompt)
        assert r.returncode == 0, r.stderr[-800:]
        kg = rig.kg_records()
        assert kg, f"the KG leg must run for a real prompt: {r.stderr[-500:]}"
        argv = kg[0]
        assert argv[0] == prompt, f"query = the prompt text verbatim: {argv!r}"
        assert "--injection-profile" in argv
        assert argv[argv.index("--injection-profile") + 1] == "prompt"
        assert argv[argv.index("--task-type") + 1] == "prompt_kg_search"
        assert "--hook-format" in argv
        env = _envelope(r.stdout)
        assert env["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "permissionDecision" not in json.dumps(env)
        assert "KG: Stub Node" in env["hookSpecificOutput"]["additionalContext"]

    def test_short_prompt_is_silent(self, rig: Rig) -> None:
        r = rig.run("ok")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_slash_command_is_silent(self, rig: Rig) -> None:
        r = rig.run("/compact focus on the injection redesign work")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_bang_passthrough_is_silent(self, rig: Rig) -> None:
        r = rig.run("!git status --short --branch --untracked-files")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_kill_switch_profile_off(self, rig: Rig) -> None:
        r = rig.run("How does the KG nudge threshold interact with compaction?",
                    env_overrides={"VCO_INJECT_PROFILE": "off"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_disable_hooks_switch(self, rig: Rig) -> None:
        r = rig.run("How does the KG nudge threshold interact with compaction?",
                    env_overrides={"VCT_DISABLE_HOOKS": "1"})
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == ""
        assert rig.kg_records() == []

    def test_repeat_prompt_injects_once_per_session(self, rig: Rig) -> None:
        prompt = "How does the KG nudge threshold interact with compaction?"
        r1 = rig.run(prompt)
        assert "KG: Stub Node" in r1.stdout
        r2 = rig.run(prompt)
        assert "KG: Stub Node" not in r2.stdout, (
            "the seen-store suppresses a repeat injection in one session"
        )

    def test_per_injection_cap_marks_the_cut(self, rig: Rig) -> None:
        r = rig.run("How does the KG nudge threshold interact with compaction?",
                    env_overrides={"VCO_STUB_KG_LONG": "1"})
        assert r.returncode == 0, r.stderr
        ctx = _envelope(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "cut to 2500 chars" in ctx, ctx[-200:]


@needs_pwsh
def test_prompt_wrapper_ps1_parity(rig: Rig) -> None:
    prompt = "How does the KG nudge threshold interact with compaction?"
    r = rig.run(prompt, shell="ps1")
    assert r.returncode == 0, r.stderr[-800:]
    env = _envelope(r.stdout)
    assert env["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "KG: Stub Node" in env["hookSpecificOutput"]["additionalContext"]
    assert rig.kg_records(), "the .ps1 wrapper must reach the KG leg"


# --- the registration -----------------------------------------------------------


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_prompt_surface_registered_on_userpromptsubmit(os_name: str) -> None:
    ext = "sh" if os_name == "linux" else "ps1"
    doc = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    cmds = [
        h.get("command", "")
        for g in doc["hooks"].get("UserPromptSubmit", [])
        for h in g.get("hooks", [])
    ]
    mine = [c for c in cmds if f"prompt-context-inject.{ext}" in c]
    assert len(mine) == 1, (
        f"{os_name}: prompt-context-inject.{ext} must be registered on "
        f"UserPromptSubmit exactly once; found {mine!r}"
    )


@pytest.mark.parametrize("os_name", sorted(TEMPLATES))
def test_prompt_registration_migrates_into_existing_installs(os_name: str) -> None:
    """The settings merge delivers the new UserPromptSubmit registration to
    an install whose file predates it — exactly once, idempotently."""
    from vco_lib import settings_merge

    ext = "sh" if os_name == "linux" else "ps1"
    template = json.loads(TEMPLATES[os_name].read_text(encoding="utf-8"))
    user_group = {
        "hooks": [
            {"type": "command",
             "command": (f'bash "${{CLAUDE_PROJECT_DIR:-.}}/.claude/hooks/'
                         f'user-prompt-submit-reminder.{ext}"' if ext == "sh" else
                         f'powershell -NoProfile -ExecutionPolicy Bypass -File '
                         f'"${{CLAUDE_PROJECT_DIR}}/.claude/hooks/'
                         f'user-prompt-submit-reminder.{ext}"'),
             "timeout": 2},
        ]
    }
    user = {"hooks": {"UserPromptSubmit": [user_group]}}
    once = settings_merge.smart_merge_settings(user, template)
    cmds = [
        h.get("command", "")
        for g in once["hooks"]["UserPromptSubmit"]
        for h in g.get("hooks", [])
    ]
    assert any(f"prompt-context-inject.{ext}" in c for c in cmds), cmds
    twice = settings_merge.smart_merge_settings(once, template)
    assert twice["hooks"]["UserPromptSubmit"] == once["hooks"]["UserPromptSubmit"]


# --- the KG leg's thread-import regression (found along the way) ----------------


@needs_bash
def test_kg_leg_survives_a_thread_hostile_producer_import(tmp_path: Path) -> None:
    """REGRESSION (W6-F, found wiring item 1): the router runs legs in
    worker threads; the real KG producer (rl_kg_search.main) imports
    weaviate_mcp.server LAZILY, and that module registers a SIGHUP handler
    at IMPORT time (server.py:282 -> _lib/sighup_handler.py:85) — illegal in
    a non-main thread. CG-bearing surfaces masked it (query_code_graph.py
    imports weaviate_mcp.server at module level and the router loads that
    module main-thread); a KG-ONLY plan (agent, prompt, a bash plan whose
    CG leg gated off) died on the import and silently never injected. The
    router now warms the import in the main thread for the shipped producer.

    Driven with the REAL rl_kg_search producer against the unroutable
    sentinel backend: pre-fix stderr carries "main thread" (the leg died on
    the import); post-fix the leg reaches the backend layer and soft-fails
    there instead. Requires the venv's weaviate stack — skips cleanly when
    it is not importable."""
    probe = subprocess.run(
        [sys.executable, "-c", "import weaviate, weaviate_mcp.server"],
        capture_output=True, text=True,
        env=child_env(),
    )
    if probe.returncode != 0:
        pytest.skip("weaviate stack not importable in this interpreter")
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    payload = json.dumps({
        "hook_event_name": "UserPromptSubmit", "session_id": "thr-1",
        "cwd": str(proj),
        "prompt": "How does the KG nudge threshold interact with compaction?",
    })
    env = child_env({k: v for k, v in os.environ.items()
                     if k not in ("VCT_DISABLE_HOOKS", "VCO_INJECT_PROFILE")})
    env.update({
        "CLAUDE_PROJECT_DIR": str(proj),
        "VCT_STATE_DIR": str(tmp_path / "vctstate"),
        "HOME": str(tmp_path / "home"),
        "KG_COLLECTION": "ThreadChkKG",
        "WEAVIATE_URL": "http://127.0.0.1:9",   # unroutable sentinel
        "OLLAMA_URL": "http://127.0.0.1:9",
        "RL_HUB_POST_DISABLED": "1",
    })
    r = subprocess.run(
        [sys.executable,
         str(REPO_ROOT / "claude_mcp_servers/scripts/hook_context_router.py"),
         "prompt"],
        input=payload, capture_output=True, text=True, timeout=120,
        cwd=str(proj), env=env,
    )
    assert r.returncode == 0
    assert "main thread" not in r.stderr, (
        "the KG leg's weaviate_mcp import ran in a worker thread: "
        f"{r.stderr[-400:]}"
    )
    assert "leg failed" in r.stderr and "Weaviate" in r.stderr, (
        "the leg should reach the backend layer and soft-fail there: "
        f"{r.stderr[-400:]}"
    )
