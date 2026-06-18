# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WS-4 Finding 3: the code-graph analyzer must not index a temp-dir root.

Agent git-worktrees created under the system temp dir (``/tmp/vco-track-*``)
were once analyzed as a root / ``--extra-path`` and leaked ~34,347 throwaway
rows into the persistent ``*_CodeFunction`` collection, with garbage
``full_name``s like ``20260611_210029___tmp_vco-track-D_install._start_services``.
The existing ``worktrees`` ignore-dir only skips ``.claude/worktrees/`` DURING
the walk; it does not catch a temp path passed AS the root/extra-path.

``_is_under_temp_dir`` + the ``main()`` guard (default-on, ``--allow-temp-root``
to override) close that recurrence vector. These tests pin the helper's logic
and the presence of the wiring.
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


@pytest.fixture(scope="module")
def acg():
    if not ANALYZER.exists():
        pytest.skip("analyze_code_graph.py not found")
    spec = importlib.util.spec_from_file_location("acg_under_test", ANALYZER)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # heavy analyzer deps missing in this env
        pytest.skip(f"analyzer module not importable here: {e}")
    return module


def test_temp_subdir_is_under_temp(acg) -> None:
    # The exact pollution vector: an agent worktree under /tmp.
    assert acg._is_under_temp_dir(Path(tempfile.gettempdir()) / "vco-track-D") is True


def test_temp_dir_itself_is_under_temp(acg) -> None:
    assert acg._is_under_temp_dir(Path(tempfile.gettempdir())) is True


def test_real_repo_root_is_not_under_temp(acg) -> None:
    # The actual orchestrator checkout — a persistent, legitimate root.
    assert acg._is_under_temp_dir(REPO_ROOT) is False


def test_guard_and_flag_are_wired() -> None:
    # Source-level presence check (reliable without a live-Weaviate subprocess):
    # the flag, the primary-root guard, and the extra-path filter must exist.
    src = ANALYZER.read_text(encoding="utf-8")
    assert "--allow-temp-root" in src, "the --allow-temp-root override flag is missing"
    assert "_is_under_temp_dir(repo_path)" in src, "primary-root temp guard is missing"
    assert "_is_under_temp_dir(ep)" in src, "extra-path temp filter is missing"
