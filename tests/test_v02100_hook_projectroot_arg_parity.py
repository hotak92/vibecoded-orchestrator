# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W5R-01: every hook-helper call that needs the project root passes it, in BOTH shells.

v0.2.100 (9be49441) dropped ``-ProjectRoot $ProjectRoot`` from 15 PowerShell
call sites. A declared ``[string]$ProjectRoot`` parameter that is not passed
binds to EMPTY inside the function and shadows the caller's variable, so
``Get-VcoSeenStorePath`` / ``Get-VcoCgInjectCountPath`` / ``Take-Snapshot`` /
``Invoke-VcoBashWriteParser`` all took their "no store" branch: on Windows the
seen-store dedupe, the code-graph inject cap, the reads ledger, the pre-bash
write anchor and the subagent snapshot silently stopped. The .sh siblings were
unaffected, so the hook body-parity gate stayed green.

Two guards:

1. STATIC, both shells: the set of helpers that need a project root is DERIVED
   from the helper libraries themselves (a PowerShell ``[string]$ProjectRoot``
   parameter without a default; a shell positional bound to ``proot`` /
   ``project_root``), and every call of one of them anywhere under
   ``templates/hooks`` must pass it.
2. BEHAVIOURAL, PowerShell: the real ``pre-edit-context-inject.ps1`` runs twice
   in one session against a stub producer; the second run must be suppressed by
   the seen-store. Dropping the argument again turns this red.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "templates" / "hooks"
LIB = HOOKS / "_lib"


# --------------------------------------------------------------------------
# PowerShell side
# --------------------------------------------------------------------------

_PS_FUNC = re.compile(r"^function\s+([A-Za-z][\w-]*)\s*(?:\(([^)]*)\))?\s*\{", re.M)


def _ps_param_block(text: str, start: int) -> str:
    """Return the ``param(...)`` text of the function body starting at *start*."""
    # param() must be the body's FIRST statement (comments allowed before it);
    # a function without one must not borrow the next function's block.
    m = re.compile(r"\s*(?:#[^\n]*\n\s*)*param\s*\(", re.I).match(text, start)
    if not m:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[m.end(): i - 1]


def _ps_helpers_needing_root() -> set[str]:
    names: set[str] = set()
    for lib in LIB.glob("*.ps1"):
        text = lib.read_text(encoding="utf-8-sig")
        for m in _PS_FUNC.finditer(text):
            params = m.group(2) if m.group(2) is not None else _ps_param_block(text, m.end())
            # A ProjectRoot parameter WITHOUT a default: an omitted argument
            # binds to "" and shadows the caller's variable.
            if re.search(r"\[string\]\s*\$ProjectRoot\b(?!\s*=)", params):
                names.add(m.group(1))
    return names


def _ps_statements(text: str):
    """Yield (lineno, logical statement) with backtick continuations joined."""
    buf, first = "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if not buf:
            first = n
        if code.rstrip().endswith("`"):
            buf += code.rstrip()[:-1] + " "
            continue
        buf += code
        yield first, buf
        buf = ""


def _ps_call_sites(name: str):
    call = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    for f in sorted(HOOKS.rglob("*.ps1")):
        text = f.read_text(encoding="utf-8-sig")
        for lineno, stmt in _ps_statements(text):
            for m in call.finditer(stmt):
                before = stmt[: m.start()]
                if re.search(r"(function|Get-Command)\s+$", before, re.I):
                    continue
                if re.search(r"['\"][^'\"]*$", before):  # inside a string literal
                    continue
                # The call's own argument run ends at a pipe, a closing paren
                # that closes the call, or a statement separator.
                tail = stmt[m.end():]
                depth, end = 0, len(tail)
                for i, ch in enumerate(tail):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        if depth == 0:
                            end = i
                            break
                        depth -= 1
                    elif ch in "|;" and depth == 0:
                        end = i
                        break
                yield f, lineno, tail[:end]


def test_powershell_helper_set_is_derived_and_non_trivial():
    names = _ps_helpers_needing_root()
    # The four that 9be49441 broke must be in the derived set, or the scan
    # below proves nothing about them.
    for must in ("Get-VcoSeenStorePath", "Get-VcoCgInjectCountPath",
                 "Test-VcoCgInjectNoteOnce", "ConvertTo-VcoRepoRelative",
                 "Take-Snapshot", "Get-VcoBashWritePreBash"):
        assert must in names, (must, sorted(names))


def test_every_powershell_call_passes_project_root():
    missing = []
    seen_calls = 0
    for name in sorted(_ps_helpers_needing_root()):
        for f, lineno, args in _ps_call_sites(name):
            seen_calls += 1
            if not re.search(r"-ProjectRoot\b", args):
                missing.append(f"{f.relative_to(REPO)}:{lineno}: {name}{args.rstrip()}")
    assert seen_calls >= 15, seen_calls
    assert not missing, "helper calls without -ProjectRoot:\n" + "\n".join(missing)


# --------------------------------------------------------------------------
# Shell side
# --------------------------------------------------------------------------

_SH_FUNC = re.compile(r"^([a-z_][a-z0-9_]*)\s*\(\)\s*\{", re.M)


def _sh_helpers_needing_root() -> dict[str, int]:
    """name -> 1-based positional index that carries the project root."""
    out: dict[str, int] = {}
    for lib in LIB.glob("*.sh"):
        text = lib.read_text(encoding="utf-8")
        funcs = list(_SH_FUNC.finditer(text))
        for i, m in enumerate(funcs):
            body = text[m.end(): funcs[i + 1].start() if i + 1 < len(funcs) else len(text)]
            head = "\n".join(body.splitlines()[:6])
            pos = re.search(r'\b(?:proot|project_root)="\$\{?(\d)\}?"', head)
            if pos:
                out[m.group(1)] = int(pos.group(1))
    return out


def _sh_call_sites(name: str):
    call = re.compile(r"(?<![\w$-])" + re.escape(name) + r"(?![\w-])")
    for f in sorted(HOOKS.rglob("*.sh")):
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split(" #", 1)[0]
            if code.lstrip().startswith("#"):
                continue
            for m in call.finditer(code):
                before = code[: m.start()]
                if re.search(r"command\s+-v\s+$", before) or code[m.end():].lstrip().startswith("()"):
                    continue
                tail = code[m.end():]
                # Cut at the end of the command: ) closing a $( ), ;, &&, ||, |, >
                cut = re.search(r"\)(?=[\"\s;|&>]|$)|;|&&|\|\||\||>", tail)
                args = tail[: cut.start()] if cut else tail
                yield f, lineno, args


def test_shell_helper_set_is_derived_and_non_trivial():
    names = _sh_helpers_needing_root()
    for must in ("vco_seen_store_path", "vco_cg_inject_count_path",
                 "vco_cg_inject_note_once", "vco_to_repo_relative"):
        assert must in names, (must, names)


def test_every_shell_call_passes_project_root():
    missing = []
    seen_calls = 0
    for name, pos in sorted(_sh_helpers_needing_root().items()):
        for f, lineno, args in _sh_call_sites(name):
            seen_calls += 1
            try:
                argv = shlex.split(args)
            except ValueError:
                argv = args.split()
            if len(argv) < pos:
                missing.append(f"{f.relative_to(REPO)}:{lineno}: {name}{args} (needs arg {pos})")
    assert seen_calls >= 10, seen_calls
    assert not missing, "helper calls without the project-root argument:\n" + "\n".join(missing)


# --------------------------------------------------------------------------
# Behavioural: the real pre-edit .ps1 dedupes across two edits of one session
# --------------------------------------------------------------------------

_STUB = (
    "import sys\n"
    "if '--hook-format' in sys.argv:\n"
    "    print('KG: Sample Node B | concept | score=0.85 | src=knowledge/b.md | FULL NODE:')\n"
    "    print('body content')\n"
)


def _ps_sandbox(tmp_path: Path):
    orch = tmp_path / "orch"
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    shutil.copytree(HOOKS, orch / "templates" / "hooks")
    (orch / "claude_mcp_servers" / "scripts").mkdir(parents=True)
    (orch / "claude_mcp_servers" / "scripts" / "rl_kg_search.py").write_text(_STUB, encoding="utf-8")
    venv_bin = orch / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(shutil.which("python3") or sys.executable, venv_bin / "python")
    return orch, proj


def _run_pre_edit(orch: Path, proj: Path, file_name: str, tmp_path: Path):
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-w5r01",
        "tool_input": {"file_path": str(proj / file_name), "new_string": "hello"},
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VCT_", "VCO_", "CLAUDE_"))}
    env.update({
        "VCT_INSTALL_ROOT": str(orch),
        "CLAUDE_PROJECT_DIR": str(proj),
        "HOME": str(tmp_path / "home"),
        "WEAVIATE_URL": "http://127.0.0.1:9",
    })
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File",
         str(orch / "templates" / "hooks" / "pre-edit-context-inject.ps1")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=120, env=env,
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout in the sandbox")
def test_pre_edit_ps1_second_edit_in_session_is_deduped(tmp_path):
    orch, proj = _ps_sandbox(tmp_path)
    first = _run_pre_edit(orch, proj, "a.md", tmp_path)
    assert first.returncode == 0, first.stderr
    assert "Sample Node B" in first.stdout, (first.stdout, first.stderr)
    seen = proj / ".claude" / "state" / "seen_inject_sess-w5r01.txt"
    assert seen.is_file() and "Sample Node B" in seen.read_text("utf-8"), (
        "the seen-store was not written under the CALLING project's state dir"
    )
    second = _run_pre_edit(orch, proj, "b.md", tmp_path)
    assert second.returncode == 0, second.stderr
    assert "Sample Node B" not in second.stdout, (
        "second edit in the same session re-injected an already-seen node: "
        f"{second.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stub CLI")
def test_ps1_codegraph_helper_forwards_the_transcript(tmp_path):
    """Every .ps1 caller passes -TranscriptPath; the helper's parameter is
    $Transcript. A plain function silently drops an unknown named argument into
    $args, so before the alias the transcript never reached the CLI on Windows
    (the .sh sibling always forwarded it)."""
    proj = tmp_path / "proj"
    scripts = proj / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    argv_file = tmp_path / "argv.txt"
    cli = scripts / "code-graph-query"
    cli.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{argv_file}"\n', encoding="utf-8")
    cli.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VCT_", "VCO_"))}
    env.update({"CLAUDE_PROJECT_DIR": str(proj), "HOME": str(tmp_path / "home")})
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-Command",
         f'. "{LIB}/codegraph-query.ps1"; '
         'Invoke-VcoCodegraphQueryBlock -Query "widget" -Limit 2 -TranscriptPath "/t/x.jsonl" | Out-Null'],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    argv = argv_file.read_text("utf-8").splitlines()
    assert "--transcript" in argv and "/t/x.jsonl" in argv, argv
