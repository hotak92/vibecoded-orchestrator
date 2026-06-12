# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.40 F5 — canonical ``vco_lib.paths.vct_root_dir`` /
``vco_lib.paths.launcher_db_path`` consolidation.

Five call sites in ``install.py`` (``_vct_state_dir``,
``_resolve_project_id_by_folder``), ``vco_lib/diagram_indexer.py``
(three sites: ``delete_diagram_from_indices``, snapshot helpers,
``snapshot_diagram_file``) and four sites in ``vco_lib/project_init.py``
(``_launcher_db_path`` plus three ``_read_*_from_launcher_db`` helpers)
used to reconstruct ``~/.vct/launcher.db`` inline. After F5 they all
delegate to :func:`vco_lib.paths.launcher_db_path` (or
:func:`vco_lib.paths.vct_root_dir`) so a single fix-point handles
future cross-OS convention changes (macOS / Windows).

Coverage:
  * ``vct_root_dir()`` honours ``VCT_STATE_DIR`` and falls back to
    ``~/.vct``.
  * ``launcher_db_path()`` returns ``vct_root_dir() / "launcher.db"``.
  * ``install.py::_vct_state_dir`` resolves to the same value as
    the canonical helper (delegation, not re-implementation).
  * ``vco_lib/project_init.py::_launcher_db_path`` returns the same
    path as the canonical helper.
  * Empty-string ``VCT_STATE_DIR`` falls back to the default (not
    ``Path("") / "launcher.db"`` — the previous inline form in
    ``diagram_indexer.py`` had this subtle bug).
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# vco_lib.paths — the canonical helpers
# ---------------------------------------------------------------------------


def test_vct_root_dir_returns_dot_vct_under_home_by_default():
    """No env var set → ``~/.vct``."""
    from vco_lib.paths import vct_root_dir

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VCT_STATE_DIR", None)
        result = vct_root_dir()

    assert isinstance(result, Path)
    assert result == Path.home() / ".vct"
    assert result.name == ".vct"


def test_vct_root_dir_honours_vct_state_dir_env_var(tmp_path: Path):
    """``$VCT_STATE_DIR`` overrides the default (multi-launcher dev)."""
    from vco_lib.paths import vct_root_dir

    custom = tmp_path / "alt-state"
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(custom)}):
        result = vct_root_dir()

    assert result == custom


def test_vct_root_dir_treats_empty_env_var_as_unset(tmp_path: Path):
    """Empty / whitespace ``VCT_STATE_DIR`` MUST fall back to ``~/.vct``.

    The previous inline form in ``diagram_indexer.py`` used a bare
    ``os.environ.get("VCT_STATE_DIR")`` (no ``.strip()``) which would
    coerce an empty-string env var into ``Path("") / "launcher.db"`` →
    ``launcher.db`` (relative!), pointing at CWD. Consolidating onto
    ``vct_root_dir`` is also a robustness fix.
    """
    from vco_lib.paths import vct_root_dir

    for empty in ("", "   ", "\t"):
        with mock.patch.dict(os.environ, {"VCT_STATE_DIR": empty}):
            result = vct_root_dir()
        assert result == Path.home() / ".vct", (
            f"empty VCT_STATE_DIR={empty!r} should fall back to ~/.vct, "
            f"got {result}"
        )


def test_launcher_db_path_is_vct_root_dir_slash_launcher_db():
    """``launcher_db_path()`` is the convenience wrapper around
    ``vct_root_dir() / "launcher.db"`` — the contract every refactored
    call site relies on.
    """
    from vco_lib.paths import launcher_db_path, vct_root_dir

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VCT_STATE_DIR", None)
        result = launcher_db_path()
        root = vct_root_dir()

    assert result == root / "launcher.db"
    assert result.name == "launcher.db"


def test_launcher_db_path_honours_vct_state_dir(tmp_path: Path):
    custom = tmp_path / "custom-state"
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(custom)}):
        from vco_lib.paths import launcher_db_path

        result = launcher_db_path()

    assert result == custom / "launcher.db"


# ---------------------------------------------------------------------------
# install.py::_vct_state_dir — must delegate to the canonical helper
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def install_module():
    """Import install.py once per test module (it's a 16k LoC file)."""
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return importlib.import_module("install")


def test_install_vct_state_dir_matches_canonical_resolver(install_module):
    """``install.py::_vct_state_dir`` delegates to
    ``vco_lib.paths.vct_root_dir`` — the two MUST return the same Path
    on every machine, regardless of ``VCT_STATE_DIR`` value.
    """
    from vco_lib.paths import vct_root_dir

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VCT_STATE_DIR", None)
        assert install_module._vct_state_dir() == vct_root_dir()


def test_install_vct_state_dir_matches_canonical_with_env(install_module, tmp_path: Path):
    from vco_lib.paths import vct_root_dir

    custom = tmp_path / "custom"
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(custom)}):
        assert install_module._vct_state_dir() == vct_root_dir()
        assert install_module._vct_state_dir() == custom


# ---------------------------------------------------------------------------
# vco_lib.project_init::_launcher_db_path — must delegate too
# ---------------------------------------------------------------------------


def test_project_init_launcher_db_path_matches_canonical():
    """``vco_lib.project_init._launcher_db_path`` delegates to the
    canonical resolver — kept as a module-local symbol for backward
    compat, but the resolution rule is now centralised.
    """
    from vco_lib.paths import launcher_db_path
    from vco_lib.project_init import _launcher_db_path

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VCT_STATE_DIR", None)
        assert _launcher_db_path() == launcher_db_path()


def test_project_init_launcher_db_path_matches_canonical_with_env(tmp_path: Path):
    from vco_lib.paths import launcher_db_path
    from vco_lib.project_init import _launcher_db_path

    custom = tmp_path / "custom"
    with mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(custom)}):
        assert _launcher_db_path() == launcher_db_path()
        assert _launcher_db_path() == custom / "launcher.db"


# ---------------------------------------------------------------------------
# Regression: no .py file outside `paths.py` / docstrings / tests should
# still reconstruct ``~/.vct/launcher.db`` inline. This is a static
# anti-regression check — if a sibling agent re-introduces an inline
# form, this test fires.
# ---------------------------------------------------------------------------


def test_no_inline_reconstructions_outside_paths_module():
    """Forbid ``Path.home() / ".vct"`` and ``expanduser("~/.vct/...")``
    in production code outside ``vco_lib/paths.py`` (the single source
    of truth) and ``templates/scripts/generate-kg-summary.py`` (which
    documents WHY it can't import vco_lib).

    Path-exclusion semantics (v0.2.44 V44-G3):
      * ``.claude/worktrees/agent-*/`` — transient git-worktree mirrors
        spawned by the multi-agent harness. They contain full copies of
        production code (including ``install.py`` / ``vco_lib/paths.py``)
        that LOOK like violations to a naive rglob walk but aren't —
        they're scratch dirs that get cleaned up between sessions. The
        canonical files inside them are already covered by the main
        repo_root scan + the ``allowed`` set, so re-scanning the mirror
        copies would only produce false-positive duplicates.
      * ``.claude/scripts/`` — gitignored bundle copies of files under
        ``templates/scripts/``. Whatever the template ships, the bundle
        mirrors. The canonical template at
        ``templates/scripts/generate-kg-summary.py`` is already in the
        ``allowed`` set (with documented design rationale for the inline
        construction); flagging the gitignored copy is double-counting.
    """
    import re

    repo_root = Path(__file__).resolve().parent.parent
    # Patterns that mark a REAL inline reconstruction (not a docstring).
    # The canonical resolver lives in ``vco_lib/paths.py`` so it's
    # allowed; everything else in production code is forbidden.
    bad_patterns = [
        re.compile(r'Path\.home\(\)\s*/\s*"\.vct"'),
        re.compile(r"Path\.home\(\)\s*/\s*'\.vct'"),
        re.compile(r'os\.path\.expanduser\(\s*["\']~/\.vct/launcher\.db'),
    ]
    # Files that are allowed to contain the literal reconstruction.
    allowed = {
        repo_root / "vco_lib" / "paths.py",
        repo_root / "templates" / "scripts" / "generate-kg-summary.py",
        # v0.2.53: bootstrap exception. install.py runs BEFORE vco_lib is
        # importable in some flows (it sets up the venv that contains
        # vco_lib). Diagnostic-output functions that reconstruct ~/.vct
        # inline are pre-vco_lib; refactoring to defer the call would
        # require restructuring the install-time logging. Documented
        # exception.
        repo_root / "install.py",
        # v0.2.53: MCP self-isolation exception. MCP servers run from
        # claude_mcp_servers/.venv which does NOT have vco_lib on its
        # path (vco_lib lives in the orchestrator-root venv).
        # _vct_root_dir at claude_mcp_servers/_lib/update_gate.py:50 is
        # explicitly documented as "Mirror of vco_lib.paths.vct_root_dir"
        # — an intentional architectural duplication for MCP isolation,
        # not drift.
        repo_root / "claude_mcp_servers" / "_lib" / "update_gate.py",
        # v0.2.53: defensive fallback exception. trainability_check.py's
        # primary path uses vco_lib.paths.launcher_db_path() (the canonical
        # resolver). The Path.home() / ".vct" reconstruction is INSIDE the
        # `except ImportError` defensive fallback for the partial-install
        # case (vco_lib not importable). The fallback exactly mirrors
        # launcher_db_reader._discover_db_path, which is intentional —
        # this is a diagnostic operator script that must work even on a
        # half-broken install. Documented exception.
        repo_root / "scripts" / "trainability_check.py",
    }

    # Walk production code only (skip tests/, .venv/, archive/).
    skip_dirs = {".venv", "tests", ".git", "node_modules", "archive", "dist"}
    offenders: list[tuple[Path, int, str]] = []
    for py_file in repo_root.rglob("*.py"):
        # Skip third-party / venv / tests.
        rel = py_file.relative_to(repo_root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        # Skip transient worktree mirrors under ``.claude/worktrees/``.
        # By convention this directory holds short-lived scratch copies
        # spawned by the multi-agent harness (full repo trees, not
        # production code). The narrow ``agent-<hexhash>`` match used
        # pre-v0.2.54 missed human-named worktrees (e.g.
        # ``track-W-windows-hub-hang``); the canonical rule is "ignore
        # the entire subtree" — the directory itself is the boundary.
        rel_parts = rel.parts
        if (
            len(rel_parts) >= 3
            and rel_parts[0] == ".claude"
            and rel_parts[1] == "worktrees"
        ):
            continue
        # Skip the gitignored ``.claude/scripts/`` bundle copies. The
        # canonical source for these scripts lives at
        # ``templates/scripts/`` and is already covered by the
        # ``allowed`` set when applicable.
        if len(rel_parts) >= 2 and rel_parts[0] == ".claude" and rel_parts[1] == "scripts":
            continue
        # Skip ``.claude/state/tool_backups/`` — auto-generated pre-edit
        # snapshots created by Claude Code's Edit hook. These are
        # timestamp-prefixed copies of files that get modified; they
        # contain the file's pre-edit state, which by definition may
        # contain the very patterns we just refactored away. They're
        # transient build artefacts, not production code.
        if (
            len(rel_parts) >= 3
            and rel_parts[0] == ".claude"
            and rel_parts[1] == "state"
            and rel_parts[2] == "tool_backups"
        ):
            continue
        if py_file in allowed:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Skip lines that are clearly comments or docstrings about
            # the path — only flag lines that look like real code
            # constructing a Path. The cheap heuristic: skip if the
            # line is just inside a triple-quoted docstring block.
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            for pat in bad_patterns:
                if pat.search(line):
                    offenders.append((rel, lineno, line.strip()))
                    break

    assert not offenders, (
        "Inline ~/.vct reconstructions found — use "
        "vco_lib.paths.vct_root_dir() / vco_lib.paths.launcher_db_path() "
        "instead:\n  "
        + "\n  ".join(f"{p}:{ln}: {txt}" for p, ln, txt in offenders)
    )
