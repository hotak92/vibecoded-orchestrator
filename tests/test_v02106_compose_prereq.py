# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106: a missing compose provider is an install prerequisite.

Field report (macOS, fresh install): podman + a running podman machine but no
``podman-compose`` / ``docker-compose`` passed every check and died at install
step 5 with "looking up compose provider failed". ``vco_lib.compose_prereq``
(over the ONE detector ``compose_provider.detect``) now feeds the bootstrap
envelope, ``--install-missing``, install.py's own pre-flight and step 5's
failure classifier.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from vco_lib import compose_prereq as cpq
from vco_lib import compose_provider as cp
from vco_lib import compose_recovery as cr

_PODMAN_NO_PROVIDER = (
    "Error: looking up compose provider failed\n"
    "7 errors occurred:\n"
    '\t* exec: "docker-compose": executable file not found in $PATH\n'
    '\t* exec: "podman-compose": executable file not found in $PATH\n'
)


def _run_factory(compose_rc: int, stderr: str = ""):
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        if list(argv[:2]) in (["podman", "compose"], ["docker", "compose"]):
            return subprocess.CompletedProcess(argv, compose_rc, "", stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run, calls


def _detect_with(tmp_path, *, compose_rc: int, which_names=(), stderr: str = ""):
    run, _ = _run_factory(compose_rc, stderr)
    found = set(which_names)
    return lambda rt: cp.detect(rt, run=run, which=lambda n: f"/usr/bin/{n}" if n in found else None,
                                env={}, home=tmp_path)


def _probe(runtime: str, *, detect) -> dict:
    block = cpq.probe(runtime, detect=detect)
    assert block is not None
    return block


def _row(block: dict, *, os_name: str) -> dict:
    row = cpq.missing_prereq(block, os_name=os_name)
    assert row is not None
    return row


# ---------------------------------------------------------------- detection

def test_the_field_case_podman_compose_without_a_provider_is_missing(tmp_path):
    """`podman compose` exists as a subcommand but exits non-zero (no provider):
    the real detector returns None, so the probe says ok=False."""
    det = _detect_with(tmp_path, compose_rc=125, which_names=("podman",), stderr=_PODMAN_NO_PROVIDER)
    block = _probe("podman", detect=det)
    assert block == {"runtime": "podman", "ok": False, "engine": None, "argv": None}
    row = _row(block, os_name="macos")
    assert row["name"] == "compose_provider" and row["severity"] == "blocking"
    assert "brew install podman-compose" in row["install_hint"]


def test_podman_with_standalone_podman_compose_is_present(tmp_path):
    det = _detect_with(tmp_path, compose_rc=125, which_names=("podman", "podman-compose"))
    block = _probe("podman", detect=det)
    assert block["ok"] is True and block["engine"] == cp.ENGINE_PODMAN_COMPOSE
    assert cpq.missing_prereq(block, os_name="linux") is None


def test_podman_compose_delegating_to_docker_compose_is_present(tmp_path):
    det = _detect_with(tmp_path, compose_rc=0, which_names=("podman",),
                       stderr='>>>> Executing external compose provider "/usr/bin/docker-compose". <<<<')
    block = _probe("podman", detect=det)
    assert block["ok"] is True and block["argv"] == ["podman", "compose"]


def test_docker_plugin_present_and_absent(tmp_path):
    assert _probe("docker", detect=_detect_with(tmp_path, compose_rc=0))["ok"] is True
    standalone = _detect_with(tmp_path, compose_rc=1, which_names=("docker-compose",))
    assert _probe("docker", detect=standalone)["ok"] is True
    none = _probe("docker", detect=_detect_with(tmp_path, compose_rc=1))
    assert none["ok"] is False
    row = _row(none, os_name="macos")
    assert "Docker Desktop ships Compose" in row["install_hint"]


def test_no_runtime_means_no_compose_row():
    assert cpq.probe(None, detect=lambda rt: pytest.fail("must not probe")) is None
    assert cpq.missing_prereq(None) is None


# ---------------------------------------------------------------- remediation per OS

@pytest.mark.parametrize("runtime,os_name,needle", [
    ("podman", "macos", "brew install podman-compose"),
    ("podman", "Darwin", "brew install podman-compose"),
    ("podman", "linux", "sudo apt-get install -y podman-compose"),
    ("podman", "Linux", "sudo dnf install -y podman-compose"),
    ("podman", "windows", "winget install --id Docker.DockerCompose"),
    ("docker", "linux", "docker-compose-plugin"),
    ("docker", "macos", "Docker Desktop ships Compose"),
    ("docker", "Windows", "Docker Desktop ships Compose"),
])
def test_remediation_names_the_per_os_command(runtime, os_name, needle):
    assert needle in cpq.remediation(runtime, os_name)


def test_install_commands_per_package_manager():
    assert cpq.install_commands("podman", "brew") == ["brew install podman-compose"]
    assert cpq.install_commands("podman", "apt") == ["sudo apt-get install -y podman-compose"]
    assert cpq.install_commands("podman", None) == []
    assert cpq.install_commands("docker", "apt") == []


# ---------------------------------------------------------------- install.py surfaces

def _system_block(compose):
    absent = {"cmd": None, "version": None, "ok": False}
    return {
        "os": "macos",
        "python": {"version": "3.12.0", "wheel_support_ok": True},
        "podman": {"cmd": "/opt/homebrew/bin/podman", "ok": True, "machine_running": True},
        "docker": dict(absent),
        "node": {"ok": True}, "git": {"ok": True}, "lean_ctx": {"ok": True},
        "compose": compose,
    }


def test_bootstrap_missing_prereqs_lists_the_compose_provider():
    import install

    names = [m["name"] for m in install._bootstrap_compute_missing_prereqs(
        _system_block({"runtime": "podman", "ok": False, "engine": None, "argv": None}))]
    assert names == ["compose_provider"]
    assert install._bootstrap_compute_missing_prereqs(
        _system_block({"runtime": "podman", "ok": True, "engine": "podman-compose",
                       "argv": ["podman-compose"]})) == []


@pytest.mark.usefixtures("fake_claude_cli")
def test_bootstrap_envelope_carries_the_compose_block(monkeypatch):
    import install

    monkeypatch.setattr(cpq, "_default_detect", lambda rt: None)
    env = install._bootstrap_build_envelope(install.PROJECT_ROOT)
    compose = env["system"]["compose"]
    if compose is None:  # a CI box with neither podman nor docker binary
        assert env["system"]["podman"]["cmd"] is None and env["system"]["docker"]["cmd"] is None
        return
    assert compose["ok"] is False
    assert "compose_provider" in [m["name"] for m in env["missing_prereqs"]]
    assert env["ready_to_install"] is False


def test_install_missing_runs_the_compose_install(monkeypatch):
    import install

    ran: list[str] = []
    monkeypatch.setattr(install.subprocess, "run",
                        lambda c, **_k: (ran.append(c), SimpleNamespace(returncode=0))[1])
    env = {"missing_prereqs": [{"name": "compose_provider", "severity": "blocking", "human": "x"}],
           "package_manager_advice": {"install_compose": ["brew install podman-compose"]}}
    assert install._bootstrap_install_missing(env, no_prompt=True) == 0
    assert ran == ["brew install podman-compose"]


def test_doctor_reports_the_compose_prereq():
    from vco_lib import doctor

    row = _row({"runtime": "podman", "ok": False}, os_name="linux")
    findings = doctor.probe_prereqs(Path("."), cast(Any, None), {"bootstrap_envelope": {"missing_prereqs": [row]}})
    assert findings[0].status == doctor.STATUS_PROBLEM
    assert "compose_provider" in findings[0].summary
    assert "podman-compose" in findings[0].command


# ---------------------------------------------------------------- pre-flight

def test_preflight_fresh_install_stops_with_the_remedy(capsys):
    assert cpq.install_preflight("podman", update=False, os_name="macos",
                                 detect=lambda rt: None) is False
    err = capsys.readouterr().err
    assert "ERROR: No compose provider for podman" in err
    assert "brew install podman-compose" in err


def test_preflight_update_warns_and_continues(capsys):
    assert cpq.install_preflight("podman", update=True, os_name="linux",
                                 detect=lambda rt: None) is True
    assert "WARNING: No compose provider" in capsys.readouterr().out


def test_preflight_leaves_a_provider_or_no_containers_alone():
    present = cp.ComposeProvider("standalone", cp.ENGINE_PODMAN_COMPOSE, ("podman-compose",),
                                 "podman", "podman")
    assert cpq.install_preflight("podman", update=False, detect=lambda rt: present) is True
    assert cpq.install_preflight("podman", update=False, no_containers=True,
                                 detect=lambda rt: pytest.fail("must not probe")) is True
    assert cpq.install_preflight("", update=False,
                                 detect=lambda rt: pytest.fail("must not probe")) is True


class _Flow:
    def __init__(self, **_kw):
        self.report = SimpleNamespace(add_entry=lambda e: None)
        self.folder = None

    def arm(self):
        pass


class _Gate:
    def __init__(self, _mode):
        pass

    def begin(self):
        pass


def test_main_non_interactive_fails_fast_before_the_venv(monkeypatch, capsys):
    """install.py main(), fresh non-interactive install, podman without a
    provider: exit 1 right after step 2 — nothing past it (system-info print,
    venv, deps, step 5) runs."""
    import install
    from vco_lib import install_companions, install_resume

    noop = lambda *a, **k: None  # noqa: E731
    for name in ("_adopt_post_venv_handoff", "_ensure_running_under_mcp_venv",
                 "validate_source_repo", "_normalise_parsed_args", "_seed_run_report",
                 "_check_global_lean_ctx_hooks", "_check_windows_shell_prereqs",
                 "_audit_and_offer_env_secret_migration", "_log_install_event"):
        monkeypatch.setattr(install, name, noop)
    monkeypatch.setattr(install_companions, "adopt_relaunch", noop)
    monkeypatch.setattr(install_resume, "load_session", noop)
    monkeypatch.setattr(install, "InstallDeferralFlow", _Flow)
    monkeypatch.setattr(install, "InstallUpdateGate", _Gate)
    monkeypatch.setattr(install, "_resolve_adopt_project_mode", lambda *a, **k: "auto")
    monkeypatch.setattr(install, "_resume_verified", lambda *a, **k: True)
    monkeypatch.setattr(install, "_detect_system", lambda args, rep: install.SystemInfo(
        os_name="Darwin", has_gpu=False, has_metal=True, container_cmd="podman", gpu_name=""))
    monkeypatch.setattr(cpq, "_default_detect", lambda rt: None)

    def _past_the_preflight(*_a, **_k):
        raise AssertionError("install continued past step 2 without a compose provider")

    for name in ("_print_system_info", "_create_venv", "_start_services"):
        monkeypatch.setattr(install, name, _past_the_preflight)
    monkeypatch.setattr(sys, "argv", ["install.py", "--yes"])
    assert install.main() == 1
    assert "No compose provider for podman" in capsys.readouterr().err


# ---------------------------------------------------------------- step 5 (defence in depth)

def test_step5_failure_names_the_remedy():
    failure = cr.classify(_PODMAN_NO_PROVIDER, runtime="podman")
    assert failure.cause == cr.PROVIDER_MISSING
    assert "podman-compose" in failure.remedy
    docker = cr.classify("docker: 'compose' is not a docker command.\nSee 'docker --help'",
                         runtime="docker")
    assert docker.cause == cr.PROVIDER_MISSING and "compose" in docker.remedy
