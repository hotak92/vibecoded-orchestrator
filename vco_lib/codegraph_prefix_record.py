# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The per-project code-graph prefix GENERATION RECORD.

``<project>/.claude/state/codegraph-prefix-generation.json`` records which
code-graph collection prefix this project was last seen using, so a change in
the prefix (a sanitizer-generation change) can be detected ONCE rather than
silently orphaning a whole generation of code classes.

Extracted from ``vco_lib.project_init`` in v0.2.92 W18. That module is >16,000
lines and CLAUDE.md forbids growing a file past ~5,000 lines by another 50; the
record is a self-contained concern (one file format, one read, one write, one
staleness decision), so it gets its own home and ``project_init`` keeps thin
aliases under the historical names every existing caller and test uses.

Split of responsibility, deliberately:

* **This module** owns the file format, the reads, the write, and the PURE
  decision functions. It performs no network I/O and imports nothing from
  ``project_init`` (which imports it), so it is unit-testable on its own.
* **``project_init``** owns the Weaviate probe that supplies the evidence and
  the deferral emission that consumes the decision — the I/O at the edges.

WHY A STALENESS DECISION EXISTS AT ALL (v0.2.92 W18)
----------------------------------------------------
Measured across five live installs, **three** of the five records named a
prefix matching ZERO live Weaviate classes: two were written by the pre-v0.2.92
folder-basename derivation, and one by a KG-rule/code-rule divergence in the
writer. A record naming a prefix that never addressed a real collection set is
not evidence of a generation CHANGE — but the drift guard compared it as though
it were, and the deferral it emitted told the user their old classes were
"ORPHANED" and printed a reclaim command for a collection set that does not
exist. :func:`classify_generation_evidence` is the gate that turns that into
either a silent correction (when the live schema proves the record was wrong)
or an honestly-worded report (when it cannot).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

__all__ = [
    "RECORD_FILENAME",
    "RECORD_SCHEMA",
    "SOURCE_BINDING",
    "SOURCE_DERIVED",
    "EVIDENCE_HEAL_TO_CURRENT",
    "EVIDENCE_REPORT_OLD_CLASSES_LIVE",
    "EVIDENCE_REPORT_OLD_CLASSES_ABSENT",
    "EVIDENCE_REPORT_UNVERIFIED",
    "record_path",
    "read_prefix",
    "read_source",
    "write",
    "recorded_is_basename_derivation",
    "classify_generation_evidence",
    "render_drift_entry_text",
]

RECORD_FILENAME = "codegraph-prefix-generation.json"
RECORD_SCHEMA = "vco.codegraph_prefix_generation.v1"

#: Provenance: the prefix came from a real ``project_codegraph_bindings`` row.
#: Same string as :data:`vco_lib.project_identity.SOURCE_BINDING`; pinned equal
#: by ``tests/test_v0292_wp4_prefix_record.py`` so the two cannot drift.
SOURCE_BINDING = "binding"
#: Provenance: the prefix was SANITIZED from a name. A guess, not a fact.
SOURCE_DERIVED = "derived"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def record_path(folder: Path) -> Path:
    """`<folder>/.claude/state/codegraph-prefix-generation.json`."""
    return Path(folder) / ".claude" / "state" / RECORD_FILENAME


def read_prefix(folder: Path) -> Optional[str]:
    """The recorded last-seen code-graph prefix, or ``None``.

    Soft-fail: ``None`` on any error (missing file / malformed JSON).
    """
    try:
        raw = record_path(folder).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — missing / unreadable → no record
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — malformed → no usable record
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("collection_prefix")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def read_source(folder: Path) -> Optional[str]:
    """The recorded prefix's PROVENANCE, or ``None`` when unknown.

    ``None`` covers three states that must all be treated alike — no file, an
    unreadable/malformed file, and a v0.2.92-pre record that predates the
    ``source`` field. The last one is the population carrying the field
    poisoning, and its provenance is genuinely unknown: it may be a good
    binding-derived value or a basename-derived one. Callers must not treat
    ``None`` as "derived" (see F-3 in
    ``project_init.detect_codegraph_prefix_drift``).
    """
    try:
        data = json.loads(record_path(folder).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing / malformed → unknown
        return None
    if not isinstance(data, dict):
        return None
    val = data.get("source")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def write(folder: Path, prefix: str, *, source: str = SOURCE_DERIVED) -> bool:
    """Atomically record the last-seen code-graph prefix generation.

    Best-effort: returns ``False`` on any I/O failure rather than raising
    (recording is an aid, not a hard requirement).

    ``source`` (v0.2.92 W8, additive — the v1 reader ignores unknown keys)
    records WHERE the prefix came from: :data:`SOURCE_BINDING` when it is the
    authoritative ``project_codegraph_bindings.collection_prefix`,
    :data:`SOURCE_DERIVED` when it was sanitized from a project name. A record
    written from a folder-basename derivation is not evidence of a generation
    change, and this field lets a later run say so instead of guessing.
    """
    p = record_path(folder)
    payload = {
        "schema": RECORD_SCHEMA,
        "collection_prefix": prefix,
        "source": source,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    body = json.dumps(payload, indent=2) + "\n"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        from vco_lib.atomic import atomic_write_text
        atomic_write_text(p, body)
        return True
    except Exception:  # noqa: BLE001 — fall back to a plain write
        try:
            p.write_text(body, encoding="utf-8")
            return True
        except Exception:  # noqa: BLE001 — recording never blocks a caller
            return False


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------

def recorded_is_basename_derivation(folder: Path, recorded: str) -> bool:
    """True when ``recorded`` is exactly what the pre-v0.2.92 folder-basename
    derivation would have produced for ``folder``.

    Narrow by construction: it matches the ONE wrong derivation v0.2.92
    removed. A prefix that merely resembles the basename (or that equals the
    basename derivation AND the authoritative binding — in which case the
    caller never reaches here, the normalised compare already returned) is not
    treated as poison.

    Uses the SHARED code-prefix sanitizer
    (``codegraph_to_mermaid._sanitize_collection_prefix``, the never-raising
    wrapper over ``project_naming.canonical_class_prefix``) — the same one
    ``project_init.derive_project_code_prefix`` delegates to. Imported here
    directly rather than through ``project_init`` because that module imports
    THIS one.
    """
    try:
        basename = Path(folder).name or ""
        if not basename:
            return False
        from vco_lib.codegraph_to_mermaid import _sanitize_collection_prefix
        from vco_lib.project_identity import normalise_for_match

        basename_derived = _sanitize_collection_prefix(basename) or ""
    except Exception:  # noqa: BLE001 — never raise into a drift decision
        return False
    if not basename_derived:
        return False
    return normalise_for_match(recorded) == normalise_for_match(basename_derived)


#: The recorded prefix names NO live class and the current one names at least
#: one: the record never described a real collection set, so correct it
#: silently. There is no orphaned generation and nothing for the user to do.
EVIDENCE_HEAL_TO_CURRENT = "heal_to_current"
#: The recorded prefix's classes are LIVE — a real orphaned generation. The
#: reclaim command may name them.
EVIDENCE_REPORT_OLD_CLASSES_LIVE = "report_old_classes_live"
#: The live schema was READ and the recorded prefix has no classes there, but
#: there is no authoritative prefix to correct the record TO. Report the change
#: WITHOUT claiming anything is orphaned and WITHOUT a reclaim command.
EVIDENCE_REPORT_OLD_CLASSES_ABSENT = "report_old_classes_absent"
#: No usable live-schema evidence (not probed, or the probe could not run).
#: Report as before, but say what was NOT verified.
EVIDENCE_REPORT_UNVERIFIED = "report_unverified"


def classify_generation_evidence(
    *,
    recorded_live: Optional[bool],
    current_live: Optional[bool],
    current_is_authoritative: bool,
) -> str:
    """Decide what a prefix disagreement MEANS, given live-schema evidence.

    Pure: all three inputs are already-resolved facts, so every branch is
    unit-testable without a server. Each argument is TRI-STATE where relevant —
    ``None`` means the probe could not run and is never read as ``False``.

    Args:
        recorded_live: does the RECORDED prefix have any live code class?
        current_live: does the CURRENT prefix have any live code class?
        current_is_authoritative: is ``current`` backed by a real
            ``project_codegraph_bindings`` row? Only then may the record be
            re-derived to it — re-deriving to a name sanitization is precisely
            the poisoning v0.2.92 F-3 removed.

    Returns one of the four ``EVIDENCE_*`` constants. The default is
    :data:`EVIDENCE_REPORT_UNVERIFIED`, i.e. the historic behaviour: a missing
    or unreadable answer never unlocks a new branch.
    """
    if recorded_live is True:
        return EVIDENCE_REPORT_OLD_CLASSES_LIVE
    if recorded_live is False:
        # The schema was READ and the recorded prefix addresses nothing.
        if current_is_authoritative and current_live is True:
            return EVIDENCE_HEAL_TO_CURRENT
        return EVIDENCE_REPORT_OLD_CLASSES_ABSENT
    return EVIDENCE_REPORT_UNVERIFIED


# ---------------------------------------------------------------------------
# The deferral's TEXT — a pure function of the evidence
# ---------------------------------------------------------------------------

def render_drift_entry_text(
    folder: Path,
    project_name: str,
    old_prefix: str,
    new_prefix: str,
    *,
    weaviate_url: str,
    code_suffixes: tuple,
    old_classes_live: Optional[bool] = None,
) -> tuple:
    """``(detected, command_to_apply)`` for ``codegraph_prefix_drift_detected``.

    ``old_classes_live`` is the TRI-STATE live-schema evidence about the OLD
    prefix: ``True`` = its classes exist, ``False`` = the schema was read and
    they do not, ``None`` = not checked / could not check. It decides what this
    entry is allowed to CLAIM. **Only ``True`` may say the old classes are
    orphaned and print the reclaim command** — a printed command is shipped
    code, and a reclaim aimed at a collection set that does not exist is the
    false-premise shape v0.2.92 exists to remove.

    ``code_suffixes`` is passed in rather than imported: ``project_init`` owns
    that SSOT and this module must not grow a second copy of it.

    Pure — no I/O, no emission — so all three variants are unit-testable
    without a deferral sink.
    """
    old_classes = ", ".join(f"{old_prefix}{s}" for s in code_suffixes)
    changed = (
        f"The code-graph collection prefix for project {project_name!r} "
        f"changed from {old_prefix!r} to {new_prefix!r} (a sanitizer-"
        f"generation change). "
    )
    reanalyze = (
        "# The new-prefix code graph is (re)built by re-running the analyzer:\n"
        f".claude/scripts/code-graph-analyze . --project {project_name!r}"
    )
    detect_cmd = (
        "python -m vco_lib.project_init detect-orphan-code-collections "
        f"--weaviate-url {weaviate_url!r} --project-folder {str(folder)!r}\n"
    )
    if old_classes_live is True:
        return (
            changed + (
                f"The previous generation's code classes ({old_classes}) are "
                f"present in the live schema at {weaviate_url} and now "
                f"ORPHANED — the analyzer writes only to the NEW prefix, so "
                f"the old set accumulates dead on-disk segments unless "
                f"migrated or dropped."
            ),
            "# The old code-graph classes are orphaned by the prefix change.\n"
            "# Reclaim them (CONSENTED) — the detector re-validates against the\n"
            "# live binding table before any drop:\n"
            + detect_cmd
            + "# then run the drop command it prints in "
              "`orphan_code_collections_detected`.\n"
            + reanalyze,
        )
    if old_classes_live is False:
        return (
            changed + (
                f"NOTHING is orphaned: the previous generation's classes "
                f"({old_classes}) were checked against the live schema at "
                f"{weaviate_url} and NONE of them exists, so there is no old "
                f"collection set to migrate or reclaim. The record has been "
                f"updated to the new prefix; this entry exists so the change "
                f"is visible, not because data is at stake."
            ),
            "# Nothing to reclaim — the old prefix names no live class.\n"
            + reanalyze,
        )
    return (
        changed + (
            f"The previous generation's code classes ({old_classes}) were NOT "
            f"checked against the live schema (Weaviate at {weaviate_url} was "
            f"not consulted, or could not be read), so whether anything is "
            f"actually orphaned is UNKNOWN. The detect command below reports "
            f"what exists before anything is dropped; it flags nothing when it "
            f"cannot read the schema."
        ),
        "# Whether the old classes still exist is UNVERIFIED. This command\n"
        "# is READ-ONLY: it reports what it finds and refuses to flag\n"
        "# anything when the live schema or the binding table is unreadable.\n"
        + detect_cmd
        + "# Only if it reports orphans, run the drop command it prints in\n"
          "# `orphan_code_collections_detected`.\n"
        + reanalyze,
    )
