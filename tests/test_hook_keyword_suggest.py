# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""End-to-end test for templates/hooks/agent-skill-keyword-suggest.sh.

We invoke the bash hook as a subprocess with a JSON payload on stdin and
assert it emits a valid hookSpecificOutput JSON envelope with an
additionalContext field mentioning the seeded agent.

POSIX-only (the hook is bash). PowerShell sibling parity is exercised by
the hook-parity gate (tests/test_check_hook_parity.py) and would need its
own PS-aware test on a Windows runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "templates" / "hooks" / "agent-skill-keyword-suggest.sh"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook is POSIX-only; .ps1 sibling covers Windows.",
)


def _write_agent(root: Path, slug: str, name: str, keywords_block: str) -> None:
    agents_dir = root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\n{keywords_block}---\n# {name}\n"
    (agents_dir / f"{slug}.md").write_text(body, encoding="utf-8")


def _run_hook(payload: dict, project_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Make sure VCT_DISABLE_HOOKS is not lingering from the parent shell.
    env.pop("VCT_DISABLE_HOOKS", None)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_hook_emits_envelope_on_match(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes, Helm]\n")
    payload = {"prompt": "review my Kubernetes manifest", "session_id": "test-1"}
    result = _run_hook(payload, tmp_path)

    assert result.returncode == 0, f"hook exited {result.returncode}; stderr={result.stderr!r}"
    assert result.stdout.strip(), f"expected JSON envelope on stdout; got empty (stderr={result.stderr!r})"
    envelope = json.loads(result.stdout)
    assert "hookSpecificOutput" in envelope
    hso = envelope["hookSpecificOutput"]
    assert hso.get("hookEventName") == "UserPromptSubmit"
    additional = hso.get("additionalContext", "")
    assert "kubernetes-agent" in additional
    assert "agent" in additional  # singular form expected


def test_hook_silent_when_no_match(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    payload = {"prompt": "write me a haiku about pasta", "session_id": "test-2"}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "", f"expected silent exit; got stdout={result.stdout!r}"


def test_hook_silent_when_disabled(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env["VCT_DISABLE_HOOKS"] = "1"
    payload = {"prompt": "review my Kubernetes manifest", "session_id": "test-3"}
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_silent_when_no_claude_dir(tmp_path):
    # Project root exists but has no `.claude/agents/` at all.
    payload = {"prompt": "review my Kubernetes manifest", "session_id": "test-4"}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_silent_on_empty_prompt(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    payload = {"prompt": "", "session_id": "test-5"}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_silent_on_missing_prompt_field(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    payload = {"session_id": "test-6"}  # no `prompt` field
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_hook_envelope_mentions_skill_too(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    skill_dir = tmp_path / ".claude" / "skills" / "tdd-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tdd-skill\nkeywords: [TDD]\n---\n# tdd-skill\n",
        encoding="utf-8",
    )
    payload = {"prompt": "apply TDD to my Kubernetes manifest", "session_id": "test-7"}
    result = _run_hook(payload, tmp_path)
    assert result.returncode == 0
    envelope = json.loads(result.stdout)
    additional = envelope["hookSpecificOutput"]["additionalContext"]
    assert "kubernetes-agent" in additional
    assert "tdd-skill" in additional


def test_hook_silent_on_malformed_json(tmp_path):
    _write_agent(tmp_path, "k", "kubernetes-agent", "keywords: [Kubernetes]\n")
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    env.pop("VCT_DISABLE_HOOKS", None)
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input="not valid json at all",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
