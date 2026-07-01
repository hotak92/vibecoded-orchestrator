# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.71 Track T-WT — worktree-guard.sh WorktreeCreate hook tests.

The WorktreeCreate hook is the Layer-0 deterministic gate for the
worktree-isolation silent-fallback safeguard (see
``.claude/context/audits/worktree-isolation-safeguard-design-2026-06-30.md``).
It receives the proposed worktree path on stdin and MUST echo the absolute
worktree path on stdout (the harness contract), EXCEPT on the one
unambiguous violation (proposed path == parent checkout toplevel), where —
once VCT_WORKTREE_GUARD_ENFORCE is set — it blocks.

Decision matrix exercised here (mirror of the design audit §4):
  * not-a-repo            → echo proposed path through, exit 0 (no-op)
  * path == parent root   → VIOLATION
        - default (log-only): echo through + JSONL `violation_logged_only`
        - ENFORCE mode:       block (exit non-zero, no path on stdout)
  * separate path         → echo through, JSONL `pass`
  * dirty parent          → WARN not block, echo through, JSONL
                            `warn_dirty_parent`
  * monorepo / subdir     → uses `git rev-parse --show-toplevel`, so the
                            toplevel resolves to the REAL repo root even
                            when CLAUDE_PROJECT_DIR is a subdirectory.
  * VCT_DISABLE_HOOKS     → full no-op (exit 0, no output)
  * full raw payload      → always captured into the JSONL log so the
                            integrator can verify the live schema.

POSIX-only (the hook is bash). The .ps1 sibling is covered for stdout-path
parity by test_worktree_guard_parity.py + the hook-parity CI gate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / "templates" / "hooks" / "worktree-guard.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook is POSIX-only; .ps1 sibling covers Windows.",
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
    """Create a real git repo with one empty commit (clean tree)."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "commit", "--allow-empty", "-q", "-m", "seed")


def _run_hook(
    payload: dict | str,
    project_dir: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if extra_env:
        env.update(extra_env)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _read_log_rows(project_dir: Path) -> list[dict]:
    log = project_dir / ".claude" / "logs" / "worktree-guard.jsonl"
    if not log.exists():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def test_hook_file_exists_and_executable() -> None:
    assert HOOK_PATH.is_file(), f"missing hook: {HOOK_PATH}"
    assert os.access(HOOK_PATH, os.X_OK), "worktree-guard.sh must be +x"


def test_not_a_repo_echoes_through_and_noops(tmp_path: Path) -> None:
    # No git repo anywhere. Proposed path echoed back unchanged; exit 0.
    proposed = str(tmp_path / "wt" / "agent-x")
    proc = _run_hook({"worktree_path": proposed}, tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed)), proc.stdout
    rows = _read_log_rows(tmp_path)
    assert rows and rows[-1]["decision"] == "noop"
    assert rows[-1]["reason"] == "not_a_repo"


def test_separate_path_passes_and_echoes(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proposed = str(tmp_path / "worktrees" / "agent-abc")
    proc = _run_hook({"worktree_path": proposed}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "pass"


def test_path_equals_parent_is_violation_logged_only_by_default(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    # Proposed path == the repo toplevel → clear violation. Default mode is
    # log-only: still echo through (don't break a create while the contract
    # is unverified), but log loudly.
    proc = _run_hook({"worktree_path": str(repo)}, repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(repo.resolve())
    assert "WARNING" in proc.stderr
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "violation_logged_only"


def test_path_equals_parent_blocks_when_enforce_set(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"worktree_path": str(repo)},
        repo,
        extra_env={"VCT_WORKTREE_GUARD_ENFORCE": "1"},
    )
    # ENFORCE mode: BLOCK — non-zero exit, NO path on stdout, reason on stderr.
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
    assert "BLOCK" in proc.stderr
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "block"


def test_dirty_parent_warns_not_blocks(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    # Make the parent dirty.
    (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    proposed = str(tmp_path / "worktrees" / "agent-dirty")
    proc = _run_hook({"worktree_path": proposed}, repo)
    # Dirty parent + a SEPARATE proposed path → WARN, still echo through.
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))
    assert "WARNING" in proc.stderr
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "warn_dirty_parent"


def test_dirty_parent_blocks_under_strict_plus_enforce(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    proposed = str(tmp_path / "worktrees" / "agent-strict")
    proc = _run_hook(
        {"worktree_path": proposed},
        repo,
        extra_env={
            "VCT_WORKTREE_GUARD_STRICT": "1",
            "VCT_WORKTREE_GUARD_ENFORCE": "1",
        },
    )
    # Strict alone never blocks; strict + enforce upgrades dirty-parent WARN
    # to a block.
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
    assert "BLOCK (strict)" in proc.stderr


def test_strict_without_enforce_only_warns(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    proposed = str(tmp_path / "worktrees" / "agent-strict-only")
    proc = _run_hook(
        {"worktree_path": proposed},
        repo,
        extra_env={"VCT_WORKTREE_GUARD_STRICT": "1"},
    )
    # Strict WITHOUT enforce must NOT block — we never block before the
    # stdout contract is verified on a live spawn.
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))


def test_monorepo_subdir_resolves_real_toplevel(tmp_path: Path) -> None:
    # Repo at `mono/`; project dir is the subdir `mono/sub/`. The hook must
    # resolve the toplevel to `mono/`, NOT treat `mono/sub` as the repo root.
    mono = tmp_path / "mono"
    _init_repo(mono)
    subdir = mono / "sub"
    subdir.mkdir()
    # Proposed worktree path EQUALS the real toplevel `mono/` → violation,
    # even though CLAUDE_PROJECT_DIR is the subdir. This proves we use
    # `git rev-parse --show-toplevel` and not the project dir.
    proc = _run_hook({"worktree_path": str(mono)}, subdir)
    assert proc.returncode == 0  # default log-only
    rows = _read_log_rows(subdir)
    # Log lives under the PROJECT_DIR (subdir) .claude/logs.
    assert rows[-1]["decision"] == "violation_logged_only"
    assert rows[-1]["resolved_path"] == str(mono.resolve())


def test_monorepo_subdir_separate_path_passes(tmp_path: Path) -> None:
    mono = tmp_path / "mono"
    _init_repo(mono)
    subdir = mono / "sub"
    subdir.mkdir()
    proposed = str(tmp_path / "wt" / "agent-mono")
    proc = _run_hook({"worktree_path": proposed}, subdir)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))
    rows = _read_log_rows(subdir)
    assert rows[-1]["decision"] == "pass"


def test_no_path_in_payload_echoes_nothing_and_noops(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook({"session_id": "abc", "agent_id": "x"}, repo)
    assert proc.returncode == 0
    # No path to validate → echo nothing (harness uses its default).
    assert proc.stdout.strip() == ""
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "noop"
    assert rows[-1]["reason"] == "no_proposed_path_parsed"


def test_malformed_json_echoes_nothing_and_noops(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook("{not json", repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_empty_stdin_is_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook("", repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_vct_disable_hooks_full_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"worktree_path": str(repo)},  # would otherwise be a violation
        repo,
        extra_env={"VCT_DISABLE_HOOKS": "1", "VCT_WORKTREE_GUARD_ENFORCE": "1"},
    )
    # Global bypass: full no-op, no output, no log row.
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert _read_log_rows(repo) == []


def test_path_synonyms_are_tolerated(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proposed = str(tmp_path / "wt" / "agent-syn")
    # Use the `path` synonym rather than `worktree_path`.
    proc = _run_hook({"path": proposed}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(Path(proposed))


def test_full_raw_payload_captured_in_log(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {
        "worktree_path": str(tmp_path / "wt" / "agent-cap"),
        "session_id": "sess-123",
        "agent_id": "agent-cap",
        "novel_field_for_schema_discovery": "VALUE-MARKER-XYZ",
    }
    proc = _run_hook(payload, repo)
    assert proc.returncode == 0
    rows = _read_log_rows(repo)
    # The integrator verifies the live schema from the captured raw payload.
    assert "VALUE-MARKER-XYZ" in rows[-1]["raw_payload"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
