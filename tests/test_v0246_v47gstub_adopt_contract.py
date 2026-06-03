"""V47-G-stub contract tests for v0.2.46 Part 2 third-party adoption mode.

This stub defines the public contract Wave-2 agents (V47-A through V47-F)
wire their gap-specific code against:

  - args.adopt_project / args.no_adopt_project /
    args.adopt_project_replace_all / args.adopt_project_dry_run
    (mutually-exclusive argparse group)

  - _resolve_adopt_project_mode(args) -> "adopt" | "no-adopt"
    | "replace-all" | "dry-run" | None

  - adopt_project_mode: str | None = None
    (optional kwarg threaded to _venv_triage, _configure_claude_settings)

These tests pin the contract — they MUST keep passing for Wave 2's edits to
land cleanly. Wave 2 agents add behavior INSIDE _venv_triage,
_configure_claude_settings, etc.; they do not change the contract signature
defined here.

Stub scope:
  - Detection heuristic + interactive prompt → V47-G-final (later)
  - Symlinks / secrets / settings-merge / venv-guard / compose-scan /
    project-name precedence → Wave 2 agents (V47-A through V47-F)
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# Load install.py as a module without importing its argparse-at-top-level
# side effects. Pattern matches other V46 test files.
_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47gstub", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47gstub"] = install_py
_spec.loader.exec_module(install_py)


# ---------------------------------------------------------------------------
# Section 1: _resolve_adopt_project_mode — explicit flag dispatch
# ---------------------------------------------------------------------------

def test_resolve_returns_adopt_when_flag_set():
    args = SimpleNamespace(
        adopt_project=True,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
    )
    assert install_py._resolve_adopt_project_mode(args) == "adopt"


def test_resolve_returns_no_adopt_when_flag_set():
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=True,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
    )
    assert install_py._resolve_adopt_project_mode(args) == "no-adopt"


def test_resolve_returns_replace_all_when_flag_set():
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=True,
        adopt_project_dry_run=False,
    )
    assert install_py._resolve_adopt_project_mode(args) == "replace-all"


def test_resolve_returns_dry_run_when_flag_set():
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=True,
    )
    assert install_py._resolve_adopt_project_mode(args) == "dry-run"


def test_resolve_returns_none_when_no_flag_set():
    """The None branch is the hook V47-G-final replaces with the detection
    heuristic + interactive prompt. Today it means "proceed normally"."""
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=False,
        adopt_project_dry_run=False,
    )
    assert install_py._resolve_adopt_project_mode(args) is None


def test_resolve_uses_getattr_default_false_for_missing_attrs():
    """The helper must not crash if args is missing one of the four
    attributes (e.g., older argparse Namespace from a partial parse).
    """
    args = SimpleNamespace()  # no attributes at all
    assert install_py._resolve_adopt_project_mode(args) is None


def test_dry_run_takes_precedence_over_replace_all():
    """Mutually-exclusive group prevents this at argparse layer; the
    helper precedence is documented as a defense-in-depth fallback."""
    args = SimpleNamespace(
        adopt_project=False,
        no_adopt_project=False,
        adopt_project_replace_all=True,
        adopt_project_dry_run=True,
    )
    # Per helper implementation order: dry-run is checked first
    assert install_py._resolve_adopt_project_mode(args) == "dry-run"


# ---------------------------------------------------------------------------
# Section 2: function signatures — adopt_project_mode kwarg presence
# ---------------------------------------------------------------------------

def test_venv_triage_accepts_adopt_project_mode_kwarg():
    """Wave-2 V47-D (Gap D) wires the actual guard logic here."""
    sig = inspect.signature(install_py._venv_triage)
    assert "adopt_project_mode" in sig.parameters
    # Must be optional with default None so existing callers don't break.
    assert sig.parameters["adopt_project_mode"].default is None


def test_configure_claude_settings_accepts_adopt_project_mode_kwarg():
    """Wave-2 V47-A (Gap A) wires the managed-block merge here."""
    sig = inspect.signature(install_py._configure_claude_settings)
    assert "adopt_project_mode" in sig.parameters
    assert sig.parameters["adopt_project_mode"].default is None


def test_venv_triage_default_call_unchanged_for_existing_callers():
    """Backwards-compat: _venv_triage(path) must still work without the
    new kwarg (the kwarg is optional). This guards Wave 1 callers."""
    # Just verify the call shape without actually running it
    # (it does subprocess.run + filesystem checks). Use a non-existent
    # path so it hits the "missing venv" short-circuit immediately.
    result = install_py._venv_triage(Path("/nonexistent/path/no-venv"))
    assert isinstance(result, dict)
    assert "action" in result


# ---------------------------------------------------------------------------
# Section 3: argparse contract — flags parse correctly
# ---------------------------------------------------------------------------

def _build_minimal_parser_for_adopt_flags() -> argparse.ArgumentParser:
    """Mirror the argparse group V47-G-stub added to install.py:main()
    so tests don't depend on running main(). The contract test is that
    these four flag declarations parse the way install.py expects.
    """
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--adopt-project", action="store_true", default=False)
    group.add_argument("--no-adopt-project", action="store_true", default=False)
    group.add_argument("--adopt-project-replace-all", action="store_true", default=False)
    group.add_argument("--adopt-project-dry-run", action="store_true", default=False)
    return parser


def test_argparse_adopt_project_parses_correctly():
    args = _build_minimal_parser_for_adopt_flags().parse_args(["--adopt-project"])
    assert args.adopt_project is True
    assert args.no_adopt_project is False


def test_argparse_no_adopt_project_parses_correctly():
    args = _build_minimal_parser_for_adopt_flags().parse_args(["--no-adopt-project"])
    assert args.no_adopt_project is True
    assert args.adopt_project is False


def test_argparse_replace_all_parses_correctly():
    args = _build_minimal_parser_for_adopt_flags().parse_args(
        ["--adopt-project-replace-all"]
    )
    assert args.adopt_project_replace_all is True


def test_argparse_dry_run_parses_correctly():
    args = _build_minimal_parser_for_adopt_flags().parse_args(
        ["--adopt-project-dry-run"]
    )
    assert args.adopt_project_dry_run is True


def test_argparse_no_flag_defaults_all_false():
    args = _build_minimal_parser_for_adopt_flags().parse_args([])
    assert args.adopt_project is False
    assert args.no_adopt_project is False
    assert args.adopt_project_replace_all is False
    assert args.adopt_project_dry_run is False


def test_argparse_mutually_exclusive_flags_raise():
    """argparse SystemExits on parse_args when mutex group is violated."""
    import pytest
    parser = _build_minimal_parser_for_adopt_flags()
    with pytest.raises(SystemExit):
        parser.parse_args(["--adopt-project", "--no-adopt-project"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--adopt-project", "--adopt-project-dry-run"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--adopt-project-replace-all", "--no-adopt-project"])


# ---------------------------------------------------------------------------
# Section 4: end-to-end resolve via real argparse (smoke test)
# ---------------------------------------------------------------------------

def test_end_to_end_argparse_to_resolve_adopt():
    args = _build_minimal_parser_for_adopt_flags().parse_args(["--adopt-project"])
    assert install_py._resolve_adopt_project_mode(args) == "adopt"


def test_end_to_end_argparse_to_resolve_no_adopt():
    args = _build_minimal_parser_for_adopt_flags().parse_args(["--no-adopt-project"])
    assert install_py._resolve_adopt_project_mode(args) == "no-adopt"


def test_end_to_end_argparse_to_resolve_replace_all():
    args = _build_minimal_parser_for_adopt_flags().parse_args(
        ["--adopt-project-replace-all"]
    )
    assert install_py._resolve_adopt_project_mode(args) == "replace-all"


def test_end_to_end_argparse_to_resolve_dry_run():
    args = _build_minimal_parser_for_adopt_flags().parse_args(
        ["--adopt-project-dry-run"]
    )
    assert install_py._resolve_adopt_project_mode(args) == "dry-run"


def test_end_to_end_argparse_to_resolve_none():
    args = _build_minimal_parser_for_adopt_flags().parse_args([])
    assert install_py._resolve_adopt_project_mode(args) is None
