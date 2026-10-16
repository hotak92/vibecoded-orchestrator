# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — the wait-guard repeat detector counts ONE TOOL CALL once.

The defect (verified in the field, state file
``~/.vct/wait_guard/wait_<session>__main.json``): the settings template
registers the guard 11 times for Bash, each registration with its own
``if`` filter, and Claude Code evaluates ``if`` against each subcommand —
so ONE compound command (``cat a b; grep x | head -60``) runs the hook
once per matching registration and :func:`vco_lib.wait_guard._repeat_check`
appended a stamp EVERY time (one call → 4 stamps within 48 ms → denied as
"4th identical status check" on its FIRST run; a heredoc → 9 stamps in
128 ms).

The fix pinned here:

* **Dedupe by ``tool_use_id``** (present in the PreToolUse payload): one
  tool call counts ONCE per repeat key, however many registrations fire
  for it. The counted ids are stored per key in the state file, pruned
  with the window.
* **Locked read-modify-write**: the registrations fire CONCURRENTLY, so
  the state update runs under the shared cross-OS lock
  (:func:`vco_lib.atomic.portable_exclusive_lock`).
* **No ``tool_use_id`` → sub-second collapse**: two stamps of the same
  key closer than ~1 s are one call's fan-out, not two polls (real
  polling is never sub-second — every poll is a model round-trip).
* **What the guard is FOR is unchanged**: N DISTINCT calls of the same
  check within the window are still denied at the Nth, and a denied
  attempt still counts.

The concurrency tests spawn REAL parallel hook processes against a tmp
state dir — the race only exists between processes, never in-thread.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_DEFAULT: dict = {}


def _payload(tool: str, tool_input: dict, **extra) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": tool,
        "tool_input": tool_input,
        **extra,
    }


def _bash(command: str, **extra) -> dict:
    return _payload("Bash", {"command": command}, **extra)


def _state_file(state_root: Path, session: str = "sess-1") -> Path:
    return state_root / f"wait_{session}__main.json"


def _entry(state_root: Path, key_substr: str, session: str = "sess-1") -> dict:
    data = json.loads(_state_file(state_root, session).read_text("utf-8"))
    for key, value in data["checks"].items():
        if key_substr in key:
            return value
    raise AssertionError(f"no check entry containing {key_substr!r}: {data}")


# ═══════════════════════════════════════════════════════════════════════
# Dedupe by tool_use_id
# ═══════════════════════════════════════════════════════════════════════


class TestToolUseIdDedupe:
    def test_same_id_fed_n_times_never_denies(self, tmp_path):
        """ONE call run through N registrations (the defect's shape):
        the same payload with the same tool_use_id 6 times — more than
        the deny threshold of 4 — must never deny."""
        from vco_lib import wait_guard as wg

        payload = _bash("cat a.log; grep -rn x . | head -60",
                        tool_use_id="toolu_same")
        for i in range(6):
            reason = wg.evaluate(payload, env=dict(ENV_DEFAULT),
                                 state_root=tmp_path, now=1000.0 + i * 0.01)
            assert reason is None, (
                f"fire {i + 1} of ONE tool call denied: {reason}"
            )

    def test_same_id_records_one_stamp_and_the_id(self, tmp_path):
        from vco_lib import wait_guard as wg

        payload = _bash("cat /tmp/app.log", tool_use_id="toolu_once")
        for i in range(3):
            assert wg.evaluate(payload, env=dict(ENV_DEFAULT),
                               state_root=tmp_path, now=1000.0 + i * 0.01) is None
        entry = _entry(tmp_path, "cat /tmp/app.log")
        assert entry["stamps"] == [1000.0], (
            f"one tool call must count ONE stamp, got {entry['stamps']}"
        )
        assert entry["ids"] == {"toolu_once": 1000.0}

    def test_distinct_ids_deny_at_the_threshold(self, tmp_path):
        """What the guard is FOR: N DISTINCT tool calls of the same check
        within the window are denied at the Nth, exactly as before."""
        from vco_lib import wait_guard as wg

        cmd = "tail -n 20 /tmp/app.log"
        for n, t in ((1, 1000.0), (2, 1002.0), (3, 1004.0)):
            assert wg.evaluate(
                _bash(cmd, tool_use_id=f"toolu_{n}"),
                env=dict(ENV_DEFAULT), state_root=tmp_path, now=t,
            ) is None, f"distinct call {n} must be allowed"
        reason = wg.evaluate(
            _bash(cmd, tool_use_id="toolu_4"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1006.0,
        )
        assert reason, "the 4th DISTINCT identical check must be denied"
        assert "4th" in reason

    def test_a_denied_id_is_not_counted_twice(self, tmp_path):
        """The deny still counts (unchanged), but the fan-out of that
        denied call must not pile on more stamps: re-fires of the denied
        call's id allow, and the NEXT distinct id denies as the 5th."""
        from vco_lib import wait_guard as wg

        cmd = "tail -n 20 /tmp/app.log"
        for n in (1, 2, 3, 4):
            wg.evaluate(_bash(cmd, tool_use_id=f"toolu_{n}"),
                        env=dict(ENV_DEFAULT), state_root=tmp_path,
                        now=1000.0 + 2 * n)
        for i in range(3):
            assert wg.evaluate(_bash(cmd, tool_use_id="toolu_4"),
                               env=dict(ENV_DEFAULT), state_root=tmp_path,
                               now=1010.0 + i * 0.01) is None, (
                "the denied call's own fan-out must not add stamps"
            )
        entry = _entry(tmp_path, cmd)
        assert len(entry["stamps"]) == 4, entry
        reason = wg.evaluate(_bash(cmd, tool_use_id="toolu_5"),
                             env=dict(ENV_DEFAULT), state_root=tmp_path,
                             now=1012.0)
        assert reason and "5th" in reason, reason

    def test_dedupe_is_per_key(self, tmp_path):
        """The id set is stored PER KEY: the same id under a different
        check key (a hypothetical) is a different counter."""
        from vco_lib import wait_guard as wg

        assert wg.evaluate(_bash("cat /tmp/a.log", tool_use_id="toolu_x"),
                           env=dict(ENV_DEFAULT), state_root=tmp_path,
                           now=1000.0) is None
        assert wg.evaluate(_bash("cat /tmp/b.log", tool_use_id="toolu_x"),
                           env=dict(ENV_DEFAULT), state_root=tmp_path,
                           now=1000.5) is None
        assert len(_entry(tmp_path, "a.log")["stamps"]) == 1
        assert len(_entry(tmp_path, "b.log")["stamps"]) == 1

    def test_read_tool_dedupes_by_id_too(self, tmp_path):
        """The repeat detector is shared: a Read's fan-out (same payload,
        same id) must also count once."""
        from vco_lib import wait_guard as wg

        ti = {"file_path": "/tmp/app.log", "offset": 1, "limit": 50}
        for i in range(6):
            reason = wg.evaluate(
                _payload("Read", ti, tool_use_id="toolu_read"),
                env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0 + i * 0.01,
            )
            assert reason is None, f"Read fan-out fire {i + 1} denied: {reason}"
        entry = _entry(tmp_path, "/tmp/app.log")
        assert entry["stamps"] == [1000.0]


# ═══════════════════════════════════════════════════════════════════════
# Real field false positives (both blocked on their FIRST run by the
# double-count bug; pinned at coordinator request 2026-10-15)
# ═══════════════════════════════════════════════════════════════════════


class TestFieldFalsePositives:
    """Two REAL commands denied as the "4th/5th identical status check"
    on their first run in the field (one tool call fanned out to N
    `if`-filtered registrations, each stamping the repeat window). Each
    is driven through the PreToolUse decision with ONE tool_use_id fired
    N times — the shape of one real call — and must never deny."""

    RENDER_SH = (
        "ls -t /tmp/*/render.sh /tmp/render.sh 2>/dev/null | head -3; "
        "f=$(ls -t /tmp/*/render.sh /tmp/render.sh 2>/dev/null | head -1); "
        'cat "$f"'
    )
    REMOTE_RUNS = (
        "cat /tmp/claude-1000/x/tasks/bq37mq98e.output; "
        "d=work/pmr/remote-runs/"
        "$(ls -t work/pmr/remote-runs | grep ^2 | head -1); "
        "echo $d; ls $d $d/udp | head -30"
    )

    @pytest.mark.parametrize("command", [RENDER_SH, REMOTE_RUNS])
    def test_static_classifier_allows(self, command):
        from vco_lib.wait_guard import static_pattern

        assert static_pattern(command) is None

    @pytest.mark.parametrize("command", [RENDER_SH, REMOTE_RUNS])
    def test_one_call_fanned_out_never_denies(self, tmp_path, command):
        """ONE tool call, one tool_use_id, N registration fires (N=8,
        above the real per-verb registration count of ~6 and the deny
        threshold of 4): never denied, on any fire."""
        from vco_lib import wait_guard as wg

        payload = _bash(command, tool_use_id="toolu_field")
        for i in range(8):
            reason = wg.evaluate(payload, env=dict(ENV_DEFAULT),
                                 state_root=tmp_path, now=1000.0 + i * 0.01)
            assert reason is None, (
                f"fire {i + 1} of ONE tool call denied: {reason}"
            )


class TestWriteIsNeverAStatusCheck:
    """A command that WRITES is never a poll (field case 2026-10-15:
    ``cat > f <<'EOF' … ; sed -i …`` keyed on ``cat`` and was blocked as
    the "6th identical status check" on its FIRST run). The write test is
    the one-home write-shape predicate
    (``vco_lib.bash_write_targets.command_has_write_shape``): an output
    redirection, a heredoc, ``sed -i``, ``tee`` kin. Pure reads still
    accumulate exactly as before."""

    WRITE_HEREDOC = (
        "cat > tools/pmr/nav/restart_session.json <<'EOF'\n"
        '{"a":1}\n'
        "EOF\n"
        "; sed -i '10a x' tools/pmr/nav/README.md"
        "; python3 tools/remote/pmr_remote.py nav --list | grep restart"
        "; python3 tools/remote/pmr_remote.py nav restart_session --dry-run"
        " | tail -4"
    )

    def test_static_classifier_allows(self):
        from vco_lib.wait_guard import static_pattern

        assert static_pattern(self.WRITE_HEREDOC) is None

    def test_write_command_one_id_fired_n_times_never_denies(self, tmp_path):
        from vco_lib import wait_guard as wg

        payload = _bash(self.WRITE_HEREDOC, tool_use_id="toolu_write")
        for i in range(8):
            reason = wg.evaluate(payload, env=dict(ENV_DEFAULT),
                                 state_root=tmp_path, now=1000.0 + i * 0.01)
            assert reason is None, (
                f"fire {i + 1} of ONE WRITE call denied: {reason}"
            )

    def test_write_command_distinct_ids_still_never_denied(self, tmp_path):
        """Writes NEVER count — even N genuinely distinct calls of the
        same write command accumulate no repeat entry."""
        from vco_lib import wait_guard as wg

        for i in range(8):
            reason = wg.evaluate(
                _bash(self.WRITE_HEREDOC, tool_use_id=f"toolu_w{i}"),
                env=dict(ENV_DEFAULT), state_root=tmp_path,
                now=1000.0 + i * 10.0,
            )
            assert reason is None, f"distinct WRITE call {i} denied: {reason}"
        assert not _state_file(tmp_path).exists() or "bash:" not in (
            tmp_path / "wait_sess-1__main.json").read_text("utf-8")

    def test_plain_redirect_write_excluded_too(self, tmp_path):
        from vco_lib import wait_guard as wg

        for i in range(8):
            reason = wg.evaluate(
                _bash("cat /tmp/app.log >> /tmp/seen.log; grep x /tmp/app.log",
                      tool_use_id=f"toolu_r{i}"),
                env=dict(ENV_DEFAULT), state_root=tmp_path,
                now=1000.0 + i * 10.0,
            )
            assert reason is None, f"append-write call {i} denied: {reason}"

    def test_pure_reads_still_count(self, tmp_path):
        """What the detector is FOR: N DISTINCT pure-read checks of the
        same file are denied at the threshold, exactly as before."""
        from vco_lib import wait_guard as wg

        cmd = "cat /tmp/x.log"
        for n in (1, 2, 3):
            assert wg.evaluate(
                _bash(cmd, tool_use_id=f"toolu_p{n}"),
                env=dict(ENV_DEFAULT), state_root=tmp_path,
                now=1000.0 + n * 10.0,
            ) is None
        reason = wg.evaluate(
            _bash(cmd, tool_use_id="toolu_p4"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1040.0,
        )
        assert reason and "4th" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # R10 F4: these have NO write target (a /dev/null sink, an
            # opaque interpreter, a comment) — the old write-shape
            # predicate excluded them and a pure-read poll escaped. The
            # write-target parser sees no target, so they KEY again.
            "tail -5 log | tee /dev/null",
            'tail -n 5 log | python3 -c "print()"',
            "cat log # > x",
            "cat log > /dev/null",
        ],
    )
    def test_reads_without_a_write_target_still_key(self, command):
        from vco_lib.wait_guard import bash_repeat_key

        assert bash_repeat_key(command) is not None, command

    def test_dev_null_read_poll_denies_at_threshold(self, tmp_path):
        from vco_lib import wait_guard as wg

        cmd = "tail -5 /tmp/build.log | tee /dev/null"
        for n in (1, 2, 3):
            assert wg.evaluate(
                _bash(cmd, tool_use_id=f"toolu_v{n}"),
                env=dict(ENV_DEFAULT), state_root=tmp_path,
                now=1000.0 + n * 10.0,
            ) is None
        reason = wg.evaluate(
            _bash(cmd, tool_use_id="toolu_v4"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1040.0,
        )
        assert reason and "4th" in reason


# ═══════════════════════════════════════════════════════════════════════
# No tool_use_id → sub-second collapse (fallback)
# ═══════════════════════════════════════════════════════════════════════


class TestNoToolUseIdCollapse:
    def test_sub_second_burst_collapses_to_one_stamp(self, tmp_path):
        """A payload without tool_use_id fired by several registrations
        at once: stamps closer than ~1 s collapse — with REPEAT_MAX=2 the
        burst must NOT deny."""
        from vco_lib import wait_guard as wg

        env = {"VCO_WAIT_GUARD_REPEAT_MAX": "2"}
        payload = _bash("cat a; grep x b | head -5")
        for i, t in enumerate((1000.0, 1000.05, 1000.10, 1000.20)):
            reason = wg.evaluate(payload, env=env, state_root=tmp_path, now=t)
            assert reason is None, (
                f"burst fire {i + 1} (t={t}) denied without an id: {reason}"
            )
        assert _entry(tmp_path, "cat a")["stamps"] == [1000.0]

    def test_spaced_checks_still_accumulate_without_id(self, tmp_path):
        """The collapse must NOT weaken the guard: checks ≥1 s apart are
        real polls and still deny at the threshold."""
        from vco_lib import wait_guard as wg

        cmd = "tail -n 20 /tmp/app.log"
        for t in (1000.0, 1002.0, 1004.0):
            assert wg.evaluate(_bash(cmd), env=dict(ENV_DEFAULT),
                               state_root=tmp_path, now=t) is None
        reason = wg.evaluate(_bash(cmd), env=dict(ENV_DEFAULT),
                             state_root=tmp_path, now=1006.0)
        assert reason and "4th" in reason, reason

    def test_empty_tool_use_id_uses_the_collapse(self, tmp_path):
        from vco_lib import wait_guard as wg

        env = {"VCO_WAIT_GUARD_REPEAT_MAX": "2"}
        payload = _bash("cat /tmp/app.log", tool_use_id="")
        assert wg.evaluate(payload, env=env, state_root=tmp_path,
                           now=1000.0) is None
        assert wg.evaluate(payload, env=env, state_root=tmp_path,
                           now=1000.1) is None, (
            "an empty tool_use_id is no id: the burst must collapse"
        )
        assert _entry(tmp_path, "app.log")["stamps"] == [1000.0]


# ═══════════════════════════════════════════════════════════════════════
# State schema: pre-v0.2.103 files (bare stamp lists) still load
# ═══════════════════════════════════════════════════════════════════════


class TestStateSchemaCompat:
    def test_old_bare_stamp_list_still_counts(self, tmp_path):
        """A field state file written by v0.2.102 (``checks[key]`` is a
        bare list of floats) must load: its stamps count toward the
        threshold of the next, id-carrying call."""
        from vco_lib import wait_guard as wg

        cmd = "tail -n 20 /tmp/app.log"
        _state_file(tmp_path).write_text(
            json.dumps({
                "checks": {f"bash:{cmd}": [994.0, 996.0, 998.0]},
                "slash": {},
            }),
            encoding="utf-8",
        )
        reason = wg.evaluate(_bash(cmd, tool_use_id="toolu_new"),
                             env=dict(ENV_DEFAULT), state_root=tmp_path,
                             now=1000.0)
        assert reason and "4th" in reason, (
            f"the 3 old stamps + this call must deny as the 4th: {reason}"
        )
        entry = _entry(tmp_path, cmd)
        assert len(entry["stamps"]) == 4
        assert entry["ids"] == {"toolu_new": 1000.0}


# ═══════════════════════════════════════════════════════════════════════
# Concurrency: real parallel hook processes against a tmp state dir
# ═══════════════════════════════════════════════════════════════════════


def _spawn_batch(state_dir: Path, payload: dict, n: int):
    """Start *n* ``python -m vco_lib.wait_guard pretooluse`` processes at
    once with the SAME payload — the registration fan-out of one call."""
    from tests.common.child_env import child_env

    env = child_env()
    env["VCT_STATE_DIR"] = str(state_dir)
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "vco_lib.wait_guard", "pretooluse"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        for _ in range(n)
    ]
    data = json.dumps(payload)
    for p in procs:  # write every stdin before reading any: true overlap
        assert p.stdin is not None
        p.stdin.write(data)
        p.stdin.close()
    out = []
    for p in procs:
        assert p.stdout is not None and p.stderr is not None
        stdout = p.stdout.read()
        stderr = p.stderr.read()
        p.stdout.close()  # read to EOF: close, never leave it to the GC
        p.stderr.close()
        p.wait(timeout=120)
        out.append((p.returncode, stdout, stderr))
    return out


class TestConcurrentHookRuns:
    def test_concurrent_registrations_of_one_call_count_one_stamp(self, tmp_path):
        state_dir = tmp_path / "state"
        payload = _bash("cat a.log; grep -rn x . | head -60",
                        tool_use_id="toolu_race")
        results = _spawn_batch(state_dir, payload, 8)
        assert all(rc == 0 for rc, _, _ in results), results
        assert all(out == "" for _, out, _ in results), (
            f"the first call's fan-out must not deny: {results}"
        )
        entry = _entry(state_dir / "wait_guard", "cat a.log")
        assert len(entry["stamps"]) == 1, (
            f"8 concurrent registrations of ONE call must record ONE stamp: "
            f"{entry['stamps']}"
        )
        assert entry["ids"] == {"toolu_race": entry["stamps"][0]}

    def test_concurrent_batches_of_distinct_ids_each_count_once(self, tmp_path):
        """4 sequential batches x 3 concurrent registrations, one
        tool_use_id per batch: exactly 4 stamps total, and exactly ONE
        deny — from the batch whose stamp reaches the threshold."""
        state_dir = tmp_path / "state"
        cmd = "tail -n 20 /tmp/app.log"
        denies = 0
        for batch in (1, 2, 3, 4):
            payload = _bash(cmd, tool_use_id=f"toolu_b{batch}")
            results = _spawn_batch(state_dir, payload, 3)
            assert all(rc == 0 for rc, _, _ in results), results
            denies += sum(1 for _, out, _ in results if out.strip())
        entry = _entry(state_dir / "wait_guard", cmd)
        assert len(entry["stamps"]) == 4, (
            f"4 distinct calls x 3 registrations must record 4 stamps: "
            f"{entry['stamps']}"
        )
        assert len(entry["ids"]) == 4
        assert denies == 1, (
            f"exactly one registration of the 4th call must deny, got {denies}"
        )
