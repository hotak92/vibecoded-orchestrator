# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 L-KGREV -- the post-commit KG reviewer must not strand its work.

Field report (2026-11-03): of 839 logged reviewer runs, 539 skipped their
commit, and skips were invisible -- they went to the log only. Three causes,
three fixes:

1. ``HEAD moved since the review started`` (235) -- the coordinator commits
   often. A moved HEAD now blocks only when one of the reviewer's OWN files
   was touched in ``start..HEAD``; otherwise the nodes land as a clean commit
   on top.
2. ``the user already had uncommitted changes on <file>`` (293), mostly a
   CASCADE: the "user" changes were the reviewer's OWN leftovers from an
   earlier skipped run. A ledger of the paths (and the sha of the bytes) the
   reviewer wrote lets a later run fold them into its own commit. A file
   whose bytes differ from the ledger is the user's and is still never
   committed.
3. every skip is now surfaced: one alert row through the shipped
   ``agent-alerts-deliver`` channel (the next prompt / tool call of the
   session carries it), plus a line in the session-start / compaction
   re-inject while anything remains.

Every test drives REAL temporary git repositories -- git itself is never
mocked -- and the reviewer's ``claude`` CLI is not involved at all.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "templates" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import agent_alerts  # noqa: E402
from vco_lib import kg_reviewer_guard as guard  # noqa: E402
from vco_lib import kg_reviewer_pending as pending  # noqa: E402

_REAL_RUN = subprocess.run

NODE = """---
title: Cache layer
type: concept
tags: [cache]
status: active
updated: 2026-01-01T00:00:00Z
---

# Cache layer

The cache keeps 100 entries.
"""

NEW_NODE = """---
title: Warm start
type: concept
tags: [startup]
status: active
updated: 2026-01-02T00:00:00Z
---

# Warm start

A warm start skips the migration sweep.
"""

CACHE = "knowledge/concepts/cache-layer.md"
NEW = "knowledge/concepts/warm-start.md"


def _git(repo: Path, *args: str) -> str:
    return _REAL_RUN(["git", *args], cwd=repo, check=True,
                     capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k, v in {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    r = tmp_path / "proj"
    (r / "knowledge" / "concepts").mkdir(parents=True)
    (r / "knowledge" / "concepts" / "cache-layer.md").write_text(NODE)
    (r / "src.py").write_text("x = 1\n")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _commit(repo: Path, snap, kept: List[str],
            reviewer_bytes=None) -> "tuple[Optional[str], List[str]]":
    """Commit ``kept``; by default the reviewer's bytes are the disk bytes."""
    if reviewer_bytes is None:
        reviewer_bytes = {p: (repo / p).read_bytes() for p in kept
                          if (repo / p).exists()}
    lines: List[str] = []
    sha = guard.commit_reviewer_changes(
        snap, kept, reviewer_bytes=reviewer_bytes, short="abc1234",
        subject="feat: warm start", model="sonnet", attributed=True,
        log=lines.append)
    return sha, lines


REVIEWER_V1 = NODE + "\nreviewer line\n"


def _skip_own_write(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive run #1: CACHE is CLEAN when the review starts, the reviewer
    rewrites it, and the commit is skipped by a git operation that never
    ends. The reviewer's bytes are the whole change: ledgered FOLDABLE."""
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 0.2)
    monkeypatch.setattr(guard, "GIT_OP_POLL_S", 0.05)
    lock = repo / ".git" / "MERGE_HEAD"
    lock.write_text("0" * 40)
    try:
        snap = guard.take_snapshot(repo)
        try:
            (repo / CACHE).write_text(REVIEWER_V1)
            sha, lines = _commit(repo, snap, [CACHE])
        finally:
            snap.cleanup()
    finally:
        lock.unlink()
    assert sha is None
    assert any("a git operation is in progress" in ln for ln in lines), lines
    assert pending.load(repo)["paths"][CACHE]["foldable"] is True


def _skip_on_a_dirty_file(repo: Path) -> None:
    """Drive run #1: the file is already dirty WITH THE USER'S EDIT when the
    review starts, so the commit is skipped; the path is ledgered (named to
    the session) but NOT foldable -- its bytes mix the user's edit in."""
    (repo / CACHE).write_text(NODE + "\nuser scratch line\n")
    snap = guard.take_snapshot(repo)
    try:
        written = NODE + "\nuser scratch line\nreviewer line\n"
        (repo / CACHE).write_text(written)
        sha, lines = _commit(repo, snap, [CACHE])
    finally:
        snap.cleanup()
    assert sha is None
    assert any("already had uncommitted changes" in ln for ln in lines), lines


# ═════════════════════════════════════════════════════════════════════════
# 1. HEAD moved -- disjoint commits, overlap still skips
# ═════════════════════════════════════════════════════════════════════════


def test_head_moved_but_disjoint_from_the_reviewers_files_is_committed(repo):
    """RED before v0.2.105: any moved HEAD stranded the reviewer's nodes."""
    snap = guard.take_snapshot(repo)
    try:
        (repo / "src.py").write_text("x = 2\n")
        _git(repo, "commit", "-q", "-am", "user commit meanwhile")
        before = _git(repo, "rev-parse", "HEAD").strip()
        (repo / NEW).write_text(NEW_NODE)
        sha, lines = _commit(repo, snap, [NEW])
    finally:
        snap.cleanup()
    assert sha, lines
    assert sha != before
    assert _git(repo, "show", "--name-only", "--format=", sha).split() == [NEW]
    assert pending.pending_paths(repo) == []


def test_head_moved_and_overlapping_is_skipped_ledgered_and_surfaced(repo):
    """The overlap rule still skips -- and now says so."""
    snap = guard.take_snapshot(repo)
    try:
        # The intervening commit touches the very node the reviewer rewrites.
        (repo / CACHE).write_text(NODE.replace("100", "150"))
        _git(repo, "commit", "-q", "-am", "user commit meanwhile")
        reviewer = NODE.replace("100", "200")
        (repo / CACHE).write_text(reviewer)
        sha, lines = _commit(repo, snap, [CACHE])
        assert sha is None
        assert any("HEAD moved" in ln for ln in lines), lines
        # ... and it was ledgered with the bytes the reviewer wrote
        assert pending.pending_paths(repo) == [CACHE]
        assert pending.own_leftovers(repo, repo) == {
            CACHE: reviewer.encode()}
        assert pending.surface(repo, "sess-overlap", log=lambda _l: None) is True
        assert "kg-reviewer left 1 file(s) uncommitted" in pending.alert_message(repo)
    finally:
        snap.cleanup()


# ═════════════════════════════════════════════════════════════════════════
# 2. Own leftovers are not user changes
# ═════════════════════════════════════════════════════════════════════════


def test_leftover_with_ledgered_bytes_is_folded_into_the_next_commit(
        repo, monkeypatch):
    """RED before v0.2.105: the cascade -- every later review skipped too."""
    _skip_own_write(repo, monkeypatch)
    assert pending.pending_paths(repo) == [CACHE]
    # Run #2 rewrites nothing of its own: the leftover must still be
    # committed, on its own, by the reviewer.
    snap = guard.take_snapshot(repo)
    try:
        sha, lines = _commit(repo, snap, [])
    finally:
        snap.cleanup()
    assert sha, lines
    assert _git(repo, "show", "--name-only", "--format=", sha).split() == [CACHE]
    assert "reviewer line" in _git(repo, "show", f"{sha}:{CACHE}")
    assert pending.pending_paths(repo) == []


def test_a_skip_over_the_users_own_dirt_is_never_folded(repo):
    """K-1: the file the review skipped because the USER had uncommitted
    changes on it holds the user's edit plus the reviewer's. A later review
    must never commit it (the user's line would be committed under the
    reviewer's name) -- it stays ledgered and named, and nothing commits."""
    _skip_on_a_dirty_file(repo)
    assert pending.load(repo)["paths"][CACHE]["foldable"] is False
    assert pending.own_leftovers(repo, repo) == {}
    before = _git(repo, "rev-parse", "HEAD").strip()
    snap = guard.take_snapshot(repo)
    try:
        sha, lines = _commit(repo, snap, [])
    finally:
        snap.cleanup()
    assert sha is None, lines
    assert _git(repo, "rev-parse", "HEAD").strip() == before
    assert "user scratch line" not in _git(repo, "show", f"HEAD:{CACHE}")
    assert pending.pending_paths(repo) == [CACHE]
    assert CACHE in pending.status_line(repo)


def test_a_review_that_rewrites_a_user_dirty_skip_is_still_blocked(repo):
    """K-1 through the cascade door: run #2 edits the file run #1 skipped
    over the user's dirt -- the user's line must still never be committed."""
    _skip_on_a_dirty_file(repo)
    snap = guard.take_snapshot(repo)
    try:
        (repo / CACHE).write_text(
            (repo / CACHE).read_text() + "second reviewer line\n")
        sha, lines = _commit(repo, snap, [CACHE])
    finally:
        snap.cleanup()
    assert sha is None
    assert any("already had uncommitted changes" in ln for ln in lines), lines
    assert "user scratch line" not in _git(repo, "show", f"HEAD:{CACHE}")


def test_a_second_review_that_touches_its_own_leftover_still_commits(
        repo, monkeypatch):
    """The cascade itself: run #2 edits the file run #1 left dirty. The
    ledger says the dirty state is the reviewer's, so the "user already had
    uncommitted changes" block must not fire again."""
    _skip_own_write(repo, monkeypatch)
    assert pending.pending_paths(repo) == [CACHE]
    v2 = REVIEWER_V1 + "another reviewer line\n"
    snap = guard.take_snapshot(repo)  # CACHE is dirty again at this start
    try:
        (repo / CACHE).write_text(v2)
        sha, lines = _commit(repo, snap, [CACHE])
    finally:
        snap.cleanup()
    assert sha, lines
    assert "another reviewer line" in _git(repo, "show", f"{sha}:{CACHE}")
    assert pending.pending_paths(repo) == []


def test_a_user_edited_leftover_is_never_committed(repo, monkeypatch):
    """The fold is a byte proof: the user's own edit stays out."""
    _skip_own_write(repo, monkeypatch)
    (repo / CACHE).write_text(
        (repo / CACHE).read_text() + "the user kept editing\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    snap = guard.take_snapshot(repo)
    try:
        sha, lines = _commit(repo, snap, [])
    finally:
        snap.cleanup()
    assert sha is None
    assert _git(repo, "rev-parse", "HEAD").strip() == before
    assert "the user kept editing" in (repo / CACHE).read_text()
    # still tracked as uncommitted work somebody should look at
    assert pending.pending_paths(repo) == [CACHE]
    assert pending.own_leftovers(repo, repo) == {}


def test_a_ledgered_file_the_user_edited_is_not_exempt_when_rewritten(
        repo, monkeypatch):
    """K-2: ledgered -> the user edits it -> this run's reviewer edits it
    again. Its dirt at the start is the USER's now, so the user-changes
    block applies and nothing is committed."""
    _skip_own_write(repo, monkeypatch)
    (repo / CACHE).write_text(REVIEWER_V1 + "the user's own line\n")
    before = _git(repo, "rev-parse", "HEAD").strip()
    snap = guard.take_snapshot(repo)
    try:
        (repo / CACHE).write_text(
            REVIEWER_V1 + "the user's own line\nreviewer again\n")
        sha, lines = _commit(repo, snap, [CACHE])
    finally:
        snap.cleanup()
    assert sha is None, lines
    assert any("already had uncommitted changes" in ln for ln in lines), lines
    assert _git(repo, "rev-parse", "HEAD").strip() == before


def test_an_entry_the_user_committed_is_cleared(repo, monkeypatch):
    """K-3: the user edits the leftover and commits it themselves -- the next
    run drops the entry, and the re-inject line goes quiet."""
    _skip_own_write(repo, monkeypatch)
    (repo / CACHE).write_text(REVIEWER_V1 + "the user's own line\n")
    _git(repo, "commit", "-q", "-am", "user commits it")
    assert pending.pending_paths(repo) == [CACHE]
    snap = guard.take_snapshot(repo)
    try:
        sha, _lines = _commit(repo, snap, [])
    finally:
        snap.cleanup()
    assert sha is None
    assert pending.pending_paths(repo) == []
    assert pending.status_line(repo) == ""


def test_a_shrinking_pending_set_raises_no_new_alert(repo, monkeypatch):
    """K-4: two files pending and alerted; one is committed -> no new row."""
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 0.2)
    monkeypatch.setattr(guard, "GIT_OP_POLL_S", 0.05)
    lock = repo / ".git" / "MERGE_HEAD"
    lock.write_text("0" * 40)
    try:
        snap = guard.take_snapshot(repo)
        try:
            (repo / CACHE).write_text(REVIEWER_V1)
            (repo / NEW).write_text(NEW_NODE)
            sha, _lines = _commit(repo, snap, [CACHE, NEW])
        finally:
            snap.cleanup()
    finally:
        lock.unlink()
    assert sha is None
    assert pending.surface(repo, "sess-k4", log=lambda _l: None) is True
    assert len(agent_alerts.collect(str(repo), "sess-k4")) == 1
    _git(repo, "add", NEW)
    _git(repo, "commit", "-q", "-m", "user commits one of them")
    pending.prune_settled(repo, repo, lambda rel: guard._head_bytes(repo, rel))
    assert pending.pending_paths(repo) == [CACHE]
    assert pending.surface(repo, "sess-k4", log=lambda _l: None) is False
    assert agent_alerts.collect(str(repo), "sess-k4") == []
    # A NEW skip (new reviewer bytes) still alerts.
    entries = pending.load(repo)["paths"]
    assert pending.record_skips(
        repo, repo, {CACHE: entries[CACHE]["sha256"]}, reason="r",
        short="s", foldable=[CACHE]) == [CACHE]
    (repo / CACHE).write_text(REVIEWER_V1 + "more\n")
    import hashlib
    pending.record_skips(
        repo, repo,
        {CACHE: hashlib.sha256((repo / CACHE).read_bytes()).hexdigest()},
        reason="r", short="s", foldable=[CACHE])
    assert pending.surface(repo, "sess-k4", log=lambda _l: None) is True


def test_the_git_op_retry_never_runs_past_the_runs_budget(repo, monkeypatch):
    """K-7: a review that spent its whole budget gets no git-op wait."""
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 30.0)
    lock = repo / ".git" / "MERGE_HEAD"
    lock.write_text("0" * 40)
    lines: List[str] = []
    try:
        snap = guard.take_snapshot(repo)
        try:
            (repo / NEW).write_text(NEW_NODE)
            t0 = time.monotonic()
            sha = guard.commit_reviewer_changes(
                snap, [NEW], reviewer_bytes={NEW: NEW_NODE.encode()},
                short="abc1234", subject="s", model="sonnet",
                attributed=True, log=lines.append,
                retry_deadline=time.monotonic() - 1)
            elapsed = time.monotonic() - t0
        finally:
            snap.cleanup()
    finally:
        lock.unlink()
    assert sha is None
    assert elapsed < 5, elapsed
    assert not any("commit deferred" in ln for ln in lines), lines
    assert guard._retry_window(time.monotonic() + 3) <= 3
    assert guard._retry_window(None) == 30.0


# ═════════════════════════════════════════════════════════════════════════
# 3. Every skip is surfaced
# ═════════════════════════════════════════════════════════════════════════


def test_alert_is_emitted_once_and_cleared_when_the_files_are_committed(
        repo, monkeypatch):
    _skip_own_write(repo, monkeypatch)
    first = pending.surface(repo, "sess-1", log=lambda _l: None)
    assert first is True
    rows = agent_alerts.collect(str(repo), "sess-1")
    assert len(rows) == 1
    assert "kg-reviewer left 1 file(s) uncommitted" in rows[0]["message"]
    assert CACHE in rows[0]["message"]
    # The same pending set never alerts twice.
    assert pending.surface(repo, "sess-1", log=lambda _l: None) is False
    assert agent_alerts.collect(str(repo), "sess-1") == []
    # ... and once the files are committed nothing is pending any more.
    snap = guard.take_snapshot(repo)
    try:
        sha, _lines = _commit(repo, snap, [])
    finally:
        snap.cleanup()
    assert sha
    assert pending.pending_paths(repo) == []
    assert pending.status_line(repo) == ""
    assert pending.alert_message(repo) == ""
    assert pending.surface(repo, "sess-1", log=lambda _l: None) is False


def test_the_alert_reaches_the_main_chat_on_the_next_prompt(repo):
    """Not just filed: delivered by the shipped alert hook."""
    _skip_on_a_dirty_file(repo)
    assert pending.surface(repo, "sess-2", log=lambda _l: None) is True
    envelope = agent_alerts.deliver_for_payload(str(repo), {
        "hook_event_name": "UserPromptSubmit", "session_id": "sess-2"})
    assert "kg-reviewer left 1 file(s) uncommitted" in envelope
    assert "knowledge/concepts/cache-layer.md" in envelope


def test_the_reinject_status_line_names_the_pending_files(repo):
    _skip_on_a_dirty_file(repo)
    line = pending.status_line(repo)
    assert line.startswith("## KG reviewer: 1 file(s) left uncommitted since ")
    assert CACHE in line
    assert "Full list" not in line  # every path is already named


def test_a_long_status_line_names_a_full_list_command_that_works(repo):
    """K-6: the command printed for the rest of the list must run as printed
    -- the resolved interpreter, never a bare `python` -- and list them all."""
    import shlex
    rels = [f"knowledge/concepts/n{i}.md" for i in range(pending.MAX_PATHS_IN_LINE + 3)]
    for rel in rels:
        (repo / rel).write_text(f"node {rel}\n")
    pending.record_skips(
        repo, repo,
        {rel: pending._sha((repo / rel).read_bytes()) for rel in rels},
        reason="r", short="s", foldable=rels)
    line = pending.status_line(repo)
    assert "(+3 more)" in line
    cmd = line.split("Full list: ", 1)[1]
    assert cmd.startswith(f'"{sys.executable}" -m vco_lib.kg_reviewer_pending paths')
    proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                          timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == sorted(rels)


# ═════════════════════════════════════════════════════════════════════════
# 4. "a git operation is in progress" -- retried once
# ═════════════════════════════════════════════════════════════════════════


def test_a_git_operation_that_ends_within_the_wait_is_retried(
        repo, monkeypatch):
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 10.0)
    monkeypatch.setattr(guard, "GIT_OP_POLL_S", 0.05)
    lock = repo / ".git" / "MERGE_HEAD"
    lock.write_text("0" * 40)
    snap = guard.take_snapshot(repo)
    timer = threading.Timer(0.4, lock.unlink)
    try:
        (repo / NEW).write_text(NEW_NODE)
        timer.start()
        sha, lines = _commit(repo, snap, [NEW])
    finally:
        snap.cleanup()
        timer.cancel()
        if lock.exists():
            lock.unlink()
    assert sha, lines
    assert any("commit deferred" in ln for ln in lines), lines
    assert pending.pending_paths(repo) == []


def test_a_git_operation_that_never_ends_is_ledgered_and_surfaced(
        repo, monkeypatch):
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 0.3)
    monkeypatch.setattr(guard, "GIT_OP_POLL_S", 0.05)
    lock = repo / ".git" / "MERGE_HEAD"
    lock.write_text("0" * 40)
    try:
        snap = guard.take_snapshot(repo)
        try:
            (repo / NEW).write_text(NEW_NODE)
            sha, lines = _commit(repo, snap, [NEW])
        finally:
            snap.cleanup()
    finally:
        lock.unlink()
    assert sha is None
    assert any("a git operation is in progress (MERGE_HEAD)" in ln
               for ln in lines), lines
    assert pending.pending_paths(repo) == [NEW]
    assert pending.surface(repo, "sess-3", log=lambda _l: None) is True
    assert NEW in pending.alert_message(repo)


def test_the_retry_does_not_swallow_a_real_user_conflict(repo, monkeypatch):
    """A block that is NOT an in-flight git operation is never retried."""
    monkeypatch.setattr(guard, "GIT_OP_RETRY_S", 30.0)
    monkeypatch.setattr(guard, "GIT_OP_POLL_S", 0.05)
    snap = guard.take_snapshot(repo)
    started = time.monotonic()
    try:
        # The intervening commit touches the very node the reviewer rewrites.
        reviewer = NODE.replace("100", "200")
        (repo / CACHE).write_text(NODE.replace("100", "150"))
        _git(repo, "commit", "-q", "-am", "user commit meanwhile")
        (repo / CACHE).write_text(reviewer)
        sha, lines = _commit(repo, snap, [CACHE],
                            reviewer_bytes={CACHE: reviewer.encode()})
    finally:
        snap.cleanup()
    assert sha is None
    assert time.monotonic() - started < 5.0
    assert any("HEAD moved" in ln for ln in lines), lines


# ═════════════════════════════════════════════════════════════════════════
# 5. The hooks hand the session id to the reviewer (.sh / .ps1 parity)
# ═════════════════════════════════════════════════════════════════════════


def _read_payload(path: Path, env: dict, sibling: str, payload: dict):
    """Run the hook with ``payload`` on stdin."""
    from tests.test_v02102_w6n_kg_reviewer_fixes import _hook_cmd

    proc = subprocess.run(_hook_cmd(sibling), cwd=path, env=env,
                          input=json.dumps(payload), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def _stub_chain_repo(tmp_path: Path) -> "tuple[Path, dict]":
    from tests.test_v02102_w6n_kg_reviewer_fixes import _scratch_repo
    from tests.test_v02102_w6r_kg_reviewer_broken_install import _STUB_CHAIN

    repo, env = _scratch_repo(tmp_path, commits=1)
    scripts = repo / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "kg_reviewer_backend.py").write_text(_STUB_CHAIN, encoding="utf-8")
    return repo, env


def _wait_chain_argv(repo: Path) -> str:
    marker = repo / ".claude" / "logs" / "chain-ran.marker"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.exists(), "the chain must run"
    return marker.read_text(encoding="utf-8")


_HOOK_SIBLINGS = [
    pytest.param("sh", marks=pytest.mark.skipif(
        sys.platform == "win32", reason="bash .sh leg; the [ps1] param covers Windows")),
    pytest.param("ps1", marks=pytest.mark.skipif(
        shutil.which("pwsh") is None, reason="pwsh not installed")),
]


@pytest.mark.parametrize("sibling", _HOOK_SIBLINGS)
def test_the_hook_hands_the_session_id_to_the_reviewer(tmp_path, sibling):
    """RED before v0.2.105: the reviewer never learned the session, so its
    "N files left uncommitted" alert had nobody it could be filed for. The
    .ps1 leg was RED a second way: it stored stdin in ``$Input`` -- the
    automatic pipeline variable -- so pwsh fed the payload to that
    enumerator and the hook read an empty payload (no session id)."""
    repo, env = _stub_chain_repo(tmp_path)
    _read_payload(repo, env, sibling, {
        "tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
        "tool_response": "[main abc123] committed",
        "session_id": "sess-abc-123"})
    argv = _wait_chain_argv(repo)
    assert "--session-id sess-abc-123" in argv, argv


@pytest.mark.parametrize("sibling", _HOOK_SIBLINGS)
def test_the_hook_passes_no_session_id_when_the_payload_has_none(
        tmp_path, sibling):
    """No empty argv entry either: the Windows command-line join would turn
    ``--session-id ''`` into a broken spawn."""
    repo, env = _stub_chain_repo(tmp_path)
    _read_payload(repo, env, sibling, {
        "tool_name": "Bash", "tool_input": {"command": "git commit -m x"},
        "tool_response": "[main abc123] committed"})
    argv = _wait_chain_argv(repo)
    assert "--session-id" not in argv, argv


# ═════════════════════════════════════════════════════════════════════════
# 6. The session-start / compaction re-inject shows the same count
# ═════════════════════════════════════════════════════════════════════════

_HOOKS = REPO_ROOT / "templates" / "hooks"
_PWSH = shutil.which("pwsh")
_REINJECT_IMPLS = [
    pytest.param("sh", marks=pytest.mark.skipif(
        sys.platform == "win32", reason="bash .sh leg; the [ps1] param covers Windows")),
    pytest.param("ps1", marks=pytest.mark.skipif(
        _PWSH is None, reason="pwsh not installed")),
]


def _stub_venv(tmp_path: Path) -> Path:
    """A VCT_VENV-shaped folder whose interpreter is the running one: the
    hook's python import resolves through PYTHONPATH (this worktree)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(sys.executable, venv / "bin" / "python3")
    return venv


def _reinject_argv(impl: str) -> "list[str]":
    if impl == "sh":
        return ["bash", str(_HOOKS / "compact-context-reinject.sh")]
    from tests.common.pwsh import PWSH_BASE_FLAGS

    return [str(_PWSH), *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
            str(_HOOKS / "compact-context-reinject.ps1")]


def _run_reinject(impl: str, proj: Path, tmp_path: Path) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCO_HOOK_CONTEXT_CAP",
                        "VCO_SNAPSHOT_MAX_AGE", "VCT_VENV",
                        "VCT_INSTALL_ROOT")}
    env.update({"CLAUDE_PROJECT_DIR": str(proj),
                "PYTHONPATH": str(REPO_ROOT),
                "VCT_VENV": str(_stub_venv(tmp_path)),
                "STDERR_CAP_DISABLE": "1"})
    proc = subprocess.run(_reinject_argv(impl), cwd=proj, env=env,
                          input=json.dumps({"source": "startup",
                                            "session_id": "s1"}),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc.stdout


def _ledger_with_two_paths(proj: Path) -> None:
    state = proj / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    state.joinpath("kg_reviewer_pending.json").write_text(json.dumps({
        "v": 1,
        "paths": {
            "knowledge/concepts/cache-layer.md": {
                "sha256": "a" * 64, "first_seen": "2026-11-01T10:00:00Z",
                "last_skip": "2026-11-02T10:00:00Z", "reason": "x",
                "commit": "abc1234"},
            "knowledge/concepts/warm-start.md": {
                "sha256": "b" * 64, "first_seen": "2026-11-03T10:00:00Z",
                "last_skip": "2026-11-03T10:00:00Z", "reason": "x",
                "commit": "abc1234"},
        },
        "alert_sig": "",
    }), encoding="utf-8")


@pytest.mark.parametrize("impl", _REINJECT_IMPLS)
def test_the_reinject_shows_how_many_reviewer_files_are_pending(
        impl, tmp_path):
    """RED before v0.2.105: nothing ever told the session about a skip."""
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    _ledger_with_two_paths(proj)
    out = _run_reinject(impl, proj, tmp_path)
    assert "## KG reviewer: 2 file(s) left uncommitted since " in out, out
    assert "knowledge/concepts/cache-layer.md" in out, out


@pytest.mark.parametrize("impl", _REINJECT_IMPLS)
def test_the_reinject_says_nothing_when_nothing_is_pending(impl, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    out = _run_reinject(impl, proj, tmp_path)
    assert "KG reviewer" not in out, out