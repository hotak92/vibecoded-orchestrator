# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Module-gated agent delivery: folder→project-id resolution + the gate.

Extracted from ``vco_lib/project_init.py`` (v0.2.96 WP-10). That module is
ratchet-capped and must SHRINK, not grow; the model-gateway delivery gate
landed there first and tripped the cap, which is the gate working — this
is the extraction it forced. Everything here is self-contained (reads the
launcher DB only; no project_init state). Production callers: the
``templates/agents/module-gateway/`` enumeration in ``_enumerate_bundle_files``
and its orphan carry-forward, ``project_init.resolve_active_modules`` (a thin
delegate of :func:`active_modules_verdict`), the launcher's Services page
(``python -m vco_lib.module_gated_delivery status --json``), and the
folder→UUID resolutions of the env projections — ONE home for each pattern,
per the one-concern-one-home rule.

v0.2.100 (AD-7, U20) — the gate had no reachable opener
--------------------------------------------------------
Until v0.2.100 the ten definitions shipped only to a project whose
launcher.db ``project_modules`` row for ``model_gateway`` was ``enabled=1``.
The ONLY writer of that row was a Services-page checkbox titled
"Model-routing guidance in project CLAUDE.md" — off by default, never naming
agents, and whose toggle re-rendered CLAUDE.md without running a bundle
update. The gateway itself is configured MACHINE-wide (a login registration
plus the VS Code panel's base URL), so on every machine, including ones whose
every chat went through the gateway, no project ever received a definition,
and nothing logged the skip.

The gate is now :func:`gateway_agents_gate`, a TRI-STATE verdict:

=====================================  ====================================
input                                  verdict
=====================================  ====================================
explicit ``enabled=1`` row             DELIVER  (signal ``project_row``)
explicit ``enabled=0`` row             SKIP     (signal ``project_row``)
no row (or no launcher.db at all, or   the MACHINE signal
the folder is not registered)          (:func:`vco_lib.gateway_ensure.
                                       machine_gateway_signal`): configured
                                       → DELIVER, not configured → SKIP,
                                       could not ask → UNKNOWN
launcher.db unreadable / locked /      UNKNOWN  (signal ``launcher_db``)
``projects`` or ``project_modules``
table absent
=====================================  ====================================

Why "not registered" and "no DB file" read the machine signal instead of
UNKNOWN: neither state CAN hold an explicit per-project row, so the question
"did the user opt this project out?" was asked and answered ("no opinion") —
that is not "could not ask". UNKNOWN is reserved for a database that exists
and could not be read, which is exactly the transient case (a lock during
"Update all") that used to delete delivered agents.

What each verdict does to the bundle (``project_init.install_project_bundle``):

* DELIVER ships the bucket; a retired definition no longer in the bucket is
  orphan-processed by the ordinary manifest logic (unmodified → deleted,
  modified → kept and retired).
* SKIP ships nothing, and previously delivered copies go through the same
  orphan logic — an explicit opt-out, or a machine that stopped routing
  through the gateway, does not keep ``claude-gw/*`` ids around. When the
  machine IS configured (so the skip is an explicit per-project opt-out),
  ``gated_delivery_skipped`` is recorded and a log line names the switch.
* UNKNOWN ships nothing new AND carries every prior manifest entry of the
  bucket forward verbatim (:func:`carries_forward`) — "could not ask" never
  deletes what an earlier run delivered — and records
  ``gated_delivery_unknown``.

v0.2.102: inside a DELIVER, each definition also passes a per-VENDOR leg —
it ships only when its vendor's key resolves (:func:`select_gateway_agents`;
a miss holds it back and carries an earlier copy forward, never deletes).

The CLAUDE.md routing section (``{{#if_module_active model_gateway}}``) stays
opt-in on the row alone: advice text is a per-project choice, the agent
definitions follow the machine.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

# v0.2.96 L-11: the ONE home for the read-only `file:` URI. Built by hand
# here (and at ten other sites) until this release, which failed shut on any
# launcher.db path containing `?`, `#` or `%` — SQLite reads everything after
# the first `?` as the query string, so the path truncated and the gate
# silently never fired for that user.
from vco_lib.launcher_db_reader import sqlite_ro_uri

#: The module name shared by the GUI toggle, the CLAUDE.md template gate
#: (``{{#if_module_active model_gateway}}``) and this delivery gate. MUST
#: stay equal to the GUI's ``ROUTING_GUIDANCE_MODULE``
#: (``launcher/src/lib/api/model_gateway.ts``) — one ``project_modules``
#: row, three effects. Pinned by
#: ``tests/test_v0292_model_gateway_gui_contract.py``.
GATEWAY_MODULE_NAME = "model_gateway"

#: Source dir for model-gateway-gated agent definitions. NEVER ``free/`` —
#: that bucket ships UNCONDITIONALLY (see ``_enumerate_bundle_files``) and
#: would put hardcoded ``claude-gw/*`` frontmatter model ids on stock
#: installs whose machine has no gateway.
GATEWAY_AGENTS_DIR = "module-gateway"

#: The manifest ``source`` prefix of every file the gated bucket delivered —
#: what :func:`carries_forward` keys on. Separator-normalised before use
#: (Windows manifests record ``templates\\agents\\...``).
GATEWAY_AGENTS_SOURCE_PREFIX = f"templates/agents/{GATEWAY_AGENTS_DIR}/"

#: The DEFAULT-ON module SET (v0.2.96 ship-gate F-N5), and it is a SET, not a
#: policy: a module NOT named here and with no ``project_modules`` row is
#: INACTIVE. Moved here from ``project_init`` in v0.2.100 with the resolver
#: that reads it (``project_init`` re-exports it as ``_DEFAULT_ACTIVE_MODULES``).
DEFAULT_ACTIVE_MODULES: frozenset[str] = frozenset({"diagrams"})

#: Deferral condition ids this module emits and resolves (registry:
#: ``vco_lib/deferral_conditions.toml``, ``clear_probe = "paired-resolution"``
#: — :func:`record_gate_outcomes` is the pairing).
CID_SKIPPED = "gated_delivery_skipped"
CID_UNKNOWN = "gated_delivery_unknown"


def _launcher_db_path() -> Path:
    """Canonical launcher.db path (thin alias, same shape as project_init's)."""
    from vco_lib.paths import launcher_db_path as _canonical
    return _canonical()


def _canonical_path_eq(a: "str | Path", b: "str | Path") -> bool:
    """Delegate to project_init's canonical comparator (its single home)."""
    from vco_lib.project_init import _canonical_path_eq as _impl
    return _impl(a, b)


def resolve_project_id_for_folder(
    folder: Path,
    *,
    db_path: Path | None = None,
) -> Optional[str]:
    """Resolve a project FOLDER to its ``projects``-table id, read-only.

    Soft-fails to ``None`` on every miss (no DB, unreadable, table absent,
    folder not registered) — callers treat ``None`` as "cannot prove
    anything about this folder", never as an error. A caller that must tell
    "not registered" from "could not ask" uses :func:`_resolve_folder`.
    """
    kind, project_id = _resolve_folder(folder, db_path=db_path)
    return project_id if kind == _FOLDER_REGISTERED else None


def project_id_for_folder_on_conn(
    conn: sqlite3.Connection, folder_canonical: Path,
) -> Optional[str]:
    """The folder->id match itself, on a connection the caller owns.

    Unlike :func:`resolve_project_id_for_folder` this does NOT soft-fail: a
    query error propagates as :class:`sqlite3.Error`, so a caller that must
    tell "not registered" (``None``) from "could not ask" (the exception)
    can — ``vco_lib.parked_hooks`` is one. ``folder_canonical`` must already
    be resolved.
    """
    rows = conn.execute("SELECT id, folder_path FROM projects").fetchall()
    for row_id, row_folder in rows:
        if _canonical_path_eq(row_folder or "", folder_canonical):
            return str(row_id)
    return None


# ---------------------------------------------------------------------------
# Tri-state reads of launcher.db
# ---------------------------------------------------------------------------

_FOLDER_NO_DB = "no_db"
_FOLDER_REGISTERED = "registered"
_FOLDER_UNREGISTERED = "unregistered"
_FOLDER_UNREADABLE = "unreadable"


def _resolve_folder(
    folder: Path, *, db_path: Path | None = None,
) -> tuple[str, Optional[str]]:
    """``(kind, project_id)`` — kind is one of the ``_FOLDER_*`` values.

    ``no_db`` (no launcher.db file: a CLI-only install) and ``unregistered``
    are ANSWERS; ``unreadable`` (open/query failed, ``projects`` absent, the
    folder cannot be resolved) is "could not ask".
    """
    target = db_path if db_path is not None else _launcher_db_path()
    try:
        if not target.is_file():
            return _FOLDER_NO_DB, None
    except OSError:
        return _FOLDER_UNREADABLE, None
    try:
        folder_canonical = folder.resolve()
    except (OSError, RuntimeError):
        return _FOLDER_UNREADABLE, None
    try:
        conn = sqlite3.connect(sqlite_ro_uri(target), uri=True, timeout=2.0)
    except sqlite3.Error:
        return _FOLDER_UNREADABLE, None
    try:
        project_id = project_id_for_folder_on_conn(conn, folder_canonical)
    except sqlite3.Error:
        return _FOLDER_UNREADABLE, None
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if project_id is None:
        return _FOLDER_UNREGISTERED, None
    return _FOLDER_REGISTERED, project_id


#: :attr:`ModulesVerdict.source` values.
MODULES_FROM_DB = "db"
MODULES_DEFAULT = "default"
MODULES_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModulesVerdict:
    """The active modules of one project, and whether the DB could be asked.

    ``source``: ``db`` (rows read; possibly none), ``default`` (no launcher.db
    file — "no launcher has ever expressed an opinion", the default-on set is
    the resolved answer), ``unknown`` (the file exists and could not be read,
    or has no ``project_modules`` table; ``active`` is then the default-on
    set, which callers that DELETE on the answer must not act on).
    """

    active: frozenset[str]
    source: str
    #: ``module_name -> enabled`` for every explicit row of this project.
    explicit: Mapping[str, bool] = field(default_factory=dict)


def active_modules_verdict(
    project_id: str, *, db_path: Path | None = None,
) -> ModulesVerdict:
    """Tri-state read of ``project_modules`` for ``project_id``. Read-only.

    Opened through the read-only URI (v0.2.100: the resolver used to open
    launcher.db READ-WRITE and collapse every error into the default set —
    the same "could not ask == no" defect the delivery gate had).
    """
    defaults = frozenset(DEFAULT_ACTIVE_MODULES)
    target = db_path if db_path is not None else _launcher_db_path()
    try:
        present = target.is_file()
    except OSError:
        return ModulesVerdict(defaults, MODULES_UNKNOWN)
    if not present:
        return ModulesVerdict(defaults, MODULES_DEFAULT)
    try:
        conn = sqlite3.connect(sqlite_ro_uri(target), uri=True, timeout=2.0)
    except sqlite3.Error:
        return ModulesVerdict(defaults, MODULES_UNKNOWN)
    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='project_modules'"
        ).fetchone() is not None
        if not has_table:
            return ModulesVerdict(defaults, MODULES_UNKNOWN)
        rows = conn.execute(
            "SELECT module_name, enabled FROM project_modules "
            "WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    except sqlite3.Error:
        return ModulesVerdict(defaults, MODULES_UNKNOWN)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    # Start with defaults, then apply explicit rows: enabled=0 REMOVES a
    # default-on module, enabled=1 adds (or keeps) one.
    active = set(defaults)
    explicit: dict[str, bool] = {}
    for module_name, enabled in rows:
        if not isinstance(module_name, str):
            continue
        explicit[module_name] = bool(enabled)
        if enabled:
            active.add(module_name)
        else:
            active.discard(module_name)
    return ModulesVerdict(frozenset(active), MODULES_FROM_DB, explicit)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


class GateState(str, Enum):
    DELIVER = "deliver"
    SKIP = "skip"
    UNKNOWN = "unknown"


#: :attr:`GateVerdict.signal` values — which input decided.
SIGNAL_PROJECT_ROW = "project_row"
SIGNAL_MACHINE = "machine"
SIGNAL_LAUNCHER_DB = "launcher_db"
#: The per-VENDOR leg (v0.2.102, :func:`select_gateway_agents`): one
#: definition held back because its vendor's key did not resolve.
SIGNAL_VENDOR_KEY = "vendor_key"


@dataclass(frozen=True)
class GateVerdict:
    """One gate decision: what happens, which input decided it, and why."""

    state: GateState
    signal: str
    reason: str
    #: The machine signal's ``configured`` when it was consulted (``None``
    #: when it was not, or could not answer).
    machine_configured: Optional[bool] = None

    @property
    def delivers(self) -> bool:
        return self.state is GateState.DELIVER

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "signal": self.signal,
            "reason": self.reason,
            "machine_configured": self.machine_configured,
        }


MachineSignalFn = Callable[[], Any]


def _default_machine_signal() -> Any:
    from vco_lib.gateway_ensure import machine_gateway_signal
    return machine_gateway_signal()


def gateway_agents_gate(
    folder: Path,
    *,
    db_path: Path | None = None,
    machine_signal: Optional[MachineSignalFn] = None,
) -> GateVerdict:
    """Decide the delivery of ``templates/agents/module-gateway/`` to ``folder``.

    See the module docstring for the table. ``machine_signal`` is a
    zero-argument callable returning an object with ``configured`` and
    ``reason`` (:class:`vco_lib.gateway_ensure.MachineGatewaySignal`); it is
    called at most once, and only when an input needs it.
    """
    read_signal = machine_signal or _default_machine_signal
    kind, project_id = _resolve_folder(folder, db_path=db_path)
    if kind == _FOLDER_UNREADABLE:
        return GateVerdict(
            GateState.UNKNOWN, SIGNAL_LAUNCHER_DB,
            "launcher.db exists but could not be read (locked, damaged, or "
            "without a projects table) — no per-project choice could be read",
        )
    explicit: Optional[bool] = None
    if kind == _FOLDER_REGISTERED and project_id is not None:
        modules = active_modules_verdict(project_id, db_path=db_path)
        if modules.source == MODULES_UNKNOWN:
            return GateVerdict(
                GateState.UNKNOWN, SIGNAL_LAUNCHER_DB,
                "launcher.db's project_modules could not be read for this "
                "project — its per-project choice is unknown",
            )
        explicit = modules.explicit.get(GATEWAY_MODULE_NAME)

    if explicit is True:
        return GateVerdict(
            GateState.DELIVER, SIGNAL_PROJECT_ROW,
            f"this project's {GATEWAY_MODULE_NAME!r} module is switched on",
        )

    try:
        signal = read_signal()
        configured = getattr(signal, "configured", None)
        signal_reason = str(getattr(signal, "reason", "") or "")
    except Exception as exc:  # noqa: BLE001 — a gate answers, it never raises
        configured, signal_reason = None, f"machine signal failed: {exc}"

    if explicit is False:
        return GateVerdict(
            GateState.SKIP, SIGNAL_PROJECT_ROW,
            f"this project's {GATEWAY_MODULE_NAME!r} module is switched OFF "
            "(Services → Model gateway, per-project toggle)",
            machine_configured=configured,
        )
    if configured is None:
        return GateVerdict(
            GateState.UNKNOWN, SIGNAL_MACHINE,
            f"could not tell whether this machine routes through the gateway: "
            f"{signal_reason}",
        )
    return GateVerdict(
        GateState.DELIVER if configured else GateState.SKIP,
        SIGNAL_MACHINE, signal_reason, machine_configured=configured,
    )


# ---------------------------------------------------------------------------
# The per-vendor leg (v0.2.102)
# ---------------------------------------------------------------------------
#
# The module gate above answers "does this project get gateway agents at
# all". It cannot answer "can this machine reach the model a definition
# names": a machine with only a z.ai key used to receive the qwen-vendor and
# kimi definitions too, every dispatch of which fails on a missing key. The
# model-selection grid already renders a vendor's rows only when its key
# resolves (``vco_lib.model_selection``); this leg applies the SAME probe
# (:func:`vco_lib.model_selection.vendor_key_probe`) to the definitions.
#
# =====================================  ====================================
# vendor key at bundle time              effect on that definition
# =====================================  ====================================
# resolves                               delivered (ordinary manifest logic)
# does not resolve / probe failed /      NOT added; a copy delivered by an
# gateway package not importable         earlier run is CARRIED FORWARD
# =====================================  ====================================
#
# Why a miss never deletes: the gateway's resolver folds "hub unreachable"
# into the same miss as "no such key" (an orchestrator update stops vct-hub
# by design, and the OS-keychain tier is reachable only through it), so a
# miss is not a positively confirmed absence. Removal stays with the module
# gate (a project switched off) and the retired-definition orphan logic.
# A held definition is a log line, never a deferral: a user without a kimi
# key has nothing to fix.

#: What a vendor probe answers: True = that vendor's key resolves.
VendorProbe = Callable[[str], bool]


def _default_vendor_probe() -> VendorProbe:
    """The gateway's own key resolution, through the grid's ONE probe."""
    from vco_lib.model_selection import vendor_key_probe

    return vendor_key_probe()


def definition_vendor(path: Path) -> Optional[str]:
    """The gateway vendor id a definition's ``model:`` routes to, or None.

    None for a definition that names no ``claude-gw/`` id (nothing to probe).
    Raises ``ImportError`` when the gateway package is not importable — the
    caller decides what "cannot tell" means.
    """
    from vco_lib.vscode_settings import GATEWAY_ID_PREFIX

    model = _frontmatter_model(path)
    if not model or not model.startswith(GATEWAY_ID_PREFIX):
        return None
    from model_router.routing import (  # pyright: ignore[reportMissingImports]
        split_namespace,
    )

    vendor, _rest = split_namespace(model)
    return vendor.vendor_id if vendor is not None else None


def vendor_reachability(
    vendor_ids: Iterable[str],
    *,
    vendor_probe: Optional[VendorProbe] = None,
) -> dict[str, tuple[bool, str]]:
    """``{vendor_id: (key_resolves, reason)}`` — one probe per vendor.

    Soft-fail: a probe that cannot be built or raises answers ``False`` for
    the vendor(s) concerned, with the exception TYPE in the reason (never its
    message — a resolver message must not be able to carry a value).
    """
    wanted = sorted(set(vendor_ids))
    if not wanted:
        return {}
    probe = vendor_probe
    if probe is None:
        try:
            probe = _default_vendor_probe()
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            reason = f"model gateway not importable ({type(exc).__name__})"
            return {v: (False, reason) for v in wanted}
    out: dict[str, tuple[bool, str]] = {}
    for vendor_id in wanted:
        try:
            ok = bool(probe(vendor_id))
            out[vendor_id] = (ok, "key resolves" if ok else
                              f"no {vendor_id!r} vendor key resolved")
        except Exception as exc:  # noqa: BLE001 — one vendor, one answer
            out[vendor_id] = (False, f"key resolution for {vendor_id!r} failed "
                                     f"({type(exc).__name__})")
    return out


def select_gateway_agents(
    src_dir: Path,
    verdict: GateVerdict,
    *,
    vendor_probe: Optional[VendorProbe] = None,
) -> tuple[list[Path], list[GateOutcome]]:
    """The gated definitions to deliver, and the held ones' carry-forward
    outcomes (see the table above).

    Only consulted when the module gate DELIVERs — a SKIP or UNKNOWN verdict
    already decided the whole bucket, and no key is probed for it. Each held
    definition yields one ``(source path, UNKNOWN verdict with
    SIGNAL_VENDOR_KEY)`` outcome, so :func:`carries_forward` keeps an earlier
    delivery of exactly that file and :func:`record_gate_outcomes` logs it
    without a deferral.
    """
    if not verdict.delivers:
        return [], []
    files = sorted(Path(src_dir).glob("*.md"))
    try:
        vendor_of = {f: definition_vendor(f) for f in files}
    except ImportError as exc:
        reason = f"model gateway not importable ({type(exc).__name__})"
        return [], [_held(f, "?", reason) for f in files]
    reach = vendor_reachability(
        (v for v in vendor_of.values() if v is not None),
        vendor_probe=vendor_probe,
    )
    deliver: list[Path] = []
    held: list[GateOutcome] = []
    for f in files:
        vendor_id = vendor_of[f]
        if vendor_id is None or reach[vendor_id][0]:
            deliver.append(f)
        else:
            held.append(_held(f, vendor_id, reach[vendor_id][1]))
    return deliver, held


def _held(path: Path, vendor_id: str, reason: str) -> GateOutcome:
    return (
        f"{GATEWAY_AGENTS_SOURCE_PREFIX}{path.name}",
        GateVerdict(
            GateState.UNKNOWN, SIGNAL_VENDOR_KEY,
            f"{path.stem} held back (vendor {vendor_id!r}): {reason}; a copy "
            "delivered earlier is kept",
        ),
    )


def module_gateway_agents_active(target_folder: Path) -> bool:
    """True iff :func:`gateway_agents_gate` says DELIVER for ``target_folder``.

    Boolean view for callers that only need "would it ship now"; SKIP and
    UNKNOWN both read ``False``. The bundle engine does NOT use this — it
    needs the UNKNOWN leg to carry delivered files forward.
    """
    return gateway_agents_gate(target_folder).delivers


# ---------------------------------------------------------------------------
# Bundle-engine hooks: carry-forward and the visible record
# ---------------------------------------------------------------------------

#: One gated bucket's outcome in a bundle run: ``(source_prefix, verdict)``.
GateOutcome = tuple[str, GateVerdict]


def carries_forward(
    prior_entry: Mapping[str, Any] | None,
    outcomes: Iterable[GateOutcome],
) -> bool:
    """True when a prior manifest entry belongs to a bucket whose gate was UNKNOWN.

    The bundle engine's orphan loop calls this before any orphan handling:
    a True answer means "keep the entry verbatim, do not touch the file" —
    the pattern of the ``--skip-kind`` carry-forward, extended to "could not
    ask".
    """
    source = str((prior_entry or {}).get("source", "") or "").replace("\\", "/")
    if not source:
        return False
    return any(
        verdict.state is GateState.UNKNOWN and source.startswith(prefix)
        for prefix, verdict in outcomes
    )


def _entry_for(verdict: GateVerdict, folder: Path) -> Any:
    from vco_lib.deferral_report import DeferralEntry

    if verdict.state is GateState.UNKNOWN:
        return DeferralEntry(
            condition_id=CID_UNKNOWN,
            title="Gateway agent delivery could not be decided",
            detected=(
                "The bundle update could not decide whether this project gets "
                "the model-gateway agent definitions: " + verdict.reason + ". "
                "Definitions delivered by an earlier update were kept as they "
                "were; nothing was added or removed."
            ),
            why_deferred=(
                "Deleting delivered agents on a read that failed would remove "
                "working definitions over a transient condition (a database "
                "lock during \"Update all\"), so the previous state is carried "
                "forward and the decision is retried by the next update."
            ),
            command_to_apply=(
                "Re-run the bundle update once the launcher is idle: "
                f"python -m vco_lib.project_init install-bundle --folder "
                f"{folder} --update"
            ),
            severity="warning",
        )
    return DeferralEntry(
        condition_id=CID_SKIPPED,
        title="Gateway agent definitions switched off for this project",
        detected=(
            "This machine routes its panel through the model gateway, but the "
            "gateway agent definitions were not delivered to this project: "
            + verdict.reason + "."
        ),
        why_deferred=(
            "A per-project opt-out is the user's choice; it is recorded so the "
            "skip is visible rather than silent."
        ),
        command_to_apply=(
            "To deliver them, switch the project on under Services → Model "
            "gateway in the launcher (the toggle runs the bundle update)."
        ),
        severity="info",
    )


def record_gate_outcomes(
    folder: Path,
    outcomes: Sequence[GateOutcome],
    *,
    dry_run: bool = False,
    log: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """Make every gated skip visible, and clear what no longer applies.

    Per verdict: UNKNOWN → ``gated_delivery_unknown``; SKIP on a machine whose
    gateway IS configured → ``gated_delivery_skipped``; anything else → both
    resolved; a per-vendor HELD outcome (``SIGNAL_VENDOR_KEY``) → a log line
    only. Returns the condition ids emitted. Soft-fail throughout
    (deferral I/O is best-effort and never aborts a bundle run); ``dry_run``
    writes nothing.
    """
    emit: list[Any] = []
    log_lines: list[str] = []
    for prefix, verdict in outcomes:
        if verdict.signal == SIGNAL_VENDOR_KEY:
            # The per-vendor leg: a log line only — a user without that
            # vendor's key has nothing to fix (see select_gateway_agents).
            log_lines.append(f"gated definition {prefix}: HELD — {verdict.reason}")
            continue
        if verdict.state is GateState.UNKNOWN:
            emit.append(_entry_for(verdict, folder))
            log_lines.append(
                f"gated bucket {prefix}: UNKNOWN — {verdict.reason}; prior "
                "deliveries carried forward"
            )
        elif verdict.state is GateState.SKIP:
            if verdict.machine_configured:
                emit.append(_entry_for(verdict, folder))
            log_lines.append(f"gated bucket {prefix}: SKIP — {verdict.reason}")
        else:
            log_lines.append(f"gated bucket {prefix}: DELIVER — {verdict.reason}")
    if log is not None:
        for line in log_lines:
            try:
                log(line)
            except Exception:  # noqa: BLE001 — logging never aborts a bundle
                pass
    emitted = [e.condition_id for e in emit]
    if dry_run:
        return emitted
    from vco_lib import deferral_emit as _de
    from vco_lib.deferral_report import DeferralReport

    if emit:
        _de.emit_entries(folder, emit, keep_first_detected=True)
    try:
        on_disk = DeferralReport.read(folder)
        stale = [
            c for c in (CID_SKIPPED, CID_UNKNOWN)
            if c not in emitted and on_disk.has_condition(c)
        ]
    except Exception:  # noqa: BLE001 — an unreadable ledger has nothing to clear
        stale = []
    if stale:
        # Only when present: an unconditional resolve would rewrite the
        # ledger on every bundle run of every project.
        _de.resolve_conditions(folder, stale)
    return emitted


# ---------------------------------------------------------------------------
# Agent-definition model ids vs the gateway registry (F-W1-11a)
# ---------------------------------------------------------------------------


def _frontmatter_model(path: Path) -> Optional[str]:
    """The ``model:`` value of an agent definition's YAML frontmatter, or None."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "model":
            return value.strip().strip("'\"") or None
    return None


def check_agent_model_ids(dirs: Iterable[Path]) -> list[dict[str, Any]]:
    """Every ``*.md`` definition in ``dirs`` whose ``model:`` names a gateway
    id the registry does not know, with the closest valid ids.

    Only ``claude-gw/``-namespaced ids are checked: a first-party id or an
    alias (``opus``) is the client's to validate. The check is the router's
    own (:func:`model_router.routing.validate_model_id`), so a definition
    that passes here is one the gateway will route. Hand-written definitions
    (a project's ``.claude/agents`` or the user's ``~/.claude/agents``) are
    what this exists for: the shipped set is pinned by a contract test, and
    nothing else looked at the rest until a mistyped id became a chat
    failure.
    """
    from model_router.routing import (  # pyright: ignore[reportMissingImports]
        validate_model_id,
    )
    from vco_lib.vscode_settings import GATEWAY_ID_PREFIX

    problems: list[dict[str, Any]] = []
    for directory in dirs:
        try:
            files = sorted(Path(directory).glob("*.md"))
        except OSError:
            continue
        for path in files:
            model = _frontmatter_model(path)
            if not model or not model.startswith(GATEWAY_ID_PREFIX):
                continue
            ok, reason, suggestions = validate_model_id(model)
            if not ok:
                problems.append({
                    "path": str(path),
                    "model": model,
                    "reason": reason,
                    "suggestions": list(suggestions),
                })
    return problems


def agent_definition_dirs(folder: Optional[Path]) -> list[Path]:
    """The directories Claude Code reads definitions from for ``folder``."""
    from vco_lib.paths import claude_user_dir

    dirs = [claude_user_dir() / "agents"]
    if folder is not None:
        dirs.insert(0, folder / ".claude" / "agents")
    return dirs


# ---------------------------------------------------------------------------
# CLI — the launcher's call surface (rule A: TS/Rust call this, never mirror)
# ---------------------------------------------------------------------------


def status_payload(
    folders: Optional[Sequence[Path | str]] = None,
) -> dict[str, Any]:
    """``{"machine_signal", "gate", "agent_id_problems", "vendor_keys",
    "folders"?}``. Reads only. ``vendor_keys`` is :func:`vendor_keys_payload`.

    ``agent_id_problems`` lists definitions (the projects' and the user's)
    naming a gateway model id the router does not know — ``None`` when the
    gateway package is not importable (no registry to check against).

    ``folders`` (any number; ONE interpreter start answers for all of them —
    the machine signal is per-machine, only the per-project row varies, so
    the Services page asks once, not once per project): the ``folders`` map
    carries, per folder, the gate verdict plus ``claude_md_section.renders``
    — whether that project's CLAUDE.md model-routing section renders,
    computed by the render's own mapping
    (``vco_lib.claude_md_sections.gateway_section_renders``), so a GUI
    consumer never re-derives it. The map is keyed by the EXACT string the
    caller passed (a ``Path`` element is keyed by ``str(path)``):
    ``str(Path(...))`` normalises — drops a trailing slash, resolves ``.`` —
    and a launcher lookup keyed by the raw ``folder_path`` would otherwise
    miss its own verdict. With exactly ONE folder the same verdict also
    appears at the top level (``gate`` / ``claude_md_section``), the shape
    the launcher's agents-gate card reads.
    """
    from vco_lib.claude_md_sections import gateway_section_renders
    from vco_lib.gateway_ensure import machine_gateway_signal

    signal = machine_gateway_signal()
    # (exact key, folder) pairs: the key echoes the caller's string, the
    # Path is what the gate and the definition scan resolve.
    entries: list[tuple[str, Path]] = [
        (folder if isinstance(folder, str) else str(folder), Path(folder))
        for folder in (folders if folders is not None else [])
    ]

    def verdict(folder: Path) -> dict[str, Any]:
        gate_verdict = gateway_agents_gate(
            folder, machine_signal=lambda: signal)
        return {
            "gate": gate_verdict.to_dict(),
            # What the CLAUDE.md render does with the same verdict — the
            # render's own mapping, so the Services page shows what the
            # file actually does instead of re-deriving it in Rust/TS.
            "claude_md_section": {
                "renders": gateway_section_renders(gate_verdict),
            },
        }

    dirs: list[Path] = []
    for _key, folder in entries:
        dirs.extend(agent_definition_dirs(folder))
    if not dirs:
        dirs = agent_definition_dirs(None)
    # N-2: every folder's dirs include the USER agents dir, so an N-folder
    # call would scan it N times and report each of its problems N times.
    # Path equality is by parsed parts, so this also folds non-canonical
    # duplicates of the same directory.
    dirs = list(dict.fromkeys(dirs))
    try:
        problems: Optional[list[dict[str, Any]]] = check_agent_model_ids(dirs)
    except ImportError:
        problems = None

    by_folder = {key: verdict(folder) for key, folder in entries}
    payload: dict[str, Any] = {
        "machine_signal": signal.to_dict(),
        "gate": None,
        "agent_id_problems": problems,
        "vendor_keys": vendor_keys_payload(),
    }
    if len(entries) == 1:
        only = by_folder[entries[0][0]]
        payload["gate"] = only["gate"]
        payload["claude_md_section"] = only["claude_md_section"]
    if by_folder:
        payload["folders"] = by_folder
    return payload


#: The shipped gated definitions, beside this package in the checkout (the
#: orchestrator runs from its own clone, ``pip install -e``).
SHIPPED_GATEWAY_AGENTS = (
    Path(__file__).resolve().parent.parent / "templates" / "agents"
    / GATEWAY_AGENTS_DIR
)


def vendor_keys_payload(
    src_dir: Optional[Path] = None,
    *,
    vendor_probe: Optional[VendorProbe] = None,
) -> Optional[dict[str, Any]]:
    """Per vendor named by the shipped definitions: does its key resolve, and
    which definitions does that hold back — the per-vendor leg as the
    Services card shows it. ``None`` when the gateway package is not
    importable (no vendor can be named). Reads only; values never leave the
    probe (booleans and key-free reasons only)."""
    files = sorted(Path(src_dir or SHIPPED_GATEWAY_AGENTS).glob("*.md"))
    try:
        vendor_of = {f: definition_vendor(f) for f in files}
    except ImportError:
        return None
    reach = vendor_reachability(
        (v for v in vendor_of.values() if v is not None),
        vendor_probe=vendor_probe,
    )
    return {
        vendor_id: {
            "resolves": ok,
            "reason": reason,
            "definitions": sorted(f.stem for f, v in vendor_of.items()
                                  if v == vendor_id),
        }
        for vendor_id, (ok, reason) in reach.items()
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m vco_lib.module_gated_delivery",
        description=(
            "Report the model-gateway agent delivery gate (reads only). The "
            "ONE home for \"is the gateway configured on this machine\"."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="Print the machine signal and, with "
                       "--folder (repeatable), those projects' gate "
                       "verdicts — one call answers for every folder.")
    s.add_argument("--folder", action="append", default=None, metavar="DIR",
                   help="Project folder to answer for; repeat for several.")
    s.add_argument("--json", action="store_true",
                   help="Print one JSON object on stdout.")
    c = sub.add_parser("check-agent-ids", help="List agent definitions whose "
                       "gateway model id the router does not know. Exit 3 "
                       "when any does.")
    c.add_argument("--folder", default=None, metavar="DIR",
                   help="Project whose .claude/agents is checked too.")
    c.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    # `status` takes a REPEATABLE --folder (one call answers for every
    # project); `check-agent-ids` keeps its single optional --folder.
    if args.cmd == "check-agent-ids":
        folder = Path(args.folder) if args.folder else None
        problems = check_agent_model_ids(agent_definition_dirs(folder))
        if args.json:
            print(json.dumps({"agent_id_problems": problems}, sort_keys=True))
        for pr in problems:
            hint = (" — did you mean " + " or ".join(pr["suggestions"]) + "?"
                    if pr["suggestions"] else "")
            print(f"[vct] {pr['path']}: model {pr['model']!r} is not a gateway "
                  f"id{hint}", file=sys.stderr if args.json else sys.stdout)
        return 3 if problems else 0
    # Raw strings, not Paths: the payload must echo the caller's exact
    # folder strings back as keys (N-3).
    folders = list(args.folder) if args.folder else None
    payload = status_payload(folders)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        ms = payload["machine_signal"]
        print(f"machine: configured={ms['configured']} — {ms['reason']}")
        if payload["gate"] is not None:
            g = payload["gate"]
            print(f"gate: {g['state']} ({g['signal']}) — {g['reason']}")
        # Single folder: its verdict already printed above as "gate:".
        if payload["gate"] is None:
            for path, entry in (payload.get("folders") or {}).items():
                g = entry["gate"]
                print(f"gate [{path}]: {g['state']} ({g['signal']}) — {g['reason']}")
        for vendor_id, vk in sorted((payload.get("vendor_keys") or {}).items()):
            state = "delivered" if vk["resolves"] else "held back"
            print(f"vendor {vendor_id}: {state} ({vk['reason']}) — "
                  + ", ".join(vk["definitions"]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
