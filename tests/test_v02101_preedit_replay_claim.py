# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 pull-in ④ — the pre-edit REPLAY path claims its slot atomically.

The N2 fix locked the ROUTER's seen-store filter, but the pre-edit wrapper's
cache-replay path filters through the SHELL seen-store
(``vco_filter_seen_blocks``: ``vco_seen_has`` then ``vco_seen_add``), which is
check-then-write. Two near-simultaneous edits of the same file both replay the
same cached blob, both pass the check, and both inject the same blocks.

The fix is portable (no flock — absent on macOS): ``vco_seen_claim`` creates a
per-(session, file-hash) claim under ``set -o noclobber`` (bash's O_EXCL), the
``.ps1`` sibling ``Invoke-VcoSeenClaim`` uses ``FileMode.CreateNew``. The loser
exits silently; the winner releases once its keys are recorded.

The race tests run REAL concurrent hook processes. The check-then-append window
is microseconds wide in production, so a ``grep`` shim on ``PATH`` sleeps on the
seen-store lookup (``grep -Fxq``) to widen it deterministically: without the
claim both spawns are inside the window together (the red-proof leg runs a copy
of the hooks with the claim reverted and asserts the double inject).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_v02101_router_surfaces import HOOKS, Rig, needs_bash, needs_pwsh

SEEN_SH = HOOKS / "_lib" / "seen-store.sh"
SEEN_PS1 = HOOKS / "_lib" / "seen-store.ps1"
BLOB = "CODE: race.symbol | function | def race.py:1 | callers: (none)\n"
CLAIM_CALL = "command -v vco_seen_claim"


# --- the primitive ---------------------------------------------------------------


def _bash(snippet: str, cwd: Path) -> subprocess.CompletedProcess:
    script = f'export PY="{sys.executable}"\n. "{SEEN_SH}"\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          timeout=30, cwd=str(cwd))


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


@needs_bash
class TestBashClaimPrimitive:
    def test_first_wins_second_held_stale_taken_over(self, tmp_path: Path) -> None:
        c = tmp_path / "d" / "x.claim"
        r = _bash(f'vco_seen_claim "{c}" 15; echo "rc=$?"', tmp_path)
        assert r.stdout.strip() == "rc=0" and c.exists(), r.stderr
        r = _bash(f'vco_seen_claim "{c}" 15; echo "rc=$?"', tmp_path)
        assert r.stdout.strip() == "rc=1", "a live claim must be HELD"
        _age(c, 60)
        r = _bash(f'vco_seen_claim "{c}" 15; echo "rc=$?"', tmp_path)
        assert r.stdout.strip() == "rc=0", "a stale leftover is taken over"
        r = _bash(f'vco_seen_claim "{c}" 15; echo "rc=$?"', tmp_path)
        assert r.stdout.strip() == "rc=1"

    def test_undecidable_is_rc2(self, tmp_path: Path) -> None:
        blocker = tmp_path / "f"
        blocker.write_text("x", encoding="utf-8")
        r = _bash(f'vco_seen_claim "{blocker}/sub/x.claim" 15; echo "rc=$?"; '
                  f'vco_seen_claim "" 15; echo "rc=$?"', tmp_path)
        assert r.stdout.split() == ["rc=2", "rc=2"], r.stdout

    def test_noclobber_does_not_leak_into_the_caller(self, tmp_path: Path) -> None:
        r = _bash(f'vco_seen_claim "{tmp_path}/a.claim" 15; '
                  f'echo hi > "{tmp_path}/plain"; echo again > "{tmp_path}/plain"; '
                  f'cat "{tmp_path}/plain"', tmp_path)
        assert r.stdout.strip() == "again", r.stderr

    def test_racing_processes_produce_exactly_one_winner(self, tmp_path: Path) -> None:
        target = tmp_path / "race" / "x.claim"
        go = time.time() + 1.0
        snippet = (f'while [ "$(date +%s%N)" -lt {int(go * 1e9)} ]; do :; done\n'
                   f'vco_seen_claim "{target}" 15; echo "rc=$?"')
        script = f'export PY="{sys.executable}"\n. "{SEEN_SH}"\n{snippet}\n'
        procs = [subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE,
                                  text=True, cwd=str(tmp_path)) for _ in range(8)]
        verdicts = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert verdicts.count("rc=0") == 1, verdicts
        assert verdicts.count("rc=1") == 7, verdicts


def _pwsh(snippet: str, cwd: Path) -> subprocess.CompletedProcess:
    script = f'. "{SEEN_PS1}"\n{snippet}\n'
    return subprocess.run(["pwsh", "-NoProfile", "-Command", script],
                          capture_output=True, text=True, timeout=60, cwd=str(cwd))


@needs_pwsh
class TestPs1ClaimPrimitive:
    def test_first_wins_second_held_stale_taken_over(self, tmp_path: Path) -> None:
        c = tmp_path / "d" / "x.claim"
        call = f'Invoke-VcoSeenClaim -ClaimFile "{c}" -StaleAfterSeconds 15'
        r = _pwsh(f"{call}; {call}", tmp_path)
        assert r.stdout.split() == ["claimed", "held"], (r.stdout, r.stderr)
        _age(c, 60)
        r = _pwsh(f"{call}; {call}", tmp_path)
        assert r.stdout.split() == ["claimed", "held"], (r.stdout, r.stderr)

    def test_undecidable(self, tmp_path: Path) -> None:
        blocker = tmp_path / "f"
        blocker.write_text("x", encoding="utf-8")
        r = _pwsh(f'Invoke-VcoSeenClaim -ClaimFile "{blocker}/sub/x.claim"; '
                  f'Invoke-VcoSeenClaim -ClaimFile ""', tmp_path)
        assert r.stdout.split() == ["undecided", "undecided"], (r.stdout, r.stderr)

    def test_racing_processes_produce_exactly_one_winner(self, tmp_path: Path) -> None:
        target = tmp_path / "race" / "x.claim"
        go = time.time() + 4.0  # pwsh cold start is slow: a generous barrier
        snippet = (f'while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt {int(go * 1000)}) {{ }}\n'
                   f'Invoke-VcoSeenClaim -ClaimFile "{target}" -StaleAfterSeconds 60')
        script = f'. "{SEEN_PS1}"\n{snippet}\n'
        procs = [subprocess.Popen(["pwsh", "-NoProfile", "-Command", script],
                                  stdout=subprocess.PIPE, text=True, cwd=str(tmp_path))
                 for _ in range(4)]
        verdicts = [p.communicate(timeout=90)[0].strip() for p in procs]
        assert verdicts.count("claimed") == 1, verdicts
        assert verdicts.count("held") == 3, verdicts


# --- the hook replay path ----------------------------------------------------------


class ReplayRig(Rig):
    """A project with a FRESH replay-cache entry for one file, so the hook
    takes the replay path (no router spawn)."""

    def __init__(self, tmp_path: Path, session: str = "sess-claim-1") -> None:
        super().__init__(tmp_path)
        self.session = session
        self.target = self.proj / "mod.py"
        self.target.write_text("x = 1\n", encoding="utf-8")
        h = hashlib.md5(str(self.target).encode("utf-8")).hexdigest()
        self.cache_dir = self.proj / ".claude" / "state" / f"edit_cache_{session}"
        self.cache_dir.mkdir(parents=True)
        self.cache_file = self.cache_dir / h
        self.cache_file.write_text(BLOB, encoding="utf-8")
        self.claim = Path(str(self.cache_file) + ".claim")
        self.inject = self.proj / ".claude" / "state" / f"seen_inject_{session}.txt"

    def payload(self) -> dict:
        return {"tool_name": "Edit", "session_id": self.session, "prompt_id": "p-1",
                "cwd": str(self.proj),
                "tool_input": {"file_path": str(self.target), "old_string": "x = 1",
                               "new_string": "x = 2"}}

    def slow_grep_env(self) -> dict:
        """PATH with a grep shim that sleeps on the seen-store lookup."""
        real = shutil.which("grep")
        assert real, "grep is required"
        shim_dir = self.tmp / "shim"
        shim_dir.mkdir(exist_ok=True)
        shim = shim_dir / "grep"
        # Read FIRST, then sleep, then answer: the verdict is stale by the time
        # the caller acts on it — exactly the check-then-write window.
        shim.write_text(
            "#!/bin/sh\n"
            f'"{real}" "$@"; rc=$?\n'
            'case " $* " in *" -Fxq "*) sleep 0.8 ;; esac\n'
            "exit $rc\n", encoding="utf-8")
        shim.chmod(0o755)
        return {"PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def _concurrent_hooks(rig: ReplayRig, hooks_dir: Path, n: int, env_extra: dict) -> list:
    env = rig.env(**env_extra)
    argv = ["bash", str(hooks_dir / "pre-edit-context-inject.sh")]
    procs = [subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, cwd=str(rig.proj),
                              env=env) for _ in range(n)]
    data = json.dumps(rig.payload())
    for p in procs:  # write every stdin before reading any: true overlap
        assert p.stdin is not None
        p.stdin.write(data)
        p.stdin.close()
    out = []
    for p in procs:
        assert p.stdout is not None and p.stderr is not None
        stdout, stderr = p.stdout.read(), p.stderr.read()
        p.wait(timeout=120)
        out.append((p.returncode, stdout, stderr))
    return out


def _reverted_hooks(tmp_path: Path) -> Path:
    """A copy of the hooks with the replay claim REVERTED (the red-proof)."""
    dst = tmp_path / "hooks_noclaim"
    shutil.copytree(HOOKS, dst)
    sh = dst / "pre-edit-context-inject.sh"
    body = sh.read_text(encoding="utf-8")
    assert body.count(CLAIM_CALL) == 1, "the claim call site moved: update the red-proof"
    sh.write_text(body.replace(CLAIM_CALL, "command -v vco_seen_claim_REVERTED"),
                  encoding="utf-8")
    return dst


@pytest.fixture()
def rrig(tmp_path: Path) -> ReplayRig:
    return ReplayRig(tmp_path)


@needs_bash
class TestReplayRace:
    def test_concurrent_replays_inject_the_block_once(self, rrig: ReplayRig) -> None:
        results = _concurrent_hooks(rrig, HOOKS, 3, rrig.slow_grep_env())
        assert all(rc == 0 for rc, _, _ in results), results
        injected = [out for _, out, _ in results if out.strip()]
        assert len(injected) == 1, f"expected ONE inject, got {len(injected)}: {results}"
        assert "CODE: race.symbol" in injected[0]
        assert all(err.strip() == "" for _, _, err in results), "the loser exits SILENTLY"
        assert rrig.inject.read_text("utf-8").splitlines() == ["race.symbol"]
        assert not rrig.claim.exists(), "the winner releases its claim"

    def test_red_proof_without_the_claim_both_inject(self, rrig: ReplayRig) -> None:
        results = _concurrent_hooks(rrig, _reverted_hooks(rrig.tmp), 3,
                                    rrig.slow_grep_env())
        assert all(rc == 0 for rc, _, _ in results), results
        injected = [out for _, out, _ in results if out.strip()]
        assert len(injected) >= 2, (
            "the check-then-append filter must double-inject without the claim — "
            f"otherwise the race test above proves nothing: {results}")


@needs_bash
class TestReplayClaimDecisions:
    def test_a_live_claim_suppresses_the_replay_and_records_nothing(
        self, rrig: ReplayRig,
    ) -> None:
        rrig.claim.write_text("999999\n", encoding="utf-8")
        r = rrig.run("pre-edit-context-inject", rrig.payload())
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
        assert not rrig.inject.exists() or rrig.inject.read_text("utf-8") == ""
        assert rrig.claim.exists(), "the loser never removes the winner's claim"

    def test_a_stale_claim_is_taken_over_and_the_replay_injects(
        self, rrig: ReplayRig,
    ) -> None:
        rrig.claim.write_text("999999\n", encoding="utf-8")
        _age(rrig.claim, 120)
        r = rrig.run("pre-edit-context-inject", rrig.payload())
        assert r.returncode == 0, r.stderr
        assert "CODE: race.symbol" in r.stdout
        assert not rrig.claim.exists()

    def test_uncontended_replay_injects_then_dedupes_to_silence(
        self, rrig: ReplayRig,
    ) -> None:
        """LEAVE-ALONE leg: sequential edits behave exactly as before."""
        r1 = rrig.run("pre-edit-context-inject", rrig.payload())
        assert "CODE: race.symbol" in r1.stdout, r1.stderr
        r2 = rrig.run("pre-edit-context-inject", rrig.payload())
        assert r2.returncode == 0 and r2.stdout.strip() == ""
        assert not rrig.claim.exists()

    def test_inject_blind_session_takes_no_claim(self, tmp_path: Path) -> None:
        """An untrustworthy session id shares the 'default' cache dir: a claim
        there would let one chat silence another's replay — so none is taken."""
        rig = ReplayRig(tmp_path, session="default")
        rig.claim.write_text("999999\n", encoding="utf-8")  # would suppress if consulted
        payload = rig.payload()
        payload["session_id"] = "bad id!"  # → "default", inject-blind
        r = rig.run("pre-edit-context-inject", payload)
        assert r.returncode == 0, r.stderr
        assert "CODE: race.symbol" in r.stdout


@needs_pwsh
class TestPs1ReplayClaim:
    def test_a_live_claim_suppresses_the_ps1_replay(self, rrig: ReplayRig) -> None:
        rrig.claim.write_text("999999\n", encoding="utf-8")
        r = rrig.run("pre-edit-context-inject", rrig.payload(), shell="ps1")
        assert r.returncode == 0 and r.stdout.strip() == "", (r.stdout, r.stderr)
        assert rrig.claim.exists()

    def test_uncontended_ps1_replay_injects_and_releases(self, rrig: ReplayRig) -> None:
        r = rrig.run("pre-edit-context-inject", rrig.payload(), shell="ps1")
        assert r.returncode == 0, r.stderr
        assert "CODE: race.symbol" in r.stdout, (r.stdout, r.stderr)
        assert not rrig.claim.exists(), "the ps1 winner releases its claim"
