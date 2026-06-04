"""V47-G-final tests for v0.2.46 Part 2: detection heuristic + interactive prompt.

V47-G-final replaces V47-G-stub's None/placeholder branches with:

1. ``_detect_third_party_project(install_path)`` — heuristic scan that returns
   None (no signal → proceed normally) or a dict describing detected signals.
2. ``_prompt_adopt_decision(detection, args)`` — interactive modal-style
   prompt with Y/cancel/details and safe non-TTY defaults.
3. ``_print_adopt_dry_run_manifest(install_path)`` — real dry-run replacing
   V47-G-stub's placeholder.
4. ``_resolve_adopt_project_mode(args, install_path=None)`` — extended
   signature that calls (1)+(2) when no explicit flag is set AND
   ``install_path`` is provided. Backward-compatible: when ``install_path``
   is None (the V47-G-stub contract surface), still returns None.

These tests cover all three sub-deliverables + the safety defaults.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


# Load install.py as a module so we can call helpers directly.
_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47gfinal", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47gfinal"] = install_py
_spec.loader.exec_module(install_py)


# ---------------------------------------------------------------------------
# Section 1: _detect_third_party_project — detection heuristic
# ---------------------------------------------------------------------------

def test_detect_returns_none_for_empty_dir(tmp_path: Path):
    """No detection signals at all → None (= proceed normally, fresh project)."""
    assert install_py._detect_third_party_project(tmp_path) is None


def test_detect_returns_none_for_nonexistent_path(tmp_path: Path):
    """Hardened against missing path: returns None rather than raising."""
    missing = tmp_path / "does-not-exist"
    assert install_py._detect_third_party_project(missing) is None


def test_detect_returns_none_when_vco_manifest_present(tmp_path: Path):
    """If a WELL-FORMED .vco-manifest.json present → project is already
    VCO-managed, never prompt. This is the ``manifest_present`` short-circuit.

    v0.2.46 post-adversarial L2 tightened the shape check: an empty /
    unparseable / unrecognized-shape manifest is now classified "broken"
    rather than "valid" and proceeds with detection (so the user sees
    they have a broken state). To pin the original V47-G-final intent
    here, the manifest must contain at least one of the
    `_V47G_MANIFEST_EXPECTED_KEYS` (vco_version / schema_version /
    files / bundled_files).
    """
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".vco-manifest.json").write_text(
        '{"vco_version": "0.2.46"}'
    )
    # Add an obvious signal that would normally trigger detection:
    (tmp_path / "CLAUDE.md").write_text("# project notes")
    assert install_py._detect_third_party_project(tmp_path) is None


def test_detect_picks_up_existing_claude_dir(tmp_path: Path):
    """`.claude/` with at least one file → triggers detection."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "agents").mkdir()
    (tmp_path / ".claude" / "agents" / "foo.md").write_text("# foo")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert "signals" in detection
    assert any("claude" in s.lower() for s in detection["signals"])


def test_detect_picks_up_existing_claude_md(tmp_path: Path):
    """Non-empty CLAUDE.md → triggers detection."""
    (tmp_path / "CLAUDE.md").write_text("# my project")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert any("CLAUDE.md" in s for s in detection["signals"])


def test_detect_ignores_empty_claude_md(tmp_path: Path):
    """Empty CLAUDE.md alone should NOT trigger detection (no content)."""
    (tmp_path / "CLAUDE.md").write_text("")
    # No other signals.
    assert install_py._detect_third_party_project(tmp_path) is None


def test_detect_picks_up_env_file(tmp_path: Path):
    """`.env` with content → triggers detection."""
    (tmp_path / ".env").write_text("FOO=bar\n")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert any(".env" in s for s in detection["signals"])


def test_detect_picks_up_venv(tmp_path: Path):
    """`.venv/` directory → triggers detection."""
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert any("venv" in s.lower() for s in detection["signals"])


def test_detect_picks_up_knowledge_dir_with_md_files(tmp_path: Path):
    """`knowledge/` with `.md` files → triggers detection."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "notes.md").write_text("# my notes")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert any("knowledge" in s.lower() for s in detection["signals"])


def test_detect_aggregates_multiple_signals(tmp_path: Path):
    """Multiple signals → all listed in detection dict."""
    (tmp_path / "CLAUDE.md").write_text("# my project")
    (tmp_path / ".env").write_text("API_KEY=sk-1234\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    detection = install_py._detect_third_party_project(tmp_path)
    assert detection is not None
    assert len(detection["signals"]) >= 3
    assert detection.get("manifest_present") is False


def test_detect_includes_summary_string(tmp_path: Path):
    """Detection dict carries a brief summary suitable for one-line logging."""
    (tmp_path / "CLAUDE.md").write_text("# my project")
    detection = install_py._detect_third_party_project(tmp_path)
    assert "summary" in detection
    assert isinstance(detection["summary"], str)
    assert detection["summary"]  # non-empty


# ---------------------------------------------------------------------------
# Section 2: _prompt_adopt_decision — interactive prompt
# ---------------------------------------------------------------------------

def _detection_fixture() -> dict:
    return {
        "signals": [
            "CLAUDE.md (existing project instructions)",
            ".env (with 2 secret-shaped keys)",
            ".venv/ (Python 3.12, ~767 packages)",
            ".claude/ (existing artifacts)",
        ],
        "summary": "4 signals detected",
        "manifest_present": False,
    }


def test_prompt_returns_adopt_on_choice_1(capsys, monkeypatch):
    """User picks '1' (Adopt) → returns 'adopt'."""
    args = SimpleNamespace(yes=False, quiet=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    # Force interactive (the prompt checks isatty).
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "adopt"


def test_prompt_returns_no_adopt_on_choice_2(monkeypatch):
    """User picks '2' (Cancel) → returns 'no-adopt'."""
    args = SimpleNamespace(yes=False, quiet=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("2\n"))
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "no-adopt"


def test_prompt_loops_on_choice_3_then_accepts(monkeypatch):
    """User picks '3' (Show details), then '1' → 'adopt'."""
    args = SimpleNamespace(yes=False, quiet=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("3\n1\n"))
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "adopt"


def test_prompt_loops_on_invalid_then_accepts(monkeypatch):
    """Invalid input → re-prompt; eventually '1' → 'adopt'."""
    args = SimpleNamespace(yes=False, quiet=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("xyz\n9\n1\n"))
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "adopt"


def test_prompt_safe_default_no_adopt_on_non_tty(monkeypatch):
    """Non-TTY stdin → default to 'no-adopt' (NEVER auto-adopt without
    explicit consent; safer than auto-adopt in CI).
    """
    args = SimpleNamespace(yes=False, quiet=False)
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "no-adopt"


def test_prompt_safe_default_no_adopt_when_yes_flag(monkeypatch):
    """--yes flag → default to 'no-adopt' (--yes shouldn't silently adopt
    a 3rd-party tree; user must explicitly --adopt-project).
    """
    args = SimpleNamespace(yes=True, quiet=False)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "no-adopt"


def test_prompt_safe_default_no_adopt_when_quiet_flag(monkeypatch):
    """--quiet → default to 'no-adopt' (no prompt possible)."""
    args = SimpleNamespace(yes=False, quiet=True)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "no-adopt"


def test_prompt_eof_returns_no_adopt(monkeypatch):
    """EOF mid-prompt → degrade to 'no-adopt' (no consent → don't adopt)."""
    args = SimpleNamespace(yes=False, quiet=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        result = install_py._prompt_adopt_decision(_detection_fixture(), args)
    assert result == "no-adopt"


# ---------------------------------------------------------------------------
# Section 3: _print_adopt_dry_run_manifest — real dry-run
# ---------------------------------------------------------------------------

def test_dry_run_manifest_emits_sections(tmp_path: Path, capsys):
    """Dry-run prints a section for each gap (A through F)."""
    (tmp_path / "CLAUDE.md").write_text("# project")
    (tmp_path / ".env").write_text("FOO=bar\n")
    install_py._print_adopt_dry_run_manifest(tmp_path)
    out = capsys.readouterr().out
    # We don't require a specific format, but each gap should be mentioned.
    assert "settings" in out.lower() or "Gap A" in out
    assert "symlink" in out.lower() or "Gap B" in out
    assert "secret" in out.lower() or "Gap C" in out
    assert "venv" in out.lower() or "Gap D" in out
    assert "compose" in out.lower() or "Gap E" in out
    assert "project name" in out.lower() or "Gap F" in out


def test_dry_run_manifest_does_not_raise_on_empty_tree(tmp_path: Path):
    """Empty install path — still emits manifest, no crash."""
    install_py._print_adopt_dry_run_manifest(tmp_path)  # no exception


def test_dry_run_manifest_handles_nonexistent_path(tmp_path: Path):
    """Hardened against missing install_path."""
    missing = tmp_path / "missing"
    install_py._print_adopt_dry_run_manifest(missing)  # no exception


# ---------------------------------------------------------------------------
# Section 4: _resolve_adopt_project_mode extended signature — install_path
#            kwarg drives the V47-G-final detection branch
# ---------------------------------------------------------------------------

def test_resolve_with_install_path_and_no_flag_no_signal_returns_none(tmp_path: Path):
    """Empty tree + no flag → still None (preserve V47-G-stub contract)."""
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
    )
    result = install_py._resolve_adopt_project_mode(args, install_path=tmp_path)
    assert result is None


def test_resolve_with_install_path_signal_non_tty_returns_no_adopt(tmp_path: Path):
    """Detection signal + non-TTY → resolver auto-prompts → safe default
    'no-adopt' (CI-safe path; never adopt without explicit consent).
    """
    (tmp_path / "CLAUDE.md").write_text("# my project")
    args = SimpleNamespace(
        adopt_project=False, no_adopt_project=False,
        adopt_project_replace_all=False, adopt_project_dry_run=False,
        yes=False, quiet=False,
    )
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        result = install_py._resolve_adopt_project_mode(args, install_path=tmp_path)
    assert result == "no-adopt"


def test_resolve_with_install_path_explicit_flag_skips_detection(tmp_path: Path):
    """Explicit --adopt-project flag → return 'adopt' without running detection
    (the user already said yes — don't second-guess them).
    """
    (tmp_path / "CLAUDE.md").write_text("# my project")
    args = SimpleNamespace(
        adopt_project=True,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
        yes=False, quiet=False,
    )
    result = install_py._resolve_adopt_project_mode(args, install_path=tmp_path)
    assert result == "adopt"


def test_resolve_without_install_path_preserves_stub_contract():
    """V47-G-stub contract: no install_path arg → never run detection,
    always return None when no explicit flag. Backward-compat.
    """
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
    )
    # No install_path keyword → must not crash, must return None.
    assert install_py._resolve_adopt_project_mode(args) is None


# ---------------------------------------------------------------------------
# Section 5: smoke test — full dispatch via main()-style invocation
# ---------------------------------------------------------------------------

def test_dry_run_does_not_call_detection_when_flag_explicit(tmp_path: Path):
    """--adopt-project-dry-run is explicit → dispatch returns 'dry-run' without
    running detection. Detection is only for the no-flag case.
    """
    (tmp_path / "CLAUDE.md").write_text("# my project")
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=True,
    )
    result = install_py._resolve_adopt_project_mode(args, install_path=tmp_path)
    assert result == "dry-run"
