# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-PROJECT record of the KG metadata-repair pass (v0.2.95).

Why this module exists
-----------------------
v0.2.95 repairs a KG row's stale stored ``title`` / ``node_type`` / ``tags``
/ ``external_links`` on the sync's EMBED-SKIP path (``vco_lib.kg_metadata_repair``):
when a node's TEXT is unchanged but its stored properties predate this
release's frontmatter parse, the row is PATCHED — one fetch, one property
update, ZERO embeds, ``content_hash`` untouched.

That repair is reachable only from a run that VISITS the node, and the
defect's own premise — the file text never changed — is exactly what stops
anything from visiting it. ``install.py``'s leg (d) closed that for the
install ROOT by keying on an ``app_state`` row. It closes NOTHING for a
registered PROJECT: a project's tree is synced by the launcher's bundle
update, which runs ``kg-sync --check-drift`` and spawns ``--all`` only on
drift — and drift is *recomputed signature ≠ stored hash*, which is EQUAL
for precisely these rows. So a 0.2.94 project that received a
``name:``-plus-nested-``metadata:`` node (the origin the CHANGELOG names)
gets "Update bundle" at 0.2.95, reports no drift, runs no sync, and keeps
its prose-scraped ``tags`` forever, with tag filters silently excluding it.

This module is the per-project half of the answer: a stamp the sync script
writes at the END of a clean whole-tree run, and the drift probe reads to
report "repair owed". The existing drift machinery then spawns the ordinary
zero-embed ``--all``, and that run's clean completion retires the signal —
ONE mechanism covering the install root, every registered project, and the
launcher's Sync-KG button alike.

Why a file in the project's own ``.claude/state/`` and not ``app_state``
------------------------------------------------------------------------
Same reasoning ``vco_lib.chunker_revision`` records for its own sentinel,
and this module deliberately mirrors that file's shape: the sync engine runs
for CLI-only and never-booted-the-GUI installs too (no ``launcher.db`` to
write), and per-project state must stay genuinely per-project — ``app_state``
is GLOBAL, so it can only ever speak for the install root's own tree.

The root keeps its ``app_state`` leg, and since the round-6 ship-gate MAJOR
that row is a PROJECTION of this file rather than a second opinion about the
same fact: ``install.py`` passes its ``PROJECT_ROOT`` to
``install_weaviate.stamp_kg_metadata_repair``, which stamps ``app_state``
only when :func:`repair_owed` answers ``False`` for the tree the run just
walked. Before that, the two could disagree — a repair that aborted part-way
is COUNTED rather than FAILED, so the run still exits 0, this file's stamp is
withheld and the exit code alone said "done". They cannot disagree now. The
remaining divergence is one-directional and harmless: an ``--all`` from the
launcher button or by hand stamps here and not there, which costs the root at
most ONE further zero-embed pass, once.

The GENERATION LADDER is not forked: :data:`KG_METADATA_REPAIR_BUMPS` and the
``generation_is_current`` comparison both live in ``vco_lib.install_weaviate``
and are CALLED from here. A future release that teaches the parser a third
dialect appends one entry there, and both the root's answer and every
project's answer follow from it.

Read-only plus one small JSON stamp per project. Never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

#: Per-project record of the newest metadata-repair generation a clean
#: whole-tree sync completed for, relative to the project folder. Sibling of
#: ``chunker_revision.STATE_REL`` and deliberately its own file: that one
#: answers "which chunker revision did this project last observe?", this one
#: "which metadata-repair generation has this project's tree been walked
#: under?".
STATE_REL = Path(".claude") / "state" / "kg-metadata-repair.json"


def state_path(folder: Path) -> Path:
    """Absolute path of the stamp file under ``folder``."""
    return Path(folder) / STATE_REL


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_stamp(folder: Path) -> Optional[str]:
    """The recorded repair generation for ``folder``.

    Returns:
        ``""``   — no stamp file: nothing has been recorded. A KNOWN state
                   (the state every 0.2.94 project is in), not an unreadable
                   one, and the state that must read as OWED.
        ``str``  — the recorded generation.
        ``None`` — the file exists and could not be read or parsed. UNKNOWN,
                   never "nothing recorded": the difference decides whether a
                   whole-tree pass is spawned, and a corrupt stamp must not be
                   able to spawn one on every update forever.

    The three-way split is ``chunker_revision.read_resync_stamps``'s, for the
    same reason — positive evidence only, in both directions.
    """
    path = state_path(folder)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    generation = data.get("generation")
    if not isinstance(generation, str) or not generation.strip():
        return None
    return generation.strip()


def write_stamp(folder: Path, generation: Optional[str] = None) -> bool:
    """Record that ``folder``'s tree has been walked under ``generation``.

    ``generation`` defaults to ``install_weaviate.KG_METADATA_REPAIR_STAMP``
    — the ONE home for what a satisfied pass is worth, shared with the root's
    ``app_state`` leg so the two can never name different values.

    Best-effort and atomic (tmp + replace), exactly like
    ``chunker_revision.write_last_revision``. Never raises; returns whether
    the stamp landed so a caller can LOG it — never gate on it. A stamp that
    does not land leaves the pass owed, and that is the safe direction: never
    a silently-skipped repair. Name the price honestly, though — it is not
    "one more pass" but ONE PER UPDATE until the stamp can land, because a
    cause that survives the run (a read-only ``.claude/state/``, a full disk)
    also survives the next one. Same class as ``chunker_revision``'s.
    """
    if generation is None:
        from vco_lib.install_weaviate import KG_METADATA_REPAIR_STAMP

        generation = KG_METADATA_REPAIR_STAMP
    generation = (generation or "").strip()
    if not generation:
        # Nothing positively known to record. Stamping a guess would retire
        # the pass with the work unproven — the one direction this whole
        # mechanism exists to prevent.
        return False
    try:
        target = state_path(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"generation": generation, "updated_at": _now_iso()}) + "\n",
            encoding="utf-8",
        )
        tmp.replace(target)
        return True
    except OSError:
        return False


def repair_owed(folder: Path) -> Optional[bool]:
    """Does ``folder`` still owe the whole-tree metadata-repair pass?

    Returns:
        True  — no stamp yet, or a stamp below the newest entry of
                :data:`~vco_lib.install_weaviate.KG_METADATA_REPAIR_BUMPS`.
        False — provably satisfied: the stamp names that generation or newer.
        None  — could not look (the stamp file exists and is corrupt).
                "Cannot look" is its own answer and the caller must degrade to
                PRIOR behaviour with it, never to a pass repeated on every
                update; an absent stamp is the owed case, an unreadable one is
                not.

    The comparison itself is ``install_weaviate.kg_metadata_repair_due`` —
    called, not re-derived, so the per-project answer and the install root's
    ``app_state`` answer cannot disagree about what "at least this
    generation" means.
    """
    stamped = read_stamp(folder)
    if stamped is None:
        return None
    from vco_lib.install_weaviate import kg_metadata_repair_due

    return kg_metadata_repair_due(stamped or None)
