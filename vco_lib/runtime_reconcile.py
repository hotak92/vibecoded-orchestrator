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
While one exists and is not provably empty (a folder VCO cannot probe — an
unsearchable parent — counts, R11 L1), the other runtime's listing is read by
:func:`bind_verdict` (R11 L2/L3), in this order:

(i)   VCO containers RUNNING under the other runtime → the stack lives there:
      switched (read-only drives it; install re-records it, informational);
(ii)  the recorded runtime is NOT installed and EVERY service's data is a
      folder (:func:`all_services_bind`) → no named volume is stranded:
      switched at INSTALL. A READ-ONLY caller needs positive evidence that the
      other runtime serves VCO (``other_kind != KIND_NONE`` — R12 M1: a
      session hook must never drive the other runtime onto the folders on the
      strength of "I found nothing anywhere"); ``KIND_NONE`` falls to the
      ``no_data`` refusal, exactly as the no-bind shape always did;
(iii) STOPPED VCO containers under the other runtime → data under both:
      nothing is driven in (a) (read-only refuses with the reason); install
      records ``container_runtime_data_under_both`` (action_required);
(iv)  only a leftover named volume there, or nothing → the record stands:
      (c) keeps it, (a) refuses read-only and records ``action_required`` at
      install.

ONE ENGINE, TWO NAMES (R12 M7): the ``podman-docker`` package makes ``docker``
a shim for podman, so one engine answers to both names and every listing is
the same on both sides. :func:`runtimes_are_one_engine` is the ONE bounded
identity probe (a version output naming the other engine, else a non-empty
intersection of container IDs); when it says the two names are one engine,
every "data under both" verdict is short-circuited to KEPT — there is no
"both", and nothing to switch — and the answer says so
(:attr:`Reconciliation.same_engine`).

The runtime is never switched silently while VCO's data lives under the recorded
one — every switch is a positive-evidence decision with a ledger record.

``decide`` — the one verdict for every surface (R12 convergence)
-----------------------------------------------------------------
:func:`decide` (CLI: ``python -m vco_lib.runtime_reconcile decide --json
[--root <install_root>] [--mode read-only|install] [--purpose infra|module]``)
is the ONE JSON answer the Rust surfaces (launcher infra stack, module plane,
hub supervisor, install preflight) render instead of mirroring this module's
logic (A>B>C tier A: the path is user-action-triggered and ms-scale). It is
the SAME core as ``python -m vco_lib.containers resolve`` — :func:`decide`
calls :func:`vco_lib.containers.resolve` and this module's :func:`reconcile`
and renders both; ``containers resolve`` renders the same answer for the
session hooks' shell contract. ``--mode read-only`` (the default) writes
nothing and starts nothing; ``--mode install`` is install.py's form (the
record may be re-written, the ledger appended). ``--purpose infra`` probes
compose; ``--purpose module`` does not (the module plane drives single
containers, not compose). Every probe inside is time-bounded by the existing
per-call timeouts (:data:`_LIST_TIMEOUT_S` and the containers module's probe
timeouts).

Exit codes: ``0`` resolved; ``3`` refused (state ``absent`` or ``unknown`` —
the ``state`` field distinguishes them); anything else is an internal error.

The JSON (``"schema": 1``; add fields, never change existing ones):

=========================  =====================================================
key                        meaning
=========================  =====================================================
``schema``                 always ``1``.
``state``                  ``resolved`` / ``absent`` / ``unknown`` —
                           :class:`vco_lib.containers.RuntimeState`.
``runtime``                the runtime to drive, or ``None``.
``compose``                compose argv prefix (``["podman", "compose"]``) or
                           ``None`` (``null`` under ``--purpose module``).
``compose_form``           ``subcommand`` / ``standalone`` / ``None``.
``binary_path``            what :func:`vco_lib.tool_search_dirs.which` finds
                           for ``runtime`` (``None`` when not resolved).
``search_path``            the PATH the caller should export so the runtime
                           tools resolve BY NAME to what was probed
                           (:func:`vco_lib.tool_search_dirs.reachable_path`);
                           ``None`` when nothing needs adding or no runtime.
``installed``              the first candidate binary installed, usable or not.
``requested``              the pinned runtime (``None`` = auto-detect).
``requested_via``          ``env`` / ``record`` / ``confirmed`` / ``auto`` —
                           ``record`` is ``state/install/runtime.txt``,
                           ``confirmed`` adds that ``runtime.confirmed``
                           matches it (never switched, only the user does).
``requested_installed``    whether the pinned binary is installed at all.
``alternative_usable``     the OTHER runtime when it is usable and the pinned
                           one is not — the user's repin target (never driven).
``record_reconciled``      the stale record was answered with the other
                           runtime, read-only, on positive evidence.
``outcome``                the reconcile's :class:`Outcome` for the record.
``not_switched_key``       why a stale record was NOT switched — a
                           ``[not_switched]`` key of the shared table, or
                           ``None``.
``not_switched``           that reason rendered (the full note; append as
                           ``(not switched: ...)``), or ``None``.
``same_engine``            the two names are ONE engine (M7) — no "both",
                           no switch; the recorded runtime is kept.
``refused``                ``True`` unless ``state`` is ``resolved``.
``refusal``                the full user-facing refusal text when refused,
                           else ``None``.
``reason``                 always set: why this runtime (or why none).
=========================  =====================================================

Examples (abridged):

.. code-block:: json

    {"schema": 1, "state": "resolved", "runtime": "podman",
     "compose": ["podman", "compose"], "compose_form": "subcommand",
     "requested": "docker", "requested_via": "record",
     "record_reconciled": true, "outcome": "rewritten",
     "not_switched_key": null, "same_engine": false, "refused": false,
     "reason": "stale runtime record: ... — using docker→podman ..."}

    {"schema": 1, "state": "absent", "runtime": null, "compose": null,
     "requested": "podman", "requested_via": "record",
     "outcome": "unusable", "not_switched_key": "no_data",
     "not_switched": "the container runtime is pinned to podman by ...: ...",
     "refused": true, "refusal": "... (not switched: ...no_data...)"}

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
import stat as stat_mod
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
    "effective_data_sources",
    "bind_sources_from",
    "bind_data_source",
    "bind_folder_holds_data",
    "all_services_bind",
    "install_all_bind",
    "data_kind",
    "vco_data_kind",
    "bind_verdict",
    "runtimes_are_one_engine",
    "KIND_RUNNING",
    "KIND_STOPPED",
    "KIND_VOLUMES",
    "KIND_NONE",
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
    "decide",
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
    #: Why a stale record was NOT switched — a ``[not_switched]`` key of the
    #: shared table (``runtime_reconcile_messages.toml``), else ``None``.
    decline: Optional[str] = None
    #: The two runtime names are ONE engine on this machine (a
    #: ``podman-docker`` shim — R12 M7): every "data under both" shape is
    #: really one engine's listing read twice, so the record is KEPT and this
    #: flag says why no switch was even considered.
    same_engine: bool = False
    #: The bind-mounted data folder this outcome was decided over (R12 M3):
    #: non-empty exactly when the decline is a BIND-layout one, so a renderer
    #: can pick the rationale that matches what was probed.
    bind: str = ""


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


def _list_output(argv: Sequence[str], run: RunFn) -> Optional[str]:
    """A bounded listing command's stdout (``None`` when it could not run or
    failed) — the raw-text sibling of :func:`_list_names`."""
    try:
        res = run(list(argv), capture_output=True, text=True, timeout=_LIST_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout or ""


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


#: The compose project VCO's own stack runs under, so a volume compose created
#: for VCO carries it in its ``com.docker.compose.project`` label — the ONE home
#: :func:`vco_lib.containers.own_compose_project` (R11 L7: this module had
#: grown the sixth inline copy of it).
vco_compose_project = _c.own_compose_project


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


#: What a runtime's listings show of VCO's data (R11 L2) — MUST MATCH
#: ``runtime_evidence.rs::DataKind`` (``tests/fixtures/runtime_data_evidence_cases.json``
#: ``kind`` runs both). A VCO container outranks a volume, a RUNNING one a
#: stopped one: a container mounts the data wherever it lives, a volume is only
#: a copy that may be a leftover.
KIND_RUNNING = "running"
KIND_STOPPED = "stopped"
KIND_VOLUMES = "volumes"
KIND_NONE = "none"


def data_kind(containers: set[str], running: set[str], volumes: set[str], names: Sequence[str], *,
              own_project: str, label_of: Callable[[str], Optional[str]],
              corroborate_overrides: bool) -> str:
    """Pure: WHAT these listings show of VCO's data under one runtime — a VCO
    container that is running (``ps``), one that is not (``ps -a`` only),
    only volumes (:func:`data_evidence`'s volume rule), or nothing. MUST MATCH
    ``runtime_evidence.rs::data_kind``."""
    ours = containers & _our_container_names()
    if ours:
        return KIND_RUNNING if running & ours else KIND_STOPPED
    if data_evidence(set(), volumes, names, own_project=own_project, label_of=label_of,
                     corroborate_overrides=corroborate_overrides):
        return KIND_VOLUMES
    return KIND_NONE


#: The verdicts of :func:`bind_verdict`.
VERDICT_SWITCH = "switch"
VERDICT_BOTH = "both"
VERDICT_KEEP = "keep"


def bind_verdict(other_kind: str, *, pinned_missing: bool, all_bind: bool,
                 here_running: bool = False,
                 positive_only: bool = False) -> str:
    """Pure: what a bind-mounted data layout makes of the OTHER runtime's
    listing (R11 L2/L3) — the precedence the stale-record reconcile applies
    while VCO's data is (partly) a folder on the host:

    1. VCO containers RUNNING under the other runtime are positive evidence
       that the stack lives there: ``switch`` (``both`` if VCO containers also
       run under the recorded runtime).
    2. The recorded runtime is NOT installed and EVERY service's data is a
       folder (no named volume in play, :func:`all_services_bind`): switching
       strands nothing — ``switch`` at install. With ``positive_only`` (a
       READ-ONLY caller — R12 M1) the rule needs evidence that the other
       runtime serves VCO (``other_kind != KIND_NONE``: a stopped container
       or a VCO volume is evidence; nothing is not), else it falls through —
       a session hook must not drive the other runtime onto the folders on
       the strength of "I found nothing anywhere", and the decline becomes
       ``no_data``, as the no-bind shape always was.
    3. STOPPED VCO containers under the other runtime next to the folder:
       data under both — ``both`` (only the user knows which one serves it).
    4. Only a leftover named volume there, or nothing: ``keep`` (R10 J2 — the
       folder is the data and a volume is not evidence about it).

    MUST MATCH ``runtime_evidence.rs::bind_verdict`` (the fixture's
    ``bind_verdict`` rows run both; the ``bind_verdict_positive_only`` rows
    are the read-only half, Python-side until the Rust mirrors retire)."""
    if other_kind == KIND_RUNNING:
        return VERDICT_BOTH if here_running else VERDICT_SWITCH
    if pinned_missing and all_bind and not (positive_only and other_kind == KIND_NONE):
        return VERDICT_SWITCH
    if other_kind == KIND_STOPPED:
        return VERDICT_BOTH
    return VERDICT_KEEP


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
    kind = _data_listing(runtime, run=run, install_root=install_root, env=env,
                         corroborate_overrides=corroborate_overrides, want_running=False)
    return None if kind is None else kind != KIND_NONE


def vco_data_kind(runtime: str, *, run: Optional[RunFn] = None,
                  install_root: Optional[Path] = None,
                  env: Optional[Mapping[str, str]] = None,
                  corroborate_overrides: bool = False) -> Optional[str]:
    """:func:`vco_data_under`, saying WHAT holds the data (:func:`data_kind`)
    — a ``KIND_*`` value, or ``None`` when a listing could not run. Lists the
    RUNNING containers (``ps``) only when a VCO container exists at all.
    Read-only. MUST MATCH ``container_runtime.rs::vco_data_kind``."""
    return _data_listing(runtime, run=run, install_root=install_root, env=env,
                         corroborate_overrides=corroborate_overrides, want_running=True)


#: Word-boundary runtime names in a ``version`` output — the shim leg of the
#: one-engine probe (R12 M7). ``podman version`` under a ``podman-docker``
#: shim prints e.g. ``docker version 4.9.3 (podman ...)``, naming BOTH.
_ENGINE_NAME_RE = re.compile(r"\b(?:podman|docker)\b")

#: A FULL container ID as ``ps -a --no-trunc --format {{.ID}}`` prints it
#: (12–64 hex chars). Only these count for the ID-intersection leg: names,
#: aliases and anything else a listing may emit must never read as "the same
#: container under both runtimes".
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")


def _container_ids(runtime: str, run: RunFn) -> Optional[set[str]]:
    """The FULL container IDs a runtime lists (``ps -a --no-trunc``), or
    ``None`` when the listing could not run."""
    out = _list_output([runtime, "ps", "-a", "--no-trunc", "--format", "{{.ID}}"], run)
    if out is None:
        return None
    return {line.strip() for line in out.splitlines()
            if _CONTAINER_ID_RE.match(line.strip())}


def runtimes_are_one_engine(a: str, b: str, *, run: Optional[RunFn] = None) -> Optional[bool]:
    """Are these two runtime NAMES one engine on this machine (R12 M7)?

    A ``podman-docker`` (or ``docker``-compatible) shim makes ``docker`` a
    second NAME for podman: every "data under both runtimes" shape is then
    one engine's listing read twice, and a "switch" would switch nothing.
    ``True`` when that is PROVEN, ``None`` when it cannot be told — never
    ``False``, because two genuinely distinct engines are indistinguishable
    from two probes that could not prove anything.

    TWO bounded legs (each within :data:`_LIST_TIMEOUT_S`):

    1. SHIM: ``<name> version`` output naming the OTHER runtime at a word
       boundary (``docker version ... (podman ...)``). One hit on either
       side proves it.
    2. IDs: both ``ps -a --no-trunc`` listings answer and SHARE a full
       container ID — two distinct engines cannot list the same container.

    Read-only. Only consulted on the ambiguous paths (both runtimes answer
    AND the data seems to be under both), so the common cases pay nothing."""
    _run = run or _tsd.run
    for name, other in ((a, b), (b, a)):
        out = _list_output([name, "version"], _run)
        if out is not None and other in _ENGINE_NAME_RE.findall(out.lower()):
            return True
    ids_a, ids_b = _container_ids(a, _run), _container_ids(b, _run)
    if ids_a is not None and ids_b is not None and ids_a & ids_b:
        return True
    return None


def _data_listing(runtime: str, *, run: Optional[RunFn], install_root: Optional[Path],
                  env: Optional[Mapping[str, str]], corroborate_overrides: bool,
                  want_running: bool) -> Optional[str]:
    _run = run or _tsd.run
    containers = _list_names([runtime, "ps", "-a", "--format", "{{.Names}}"], _run)
    if containers is None:
        return None
    volumes = _list_names([runtime, "volume", "ls", "--format", "{{.Name}}"], _run)
    if volumes is None:
        return None
    running: set[str] = set()
    if want_running and containers & _our_container_names():
        listed = _list_names([runtime, "ps", "--format", "{{.Names}}"], _run)
        if listed is None:
            return None
        running = listed
    elif not want_running:
        # Without the running listing a VCO container is still data (the
        # bool question does not ask whether it runs).
        running = containers
    return data_kind(
        containers, running, volumes, vco_volume_names(install_root, env=env),
        own_project=vco_compose_project(install_root) if corroborate_overrides else "",
        label_of=lambda vol: _volume_project_label(runtime, vol, _run),
        corroborate_overrides=corroborate_overrides,
    )


_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_bind_source(value: str) -> bool:
    """Compose's rule for a volume SOURCE: a path (``/``, ``.``, ``~``, ``\\``
    or a drive letter) is a bind mount; anything else names a volume."""
    return value.startswith(("/", ".", "~", "\\")) or bool(_DRIVE_PATH_RE.match(value))


def effective_data_sources(env_file_text: str, env: Mapping[str, str]) -> dict[str, str]:
    """ONE resolver for the ``VCT_*_DATA_SOURCE`` knobs (R12 M8): the value
    compose will actually use per :data:`DATA_SOURCE_KEYS` key — ``env``'s
    when the key is SET there (the shell environment wins over the file,
    even when set to the empty string), else the LAST assignment for the key
    in ``infrastructure/.env`` (compose's own precedence). Keys set nowhere
    are absent from the result — a missing key means the service's named
    volume. :func:`bind_sources_from` and :func:`all_services_bind` both
    read THIS; no third copy of the precedence rule may appear."""
    effective: dict[str, str] = {}
    for key, value in parse_env_lines(env_file_text or ""):
        if key in DATA_SOURCE_KEYS:
            effective[key] = value
    for key in DATA_SOURCE_KEYS:
        if key in env:
            effective[key] = env[key]
    return effective


def bind_sources_from(env_file_text: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Pure: every :data:`DATA_SOURCE_KEYS` value that is a PATH (a bind
    mount), each once. A value that is not a path names a volume and is left
    to :func:`vco_volume_names`. Order: the keys the ``infrastructure/.env``
    file mentions, in file order (so the fixture's ``bind_sources`` rows keep
    byte-stable output), then the keys only ``env`` sets, in
    :data:`DATA_SOURCE_KEYS` order — both read through the ONE resolver
    :func:`effective_data_sources`. MUST MATCH
    ``container_runtime.rs::bind_sources_from`` (the shared fixture)."""
    effective = effective_data_sources(env_file_text, env)
    mentioned = [key for key, _ in parse_env_lines(env_file_text or "") if key in DATA_SOURCE_KEYS]
    found: list[str] = []

    def _add(value: str) -> None:
        v = (value or "").strip()
        if v and _is_bind_source(v) and v not in found:
            found.append(v)

    for key in mentioned:
        _add(effective[key])
    for key in DATA_SOURCE_KEYS:
        if key in effective and key not in mentioned:
            _add(effective[key])
    return tuple(found)


def all_services_bind(env_file_text: str, env: Mapping[str, str]) -> bool:
    """Pure: does EVERY service's data mount resolve to a host folder (R11 L3)?
    Every :data:`DATA_SOURCE_KEYS` key must be set (via
    :func:`effective_data_sources` — the ONE precedence resolver) and its
    value a path (:func:`_is_bind_source`); an unset or empty key, or one
    that names a volume, means a named volume is in play. Then no named
    volume is in play anywhere, so a runtime switch strands none. MUST MATCH
    ``runtime_evidence.rs::all_services_bind`` (the fixture's ``all_bind``
    rows run both)."""
    effective = effective_data_sources(env_file_text, env)
    if len(effective) != len(DATA_SOURCE_KEYS):
        return False
    return all(_is_bind_source((value or "").strip()) for value in effective.values())


def install_all_bind(install_root: Optional[Path], *,
                     env: Optional[Mapping[str, str]] = None) -> bool:
    """:func:`all_services_bind` for this install's ``infrastructure/.env`` and
    ``env`` (default :data:`os.environ`)."""
    if install_root is None:
        return False
    return all_services_bind(_infra_env_text(install_root), os.environ if env is None else env)


def _absent(exc: OSError) -> bool:
    """A probe error that means "no folder there" — everything else (EACCES on
    an unsearchable parent, a symlink loop, an I/O error) means "cannot tell".
    MUST MATCH ``runtime_evidence.rs::bind_probe`` (NotFound / NotADirectory)."""
    return isinstance(exc, (FileNotFoundError, NotADirectoryError))


def bind_folder_holds_data(path: Path) -> bool:
    """Is ``path`` a data folder that is not provably empty (R10 J2, R11 L1)?

    A missing path or a non-directory → no. A directory with an entry → yes.
    ANY other error while probing — ``stat`` raising ``PermissionError``
    because a parent is not searchable (``Path.is_dir()`` re-raises that), or
    a directory this user cannot list — means VCO cannot tell, and a folder it
    cannot prove empty counts as data. Never raises. MUST MATCH
    ``runtime_evidence.rs::bind_folder_holds_data`` (the fixture's
    ``bind_probe`` rows run both)."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return not _absent(exc)
    except ValueError:
        return False  # an embedded NUL: not a path compose could mount either
    if not stat_mod.S_ISDIR(st.st_mode):
        return False
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is not None
    except OSError:
        return True  # a data folder this user cannot list is not provably empty


def bind_data_source(install_root: Optional[Path], *,
                     env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The first bind-mounted data folder of this install that holds data
    (:func:`bind_folder_holds_data`: not empty, or not provably empty), or
    ``None``.

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
        if bind_folder_holds_data(path):
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
    ostatus, ostarted = _start_if_down(other, _status(other, _which, _run), starter, _which, _run)
    started = "; ".join(x for x in (started, ostarted) if x)
    if ostatus != "usable":
        return _unusable(root, pinned, via, status, "other_not_usable", started, rewrite)
    decision = _bind_decision(root, pinned, other, pinned_missing=True,
                              positive_only=not rewrite, env=env, run=_run)
    if decision is not None:
        return _bind_stale_record(root, pinned, other, via, status, decision, started, rewrite)
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
    decision = _bind_decision(root, pinned, other, pinned_missing=False,
                              positive_only=not rewrite, env=env, run=run)
    if decision is not None:
        verdict, bind, there, _all_bind = decision
        if verdict == VERDICT_SWITCH:
            # R11 L2 (i): the stack RUNS under the other runtime, on the folder.
            return _rewritten(root, pinned, other,
                              f"{pinned} answers but {other} is running VCO's containers "
                              f"(VCO's data is in the bind-mounted folder {bind})", rewrite, started)
        if verdict == VERDICT_BOTH:
            # R12 M7: a podman-docker shim is ONE engine — its "containers
            # under both" is one listing read twice; no both, no switch.
            if runtimes_are_one_engine(pinned, other, run=run):
                return Reconciliation(
                    Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                    f"{pinned} and {other} are one engine on this machine; the "
                    f"recorded {pinned} is kept", started=started, same_engine=True)
            # R11 L2 (ii): VCO containers under the other runtime next to the
            # folder — only the user knows which runtime serves it.
            entries = (_both_entry(root, pinned, other, bind=bind, kept=True, there=there),) \
                if rewrite else ()
            return Reconciliation(Outcome.DATA_UNDER_BOTH, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                                  f"VCO's data is in the bind-mounted folder {bind} and {other} "
                                  f"holds VCO containers too; keeping the recorded {pinned}",
                                  entries, started, decline="data_under_both", bind=bind)
        # R10 J2 / R11 L2 (iii): at most a leftover volume there — the folder
        # is the data, and the record stands (an unlistable runtime too).
        return Reconciliation(Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                              f"{pinned} answers; VCO's data is in the bind-mounted folder "
                              f"{bind}, so the recorded {pinned} is kept", started=started)
    here = vco_data_kind(pinned, run=run, install_root=root, env=env)
    there = vco_data_kind(other, run=run, install_root=root, env=env, corroborate_overrides=True)
    if here is None or there is None or there == KIND_NONE:
        return kept
    if here != KIND_NONE:
        if runtimes_are_one_engine(pinned, other, run=run):
            return Reconciliation(
                Outcome.KEPT, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                f"{pinned} and {other} are one engine on this machine; the "
                f"recorded {pinned} is kept", started=started, same_engine=True)
        entries = (_both_entry(root, pinned, other, there=there,
                               here_running=here == KIND_RUNNING),) if rewrite else ()
        return Reconciliation(Outcome.DATA_UNDER_BOTH, pinned, pinned, _c.PIN_VIA_RUNTIME_TXT,
                              f"VCO containers/volumes exist under both {pinned} and {other}; "
                              f"keeping the recorded {pinned}", entries, started)
    return _rewritten(root, pinned, other,
                      f"{pinned} answers but holds none of VCO's containers or volumes, "
                      f"while {other} holds them", rewrite, started)


def _bind_decision(root: Path, pinned: str, other: str, *, pinned_missing: bool,
                   env: Optional[Mapping[str, str]], run: RunFn,
                   positive_only: bool = False) -> Optional[tuple[str, str, str, bool]]:
    """The bind-mount arm of the reconcile (R10 J2, R11 L2/L3), read-only:
    ``None`` when it does not apply — no data folder holds anything and (for a
    recorded runtime that is not installed) not every service is a folder —
    else ``(verdict, folder, kind, all_bind)``: a :func:`bind_verdict` (or
    ``"unlistable"`` when a listing could not run), the first data folder
    (``""`` when only :func:`all_services_bind` applies), what the other
    runtime holds (a ``KIND_*``, ``""`` when unlistable) and whether EVERY
    service's data is a host folder (:func:`install_all_bind`).

    ``positive_only`` is the read-only caller's rule for
    :func:`bind_verdict` (R12 M1). The other runtime is asked with
    ``corroborate_overrides`` (R10 J8); the recorded one only when the other
    RUNS VCO's containers and it answers."""
    bind = bind_data_source(root, env=env)
    all_bind = pinned_missing and install_all_bind(root, env=env)
    if bind is None and not all_bind:
        return None
    there = vco_data_kind(other, run=run, install_root=root, env=env, corroborate_overrides=True)
    if there is None:
        return "unlistable", bind or "", "", all_bind
    here_running = False
    if there == KIND_RUNNING and not pinned_missing:
        here = vco_data_kind(pinned, run=run, install_root=root, env=env)
        if here is None:
            return "unlistable", bind or "", there, all_bind
        here_running = here == KIND_RUNNING
    return (bind_verdict(there, pinned_missing=pinned_missing, all_bind=all_bind,
                         here_running=here_running, positive_only=positive_only),
            bind or "", there, all_bind)


def _bind_stale_record(root: Path, pinned: str, other: str, via: Optional[str], status: str,
                       decision: tuple[str, str, str, bool], started: str,
                       rewrite: bool) -> Reconciliation:
    """Case (a) — the recorded runtime is not installed — under a bind-mount
    layout, per :func:`bind_verdict`."""
    verdict, bind, there, all_bind = decision
    if verdict == "unlistable":
        return _unusable(root, pinned, via, status, "unlistable", started, rewrite)
    if verdict == VERDICT_SWITCH:
        # R11 L2 (i) / L3: a switch either follows the running stack or, with
        # every service in a folder, strands no named volume — read-only
        # surfaces may drive it, install re-records it (informational).
        why = (f"{pinned} is not installed; {other} answers and is running VCO's containers"
               + (f" (VCO's data is in the bind-mounted folder {bind})" if bind else "")
               if there == KIND_RUNNING else
               f"{pinned} is not installed; {other} answers, and every service's data is "
               "in a folder on this host, so switching leaves no named volume behind")
        return _rewritten(root, pinned, other, why, rewrite, started)
    if verdict == VERDICT_BOTH:
        # R11 L2 (ii): stopped VCO containers under the other runtime next to
        # the folder — data under both. Nothing is driven; install asks.
        res = _unusable(root, pinned, via, status, "data_under_both", started, False, bind=bind)
        entries = (_both_entry(root, pinned, other, bind=bind, kept=False, there=there),) \
            if rewrite else ()
        return Reconciliation(res.outcome, None, pinned, via, res.detail, entries, started,
                              decline="data_under_both", bind=bind)
    if not rewrite and there == KIND_NONE and all_bind:
        # R12 M1: the read-only rule (ii) needed positive evidence that the
        # other runtime serves VCO; nothing was found anywhere, so nothing is
        # switched — the no-bind "no data anywhere" decline, not "bind_data".
        return _unusable(root, pinned, via, status, "no_data", started, rewrite, bind=bind)
    # R10 J2 / R11 L2 (iii): only a leftover volume there — the folder is the
    # data; install keeps the record (action_required), read-only refuses.
    return _unusable(root, pinned, via, status, "bind_data", started, rewrite, bind=bind)


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
    entries = (_unusable_entry(root, pinned, via or "", status, detail,
                               bind_decline=bool(bind)),) if rewrite else ()
    return Reconciliation(Outcome.UNUSABLE, None, pinned, via, detail, entries, started,
                          decline=decline, bind=bind)


def _unusable_title(pinned: str) -> str:
    """The ONE title of ``container_runtime_unusable`` (R10 J4) — install, the
    boot wrapper and the session hooks all write this condition; a title per
    writer made the entry flip on every re-emission."""
    if pinned:
        return f"Container runtime {pinned} is not usable — containers were not started"
    return "No usable container runtime — containers were not started"


def _unusable_entry(root: Optional[Path], pinned: str, via: str, status: str,
                    detail: str, *, bind_decline: bool = False) -> DeferralEntry:
    """``bind_decline``: the refusal is a BIND-layout decline (R12 M3) —
    VCO's data is a host folder, so the "SEPARATE named volumes / EMPTY
    knowledge graph" rationale does not apply and must not be claimed."""
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
    if bind_decline:
        why_deferred = (
            "VCO's data is the bind-mounted folder on this host, which either runtime "
            f"could serve, and VCO will not pick one for you — it will not start the stack "
            f"under {other} until the choice is made. Everything that does not need "
            "containers was completed. This entry clears by itself once "
            f"{pinned} answers; the next session start (or `python install.py --update`) "
            "then brings the stack up."
        )
    else:
        why_deferred = (
            f"VCO will not start the stack under {other} on its own: podman and docker keep "
            "SEPARATE named volumes, so the other runtime would bring up an EMPTY knowledge "
            "graph next to your data. Everything that does not need containers was completed. "
            f"This entry clears by itself once {pinned} answers; the next session start (or "
            "`python install.py --update`) then brings the stack up."
        )
    return DeferralEntry(
        condition_id=CID_UNUSABLE,
        title=_unusable_title(pinned),
        detected=detail + ".",
        why_deferred=why_deferred,
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


def _both_entry(root: Path, recorded: str, other: str, *, bind: str = "",
                kept: bool = True, there: str = KIND_STOPPED,
                here_running: bool = False) -> DeferralEntry:
    """``bind``: VCO's data is that host folder (R11 L2 (ii)) and ``other``
    holds VCO containers beside it; ``kept``: whether VCO went on using
    ``recorded`` (it answers) or started nothing (it is not installed).
    ``there`` / ``here_running`` say what the probes actually FOUND under
    ``other`` (R12 M2: the text never claims more than was probed — not
    "containers or volumes" when the listing said which, and "running" only
    when a running container was seen)."""
    if there == KIND_RUNNING:
        holds = (f"{other} is running VCO containers as well" if here_running else
                 f"{other} is running VCO containers")
    elif there == KIND_VOLUMES:
        holds = f"{other} holds VCO volumes (no containers)"
    else:
        holds = f"{other} holds VCO containers that are not running there"
    outcome = (f"VCO kept using {recorded}." if kept else
               f"{recorded} is not installed, so VCO started nothing.")
    if bind:
        detected = (f"VCO's data is in the bind-mounted folder {bind}. {recorded} is recorded in "
                    f"{_c.runtime_txt_path(root)}, and {holds}; either runtime could serve that "
                    f"folder. {outcome}")
        why = ("Only you know which runtime should serve that folder — two engines on one data "
               "folder would corrupt it — so VCO will not pick for you.")
    else:
        detected = (f"{recorded} is recorded in {_c.runtime_txt_path(root)} and answers, and "
                    f"{holds}. {outcome}")
        why = "Only you know which copy is current; VCO will not merge them or pick for you."
    if kept:
        command_to_apply = (f"# Keep {recorded} (confirms the record; silences this entry):\n"
                            f"python install.py --update --container {recorded}\n"
                            f"# Or switch to {other}:\n"
                            f"python install.py --update --container {other}")
    else:
        command_to_apply = (f"# Reinstall {recorded}, then keep it:\n"
                            f"python install.py --update --container {recorded}\n"
                            f"# Or switch to {other} (re-records runtime.txt):\n"
                            f"python install.py --update --container {other}")
    return DeferralEntry(
        condition_id=CID_DATA_UNDER_BOTH,
        title="VCO containers/volumes exist under both podman and docker",
        detected=detected,
        why_deferred=why,
        command_to_apply=command_to_apply,
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
        choice = res.decline == "data_under_both"
        out(f"  [!] {res.detail}.")
        out("      Container setup is deferred (UPDATE_DEFERRED.md names "
            + ("the two choices" if choice else "what to start") + "); everything else continues.")
        args.no_containers = True
        args.containers_deferred = res.detail
        # R11 L2 (ii): this one waits for the user's choice, not for a runtime.
        args.containers_deferred_choice = choice
    return res


def containers_skipped_note(args: Any) -> str:
    """The end-of-run line for a run without containers — truthful about WHY."""
    deferred = getattr(args, "containers_deferred", "")
    if deferred and getattr(args, "containers_deferred_choice", False):
        return ("  NOTE: container setup was deferred: " + deferred + ".\n"
                "  UPDATE_DEFERRED.md names the two choices; once you run one of them the\n"
                "  stack comes up under the runtime you picked.")
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
    _which, _run = which or _tsd.which, run or _tsd.run
    other = _other(recorded)
    rec_status = _status(recorded, _which, _run)
    if rec_status != "down" and _status(other, _which, _run) == "usable":
        # R11 L2: the bind-mount arm decides "both" the way the reconcile does
        # (read-only rules: R12 M1's positive-evidence requirement applies).
        decision = _bind_decision(root, recorded, other, pinned_missing=rec_status == "missing",
                                  positive_only=True, env=None, run=_run)
        if decision is not None:
            if decision[0] == "unlistable":
                return None
            if decision[0] != VERDICT_BOTH:
                return False
            # R12 M7: one engine under two names is not "data under both".
            return runtimes_are_one_engine(recorded, other, run=_run) is not True
    if any(_status(rt, _which, _run) != "usable" for rt in _c.RUNTIME_CANDIDATES):
        return None
    here = vco_data_under(recorded, run=_run, install_root=root)
    there = vco_data_under(_other(recorded), run=_run, install_root=root,
                           corroborate_overrides=True)
    if here is None or there is None:
        return None
    if here and there and runtimes_are_one_engine(recorded, other, run=_run) is True:
        # R12 M7: one engine under two names is not "data under both".
        return False
    return bool(here and there)


# ---------------------------------------------------------------------------
# decide — the one JSON verdict (R12 convergence; see the module doc)
# ---------------------------------------------------------------------------


def decide(install_root: Optional[Path] = None, *, mode: str = "read-only",
           purpose: str = "infra", env: Optional[Mapping[str, str]] = None,
           which: Optional[WhichFn] = None, run: Optional[RunFn] = None,
           home: Optional[Path] = None) -> dict:
    """The ONE verdict every surface renders (schema and semantics in the
    module docstring's ``decide`` section; ``"schema": 1``).

    Same core as ``python -m vco_lib.containers resolve``: this calls
    :func:`vco_lib.containers.resolve` AND :func:`reconcile` and renders
    both — ``containers resolve`` is the same answer rendered for the
    session hooks' shell contract. ``mode="read-only"`` writes nothing and
    starts nothing; ``mode="install"`` is install.py's form. ``purpose``
    picks whether compose is probed (``"infra"``) or not (``"module"`` —
    the module plane drives single containers). Raises ``ValueError`` on an
    unknown ``mode`` / ``purpose``; every probe inside is time-bounded by
    the existing per-call timeouts."""
    if mode not in ("read-only", "install"):
        raise ValueError(f"unknown mode: {mode!r} (expected 'read-only' or 'install')")
    if purpose not in ("infra", "module"):
        raise ValueError(f"unknown purpose: {purpose!r} (expected 'infra' or 'module')")
    _env = os.environ if env is None else env
    if install_root is not None:
        root: Optional[Path] = Path(install_root)
    else:
        from vco_lib.python_exe import resolve_install_root  # noqa: PLC0415

        root = resolve_install_root()
    rec = reconcile(root, env=_env, which=which, run=run, rewrite=mode == "install")
    det = _c.resolve(env=_env, which=which, run=run, warn=lambda _m: None,
                     probe_compose=(purpose == "infra"), home=home, install_root=root)
    if det.requested_via == _c.PIN_VIA_ENV:
        requested_via = "env"
    elif (rec.pinned is not None and root is not None
          and read_confirmed(root) == rec.pinned):
        requested_via = "confirmed"
    elif det.requested_via == _c.PIN_VIA_RUNTIME_TXT:
        requested_via = "record"
    else:
        requested_via = "auto"
    resolved = det.state is _c.RuntimeState.RESOLVED
    return {
        "schema": 1,
        "state": det.state.value,
        "runtime": det.runtime,
        "compose": det.compose if purpose == "infra" else None,
        "compose_form": det.compose_form if purpose == "infra" else None,
        "binary_path": _tsd.which(det.runtime, env=_env) if det.runtime else None,
        "search_path": _tsd.reachable_path(env=_env) if det.runtime else None,
        "installed": det.installed,
        "requested": det.requested,
        "requested_via": requested_via,
        "requested_installed": det.requested_installed,
        "alternative_usable": det.alternative_usable,
        "record_reconciled": det.record_reconciled,
        "outcome": rec.outcome.value,
        "not_switched_key": rec.decline,
        "not_switched": rec.detail if rec.decline else None,
        "same_engine": rec.same_engine,
        "refused": not resolved,
        "refusal": None if resolved else det.reason,
        "reason": det.reason,
    }


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
    d = sub.add_parser(
        "decide",
        help="The ONE JSON verdict (R12 convergence): runtime to drive, compose form, pin "
             "source, refusal text. Exit 0 resolved, 3 refused, else internal error.")
    d.add_argument("--root", default=None,
                   help="Install root to decide for (default: the clone this vco_lib "
                        "belongs to).")
    d.add_argument("--json", action="store_true",
                   help="Print the schema-1 JSON verdict (the contract form).")
    d.add_argument("--mode", choices=["read-only", "install"], default="read-only",
                   help="read-only (default): write nothing, start nothing. install: the "
                        "record may be re-written and the ledger appended.")
    d.add_argument("--purpose", choices=["infra", "module"], default="infra",
                   help="infra (default): probe compose. module: the module plane's "
                        "single-container verdict, compose not probed.")
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
    if a.cmd == "decide":
        root = Path(a.root) if a.root else None
        try:
            verdict = decide(root, mode=a.mode, purpose=a.purpose)
        except ValueError as exc:
            print(f"decide: {exc}", file=sys.stderr)
            return 2
        if a.json:
            print(json.dumps(verdict, sort_keys=True))
        else:
            print(f"VCO_RUNTIME={shlex.quote(verdict['runtime'] or '')}")
            print(f"VCO_STATE={shlex.quote(verdict['state'])}")
            print(f"VCO_COMPOSE_FORM={shlex.quote(verdict['compose_form'] or '')}")
            print(f"VCO_REQUESTED_VIA={shlex.quote(verdict['requested_via'])}")
            print(f"VCO_REASON={shlex.quote(verdict['reason'])}")
        return 0 if verdict["state"] == "resolved" else 3
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
