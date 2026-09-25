# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Reconcile VCO's own runtime record with the machine (v0.2.97, R8 G1/G5/G6).

Two pins, two contracts
-----------------------
``VCT_CONTAINER_RUNTIME`` is the USER's pin. It stays strict everywhere: a
pinned runtime that is down is refused, and the refusal names the pin.

``state/install/runtime.txt`` is VCO's OWN record of the runtime the install put
the data on. Normal operation (session hooks, services, storage, supervisor)
honours it as a pin — :func:`vco_lib.containers.runtime_pin`. But a record can go
stale (the user uninstalled Docker Desktop and installed podman; the install
recorded docker on a day podman was down), and before this module an update on
such a machine exited 1 behind a prompt that named the WRONG runtime, with no
ledger entry: a manual repair was the only way out (R8 G1).

This module is the ONE place that decides what a record means for the machine
as it is now. :func:`reconcile` runs early in ``install.py`` (install and
``--update``), before anything uses the pin, and read-only in the boot wrapper
(``scripts/launch-claude-mcp-stack.{sh,ps1}``, which must never rewrite state):

(a) the recorded runtime is NOT INSTALLED and the other runtime answers, holding
    VCO's containers/volumes (or none exist anywhere) → the record is rewritten to
    the other runtime and an ``informational_record`` says what changed and why;
(b) the recorded runtime is installed but its daemon does not answer → the
    documented start is tried (install.py's ``_try_start_*_daemon``); if it still
    does not answer, an ``action_required`` entry names exactly what to start, the
    update continues without containers and finishes with a clear summary;
(c) both answer and VCO's data is under the recorded one → the record is kept;
    data under BOTH → ``action_required`` (only the user knows which is current).
    Data ONLY under the other runtime, while the recorded one answers and holds
    none of VCO's containers or volumes, is the stale-record shape again: the
    record is rewritten (informational) — keeping it would bring the stack up on
    empty volumes.

A runtime the user chose with ``install.py --container`` is CONFIRMED
(``state/install/runtime.confirmed``): (c)'s heuristics never override it.

The runtime is never switched silently while VCO's data lives under the recorded
one — every switch is a positive-evidence decision with a ledger record.

Stdlib + stdlib-only ``vco_lib`` modules: this runs from install.py's pre-venv
phase (``_detect_system``).
"""
from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib import containers as _c
from vco_lib.deferral_report import DeferralEntry

__all__ = [
    "CID_RECORD_RECONCILED",
    "CID_UNUSABLE",
    "CID_DATA_UNDER_BOTH",
    "VCO_VOLUME_NAMES",
    "Outcome",
    "Reconciliation",
    "reconcile",
    "vco_data_under",
    "apply_at_install",
    "record_explicit_choice",
    "read_confirmed",
    "record_boot_refusal",
    "unusable_still_applies",
    "data_still_under_both",
    "runtime_down_message",
    "containers_skipped_note",
    "main",
]

CID_RECORD_RECONCILED = "container_runtime_record_reconciled"
CID_UNUSABLE = "container_runtime_unusable"
CID_DATA_UNDER_BOTH = "container_runtime_data_under_both"

#: VCO's named volumes — MUST MATCH the ``name: ${VCT_*_VOLUME_NAME:-<default>}``
#: defaults in ``infrastructure/docker-compose.yml`` (pinned by
#: ``tests/test_v0297_runtime_reconcile.py``).
VCO_VOLUME_NAMES: tuple[str, ...] = ("vco_weaviate_data", "vco_ollama_data", "vco_code_embed_cache")

#: The runtime the user chose explicitly (``install.py --container``).
CONFIRMED_REL = Path("state") / "install" / "runtime.confirmed"

_LIST_TIMEOUT_S = 15

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]
WhichFn = Callable[[str], Optional[str]]
StartFn = Callable[[str], "tuple[bool, str]"]


class Outcome(str, Enum):
    NO_RECORD = "no_record"            # nothing pinned: auto-detection decides
    KEPT = "kept"                      # the pin stands and its runtime answers
    REWRITTEN = "rewritten"            # (a) / stale record → the other runtime
    UNUSABLE = "unusable"              # (b) / a refused pin: no containers this run
    DATA_UNDER_BOTH = "data_under_both"  # (c) ambiguous — record kept


@dataclass(frozen=True)
class Reconciliation:
    outcome: Outcome
    #: The runtime to drive, ``None`` when none may be driven (UNUSABLE) or
    #: nothing was decided (NO_RECORD).
    runtime: Optional[str]
    pinned: Optional[str]
    via: Optional[str]
    detail: str
    entries: tuple[DeferralEntry, ...] = ()
    started: str = ""


def _other(runtime: str) -> str:
    return "docker" if runtime == "podman" else "podman"


def read_confirmed(install_root: Path) -> Optional[str]:
    """The runtime the user chose with ``--container``, or ``None``."""
    try:
        token = (Path(install_root) / CONFIRMED_REL).read_text(encoding="utf-8").strip().lower()
    except (OSError, ValueError):
        return None
    return token if token in _c.RUNTIME_CANDIDATES else None


def record_explicit_choice(install_root: Path, runtime: str) -> Optional[str]:
    """``install.py --container X``: record X AND mark it confirmed, through the
    one runtime.txt writer. Raises ``OSError``."""
    token = _c.write_runtime_txt(install_root, runtime)
    if token is not None:
        (Path(install_root) / CONFIRMED_REL).write_text(token + "\n", encoding="utf-8")
    return token


def _list_names(argv: Sequence[str], run: RunFn) -> Optional[set[str]]:
    try:
        res = run(list(argv), capture_output=True, text=True, timeout=_LIST_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return {ln.strip().lstrip("/") for ln in (res.stdout or "").splitlines() if ln.strip()}


def vco_data_under(runtime: str, *, run: Optional[RunFn] = None) -> Optional[bool]:
    """Does ``runtime`` hold VCO's containers or named volumes?

    ``True``/``False`` only when BOTH listings answered; ``None`` when either
    could not (daemon down, CLI error) — "could not look" is never "empty".
    Only VCO-prefixed names count (``vco_*`` / ``vct_*`` containers and
    :data:`VCO_VOLUME_NAMES`): an unprefixed ``ollama`` may be the user's own.
    Read-only: ``ps -a`` and ``volume ls``.
    """
    _run = run or subprocess.run
    containers = _list_names([runtime, "ps", "-a", "--format", "{{.Names}}"], _run)
    if containers is None:
        return None
    volumes = _list_names([runtime, "volume", "ls", "--format", "{{.Name}}"], _run)
    if volumes is None:
        return None
    ours = {
        n for s in _c.CANONICAL_CONTAINERS for n in _c.all_known_names(s)
        if n.startswith(("vco_", "vct_"))
    }
    return bool(containers & ours) or bool(volumes & set(VCO_VOLUME_NAMES))


def _status(runtime: str, which: WhichFn, run: RunFn) -> str:
    """``usable`` / ``missing`` / ``down`` (installed, the daemon did not answer
    or the probe could not run) — :func:`vco_lib.containers._evaluate_candidate`,
    the resolver's own ladder, without compose (compose has its own step)."""
    p = _c._evaluate_candidate(  # noqa: SLF001 — the one per-candidate ladder
        runtime, which=which, run=run, probe_daemon=True, probe_compose=False, home=None,
    )
    if p.status == "usable":
        return "usable"
    return "missing" if p.status == "missing" else "down"


def _start_if_down(runtime: str, status: str, start_daemon: Optional[StartFn],
                   which: WhichFn, run: RunFn) -> tuple[str, str]:
    if status != "down" or start_daemon is None:
        return status, ""
    try:
        ok, why = start_daemon(runtime)
    except Exception as exc:  # noqa: BLE001 — a start helper defect is "did not start"
        ok, why = False, f"start helper failed: {exc}"
    note = f"tried to start {runtime}: {'started' if ok else why}"
    return _status(runtime, which, run), note


def reconcile(
    install_root: Optional[Path],
    *,
    env: Optional[Mapping[str, str]] = None,
    which: Optional[WhichFn] = None,
    run: Optional[RunFn] = None,
    start_daemon: Optional[StartFn] = None,
    rewrite: bool = True,
) -> Reconciliation:
    """Decide what the pin means for this machine now (see the module doc).

    ``rewrite=False`` is the boot wrapper's read-only form: the same decision,
    no runtime.txt write, no daemon start, no ledger entries.
    """
    import shutil  # noqa: PLC0415 — stdlib, only for the default

    _which = which or shutil.which
    _run = run or subprocess.run
    root = Path(install_root) if install_root is not None else None
    pin = _c.runtime_pin(env, install_root=root, warn=lambda _m: None)
    if pin is None:
        return Reconciliation(Outcome.NO_RECORD, None, None, None, "no runtime pin")
    pinned, via, _file = pin
    other = _other(pinned)
    starter = start_daemon if rewrite else None
    status, started = _start_if_down(pinned, _status(pinned, _which, _run), starter, _which, _run)

    if via == _c.PIN_VIA_ENV or root is None:
        if status == "usable":
            return Reconciliation(Outcome.KEPT, pinned, pinned, via, f"{pinned} answers", started=started)
        return _unusable(root, pinned, via, status, None, started, rewrite)

    if status == "usable":
        return _reconcile_usable(root, pinned, other, _which, _run, rewrite, started)
    if status == "down":
        # (b) Its data may well be there; it cannot be looked at, so the record
        # stands and nothing is switched.
        return _unusable(root, pinned, via, status, None, started, rewrite)

    # (a) The recorded runtime is not installed at all.
    ostatus, ostarted = _start_if_down(other, _status(other, _which, _run), starter, _which, _run)
    started = "; ".join(x for x in (started, ostarted) if x)
    if ostatus != "usable":
        return _unusable(root, pinned, via, status, (other, ostatus), started, rewrite)
    data = vco_data_under(other, run=_run)
    if data is None:
        return _unusable(root, pinned, via, status, (other, "unlistable"), started, rewrite)
    why = (f"{pinned} is not installed; {other} answers and holds VCO's containers/volumes"
           if data else f"{pinned} is not installed; {other} answers and no VCO data exists under it")
    return _rewritten(root, pinned, other, why, rewrite, started)


def _reconcile_usable(root: Path, pinned: str, other: str, which: WhichFn, run: RunFn,
                      rewrite: bool, started: str) -> Reconciliation:
    kept = Reconciliation(Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                          f"{pinned} answers", started=started)
    if read_confirmed(root) == pinned or _status(other, which, run) != "usable":
        return kept
    here, there = vco_data_under(pinned, run=run), vco_data_under(other, run=run)
    if here is None or there is None or not there:
        return kept
    if here:
        entries = (_both_entry(root, pinned, other),) if rewrite else ()
        return Reconciliation(Outcome.DATA_UNDER_BOTH, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                              f"VCO containers/volumes exist under both {pinned} and {other}; "
                              f"keeping the recorded {pinned}", entries, started)
    return _rewritten(root, pinned, other,
                      f"{pinned} answers but holds none of VCO's containers or volumes, "
                      f"while {other} holds them", rewrite, started)


def _rewritten(root: Path, old: str, new: str, why: str, rewrite: bool,
               started: str) -> Reconciliation:
    entries: tuple[DeferralEntry, ...] = ()
    if rewrite:
        try:
            _c.write_runtime_txt(root, new)
        except OSError as exc:
            # The record stays stale, so every later step would refuse it:
            # this run is the unusable case, told truthfully.
            detail = (f"stale runtime record: {why}, but re-recording {new} in "
                      f"{_c.runtime_txt_path(root)} failed: {exc}")
            return Reconciliation(
                Outcome.UNUSABLE, None, old, _c.PIN_VIA_RUNTIME_TXT, detail,
                (_unusable_entry(root, old, _c.PIN_VIA_RUNTIME_TXT, "missing", detail),), started)
        entries = (_reconciled_entry(root, old, new, why),)
        try:
            from vco_lib.deferral_emit import record_auto_resolution  # noqa: PLC0415

            record_auto_resolution(root, CID_RECORD_RECONCILED,
                                   f"re-recorded the container runtime {old} -> {new}", why)
        except Exception:  # noqa: BLE001 — observability, never a gate
            pass
    verb = "re-recorded" if rewrite else "using (the next update re-records it)"
    return Reconciliation(Outcome.REWRITTEN, new, old, _c.PIN_VIA_RUNTIME_TXT,
                          f"stale runtime record: {why} — {verb} {new}", entries, started)


def _start_command(runtime: str) -> str:
    system = platform.system()
    if runtime == "podman":
        return ("systemctl --user start podman.socket" if system == "Linux"
                else "podman machine start")
    if system == "Linux":
        return "sudo systemctl start docker"
    return "start Docker Desktop and wait for it to settle"


def _unusable(root: Optional[Path], pinned: str, via: Optional[str], status: str,
              other: Optional[tuple[str, str]], started: str, rewrite: bool) -> Reconciliation:
    source = via if via == _c.PIN_VIA_ENV else str(_c.runtime_txt_path(root)) if root else str(via)
    if status == "missing":
        what = f"{pinned} is not installed"
    else:
        what = f"{pinned} is installed but does not answer `{pinned} info`"
    if other is not None:
        what += f", and {other[0]} is not usable either" if other[1] != "unlistable" else (
            f"; {other[0]} answers but its containers/volumes could not be listed")
    detail = f"the container runtime is pinned to {pinned} by {source}: {what}"
    if started:
        detail += f" ({started})"
    entries = (_unusable_entry(root, pinned, via or "", status, detail),) if rewrite else ()
    return Reconciliation(Outcome.UNUSABLE, None, pinned, via, detail, entries, started)


def _unusable_entry(root: Optional[Path], pinned: str, via: str, status: str,
                    detail: str) -> DeferralEntry:
    other = _other(pinned)
    if status != "missing":
        remedy = f"# Start {pinned}:\n{_start_command(pinned)}"
    elif via == _c.PIN_VIA_ENV:
        remedy = f"# Install {pinned}, or stop pinning it:\nunset VCT_CONTAINER_RUNTIME"
    else:
        remedy = (f"# Reinstall {pinned}, OR switch to {other} (re-records runtime.txt):\n"
                  f"python install.py --update --container {other}")
    if via == _c.PIN_VIA_ENV and status != "missing":
        remedy += f"\n# Or stop pinning it: unset VCT_CONTAINER_RUNTIME (or set it to {other})"
    return DeferralEntry(
        condition_id=CID_UNUSABLE,
        title=f"Container runtime {pinned} is not usable — containers were not started",
        detected=detail + ".",
        why_deferred=(
            f"VCO will not start the stack under {other} on its own: podman and docker keep "
            "SEPARATE named volumes, so the other runtime would bring up an EMPTY knowledge "
            "graph next to your data. Everything that does not need containers was completed. "
            f"This entry clears by itself once {pinned} answers; the next session start (or "
            "`python install.py --update`) then brings the stack up."
        ),
        command_to_apply=remedy,
        severity="warning",
        dismiss_fields={"runtime": pinned, "via": via,
                        "root": str(root) if root is not None else ""},
    )


def _reconciled_entry(root: Path, old: str, new: str, why: str) -> DeferralEntry:
    return DeferralEntry(
        condition_id=CID_RECORD_RECONCILED,
        title=f"Container runtime record updated: {old} -> {new}",
        detected=(f"{_c.runtime_txt_path(root)} recorded {old}, but {why}. "
                  f"VCO re-recorded {new}, where its containers and volumes live."),
        why_deferred="Nothing is pending — this records a change VCO made on its own.",
        command_to_apply=(f"# Nothing to do. If your data was under {old}, reinstall/start {old} and run:\n"
                          f"python install.py --update --container {old}"),
        severity="info",
    )


def _both_entry(root: Path, recorded: str, other: str) -> DeferralEntry:
    return DeferralEntry(
        condition_id=CID_DATA_UNDER_BOTH,
        title="VCO containers/volumes exist under both podman and docker",
        detected=(f"Both {recorded} (recorded in {_c.runtime_txt_path(root)}) and {other} hold "
                  f"VCO containers or volumes. VCO kept using {recorded}."),
        why_deferred="Only you know which copy is current; VCO will not merge them or pick for you.",
        command_to_apply=(f"# Keep {recorded} (confirms the record; silences this entry):\n"
                          f"python install.py --update --container {recorded}\n"
                          f"# Or switch to {other}:\n"
                          f"python install.py --update --container {other}"),
        severity="warning",
        dismiss_fields={"recorded": recorded, "root": str(root)},
    )


# ---------------------------------------------------------------------------
# install.py glue
# ---------------------------------------------------------------------------


def apply_at_install(
    install_root: Path,
    args: Any,
    report: Any,
    *,
    start_daemon: Optional[StartFn],
    log_event: Callable[..., None],
    out: Callable[[str], None] = print,
    **kw: Any,
) -> Optional[Reconciliation]:
    """install.py's one call (``_detect_system``, before the pin is used).

    ``--no-containers`` → nothing. ``--container X`` → X is recorded and
    CONFIRMED (the user's explicit choice). Otherwise :func:`reconcile`; its
    entries go into the run's report, and an UNUSABLE outcome makes the rest of
    the run skip containers (``args.no_containers``) with the reason in
    ``args.containers_deferred`` — never ``return 1`` behind a prompt.
    """
    if getattr(args, "no_containers", False):
        return None
    if getattr(args, "container", None):
        try:
            record_explicit_choice(install_root, args.container)
        except OSError as exc:
            log_event("runtime_reconcile", "warn", f"could not record --container: {exc}")
        return None
    try:
        res = reconcile(install_root, start_daemon=start_daemon, **kw)
    except Exception as exc:  # noqa: BLE001 — never blocks the install; the old flow runs
        log_event("runtime_reconcile", "warn", f"reconcile failed: {exc}")
        return None
    for entry in res.entries:
        if report is not None:
            report.add_entry(entry)
    if res.outcome is not Outcome.NO_RECORD:
        level = "ok" if res.outcome is Outcome.KEPT else "warn"
        log_event("runtime_reconcile", level, res.detail,
                  data={"outcome": res.outcome.value, "runtime": res.runtime, "pinned": res.pinned})
    if res.outcome is Outcome.REWRITTEN:
        out(f"  [i] {res.detail} (see UPDATE_DEFERRED.md)")
    elif res.outcome is Outcome.DATA_UNDER_BOTH:
        out(f"  [!] {res.detail} — see UPDATE_DEFERRED.md")
    elif res.outcome is Outcome.UNUSABLE:
        out(f"  [!] {res.detail}.")
        out("      Container setup is deferred (UPDATE_DEFERRED.md names what to start);"
            " everything else continues.")
        args.no_containers = True
        args.containers_deferred = res.detail
    return res


def containers_skipped_note(args: Any) -> str:
    """The end-of-run line for a run without containers — truthful about WHY."""
    deferred = getattr(args, "containers_deferred", "")
    if deferred:
        return ("  NOTE: container setup was deferred: " + deferred + ".\n"
                "  UPDATE_DEFERRED.md names what to start; the entry clears by itself once the\n"
                "  runtime answers, and the next session start brings the stack up.")
    return ("  NOTE: You skipped container setup. Start Weaviate and Ollama\n"
            "  manually before using the orchestrator.")


def runtime_down_message(os_name: str, installed: str, *,
                         env: Optional[Mapping[str, str]] = None,
                         which: Optional[WhichFn] = None,
                         install_root: object = None) -> str:
    """The prompt's "installed but not responding" text — or ``""`` when no
    runtime binary is installed. Under a pin it names the PINNED runtime, never
    the other one as "installed but its daemon isn't responding" (R8 G1)."""
    import shutil  # noqa: PLC0415

    _which = which or shutil.which
    pin = _c.runtime_pin(env, install_root=install_root if install_root is not None
                         else _c._DEFAULT_ROOT, warn=lambda _m: None)  # noqa: SLF001
    if pin is not None and not _which(pin[0]):
        pinned, via = pin[0], pin[1]
        return (f"\n[!] The container runtime is pinned to {pinned} by {via}, and {pinned} is not "
                f"installed.\n    VCO will not switch to {_other(pinned)} on its own (its volumes "
                f"do not hold the data recorded under {pinned}).\n"
                f"    Reinstall {pinned}, or run: python install.py --update --container {_other(pinned)}")
    if not installed:
        return ""
    lines = [f"\n[!] {installed} is installed but its daemon isn't responding."]
    if installed == "docker" and os_name in ("Windows", "Darwin"):
        lines.append("    Open Docker Desktop, wait for the whale icon to settle (~30 seconds), "
                     "then re-run install.")
    elif installed == "podman":
        lines.append("    Start the Podman machine (Linux: systemctl --user start podman.socket;\n"
                     "    macOS / Windows: podman machine start), then re-run install.")
    else:
        lines.append("    Start the Docker daemon (`sudo systemctl start docker` on Linux), "
                     "then re-run install.")
    lines.append("    Or re-run with --no-containers to skip container setup.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clear probes (registry: probe:py:<name>) — READ-ONLY
# ---------------------------------------------------------------------------


def _entry_root(entry: Any) -> Optional[Path]:
    fields = getattr(entry, "dismiss_fields", None) or {}
    raw = str(fields.get("root") or "").strip()
    if raw:
        return Path(raw)
    from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

    return resolve_install_root()


def unusable_still_applies(entry: Any, *, env: Optional[Mapping[str, str]] = None,
                           which: Optional[WhichFn] = None,
                           run: Optional[RunFn] = None) -> Optional[bool]:
    """``container_runtime_unusable``: True while the pin (as THIS process sees
    it) still resolves to no runtime with compose; False once it answers; None
    when a probe could not run."""
    res = _c.resolve(env=env, which=which, run=run, warn=lambda _m: None,
                     install_root=_entry_root(entry))
    if res.state is _c.RuntimeState.UNKNOWN:
        return None
    return not (res.state is _c.RuntimeState.RESOLVED and res.compose is not None)


def data_still_under_both(entry: Any, *, which: Optional[WhichFn] = None,
                          run: Optional[RunFn] = None) -> Optional[bool]:
    """``container_runtime_data_under_both``: False once the user confirmed the
    record (``--container``) or VCO's data is no longer under both runtimes;
    None when either runtime cannot be looked at."""
    import shutil  # noqa: PLC0415

    root = _entry_root(entry)
    recorded = _c.read_runtime_txt(root)
    if root is None or recorded is None:
        return None
    if read_confirmed(root) == recorded:
        return False
    _which, _run = which or shutil.which, run or subprocess.run
    if any(_status(rt, _which, _run) != "usable" for rt in _c.RUNTIME_CANDIDATES):
        return None
    here, there = vco_data_under(recorded, run=_run), vco_data_under(_other(recorded), run=_run)
    if here is None or there is None:
        return None
    return bool(here and there)


# ---------------------------------------------------------------------------
# Boot wrapper (G5 read-only reconcile, G6 exit-3 visibility)
# ---------------------------------------------------------------------------


def record_boot_refusal(install_root: Path, reason: str, *,
                        env: Optional[Mapping[str, str]] = None) -> bool:
    """The boot wrapper exited 3 (pinned runtime refused, or none found): put it
    in ``install_root``'s ledger through the one emitter, so session start and
    the launcher show it. Clears through :func:`unusable_still_applies`.

    Only an INSTALLED clone has a ledger to write (``state/install/`` is what
    install.py creates): a development checkout running the wrapper — the test
    suite does — records nothing (returns ``False``)."""
    from vco_lib.deferral_emit import emit  # noqa: PLC0415

    if not (Path(install_root) / "state" / "install").is_dir():
        return False
    pin = _c.runtime_pin(env, install_root=install_root, warn=lambda _m: None)
    pinned, via = (pin[0], pin[1]) if pin is not None else ("", "")
    entry = DeferralEntry(
        condition_id=CID_UNUSABLE,
        title=("Containers did not start at boot: "
               + (f"{pinned} is not usable" if pinned else "no usable container runtime")),
        detected=f"The boot service (launch-claude-mcp-stack) started nothing: {reason.strip()}",
        why_deferred=(
            "The boot wrapper never starts the stack under the runtime the data is NOT on "
            "(podman and docker keep separate volumes). This entry clears by itself once the "
            "runtime answers; the next session start then brings the stack up."
        ),
        command_to_apply=(f"# Start {pinned}:\n{_start_command(pinned)}" if pinned else
                          "# Install or start podman (or docker), then start a session or run:\n"
                          "python install.py --update"),
        severity="warning",
        dismiss_fields={"runtime": pinned, "via": via, "root": str(install_root)},
    )
    return emit(Path(install_root), entry)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vco_lib.runtime_reconcile")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boot", help="Read-only reconcile of the install's runtime record.")
    b.add_argument("--root", required=True)
    b.add_argument("--json", action="store_true")
    r = sub.add_parser("record-boot-refusal", help="Ledger entry for the wrapper's exit 3.")
    r.add_argument("--root", required=True)
    r.add_argument("--reason", default="")
    a = p.parse_args(argv)
    if a.cmd == "boot":
        res = reconcile(Path(a.root), rewrite=False)
        if a.json:
            print(json.dumps({"outcome": res.outcome.value, "runtime": res.runtime or "",
                              "pinned": res.pinned or "", "detail": res.detail}))
        else:
            print(f"VCO_RECONCILE_OUTCOME={shlex.quote(res.outcome.value)}")
            print(f"VCO_RECONCILE_RUNTIME={shlex.quote(res.runtime or '')}")
            print(f"VCO_RECONCILE_DETAIL={shlex.quote(res.detail)}")
        return 0
    try:
        record_boot_refusal(Path(a.root), a.reason)
    except Exception as exc:  # noqa: BLE001 — best effort: never blocks boot
        print(f"could not record the boot refusal: {exc}", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    return _cli(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
