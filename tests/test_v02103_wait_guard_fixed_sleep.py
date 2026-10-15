# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.103 — a LITERAL sleep BETWEEN other actions is allowed (owner
ruling 2026-10-15).

Before this change the wait-loop guard denied ``sleep`` wherever it
appeared as a verb, so even a fixed pause between two steps of one command
(``start-x && sleep 3 && curl ...``) was blocked. The ruling:

* ALLOWED: ``sleep`` with a literal numeric duration (``sleep 3``,
  ``sleep 0.5``, ``sleep 2s``) sitting BETWEEN other segments of one
  command — never the first, the last or the only segment.
* STILL DENIED:
  (a) any sleep inside a loop body (``while``/``until``/``for``/``select``
      — including ``while ! check; do echo x; sleep 2; done``, where the
      sleep segment does not start with ``do``, and a sleep in the loop
      CONDITION, which is loop machinery too);
  (b) a non-literal duration (``sleep $N``, ``sleep infinity``, a flag,
      no argument);
  (c) the sleep as the sole or last segment — it only waits — and (per
      the ruling's "BETWEEN other actions") as the first;
  (d) everything else the guard already denied (``watch``, ``tail -f``,
      timeout-wrapped wait verbs, PS ``Wait-*``, PS loops).
* The same rule applies inside ``bash -c '...'`` / ``timeout ... bash -c``
  recursion, and to the PowerShell reading (``Start-Sleep -Seconds 2`` →
  ``sleep 2`` — wait_guard already classified ``Start-Sleep``); PS loops
  stay denied via :func:`vco_lib.powershell_command.loop_pattern`.

The structural facts the decision needs (segment position, loop-body
membership — including through ``bash -c`` recursion) come from the ONE
chain walker: :func:`vco_lib.bash_command_walk.walk_segments` and its
:data:`~vco_lib.bash_command_walk.ChainSegment`. The walker tests pin that
``walk_command``'s collector contract is byte-identical for the existing
consumers (``bash_write_targets``, ``git_commit_gate``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_DEFAULT: dict = {}


def _payload(tool: str, tool_input: dict, **extra) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": tool,
        "tool_input": tool_input,
        **extra,
    }


def _eval(tmp_path, command: str, tool: str = "Bash"):
    from vco_lib import wait_guard as wg

    return wg.evaluate(
        _payload(tool, {"command": command}),
        env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
    )


def _deny(tmp_path, command: str, tool: str = "Bash") -> str:
    reason = _eval(tmp_path, command, tool)
    assert reason, f"{command!r} must be denied"
    return reason


def _allow(tmp_path, command: str, tool: str = "Bash") -> None:
    reason = _eval(tmp_path, command, tool)
    assert reason is None, f"{command!r} must be allowed: {reason}"


# ═══════════════════════════════════════════════════════════════════════
# The allowed shape: literal sleep BETWEEN other segments
# ═══════════════════════════════════════════════════════════════════════


class TestLiteralSleepBetweenActionsAllowed:
    @pytest.mark.parametrize(
        "command",
        [
            "start-x && sleep 3 && curl -s localhost:8080/health",
            "foo; sleep 2; bar",
            "foo; sleep 0.5; bar",
            "foo; sleep 2s; bar",
            "foo; sleep 1m; bar",
            "make build && sleep 10 && make test",
            "foo; sleep 2; bar; sleep 1; baz",  # two fixed pauses
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_allowed_inside_bash_c_recursion(self, tmp_path):
        _allow(tmp_path, "bash -c 'foo; sleep 2; bar'")

    def test_allowed_inside_timeout_bash_c_recursion(self, tmp_path):
        _allow(tmp_path, "timeout 30 bash -c 'foo; sleep 2; bar'")


class TestLiteralSleepBetweenActionsAllowedPowerShell:
    """wait_guard already classifies Start-Sleep (the translation reads
    ``Start-Sleep -Seconds 2`` as ``sleep 2``), so the ruling applies to
    the PowerShell reading too."""

    @pytest.mark.parametrize(
        "command",
        [
            "Write-Output a; Start-Sleep -Seconds 2; Write-Output b",
            "Write-Output a; Start-Sleep 2; Write-Output b",
            "Get-Content f -Tail 1; Start-Sleep -Milliseconds 500; Get-Content f -Tail 1",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command, tool="PowerShell")


# ═══════════════════════════════════════════════════════════════════════
# (a) sleep inside a loop stays denied
# ═══════════════════════════════════════════════════════════════════════


class TestLoopSleepStillDenied:
    @pytest.mark.parametrize(
        "command",
        [
            # The ruling's exact example: the sleep segment does NOT start
            # with `do`.
            "while ! check; do echo x; sleep 2; done",
            "while true; do tail -n 1 /tmp/app.log; sleep 5; done",
            "until curl -s localhost:8080; do sleep 2; done",
            "for i in 1 2 3; do echo $i; sleep 0.5; done",
            "select x in a b; do sleep 1; break; done",
            # Nested loops.
            "while true; do for i in 1 2; do sleep 1; done; done",
            # Loop inside bash -c recursion.
            "bash -c 'while true; do sleep 1; done'",
            # bash -c as a LATER segment of a loop body: the walker
            # recurses and the loop context propagates, so the inner
            # between-actions sleep is denied.
            "while ! check; do echo x; bash -c 'foo; sleep 2; echo y'; done",
            # timeout-wrapped bash -c inside a loop body (the wait-guard
            # timeout recursion carries the loop context too).
            "while ! check; do echo x; timeout 5 bash -c 'foo; sleep 2; bar'; done",
            # `do bash -c …` DIRECTLY after the keyword (owner ruling
            # 2026-10-15, gap 1): the walker peels the leading compound
            # keyword BEFORE the dash-c decision, so the sub-command is
            # recursed into — with the loop context inherited.
            "while c; do bash -c 'sleep 1'; done",
            "while c; do bash -c 'foo; sleep 1; bar'; done",
            "for i in 1 2; do timeout 5 bash -c 'sleep 1'; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "sleep" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "while ($true) { Get-Content f; Start-Sleep 5 }",
            "do { Get-Content f; Start-Sleep 2 } until (Test-Path f)",
            # A wait as the loop CONDITION (PS reading): loop_pattern
            # denies any while loop, sleep or not.
            "while (sleep 1) { Write-Output x }",
        ],
    )
    def test_powershell_loops_still_denied(self, tmp_path, command):
        _deny(tmp_path, command, tool="PowerShell")


# ═══════════════════════════════════════════════════════════════════════
# (a2) a WAIT as the loop CONDITION is denied too (owner ruling 2026-10-15,
# gap 2): the condition segments of while/until count as in-loop.
# ═══════════════════════════════════════════════════════════════════════


class TestLoopConditionWaitDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "while sleep 2; do x; done",
            "until sleep 1; do echo x; done",
            # A `&&`-chained condition: every segment up to `do` is the
            # condition, hence in-loop.
            "while foo && sleep 1; do x; done",
            # The condition's bash -c recursion inherits the loop.
            "while bash -c 'sleep 1'; do x; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "sleep" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # A loop WITHOUT a wait is not the guard's business.
            "while true; do echo x; done",
            "until curl -s localhost:8080; do echo x; done",
            # A condition that is a plain check, with a legal fixed pause
            # after the loop.
            "while ! check; do echo x; done; sleep 2; echo next",
        ],
    )
    def test_no_wait_means_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


# ═══════════════════════════════════════════════════════════════════════
# (b) non-literal duration stays denied, even between actions
# ═══════════════════════════════════════════════════════════════════════


class TestNonLiteralSleepStillDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "foo; sleep $N; bar",
            "foo; sleep ${N}s; bar",
            "foo; sleep infinity; bar",
            "foo; sleep; bar",
            "foo; sleep --help; bar",
        ],
    )
    def test_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    def test_powershell_non_literal_denied(self, tmp_path):
        _deny(tmp_path, "Write-Output a; Start-Sleep -Seconds $N; Write-Output b",
              tool="PowerShell")


# ═══════════════════════════════════════════════════════════════════════
# (c) sole / last / first segment stays denied
# ═══════════════════════════════════════════════════════════════════════


class TestPositionRuleStillDenied:
    @pytest.mark.parametrize(
        "command",
        [
            "sleep 3",                       # sole segment
            "sleep 0.5",
            "foo && sleep 3",                # last: it only waits
            "foo; sleep 2",
            "cd /tmp && sleep 5",            # last (pre-existing pin)
            "sleep 60 && tail -n 40 /tmp/build.log",  # first: not BETWEEN
            "sleep 3 && foo",                # first
        ],
    )
    def test_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    def test_powershell_sole_and_last_denied(self, tmp_path):
        _deny(tmp_path, "Start-Sleep -Seconds 3", tool="PowerShell")
        _deny(tmp_path, "Write-Output a; Start-Sleep 2", tool="PowerShell")


# ═══════════════════════════════════════════════════════════════════════
# (d) everything else the guard denies is untouched
# ═══════════════════════════════════════════════════════════════════════


class TestOtherPatternsUntouched:
    @pytest.mark.parametrize(
        "command",
        [
            "watch kubectl get pods",
            "tail -f /tmp/app.log",
            "timeout 5 sleep 3",                 # timeout-wrapped wait verb
            "timeout 30 bash -c 'sleep 30'",
        ],
    )
    def test_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    def test_powershell_wait_verbs_denied(self, tmp_path):
        _deny(tmp_path, "Wait-Job -Id 1", tool="PowerShell")

    def test_quoted_sleep_text_is_not_a_verb(self, tmp_path):
        _allow(tmp_path, 'echo "sleep 5"')


# ═══════════════════════════════════════════════════════════════════════
# Walker: ChainSegment context (position + loop membership), one home
# ═══════════════════════════════════════════════════════════════════════


class TestWalkSegments:
    def _segments(self, command: str):
        from vco_lib.bash_command_walk import walk_segments

        return walk_segments(command)

    def test_position_and_count(self):
        segs = self._segments("foo; sleep 2; bar")
        assert [s.tokens[0] for s in segs] == ["foo", "sleep", "bar"]
        assert [(s.position, s.chain_size) for s in segs] == [(0, 3), (1, 3), (2, 3)]
        assert all(not s.in_loop for s in segs)

    def test_loop_body_membership(self):
        segs = self._segments("while ! check; do echo x; sleep 2; done")
        by_verb = {}
        for s in segs:
            body = s.tokens
            while body and body[0] in ("do", "then", "else", "{", "!", "time"):
                body = body[1:]
            if body:
                by_verb.setdefault(body[0], s)
        # The loop OPENER (the condition) is loop machinery too.
        assert segs[0].tokens[0] == "while" and segs[0].in_loop
        assert by_verb["echo"].in_loop
        assert by_verb["sleep"].in_loop
        # `done` closes the body: a segment AFTER it is outside.
        segs2 = self._segments("while c; do sleep 1; done; echo after")
        assert segs2[-1].tokens[0] == "echo" and not segs2[-1].in_loop

    def test_nested_loops(self):
        segs = self._segments(
            "while a; do for i in 1 2; do sleep 1; done; done; echo after"
        )
        sleep = [s for s in segs if "sleep" in s.tokens][0]
        assert sleep.in_loop
        assert not segs[-1].in_loop

    def test_loop_context_propagates_into_bash_c(self):
        # `bash -c` as a LATER segment of a loop body (no `do` prefix —
        # the walker recurses only when the peeled verb is the shell):
        # the inner chain keeps its own positions AND inherits the loop.
        segs = self._segments(
            "while ! check; do echo x; bash -c 'foo; sleep 2; echo y'; done"
        )
        inner = [s for s in segs if s.tokens and s.tokens[0] == "sleep"]
        assert len(inner) == 1 and inner[0].in_loop
        # The inner chain keeps its OWN positions (the ruling: the same
        # rule applies inside the recursion).
        assert (inner[0].position, inner[0].chain_size) == (1, 3)

    def test_bash_c_positions_are_per_chain_level(self):
        segs = self._segments("bash -c 'foo; sleep 2; bar'")
        assert [s.tokens[0] for s in segs] == ["foo", "sleep", "bar"]
        assert [(s.position, s.chain_size) for s in segs] == [(0, 3), (1, 3), (2, 3)]
        assert all(s.depth == 1 for s in segs)

    def test_do_prefixed_bash_c_recurses_with_loop_context(self):
        """Gap 1 (owner ruling 2026-10-15): the dash-c decision looks
        THROUGH leading compound keywords (do/then/else/{/!), so
        `do bash -c '…'` recurses like a bare `bash -c '…'`."""
        segs = self._segments("while c; do bash -c 'sleep 1'; done")
        assert [s.tokens for s in segs] == [["while", "c"], ["sleep", "1"], ["done"]]
        sleep = segs[1]
        assert sleep.in_loop and sleep.depth == 1
        assert (sleep.position, sleep.chain_size) == (0, 1)

    def test_condition_segments_are_in_loop(self):
        """Gap 2: every segment of a while/until CONDITION (up to `do`)
        is in-loop, chained conditions included."""
        segs = self._segments("while foo && sleep 1; do x; done")
        assert [s.tokens[0] for s in segs] == ["while", "sleep", "do", "done"]
        assert [s.in_loop for s in segs] == [True, True, True, False]

    def test_bash_without_c_still_emits_nothing_even_after_do(self):
        from vco_lib.bash_command_walk import walk_command

        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("bash script.sh", one) == []
        assert walk_command("do bash script.sh", one) == []
        assert walk_command("do bash -c 'a; b'", one) == ["a", "b"]

    def test_walk_command_contract_unchanged(self):
        """The existing consumers' collector contract is byte-identical:
        same peeled segments, same source order, bash -c recursion inline,
        collector findings flattened (one string per segment here)."""
        from vco_lib.bash_command_walk import walk_command

        one = lambda toks: ["|".join(toks)]  # noqa: E731 — one finding per segment
        got = walk_command("bash -c 'a; b' && c", one)
        assert got == ["a", "b", "c"]
        got = walk_command("bash script.sh", one)  # no -c: nothing collected
        assert got == []
        got = walk_command("cd /tmp && sudo rm -rf x | cat", one)
        assert got == ["cd|/tmp", "rm|-rf|x", "cat"]
        got = walk_command('echo "done;"', one)  # quoted tail stays data
        assert got == ["echo|done;"]
        got = walk_command("while ! check; do echo x; sleep 2; done", one)
        assert got == ["while|!|check", "do|echo|x", "sleep|2", "done"]

    def test_parse_dash_c_api_unchanged(self):
        from vco_lib.bash_command_walk import parse_dash_c

        one = lambda toks: ["|".join(toks)]  # noqa: E731
        out = parse_dash_c(["bash", "-c", "x; y"], 0, one)
        assert out == ["x", "y"]
        assert parse_dash_c(["bash", "script.sh"], 0, one) == []
