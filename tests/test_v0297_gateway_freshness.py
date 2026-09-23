# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97 — an update never restarted the model gateway, so it kept serving
the previous release's code (field: 14 h across the 0.2.96 update).

The fix is a PROVEN staleness verdict plus a restart that happens only on an
explicit request (the launcher modal's Continue, or the command install.py
prints). These tests pin every decision in both directions: the restart runs
exactly when proven stale and owned, and nothing runs in every other case —
current, unknown, not running, not registered, not the unit's process.

No daemon, no network, no init system: every seam is injected, and nothing
here can reach the REAL gateway on this machine (conftest redirects the state
dir, and every test that could probe passes its own `probe`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import boot_service
from vco_lib import gateway_boot_render
from vco_lib import gateway_freshness as gf

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "claude_mcp_servers" / "model_router"
SERVICE = "vct-model-gateway"


def _identity_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_t_source_identity", PACKAGE / "source_identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _health(**over):
    base = {"ok": True, "service": SERVICE, "version": "0.2.97", "source_sha": "a" * 64}
    base.update(over)
    return base


EXPECTED = gf.CheckoutIdentity(version="0.2.97", source_sha="a" * 64)


# ---------------------------------------------------------------------------
# The identity — one function, both sides
# ---------------------------------------------------------------------------


def test_the_digest_changes_with_the_source_and_is_stable_otherwise(tmp_path):
    ident = _identity_module()
    (tmp_path / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (tmp_path / "routing.py").write_text("x = 1\n")
    first = ident.source_sha(tmp_path)
    assert first and first == ident.source_sha(tmp_path)
    (tmp_path / "routing.py").write_text("x = 2\n")
    assert ident.source_sha(tmp_path) != first
    assert ident.package_version(tmp_path) == "1.2.3"


def test_a_new_module_or_data_file_is_covered_the_day_it_lands(tmp_path):
    ident = _identity_module()
    (tmp_path / "__init__.py").write_text("")
    before = ident.source_sha(tmp_path)
    (tmp_path / "static_catalog.json").write_text("{}")
    assert ident.source_sha(tmp_path) != before
    # Bytecode caches are not source.
    (tmp_path / "__pycache__").mkdir()
    after = ident.source_sha(tmp_path)
    (tmp_path / "notes.pyc").write_bytes(b"\0")
    assert ident.source_sha(tmp_path) == after


def test_could_not_look_is_none_never_a_fabricated_digest(tmp_path):
    ident = _identity_module()
    assert ident.source_sha(tmp_path / "missing") is None
    assert ident.source_sha(tmp_path) is None  # empty: nothing to vouch for
    assert ident.package_version(tmp_path) is None


def test_the_daemon_and_the_host_compute_the_same_digest_for_this_checkout():
    """The served side (hashed at import) and the host side (loaded by path
    from the checkout) must agree on an unchanged tree, or every gateway reads
    stale forever."""
    from model_router import server
    from model_router import __version__

    host = gf.checkout_identity(REPO)
    assert host.source_sha is not None
    assert server.SOURCE_SHA == host.source_sha
    assert host.version == __version__
    verdict = gf.served_state(
        host, {"service": SERVICE, "version": __version__, "source_sha": server.SOURCE_SHA},
        service_name=SERVICE,
    )
    assert verdict.verdict == gf.CURRENT


def test_a_checkout_without_the_identity_module_yields_unknown_not_stale(tmp_path):
    ident = gf.checkout_identity(tmp_path)
    assert ident == gf.CheckoutIdentity()
    verdict = gf.served_state(ident, _health(), service_name=SERVICE)
    assert verdict.verdict == gf.UNKNOWN


# ---------------------------------------------------------------------------
# served_state — every arm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "health, expected, verdict",
    [
        (None, EXPECTED, gf.UNKNOWN),
        ({}, EXPECTED, gf.UNKNOWN),
        (_health(service="something-else"), EXPECTED, gf.UNKNOWN),
        (_health(version="0.2.96"), EXPECTED, gf.STALE),
        # The 0.2.96 gateway this release first meets: same-or-older, no key.
        ({"service": SERVICE, "version": "0.2.97"}, EXPECTED, gf.STALE),
        (_health(), gf.CheckoutIdentity(version="0.2.97"), gf.UNKNOWN),
        (_health(source_sha=None), EXPECTED, gf.UNKNOWN),
        (_health(), EXPECTED, gf.CURRENT),
        (_health(source_sha="b" * 64), EXPECTED, gf.STALE),
    ],
    ids=[
        "no-answer", "empty", "foreign-listener", "version-differs",
        "predates-source-sha", "no-checkout-digest", "served-null",
        "digests-equal", "digests-differ",
    ],
)
def test_served_state_arms(health, expected, verdict):
    assert gf.served_state(expected, health, service_name=SERVICE).verdict == verdict


# ---------------------------------------------------------------------------
# check — read-only
# ---------------------------------------------------------------------------


class _Probe:
    def __init__(self, answers: dict[int, Optional[dict]]):
        self.answers = answers
        self.calls: list[int] = []

    def __call__(self, port: int) -> Optional[dict]:
        self.calls.append(port)
        return self.answers.get(port)


def test_not_running_offers_nothing_and_probes_nothing():
    probe = _Probe({11460: _health(source_sha="b" * 64)})
    report = gf.check(expected=EXPECTED, probe=probe, running=False, pid=None)
    assert report.verdict.verdict == gf.NOT_RUNNING
    assert report.to_dict()["prompt"] is False
    assert probe.calls == []


def test_a_current_gateway_offers_nothing_and_plans_nothing():
    readers: list[str] = []
    report = gf.check(
        expected=EXPECTED, port=11460, probe=_Probe({11460: _health()}),
        running=True, pid=42, main_pid_reader=lambda unit: readers.append(unit) or 42,
    )
    assert report.verdict.verdict == gf.CURRENT
    assert report.to_dict()["prompt"] is False
    assert report.plan is None and readers == []


def test_unprovable_staleness_offers_nothing():
    report = gf.check(
        expected=EXPECTED, port=11460, probe=_Probe({}), running=True, pid=42,
    )
    assert report.verdict.verdict == gf.UNKNOWN
    assert report.to_dict()["prompt"] is False
    assert report.plan is None


def test_a_stale_gateway_prompts_and_carries_a_plan(monkeypatch):
    _registered(monkeypatch)
    report = gf.check(
        expected=EXPECTED, port=11460, probe=_Probe({11460: _health(version="0.2.96")}),
        running=True, pid=42, system="Linux", main_pid_reader=lambda unit: 42,
    )
    payload = report.to_dict()
    assert payload["prompt"] is True
    assert payload["restart"]["mechanism"] == gf.MECH_BOOT_SERVICE
    assert payload["port"] == 11460 and payload["pid"] == 42


def test_the_gateway_is_found_on_the_port_that_answers_as_the_gateway():
    probe = _Probe({11436: {"service": "legacy-router"}, 11460: _health()})
    port, health = gf.find_gateway([11436, 11460], SERVICE, probe)
    assert port == 11460 and health["service"] == SERVICE


# ---------------------------------------------------------------------------
# restart_plan — who may restart it
# ---------------------------------------------------------------------------


def _registered(monkeypatch, registered: bool = True):
    monkeypatch.setattr(
        gf.boot_service, "status",
        lambda spec, home=None, system=None: (
            boot_service.BootStatus.ENABLED if registered
            else boot_service.BootStatus.NOT_INSTALLED
        ),
    )
    monkeypatch.setattr(gf.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv(boot_service.DISABLE_ENV, raising=False)


def test_an_unregistered_gateway_cannot_be_restarted_from_here(monkeypatch):
    _registered(monkeypatch, registered=False)
    plan = gf.restart_plan(42, system="Linux", main_pid_reader=lambda u: 42)
    assert plan.mechanism == gf.MECH_NONE
    assert "stop it where it was started" in plan.reason


def test_linux_restarts_only_the_units_own_process(monkeypatch):
    _registered(monkeypatch)
    assert gf.restart_plan(42, system="Linux", main_pid_reader=lambda u: 42).possible
    other = gf.restart_plan(42, system="Linux", main_pid_reader=lambda u: 99)
    assert not other.possible and "pid 99" in other.reason
    none = gf.restart_plan(42, system="Linux", main_pid_reader=lambda u: None)
    assert not none.possible and "no main process" in none.reason


def test_the_kill_switch_means_hands_off(monkeypatch):
    _registered(monkeypatch)
    monkeypatch.setenv(boot_service.DISABLE_ENV, "1")
    assert not gf.restart_plan(42, system="Linux", main_pid_reader=lambda u: 42).possible


@pytest.mark.parametrize("os_name", ["Darwin", "Windows"])
def test_macos_and_windows_restart_the_registration_and_verify_after(monkeypatch, os_name):
    _registered(monkeypatch)
    assert gf.restart_plan(42, system=os_name).mechanism == gf.MECH_BOOT_SERVICE


def test_restart_steps_per_os(monkeypatch):
    monkeypatch.setattr(gf.shutil, "which", lambda name: f"/bin/{name}")
    linux = gf.restart_steps(boot_service.gateway_names_spec("Linux"), system="Linux")
    assert linux.stop == ()
    assert [c[2] for c in linux.start] == ["reset-failed", "restart"]
    mac = gf.restart_steps(boot_service.gateway_names_spec("Darwin"), system="Darwin")
    assert mac.start[0][1:3] == ("kickstart", "-k")
    win = gf.restart_steps(boot_service.gateway_names_spec("Windows"), system="Windows")
    assert win.stop[0][1] == "/End" and win.start[0][1] == "/Run"
    monkeypatch.setattr(gf.shutil, "which", lambda name: None)
    assert not gf.restart_steps(boot_service.gateway_names_spec("Linux"), system="Linux")


# ---------------------------------------------------------------------------
# restart — the destructive decision, both directions
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def _restart(monkeypatch, probe, *, system="Linux", main_pid=42, **kw):
    ran: list[tuple] = []
    clock = _Clock()
    outcome = gf.restart(
        expected=EXPECTED, port=11460, probe=probe, running=kw.pop("running", True),
        pid=42, system=system, main_pid_reader=lambda u: main_pid,
        command_runner=lambda cmd: ran.append(tuple(cmd)) or 0,
        pid_alive=kw.pop("pid_alive", lambda pid: False),
        sleep=clock.sleep, clock=clock, verify_timeout=kw.pop("verify_timeout", 5.0),
        **kw,
    )
    return outcome, ran


class _SwitchingProbe:
    """Old code until a restart command has run, then the checkout's code."""

    def __init__(self, ran: list, before: dict, after: dict):
        self.ran, self.before, self.after = ran, before, after

    def __call__(self, port):
        return self.after if self.ran else self.before


def test_continue_restarts_a_proven_stale_owned_gateway_and_verifies_it(monkeypatch):
    _registered(monkeypatch)
    ran: list = []
    probe = _SwitchingProbe(ran, _health(version="0.2.96"), _health())
    clock = _Clock()
    outcome = gf.restart(
        expected=EXPECTED, port=11460, probe=probe, running=True, pid=42,
        system="Linux", main_pid_reader=lambda u: 42,
        command_runner=lambda cmd: ran.append(tuple(cmd)) or 0,
        pid_alive=lambda pid: False, sleep=clock.sleep, clock=clock,
    )
    assert outcome.outcome == gf.OUTCOME_RESTARTED and outcome.exit_code == 0
    assert [c[2] for c in ran] == ["reset-failed", "restart"]
    assert outcome.after.verdict == gf.CURRENT


def test_a_restart_that_does_not_take_is_reported_unverified(monkeypatch):
    _registered(monkeypatch)
    outcome, ran = _restart(monkeypatch, _Probe({11460: _health(version="0.2.96")}))
    assert ran, "the restart was requested"
    assert outcome.outcome == gf.OUTCOME_UNVERIFIED and outcome.exit_code == 4


@pytest.mark.parametrize(
    "health, running",
    [(_health(), True), (None, True), (_health(service="other"), True), (_health(version="0.2.96"), False)],
    ids=["current", "unknown-no-answer", "unknown-foreign", "not-running"],
)
def test_nothing_runs_unless_proven_stale(monkeypatch, health, running):
    _registered(monkeypatch)
    outcome, ran = _restart(monkeypatch, _Probe({11460: health}), running=running)
    assert ran == []
    assert outcome.outcome == gf.OUTCOME_NOT_NEEDED and outcome.exit_code == 0


def test_nothing_runs_when_no_service_manager_owns_it(monkeypatch):
    _registered(monkeypatch, registered=False)
    outcome, ran = _restart(monkeypatch, _Probe({11460: _health(version="0.2.96")}))
    assert ran == []
    assert outcome.outcome == gf.OUTCOME_UNSUPPORTED and outcome.exit_code == 3


def test_nothing_runs_for_a_terminal_started_gateway_beside_an_enabled_unit(monkeypatch):
    _registered(monkeypatch)
    outcome, ran = _restart(
        monkeypatch, _Probe({11460: _health(version="0.2.96")}), main_pid=None,
    )
    assert ran == [] and outcome.outcome == gf.OUTCOME_UNSUPPORTED


def test_windows_waits_for_the_old_process_before_starting_the_new_one(monkeypatch):
    _registered(monkeypatch)
    events: list[str] = []
    alive_polls = iter([True, True, False])

    def alive(pid):
        value = next(alive_polls, False)
        events.append(f"alive?{value}")
        return value

    ran: list = []
    clock = _Clock()
    gf.restart(
        expected=EXPECTED, port=11460, probe=_Probe({11460: _health(version="0.2.96")}),
        running=True, pid=42, system="Windows",
        command_runner=lambda cmd: (ran.append(tuple(cmd)), events.append(cmd[1]))[0] or 0,
        pid_alive=alive, sleep=clock.sleep, clock=clock, verify_timeout=1.0,
    )
    assert events.index("/End") < events.index("alive?False") < events.index("/Run")


# ---------------------------------------------------------------------------
# The CLI user's line
# ---------------------------------------------------------------------------


def _stale_report(possible: bool) -> gf.FreshnessReport:
    return gf.FreshnessReport(
        gf.FreshnessVerdict(gf.STALE, "s", running_version="0.2.96", checkout_version="0.2.97"),
        pid=42, port=11460,
        plan=gf.RestartPlan(
            gf.MECH_BOOT_SERVICE if possible else gf.MECH_NONE,
            "restart it" if possible else "stop it where it was started and start it again.",
        ),
    )


def _fake_root_with_venv(tmp_path, *, real: bool = False) -> Path:
    """A clone whose venv interpreter is where `install_companions` looks.

    ``real=True`` makes it RUNNABLE: the venv python is a symlink to this
    interpreter and the clone carries `vco_lib` / `claude_mcp_servers` by
    symlink, so a child started with ``cwd=root`` imports THIS checkout.
    """
    import os
    import sys

    from vco_lib import install_companions

    root = tmp_path / "clone"
    windows = os.name == "nt"
    python = root / ".venv" / ("Scripts" if windows else "bin") / ("python.exe" if windows else "python")
    python.parent.mkdir(parents=True)
    if real:
        python.symlink_to(sys.executable)
        (root / "vco_lib").symlink_to(REPO / "vco_lib")
        (root / "claude_mcp_servers").symlink_to(REPO / "claude_mcp_servers")
    else:
        python.write_text("")
    assert install_companions.resolve_install_venv_python(root) == python
    return root


def _child(payload: Optional[dict] = None, *, code: int = 0, stderr: str = ""):
    """A fake venv child: records the argv/cwd, answers like the real CLI."""
    import subprocess

    calls: list = []

    def run(cmd, cwd):
        calls.append((list(cmd), cwd))
        out = json.dumps(payload) + "\n" if payload is not None else ""
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_the_notice_prints_a_command_that_exists(tmp_path):
    root = _fake_root_with_venv(tmp_path)
    line = gf.update_notice(_stale_report(True), root)
    assert line and "pid 42" in line and "NOT restarted" in line
    assert line.rstrip().endswith("-m vco_lib.gateway_freshness restart")
    assert str(root / ".venv") in line
    # The printed subcommand is one this module's own parser accepts.
    args = gf._build_arg_parser().parse_args(["restart"])
    assert args.cmd == "restart"


def test_the_command_falls_back_to_this_interpreter_and_quotes_per_platform(tmp_path):
    root = tmp_path / "my clone"  # a space, so quoting is exercised
    root.mkdir()
    posix = gf.restart_command_line(root, platform_name="posix", fallback_python="/opt/py/bin/python3")
    assert posix == f"cd '{root}' && /opt/py/bin/python3 -m vco_lib.gateway_freshness restart"
    nt = gf.restart_command_line(root, platform_name="nt", fallback_python=r"C:\Py\python.exe")
    assert nt.startswith(f'cd "{root}" && "C:\\Py\\python.exe" -m vco_lib.gateway_freshness')


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX shell form")
def test_the_printed_command_actually_runs(tmp_path):
    """A printed command is shipped code. Run it EXACTLY as printed — against a
    sandboxed state dir (no pid file: nothing is running, so nothing can be
    restarted) with the boot-service kill switch on as a second belt. It must
    reach the module and answer `not_needed`, proving the imports resolve."""
    import os
    import subprocess
    import sys

    line = gf.restart_command_line(REPO, fallback_python=sys.executable)
    env = {
        **os.environ,
        "VCT_STATE_DIR": str(tmp_path),
        boot_service.DISABLE_ENV: "1",
        "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'claude_mcp_servers'}",
    }
    proc = subprocess.run(
        line, shell=True, env=env, capture_output=True, text=True,  # noqa: S602
        timeout=60, cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"{gf.OUTCOME_NOT_NEEDED}:"), proc.stdout


def test_the_notice_names_the_manual_way_when_nothing_owns_it(tmp_path):
    root = _fake_root_with_venv(tmp_path)
    line = gf.update_notice(_stale_report(False), root)
    assert line and "gateway_freshness restart" not in line
    assert "stop it where it was started" in line


def test_no_notice_unless_stale(tmp_path):
    for verdict in (gf.CURRENT, gf.UNKNOWN, gf.NOT_RUNNING):
        report = gf.FreshnessReport(gf.FreshnessVerdict(verdict, "s"))
        assert gf.update_notice(report, tmp_path) is None


def test_report_after_update_is_a_no_op_on_a_plain_install(tmp_path):
    child = _child(_stale_report(True).to_dict())
    root = _fake_root_with_venv(tmp_path)
    assert gf.report_after_update(update=False, install_root=root, runner=child) is None
    assert child.calls == []


def test_the_update_check_runs_in_the_venv_child_from_the_checkout(tmp_path):
    root = _fake_root_with_venv(tmp_path)
    child = _child(_stale_report(True).to_dict(), code=3)
    printed, logged = [], []
    line = gf.report_after_update(
        update=True, install_root=root, out=printed.append, runner=child,
        log=lambda phase, level, detail: logged.append((phase, level, detail)),
    )
    (cmd, cwd), = child.calls
    assert cmd[0] == str(root / ".venv" / "bin" / "python") or cmd[0].endswith("python.exe")
    assert cmd[1:5] == ["-m", "vco_lib.gateway_freshness", "check", "--json"]
    assert cwd == root, "cwd is the checkout, so -m resolves ITS vco_lib"
    assert line and "restart it:" in line
    assert printed == [line] and logged == [("boot-service", "warn", line)]


def test_the_update_path_never_imports_model_router_in_process(monkeypatch, tmp_path):
    """The shape that broke a sibling leg for three releases: install.py's own
    interpreter cannot import `model_router`. Poison it; the path must still
    produce its line, because only the venv child imports it."""
    import sys

    monkeypatch.setitem(sys.modules, "model_router", None)
    monkeypatch.setitem(sys.modules, "model_router.config", None)
    root = _fake_root_with_venv(tmp_path)
    printed: list = []
    line = gf.report_after_update(
        update=True, install_root=root, out=printed.append,
        runner=_child(_stale_report(True).to_dict(), code=3),
    )
    assert line and "previous release" in line and printed == [line]


@pytest.mark.parametrize(
    "setup, expect",
    [
        ("no-venv", "no venv interpreter"),
        ("child-died", "exited 1 without a result"),
        ("venv-cannot-import", "cannot be imported"),
    ],
)
def test_a_check_that_cannot_run_is_printed_not_only_logged(tmp_path, setup, expect):
    if setup == "no-venv":
        root, runner = tmp_path, _child(None)
    elif setup == "child-died":
        root, runner = _fake_root_with_venv(tmp_path), _child(None, code=1, stderr="Traceback ...")
    else:
        broken = gf.FreshnessReport(
            gf.FreshnessVerdict(gf.UNKNOWN, "x"), pid=42,
            error="model gateway: `model_router` cannot be imported by this interpreter",
        )
        root, runner = _fake_root_with_venv(tmp_path), _child(broken.to_dict())
    printed: list = []
    line = gf.report_after_update(update=True, install_root=root, out=printed.append, runner=runner)
    assert printed == [line] and line and expect in line and "Could not check" in line


def test_a_healthy_check_that_finds_nothing_stale_is_silent(tmp_path):
    root = _fake_root_with_venv(tmp_path)
    for verdict in (gf.CURRENT, gf.UNKNOWN, gf.NOT_RUNNING):
        report = gf.FreshnessReport(gf.FreshnessVerdict(verdict, "s"))
        printed: list = []
        assert gf.report_after_update(
            update=True, install_root=root, out=printed.append, runner=_child(report.to_dict()),
        ) is None
        assert printed == []


def test_report_after_update_never_raises(tmp_path):
    def boom(cmd, cwd):
        raise RuntimeError("probe exploded")

    printed: list = []
    line = gf.report_after_update(
        update=True, install_root=_fake_root_with_venv(tmp_path), out=printed.append, runner=boom,
    )
    assert line and "probe exploded" in line and printed == [line]


@pytest.mark.skipif(__import__("os").name == "nt", reason="symlinked venv interpreter")
def test_the_real_venv_child_runs_and_is_silent_when_nothing_is_running(tmp_path, monkeypatch):
    """No fake: a real child process, from a clone laid out like an install,
    against a state dir with no gateway in it."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    root = _fake_root_with_venv(tmp_path, real=True)
    report, problem = gf.check_in_venv(root)
    assert problem is None
    assert report is not None and report.verdict.verdict == gf.NOT_RUNNING
    printed: list = []
    assert gf.report_after_update(update=True, install_root=root, out=printed.append) is None
    assert printed == []


def test_the_report_round_trips_through_json():
    original = _stale_report(True)
    back = gf.FreshnessReport.from_dict(json.loads(json.dumps(original.to_dict())))
    assert back.stale and back.pid == 42 and back.port == 11460
    assert back.plan == original.plan


# ---------------------------------------------------------------------------
# The install.py --update tail rides the re-render
# ---------------------------------------------------------------------------


def _fake_registration(**kw):
    return boot_service.GatewayRegistration(
        registered=False, refused=False,
        exec_result=boot_service.GatewayExec(argv=(), verified=False),
        spec=None, reason="not registered",
    )


def test_the_update_tail_reports_staleness_after_the_rerender(monkeypatch, tmp_path):
    calls: list = []
    monkeypatch.setattr(boot_service, "register_model_gateway", _fake_registration)
    monkeypatch.setattr(gf, "report_after_update", lambda **kw: calls.append(kw))
    gateway_boot_render.rerender_on_update(update=True, templates_root=tmp_path, install_root=tmp_path)
    assert len(calls) == 1 and calls[0]["update"] is True
    assert calls[0]["install_root"] == tmp_path

    calls.clear()
    gateway_boot_render.rerender_on_update(update=False, templates_root=tmp_path, install_root=tmp_path)
    assert calls == []


def test_the_update_tail_still_reports_when_the_rerender_raised(monkeypatch, tmp_path):
    calls: list = []

    def boom(**kw):
        raise OSError("disk")

    monkeypatch.setattr(boot_service, "register_model_gateway", boom)
    monkeypatch.setattr(gf, "report_after_update", lambda **kw: calls.append(kw))
    assert gateway_boot_render.rerender_on_update(
        update=True, templates_root=tmp_path, install_root=tmp_path,
    ) is None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# CLI — the machine contract the launcher parses
# ---------------------------------------------------------------------------


def test_cli_check_json_on_a_machine_with_no_gateway(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    code = gf.main(["check", "--json", "--install-root", str(REPO)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["verdict"] == gf.NOT_RUNNING and payload["prompt"] is False
    assert set(payload) >= {"verdict", "summary", "pid", "port", "prompt", "restart"}


def test_cli_restart_on_a_machine_with_no_gateway_runs_nothing(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    ran: list = []
    monkeypatch.setattr(boot_service, "run_quiet", lambda cmd, **kw: ran.append(cmd))
    code = gf.main(["restart", "--json", "--install-root", str(REPO)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["outcome"] == gf.OUTCOME_NOT_NEEDED
    assert ran == []
