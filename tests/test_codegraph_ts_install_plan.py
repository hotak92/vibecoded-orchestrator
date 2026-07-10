# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for the pure codegraph-ts install DECISION
(``vco_lib.install_companions.codegraph_ts_install_plan``, CG-2 / Part 5).

This is the extracted, testable core of install.py's soft-fail
``_install_codegraph_treesitter`` step (the subprocess run + logging stays in
install.py as irreducible glue). Covers each decision branch: opt-out env,
missing pyproject, and the install path's argv shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.install_companions import (  # noqa: E402
    CODEGRAPH_TS_SKIP_ENV,
    CODEGRAPH_TS_SKIP_NO_PYPROJECT,
    codegraph_ts_install_plan,
)


def test_install_path_returns_extra_argv() -> None:
    """ACT: pyproject present + no opt-out → install with the
    ``<root>[codegraph-ts]`` pip target (pins sourced from pyproject)."""
    should, reason, argv = codegraph_ts_install_plan(
        pyproject_exists=True, skip_env=False, project_root="/opt/vco",
    )
    assert should is True
    assert reason is None
    assert argv == ["/opt/vco[codegraph-ts]"]


def test_env_opt_out_skips() -> None:
    """LEAVE-ALONE: VCT_SKIP_CODEGRAPH_TS=1 → skip, no argv, env reason."""
    should, reason, argv = codegraph_ts_install_plan(
        pyproject_exists=True, skip_env=True, project_root="/opt/vco",
    )
    assert should is False
    assert reason == CODEGRAPH_TS_SKIP_ENV
    assert argv is None


def test_missing_pyproject_skips() -> None:
    """LEAVE-ALONE: no pyproject → skip (the extra's pins live there)."""
    should, reason, argv = codegraph_ts_install_plan(
        pyproject_exists=False, skip_env=False, project_root="/opt/vco",
    )
    assert should is False
    assert reason == CODEGRAPH_TS_SKIP_NO_PYPROJECT
    assert argv is None


def test_env_opt_out_wins_over_missing_pyproject() -> None:
    """Both skip conditions true → the env opt-out reason takes precedence
    (deterministic ordering; either way it skips)."""
    should, reason, _ = codegraph_ts_install_plan(
        pyproject_exists=False, skip_env=True, project_root="/opt/vco",
    )
    assert should is False
    assert reason == CODEGRAPH_TS_SKIP_ENV
