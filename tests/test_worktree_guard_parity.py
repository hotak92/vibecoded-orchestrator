# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 Track T5-2 — cross-OS parity for the worktree-guard CREATE contract.

The WorktreeCreate hook is responsible for CREATING the worktree (per the
official Claude Code Hooks Reference — "Replaces default git behavior"), not
merely validating a path. worktree-guard.sh and worktree-guard.ps1 MUST behave
identically on that create contract. PowerShell isn't reliably installed on
Linux CI, so this file pins parity two ways:

1. STATIC (always runs): assert both scripts share the load-bearing decision
   invariants — the create path convention (`<toplevel>/.claude/worktrees/<id>`),
   the detached-HEAD `git worktree add`, the identifier synonyms
   (`worktree_name` / `name` / ...), the path synonyms (belt-and-suspenders),
   the repo synonyms, the JSONL decision vocabulary, the VCT_DISABLE_HOOKS
   bypass, the full-raw-payload capture, `git rev-parse --show-toplevel`, and
   the "must match" mirror comments both ways.

2. DYNAMIC (only when pwsh/powershell is present): run BOTH scripts against a
   real repo with the SAME real-shaped payload and assert they produce the
   SAME created worktree path (character-for-character), the path exists, and
   `git worktree list` shows it — plus the not-a-repo no-op and idempotent
   re-fire.

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
    # The .ps1 carries a UTF-8 BOM by design (OS-EXEMPT-PARITY); utf-8-sig
    # strips it so substring assertions match cleanly.
    return PS1.read_text(encoding="utf-8-sig")


# ── STATIC parity ──────────────────────────────────────────────────────────


def test_both_files_exist() -> None:
    assert SH.is_file(), f"missing {SH}"
    assert PS1.is_file(), f"missing {PS1}"


def test_both_have_must_match_mirror_comments() -> None:
    # Cross-language-mirror rule: each file must point at the other so they
    # can't silently drift.
    assert "worktree-guard.ps1" in _sh_text(), ".sh must reference its .ps1 sibling"
    assert "worktree-guard.sh" in _ps1_text(), ".ps1 must reference its .sh sibling"


def test_both_create_via_detached_worktree_add() -> None:
    # THE load-bearing behaviour: both create the worktree with a detached
    # HEAD (no branch-name collisions across parallel agents).
    assert "worktree add --detach" in _sh_text()
    assert "worktree add --detach" in _ps1_text()


def test_both_use_worktrees_path_convention() -> None:
    # Same on-disk convention so the SubagentStop reconcile + agents can find
    # the worktree deterministically.
    assert ".claude/worktrees" in _sh_text()
    for token in ("worktrees",):
        assert token in _ps1_text()


def test_both_tolerate_worktree_name_and_name_identifier() -> None:
    # The docs name the identifier `worktree_name`; the live harness sends
    # `name`. Both scripts must tolerate BOTH keys.
    for syn in ("worktree_name", "name"):
        assert syn in _sh_text(), f".sh missing identifier synonym '{syn}'"
        assert syn in _ps1_text(), f".ps1 missing identifier synonym '{syn}'"


def test_both_honor_vct_disable_hooks() -> None:
    assert "VCT_DISABLE_HOOKS" in _sh_text()
    assert "VCT_DISABLE_HOOKS" in _ps1_text()


def test_both_gate_explicit_parent_block_behind_enforce() -> None:
    # ENFORCE still gates the belt-and-suspenders explicit-path==parent block.
    assert "VCT_WORKTREE_GUARD_ENFORCE" in _sh_text()
    assert "VCT_WORKTREE_GUARD_ENFORCE" in _ps1_text()


def test_both_share_jsonl_decision_vocabulary() -> None:
    sh = _sh_text()
    ps = _ps1_text()
    for decision in (
        "noop",
        "not_a_repo",
        "no_worktree_identifier",
        "created",
        "idempotent_existing_worktree",
        "worktree_add_detached_head",
        "create_failed",
        "redirect_parent_path",
        "block",
    ):
        assert decision in sh, f".sh missing decision '{decision}'"
        assert decision in ps, f".ps1 missing decision '{decision}'"


def test_both_share_path_synonyms() -> None:
    # Belt-and-suspenders explicit-path synonyms (a future harness might send
    # a path). Kept identical so both scripts honour the same vocabulary.
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


def _worktree_paths(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        line[len("worktree ") :].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _run_sh(payload: dict, project_dir: Path, extra_env: dict | None = None):
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


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
def test_ps1_creates_worktree_from_real_payload(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {
        "session_id": "s1",
        "cwd": str(repo),
        "hook_event_name": "WorktreeCreate",
        "name": "agent-ps-create",
    }
    proc = _run_ps1(payload, repo)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    # v0.2.74 (M-3): path is `<token>-<sha256[:8]>`. Assert the directory sits
    # under .claude/worktrees/ and its basename starts with the token.
    out_path = Path(out).resolve()
    assert out_path.parent == (repo / ".claude" / "worktrees").resolve()
    assert out_path.name.startswith("agent-ps-create-"), out
    assert out_path.is_dir()
    assert str(out_path) in _worktree_paths(repo)


@needs_ps
def test_sh_and_ps1_produce_same_path(tmp_path: Path) -> None:
    """Parity: identical input → identical created worktree path from both
    implementations (run against separate repos to avoid cross-contamination,
    then compare the repo-relative path)."""
    sh_repo = tmp_path / "sh_proj"
    ps_repo = tmp_path / "ps_proj"
    _init_repo(sh_repo)
    _init_repo(ps_repo)
    ident = "agent-parity-xyz"
    sh_proc = _run_sh(
        {"hook_event_name": "WorktreeCreate", "name": ident, "cwd": str(sh_repo)}, sh_repo
    )
    ps_proc = _run_ps1(
        {"hook_event_name": "WorktreeCreate", "name": ident, "cwd": str(ps_repo)}, ps_repo
    )
    assert sh_proc.returncode == 0, sh_proc.stderr
    assert ps_proc.returncode == 0, ps_proc.stderr
    # Compare the tail after the repo root — must be identical (THE parity
    # assertion: sh and ps1 derive the SAME path, incl. the M-3 sha256[:8] hash
    # suffix, from the same raw id).
    sh_rel = Path(sh_proc.stdout.strip()).relative_to(sh_repo.resolve())
    ps_rel = Path(ps_proc.stdout.strip()).relative_to(ps_repo.resolve())
    assert sh_rel == ps_rel, f"sh={sh_rel} != ps1={ps_rel}"
    # And the shape is `.claude/worktrees/<ident>-<8hex>`.
    assert sh_rel.parent == Path(".claude/worktrees")
    assert sh_rel.name.startswith(ident + "-")
    assert len(sh_rel.name) == len(ident) + 1 + 8, sh_rel.name


@needs_ps
def test_ps1_not_a_repo_graceful_noop(tmp_path: Path) -> None:
    proc = _run_ps1(
        {"hook_event_name": "WorktreeCreate", "name": "agent-x", "cwd": str(tmp_path)}, tmp_path
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


@needs_ps
def test_ps1_idempotent_refire(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {"hook_event_name": "WorktreeCreate", "name": "agent-ps-refire", "cwd": str(repo)}
    p1 = _run_ps1(payload, repo)
    p2 = _run_ps1(payload, repo)
    assert p1.returncode == 0 and p2.returncode == 0, (p1.stderr, p2.stderr)
    assert p1.stdout.strip() == p2.stdout.strip()
    assert len(_worktree_paths(repo)) == 2


@needs_ps
def test_ps1_explicit_path_equals_parent_blocks_under_enforce(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_ps1(
        {"hook_event_name": "WorktreeCreate", "worktree_path": str(repo), "cwd": str(repo)},
        repo,
        extra_env={"VCT_WORKTREE_GUARD_ENFORCE": "1"},
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
