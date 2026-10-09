# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W4-D — the wait-loop / polling guard.

Pins, per the lane brief (``plans/v02102-lanes/W4-D-wait-loop-guard.md``):

* ``vco_lib/wait_guard.py`` — the ONE decision module: static polling
  patterns on foreground Bash (``sleep`` anywhere as a VERB, ``watch``,
  ``tail -f``/``-F``, ``timeout ... bash -c '...sleep...'``, while/until
  loops via the sleep they contain), self-wake tools
  (ScheduleWakeup/CronCreate/Monitor: always denied in a subagent, in the
  main chat allowed only after the UserPromptSubmit recorder saw the USER
  type ``/loop`` or ``/schedule`` in that session), and the per-actor
  repeat detector (Nth identical status check within a window denied).
  ``run_in_background: true`` is the sanctioned trigger and stays allowed.
* The loud-fail contract (owner 2026-10-08): every deny reason states
  WHAT was blocked (command + pattern), WHY (each poll re-sends the whole
  conversation), WHAT TO DO INSTEAD (concrete for the case), and names
  ``VCO_WAIT_GUARD=off`` as a USER decision only.
* ``templates/hooks/wait-guard.{sh,ps1}`` — thin wrappers producing the
  SAME decision for the same stdin (executed; pwsh gated on availability).
* Registration in BOTH settings templates (the Bash ``if``-filtered group,
  Read, the self-wake matcher, the UserPromptSubmit recorder) plus the
  shipped ``skillOverrides`` (``loop`` → ``user-invocable-only``) merged
  user-wins through :func:`vco_lib.settings_merge.smart_merge_settings`.
* Env knobs read by the code (``VCO_WAIT_GUARD``, ``VCO_WAIT_GUARD_
  REPEAT_MAX``, ``VCO_WAIT_GUARD_REPEAT_WINDOW_S``) and documented.

The unlock recorder rides the DOCUMENTED event for a typed slash command:
**UserPromptExpansion** (vendor hooks doc, ``/tmp/ccdocs/hooks.md`` —
"typing /skillname directly bypasses PreToolUse. UserPromptExpansion fires
on that direct path. Matches on ``command_name``"), registered with matcher
``loop|schedule`` and recording from ``command_name`` +
``expansion_type == "slash_command"`` — never from a parse of prompt text.
``TestDocumentedExpansionShape`` pins the doc's input example verbatim.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
LINUX = TEMPLATES / "settings.json.linux.template"
WINDOWS = TEMPLATES / "settings.json.windows.template"
HOOK_SH = TEMPLATES / "hooks" / "wait-guard.sh"
HOOK_PS1 = TEMPLATES / "hooks" / "wait-guard.ps1"

ENV_OFF = {"VCO_WAIT_GUARD": "off"}
ENV_DEFAULT: dict = {}


def _payload(tool: str, tool_input: dict, **extra) -> dict:
    return {
        "session_id": "sess-1",
        "tool_name": tool,
        "tool_input": tool_input,
        **extra,
    }


def _bash(command: str, background: bool = False, **extra) -> dict:
    ti: dict = {"command": command}
    if background:
        ti["run_in_background"] = True
    return _payload("Bash", ti, **extra)


# ═══════════════════════════════════════════════════════════════════════
# Static polling patterns (foreground Bash)
# ═══════════════════════════════════════════════════════════════════════


class TestStaticPatterns:
    def _deny(self, tmp_path, command: str) -> str:
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _bash(command), env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0
        )
        assert reason, f"{command!r} must be denied in the foreground"
        return reason

    def _allow(self, tmp_path, command: str) -> None:
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _bash(command), env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0
        )
        assert reason is None, f"{command!r} must be allowed: {reason}"

    @pytest.mark.parametrize(
        "command",
        [
            "sleep 60",
            "sleep 60 && tail -n 40 /tmp/build.log",
            "cd /tmp && sleep 5",
            "bash -c 'sleep 30'",
            "watch kubectl get pods",
            "tail -f /tmp/app.log",
            "tail -F /tmp/app.log",
            "tail --follow /tmp/app.log",
            "while true; do tail -n 1 /tmp/app.log; sleep 5; done",
            "until curl -s localhost:8080; do sleep 2; done",
            "timeout 30 bash -c 'sleep 30'",
            "timeout 30 bash -c 'while true; do sleep 1; done'",
        ],
    )
    def test_foreground_polling_denied(self, tmp_path, command):
        self._deny(tmp_path, command)

    @pytest.mark.parametrize(
        "command",
        [
            "sleep 60",
            "sleep 60 && tail -n 40 /tmp/build.log",
            "watch -n 5 kubectl get pods",
            "tail -f /tmp/app.log",
            "timeout 30 bash -c 'sleep 30'",
        ],
    )
    def test_run_in_background_is_sanctioned_and_allowed(self, tmp_path, command):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _bash(command, background=True),
            env=dict(ENV_DEFAULT),
            state_root=tmp_path,
            now=1000.0,
        )
        assert reason is None, f"background {command!r} must stay allowed: {reason}"

    @pytest.mark.parametrize(
        "command",
        [
            'echo "sleep 5"',          # quoted text, not a verb — the brief's case
            "echo please do not sleep now",
            "tail -n 20 /tmp/app.log",  # plain tail: not follow, first occurrence
            "timeout 60 make test",     # timeout alone is not a polling shape
            "cat /tmp/app.log",
            "ls -la /tmp",
        ],
    )
    def test_benign_commands_allowed(self, tmp_path, command):
        self._allow(tmp_path, command)

    def test_deny_reason_names_the_pattern_and_the_fix(self, tmp_path):
        reason = self._deny(tmp_path, "sleep 60 && tail -n 40 /tmp/build.log")
        assert "sleep" in reason
        assert "run_in_background" in reason
        assert "END YOUR TURN" in reason
        assert "notification" in reason


# ═══════════════════════════════════════════════════════════════════════
# Self-wake tools (ScheduleWakeup / CronCreate / Monitor)
# ═══════════════════════════════════════════════════════════════════════


class TestSelfWakeTools:
    @pytest.mark.parametrize("tool", ["ScheduleWakeup", "CronCreate", "Monitor"])
    def test_denied_inside_subagent(self, tmp_path, tool):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _payload(tool, {"delay": "1h"}, agent_id="agent-7"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        assert reason, f"{tool} in a subagent must be denied"
        assert "subagent" in reason
        assert "END YOUR TURN" in reason

    def test_denied_in_main_chat_without_recorded_loop(self, tmp_path):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _payload("ScheduleWakeup", {"delay": "1h"}),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        assert reason, "self-wake in the main chat without /loop must be denied"
        assert "/loop" in reason
        assert "END YOUR TURN" in reason

    @pytest.mark.parametrize("command_name", ["loop", "schedule"])
    def test_allowed_after_recorder_saw_user_slash_command(
        self, tmp_path, command_name
    ):
        from vco_lib import wait_guard as wg

        recorded = wg.record_expansion_event(
            {
                "session_id": "sess-1",
                "expansion_type": "slash_command",
                "command_name": command_name,
            },
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        assert recorded, f"/{command_name} must be recorded"
        reason = wg.evaluate(
            _payload("ScheduleWakeup", {"delay": "1h"}),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        )
        assert reason is None, (
            f"after the user typed /{command_name} the session must be unlocked"
        )

    def test_mcp_prompt_expansion_does_not_unlock(self, tmp_path):
        from vco_lib import wait_guard as wg

        recorded = wg.record_expansion_event(
            {
                "session_id": "sess-1",
                "expansion_type": "mcp_prompt",
                "command_name": "loop",
            },
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        assert not recorded, "an MCP prompt named loop is not the /loop command"
        reason = wg.evaluate(
            _payload("ScheduleWakeup", {"delay": "1h"}),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        )
        assert reason, "only a typed slash command unlocks"

    def test_a_different_session_is_not_unlocked(self, tmp_path):
        from vco_lib import wait_guard as wg

        wg.record_expansion_event(
            {
                "session_id": "sess-1",
                "expansion_type": "slash_command",
                "command_name": "loop",
            },
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        reason = wg.evaluate(
            _payload("ScheduleWakeup", {"delay": "1h"}, session_id="sess-2"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        )
        assert reason, "the unlock is per session_id"


class TestDocumentedExpansionShape:
    """The recorder is pinned to the vendor-documented UserPromptExpansion
    input (hooks.md 'UserPromptExpansion input'): session_id (common),
    expansion_type ('slash_command' | 'mcp_prompt'), command_name,
    command_args, command_source, prompt. Recording keys on command_name —
    a payload with NO prompt field at all must still unlock (the doc does
    not define a slash-command shape for the prompt string, so nothing may
    depend on it)."""

    #: The doc's example payload, adapted to /loop (same fields verbatim).
    DOC_PAYLOAD = {
        "session_id": "abc123",
        "transcript_path": "/Users/.../00893aaf.jsonl",
        "cwd": "/Users/...",
        "permission_mode": "default",
        "hook_event_name": "UserPromptExpansion",
        "expansion_type": "slash_command",
        "command_name": "loop",
        "command_args": "check the build",
        "command_source": "plugin",
        "prompt": "/loop check the build",
    }

    def test_documented_payload_unlocks_without_prompt_field(self, tmp_path):
        from vco_lib import wait_guard as wg

        no_prompt = {k: v for k, v in self.DOC_PAYLOAD.items() if k != "prompt"}
        assert wg.record_expansion_event(
            no_prompt, env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0
        )
        assert wg.evaluate(
            {"session_id": "abc123", "tool_name": "CronCreate", "tool_input": {}},
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        ) is None

    def test_documented_payload_verbatim_unlocks(self, tmp_path):
        from vco_lib import wait_guard as wg

        assert wg.record_expansion_event(
            self.DOC_PAYLOAD, env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0
        )
        assert wg.evaluate(
            {"session_id": "abc123", "tool_name": "Monitor", "tool_input": {}},
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        ) is None

    def test_other_command_name_does_not_unlock(self, tmp_path):
        from vco_lib import wait_guard as wg

        payload = dict(self.DOC_PAYLOAD, command_name="example-skill",
                       prompt="/example-skill arg1 arg2")
        assert not wg.record_expansion_event(
            payload, env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0
        )
        assert wg.evaluate(
            {"session_id": "abc123", "tool_name": "Monitor", "tool_input": {}},
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1010.0,
        )


# ═══════════════════════════════════════════════════════════════════════
# Repeat detector
# ═══════════════════════════════════════════════════════════════════════


class TestRepeatDetector:
    def _check(self, tmp_path, tool_input, tool="Bash", now=1000.0):
        from vco_lib import wait_guard as wg

        return wg.evaluate(
            _payload(tool, tool_input),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=now,
        )

    def test_three_identical_allowed_fourth_denied(self, tmp_path):
        cmd = {"command": "tail -n 20 /tmp/app.log"}
        assert self._check(tmp_path, cmd, now=1000.0) is None
        assert self._check(tmp_path, cmd, now=1002.0) is None
        assert self._check(tmp_path, cmd, now=1004.0) is None
        reason = self._check(tmp_path, cmd, now=1006.0)
        assert reason, "the 4th identical check within the window must be denied"
        assert "status check" in reason
        assert "4th" in reason
        assert "run_in_background" in reason or "notification" in reason
        assert "END YOUR TURN" in reason

    def test_different_targets_never_accumulate(self, tmp_path):
        a = {"command": "tail -n 20 /tmp/a.log"}
        b = {"command": "tail -n 20 /tmp/b.log"}
        for i in range(3):
            assert self._check(tmp_path, a, now=1000.0 + i) is None
            assert self._check(tmp_path, b, now=1000.0 + i) is None

    def test_window_expiry_resets_the_count(self, tmp_path):
        cmd = {"command": "tail -n 20 /tmp/app.log"}
        window = 600
        assert self._check(tmp_path, cmd, now=1000.0) is None
        assert self._check(tmp_path, cmd, now=1002.0) is None
        assert self._check(tmp_path, cmd, now=1004.0) is None
        # Far past the window: the earlier checks pruned away.
        late = 1000.0 + window + 60
        assert self._check(tmp_path, cmd, now=late) is None

    def test_read_same_file_offset_limit_denied_on_fourth(self, tmp_path):
        ti = {"file_path": "/tmp/app.log", "offset": 1, "limit": 50}
        assert self._check(tmp_path, ti, tool="Read", now=1000.0) is None
        assert self._check(tmp_path, ti, tool="Read", now=1002.0) is None
        assert self._check(tmp_path, ti, tool="Read", now=1004.0) is None
        assert self._check(tmp_path, ti, tool="Read", now=1006.0), (
            "the 4th identical Read of the same offset/limit must be denied"
        )

    def test_read_different_offset_is_a_different_check(self, tmp_path):
        for i in range(4):
            ti = {"file_path": "/tmp/app.log", "offset": i * 50, "limit": 50}
            assert self._check(tmp_path, ti, tool="Read", now=1000.0 + i) is None

    def test_repeat_max_knob_changes_the_threshold(self, tmp_path):
        from vco_lib import wait_guard as wg

        env = {"VCO_WAIT_GUARD_REPEAT_MAX": "2"}
        cmd = {"command": "tail -n 20 /tmp/app.log"}
        assert wg.evaluate(_payload("Bash", cmd), env=env, state_root=tmp_path,
                           now=1000.0) is None
        assert wg.evaluate(_payload("Bash", cmd), env=env, state_root=tmp_path,
                           now=1001.0), "max=2 must deny the 2nd identical check"

    def test_repeat_window_knob_shortens_the_memory(self, tmp_path):
        from vco_lib import wait_guard as wg

        env = {"VCO_WAIT_GUARD_REPEAT_WINDOW_S": "10"}
        cmd = {"command": "tail -n 20 /tmp/app.log"}
        for i, t in enumerate((1000.0, 1001.0, 1002.0)):
            assert wg.evaluate(_payload("Bash", cmd), env=env, state_root=tmp_path,
                               now=t) is None
        # 60 s later with a 10 s window: everything pruned, still allowed.
        assert wg.evaluate(_payload("Bash", cmd), env=env, state_root=tmp_path,
                           now=1060.0) is None


# ═══════════════════════════════════════════════════════════════════════
# Kill switch
# ═══════════════════════════════════════════════════════════════════════


class TestKillSwitch:
    @pytest.mark.parametrize("value", ["off", "0", "false"])
    def test_off_disables_everything(self, tmp_path, value):
        from vco_lib import wait_guard as wg

        env = {"VCO_WAIT_GUARD": value}
        assert wg.evaluate(_bash("sleep 60"), env=env, state_root=tmp_path,
                           now=1000.0) is None
        assert wg.evaluate(
            _payload("ScheduleWakeup", {}, agent_id="a"), env=env,
            state_root=tmp_path, now=1000.0) is None
        cmd = {"command": "tail -n 1 x"}
        for i in range(6):
            assert wg.evaluate(_payload("Bash", cmd), env=env, state_root=tmp_path,
                               now=1000.0 + i) is None


# ═══════════════════════════════════════════════════════════════════════
# Loud-fail: every deny message carries (a)-(d)
# ═══════════════════════════════════════════════════════════════════════


class TestDenyMessagesTeach:
    """Owner 2026-10-08: each reason states WHAT (quoted offender + pattern),
    WHY (token cost of re-sending the conversation), WHAT TO DO INSTEAD
    (concrete), and names the knob as a USER decision only."""

    def _assert_all_four(self, reason: object, offender: str) -> None:
        assert isinstance(reason, str), "expected a deny reason"
        assert offender in reason, f"must quote the offender ({offender})"
        assert "token" in reason.lower(), "must state the token cost"
        assert "notification" in reason, "must say waiting is done by notification"
        assert "END YOUR TURN" in reason, "must say to end the turn"
        assert "VCO_WAIT_GUARD=off" in reason, "must name the kill switch"
        assert "user" in reason.lower(), (
            "the kill switch must be marked a USER decision, not an agent action"
        )

    def test_static_message(self, tmp_path):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(_bash("sleep 60"), env=dict(ENV_DEFAULT),
                             state_root=tmp_path, now=1000.0)
        self._assert_all_four(reason, "sleep 60")

    def test_self_wake_subagent_message(self, tmp_path):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _payload("CronCreate", {"schedule": "*/5 * * * *"}, agent_id="a1"),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        self._assert_all_four(reason, "CronCreate")

    def test_self_wake_main_message(self, tmp_path):
        from vco_lib import wait_guard as wg

        reason = wg.evaluate(
            _payload("Monitor", {"condition": "file exists"}),
            env=dict(ENV_DEFAULT), state_root=tmp_path, now=1000.0,
        )
        self._assert_all_four(reason, "Monitor")

    def test_repeat_message(self, tmp_path):
        from vco_lib import wait_guard as wg

        cmd = {"command": "cat /tmp/app.log"}
        for t in (1000.0, 1001.0, 1002.0):
            assert wg.evaluate(_payload("Bash", cmd), env=dict(ENV_DEFAULT),
                               state_root=tmp_path, now=t) is None
        reason = wg.evaluate(_payload("Bash", cmd), env=dict(ENV_DEFAULT),
                             state_root=tmp_path, now=1003.0)
        self._assert_all_four(reason, "cat /tmp/app.log")


# ═══════════════════════════════════════════════════════════════════════
# CLI (python -m vco_lib.wait_guard)
# ═══════════════════════════════════════════════════════════════════════


def _cli(tmp_path, mode: str, stdin_text: str):
    from tests.common.child_env import child_env

    env = child_env()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "VCT_STATE_DIR": str(tmp_path / "state"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.wait_guard", mode],
        input=stdin_text, capture_output=True, text=True, env=env, timeout=60,
    )


class TestCli:
    def test_deny_prints_pretooluse_deny_envelope(self, tmp_path):
        proc = _cli(
            tmp_path, "pretooluse",
            json.dumps(_bash("sleep 90 && tail /tmp/x.log")),
        )
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        hso = out["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert "run_in_background" in hso["permissionDecisionReason"]
        assert proc.stderr.startswith("[wait-guard]"), (
            "the deny must ALSO write one stderr line for the transcript"
        )

    def test_benign_command_prints_nothing(self, tmp_path):
        proc = _cli(tmp_path, "pretooluse", json.dumps(_bash("ls /tmp")))
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_bad_json_allows_loudly_without_blocking(self, tmp_path):
        proc = _cli(tmp_path, "pretooluse", "{not json")
        assert proc.returncode == 0, "an internal error must never block the tool"
        assert proc.stdout == ""
        assert "wait-guard" in proc.stderr

    def test_record_expansion_unlocks_self_wake(self, tmp_path):
        proc = _cli(
            tmp_path, "record-expansion",
            json.dumps(
                {
                    "session_id": "sess-cli",
                    "expansion_type": "slash_command",
                    "command_name": "loop",
                }
            ),
        )
        assert proc.returncode == 0
        proc2 = _cli(
            tmp_path, "pretooluse",
            json.dumps(
                _payload("ScheduleWakeup", {"delay": "1h"}, session_id="sess-cli")
            ),
        )
        assert proc2.returncode == 0
        assert proc2.stdout == "", "the recorded /loop must unlock the session"


# ═══════════════════════════════════════════════════════════════════════
# Thin wrappers (.sh + .ps1 produce the SAME decision)
# ═══════════════════════════════════════════════════════════════════════


def _wrapper_env(tmp_path: Path) -> dict:
    from tests.common.child_env import child_env

    env = child_env()
    env.update(
        {
            "VCT_STATE_DIR": str(tmp_path / "state"),
            "VCT_VENV": sys.executable,
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path / "home"),
        }
    )
    return env


class TestWrapperParity:
    DENY_STDIN = json.dumps(_bash("sleep 45"))
    ALLOW_STDIN = json.dumps(_bash("ls /tmp"))

    def test_sh_wrapper_denies(self, tmp_path):
        if shutil.which("bash") is None:
            pytest.skip("no bash")
        proc = subprocess.run(
            ["bash", str(HOOK_SH), "pretooluse"],
            input=self.DENY_STDIN, capture_output=True, text=True,
            env=_wrapper_env(tmp_path), timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        hso = json.loads(proc.stdout)["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"

    def test_sh_wrapper_allows(self, tmp_path):
        if shutil.which("bash") is None:
            pytest.skip("no bash")
        proc = subprocess.run(
            ["bash", str(HOOK_SH), "pretooluse"],
            input=self.ALLOW_STDIN, capture_output=True, text=True,
            env=_wrapper_env(tmp_path), timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_ps1_wrapper_same_decision_as_sh(self, tmp_path):
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            pytest.skip("pwsh absent (recorded in the lane report)")
        for stdin_text, want_deny in (
            (self.DENY_STDIN, True),
            (self.ALLOW_STDIN, False),
        ):
            sh = subprocess.run(
                ["bash", str(HOOK_SH), "pretooluse"],
                input=stdin_text, capture_output=True, text=True,
                env=_wrapper_env(tmp_path), timeout=60,
            )
            ps = subprocess.run(
                [pwsh, *PWSH_BASE_FLAGS, "-File", str(HOOK_PS1), "pretooluse"],
                input=stdin_text, capture_output=True, text=True,
                env=_wrapper_env(tmp_path), timeout=120,
            )
            assert ps.returncode == 0, ps.stderr
            assert sh.returncode == 0, sh.stderr
            if want_deny:
                assert json.loads(ps.stdout)["hookSpecificOutput"][
                    "permissionDecision"
                ] == "deny"
                assert json.loads(sh.stdout)["hookSpecificOutput"][
                    "permissionDecision"
                ] == "deny"
            else:
                assert ps.stdout == ""
                assert sh.stdout == ""

    def test_ps1_is_pure_ascii(self):
        HOOK_PS1.read_bytes().decode("ascii")

    def test_sh_scrub_precedes_both_guards(self):
        text = HOOK_SH.read_text(encoding="utf-8")
        lines = text.splitlines()
        code = [
            (i, ln) for i, ln in enumerate(lines)
            if ln.strip() and not ln.strip().startswith("#")
        ]
        scrub = next(i for i, ln in code if "unset" in ln and "GITHUB_TOKEN" in ln)
        disable = next(
            i for i, ln in code if "VCT_DISABLE_HOOKS" in ln and "exit 0" in ln
        )
        wait_off = next(i for i, ln in code if "VCO_WAIT_GUARD" in ln)
        assert scrub < disable < wait_off


# ═══════════════════════════════════════════════════════════════════════
# Settings registrations + skillOverrides merge
# ═══════════════════════════════════════════════════════════════════════

#: The Bash if-prefilter rules: one per pattern/verb class the guard can
#: deny, verb-anchored like the v0.2.101 §C1 shapes (leading-wildcard `if`
#: support could not be verified — the vendor doc download was absent — so
#: the harness prefilter covers the verb-anchored shapes and the MODULE's
#: own analysis still checks the whole command).
WAIT_GUARD_BASH_RULES = [
    "Bash(sleep *)",
    "Bash(watch *)",
    "Bash(tail *)",
    "Bash(while *)",
    "Bash(until *)",
    "Bash(timeout *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(ls *)",
    "Bash(wc *)",
    "Bash(grep *)",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_guard_groups(doc: dict, event: str, matcher: str | None = None):
    out = []
    for g in doc.get("hooks", {}).get(event, []):
        cmds = [h.get("command", "") for h in g.get("hooks", [])]
        if any("wait-guard" in c for c in cmds):
            if matcher is None or g.get("matcher") == matcher:
                out.append(g)
    return out


@pytest.mark.parametrize("tpl", [LINUX, WINDOWS], ids=["linux", "windows"])
class TestRegistrations:
    def test_bash_group_if_rules(self, tpl):
        # v0.2.102 W5-D: the group matches Bash|PowerShell; its Bash rules
        # come first and stay exactly these (the PowerShell twins are pinned
        # in tests/test_v02102_powershell_hooks.py).
        (g,) = _wait_guard_groups(_load(tpl), "PreToolUse", "Bash|PowerShell")
        rules = [h.get("if") for h in g["hooks"] if h.get("if", "").startswith("Bash(")]
        assert rules == WAIT_GUARD_BASH_RULES
        for h in g["hooks"]:
            assert h.get("timeout") == 10
            assert h.get("type") == "command"

    def test_read_registration(self, tpl):
        (g,) = _wait_guard_groups(_load(tpl), "PreToolUse", "Read")
        (h,) = g["hooks"]
        assert h.get("if") == "Read(*)"
        assert h.get("timeout") == 10

    def test_self_wake_matcher_registration(self, tpl):
        (g,) = _wait_guard_groups(_load(tpl), "PreToolUse",
                                  "ScheduleWakeup|CronCreate|Monitor")
        (h,) = g["hooks"]
        assert h.get("timeout") == 10

    def test_user_prompt_expansion_recorder_registration(self, tpl):
        groups = _wait_guard_groups(_load(tpl), "UserPromptExpansion")
        assert len(groups) == 1
        (g,) = groups
        assert g.get("matcher") == "loop|schedule", (
            "the documented event matches on command_name; the matcher IS "
            "the prefilter"
        )
        (h,) = g["hooks"]
        assert "record-expansion" in h["command"]
        assert h.get("timeout") == 10

    def test_no_user_prompt_submit_recorder_survives(self, tpl):
        doc = _load(tpl)
        assert _wait_guard_groups(doc, "UserPromptSubmit") == [], (
            "the unlock recorder lives on UserPromptExpansion (the documented "
            "path for a typed slash command), not on UserPromptSubmit"
        )

    def test_skilloverrides_shipped(self, tpl):
        assert _load(tpl).get("skillOverrides") == {"loop": "user-invocable-only"}


class TestSettingsMergeSkillOverrides:
    """Owner decision 1: merged like every shipped settings key — the user's
    own ``skillOverrides`` entries survive, ``loop`` is only ADDED when
    absent, a user value for ``loop`` wins."""

    def _template(self) -> dict:
        return _load(LINUX)

    def test_added_when_absent(self):
        from vco_lib.settings_merge import smart_merge_settings

        merged = smart_merge_settings({}, self._template())
        assert merged["skillOverrides"] == {"loop": "user-invocable-only"}

    def test_user_entries_untouched_and_loop_added(self):
        from vco_lib.settings_merge import smart_merge_settings

        merged = smart_merge_settings(
            {"skillOverrides": {"my-skill": "ask"}}, self._template()
        )
        assert merged["skillOverrides"] == {
            "my-skill": "ask",
            "loop": "user-invocable-only",
        }

    def test_user_loop_value_wins(self):
        from vco_lib.settings_merge import smart_merge_settings

        merged = smart_merge_settings(
            {"skillOverrides": {"loop": "enabled"}}, self._template()
        )
        assert merged["skillOverrides"] == {"loop": "enabled"}

    def test_linux_windows_hooks_structure_parity(self):
        def _sig(doc):
            sig = []
            for event, groups in doc.get("hooks", {}).items():
                for g in groups:
                    for h in g.get("hooks", []):
                        sig.append(
                            (event, g.get("matcher"), h.get("timeout"), h.get("if"))
                        )
            return sig

        assert _sig(_load(LINUX)) == _sig(_load(WINDOWS))


# ═══════════════════════════════════════════════════════════════════════
# Docs
# ═══════════════════════════════════════════════════════════════════════


class TestDocs:
    def test_changelog_unreleased_entry(self):
        text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = text.split("## [Unreleased]", 1)[1].split("\n## ", 1)[0]
        assert "wait-loop" in unreleased.lower()
        assert "VCO_WAIT_GUARD" in unreleased

    def test_configuration_documents_the_knobs(self):
        text = (REPO_ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        for knob in (
            "VCO_WAIT_GUARD",
            "VCO_WAIT_GUARD_REPEAT_MAX",
            "VCO_WAIT_GUARD_REPEAT_WINDOW_S",
        ):
            assert knob in text, f"{knob} must be documented"

    def test_claude_template_mentions_the_guard(self):
        text = (TEMPLATES / "CLAUDE.md.template").read_text(encoding="utf-8")
        assert "wait-loop guard" in text
        assert "run_in_background" in text
