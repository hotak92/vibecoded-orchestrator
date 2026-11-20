# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 -- PowerShell hooks read their payload as UTF-8, whatever the
console code page.

Claude Code writes the hook payload as raw UTF-8 (``JSON.stringify`` leaves
non-ASCII characters unescaped). ``[Console]::In`` decoded it in the console
code page -- OEM 850/437 for a hook Claude Code starts on Windows -- so a
non-ASCII path slipped past Build Anchor, the pre-edit backup and the
credential scan (the mangled path does not exist), the lean-ctx command file
held mojibake, and the kill-guard rewrite changed the command text. Every
hook now reads through ``_lib/hook-stdin.ps1`` (``Read-VcoHookStdin``: the
raw stdin stream as UTF-8; UTF-8 output; ``PYTHONIOENCODING``).

Static legs: no hook reads ``[Console]::In``; every reader loads the helper
before the stderr cap. Behaviour legs feed RAW UTF-8 (``ensure_ascii=False``;
``json.dumps``'s default ``\\uXXXX`` escaping hides the defect) to the real
hooks under a console encoding other than UTF-8:

* POSIX ``pwsh``: ``LANG``/``LC_ALL=en_US.ISO-8859-1`` -- .NET then decodes
  console input as Latin-1, reproducing the Windows defect (measured: the two
  characters "e-grave check-mark" arrive as five).
* Windows: the runner's console code page; the legs skip when it already is
  65001 (nothing to prove there), which a probe checks first.

The ``.sh`` sibling gets the same payload and must reach the same verdict.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS, base_flags_for

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
LIB = HOOKS / "_lib" / "hook-stdin.ps1"
_WIN = sys.platform == "win32"

_PS = shutil.which("pwsh") or (shutil.which("powershell") if _WIN else None)
_PS_FLAGS = (PWSH_BASE_FLAGS if _PS and Path(_PS).stem.lower() == "pwsh"
             else (base_flags_for(_PS) if _PS else []))

#: Non-UTF-8 console input on POSIX .NET (no installed locale needed: .NET
#: reads the charset from the variable itself).
_LATIN1_ENV = {} if _WIN else {"LANG": "en_US.ISO-8859-1",
                               "LC_ALL": "en_US.ISO-8859-1"}

NAME = "città_日本.py"          # città_日本.py
COMMAND_TEXT = 'Write-Output "città ✓"'


# --------------------------------------------------------------------------
# static
# --------------------------------------------------------------------------

def _code(p: Path) -> list[tuple[int, str]]:
    return [(n, ln) for n, ln in
            enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1)
            if not ln.lstrip().startswith("#")]


def test_no_hook_reads_console_in():
    offenders = [f"{p.relative_to(REPO_ROOT)}:{n}"
                 for p in sorted(HOOKS.rglob("*.ps1"))
                 for n, ln in _code(p) if re.search(r"\[Console\]::In\b", ln)]
    assert offenders == [], (
        "[Console]::In decodes the UTF-8 payload in the console code page; "
        f"use Read-VcoHookStdin (_lib/hook-stdin.ps1): {offenders}")


def _readers() -> list[Path]:
    return sorted(p for p in HOOKS.glob("*.ps1")
                  if any("Read-VcoHookStdin" in ln for _n, ln in _code(p)))


def test_the_payload_readers_are_the_hooks_that_read_stdin():
    names = {p.name for p in _readers()}
    # The pre-v0.2.107 readers of [Console]::In (40 hooks) plus
    # session-worktree-reap.ps1 (new in v0.2.107, reads its payload through the
    # helper from the start) -- a guard that the sweep missed none and that the
    # static test above is not vacuous.
    assert len(names) == 41, sorted(names)
    for must in ("pre-tool-use.ps1", "post-tool-security.ps1",
                 "lean-ctx-rewrite.ps1", "wait-guard.ps1",
                 "post-file-edit.ps1", "py-compile-check.ps1"):
        assert must in names


@pytest.mark.parametrize("hook", _readers(), ids=lambda p: p.name)
def test_every_reader_loads_the_helper_first(hook: Path):
    lines = _code(hook)
    load = [n for n, ln in lines
            if ln.startswith('. "$PSScriptRoot/_lib/hook-stdin.ps1"')]
    assert len(load) == 1, f"{hook.name}: dot-source _lib/hook-stdin.ps1 once at top level"
    first_read = min(n for n, ln in lines if "Read-VcoHookStdin" in ln)
    assert load[0] < first_read
    caps = [n for n, ln in lines if "stderr-cap.ps1" in ln]
    if caps:
        assert load[0] < min(caps), (
            f"{hook.name}: load hook-stdin.ps1 before stderr-cap.ps1 so the "
            "capped stderr wraps the UTF-8 writer")


def test_the_helper_runs_no_cmdlet():
    """The helper is on agent-alerts-deliver's path (every tool call): plain
    .NET only, so loading it autoloads no PowerShell module."""
    body = "\n".join(ln for _n, ln in _code(LIB))
    assert not re.search(r"\b(New-Object|Test-Path|Join-Path|Get-\w+|Set-\w+)\b", body), body


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------

def _probe_input_encoding() -> str:
    if not _PS:
        return ""
    proc = subprocess.run(
        [_PS, *_PS_FLAGS, "-NonInteractive", "-Command",
         "[Console]::InputEncoding.WebName"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, **_LATIN1_ENV}, stdin=subprocess.PIPE)
    return proc.stdout.strip().lower()


_ENC = _probe_input_encoding() if _PS else ""
needs_non_utf8_console = pytest.mark.skipif(
    not _PS or _ENC in ("", "utf-8"),
    reason=("PowerShell not installed" if not _PS else
            f"console input encoding is {_ENC or 'unknown'}: no non-UTF-8 "
            "code page to reproduce the defect under"))
#: The .sh arm drives the hook through POSIX bash; on Windows the same test's
#: [ps1] param (the leg this file exists for) covers the behaviour.
needs_bash = pytest.mark.skipif(
    _WIN or shutil.which("bash") is None,
    reason="bash .sh leg; Windows runs the .ps1 sibling: this test's [ps1] param")


def _raw(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _base_env(proj: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCO_KILL_GUARD",
                        "VCO_LEAN_CTX_DEFAULT", "STDERR_CAP_DISABLE",
                        "PYTHONIOENCODING")}
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env.update(extra)
    return env


def _run(impl: str, hook: str, payload: dict, proj: Path, **env_extra):
    if impl == "ps1":
        argv = [_PS, *_PS_FLAGS, "-NonInteractive", "-File", str(HOOKS / f"{hook}.ps1")]
        env = _base_env(proj, **_LATIN1_ENV, **env_extra)
    else:
        argv = ["bash", str(HOOKS / f"{hook}.sh")]
        env = _base_env(proj, **env_extra)
    return subprocess.run(argv, input=_raw(payload), capture_output=True,
                          cwd=str(proj), env=env, timeout=120)


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "logs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    return proj


IMPLS = [pytest.param("ps1", marks=needs_non_utf8_console),
         pytest.param("sh", marks=needs_bash)]


@pytest.mark.parametrize("impl", IMPLS)
@pytest.mark.parametrize("name", ["plain.py", NAME], ids=["ascii", "non-ascii"])
def test_build_anchor_blocks_an_unread_non_ascii_write_by_its_name(impl, name, tmp_path):
    proj = _project(tmp_path)
    target = proj / name
    target.write_text("x = 1\n", encoding="utf-8")
    res = _run(impl, "pre-tool-use",
               {"hook_event_name": "PreToolUse", "tool_name": "Write",
                "session_id": "s-utf8", "tool_input": {"file_path": str(target),
                                                      "content": "y = 2\n"}},
               proj)
    err = res.stderr.decode("utf-8", "strict")
    assert res.returncode == 2, (res.returncode, err)
    assert f"Build Anchor Protocol: '{name}' has not been Read" in err, err


_NOTIFY = """import sys
with open(sys.argv[0] + ".calls", "a", encoding="utf-8") as fh:
    fh.write(" | ".join(sys.argv[1:]) + "\\n")
"""
_TOKEN = "ghp_" + "Q" * 36


@pytest.mark.parametrize("impl", IMPLS)
def test_credential_scan_alerts_on_a_non_ascii_path(impl, tmp_path):
    proj = _project(tmp_path)
    (proj / ".claude" / "scripts" / "notify.py").write_text(_NOTIFY, encoding="utf-8")
    target = proj / NAME
    content = f'TOKEN = "{_TOKEN}"\n'
    target.write_text(content, encoding="utf-8")
    res = _run(impl, "post-tool-security",
               {"hook_event_name": "PostToolUse", "tool_name": "Write",
                "session_id": "s-utf8", "tool_input": {"file_path": str(target),
                                                      "content": content}},
               proj, STDERR_CAP_DISABLE="1")
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    ledger = proj / ".claude" / "logs" / "credential_alerts.jsonl"
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines()
            if ln.strip()] if ledger.exists() else []
    assert rows, ("no credential alert for a token in a non-ASCII-named file",
                  res.stdout.decode("utf-8", "replace"))
    assert rows[-1]["file"] == NAME, rows[-1]
    assert _TOKEN not in res.stdout.decode("utf-8", "replace")


def _fake_lean_ctx(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "lean-ctx"
    fake.write_text("#!/usr/bin/env bash\ncat > /dev/null\nprintf 'X\\n'\n", encoding="utf-8")
    fake.chmod(0o755)


@pytest.mark.parametrize("impl", [pytest.param("ps1", marks=[
    needs_non_utf8_console,
    pytest.mark.skipif(_WIN, reason="the fake lean-ctx binary is a POSIX script")])])
def test_lean_ctx_command_file_holds_the_original_bytes(impl, tmp_path):
    proj = _project(tmp_path)
    bin_dir = tmp_path / "fakebin"
    _fake_lean_ctx(bin_dir)
    cmd = 'pytest -q "tests/perché_日本.py"'
    res = _run(impl, "lean-ctx-rewrite",
               {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": cmd}},
               proj, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    cmdfiles = list((proj / ".claude" / "state" / "lean-ctx-tee").glob("*.cmd"))
    assert len(cmdfiles) == 1, (res.stdout, res.stderr)
    assert cmdfiles[0].read_bytes() == cmd.encode("utf-8")
    # The rewritten command the harness runs is valid UTF-8 JSON.
    json.loads(res.stdout.decode("utf-8", "strict"))


@pytest.mark.parametrize("impl", IMPLS)
def test_kill_guard_rewrite_keeps_the_command_text(impl, tmp_path):
    proj = _project(tmp_path)
    cmd = f"Stop-Process -Id 999999; {COMMAND_TEXT}"
    res = _run(impl, "lean-ctx-rewrite",
               {"hook_event_name": "PreToolUse", "tool_name": "PowerShell",
                "tool_input": {"command": cmd, "description": "d"}},
               proj, HOME=str(tmp_path / "home"))
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    out = json.loads(res.stdout.decode("utf-8", "strict"))
    ui = out["hookSpecificOutput"]["updatedInput"]
    assert ui["command"].startswith("# VCO kill guard"), ui
    assert ui["command"].endswith(cmd), ui["command"]
    assert ui["description"] == "d"


@pytest.mark.parametrize("impl", [pytest.param("ps1", marks=needs_non_utf8_console)])
def test_python_children_get_utf8_both_ways(impl, tmp_path):
    """The helper's PYTHONIOENCODING + $OutputEncoding + console output
    encoding: text piped into Python and Python's answer survive intact."""
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        pytest.skip("no Python on PATH")
    lib = tmp_path / "_lib"
    lib.mkdir()
    shutil.copy2(LIB, lib / "hook-stdin.ps1")
    script = tmp_path / "probe.ps1"
    script.write_text(
        '. "$PSScriptRoot/_lib/hook-stdin.ps1"\n'
        "$p = (Read-VcoHookStdin | ConvertFrom-Json).x\n"
        f"$r = $p | & '{py}' -c \"import sys; print('py:' + sys.stdin.read().strip())\"\n"
        "Write-Output $r\n", encoding="utf-8")
    res = subprocess.run([_PS, *_PS_FLAGS, "-NonInteractive", "-File", str(script)],
                         input=_raw({"x": "città ✓"}), capture_output=True,
                         env={**os.environ, **_LATIN1_ENV}, timeout=60)
    assert res.stdout.decode("utf-8", "strict").strip() == "py:città ✓", res
