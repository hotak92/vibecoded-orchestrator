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
            # Bare echo/true/: bodies are BUSY-WAIT loops now (owner ruling
            # 2026-10-15, item 6) — denied even with no sleep in them.
            "while true; do echo x; done",
            "until curl -s localhost:8080; do echo x; done",
            "while ! check; do echo x; done; sleep 2; echo next",
        ],
    )
    def test_bare_echo_body_is_a_busy_wait_now(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "busy-wait loop" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # A loop whose body does real work is not the guard's business.
            "while true; do build_step; done",
            "until curl -s localhost:8080 >/dev/null; do prepare; done",
            # A condition that is a plain check, with a legal fixed pause
            # after the loop.
            "while ! check; do process; done; sleep 2; echo next",
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
        # R6 item 3: a recursed SOLE sub-chain inherits the outer
        # segment's position (the bash -c is segment 1 of 3).
        assert (sleep.position, sleep.chain_size) == (1, 3)

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

    def test_substitution_bodies_are_opt_in(self):
        """`walk_command` consumers see NO change: substitution bodies are
        walked only when the caller opts in (the wait guard does)."""
        from vco_lib.bash_command_walk import walk_segments

        plain = walk_segments("echo $(sleep 5)")
        assert [s.tokens[0] for s in plain] == ["echo"]
        opted = walk_segments("echo $(sleep 5)", _with_substitutions=True)
        assert [s.tokens[0] for s in opted] == ["echo", "sleep"]
        body = opted[1]
        assert body.depth == 1 and (body.position, body.chain_size) == (0, 1)

    def test_substitution_extraction_non_nested_only(self):
        from vco_lib.bash_command_walk import substitution_bodies

        assert substitution_bodies(["echo", "$(sleep", "5)"]) == ["sleep 5"]
        assert substitution_bodies(["echo", "`sleep", "5`"]) == ["sleep 5"]
        assert substitution_bodies(["echo", "hello"]) == []
        # Nested parens: the INNERMOST body is extracted (where a hidden
        # wait would live).
        assert substitution_bodies(["echo", "$(echo", "$(sleep", "5))"]) == [
            "sleep 5"
        ]

    def test_strip_subshell_parens(self):
        from vco_lib.bash_command_walk import strip_subshell_parens

        assert strip_subshell_parens(["(", "sleep", "5", ")"]) == ["sleep", "5"]
        assert strip_subshell_parens(["(sleep", "5)"]) == ["sleep", "5"]
        # `$(` is a command substitution, never peeled.
        assert strip_subshell_parens(["$(sleep", "5)"]) == ["$(sleep", "5)"]
        assert strip_subshell_parens(["sleep", "5"]) == ["sleep", "5"]

    def test_mid_segment_opener_after_brace_marks_in_loop(self):
        segs = self._segments("f() { while sleep 1; do x; done; }; f")
        assert [s.in_loop for s in segs] == [True, True, False, False, False]

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


# ═══════════════════════════════════════════════════════════════════════
# R6 review fixes (GLM R6, MERGE-AFTER-FIXES; coordinator 2026-10-15)
# ═══════════════════════════════════════════════════════════════════════


class TestScanWrappedMultiFlagDashC:
    """MEDIUM (R6): `_scan_wrapped` used to test only rest[0] for
    `-c`-ness, so a preceding non-c flag defeated it. It now routes
    through the walker's `dash_c_subcommand` (one home)."""

    @pytest.mark.parametrize(
        "command",
        [
            "timeout 30 bash -e -c 'sleep 30'",
            "timeout 30 bash -u -c 'sleep 30'",
            "timeout 5 bash -l -c 'sleep 5'",
            "timeout 5 sh -e -c 'sleep 5'",
            "nohup bash -e -c 'sleep 5'",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "sleep" in reason

    def test_fixed_pause_inside_still_allowed(self, tmp_path):
        # The recursion applies the SAME rule: a literal sleep between
        # actions of the wrapped sub-chain is a fixed pause.
        _allow(tmp_path, "timeout 30 bash -e -c 'foo; sleep 2; bar'")


class TestSubshellAndGroupWaits:
    """LOW (R6): a sole wait inside a subshell/group is a sole wait."""

    @pytest.mark.parametrize(
        "command",
        [
            "( sleep 5 )",
            "(sleep 5)",
            "{ sleep 5; }",
            "( sleep 5 ) && echo done",   # first: not between
            "echo start; ( sleep 5 )",    # last
        ],
    )
    def test_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "x && ( sleep 2 ) && y",      # R6: stays allowed — a fixed
            "x && (sleep 2) && y",        # pause between actions
            "x && { sleep 2; } && y",
        ],
    )
    def test_between_actions_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


class TestCommandSubstitutionWaits:
    """LOW (R6): the walker extracts non-nested `$(...)` / backtick bodies
    (opt-in for the wait guard) and walks each as its own sub-chain."""

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(sleep 5)",
            "echo `sleep 5`",
            "echo done $(sleep 5)",
        ],
    )
    def test_sole_wait_in_substitution_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(date +%s)",
            "echo $(foo; sleep 1; bar)",  # fixed pause inside the body
        ],
    )
    def test_no_sole_wait_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


class TestXargsPerItemWaits:
    """LOW (R6): xargs runs its command PER ITEM — a loop context."""

    @pytest.mark.parametrize(
        "command",
        [
            "seq 1 5 | xargs -I{} sh -c 'sleep 1; curl x'",
            "cat list | xargs -I{} bash -c 'sleep 2'",
            "xargs -n1 sleep < list.txt",
            "xargs -n1 sleep 5 < list.txt",
        ],
    )
    def test_denied(self, tmp_path, command):
        _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "cat list | xargs -I{} echo {}",
            "xargs -n1 echo < list.txt",
            "printf 'a\\nb\\n' | xargs -n 2 cat",
        ],
    )
    def test_no_wait_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


class TestFunctionBodyLoops:
    """LOW (R6): the loop rule applies inside function bodies — the walker
    recognises an opener right after `{`/`(` (e.g. `f() { while …`)."""

    @pytest.mark.parametrize(
        "command",
        [
            "f() { while sleep 1; do echo hi; done; }; f",
            "f() { while true; do sleep 1; done; }; f",
            "f() { until check; do sleep 2; done; }; f",
            "f() { for i in 1 2; do sleep 0.5; done; }; f",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "sleep" in reason

    def test_function_without_wait_allowed(self, tmp_path):
        _allow(tmp_path, "f() { echo hi; }; f")


class TestDashCPositionInheritance:
    """INFO (R6, over-block): a recursed sub-chain inherits the OUTER
    segment's position — a sole sleep inside a `bash -c` that itself sits
    between actions is a between-actions pause."""

    def test_sole_sleep_in_between_actions_bash_c_allowed(self, tmp_path):
        _allow(tmp_path, "x && bash -c 'sleep 3' && y")

    @pytest.mark.parametrize(
        "command",
        [
            "bash -c 'sleep 3'",           # sole at top level
            "foo && bash -c 'sleep 3'",    # the bash -c is LAST
            "bash -c 'sleep 3' && foo",    # the bash -c is FIRST
        ],
    )
    def test_still_denied_when_not_between(self, tmp_path, command):
        _deny(tmp_path, command)

    def test_walker_stamps_the_outer_position(self):
        from vco_lib.bash_command_walk import walk_segments

        segs = walk_segments("x && bash -c 'sleep 3' && y")
        sleep = [s for s in segs if s.tokens and s.tokens[0] == "sleep"]
        assert len(sleep) == 1
        assert (sleep[0].position, sleep[0].chain_size) == (1, 3)
        assert sleep[0].depth == 1

    def test_timeout_bash_c_inherits_too(self, tmp_path):
        # The wait-guard's own timeout recursion carries the same override.
        _allow(tmp_path, "x && timeout 5 bash -c 'sleep 3' && y")
        _deny(tmp_path, "timeout 5 bash -c 'sleep 3'")


class TestBackgroundWaitLeaveAlone:
    """`cmd & wait` is NOT a poll: `cmd` runs in the background and the
    builtin blocks ONCE, in-process, until it finishes — no request is
    re-sent. Owner (coordinator 2026-10-15): leave allowed."""

    def test_cmd_backgrounded_then_wait_allowed(self, tmp_path):
        _allow(tmp_path, "cmd & wait")

    def test_sleep_backgrounded_then_wait_still_denied(self, tmp_path):
        # The sleep is the FIRST segment of the chain — a plain wait.
        _deny(tmp_path, "sleep 5 & wait")


class TestAssignmentSubstitutionWaits:
    """A substitution in an ASSIGNMENT value is walked like any other: the
    env-assignment strip used to eat the token before the substitution
    scan, so `x=$(sleep 5)` (a sole wait) slipped through."""

    @pytest.mark.parametrize(
        "command",
        [
            "x=$(sleep 5)",
            "x=`sleep 5`",
            'x="$(sleep 5)"',
            "export x=$(sleep 5)",
            "x=$(sleep 5) cmd",
            "sudo -E X=$(sleep 1) ls",  # assignment in the wrapper zone
        ],
    )
    def test_sole_wait_in_assignment_denied(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "x=$(date +%s)",
            "x=$(git rev-parse HEAD) && echo $x",
            "x=$(foo; sleep 1; bar)",  # fixed pause inside the body
            "FOO=1 make",
        ],
    )
    def test_assignment_without_sole_wait_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_walker_walks_assignment_substitutions_opt_in(self):
        from vco_lib.bash_command_walk import walk_command, walk_segments

        # Not opted in: the pre-existing layout (the split assignment's
        # tail fragment), no substitution body.
        plain = walk_segments("x=$(sleep 5)")
        assert [s.tokens for s in plain] == [["5)"]]
        opted = walk_segments("x=$(sleep 5)", _with_substitutions=True)
        assert [s.tokens for s in opted] == [["5)"], ["sleep", "5"]]
        assert (opted[1].depth, opted[1].position, opted[1].chain_size) == (
            1, 0, 1)
        # walk_command consumers are unchanged: no substitution body is
        # handed to the collector.
        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("x=$(sleep 5)", one) == ["5)"]
        assert walk_command('x="$(sleep 5)"', one) == []
        assert walk_command("FOO=$(pwd) make all", one) == ["make|all"]


class TestFunctionBodySoleWaits:
    """A function body's waits are judged by the position rule WITHIN the
    body when the function is called in the same command (sole/first/last
    in the body ⇒ deny; between actions in the body ⇒ allowed). A
    definition-only command runs nothing and is judged as before."""

    @pytest.mark.parametrize(
        "command",
        [
            "f() { sleep 1; }; f",
            "function f { sleep 2; }; f",
            "function f() { sleep 2; }; f",
            "f () { sleep 2; }; f",
            "f(){ sleep 2; }; f",
            "f() { a; sleep 1; }; f",        # last in the body
            "f() { sleep 1; a; }; f",        # first in the body
            "f() { sleep 1; }; x=$(f)",      # called through a substitution
            "f() { g() { sleep 1; }; g; }; f",  # nested definition
            # Between actions in the body, but called per loop iteration.
            "f() { a; sleep 1; b; }; while c; do f; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "f() { a; sleep 1; b; }; f",     # between actions in the body
            "f() { sleep 1; }",              # definition only
            "function f { sleep 2; }",       # definition only
            "f() { a; sleep 1; }",           # definition only
            "f() { sleep 1; }; echo f",      # `f` is an argument, not a call
            "f() { echo hi; }; f",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_function_header_shapes(self):
        from vco_lib.bash_command_walk import function_header

        assert function_header(["f()", "{", "sleep"]) == ("f", 2)
        assert function_header(["f(){", "sleep"]) == ("f", 1)
        assert function_header(["f", "()", "{"]) == ("f", 3)
        assert function_header(["function", "f", "{"]) == ("f", 3)
        assert function_header(["function", "f()", "{"]) == ("f", 3)
        assert function_header(["function", "f", "()", "{"]) == ("f", 4)
        assert function_header(["echo", "f()", "{"]) is None
        # Subshell bodies (v0.2.103 item 4): recognised like brace bodies.
        assert function_header(["f()", "(", "sleep"]) == ("f", 2)
        assert function_header(["f()(", "sleep", "1", ")"]) == ("f", 1)
        assert function_header(["f", "()", "(", "sleep"]) == ("f", 3)
        assert function_header(["function", "f", "(", "sleep"]) == ("f", 3)

    def test_walker_positions_body_only_when_called_and_opted_in(self):
        from vco_lib.bash_command_walk import walk_command, walk_segments

        called = walk_segments("f() { a; sleep 1; }; f",
                               _with_function_bodies=True)
        sleep = [s for s in called if s.tokens[0] == "sleep"]
        assert [(s.position, s.chain_size) for s in sleep] == [(1, 2)]
        # Definition only: the plain layout, header prefix kept.
        defn = walk_segments("f() { sleep 1; }", _with_function_bodies=True)
        assert defn[0].tokens == ["f()", "{", "sleep", "1"]
        # Not opted in: byte-identical to before.
        plain = walk_segments("f() { sleep 1; }; f")
        assert plain[0].tokens == ["f()", "{", "sleep", "1"]
        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("f() { sleep 1; }; f", one) == [
            "f()|{|sleep|1", "}", "f"]


# ═══════════════════════════════════════════════════════════════════════
# R9 follow-up wave (owner ruling 2026-10-15): newline separators, literal
# eval bodies, process substitutions, subshell-bodied functions, walker
# performance, and the busy-wait loop rule.
# ═══════════════════════════════════════════════════════════════════════


class TestNewlineCommandSeparator:
    """An unquoted newline is a command separator exactly like `;` (one
    home: the walker's tokenize). A quoted newline is data; a backslash
    newline is a continuation."""

    @pytest.mark.parametrize(
        "command",
        [
            "make\nsleep 5",                  # the sleep is LAST: a wait
            "make\nsleep 5\nmake test",       # between actions: allowed
            "while ! check\ndo sleep 2\ndone",
            "while ! check; do echo x; sleep 2; done\nsleep 2\necho next",
        ],
    )
    def test_newline_form_matches_semicolon_form(self, tmp_path, command):
        from vco_lib.wait_guard import static_pattern

        assert static_pattern(command) == static_pattern(
            command.replace("\n", "; "))

    def test_denied_last_sleep_on_newline(self, tmp_path):
        assert "sleep" in _deny(tmp_path, "make\nsleep 5")

    def test_allowed_between_actions_on_newlines(self, tmp_path):
        _allow(tmp_path, "make\nsleep 5\nmake test")

    def test_multiline_loop_sleep_denied(self, tmp_path):
        reason = _deny(tmp_path, "while ! check\ndo sleep 2\ndone")
        assert "sleep" in reason

    def test_quoted_newline_is_data(self, tmp_path):
        _allow(tmp_path, 'echo "a\nsleep 5"')

    def test_backslash_newline_is_a_continuation(self, tmp_path):
        # `sleep 5` stays ONE command with its argument — the sole/last
        # segment rule applies as to the one-line form.
        assert "sleep" in _deny(tmp_path, "make && sleep \\\n5")

    def test_walker_splits_newlines_like_semicolons(self):
        from vco_lib.bash_command_walk import tokenize, walk_command

        assert tokenize("make\nsleep 5") == ["make", ";", "sleep", "5"]
        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("make\nsleep 5", one) == ["make", "sleep|5"]
        assert walk_command("make\nsleep 5\nmake test", one) == [
            "make", "sleep|5", "make|test"]

    def test_heredoc_body_is_data_not_a_command(self, tmp_path):
        # A heredoc BODY line is raw text: `sleep 5` inside one is not a
        # wait verb any more than `echo "sleep 5"` is.
        _allow(tmp_path, "cat > f <<'EOF'\nsleep 5\nEOF")


class TestLiteralEvalBodies:
    """`eval '<literal>'` runs the string as a shell command — re-parsed
    exactly like a `bash -c` body (loop context and position inherited).
    A NON-literal body (`eval "$cmd"`) is never re-parsed."""

    @pytest.mark.parametrize(
        "command",
        [
            "eval 'sleep 5'",
            'eval "sleep 5"',
            "foo; eval 'sleep 5'",           # last: it only waits
            "while eval 'sleep 1'; do x; done",
            "timeout 5 eval 'sleep 3'",
        ],
    )
    def test_denied(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    def test_between_actions_allowed(self, tmp_path):
        # A sole sub-chain inherits the outer between-actions position —
        # the same pause `x && bash -c 'sleep 3' && y` is.
        _allow(tmp_path, "foo; eval 'sleep 5'; bar")
        _allow(tmp_path, "x && eval 'sleep 3' && y")
        _allow(tmp_path, "eval 'foo; sleep 2; bar'")

    def test_non_literal_eval_stays_as_is(self, tmp_path):
        # The body is not a fixed command: judged as the plain `eval`
        # segment (no wait verb), exactly as before.
        _allow(tmp_path, 'eval "sleep $N"')
        _allow(tmp_path, "eval \"$cmd\"")

    def test_walker_recurses_literal_eval_like_dash_c(self):
        from vco_lib.bash_command_walk import (
            literal_eval_body, walk_command,
        )

        assert literal_eval_body(["eval", "sleep 5"]) == "sleep 5"
        assert literal_eval_body(["eval", "sleep $N"]) is None
        assert literal_eval_body(["eval", "$cmd"]) is None
        assert literal_eval_body(["eval", "sleep", "5"]) is None
        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("eval 'a; b'", one) == ["a", "b"]
        # Non-literal: the segment is emitted, not recursed.
        assert walk_command('eval "$cmd"', one) == ["eval|$cmd"]


class TestProcessSubstitutionWaits:
    """`<(sleep 5)` / `>(...)` run their body as a command — scanned like
    a command substitution."""

    @pytest.mark.parametrize(
        "command",
        [
            "diff <(sleep 5) <(cat x)",
            "cmp <(cat a) <(sleep 2)",
            "tee >(sleep 5)",
            "echo x > >(sleep 3)",
        ],
    )
    def test_denied(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    def test_between_actions_substitution_allowed(self, tmp_path):
        # The body's own chain is a fixed pause between body actions.
        _allow(tmp_path, "diff <(foo; sleep 1; bar) <(cat x)")

    def test_no_wait_substitution_allowed(self, tmp_path):
        _allow(tmp_path, "diff <(cat a) <(cat b)")
        _allow(tmp_path, "wc -l <(ls)")


class TestSubshellBodiedFunctions:
    """`f() ( sleep 1 )` — same rule as brace bodies when the function is
    called in the same command."""

    @pytest.mark.parametrize(
        "command",
        [
            "f() ( sleep 1 ); f",
            "f()( sleep 2 ); f",
            "f () ( sleep 2 ); f",
            "function f ( sleep 2 ); f",
            "f() (\n  sleep 1\n); f",
            "f() ( a; sleep 1 ); f",        # last in the body
            "f() ( sleep 1; a ); f",        # first in the body
            "f() ( while sleep 1; do x; done ); f",
        ],
    )
    def test_denied(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "f() ( a; sleep 1; b ); f",     # between actions in the body
            "f() ( sleep 1 )",              # definition only
            "f() ( echo hi ); f",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


class TestWalkerFunctionBodyPerformance:
    """The per-definition call-site scan used to be quadratic (review R9:
    1500 function definitions → 5.2 s; ~2 s on this lane's machine). The
    invocation index makes it linear-ish."""

    def test_1500_definitions_walk_fast(self):
        import time

        from vco_lib.bash_command_walk import walk_segments

        command = "; ".join("f%d() { echo hi; }" % i for i in range(1500))
        start = time.perf_counter()
        segments = walk_segments(command, _with_function_bodies=True)
        elapsed = time.perf_counter() - start
        assert len(segments) == 3000  # headers + `echo hi` bodies
        # Generous bound: the pre-fix walk took ~2-5 s; the indexed one
        # runs in tens of milliseconds. Slow CI stays far under this.
        assert elapsed < 1.0, f"walker regressed to quadratic: {elapsed:.2f}s"


class TestBusyWaitLoops:
    """Owner ruling 2026-10-15: a while/until loop whose body does NO real
    work is a busy-wait — denied with its own message even with no sleep."""

    @pytest.mark.parametrize(
        "command",
        [
            "until curl -s localhost:8080; do :; done",
            "while ! test -f x; do true; done",
            "while true; do :; done",
            "while true; do echo x; done",
            "until curl -s localhost:8080; do echo waiting; done",
            "while ! check; do printf .; done",
            "while check; do done",
            # Multi-line form (newline separator).
            "until curl -s localhost:8080\ndo :\ndone",
            # Inside a called function body.
            "f() { while true; do :; done; }; f",
            "f() ( while true; do :; done ); f",
            # Nested: the INNER spin is caught.
            "while true; do while true; do :; done; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "busy-wait loop" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # The ruling's leave-alone examples: the body does real work.
            "for f in *.py; do ruff check $f; done",
            "while read l; do echo \"$l\" >> out; done < in",
            # for/select iterate a finite word list: never a busy-wait.
            "for f in *.py; do echo $f; done",
            "select x in a b; do echo $x; break; done",
            # Real work in the body, spin or not.
            "while true; do build_step; done",
            "while :; do date; done",
            # A redirected echo accumulates output — that is work.
            "while read l; do echo \"$l\" >> out; done",
            # A pipe out of the body is downstream work.
            "while true; do echo x | gzip >> log.gz; done",
            # An embedded loop in the body is work at this level.
            "while a; do for i in 1 2; do echo $i; done; done",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_deny_reason_has_its_own_message(self, tmp_path):
        reason = _deny(tmp_path, "until curl -s localhost:8080; do :; done")
        assert "busy-wait loop" in reason

    def test_sleep_in_body_still_names_the_sleep(self, tmp_path):
        reason = _deny(tmp_path, "while ! check; do echo x; sleep 2; done")
        assert "sleep" in reason and "busy-wait" not in reason


# ═══════════════════════════════════════════════════════════════════════
# R10 review fixes (Opus, MERGE-AFTER-FIXES; 2026-10-15)
# ═══════════════════════════════════════════════════════════════════════


class TestR10InputConsumingLoops:
    """F1 MAJOR: a loop whose CONDITION consumes input (read/mapfile/
    readarray) is bounded by that input — it ends at EOF — and is never a
    busy-wait, whatever the body does with the lines."""

    @pytest.mark.parametrize(
        "command",
        [
            "while read -r l; do echo \"$l\"; done < f",
            "while read -r l; do printf \"%s\\n\" \"$l\"; done < f",
            "cat f | while read -r l; do echo \"$l\"; done",
            "while IFS= read -r line; do\n echo \"$line\"\ndone < in.txt",
            "while mapfile -t lines; do :; done < f",
            "while readarray -t arr; do true; done < f",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_file_existence_poll_still_denied(self, tmp_path):
        # The review's contrast: an existence check is a real poll.
        reason = _deny(tmp_path, "until [ -f x ]; do :; done")
        assert "busy-wait loop" in reason


class TestR10ProgressOutputIsNotWork:
    """F2: fd duplications and /dev/null sinks write nowhere — progress
    dots on stderr are NOT work, so those loops are busy-waits."""

    @pytest.mark.parametrize(
        "command",
        [
            "until nc -z h p; do printf . >&2; done",
            "while c; do echo . 2>&1; done",
            "while c; do echo x 2>/dev/null; done",
            "while c; do echo x > /dev/null; done",
            "while c; do echo x &> /dev/null; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        assert "busy-wait loop" in _deny(tmp_path, command)

    def test_real_output_redirect_is_still_work(self, tmp_path):
        _allow(tmp_path, "while read l; do echo \"$l\" >> out; done < in")
        _allow(tmp_path, "while c; do echo x | gzip >> log.gz; done")


class TestR10ArithmeticFor:
    """F3: `for ((;;))` (empty middle clause) is the arithmetic infinite
    loop — `while true` in disguise, judged like one. A bounded arithmetic
    `for` stays finite."""

    @pytest.mark.parametrize(
        "command",
        [
            "for ((;;)); do :; done",
            "for ((;;)); do echo .; done",
            "for ((i=0;;)); do true; done",  # init but no bound
        ],
    )
    def test_infinite_denied(self, tmp_path, command):
        assert "busy-wait loop" in _deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "for ((i=0; i<3; i++)); do :; done",
            "for ((i=0; i<3; i++)); do echo $i; done",
            "for i in 1 2 3; do echo $i; done",
        ],
    )
    def test_bounded_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


class TestR10InputRedirectIsNotOutput:
    """F7: an INPUT redirect writes nothing — `echo x < f` is not work."""

    def test_denied(self, tmp_path):
        assert "busy-wait loop" in _deny(tmp_path, "while c; do echo x < f; done")


class TestR10NewlineScannerHardening:
    """F6: the newline-as-separator scanner is the one place comments are
    stripped (shlex runs with comments disabled); `\\"` closes the string
    (even backslash run); `$'…'` is ANSI-C quoting; a backslash-newline
    continuation vanishes entirely."""

    @pytest.mark.parametrize(
        "command",
        [
            "# it's fine\nsleep 30",
            "echo pre\n# comment with ' quote\nsleep 5",
            'echo "a\\\\"\nsleep 5',
            "echo $'it\\'s'\nsleep 5",
        ],
    )
    def test_guard_not_blinded(self, tmp_path, command):
        assert "sleep" in _deny(tmp_path, command)

    def test_comment_is_not_part_of_the_command(self, tmp_path):
        from vco_lib.bash_command_walk import tokenize

        assert tokenize("cat log # > x") == ["cat", "log"]
        # A mid-word `#` is data, not a comment start.
        assert tokenize("echo x#y\nsleep 5") == ["echo", "x#y", ";", "sleep", "5"]

    def test_continuation_vanishes_entirely(self, tmp_path):
        from vco_lib.bash_command_walk import tokenize
        from vco_lib.bash_write_targets import collect_candidates

        assert tokenize("cp a \\\n dest.txt") == ["cp", "a", "dest.txt"]
        assert collect_candidates("cp a \\\n dest.txt") == ["dest.txt"]
        # The command keeps its shape: a between-actions pause stays one.
        _allow(tmp_path, "make \\\n && sleep 5 && make")


# ═══════════════════════════════════════════════════════════════════════
# R14 review fixes (Opus part A; 2026-10-15)
# ═══════════════════════════════════════════════════════════════════════


class TestR14InputExemptionNarrowing:
    """A2: the `while read` exemption is for a loop BOUNDED at EOF only —
    `until read`, `while ! read` spin at EOF, a timed `read -t` waits by
    polling, and a piped producer re-runs every pass. None is exempt."""

    @pytest.mark.parametrize(
        "command",
        [
            "until read -r x; do :; done < f",
            "while ! read -r x; do :; done < f",
            "until read -t 1 x; do :; done",
            "while read -t 0; do :; done",
            "while curl -s u | read x; do :; done",
        ],
    )
    def test_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "busy-wait loop" in reason

    def test_plain_while_read_still_exempt(self, tmp_path):
        _allow(tmp_path, "while read -r l; do echo \"$l\"; done < f")


class TestR14ConditionRefreshPoll:
    """A3 (coordinator ruling 2026-10-15): a loop whose ONLY work is
    re-fetching the value its own condition tests is a poll — denied with
    its own message. Counted loops (arithmetic on the condition variable)
    and bodies with real work stay allowed."""

    @pytest.mark.parametrize(
        "command",
        [
            "until [ \"$r\" = 200 ]; do r=$(curl -s u); done",
            "while [ -z \"$x\" ]; do x=$(cat f 2>/dev/null); done",
            "until [ \"$r\" = 200 ]; do r=`curl -s u`; done",
            # A no-work statement beside the fetch changes nothing.
            "while [ -z \"$x\" ]; do x=$(stat -c %s f); echo .; done",
        ],
    )
    def test_polls_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "condition-refresh poll" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # Arithmetic on the condition variable: a counted loop.
            "i=0; while [ $i -lt 5 ]; do i=$((i+1)); done",
            "while [ -z \"$x\" ]; do x=200; done",       # literal: exits
            # Assigns a variable the condition never reads.
            "while [ -z \"$x\" ]; do y=$(cat f); done",
            # Real work beside the refresh.
            "until [ \"$r\" = 200 ]; do r=$(curl -s u); other; done",
            # The Bash twin of the PowerShell A1 shapes.
            "while [ $q -gt 0 ]; do item=$(dequeue); done",
        ],
    )
    def test_allowed(self, tmp_path, command):
        _allow(tmp_path, command)


# ═══════════════════════════════════════════════════════════════════════
# R16 review fixes (Opus part A; 2026-10-15): assignments ride on the
# segment, so multi-line bodies judge like single-line ones; the spaced
# arithmetic for header is one segment again.
# ═══════════════════════════════════════════════════════════════════════


class TestR16MultiLineBodies:
    """A-N1/A-N2: the walker carries a statement's assignment prefix on
    the segment, so a multi-line `r=$(curl -s u)` body line is a fetch
    (poll denied) and a counted loop's `i=$((i+1))` line is work (loop
    allowed) — exactly like their single-line twins."""

    @pytest.mark.parametrize(
        "command",
        [
            'until [ "$r" = 200 ]; do\n  r=$(curl -s u)\ndone',
            'until [ "$r" = 200 ]\ndo\n  r=$(curl -s u)\ndone',
            'while [ "$r" != 200 ]; do\n'
            '  r=$(curl -s -o /dev/null -w "%{http_code}" u)\ndone',
            "until [ -n \"$out\" ]; do\n  out=`cat f`\ndone",
            'until [ "$r" = 200 ]; do echo .; r=$(curl -s u); done',
        ],
    )
    def test_multi_line_polls_denied(self, tmp_path, command):
        reason = _deny(tmp_path, command)
        assert "condition-refresh poll" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "i=0; while [ $i -lt 5 ]; do\n  i=$((i+1))\ndone",
            "while [ $n -gt 0 ]; do\n  n=$((n-1))\ndone",
        ],
    )
    def test_multi_line_counted_loops_allowed(self, tmp_path, command):
        _allow(tmp_path, command)

    def test_walker_carries_assignments(self):
        from vco_lib.bash_command_walk import walk_command, walk_segments

        # Opt-in: an assignment-only statement is emitted with EMPTY
        # tokens and its re-joined assignment.
        opted = walk_segments("r=$(curl -s u)", _with_assignments=True)
        assert [(s.tokens, s.assignments) for s in opted] == [
            ([], ("r=$(curl -s u)",))]
        multi = walk_segments("until c; do\n  r=$(curl -s u)\ndone",
                              _with_assignments=True)
        assert any(s.tokens == [] and s.assignments == ("r=$(curl -s u)",)
                   for s in multi)
        # A value spanning tokens stays ONE assignment.
        x = walk_segments("x=$(sleep 5) cmd", _with_assignments=True)
        assert x[0].assignments == ("x=$(sleep 5)",) and x[0].tokens == ["cmd"]
        # walk_command's collector contract is unchanged: without the
        # opt-in, the emission is exactly the pre-R16 one (the torn tail
        # for a value-spanning assignment — the shape the field fixes for
        # the guard), and a plain env prefix still peels.
        one = lambda toks: ["|".join(toks)]  # noqa: E731
        assert walk_command("r=$(curl -s u)", one) == ["-s|u)"]
        assert walk_command("FOO=1 make", one) == ["make"]

    def test_spaced_arithmetic_for_header(self, tmp_path):
        # A-N3: the header's inner `;` no longer tears the segment.
        assert "busy-wait loop" in _deny(tmp_path, "for (( ; ; )); do :; done")
        assert "busy-wait loop" in _deny(
            tmp_path, "for (( i=0 ; ; i++ )); do :; done")
        _allow(tmp_path, "for (( i=0 ; i<3 ; i++ )); do :; done")
        _allow(tmp_path, "for ((i=0; i<3; i++)); do :; done")


# ═══════════════════════════════════════════════════════════════════════
# Windows verification (v0.2.103 item 19): a WAIT inside a BOUNDED
# PowerShell loop is denied, like its Bash twin. Before, loop_pattern gave a
# bounded `for`, a `foreach` over a collection and a `| ForEach-Object`
# stage no label at all, so the wait check never ran on them.
# ═══════════════════════════════════════════════════════════════════════


class TestPowerShellBoundedLoopWaits:
    @pytest.mark.parametrize(
        "command",
        [
            "for ($i=0;$i -lt 9;$i++) { Start-Sleep 1; Get-Content f }",
            "for ($i=0; $i -lt 9; $i++) { Get-Content f; Start-Sleep -Seconds 1 }",
            "foreach ($i in 1..9) { Start-Sleep 1; Get-Content f }",
            "foreach ($f in Get-ChildItem) { Get-Content $f; Start-Sleep 2 }",
            "1..9 | ForEach-Object { Start-Sleep 1; Get-Content f }",
            "1..9 | % { Get-Content f; Start-Sleep 1 }",
            "Get-ChildItem | foreach { [Threading.Thread]::Sleep(500); $_ }",
        ],
    )
    def test_wait_in_a_bounded_loop_is_denied(self, tmp_path, command):
        _deny(tmp_path, command, tool="PowerShell")

    @pytest.mark.parametrize(
        "command",
        [
            "for ($i=0;$i -lt 3;$i++) { Write-Output $i }",
            "foreach ($f in Get-ChildItem) { ruff check $f }",
            "Get-ChildItem | ForEach-Object { ruff check $_ }",
            "Get-ChildItem | foreach { $_.Name }",
            # the loop text inside a string is not a loop
            "Write-Output 'for ($i=0;$i -lt 3;$i++) { Start-Sleep 1 }'",
        ],
    )
    def test_bounded_loop_without_a_wait_is_allowed(self, tmp_path, command):
        _allow(tmp_path, command, tool="PowerShell")

    def test_bash_twins_agree(self, tmp_path):
        _deny(tmp_path, "for i in 1 2 3; do sleep 1; cat f; done")
        _deny(tmp_path, "for ((i=0;i<9;i++)); do sleep 1; cat f; done")
        _allow(tmp_path, "for f in a b; do ruff check $f; done")
