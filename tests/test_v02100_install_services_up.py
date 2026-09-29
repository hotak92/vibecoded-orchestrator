# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-04 — install.py step 5's extracted compose tail
(`vco_lib.install_services_up`), driven end to end with fakes.

No runtime, no systemctl, no compose is ever run: `compose_run` and `run` are
injected; install.py's helpers are stubs in :class:`Step5Hooks`."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shlex
import subprocess
from pathlib import Path

from vco_lib import compose_provider as cp
from vco_lib import compose_recovery as cr
from vco_lib import install_services_up as isu

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = {c["id"]: c["stderr"] for c in json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "compose_failure_corpus.json").read_text(
        encoding="utf-8"))["cases"]}
BANNER_DC = '>>>> Executing external compose provider "/usr/local/bin/docker-compose". <<<<\n'
BANNER_PC = '>>>> Executing external compose provider "/usr/bin/podman-compose". <<<<\n'


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Ledger:
    def __init__(self):
        self.entries = []

    def add_entry(self, e):
        self.entries.append(e)

    @property
    def cids(self):
        return [e.condition_id for e in self.entries]


class World:
    def __init__(self, tmp: Path, *, runtime="podman", compose=("podman", "compose"),
                 banner=BANNER_DC, compose_results=((0, ""),), reachable=True,
                 has_gpu=True, gpu_vendor="nvidia", ps_attached=""):
        self.infra = tmp / "infrastructure"
        self.infra.mkdir()
        self.compose_file = self.infra / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")
        for name in ("docker-compose.gpu.yml", "podman-compose.gpu.yml"):
            (self.infra / name).write_text("services: {}\n", encoding="utf-8")
        self.runtime, self.compose, self.banner = runtime, list(compose), banner
        self.results = list(compose_results)
        self.compose_calls, self.probe_calls, self.events = [], [], []
        self.reachable_v, self.ps_attached = reachable, ps_attached
        self.started, self.cdi = [], []
        self.ledger = Ledger()
        self.plan = isu.Step5Plan(
            runtime=runtime, has_gpu=has_gpu, gpu_vendor=gpu_vendor, infra_dir=self.infra,
            compose_file=self.compose_file, embed_config={}, services_to_start=[],
            services_to_recreate=["code_embed"], recreate_for_rebuild=["code_embed"],
            build_services=["code_embed"], args=argparse.Namespace(update=False),
            detected={}, deferral_report=self.ledger, install_root=tmp)

    # -- seams ---------------------------------------------------------------
    def compose_run(self, argv, **kw):
        self.compose_calls.append(list(argv))
        rc, err = self.results.pop(0)
        return _cp(argv, rc, "", err)

    def run(self, argv, **kw):
        self.probe_calls.append(list(argv))
        if argv[-1] == "version":
            return _cp(argv, 0, "", self.banner)
        if argv[1] == "ps" and any(a.startswith("network=") for a in argv):
            return _cp(argv, 0, self.ps_attached)
        if argv[1:3] == ["network", "rm"]:
            return _cp(argv)
        if argv[1] == "inspect":  # the provider-mismatch label read: unreadable here
            return _cp(argv, 125, "", "no such container")
        raise AssertionError(f"unexpected probe {argv}")

    def hooks(self, **over):
        h = dict(
            log_event=lambda *a, **k: self.events.append((a, k)),
            reachable=lambda rt: self.reachable_v,
            try_start_podman=lambda: (self.started.append("podman"), (False, "restart failed"))[1],
            try_start_docker=lambda: (False, "n/a"),
            emit_podman_start_failed=lambda report, detail: report.add_entry(
                type("E", (), {"condition_id": "podman_daemon_start_failed"})()),
            get_compose_command=lambda rt: list(self.compose),
            write_infra_env=lambda cfg: None,
            compose_subst_env=lambda cfg: {},
            gpu_tool_live=lambda tool, args: False,
            ensure_cdi=lambda: self.cdi.append(1),
            run=self.run, compose_run=self.compose_run,
            system=lambda: "Linux",
            socket_status=lambda rt: cp.SocketStatus(cp.SOCKET_OK),
        )
        h.update(over)
        return isu.Step5Hooks(**h)

    def go(self, **over):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = isu.compose_up_step(self.plan, self.hooks(**over))
        return outcome, buf.getvalue()


def test_success_argv_carries_the_full_f_chain_and_project(tmp_path):
    w = World(tmp_path)
    outcome, out = w.go()
    assert outcome == isu.OK and "  OK" in out
    argv = w.compose_calls[0]
    assert argv[:4] == ["podman", "compose", "-f", str(w.compose_file)]
    assert ["-p", "infrastructure"] == argv[argv.index("-p"):argv.index("-p") + 2]
    assert "--build" in argv and "--force-recreate" in argv and argv[-1] == "code_embed"


def test_provider_picks_the_overlay_docker_compose_delegate(tmp_path):
    w = World(tmp_path, banner=BANNER_DC)
    _, out = w.go()
    argv = w.compose_calls[0]
    assert str(w.infra / "docker-compose.gpu.yml") in argv
    assert str(w.infra / "podman-compose.gpu.yml") not in argv
    assert "docker-compose" in out and w.cdi == [1]  # podman still needs the CDI spec


def test_provider_picks_the_overlay_podman_compose_delegate(tmp_path):
    w = World(tmp_path, banner=BANNER_PC)
    w.go()
    assert str(w.infra / "podman-compose.gpu.yml") in w.compose_calls[0]


def test_failure_prints_the_exact_manual_command_and_a_typed_cause(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["podman_name_conflict_known_container"]),))
    outcome, out = w.go()
    assert outcome == isu.FAIL
    assert shlex.join(w.compose_calls[0]) in out
    assert "not running" not in out.lower()
    assert "was not created by this compose" in out


def test_network_label_with_nothing_attached_self_heals_and_retries(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["field_2026_09_29_network_label"]), (0, "")))
    outcome, out = w.go()
    assert outcome == isu.OK
    assert ["podman", "network", "rm", "infrastructure_default"] in w.probe_calls
    assert len(w.compose_calls) == 2 and w.ledger.cids == []


def test_network_label_with_attached_containers_is_ledgered_and_left(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["field_2026_09_29_network_label"]),),
              ps_attached="abc123def456\n")
    outcome, _ = w.go()
    assert outcome == isu.FAIL
    assert not any(c[1:3] == ["network", "rm"] for c in w.probe_calls)
    assert cr.CID_NETWORK_LABEL_ATTACHED in w.ledger.cids


def test_build_flag_rejected_retries_once_and_warns_with_the_full_command(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["podman_compose_rejects_build"]), (0, "")))
    outcome, out = w.go()
    assert outcome == isu.OK
    assert "--build" not in w.compose_calls[1]
    assert "NOT rebuilt" in out
    assert shlex.join(w.compose_calls[0]) in out  # the full argv, WITH --build


def test_real_build_failure_is_surfaced_not_silently_retried(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["buildkit_failed_to_solve"]),))
    outcome, out = w.go()
    assert outcome == isu.FAIL and len(w.compose_calls) == 1
    assert "failed to solve" in out
    assert "does not accept `--build`" not in out


def test_update_with_services_answering_continues_and_keeps_first_stderr(tmp_path):
    w = World(tmp_path, compose_results=((1, CORPUS["field_2026_09_29_network_label"]),
                                         (1, "Error: something else\n")))
    w.plan.args = argparse.Namespace(update=True)
    w.plan.detected = {"weaviate_url": "http://w", "ollama_url": "http://o",
                       "code_embed_url": "http://c"}
    outcome, out = w.go()
    assert outcome == isu.CONTINUE
    row = [e for e in w.ledger.entries if e.condition_id == "services_compose_up_failed"][0]
    assert "incorrect label" in row.detected  # the FIRST stderr, not the retry's
    assert "After recovery, compose failed again" in out


def test_preflight_heals_a_vanished_socket_file(tmp_path):
    w = World(tmp_path, reachable=False)
    status = cp.SocketStatus(cp.SOCKET_UNIT_ACTIVE_FILE_MISSING,
                             "/run/user/1000/podman/podman.sock", "podman.socket")
    outcome, out = w.go(socket_status=lambda rt: status,
                        try_start_podman=lambda: (w.started.append("p"), (True, "ok"))[1])
    assert w.started == ["p"] and outcome == isu.OK
    assert "/run/user/1000/podman/podman.sock" in out
    assert "not running" not in out.lower()


def test_preflight_socket_heal_failure_is_ledgered(tmp_path):
    w = World(tmp_path, reachable=False)
    status = cp.SocketStatus(cp.SOCKET_UNIT_ACTIVE_FILE_MISSING, "/s", "podman.socket")
    w.go(socket_status=lambda rt: status)
    assert cr.CID_SOCKET_HEAL_FAILED in w.ledger.cids


def test_preflight_leaves_a_reachable_runtime_alone(tmp_path):
    w = World(tmp_path, reachable=True)
    w.go(socket_status=lambda rt: (_ for _ in ()).throw(AssertionError("no probe")))
    assert w.started == []


def test_timeout_fails(tmp_path):
    w = World(tmp_path)

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    outcome, out = w.go(compose_run=boom)
    assert outcome == isu.FAIL and "timed out" in out


def test_compose_timeout_env():
    assert isu.compose_timeout_s({}) == 900
    assert isu.compose_timeout_s({"VCT_INSTALL_DOCKER_TIMEOUT": "30"}) == 60
    assert isu.compose_timeout_s({"VCT_INSTALL_DOCKER_TIMEOUT": "1800"}) == 1800
    assert isu.compose_timeout_s({"VCT_INSTALL_DOCKER_TIMEOUT": "x"}) == 900
