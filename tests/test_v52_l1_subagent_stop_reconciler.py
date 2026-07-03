# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-L.1: SubagentStop reconciler + snapshot/credscan helper tests.

V52-L.1 (v0.2.52) extended the V52-L.2 logging-only baseline SubagentStop
hook into a full reconciler that drives five side effects:
  1. JSONL audit log (V52-L.2 baseline — covered by test_v52_l2 already).
  2. KG-sync for modified `knowledge/**/*.md` files.
  3. Code-graph queue (.claude/state/code-graph-queue.jsonl) for code files.
  4. Credential scan (via _lib/credscan.sh) on every modified file.
  5. Nudge counter increment (~/.claude/metrics/kg_update_tokens.jsonl).

File-modification discovery uses a SubagentStart-time SHA-256 snapshot
of watched paths (`_lib/snapshot.sh take_snapshot`) diff'd at
SubagentStop (`diff_snapshot`).

This file covers:
  T01  Snapshot creation by subagent-start-kg-inject.sh.
  T02  Diff detection via _lib/snapshot.sh `diff_snapshot` (modified +
       added + deleted files).
  T03  kg-sync invocation when modified `knowledge/**/*.md` is detected.
  T04  Code-graph queue append when modified .py file is detected.
  T05  Credential scan emits alert on AWS-test-key marker.
  T06  Nudge counter increment for the parent session row.
  T07  Snapshot file cleanup after successful reconcile.
  T08  Soft-fail when .claude/state/ is missing (no snapshot → no crash).
  T09  Soft-fail on corrupt snapshot file (unterminated JSON).
  T10  bash -n syntax check for all 4 .sh files; pwsh syntax check
       skipped when pwsh missing (covers the .ps1 siblings on systems
       with PowerShell installed).
  T11  Snapshot JSON schema parity between snapshot.sh and snapshot.ps1
       (top-level keys match; values may differ).

POSIX-only for the bash-driving tests; the syntax-check + schema-parity
tests handle PowerShell sibling coverage via conditional pwsh detection.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"
LIB_DIR = HOOKS_DIR / "_lib"

SNAPSHOT_SH = LIB_DIR / "snapshot.sh"
SNAPSHOT_PS1 = LIB_DIR / "snapshot.ps1"
CREDSCAN_SH = LIB_DIR / "credscan.sh"
CREDSCAN_PS1 = LIB_DIR / "credscan.ps1"
SUBAGENT_START = HOOKS_DIR / "subagent-start-kg-inject.sh"
SUBAGENT_STOP = HOOKS_DIR / "subagent-stop-reconcile.sh"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hooks are POSIX-only; .ps1 siblings covered by hook-OS-parity gate.",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _setup_project(tmp_path: Path) -> Path:
    """Create a minimal project skeleton: .claude/{logs,state}/,
    knowledge/, docs/, src/, plus a couple of seed files so the
    snapshot has something non-empty to hash."""
    (tmp_path / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    # Seed files
    (tmp_path / "knowledge" / "intro.md").write_text(
        "# intro\nseed kg node\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "readme.md").write_text(
        "# readme\nseed docs\n", encoding="utf-8"
    )
    (tmp_path / "src" / "main.py").write_text(
        "# seed code\ndef foo():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


def _run_hook(
    hook_path: Path,
    payload: dict,
    project_root: Path,
    extra_env: dict | None = None,
    home_override: Path | None = None,
) -> subprocess.CompletedProcess:
    """Invoke a hook with stdin JSON payload, returning the
    CompletedProcess. Always uses bash to satisfy the shebang regardless
    of executable bit."""
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env.pop("VCT_DISABLE_HOOKS", None)
    if home_override is not None:
        env["HOME"] = str(home_override)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def _safe_id(agent_id: str) -> str:
    """Mirror the snapshot.sh agent_id sanitizer (alnum + dash +
    underscore only, ≤64 chars)."""
    out = []
    for ch in agent_id:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:64]


def _call_snapshot_sh(
    func: str,
    agent_id: str,
    project_root: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Source `_lib/snapshot.sh` in a bash subshell and invoke a
    function with the given agent_id + project_root args. Used by tests
    that exercise the helper directly without going through the hook."""
    snap_dir = project_root / ".claude" / "state"
    cmd = (
        f'. "{SNAPSHOT_SH}" && '
        f'{func} "{agent_id}" "{project_root}" "{snap_dir}"'
    )
    env = os.environ.copy()
    env.pop("VCT_DISABLE_HOOKS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


# --------------------------------------------------------------------------- #
# T01: Snapshot creation by subagent-start-kg-inject.sh
# --------------------------------------------------------------------------- #


def test_subagent_start_creates_snapshot_file(tmp_path):
    """SubagentStart hook with a payload that includes agent_id must
    write `.claude/state/subagent-snapshot-<agent_id>.json` containing
    SHA-256 hashes of the seeded files under knowledge/ + docs/ + src/.

    The hook ALWAYS attempts the snapshot, even when the venv / rl_kg_search
    are unavailable — the snapshot must happen BEFORE the venv guard
    (the SubagentStop reconciler depends on it regardless of whether
    KG retrieval ran)."""
    _setup_project(tmp_path)
    agent_id = "T01-agent-uuid"
    payload = {
        "prompt": "implement caching",
        "session_id": "T01-session",
        "agent_id": agent_id,
        "agent_type": "@agent-coder",
    }
    result = _run_hook(SUBAGENT_START, payload, tmp_path)
    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}")

    snap_file = tmp_path / ".claude/state" / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    assert snap_file.exists(), (
        f"snapshot file not created at {snap_file}; "
        f"state dir contents: {list((tmp_path / '.claude/state').iterdir())}"
    )
    doc = json.loads(snap_file.read_text(encoding="utf-8"))
    assert doc["agent_id"] == agent_id
    # Top-level schema sanity.
    for key in ("version", "agent_id", "project_root", "created_at", "files"):
        assert key in doc, f"missing top-level key {key!r} in snapshot JSON"
    # The 3 seed files (.md, .md, .py) should all be hashed.
    files = doc["files"]
    assert "knowledge/intro.md" in files
    assert "docs/readme.md" in files
    assert "src/main.py" in files
    # Hashes are hex SHA-256 (64 chars).
    for path, hexhash in files.items():
        assert len(hexhash) == 64, (
            f"unexpected hash length for {path}: {hexhash!r}")


# --------------------------------------------------------------------------- #
# T02: Diff detection
# --------------------------------------------------------------------------- #


def test_diff_snapshot_identifies_modified_file(tmp_path):
    """Modify a fixture knowledge/test.md between snapshot + reconcile;
    `diff_snapshot` must emit that file's relative path on stdout."""
    _setup_project(tmp_path)
    agent_id = "T02-agent"
    target = tmp_path / "knowledge" / "test.md"
    target.write_text("# original\n", encoding="utf-8")

    # 1. Take snapshot.
    take = _call_snapshot_sh("take_snapshot", agent_id, tmp_path)
    assert take.returncode == 0, (
        f"take_snapshot exited {take.returncode}; stderr={take.stderr!r}")
    snap_file = (
        tmp_path / ".claude/state"
        / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    )
    assert snap_file.exists()

    # 2. Modify the file.
    target.write_text("# modified after snapshot\n", encoding="utf-8")
    # Also add a new file and delete an existing one for fuller coverage.
    (tmp_path / "knowledge" / "added.md").write_text(
        "# new file\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "readme.md").unlink()

    # 3. Diff.
    diff = _call_snapshot_sh("diff_snapshot", agent_id, tmp_path)
    assert diff.returncode == 0, (
        f"diff_snapshot exited {diff.returncode}; stderr={diff.stderr!r}")
    changed = set(
        line for line in diff.stdout.splitlines() if line.strip()
    )
    assert "knowledge/test.md" in changed, (
        f"modified file not in diff output: {changed!r}")
    assert "knowledge/added.md" in changed, (
        f"added file not in diff output: {changed!r}")
    assert "docs/readme.md" in changed, (
        f"deleted file not in diff output: {changed!r}")


# --------------------------------------------------------------------------- #
# T03: kg-sync invocation
# --------------------------------------------------------------------------- #


def _install_kg_sync_mock(project_root: Path, sidecar: Path) -> Path:
    """Install a mock .claude/scripts/kg-sync that just writes its
    argv (one per call, newline-delimited) to `sidecar`. Returns the
    mock path."""
    scripts_dir = project_root / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    mock = scripts_dir / "kg-sync"
    mock.write_text(
        f"#!/usr/bin/env bash\n"
        f"# Mock kg-sync: write argv to sidecar.\n"
        f'echo "$@" >> "{sidecar}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    mock.chmod(mock.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return mock


def test_kg_sync_invoked_for_modified_knowledge_md(tmp_path):
    """When the diff includes a `knowledge/**/*.md` file, the reconciler
    must invoke the project's `.claude/scripts/kg-sync` with the
    absolute path of that file as the first argument."""
    project = _setup_project(tmp_path)
    agent_id = "T03-agent"
    sidecar = tmp_path / "kg_sync_calls.log"
    sidecar.write_text("", encoding="utf-8")
    _install_kg_sync_mock(project, sidecar)

    # 1. SubagentStart: take snapshot.
    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": "s", "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    assert start_result.returncode == 0

    # 2. Subagent modifies a knowledge .md file.
    kg_target = project / "knowledge" / "foo.md"
    kg_target.write_text("# new content\n", encoding="utf-8")

    # 3. SubagentStop: reconcile.
    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": "s",
            "agent_id": agent_id,
            "agent_type": "@a",
            "transcript_path": "/x.jsonl",
            "stop_reason": "stop",
        },
        project,
    )
    assert stop_result.returncode == 0, (
        f"reconcile exited {stop_result.returncode}; "
        f"stderr={stop_result.stderr!r}")

    calls = sidecar.read_text(encoding="utf-8").strip().splitlines()
    assert calls, (
        "kg-sync mock was never invoked; "
        f"reconcile stdout={stop_result.stdout!r} stderr={stop_result.stderr!r}"
    )
    # The mock receives the absolute path. Match on the relative
    # suffix for resilience against symlinks (macOS /private/tmp).
    assert any("knowledge/foo.md" in line for line in calls), (
        f"kg-sync calls did not include knowledge/foo.md: {calls!r}")


# --------------------------------------------------------------------------- #
# T04: Code-graph drain enqueue (v0.2.73 HIGH-2)
# --------------------------------------------------------------------------- #


def test_code_graph_edit_enqueues_into_shared_drain_queue(tmp_path):
    """When the diff includes a code file, the reconciler routes it into the
    SESSION-AGNOSTIC shared drain queue (.claude/state/codegraph_drain_shared.txt)
    that the Stop-hook batched drain consumes — NOT the removed orphan
    code-graph-queue.jsonl. It only ENQUEUES (never runs the analyzer).

    The project here is NOT a git repo, so the canonical-root resolver returns
    empty → the worktree gate treats it as a non-worktree edit → INDEX
    (conservative default). The gated-IN absolute path is appended, one per
    line."""
    project = _setup_project(tmp_path)
    agent_id = "T04-agent"
    session = "T04-session"

    # 1. Snapshot.
    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": session, "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    assert start_result.returncode == 0

    # 2. Modify a .py file.
    code_target = project / "src" / "main.py"
    code_target.write_text(
        "# modified after snapshot\ndef foo():\n    return 42\n",
        encoding="utf-8",
    )

    # 3. Reconcile.
    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session,
            "agent_id": agent_id,
            "agent_type": "@a",
            "stop_reason": "stop",
        },
        project,
    )
    assert stop_result.returncode == 0, (
        f"reconcile exited {stop_result.returncode}; "
        f"stderr={stop_result.stderr!r}")

    shared_queue = project / ".claude" / "state" / "codegraph_drain_shared.txt"
    assert shared_queue.exists(), "codegraph_drain_shared.txt was not written"
    lines = [ln for ln in shared_queue.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, f"shared drain queue empty: {shared_queue.read_text()!r}"
    # Newline-delimited ABSOLUTE paths (matching the drain's format).
    assert any(ln.endswith("src/main.py") for ln in lines), (
        f"src/main.py (absolute) not in shared drain queue: {lines!r}")
    for ln in lines:
        assert os.path.isabs(ln), f"drain queue path is not absolute: {ln!r}"


def test_orphan_code_graph_queue_is_never_written(tmp_path):
    """v0.2.73 HIGH-2: the orphan .claude/state/code-graph-queue.jsonl (which
    nothing ever drained) must NEVER be written by the reconciler anymore. A
    code edit must land in the shared drain queue instead."""
    project = _setup_project(tmp_path)
    agent_id = "T04b-agent"
    session = "T04b-session"

    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": session, "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    assert start_result.returncode == 0

    (project / "src" / "main.py").write_text(
        "# changed\ndef foo():\n    return 7\n", encoding="utf-8"
    )

    stop_result = _run_hook(
        SUBAGENT_STOP,
        {"session_id": session, "agent_id": agent_id, "agent_type": "@a", "stop_reason": "stop"},
        project,
    )
    assert stop_result.returncode == 0, stop_result.stderr

    orphan = project / ".claude" / "state" / "code-graph-queue.jsonl"
    assert not orphan.exists(), (
        "the orphan code-graph-queue.jsonl must NOT be written anymore; "
        f"contents: {orphan.read_text() if orphan.exists() else ''!r}")
    # And the edit did reach the batched path.
    shared_queue = project / ".claude" / "state" / "codegraph_drain_shared.txt"
    assert shared_queue.exists() and shared_queue.read_text().strip(), (
        "code edit did not reach the shared drain queue")


def test_ephemeral_worktree_edit_is_dropped_not_enqueued(tmp_path):
    """A subagent code edit in an EPHEMERAL/unregistered git worktree is DROPPED
    by the FIX-A' gate — not enqueued, and the orphan file is not written.

    Setup: a real git MAIN repo with a stub resolver that answers
    `resolve-project` with exit 2 (not registered); a linked worktree whose
    canonical root is that main repo. An edit in the worktree resolves to a
    canonical root that DIFFERS from the worktree root AND probes 'unregistered'
    → SKIP."""
    if not shutil.which("git"):
        pytest.skip("git required")

    def _git(cwd, *args):
        subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / ".claude" / "logs").mkdir(parents=True)
    (main / ".claude" / "state").mkdir(parents=True)
    # Stub resolver under the MAIN (canonical) root: resolve-project → exit 2.
    scripts = main / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    resolver = scripts / "vct_project_config.sh"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "resolve-project" ]; then exit 2; fi\nexit 4\n'
    )
    resolver.chmod(0o755)
    (main / "src").mkdir()
    (main / "src" / "seed.py").write_text("def s():\n    return 0\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "seed")

    # Linked worktree (its own .claude/{logs,state,scripts} so the hook + gate
    # can run there); the gate probes the CANONICAL root's resolver (exit 2).
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feat")
    (wt / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (wt / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    wt_scripts = wt / ".claude" / "scripts"
    wt_scripts.mkdir(parents=True, exist_ok=True)
    (wt_scripts / "vct_project_config.sh").write_text(resolver.read_text())
    (wt_scripts / "vct_project_config.sh").chmod(0o755)

    # 1. Snapshot the worktree checkout.
    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": "wt-s", "agent_id": "wt-a", "agent_type": "@a"},
        wt,
    )
    assert start_result.returncode == 0

    # 2. Edit a code file inside the worktree, under a snapshot-watched dir (src/).
    (wt / "src" / "feat.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    # 3. Reconcile against the worktree root.
    stop_result = _run_hook(
        SUBAGENT_STOP,
        {"session_id": "wt-s", "agent_id": "wt-a", "agent_type": "@a", "stop_reason": "stop"},
        wt,
    )
    assert stop_result.returncode == 0, stop_result.stderr

    shared_queue = wt / ".claude" / "state" / "codegraph_drain_shared.txt"
    # The worktree edit was gated OUT → shared queue absent or has no code path.
    if shared_queue.exists():
        lines = [ln for ln in shared_queue.read_text().splitlines() if ln.strip()]
        assert not any(ln.endswith("feat.py") for ln in lines), (
            f"ephemeral worktree edit must be dropped, not enqueued: {lines!r}")
    orphan = wt / ".claude" / "state" / "code-graph-queue.jsonl"
    assert not orphan.exists(), "orphan file must not be written for a dropped edit"


def test_registered_worktree_edit_is_enqueued(tmp_path):
    """A subagent code edit in a worktree whose canonical root IS a registered
    launcher project (resolver exit 0) is ENQUEUED under the batched path — the
    bare-repo/worktree-PRIMARY case must still index."""
    if not shutil.which("git"):
        pytest.skip("git required")

    def _git(cwd, *args):
        subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    scripts = main / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    resolver = scripts / "vct_project_config.sh"
    resolver.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "resolve-project" ]; then echo "proj-123"; exit 0; fi\nexit 4\n'
    )
    resolver.chmod(0o755)
    (main / "src").mkdir()
    (main / "src" / "seed.py").write_text("def s():\n    return 0\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "seed")

    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feat")
    (wt / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (wt / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    wt_scripts = wt / ".claude" / "scripts"
    wt_scripts.mkdir(parents=True, exist_ok=True)
    (wt_scripts / "vct_project_config.sh").write_text(resolver.read_text())
    (wt_scripts / "vct_project_config.sh").chmod(0o755)

    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": "rw-s", "agent_id": "rw-a", "agent_type": "@a"},
        wt,
    )
    assert start_result.returncode == 0

    (wt / "src" / "feat.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    stop_result = _run_hook(
        SUBAGENT_STOP,
        {"session_id": "rw-s", "agent_id": "rw-a", "agent_type": "@a", "stop_reason": "stop"},
        wt,
    )
    assert stop_result.returncode == 0, stop_result.stderr

    shared_queue = wt / ".claude" / "state" / "codegraph_drain_shared.txt"
    assert shared_queue.exists(), "registered-worktree edit must be enqueued"
    lines = [ln for ln in shared_queue.read_text().splitlines() if ln.strip()]
    assert any(ln.endswith("feat.py") for ln in lines), (
        f"registered-worktree edit not enqueued: {lines!r}")


# --------------------------------------------------------------------------- #
# T05: Credential scan
# --------------------------------------------------------------------------- #


def test_credential_scan_emits_alert_on_aws_key(tmp_path):
    """A modified file containing an AWS-test-key pattern triggers an
    alert row in `.claude/logs/credential_alerts.jsonl`. The schema
    must match post-tool-security.sh's shape (file + patterns +
    session_id + agent_id + source)."""
    project = _setup_project(tmp_path)
    agent_id = "T05-agent"
    session = "T05-session"

    # 1. Snapshot.
    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": session, "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    assert start_result.returncode == 0

    # 2. Subagent writes a file with the canonical AWS-test-key.
    leak = project / "src" / "leak.py"
    # AKIAIOSFODNN7EXAMPLE is the AWS documentation example key —
    # matches the `AKIA[A-Z0-9]{16}` pattern in credscan.sh.
    leak.write_text(
        "AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'\n",
        encoding="utf-8",
    )

    # 3. Reconcile.
    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session,
            "agent_id": agent_id,
            "agent_type": "@a",
            "stop_reason": "stop",
        },
        project,
    )
    assert stop_result.returncode == 0, (
        f"reconcile exited {stop_result.returncode}; "
        f"stderr={stop_result.stderr!r}")

    alert_log = project / ".claude" / "logs" / "credential_alerts.jsonl"
    assert alert_log.exists(), "credential_alerts.jsonl was not created"
    rows = [
        json.loads(ln)
        for ln in alert_log.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert rows, f"alert log empty: {alert_log.read_text()!r}"
    aws_rows = [r for r in rows if "AWS access key" in r.get("patterns", "")]
    assert aws_rows, f"no AWS-access-key alert in rows: {rows!r}"
    row = aws_rows[-1]
    assert "leak.py" in row["file"]
    assert row["session_id"] == session
    assert row["agent_id"] == agent_id
    assert row["source"] == "subagent_stop_reconciler"


# --------------------------------------------------------------------------- #
# T06: Nudge counter increment
# --------------------------------------------------------------------------- #


def test_nudge_counter_incremented_for_session(tmp_path, monkeypatch):
    """Pre-seed `$HOME/.claude/metrics/kg_update_tokens.jsonl` with a
    row for our session_id. After reconcile, that row's
    `subagent_work_units` must have increased."""
    project = _setup_project(tmp_path)
    fake_home = tmp_path / "fake-home"
    (fake_home / ".claude" / "metrics").mkdir(parents=True, exist_ok=True)
    nudge_file = fake_home / ".claude" / "metrics" / "kg_update_tokens.jsonl"

    session = "T06-session-uuid"
    agent_id = "T06-agent"

    # Pre-seed: row for our session. kg-update-nudge owns most of the
    # fields; we only set session_id + subagent_work_units (the field
    # the reconciler bumps).
    seed = {
        "session_id": session,
        "baseline": 1000,
        "last_seen_total": 1500,
        "fired_once": False,
        "subagent_work_units": 50,
    }
    nudge_file.write_text(json.dumps(seed) + "\n", encoding="utf-8")

    # 1. Snapshot.
    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": session, "agent_id": agent_id, "agent_type": "@a"},
        project,
        home_override=fake_home,
    )
    assert start_result.returncode == 0

    # 2. Subagent modifies multiple files to drive the work-unit bump.
    (project / "knowledge" / "newish.md").write_text(
        "# changed\n" * 100, encoding="utf-8"
    )
    (project / "src" / "main.py").write_text(
        "# changed\n" * 100, encoding="utf-8"
    )

    # 3. Reconcile.
    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session,
            "agent_id": agent_id,
            "agent_type": "@a",
            "stop_reason": "stop",
        },
        project,
        home_override=fake_home,
    )
    assert stop_result.returncode == 0, (
        f"reconcile exited {stop_result.returncode}; "
        f"stderr={stop_result.stderr!r}")

    # Read the post-reconcile file. The reconciler rewrites atomically
    # so last-row-wins for our session.
    lines = [
        ln for ln in nudge_file.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    rows = [json.loads(ln) for ln in lines]
    matching = [r for r in rows if r.get("session_id") == session]
    assert matching, (
        f"no row for session {session!r} after reconcile; rows={rows!r}")
    bumped = matching[-1]
    assert bumped.get("subagent_work_units", 0) > 50, (
        f"subagent_work_units did not increase past 50: {bumped!r}")
    assert bumped.get("subagent_count", 0) >= 1
    assert bumped.get("subagent_last_at"), (
        "subagent_last_at must be set to an ISO timestamp")


# --------------------------------------------------------------------------- #
# T07: Snapshot cleanup
# --------------------------------------------------------------------------- #


def test_snapshot_file_deleted_after_reconcile(tmp_path):
    """After a successful reconcile, the snapshot file at
    `.claude/state/subagent-snapshot-<id>.json` must be deleted.
    Otherwise stale snapshots accumulate forever."""
    project = _setup_project(tmp_path)
    agent_id = "T07-agent"
    session = "T07-session"

    start_result = _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": session, "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    assert start_result.returncode == 0
    snap_file = (
        project / ".claude/state"
        / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    )
    assert snap_file.exists(), "snapshot should exist after SubagentStart"

    # Trigger reconcile WITH a modification so we exercise the full
    # path (the no-changes early exit also cleans up — test that too
    # implicitly by repeating the assert below).
    (project / "knowledge" / "intro.md").write_text(
        "# bumped\n", encoding="utf-8"
    )

    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": session,
            "agent_id": agent_id,
            "agent_type": "@a",
            "stop_reason": "stop",
        },
        project,
    )
    assert stop_result.returncode == 0
    assert not snap_file.exists(), (
        f"snapshot {snap_file} should be deleted after reconcile")


def test_snapshot_file_deleted_when_no_changes(tmp_path):
    """Even when the subagent made zero changes, the snapshot must be
    cleaned up so we don't leak the state file on idle subagents."""
    project = _setup_project(tmp_path)
    agent_id = "T07b-agent"

    _run_hook(
        SUBAGENT_START,
        {"prompt": "p", "session_id": "s", "agent_id": agent_id, "agent_type": "@a"},
        project,
    )
    snap_file = (
        project / ".claude/state"
        / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    )
    assert snap_file.exists()

    stop_result = _run_hook(
        SUBAGENT_STOP,
        {
            "session_id": "s",
            "agent_id": agent_id,
            "agent_type": "@a",
            "stop_reason": "stop",
        },
        project,
    )
    assert stop_result.returncode == 0
    assert not snap_file.exists(), (
        "snapshot should be cleaned up even when no files changed")


# --------------------------------------------------------------------------- #
# T08 + T09: Soft-fail paths
# --------------------------------------------------------------------------- #


def test_reconcile_soft_fails_when_state_dir_missing(tmp_path):
    """When `.claude/state/` does not exist, the reconciler must
    exit 0 cleanly. (It pre-creates the dir for the audit log path
    but must not crash when the snapshot is absent.)"""
    # Bare project, no .claude/ at all.
    (tmp_path / "knowledge").mkdir()
    payload = {
        "session_id": "T08-session",
        "agent_id": "T08-agent",
        "agent_type": "@a",
        "stop_reason": "stop",
    }
    result = _run_hook(SUBAGENT_STOP, payload, tmp_path)
    assert result.returncode == 0, (
        f"reconcile exited {result.returncode}; stderr={result.stderr!r}")
    assert "Traceback" not in result.stderr, (
        f"Python traceback leaked to stderr: {result.stderr!r}")


def test_reconcile_soft_fails_on_corrupt_snapshot(tmp_path):
    """When the snapshot file is corrupt (unterminated JSON), the
    reconciler must exit 0 with no traceback. The diff helper will
    silently abandon and the reconciler falls through to logging-only."""
    project = _setup_project(tmp_path)
    agent_id = "T09-agent"
    snap_dir = project / ".claude/state"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    # Write unterminated JSON.
    snap_file.write_text('{"corrupt": true', encoding="utf-8")

    payload = {
        "session_id": "T09-session",
        "agent_id": agent_id,
        "agent_type": "@a",
        "stop_reason": "stop",
    }
    result = _run_hook(SUBAGENT_STOP, payload, project)
    assert result.returncode == 0, (
        f"reconcile exited {result.returncode}; stderr={result.stderr!r}")
    assert "Traceback" not in result.stderr, (
        f"Python traceback leaked to stderr: {result.stderr!r}")


# --------------------------------------------------------------------------- #
# T10: Syntax checks (.sh always; .ps1 if pwsh available)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "shell_file",
    [
        SUBAGENT_START,
        SUBAGENT_STOP,
        SNAPSHOT_SH,
        CREDSCAN_SH,
    ],
    ids=[
        "subagent-start-kg-inject.sh",
        "subagent-stop-reconcile.sh",
        "_lib/snapshot.sh",
        "_lib/credscan.sh",
    ],
)
def test_bash_syntax_clean(shell_file):
    """`bash -n` accepts all 4 .sh files (the 2 hooks + the 2 libs).
    Catches stray syntax errors before any hook fires in production."""
    assert shell_file.exists(), f"{shell_file} missing on disk"
    result = subprocess.run(
        ["bash", "-n", str(shell_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"bash -n rejected {shell_file.name}: stderr={result.stderr!r}")


@pytest.mark.skipif(
    shutil.which("pwsh") is None,
    reason="pwsh not on PATH; PowerShell siblings covered by hook-OS-parity gate.",
)
@pytest.mark.parametrize(
    "ps1_file",
    [SNAPSHOT_PS1, CREDSCAN_PS1],
    ids=["_lib/snapshot.ps1", "_lib/credscan.ps1"],
)
def test_powershell_syntax_clean(ps1_file):
    """When pwsh is available, parse the .ps1 siblings to catch
    PowerShell-side syntax errors. The standard PS parse-only probe is
    `[System.Management.Automation.Language.Parser]::ParseFile`."""
    assert ps1_file.exists(), f"{ps1_file} missing on disk"
    probe = (
        "$ErrorActionPreference='Stop';"
        f"$tokens=$null;$errors=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{ps1_file}',[ref]$tokens,[ref]$errors) | Out-Null;"
        "if ($errors -and $errors.Count -gt 0) "
        "{ $errors | ForEach-Object { Write-Error $_.Message }; exit 1 } "
        "else { exit 0 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", probe],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"pwsh parse rejected {ps1_file.name}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}")


# --------------------------------------------------------------------------- #
# T11: Schema parity between snapshot.sh and snapshot.ps1
# --------------------------------------------------------------------------- #


def test_snapshot_schema_parity_sh_ps1(tmp_path):
    """The bash and PowerShell snapshot helpers must produce JSON with
    the same top-level keys. Values may differ (paths, timestamps) but
    a consumer that reads either one needs the same shape.

    The .sh sibling is exercised inline; the .ps1 sibling is exercised
    only when pwsh is present. When pwsh is absent we still assert the
    sh sibling produces the expected canonical schema — a regression
    on the sh side would break parity even if we can't verify the ps1
    side on this host."""
    project = _setup_project(tmp_path)
    agent_id = "T11-agent"
    snap_dir = project / ".claude/state"

    # --- .sh sibling ---
    sh_result = _call_snapshot_sh("take_snapshot", agent_id, project)
    assert sh_result.returncode == 0, (
        f"take_snapshot (.sh) exited {sh_result.returncode}; "
        f"stderr={sh_result.stderr!r}")
    sh_snap = snap_dir / f"subagent-snapshot-{_safe_id(agent_id)}.json"
    assert sh_snap.exists()
    sh_doc = json.loads(sh_snap.read_text(encoding="utf-8"))
    sh_keys = set(sh_doc.keys())
    canonical = {"version", "agent_id", "project_root", "created_at", "files"}
    assert canonical.issubset(sh_keys), (
        f".sh snapshot missing canonical keys; got {sh_keys!r}")

    # --- .ps1 sibling (skip when pwsh missing) ---
    if shutil.which("pwsh") is None:
        pytest.skip("pwsh not available; .sh canonical schema verified")

    # Pick a different snap-dir so the two outputs don't overwrite.
    ps1_snap_dir = project / ".claude/state-ps1"
    ps1_snap_dir.mkdir(parents=True, exist_ok=True)
    cmd = (
        f". '{SNAPSHOT_PS1}'; "
        f"Take-Snapshot -AgentId 'T11-ps' -ProjectRoot '{project}' "
        f"-SnapshotDir '{ps1_snap_dir}' | Out-Null"
    )
    ps1_result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ps1_result.returncode == 0, (
        f"pwsh Take-Snapshot failed: stdout={ps1_result.stdout!r} "
        f"stderr={ps1_result.stderr!r}")
    ps1_snap = ps1_snap_dir / f"subagent-snapshot-{_safe_id('T11-ps')}.json"
    assert ps1_snap.exists(), (
        f"PS1 snapshot not created; dir contents: {list(ps1_snap_dir.iterdir())}"
    )
    ps1_doc = json.loads(ps1_snap.read_text(encoding="utf-8"))
    ps1_keys = set(ps1_doc.keys())

    # Parity: both must contain the canonical key set. Either side
    # adding extra keys is fine (extension is forward-compatible);
    # missing keys break consumers.
    assert canonical.issubset(ps1_keys), (
        f".ps1 snapshot missing canonical keys; got {ps1_keys!r}")
    # The `files` field on both sides must be a dict.
    assert isinstance(sh_doc["files"], dict)
    assert isinstance(ps1_doc["files"], dict)
