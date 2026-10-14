# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Behavioural guard for the v0.2.65 Track B debounce-hardening pass, the
v0.2.103 single-flight fix, and their shared invariants.

Covers:

  * Item 2a (flusher cap): when too many ".lock" dirs are already pending
    (the ceiling, 64), a fresh schedule call must NOT add a 65th sleeping
    flusher — it must fall through to an IMMEDIATE background sync. The
    invariant under test is the strict one from the spec: the cap NEVER
    DROPS a sync, it only swaps "schedule a sleeper" for "sync now".

  * Item 2b (reaper throttle): the reaper globs ALL pending work dirs
    (O(M^2)); it is now rate-limited to once per ~30s via a `.last_reap.ts`
    stamp file. A second reap inside the window must be a no-op (a
    backdated orphan is left untouched); once the stamp ages past the
    throttle window the orphan is recovered (coalesce-NEVER-DROP holds —
    recovery is only delayed, never skipped).

  * v0.2.103 SINGLE-FLIGHT (field defect 2026-10-14: ONE edit of a docs
    file produced FOUR concurrent `sync_knowledge_graph.py` children of
    the debounce flusher, one per re-edit during a minutes-long sync).
    At most ONE in-flight sync per (project, path, channel): re-edits
    arriving while a sync runs are coalesced into exactly ONE follow-up
    sync that re-reads the file from disk (latest content, never lost);
    different paths keep syncing independently. The reaper must not
    "recover" a follow-up lock whose sync is still running.

The test sources the production `_lib/kg-sync-debounce.sh` directly and
drives `_kg_debounce_schedule` / `_kg_debounce_reap_stale` with a sync
command that just appends a marker to a log file (no Weaviate, no venv).
The .ps1 sibling is driven end-to-end the same way when pwsh exists.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "templates" / "hooks" / "_lib" / "kg-sync-debounce.sh"
LIB_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "kg-sync-debounce.ps1"

# Keep in step with the constants in the lib (and the .ps1 sibling).
CEILING = 64
THROTTLE = 30


def _has_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = [
    pytest.mark.skipif(platform.system() == "Windows", reason="bash lib; .ps1 mirrors"),
    pytest.mark.skipif(not _has_bash(), reason="bash not on PATH"),
]


def _run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _wait_for(path: Path, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


@pytest.fixture
def proj(tmp_path: Path):
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    return root


def _pending_dir(proj: Path) -> Path:
    return proj / ".claude" / "state" / "kg_sync_pending"


def test_cap_fallthrough_runs_immediate_never_drops(proj: Path, tmp_path: Path):
    """Above the lock ceiling, a schedule call syncs IMMEDIATELY (with a
    huge window so a scheduled sleeper would never fire in test time) and
    does NOT create a new sleeper lock for the edited file."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    # Saturate with CEILING bogus lock dirs.
    for i in range(CEILING):
        (pend / f"bogus_{i}.lock").mkdir()

    out = tmp_path / "cap.log"
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        # window=999 → a *scheduled* sleeper could not flush within the
        # test window; only the cap's immediate path can produce output.
        VCO_KG_SYNC_DEBOUNCE_SECONDS=999 _kg_debounce_schedule \\
            "{proj}" "{proj}/file2.md" "$(command -v python3)" \\
            "{proj}" "echo CAPSYNC >> {out}" "kg"
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr

    assert _wait_for(out), "cap fall-through must run the sync immediately (never dropped)"
    assert out.read_text().count("CAPSYNC") == 1

    # No sleeper lock for file2 was created — the cap bypassed scheduling.
    key = subprocess.run(
        ["python3", "-c", "import hashlib,sys;print(hashlib.md5(sys.argv[1].encode()).hexdigest())",
         f"{proj}/file2.md"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert not (pend / f"kg_{key}.lock").exists(), "cap must not schedule a sleeper lock"


def test_reaper_throttled_within_window_then_recovers(proj: Path, tmp_path: Path):
    """A reap inside the throttle window is a no-op; once the stamp ages
    past the window the backdated orphan is recovered (NEVER-DROP, only
    delayed)."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    reaplog = tmp_path / "reap.log"

    # Stamp = now (fresh) → the next reap should be throttled. Plant an old
    # orphan that WOULD be reaped if the throttle didn't block.
    script_throttled = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        NOW=$(date +%s)
        printf '%s' "$NOW" > "{pend}/.last_reap.ts"
        mkdir -p "{pend}/orphan.lock"
        printf '%s\\t%s\\n' "{proj}" "echo REAPED >> {reaplog}" > "{pend}/orphan.lock/cmd"
        # Backdate the orphan well past grace so age alone would trip it.
        touch -d '2000-01-01' "{pend}/orphan.lock" 2>/dev/null \\
            || touch -t 200001010000 "{pend}/orphan.lock"
        _kg_debounce_reap_stale "{proj}"
        """
    )
    res = _run(script_throttled, cwd=proj)
    assert res.returncode == 0, res.stderr
    time.sleep(1.0)  # give any (erroneously-spawned) reaper a chance to run
    assert not reaplog.exists(), "reaper must be throttled within the window (orphan untouched)"

    # Age the stamp past the throttle window → the next reap recovers it.
    script_recover = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        NOW=$(date +%s)
        printf '%s' "$((NOW - {THROTTLE} - 5))" > "{pend}/.last_reap.ts"
        _kg_debounce_reap_stale "{proj}"
        """
    )
    res = _run(script_recover, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert _wait_for(reaplog), "orphan must be recovered once the throttle window expires"
    assert reaplog.read_text().count("REAPED") == 1, "exactly-once recovery"


def test_normal_schedule_flushes_once(proj: Path, tmp_path: Path):
    """Positive control: a single edit with a short window flushes exactly
    once via the (detached, Item 1) flusher — proves the hardening pass
    didn't break the common path."""
    out = tmp_path / "norm.log"
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        VCO_KG_SYNC_DEBOUNCE_SECONDS=1 _kg_debounce_schedule \\
            "{proj}" "{proj}/file1.md" "$(command -v python3)" \\
            "{proj}" "echo SYNCED >> {out}" "kg"
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert _wait_for(out, timeout=8.0), "normal debounce must flush once"
    assert out.read_text().count("SYNCED") == 1


# ─── v0.2.103 single-flight (field defect 2026-10-14) ─────────────────────
#
# The defect: while a sync of a path RAN (lock renamed to ".claimed.<pid>"
# at sync START), every re-edit of that path found the ".lock" absent,
# won the mkdir gate and spawned its OWN flusher → one concurrent sync per
# re-edit. These tests reproduce that (red on the pre-fix lib) and pin the
# fix: peak concurrency 1, exactly one follow-up, latest content, and
# independent behaviour across different paths.


def _schedule(proj: Path, fpath: str, cmd: str, window: int = 1,
              chan: str = "kg") -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        VCO_KG_SYNC_DEBOUNCE_SECONDS={window} _kg_debounce_schedule \\
            "{proj}" "{fpath}" "$(command -v python3)" \\
            "{proj}" "{cmd}" "{chan}"
        """
    )
    return _run(script, cwd=proj)


def _wait_for_line(path: Path, needle: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and needle in path.read_text():
            return True
        time.sleep(0.1)
    return path.exists() and needle in path.read_text()


def _wait_quiesce(pend: Path, timeout: float = 25.0) -> None:
    """Wait until no pending lock/claim/reaping DIR is left."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pend.exists() or not any(p.is_dir() for p in pend.iterdir()):
            return
        time.sleep(0.2)


def _max_concurrency(log: Path) -> tuple:
    """(peak concurrent syncs, total syncs) from a START/END event log."""
    running = peak = starts = 0
    for line in log.read_text().splitlines():
        if line == "START":
            running += 1
            starts += 1
            if running > peak:
                peak = running
        elif line == "END":
            running -= 1
    return peak, starts


def test_singleflight_reedit_during_sync_never_runs_concurrently(proj: Path, tmp_path: Path):
    """THE defect repro: N re-edits of one path while its sync runs must
    NOT start N concurrent syncs — exactly one follow-up runs after the
    first finishes (peak concurrency 1, 2 syncs total)."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "sf.log"
    fpath = f"{proj}/docs/page.md"
    cmd = f"echo START >> {out}; sleep 4; echo END >> {out}"

    res = _schedule(proj, fpath, cmd, window=1)
    assert res.returncode == 0, res.stderr
    assert _wait_for_line(out, "START"), "first sync must start"

    # Three re-edits of the SAME path while sync #1 runs (it sleeps 4s).
    # Pre-fix each of these spawned its own flusher → its own concurrent
    # sync (the 2026-10-14 field report: 4 syncs within ~45 s).
    for _ in range(3):
        r = _schedule(proj, fpath, cmd, window=1)
        assert r.returncode == 0, r.stderr
        time.sleep(0.8)

    _wait_quiesce(pend, timeout=30.0)
    # Let any (erroneously) still-running sync append its END so the count
    # is complete, then re-read.
    time.sleep(1.0)
    peak, starts = _max_concurrency(out)
    assert starts == 2, (
        f"re-edits during a sync must coalesce into exactly ONE follow-up "
        f"(2 syncs total), got {starts}"
    )
    assert peak == 1, (
        f"at most one in-flight sync per path — observed {peak} concurrent"
    )


def test_singleflight_followup_syncs_the_latest_content(proj: Path, tmp_path: Path):
    """The follow-up re-reads the file from disk: an edit that landed
    after the running sync READ the file is carried by exactly one
    follow-up sync (content never dropped, never duplicated)."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "late.log"
    doc = tmp_path / "doc.md"
    fpath = f"{proj}/docs/late.md"
    # sleep 1 → cat → sleep 2: a 3s sync whose read happens in the middle,
    # so a write at t≈2.5 lands AFTER the read but BEFORE the sync ends.
    cmd = f"sleep 1; cat {doc} >> {out}; sleep 2"

    doc.write_text("A\n")
    res = _schedule(proj, fpath, cmd, window=1)
    assert res.returncode == 0, res.stderr
    assert _wait_for_line(out, "A"), "first sync must read and record A"

    # The "edit": new content on disk + a schedule call while sync #1 is
    # still inside its trailing sleep (claim live).
    doc.write_text("B\n")
    r = _schedule(proj, fpath, cmd, window=1)
    assert r.returncode == 0, r.stderr

    _wait_quiesce(pend, timeout=30.0)
    time.sleep(1.0)
    lines = [ln.strip() for ln in out.read_text().splitlines() if ln.strip()]
    assert lines == ["A", "B"], (
        f"exactly one follow-up carrying the LATEST content: got {lines}"
    )


def test_singleflight_different_paths_stay_independent(proj: Path, tmp_path: Path):
    """Single-flight is per (path, channel): two different files sync
    concurrently — the fix must not serialise unrelated paths."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    log = tmp_path / "par.log"

    def cmd(tag: str) -> str:
        # \\$(...) — the substitution must run at SYNC time (inside the
        # flusher's eval), not at schedule time (the outer double quotes).
        return (
            f"echo {tag}-START \\$(date +%s.%N) >> {log}; "
            f"sleep 3; "
            f"echo {tag}-END \\$(date +%s.%N) >> {log}"
        )

    for tag, rel in (("A", "docs/alpha.md"), ("B", "docs/beta.md")):
        res = _schedule(proj, f"{proj}/{rel}", cmd(tag), window=1)
        assert res.returncode == 0, res.stderr

    _wait_quiesce(pend, timeout=30.0)
    time.sleep(1.0)
    spans = {}
    counts = {}
    for line in log.read_text().splitlines():
        parts = line.split()
        tag, kind = parts[0].rsplit("-", 1)   # "A-START" → ("A", "START")
        ts = float(parts[1])
        spans.setdefault(tag, {})[kind] = ts
        counts[tag] = counts.get(tag, 0) + 1
    assert counts == {"A": 2, "B": 2}, f"each path synced exactly once: {counts}"
    overlap = (min(spans["A"]["END"], spans["B"]["END"])
               - max(spans["A"]["START"], spans["B"]["START"]))
    assert overlap > 0, (
        f"independent paths must sync in parallel (overlap={overlap}s)"
    )


def test_singleflight_reaper_leaves_a_followup_whose_sync_runs(proj: Path, tmp_path: Path):
    """A follow-up ".lock" older than grace whose key has a LIVE claim is
    NOT an orphan: reaping it would start a second concurrent sync. The
    reaper must skip it (the running sync's runner serves it at sync end).
    The claim must be GENUINELY live: its stamp post-dates the owner
    process's start (a backdated claim under a live pid is the recycled
    case, covered by the pid-reuse tests below)."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    reaplog = tmp_path / "reap.log"
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        NOW=$(date +%s)
        printf '%s' "$((NOW - {THROTTLE} - 5))" > "{pend}/.last_reap.ts"
        sleep 60 &
        LIVE_PID=$!
        base="kg_deadbeef.lock"
        mkdir -p "{pend}/$base"
        printf '%s\\t%s\\n' "{proj}" "echo REAPED >> {reaplog}" > "{pend}/$base/cmd"
        # A LIVE in-flight claim for the same key (sync running now): the
        # sleep process started BEFORE the claim was stamped, so the
        # pid-reuse guard sees it as the claim's live owner.
        mkdir -p "{pend}/$base.claimed.$LIVE_PID"
        # Backdate only the LOCK well past grace: age alone would trip
        # Sweep A. The claim stays fresh (age < grace → Sweep B skips it).
        touch -d '2000-01-01' "{pend}/$base" 2>/dev/null \\
            || touch -t 200001010000 "{pend}/$base"
        _kg_debounce_reap_stale "{proj}"
        if [ -d "{pend}/$base" ]; then echo LOCK_PRESENT; else echo LOCK_GONE; fi
        kill "$LIVE_PID" 2>/dev/null
        wait "$LIVE_PID" 2>/dev/null
        exit 0
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert "LOCK_PRESENT" in res.stdout, (
        "reaper must not claim a follow-up lock whose sync still runs"
    )
    time.sleep(1.0)  # catch any erroneously-spawned reaper runner
    assert not reaplog.exists(), "no second sync may be started by the reaper"


# ─── v0.2.103 PID-reuse guard (HIGH-3, review R3 2026-10-14) ────────────────
#
# A claim left by a DEAD sync whose pid the OS later handed to an unrelated
# live process used to look "alive" forever (kill -0 / Get-Process -Id only
# answer "does SOME process have this pid?"): every later edit of that file
# was parked as a follow-up nothing served, and the reaper skipped it. The
# guard: a pid is the claim's live owner only if the process ALSO started
# at/before the claim's stamp.


def test_pid_reuse_recycled_claim_is_not_inflight(proj: Path, tmp_path: Path):
    """Unit-level, deterministic: a claim stamped long ago whose pid now
    belongs to a process that started AFTER the stamp (recycled) is NOT in
    flight; the same pid with a FRESH stamp (genuine owner) IS in flight."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        sleep 60 &
        LIVE_PID=$!
        base="kg_deadbeef.lock"
        claim="{pend}/$base.claimed.$LIVE_PID"
        # RECYCLED: pid answers kill -0, but the process started ~now while
        # the claim says the sync began years ago.
        mkdir -p "$claim"
        touch -d '2000-01-01' "$claim" 2>/dev/null || touch -t 200001010000 "$claim"
        if _kg_debounce_inflight "{pend}" "$base"; then echo RECYCLED_INFLIGHT; else echo RECYCLED_CLEAR; fi
        rm -rf "$claim"
        # GENUINE: claim stamped now → the pre-dating process owns it.
        mkdir -p "$claim"
        if _kg_debounce_inflight "{pend}" "$base"; then echo LIVE_INFLIGHT; else echo LIVE_CLEAR; fi
        kill "$LIVE_PID" 2>/dev/null
        wait "$LIVE_PID" 2>/dev/null
        exit 0
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert "RECYCLED_CLEAR" in res.stdout, (
        "a claim whose pid was recycled by a later-starting process must "
        "not be treated as an in-flight sync"
    )
    assert "LIVE_INFLIGHT" in res.stdout, (
        "a claim stamped after its owner process started is genuinely live"
    )


def test_pid_reuse_recycled_claim_does_not_freeze_later_edits(proj: Path, tmp_path: Path):
    """THE freeze repro (behavioral): with a recycled-pid claim parked in
    the state dir, a fresh edit of that file must still be SERVED — the
    ordinary schedule path, or Sweep B recovery + follow-up serve — never
    parked forever behind the bogus claim."""
    import hashlib
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "reuse.log"
    fpath = f"{proj}/docs/reuse.md"
    key = hashlib.md5(fpath.encode()).hexdigest()
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        sleep 60 &
        LIVE_PID=$!
        base="kg_{key}.lock"
        claim="{pend}/$base.claimed.$LIVE_PID"
        mkdir -p "$claim"
        printf '%s\\t%s\\n' "{proj}" "echo PARKED >> {out}" > "$claim/cmd"
        touch -d '2000-01-01' "$claim" 2>/dev/null || touch -t 200001010000 "$claim"
        VCO_KG_SYNC_DEBOUNCE_SECONDS=1 _kg_debounce_schedule \\
            "{proj}" "{fpath}" "$(command -v python3)" \\
            "{proj}" "echo FRESH >> {out}" "kg"
        kill "$LIVE_PID" 2>/dev/null
        wait "$LIVE_PID" 2>/dev/null
        exit 0
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert _wait_for_line(out, "FRESH", timeout=20.0), (
        "a recycled-pid claim must not freeze the file: the edit is served"
    )


def test_pid_reuse_genuinely_live_claim_still_parks(proj: Path, tmp_path: Path):
    """Counterweight: a claim whose owner process genuinely pre-dates the
    stamp (a real running sync) must still park the edit as a follow-up —
    the pid-reuse guard must not loosen genuine single-flight."""
    import hashlib
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "park.log"
    fpath = f"{proj}/docs/park.md"
    key = hashlib.md5(fpath.encode()).hexdigest()
    script = textwrap.dedent(
        f"""
        set -u
        . "{LIB}"
        sleep 60 &
        LIVE_PID=$!
        base="kg_{key}.lock"
        claim="{pend}/$base.claimed.$LIVE_PID"
        # GENUINE live claim: the sleep started BEFORE this stamp.
        mkdir -p "$claim"
        printf '%s\\t%s\\n' "{proj}" "echo STALE >> {out}" > "$claim/cmd"
        VCO_KG_SYNC_DEBOUNCE_SECONDS=1 _kg_debounce_schedule \\
            "{proj}" "{fpath}" "$(command -v python3)" \\
            "{proj}" "echo FRESH >> {out}" "kg"
        sleep 3
        if [ -d "{pend}/$base" ]; then echo FOLLOWUP_PARKED; else echo FOLLOWUP_ABSENT; fi
        kill "$LIVE_PID" 2>/dev/null
        wait "$LIVE_PID" 2>/dev/null
        exit 0
        """
    )
    res = _run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    # Window is 1s, so an ordinary schedule would have flushed by ~1-2s;
    # 3s of silence + the follow-up lock present = parked behind the live
    # claim (the running sync's runner serves it at sync end).
    assert "FOLLOWUP_PARKED" in res.stdout, (
        "a genuinely live claim must park the edit as a follow-up lock"
    )
    assert not out.exists() or "FRESH" not in out.read_text(), (
        "no sync may start while the genuine claim is live"
    )


# ─── v0.2.103 single-flight — .ps1 sibling end-to-end ──────────────────────
#
# The .ps1 lib carries the identical semantics (MUST MATCH). When pwsh is
# present we drive it end-to-end the same way as the .sh: schedule a slow
# sync, re-edit the same path while it runs, assert peak concurrency 1 and
# exactly one follow-up.


def _has_pwsh() -> bool:
    return shutil.which("pwsh") is not None


@pytest.mark.skipif(not _has_pwsh(), reason="pwsh not on PATH")
def test_ps1_singleflight_reedit_during_sync_never_runs_concurrently(proj: Path, tmp_path: Path):
    from tests.common.pwsh import PWSH_BASE_FLAGS
    exe = shutil.which("pwsh")
    assert exe is not None  # guaranteed by the skipif; narrows for pyright
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "sfps.log"
    fpath = f"{proj}/docs/page.md"
    # In PowerShell `echo` is Write-Output, `sleep` is Start-Sleep, and
    # `>>` appends — the same fake-slow-sync marker shape as the .sh test.
    # The fake sync sleeps 8s (vs 4s in the .sh test) because each pwsh
    # invocation costs ~1.5-2s of startup: the three re-edit schedule
    # calls must all land while sync #1 still runs.
    cmd = f"echo START >> '{out}'; sleep 8; echo END >> '{out}'"

    def schedule() -> subprocess.CompletedProcess:
        # The -Command value is a PS DOUBLE-quoted string: single quotes in
        # the cmd need no escaping there (only " ` $ would, and the fake
        # sync cmd carries none).
        script = (
            f". '{LIB_PS1}'\n"
            f"$env:VCO_KG_SYNC_DEBOUNCE_SECONDS = '1'\n"
            f"Invoke-KgDebounceSchedule -ProjectRoot '{proj}' "
            f"-FilePath '{fpath}' -WorkingDir '{proj}' "
            f"-Command \"{cmd}\" -Channel 'kg'\n"
        )
        return subprocess.run(
            [exe, *PWSH_BASE_FLAGS, "-Command", script],
            cwd=str(proj),
            # NOT capture_output: Start-VcoDetachedProcess redirects nothing
            # on POSIX, so the detached flusher child INHERITS this pipe and
            # a captured call would block until the whole sync finishes —
            # exactly what the single-flight test must not do. /dev/null
            # releases the parent immediately.
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=90,
        )

    res = schedule()
    assert res.returncode == 0, res.stderr
    assert _wait_for_line(out, "START"), "first sync must start"

    for _ in range(3):
        r = schedule()
        assert r.returncode == 0, r.stderr
        time.sleep(0.8)

    _wait_quiesce(pend, timeout=45.0)
    time.sleep(1.0)
    peak, starts = _max_concurrency(out)
    assert starts == 2, (
        f"[ps1] re-edits during a sync must coalesce into exactly ONE "
        f"follow-up (2 syncs total), got {starts}"
    )
    assert peak == 1, (
        f"[ps1] at most one in-flight sync per path — observed {peak} concurrent"
    )


def _ps1_run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    exe = shutil.which("pwsh")
    assert exe is not None  # guaranteed by the callers' skipif; narrows for pyright
    from tests.common.pwsh import PWSH_BASE_FLAGS
    return subprocess.run(
        [exe, *PWSH_BASE_FLAGS, "-Command", script],
        cwd=str(cwd),
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.skipif(not _has_pwsh(), reason="pwsh not on PATH")
def test_ps1_pid_reuse_recycled_claim_is_not_inflight(proj: Path, tmp_path: Path):
    """[ps1] PID-reuse guard (HIGH-3): a claim stamped long ago whose pid
    now belongs to a process that started AFTER the stamp (recycled) is
    NOT in flight; the same pid under a FRESH stamp (genuine owner) IS."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    script = (
        f". '{LIB_PS1}'\n"
        f"$live = Start-Process -FilePath 'sleep' -ArgumentList '60' -PassThru\n"
        f"$dir = '{pend}'\n"
        f"$base = 'kg_deadbeef.lock'\n"
        f"$c = Join-Path $dir ($base + '.claimed.' + $live.Id)\n"
        f"New-Item -ItemType Directory -Path $c -Force | Out-Null\n"
        f"(Get-Item -LiteralPath $c).LastWriteTime = (Get-Date).AddYears(-10)\n"
        f"if (Test-KgDebounceInflight -StateDir $dir -Base $base) {{ 'RECYCLED_INFLIGHT' }} "
        f"else {{ 'RECYCLED_CLEAR' }}\n"
        f"Remove-Item -LiteralPath $c -Recurse -Force\n"
        f"New-Item -ItemType Directory -Path $c -Force | Out-Null\n"
        f"if (Test-KgDebounceInflight -StateDir $dir -Base $base) {{ 'LIVE_INFLIGHT' }} "
        f"else {{ 'LIVE_CLEAR' }}\n"
        f"Stop-Process -Id $live.Id -ErrorAction SilentlyContinue\n"
        f"exit 0\n"
    )
    res = _ps1_run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert "RECYCLED_CLEAR" in res.stdout, (
        "[ps1] a recycled pid (started after the claim stamp) must not "
        "be treated as an in-flight sync"
    )
    assert "LIVE_INFLIGHT" in res.stdout, (
        "[ps1] a claim stamped after its owner process started is live"
    )


@pytest.mark.skipif(not _has_pwsh(), reason="pwsh not on PATH")
def test_ps1_pid_reuse_recycled_claim_does_not_freeze_later_edits(proj: Path, tmp_path: Path):
    """[ps1] THE freeze repro: with a recycled-pid claim parked in the
    state dir, a fresh edit must still be SERVED, never parked forever."""
    import hashlib
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "reuseps.log"
    fpath = f"{proj}/docs/reuseps.md"
    key = hashlib.md5(fpath.encode()).hexdigest()
    script = (
        f". '{LIB_PS1}'\n"
        f"$live = Start-Process -FilePath 'sleep' -ArgumentList '60' -PassThru\n"
        f"$dir = '{pend}'\n"
        f"$base = 'kg_{key}.lock'\n"
        f"$c = Join-Path $dir ($base + '.claimed.' + $live.Id)\n"
        f"New-Item -ItemType Directory -Path $c -Force | Out-Null\n"
        f"Set-Content -LiteralPath (Join-Path $c 'cmd') "
        f"-Value (\"{proj}`t\" + \"echo PARKED >> '{out}'\")\n"
        f"(Get-Item -LiteralPath $c).LastWriteTime = (Get-Date).AddYears(-10)\n"
        f"$env:VCO_KG_SYNC_DEBOUNCE_SECONDS = '1'\n"
        f"Invoke-KgDebounceSchedule -ProjectRoot '{proj}' -FilePath '{fpath}' "
        f"-WorkingDir '{proj}' -Command \"echo FRESH >> '{out}'\" -Channel 'kg'\n"
        f"Stop-Process -Id $live.Id -ErrorAction SilentlyContinue\n"
        f"exit 0\n"
    )
    res = _ps1_run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert _wait_for_line(out, "FRESH", timeout=30.0), (
        "[ps1] a recycled-pid claim must not freeze the file"
    )


@pytest.mark.skipif(not _has_pwsh(), reason="pwsh not on PATH")
def test_ps1_pid_reuse_genuinely_live_claim_still_parks(proj: Path, tmp_path: Path):
    """[ps1] Counterweight: a genuinely live claim (owner pre-dates the
    stamp) must still park the edit as a follow-up — single-flight must
    not be loosened by the pid-reuse guard."""
    import hashlib
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "parkps.log"
    fpath = f"{proj}/docs/parkps.md"
    key = hashlib.md5(fpath.encode()).hexdigest()
    script = (
        f". '{LIB_PS1}'\n"
        f"$live = Start-Process -FilePath 'sleep' -ArgumentList '60' -PassThru\n"
        f"$dir = '{pend}'\n"
        f"$base = 'kg_{key}.lock'\n"
        f"$c = Join-Path $dir ($base + '.claimed.' + $live.Id)\n"
        f"# GENUINE live claim: the sleep started BEFORE this stamp.\n"
        f"New-Item -ItemType Directory -Path $c -Force | Out-Null\n"
        f"$env:VCO_KG_SYNC_DEBOUNCE_SECONDS = '1'\n"
        f"Invoke-KgDebounceSchedule -ProjectRoot '{proj}' -FilePath '{fpath}' "
        f"-WorkingDir '{proj}' -Command \"echo FRESH >> '{out}'\" -Channel 'kg'\n"
        f"Start-Sleep -Seconds 3\n"
        f"if (Test-Path -LiteralPath (Join-Path $dir $base) -PathType Container) "
        f"{{ 'FOLLOWUP_PARKED' }} else {{ 'FOLLOWUP_ABSENT' }}\n"
        f"Stop-Process -Id $live.Id -ErrorAction SilentlyContinue\n"
        f"exit 0\n"
    )
    res = _ps1_run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    assert "FOLLOWUP_PARKED" in res.stdout, (
        "[ps1] a genuinely live claim must park the edit as a follow-up lock"
    )
    assert not out.exists() or "FRESH" not in out.read_text(), (
        "[ps1] no sync may start while the genuine claim is live"
    )


@pytest.mark.skipif(not _has_pwsh(), reason="pwsh not on PATH")
def test_ps1_run_won_serves_followup_when_lib_unreadable(proj: Path, tmp_path: Path):
    """[ps1] LOW-3 (review R3): the follow-up serve at sync end must not
    depend on the lib file being readable — the .sh sibling falls back to
    an in-process flusher, and so must the generated run-won child. We
    generate the REAL fragment, rewrite its embedded lib path to a
    nonexistent file (simulating an unreadable lib at child runtime) and
    execute it: the won dir's cmd runs, and the parked follow-up lock is
    then served by the self-contained fallback flusher."""
    pend = _pending_dir(proj)
    pend.mkdir(parents=True)
    out = tmp_path / "fb.log"
    base = "kg_fb.lock"
    won = pend / f"{base}.reaping.123.456"
    won.mkdir()
    (won / "cmd").write_text(f"{proj}\techo FIRST >> '{out}'")
    followup = pend / base
    followup.mkdir()
    (followup / "cmd").write_text(f"{proj}\techo SECOND >> '{out}'")

    script = (
        f". '{LIB_PS1}'\n"
        f"$frag = Get-KgDebounceRunWonFragment -WonExpr \"'{won}'\" "
        f"-StateExpr \"'{pend}'\" -BaseExpr \"'{base}'\"\n"
        f"$frag = $frag.Replace('{LIB_PS1}', '/nonexistent/kg-sync-debounce.ps1')\n"
        f"$env:VCO_KG_SYNC_DEBOUNCE_SECONDS = '1'\n"
        f"Invoke-Expression $frag\n"
        f"exit 0\n"
    )
    res = _ps1_run(script, cwd=proj)
    assert res.returncode == 0, res.stderr
    lines = [ln.strip() for ln in out.read_text().splitlines() if ln.strip()]
    assert lines == ["FIRST", "SECOND"], (
        f"[ps1] lib-unreadable run-won child must run the won cmd AND serve "
        f"the follow-up, got {lines}"
    )
