# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ``{{MODEL_SELECTION_GRID}}`` renderer — which model for which task.

The GRID DATA lives in ``vco_lib/model_selection.toml`` (rows, scores, ``*``
flags, reach-via agents, providers, per-model notes, model-independent
general notes). This module decides
which PROVIDERS are reachable at render time and renders only their rows:

* Anthropic/Claude rows always render.
* A vendor row (``zai``, ``qwen``) renders only when the model gateway can
  resolve that vendor's key AT RENDER TIME. This is a snapshot taken during
  install/update — nothing monitors key availability afterwards; the next
  install/update re-renders the section.

Provider reachability REUSES the model gateway's own key resolution —
``model_router.secrets.VendorKeyResolver`` over ``model_router.vendors.VENDORS``
(key NAMES from the vendor rows, values through ``vco_lib.agent_secrets.get``
with the gateway's own scope rules). There is no third key lookup here, and
no key VALUE ever reaches a message, a log or the rendered markdown: only
booleans leave the probe.

Soft-fail contract (the value feeds the materializer, which must never crash
a render): when the gateway package is not installed/active, or a resolution
raises, the vendor's rows are simply not rendered and ONE stderr line says
why. A missing/unreadable/inconsistent data table, by contrast, is a broken
install and raises :class:`GridError` (the materializer turns that into an
unresolved placeholder plus a deferral row, the ordinary owner-rule path).
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, FrozenSet, List, Optional, Tuple

__all__ = [
    "ALWAYS_PROVIDERS",
    "Grid",
    "GridError",
    "GridNote",
    "GridRow",
    "GRID_PATH",
    "load_grid",
    "reachable_providers",
    "render_grid",
]

#: The .toml sits next to this loader inside the ``vco_lib`` package so it
#: ships in the wheel automatically (same shape as ``deferral_conditions.toml``).
GRID_PATH: Path = Path(__file__).resolve().parent / "model_selection.toml"

#: Providers whose rows render unconditionally — first-party Claude needs no
#: vct-secrets key (OAuth passthrough), so it is never probed.
ALWAYS_PROVIDERS: FrozenSet[str] = frozenset({"anthropic"})

#: What a key probe answers per vendor id: True = key resolved (rows render).
KeyProbe = Callable[[str], bool]


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


def _default_key_probe() -> KeyProbe:
    """Build the real probe from the gateway's own key resolution.

    ``VendorKeyResolver.resolve`` never raises for an unreachable store — it
    returns a :class:`~model_router.secrets.KeyResult` whose ``key`` is
    ``None`` — so the try/except around the probe below covers only the
    genuinely unexpected. Import failures (gateway not installed/active) are
    the caller's business.
    """
    from model_router.secrets import VendorKeyResolver
    from model_router.vendors import VENDORS

    resolver = VendorKeyResolver()

    def probe(vendor_id: str) -> bool:
        return bool(resolver.resolve(VENDORS[vendor_id]).key)

    return probe


def reachable_providers(*, key_probe: Optional[KeyProbe] = None,
                        path: Optional[Path] = None) -> FrozenSet[str]:
    """Which providers' rows render right now: :data:`ALWAYS_PROVIDERS` plus
    every vendor named in the grid whose key resolves.

    ``key_probe`` is injectable for tests; the default is the gateway's own
    resolver (see :func:`_default_key_probe`). A probe that raises marks that
    ONE vendor unavailable (soft-fail, one stderr line); a gateway package
    that cannot be imported at all leaves the Claude rows standing.
    """
    available = set(ALWAYS_PROVIDERS)
    grid = load_grid(path)
    wanted = sorted({p for row in grid.rows for p in row.providers
                     if p not in ALWAYS_PROVIDERS})
    if not wanted:
        return frozenset(available)
    if key_probe is None:
        try:
            key_probe = _default_key_probe()
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            _warn_line(
                f"model gateway not importable ({type(exc).__name__}); "
                "rendering Claude rows only"
            )
            return frozenset(available)
    for vendor_id in wanted:
        try:
            if key_probe(vendor_id):
                available.add(vendor_id)
        except Exception as exc:  # noqa: BLE001 — one vendor, one line
            _warn_line(
                f"key resolution for vendor {vendor_id!r} failed "
                f"({type(exc).__name__}); its rows are not rendered"
            )
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
