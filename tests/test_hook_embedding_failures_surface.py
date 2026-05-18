# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for templates/hooks/embedding-failures-surface.{sh,ps1}.

The SessionStart hook surfaces the contents of
``.claude/context/EMBEDDING_FAILURES.md`` (when present) to stdout so
Claude Code injects it as a system-reminder. The hook must be:

  * idempotent (zero output, exit 0 when no failure recorded)
  * cross-OS (paired .sh + .ps1 with equivalent behaviour)
  * soft-fail (never block SessionStart, even when CLAUDE_PROJECT_DIR is
    wrong or unreadable)

These tests exercise the .sh side end-to-end (running the script under
bash with a temp $CLAUDE_PROJECT_DIR). The .ps1 side is checked
structurally — body fingerprints — because we can't reliably run
PowerShell on the Linux CI host.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "embedding-failures-surface.sh"
HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "embedding-failures-surface.ps1"


def _run_hook(project_root: Path, *, extra_env: dict[str, str] | None = None):
    """Invoke the shell hook with CLAUDE_PROJECT_DIR pointing at *project_root*."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Make sure we don't accidentally inherit a disable flag from the
    # test runner's environment.
    env.pop("VCT_DISABLE_HOOKS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# File presence + parity (cross-OS structural)
# ---------------------------------------------------------------------------


def test_both_hook_files_exist():
    """Cross-OS contract: every .sh hook must have a .ps1 sibling."""
    assert HOOK_SH.is_file(), f"missing: {HOOK_SH}"
    assert HOOK_PS1.is_file(), f"missing: {HOOK_PS1}"


def test_sh_hook_is_executable():
    """The .sh hook must be executable so install.py's copy2 preserves
    the bit when shipping to user projects."""
    assert os.access(HOOK_SH, os.X_OK), (
        f"{HOOK_SH} is not executable; install.py uses shutil.copy2 which "
        "preserves the exec bit, so the source must have it set."
    )


def test_sh_hook_has_agpl_header():
    """All hooks under templates/hooks/ inherit AGPL-3.0."""
    body = HOOK_SH.read_text(encoding="utf-8")
    assert "AGPL-3.0" in body, "missing AGPL header in .sh hook"


def test_ps1_hook_has_agpl_header():
    body = HOOK_PS1.read_text(encoding="utf-8")
    assert "AGPL-3.0" in body, "missing AGPL header in .ps1 hook"


# ---------------------------------------------------------------------------
# Body parity (fingerprints — see test_hook_ps1_body_parity.py style)
# ---------------------------------------------------------------------------


def test_ps1_hook_handles_disable_flag():
    """The .ps1 must respect $env:VCT_DISABLE_HOOKS (sibling to the .sh
    check `[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0`)."""
    body = HOOK_PS1.read_text(encoding="utf-8")
    assert "VCT_DISABLE_HOOKS" in body, "ps1 must honour the disable flag"


def test_ps1_hook_uses_userprofile_for_home():
    """The .ps1 must resolve $HOME via USERPROFILE on Windows."""
    body = HOOK_PS1.read_text(encoding="utf-8")
    # Either GetFolderPath('UserProfile') or $env:USERPROFILE is fine.
    assert ("UserProfile" in body) or ("USERPROFILE" in body), (
        "ps1 must use Windows-native home resolution (USERPROFILE / "
        "GetFolderPath('UserProfile'))"
    )


def test_ps1_hook_reads_correct_hint_file():
    """The .ps1 must read the same EMBEDDING_FAILURES.md path the .sh does."""
    body = HOOK_PS1.read_text(encoding="utf-8")
    assert "EMBEDDING_FAILURES.md" in body
    # Windows-style path separator with escaped backslash.
    assert r"\.claude\context\EMBEDDING_FAILURES.md" in body or \
           ".claude/context/EMBEDDING_FAILURES.md" in body


# ---------------------------------------------------------------------------
# Runtime behaviour (.sh) — idempotent, soft-fail, exit 0
# ---------------------------------------------------------------------------


def test_hook_silent_when_no_hint_file(tmp_path):
    """No EMBEDDING_FAILURES.md → zero stdout, exit 0 (idempotent)."""
    # tmp_path is a fresh dir, no .claude/context inside.
    result = _run_hook(tmp_path)
    assert result.returncode == 0, f"unexpected non-zero exit: {result.stderr}"
    assert result.stdout == "", f"expected empty stdout, got: {result.stdout!r}"


def test_hook_silent_when_claude_project_dir_unset(tmp_path):
    """No CLAUDE_PROJECT_DIR + no git toplevel → exit 0, no output."""
    env = os.environ.copy()
    env.pop("CLAUDE_PROJECT_DIR", None)
    env.pop("VCT_DISABLE_HOOKS", None)
    # Run from a non-git directory so the git toplevel probe also fails.
    result = subprocess.run(
        ["bash", str(HOOK_SH)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    # Output should be empty (no project context found).
    assert result.stdout == ""


def test_hook_surfaces_hint_when_present(tmp_path):
    """When EMBEDDING_FAILURES.md exists, the hook must print its path
    and contents to stdout (Claude Code injects stdout as a
    system-reminder during SessionStart)."""
    hint_dir = tmp_path / ".claude" / "context"
    hint_dir.mkdir(parents=True)
    hint_file = hint_dir / "EMBEDDING_FAILURES.md"
    hint_body = (
        "# Embedding backend failure\n\n"
        "**Attempted backends**: ollama, openai\n\n"
        "## Per-backend errors\n\n"
        "- **ollama**: connection refused\n"
        "- **openai**: auth failed\n"
    )
    hint_file.write_text(hint_body, encoding="utf-8")

    result = _run_hook(tmp_path)
    assert result.returncode == 0, f"unexpected non-zero exit: {result.stderr}"
    # Path must be surfaced so Claude knows where to find the source.
    assert str(hint_file) in result.stdout, (
        f"hint file path missing from stdout. Got:\n{result.stdout}"
    )
    # Hint body must be inlined (Claude sees both pointer + content).
    assert "ollama" in result.stdout
    assert "openai" in result.stdout
    assert "connection refused" in result.stdout
    # The pointer to the JSONL detail log must also be surfaced.
    assert "embedding_failures.jsonl" in result.stdout


def test_hook_respects_disable_flag(tmp_path):
    """VCT_DISABLE_HOOKS=1 → no output, exit 0 (universal escape hatch)."""
    hint_dir = tmp_path / ".claude" / "context"
    hint_dir.mkdir(parents=True)
    hint_file = hint_dir / "EMBEDDING_FAILURES.md"
    hint_file.write_text("# This should NOT be surfaced", encoding="utf-8")

    result = _run_hook(tmp_path, extra_env={"VCT_DISABLE_HOOKS": "1"})
    assert result.returncode == 0
    assert result.stdout == "", (
        "VCT_DISABLE_HOOKS must short-circuit before any output. "
        f"Got: {result.stdout!r}"
    )


def test_hook_idempotent_across_runs(tmp_path):
    """Running the hook twice produces the same output (no state mutation)."""
    hint_dir = tmp_path / ".claude" / "context"
    hint_dir.mkdir(parents=True)
    (hint_dir / "EMBEDDING_FAILURES.md").write_text("# Test hint", encoding="utf-8")

    result1 = _run_hook(tmp_path)
    result2 = _run_hook(tmp_path)
    assert result1.returncode == 0
    assert result2.returncode == 0
    assert result1.stdout == result2.stdout, (
        "Hook must be stateless — successive runs must produce identical "
        "output (the hook does not delete the hint file; clearing is the "
        "EmbeddingService's responsibility on the next successful call)."
    )


# ---------------------------------------------------------------------------
# Hook registration in settings.json templates
# ---------------------------------------------------------------------------


def test_hook_registered_in_linux_template():
    template = REPO_ROOT / "templates" / "settings.json.linux.template"
    body = template.read_text(encoding="utf-8")
    assert "embedding-failures-surface.sh" in body, (
        "Hook must be registered in templates/settings.json.linux.template "
        "under SessionStart so it fires on every Claude Code chat start."
    )


def test_hook_registered_in_windows_template():
    template = REPO_ROOT / "templates" / "settings.json.windows.template"
    body = template.read_text(encoding="utf-8")
    assert "embedding-failures-surface.ps1" in body, (
        "Hook must be registered in templates/settings.json.windows.template "
        "(SessionStart) for cross-OS parity."
    )
