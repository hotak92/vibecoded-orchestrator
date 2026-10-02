# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-17 — injected hook output stays under Claude Code's
10 000-character limit, and says when it was cut.

Claude Code's hooks contract: injected hook output past 10 000 characters is
not shown — the model gets a file path and a 2 000-character preview. On this
project's own transcripts ``compact-context-reinject`` was over on 223 of 235
runs (up to 144 891 characters) and ``diff-context-inject`` on 374 of 707, so
after a compaction the model saw a preview of the state the hook exists to
restore. The shared cap (``_lib/emit-context.{sh,ps1}``) cuts at
``VCO_HOOK_CONTEXT_CAP`` (default 9 500) and ends with a marker naming the
file to Read. Every hook runs for real against a project under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
PWSH = shutil.which("pwsh") or "pwsh"  # tests needing it are skipped when absent
HAVE_PWSH = shutil.which("pwsh") is not None
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(not HAVE_PWSH, reason="pwsh not installed"))]
MARKER = "[VCO: output cut at "
DEFAULT_CAP = 9500


def _env(project: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for key in ("VCT_DISABLE_HOOKS", "VCO_HOOK_CONTEXT_CAP"):
        env.pop(key, None)
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env.update(extra)
    return env


def _hook(impl: str, name: str, project: Path, payload: str, **extra: str) -> str:
    argv = (["bash", str(HOOKS / f"{name}.sh")] if impl == "sh" else
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(HOOKS / f"{name}.ps1")])
    res = subprocess.run(argv, input=payload, capture_output=True, text=True, timeout=120,
                         env=_env(project, **extra), cwd=str(project))
    assert res.returncode == 0, res.stderr
    return res.stdout


def _project(tmp_path: Path, context_state: str) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "context").mkdir(parents=True)
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(context_state, encoding="utf-8")
    return proj


def _big_state(chars: int) -> str:
    line = "- progress note with ünïcode and enough words to fill the line nicely\n"
    return "# State\n\n## Current Status\n" + line * (chars // len(line) + 1)


# ─── the shared helper ──────────────────────────────────────────────────


def _cap_sh(text: str, pointer: str = "", **env: str) -> str:
    res = subprocess.run(["bash", "-c", '. "$1"; vco_cap_context "$2" "$3"', "_",
                          str(HOOKS / "_lib" / "emit-context.sh"), text, pointer],
                         capture_output=True, text=True, timeout=60, env=dict(os.environ, **env))
    return res.stdout


def _pwsh_script(body: str, *args: str, env: dict | None = None) -> bytes:
    """Run ``body`` as a .ps1 FILE (``-Command`` would splice the args into
    the command text instead of binding ``$args``)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "drive.ps1"
        script.write_text(body, encoding="utf-8")
        res = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
                             capture_output=True, timeout=60, env=env or dict(os.environ))
        return res.stdout


def _cap_ps1(text: str, pointer: str = "", **env: str) -> str:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "text.txt"
        data.write_text(text, encoding="utf-8")
        out = _pwsh_script(
            "param([string]$Lib, [string]$Data, [string]$Pointer)\n"
            ". $Lib\n"
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "[Console]::Out.Write((Limit-VcoHookContext -Text ([IO.File]::ReadAllText($Data)) -Pointer $Pointer))\n",
            str(HOOKS / "_lib" / "emit-context.ps1"), str(data), pointer,
            env=dict(os.environ, **env))
        return out.decode("utf-8")


_CAPPERS = [pytest.param(_cap_sh, id="sh"),
            pytest.param(_cap_ps1, id="ps1", marks=pytest.mark.skipif(not HAVE_PWSH, reason="pwsh"))]


@pytest.mark.parametrize("cap", _CAPPERS)
def test_long_text_is_cut_with_a_marker(cap):
    out = cap("é" * 12000, "Read X.md.")
    assert len(out) <= DEFAULT_CAP
    assert MARKER in out and out.rstrip().endswith("Read X.md.]")
    assert "of 12000 characters" in out


@pytest.mark.parametrize("cap", _CAPPERS)
def test_short_text_is_untouched(cap):
    assert cap("hello\nworld") == "hello\nworld"


@pytest.mark.parametrize("cap", _CAPPERS)
@pytest.mark.parametrize("value,expected", [("3000", 3000), ("50", 1000), ("999999", 10000), ("x", DEFAULT_CAP)])
def test_cap_env_is_honoured_and_clamped(cap, value, expected):
    out = cap("a" * 20000, **{"VCO_HOOK_CONTEXT_CAP": value})
    assert len(out) == expected


# ─── the hooks that exceeded the limit ──────────────────────────────────


@pytest.mark.parametrize("impl", _SHELLS)
def test_compact_reinject_is_capped_and_keeps_the_start(impl, tmp_path):
    proj = _project(tmp_path, _big_state(60000))
    out = _hook(impl, "compact-context-reinject", proj, json.dumps({"session_id": "s1"}))
    assert len(out.rstrip("\n")) <= DEFAULT_CAP
    assert out.startswith("## Current Task State")
    assert MARKER in out and ".claude/CONTEXT_STATE.md" in out.split(MARKER, 1)[1]


@pytest.mark.parametrize("impl", _SHELLS)
def test_compact_reinject_small_state_is_not_marked(impl, tmp_path):
    proj = _project(tmp_path, "# State\n\n## Current Status\nshort\n")
    out = _hook(impl, "compact-context-reinject", proj, json.dumps({"session_id": "s1"}))
    assert "short" in out
    assert MARKER not in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_diff_inject_is_capped(impl, tmp_path):
    proj = _project(tmp_path, "# State\n\n## Current Status\nold\n")
    payload = json.dumps({"session_id": "s1"})
    assert _hook(impl, "diff-context-inject", proj, payload).strip() == ""  # baseline
    (proj / ".claude" / "CONTEXT_STATE.md").write_text(_big_state(40000), encoding="utf-8")
    out = _hook(impl, "diff-context-inject", proj, payload)
    assert len(out.rstrip("\n")) <= DEFAULT_CAP
    assert "changed sections" in out
    assert MARKER in out


@pytest.mark.parametrize("impl", _SHELLS)
def test_diff_inject_small_change_is_not_marked(impl, tmp_path):
    proj = _project(tmp_path, "# State\n\n## Current Status\nold\n")
    payload = json.dumps({"session_id": "s1"})
    _hook(impl, "diff-context-inject", proj, payload)
    (proj / ".claude" / "CONTEXT_STATE.md").write_text("# State\n\n## Current Status\nnew\n", encoding="utf-8")
    out = _hook(impl, "diff-context-inject", proj, payload)
    assert "new" in out and MARKER not in out


def test_emit_additional_context_uses_the_cap():
    res = subprocess.run(["bash", "-c", '. "$1"; emit_additional_context "$2" PreToolUse', "_",
                          str(HOOKS / "_lib" / "emit-context.sh"), "x" * 15000],
                         capture_output=True, text=True, timeout=60)
    ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= DEFAULT_CAP + 1  # here-string adds one newline
    assert MARKER in ctx


@pytest.mark.skipif(not HAVE_PWSH, reason="pwsh not installed")
def test_emit_additional_context_ps1_uses_the_cap():
    out = _pwsh_script("param([string]$Lib)\n. $Lib\nEmit-AdditionalContext ('x' * 15000) 'PreToolUse'\n",
                       str(HOOKS / "_lib" / "emit-context.ps1"))
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= DEFAULT_CAP and MARKER in ctx
