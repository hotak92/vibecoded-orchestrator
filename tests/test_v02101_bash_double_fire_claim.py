# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 N2 — one run per multi-match Bash call; a race-free seen-store.

The §C1 settings group registers the pre-bash wrapper once per matching
``if`` rule, so ONE ``cat x | grep y`` tool call spawns it TWICE,
concurrently, with the same payload.

Pre-N2 there were two check-then-write races:

* the wrapper's pairing dedupe (``find -mmin -1`` on the state file BEFORE
  either spawn wrote it) — two cold spawns both wrote state and both emitted
  a ``pre_bash`` event (an orphan RL event);
* the router's seen-store filter (key lookup, then append, unlocked) — two
  near-simultaneous completions both injected the same block.

The existing sequential test (``test_v02101_rl_continuity``
``test_multi_match_spawns_emit_one_outcome``) could not see either: run one
after the other, the second spawn always found the first one's file. These
tests run the spawns CONCURRENTLY.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

from tests.common.child_env import child_env  # noqa: E402
import time
from pathlib import Path

import pytest

from tests.test_v02101_rl_continuity import OutcomeRig
from tests.test_v02101_router_surfaces import HOOKS, _bash_payload, needs_bash, needs_pwsh

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "claude_mcp_servers" / "scripts"
ROUTER = SCRIPTS / "hook_context_router.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hook_context_router as router  # noqa: E402

MULTI_MATCH_CMD = "cat notes.md | grep -rn vco_seen_add templates/"


@pytest.fixture()
def oririg(tmp_path: Path) -> OutcomeRig:
    return OutcomeRig(tmp_path)


def _outcome_rows(rig: OutcomeRig) -> list:
    if not rig.outcome_record.exists():
        return []
    return [json.loads(ln) for ln in rig.outcome_record.read_text("utf-8").splitlines()
            if ln.strip()]


def _concurrent(rig: OutcomeRig, payload: dict, shell: str, n: int = 2) -> list:
    """Start *n* wrapper spawns at once (as the harness does for an if-group)."""
    env = rig.env()
    if shell == "ps1":
        argv = ["pwsh", "-NoProfile", "-File", str(HOOKS / "pre-bash-context-inject.ps1")]
    else:
        argv = ["bash", str(HOOKS / "pre-bash-context-inject.sh")]
    procs = [
        subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=str(rig.proj), env=env)
        for _ in range(n)
    ]
    data = json.dumps(payload)
    for p in procs:  # write both stdins before reading either: true overlap
        p.stdin.write(data)
        p.stdin.close()
    out = []
    for p in procs:
        stdout = p.stdout.read()
        stderr = p.stderr.read()
        p.wait(timeout=120)
        out.append((p.returncode, stdout, stderr))
    return out


def _assert_one_run(rig: OutcomeRig, results: list) -> None:
    assert all(rc == 0 for rc, _, _ in results), results
    time.sleep(1.5)  # let a wrongly-second backgrounded outcome child land
    assert len(rig.state_files()) == 1, rig.state_files()
    rows = _outcome_rows(rig)
    assert len(rows) == 1, (
        "concurrent multi-match spawns must emit ONE pre_bash event, got "
        f"{len(rows)}: {[r.get('task_id') for r in rows]}")
    injected = [out for _, out, _ in results if out.strip()]
    assert len(injected) <= 1, "the duplicate spawn injected a second copy"
    state_task = json.loads(rig.state_files()[0].read_text("utf-8"))["task_id"]
    assert rows[0]["task_id"] == state_task, "the event and the pairing file must join"
    # the loser exits SILENTLY: nothing on stderr from either spawn
    assert all(err.strip() == "" for _, _, err in results), results
    leftover = sorted((rig.proj / ".claude" / "state").glob("bash_intent_*"))
    assert leftover == [], f"per-spawn intent handoffs must be cleaned up: {leftover}"


@needs_bash
class TestConcurrentWrapperSpawns:
    def test_concurrent_spawns_pair_once_without_tool_use_id(self, oririg: OutcomeRig) -> None:
        payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-a", cwd=str(oririg.proj))
        _assert_one_run(oririg, _concurrent(oririg, payload, "sh"))

    def test_concurrent_spawns_pair_once_with_tool_use_id(self, oririg: OutcomeRig) -> None:
        payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-b", cwd=str(oririg.proj))
        payload["tool_use_id"] = "toolu_01N2DoubleFire"
        _assert_one_run(oririg, _concurrent(oririg, payload, "sh"))

    def test_a_rerun_with_a_new_tool_use_id_is_not_suppressed(self, oririg: OutcomeRig) -> None:
        """LEAVE-ALONE leg: the claim is per CALL — the same command issued
        again (a new tool_use_id) inside the window is a new run."""
        for tuid in ("toolu_first", "toolu_second"):
            payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-c",
                                    cwd=str(oririg.proj))
            payload["tool_use_id"] = tuid
            r = oririg.run("pre-bash-context-inject", payload)
            assert r.returncode == 0, r.stderr
        time.sleep(1.5)
        assert len(_outcome_rows(oririg)) == 2


@needs_pwsh
class TestConcurrentWrapperSpawnsPs1:
    def test_concurrent_ps1_spawns_pair_once(self, oririg: OutcomeRig) -> None:
        payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-ps", cwd=str(oririg.proj))
        payload["tool_use_id"] = "toolu_01N2Ps1"
        _assert_one_run(oririg, _concurrent(oririg, payload, "ps1"))


class TestRouterClaim:
    def _run(self, tmp_path: Path, payload: dict, intent: Path) -> subprocess.CompletedProcess:
        # child_env(): the router child imports vco_lib from THIS checkout
        # (the child-env ratchet); VCO_INJECT_PROFILE is dropped so the
        # default profile applies in both spawned runs.
        env = child_env(
            CLAUDE_PROJECT_DIR=str(tmp_path), RL_HUB_POST_DISABLED="1",
            VCO_ROUTER_KG_SCRIPT=str(tmp_path / "absent_kg.py"),
            VCO_CG_SCRIPT=str(tmp_path / "absent_cg.py"),
        )
        env.pop("VCO_INJECT_PROFILE", None)
        return subprocess.run(
            [sys.executable, str(ROUTER), "bash", "--intent-out", str(intent), "--claim"],
            input=json.dumps(payload), capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path), env=env,
        )

    def test_the_second_claimant_exits_silently_and_writes_no_intent(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / ".claude" / "state").mkdir(parents=True)
        payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-r", cwd=str(tmp_path))
        payload["tool_use_id"] = "toolu_router"
        first = self._run(tmp_path, payload, tmp_path / "i1.json")
        second = self._run(tmp_path, payload, tmp_path / "i2.json")
        assert first.returncode == 0 and second.returncode == 0
        assert (tmp_path / "i1.json").exists(), "the winner classifies as before"
        assert not (tmp_path / "i2.json").exists(), "the loser must leave no intent"
        assert second.stdout == "" and second.stderr == ""

    def test_without_claim_flag_nothing_is_claimed(self, tmp_path: Path) -> None:
        """The flag is the bash wrapper's opt-in; other callers are unchanged."""
        (tmp_path / ".claude" / "state").mkdir(parents=True)
        payload = _bash_payload(MULTI_MATCH_CMD, session="sess-n2-r2", cwd=str(tmp_path))
        router.claim_bash_call(payload, str(tmp_path))  # an existing claim…
        env = child_env(
            CLAUDE_PROJECT_DIR=str(tmp_path),
            VCO_ROUTER_KG_SCRIPT=str(tmp_path / "absent_kg.py"),
            VCO_CG_SCRIPT=str(tmp_path / "absent_cg.py"),
        )
        env.pop("VCO_INJECT_PROFILE", None)
        r = subprocess.run([sys.executable, str(ROUTER), "bash", "--intent-out",
                            str(tmp_path / "i.json")], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60, env=env,
                           cwd=str(tmp_path))
        assert r.returncode == 0
        assert (tmp_path / "i.json").exists(), "…does not gate a run without --claim"

    def test_claim_path_is_per_call_when_the_harness_names_the_call(self, tmp_path: Path) -> None:
        a = _bash_payload("cat x", session="s1")
        b = dict(a, tool_use_id="toolu_1")
        c = dict(a, tool_use_id="toolu_2")
        paths = {router.bash_claim_path(p, str(tmp_path)) for p in (a, b, c)}
        assert len(paths) == 3
        evil = dict(a, tool_use_id="../../etc/passwd")
        assert "/" not in Path(router.bash_claim_path(evil, str(tmp_path))).name
        assert router.bash_claim_path(_bash_payload(""), str(tmp_path)) == ""

    def test_an_unwritable_state_dir_fails_open(self, tmp_path: Path) -> None:
        """claim_once → None (cannot decide) proceeds: the pre-N2 behaviour."""
        blocker = tmp_path / ".claude"
        blocker.write_text("not a directory", encoding="utf-8")
        payload = _bash_payload("cat x", session="s1")
        assert router.claim_bash_call(payload, str(tmp_path)) is True
        assert router.claim_bash_call(payload, str(tmp_path)) is True


class TestClaimOnce:
    def test_first_wins_second_loses_stale_is_taken_over(self, tmp_path: Path) -> None:
        from vco_lib.atomic import claim_once

        path = tmp_path / "state" / "c"
        assert claim_once(path, stale_after_s=60) is True
        assert claim_once(path, stale_after_s=60) is False
        old = time.time() - 120
        os.utime(path, (old, old))
        assert claim_once(path, stale_after_s=60) is True, "a leftover is taken over"
        assert claim_once(path, stale_after_s=60) is False

    def test_racing_processes_produce_exactly_one_winner(self, tmp_path: Path) -> None:
        script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from pathlib import Path
            from vco_lib.atomic import claim_once
            while time.time() < float(sys.argv[2]):
                pass
            print(claim_once(Path(sys.argv[1]), stale_after_s=60))
        """)
        target = tmp_path / "race" / "claim"
        go = str(time.time() + 1.0)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(target), go],
                                  stdout=subprocess.PIPE, text=True,
                                  env=child_env()) for _ in range(8)]
        verdicts = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert verdicts.count("True") == 1, verdicts
        assert verdicts.count("False") == 7, verdicts


# --- seen-store: the read-check-append is one critical section ---------------

_SLOW_FILTER = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {repo!r}); sys.path.insert(0, {scripts!r})
    import hook_context_router as r
    _orig = r._seen_lines
    def _slow(path):
        lines = _orig(path)
        time.sleep(0.4)  # widen the check-then-write window
        return lines
    r._seen_lines = _slow
    if sys.argv[3] == "unlocked":
        r.filter_seen_blocks = r._filter_seen_blocks_unlocked
    while time.time() < float(sys.argv[2]):
        pass
    out = r.filter_seen_blocks("KG: Same Block | concept\\nbody line\\n", sys.argv[1], "", "")
    print("INJECTED" if out.strip() else "SUPPRESSED")
""")


def _race_filter(tmp_path: Path, mode: str) -> list:
    inject = tmp_path / f"seen_inject_{mode}.txt"
    script = _SLOW_FILTER.format(repo=str(REPO_ROOT), scripts=str(SCRIPTS))
    go = str(time.time() + 1.0)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(inject), go, mode],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=child_env())
             for _ in range(2)]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=60)
        assert p.returncode == 0, e
        outs.append(o.strip())
    return outs


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX-only (documented)")
class TestSeenStoreLock:
    def test_near_simultaneous_completions_inject_a_block_once(self, tmp_path: Path) -> None:
        assert sorted(_race_filter(tmp_path, "locked")) == ["INJECTED", "SUPPRESSED"]

    def test_red_proof_the_unlocked_filter_double_injects(self, tmp_path: Path) -> None:
        """The same race with the pre-N2 unlocked body: both inject."""
        assert _race_filter(tmp_path, "unlocked") == ["INJECTED", "INJECTED"]

    def test_lock_contention_injects_nothing_and_records_nothing(self, tmp_path: Path) -> None:
        """Fail mode on timeout: contention IS the double-inject condition,
        so the waiter stays silent (and leaves the key unrecorded)."""
        from vco_lib.atomic import exclusive_file_lock

        inject = tmp_path / "seen_inject_s.txt"
        inject.write_text("", encoding="utf-8")
        with exclusive_file_lock(Path(str(inject) + ".lock")):
            out = router.filter_seen_blocks("KG: B | concept\nbody\n", str(inject), "", "")
        assert out == ""
        assert inject.read_text(encoding="utf-8") == ""
        # …and the next uncontended call injects it normally.
        assert router.filter_seen_blocks("KG: B | concept\nbody\n", str(inject), "", "")

    def test_an_unlockable_store_falls_back_to_the_unlocked_filter(self, tmp_path: Path) -> None:
        blocker = tmp_path / "f"
        blocker.write_text("x", encoding="utf-8")
        inject = blocker / "seen_inject_s.txt"  # parent is a FILE → no lock file
        out = router.filter_seen_blocks("KG: B | concept\nbody\n", str(inject), "", "")
        assert "KG: B" in out
