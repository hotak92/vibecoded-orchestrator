# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Behavioural guard for the v0.2.65 Track B debounce-hardening pass.

Covers Items 1+2 of the kg-sync-debounce hardening:

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

The test sources the production `_lib/kg-sync-debounce.sh` directly and
drives `_kg_debounce_schedule` / `_kg_debounce_reap_stale` with a sync
command that just appends a marker to a log file (no Weaviate, no venv).

Linux/macOS-only: the `.ps1` sibling carries the identical logic (verified
structurally by the hook body-parity suite + the shared "MUST MATCH"
ceiling/throttle constants); an end-to-end pwsh run isn't in budget here.
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
