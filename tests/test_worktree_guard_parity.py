# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.71 Track T-WT — cross-OS parity for the worktree-guard stdout contract.

The stdout-path contract is the ONE place worktree-guard.sh and
worktree-guard.ps1 MUST behave identically (per the design audit §Cross-OS
shape). PowerShell isn't reliably installed on Linux CI, so this file pins
parity two ways:

1. STATIC (always runs): assert both scripts share the load-bearing decision
   invariants — the single-block violation test (proposed == toplevel), the
   same staged-enable gating env (VCT_WORKTREE_GUARD_ENFORCE), the same
   strict-upgrade env (VCT_WORKTREE_GUARD_STRICT), the same JSONL decision
   vocabulary, the same path/repo synonym sets, the VCT_DISABLE_HOOKS bypass,
   the full-raw-payload capture, and the "must match" mirror comments both
   ways.

2. DYNAMIC (only when pwsh/powershell is present): run the .ps1 against a
   real repo and assert the same stdout-path contract the .sh test pins —
   not-a-repo echoes through, separate path echoes through, path==toplevel
   is violation-logged-only by default and blocks under ENFORCE.

The dynamic half is the authoritative parity check when a PowerShell runtime
exists; the static half is the always-on guard against drift.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SH = REPO_ROOT / "templates" / "hooks" / "worktree-guard.sh"
PS1 = REPO_ROOT / "templates" / "hooks" / "worktree-guard.ps1"


def _sh_text() -> str:
    return SH.read_text(encoding="utf-8")


def _ps1_text() -> str:
    return PS1.read_text(encoding="utf-8")


# ── STATIC parity ──────────────────────────────────────────────────────────


def test_both_files_exist() -> None:
    assert SH.is_file(), f"missing {SH}"
    assert PS1.is_file(), f"missing {PS1}"


def test_both_have_must_match_mirror_comments() -> None:
    # Cross-language-mirror rule: each file must point at the other so they
    # can't silently drift.
    assert "worktree-guard.ps1" in _sh_text(), ".sh must reference its .ps1 sibling"
    assert "worktree-guard.sh" in _ps1_text(), ".ps1 must reference its .sh sibling"


def test_both_gate_block_behind_enforce_env() -> None:
    sh = _sh_text()
    ps = _ps1_text()
    assert "VCT_WORKTREE_GUARD_ENFORCE" in sh
    assert "VCT_WORKTREE_GUARD_ENFORCE" in ps


def test_both_support_strict_env() -> None:
    assert "VCT_WORKTREE_GUARD_STRICT" in _sh_text()
    assert "VCT_WORKTREE_GUARD_STRICT" in _ps1_text()


def test_both_honor_vct_disable_hooks() -> None:
    assert "VCT_DISABLE_HOOKS" in _sh_text()
    assert "VCT_DISABLE_HOOKS" in _ps1_text()


def test_both_share_jsonl_decision_vocabulary() -> None:
    sh = _sh_text()
    ps = _ps1_text()
    for decision in (
        "noop",
        "not_a_repo",
        "violation_logged_only",
        "block",
        "warn_dirty_parent",
        "pass",
    ):
        assert decision in sh, f".sh missing decision '{decision}'"
        assert decision in ps, f".ps1 missing decision '{decision}'"


def test_both_share_path_synonyms() -> None:
    sh = _sh_text()
    ps = _ps1_text()
    for syn in ("worktree_path", "path", "proposed_path", "worktree", "target_path", "dir"):
        assert syn in sh, f".sh missing path synonym '{syn}'"
        assert syn in ps, f".ps1 missing path synonym '{syn}'"


def test_both_share_repo_synonyms() -> None:
    sh = _sh_text()
    ps = _ps1_text()
    for syn in ("repo_root", "repo", "project_root", "cwd", "toplevel"):
        assert syn in sh, f".sh missing repo synonym '{syn}'"
        assert syn in ps, f".ps1 missing repo synonym '{syn}'"


def test_both_capture_full_raw_payload() -> None:
    # The integrator verifies the live schema from this field — must exist in
    # both logs.
    assert "raw_payload" in _sh_text()
    assert "raw_payload" in _ps1_text()


def test_both_use_show_toplevel_for_repo_resolution() -> None:
    # Monorepo / subdir correctness depends on this exact git call.
    assert "rev-parse --show-toplevel" in _sh_text()
    assert "rev-parse --show-toplevel" in _ps1_text()


# ── DYNAMIC parity (only when a PowerShell runtime is present) ──────────────


def _find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


_PS = _find_powershell()
needs_ps = pytest.mark.skipif(
    _PS is None,
    reason="no pwsh/powershell on PATH; static parity test covers drift",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "commit", "--allow-empty", "-q", "-m", "seed")


def _run_ps1(payload: dict, project_dir: Path, extra_env: dict | None = None):
    assert _PS is not None
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [_PS, "-NoProfile", "-File", str(PS1)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@needs_ps
def test_ps1_not_a_repo_echoes_through(tmp_path: Path) -> None:
    proposed = str(tmp_path / "wt" / "agent-x")
    proc = _run_ps1({"worktree_path": proposed}, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))


@needs_ps
def test_ps1_separate_path_echoes_through(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proposed = str(tmp_path / "wt" / "agent-sep")
    proc = _run_ps1({"worktree_path": proposed}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))


@needs_ps
def test_ps1_path_equals_parent_logonly_by_default(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_ps1({"worktree_path": str(repo)}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(repo.resolve())


@needs_ps
def test_ps1_path_equals_parent_blocks_under_enforce(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_ps1(
        {"worktree_path": str(repo)},
        repo,
        extra_env={"VCT_WORKTREE_GUARD_ENFORCE": "1"},
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
