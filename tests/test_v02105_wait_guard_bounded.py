# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 fix round — the wait guard's bounded-total-wait reading of
script loops, the shipped commands it refuses, and the kill guard's
signal-0 parity.

* A loop inside a script FILE whose worst-case total wait is proved at
  most 5 s is a retry, not a poll (owner ruling 2026-10-29); no literal
  bound, a non-literal sleep, or more than 5 s → refused, as before.
  Sampling loops stay banned.
* The shipped ``rc-native`` skill runs its script in the background (the
  25 s readiness loop is refused in the foreground, allowed in the
  background).
* The release scripts are still refused in the foreground; their usage
  headers say "run in background".
* Kill guard: ``kill -0 1`` (a signal-0 probe) passes the static layer as
  it passes the runtime layer; the composed here-doc gap is documented.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vco_lib import wait_guard as wg
from vco_lib.kill_guard import static_check
from vco_lib.wait_guard_bounded import (
    WAIT_BUDGET_S,
    for_list_count,
    sleep_seconds,
)

REPO = Path(__file__).resolve().parents[1]


def _eval(tmp_path: Path, tool: str, command: str, cwd: Path, **extra):
    payload = {"session_id": "s-v02105-bounded", "tool_name": tool,
               "tool_input": {"command": command, **extra}, "cwd": str(cwd)}
    return wg.evaluate(payload, env={}, state_root=tmp_path / "state",
                       now=1000.0)


# ═══════════════════════════════════════════════════════════════════════
# Item 2 — bounded total wait (Bash)
# ═══════════════════════════════════════════════════════════════════════

BOUNDED_SH = {
    "five_words": "for i in 1 2 3 4 5; do sleep 1; done\n",
    "seq_lock_retry": ("for _ in $(seq 1 40); do\n  [ -s f ] && break\n"
                       "  sleep 0.05\ndone\n"),
    "brace_range": "for i in {1..10}; do sleep 0.5; done\n",
    "arith_for": "for ((i=0; i<20; i++)); do sleep 0.05; done\n",
    "two_loops": "for i in 1 2; do sleep 2; done\nfor i in 1 2; do sleep 2; done\n",
    "in_function": "f() {\n  for i in 1 2 3; do sleep 1; done\n}\nf\n",
    "ms_suffix": "for i in $(seq 20); do sleep 250ms; done\n",
}

UNBOUNDED_SH = {
    "over_budget": "for i in 1 2 3 4 5 6; do sleep 1; done\n",
    "rc_native_shape": "for _ in $(seq 1 25); do sleep 1; done\n",
    "two_sleeps_sum": "for i in 1 2 3; do sleep 1; sleep 1; done\n",
    "while_counter": "n=0\nwhile [ $n -lt 3 ]; do sleep 1; n=$((n+1)); done\n",
    "while_true": "while true; do sleep 1; done\n",
    "until": "until [ -f x ]; do sleep 0.1; done\n",
    "variable_list": "for i in $LIST; do sleep 1; done\n",
    "positional_list": "for i; do sleep 1; done\n",
    "glob_list": "for f in *.txt; do sleep 0.1; done\n",
    "non_literal_sleep": "for i in 1 2; do sleep $D; done\n",
    "minutes": "for i in 1; do sleep 1m; done\n",
    "nested": "for i in 1 2; do for j in 1 2; do sleep 0.1; done; done\n",
    "arith_var_written": "for ((i=0; i<20; i++)); do sleep 0.05; i=0; done\n",
    "dash_c_sleep": "for i in 1 2; do bash -c 'sleep 0.1'; done\n",
    "wrapped_sleep": "for i in 1 2; do timeout 1 sleep 0.1; done\n",
    "mixed": "for i in 1 2; do sleep 1; done\nwhile :; do sleep 1; done\n",
}


@pytest.fixture
def shdir(tmp_path):
    d = tmp_path / "sh"
    d.mkdir()
    for name, body in {**BOUNDED_SH, **UNBOUNDED_SH}.items():
        (d / f"{name}.sh").write_text(body, encoding="utf-8")
    (d / "outer.sh").write_text("for k in 1 2; do bash five_words.sh; done\n",
                                encoding="utf-8")
    return d


@pytest.mark.parametrize("name", sorted(BOUNDED_SH))
def test_a_bounded_script_loop_is_allowed(tmp_path, shdir, name):
    assert _eval(tmp_path, "Bash", f"bash {name}.sh", shdir) is None


@pytest.mark.parametrize("name", sorted(UNBOUNDED_SH))
def test_an_unbounded_script_loop_is_still_refused(tmp_path, shdir, name):
    reason = _eval(tmp_path, "Bash", f"bash {name}.sh", shdir)
    assert reason is not None and "in script" in reason, name


@pytest.mark.parametrize("command", [
    "for k in 1 2; do bash five_words.sh; done",   # the caller multiplies it
    "bash outer.sh",                                # …from a script, too
    "ls | xargs -n1 bash five_words.sh",            # once per input item
])
def test_a_bounded_script_run_in_a_loop_is_refused(tmp_path, shdir, command):
    assert _eval(tmp_path, "Bash", command, shdir) is not None


def test_the_budget_applies_only_inside_script_files(tmp_path, shdir):
    # Typed inline, a sleep in a loop stays a wait whatever its count.
    assert _eval(tmp_path, "Bash", "for i in 1 2; do sleep 1; done",
                 shdir) is not None


def test_a_busy_wait_next_to_a_bounded_loop_is_still_refused(tmp_path, shdir):
    (shdir / "busy.sh").write_text(
        "for i in 1 2; do sleep 1; done\nwhile [ ! -f x ]; do :; done\n",
        encoding="utf-8")
    reason = _eval(tmp_path, "Bash", "bash busy.sh", shdir)
    assert reason is not None and "busy-wait" in reason


def test_helpers():
    assert WAIT_BUDGET_S == 5.0
    assert sleep_seconds(["0.5"]) == 0.5
    assert sleep_seconds(["1", "2s"]) == 3.0
    assert sleep_seconds(["50ms"]) == pytest.approx(0.05)
    assert sleep_seconds(["$n"]) is None
    assert sleep_seconds(["infinity"]) is None
    assert sleep_seconds([]) is None
    assert for_list_count(["a", "b", "c"]) == 3
    assert for_list_count(["$(seq", "1", "25)"]) == 25
    assert for_list_count(["$(seq", "40)"]) == 40
    assert for_list_count(["$(seq", "1", "2", "9)"]) == 5
    assert for_list_count(["{1..10}"]) == 10
    assert for_list_count(["{1..10..3}"]) == 4
    assert for_list_count(["$x"]) is None
    assert for_list_count(["*.txt"]) is None


# ═══════════════════════════════════════════════════════════════════════
# Item 2 — bounded total wait (PowerShell)
# ═══════════════════════════════════════════════════════════════════════

LOCK_PS1 = """$a = 0
while ($null -eq $s -and $a -lt 20) {
  try { $s = Open-It } catch { Start-Sleep -Milliseconds 50; $a++ }
}
"""

BOUNDED_PS1 = {
    "lock_retry": LOCK_PS1,
    "for_five": "for ($i = 0; $i -lt 5; $i++) { Start-Sleep 1 }\n",
    "foreach_range": "foreach ($x in 1..4) { Start-Sleep -s 1 }\n",
}

UNBOUNDED_PS1 = {
    "lock_over_budget": LOCK_PS1.replace("50;", "500;"),
    "lock_no_increment": LOCK_PS1.replace("$a++", "Write-Host x"),
    "lock_or": LOCK_PS1.replace("-and", "-or"),
    "lock_reset": LOCK_PS1.replace("$a++ }", "$a++; $a = 0 }"),
    "lock_continue": LOCK_PS1.replace("$a++ }", "continue; $a++ }"),
    "lock_no_init": LOCK_PS1.replace("$a = 0\n", ""),
    "lock_call_after_init": LOCK_PS1.replace("$a = 0\n", "$a = 0\n$b = Get-Foo\n"),
    "wait_inside_try": ("$a = 0\nwhile ($a -lt 20) {\n"
                        "  try { Start-Sleep -Milliseconds 50; Foo; $a++ } catch { }\n}\n"),
    "increment_in_other_branch": ("$a = 0\nwhile ($a -lt 20) {\n"
                                  "  if ($x) { Start-Sleep -Milliseconds 50 } else { $a++ }\n}\n"),
    "per_item_scriptblock": ("$a = 0\nwhile ($a -lt 3) { 1..100 | ForEach-Object "
                             "{ Start-Sleep -Milliseconds 10 }; $a++ }\n"),
    "for_over_budget": "for ($i = 0; $i -lt 6; $i++) { Start-Sleep 1 }\n",
    "for_var_written": "for ($i = 0; $i -lt 5; $i++) { Start-Sleep 1; $i-- }\n",
    "foreach_collection": "foreach ($x in $items) { Start-Sleep -s 1 }\n",
    "while_true": "while ($true) { Start-Sleep 1 }\n",
    "do_loop": "$a=0\ndo { Start-Sleep 1; $a++ } while ($a -lt 2)\n",
    "thread_sleep": "for ($i = 0; $i -lt 2; $i++) { [Threading.Thread]::Sleep(10) }\n",
    "nested": ("for ($i = 0; $i -lt 2; $i++) { for ($j = 0; $j -lt 2; $j++) "
               "{ Start-Sleep 1 } }\n"),
}


@pytest.fixture
def psdir(tmp_path):
    d = tmp_path / "ps"
    d.mkdir()
    for name, body in {**BOUNDED_PS1, **UNBOUNDED_PS1}.items():
        (d / f"{name}.ps1").write_text(body, encoding="utf-8")
    return d


@pytest.mark.parametrize("name", sorted(BOUNDED_PS1))
def test_a_bounded_ps1_loop_is_allowed(tmp_path, psdir, name):
    assert _eval(tmp_path, "PowerShell", f"& ./{name}.ps1", psdir) is None


@pytest.mark.parametrize("name", sorted(UNBOUNDED_PS1))
def test_an_unbounded_ps1_loop_is_still_refused(tmp_path, psdir, name):
    reason = _eval(tmp_path, "PowerShell", f"& ./{name}.ps1", psdir)
    assert reason is not None and "in script" in reason, name


def test_a_bounded_ps1_run_in_a_loop_is_refused(tmp_path, psdir):
    assert _eval(tmp_path, "PowerShell",
                 "foreach ($k in 1..3) { & ./lock_retry.ps1 }", psdir) is not None


def test_the_ps_budget_applies_only_inside_script_files(tmp_path, psdir):
    assert _eval(tmp_path, "PowerShell", LOCK_PS1, psdir) is not None


# ═══════════════════════════════════════════════════════════════════════
# The shipped scripts (the reviewer's verification shapes)
# ═══════════════════════════════════════════════════════════════════════


def test_the_documented_windows_resolver_is_allowed(tmp_path):
    # vct_project_config.ps1's 20 x 50 ms lock retry = 1 s worst case.
    assert _eval(tmp_path, "PowerShell",
                 "& templates/scripts/vct_project_config.ps1 hub-port",
                 REPO) is None


@pytest.fixture
def installed(tmp_path):
    """The rc-native scripts where a bundle install puts them."""
    proj = tmp_path / "proj"
    (proj / ".claude" / "scripts").mkdir(parents=True)
    for name in ("rc-native.sh", "rc-native.ps1"):
        shutil.copy(REPO / "templates" / "scripts" / name,
                    proj / ".claude" / "scripts" / name)
    return proj


RC_SH = "bash .claude/scripts/rc-native.sh start"
RC_PS = ("powershell -NoProfile -ExecutionPolicy Bypass -File "
         ".claude/scripts/rc-native.ps1 start")


@pytest.mark.parametrize("tool,command", [("Bash", RC_SH), ("PowerShell", RC_PS)])
def test_rc_native_is_refused_in_the_foreground(tmp_path, installed, tool, command):
    reason = _eval(tmp_path, tool, command, installed)
    assert reason is not None and "in script" in reason


@pytest.mark.parametrize("tool,command", [("Bash", RC_SH), ("PowerShell", RC_PS)])
def test_rc_native_is_allowed_in_the_background(tmp_path, installed, tool, command):
    assert _eval(tmp_path, tool, command, installed,
                 run_in_background=True) is None


def test_the_rc_native_skill_runs_its_commands_in_the_background():
    text = (REPO / "templates" / "skills" / "rc-native" / "SKILL.md").read_text(
        encoding="utf-8")
    start = text.index("## On EVERY invocation")
    section = text[start:text.index("\n## ", start + 1)]
    assert "run_in_background: true" in section
    assert "`start`, `status`, `url`, `stop` and `logs`" in section
    commands = text[text.index("## Commands"):]
    assert "run_in_background: true" in commands[:commands.index("\n## ", 1)]


RELEASE_SCRIPTS = ["scripts/pre-ship-check.sh", "scripts/launcher-boot-smoke.sh",
                   "scripts/launch-claude-mcp-stack.sh"]


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_release_scripts_are_refused_in_the_foreground(tmp_path, script):
    reason = _eval(tmp_path, "Bash", f"bash {script}", REPO)
    assert reason is not None and "in script" in reason


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_release_scripts_are_allowed_in_the_background(tmp_path, script):
    assert _eval(tmp_path, "Bash", f"bash {script}", REPO,
                 run_in_background=True) is None


@pytest.mark.parametrize("script", RELEASE_SCRIPTS)
def test_release_scripts_say_run_in_background(script):
    head = (REPO / script).read_text(encoding="utf-8").split("\nset ", 1)[0]
    assert "run_in_background: true" in head


def test_contributing_says_run_the_release_gate_in_background():
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "run_in_background: true" in text


# ═══════════════════════════════════════════════════════════════════════
# Item 4 — kill guard
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("command", [
    "kill -0 1", "kill -0 -1", "kill -s 0 0", "kill -s0 1", "kill --signal=0 1",
    "kill -0 $$",
])
def test_a_signal_zero_probe_of_a_special_pid_passes_the_static_layer(command):
    assert static_check(command) is None


@pytest.mark.parametrize("command", [
    "kill -9 1", "kill 1", "kill -TERM -1", "kill -0 1; kill 1",
    "kill -0 $(ps -o ppid= -p 5)",   # the parent-lookup rule still holds
])
def test_a_real_kill_of_a_special_pid_is_still_refused(command):
    assert static_check(command) is not None


def test_the_composed_heredoc_gap_is_documented():
    text = (REPO / "docs" / "features" / "03-agents-skills-hooks.md").read_text(
        encoding="utf-8")
    kill = text[text.index("### Kill guard"):text.index("### `wait-guard.sh`")]
    assert "compose into one uncovered path" in kill
    assert "here-doc" in kill and "bash k.sh" in kill
