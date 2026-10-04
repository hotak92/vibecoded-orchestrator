# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""vco_lib.gpu_verify — did the GPU the spec asked for reach the container?

v0.2.101 (plan items 1 + 5). Every hardware-spec choice (GPU overlay,
embedding tier, low-resource, ROCm) was proven only up to the compose argv
or the ``.env`` write — never at the RUNNING container. On 2026-09-29 an
external docker-compose provider silently dropped the podman CDI device
``nvidia.com/gpu=all`` from the GPU overlay: ``vco_ollama`` started
CPU-only ("offloaded 0/29 layers to GPU"), ``install.py`` exited 0, and
nothing said so for days.

This module is the ONE post-start verifier (pure decision + ONE thin
probe, every dependency injectable):

* :func:`probe` asks the runtime which devices the container actually
  holds — ``<runtime> inspect --format '{{json .HostConfig.Devices}}'`` —
  positive evidence from the container itself, not from the spec. A
  GPU-less container answers with an empty (or non-matching) device list;
  that is the incident's exact shape.
* :func:`decide` is the pure truth table: GPU not expected → ``skip`` (no
  check, no row); expected + device present → ``ok``; expected + the
  container answers with NO matching device → ``missing``; the probe could
  not tell (container absent, inspect failed, unparseable output) →
  ``unknown`` — and UNKNOWN DOES NOTHING (the conservative default; no
  ledger row on uncertainty).
* :func:`deferral_entry` renders the ``compose_gpu_device_missing`` row
  (registered in ``vco_lib/deferral_conditions.toml``) with the recipe
  that repairs it: a compose tool that keeps the CDI ``devices:`` spec,
  the nvidia-ctk CDI regeneration, the CDI refresh unit, the recreate
  command, the ``--update`` re-run.

Callers (no shell mirror — the wrappers shell into the CLI):

* install.py step 5 (``vco_lib.install_services_up.compose_up_step``):
  after a successful compose-up whose argv carried the GPU overlay, and
  BEFORE it when the detected provider cannot deliver CDI devices at all
  (a podman runtime whose only compose tool delegates to docker-compose);
* the GPU-safe wrappers ``scripts/launch-claude-mcp-stack.{sh,ps1}`` on
  their success path — including the CDI-wait CPU degrade — via
  ``python -m vco_lib.gpu_verify``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from vco_lib import tool_search_dirs as _tsd
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "CID_COMPOSE_GPU_DEVICE_MISSING",
    "GPU_OK", "GPU_MISSING", "GPU_UNKNOWN", "GPU_SKIP",
    "OLLAMA_CONTAINER", "REASON_OVERLAY", "REASON_DEGRADED", "REASON_NO_CDI_TOOL",
    "GPUVerdict", "probe", "decide", "deferral_entry", "main",
]

#: The registered deferral condition (deferral_conditions.toml).
CID_COMPOSE_GPU_DEVICE_MISSING = "compose_gpu_device_missing"

#: The container the check inspects — the compose files pin
#: ``container_name: vco_ollama``, so the name is runtime-stable.
OLLAMA_CONTAINER = "vco_ollama"

GPU_OK = "ok"
GPU_MISSING = "missing"
GPU_UNKNOWN = "unknown"
GPU_SKIP = "skip"

#: Why the caller expects a GPU in the container (shapes the row's text).
REASON_OVERLAY = "overlay"            # the GPU overlay was applied
REASON_DEGRADED = "degraded"          # host has a GPU but compose fell back to CPU
REASON_NO_CDI_TOOL = "no-cdi-tool"    # the only compose tool drops CDI devices

_PROBE_TIMEOUT_S = 30

#: Host/device path prefixes that positively answer "a <vendor> GPU is in
#: the container". NVIDIA: the resolved CDI device nodes (the repaired
#: 2026-10-02 container lists /dev/nvidia0, /dev/nvidiactl, …) or the CDI
#: tag itself. AMD ROCm: the compute node /dev/kfd (unambiguous) or the
#: DRM render nodes /dev/dri (present on ROCm overlays).
_DEVICE_PREFIXES = {
    "nvidia": ("/dev/nvidia",),
    "amd": ("/dev/kfd", "/dev/dri"),
}
#: CDI vendor tags that may appear verbatim in the device list / inspect
#: output before the runtime resolves them to device nodes.
_CDI_TAGS = {
    "nvidia": ("nvidia.com/gpu",),
    "amd": ("amd.com/gpu",),
}


@dataclass(frozen=True)
class GPUVerdict:
    """``kind`` plus the human evidence line that produced it."""

    kind: str
    detail: str = ""
    evidence: str = ""


def _vendor(gpu_vendor: Optional[str]) -> str:
    return "amd" if (gpu_vendor or "").lower() == "amd" else "nvidia"


def _device_paths(devices: Any) -> list[str]:
    """Every host/container path the inspect JSON names, strings included."""
    paths: list[str] = []
    if isinstance(devices, list):
        for d in devices:
            if isinstance(d, dict):
                for key in ("PathOnHost", "PathInContainer"):
                    val = d.get(key)
                    if isinstance(val, str) and val:
                        paths.append(val)
            elif isinstance(d, str):
                paths.append(d)
    return paths


def _matching_device(paths: Sequence[str], raw: str, vendor: str) -> Optional[str]:
    prefixes = _DEVICE_PREFIXES[vendor]
    for p in paths:
        if any(p.startswith(pre) for pre in prefixes):
            return p
    for tag in _CDI_TAGS[vendor]:
        if tag in raw:
            return tag
    return None


def probe(
    runtime: str,
    container: str = OLLAMA_CONTAINER,
    gpu_vendor: Optional[str] = None,
    *,
    run: Optional[Callable[..., Any]] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
    timeout_s: int = _PROBE_TIMEOUT_S,
) -> GPUVerdict:
    """ONE thin probe: what does ``<runtime> inspect`` say the container
    holds? ``ok`` / ``missing`` (positive answers) / ``unknown`` (the probe
    could not tell — never a verdict on uncertainty)."""
    vendor = _vendor(gpu_vendor)
    _run = run or _tsd.run
    exe = (which or _tsd.which)(runtime) or runtime
    try:
        res = _run([exe, "inspect", "--format", "{{json .HostConfig.Devices}}", container],
                   capture_output=True, text=True, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return GPUVerdict(GPU_UNKNOWN, f"`{runtime} inspect {container}` could not run ({exc})")
    if res.returncode != 0:
        err = (res.stderr or "").strip().splitlines()
        tail = err[-1] if err else f"exit {res.returncode}"
        return GPUVerdict(GPU_UNKNOWN, f"`{runtime} inspect {container}` failed: {tail}")
    raw = (res.stdout or "").strip()
    if not raw:
        # rc 0 but no output is an anomalous answer, not evidence of a
        # GPU-less container — never a verdict on it.
        return GPUVerdict(GPU_UNKNOWN, f"`{runtime} inspect {container}` printed nothing")
    try:
        devices = json.loads(raw)
    except json.JSONDecodeError:
        return GPUVerdict(GPU_UNKNOWN, f"`{runtime} inspect {container}` printed unparseable JSON")
    if not isinstance(devices, list):
        return GPUVerdict(GPU_UNKNOWN, f"`{runtime} inspect {container}` printed no device list")
    hit = _matching_device(_device_paths(devices), raw, vendor)
    if hit is not None:
        return GPUVerdict(GPU_OK, f"{container} holds the {vendor} device {hit}", evidence=hit)
    named = ", ".join(_device_paths(devices)) or "(none)"
    return GPUVerdict(
        GPU_MISSING,
        f"{container} exposes no {vendor} GPU device — inspect lists: {named}",
        evidence=raw[:400],
    )


def decide(gpu_expected: bool, verdict: GPUVerdict) -> GPUVerdict:
    """The pure truth table. Not expected → ``skip`` (no check was owed);
    otherwise the probe's verdict stands — ``unknown`` stays ``unknown``
    and the CALLER must do nothing on it."""
    if not gpu_expected:
        return GPUVerdict(GPU_SKIP, "no GPU was expected in the container")
    return verdict


#: Why the GPU was expected — one line per caller shape, rendered into the
#: ledger row's ``detected`` text.
_REASON_TEXT = {
    REASON_OVERLAY: "The GPU compose overlay was applied, but the running container "
                    "holds no GPU device.",
    REASON_DEGRADED: "The host reports an NVIDIA GPU, but the stack composed "
                     "CPU-only (the CDI spec was not ready within the wait).",
    REASON_NO_CDI_TOOL: "This podman host has no compose tool that keeps the CDI "
                        "`devices:` spec (only a docker-compose delegate) — the "
                        "overlay's GPU device cannot reach the container.",
}


def _overlay_name(runtime: str, gpu_vendor: Optional[str]) -> str:
    """The overlay name the recipe recreates with (display only — the
    SELECTION rule stays in :mod:`vco_lib.compose_provider`)."""
    stem = "podman-compose" if runtime == "podman" else "docker-compose"
    return f"{stem}.rocm.yml" if _vendor(gpu_vendor) == "amd" else f"{stem}.gpu.yml"


def _recipe(runtime: str, container: str, gpu_vendor: Optional[str],
            infra_dir: Optional[Path], rerun_cmd: str) -> str:
    vendor = _vendor(gpu_vendor)
    overlay = _overlay_name(runtime, gpu_vendor)
    infra = str(infra_dir) if infra_dir else "<infrastructure dir>"
    if vendor == "amd":
        prep = ("# a compose tool that keeps the devices: spec (podman hosts), then\n"
                "# make sure the ROCm compute nodes exist on the host (amdgpu + ROCm):\n"
                "ls /dev/kfd /dev/dri/*\n")
        tool = "podman-compose" if runtime == "podman" else "docker compose"
    elif runtime == "docker":
        prep = ("# Docker needs the NVIDIA Container Toolkit integrated with the daemon:\n"
                "sudo nvidia-ctk runtime configure --runtime=docker\n"
                "sudo systemctl restart docker\n")
        tool = "docker compose"
    else:
        prep = ("# 1. a compose tool that keeps the CDI `devices:` spec (if missing):\n"
                "pip install --user podman-compose\n"
                "# 2. regenerate the CDI spec and keep it fresh across driver updates:\n"
                "sudo nvidia-ctk cdi generate --output=/var/run/cdi/nvidia.yaml\n"
                "systemctl enable --now nvidia-cdi-refresh.path 2>/dev/null || true\n")
        tool = "podman-compose"
    return (
        f"{prep}"
        f"# 3. recreate the stack with the GPU overlay (run inside {infra}):\n"
        f"cd {infra}\n"
        f"{tool} -f docker-compose.yml -f {overlay} --profile gpu up -d --force-recreate ollama\n"
        f"# 4. verify the device reached the container, then re-run:\n"
        f"{runtime} inspect --format '{{{{json .HostConfig.Devices}}}}' {container}   "
        f"# must list a {vendor} device\n"
        f"{rerun_cmd}"
    )


def deferral_entry(
    verdict: GPUVerdict,
    *,
    reason: str = REASON_OVERLAY,
    runtime: str = "podman",
    container: str = OLLAMA_CONTAINER,
    gpu_vendor: Optional[str] = None,
    infra_dir: Optional[Path] = None,
    rerun_cmd: str = "python install.py --update",
) -> Optional[DeferralEntry]:
    """The ``compose_gpu_device_missing`` row for a ``missing`` verdict —
    ``None`` for every other kind (``unknown`` does NOTHING)."""
    if verdict.kind != GPU_MISSING:
        return None
    why = _REASON_TEXT.get(reason, _REASON_TEXT[REASON_OVERLAY])
    return DeferralEntry(
        condition_id=CID_COMPOSE_GPU_DEVICE_MISSING,
        title="Container runs CPU-only — the GPU never reached it",
        detected=(f"{why} Evidence from the running container: {verdict.detail}"),
        why_deferred=("Ollama serves every embedding on the CPU in this state (the "
                      "2026-09-29 incident ran silently CPU-only for days). The "
                      "container is left exactly as it is; the recipe recreates it "
                      "with a compose tool that keeps the GPU device spec."),
        command_to_apply=_recipe(runtime, container, gpu_vendor, infra_dir, rerun_cmd),
        severity="warning",
        kg_node_refs=[],
    )


def _human(verdict: GPUVerdict, ledger: Optional[Path]) -> str:
    if verdict.kind == GPU_MISSING:
        where = (f"deferral {CID_COMPOSE_GPU_DEVICE_MISSING} written to "
                 f"{ledger / '.claude' / 'context' / 'UPDATE_DEFERRED.md'}"
                 if ledger else "NOT ledgered (no --install-root given)")
        return f"gpu-verify: MISSING — {verdict.detail}; {where}"
    return f"gpu-verify: {verdict.kind} — {verdict.detail}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m vco_lib.gpu_verify`` — the wrappers' entry point.

    The caller decides the GPU was expected (it invokes this only then);
    a ``missing`` verdict writes the ledger row under ``--install-root``
    via :func:`vco_lib.deferral_emit.emit` (the ONE emitter home). Always
    exits 0: a verification must never fail the boot that succeeded."""
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.gpu_verify",
        description="Post-start check: did the GPU reach the running container?")
    parser.add_argument("--runtime", required=True, help="podman | docker")
    parser.add_argument("--container", default=OLLAMA_CONTAINER)
    parser.add_argument("--vendor", default="nvidia", choices=["nvidia", "amd"])
    parser.add_argument("--reason", default=REASON_OVERLAY,
                        choices=[REASON_OVERLAY, REASON_DEGRADED, REASON_NO_CDI_TOOL],
                        help="why the GPU was expected")
    parser.add_argument("--infra-dir", default=None, help="the compose dir (for the recipe)")
    parser.add_argument("--install-root", default=None,
                        help="the orchestrator root whose UPDATE_DEFERRED.md the row lands in")
    parser.add_argument("--json", action="store_true", help="machine-readable verdict on stdout")
    args = parser.parse_args(argv)

    verdict = decide(True, probe(args.runtime, args.container, args.vendor))
    root = Path(args.install_root) if args.install_root else None
    entry = deferral_entry(verdict, reason=args.reason, runtime=args.runtime,
                           container=args.container, gpu_vendor=args.vendor,
                           infra_dir=Path(args.infra_dir) if args.infra_dir else None)
    if entry is not None and root is not None:
        from vco_lib.deferral_emit import emit  # noqa: PLC0415 — the emitter home

        emit(root, entry, log=lambda msg: print(msg, file=sys.stderr, flush=True))
    if args.json:
        print(json.dumps({"kind": verdict.kind, "detail": verdict.detail,
                          "container": args.container, "condition": CID_COMPOSE_GPU_DEVICE_MISSING}))
    else:
        print(_human(verdict, root))
    return 0


if __name__ == "__main__":  # pragma: no cover — the wrappers shell in here
    raise SystemExit(main())
