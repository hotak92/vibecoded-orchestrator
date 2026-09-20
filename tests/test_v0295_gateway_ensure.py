# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Ensuring the model gateway on every session — and the third state.

v0.2.95 R5c. Three user requirements, one mechanism (ruling R20 — the ensure
rides the SessionStart hook that already ensures the hub, it is not a second
ensure):

* it must come up with the session, on every OS;
* it must not come up TWICE — and the "not twice" mechanism is the daemon's
  own pid/port guard, which this module READS rather than duplicating;
* a registration that cannot run is its own state, distinct from "not
  registered" and from "running". That state is what this machine sat in for
  eight hours on 2026-09-10 while the launcher toggle read "registered".

Every gate has an ACT test and a LEAVE-ALONE test. Nothing here runs
``systemctl`` / ``launchctl`` / ``schtasks``: the tool paths come from a faked
``shutil.which`` and the invocations are recorded, which is what lets one box
assert the SHAPE of all three mechanisms (applying a launchd job or importing
a Windows task remains an integration gap, named as one).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from vco_lib import boot_service as bs  # noqa: E402
from vco_lib import gateway_ensure as ge  # noqa: E402

ALL_OS = ("Linux", "Darwin", "Windows")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(home))
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv("VCT_DISABLE_BOOT_SERVICE", raising=False)
    monkeypatch.delenv("VCT_MODEL_GATEWAY_SECRET_PROJECT", raising=False)
    # No real init tool is ever reachable from this suite.
    monkeypatch.setattr(bs.shutil, "which", lambda name: None)
    return home, state


class _Tools(list):
    """Recorded init-tool invocations, plus the one fact a FILE cannot carry.

    Linux and macOS answer "is it registered?" from an artefact this suite
    writes; Windows answers it from ``schtasks /Query``, which no file can
    stand in for. ``task_exists`` is that answer, and it defaults to False so
    a test that writes no registration describes the same machine on all three
    OSes.
    """

    task_exists = False


@pytest.fixture
def tools(monkeypatch):
    """Every init tool 'present'; every invocation recorded, none executed."""
    calls = _Tools()
    monkeypatch.setattr(bs.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _Done:
        def __init__(self, returncode=0, stdout="enabled"):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def run(argv, *a, **kw):
        flat = [str(x) for x in argv]
        calls.append(flat)
        if "schtasks" in flat[0] and "/Query" in flat and not calls.task_exists:
            return _Done(returncode=1, stdout="")
        return _Done()

    monkeypatch.setattr(bs.subprocess, "run", run)
    return calls


def _register(
    os_key: str, home: Path, state: Path, argv=("/opt/vco/bin/gw",), tools=None,
):
    spec = bs.model_gateway_spec(
        os_key=os_key, exec_argv=list(argv), state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system=os_key, home=home)
    if tools is not None:
        # Windows keeps its registration in the task store, not in a file the
        # suite can create — see `_Tools`.
        tools.task_exists = True
    return spec


def _runs(rc: int = 0, detail: str = ""):
    def run(cmd, timeout):
        return rc, detail

    return run


def _write_pid(state: Path, pid: int) -> None:
    (state / bs.GATEWAY_PID_BASENAME).write_text(f"{pid}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# LEAVE-ALONE: the gateway is opt-in, so an absent registration is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("os_key", ALL_OS)
def test_a_machine_that_never_opted_in_invokes_nothing(sandbox, tools, os_key):
    """The security-relevant half: a session-start ensure that REGISTERED a
    login-time OAuth-bearing daemon would be making the user's decision."""
    home, state = sandbox

    result = ge.ensure_running(
        home=home, system=os_key, state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: tools.append([str(c) for c in cmd]),
    )

    assert result.state is ge.GatewayState.NOT_REGISTERED
    assert result.exit_code == 0
    assert not any("reset-failed" in " ".join(c) for c in tools)
    assert not any("/Run" in " ".join(c) for c in tools)
    assert not any("kickstart" in " ".join(c) for c in tools)


def test_the_kill_switch_stops_the_ensure_before_it_looks(sandbox, tools, monkeypatch):
    home, state = sandbox
    _register("Linux", home, state)
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    invoked: list = []

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append(cmd),
    )

    assert result.state is ge.GatewayState.DISABLED_BY_ENV
    assert invoked == []


def test_a_running_gateway_is_left_alone(sandbox, tools, monkeypatch):
    """"Not twice" is the DAEMON's pid/port guard; this reads it rather than
    adding a second one, and a live pid means nothing is invoked at all."""
    home, state = sandbox
    _register("Linux", home, state)
    _write_pid(state, 4242)
    monkeypatch.setattr("vco_lib.deferral_probes.pid_is_alive", lambda pid: pid == 4242)
    invoked: list = []

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append(cmd),
    )

    assert result.state is ge.GatewayState.RUNNING
    assert result.pid == 4242
    assert invoked == []


def test_a_stale_pid_file_does_not_count_as_running(sandbox, tools, monkeypatch):
    """The crash case: the pid file outlives its process, and an ensure that
    believed it would never restart the gateway again."""
    home, state = sandbox
    _register("Linux", home, state)
    _write_pid(state, 4242)
    monkeypatch.setattr("vco_lib.deferral_probes.pid_is_alive", lambda pid: False)
    invoked: list = []

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append([str(c) for c in cmd]),
    )

    assert result.state is ge.GatewayState.STARTED
    assert invoked


# ---------------------------------------------------------------------------
# ACT: the three OS shapes, and the reset-failed that makes Linux work at all
# ---------------------------------------------------------------------------


def test_linux_resets_a_parked_unit_before_starting_it(sandbox, tools):
    """A unit parked by StartLimitBurst answers `start` with "repeated too
    quickly" and does NOTHING — a no-op exactly when the ensure is needed."""
    home, state = sandbox
    _register("Linux", home, state)
    invoked: list[list[str]] = []

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append([str(c) for c in cmd]),
    )

    assert result.state is ge.GatewayState.STARTED
    flat = [" ".join(c) for c in invoked]
    assert any("--user reset-failed vct-model-gateway.service" in c for c in flat), flat
    assert any("--user start vct-model-gateway.service" in c for c in flat), flat
    assert flat.index(
        next(c for c in flat if "reset-failed" in c)
    ) < flat.index(next(c for c in flat if " start " in c)), "reset must come FIRST"


def test_macos_kickstarts_without_k_so_a_live_stream_is_not_killed(sandbox, tools):
    """`kickstart -k` RESTARTS: it would kill a healthy gateway mid-answer,
    which is the opposite of an ensure."""
    home, state = sandbox
    _register("Darwin", home, state)
    invoked: list[list[str]] = []

    result = ge.ensure_running(
        home=home, system="Darwin", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append([str(c) for c in cmd]),
    )

    assert result.state is ge.GatewayState.STARTED
    flat = " ".join(" ".join(c) for c in invoked)
    assert "kickstart" in flat
    assert bs.MODEL_GATEWAY_PLIST_LABEL in flat
    assert " -k " not in f" {flat} "


def test_windows_runs_the_task_which_declares_ignore_new(sandbox, tools):
    home, state = sandbox
    _register("Windows", home, state, tools=tools)
    invoked: list[list[str]] = []

    result = ge.ensure_running(
        home=home, system="Windows", state_dir=state, runner=_runs(),
        command_runner=lambda cmd, timeout=15: invoked.append([str(c) for c in cmd]),
    )

    assert result.state is ge.GatewayState.STARTED
    flat = " ".join(" ".join(c) for c in invoked)
    assert f"/Run /TN {bs.MODEL_GATEWAY_TASK_NAME}" in flat
    # The task's own policy is what makes a second /Run safe — assert the
    # SHIPPED template still says so, rather than trusting the memory of it.
    xml = (REPO_ROOT / "templates" / "windows"
           / "vct-model-gateway.task.xml.template").read_text(encoding="utf-8")
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml


def test_no_init_tool_is_a_named_failure_not_a_silent_success(sandbox):
    home, state = sandbox
    _register("Linux", home, state)

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state, runner=_runs(),
    )

    assert result.state is ge.GatewayState.START_FAILED
    assert result.exit_code == 4
    assert "Linux" in result.reason


# ---------------------------------------------------------------------------
# The THIRD state — registered, and unable to run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("os_key", ALL_OS)
def test_an_unrunnable_registration_is_its_own_state_on_every_os(
    sandbox, tools, os_key,
):
    home, state = sandbox
    _register(os_key, home, state, argv=("/usr/bin/python3.12", "-m", "model_router"), tools=tools)

    result = ge.gateway_status(
        home=home, system=os_key, state_dir=state,
        runner=_runs(1, "No module named 'model_router'"),
    )

    assert result.state is ge.GatewayState.REGISTERED_BUT_UNRUNNABLE
    assert result.exit_code == 3
    assert result.registered is True
    assert result.to_dict()["runnable"] is False
    assert "install.py --update" in result.reason


def test_an_unrunnable_registration_is_never_start_looped(sandbox, tools):
    """The init system is already retrying an argv that fails the same way
    every time; adding attempts from here would only add noise."""
    home, state = sandbox
    _register("Linux", home, state, argv=("/usr/bin/python3.12", "-m", "model_router"))
    invoked: list = []

    result = ge.ensure_running(
        home=home, system="Linux", state_dir=state,
        runner=_runs(1, "No module named 'model_router'"),
        command_runner=lambda cmd, timeout=15: invoked.append(cmd),
    )

    assert result.state is ge.GatewayState.REGISTERED_BUT_UNRUNNABLE
    assert invoked == []


def test_an_unrunnable_registration_writes_the_ledger_row(sandbox, tools, tmp_path):
    home, state = sandbox
    _register("Linux", home, state, argv=("/usr/bin/python3.12", "-m", "model_router"))
    project = tmp_path / "project"
    (project / ".claude" / "context").mkdir(parents=True)

    ge.ensure_running(
        home=home, system="Linux", state_dir=state, folder=project,
        runner=_runs(1, "No module named 'model_router'"),
        command_runner=lambda cmd, timeout=15: None,
    )

    ledger = project / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert ledger.is_file()
    assert ge.CID_GATEWAY_UNRUNNABLE in ledger.read_text(encoding="utf-8")


def test_nothing_is_written_into_a_folder_that_is_not_a_managed_project(
    sandbox, tools, tmp_path,
):
    """LEAVE-ALONE: a session-start hook must never create `.claude/` in a
    directory the user did not ask VCO to manage."""
    home, state = sandbox
    _register("Linux", home, state, argv=("/usr/bin/python3.12", "-m", "model_router"))
    stranger = tmp_path / "somewhere-else"
    stranger.mkdir()

    ge.ensure_running(
        home=home, system="Linux", state_dir=state, folder=stranger,
        runner=_runs(1, "No module named 'model_router'"),
        command_runner=lambda cmd, timeout=15: None,
    )

    assert list(stranger.iterdir()) == []


def test_a_registration_naming_no_entry_point_at_all_is_unrunnable(sandbox, tools):
    home, state = sandbox
    spec = bs.gateway_names_spec("Linux", state_dir=state)
    unit = bs.systemd_unit_path(spec, home)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n# hand-edited: no ExecStart\n", encoding="utf-8")

    result = ge.gateway_status(home=home, system="Linux", state_dir=state)

    assert result.state is ge.GatewayState.REGISTERED_BUT_UNRUNNABLE


def test_status_starts_nothing(sandbox, tools):
    home, state = sandbox
    _register("Linux", home, state)
    invoked: list = []
    original = bs.run_quiet

    def guard(cmd, timeout=15):
        invoked.append(cmd)
        return original(cmd, timeout)

    ge.gateway_status(home=home, system="Linux", state_dir=state, runner=_runs())
    assert invoked == []


# ---------------------------------------------------------------------------
# The clear probe and the doctor read the SAME verdict
# ---------------------------------------------------------------------------


def test_the_clear_probe_says_still_applies_while_it_is_unrunnable(monkeypatch):
    from vco_lib import deferral_probes

    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **kw: ge.GatewayEnsureResult(
            state=ge.GatewayState.REGISTERED_BUT_UNRUNNABLE, reason="broken",
        ),
    )
    ctx = deferral_probes.ProbeContext(folder=Path("/tmp"))
    assert deferral_probes.gateway_exec_still_unrunnable(ctx) is True


@pytest.mark.parametrize(
    "state",
    [
        ge.GatewayState.RUNNING,
        ge.GatewayState.NOT_REGISTERED,
        ge.GatewayState.REGISTERED_NOT_RUNNING,
    ],
)
def test_the_clear_probe_says_provably_over_once_it_can_run(monkeypatch, state):
    from vco_lib import deferral_probes

    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **kw: ge.GatewayEnsureResult(state=state, reason="fine"),
    )
    ctx = deferral_probes.ProbeContext(folder=Path("/tmp"))
    assert deferral_probes.gateway_exec_still_unrunnable(ctx) is False


def test_the_registry_declares_this_condition_with_that_probe():
    from vco_lib.deferral_registry import condition

    spec = condition(ge.CID_GATEWAY_UNRUNNABLE)
    assert spec is not None
    assert spec.probe_name == "gateway_exec_still_unrunnable"


def test_the_doctor_reports_nothing_when_the_gateway_is_not_registered():
    """Autostart is opt-in, so "unknown" on every machine that never took it
    would be noise, not evidence (the `code_embed_image` precedent)."""
    from vco_lib import doctor

    res = doctor.DoctorResolvers(
        gateway_state=lambda: ge.GatewayEnsureResult(
            state=ge.GatewayState.NOT_REGISTERED, reason="not registered",
        ),
    )
    assert doctor.probe_model_gateway_runnable(Path("/tmp"), res, {}) == []


def test_the_doctor_defers_the_condition_when_it_cannot_run(tmp_path):
    from vco_lib import doctor

    res = doctor.DoctorResolvers(
        gateway_state=lambda: ge.GatewayEnsureResult(
            state=ge.GatewayState.REGISTERED_BUT_UNRUNNABLE,
            reason="the registered entry point cannot run",
        ),
    )
    findings = doctor.probe_model_gateway_runnable(tmp_path, res, {})
    assert len(findings) == 1
    assert findings[0].status == doctor.STATUS_PROBLEM
    assert findings[0].condition_id == ge.CID_GATEWAY_UNRUNNABLE
    assert "install.py --update" in findings[0].command

    report = doctor.DoctorReport(folder=tmp_path, scope=doctor.SCOPE_FULL)
    report.findings.extend(findings)
    entries = doctor.deferral_entries_for(report)
    assert [e.condition_id for e in entries] == [ge.CID_GATEWAY_UNRUNNABLE]


def test_the_doctor_clears_the_condition_when_it_runs_again(tmp_path):
    """Self-resolving: the reading that emits is the reading that clears, so
    "it goes away by itself once fixed" holds at every invocation point."""
    from vco_lib import doctor

    res = doctor.DoctorResolvers(
        gateway_state=lambda: ge.GatewayEnsureResult(
            state=ge.GatewayState.RUNNING, reason="running (pid 1)",
        ),
    )
    report = doctor.DoctorReport(folder=tmp_path, scope=doctor.SCOPE_FULL)
    report.findings.extend(doctor.probe_model_gateway_runnable(tmp_path, res, {}))

    assert doctor.healthy_condition_ids(report) == [ge.CID_GATEWAY_UNRUNNABLE]


def test_the_doctor_probe_is_full_scope_only():
    """It SPAWNS the registered entry point — past the boot subset's
    file-read budget, and its answer changes when an install/update runs."""
    from vco_lib import doctor

    _fn, scopes = doctor.PROBES["model_gateway_runnable"]
    assert scopes == (doctor.SCOPE_FULL,)


# ---------------------------------------------------------------------------
# The CLI contract the hook depends on
# ---------------------------------------------------------------------------


def test_the_shell_output_is_evaluable_and_names_the_state(capsys, monkeypatch):
    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **kw: ge.GatewayEnsureResult(
            state=ge.GatewayState.NOT_REGISTERED, reason="not registered",
        ),
    )
    rc = ge.main(["status", "--shell"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "VCO_GATEWAY_STATE=not_registered" in out
    for line in out.strip().splitlines():
        assert "=" in line and line.split("=", 1)[0].isidentifier()


def test_the_json_output_carries_the_field_the_launcher_toggle_reads(
    capsys, monkeypatch,
):
    import json

    monkeypatch.setattr(
        ge, "gateway_status",
        lambda **kw: ge.GatewayEnsureResult(
            state=ge.GatewayState.REGISTERED_BUT_UNRUNNABLE, reason="broken",
            argv=("/usr/bin/python3.12", "-m", "model_router"),
        ),
    )
    rc = ge.main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 3
    assert payload["state"] == "registered_but_unrunnable"
    assert payload["registered"] is True
    assert payload["running"] is False
    assert payload["runnable"] is False


def test_every_state_has_an_exit_code():
    """A state added without a decision about its exit code would crash the
    hook that reads it — this is the reminder in test form."""
    for state in ge.GatewayState:
        assert state in ge.ENSURE_EXIT_CODES


def _hook_code(name: str) -> str:
    """A hook's EXECUTABLE lines — comments stripped.

    A "this file must not contain X" test that reads raw bytes flags the very
    comment explaining why X is not there, which teaches the next author to
    delete the explanation rather than keep the property
    (``test_model_gateway_boot._code_without_prose``, same reasoning).
    """
    text = (REPO_ROOT / "templates" / "hooks" / name).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_shipped_hook_calls_this_module_and_registers_nothing():
    """R20: one ensure mechanism. The hook must CALL the module, and must not
    carry a second copy of a systemctl/launchctl/schtasks invocation — nor any
    path that could REGISTER the opt-in daemon."""
    for name in ("session-start-ensure-hub.sh", "session-start-ensure-hub.ps1"):
        code = _hook_code(name)
        assert "vco_lib.gateway_ensure" in code, name
        for forbidden in ("systemctl", "launchctl", "schtasks", "register-boot"):
            assert forbidden not in code, f"{forbidden!r} in {name}"
