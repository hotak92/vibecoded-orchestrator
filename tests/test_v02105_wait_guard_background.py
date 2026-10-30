# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 fix round 2 — a BACKGROUND wait loop must be BOUNDED, and the
wait guard reads ONE xargs option table.

* A background wait loop stays allowed (owner rulings: only FOREGROUND
  sampling is banned) but it must be bounded: a literal ``timeout <N>``
  around the whole command (also through a ``bash -c`` body), or a literal
  iteration cap. An unbounded one is refused and the message shows the
  bounded form — field report 2026-10-30: an ``until`` loop whose command
  was not on PATH ran 83 minutes.
* ``2>/dev/null`` (or ``&>/dev/null``) on a command in that loop's
  CONDITION is refused too, and the message says why: a command that
  cannot run then looks like "not yet" forever.
* Foreground decisions are unchanged.
* The xargs separate-value options are the walker's ONE table
  (:data:`vco_lib.bash_command_walk.XARGS_VALUE_OPTS`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vco_lib import wait_guard as wg
from vco_lib.bash_command_walk import XARGS_VALUE_OPTS


def _eval(tmp_path: Path, command: str, *, background: bool = True,
          tool: str = "Bash", cwd: Path | None = None):
    tool_input: dict = {"command": command}
    if background:
        tool_input["run_in_background"] = True
    payload = {"session_id": "s-v02105-bg", "tool_name": tool,
               "tool_input": tool_input, "tool_use_id": "tu-1"}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return wg.evaluate(payload, env={}, state_root=tmp_path / "state",
                       now=1000.0)


# The field report's loop, verbatim in shape: the condition's stderr is
# thrown away, so a `ninja` that is not on PATH reads as "not yet" forever.
FIELD_LOOP = (
    'until [ -f build/linux/build.ninja ] && '
    'ninja -C build/linux -n 2>/dev/null | grep -q "nothing to do"; '
    'do sleep 15; done'
)


# ═══════════════════════════════════════════════════════════════════════
# 1. An unbounded background wait loop is refused
# ═══════════════════════════════════════════════════════════════════════

def test_the_field_report_loop_is_refused(tmp_path):
    reason = _eval(tmp_path, FIELD_LOOP)
    assert reason is not None
    assert "UNBOUNDED wait loop" in reason
    assert "timeout 600" in reason          # the bounded form is shown
    assert "VCO_WAIT_GUARD=off" in reason


def test_the_message_says_the_condition_throws_stderr_away(tmp_path):
    reason = _eval(tmp_path, FIELD_LOOP)
    assert reason is not None
    assert "throws stderr away" in reason
    assert "`2>/dev/null`" in reason


def test_a_condition_that_keeps_stderr_gets_the_bound_message_only(tmp_path):
    kept = FIELD_LOOP.replace(" 2>/dev/null", "")
    reason = _eval(tmp_path, kept)
    assert reason is not None and "UNBOUNDED wait loop" in reason
    assert "throws stderr away" not in reason


@pytest.mark.parametrize("command", [
    "until [ -f x ]; do sleep 15; done",
    "while [ ! -f x ]; do sleep 15; done",
    "while true; do sleep 1; done",
    "bash -c 'until [ -f x ]; do sleep 15; done'",
])
def test_an_unbounded_background_loop_is_refused(tmp_path, command):
    assert _eval(tmp_path, command) is not None, command


def test_an_unbounded_background_busy_loop_is_refused(tmp_path):
    reason = _eval(tmp_path, "while true; do :; done")
    assert reason is not None and "busy-wait" in reason


# ═══════════════════════════════════════════════════════════════════════
# 2. The bounded forms are allowed
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("command", [
    "timeout 600 bash -c 'until [ -f x ]; do sleep 15; done'",
    "timeout 600 sh -c 'while true; do sleep 15; done'",
    "nohup timeout 30 make check",
    "/usr/bin/timeout 30s ./wait.sh",
])
def test_a_literal_timeout_bounds_the_whole_command(tmp_path, command):
    assert _eval(tmp_path, command) is None, command


@pytest.mark.parametrize("command", [
    "for i in 1 2 3; do sleep 1; done",
    "for i in {1..4}; do sleep 1; done",
    "for ((i=0; i<3; i++)); do sleep 1; done",
    "for h in a b; do curl -s $h; sleep 1; done",
])
def test_a_literal_iteration_cap_bounds_the_loop(tmp_path, command):
    assert _eval(tmp_path, command) is None, command


@pytest.mark.parametrize("command", [
    "sleep 600",                        # a background pause is not a loop
    "make -j8 && echo done",            # real work, no loop
    "while read l; do echo $l; done < f",   # bounded by its input (EOF)
])
def test_a_background_command_with_no_wait_loop_is_allowed(tmp_path, command):
    assert _eval(tmp_path, command) is None, command


def test_a_non_literal_timeout_never_bounds(tmp_path):
    assert _eval(tmp_path, "timeout $N bash -c 'until [ -f x ]; do sleep 1; done'") is not None


def test_timeout_bounded_reads_the_literal_forms():
    assert wg._timeout_bounded("timeout 600 bash x.sh") is True
    assert wg._timeout_bounded("nohup timeout 5m make") is True
    assert wg._timeout_bounded("bash -c 'timeout 600 bash x.sh'") is True
    assert wg._timeout_bounded("timeout -s KILL 30 make") is True
    assert wg._timeout_bounded("timeout $N make") is False
    assert wg._timeout_bounded("timeout infinity make") is False
    assert wg._timeout_bounded("make check") is False


# ═══════════════════════════════════════════════════════════════════════
# 3. Foreground is unchanged
# ═══════════════════════════════════════════════════════════════════════

def test_foreground_keeps_its_own_rule_and_message(tmp_path):
    foreground = _eval(tmp_path, "while true; do sleep 1; done",
                       background=False)
    assert foreground is not None
    assert "matches the polling pattern" in foreground
    assert "UNBOUNDED wait loop" not in foreground


def test_a_foreground_sole_sleep_is_still_refused(tmp_path):
    assert _eval(tmp_path, "sleep 600", background=False) is not None


# ═══════════════════════════════════════════════════════════════════════
# 4. PowerShell parity
# ═══════════════════════════════════════════════════════════════════════

def test_a_powershell_unbounded_background_loop_is_refused(tmp_path):
    reason = _eval(tmp_path, "while ($true) { Start-Sleep -Seconds 15 }",
                   tool="PowerShell")
    assert reason is not None and "UNBOUNDED wait loop" in reason


@pytest.mark.parametrize("command", [
    "for ($i = 0; $i -lt 5; $i++) { Start-Sleep -Seconds 2 }",
    # PowerShell also accepts the assignment glued; the parser must read both.
    "for ($i=0; $i -lt 5; $i++) { Start-Sleep -Seconds 2 }",
    "for ($i=0; $i -lt 5; $i+=1) { Start-Sleep -Seconds 2 }",
    "foreach ($n in 1..5) { Start-Sleep -Seconds 2 }",
])
def test_a_powershell_literal_cap_bounds_the_loop(tmp_path, command):
    assert _eval(tmp_path, command, tool="PowerShell") is None, command


def test_a_glued_but_non_literal_powershell_bound_is_still_refused(tmp_path):
    reason = _eval(tmp_path, "for ($i=$n; $i -lt 5; $i++) { Start-Sleep -Seconds 2 }",
                   tool="PowerShell")
    assert reason is not None and "UNBOUNDED wait loop" in reason


def test_a_powershell_condition_that_hides_errors_is_named(tmp_path):
    reason = _eval(
        tmp_path,
        "while ((Test-Path x) -and ((Get-Y) 2>$null)) "
        "{ Start-Sleep -Seconds 15 }",
        tool="PowerShell")
    assert reason is not None and "throws stderr away" in reason


def test_a_powershell_foreground_loop_is_unchanged(tmp_path):
    reason = _eval(tmp_path, "while ($true) { Start-Sleep -Seconds 15 }",
                   background=False, tool="PowerShell")
    assert reason is not None and "polling pattern" in reason


# ═══════════════════════════════════════════════════════════════════════
# 5. The xargs option table is the walker's ONE table (F2-D item 4)
# ═══════════════════════════════════════════════════════════════════════

def test_the_guard_reads_the_shared_xargs_table():
    assert not hasattr(wg, "_XARGS_VALUE_FLAGS")
    assert wg.XARGS_VALUE_OPTS is XARGS_VALUE_OPTS


def test_the_shared_table_reads_the_replace_str_form():
    # `xargs -i sleep echo`: "-i sleep" is the replace-str, the per-item
    # command is `echo`. The old local table did not know `-i`, took
    # `sleep` for the per-item command and refused a harmless echo.
    assert wg.static_pattern("ls | xargs -i sleep echo") is None


def test_a_per_item_wait_is_still_refused(tmp_path):
    assert wg.static_pattern("xargs -n1 sleep < list.txt") is not None


# ═══════════════════════════════════════════════════════════════════════
# 6. Fix round 3 (review E-1/E-2/E-3/E-6/E-7)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("command", [
    # E-1: one bounded segment must not bless an unbounded sibling.
    "timeout 5 ls; until [ -f x ]; do sleep 15; done",
    "until [ -f x ]; do sleep 15; done; timeout 5 ls",
    "timeout 5 true; f() { until [ -f x ]; do sleep 15; done; }; f",
    # and a loop after the timeout-wrapped one, joined either way
    "timeout 30 bash -c 'until [ -f x ]; do sleep 15; done'; "
    "until [ -f y ]; do sleep 15; done",
])
def test_one_bounded_segment_does_not_bless_the_command(tmp_path, command):
    reason = _eval(tmp_path, command)
    assert reason is not None and "UNBOUNDED wait loop" in reason, command


def test_a_timeout_wrapped_loop_with_more_after_it(tmp_path):
    """The strict side of E-1's rule: a second top-level statement (even a
    harmless one) means no single timeout bounds the command, so the loop
    is refused — the message's bounded form shows how to write it."""
    reason = _eval(
        tmp_path, "timeout 600 bash -c 'until [ -f x ]; do sleep 15; done' "
                  "&& echo done")
    assert reason is not None and "UNBOUNDED wait loop" in reason


def test_a_bounded_loop_with_a_silenced_condition_is_refused(tmp_path):
    """E-2: the stderr rule is independent of the bound — a loop whose
    condition throws stderr away is refused even wrapped in ``timeout``,
    and the message names ONLY the redirection (no bound-it advice: it is
    already bounded)."""
    reason = _eval(
        tmp_path,
        "timeout 30 bash -c 'until [ -f x ] && ninja -n 2>/dev/null "
        "| grep -q y; do sleep 15; done'")
    assert reason is not None, "the silenced condition must refuse it"
    assert "throws its CONDITION's stderr away" in reason
    assert "`2>/dev/null`" in reason
    assert "UNBOUNDED" not in reason


def test_the_powershell_advice_leads_with_the_counter_for(tmp_path):
    """E-3: the PowerShell branch never accepts a ``timeout`` wrapper on
    an INLINE loop, so the advice must lead with the form that works
    there (the counter ``for``)."""
    reason = _eval(tmp_path, "while ($true) { Start-Sleep -Seconds 15 }",
                   tool="PowerShell")
    assert reason is not None
    assert "give the loop a counter `for`" in reason
    assert reason.index("counter `for`") < reason.index("timeout")


def test_a_glued_powershell_counter_retry_is_bounded(tmp_path):
    """E-6: ``$n=0`` glued (no spaces) is the same counter retry as the
    spaced form — the ungluing ``_ps_for_iterations`` does now also covers
    the while-loop counter init."""
    assert _eval(
        tmp_path,
        "$n=0\nwhile ($n -lt 20) { Start-Sleep -Milliseconds 50; $n++ }",
        tool="PowerShell") is None


def test_a_silenced_command_outside_the_powershell_loop_is_not_named(tmp_path):
    """E-7: only the loop's own condition can decorate the refusal with
    the stderr claim — a `2>$null` elsewhere in the command says nothing
    about the loop."""
    reason = _eval(
        tmp_path,
        "Get-Content setup.log 2>$null; "
        "while ($true) { Start-Sleep -Seconds 15 }",
        tool="PowerShell")
    assert reason is not None and "UNBOUNDED wait loop" in reason
    assert "throws stderr away" not in reason