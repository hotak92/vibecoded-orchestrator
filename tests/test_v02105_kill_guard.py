# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 KILL-GUARD: refuse kills that can take down the user's session.

On 2026-10-23 a session ran ``kill -9 $p $(ps -o ppid= -p $p)`` on a leaked
test process; it was an orphan, its parent was ``systemd --user``, and the
whole desktop session went down. Two layers, one home
(``vco_lib/kill_guard.py`` + ``vco_lib/kill_guard_runtime.py``):

* layer 1, static deny, called by the pre-tool-use scanner
  (``templates/scripts/bash_security.py`` -> ``_kill_guard_check``);
* layer 2, runtime guard, a rewrite emitted by ``lean-ctx-rewrite.{sh,ps1}``
  (the one ``updatedInput`` emitter for shell calls) that routes every kill
  through ``python -m vco_lib.kill_guard check``.

SAFETY OF THIS FILE: no test targets a real critical pid. The runtime
decisions about pid 1, ``systemd`` and the desktop processes run against
INJECTED process tables / a ``/proc`` fixture tree. The end-to-end runs only
ever signal processes this test started: a ``sleep`` child, and the test's
own outer ``bash`` with ``SIGWINCH`` (default action: ignore), so even a
broken guard could not hurt anything.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib import kill_guard as kg
from vco_lib import kill_guard_runtime as rt
from vco_lib.hook_retirements import MATCHER_MIGRATIONS, migrated_matcher

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"
SH_HOOK = HOOKS / "lean-ctx-rewrite.sh"
PS1_HOOK = HOOKS / "lean-ctx-rewrite.ps1"
SCANNER = REPO_ROOT / "templates" / "scripts" / "bash_security.py"
TPLS = [REPO_ROOT / "templates" / "settings.json.linux.template",
        REPO_ROOT / "templates" / "settings.json.windows.template"]

sys.path.insert(0, str(SCANNER.parent))
import bash_security as bs  # noqa: E402  # pyright: ignore[reportMissingImports]  # resolved by the sys.path.insert above

PWSH = shutil.which("pwsh")
_POSIX = sys.platform != "win32"
needs_posix = pytest.mark.skipif(not _POSIX, reason="bash + /proc end-to-end")
needs_pwsh = pytest.mark.skipif(PWSH is None or not _POSIX,
                                reason="pwsh not installed (parity arm)")

#: The exact command of the 2026-10-23 00:37:21 incident.
INCIDENT = "kill -9 $p $(ps -o ppid= -p $p)"


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """The owner's switch must not leak in from the developer's shell."""
    monkeypatch.delenv("VCO_KILL_GUARD", raising=False)


# ═══════════════════════════════════════════════════════════════════════
# Layer 1 — static deny, through the CALLER (bash_security.check_command)
# ═══════════════════════════════════════════════════════════════════════

REFUSED_BASH = [
    INCIDENT,
    "kill -9 $PPID",
    "pkill -f python",
    "kill -- -1",
    "kill -9 -1",
    "kill 1",
    "kill 0",
    "kill $$",
    "kill -TERM -$$",
    "ps -o ppid= -p $p | xargs kill -9",
    "awk '{print $4}' /proc/$p/stat | xargs kill",
    "kill $(pgrep -P 1)",
    "bash -c 'kill -9 $PPID'",
    "sudo kill 1",
    "timeout 5 kill 1",
    "pkill sh",
    "pkill node",
    "pkill -x python",
    "pkill -f '^python'",
    "pkill -u someuser",
    "pkill -v myserverd",
    "pkill -P 1",
    "killall python3",
    "killall systemd",
    "killall -u someuser",
    "killall5 -9",
    "/bin/kill 4242",
    "builtin kill 4242",
    "command kill 4242",
    "taskkill /F /IM explorer.exe",
    'taskkill /FI "STATUS eq RUNNING"',
]

ALLOWED_BASH = [
    "sleep 30 & kill $!",
    "sleep 30 & p=$!; kill -TERM $p",
    "pkill -x myserverd",
    "pkill -f '^python -m myapp.server --port 8123'",
    "pkill -P $$",
    "killall myserverd",
    "kill -0 4242",
    "kill %1",
    "kill -l",
    "command -v kill",
    "taskkill /F /PID 4242",
    # prose, not a call
    'git commit -m "fix: kill 1 stray process"',
    "echo 'kill -9 1'",
    "grep -rn pkill .",
    # a here-doc BODY written to a file is data
    "cat > notes.txt <<'EOF'\nkill -9 $PPID\nEOF",
]

REFUSED_PS = [
    "Stop-Process -Id $PID",
    "Stop-Process -Id $PID -Force",
    "kill -Name explorer",
    "spps -Name winlogon",
    "Stop-Process -Name py*",
    "Stop-Process 1",
    "Stop-Process -Id 4",
    "Get-Process | Stop-Process",
    "Get-Process -Name python | Stop-Process",
    "Stop-Process -Id (Get-Process -Id $PID).Parent.Id",
    "Stop-Process -Id (Get-CimInstance Win32_Process -Filter \"ProcessId=$PID\").ParentProcessId",
    "taskkill /IM lsass.exe /F",
    "taskkill /F /FI \"USERNAME eq me\"",
]

ALLOWED_PS = [
    "Stop-Process -Id 4242 -Force",
    "spps -Name myserver",
    "Get-Process -Name myserver | Stop-Process",
    "$p = Start-Process sleep -PassThru; Stop-Process -Id $p.Id",
    "taskkill /PID 4242 /T",
    "Get-Content kill.log",
]


class TestStaticDenyThroughScanner:
    @pytest.mark.parametrize("cmd", REFUSED_BASH)
    def test_refused(self, cmd):
        safe, reason = bs.check_command(cmd)
        assert not safe and reason.startswith("[kill_guard] Refused"), (cmd, reason)

    @pytest.mark.parametrize("cmd", ALLOWED_BASH)
    def test_allowed(self, cmd):
        safe, reason = bs.check_command(cmd)
        assert safe, (cmd, reason)

    @pytest.mark.parametrize("cmd", REFUSED_PS)
    def test_powershell_refused(self, cmd):
        safe, reason = bs.check_command(cmd, shell="powershell")
        assert not safe and "[kill_guard]" in reason, (cmd, reason)

    @pytest.mark.parametrize("cmd", ALLOWED_PS)
    def test_powershell_allowed(self, cmd):
        safe, reason = bs.check_command(cmd, shell="powershell")
        assert safe, (cmd, reason)

    def test_message_names_the_safe_pattern_and_the_user_only_switch(self):
        _, reason = bs.check_command(INCIDENT)
        assert "parent" in reason.lower()
        assert "_reap_by_env_token" in reason and "$!" in reason
        assert ("For the USER only (your decision, never the agent's): "
                "VCO_KILL_GUARD=off disables this guard.") in reason

    def test_incident_refused_by_the_scanner_cli(self):
        """The exact CLI the pre-tool-use hook runs: exit 2 = blocked."""
        res = subprocess.run([sys.executable, str(SCANNER)], input=INCIDENT,
                             capture_output=True, text=True, env=child_env(),
                             timeout=60)
        assert res.returncode == 2, res.stderr
        assert "[kill_guard] Refused" in res.stderr

    def test_kill_switch_off_lets_layer_1_pass(self, monkeypatch):
        monkeypatch.setenv("VCO_KILL_GUARD", "off")
        assert bs.check_command("kill -9 $PPID") == (True, "")

    def test_a_switch_set_inside_the_command_changes_nothing(self):
        safe, _ = bs.check_command("VCO_KILL_GUARD=off kill -9 $PPID")
        assert not safe

    def test_oversized_command_is_judged_without_the_slow_walk(self):
        """shlex is super-linear on huge words; the scanner has a 2 s bound.
        Over SCAN_MAX_CHARS a command naming a kill verb is refused outright,
        one that names none is untouched by the kill guard."""
        import time

        big = "x" * (kg.SCAN_MAX_CHARS * 8)
        t0 = time.monotonic()
        safe, reason = bs.check_command(big + " ; kill 4242")
        assert not safe and "too large for the kill guard" in reason
        assert time.monotonic() - t0 < 1.5
        assert kg.static_check(big + " ; ls") is None
        assert kg.bash_kill_segments(big + " kill") is True

    def test_broken_install_refuses_kills_only(self, tmp_path):
        """No vco_lib importable = broken install: a kill call is refused
        with the cause named; a mere mention of the word is not."""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONNOUSERSITE"] = "1"
        py = shutil.which("python3") or sys.executable
        for cmd, want in (("kill 4242", 2), ("git log --grep kill", 0)):
            res = subprocess.run([py, "-S", str(SCANNER)], input=cmd,  # -S: no site-packages, so no editable vco_lib
                                 capture_output=True, text=True, env=env,
                                 cwd=tmp_path, timeout=60)
            assert res.returncode == want, (cmd, res.stderr)
            if want == 2:
                assert "could not load" in res.stderr


# ═══════════════════════════════════════════════════════════════════════
# Layer 2 — runtime decisions over INJECTED process tables
# ═══════════════════════════════════════════════════════════════════════

#: A fabricated desktop: init -> systemd --user -> gnome-shell / code ->
#: claude -> bash (the agent's shell) -> python (the guard) ; plus an
#: orphan test flusher reparented to systemd --user, and a child of bash.
GUARD = 5000
SHELL = 4000
TABLE = {
    1: rt.Proc(1, 0, "systemd", 1, 1, "/sbin/init"),
    900: rt.Proc(900, 1, "systemd", 900, 900, "/usr/lib/systemd/systemd --user"),
    1200: rt.Proc(1200, 900, "gnome-shell", 1200, 1200, "/usr/bin/gnome-shell"),
    1300: rt.Proc(1300, 900, "dbus-daemon", 1300, 1300, "/usr/bin/dbus-daemon --session"),
    2000: rt.Proc(2000, 900, "code", 2000, 2000, "/usr/share/code/code"),
    3000: rt.Proc(3000, 2000, "claude", 3000, 3000, "node claude"),
    SHELL: rt.Proc(SHELL, 3000, "bash", SHELL, SHELL, "/bin/bash -c kill ..."),
    GUARD: rt.Proc(GUARD, SHELL, "python3", SHELL, SHELL, "python -m vco_lib.kill_guard check"),
    6000: rt.Proc(6000, 900, "python3", 6000, 6000, "python flusher.py"),  # the orphan
    7000: rt.Proc(7000, SHELL, "sleep", SHELL, SHELL, "sleep 30"),  # a child we started
    7100: rt.Proc(7100, 7000, "sleep", 7100, 7100, "sleep 31"),
    8000: rt.Proc(8000, 1, "myserverd", 8000, 8000, "/opt/myserverd --port 1"),
}


def _check(verb, *args, pgrep=None) -> str:
    """The refusal reason, or "" when the call may run."""
    return rt.check_call(verb, list(args), table=TABLE, self_pid=GUARD,
                         pgrep=pgrep) or ""


class TestRuntimeDecisions:
    def test_pid_1_refused(self):
        assert "is init / every process" in _check("kill", "-9", "1")

    def test_shells_own_parent_refused(self):
        why = _check("kill", "-9", "3000")
        assert "ancestor" in why

    def test_the_incident_target_systemd_user_refused(self):
        # kill -9 $p $(ps -o ppid= -p $p) with $p = the orphan 6000 -> 900
        why = _check("kill", "-9", "6000", "900")
        assert "900" in why

    def test_the_shell_itself_and_its_session_leader_refused(self):
        assert _check("kill", str(SHELL)) != ""

    def test_desktop_critical_name_refused(self):
        assert "desktop/system-critical" in _check("kill", "1200")
        assert _check("kill", "1300") != ""

    def test_minus_one_and_zero_refused(self):
        assert "every process" in _check("kill", "--", "-1")
        assert _check("kill", "-9", "0") != ""

    def test_negative_pid_checks_the_whole_group(self):
        # group SHELL holds the shell and the guard
        assert _check("kill", "--", "-%d" % SHELL) != ""
        assert _check("kill", "--", "-7100") == ""

    def test_child_we_started_allowed(self):
        assert _check("kill", "7000") == ""
        assert _check("kill", "-TERM", "7000", "7100") == ""

    def test_orphan_itself_allowed(self):
        # killing the leaked process is fine; its PARENT is the danger
        assert _check("kill", "-9", "6000") == ""

    def test_signal_zero_probe_never_refused(self):
        assert _check("kill", "-0", "1") == ""

    def test_job_specs_pass(self):
        assert _check("kill", "%1") == ""

    def test_pkill_resolves_through_pgrep_and_refuses_an_ancestor(self):
        seen = []

        def fake_pgrep(args):
            seen.append(list(args))
            return [7000, 3000]  # the pattern also matched claude

        why = _check("pkill", "-9", "-f", "^sleep", pgrep=fake_pgrep)
        assert "3000" in why
        assert seen == [["-f", "^sleep"]], "the signal spec is not a pgrep arg"

    def test_pkill_allowed_when_matches_are_safe(self):
        assert _check("pkill", "-x", "sleep", pgrep=lambda a: [7000, 7100]) == ""

    def test_pkill_ignores_the_guard_itself(self):
        assert _check("pkill", "-x", "python3", pgrep=lambda a: [GUARD]) == ""

    def test_pkill_fails_closed_when_pgrep_cannot_answer(self):
        assert "pgrep" in _check("pkill", "myserverd", pgrep=lambda a: None)

    def test_killall_resolution(self):
        assert _check("killall", "myserverd") == ""
        assert _check("killall", "gnome-shell") != ""
        assert _check("killall", "-r", "^dbus") != ""
        assert "every process of that user" in _check("killall", "-u", "me")

    def test_stop_process_and_taskkill(self):
        assert _check("stop-process", "--id", "7000") == ""
        assert _check("stop-process", "--id", str(SHELL)) != ""
        assert _check("stop-process", "--name", "gnome-*") != ""
        assert _check("taskkill", "/PID", "7000", "/F") == ""
        assert _check("taskkill", "/IM", "dbus-daemon.exe") != ""
        assert "filter" in _check("taskkill", "/FI", "PID gt 0")

    def test_unreadable_table_fails_closed(self, monkeypatch):
        monkeypatch.setattr(rt, "current_table", lambda: None)
        why = rt.check_call("kill", ["7000"], self_pid=GUARD)
        assert why is not None and "process table" in why

    def test_check_cli_exit_codes(self, monkeypatch, capsys):
        monkeypatch.setattr(rt, "current_table", lambda: TABLE)
        monkeypatch.setattr(os, "getpid", lambda: GUARD)
        assert kg.main(["check", "kill", "--", "-9", "900"]) == kg.EXIT_REFUSED
        assert "[kill_guard] Refused" in capsys.readouterr().err
        assert kg.main(["check", "kill", "--", "7000"]) == 0


class TestProcFixture:
    def test_read_proc_table_from_a_fixture_tree(self, tmp_path):
        def proc(pid, stat, cmdline):
            d = tmp_path / str(pid)
            d.mkdir()
            (d / "stat").write_text(stat)
            (d / "cmdline").write_bytes(cmdline)

        proc(1, "1 (systemd) S 0 1 1 0 -1", b"/sbin/init\0splash\0")
        # comm with spaces AND a parenthesis: cut at the LAST ')'
        proc(42, "42 (my (odd) name) S 1 42 42 0 -1", b"odd\0")
        proc(43, "43 (sleep) S 42 42 42 0 -1", b"sleep\x0030\0")
        (tmp_path / "self").mkdir()
        (tmp_path / "notapid").mkdir()
        table = rt.read_proc_table(str(tmp_path))
        assert table is not None and set(table) == {1, 42, 43}
        assert table[42].name == "my (odd) name" and table[42].ppid == 1
        assert table[43].cmdline == "sleep 30" and table[43].sid == 42
        assert rt.ancestors(43, table) == [42, 1]
        assert rt.check_call("kill", ["1"], table=table, self_pid=43) is not None
        assert rt.check_call("kill", ["42"], table=table, self_pid=43) is not None
        assert rt.check_call("kill", ["43"], table=table, self_pid=999) is None


# ═══════════════════════════════════════════════════════════════════════
# The rewrite (one emitter) — through the hook siblings
# ═══════════════════════════════════════════════════════════════════════


def _payload(cmd, tool="Bash", **extra):
    ti = {"command": cmd, **extra}
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": ti})


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (proj / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    return proj


def _env(tmp_path, **extra):
    env = child_env(HOME=str(tmp_path / "home"), **extra)
    for k in ("VCT_DISABLE_HOOKS", "CLAUDE_PROJECT_DIR", "VCO_KILL_GUARD",
              "VCO_LEAN_CTX_DEFAULT"):
        env.pop(k, None)
    env.update(extra)
    return env


def _run_hook(impl, cmd, tmp_path, tool="Bash", **env_extra):
    argv = (["bash", str(SH_HOOK)] if impl == "sh"
            else [PWSH or "pwsh", *PWSH_BASE_FLAGS, "-File", str(PS1_HOOK)])
    return subprocess.run(argv, input=_payload(cmd, tool, description="d", timeout=7),
                          capture_output=True, text=True, cwd=_project(tmp_path),
                          env=_env(tmp_path, **env_extra), timeout=120)


def _updated(res):
    assert res.returncode == 0, res.stderr
    if not res.stdout.strip():
        return None
    return json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]


_IMPLS = ["sh", pytest.param("ps1", marks=needs_pwsh)]

#: (tool, command, rewritten?) — the SAME table for both siblings.
REWRITE_CASES = [
    ("Bash", "sleep 30 & kill $!", True),
    ("Bash", "echo 4242 | xargs kill", True),
    ("Bash", "pkill -x myserverd", True),
    ("Bash", "bash -c 'kill 4242'", True),
    ("Bash", "pytest tests/test_kill_guard.py -q", False),
    ("Bash", "git log --grep kill", False),
    ("Bash", "ls -la", False),
    ("PowerShell", "Stop-Process -Id 4242", True),
    ("PowerShell", "spps -Name myserver", True),
    ("PowerShell", "taskkill /PID 4242", True),
    ("PowerShell", "Get-Content kill.log", False),
]


@needs_posix
@pytest.mark.parametrize("impl", _IMPLS)
class TestHookRewriteParity:
    @pytest.mark.parametrize("tool,cmd,want", REWRITE_CASES)
    def test_rewrite_decision(self, impl, tool, cmd, want, tmp_path):
        ui = _updated(_run_hook(impl, cmd, tmp_path, tool=tool))
        if not want:
            # No kill-guard rewrite. (An allow-listed command such as pytest
            # may still be lean-ctx-wrapped by stage 2 when a lean-ctx binary
            # is on this machine — proof the kill stage stepped aside.)
            assert ui is None or not ui["command"].startswith("# VCO kill guard"), ui
            return
        assert ui is not None, "a kill verb must be routed through the guard"
        assert ui["command"].endswith(cmd), "the original command runs unchanged after the prefix"
        assert ui["command"].startswith("# VCO kill guard")
        assert ui["description"] == "d" and ui["timeout"] == 7, "other fields preserved"
        assert "permissionDecision" not in json.dumps(ui)

    def test_switch_off_means_no_rewrite(self, impl, tmp_path):
        res = _run_hook(impl, "kill 4242", tmp_path, VCO_KILL_GUARD="off")
        assert _updated(res) is None

    def test_lean_ctx_off_does_not_disable_the_kill_guard(self, impl, tmp_path):
        res = _run_hook(impl, "kill 4242", tmp_path, VCO_LEAN_CTX_DEFAULT="off")
        assert _updated(res) is not None

    def test_vct_disable_hooks_still_wins(self, impl, tmp_path):
        res = _run_hook(impl, "kill 4242", tmp_path, VCT_DISABLE_HOOKS="1")
        assert _updated(res) is None


def _rewritten(cmd, tmp_path, tool="Bash"):
    ui = _updated(_run_hook("sh", cmd, tmp_path, tool=tool))
    assert ui is not None
    return ui["command"]


@needs_posix
class TestRuntimeEndToEnd:
    """Real shells, real kill — but only ever on processes started here."""

    def _outer(self, inner_cmd, tmp_path):
        # The outer bash is OURS; $OUTER is its pid and the inner command's
        # shell is its child. SIGWINCH's default action is "ignore", so a
        # guard that failed to refuse would still harm nothing.
        script = 'export OUTER=$$; bash -c "$INNER"; echo "outer-alive"'
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=_env(tmp_path, INNER=inner_cmd),
                              timeout=120)

    def test_kill_on_the_shells_parent_is_refused(self, tmp_path):
        inner = _rewritten('kill -s WINCH "$OUTER"; echo "rc=$?"', tmp_path)
        res = self._outer(inner, tmp_path)
        assert "rc=1" in res.stdout and "outer-alive" in res.stdout, res
        assert "[kill_guard] Refused" in res.stderr and "ancestor" in res.stderr

    def test_xargs_kill_on_the_parent_is_refused_via_the_path_shim(self, tmp_path):
        inner = _rewritten('echo "$OUTER" | xargs kill -s WINCH; echo "rc=$?"', tmp_path)
        res = self._outer(inner, tmp_path)
        assert "rc=0" not in res.stdout and "outer-alive" in res.stdout, res
        assert "[kill_guard] Refused" in res.stderr

    def test_nested_bash_c_sees_the_exported_guard(self, tmp_path):
        inner = _rewritten('bash -c \'kill -s WINCH "$OUTER"\'; echo "rc=$?"', tmp_path)
        res = self._outer(inner, tmp_path)
        assert "rc=1" in res.stdout, res
        assert "[kill_guard] Refused" in res.stderr

    def test_kill_on_a_child_it_spawned_is_done(self, tmp_path):
        cmd = _rewritten('sleep 60 & p=$!; kill "$p"; wait "$p"; echo "rc=$?"', tmp_path)
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             env=_env(tmp_path), timeout=120)
        assert "rc=143" in res.stdout, res  # 128 + SIGTERM: the child was killed
        assert "Refused" not in res.stderr

    def test_xargs_kill_on_a_child_is_done(self, tmp_path):
        cmd = _rewritten('sleep 60 & p=$!; echo "$p" | xargs kill; wait "$p"; echo "rc=$?"',
                         tmp_path)
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                             env=_env(tmp_path), timeout=120)
        assert "rc=143" in res.stdout, res


@needs_pwsh
class TestPowerShellRuntimeEndToEnd:
    def _run(self, cmd, tmp_path):
        ui = _updated(_run_hook("ps1", cmd, tmp_path, tool="PowerShell"))
        assert ui is not None
        return subprocess.run([PWSH or "pwsh", *PWSH_BASE_FLAGS, "-Command", ui["command"]],
                              capture_output=True, text=True, env=_env(tmp_path),
                              timeout=120)

    def test_stop_process_on_its_own_shell_is_refused(self, tmp_path):
        # $PID is the pwsh this test started; the guard is its child.
        res = self._run('Stop-Process -Id $PID; "survived"; spps -Id $PID; "survived2"', tmp_path)
        assert "survived" in res.stdout and "survived2" in res.stdout, res
        assert "[kill_guard] Refused" in res.stdout + res.stderr

    def test_stop_process_on_a_child_it_spawned_is_done(self, tmp_path):
        res = self._run('$s = Start-Process sleep -ArgumentList 60 -PassThru; '
                        'Stop-Process -Id $s.Id; Start-Sleep -Milliseconds 500; '
                        '$s.Refresh(); "exited=$($s.HasExited)"', tmp_path)
        assert "exited=True" in res.stdout, res


# ═══════════════════════════════════════════════════════════════════════
# Registration — the one emitter now sees PowerShell-tool calls too
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("tpl", TPLS, ids=["linux", "windows"])
def test_lean_ctx_rewrite_registered_on_bash_and_powershell(tpl):
    doc = json.loads(tpl.read_text(encoding="utf-8"))
    groups = [g for g in doc["hooks"]["PreToolUse"]
              if any("lean-ctx-rewrite" in h["command"] for h in g["hooks"])]
    assert [g["matcher"] for g in groups] == ["Bash|PowerShell"]


def test_installed_bash_group_migrates_to_the_widened_matcher():
    for script in ("lean-ctx-rewrite.sh", "lean-ctx-rewrite.ps1"):
        assert migrated_matcher("PreToolUse", "Bash", script) == "Bash|PowerShell"
    assert any(r.retired_in == "v0.2.105" for r in MATCHER_MIGRATIONS)


def test_no_second_updated_input_emitter_for_shell_tools():
    """Two PreToolUse hooks rewriting one call conflict: the only shipped
    hooks that emit updatedInput are lean-ctx-rewrite (shell tools) and the
    Agent|Task brief injector."""
    emitters = sorted(p.name for p in HOOKS.glob("*.*")
                      if p.suffix in (".sh", ".ps1")
                      and "updatedInput" in p.read_text(encoding="utf-8", errors="replace"))
    assert emitters == ["agent-brief-kg-inject.ps1", "agent-brief-kg-inject.sh",
                        "lean-ctx-rewrite.ps1", "lean-ctx-rewrite.sh"], emitters


def test_bash_runtime_rewrite_is_skipped_on_windows_only(monkeypatch):
    """Git Bash's kill takes MSYS pids, which the Windows table cannot map:
    no Bash rewrite there; PowerShell is still guarded; POSIX Bash is."""
    def rewrite(tool, cmd):
        return kg.build_rewrite({"tool_name": tool, "tool_input": {"command": cmd}},
                                python="/x/python", project_root=None)

    monkeypatch.setattr(kg.sys, "platform", "win32")
    assert rewrite("Bash", "kill 4242") is None
    assert rewrite("PowerShell", "Stop-Process -Id 4242") is not None
    monkeypatch.setattr(kg.sys, "platform", "linux")
    assert rewrite("Bash", "kill 4242") is not None
