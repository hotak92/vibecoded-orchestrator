# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""WS-4 Finding 3: the analyzer must not index an EPHEMERAL agent git-worktree.

Agent worktrees created by ``git worktree add /tmp/vco-track-*`` (a linked
worktree has ``.git`` as a FILE, not a directory) were once analyzed as a root
/ ``--extra-path`` and leaked ~34,347 throwaway rows into the persistent
``*_CodeFunction`` collection, with garbage ``full_name``s like
``20260611_210029___tmp_vco-track-D_install._start_services``.

The guard is deliberately NARROW — it skips only a git LINKED WORKTREE under
the system temp dir. A plain temp directory, a ``git init`` repo in temp
(``.git`` is a dir), or a ``git clone`` into temp (``.git`` dir) are legitimate
analysis roots and must NOT be skipped (an earlier "skip ALL temp roots"
version silently no-op'd legit temp analysis — incl. the test fixtures in
``test_analyze_code_graph_*`` that run the analyzer on a pytest ``tmp_path``).
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


# ── _is_under_temp_dir (the location primitive) ──────────────────────────
def test_temp_subdir_is_under_temp(acg) -> None:
    assert acg._is_under_temp_dir(Path(tempfile.gettempdir()) / "vco-track-D") is True


def test_real_repo_root_is_not_under_temp(acg) -> None:
    assert acg._is_under_temp_dir(REPO_ROOT) is False


# ── _is_ephemeral_worktree_root (the actual guard predicate) ─────────────
def test_temp_linked_worktree_is_ephemeral(acg, tmp_path) -> None:
    # tmp_path is under the system temp dir; a `.git` FILE marks a linked worktree.
    wt = tmp_path / "vco-track-D"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /main/.git/worktrees/vco-track-D\n")
    assert acg._is_ephemeral_worktree_root(wt) is True


def test_temp_git_init_repo_is_not_ephemeral(acg, tmp_path) -> None:
    # A real repo (.git is a DIR) in temp is a legitimate root — never skipped.
    repo = tmp_path / "realrepo"
    (repo / ".git").mkdir(parents=True)
    assert acg._is_ephemeral_worktree_root(repo) is False


def test_temp_plain_dir_is_not_ephemeral(acg, tmp_path) -> None:
    # The fixture shape used by test_analyze_code_graph_* — must NOT be skipped.
    plain = tmp_path / "fake_repo"
    plain.mkdir()
    assert acg._is_ephemeral_worktree_root(plain) is False


def test_real_repo_root_is_not_ephemeral(acg) -> None:
    assert acg._is_ephemeral_worktree_root(REPO_ROOT) is False


# ── wiring presence (reliable without a live-Weaviate subprocess) ────────
def test_guard_and_flag_are_wired() -> None:
    src = ANALYZER.read_text(encoding="utf-8")
    assert "--allow-temp-root" in src, "the --allow-temp-root override flag is missing"
    assert "_is_ephemeral_worktree_root(repo_path)" in src, "primary-root guard is missing"
    assert "_is_ephemeral_worktree_root(ep)" in src, "extra-path filter is missing"
