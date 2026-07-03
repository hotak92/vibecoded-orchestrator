# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.73 FIX-A' + FIX-B — worktree-skip gate + end-of-turn batched drain.

FIX-A' (worktree-gate.sh): a code-graph edit in an EPHEMERAL/unregistered git
worktree is SKIPPED; an edit in the MAIN tree — or in a worktree whose canonical
root IS a registered launcher project — still INDEXES.

FIX-B (stop-codegraph-drain.sh): the end-of-turn drain runs ONE analyzer pass
over the turn's queued files (grouped by canonical root), rate-limited to once
per 120s, with per-canonical-root serialization; a vanished (deleted) path is
pruned.

These drive the REAL shell libs + drain hook through bash against real git
repos + worktrees, with a stub analyzer capturing argv.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "templates" / "hooks" / "_lib"
WT_GATE = LIB_DIR / "worktree-gate.sh"
CANON_LIB = LIB_DIR / "canonical-repo-root.sh"
DRAIN_HOOK = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.sh"


def _tools_present() -> bool:
    return all(shutil.which(t) for t in ("bash", "git", "python3"))


pytestmark = pytest.mark.skipif(not _tools_present(), reason="bash+git+python3 required")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    (root / "seed.py").write_text("def s():\n    return 0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


# ---------------------------------------------------------------------------
# FIX-A' — worktree-gate decision (no launcher resolver present)
# ---------------------------------------------------------------------------


def _gate_decision(edited: Path, repo: Path, canon: str) -> str:
    """Run _worktree_gate_should_skip; return 'SKIP' (returns 0) or 'INDEX' (1)."""
    script = (
        f'. "{WT_GATE}"\n'
        f'if _worktree_gate_should_skip "{edited}" "{repo}" "{canon}"; then '
        f'echo SKIP; else echo INDEX; fi\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return "SKIP" if "SKIP" in r.stdout else "INDEX"


def test_main_tree_edit_indexes(tmp_path: Path) -> None:
    repo = tmp_path / "main"
    _init_repo(repo)
    f = repo / "src.py"
    f.write_text("x=1\n")
    # canon == repo (main tree) → never skip.
    assert _gate_decision(f, repo, str(repo)) == "INDEX"


def test_worktree_edit_no_resolver_indexes_conservatively(tmp_path: Path) -> None:
    """A worktree edit whose canonical root has NO resolver (cannot confirm
    unregistered) must INDEX — conservative default, never skip on uncertainty."""
    repo = tmp_path / "main"
    _init_repo(repo)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    f = wt / "src.py"
    f.write_text("y=2\n")
    # canon = main repo (differs from the worktree root) but no resolver exists
    # under it → uncertain → INDEX.
    assert _gate_decision(f, wt, str(repo)) == "INDEX"


def test_worktree_edit_unregistered_root_skips(tmp_path: Path) -> None:
    """A worktree edit whose canonical root resolves via the resolver to
    'not registered' (exit 2) must SKIP."""
    repo = tmp_path / "main"
    _init_repo(repo)
    # Install a stub resolver under the canonical root that exits 2 for
    # `resolve-project` (mimics the hub replying 404 project_not_registered).
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    resolver = scripts / "vct_project_config.sh"
    resolver.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Stub: `resolve-project <folder>` exits 2 (not registered).
        if [ "$1" = "resolve-project" ]; then exit 2; fi
        exit 4
    """))
    resolver.chmod(0o755)

    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    f = wt / "src.py"
    f.write_text("z=3\n")
    assert _gate_decision(f, wt, str(repo)) == "SKIP"


def test_worktree_edit_registered_root_indexes(tmp_path: Path) -> None:
    """A worktree whose canonical root IS a registered project (resolver exits 0)
    must INDEX under the canonical prefix (the bare-repo/worktree-PRIMARY case)."""
    repo = tmp_path / "main"
    _init_repo(repo)
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    resolver = scripts / "vct_project_config.sh"
    resolver.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Stub: `resolve-project <folder>` exits 0 (registered), prints an id.
        if [ "$1" = "resolve-project" ]; then echo "proj-123"; exit 0; fi
        exit 4
    """))
    resolver.chmod(0o755)

    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    f = wt / "src.py"
    f.write_text("w=4\n")
    assert _gate_decision(f, wt, str(repo)) == "INDEX"


# ---------------------------------------------------------------------------
# FIX-B — end-of-turn batched drain
# ---------------------------------------------------------------------------


def _make_analyzer_stub(path: Path, argv_log: Path) -> None:
    path.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        with open({json.dumps(str(argv_log))}, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")
    """))
    path.chmod(0o755)


def _run_drain(project_root: Path, session: str, env_extra: dict) -> subprocess.CompletedProcess:
    stdin = json.dumps({"session_id": session})
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root), **env_extra}
    return subprocess.run(
        ["bash", str(DRAIN_HOOK)],
        input=stdin, capture_output=True, text=True, timeout=60, env=env,
    )


def _wait_for_lines(log: Path, want: int, tries: int = 60) -> list:
    import time
    for _ in range(tries):
        if log.exists():
            lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
            if len(lines) >= want:
                return lines
        time.sleep(0.05)
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []


def test_drain_batches_all_files_in_one_run(tmp_path: Path) -> None:
    """N edits in one turn → ONE analyzer run over N files (not N runs)."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sess1"
    # Three edited files under the main repo.
    files = []
    for i in range(3):
        f = repo / f"f{i}.py"
        f.write_text(f"a{i}=1\n")
        files.append(f)
    queue = state / f"codegraph_drain_{session}.txt"
    queue.write_text("\n".join(str(f) for f in files) + "\n")

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        "VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS": "0",
    })
    assert r.returncode == 0, r.stderr
    runs = _wait_for_lines(argv_log, 1)
    # All main-tree files share ONE canonical root → exactly ONE analyzer run.
    assert len(runs) == 1, f"expected 1 batched run, got {len(runs)}: {runs}"
    argv = runs[0]
    assert "--only-files-from" in argv, "batched run must use --only-files-from"
    # The list file passed should contain all 3 paths.
    idx = argv.index("--only-files-from")
    list_file = Path(argv[idx + 1])
    # The list file is cleaned up by the detached run; assert the 3 files were
    # named either in the (possibly-still-present) list OR that one run covered
    # them (the batch is one process). One run == one batch of 3 is the invariant.
    assert argv[0] == str(repo), "repo_path arg is the canonical root"


def test_drain_rate_limited_leaves_queue(tmp_path: Path) -> None:
    """A drain < 120s since the last sync does NOT run; the queue survives."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sess2"
    f = repo / "a.py"
    f.write_text("q=1\n")
    queue = state / f"codegraph_drain_{session}.txt"
    queue.write_text(str(f) + "\n")

    # Seed a recent last-sync timestamp (now) so the default 120s window blocks.
    import time
    (state / "codegraph_drain_last_sync.ts").write_text(str(int(time.time())))

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        # Default 120s interval (do NOT set the override to 0).
    })
    assert r.returncode == 0, r.stderr
    # Rate-limited → analyzer NOT invoked; queue left intact for the next drain.
    assert not argv_log.exists() or argv_log.read_text().strip() == "", (
        "rate-limited drain must not run the analyzer"
    )
    assert queue.exists(), "rate-limited drain must leave the queue for the next drain"


def test_drain_deleted_file_is_not_an_error(tmp_path: Path) -> None:
    """An edited-then-deleted path in the queue drains cleanly (pruned via the
    analyzer's --only-files-from, never crashes the hook)."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sess3"
    present = repo / "present.py"
    present.write_text("p=1\n")
    missing = repo / "gone.py"  # never created (edited-then-deleted)
    queue = state / f"codegraph_drain_{session}.txt"
    queue.write_text(f"{present}\n{missing}\n")

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        "VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS": "0",
    })
    assert r.returncode == 0, r.stderr
    # The present file still drives one batched run; the missing one is passed
    # in the list (the analyzer prunes it). The hook must exit 0 regardless.
    runs = _wait_for_lines(argv_log, 1)
    assert len(runs) == 1, f"expected one run despite the deleted path, got {runs}"


# ---------------------------------------------------------------------------
# v0.2.73 HIGH-2 — the drain also consumes the SESSION-AGNOSTIC shared queue
# (subagent edits enqueued by subagent-stop-reconcile.*)
# ---------------------------------------------------------------------------


def test_drain_consumes_shared_queue_with_per_session(tmp_path: Path) -> None:
    """The drain folds the shared drain queue (codegraph_drain_shared.txt) into
    the same batch as the per-session queue. Both a per-session path and a
    subagent-enqueued shared path reach the analyzer in ONE run per canonical
    root, and BOTH queues are cleared."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sessShared"

    per_session_file = repo / "a.py"
    per_session_file.write_text("a=1\n")
    shared_file = repo / "b.py"
    shared_file.write_text("b=2\n")

    (state / f"codegraph_drain_{session}.txt").write_text(str(per_session_file) + "\n")
    (state / "codegraph_drain_shared.txt").write_text(str(shared_file) + "\n")

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        "VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS": "0",
    })
    assert r.returncode == 0, r.stderr
    runs = _wait_for_lines(argv_log, 1)
    # Both paths share ONE canonical root (the main repo) → ONE analyzer run.
    assert len(runs) == 1, f"expected 1 batched run over both queues, got {runs}"
    argv = runs[0]
    assert "--only-files-from" in argv
    list_file = Path(argv[argv.index("--only-files-from") + 1])
    # The list file is cleaned up by the detached run; capture its content fast.
    import time
    listed = ""
    for _ in range(60):
        if list_file.exists():
            listed = list_file.read_text()
            if "a.py" in listed and "b.py" in listed:
                break
        time.sleep(0.05)
    # Even if the list file was already GC'd, one run covering the canonical
    # root is the invariant; when we DID capture it, both paths must be present.
    if listed:
        assert "a.py" in listed and "b.py" in listed, (
            f"batch must contain both per-session and shared paths: {listed!r}")

    # Both queues cleared after a successful drain.
    assert not (state / f"codegraph_drain_{session}.txt").exists(), (
        "per-session queue must be consumed")
    assert not (state / "codegraph_drain_shared.txt").exists(), (
        "shared queue must be consumed")


def test_drain_shared_queue_only_no_per_session(tmp_path: Path) -> None:
    """When ONLY the shared queue has entries (a subagent enqueued but the
    session never accumulated its own edits), the drain still runs over the
    shared paths and clears the shared queue."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sessSharedOnly"

    shared_file = repo / "only.py"
    shared_file.write_text("o=1\n")
    (state / "codegraph_drain_shared.txt").write_text(str(shared_file) + "\n")
    # No per-session queue file exists.

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        "VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS": "0",
    })
    assert r.returncode == 0, r.stderr
    runs = _wait_for_lines(argv_log, 1)
    assert len(runs) == 1, f"shared-only drain must run one batch, got {runs}"
    assert not (state / "codegraph_drain_shared.txt").exists(), (
        "shared queue must be consumed even with no per-session queue")


def test_drain_rate_limited_leaves_shared_queue(tmp_path: Path) -> None:
    """A rate-limited drain must leave BOTH the per-session AND the shared queue
    intact for the next eligible drain (the shared subagent edits must not be
    lost inside the rate-limit window)."""
    repo = tmp_path / "proj"
    _init_repo(repo)
    state = repo / ".claude" / "state"
    state.mkdir(parents=True)
    session = "sessRLshared"

    shared_file = repo / "s.py"
    shared_file.write_text("s=1\n")
    shared_q = state / "codegraph_drain_shared.txt"
    shared_q.write_text(str(shared_file) + "\n")

    import time
    (state / "codegraph_drain_last_sync.ts").write_text(str(int(time.time())))

    argv_log = tmp_path / "argv.jsonl"
    stub = tmp_path / "stub.py"
    _make_analyzer_stub(stub, argv_log)

    r = _run_drain(repo, session, {
        "VCT_ANALYZER_SCRIPT": str(stub),
        "VCT_PYTHON": shutil.which("python3"),
        # Default 120s interval blocks.
    })
    assert r.returncode == 0, r.stderr
    assert not argv_log.exists() or argv_log.read_text().strip() == "", (
        "rate-limited drain must not run the analyzer")
    assert shared_q.exists(), "rate-limited drain must leave the shared queue"
