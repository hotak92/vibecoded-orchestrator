# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — one KG review at a time; a burst of commits is coalesced.

Proves the lock / queue half of the latest-status work against review
V02105-KGREV2 (real temp git repos, the ``claude`` CLI always faked):

* the lock answers acquired / busy / ERROR — an I/O failure is loud and the
  commit is still reviewed (M3);
* a stale lock is taken over by exactly one contender (H5), a live holder
  is never taken over and keeps its heartbeat fresh (M6), and liveness never
  goes through ``os.kill`` on Windows (H4);
* a queued commit leaves the queue only when a pass covering it succeeded
  or by a logged drop — a kg-sync / unreadable newest sha, a crash, a
  failed pass lose nothing (M2); the own commit is reviewed once (L3); one
  pass covers at most ``coalesced_max`` commits (L2).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "templates" / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import kg_reviewer_queue as q  # noqa: E402

_REAL_RUN = subprocess.run


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
    (r / "knowledge").mkdir(parents=True)
    (r / "knowledge" / "n.md").write_text("---\ntitle: N\n---\n\nsrc.py\n")
    (r / "src.py").write_text("x = 0\n")
    _git(r, "init", "-q")
    (r / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (r / ".git" / "info" / "exclude").write_text(".claude/state/\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _commit(repo: Path, n: int, subject: str = "") -> str:
    (repo / "src.py").write_text(f"x = {n}\n")
    _git(repo, "commit", "-q", "-am", subject or f"feat: x is {n}")
    return _git(repo, "rev-parse", "HEAD").strip()


def _write_lock(repo: Path, payload: dict) -> Path:
    path = q.lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ═════════════════════════════════════════════════════════════════════════
# 1. The lock
# ═════════════════════════════════════════════════════════════════════════


def test_acquire_busy_release(repo):
    a = q.acquire(repo)
    assert a.status == q.ACQUIRED and a.lock is not None
    b = q.acquire(repo)
    assert b.status == q.BUSY and "this process" in b.detail
    a.lock.release()
    c = q.acquire(repo)
    assert c.status == q.ACQUIRED
    assert c.lock is not None
    c.lock.release()
    assert not q.lock_path(repo).exists()


def test_an_unusable_state_dir_is_an_error_not_busy(repo):
    """M3: a path collision (``.claude/state`` is a FILE) is ERROR."""
    (repo / ".claude").mkdir()
    (repo / ".claude" / "state").write_text("not a dir")
    res = q.acquire(repo)
    assert res.status == q.ERROR and res.detail


def test_a_dead_holder_with_a_fresh_heartbeat_is_taken_over(repo):
    """Split stale test 1/2: the dead pid ALONE decides (heartbeat fresh)."""
    _write_lock(repo, {"pid": _dead_pid(), "token": "old",
                       "heartbeat": time.time()})
    lines: List[str] = []
    res = q.acquire(repo, log=lines.append)
    assert res.status == q.ACQUIRED, res.detail
    assert any("taking it over" in ln for ln in lines)
    assert res.lock is not None
    res.lock.release()


def test_a_live_holder_with_an_old_heartbeat_is_never_taken_over(repo):
    """Split stale test 2/2: an ALIVE holder (our parent process, started
    long before its heartbeat) is kept however old the heartbeat."""
    from vco_lib.update_lock import process_start_time

    ppid = os.getppid()
    started = process_start_time(ppid)
    beat = (started + 1.0) if started else time.time() - 3600
    _write_lock(repo, {"pid": ppid, "token": "live", "heartbeat": beat})
    res = q.acquire(repo, stale_s=1.0)
    assert res.status == q.BUSY, res.detail
    assert json.loads(q.lock_path(repo).read_text())["token"] == "live"


def test_a_recycled_pid_is_taken_over(repo):
    """The pid is alive but its process started AFTER the lock's last
    heartbeat: it cannot be the holder."""
    from vco_lib.update_lock import process_start_time

    child = subprocess.Popen(["sleep", "30"])
    try:
        if process_start_time(child.pid) is None:
            pytest.skip("process start time unknown on this machine")
        _write_lock(repo, {"pid": child.pid, "token": "old",
                           "heartbeat": time.time() - 3600})
        res = q.acquire(repo, stale_s=60.0)
        assert res.status == q.ACQUIRED, res.detail
        assert res.lock is not None
        res.lock.release()
    finally:
        child.kill()
        child.wait()


def test_a_takeover_rechecks_what_it_claimed(repo, monkeypatch):
    """H5: between judging the lock stale and claiming it, another run
    replaced it with a FRESH lock. The claim re-reads the bytes, sees they
    are not what it judged, puts the fresh lock back untouched: BUSY."""
    path = _write_lock(repo, {"pid": _dead_pid(), "token": "stale",
                              "heartbeat": time.time()})
    real_claim = q.claim_by_rename

    def racing_claim(p, mark):
        p.write_text(json.dumps({"pid": os.getppid(), "token": "fresh",
                                 "heartbeat": time.time()}))
        return real_claim(p, mark)

    monkeypatch.setattr(q, "claim_by_rename", racing_claim)
    res = q.acquire(repo)
    assert res.status == q.BUSY, res.detail
    assert json.loads(path.read_text())["token"] == "fresh"


def test_concurrent_takeovers_admit_exactly_one(repo, monkeypatch):
    """H5: eight contenders race for the same stale lock. Judging is slowed
    (so every contender could judge the SAME stale bytes) and each claim is
    staggered (contender i claims i*30 ms later — after an earlier one has
    already created its fresh lock). Exactly one acquires: the others either
    wait on the ops lock and then see a live lock, or re-read what they
    claimed and put it back."""
    _write_lock(repo, {"pid": _dead_pid(), "token": "stale",
                       "heartbeat": time.time()})
    real_gone = q._holder_gone
    real_claim = q.claim_by_rename

    def slow_gone(*a, **k):
        time.sleep(0.05)
        return real_gone(*a, **k)

    def staggered_claim(path, mark):
        time.sleep(0.03 * int(threading.current_thread().name.split("-")[-1]))
        return real_claim(path, mark)

    monkeypatch.setattr(q, "_holder_gone", slow_gone)
    monkeypatch.setattr(q, "claim_by_rename", staggered_claim)
    barrier = threading.Barrier(8)
    results: List[str] = []

    def contend() -> None:
        barrier.wait()
        results.append(q.acquire(repo).status)

    threads = [threading.Thread(target=contend, name=f"contender-{i}")
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(q.ACQUIRED) == 1, results


def test_the_heartbeat_keeps_a_live_lock_fresh_and_notices_a_takeover(repo):
    """M6: the holder refreshes its heartbeat while it runs; once the lock
    is not ours any more it stops refreshing (``lost``) and its release
    leaves the successor's lock alone."""
    res = q.acquire(repo)
    assert res.lock is not None
    lock = res.lock
    first = json.loads(q.lock_path(repo).read_text())["heartbeat"]
    lock.start_heartbeat(interval_s=0.05)
    beat = first
    deadline = time.time() + 5
    while time.time() < deadline:
        beat = json.loads(q.lock_path(repo).read_text())["heartbeat"]
        if beat > first:
            break
        time.sleep(0.02)
    assert beat > first
    q.lock_path(repo).write_text(json.dumps({"pid": os.getppid(),
                                             "token": "successor",
                                             "heartbeat": time.time()}))
    deadline = time.time() + 5
    while not lock.lost and time.time() < deadline:
        time.sleep(0.02)
    assert lock.lost
    lock.release()
    assert json.loads(q.lock_path(repo).read_text())["token"] == "successor"


def test_liveness_on_windows_never_uses_os_kill(repo, monkeypatch):
    """H4: on Windows ``os.kill(pid, 0)`` sends CTRL_C_EVENT. The lock's
    liveness goes through the one cross-OS rule (``update_lock.pid_alive``
    -> ``OpenProcess`` there), never ``os.kill``."""
    from vco_lib import update_lock

    calls: List[int] = []
    monkeypatch.setattr(update_lock, "_IS_WINDOWS", True)
    monkeypatch.setattr(update_lock, "_psutil", lambda: None)
    monkeypatch.setattr(update_lock, "_win_pid_alive",
                        lambda pid: calls.append(pid) or False)

    def no_kill(*_a, **_k):
        raise AssertionError("os.kill must not be a liveness probe")

    monkeypatch.setattr(os, "kill", no_kill)
    _write_lock(repo, {"pid": 4242, "token": "old",
                       "heartbeat": time.time()})
    res = q.acquire(repo)
    assert res.status == q.ACQUIRED and calls == [4242]
    assert res.lock is not None
    res.lock.release()


# ═════════════════════════════════════════════════════════════════════════
# 2. The queue
# ═════════════════════════════════════════════════════════════════════════


def test_the_queue_peeks_acks_and_counts_failures(repo):
    assert q.enqueue(repo, "a" * 40)
    assert q.enqueue(repo, "b" * 40)
    assert q.enqueue(repo, "a" * 40)  # de-duplicated
    assert [i.sha for i in q.peek(repo) or []] == ["a" * 40, "b" * 40]
    assert [i.sha for i in q.peek(repo) or []] == ["a" * 40, "b" * 40], \
        "peek never removes"
    lines: List[str] = []
    for _ in range(q.MAX_ATTEMPTS):
        q.record_failure(repo, ["a" * 40], lines.append)
    assert [i.sha for i in q.peek(repo) or []] == ["b" * 40]
    assert any("after 3 failed passes" in ln for ln in lines)
    q.ack(repo, ["b" * 40])
    assert q.peek(repo) == []


def test_the_queue_is_bounded_and_logs_every_drop(repo, monkeypatch):
    monkeypatch.setattr(q, "MAX_QUEUE", 2)
    lines: List[str] = []
    for c in "abc":
        q.enqueue(repo, c * 40, lines.append)
    assert [i.sha for i in q.peek(repo) or []] == ["b" * 40, "c" * 40]
    assert any("DROPPED the oldest, aaaaaaaaaaaa" in ln for ln in lines)


def test_an_unreadable_queue_is_logged_not_empty(repo):
    q.queue_path(repo).parent.mkdir(parents=True, exist_ok=True)
    q.queue_path(repo).write_text("{broken", encoding="utf-8")
    lines: List[str] = []
    assert q.peek(repo, lines.append) is None
    assert any("unreadable" in ln for ln in lines)
    assert not q.enqueue(repo, "a" * 40, lines.append)
    assert q.queue_path(repo).read_text() == "{broken", "never clobbered"


# ═════════════════════════════════════════════════════════════════════════
# 3. The backend run loop
# ═════════════════════════════════════════════════════════════════════════


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sb = _load("_v02105q_summary_backends", SCRIPTS_DIR / "summary_backends.py")
    sb.reset_backend_cache()
    monkeypatch.setattr(sb, "_breaker_path", lambda: tmp_path / "breaker.json")
    mod = _load("_v02105q_kg_reviewer_backend",
                SCRIPTS_DIR / "kg_reviewer_backend.py")
    setattr(mod, "_sb", sb)
    monkeypatch.delenv("VCO_KG_REVIEW_MAX_TURNS", raising=False)
    monkeypatch.delenv("VCO_KG_REVIEW_MODEL", raising=False)
    monkeypatch.setattr(
        mod, "select_reviewer_backend",
        lambda log=None, model=None: {"mode": "claude", "model": "sonnet",
                                      "base_url": None, "reason": "test"})
    monkeypatch.setattr(mod, "resolve_claude_executable",
                        lambda: "/usr/bin/claude")
    mod.prompts = []  # type: ignore[attr-defined]
    mod.exit_codes = []  # type: ignore[attr-defined]

    def fake_run(cmd, prompt, env, cwd, timeout_s, *, direct_rung, log):
        mod.prompts.append(prompt)  # type: ignore[attr-defined]
        code = mod.exit_codes.pop(0) if mod.exit_codes else 0  # type: ignore[attr-defined]
        if code == "crash":
            raise RuntimeError("the reviewer process died")
        return mod.ReviewRun("", "", code, ("", "", code))

    monkeypatch.setattr(mod, "run_with_quota_fallback", fake_run)
    spawned: List[str] = []
    mod.spawned = spawned  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "_spawn_successor",
                        lambda args, log, root, sha: spawned.append(sha) or True)
    return mod


def _run(chain, repo: Path, tmp_path: Path, sha: str, *extra: str) -> int:
    return chain.main(["run", "--project-root", str(repo), "--commit", sha,
                       "--log", str(tmp_path / "r.log"), *extra])


def _queued(repo: Path) -> List[str]:
    return [i.sha for i in q.peek(repo) or []]


def _commit_lines(prompt: str) -> List[str]:
    return [ln for ln in prompt.splitlines() if ln.startswith("## Commit: ")
            or ln.startswith("#### `")]


def test_a_burst_is_covered_by_one_pass_and_the_own_commit_once(
        chain, repo, tmp_path):
    c1, c2, c3 = _commit(repo, 1), _commit(repo, 2), _commit(repo, 3)
    held = q.acquire(repo)
    assert held.lock is not None
    assert _run(chain, repo, tmp_path, c1[:12]) == 0
    assert _run(chain, repo, tmp_path, c2[:12]) == 0
    assert chain.prompts == [] and _queued(repo) == [c1, c2]
    held.lock.release()
    q.enqueue(repo, c3)  # a retried hook queued the run's own commit too
    assert _run(chain, repo, tmp_path, c3[:12]) == 0
    assert len(chain.prompts) == 2, [_commit_lines(p) for p in chain.prompts]
    short = {c: _git(repo, "rev-parse", "--short", c).strip()
             for c in (c1, c2, c3)}
    assert f"## Commit: {short[c3]}" in chain.prompts[0]
    assert f"## Commit: {short[c2]}" in chain.prompts[1]
    assert f"#### `{short[c1]}`" in chain.prompts[1]
    assert "x = 1" in chain.prompts[1], "the coalesced commit's diff"
    assert _queued(repo) == [] and chain.spawned == []


def test_a_kg_sync_newest_never_drops_the_older_commits(chain, repo, tmp_path):
    """M2 repro: HEAD at enqueue time was the reviewer's own kg-sync commit.
    It is dropped (logged); the older queued commit is still reviewed."""
    c1 = _commit(repo, 1)
    k = _commit(repo, 2, "kg-sync: abc1234 feat: nodes")
    c3 = _commit(repo, 3)
    q.enqueue(repo, c1)
    q.enqueue(repo, k)
    assert _run(chain, repo, tmp_path, c3[:12]) == 0
    assert len(chain.prompts) == 2
    short1 = _git(repo, "rev-parse", "--short", c1).strip()
    assert f"## Commit: {short1}" in chain.prompts[1]
    log = (tmp_path / "r.log").read_text()
    assert "a reviewer (kg-sync:) commit is never reviewed" in log
    assert _queued(repo) == []


def test_an_unreadable_queued_sha_is_dropped_with_a_log_line(
        chain, repo, tmp_path):
    c1, c2 = _commit(repo, 1), _commit(repo, 2)
    q.enqueue(repo, c1)
    q.enqueue(repo, "0" * 40)
    assert _run(chain, repo, tmp_path, c2[:12]) == 0
    short1 = _git(repo, "rev-parse", "--short", c1).strip()
    assert f"## Commit: {short1}" in chain.prompts[1]
    assert "dropped 000000000000 - git cannot read" in (
        tmp_path / "r.log").read_text()
    assert _queued(repo) == []


def test_a_crash_mid_pass_loses_no_queued_commit(chain, repo, tmp_path):
    """M2: the queue is peeked, not drained — a pass that dies leaves its
    commits queued for the next run."""
    c1, c2, c3 = _commit(repo, 1), _commit(repo, 2), _commit(repo, 3)
    q.enqueue(repo, c1)
    q.enqueue(repo, c2)
    chain.exit_codes[:] = [0, "crash"]
    with pytest.raises(RuntimeError):
        _run(chain, repo, tmp_path, c3[:12])
    assert _queued(repo) == [c1, c2]
    assert not q.lock_path(repo).exists(), "the lock is released"


def test_a_failed_pass_keeps_its_commits_and_counts_the_failure(
        chain, repo, tmp_path):
    c1, c2 = _commit(repo, 1), _commit(repo, 2)
    q.enqueue(repo, c1)
    chain.exit_codes[:] = [0, 1]
    assert _run(chain, repo, tmp_path, c2[:12]) == 1
    items = q.peek(repo) or []
    assert [(i.sha, i.attempts) for i in items] == [(c1, 1)]
    assert chain.spawned == [], "no successor hammers a failing backend"


def test_a_failed_own_pass_leaves_the_queue_alone(chain, repo, tmp_path):
    c1, c2 = _commit(repo, 1), _commit(repo, 2)
    q.enqueue(repo, c1)
    chain.exit_codes[:] = [1]
    assert _run(chain, repo, tmp_path, c2[:12]) == 1
    assert len(chain.prompts) == 1 and _queued(repo) == [c1]


def test_one_pass_covers_at_most_coalesced_max(chain, repo, tmp_path,
                                               monkeypatch):
    """L2: the batch is bounded; what does not fit stays queued for the
    next pass (and, past the process's pass budget, a successor)."""
    monkeypatch.setattr(chain._prompt, "coalesced_max", lambda: 2)
    monkeypatch.setattr(chain, "MAX_PASSES_PER_RUN", 2)
    shas = [_commit(repo, n) for n in range(1, 6)]
    for s in shas[:4]:
        q.enqueue(repo, s)
    assert _run(chain, repo, tmp_path, shas[4][:12]) == 0
    assert len(chain.prompts) == 2
    assert _queued(repo) == shas[2:4]
    assert chain.spawned == [shas[3]]


def test_a_reviewer_commit_is_not_reviewed_or_queued(chain, repo, tmp_path):
    k = _commit(repo, 1, "kg-sync: abc1234 feat: nodes")
    held = q.acquire(repo)
    assert _run(chain, repo, tmp_path, k[:12]) == 0
    assert _queued(repo) == [] and chain.prompts == []
    assert held.lock is not None
    held.lock.release()


def test_a_lock_error_is_loud_and_the_commit_still_reviewed(
        chain, repo, tmp_path, capsys):
    """M3: the state dir is unusable -> log + stderr, and the commit is
    reviewed without the lock (never silently muted)."""
    c1 = _commit(repo, 1)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "state").write_text("not a dir")
    assert _run(chain, repo, tmp_path, c1[:12], "--session-id", "s1") == 0
    assert len(chain.prompts) == 1
    log = (tmp_path / "r.log").read_text()
    assert "cannot be used" in log and "WITHOUT the one-review" in log
    assert "could not queue" not in log, "the ERROR branch, not busy"
    assert "cannot be used" in capsys.readouterr().err


def test_busy_with_an_unwritable_queue_reviews_unlocked(
        chain, repo, tmp_path, monkeypatch):
    c1 = _commit(repo, 1)
    held = q.acquire(repo)
    monkeypatch.setattr(q, "enqueue", lambda *a, **k: False)
    assert _run(chain, repo, tmp_path, c1[:12]) == 0
    assert len(chain.prompts) == 1
    assert "could not queue" in (tmp_path / "r.log").read_text()
    assert held.lock is not None
    held.lock.release()


def test_a_skip_mode_pass_logs_every_commit_it_acknowledges(
        chain, repo, tmp_path, monkeypatch):
    """N3: no reviewer rung can run, so the pass exits 0 and the caller
    ACKS its commits. Each one gets its own log line — the queue must never
    swallow a commit silently."""
    c1, c2 = _commit(repo, 1), _commit(repo, 2)
    q.enqueue(repo, c1)
    monkeypatch.setattr(
        chain, "select_reviewer_backend",
        lambda log=None, model=None: {"mode": "skip", "model": "",
                                      "base_url": None, "reason": "no rung"})
    assert _run(chain, repo, tmp_path, c2[:12]) == 0
    assert chain.prompts == []
    assert _queued(repo) == []
    log = (tmp_path / "r.log").read_text()
    for c in (c1, c2):
        short = _git(repo, "rev-parse", "--short", c).strip()
        assert f"{short} not reviewed: no reviewer rung available" in log


def test_a_queued_copy_of_the_runs_own_commit_is_acked_on_lock_loss(
        chain, repo, tmp_path, monkeypatch):
    """N4: a retried hook had queued the run's own commit, and the lock is
    lost mid-run — the loop breaks before _next_batch's done-ack. The own
    commit must still leave the queue, so no successor reviews it again."""
    c1 = _commit(repo, 1)
    q.enqueue(repo, c1)          # a retried hook queued this very commit
    real = q.acquire

    def acquire(root, **kw):
        res = real(root, **kw)
        if res.lock is not None:
            res.lock.lost = True     # taken over by a successor mid-run
        return res

    monkeypatch.setattr(q, "acquire", acquire)
    assert _run(chain, repo, tmp_path, c1[:12]) == 0
    assert len(chain.prompts) == 1, "reviewed once, never twice"
    assert _queued(repo) == []
    assert chain.spawned == []


def test_a_crashed_heartbeat_temp_file_is_swept_at_acquire_and_release(repo):
    """A crash between the heartbeat temp write and its ``os.replace``
    leaves ``<lock>.hb.<pid>.<token8>`` in the state dir. Both acquire and
    release sweep it (each holds the ops lock every live writer uses)."""
    state = repo / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    stale = state / "kg_reviewer.lock.hb.999999.deadbeef"
    stale.write_text("{}", encoding="utf-8")
    res = q.acquire(repo)
    assert res.lock is not None
    assert not stale.exists(), "the crash leftover is swept on acquire"
    later = state / "kg_reviewer.lock.hb.999998.cafebabe"
    later.write_text("{}", encoding="utf-8")
    res.lock.release()
    assert not later.exists(), "and again on release"
