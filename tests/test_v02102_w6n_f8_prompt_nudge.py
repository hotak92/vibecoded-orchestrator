# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-N F-8 (owner ruling 2026-10-12).

1. ``post-file-edit`` (both siblings) emits no after-edit workflow nudge at
   all: the "Run /workflow-optimizer" line (no such skill ships) went in
   F-8, and the rest of the nudge -- "[Workflow file edited]" with its
   skills-setup-guide.md pointer, and the CONTEXT_STATE "expert-skill
   review" pointer at a never-shipped skill file -- in the review round
   (owner-approved removal 2026-10-12).
2. BEFORE an Edit/Write of a prompt-shaped file (agent definition, skill,
   prompt template) the pre-edit / pre-write context router suggests the
   project's ENABLED prompt-authoring agent, as a ``NOTE:`` block deduped by
   the session seen-store — at most once per session per file, cache replays
   included (the shell and PowerShell filters parse NOTE too).
"""
from __future__ import annotations

import io
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
HOOKS_LIB = HOOKS / "_lib"
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers" / "scripts"))


def _project(tmp_path: Path, *, agent: str | None = "agent-author",
             disabled: bool = False) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "agents").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    if agent:
        where = "agents.disabled" if disabled else "agents"
        (proj / ".claude" / where).mkdir(parents=True, exist_ok=True)
        (proj / ".claude" / where / f"{agent}.md").write_text(
            f"---\nname: {agent}\n---\n", encoding="utf-8")
    return proj


# ── 1. removal pinned (both siblings, run for real) ───────────────────────


def _run_post_file_edit(cmd: list, proj: Path, edited: Path) -> str:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    for k in ("VCT_DISABLE_HOOKS", "VCT_VENV"):
        env.pop(k, None)
    payload = {"tool_name": "Edit", "session_id": "sess-f8",
               "tool_input": {"file_path": str(edited)}}
    proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
def test_post_file_edit_emits_no_after_edit_workflow_nudge(tmp_path,
                                                            sibling):
    if sibling == "sh":
        cmd = ["bash", str(HOOKS / "post-file-edit.sh")]
    else:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("pwsh not installed")
        cmd = [pwsh, *PWSH_BASE_FLAGS, "-File", str(HOOKS / "post-file-edit.ps1")]
    proj = _project(tmp_path)
    skill = proj / ".claude" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\n---\n", encoding="utf-8")
    hook = proj / ".claude" / "hooks" / "demo.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    # The CONTEXT_STATE arm fired only when this (never shipped) skill file
    # existed and the state file carried > 5 markers: arrange both, so the
    # removal is proven on the arm's own trigger.
    expert = (proj / ".claude" / "skills" / "project-experts"
              / "claude-orchestrator-expert.md")
    expert.parent.mkdir(parents=True)
    expert.write_text("x\n", encoding="utf-8")
    state = proj / ".claude" / "CONTEXT_STATE.md"
    state.write_text("".join(f"## Status\n- \u2705 item {i}\n"
                             for i in range(8)), encoding="utf-8")
    for edited in (skill, hook, state):
        out = _run_post_file_edit(cmd, proj, edited)
        # Owner-approved removal 2026-10-12 (W6-N review round): the WHOLE
        # after-edit workflow nudge is gone, not just its
        # /workflow-optimizer line.
        for gone in ("workflow-optimizer", "[Workflow file edited]",
                     "skills-setup-guide.md", "expert-skill review",
                     "claude-orchestrator-expert.md"):
            assert gone not in out, (edited.name, gone, out)


# ── 2a. classification + presence (the one home) ─────────────────────────


@pytest.mark.parametrize("rel, expected", [
    (".claude/agents/code-reviewer.md", True),
    (".claude/agents/team/sub-agent.md", True),
    (".claude/skills/fix-issue/SKILL.md", True),
    (".claude/commands/review.md", True),
    # The VCO-repo-only templates/**/agents rule was dropped (W6-N review
    # MEDIUM-2): it described this repo, not an ordinary project.
    ("templates/agents/free/expert-coder.md", False),
    ("templates/packs/x/skills/y/SKILL.md", True),
    ("prompts/system.j2", True),
    ("src/summarize.prompt.md", True),
    ("README.md", False),
    ("templates/agents/README.md", False),
    ("docs/agents.md", False),
    (".claude/skills/fix-issue/helper.py", False),
    ("src/app.py", False),
    ("knowledge/concepts/prompt-engineering-fundamentals.md", False),
])
def test_prompt_shaped_classification(rel, expected, tmp_path):
    from vco_lib.prompt_edit_nudge import is_prompt_shaped
    assert is_prompt_shaped(str(tmp_path / rel), str(tmp_path)) is expected
    assert is_prompt_shaped(rel, "") is expected


def test_enabled_agent_resolution(tmp_path):
    from vco_lib.prompt_edit_nudge import enabled_prompt_agent
    assert enabled_prompt_agent(str(_project(tmp_path / "a"))) == "agent-author"
    assert enabled_prompt_agent(str(_project(
        tmp_path / "b", agent="prompt-engineer"))) == "prompt-engineer"
    assert enabled_prompt_agent(str(_project(tmp_path / "c", agent=None))) is None
    assert enabled_prompt_agent(str(_project(
        tmp_path / "d", disabled=True))) is None


# ── 2b. the router surface (in-process; retrieval legs stubbed) ──────────


def _drive(monkeypatch, proj: Path, surface: str, file_path: Path,
           sid: str = "sess-f8") -> str:
    import hook_context_router as router_mod

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.delenv("VCO_INJECT_PROFILE", raising=False)
    monkeypatch.setattr(router_mod, "_run_legs", lambda *a, **k: ("", ""))
    tool = "Edit" if surface == "edit" else "Write"
    tool_input = {"file_path": str(file_path)}
    if surface == "edit":
        tool_input.update(old_string="a", new_string="b")
    else:
        tool_input["content"] = "x"
    payload = {"session_id": sid, "prompt_id": "p1", "tool_name": tool,
               "cwd": str(proj), "tool_input": tool_input}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    try:
        assert router_mod.main([surface]) == 0
    finally:
        monkeypatch.undo()
    return buf.getvalue()


@pytest.mark.parametrize("surface", ["edit", "write"])
@pytest.mark.parametrize("rel", [".claude/agents/code-reviewer.md",
                                 ".claude/skills/demo/SKILL.md"])
def test_nudge_before_editing_agent_and_skill_files(tmp_path, monkeypatch,
                                                    surface, rel):
    proj = _project(tmp_path)
    out = _drive(monkeypatch, proj, surface, proj / rel)
    assert f"NOTE: prompt-shaped file {rel} | agent=agent-author" in out, out
    assert "`agent-author` agent" in out


@pytest.mark.parametrize("surface", ["edit", "write"])
def test_no_nudge_for_other_files(tmp_path, monkeypatch, surface):
    proj = _project(tmp_path)
    out = _drive(monkeypatch, proj, surface, proj / "src" / "app.py")
    assert "NOTE:" not in out, out


@pytest.mark.parametrize("disabled", [False, True], ids=["absent", "disabled"])
def test_no_nudge_without_an_enabled_prompt_agent(tmp_path, monkeypatch,
                                                  disabled):
    proj = _project(tmp_path, agent="agent-author" if disabled else None,
                    disabled=disabled)
    out = _drive(monkeypatch, proj, "edit",
                 proj / ".claude" / "agents" / "x.md")
    assert "NOTE:" not in out, out


def test_nudge_is_deduped_per_session_per_file(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    a = proj / ".claude" / "agents" / "a.md"
    b = proj / ".claude" / "agents" / "b.md"
    assert "NOTE:" in _drive(monkeypatch, proj, "edit", a)
    assert "NOTE:" not in _drive(monkeypatch, proj, "write", a)  # same file
    assert "NOTE:" in _drive(monkeypatch, proj, "edit", b)       # other file
    assert "NOTE:" in _drive(monkeypatch, proj, "edit", a, sid="sess-other")


def test_untrustworthy_session_gets_no_nudge(tmp_path, monkeypatch):
    """Without a session store 'at most once' cannot hold — stay silent."""
    proj = _project(tmp_path)
    out = _drive(monkeypatch, proj, "edit",
                 proj / ".claude" / "agents" / "a.md", sid="default")
    assert "NOTE:" not in out, out


# ── 2c. the cache-replay filters dedupe NOTE identically (py / sh / ps1) ──


_NOTE = ("NOTE: prompt-shaped file .claude/agents/a.md | agent=agent-author\n"
         "  Consider delegating.\n")


def _sh_filter(tmp_path: Path, corpus: str, inject: Path) -> str:
    payload = tmp_path / "corpus.txt"
    payload.write_text(corpus, encoding="utf-8")
    py = shutil.which("python3") or sys.executable
    script = (f'export PY="{py}"\nexport PROJECT_ROOT="{tmp_path}"\n'
              f'. "{HOOKS_LIB}/seen-store.sh"\n'
              f'vco_filter_seen_blocks "$(cat "{payload}")" "{inject}" ""\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       timeout=60, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    return r.stdout


def _ps1_filter(tmp_path: Path, corpus: str, inject: Path) -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    payload = tmp_path / "corpus_ps.txt"
    payload.write_text(corpus, encoding="utf-8")
    cmd = (f". '{HOOKS_LIB}/seen-store.ps1'; "
           f"$t = [IO.File]::ReadAllText('{payload}'); "
           f"Invoke-VcoFilterSeenBlocks -InputText $t -InjectFile '{inject}' "
           f"-ReadsFile ''")
    r = subprocess.run([pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command",
                        cmd], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.parametrize("impl", ["py", "sh", "ps1"])
def test_replay_filters_dedupe_note_blocks(tmp_path, impl):
    from hook_context_router import filter_seen_blocks
    inject = tmp_path / f"inject_{impl}.txt"
    corpus = _NOTE + "KG: Node | concept | score=0.9 | body\n"

    def run() -> str:
        if impl == "py":
            return filter_seen_blocks(corpus, str(inject), "", str(tmp_path))
        if impl == "sh":
            return _sh_filter(tmp_path, corpus, inject)
        return _ps1_filter(tmp_path, corpus, inject)

    first = run()
    assert "NOTE: prompt-shaped file .claude/agents/a.md" in first, first
    assert "Consider delegating." in first
    second = run()
    assert "NOTE:" not in second and "Consider delegating." not in second, second
    keys = inject.read_text(encoding="utf-8").splitlines()
    assert "prompt-shaped file .claude/agents/a.md" in keys, keys
