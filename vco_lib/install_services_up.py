# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py step 5 — the compose-up tail (runtime pre-flight, provider,
GPU overlay, ``compose up`` with recovery, failure surfacing).

Extracted from ``install.py::_start_services`` in v0.2.100 (WP-04; the
monolith ratchet forbids growth and this is where the new recovery lands).
``_start_services`` still decides WHICH services to start/recreate/build and
applies the ownership guard; everything from "is the runtime reachable?" to
"compose up succeeded / failed" lives here.

install.py's own helpers (the Docker Desktop starter, the CDI generator, the
install-event log, the infrastructure ``.env`` writer, …) are passed in as
:class:`Step5Hooks`, looked up by install.py at call time, so a test that
patches ``install.<helper>`` still reaches this code.

What changed against the inline original (review L1-F02/F03/F07/F10):

* the podman API socket is checked, not only ``podman info``
  (:func:`vco_lib.compose_provider.runtime_reachable`) and healed
  non-destructively;
* the compose PROVIDER is detected and picks the GPU overlay
  (:func:`vco_lib.compose_provider.overlay_for_provider`), not the runtime's
  name;
* ``compose up`` runs through :func:`vco_lib.compose_recovery.
  compose_up_with_recovery`: the FIRST stderr is kept, ``--build`` is dropped
  only on positive "unsupported" evidence, a real build failure is surfaced
  with its reason, and non-destructive drift (socket, stale network label,
  storage-only leftover) is healed;
* the printed manual command is the exact argv — full ``-f`` chain and ``-p``.
"""
from __future__ import annotations

import os
import platform
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from vco_lib import code_embed_image as _cei
from vco_lib import compose_provider as _cp
from vco_lib import compose_recovery as _cr
from vco_lib import containers as _containers
from vco_lib import install_services_guard as _svc_guard
from vco_lib.deferral_report import DeferralEntry

__all__ = ["Step5Plan", "Step5Hooks", "compose_up_step", "OK", "NOTHING", "CONTINUE", "FAIL",
           "compose_timeout_s"]

OK = "ok"
NOTHING = "nothing"
CONTINUE = "continue"
FAIL = "fail"

LogEvent = Callable[..., None]


@dataclass
class Step5Plan:
    """What ``_start_services`` decided; this module only executes it."""

    runtime: str
    has_gpu: bool
    gpu_vendor: Optional[str]
    infra_dir: Path
    compose_file: Path
    embed_config: dict
    services_to_start: list
    services_to_recreate: list
    recreate_for_rebuild: list
    build_services: list
    args: Any = None
    detected: Optional[dict] = None
    deferral_report: Any = None
    guard_rows: Sequence[DeferralEntry] = ()
    install_root: Optional[Path] = None


@dataclass
class Step5Hooks:
    """install.py's helpers (see the module doc)."""

    log_event: LogEvent
    reachable: Callable[[str], bool]
    try_start_podman: Callable[[], "tuple[bool, str]"]
    try_start_docker: Callable[[], "tuple[bool, str]"]
    emit_podman_start_failed: Callable[..., None]
    get_compose_command: Callable[[str], list]
    write_infra_env: Callable[[dict], None]
    compose_subst_env: Callable[[dict], dict]
    gpu_tool_live: Callable[[str, list], bool]
    ensure_cdi: Callable[[], None]
    #: probes / heals; ``None`` = the default runner
    run: Optional[Callable[..., Any]] = None
    #: the compose command itself; ``None`` = ``subprocess.run`` at call time
    compose_run: Optional[Callable[..., Any]] = None
    system: Callable[[], str] = field(default=platform.system)
    #: the podman socket probe; ``None`` = :func:`vco_lib.compose_provider.socket_status`
    socket_status: Optional[Callable[[str], "_cp.SocketStatus"]] = None
    #: the compose-failure heal; ``None`` = :func:`vco_lib.compose_recovery.heal`
    heal: Optional[Callable[..., "_cp.HealResult"]] = None


def compose_timeout_s(env: Optional[dict] = None) -> int:
    """15 min default cap (first-run image pulls on slow links); override with
    ``VCT_INSTALL_DOCKER_TIMEOUT`` seconds, floor 60."""
    raw = ((os.environ if env is None else env).get("VCT_INSTALL_DOCKER_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(60, int(raw))
        except ValueError:
            pass
    return 900


# ---------------------------------------------------------------------------
# 1. runtime pre-flight
# ---------------------------------------------------------------------------


def _preflight(plan: Step5Plan, hooks: Step5Hooks) -> None:
    """Before compose-up: a runtime that does not answer (or a podman whose
    API socket is known broken) is started / healed with the per-OS recipe,
    and the user sees the REAL state, not a generic "daemon down"."""
    rt = plan.runtime
    if hooks.reachable(rt):
        return
    system = hooks.system()
    if rt == "podman":
        sock = (hooks.socket_status or (lambda r: _cp.socket_status(r, run=hooks.run,
                                                                   system=system)))(rt)
        if sock.kind == _cp.SOCKET_UNIT_ACTIVE_FILE_MISSING:
            print(f"  [!] podman answers, but its API socket file {sock.path} is missing while "
                  f"{sock.unit} is active. Restarting the socket unit (containers keep running)...")
        else:
            print("  [!] podman is installed but it is not reachable "
                  f"({sock.detail or sock.kind}). Attempting auto-start...")
        ok, detail = hooks.try_start_podman()
        if ok:
            print("      [OK] Podman is reachable again.")
            return
        print(f"      [!] Auto-start failed: {detail}")
        if sock.kind == _cp.SOCKET_UNIT_ACTIVE_FILE_MISSING:
            if plan.deferral_report is not None:
                plan.deferral_report.add_entry(_cr.heal_deferral_entry(
                    _cp.HealResult(False, [f"systemctl --user restart {sock.unit}"], detail,
                                   _cr.CID_SOCKET_HEAL_FAILED),
                    manual_cmd="python install.py --update"))
        else:
            hooks.emit_podman_start_failed(plan.deferral_report, detail=detail)
        print("      A deferral entry has been written to UPDATE_DEFERRED.md with the manual "
              "recovery recipe.")
        print("      compose-up below may fail; re-run install.py once podman is reachable.")
        return
    if rt == "docker" and system in ("Darwin", "Windows"):
        print("  [!] docker is installed but its daemon isn't responding to `docker info`. "
              "Attempting to start Docker Desktop...")
        ok, detail = hooks.try_start_docker()
        if ok:
            print("      [OK] Docker daemon is now responsive.")
        else:
            print(f"      [!] Auto-start failed: {detail}")
            print("      compose-up below may fail; start Docker Desktop manually and re-run "
                  "install.py once it settles.")
        return
    print(f"  [!] {rt} is installed but its daemon isn't responding to `{rt} info`. The "
          "compose-up below will fail.")
    print(f"      {_cr.daemon_remedy(rt)}")


# ---------------------------------------------------------------------------
# 2. GPU overlay by provider
# ---------------------------------------------------------------------------


def _ambiguity_check(plan: Step5Plan, hooks: Step5Hooks,
                     provider: Optional[_cp.ComposeProvider]) -> None:
    """Both NVIDIA and AMD overlays on disk AND both GPU tools live → the
    vendor cannot be picked safely; ask the user through the ledger."""
    if not plan.has_gpu or plan.deferral_report is None:
        return
    nvidia = _cp.overlay_for_provider(provider, "nvidia", plan.infra_dir)
    amd = _cp.overlay_for_provider(provider, "amd", plan.infra_dir)
    if not (nvidia and amd):
        return
    if hooks.gpu_tool_live("nvidia-smi", ["-L"]) and hooks.gpu_tool_live("rocm-smi", ["--showid"]):
        plan.deferral_report.add_entry(DeferralEntry(
            condition_id="compose_overlay_ambiguous",
            title="Compose GPU overlay ambiguous",
            detected=("Both nvidia-smi and rocm-smi report a live GPU, and both NVIDIA and AMD "
                      "ROCm compose overlay files exist. Cannot safely pick an overlay "
                      "automatically."),
            why_deferred=("Picking the wrong overlay causes Ollama to silently run CPU-only. "
                          "User must specify the GPU vendor."),
            command_to_apply=("# For NVIDIA:\nVCT_GPU_VENDOR=nvidia python install.py --gpu --update\n"
                              "# For AMD ROCm:\nVCT_GPU_VENDOR=amd python install.py --gpu --update"),
            severity="warning",
            kg_node_refs=[],
        ))


def _gpu_overlay_args(plan: Step5Plan, hooks: Step5Hooks,
                      provider: Optional[_cp.ComposeProvider]) -> list[str]:
    """``-f <overlay> --profile gpu`` for the provider that parses it, or ``[]``
    (CPU-only, with the reason printed)."""
    if not plan.has_gpu:
        return []
    vendor = "amd" if plan.gpu_vendor == "amd" else "nvidia"
    name = _cp.overlay_for_provider(provider, vendor, plan.infra_dir)
    engine = provider.engine if provider else "unknown compose"
    if name is None:
        tried = ", ".join(_cp.overlay_candidates(provider, vendor))
        print(f"  WARNING: GPU overlay not found for {engine} (tried: {tried}), running CPU-only")
        return []
    if vendor == "nvidia" and plan.runtime == "podman":
        # Podman needs the nvidia-ctk CDI spec on the host for either provider.
        hooks.ensure_cdi()
    label = "AMD ROCm" if vendor == "amd" else "NVIDIA"
    print(f"  GPU overlay: {label} ({engine}: {name})")
    return ["-f", str(plan.infra_dir / name), "--profile", "gpu"]


# ---------------------------------------------------------------------------
# 3. compose up
# ---------------------------------------------------------------------------


def _describe_recreate(plan: Step5Plan) -> None:
    if plan.services_to_recreate:
        config_changed = [s for s in plan.services_to_recreate if s not in plan.recreate_for_rebuild]
        parts = []
        if config_changed:
            parts.append(f"config changed: {', '.join(config_changed)}")
        if plan.recreate_for_rebuild:
            parts.append(f"image rebuilt: {', '.join(plan.recreate_for_rebuild)}")
        if plan.services_to_start:
            parts.append(f"starting: {', '.join(plan.services_to_start)}")
        print("  Recreating (" + "; ".join(parts) + ")")
    elif plan.services_to_start:
        print(f"  Starting only: {', '.join(plan.services_to_start)}")


def _print_failure(result: _cr.UpResult, plan: Step5Plan, manual: str) -> None:
    print("  FAIL")
    for line in (result.first_stderr or result.stderr or "").strip().splitlines()[-10:]:
        print(f"  {line}")
    if result.final_failure is not None and result.final_failure != result.failure:
        print(f"  After recovery, compose failed again: {result.final_failure.evidence}")
    for h in result.heals:
        if not h.healed:
            print(f"  Recovery not applied: {h.reason}")
    print("\n  Try starting manually:")
    print(f"    cd {plan.infra_dir}")
    print(f"    {manual}")
    shown = result.final_failure or result.failure
    if shown is not None:
        _svc_guard.print_compose_failure_hints(shown, plan.runtime)


def compose_up_step(plan: Step5Plan, hooks: Step5Hooks) -> str:
    """Run step 5's compose tail. Returns ``OK`` / ``NOTHING`` (no service to
    name) / ``CONTINUE`` (failed, but an ``--update`` with every required
    service answering goes on — ledgered) / ``FAIL`` (the caller exits 1)."""
    _preflight(plan, hooks)
    compose_cmd = list(hooks.get_compose_command(plan.runtime))
    provider = _cp.detect(plan.runtime, argv=compose_cmd, run=hooks.run)

    # Persist + export the compose-substitution keys (v0.2.54 gpu-audit C-4):
    # infrastructure/.env covers every later compose run; the env below THIS one.
    hooks.write_infra_env(plan.embed_config)
    compose_env = {**os.environ, **hooks.compose_subst_env(plan.embed_config)}

    project = _containers.compose_project_of(plan.compose_file)
    cmd = [*compose_cmd, "-f", str(plan.compose_file)]
    # An explicit -f chain disables compose's auto-load of compose.override.yaml
    # (v0.2.96 WP-4): append the present override files.
    cmd.extend(_svc_guard.override_f_chain(plan.infra_dir))
    _ambiguity_check(plan, hooks, provider)
    cmd.extend(_gpu_overlay_args(plan, hooks, provider))
    if project:
        cmd.extend(["-p", project])

    # The argv comes from the ONE builder (v0.2.97): `--no-deps` always,
    # `--build` only for a stale code_embed image, no bare `up -d` ever (I1).
    from vco_lib.service_lifecycle import compose_up_args  # noqa: PLC0415
    explicit = plan.services_to_start + [
        s for s in plan.services_to_recreate if s not in plan.services_to_start]
    up_args, _dropped = compose_up_args(
        explicit, build=bool(plan.build_services),
        force_recreate=bool(plan.services_to_recreate),
        gpu_mode="gpu" if plan.has_gpu else "unknown", prefix=cmd,
    )
    if not up_args:
        print("  Nothing for compose to start or recreate.")
        return NOTHING
    cmd.extend(up_args)
    # A printed command is shipped code: the exact argv, -f chain and -p included.
    manual = shlex.join(cmd)
    _describe_recreate(plan)

    timeout = compose_timeout_s()
    result = _cr.compose_up_with_recovery(
        cmd, cwd=str(plan.infra_dir), env=compose_env, timeout=timeout,
        runtime=plan.runtime, provider=provider, run=hooks.run,
        compose_run=hooks.compose_run, heal_fn=hooks.heal or _cr.heal,
    )
    for entry in _cr.deferral_entries(result, manual_cmd=f"cd {plan.infra_dir} && {manual}"):
        if plan.deferral_report is not None:
            plan.deferral_report.add_entry(entry)
    if result.timed_out:
        print(f"  FAIL (timed out after {timeout // 60} min)")
        print("  Container daemon may be hung. Try manually:")
        print(f"    cd {plan.infra_dir}")
        print(f"    {manual}")
        print("  Or bump the timeout: VCT_INSTALL_DOCKER_TIMEOUT=1800 python install.py ...")
        hooks.log_event("5/10", "error", f"compose up timed out after {timeout // 60} min",
                        data={"runtime": plan.runtime, "timeout_sec": timeout})
        return FAIL
    if result.ok:
        if result.build_dropped:
            for line in _cei.build_rejected_lines(result.failure, manual, plan.infra_dir):
                print(line)
            hooks.log_event("5/10", "warning",
                            "compose rejected --build; code_embed image NOT rebuilt",
                            data={"runtime": plan.runtime,
                                  "evidence": result.failure.evidence if result.failure else ""})
        for h in result.heals:
            hooks.log_event("5/10", "heal", h.reason, data={"actions": h.actions})
        print("  OK")
        hooks.log_event("5/10", "ok", "compose up completed")
        return OK

    _print_failure(result, plan, manual)
    first = result.failure
    hooks.log_event(
        "5/10", "error", f"compose up failed (exit {result.returncode})",
        data={"runtime": plan.runtime, "exit_code": result.returncode,
              "cause": first.cause if first else "",
              "provider": provider.engine if provider else "",
              "heals": [h.reason for h in result.heals],
              "stderr_tail": (result.first_stderr or result.stderr).strip()[-400:]},
    )
    # v0.2.93: --update with every required service answering → record + go on.
    if _svc_guard.compose_failure_followup(
        args=plan.args, detected=plan.detected, has_gpu=plan.has_gpu,
        deferral_report=plan.deferral_report, exit_code=result.returncode or 1,
        stderr=result.first_stderr or result.stderr,
        manual_cmd=f"cd {plan.infra_dir} && {manual}",
        log_event=hooks.log_event, install_root=plan.install_root,
        persist_on_hard_stop=plan.guard_rows,
    ):
        return CONTINUE
    return FAIL
