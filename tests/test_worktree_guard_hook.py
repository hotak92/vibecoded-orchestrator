# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.74 Track T5-2 — worktree-guard.sh WorktreeCreate hook tests.

The WorktreeCreate hook is the Layer-0 deterministic gate for the
worktree-isolation silent-fallback safeguard (see
``.claude/context/audits/worktree-isolation-safeguard-design-2026-06-30.md``).

**Contract (verified against the official Claude Code Hooks Reference,
https://code.claude.com/docs/en/hooks.md, 2026-07-06):** the hook is
RESPONSIBLE FOR CREATING the worktree ("Replaces default git behavior"),
not merely validating a path. The real stdin payload carries
``{session_id, transcript_path, cwd, prompt_id, hook_event_name, name}``
where ``name`` is the agent id — there is NO proposed-path field. The hook
must DECIDE the path, CREATE the worktree, print its absolute path on
stdout, and exit 0. Any non-zero exit ABORTS the create.

The pre-v0.2.74 hook was a pure validator that no-op'd when no path was
present (always, for the real payload) → the worktree was never created →
the subagent silently fell back to the shared parent tree. These tests pin
the fixed CREATE behaviour.

Decision matrix exercised here:
  * real payload (no path, key ``name``) → CREATES
    ``<repo>/.claude/worktrees/<sanitized-id>``, echoes its abs path,
    ``git worktree list`` shows it, JSONL ``created``.
  * ``worktree_name`` key (per docs) → same create behaviour.
  * idempotent re-fire (same id twice) → same path, success both times,
    JSONL ``idempotent_existing_worktree``.
  * not-a-repo → graceful no-op (empty stdout, exit 0), JSONL ``not_a_repo``.
  * empty stdin / malformed JSON / no identifier → graceful no-op
    (empty stdout, exit 0) — do NOT fabricate a worktree.
  * ``git worktree add`` failure → LOUD abort (non-zero exit, reason on
    stderr), JSONL ``create_failed``.
  * explicit separate path (belt-and-suspenders) → creates it there.
  * explicit path == parent toplevel → default derives a safe separate
    path; ENFORCE hard-blocks (non-zero exit).
  * monorepo / subdir → uses ``git rev-parse --show-toplevel`` so the
    worktree lands under the REAL repo root.
  * VCT_DISABLE_HOOKS → full no-op (exit 0, no output, no log row).
  * full raw payload → always captured into the JSONL log.

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


# ── PRIMARY: the real payload shape (no path, key `name`) CREATES ──────────


def test_real_payload_creates_worktree(tmp_path: Path) -> None:
    """The live harness payload: no path field, agent id under `name`.

    This is the exact shape captured in SD15/.claude/logs/worktree-guard.jsonl
    that the old validator hook no-op'd on. The fixed hook must CREATE a
    worktree, echo its abs path, and register it in git.
    """
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {
        "session_id": "a78ee020",
        "transcript_path": "/x.jsonl",
        "cwd": str(repo),
        "prompt_id": "bc42d831",
        "hook_event_name": "WorktreeCreate",
        "name": "agent-a10c46d251a62b21d",
    }
    proc = _run_hook(payload, repo)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    expected = repo / ".claude" / "worktrees" / "agent-a10c46d251a62b21d"
    assert out == str(expected.resolve()), out
    # The worktree directory actually exists on disk...
    assert Path(out).is_dir(), f"worktree dir not created: {out}"
    # ...and git knows about it as a separate worktree.
    assert str(expected.resolve()) in _worktree_paths(repo)
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "created"
    assert rows[-1]["reason"] == "worktree_add_detached_head"
    assert rows[-1]["resolved_path"] == str(expected.resolve())


def test_worktree_name_key_also_creates(tmp_path: Path) -> None:
    """The docs name the identifier `worktree_name`; must be tolerated too."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "worktree_name": "agent-docs-key", "cwd": str(repo)},
        repo,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    expected = repo / ".claude" / "worktrees" / "agent-docs-key"
    assert out == str(expected.resolve())
    assert Path(out).is_dir()
    assert str(expected.resolve()) in _worktree_paths(repo)


def test_created_worktree_is_separate_checkout(tmp_path: Path) -> None:
    """A commit in the created worktree must NOT land on the parent branch —
    the whole point of isolation. Detached HEAD guarantees this."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "agent-iso", "cwd": str(repo)},
        repo,
    )
    wt = Path(proc.stdout.strip())
    parent_head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Commit inside the worktree.
    (wt / "f.txt").write_text("x", encoding="utf-8")
    _git(wt, "add", "f.txt")
    _git(wt, "commit", "-q", "-m", "in-worktree")
    parent_head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent_head_before == parent_head_after, (
        "commit in worktree leaked onto the parent branch — isolation broken"
    )


def test_idempotent_refire_same_path_success(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {"hook_event_name": "WorktreeCreate", "name": "agent-refire", "cwd": str(repo)}
    p1 = _run_hook(payload, repo)
    p2 = _run_hook(payload, repo)
    assert p1.returncode == 0 and p2.returncode == 0, (p1.stderr, p2.stderr)
    assert p1.stdout.strip() == p2.stdout.strip()
    # Exactly one extra worktree (main + the one agent worktree).
    assert len(_worktree_paths(repo)) == 2
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "created"
    assert rows[-1]["reason"] == "idempotent_existing_worktree"


# ── GRACEFUL NO-OPS (must NOT fabricate a worktree, must NOT abort) ────────


def test_not_a_repo_graceful_noop(tmp_path: Path) -> None:
    # No git repo anywhere → cannot isolate → echo nothing, exit 0.
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "agent-x", "cwd": str(tmp_path)},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""
    rows = _read_log_rows(tmp_path)
    assert rows and rows[-1]["decision"] == "noop"
    assert rows[-1]["reason"] == "not_a_repo"


def test_empty_stdin_graceful_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook("", repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    # No worktree fabricated.
    assert len(_worktree_paths(repo)) == 1


def test_malformed_json_graceful_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook("{not json", repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert len(_worktree_paths(repo)) == 1


def test_no_identifier_and_no_path_graceful_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    # Valid JSON, but nothing that identifies a worktree and no path.
    proc = _run_hook({"session_id": "abc", "hook_event_name": "WorktreeCreate"}, repo)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert len(_worktree_paths(repo)) == 1
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "noop"
    assert rows[-1]["reason"] == "no_worktree_identifier"


def test_dirty_identifier_sanitized_but_created(tmp_path: Path) -> None:
    """A non-empty identifier with unsafe chars is sanitized (never traverses
    out of the worktrees dir) but STILL yields a created worktree."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "weird/../id with spaces", "cwd": str(repo)},
        repo,
    )
    assert proc.returncode == 0, proc.stderr
    out = Path(proc.stdout.strip())
    # Must be under the convention dir (no traversal escape).
    worktrees_dir = (repo / ".claude" / "worktrees").resolve()
    assert worktrees_dir in out.resolve().parents, out
    assert out.is_dir()


# ── LOUD ABORT on a genuine create failure ────────────────────────────────


def test_create_failure_aborts_loudly(tmp_path: Path) -> None:
    """If `git worktree add` cannot create the target, the hook must abort
    NON-ZERO with a reason on stderr — never silently fall back to the shared
    tree. We force a failure by making the target path collide with an
    existing regular FILE (not a registered worktree)."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    # Pre-create a FILE exactly where the worktree dir would go.
    wtdir = repo / ".claude" / "worktrees"
    wtdir.mkdir(parents=True)
    (wtdir / "agent-collide").write_text("i am a file, not a worktree", encoding="utf-8")
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "agent-collide", "cwd": str(repo)},
        repo,
    )
    assert proc.returncode != 0, "create failure must abort with non-zero exit"
    assert proc.stdout.strip() == "", "no path on stdout when create fails"
    assert "ABORT" in proc.stderr
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "create_failed"


# ── BELT-AND-SUSPENDERS: explicit path branch (future harness builds) ──────


def test_explicit_separate_path_created_there(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proposed = tmp_path / "elsewhere" / "agent-explicit"
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "worktree_path": str(proposed), "cwd": str(repo)},
        repo,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(proposed.resolve())
    assert proposed.is_dir()
    assert str(proposed.resolve()) in _worktree_paths(repo)


def test_explicit_path_equals_parent_derives_safe_default(tmp_path: Path) -> None:
    """Explicit path == parent toplevel, default mode: don't create at the
    parent (that IS the shared-tree collapse) — derive a safe separate path
    from the identifier instead."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {
            "hook_event_name": "WorktreeCreate",
            "worktree_path": str(repo),
            "name": "agent-redir",
            "cwd": str(repo),
        },
        repo,
    )
    assert proc.returncode == 0, proc.stderr
    out = Path(proc.stdout.strip())
    # Landed under the convention dir, NOT at the parent root.
    assert out != repo.resolve()
    assert out == (repo / ".claude" / "worktrees" / "agent-redir").resolve()
    assert out.is_dir()
    decisions = [r["decision"] for r in _read_log_rows(repo)]
    assert "redirect_parent_path" in decisions


def test_explicit_path_equals_parent_blocks_under_enforce(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "worktree_path": str(repo), "cwd": str(repo)},
        repo,
        extra_env={"VCT_WORKTREE_GUARD_ENFORCE": "1"},
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
    assert "BLOCK" in proc.stderr
    rows = _read_log_rows(repo)
    assert rows[-1]["decision"] == "block"


# ── MONOREPO / SUBDIR resolution ──────────────────────────────────────────


def test_monorepo_subdir_creates_under_real_toplevel(tmp_path: Path) -> None:
    """Repo at `mono/`; project dir is `mono/sub/`. The worktree must land
    under the REAL repo root `mono/.claude/worktrees/...`, proving we use
    `git rev-parse --show-toplevel` and not the project dir."""
    mono = tmp_path / "mono"
    _init_repo(mono)
    subdir = mono / "sub"
    subdir.mkdir()
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "agent-mono", "cwd": str(subdir)},
        subdir,
    )
    assert proc.returncode == 0, proc.stderr
    out = Path(proc.stdout.strip())
    expected = mono / ".claude" / "worktrees" / "agent-mono"
    assert out == expected.resolve()
    assert out.is_dir()
    assert str(expected.resolve()) in _worktree_paths(mono)


# ── GLOBAL BYPASS + PAYLOAD CAPTURE ───────────────────────────────────────


def test_vct_disable_hooks_full_noop(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    proc = _run_hook(
        {"hook_event_name": "WorktreeCreate", "name": "agent-bypass", "cwd": str(repo)},
        repo,
        extra_env={"VCT_DISABLE_HOOKS": "1"},
    )
    # Global bypass: full no-op, no output, no worktree, no log row.
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert len(_worktree_paths(repo)) == 1
    assert _read_log_rows(repo) == []


def test_full_raw_payload_captured_in_log(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _init_repo(repo)
    payload = {
        "hook_event_name": "WorktreeCreate",
        "name": "agent-cap",
        "cwd": str(repo),
        "novel_field_for_schema_discovery": "VALUE-MARKER-XYZ",
    }
    proc = _run_hook(payload, repo)
    assert proc.returncode == 0, proc.stderr
    rows = _read_log_rows(repo)
    # The integrator verifies the live schema from the captured raw payload.
    assert "VALUE-MARKER-XYZ" in rows[-1]["raw_payload"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
