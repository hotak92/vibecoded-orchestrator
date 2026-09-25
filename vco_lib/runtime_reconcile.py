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

(a) the recorded runtime is NOT INSTALLED — not on this process's PATH nor in
    the usual install locations (:mod:`vco_lib.tool_search_dirs`, R9 H1(b): a
    short-PATH boot unit must never read a runtime in ``~/bin`` or
    ``/opt/homebrew/bin`` as absent) — and the other runtime answers, holding
    VCO's containers/volumes → the record is rewritten to the other runtime and
    an ``informational_record`` says what changed and why. "No VCO data exists
    anywhere" is rewritten ONLY by install.py (``rewrite=True``): a READ-ONLY
    caller (``rewrite=False`` — the resolver, the boot wrapper, the Rust
    mirrors) switches on POSITIVE evidence alone and otherwise refuses (R9 H1),
    because a runtime it merely failed to find may still hold the data;
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
(``state/install/runtime.confirmed``): NOTHING here overrides it — neither (a)
nor (c), neither read-only nor at install (R9 H2). When a confirmed runtime is
unusable, read-only callers refuse with the reason and install records an
``action_required`` entry; only the user switches, by running
``install.py --container <other>``, which rewrites BOTH files.

"VCO's data" is looked for under the ACTUAL volume names
(:func:`vco_volume_names`, R9 H6): the compose defaults plus every
``VCT_*_VOLUME_NAME`` override in ``infrastructure/.env`` (the launcher writes
them from the ``service_endpoints`` rows) and in the environment — an install
that adopted a pre-existing volume by name must not read as "no data". An
override name is a name the USER picked, so under the runtime VCO would switch
TO it is evidence only when corroborated there (R10 J8): a VCO container under
that runtime, or the volume's compose project label naming VCO's own project;
the ``vco_*`` defaults count on their own.

A bind-mounted data folder (``VCT_*_DATA_SOURCE=<dir>``, the launcher's
"relocate to a folder") is VCO's data on the HOST, under no runtime (R10 J2).
While one exists and is not empty, the runtime choice cannot be read off the
runtimes' volumes: the record is treated as holding the data — (c) keeps it,
(a) refuses read-only and records ``action_required`` at install — and a
leftover named volume under the other runtime never switches it.

The runtime is never switched silently while VCO's data lives under the recorded
one — every switch is a positive-evidence decision with a ledger record.

Stdlib + stdlib-only ``vco_lib`` modules: this runs from install.py's pre-venv
phase (``_detect_system``).
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from vco_lib import containers as _c
from vco_lib import tool_search_dirs as _tsd
from vco_lib.compose_env import DATA_KNOBS
from vco_lib.deferral_report import DeferralEntry
from vco_lib.envfile import parse_env_lines

__all__ = [
    "CID_RECORD_RECONCILED",
    "CID_UNUSABLE",
    "CID_DATA_UNDER_BOTH",
    "VCO_VOLUME_NAMES",
    "VOLUME_NAME_KEYS",
    "BOOT_LEDGER_LOCK_TIMEOUT_S",
    "Outcome",
    "Reconciliation",
    "reconcile",
    "vco_data_under",
    "data_evidence",
    "vco_compose_project",
    "vco_volume_names",
    "volume_names_from",
    "DATA_SOURCE_KEYS",
    "bind_sources_from",
    "bind_data_source",
    "unusable_detail",
    "not_switched_text",
    "MESSAGES_PATH",
    "apply_at_install",
    "record_explicit_choice",
    "read_confirmed",
    "record_boot_refusal",
    "record_refusal_bounded",
    "RECORD_REFUSAL_DEADLINE_S",
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

#: The compose knobs that rename those volumes (``name: ${KEY:-<default>}``) —
#: the VOLUME_NAME half of :data:`vco_lib.compose_env.DATA_KNOBS`, in its order.
#: MUST MATCH ``container_runtime.rs::VCO_VOLUME_NAME_KEYS``.
VOLUME_NAME_KEYS: tuple[str, ...] = tuple(pair[1] for pair in DATA_KNOBS.values())

#: The compose knobs that turn a service's data mount into a BIND mount of a
#: host folder (``${KEY:-<volume key>}:/data``) — the DATA_SOURCE half of
#: :data:`vco_lib.compose_env.DATA_KNOBS`, in its order. MUST MATCH
#: ``container_runtime.rs::VCO_DATA_SOURCE_KEYS``.
DATA_SOURCE_KEYS: tuple[str, ...] = tuple(pair[0] for pair in DATA_KNOBS.values())

#: The refusal wording shared with Rust (``container_runtime.rs``
#: ``include_str!``s the same file) — R10 J6.
MESSAGES_PATH = Path(__file__).with_name("runtime_reconcile_messages.toml")

#: The compose project dir whose ``.env`` compose reads (install.py's
#: ``infrastructure/``; ``compose_env.write_service_keys`` writes the knobs there).
INFRA_ENV_REL = Path("infrastructure") / ".env"
INFRA_COMPOSE_REL = Path("infrastructure") / "docker-compose.yml"

#: How long the boot wrapper's ledger write waits for the deferral lock (R9 H7):
#: an update holding it must never stall boot. Past it the entry is skipped
#: with a log line; the next boot or session writes it.
BOOT_LEDGER_LOCK_TIMEOUT_S = 10.0

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


def vco_volume_names(install_root: Optional[Path], *,
                     env: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """The named volumes that hold VCO's data on this install (R9 H6): the
    compose defaults (:data:`VCO_VOLUME_NAMES`), then every non-empty
    :data:`VOLUME_NAME_KEYS` value in ``<install_root>/infrastructure/.env``
    (whole file, file order — compose reads all of it) and then in ``env``
    (default :data:`os.environ`; compose's shell env wins over ``.env``).
    A union, never a replacement: a default-named volume left behind by an
    earlier layout is still VCO's. MUST MATCH
    ``container_runtime.rs::vco_volume_names`` —
    ``tests/fixtures/vco_volume_names_cases.json`` runs both."""
    return volume_names_from(_infra_env_text(install_root), os.environ if env is None else env)


def volume_names_from(env_file_text: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Pure half of :func:`vco_volume_names` (what the parity fixture drives).
    MUST MATCH ``container_runtime.rs::volume_names_from``."""
    names = list(VCO_VOLUME_NAMES)

    def _add(value: str) -> None:
        v = (value or "").strip()
        if v and v not in names:
            names.append(v)

    for key, value in parse_env_lines(env_file_text or ""):
        if key in VOLUME_NAME_KEYS:
            _add(value)
    for key in VOLUME_NAME_KEYS:
        _add(env.get(key) or "")
    return tuple(names)


def _infra_env_text(install_root: Optional[Path]) -> str:
    if install_root is None:
        return ""
    try:
        return (Path(install_root) / INFRA_ENV_REL).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _our_container_names() -> frozenset[str]:
    """VCO's own container names under a runtime: the ``vco_*`` / ``vct_*``
    subset of every canonical service's known names (an unprefixed ``ollama``
    may be the user's own). MUST MATCH
    ``container_runtime.rs::VCO_OUR_CONTAINER_NAMES``."""
    return frozenset(
        n for s in _c.CANONICAL_CONTAINERS for n in _c.all_known_names(s)
        if n.startswith(("vco_", "vct_"))
    )


def vco_compose_project(install_root: Optional[Path]) -> str:
    """The compose project VCO's own stack runs under — what the compose
    identity guard already derives (:func:`vco_lib.containers.compose_project_name`
    of ``infrastructure/``), so a volume compose created for VCO carries it in
    its ``com.docker.compose.project`` label. ``""`` without an install root."""
    if install_root is None:
        return ""
    infra = Path(install_root) / INFRA_COMPOSE_REL.parent
    try:
        text = (Path(install_root) / INFRA_COMPOSE_REL).read_text(encoding="utf-8")
    except (OSError, ValueError):
        text = ""
    return _c.compose_project_name(infra, text)


def data_evidence(containers: set[str], volumes: set[str], names: Sequence[str], *,
                  own_project: str, label_of: Callable[[str], Optional[str]],
                  corroborate_overrides: bool) -> bool:
    """Pure: do these listings show VCO's data under ONE runtime?

    A VCO container, or a ``vco_*`` default volume, is evidence on its own. A
    volume named by a ``VCT_*_VOLUME_NAME`` override (``names`` beyond the
    defaults) is a name the USER picked — an unrelated ``ollama`` volume can
    carry it — so with ``corroborate_overrides`` it counts only when its compose
    project label (``label_of``) is ``own_project`` (R10 J8; a VCO container
    under the same runtime has already answered ``True``). MUST MATCH
    ``container_runtime.rs::data_evidence`` —
    ``tests/fixtures/runtime_data_evidence_cases.json`` runs both."""
    if containers & _our_container_names():
        return True
    for name in names:
        if name not in volumes:
            continue
        if name in VCO_VOLUME_NAMES or not corroborate_overrides:
            return True
        if own_project and label_of(name) == own_project:
            return True
    return False


def _volume_project_label(runtime: str, volume: str, run: RunFn) -> Optional[str]:
    fmt = '{{index .Labels "' + _c.COMPOSE_PROJECT_LABEL + '"}}'
    try:
        res = run([runtime, "volume", "inspect", "--format", fmt, volume],
                  capture_output=True, text=True, timeout=_LIST_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def vco_data_under(runtime: str, *, run: Optional[RunFn] = None,
                   install_root: Optional[Path] = None,
                   env: Optional[Mapping[str, str]] = None,
                   corroborate_overrides: bool = False) -> Optional[bool]:
    """Does ``runtime`` hold VCO's containers or named volumes?

    ``True``/``False`` only when BOTH listings answered; ``None`` when either
    could not (daemon down, CLI error) — "could not look" is never "empty".
    Only VCO-prefixed containers count (``vco_*`` / ``vct_*``: an unprefixed
    ``ollama`` may be the user's own) and the install's ACTUAL volume names
    (:func:`vco_volume_names` of ``install_root``). Read-only: ``ps -a``,
    ``volume ls`` and — only for an override-named volume that needs it —
    ``volume inspect``.

    ``corroborate_overrides``: set it when asking about the runtime VCO would
    SWITCH TO — an override-named volume then counts only when corroborated
    there (:func:`data_evidence`, R10 J8). The recorded runtime is asked
    without it: counting the user's adopted volume as data there only ever
    keeps the record.
    """
    _run = run or _tsd.run
    containers = _list_names([runtime, "ps", "-a", "--format", "{{.Names}}"], _run)
    if containers is None:
        return None
    volumes = _list_names([runtime, "volume", "ls", "--format", "{{.Name}}"], _run)
    if volumes is None:
        return None
    return data_evidence(
        containers, volumes, vco_volume_names(install_root, env=env),
        own_project=vco_compose_project(install_root) if corroborate_overrides else "",
        label_of=lambda vol: _volume_project_label(runtime, vol, _run),
        corroborate_overrides=corroborate_overrides,
    )


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_bind_source(value: str) -> bool:
    """Compose's rule for a volume SOURCE: a path (``/``, ``.``, ``~``, ``\\``
    or a drive letter) is a bind mount; anything else names a volume."""
    return value.startswith(("/", ".", "~", "\\")) or bool(_DRIVE_PATH_RE.match(value))


def bind_sources_from(env_file_text: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Pure: every non-empty :data:`DATA_SOURCE_KEYS` value that is a PATH
    (a bind mount), in ``infrastructure/.env`` (file order) then in ``env``
    (key order), each once. A value that is not a path names a volume and is
    left to :func:`vco_volume_names`. MUST MATCH
    ``container_runtime.rs::bind_sources_from`` (the shared fixture)."""
    found: list[str] = []

    def _add(value: str) -> None:
        v = (value or "").strip()
        if v and _is_bind_source(v) and v not in found:
            found.append(v)

    for key, value in parse_env_lines(env_file_text or ""):
        if key in DATA_SOURCE_KEYS:
            _add(value)
    for key in DATA_SOURCE_KEYS:
        _add(env.get(key) or "")
    return tuple(found)


def bind_data_source(install_root: Optional[Path], *,
                     env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The first bind-mounted data folder of this install that EXISTS and is
    not empty (or cannot be listed — not provably empty), or ``None``.

    Relative sources resolve against ``infrastructure/`` (compose's project
    dir), ``~`` against the home directory. Such a folder is VCO's data on the
    host — no runtime's volume listing can say where it belongs (R10 J2).
    MUST MATCH ``container_runtime.rs::bind_data_source``."""
    if install_root is None:
        return None
    infra = Path(install_root) / INFRA_ENV_REL.parent
    for src in bind_sources_from(_infra_env_text(install_root),
                                 os.environ if env is None else env):
        path = Path(os.path.expanduser(src))
        if not path.is_absolute():
            path = infra / path
        if not path.is_dir():
            continue
        try:
            if next(path.iterdir(), None) is None:
                continue
        except OSError:
            pass  # a data folder this user cannot list is not provably empty
        return str(path)
    return None


@functools.lru_cache(maxsize=1)
def _messages() -> dict[str, dict[str, str]]:
    data = tomllib.loads(MESSAGES_PATH.read_text(encoding="utf-8"))
    return {"unusable": dict(data["unusable"]), "not_switched": dict(data["not_switched"])}


def _fill(template: str, **values: str) -> str:
    """``{name}`` → value, literally (the Rust side does the same)."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def not_switched_text(decline: str, *, pinned: str, bind: str = "") -> str:
    """The ``not_switched`` suffix for ``decline`` (a key of the shared table)."""
    return _fill(_messages()["not_switched"][decline], pinned=pinned,
                 other=_other(pinned), bind=bind)


def unusable_detail(pinned: str, source: str, status: str,
                    decline: Optional[str] = None, *, bind: str = "") -> str:
    """Why ``pinned`` is not driven, from the shared table
    (``runtime_reconcile_messages.toml``): its state (``missing``, else
    ``down``) plus the ``decline`` suffix. MUST MATCH
    ``container_runtime.rs::record_reconcile_note``."""
    msgs = _messages()["unusable"]
    what = _fill(msgs["missing" if status == "missing" else "down"], pinned=pinned)
    if decline is not None:
        what += not_switched_text(decline, pinned=pinned, bind=bind)
    return _fill(msgs["head"], pinned=pinned, source=source, what=what)


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

    ``rewrite=False`` is the READ-ONLY form (the resolver, the boot wrapper):
    no runtime.txt write, no daemon start, no ledger entries — and in case (a)
    it switches only on positive evidence (the other runtime holds VCO's
    data); "no data anywhere" stays install's decision (R9 H1).
    """
    _which = which or _tsd.which
    _run = run or _tsd.run
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
        return _reconcile_usable(root, pinned, other, _which, _run, rewrite, started, env)
    if status == "down":
        # (b) Its data may well be there; it cannot be looked at, so the record
        # stands and nothing is switched.
        return _unusable(root, pinned, via, status, None, started, rewrite)

    # (a) The recorded runtime is not installed (not on PATH, not in the usual
    # install locations).
    if read_confirmed(root) == pinned:
        # R9 H2: the user chose it with `--container`; only the user switches
        # (and the other runtime is not even started for a switch that will
        # not happen).
        return _unusable(root, pinned, via, status, "confirmed", started, rewrite)
    bind = bind_data_source(root, env=env)
    if bind is not None:
        # R10 J2: the data is a folder on the host, under neither runtime —
        # the record is the only statement of which runtime serves it. Install
        # keeps it (action_required names the explicit switch); read-only
        # refuses. Nothing is started for a switch that will not happen.
        return _unusable(root, pinned, via, status, "bind_data", started, rewrite, bind=bind)
    ostatus, ostarted = _start_if_down(other, _status(other, _which, _run), starter, _which, _run)
    started = "; ".join(x for x in (started, ostarted) if x)
    if ostatus != "usable":
        return _unusable(root, pinned, via, status, "other_not_usable", started, rewrite)
    data = vco_data_under(other, run=_run, install_root=root, env=env, corroborate_overrides=True)
    if data is None:
        return _unusable(root, pinned, via, status, "unlistable", started, rewrite)
    if not data and not rewrite:
        # R9 H1: a read-only caller switches on POSITIVE evidence only. "Not
        # found by me" is not "gone" (a PATH the table does not cover), and
        # starting the stack on the other runtime's EMPTY volumes is the
        # outcome this module exists to prevent. install.py decides this one.
        return _unusable(root, pinned, via, status, "no_data", started, rewrite)
    why = (f"{pinned} is not installed; {other} answers and holds VCO's containers/volumes"
           if data else f"{pinned} is not installed; {other} answers and no VCO data exists under it")
    return _rewritten(root, pinned, other, why, rewrite, started)


def _reconcile_usable(root: Path, pinned: str, other: str, which: WhichFn, run: RunFn,
                      rewrite: bool, started: str,
                      env: Optional[Mapping[str, str]] = None) -> Reconciliation:
    kept = Reconciliation(Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                          f"{pinned} answers", started=started)
    if read_confirmed(root) == pinned or _status(other, which, run) != "usable":
        return kept
    bind = bind_data_source(root, env=env)
    if bind is not None:
        # R10 J2: VCO's data is a host folder (a bind mount): whatever the
        # other runtime lists, it is not "the data" — the record stands.
        return Reconciliation(Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                              f"{pinned} answers; VCO's data is in the bind-mounted folder "
                              f"{bind}, so the recorded {pinned} is kept", started=started)
    here = vco_data_under(pinned, run=run, install_root=root, env=env)
    there = vco_data_under(other, run=run, install_root=root, env=env, corroborate_overrides=True)
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
              decline: Optional[str], started: str, rewrite: bool, *,
              bind: str = "") -> Reconciliation:
    """``decline``: why a stale record was NOT switched — a ``not_switched``
    key of the shared table (``None`` when no switch was considered)."""
    source = via if via == _c.PIN_VIA_ENV else str(_c.runtime_txt_path(root)) if root else str(via)
    detail = unusable_detail(pinned, source, status, decline, bind=bind)
    if started:
        detail += f" ({started})"
    entries = (_unusable_entry(root, pinned, via or "", status, detail),) if rewrite else ()
    return Reconciliation(Outcome.UNUSABLE, None, pinned, via, detail, entries, started)


def _unusable_title(pinned: str) -> str:
    """The ONE title of ``container_runtime_unusable`` (R10 J4) — install, the
    boot wrapper and the session hooks all write this condition; a title per
    writer made the entry flip on every re-emission."""
    if pinned:
        return f"Container runtime {pinned} is not usable — containers were not started"
    return "No usable container runtime — containers were not started"


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
        title=_unusable_title(pinned),
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
    # R9 H1(b)/H5: a runtime found only in the usual install locations is
    # "installed" to the reconcile below; the rest of this run spawns it BY
    # NAME, so its directory joins this process's PATH, placed per the table
    # (`tool_search_dirs.reachable_path` — the order every surface uses).
    reach = _tsd.reachable_path()
    if reach:
        os.environ["PATH"] = reach
        log_event("runtime_reconcile", "ok",
                  "container runtime found outside PATH; its directory was added to PATH",
                  data={"path": reach})
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
    _which = which or _tsd.which
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
    root = _entry_root(entry)
    recorded = _c.read_runtime_txt(root)
    if root is None or recorded is None:
        return None
    if read_confirmed(root) == recorded:
        return False
    if bind_data_source(root) is not None:
        return False  # R10 J2: the reconcile keeps the record — nothing to ask
    _which, _run = which or _tsd.which, run or _tsd.run
    if any(_status(rt, _which, _run) != "usable" for rt in _c.RUNTIME_CANDIDATES):
        return None
    here = vco_data_under(recorded, run=_run, install_root=root)
    there = vco_data_under(_other(recorded), run=_run, install_root=root,
                           corroborate_overrides=True)
    if here is None or there is None:
        return None
    return bool(here and there)


# ---------------------------------------------------------------------------
# Boot wrapper (G5 read-only reconcile, G6 exit-3 visibility)
# ---------------------------------------------------------------------------


#: Who is recording a refusal — the entry's ``detected`` text says which
#: surface started nothing (``--source`` of the CLI). The TITLE is the
#: condition's one title (:func:`_unusable_title`, R10 J4).
_REFUSAL_SOURCES: dict[str, str] = {
    "boot": "The boot service (launch-claude-mcp-stack) started nothing",
    "session": "A session-start hook started nothing",
}

#: The hard ceiling on ``python -m vco_lib.runtime_reconcile record-boot-refusal``
#: (R9 H7): past it the CLI exits 0 whatever is still in flight. Above
#: :data:`BOOT_LEDGER_LOCK_TIMEOUT_S`, so a held lock is normally what ends the
#: wait; this bounds everything else (a stalled filesystem, a slow import).
#: Callers need no `timeout` binary of their own (macOS has none).
RECORD_REFUSAL_DEADLINE_S = 20.0


def record_boot_refusal(install_root: Path, reason: str, *,
                        env: Optional[Mapping[str, str]] = None,
                        source: str = "boot") -> bool:
    """A pinned runtime was refused (or none was found) and nothing was
    started — by the boot wrapper's exit 3 (``source="boot"``) or a session
    hook (``source="session"``): put it in ``install_root``'s ledger through
    the one emitter, so session start and the launcher show it. Clears through
    :func:`unusable_still_applies`.

    Only an INSTALLED clone has a ledger to write (``state/install/`` is what
    install.py creates): a development checkout running the wrapper — the test
    suite does — records nothing (returns ``False``)."""
    from vco_lib.deferral_emit import emit  # noqa: PLC0415

    if not (Path(install_root) / "state" / "install").is_dir():
        return False
    pin = _c.runtime_pin(env, install_root=install_root, warn=lambda _m: None)
    pinned, via = (pin[0], pin[1]) if pin is not None else ("", "")
    detected_head = _REFUSAL_SOURCES.get(source, _REFUSAL_SOURCES["boot"])
    entry = DeferralEntry(
        condition_id=CID_UNUSABLE,
        title=_unusable_title(pinned),
        detected=f"{detected_head}: {reason.strip()}",
        why_deferred=(
            "VCO never starts the stack under the runtime the data is NOT on "
            "(podman and docker keep separate volumes). This entry clears by itself once the "
            "runtime answers; the next session start then brings the stack up."
        ),
        command_to_apply=(f"# Start {pinned}:\n{_start_command(pinned)}" if pinned else
                          "# Install or start podman (or docker), then start a session or run:\n"
                          "python install.py --update"),
        severity="warning",
        dismiss_fields={"runtime": pinned, "via": via, "root": str(install_root)},
    )
    # R9 H7: bounded — an update holding the ledger lock must never stall boot
    # (the wrapper's own `timeout` is absent on macOS). Past the bound the
    # emitter logs and returns False; the next boot or session writes it.
    # R10 J4: every session start (twice) and every boot re-emit this while it
    # holds — the entry keeps the `detected_at` it was first recorded with,
    # and an unchanged entry is not rewritten.
    return emit(Path(install_root), entry, lock_timeout_s=BOOT_LEDGER_LOCK_TIMEOUT_S,
                keep_first_detected=True)


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vco_lib.runtime_reconcile")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("boot", help="Read-only reconcile of the install's runtime record.")
    b.add_argument("--root", required=True)
    b.add_argument("--json", action="store_true")
    r = sub.add_parser(
        "record-boot-refusal",
        help="Ledger entry for a refused runtime (the boot wrapper's exit 3, a session hook's "
             "skip). Bounded by itself: never needs an external `timeout`.")
    r.add_argument("--root", default=None,
                   help="Install root whose ledger to write (default: the clone this vco_lib "
                        "belongs to — the same root whose runtime.txt the resolver read).")
    r.add_argument("--reason", default="")
    r.add_argument("--source", choices=sorted(_REFUSAL_SOURCES), default="boot")
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
    if a.root:
        root: Optional[Path] = Path(a.root)
    else:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

        root = resolve_install_root()
    if root is None:
        return 0
    if not record_refusal_bounded(root, a.reason, source=a.source):
        # The worker is still in flight past the deadline: leave NOW, whatever
        # it is blocked on (a daemon thread would otherwise be waited for by
        # nothing, but interpreter shutdown can still stall on I/O).
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


def record_refusal_bounded(install_root: Path, reason: str, *, source: str = "boot",
                           deadline_s: Optional[float] = None) -> bool:
    """:func:`record_boot_refusal` under a hard deadline (R9 H7) — ``True``
    when it finished (written or not), ``False`` when it was still running at
    :data:`RECORD_REFUSAL_DEADLINE_S` (the ledger is then left as it was; the
    next boot or session records it). Never raises."""
    import threading  # noqa: PLC0415

    limit = RECORD_REFUSAL_DEADLINE_S if deadline_s is None else deadline_s

    def _work() -> None:
        try:
            record_boot_refusal(install_root, reason, source=source)
        except Exception as exc:  # noqa: BLE001 — best effort: never blocks boot
            print(f"could not record the runtime refusal: {exc}", file=sys.stderr)

    worker = threading.Thread(target=_work, name="record-boot-refusal", daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        print(f"record-boot-refusal: gave up after {limit:g}s; the ledger was left as it was",
              file=sys.stderr)
        return False
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    return _cli(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
