# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``{{MODEL_SELECTION_GRID}}`` renderer — which model for which task.

The GRID DATA lives in ``vco_lib/model_selection.toml`` (rows, scores, ``*``
flags, reach-via agents, providers, per-model notes, model-independent
general notes). This module decides
which PROVIDERS are reachable at render time and renders only their rows:

* Anthropic/Claude rows always render.
* A vendor row (``zai``, ``qwen``, ``kimi``) renders only when the model gateway can
  resolve that vendor's key AT RENDER TIME. Renders happen at install/update AND
  at every automatic guidance refresh (``vco_lib.model_guidance``: launcher
  startup and every Secrets-panel key change), so the section follows the keys
  the user actually holds.

Provider reachability REUSES the model gateway's own key resolution —
``model_router.secrets.VendorKeyResolver`` over ``model_router.vendors.VENDORS``
(key NAMES from the vendor rows, values through ``vco_lib.agent_secrets.get``
with the gateway's own scope rules). There is no third key lookup here, and
no key VALUE ever reaches a message, a log or the rendered markdown: only
booleans leave the probe.

The probe is TRI-STATE (v0.2.103, plan item 12): :class:`KeyState` separates
"key present" from "key absent" from "the store could not be asked"
(keychain busy/locked, vct-hub restarting — the gateway's own
``KeyResult.transient``). The field defect this closes: a mid-update render
probed while the store was momentarily down and read "unavailable" as "no
key", silently dropping the provider's rows. An UNAVAILABLE answer is
retried once, then falls back to the last-known provider record
(:func:`state_record_path`) — never dropped on a transient failure. The
record is rewritten only by a DEFINITIVE round (no vendor unavailable) whose
answer differs from it.

Soft-fail contract (the value feeds the materializer, which must never crash
a render): when the gateway package is not installed/active, or a resolution
raises, the vendor's rows fall back exactly as an UNAVAILABLE probe does and
ONE stderr line says why. A missing/unreadable/inconsistent data table, by
contrast, is a broken install and raises :class:`GridError` (the
materializer turns that into an unresolved placeholder plus a deferral row,
the ordinary owner-rule path).
"""

from __future__ import annotations

import json
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

__all__ = [
    "ALWAYS_PROVIDERS",
    "Grid",
    "GridError",
    "GridNote",
    "GridRow",
    "GRID_PATH",
    "KeyState",
    "load_grid",
    "load_record",
    "reachable_providers",
    "recorded_providers",
    "render_grid",
    "save_record",
    "set_provider_override",
    "state_record_path",
    "vendor_key_probe",
    "vendor_key_state_probe",
]

#: The .toml sits next to this loader inside the ``vco_lib`` package so it
#: ships in the wheel automatically (same shape as ``deferral_conditions.toml``).
GRID_PATH: Path = Path(__file__).resolve().parent / "model_selection.toml"

#: Providers whose rows render unconditionally — first-party Claude needs no
#: vct-secrets key (OAuth passthrough), so it is never probed.
ALWAYS_PROVIDERS: FrozenSet[str] = frozenset({"anthropic"})


class KeyState(str, Enum):
    """The tri-state answer of one vendor's key probe (v0.2.103).

    ``UNAVAILABLE`` is NOT a miss: the store could not be asked (the OS
    keychain was busy or locked, vct-hub was restarting), so nothing about
    the key's existence is known. The renderer must never treat it as
    "no key" — that conflation is the field defect plan item 12 fixes.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


#: What a key probe answers per vendor id: True = key resolved (rows render).
KeyProbe = Callable[[str], bool]

#: The tri-state probe shape.
KeyStateProbe = Callable[[str], KeyState]

#: Pause between the first UNAVAILABLE answer and the one retry. Sits just
#: above the gateway resolver's own transient cache (``TRANSIENT_RETRY_S`` in
#: ``model_router.secrets``), so the retry is a real re-ask, not a cache hit.
_TRANSIENT_RETRY_SLEEP_S = 2.5


class GridError(RuntimeError):
    """The data table is missing, unreadable or inconsistent — a broken
    install, never silently papered over with a hard-coded grid."""


@dataclass(frozen=True)
class GridRow:
    id: str
    model: str
    reach_via: str
    providers: Tuple[str, ...]
    scores: Tuple[str, ...]
    #: The ``†`` marker: this model has only xhigh/max benchmark results
    #: (no medium/high data) — weaker evidence. Rendered after the name.
    dagger: bool = False


@dataclass(frozen=True)
class GridNote:
    models: Tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Grid:
    columns: Tuple[str, ...]
    intro: Tuple[str, ...]
    column_legend: str
    rows: Tuple[GridRow, ...]
    notes: Tuple[GridNote, ...]
    #: Caveats not tied to one model (the grid's evidence-quality and
    #: maintenance notes). Rendered whenever the grid renders.
    general_notes: Tuple[str, ...]


def load_grid(path: Optional[Path] = None) -> Grid:
    """Parse and validate the data table. Raises :class:`GridError` naming
    the path on any defect; consistency checks (one score per column, unique
    row ids, notes referencing real rows) are what keep a later edit of the
    .toml from rendering a silently misaligned table."""
    table = Path(path) if path is not None else GRID_PATH
    try:
        data = tomllib.loads(table.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GridError(f"model-selection grid unreadable at {table}: {exc}") from exc
    columns = tuple(data.get("columns", ()))
    intro = tuple(data.get("intro", ()))
    legend = str(data.get("column_legend", ""))
    general_notes = tuple(str(n).strip() for n in data.get("general_notes", ()))
    if not columns or not intro or not legend or not general_notes:
        raise GridError(
            f"model-selection grid incomplete at {table}: columns, intro, "
            "column_legend and general_notes are all required"
        )
    rows: List[GridRow] = []
    seen_ids = set()
    for raw in data.get("row", ()):
        row = GridRow(
            id=str(raw.get("id", "")),
            model=str(raw.get("model", "")),
            reach_via=str(raw.get("reach_via", "")),
            providers=tuple(raw.get("providers", ())),
            scores=tuple(str(s) for s in raw.get("scores", ())),
            dagger=bool(raw.get("dagger", False)),
        )
        if not row.id or not row.model or not row.reach_via or not row.providers:
            raise GridError(
                f"model-selection row {row!r} at {table}: id, model, reach_via "
                "and providers are all required"
            )
        if row.id in seen_ids:
            raise GridError(f"model-selection row id {row.id!r} duplicated in {table}")
        seen_ids.add(row.id)
        if len(row.scores) != len(columns):
            raise GridError(
                f"model-selection row {row.id!r} has {len(row.scores)} scores "
                f"for {len(columns)} columns in {table}"
            )
        rows.append(row)
    notes: List[GridNote] = []
    for raw in data.get("note", ()):
        models = tuple(str(m) for m in raw.get("models", ()))
        text = str(raw.get("text", "")).strip()
        unknown = [m for m in models if m not in seen_ids]
        if not models or not text:
            raise GridError(f"model-selection note {raw!r} in {table}: models and text required")
        if unknown:
            raise GridError(
                f"model-selection note in {table} names unknown row ids {unknown}"
            )
        notes.append(GridNote(models=models, text=text))
    if not rows:
        raise GridError(f"model-selection grid at {table} declares no rows")
    return Grid(columns=columns, intro=intro, column_legend=legend,
                rows=tuple(rows), notes=tuple(notes),
                general_notes=general_notes)


def _warn_line(message: str) -> None:
    """One stderr line, never a crash (a warning must not break a render)."""
    try:
        print(f"[vco] model-selection: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — a warning must never raise
        pass


def vendor_key_state_probe() -> KeyStateProbe:
    """Build the real TRI-STATE probe from the gateway's own key resolution.

    Maps the resolver's :class:`~model_router.secrets.KeyResult` onto
    :class:`KeyState`: a resolved key → ``PRESENT``; the resolver's
    ``transient`` flag (the store could not be asked — keychain busy,
    hub timing out) → ``UNAVAILABLE``; anything else → ``ABSENT``.

    ``VendorKeyResolver.resolve`` never raises for an unreachable store — it
    returns a :class:`~model_router.secrets.KeyResult` whose ``key`` is
    ``None`` — so the try/except around the probe below covers only the
    genuinely unexpected. Import failures (gateway not installed/active) are
    the caller's business.
    """
    from model_router.secrets import VendorKeyResolver
    from model_router.vendors import VENDORS

    resolver = VendorKeyResolver()

    def probe(vendor_id: str) -> KeyState:
        result = resolver.resolve(VENDORS[vendor_id])
        if result.key:
            return KeyState.PRESENT
        if result.transient:
            return KeyState.UNAVAILABLE
        return KeyState.ABSENT

    return probe


def vendor_key_probe() -> KeyProbe:
    """The BOOLEAN view of :func:`vendor_key_state_probe`.

    The ONE home for "does this vendor's key resolve right now" as a yes/no:
    the per-vendor leg of the gateway agent delivery gate
    (``vco_lib.module_gated_delivery.select_gateway_agents``) calls it. Its
    contract there is "a miss never deletes" — an UNAVAILABLE store folds
    into False exactly like a clean miss, and the gate carries the earlier
    delivery forward. The grid filter uses the tri-state probe instead,
    because for RENDERED guidance the two answers differ (see
    :func:`reachable_providers`).
    """
    state_probe = vendor_key_state_probe()

    def probe(vendor_id: str) -> bool:
        return state_probe(vendor_id) is KeyState.PRESENT

    return probe


# ---------------------------------------------------------------------------
# The last-known provider record (v0.2.103, plan item 12)
#
# One machine-level JSON file holding, per vendor, whether its key resolved
# at the last DEFINITIVE probe round. It exists so a transient store outage
# during a render keeps the previously rendered provider set instead of
# silently dropping rows. Provider booleans only — never a key value.
# ---------------------------------------------------------------------------

#: Record format version (bump with the shape).
RECORD_VERSION = 1


def state_record_path() -> Path:
    """The machine-level last-known provider record.

    Lives under the launcher state root (``vco_lib.paths.vct_root_dir`` —
    ``VCT_STATE_DIR`` honoured), NOT in any project: vendor keys resolve
    through the gateway's shared scope, so the answer is machine-wide.
    """
    from vco_lib.paths import vct_root_dir

    return vct_root_dir() / "model-guidance.json"


def load_record(path: Optional[Path] = None) -> dict:
    """Read the record; ``{}`` when absent, unreadable or corrupt.

    A corrupt record is not a defect worth failing a render over — it is
    treated as "no last-known state" with one stderr line, exactly like a
    first render.
    """
    record_path = Path(path) if path is not None else state_record_path()
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _warn_line(f"provider record at {record_path} unreadable "
                   f"({type(exc).__name__}); treated as no record")
        return {}
    if not isinstance(data, dict):
        _warn_line(f"provider record at {record_path} is not an object; "
                   "treated as no record")
        return {}
    return data


def recorded_providers(record: dict) -> Optional[Dict[str, bool]]:
    """The ``{vendor_id: key_resolved}`` map a record carries, or ``None``."""
    providers = record.get("providers")
    if not isinstance(providers, dict):
        return None
    return {str(v): bool(p) for v, p in providers.items()}


def save_record(record: dict, path: Optional[Path] = None) -> None:
    """Persist the record atomically. Best-effort: a failed write is one
    stderr line, never a raised render."""
    from vco_lib.atomic import atomic_write_json

    record_path = Path(path) if path is not None else state_record_path()
    try:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(record_path, record)
    except Exception as exc:  # noqa: BLE001 — the record is a cache, not state
        _warn_line(f"provider record at {record_path} could not be written "
                   f"({type(exc).__name__})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The per-process provider override (the refresh run's ONE answer)
# ---------------------------------------------------------------------------

#: When set, :func:`reachable_providers` answers exactly this set without
#: probing (and without reading or writing the record). The guidance refresh
#: (``vco_lib.model_guidance``) probes ONCE per run and pins the answer here,
#: so the grid and every provider-conditional section of every file it
#: re-renders agree, and a run costs one probe round, not one per file.
_PROVIDER_OVERRIDE: Optional[FrozenSet[str]] = None


def set_provider_override(providers: Optional[FrozenSet[str]]) -> None:
    """Pin (``frozenset``) or clear (``None``) the per-process provider set."""
    global _PROVIDER_OVERRIDE
    _PROVIDER_OVERRIDE = providers


def reachable_providers(*, key_probe: Optional[KeyProbe] = None,
                        state_probe: Optional[KeyStateProbe] = None,
                        state_path: Optional[Path] = None,
                        path: Optional[Path] = None,
                        sleeper: Callable[[float], None] = time.sleep,
                        ) -> FrozenSet[str]:
    """Which providers' rows render right now: :data:`ALWAYS_PROVIDERS` plus
    every vendor named in the grid whose key resolves.

    Probes are injectable for tests; the default is the gateway's own
    resolver (see :func:`vendor_key_state_probe`). ``key_probe`` is the
    legacy boolean shape (True/False map to PRESENT/ABSENT).

    The tri-state contract (v0.2.103): an UNAVAILABLE vendor is retried once
    (``sleeper`` paces the retry; the gateway's transient cache is ~2 s), and
    a still-unavailable vendor falls back to the last-known record at
    ``state_path`` — its rows are never dropped on a transient failure. With
    no record the vendor is absent (the pre-tri-state behaviour). A round
    where NO vendor is unavailable rewrites the record, but only when the
    answer changed. A probe that raises marks that ONE vendor unavailable
    (soft-fail, one stderr line); a gateway package that cannot be imported
    at all leaves the Claude rows standing.
    """
    if _PROVIDER_OVERRIDE is not None and key_probe is None and state_probe is None:
        return _PROVIDER_OVERRIDE
    available = set(ALWAYS_PROVIDERS)
    grid = load_grid(path)
    wanted = sorted({p for row in grid.rows for p in row.providers
                     if p not in ALWAYS_PROVIDERS})
    if not wanted:
        return frozenset(available)
    probe = state_probe
    if probe is None:
        if key_probe is not None:
            bool_probe = key_probe

            def _from_bool(vendor_id: str) -> KeyState:
                return (KeyState.PRESENT if bool_probe(vendor_id)
                        else KeyState.ABSENT)

            probe = _from_bool
        else:
            try:
                probe = vendor_key_state_probe()
            except Exception as exc:  # noqa: BLE001 — reported, never fatal
                _warn_line(
                    f"model gateway not importable ({type(exc).__name__}); "
                    "rendering Claude rows only"
                )
                return frozenset(available)

    def _ask(vendor_id: str, *, quiet: bool = False) -> KeyState:
        try:
            return probe(vendor_id)
        except Exception as exc:  # noqa: BLE001 — one vendor, one line
            if not quiet:
                _warn_line(
                    f"key resolution for vendor {vendor_id!r} failed "
                    f"({type(exc).__name__})"
                )
            return KeyState.UNAVAILABLE

    states: Dict[str, KeyState] = {v: _ask(v) for v in wanted}
    if any(s is KeyState.UNAVAILABLE for s in states.values()):
        sleeper(_TRANSIENT_RETRY_SLEEP_S)
        for vendor_id in wanted:
            if states[vendor_id] is KeyState.UNAVAILABLE:
                # Quiet: the initial pass already reported this vendor once.
                states[vendor_id] = _ask(vendor_id, quiet=True)

    definitive = all(s is not KeyState.UNAVAILABLE for s in states.values())
    record_path = Path(state_path) if state_path is not None else state_record_path()
    if not definitive:
        # The record answers for the vendors the store could not.
        known = recorded_providers(load_record(record_path)) or {}
        for vendor_id in wanted:
            if states[vendor_id] is not KeyState.UNAVAILABLE:
                continue
            if vendor_id in known:
                states[vendor_id] = (KeyState.PRESENT if known[vendor_id]
                                     else KeyState.ABSENT)
                _warn_line(
                    f"secret store unavailable for vendor {vendor_id!r}; "
                    f"keeping the last-known answer "
                    f"({'key present' if known[vendor_id] else 'no key'})"
                )
            else:
                states[vendor_id] = KeyState.ABSENT
                _warn_line(
                    f"secret store unavailable for vendor {vendor_id!r} and "
                    "no last-known record — its rows are not rendered"
                )
    for vendor_id in wanted:
        if states[vendor_id] is KeyState.PRESENT:
            available.add(vendor_id)
    if definitive:
        answer = {v: states[v] is KeyState.PRESENT for v in wanted}
        prior = load_record(record_path)
        if recorded_providers(prior) != answer:
            next_record = dict(prior)
            next_record.update({
                "version": RECORD_VERSION,
                "providers": answer,
                "recorded_at": _now_iso(),
            })
            save_record(next_record, record_path)
    return frozenset(available)


def render_grid(*, providers: Optional[FrozenSet[str]] = None,
                path: Optional[Path] = None) -> str:
    """The markdown that fills ``{{MODEL_SELECTION_GRID}}``: intro, the
    table, the column legend, the per-model notes whose model rendered, and
    the model-independent :attr:`Grid.general_notes` (always rendered).

    ``providers`` is injectable for tests; ``None`` probes the machine (see
    :func:`reachable_providers`). A row renders when ANY of its providers is
    reachable (GLM-5.3 is served by either vendor)."""
    grid = load_grid(path)
    if providers is None:
        providers = reachable_providers(path=path)
    rendered_rows = [row for row in grid.rows if set(row.providers) & set(providers)]
    lines: List[str] = list(grid.intro)
    lines.append("")
    lines.append("| Model (reach via) | " + " | ".join(grid.columns) + " |")
    lines.append("|" + "---|" * (len(grid.columns) + 1))
    for row in rendered_rows:
        name = f"{row.model} †" if row.dagger else row.model
        lines.append(f"| {name} (`{row.reach_via}`) | "
                     + " | ".join(row.scores) + " |")
    lines.append("")
    lines.append(grid.column_legend)
    rendered_ids = {row.id for row in rendered_rows}
    notes = [n for n in grid.notes if set(n.models) & rendered_ids]
    lines.append("")
    lines.append("What changes the pick:")
    for note in notes:
        lines.append(f"- {note.text}")
    for text in grid.general_notes:
        lines.append(f"- {text}")
    return "\n".join(lines)
