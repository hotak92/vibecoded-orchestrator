# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for `_check_global_lean_ctx_hooks` (PR-11 v0.2.11).

The helper runs at the very start of install.py's main() — before any
compose-up or hook installation — to detect global lean-ctx artifacts
that caused two fork-bomb incidents (2026-04-30 + 2026-05-15).

Coverage matrix:

  - No ~/.claude/ directory → no warning, no deferral.
  - ~/.claude/ exists but empty (no settings.json, no hooks/) → no warning.
  - settings.json present but has no hooks key → no warning.
  - settings.json has PreToolUse entries, none referencing lean-ctx → no warning.
  - settings.json has a PreToolUse entry whose command contains "lean-ctx" →
    warning emitted to stderr + deferral entry added.
  - ~/.claude/hooks/lean-ctx-rewrite.sh exists → warning + deferral.
  - Both settings.json entry AND hook file present → single deferral entry
    summarising both; warning lists both.
  - Malformed (non-JSON) settings.json → soft-fail: warning to stderr about
    the parse error but no exception propagated.
  - Unreadable settings.json (permission error) → soft-fail.
  - Case-insensitive match on "LEAN-CTX" in command → warning emitted.
  - .lean-ctx.bak file in hooks dir → detected (starts with ".lean-ctx").

All tests mock Path.home() to a tmp_path-based fake HOME so they are
fully hermetic — no real ~/.claude/ is touched regardless of host platform.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    pre_tool_use_commands: list[str] | None = None,
    extra_hooks: dict | None = None,
) -> dict:
    """Build a minimal settings.json dict with optional PreToolUse entries."""
    hooks: dict = {}
    if pre_tool_use_commands is not None:
        hooks["PreToolUse"] = [
            {"command": cmd} for cmd in pre_tool_use_commands
        ]
    if extra_hooks:
        hooks.update(extra_hooks)
    return {"hooks": hooks} if hooks else {}


def _write_settings(claude_dir: Path, data: dict) -> Path:
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps(data), encoding="utf-8")
    return settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect Path.home() to a tmp dir for the duration of the test."""
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def claude_dir(fake_home: Path) -> Path:
    """Create ~/.claude/ inside the fake home."""
    d = fake_home / ".claude"
    d.mkdir()
    return d


@pytest.fixture
def hooks_dir(claude_dir: Path) -> Path:
    """Create ~/.claude/hooks/ inside the fake claude dir."""
    d = claude_dir / "hooks"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# No ~/.claude/ directory
# ---------------------------------------------------------------------------


def test_no_claude_dir_no_warning(fake_home: Path, capsys):
    """When ~/.claude/ does not exist, function returns early silently."""
    # Verify the dir truly doesn't exist
    assert not (fake_home / ".claude").exists()

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert dr.entries == []


# ---------------------------------------------------------------------------
# ~/.claude/ exists but nothing lean-ctx related
# ---------------------------------------------------------------------------


def test_empty_claude_dir_no_warning(claude_dir: Path, capsys):
    """Empty ~/.claude/ (no settings.json, no hooks/) → no warning."""
    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert dr.entries == []


def test_settings_no_hooks_key_no_warning(claude_dir: Path, capsys):
    """settings.json without a 'hooks' key → no warning."""
    _write_settings(claude_dir, {"telemetry": {"enabled": False}})

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert dr.entries == []


def test_settings_pretooluse_no_lean_ctx_no_warning(claude_dir: Path, capsys):
    """PreToolUse entries that don't mention lean-ctx → no warning."""
    data = _make_settings(
        pre_tool_use_commands=["/usr/local/bin/some-other-hook arg1"]
    )
    _write_settings(claude_dir, data)

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert dr.entries == []


# ---------------------------------------------------------------------------
# settings.json contains a lean-ctx PreToolUse entry
# ---------------------------------------------------------------------------


def test_settings_lean_ctx_command_triggers_warning(claude_dir: Path, capsys):
    """A PreToolUse entry with 'lean-ctx' in the command → warning + deferral."""
    data = _make_settings(
        pre_tool_use_commands=["/usr/local/bin/lean-ctx hook --agent claude"]
    )
    _write_settings(claude_dir, data)

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert "lean-ctx" in captured.err.lower()
    assert "WARNING" in captured.err or "warning" in captured.err.lower()
    assert len(dr.entries) == 1
    entry = dr.entries[0]
    assert entry.condition_id == "global_lean_ctx_hooks_detected"
    assert entry.severity == "warning"
    assert "lean-ctx" in entry.detected.lower()


def test_settings_lean_ctx_case_insensitive(claude_dir: Path, capsys):
    """Detection is case-insensitive — 'LEAN-CTX' must also trigger."""
    data = _make_settings(
        pre_tool_use_commands=["/usr/bin/LEAN-CTX hook"]
    )
    _write_settings(claude_dir, data)

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    assert len(dr.entries) == 1
    assert dr.entries[0].condition_id == "global_lean_ctx_hooks_detected"


# ---------------------------------------------------------------------------
# ~/.claude/hooks/ contains lean-ctx-* files
# ---------------------------------------------------------------------------


def test_hook_file_lean_ctx_rewrite_triggers_warning(
    claude_dir: Path, hooks_dir: Path, capsys
):
    """lean-ctx-rewrite.sh in hooks/ → warning + deferral entry."""
    hook = hooks_dir / "lean-ctx-rewrite.sh"
    hook.write_text("#!/bin/bash\n# lean-ctx hook\n", encoding="utf-8")

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert "lean-ctx" in captured.err.lower()
    assert len(dr.entries) == 1
    entry = dr.entries[0]
    assert entry.condition_id == "global_lean_ctx_hooks_detected"
    assert str(hook) in entry.detected


def test_hook_file_dot_lean_ctx_bak_detected(
    claude_dir: Path, hooks_dir: Path, capsys
):
    """`.lean-ctx.bak` (starts with '.lean-ctx') → detected."""
    bak = hooks_dir / ".lean-ctx.bak"
    bak.write_text("backup content", encoding="utf-8")

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    assert len(dr.entries) == 1
    assert str(bak) in dr.entries[0].detected


# ---------------------------------------------------------------------------
# Both settings.json entry AND hook file present
# ---------------------------------------------------------------------------


def test_both_settings_and_file_single_deferral_entry(
    claude_dir: Path, hooks_dir: Path, capsys
):
    """When both a hook file and a settings entry are present, a single
    deferral entry is added that mentions both artifacts."""
    # Hook file
    hook = hooks_dir / "lean-ctx-redirect.sh"
    hook.write_text("#!/bin/bash\n", encoding="utf-8")
    # settings.json entry
    data = _make_settings(
        pre_tool_use_commands=["/opt/lean-ctx hook"]
    )
    _write_settings(claude_dir, data)

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    # Exactly one deferral entry — not two.
    assert len(dr.entries) == 1
    entry = dr.entries[0]
    assert entry.condition_id == "global_lean_ctx_hooks_detected"
    # Both artifact types mentioned in the detected description.
    assert str(hook) in entry.detected
    assert "lean-ctx" in entry.detected.lower()

    captured = capsys.readouterr()
    # Warning mentions hook files section
    assert "Hook files" in captured.err or str(hook) in captured.err
    # Warning mentions settings entries section
    assert "settings.json" in captured.err or "/opt/lean-ctx" in captured.err


# ---------------------------------------------------------------------------
# Soft-fail: malformed / unreadable settings.json
# ---------------------------------------------------------------------------


def test_malformed_settings_json_soft_fail(claude_dir: Path, capsys):
    """Malformed (non-JSON) settings.json → logs parse error to stderr,
    does NOT raise, and continues (no deferral entry if no hook files)."""
    settings = claude_dir / "settings.json"
    settings.write_text("{this is not valid json!!", encoding="utf-8")

    dr = DeferralReport()
    # Must not raise
    install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    # The parse-failure warning goes to stderr
    assert "settings.json" in captured.err or "lean-ctx" in captured.err.lower()
    # No deferral added (no hook files either)
    assert dr.entries == []


def test_unreadable_settings_json_soft_fail(claude_dir: Path, capsys):
    """An OSError when reading settings.json → logs to stderr, does not raise."""
    settings = claude_dir / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    with mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")):
        dr = DeferralReport()
        install._check_global_lean_ctx_hooks(dr)

    captured = capsys.readouterr()
    assert "permission denied" in captured.err or "settings" in captured.err.lower()
    assert dr.entries == []


# ---------------------------------------------------------------------------
# Cross-OS: Path.home() is mocked — runs identically on Linux/macOS/Windows
# ---------------------------------------------------------------------------


def test_cross_os_home_via_monkeypatch(tmp_path: Path, monkeypatch, capsys):
    """Path.home() is mocked to a tmp_path-based fake HOME — proves no
    hard-coded platform paths are used and the function works on any OS."""
    alt_home = tmp_path / "alt_user_home"
    alt_home.mkdir()
    # Build a minimal ~/.claude/hooks/lean-ctx-rewrite-native.sh
    claude_dir = alt_home / ".claude"
    claude_dir.mkdir()
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir()
    hook = hooks_dir / "lean-ctx-rewrite-native.sh"
    hook.write_text("#!/bin/bash\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: alt_home))

    dr = DeferralReport()
    install._check_global_lean_ctx_hooks(dr)

    assert len(dr.entries) == 1
    assert str(hook) in dr.entries[0].detected
    captured = capsys.readouterr()
    assert "lean-ctx" in captured.err.lower()
