# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — the wait-loop guard: one-shot CronCreate, script files,
no-op wait loops and the R9 base-equal gaps.

* **One-shot CronCreate** (owner ruling 2026-10-29, #508): a ``CronCreate``
  whose input carries ``recurring: false`` (the field Claude Code's tool
  takes; a real input is ``{"cron": …, "recurring": false, "prompt": …}``)
  is allowed in the main chat without /loop or /schedule. A recurring one,
  a missing field or any non-boolean value keeps the old rule; a subagent
  is still always refused.
* **Sampling loops stay banned** (same ruling).
* **Script files** (field report 2026-10-21): ``bash poll.sh``,
  ``./poll.sh``, ``source``/``.``, ``cat poll.sh | bash``, a wrapper in
  front — the file is read (≤ 256 KiB, text, a shell script) and judged
  by the same classifier in its script reading: loops that wait, busy-wait
  and condition-refresh loops, ``watch``/``tail -f`` are polls; a single
  pause is not. ``run_in_background`` stays exempt. PowerShell: ``.ps1``.
* **No-op wait loops**: the 3rd identical ``true``/``:``/``echo …``/
  ``sleep 0`` call from one session within 120 s is refused.
* **R9 gaps**: code fed to a shell on stdin / a here-string / a heredoc,
  ``case`` arms, ``coproc``, code held in a variable.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from vco_lib import wait_guard as wg
from vco_lib.bash_command_walk import (
    split_chain,
    split_chain_ops,
    strip_case_arm,
    tokenize,
)
from vco_lib.bash_write_targets import strip_heredocs


def _payload(tool: str, tool_input: dict, **extra) -> dict:
    p = {"session_id": "s-v02105", "tool_name": tool, "tool_input": tool_input}
    p.update(extra)
    return p


def _eval(tmp_path: Path, payload: dict, now: float = 1000.0):
    return wg.evaluate(payload, env={}, state_root=tmp_path / "state", now=now)


# ═══════════════════════════════════════════════════════════════════════
# 1. One-shot CronCreate
# ═══════════════════════════════════════════════════════════════════════

_CRON = {"cron": "33 10 3 9 *", "prompt": "re-dispatch the lanes"}


class TestOneShotCronCreate:
    def test_non_recurring_is_allowed_without_loop_or_schedule(self, tmp_path):
        assert _eval(tmp_path, _payload(
            "CronCreate", dict(_CRON, recurring=False))) is None

    def test_non_recurring_durable_is_allowed(self, tmp_path):
        # The exact shape a real transcript carries.
        assert _eval(tmp_path, _payload(
            "CronCreate", dict(_CRON, recurring=False, durable=True))) is None

    @pytest.mark.parametrize("extra", [
        {"recurring": True},
        {},                       # the tool's default is recurring
        {"recurring": "false"},   # not the explicit boolean
        {"recurring": 0},
        {"recurring": None},
    ], ids=["true", "missing", "string", "zero", "null"])
    def test_recurring_or_ambiguous_keeps_the_rule(self, tmp_path, extra):
        reason = _eval(tmp_path, _payload("CronCreate", dict(_CRON, **extra)))
        assert reason is not None and "/loop or /schedule" in reason

    def test_one_shot_in_a_subagent_is_still_refused(self, tmp_path):
        reason = _eval(tmp_path, _payload(
            "CronCreate", dict(_CRON, recurring=False), agent_id="a1"))
        assert reason is not None and "inside a subagent" in reason

    def test_recurring_after_user_typed_loop_is_still_allowed(self, tmp_path):
        assert wg.record_expansion_event(
            {"session_id": "s-v02105", "expansion_type": "slash_command",
             "command_name": "loop"},
            env={}, state_root=tmp_path / "state", now=999.0)
        assert _eval(tmp_path, _payload(
            "CronCreate", dict(_CRON, recurring=True))) is None

    def test_schedule_wakeup_is_not_affected(self, tmp_path):
        assert _eval(tmp_path, _payload(
            "ScheduleWakeup", {"recurring": False})) is not None


# ═══════════════════════════════════════════════════════════════════════
# 2. Sampling loops stay banned
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("command", [
    "while ! ss -tlnp | grep -q 8080; do ss -s; sleep 0.3; done",
    "while true; do ss -tn | wc -l; sleep 0.3; done",
    "for i in $(seq 40); do ss -s >> samples.txt; sleep 1; done",
])
def test_sampling_loops_are_still_refused(tmp_path, command):
    reason = _eval(tmp_path, _payload("Bash", {"command": command}))
    assert reason is not None and "`sleep`" in reason


# ═══════════════════════════════════════════════════════════════════════
# 3. Script files
# ═══════════════════════════════════════════════════════════════════════

POLL_SH = "#!/bin/bash\nfor i in $(seq 180); do\n  ssh host true\n  sleep 15\ndone\n"
OK_SH = "#!/bin/bash\nset -euo pipefail\nmake build\nsleep 2\nmake test\n"
ONE_SLEEP_SH = "sleep 30\n"
RETRY_FN_SH = (
    "#!/bin/bash\nfetch() { curl -s \"$1\" || { sleep 1; curl -s \"$1\"; }; }\n"
    "for u in a b c; do fetch \"$u\"; done\n")
BUSY_SH = "#!/bin/sh\nuntil curl -s localhost:8080; do :; done\n"
WATCH_SH = "#!/bin/bash\nwatch -n 5 kubectl get pods\n"


@pytest.fixture
def scripts(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    for name, body in [("poll.sh", POLL_SH), ("ok.sh", OK_SH),
                       ("one.sh", ONE_SLEEP_SH), ("retry.sh", RETRY_FN_SH),
                       ("busy.sh", BUSY_SH), ("watch.sh", WATCH_SH)]:
        (d / name).write_text(body, encoding="utf-8")
        (d / name).chmod((d / name).stat().st_mode | stat.S_IXUSR)
    (d / "outer.sh").write_text("#!/bin/bash\necho start\nbash poll.sh\n",
                                encoding="utf-8")
    (d / "pyloop").write_text(
        "#!/usr/bin/env python3\nimport time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8")
    (d / "pyloop").chmod(0o755)
    return d


def _bash_eval(tmp_path, cwd, command, **extra):
    return _eval(tmp_path, _payload("Bash", {"command": command, **extra},
                                    cwd=str(cwd)))


class TestScriptFiles:
    @pytest.mark.parametrize("command", [
        "bash poll.sh",
        "sh poll.sh",
        "bash -x -o pipefail poll.sh arg1",
        "./poll.sh",
        "source poll.sh",
        ". ./poll.sh",
        "cat poll.sh | bash",
        "timeout 3600 bash poll.sh",
        "make && bash poll.sh && echo done",
        "bash busy.sh",
        "bash watch.sh",
        "bash outer.sh",          # a script that runs the polling script
        "for h in a b; do bash one.sh; done",  # a single pause, run per item
    ])
    def test_a_polling_script_is_refused(self, tmp_path, scripts, command):
        reason = _bash_eval(tmp_path, scripts, command)
        assert reason is not None, command
        assert "in script" in reason

    def test_the_absolute_path_resolves_without_cwd(self, tmp_path, scripts):
        reason = _eval(tmp_path, _payload(
            "Bash", {"command": f"bash {scripts / 'poll.sh'}"}))
        assert reason is not None and "in script" in reason

    @pytest.mark.parametrize("command", [
        "bash ok.sh",
        "./ok.sh",
        "source ok.sh",
        "bash one.sh",        # a single pause in a script is not a poll
        "bash retry.sh",      # a retry pause in a helper a loop calls
        "bash missing.sh",
        "./pyloop",           # not a shell script: never judged
        "bash -c 'echo hi'",
    ])
    def test_a_non_polling_script_is_allowed(self, tmp_path, scripts, command):
        assert _bash_eval(tmp_path, scripts, command) is None, command

    def test_the_background_exemption_stays(self, tmp_path, scripts):
        assert _bash_eval(tmp_path, scripts, "bash poll.sh",
                          run_in_background=True) is None

    def test_a_file_over_the_size_bound_passes(self, tmp_path, scripts):
        big = scripts / "big.sh"
        big.write_text(POLL_SH + "#" * (wg.SCRIPT_MAX_BYTES + 1) + "\n",
                       encoding="utf-8")
        assert _bash_eval(tmp_path, scripts, "bash big.sh") is None

    def test_a_binary_file_passes(self, tmp_path, scripts):
        (scripts / "bin.sh").write_bytes(POLL_SH.encode() + b"\0\x01")
        assert _bash_eval(tmp_path, scripts, "bash bin.sh") is None

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                        reason="chmod 000 does not deny reads here")
    def test_an_unreadable_file_passes(self, tmp_path, scripts):
        p = scripts / "secret.sh"
        p.write_text(POLL_SH, encoding="utf-8")
        p.chmod(0)
        try:
            assert _bash_eval(tmp_path, scripts, "bash secret.sh") is None
        finally:
            p.chmod(0o600)

    def test_a_shebangless_direct_run_needs_a_sh_name(self, tmp_path, scripts):
        (scripts / "noext").write_text("while true; do sleep 1; done\n",
                                       encoding="utf-8")
        (scripts / "noext").chmod(0o755)
        assert _bash_eval(tmp_path, scripts, "./noext") is None
        # …while `bash noext` names the interpreter: judged.
        assert _bash_eval(tmp_path, scripts, "bash noext") is not None

    def test_without_a_cwd_the_classifier_reads_no_file(self, scripts):
        # The pure entry point stays pure for callers that name no cwd.
        assert wg.shell_static_pattern("Bash", "bash poll.sh") is None
        assert wg.shell_static_pattern(
            "Bash", "bash poll.sh", cwd=str(scripts)) is not None

    def test_a_variable_path_is_never_guessed(self, tmp_path, scripts):
        assert _bash_eval(tmp_path, scripts, "bash $D/poll.sh") is None


POLL_PS1 = "while ($true) {\n    Get-Process foo\n    Start-Sleep -Seconds 15\n}\n"
OK_PS1 = 'Write-Output "building"\nStart-Sleep -Seconds 2\nGet-ChildItem\n'


class TestPowerShellScriptFiles:
    @pytest.fixture
    def ps(self, tmp_path):
        d = tmp_path / "ps"
        d.mkdir()
        (d / "poll.ps1").write_text(POLL_PS1, encoding="utf-8")
        (d / "ok.ps1").write_text(OK_PS1, encoding="utf-8")
        (d / "poll.sh").write_text(POLL_SH, encoding="utf-8")
        return d

    def _ps(self, tmp_path, cwd, command, **extra):
        return _eval(tmp_path, _payload("PowerShell", {"command": command, **extra},
                                        cwd=str(cwd)))

    @pytest.mark.parametrize("command", [
        "./poll.ps1", "& ./poll.ps1", ". ./poll.ps1", "pwsh -File poll.ps1",
        "powershell -NoProfile -File ./poll.ps1", "bash poll.sh",
    ])
    def test_a_polling_script_is_refused(self, tmp_path, ps, command):
        reason = self._ps(tmp_path, ps, command)
        assert reason is not None and "in script" in reason, command

    @pytest.mark.parametrize("command", ["./ok.ps1", "pwsh -File ok.ps1",
                                         "./missing.ps1"])
    def test_a_non_polling_script_is_allowed(self, tmp_path, ps, command):
        assert self._ps(tmp_path, ps, command) is None

    def test_the_background_exemption_stays(self, tmp_path, ps):
        assert self._ps(tmp_path, ps, "./poll.ps1", run_in_background=True) is None


# ═══════════════════════════════════════════════════════════════════════
# 4. No-op wait loops
# ═══════════════════════════════════════════════════════════════════════

class TestNoopWaitLoop:
    def _call(self, tmp_path, command, i, now, tool="Bash", agent=None):
        extra = {"tool_use_id": f"tu-{i}"}
        if agent:
            extra["agent_id"] = agent
        return _eval(tmp_path, _payload(tool, {"command": command}, **extra), now=now)

    def test_a_single_true_passes(self, tmp_path):
        assert self._call(tmp_path, "true", 1, 1000.0) is None

    @pytest.mark.parametrize("command", ["true", ":", "echo waiting for build",
                                         "true; true", "exit 0", "exit",
                                         "printf waiting"])
    def test_the_third_identical_noop_is_refused(self, tmp_path, command):
        assert self._call(tmp_path, command, 1, 1000.0) is None
        assert self._call(tmp_path, command, 2, 1030.0) is None
        reason = self._call(tmp_path, command, 3, 1060.0)
        assert reason is not None
        assert "3rd identical no-op" in reason
        assert "end your turn; the completion notification wakes you" in reason
        assert "VCO_WAIT_GUARD=off" in reason

    def test_sleep_zero_is_refused_on_its_own(self, tmp_path):
        assert self._call(tmp_path, "sleep 0", 1, 1000.0) is not None

    def test_calls_spread_beyond_the_window_pass(self, tmp_path):
        for i, t in enumerate((1000.0, 1100.0, 1200.0, 1300.0)):
            assert self._call(tmp_path, "true", i, t) is None

    def test_different_noops_do_not_accumulate(self, tmp_path):
        for i, c in enumerate(["true", ":", "echo a", "echo b"]):
            assert self._call(tmp_path, c, i, 1000.0 + i) is None

    @pytest.mark.parametrize("command", [
        "true && ls", "echo hi > out.txt", "echo $(date)", "echo a | cat",
        "printf x > out.txt", "printf x | cat", "exit 1", "exit 0 && make",
        "make",
    ])
    def test_a_command_that_does_work_is_never_counted(self, tmp_path, command):
        assert wg.noop_repeat_key(command) is None
        for i in range(4):
            assert self._call(tmp_path, command, i, 1000.0 + i) is None

    def test_one_call_fanned_out_counts_once(self, tmp_path):
        for _ in range(5):  # five if-registrations, one tool_use_id
            assert self._call(tmp_path, "true", 7, 1000.0) is None

    def test_each_agent_counts_on_its_own(self, tmp_path):
        assert self._call(tmp_path, "true", 1, 1000.0, agent="a1") is None
        assert self._call(tmp_path, "true", 2, 1001.0, agent="a1") is None
        assert self._call(tmp_path, "true", 3, 1002.0, agent="a2") is None

    def test_powershell_write_host_counts_too(self, tmp_path):
        for i in range(2):
            assert self._call(tmp_path, "Write-Host waiting", i, 1000.0 + i,
                              tool="PowerShell") is None
        assert self._call(tmp_path, "Write-Host waiting", 3, 1003.0,
                          tool="PowerShell") is not None

    def test_noop_state_never_prunes_status_check_stamps(self, tmp_path):
        # Regression: one per-actor state file, two windows (120 s / 600 s).
        # A no-op call 300 s after three status checks must not drop them.
        for i, t in enumerate((1000.0, 1001.0, 1002.0)):
            assert self._call(tmp_path, "tail -n 5 build.log", i, t) is None
        assert self._call(tmp_path, "true", 10, 1300.0) is None
        assert self._call(tmp_path, "tail -n 5 build.log", 11, 1301.0) is not None


# ═══════════════════════════════════════════════════════════════════════
# 5. R9 base-equal gaps
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("command", [
    "echo 'sleep 5' | bash",
    "echo 'sleep 5' | sh -s -- a",
    "printf 'sleep 5\\n' | bash",
    "echo 'sleep 5' | bash /dev/stdin",
    "echo 'sleep 5' | source /dev/stdin",
    "bash <<< 'sleep 5'",
    "bash <<'X'\nsleep 5\nX",
    "cat <<EOF | bash\nwhile true; do curl -s u; sleep 2; done\nEOF",
    "case x in a) sleep 5;; esac",
    "case $1 in a|b) sleep 5 ;; *) make;; esac",
    "case x in a) bash -c 'sleep 5';; esac",
    "coproc sleep 5",
    "coproc bash -c 'sleep 5'",
    "c='sleep 5'; eval \"$c\"",
    "c='sleep 5'; $c",
    "c='sleep 5'; bash -c \"$c\"",
    "export W='sleep 30'; eval $W",
    "w='while true; do curl -s u; sleep 1; done'\neval \"$w\"",
])
def test_r9_gaps_are_refused(tmp_path, command):
    assert _eval(tmp_path, _payload("Bash", {"command": command})) is not None, command


@pytest.mark.parametrize("command", [
    "cat > notes.sh <<'EOF'\nsleep 5\nEOF",       # heredoc DATA to a file
    "echo 'sleep 5' > f.txt",
    "echo 'sleep 5' | grep sleep",
    "case $1 in start) make; sleep 3; make test;; esac",  # a pause between actions
    "c='ls -la'; $c",
    "cmd='echo hi'; eval \"$cmd\"",
    "c=$(get-cmd); eval \"$c\"",                    # non-literal: never guessed
    "c='sleep 5'; echo \"$c\"",                     # printed, not run
    "echo hi | bash -c 'cat'",
])
def test_r9_lookalikes_are_allowed(tmp_path, command):
    assert _eval(tmp_path, _payload("Bash", {"command": command})) is None, command


# ═══════════════════════════════════════════════════════════════════════
# Shared helpers (walker / heredoc) — act and leave-alone
# ═══════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_split_chain_ops_reports_the_preceding_operator(self):
        toks = tokenize("a | b && c ; ; d") or []
        assert split_chain_ops(toks) == [
            (None, ["a"]), ("|", ["b"]), ("&&", ["c"]), (";", ["d"])]
        assert split_chain(toks) == [["a"], ["b"], ["c"], ["d"]]

    def test_split_chain_ops_keeps_the_arith_header_whole(self):
        toks = tokenize("for (( i=0 ; i<3 ; i++ )); do x; done") or []
        assert [seg for _op, seg in split_chain_ops(toks)] == split_chain(toks)

    @pytest.mark.parametrize("tokens,expected", [
        (["case", "x", "in", "a)", "sleep", "5"], ["sleep", "5"]),
        (["*)", "make"], ["make"]),
        (["(a|b)", "make"], ["make"]),
        (["esac"], []),
        (["make", "x)"], ["make", "x)"]),
        (["$(cmd)", "x"], ["$(cmd)", "x"]),
        ([")"], [")"]),
    ])
    def test_strip_case_arm(self, tokens, expected):
        assert strip_case_arm(tokens) == expected

    def test_strip_heredocs_inlines_only_when_asked(self):
        cmd = "bash <<'X'\nsleep 5\nX\necho after"
        plain, bodies = strip_heredocs(cmd)
        assert "sleep" not in plain and bodies == ["sleep 5"]
        inl, bodies2 = strip_heredocs(cmd, inline_when=lambda _line: True)
        assert "<<< 'sleep 5'" in inl and bodies2 == ["sleep 5"]
        kept, _ = strip_heredocs(cmd, inline_when=lambda _line: False)
        assert kept == plain

    def test_wrapper_header_names_the_v02105_rules(self):
        sh = (Path(__file__).resolve().parents[1] / "templates" / "hooks"
              / "wait-guard.sh").read_text(encoding="utf-8")
        assert "script file" in sh and "no-op" in sh and "recurring" in sh

    def test_cli_envelope_for_a_one_shot_cron_is_empty(self, tmp_path,
                                                       monkeypatch, capsys):
        import io
        monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("VCO_WAIT_GUARD", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(
            "CronCreate", dict(_CRON, recurring=False)))))
        assert wg.main(["pretooluse"]) == 0
        assert capsys.readouterr().out == ""
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_payload(
            "CronCreate", dict(_CRON, recurring=True)))))
        assert wg.main(["pretooluse"]) == 0
        out = capsys.readouterr().out
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# ═══════════════════════════════════════════════════════════════════════
# The shipped wrappers carry the same decisions (.sh and .ps1)
# ═══════════════════════════════════════════════════════════════════════

_REPO = Path(__file__).resolve().parents[1]


def _wrapper_cmds():
    import shutil
    import sys
    cmds = []
    if sys.platform != "win32" and shutil.which("bash"):
        cmds.append(("sh", ["bash", str(_REPO / "templates/hooks/wait-guard.sh"),
                            "pretooluse"]))
    pwsh = shutil.which("pwsh")
    if pwsh:
        from tests.common.pwsh import PWSH_BASE_FLAGS
        cmds.append(("ps1", [pwsh, *PWSH_BASE_FLAGS, "-File",
                             str(_REPO / "templates/hooks/wait-guard.ps1"),
                             "pretooluse"]))
    return cmds


@pytest.mark.parametrize("label,argv", _wrapper_cmds() or [
    pytest.param("none", [], marks=pytest.mark.skip(reason="no bash/pwsh"))])
def test_wrappers_judge_scripts_and_one_shot_cron(tmp_path, scripts, label, argv):
    import subprocess
    import sys
    from tests.common.child_env import child_env

    env = child_env()
    env.update({"VCT_STATE_DIR": str(tmp_path / "state"),
                "VCT_VENV": sys.executable, "PYTHONPATH": str(_REPO),
                "HOME": str(tmp_path / "home")})
    env.pop("VCO_WAIT_GUARD", None)

    def run(payload):
        proc = subprocess.run(argv, input=json.dumps(payload),
                              capture_output=True, text=True, env=env,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    denied = run(_payload("Bash", {"command": "bash poll.sh"}, cwd=str(scripts)))
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run(_payload("Bash", {"command": "bash ok.sh"}, cwd=str(scripts))) == ""
    assert run(_payload("CronCreate", dict(_CRON, recurring=False))) == ""
