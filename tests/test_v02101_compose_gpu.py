# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 plan items 1 + 5 — one compose tool per runtime + post-start GPU
verification (`vco_lib.gpu_verify`, `vco_lib.containers.compose_command`,
install step 5, the wrapper pair).

No runtime, no compose, no container is ever really run: probes are injected
fakes; the wrapper tests source the scripts and drive their helper functions
with a recording fake interpreter; the CLI test uses a fake `podman` on PATH.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.common.child_env import child_env

import pytest

from vco_lib import compose_provider as cp
from vco_lib import containers
from vco_lib import gpu_verify as gv
from vco_lib import install_services_up as isu

REPO_ROOT = Path(__file__).resolve().parents[1]
SH = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.sh"
PS1 = REPO_ROOT / "scripts" / "launch-claude-mcp-stack.ps1"

BANNER_DC = '>>>> Executing external compose provider "/usr/local/bin/docker-compose". <<<<\n'
BANNER_PC = '>>>> Executing external compose provider "/usr/bin/podman-compose". <<<<\n'

DEVICES_OK = json.dumps([
    {"PathOnHost": "/dev/nvidia0", "PathInContainer": "/dev/nvidia0", "CgroupPermissions": ""},
    {"PathOnHost": "/dev/nvidiactl", "PathInContainer": "/dev/nvidiactl", "CgroupPermissions": ""},
])
DEVICES_EMPTY = "[]"
DEVICES_KFD = json.dumps([{"PathOnHost": "/dev/kfd", "PathInContainer": "/dev/kfd"}])
# docker's documented inspect shape (Engine API: HostConfig.DeviceRequests —
# DeviceRequest{Driver, Count, DeviceIDs, Capabilities, Options}); the
# compose GPU overlay's `deploy.resources.reservations.devices` block maps
# HERE, so a correct docker+NVIDIA container answers `[]` for Devices.
# Fixtures follow the documented JSON (no docker on the CI machines).
REQUESTS_NVIDIA_DRIVER = json.dumps([
    {"Driver": "nvidia", "Count": -1, "DeviceIDs": None,
     "Capabilities": [["gpu", "compute"]], "Options": {}}])
REQUESTS_GPU_CAPABILITY = json.dumps([
    {"Driver": "", "Count": "all", "DeviceIDs": None, "Capabilities": [["gpu"]]}])
REQUESTS_AMD = json.dumps([
    {"Driver": "amd", "Count": 1, "DeviceIDs": None, "Capabilities": [["gpu"]]}])
REQUESTS_EMPTY = "[]"


def _cp(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


# ---------------------------------------------------------------------------
# 1. one compose tool per runtime (vco_lib.containers.compose_command)
# ---------------------------------------------------------------------------


def test_podman_with_both_tools_pins_standalone_podman_compose(tmp_path: Path):
    """The incident shape: `podman compose version` answers 0 (a delegating
    shim) AND a standalone podman-compose exists — the standalone wins, and
    no subcommand probe runs at all."""
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(list(argv))
        return _cp(argv, 0)

    which = lambda n: "/usr/bin/podman-compose" if n == "podman-compose" else None  # noqa: E731
    got = containers.compose_command("podman", which=which, run=run, home=tmp_path)
    assert got == (["podman-compose"], "standalone")
    assert calls == []


def test_podman_without_standalone_falls_to_the_subcommand(tmp_path: Path):
    got = containers.compose_command("podman", which=lambda _n: None,
                                     run=lambda argv, **_k: _cp(argv, 0),
                                     home=tmp_path / "no-such-home")
    assert got == (["podman", "compose"], "subcommand")


def test_docker_keeps_the_subcommand_first(tmp_path: Path):
    which = lambda n: "/usr/bin/docker-compose" if n == "docker-compose" else None  # noqa: E731
    got = containers.compose_command("docker", which=which,
                                     run=lambda argv, **_k: _cp(argv, 0), home=tmp_path)
    assert got == (["docker", "compose"], "subcommand")


# ---------------------------------------------------------------------------
# 2. the GPU verdict truth table (pure decision + thin probe)
# ---------------------------------------------------------------------------


def _probe_run(devices: str, rc: int = 0):
    return lambda argv, **_kw: _cp(argv, rc, devices if rc == 0 else "", "boom" if rc else "")


def test_probe_ok_when_the_container_holds_the_device():
    v = gv.probe("podman", run=_probe_run(DEVICES_OK))
    assert v.kind == gv.GPU_OK and "/dev/nvidia0" in v.detail


def test_probe_missing_when_the_container_answers_with_no_device():
    """The incident's exact shape: a docker-compose-created container whose
    device list is EMPTY."""
    v = gv.probe("podman", run=_probe_run(DEVICES_EMPTY))
    assert v.kind == gv.GPU_MISSING and "no nvidia GPU device" in v.detail


def test_probe_amd_vendor_matches_kfd_and_dri():
    assert gv.probe("podman", gpu_vendor="amd", run=_probe_run(DEVICES_KFD)).kind == gv.GPU_OK
    dri = json.dumps([{"PathOnHost": "/dev/dri/renderD129", "PathInContainer": "/dev/dri/renderD129"}])
    assert gv.probe("podman", gpu_vendor="amd", run=_probe_run(dri)).kind == gv.GPU_OK
    # ...and an amd vendor does NOT match nvidia nodes (the mixed host).
    assert gv.probe("podman", gpu_vendor="amd", run=_probe_run(DEVICES_OK)).kind == gv.GPU_MISSING


def test_probe_unknown_when_it_cannot_tell():
    """rc != 0 (container absent), unparseable JSON — never a verdict."""
    assert gv.probe("podman", run=_probe_run("", rc=125)).kind == gv.GPU_UNKNOWN
    garbage = lambda argv, **_kw: _cp(argv, 0, "{not json", "")  # noqa: E731
    assert gv.probe("podman", run=garbage).kind == gv.GPU_UNKNOWN
    def boom(argv, **_kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert gv.probe("podman", run=boom).kind == gv.GPU_UNKNOWN


def _docker_probe_run(devices: str, requests: str, *, req_rc: int = 0):
    """A fake ``run`` that answers by the ``--format`` argument — docker's
    two inspect fields are separate calls."""
    def run(argv, **_kw):
        fmt = argv[argv.index("--format") + 1]
        if "DeviceRequests" in fmt:
            return _cp(argv, req_rc, requests if req_rc == 0 else "", "")
        return _cp(argv, 0, devices, "")
    return run


def test_probe_docker_gpu_via_device_requests_driver_nvidia():
    """The B2 blocker's exact shape: a correct docker+NVIDIA container (the
    compose overlay's reservations.devices block) answers EMPTY Devices and
    holds its GPU in a DeviceRequest."""
    v = gv.probe("docker", run=_docker_probe_run(DEVICES_EMPTY, REQUESTS_NVIDIA_DRIVER))
    assert v.kind == gv.GPU_OK, v.detail
    assert "device request" in v.detail and "driver=nvidia" in v.detail


def test_probe_docker_gpu_via_amd_driver_request():
    """A driver-tagged AMD request satisfies an AMD expectation."""
    assert gv.probe("docker", gpu_vendor="amd", run=_docker_probe_run(
        DEVICES_EMPTY, REQUESTS_AMD)).kind == gv.GPU_OK


def test_probe_docker_foreign_vendor_request_is_missing():
    """SF-2: the request path must be vendor-discriminating like the device
    list — an AMD request while NVIDIA is expected (and vice versa) is the
    expected-GPU-not-in-container shape, never a false ok. Empty device list
    + no matching request → MISSING (the existing rule)."""
    v = gv.probe("docker", run=_docker_probe_run(DEVICES_EMPTY, REQUESTS_AMD))
    assert v.kind == gv.GPU_MISSING, v.detail
    v = gv.probe("docker", gpu_vendor="amd",
                 run=_docker_probe_run(DEVICES_EMPTY, REQUESTS_NVIDIA_DRIVER))
    assert v.kind == gv.GPU_MISSING, v.detail


def test_probe_docker_bare_gpu_capability_is_unknown():
    """SF-2: a driverless, id-less ``["gpu"]`` request carries no vendor
    evidence — docker allocates whichever GPU the host has, so the probe
    cannot confirm it is the expected vendor and answers UNKNOWN (no row):
    never a false ok, never a false missing."""
    for vendor in (None, "amd"):
        v = gv.probe("docker", gpu_vendor=vendor, run=_docker_probe_run(
            DEVICES_EMPTY, REQUESTS_GPU_CAPABILITY))
        assert v.kind == gv.GPU_UNKNOWN, v.detail


def test_probe_docker_empty_devices_and_empty_requests_is_missing():
    v = gv.probe("docker", run=_docker_probe_run(DEVICES_EMPTY, REQUESTS_EMPTY))
    assert v.kind == gv.GPU_MISSING, v.detail
    assert "device requests: (none)" in v.detail


def test_probe_docker_requests_probe_failure_is_unknown():
    """The second field unreadable ⇒ could-not-tell, never a false row."""
    v = gv.probe("docker", run=_docker_probe_run(DEVICES_EMPTY, "", req_rc=125))
    assert v.kind == gv.GPU_UNKNOWN, v.detail


def test_probe_podman_never_asks_for_device_requests():
    """Podman's inspect has NO DeviceRequests field (verified live — the
    template errors), so a podman answer comes from Devices alone and the
    requests query is never made."""
    def run(argv, **_kw):
        fmt = argv[argv.index("--format") + 1]
        assert "DeviceRequests" not in fmt, "podman cannot evaluate that field"
        return _cp(argv, 0, DEVICES_OK, "")
    assert gv.probe("podman", run=run).kind == gv.GPU_OK


def test_decide_truth_table():
    ok = gv.GPUVerdict(gv.GPU_OK, "d")
    missing = gv.GPUVerdict(gv.GPU_MISSING, "d")
    unknown = gv.GPUVerdict(gv.GPU_UNKNOWN, "d")
    assert gv.decide(False, missing).kind == gv.GPU_SKIP       # not applied → no check owed
    assert gv.decide(True, ok).kind == gv.GPU_OK
    assert gv.decide(True, missing).kind == gv.GPU_MISSING
    assert gv.decide(True, unknown).kind == gv.GPU_UNKNOWN     # could not tell → caller does NOTHING


def test_deferral_entry_only_for_missing_and_recipe_resolves():
    for v in (gv.GPUVerdict(gv.GPU_OK), gv.GPUVerdict(gv.GPU_UNKNOWN, "x"),
              gv.GPUVerdict(gv.GPU_SKIP)):
        assert gv.deferral_entry(v, runtime="podman") is None
    entry = gv.deferral_entry(gv.GPUVerdict(gv.GPU_MISSING, "vco_ollama exposes no nvidia GPU device"),
                              runtime="podman", infra_dir=Path("/infra"), rerun_cmd="python install.py --update")
    assert entry is not None
    assert entry.condition_id == gv.CID_COMPOSE_GPU_DEVICE_MISSING
    recipe = entry.command_to_apply
    # every command the recipe prints must exist / help (a printed command is
    # shipped code): the CDI-capable compose tool, the CDI regeneration, the
    # refresh unit, the recreate, the re-run.
    assert "podman-compose" in recipe
    assert "nvidia-ctk cdi generate" in recipe
    assert "nvidia-cdi-refresh.path" in recipe
    assert "up -d --force-recreate ollama" in recipe
    assert "cd /infra" in recipe
    assert "python install.py --update" in recipe
    # docker hosts get the toolkit-into-daemon recipe instead of CDI.
    docker_entry = gv.deferral_entry(gv.GPUVerdict(gv.GPU_MISSING, "x"), runtime="docker",
                                     rerun_cmd="python install.py --update")
    assert docker_entry is not None
    docker_recipe = docker_entry.command_to_apply
    assert "nvidia-ctk runtime configure --runtime=docker" in docker_recipe
    assert "podman-compose" not in docker_recipe


def test_condition_is_registered_action_required():
    from vco_lib.deferral_registry import disposition_for

    assert disposition_for(gv.CID_COMPOSE_GPU_DEVICE_MISSING) == "action_required"


# ---------------------------------------------------------------------------
# 3. install step 5 wiring (overlay applied → check; unknown → nothing)
# ---------------------------------------------------------------------------


class Step5World:
    """A minimal fake harness around isu.compose_up_step (no runtime)."""

    def __init__(self, tmp: Path, *, banner=BANNER_PC, compose=("podman-compose",),
                 has_gpu=True, gpu_vendor="nvidia",
                 devices: "str | tuple[int, str]" = DEVICES_OK, compose_rc=0):
        self.infra = tmp / "infrastructure"
        self.infra.mkdir()
        self.compose_file = self.infra / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")
        for name in ("docker-compose.gpu.yml", "podman-compose.gpu.yml"):
            (self.infra / name).write_text("services: {}\n", encoding="utf-8")
        self.banner, self.compose, self.devices = banner, list(compose), devices
        self.compose_rc = compose_rc
        self.compose_calls, self.probe_calls, self.inspect_devices_calls = [], [], []
        self.entries: list = []
        self.plan = isu.Step5Plan(
            runtime="podman", has_gpu=has_gpu, gpu_vendor=gpu_vendor, infra_dir=self.infra,
            compose_file=self.compose_file, embed_config={}, services_to_start=["ollama"],
            services_to_recreate=[], recreate_for_rebuild=[], build_services=[],
            args=argparse.Namespace(update=True), detected={}, deferral_report=self,
            install_root=tmp)

    def add_entry(self, e):
        self.entries.append(e)

    @property
    def cids(self):
        return [e.condition_id for e in self.entries]

    def compose_run(self, argv, **_kw):
        self.compose_calls.append(list(argv))
        return _cp(argv, self.compose_rc, "", "")

    def run(self, argv, **_kw):
        self.probe_calls.append(list(argv))
        a = list(argv)
        if a[-1] == "version":
            return _cp(argv, 0, "", self.banner)
        if a[1] == "inspect" and "vco_ollama" in a:
            self.inspect_devices_calls.append(a)
            rc = 0 if isinstance(self.devices, str) else self.devices[0]
            out = self.devices if isinstance(self.devices, str) else self.devices[1]
            return _cp(argv, rc, out, "")
        # a probe this harness does not model (compose recovery, …) — answer
        # "nothing found" so the step under test proceeds.
        return _cp(argv, 1, "", "")

    def hooks(self):
        return isu.Step5Hooks(
            log_event=lambda *a, **k: None,
            reachable=lambda rt: True,
            heal_podman=lambda: cp.HealResult(True, [], "ok"),
            try_start_docker=lambda: (False, "n/a"),
            emit_podman_start_failed=lambda report, detail: None,
            get_compose_command=lambda rt: list(self.compose),
            write_infra_env=lambda cfg: None,
            compose_subst_env=lambda cfg: {},
            gpu_tool_live=lambda tool, args: False,
            ensure_cdi=lambda: None,
            run=self.run, compose_run=self.compose_run,
            system=lambda: "Linux",
            socket_status=lambda rt: cp.SocketStatus(cp.SOCKET_OK))

    def go(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = isu.compose_up_step(self.plan, self.hooks())
        return outcome, buf.getvalue()


def test_step5_gpu_spec_end_to_end_nvidia_overlay_and_check(tmp_path: Path):
    """Item 5's nvidia row: GPU plan → the compose argv carries the provider's
    overlay + `--profile gpu` AND the post-start check inspects the running
    container."""
    w = Step5World(tmp_path, devices=DEVICES_OK)
    outcome, out = w.go()
    assert outcome == isu.OK
    argv = w.compose_calls[0]
    assert str(w.infra / "podman-compose.gpu.yml") in argv
    assert "--profile" in argv and argv[argv.index("--profile") + 1] == "gpu"
    assert w.inspect_devices_calls, "the post-start check never ran"
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING not in w.cids


def test_step5_cpu_spec_end_to_end_no_overlay_no_check(tmp_path: Path):
    """Item 5's cpu row: no GPU → no overlay in the argv, no post-start
    probe, no row."""
    w = Step5World(tmp_path, has_gpu=False, devices=DEVICES_EMPTY)
    outcome, _ = w.go()
    assert outcome == isu.OK
    argv = w.compose_calls[0]
    assert ".gpu.yml" not in " ".join(argv)
    assert w.inspect_devices_calls == []
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING not in w.cids


def test_step5_overlay_applied_and_device_missing_defers(tmp_path: Path):
    w = Step5World(tmp_path, devices=DEVICES_EMPTY)
    outcome, out = w.go()
    assert outcome == isu.OK
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING in w.cids
    assert "WARNING" in out and "UPDATE_DEFERRED.md" in out


def test_step5_probe_could_not_tell_records_nothing(tmp_path: Path):
    w = Step5World(tmp_path, devices=(125, ""))
    w.go()
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING not in w.cids


def test_step5_no_cdi_capable_tool_defers_before_composing(tmp_path: Path):
    """Podman + only a docker-compose delegate: the row lands even when
    compose itself fails (the silent-CPU start is known in advance)."""
    w = Step5World(tmp_path, banner=BANNER_DC, compose=("podman", "compose"),
                   devices=(125, ""), compose_rc=1)
    outcome, out = w.go()
    assert outcome == isu.FAIL
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING in w.cids
    assert "will start CPU-only" in out
    assert str(w.infra / "docker-compose.gpu.yml") in w.compose_calls[0]


def test_step5_docker_compose_on_docker_is_not_the_no_cdi_case(tmp_path: Path):
    """docker runtime + the docker plugin: a normal engine, no pre-start
    row (the post-start check still verifies)."""
    w = Step5World(tmp_path, compose=("docker", "compose"))
    w.plan.runtime = "docker"
    outcome, _ = w.go()
    assert outcome == isu.OK
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING not in w.cids


# ---------------------------------------------------------------------------
# 4. the CLI (the wrappers' entry point)
# ---------------------------------------------------------------------------


def _fake_podman_dir(tmp_path: Path, devices: str) -> Path:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "podman").write_text("#!/bin/sh\nif [ \"$1\" = inspect ]; then printf '%s' \"$DEVICES\"; fi\nexit 0\n")
    (fake / "podman").chmod(0o755)
    return fake


def _cli(tmp_path: Path, *extra: str, devices: str = DEVICES_EMPTY):
    fake = _fake_podman_dir(tmp_path, devices)
    base = {k: v for k, v in os.environ.items() if not k.startswith("VCT_")}
    env = child_env(
        base,
        PATH=str(fake) + os.pathsep + base.get("PATH", ""),
        DEVICES=devices,
    )
    return subprocess.run(
        [sys.executable, "-m", "vco_lib.gpu_verify", "--runtime", "podman", "--json", *extra],
        capture_output=True, text=True, timeout=60, cwd=str(tmp_path), env=env)


def test_cli_missing_writes_the_ledger_row(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    proc = _cli(tmp_path, "--install-root", str(root), "--reason", "degraded")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["kind"] == "missing"
    assert payload["condition"] == gv.CID_COMPOSE_GPU_DEVICE_MISSING
    ledger = root / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert ledger.is_file(), "the row never landed in UPDATE_DEFERRED.md"
    assert gv.CID_COMPOSE_GPU_DEVICE_MISSING in ledger.read_text(encoding="utf-8")


def test_cli_ok_writes_nothing_and_exits_zero(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    proc = _cli(tmp_path, "--install-root", str(root), devices=DEVICES_OK)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["kind"] == "ok"
    assert not (root / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()


def test_cli_unknown_without_install_root_records_nothing(tmp_path: Path):
    proc = _cli(tmp_path, devices="")  # inspect prints empty → not JSON → unknown
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["kind"] == "unknown"


# ---------------------------------------------------------------------------
# 5. the wrapper pair (.sh + .ps1 call the SAME Python check, no mirror)
# ---------------------------------------------------------------------------

BASH = shutil.which("bash")
PWSH = shutil.which("pwsh") or shutil.which("powershell")

_EXPECTED_ARGS = ["-m", "vco_lib.gpu_verify", "--runtime", "podman", "--reason", "degraded"]


@pytest.mark.skipif(BASH is None, reason="bash not available (script is Linux/macOS only)")
def test_sh_wrapper_success_path_calls_the_python_check(tmp_path: Path):
    record = tmp_path / "gv-args.txt"
    fake_py = tmp_path / "fake-stack-py"
    fake_py.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\necho "gpu-verify: ok — fake"\n')
    fake_py.chmod(0o755)
    root, wd = tmp_path / "root", tmp_path / "infra"
    wd.mkdir()
    script = (
        f'source "{SH}"; '
        'STACK_PY="$1"; _VCT_OWN_ROOT="$2"; VCT_STACK_WORKING_DIR="$3"; '
        'VCT_STACK_LOG_FILE="$4"; '
        'gpu_verify_after_up podman degraded; rc=$?; exit $rc'
    )
    proc = subprocess.run(
        [BASH or "bash", "-c", script, "_", str(fake_py), str(root), str(wd), str(tmp_path / "log")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    args = record.read_text().splitlines()
    assert args[:len(_EXPECTED_ARGS)] == _EXPECTED_ARGS
    assert args[args.index("--install-root") + 1] == str(root)
    assert args[args.index("--infra-dir") + 1] == str(wd)
    assert "gpu-verify: ok — fake" in proc.stdout  # logged, not swallowed


@pytest.mark.skipif(BASH is None, reason="the fake interpreter is a bash script")
def test_sh_wrapper_check_never_fails_the_boot(tmp_path: Path):
    record = tmp_path / "gv-args.txt"
    fake_py = tmp_path / "fake-stack-py"
    fake_py.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\necho "boom" >&2\nexit 3\n')
    fake_py.chmod(0o755)
    script = (
        f'source "{SH}"; '
        'STACK_PY="$1"; _VCT_OWN_ROOT="$2"; '
        'VCT_STACK_LOG_FILE="$3"; '
        'gpu_verify_after_up podman overlay; rc=$?; exit $rc'
    )
    proc = subprocess.run(
        [BASH or "bash", "-c", script, "_", str(fake_py), str(tmp_path), str(tmp_path / "log")],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr  # a failing check is logged, never fatal
    assert "rc=3" in proc.stdout


@pytest.mark.skipif(PWSH is None, reason="no PowerShell runtime (pwsh) on PATH")
@pytest.mark.skipif(BASH is None, reason="the fake interpreter is a bash script")
def test_ps1_wrapper_parity_same_check_same_args(tmp_path: Path):
    record = tmp_path / "gv-args-ps1.txt"
    fake_py = tmp_path / "fake-stack-py"
    fake_py.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\n')
    fake_py.chmod(0o755)
    root, wd = tmp_path / "root", tmp_path / "infra"
    wd.mkdir()
    ps_cmd = (
        f". '{PS1}'; "
        f"$script:VctOwnRoot = '{root}'; $script:VctStackWorkingDir = '{wd}'; "
        f"$script:VctStackLogFile = '{tmp_path / 'log'}'; "
        f"Invoke-GpuVerify -Python '{fake_py}' -RuntimeBin 'podman' -Reason 'degraded'"
    )
    proc = subprocess.run(
        [PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    args = record.read_text().splitlines()
    assert args[:len(_EXPECTED_ARGS)] == _EXPECTED_ARGS
    assert args[args.index("--install-root") + 1] == str(root)
    assert args[args.index("--infra-dir") + 1] == str(wd)
