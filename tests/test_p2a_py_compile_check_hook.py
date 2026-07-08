# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P2a (v0.2.75): named py-compile-check hook replaces the inline entry.

The inline ``python3 -c "...py_compile..."`` registration at
``settings.json.{linux,windows}.template:397`` was the ONLY hook without a
``VCT_DISABLE_HOOKS`` guard (so the documented opt-out was incomplete — D-5
compiled files while "disabled"), ran with UNSCRUBBED env, was not
basename-supersedable by a bundle update, and printed a misleading "Syntax
error" on ANY failure — including malformed stdin (not a syntax error).

P2a converts it to a named ``py-compile-check.{sh,ps1}`` sibling pair that
self-guards, scrubs the canonical env, and reports the TRUE compiler
message. This pins:

  * ``VCT_DISABLE_HOOKS=1`` → no compile (act/leave-alone).
  * malformed stdin → exit 0, NO "Syntax error" lie.
  * real syntax error → the actual message surfaced.
  * both settings templates now reference the named script (D-4 parity +
    scrub-parity + hook-os-parity gates then cover it — asserted here too).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "py-compile-check.sh"
HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "py-compile-check.ps1"
LINUX_TMPL = REPO_ROOT / "templates" / "settings.json.linux.template"
WIN_TMPL = REPO_ROOT / "templates" / "settings.json.windows.template"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook; .ps1 covered by hook-os-parity + a pwsh case elsewhere.",
)


def _run_sh(payload: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SH)], input=payload,
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_valid_python_compiles_clean(tmp_path):
    f = tmp_path / "good.py"
    f.write_text("x = 1\n")
    res = _run_sh(json.dumps({"tool_input": {"file_path": str(f)}}))
    assert res.returncode == 0
    assert res.stdout.strip() == "", "clean compile says nothing"


def test_real_syntax_error_is_surfaced(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("def (:\n")
    res = _run_sh(json.dumps({"tool_input": {"file_path": str(f)}}))
    assert res.returncode == 0, "hook is non-blocking (always exit 0)"
    assert "py_compile:" in res.stdout, res.stdout
    # The true message mentions the file / a SyntaxError — NOT a bare lie.
    assert "Syntax error" != res.stdout.strip(), (
        "must surface the real compiler message, not the old bare lie"
    )


def test_malformed_stdin_no_syntax_error_lie():
    res = _run_sh("this is not json {{{")
    assert res.returncode == 0
    assert res.stdout.strip() == "", (
        "malformed stdin is NOT a syntax error — must say nothing "
        f"(got: {res.stdout!r})"
    )


def test_absent_file_path_is_noop():
    res = _run_sh(json.dumps({"tool_input": {}}))
    assert res.returncode == 0
    assert res.stdout.strip() == ""


def test_vct_disable_hooks_skips_compile(tmp_path):
    """act/leave-alone: with the kill-switch set, a BAD file is NOT compiled
    (the whole point of D-5 — the opt-out must be complete)."""
    f = tmp_path / "bad.py"
    f.write_text("def (:\n")
    res = _run_sh(
        json.dumps({"tool_input": {"file_path": str(f)}}),
        extra_env={"VCT_DISABLE_HOOKS": "1"},
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "", "VCT_DISABLE_HOOKS=1 must skip the compile"


def test_hook_self_guards_and_scrubs():
    sh = HOOK_SH.read_text(encoding="utf-8")
    ps1 = HOOK_PS1.read_text(encoding="utf-8")
    # Self-guard.
    assert 'VCT_DISABLE_HOOKS' in sh and "exit 0" in sh
    assert 'VCT_DISABLE_HOOKS' in ps1
    # Canonical scrub line present (scrub-parity gate enforces the exact set).
    assert "unset SUPABASE_KEY" in sh and "GITHUB_TOKEN" in sh
    assert "GITHUB_TOKEN" in ps1
    # No "Syntax error" lie EMITTED: the old inline form printed it via
    # `|| echo "Syntax error"` (.sh) / `catch { Write-Output "Syntax error" }`
    # (.ps1). Neither emission pattern may survive (the string may appear in a
    # comment explaining the fix, but never as an echo/Write-Output arg).
    assert 'echo "Syntax error"' not in sh
    assert 'echo \\"Syntax error\\"' not in sh
    assert 'Write-Output "Syntax error"' not in ps1
    assert 'Write-Output \\"Syntax error\\"' not in ps1


def test_templates_reference_named_hook_not_inline():
    linux = LINUX_TMPL.read_text(encoding="utf-8")
    win = WIN_TMPL.read_text(encoding="utf-8")
    assert "py-compile-check.sh" in linux, "linux template must call the named hook"
    assert "py-compile-check.ps1" in win, "windows template must call the named hook"
    # The old inline py_compile entry must be gone from both.
    assert "import json,sys,py_compile" not in linux, "inline py_compile lingering"
    assert 'catch { Write-Output' not in win or "Syntax error" not in win, (
        "old inline windows py_compile lie lingering"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
