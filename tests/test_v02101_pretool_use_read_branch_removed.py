# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 §C2 (PLAN-V02101) — the Read injection moves OUT of
``pre-tool-use.{sh,ps1}`` into the NEW PostToolUse ``read-context-inject``
hook (one concern, one home), while the Build-Anchor reads ledger and the
injector ``seen_reads`` ledger — which same-turn Write-anchor checks and
KG/CODE suppression read — MUST keep being written PreToolUse.

Red on the base tree: the old Read branch calls ``_cg_inject`` for every
code file, so with a code-graph CLI stub that returns a block the hook
EMITS a "Code-graph context" envelope (and executes the stub). After the
removal: no envelope, no stub execution, ledgers intact.

The new ``read-context-inject`` / ``grep-context-inject`` wrappers are
smoked here too (they are the Read/Grep surfaces' new homes; the router
itself is covered by test_v02101_inject_gates.py + the bash/edit/write
lane's router-surfaces suite).
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

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS = REPO_ROOT / "templates" / "hooks"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash hook; .ps1 sibling driven separately below")
needs_pwsh = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="pwsh not installed")

_CODE_CONTENT = "def alpha_widget():\n    return 1\n"


def _hook_env(root: Path, extra: dict | None = None) -> dict:
    from tests.common.child_env import child_env

    env = child_env(CLAUDE_PROJECT_DIR=str(root))
    env["VCT_INSTALL_ROOT"] = str(root)
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    env.update(extra or {})
    return env


def _read_sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """(project_root, code_file) + a code-graph CLI stub that PROVES it ran.

    The stub lands at ``<root>/.claude/scripts/code-graph-query`` — exactly
    where ``vco_codegraph_cli()`` looks — so ANY code-graph spawn from the
    hook is visible as the marker file."""
    root = tmp_path / "proj"
    scripts = root / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (root / ".claude" / "state").mkdir(parents=True)
    marker = tmp_path / "cg_cli_ran"
    code_file = root / "src" / "widget.py"
    code_file.parent.mkdir(parents=True)
    code_file.write_text(_CODE_CONTENT, encoding="utf-8")
    cli = scripts / "code-graph-query"
    cli.write_text(
        "#!/usr/bin/env bash\n"
        f"echo x >> '{marker}'\n"
        "echo 'CODE: alpha_widget | function | def src/widget.py:1 | callers: [beta]'\n",
        encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return root, code_file


def _read_payload(code_file: Path) -> dict:
    return {"tool_name": "Read", "session_id": "sess-read-1",
            "prompt_id": "p-read-1",
            "tool_input": {"file_path": str(code_file)}}


# --------------------------------------------------------------------------- #
# pre-tool-use.sh: the Read code-graph inject branch is GONE
# --------------------------------------------------------------------------- #

@needs_bash
def test_sh_read_branch_no_longer_injects(tmp_path):
    root, code_file = _read_sandbox(tmp_path)
    marker = tmp_path / "cg_cli_ran"
    proc = subprocess.run(
        ["bash", str(HOOKS / "pre-tool-use.sh"), "Read", ""],
        input=json.dumps(_read_payload(code_file)), capture_output=True,
        text=True, env=_hook_env(root), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert "Code-graph context" not in proc.stdout, (
        "the Read surface's injection home is read-context-inject (PostToolUse); "
        "pre-tool-use.sh must not inject for Read any more")
    assert "additionalContext" not in proc.stdout
    assert not marker.exists(), (
        "removing the branch must also remove the code-graph SPAWN — a silent "
        "stub call would still pay the latency the redesign cuts")


@needs_bash
def test_sh_reads_ledgers_still_written(tmp_path):
    root, code_file = _read_sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", str(HOOKS / "pre-tool-use.sh"), "Read", ""],
        input=json.dumps(_read_payload(code_file)), capture_output=True,
        text=True, env=_hook_env(root), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    state = root / ".claude" / "state"
    anchor = (state / "reads_sess-read-1.txt").read_text("utf-8")
    assert str(code_file) in anchor, "Build-Anchor ledger (harness gate) lost"
    seen = (state / "seen_reads_sess-read-1.txt").read_text("utf-8")
    assert "src/widget.py" in seen, (
        "the injector reads ledger (repo-relative src= shape) must still be "
        "written PreToolUse so same-turn suppression sees it")


@needs_pwsh
def test_ps1_read_branch_no_longer_injects(tmp_path):
    root, code_file = _read_sandbox(tmp_path)
    marker = tmp_path / "cg_cli_ran"
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "pre-tool-use.ps1"),
         "Read", ""],
        input=json.dumps(_read_payload(code_file)), capture_output=True,
        text=True, env=_hook_env(root), timeout=120, cwd=str(root))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Code-graph context" not in proc.stdout
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# §C6: the Grep(code-symbol) injection branch is GONE too — grep-context-
# inject is its one home. Same proof shape as the Read rows: with a code-graph
# CLI stub that returns a block, the OLD branch emitted; now the hook must
# neither emit nor even SPAWN the CLI (and Grep still falls through safely).
# --------------------------------------------------------------------------- #

def _grep_payload(symbol: str) -> dict:
    return {"tool_name": "Grep", "session_id": "sess-grep-1",
            "prompt_id": "p-grep-1",
            "tool_input": {"pattern": symbol}}


@needs_bash
def test_sh_grep_branch_no_longer_injects(tmp_path):
    root, code_file = _read_sandbox(tmp_path)
    marker = tmp_path / "cg_cli_ran"
    proc = subprocess.run(
        ["bash", str(HOOKS / "pre-tool-use.sh"), "Grep", ""],
        input=json.dumps(_grep_payload("alpha_widget")), capture_output=True,
        text=True, env=_hook_env(root), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert "Code-graph context" not in proc.stdout, (
        "§C6: grep-context-inject is the Grep surface's one injection home — "
        "pre-tool-use.sh must not inject (or double-inject) for Grep")
    assert not marker.exists(), "the retired branch must not even spawn the CLI"


@needs_pwsh
def test_ps1_grep_branch_no_longer_injects(tmp_path):
    root, code_file = _read_sandbox(tmp_path)
    marker = tmp_path / "cg_cli_ran"
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "pre-tool-use.ps1"),
         "Grep", ""],
        input=json.dumps(_grep_payload("alpha_widget")), capture_output=True,
        text=True, env=_hook_env(root), timeout=120, cwd=str(root))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Code-graph context" not in proc.stdout
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# The new wrappers (read: PostToolUse, grep: PreToolUse)
# --------------------------------------------------------------------------- #

_STUB_KG = '''\
import argparse, os

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--hook-format", action="store_true")
    ap.add_argument("--injection-profile")
    ap.add_argument("--task-type")
    ap.add_argument("--transcript")
    a = ap.parse_args(argv)
    if os.environ.get("VCO_STUB_KG_MARKER"):
        with open(os.environ["VCO_STUB_KG_MARKER"], "w", encoding="utf-8") as fh:
            fh.write(a.query)
    print("KG: Widget Module Notes | concept | score=0.88 | notes body")
'''

_STUB_CG = '''\
import argparse

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("subcommand")
    ap.add_argument("kind")
    ap.add_argument("target")
    ap.add_argument("--hook-format", action="store_true")
    ap.add_argument("--source-file")
    ap.add_argument("--exclude-file")
    ap.add_argument("--indexed-revision", action="store_true")
    a = ap.parse_args(argv)
    print("CODE: alpha_widget | function | def src/widget.py:1 | callers: [beta]")
'''


def _wrapper_sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "widget.py").write_text(_CODE_CONTENT, encoding="utf-8")
    vb = root / ".venv" / "bin"
    vb.mkdir(parents=True)
    os.symlink(sys.executable, vb / "python")
    (root / "stub_kg.py").write_text(_STUB_KG, encoding="utf-8")
    (root / "stub_cg.py").write_text(_STUB_CG, encoding="utf-8")
    return root


def _wrapper_env(root: Path, kg_marker: str = "") -> dict:
    from tests.common.child_env import child_env

    env = child_env(
        CLAUDE_PROJECT_DIR=str(root),
        VCO_ROUTER_KG_SCRIPT=str(root / "stub_kg.py"),
        VCO_CG_SCRIPT=str(root / "stub_cg.py"),
    )
    env["VCT_INSTALL_ROOT"] = str(root)
    if kg_marker:
        env["VCO_STUB_KG_MARKER"] = kg_marker
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCO_INJECT_PROFILE", None)
    return env


@needs_bash
def test_sh_read_context_inject_wrapper_emits_posttooluse(tmp_path):
    root = _wrapper_sandbox(tmp_path)
    code_file = root / "src" / "widget.py"
    payload = {"tool_name": "Read", "session_id": "sess-r2",
               "prompt_id": "p-r2", "cwd": str(root),
               "tool_input": {"file_path": str(code_file)},
               "tool_response": {"content": _CODE_CONTENT}}
    proc = subprocess.run(
        ["bash", str(HOOKS / "read-context-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_wrapper_env(root), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "expected an additionalContext envelope"
    doc = json.loads(proc.stdout)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse", "§C2: Read is PostToolUse"
    ctx = hso["additionalContext"]
    assert "KG: Widget Module Notes" in ctx, "the KG leg ran through the router"
    assert "CODE: alpha_widget" in ctx, "the exact-symbol CG leg ran"


@needs_bash
def test_sh_grep_context_inject_wrapper_emits_pretooluse_no_kg(tmp_path):
    root = _wrapper_sandbox(tmp_path)
    kg_marker = str(tmp_path / "kg_ran")
    payload = {"tool_name": "Grep", "session_id": "sess-g1",
               "prompt_id": "p-g1", "cwd": str(root),
               "tool_input": {"pattern": "alpha_widget", "path": str(root)}}
    proc = subprocess.run(
        ["bash", str(HOOKS / "grep-context-inject.sh")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_wrapper_env(root, kg_marker=kg_marker), timeout=60, cwd=str(root))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip()
    doc = json.loads(proc.stdout)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "CODE: alpha_widget" in hso["additionalContext"]
    assert not Path(kg_marker).exists(), "§2.1: the Grep surface runs NO KG leg"


@needs_pwsh
def test_ps1_read_context_inject_wrapper_emits_posttooluse(tmp_path):
    root = _wrapper_sandbox(tmp_path)
    code_file = root / "src" / "widget.py"
    payload = {"tool_name": "Read", "session_id": "sess-r2p",
               "prompt_id": "p-r2p", "cwd": str(root),
               "tool_input": {"file_path": str(code_file)},
               "tool_response": {"content": _CODE_CONTENT}}
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(HOOKS / "read-context-inject.ps1")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_wrapper_env(root), timeout=120, cwd=str(root))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip()
    doc = json.loads(proc.stdout)
    hso = doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "CODE: alpha_widget" in hso["additionalContext"]
