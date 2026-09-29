# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-04 — compose provider detection, the podman API socket, and the
provider-picked GPU overlay. Injected probes only: nothing here runs podman,
docker or systemctl."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vco_lib import compose_provider as cp
from vco_lib import runtime_reconcile as rr
from vco_lib import service_adoption

BANNER_DC = (">>>> Executing external compose provider \"/usr/local/bin/docker-compose\". "
             "Please see podman-compose(1) for how to disable this message. <<<<\n")
BANNER_PC = (">>>> Executing external compose provider \"/usr/bin/podman-compose\". "
             "Please see podman-compose(1) for how to disable this message. <<<<\n")


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def _version_run(out="", err="", rc=0):
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return _cp(argv, rc, out, err)
    run.calls = calls
    return run


# ---------------------------------------------------------------------------
# detection matrix
# ---------------------------------------------------------------------------


def test_podman_compose_delegating_to_docker_compose(tmp_path):
    run = _version_run(out="Docker Compose version v2.29.1\n", err=BANNER_DC)
    p = cp.detect("podman", argv=["podman", "compose"], run=run, which=lambda n: None,
                  env={}, home=tmp_path)
    assert (p.form, p.engine, p.label_family) == ("subcommand", cp.ENGINE_DOCKER_COMPOSE, "docker")
    assert p.needs_api_socket
    assert run.calls == [["podman", "compose", "version"]]


def test_podman_compose_delegating_to_podman_compose(tmp_path):
    run = _version_run(out="podman-compose version 1.2.0\npodman version 5.2.2\n", err=BANNER_PC)
    p = cp.detect("podman", argv=["podman", "compose"], run=run, which=lambda n: None,
                  env={}, home=tmp_path)
    assert (p.engine, p.label_family) == (cp.ENGINE_PODMAN_COMPOSE, "podman")
    assert not p.needs_api_socket


def test_docker_compose_plugin(tmp_path):
    run = _version_run(out="Docker Compose version v2.29.1\n")
    p = cp.detect("docker", argv=["docker", "compose"], run=run, which=lambda n: None,
                  env={}, home=tmp_path)
    assert (p.engine, p.label_family) == (cp.ENGINE_DOCKER, "docker")
    assert not p.needs_api_socket
    assert run.calls == []  # docker's plugin needs no delegate question


def test_standalone_podman_compose(tmp_path):
    p = cp.detect("podman", argv=[str(tmp_path / ".local/bin/podman-compose")],
                  run=_version_run(), which=lambda n: None, env={}, home=tmp_path)
    assert (p.form, p.engine, p.label_family) == ("standalone", cp.ENGINE_PODMAN_COMPOSE, "podman")


def test_env_provider_is_honoured_when_the_banner_is_silent(tmp_path):
    p = cp.detect("podman", argv=["podman", "compose"], run=_version_run(),
                  which=lambda n: "/usr/bin/" + n,
                  env={"PODMAN_COMPOSE_PROVIDER": "/opt/tools/podman-compose"}, home=tmp_path)
    assert p.engine == cp.ENGINE_PODMAN_COMPOSE and "PODMAN_COMPOSE_PROVIDER" in p.evidence


def test_containers_conf_is_honoured_first_existing_entry(tmp_path):
    conf = tmp_path / "containers.conf"
    conf.write_text('[engine]\ncompose_providers = ["/nonexistent/docker-compose", '
                    '"podman-compose"]\n', encoding="utf-8")
    p = cp.detect("podman", argv=["podman", "compose"], run=_version_run(),
                  which=lambda n: "/usr/bin/" + n, env={"CONTAINERS_CONF": str(conf)},
                  home=tmp_path)
    assert p.engine == cp.ENGINE_PODMAN_COMPOSE and "compose_providers" in p.evidence


def test_path_search_order_is_docker_compose_first(tmp_path):
    p = cp.detect("podman", argv=["podman", "compose"], run=_version_run(),
                  which=lambda n: "/usr/bin/" + n, env={}, home=tmp_path)
    assert p.engine == cp.ENGINE_DOCKER_COMPOSE


def test_no_evidence_at_all_is_unknown_not_a_guess(tmp_path):
    p = cp.detect("podman", argv=["podman", "compose"], run=_version_run(rc=1),
                  which=lambda n: None, env={}, home=tmp_path)
    assert p.engine == cp.ENGINE_UNKNOWN and p.label_family == "unknown"


def test_detect_resolves_the_compose_command_itself_when_not_given(tmp_path):
    run = _version_run(out="Docker Compose version v2\n", err=BANNER_DC)
    p = cp.detect("podman", run=run, which=lambda n: "/usr/bin/" + n, env={}, home=tmp_path)
    assert p.argv == ("podman", "compose") and p.engine == cp.ENGINE_DOCKER_COMPOSE


def test_no_compose_at_all_is_none(tmp_path):
    assert cp.detect("podman", run=_version_run(rc=1), which=lambda n: None, env={},
                     home=tmp_path) is None


# ---------------------------------------------------------------------------
# socket status
# ---------------------------------------------------------------------------

SOCK = "/run/user/1000/podman/podman.sock"
ENV = {"XDG_RUNTIME_DIR": "/run/user/1000"}


class Systemd:
    """A fake `systemctl --user`: ``active`` state + whether the socket file
    exists; a restart/start brings the file back when ``heals``."""

    def __init__(self, active=True, file=False, heals=True, verb_rc=0):
        self.active, self.file, self.heals, self.verb_rc = active, file, heals, verb_rc
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] != "systemctl":
            raise AssertionError(argv)
        verb = argv[2]
        if verb == "is-active":
            return _cp(argv, 0 if self.active else 3, "active\n" if self.active else "inactive\n")
        if verb in ("restart", "start"):
            if self.verb_rc == 0 and self.heals:
                self.file, self.active = True, True
            return _cp(argv, self.verb_rc, "", "Failed" if self.verb_rc else "")
        raise AssertionError(argv)

    def exists(self, path):
        assert path == SOCK
        return self.file

    def kw(self):
        return dict(run=self.run, which=lambda n: "/usr/bin/" + n, env=ENV, system="Linux",
                    exists=self.exists, uid=1000)


def test_socket_path_resolution():
    assert cp.socket_path(ENV, uid=1000) == SOCK
    assert cp.socket_path({}, uid=1000) == SOCK
    assert cp.socket_path({"DOCKER_HOST": "unix:///tmp/x.sock"}, uid=1000) == "/tmp/x.sock"
    assert cp.socket_path({}, uid=0) == "/run/podman/podman.sock"


def test_socket_present_is_ok_without_asking_systemd():
    s = Systemd(file=True)
    assert cp.socket_status("podman", **s.kw()).kind == cp.SOCKET_OK
    assert s.calls == []


def test_unit_active_but_file_missing_is_its_own_state():
    s = Systemd(active=True, file=False)
    st = cp.socket_status("podman", **s.kw())
    assert st.kind == cp.SOCKET_UNIT_ACTIVE_FILE_MISSING and st.path == SOCK


def test_unit_inactive_and_file_missing_is_down():
    s = Systemd(active=False, file=False)
    assert cp.socket_status("podman", **s.kw()).kind == cp.SOCKET_DOWN


def test_no_systemctl_is_unknown():
    s = Systemd(file=False)
    kw = s.kw() | {"which": lambda n: None}
    assert cp.socket_status("podman", **kw).kind == cp.SOCKET_UNKNOWN


def test_docker_socket_is_not_applicable():
    assert cp.socket_status("docker", run=lambda *a, **k: pytest.fail()).kind == cp.SOCKET_NOT_APPLICABLE


@pytest.mark.parametrize("rc,kind", [(0, cp.SOCKET_OK), (125, cp.SOCKET_MACHINE_DOWN)])
def test_macos_windows_read_the_machine(rc, kind):
    run = lambda argv, **kw: _cp(argv, rc)  # noqa: E731
    for system in ("Darwin", "Windows"):
        assert cp.socket_status("podman", run=run, which=lambda n: "/b/" + n,
                                system=system).kind == kind


# ---------------------------------------------------------------------------
# heal_socket — act / leave-alone
# ---------------------------------------------------------------------------


def _heal(s, **kw):
    return cp.heal_socket("podman", sleep=lambda _s: None, wait_s=0, **s.kw(), **kw)


def test_heal_restarts_an_active_unit_whose_file_vanished():
    s = Systemd(active=True, file=False)
    h = _heal(s)
    assert h.healed
    assert ["systemctl", "--user", "restart", "podman.socket"] in s.calls
    assert h.actions == ["systemctl --user restart podman.socket"]


def test_heal_starts_a_down_unit():
    s = Systemd(active=False, file=False)
    h = _heal(s)
    assert h.healed and ["systemctl", "--user", "start", "podman.socket"] in s.calls


def test_heal_leaves_a_healthy_socket_alone():
    s = Systemd(file=True)
    h = _heal(s)
    assert h.healed and h.actions == [] and s.calls == []


def test_heal_does_nothing_on_uncertainty():
    s = Systemd(file=False)
    h = cp.heal_socket("podman", sleep=lambda _s: None, wait_s=0,
                       **(s.kw() | {"which": lambda n: None}))
    assert not h.healed and h.actions == [] and s.calls == []


def test_heal_failure_names_the_ledger_row():
    s = Systemd(active=True, file=False, verb_rc=1)
    h = _heal(s)
    assert not h.healed and h.deferral_cid == "compose_socket_heal_failed"


def test_heal_that_does_not_bring_the_file_back_is_a_failure():
    s = Systemd(active=True, file=False, heals=False)
    h = _heal(s)
    assert not h.healed and h.deferral_cid == "compose_socket_heal_failed"
    assert SOCK in h.reason


def test_heal_starts_the_podman_machine_on_macos():
    state = {"up": False}

    def run(argv, **kw):
        return _cp(argv, 0 if state["up"] else 125)

    def machine_start():
        state["up"] = True
        return True, "podman machine started"

    h = cp.heal_socket("podman", run=run, which=lambda n: "/b/" + n, system="Darwin",
                       machine_start=machine_start, sleep=lambda _s: None, wait_s=0)
    assert h.healed and h.actions == ["podman machine start"]


def test_docker_is_never_healed_here():
    h = cp.heal_socket("docker", run=lambda *a, **k: pytest.fail("no call"))
    assert not h.healed and h.actions == []


# ---------------------------------------------------------------------------
# runtime_reachable + runtime_reconcile's use of socket_status
# ---------------------------------------------------------------------------


def _info_run(info_rc, systemd: Systemd):
    def run(argv, **kw):
        if argv[:2] == ["podman", "info"]:
            return _cp(argv, info_rc)
        return systemd.run(argv, **kw)
    return run


@pytest.mark.parametrize("info_rc,active,file,expected", [
    (0, True, True, True),     # healthy
    (0, True, False, False),   # podman info passes, socket file gone: NOT reachable
    (0, False, False, True),   # socket never started: podman-compose works, compose heals later
    (125, True, True, False),  # podman info fails
])
def test_runtime_reachable(info_rc, active, file, expected):
    s = Systemd(active=active, file=file)
    kw = s.kw()
    kw["run"] = _info_run(info_rc, s)
    assert cp.runtime_reachable("podman", **{k: kw[k] for k in
                                             ("run", "which", "env", "system", "exists")}) is expected


@pytest.mark.parametrize("kind,called", [
    (cp.SOCKET_UNIT_ACTIVE_FILE_MISSING, True),
    (cp.SOCKET_OK, False),
    (cp.SOCKET_DOWN, False),
])
def test_runtime_reconcile_heals_the_socket_of_a_usable_podman(monkeypatch, kind, called):
    monkeypatch.setattr(cp, "socket_status", lambda rt, **kw: cp.SocketStatus(kind))
    monkeypatch.setattr(rr, "_status", lambda rt, which, run: "usable")
    started = []
    status, note = rr._start_if_down("podman", "usable",
                                     lambda rt: (started.append(rt), (True, "healed"))[1],
                                     lambda n: None, lambda *a, **k: None)
    assert bool(started) is called
    assert status == "usable"
    assert ("tried to start podman" in note) is called


def test_runtime_reconcile_never_socket_probes_docker(monkeypatch):
    monkeypatch.setattr(cp, "socket_status", lambda *a, **k: pytest.fail("no probe"))
    status, note = rr._start_if_down("docker", "usable", lambda rt: (True, ""), None, None)
    assert (status, note) == ("usable", "")


# ---------------------------------------------------------------------------
# GPU overlay by provider (L1-F10)
# ---------------------------------------------------------------------------


def _prov(engine, form="subcommand", runtime="podman"):
    fam = {"docker-compose": "docker", "docker": "docker", "podman-compose": "podman"}.get(engine, "unknown")
    return cp.ComposeProvider(form, engine, (), fam, runtime)


def test_overlay_follows_the_parsing_engine_not_the_runtime_name():
    # podman runtime, but `podman compose` delegates to docker-compose
    assert cp.overlay_for_provider(_prov(cp.ENGINE_DOCKER_COMPOSE), "nvidia") == "docker-compose.gpu.yml"
    # podman runtime, `podman compose` delegating to podman-compose (the form-only rule got this wrong)
    assert cp.overlay_for_provider(_prov(cp.ENGINE_PODMAN_COMPOSE), "nvidia") == "podman-compose.gpu.yml"
    assert cp.overlay_for_provider(_prov(cp.ENGINE_DOCKER, runtime="docker"), "nvidia") == "docker-compose.gpu.yml"
    assert cp.overlay_for_provider(None, "nvidia") is None


def test_amd_overlay_prefers_the_short_name_then_the_legacy_one(tmp_path: Path):
    p = _prov(cp.ENGINE_PODMAN_COMPOSE, form="standalone")
    assert cp.overlay_for_provider(p, "amd", tmp_path) is None
    (tmp_path / "podman-compose.amd-rocm.yml").write_text("x")
    assert cp.overlay_for_provider(p, "amd", tmp_path) == "podman-compose.amd-rocm.yml"
    (tmp_path / "podman-compose.rocm.yml").write_text("x")
    assert cp.overlay_for_provider(p, "amd", tmp_path) == "podman-compose.rocm.yml"


def test_unknown_engine_falls_back_to_the_form_rule():
    assert cp.overlay_for_provider(_prov(cp.ENGINE_UNKNOWN, "standalone"), "nvidia") == "podman-compose.gpu.yml"
    assert cp.overlay_for_provider(_prov(cp.ENGINE_UNKNOWN, "subcommand"), "nvidia") == "docker-compose.gpu.yml"


def test_service_adoption_overlay_delegates_to_the_one_home():
    assert service_adoption.gpu_overlay_for_form("subcommand") == cp.overlay_for_provider(
        cp.provider_from_form("subcommand"), "nvidia")
    assert not hasattr(service_adoption, "GPU_OVERLAY_BY_FORM")
