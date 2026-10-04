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
from vco_lib import gpu_verify as _gv
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
    #: the podman start/heal: the REAL :class:`HealResult` (W1R-03) — the ledger
    #: row then names only the commands that actually ran
    heal_podman: Callable[[], "_cp.HealResult"]
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


def _rerun_cmd(plan: Step5Plan) -> str:
    """The re-run a ledger row prints: ``--update`` only for an update run — a
    fresh install that never completed is re-run as an install (W1R-03)."""
    return "python install.py --update" if getattr(plan.args, "update", False) else "python install.py"


def _info_stderr(rt: str, hooks: Step5Hooks) -> str:
    """The last stderr line of ``<rt> info`` — the ACTUAL reason a runtime does
    not answer (storage mismatch, permissions), shown instead of "(ok)" (W1R-09)."""
    import subprocess

    try:
        res = (hooks.run or subprocess.run)([rt, "info"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`{rt} info` could not run ({exc})"
    lines = (getattr(res, "stderr", "") or "").strip().splitlines()
    return lines[-1] if lines else f"`{rt} info` does not answer"


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
            why = sock.detail or (_info_stderr(rt, hooks) if sock.kind == _cp.SOCKET_OK else sock.kind)
            print(f"  [!] podman is installed but it is not reachable ({why}). Attempting auto-start...")
        res = hooks.heal_podman()
        if res.healed:
            print("      [OK] Podman is reachable again.")
            return
        print(f"      [!] Auto-start failed: {res.reason}")
        entry = _cr.heal_deferral_entry(res, manual_cmd=_rerun_cmd(plan)) if res.deferral_cid else None
        if entry is not None:
            if plan.deferral_report is not None:
                plan.deferral_report.add_entry(entry)
        else:
            hooks.emit_podman_start_failed(plan.deferral_report, detail=res.reason)
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


def _warn_no_cdi_compose_tool(plan: Step5Plan, provider: Optional[_cp.ComposeProvider],
                              gpu_args: list[str]) -> None:
    """Podman + GPU overlay + a docker-compose delegate: the overlay's CDI
    ``devices:`` spec cannot reach the container (v0.2.101 plan item 1) — say
    so with a ledger row instead of starting silently on CPU. The post-start
    check below re-states it with container evidence once compose answered."""
    if not gpu_args or plan.runtime != "podman":
        return
    if provider is None or provider.engine != _cp.ENGINE_DOCKER_COMPOSE:
        return
    print(f"  WARNING: the only compose tool here is {provider.engine} ({provider.evidence}); "
          "it drops the GPU overlay's CDI device spec — the stack will start CPU-only.")
    entry = _gv.deferral_entry(
        _gv.GPUVerdict(
            _gv.GPU_MISSING,
            f"no CDI-capable compose tool: this podman host has only {provider.engine} "
            f"({provider.evidence})"),
        reason=_gv.REASON_NO_CDI_TOOL, runtime=plan.runtime, gpu_vendor=plan.gpu_vendor,
        infra_dir=plan.infra_dir, rerun_cmd=_rerun_cmd(plan))
    if entry is not None and plan.deferral_report is not None:
        plan.deferral_report.add_entry(entry)


def _gpu_post_start_check(plan: Step5Plan, hooks: Step5Hooks, gpu_args: list[str]) -> None:
    """v0.2.101 (plan items 1 + 5): the GPU overlay was in the compose argv —
    did the GPU reach the RUNNING container? Positive evidence only
    (:mod:`vco_lib.gpu_verify`); ``unknown`` (probe could not tell) does
    NOTHING — no row on uncertainty."""
    if not gpu_args:
        return
    verdict = _gv.decide(True, _gv.probe(
        plan.runtime, gpu_vendor=plan.gpu_vendor, run=hooks.run))
    if verdict.kind != _gv.GPU_MISSING:
        return
    print(f"  WARNING: {verdict.detail}")
    entry = _gv.deferral_entry(verdict, runtime=plan.runtime, gpu_vendor=plan.gpu_vendor,
                               infra_dir=plan.infra_dir, rerun_cmd=_rerun_cmd(plan))
    if entry is not None and plan.deferral_report is not None:
        plan.deferral_report.add_entry(entry)
        print("      A deferral entry has been written to UPDATE_DEFERRED.md with the "
              "recovery recipe (recreate with a CDI-capable compose tool).")


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
    gpu_args = _gpu_overlay_args(plan, hooks, provider)
    cmd.extend(gpu_args)
    _warn_no_cdi_compose_tool(plan, provider, gpu_args)
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
        if plan.deferral_report is not None:
            plan.deferral_report.add_entry(_hard_stop_entry(
                124, f"compose up timed out after {timeout // 60} min (daemon hung?)",
                f"cd {plan.infra_dir} && {manual}", _rerun_cmd(plan)))
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
        _gpu_post_start_check(plan, hooks, gpu_args)
        return OK

    # W1R-10: "Try starting manually" names the LAST argv (after a rejected
    # `--build` was dropped); the rebuild hint keeps the original on purpose.
    manual_last = shlex.join(result.argv) if getattr(result, "argv", None) else manual
    _print_failure(result, plan, manual_last)
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
        manual_cmd=f"cd {plan.infra_dir} && {manual_last}",
        log_event=hooks.log_event, install_root=plan.install_root,
        persist_on_hard_stop=plan.guard_rows,
    ):
        return CONTINUE
    # The hard stop (install.py exits 1): the failure row goes into the run
    # report, which the exit-path flush writes (AD-9, L1-F09).
    if plan.deferral_report is not None:
        plan.deferral_report.add_entry(_hard_stop_entry(
            result.returncode or 1, result.first_stderr or result.stderr,
            f"cd {plan.infra_dir} && {manual_last}", _rerun_cmd(plan)))
    return FAIL


def _hard_stop_entry(exit_code: int, stderr: str, manual_cmd: str, rerun: str) -> DeferralEntry:
    """``services_compose_up_failed`` for the STOPPED run (the continued-update
    wording lives in ``install_services_guard.emit_compose_up_failed_deferral``)."""
    tail = "\n".join(f"  {ln}" for ln in (stderr or "").strip().splitlines()[-8:])
    return DeferralEntry(
        condition_id=_svc_guard.CID_COMPOSE_UP_FAILED,
        title="`compose up` failed — the install/update stopped at step 5",
        detected=f"`compose up` exited {exit_code} and a required service was not answering, "
                 f"so the run stopped. Last lines of its stderr:\n{tail}",
        why_deferred="Steps after 5 need the services; the run stopped instead of continuing "
                     "half-applied. Nothing was removed.",
        command_to_apply=("# Re-run compose by hand to see the full error, fix it, then re-run:\n"
                          f"{manual_cmd}\n{rerun}"),
        severity="critical",
        kg_node_refs=[],
    )
