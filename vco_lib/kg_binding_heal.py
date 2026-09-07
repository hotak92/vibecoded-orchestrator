# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""KG-binding self-heal — the single Python writer for ``launcher.db`` KG
binding / access / codegraph-prefix repair (X-1, v0.2.73).

Single-writer contract
-----------------------
Before v0.2.73 the ~790-line ``install.py::_self_heal_kg_bindings_on_update``
accumulated FIVE layers of drift-repair (case rebind, access-row rebind,
cross-prefix adoption, W40 adoption uplift, access-parity backfill) inline in
the install mega-file. Every layer wrote ``project_kg_bindings`` /
``kg_collection_access`` directly, and the repair existed *because* those
tables drifted — the launcher (Rust) creates the rows with canonical casing,
but historical installs, renames and manual overrides left stale rows the
next ``--update`` had to reconcile.

This module is now the ONE Python home for that repair. The contract:

* The launcher (Rust ``project_state``/``projects_v2`` commands) is the
  authoritative *creator* of binding rows.
* This module is the ONLY Python code that *heals* (updates) those rows,
  and it does so through a single entry point, :func:`self_heal_kg_bindings`.
* ``install.py`` keeps a thin shim (``_self_heal_kg_bindings_on_update``)
  that injects its own ``launcher.db``/logging/migrate helpers and delegates
  here. The heal SQL lives here, nowhere else.
* ``tests/test_kg_binding_heal_single_writer.py`` lints that no other Python
  module UPDATEs those columns.

Dependency injection
---------------------
The heal touches launcher.db + Weaviate + the migrate-collections smart path,
all of which install.py already knows how to reach. Rather than import
install.py back (a circular import — install.py is the entry script), the
public entry takes callables:

    self_heal_kg_bindings(
        deferral_report,
        *,
        db_path,                 # resolved launcher.db Path
        weaviate_url,            # resolved base URL
        existing_classes,        # set[str] from /v1/schema
        existing_by_lower,       # {lower: canonical}
        log_event,               # (stage, level, msg, *, data=None) -> None
        connect_rw,              # (db_path, *, label) -> sqlite3.Connection
        run_adoption_uplifts,    # optional smart-path uplift callable
    )

install.py resolves launcher.db + Weaviate schema (the cheap detection pass
that decides whether the writer lock is even needed) and hands the resolved
values in. Behaviour is byte-for-byte the pre-extraction behaviour.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

# Access-level privilege ranking for kg_collection_access dedup decisions.
_KG_ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "write": 2}

# The orchestrator-root project's canonical NAME. The pre-fix launcher bug
# seeded the root's own-primary kg_collection_access row as
# ``sanitize_for_weaviate_class(ORCHESTRATOR_ROOT_NAME) + "_KnowledgeGraph"``
# = ``"VibeCodedOrchestrator_KnowledgeGraph"`` (identical to the shared-KG
# default class name). That is the ONLY dead literal the R8 5b sweep heals —
# see ``_dead_root_own_primary_name`` + ``_sweep_dead_root_own_primary_rows``.
ORCHESTRATOR_ROOT_NAME = "VibeCodedOrchestrator"


def _dead_root_own_primary_name() -> str:
    """The single dead literal the R8 5b sweep is allowed to touch.

    Derived through the canonical writer sanitizer so it can never drift from
    the name the pre-fix launcher actually seeded. Kept as a function (not a
    module constant) so the sanitizer import stays lazy — this module is
    imported on the install/update hot path and the sanitizer pulls in the
    ``codegraph_naming`` module.
    """
    from vco_lib.codegraph_naming import sanitize_for_weaviate_class

    return sanitize_for_weaviate_class(ORCHESTRATOR_ROOT_NAME) + "_KnowledgeGraph"

# Cross-prefix self-heal — suffixes considered when probing Weaviate for a
# populated sibling under a different prefix. Mirrors
# ``vco_lib.project_init._KG_SUFFIXES``; kept here so install.py's detection
# pass and this module agree without a cross-import.
_KG_BINDING_PREFIX_ADOPT_SUFFIXES: tuple[str, ...] = (
    "_KnowledgeGraph",
    "_Development",
)


def _count_weaviate_class_objects(
    weaviate_url: str, class_name: str,
) -> Optional[int]:
    """Count objects in ``class_name`` via Weaviate's GraphQL Aggregate.

    Returns:
        int  — object count when the request succeeds (0 for empty class).
        None — Weaviate unreachable, malformed response, or HTTP error.
               Callers MUST treat ``None`` as "unknown" (not zero) so a
               transient network blip cannot cause a populated collection
               to look empty and miss adoption.

    Soft-fails throughout: never raises into the caller.
    """
    base = (weaviate_url or "http://localhost:8081").rstrip("/")
    # GraphQL injection guard: class_name comes from Weaviate's own schema
    # endpoint (we filter from existing_classes), so it's already safe. But
    # validate the shape anyway to fail-closed if a future caller passes
    # user input.
    if not class_name or not class_name.replace("_", "").isalnum():
        return None
    query = (
        "{ Aggregate { "
        f"{class_name} {{ meta {{ count }} }}"
        " } }"
    )
    try:
        data = json.dumps({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/graphql",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(  # noqa: S310 (localhost only)
            req, timeout=10,
        )
    except Exception:
        return None
    try:
        status = resp.getcode()
    except Exception:
        status = 0
    if status != 200:
        return None
    try:
        payload = json.loads(resp.read())
    except Exception:
        return None
    try:
        agg = payload.get("data", {}).get("Aggregate", {}) or {}
        rows = agg.get(class_name) or []
        if not rows:
            # Aggregate returns empty list for missing class.
            return 0
        meta = rows[0].get("meta") or {}
        count = meta.get("count")
        if isinstance(count, int):
            return count
    except Exception:
        return None
    return None


def _rebind_collection_names_to_on_disk_casing(
    cur,
    *,
    table: str,
    project_id_col: str,
    collection_name_col: str,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    extra_select_cols: tuple[str, ...] = (),
    do_rebind: Callable[..., None],
    resolve_conflict: Optional[Callable[..., None]] = None,
) -> list[tuple]:
    """Generic helper: rebind a SQLite table's ``collection_name`` column to
    the on-disk Weaviate casing when a case-different sibling exists in
    ``existing_classes``.

    Algorithm (per row):
      1. ``SELECT project_id, collection_name, *extra_select_cols FROM <table>``.
      2. If ``collection_name`` is exact-match in ``existing_classes`` → skip.
      3. If no case-insensitive sibling in ``existing_by_lower`` → skip
         (genuine missing class; orphan-prune sync recreates lazily).
      4. Otherwise the row needs rebinding. When ``resolve_conflict`` is
         provided, the helper probes the SELECT-time row set for a
         ``(project_id, target_name)`` collision; on hit, delegates to
         ``resolve_conflict`` (which mutates the DB and appends to
         ``rebinds`` via the closure). Otherwise — and always for tables
         where the rebind can't violate a unique constraint — calls
         ``do_rebind`` for a straight UPDATE.

    The helper is SQL-shape-agnostic: callers own the exact ``UPDATE`` /
    ``DELETE`` statements via ``do_rebind`` / ``resolve_conflict`` so
    table-specific concerns (extra ``SET`` columns, natural-key shape,
    privilege rules) stay with the caller.

    Returns the audit list — caller-supplied via the closures — so the
    parent function can pull a final summary into the deferral entry.
    """
    rebinds: list[tuple] = []
    select_cols = (project_id_col, collection_name_col) + extra_select_cols
    cur.execute(
        f"SELECT {', '.join(select_cols)} FROM {table}"
    )
    rows = cur.fetchall()

    # Build conflict lookup once if conflict-resolution is enabled — keyed
    # by (project_id, name) with the FULL row tuple as the value so
    # resolve_conflict can read extras (e.g. access_level for the
    # privilege-rank decision).
    conflict_lookup: dict[tuple, tuple] = {}
    if resolve_conflict is not None:
        for row in rows:
            proj_id = row[0]
            coll = row[1]
            if proj_id and coll:
                conflict_lookup[(proj_id, coll)] = row

    for row in rows:
        proj_id = row[0]
        coll_name = row[1]
        if not coll_name:
            continue
        if coll_name in existing_classes:
            # Exact match — nothing to do.
            continue
        actual = existing_by_lower.get(coll_name.lower())
        if actual is None or actual == coll_name:
            # Genuinely missing OR already canonical (defensive — filtered
            # above for missing-from-existing_classes).
            continue

        conflict_row: Optional[tuple] = None
        if resolve_conflict is not None:
            conflict_row = conflict_lookup.get((proj_id, actual))

        if conflict_row is not None and resolve_conflict is not None:
            resolve_conflict(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                current_row=row,
                conflict_row=conflict_row,
                rebinds=rebinds,
            )
        else:
            do_rebind(
                cur,
                project_id=proj_id,
                old_name=coll_name,
                new_name=actual,
                row=row,
                rebinds=rebinds,
            )

    return rebinds


def _prefix_adopt_kg_bindings_pass(
    cur,
    *,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    weaviate_url: str,
) -> dict[str, list]:
    """Second-pass cross-prefix adoption for ``project_kg_bindings``.

    The case-insensitive first pass (see
    :func:`_rebind_collection_names_to_on_disk_casing`) handles rows whose
    ``collection_name`` differs only in casing from a live Weaviate class.
    This second pass handles a different shape: a row whose
    ``collection_name`` is GENUINELY MISSING from Weaviate (and has no
    case-sibling), but where Weaviate holds a populated class under a
    *different prefix* but the *same suffix*.

    Algorithm (per binding row left un-aligned by pass 1):
      1. Skip rows whose ``collection_name`` is already in
         ``existing_classes`` (exact match — nothing to do).
      2. Skip rows whose ``collection_name.lower()`` is in
         ``existing_by_lower`` (pass 1 would have rebound this).
      3. Determine the suffix (``_KnowledgeGraph`` or ``_Development``).
         Rows whose advertised name doesn't end in a known suffix are
         skipped — we don't second-guess arbitrary user-set names.
      4. Probe ``existing_classes`` for every class ending in that suffix;
         query Weaviate Aggregate for row counts; filter to candidates with
         ``row_count > 0``.
      5. Decision:
           * Exactly one candidate → UPDATE the binding row, tag
                                     ``manual_override:v0.2.40-prefix-adopt``.
           * Multiple populated    → record for
                                     ``multi_candidate_prefix_adopt``
                                     deferral; DO NOT modify the row.
           * Zero candidates       → no-op (legitimate missing-class state).

    Idempotency: a second run finds the adopted ``collection_name`` in
    ``existing_classes`` (step 1) and short-circuits at the per-row skip.

    Returns a dict with two keys:
        adopts:           list of (project_id, role, old_name, new_name, row_count)
        multi_candidates: list of (project_id, role, old_name, [(cand, count), ...])

    Soft-fails on Weaviate errors per candidate: when row-count probing
    returns ``None`` for a candidate, that candidate is treated as
    "unknown" and skipped (never adopted blindly).
    """
    adopts: list[tuple[str, str, str, str, int]] = []
    multi_candidates: list[tuple[str, str, str, list[tuple[str, int]]]] = []

    cur.execute(
        "SELECT project_id, role, collection_name, config_json "
        "FROM project_kg_bindings"
    )
    rows = cur.fetchall()

    # Group existing classes by suffix once — we'll consult per row.
    classes_by_suffix: dict[str, list[str]] = {
        s: [] for s in _KG_BINDING_PREFIX_ADOPT_SUFFIXES
    }
    for cls in existing_classes:
        for suffix in _KG_BINDING_PREFIX_ADOPT_SUFFIXES:
            if cls.endswith(suffix):
                classes_by_suffix[suffix].append(cls)
                break

    # Cache row-counts so we don't re-probe Weaviate twice for the same
    # class when multiple binding rows map to the same suffix.
    count_cache: dict[str, Optional[int]] = {}

    def _count(name: str) -> Optional[int]:
        if name not in count_cache:
            count_cache[name] = _count_weaviate_class_objects(
                weaviate_url, name,
            )
        return count_cache[name]

    for row in rows:
        proj_id, role, coll_name, config_json = row
        if not coll_name:
            continue
        # Pass-1 already aligned exact-match and case-sibling rows.
        if coll_name in existing_classes:
            continue
        if coll_name.lower() in existing_by_lower:
            # Defensive: pass-1 should have rebound this. If it didn't,
            # leave the row alone — pass-1 owns that case.
            continue

        # Determine the suffix the row's name ends in. Unknown suffix →
        # skip (we don't probe arbitrary prefixes; the user might have a
        # custom convention we shouldn't second-guess).
        suffix: Optional[str] = None
        for s in _KG_BINDING_PREFIX_ADOPT_SUFFIXES:
            if coll_name.endswith(s):
                suffix = s
                break
        if suffix is None:
            continue

        # Find all populated candidate classes matching the suffix.
        candidates: list[tuple[str, int]] = []
        for cand in classes_by_suffix.get(suffix, []):
            if cand == coll_name:
                # Won't happen (we already filtered exact-match above),
                # but keep the guard defensively.
                continue
            cnt = _count(cand)
            if cnt is None:
                # Weaviate transient error or malformed response — skip
                # this candidate. Never auto-adopt with unknown count.
                continue
            if cnt > 0:
                candidates.append((cand, cnt))

        if not candidates:
            # No populated sibling under this suffix. Legitimate
            # missing-class state — preserve the existing contract
            # (orphan-prune sync recreates lazily; user picks via the
            # launcher's Shared KG picker).
            continue

        if len(candidates) == 1:
            # Exactly one populated sibling — auto-adopt.
            cand_name, cand_count = candidates[0]
            # Build the new config_json with the v0.2.40 sentinel.
            # Preserve other config_json keys so we don't clobber
            # user/launcher state in there.
            try:
                cfg = json.loads(config_json) if config_json else {}
                if not isinstance(cfg, dict):
                    cfg = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                cfg = {}
            cfg["manual_override"] = "v0.2.40-prefix-adopt"
            new_config = json.dumps(cfg)

            cur.execute(
                "UPDATE project_kg_bindings "
                "SET collection_name = ?, config_json = ?, updated_at = ? "
                "WHERE project_id = ? AND role = ?",
                (cand_name, new_config, int(time.time() * 1000), proj_id, role),
            )
            adopts.append((proj_id, role, coll_name, cand_name, cand_count))
        else:
            # Multiple populated candidates — refuse to guess. Sort by row
            # count descending so the deferral's listing leads with the
            # most populated candidate.
            candidates.sort(key=lambda c: c[1], reverse=True)
            multi_candidates.append((proj_id, role, coll_name, candidates))

    return {"adopts": adopts, "multi_candidates": multi_candidates}


# ═══════════════════════════════════════════════════════════════════════════
# D18 heal — evidence-backed PRIMARY repoint (v0.2.92)
# ═══════════════════════════════════════════════════════════════════════════
#
# The gap the two passes above leave. ``_prefix_adopt_kg_bindings_pass`` heals
# a binding whose class is ABSENT from Weaviate; it skips, at its very first
# guard, every row whose class EXISTS. A ghost that has already received the
# project's writes EXISTS (the MCP creates a class on first store), so the
# recorded field shape — a project's KG objects living in a class its primary
# binding does not name — is the one case no writer touched. Until v0.2.92 it
# was diagnosed by ``vco_lib.kg_binding_doctor`` and left to a human.
#
# WHY THIS IS NOT THE FIX R38 REJECTED. R38 records that the previous attempt
# at "healing D18" rewrote state from a NAME-DERIVED guess, and that on the
# most likely field machine — where the binding itself is the stale thing —
# it would have re-stamped the ghost on every run while reporting success.
# The difference here is the eligibility input, and it is total:
#
#   * the OLD design's candidate came from the naming rule
#     (``expected_kg_primary_class(name)``) — a guess about what the class
#     SHOULD be called, which is worthless precisely when the name is what
#     went wrong;
#   * this design's candidate comes from FILE-BACKED POSITIVE EVIDENCE —
#     ``ProjectBindingVerdict.evidence``, the doctor's own measured
#     answer to "which class demonstrably holds THIS project's objects",
#     calibrated on a live 7-project machine (>= 2 distinct sampled
#     ``file_path`` values existing under the project folder AND >= 80% of
#     the sample; a legitimate cross-copy case measured 52% and was
#     correctly excluded).
#
# Nothing in this module MEASURES evidence. :func:`plan_evidence_repoints`
# reads ``verdict.evidence`` and NOTHING ELSE — no threshold of its own, no
# path test, no object count, no second visit to Weaviate or to the filesystem.
# What it adds is a DECISION over the doctor's already-measured numbers: when
# more than one class cleared the bar, it ranks them by the doctor's own
# ``matched_paths`` and asks whether the leader's lead is decisive — more than
# :data:`EVIDENCE_MARGIN_FACTOR` times the runner-up's matched paths AND more
# than :data:`EVIDENCE_MARGIN_ABS` above it (both strict; see the constants).
# That is a comparison of two numbers the doctor produced, not a third opinion
# about what counts as evidence: no input to it exists that the doctor did not
# already publish, and moving the doctor's calibration moves the leader, the
# runner-up and therefore the verdict. So the ownership bar still has exactly
# ONE home (``kg_binding_doctor``) and this stays a consumer of it — the
# property that keeps the report and the repair from disagreeing about one
# machine. A forked bar would be a second answer to "is this class the home?";
# a margin is an answer to "did the one calibration speak clearly enough to act
# on?", which is the heal's own question and belongs here. The pin is
# behavioural, not a source scan: moving the doctor's constants moves this
# pass's decision, and moving the MARGIN constants moves it too
# (``tests/test_v0292_d18_evidence_heal.py``).
#
# ELIGIBILITY, in full. A primary binding is repointed only when EVERY one of
# these holds; any miss leaves the row untouched and the state diagnosed
# exactly as it is today:
#
#   1. the scan COMPLETED (``scan_kg_binding_evidence`` returned a scan, not
#      ``None``) — probe failure is not evidence, and an unreachable Weaviate
#      or an unreadable launcher.db yields no plan at all;
#   2. the project has a REAL primary binding row (the scan only issues
#      verdicts for ``kg_primary_source == SOURCE_BINDING``);
#   3. the cleared classes — counting BOUND ones too — name ONE class the
#      evidence is unambiguous about, and it is not the one already bound.
#      Zero cleared → nothing to heal. Exactly one → that one. Two or more →
#      the DECISIVE-MARGIN rule (:data:`EVIDENCE_MARGIN_FACTOR` /
#      :data:`EVIDENCE_MARGIN_ABS`): a leader that dominates the runner-up by
#      both margins is what the evidence says, and anything short of that is
#      refused with :data:`REFUSE_AMBIGUOUS` — a near-tie is a data split, not
#      a tie to break, and it is now ASKED about
#      (``kg_binding_ambiguous_evidence``, emitted from the read-only plan
#      site) instead of left silent. Counting the bound class matters twice:
#      when it holds a comparable share of the project's files the evidence
#      cannot say which of the two is the home (refuse), and when it is itself
#      the dominant leader the row is already right (leave it alone — never
#      repoint away from a class the evidence puts on top);
#   4. at WRITE time, re-read under the same cursor: the row still exists,
#      still names the class the scan measured (an earlier pass in this very
#      run may have moved it), carries no ``manual_override`` sentinel, and
#      the target is still named by no binding row.
#
# Guard 4 is not belt-and-braces: passes 1-3 above run against the same
# cursor before this one and can legitimately change a row the scan observed
# earlier, and the scan's snapshot is by construction older than the write.
#
# What it does NOT do: no Weaviate data is deleted, moved or created. This
# repoints ONE launcher.db row (and records the previous value in its
# ``config_json``). Objects already in the old class stay exactly where they
# are — ``migrate-collections`` remains the separate, user-owned decision.


#: ``config_json`` key recording an automated evidence repoint: the previous
#: collection name, the tag, and when. Deliberately NOT ``manual_override``
#: — that sentinel means "a human chose this" and is the thing every
#: automated pass must not overwrite. An automated writer stamping it would
#: launder machine output as human intent and freeze the row against every
#: future repair, so the audit gets its own key.
EVIDENCE_REPOINT_KEY = "evidence_repoint"
EVIDENCE_REPOINT_TAG = "v0.2.92-evidence-repoint"

#: Why a measured mismatch was NOT healed. Each is a distinct refusal arm with
#: its own red-proof; none of them writes anything.
REFUSE_AMBIGUOUS = "ambiguous_evidence"
REFUSE_MANUAL_OVERRIDE = "manual_override"
REFUSE_ROW_CHANGED = "row_changed_since_scan"
REFUSE_TARGET_BOUND = "target_bound_by_another_row"

#: The DECISIVE-MARGIN rule (v0.2.92). When two or more classes clear the
#: doctor's ownership bar, the heal acts only on a leader that dominates the
#: runner-up on the doctor's OWN ``matched_paths`` measure by BOTH of these:
#:
#:   * more than :data:`EVIDENCE_MARGIN_FACTOR` times as many matched paths;
#:   * more than :data:`EVIDENCE_MARGIN_ABS` matched paths in absolute terms.
#:
#: Both comparisons are STRICT (``>``, never ``>=``): a constant names the
#: margin the evidence must EXCEED, so a hand-set factor of 2.0 is not
#: satisfied by a ratio of exactly 2.0 — at the bar, the answer is "ask".
#:
#: Why two legs. The factor alone would call 3-vs-1 decisive, but three
#: matching paths is barely over the doctor's own floor and one stray sync run
#: could produce it; the absolute leg makes the leader show a real corpus. The
#: absolute leg alone would call 60-vs-50 decisive, where the two classes
#: plainly hold comparable shares of the project. Requiring both is what makes
#: "the leader is where the data lives" the only reading that passes.
#:
#: The ceiling matters and is deliberate: the doctor samples at most
#: ``kg_binding_doctor.SAMPLE_LIMIT`` paths per class, so two classes that both
#: saturate the sample rank equal and can never be decisive. That is the
#: conservative direction — a sample that cannot tell them apart must not be
#: read as a verdict.
EVIDENCE_MARGIN_FACTOR = 2.0
EVIDENCE_MARGIN_ABS = 10

#: The ASK raised when the margin is not met. Registered in
#: ``vco_lib/deferral_conditions.toml`` as ``action_required``: it names a
#: state only a human can settle, so it never resolves on its own.
AMBIGUOUS_EVIDENCE_CID = "kg_binding_ambiguous_evidence"


@dataclass(frozen=True)
class EvidenceRepoint:
    """One primary binding the evidence says should name a different class."""

    project_id: str
    project_name: str
    folder: str
    old_name: str
    new_name: str
    #: Objects in the target class, and the evidence that identified it —
    #: carried verbatim from the doctor's :class:`ClassEvidence` so the
    #: ledger entry can show the reader WHY this class was chosen.
    object_count: int
    matched_paths: int
    sampled_paths: int


@dataclass(frozen=True)
class EvidenceCandidate:
    """One class that cleared the ownership bar, as the refusal reports it.

    A named record rather than a tuple because the ledger entry has to print
    the numbers the DECISION used — ``matched_paths`` first, since that is what
    the margin rule ranks on — and a reader who only sees object counts cannot
    tell how close the call was. Carried verbatim from the doctor's
    :class:`~vco_lib.kg_binding_doctor.ClassEvidence`; nothing here is
    recomputed.
    """

    name: str
    #: Objects in the class (Aggregate count).
    count: int
    #: DISTINCT sampled ``file_path`` values that exist under the project
    #: folder, out of :attr:`sampled_paths` sampled. THE ranking measure.
    matched_paths: int
    sampled_paths: int
    #: True when some binding row (any project, any role) names this class.
    bound: bool


@dataclass(frozen=True)
class EvidenceRefusal:
    """A measured mismatch this pass deliberately did NOT heal."""

    project_id: str
    project_name: str
    bound: str
    reason: str
    #: Every class that cleared the bar, best-evidenced FIRST (the ranking the
    #: decision used). Populated for :data:`REFUSE_AMBIGUOUS`; empty otherwise.
    candidates: tuple[EvidenceCandidate, ...] = ()
    #: The project folder, for a ledger entry that has to be unambiguous about
    #: WHICH project it is naming. Empty when the refusal arm does not have it.
    folder: str = ""


@dataclass(frozen=True)
class EvidenceHealPlan:
    """What the evidence permits — computed before any lock is taken."""

    repoints: tuple[EvidenceRepoint, ...] = ()
    refusals: tuple[EvidenceRefusal, ...] = ()

    @property
    def has_work(self) -> bool:
        """True only when a WRITE is warranted (refusals are not work)."""
        return bool(self.repoints)


def rank_evidence(cleared):
    """The doctor's cleared classes, best-evidenced first — TOTAL and stable.

    ``matched_paths`` descending is the ranking the margin rule compares on:
    it is the measure of how much of THIS project's corpus a class provably
    holds, where ``count`` also counts objects that may belong to nobody here.
    Object count breaks a matched-paths tie (more of the project's data is
    still more), and the class NAME breaks the rest — so the order never
    depends on Weaviate's schema listing order, and two runs on one machine
    cannot reach different verdicts.
    """
    return sorted(cleared, key=lambda e: (-e.matched_paths, -e.count, e.name))


def evidence_is_decisive(top, runner_up) -> bool:
    """True when ``top``'s lead over ``runner_up`` is big enough to act on.

    Both comparisons are STRICT — see :data:`EVIDENCE_MARGIN_FACTOR` /
    :data:`EVIDENCE_MARGIN_ABS`. Reads only numbers the doctor measured.
    """
    return (
        top.matched_paths > runner_up.matched_paths * EVIDENCE_MARGIN_FACTOR
        and top.matched_paths - runner_up.matched_paths > EVIDENCE_MARGIN_ABS
    )


def _as_candidate(e) -> EvidenceCandidate:
    """Doctor :class:`ClassEvidence` → the refusal's reporting record."""
    return EvidenceCandidate(
        name=e.name,
        count=e.count,
        matched_paths=e.matched_paths,
        sampled_paths=e.sampled_paths,
        bound=bool(e.bound),
    )


def plan_evidence_repoints(scan) -> EvidenceHealPlan:
    """PURE decision over :func:`kg_binding_doctor.scan_kg_binding_evidence`.

    Consumes ``ProjectBindingVerdict.evidence`` — the doctor's file-backed
    ownership verdict — and MEASURES nothing of its own. That is the whole
    safety argument (see the block comment above): one calibration, one home,
    so the diagnosis and the repair can never disagree about which class holds
    a project's data.

    One class cleared the bar → that class. Two or more → :func:`rank_evidence`
    orders them and :func:`evidence_is_decisive` asks whether the leader
    dominates; if it does, the leader is treated exactly like a lone candidate,
    and if it does not the project gets a :data:`REFUSE_AMBIGUOUS` refusal
    (nothing written, and the ambiguity is ASKED about — see
    :func:`emit_ambiguous_evidence_entry`).

    In BOTH shapes the identified class is then compared with the bound one:
    equal → healthy, nothing produced. Different → a repoint. So a bound class
    the evidence puts decisively on top is left alone rather than argued with,
    and zero cleared classes stay diagnosed rather than guessed at.
    """
    repoints: list[EvidenceRepoint] = []
    refusals: list[EvidenceRefusal] = []
    for verdict in scan.verdicts:
        # EVERY class that cleared the bar, bound ones included — not just
        # `unbound_evidence`. If the currently-bound class ALSO holds a
        # comparable share of the project's files, the evidence does not say
        # which of the two is the home, and picking the unbound one would be a
        # guess dressed as a measurement. Counting bound classes is what turns
        # that state into the ambiguity it is (and what lets a dominant bound
        # class read as "already right").
        cleared = verdict.evidence
        if not cleared:
            # Nothing cleared the ownership bar: no evidence, no repoint.
            # (The below-bar populations stay diagnosed — an under-evidenced
            # guess is the R38 failure mode this heal exists to avoid.)
            continue
        ranked = rank_evidence(cleared)
        found = ranked[0]
        if len(ranked) > 1 and not evidence_is_decisive(found, ranked[1]):
            refusals.append(
                EvidenceRefusal(
                    project_id=verdict.project_id,
                    project_name=verdict.project_name,
                    bound=verdict.bound,
                    reason=REFUSE_AMBIGUOUS,
                    candidates=tuple(_as_candidate(e) for e in ranked),
                    folder=verdict.folder,
                )
            )
            continue
        if found.name == verdict.bound:
            # Healthy: the binding already names the class the evidence puts
            # on top. Short-circuiting HERE (rather than leaning on the
            # write-time gates) is what keeps a healed machine off the writer
            # lock — an empty plan means install.py never opens launcher.db RW.
            # It is also the bound-class protection: a dominant bound class is
            # never repointed away from, and never re-asked about either.
            continue
        repoints.append(
            EvidenceRepoint(
                project_id=verdict.project_id,
                project_name=verdict.project_name,
                folder=verdict.folder,
                old_name=verdict.bound,
                new_name=found.name,
                object_count=found.count,
                matched_paths=found.matched_paths,
                sampled_paths=found.sampled_paths,
            )
        )
    return EvidenceHealPlan(tuple(repoints), tuple(refusals))


def resolve_evidence_heal_plan(
    *,
    db_path,
    weaviate_url: str,
    existing_classes: set[str],
    log_event: Optional[Callable[..., None]] = None,
    scan_evidence: Optional[Callable[..., object]] = None,
) -> Optional[EvidenceHealPlan]:
    """Read-only: decide whether an evidence repoint is owed, before locking.

    Returns ``None`` — meaning *do nothing at all*, not even a downgrade of
    what is deferred today — whenever the question could not be LOOKED at,
    and short-circuits before the expensive scan when the answer is already
    determined:

    * launcher.db unreadable → ``None``;
    * NO ``*_KnowledgeGraph`` class exists that no binding row names → no
      repoint can be APPLIED, so the scan is skipped. The argument that this
      loses nothing: an applied repoint needs a target that is a
      ``*_KnowledgeGraph`` class (the only family the scan samples) and that
      the write-time gate found named by NO binding row — which is exactly
      the class this test says does not exist. Computed from data already in
      hand (the caller's ``/v1/schema`` set + one read-only DB open), it
      spares the healthy machine one Aggregate and one Get per KG class;
    * the evidence scan itself returned ``None`` (Weaviate unreachable or
      unparsable) → ``None``.

    It also drops the ambiguity ASK for a project whose primary binding row
    carries a ``manual_override`` sentinel: a human has already chosen there,
    the write-time gate refuses to touch such a row anyway, and an entry that
    keeps asking a question its reader has answered is the silting the
    disposition classes exist to prevent. That filter lives HERE, not in
    :func:`plan_evidence_repoints`, because it is a fact about the DB rather
    than about the evidence — the planner stays pure and testable on a scan
    alone. Only the ASK is dropped; a repoint proposed for such a row is still
    produced and still refused at write time (:data:`REFUSE_MANUAL_OVERRIDE`),
    so the one gate that must not weaken keeps its own red proof.

    Never raises: the caller is an install/update step that must exit clean.
    """
    try:
        # ONE read-only open (the sanctioned helper), reading the binding
        # table DIRECTLY rather than through the per-project projection: a
        # binding row whose `projects` row is missing still OWNS its class,
        # and the projection — keyed by project — cannot see it. That is the
        # same table-level question the write-time gate asks, so the cheap
        # precondition and the hard guard cannot disagree.
        from vco_lib.kg_binding_read import config_has_manual_override
        from vco_lib.launcher_db_reader import _open_db_readonly

        conn = _open_db_readonly(db_path)
        if conn is None:
            return None  # launcher.db unreadable — look at nothing, do nothing
        try:
            # The same ONE read also answers "which primary rows has a human
            # already spoken for" — one query, no extra open, no extra IO.
            # Falls back to the pre-v0.2.92 projection if `config_json` is
            # absent (an older schema): the precondition below must behave
            # exactly as it did, and "no overrides known" only ever asks MORE.
            try:
                rows = conn.execute(
                    "SELECT collection_name, project_id, role, config_json "
                    "FROM project_kg_bindings"
                ).fetchall()
            except Exception:  # noqa: BLE001 — older schema, narrower read
                rows = [
                    (r[0], "", "", None)
                    for r in conn.execute(
                        "SELECT collection_name FROM project_kg_bindings"
                    ).fetchall()
                ]
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        bound_anywhere = {row[0] for row in rows if row[0]}
        manual_override_projects = {
            pid for (_cn, pid, role, cfg) in rows
            if pid and role == "primary" and config_has_manual_override(cfg)
        }
        if not any(
            isinstance(cls, str)
            and cls.endswith("_KnowledgeGraph")
            and cls not in bound_anywhere
            for cls in existing_classes
        ):
            return None

        if scan_evidence is None:
            from vco_lib.kg_binding_doctor import scan_kg_binding_evidence

            scan = scan_kg_binding_evidence(
                db_path=db_path, weaviate_url=weaviate_url,
            )
        else:
            scan = scan_evidence(db_path=db_path, weaviate_url=weaviate_url)
        if scan is None:
            # Probe failure is not evidence. Emit nothing, change nothing.
            return None
        plan = plan_evidence_repoints(scan)
        if manual_override_projects and plan.refusals:
            kept = tuple(
                r for r in plan.refusals
                if not (
                    r.reason == REFUSE_AMBIGUOUS
                    and r.project_id in manual_override_projects
                )
            )
            if len(kept) != len(plan.refusals):
                plan = EvidenceHealPlan(repoints=plan.repoints, refusals=kept)
    except Exception as exc:  # noqa: BLE001 — a read must never break --update
        if log_event is not None:
            log_event(
                "7e/10", "warn",
                f"D18 evidence-heal probe failed ({type(exc).__name__}); "
                "no binding was touched",
                data={"error": str(exc)[:200]},
            )
        return None
    if log_event is not None and (plan.repoints or plan.refusals):
        log_event(
            "7e/10", "ok",
            f"[kg-heal] D18 evidence plan: {len(plan.repoints)} repoint(s), "
            f"{len(plan.refusals)} refused",
            data={
                "repoints": [
                    {"project": r.project_name, "from": r.old_name,
                     "to": r.new_name, "objects": r.object_count}
                    for r in plan.repoints
                ],
                "refusals": [
                    {"project": r.project_name, "bound": r.bound,
                     "reason": r.reason,
                     "candidates": [
                         {"name": c.name, "objects": c.count,
                          "matched_paths": c.matched_paths,
                          "sampled_paths": c.sampled_paths}
                         for c in r.candidates
                     ]}
                    for r in plan.refusals
                ],
            },
        )
    return plan


def emit_ambiguous_evidence_entry(
    deferral_report, *, plan: Optional[EvidenceHealPlan], deferral_entry_cls,
) -> bool:
    """Raise the :data:`AMBIGUOUS_EVIDENCE_CID` ask for every refused project.

    Returns True when an entry was added.

    WHY THIS IS CALLED FROM THE PLAN SITE, not from the RW heal pass beside
    the repoint entry. An ambiguous project owes NO write, so
    :attr:`EvidenceHealPlan.has_work` is false, ``needs_rebind`` stays false in
    install.py's caller, and the RW pass is never entered — that is the Bug-N
    discipline (a machine with nothing to write must not touch the writer lock
    the hub holds). An emit sited next to ``kg_binding_evidence_repointed``
    would therefore be INERT in exactly the case it exists for: it would fire
    only on machines that happened to owe some unrelated rebind. The read-only
    plan is the earliest point at which the answer is known and the last point
    at which every ambiguous machine is still on the path.

    The caller is the ONE install-run site that resolves the plan, so the
    entry is added at most once per run (and ``add_entry`` is last-write-wins
    per condition_id in any case).
    """
    if plan is None:
        return False
    refused = [r for r in plan.refusals if r.reason == REFUSE_AMBIGUOUS]
    if not refused:
        return False

    def _marker(candidate, bound_name: str) -> str:
        """Which of the three positions a candidate is in — all decision-
        relevant: the bound one is what the project reads today, an unbound one
        is takeable, and one another binding row already names is NOT (pointing
        two projects at one class is refused at write time, so offering it as a
        remedy would propose a state VCO declines to create)."""
        if candidate.name == bound_name:
            return "  [currently bound — what this project reads today]"
        if candidate.bound:
            return "  [named by another project's binding — not available]"
        return "  [unbound]"

    project_blocks: list[str] = []
    sql_lines: list[str] = []
    for r in refused:
        top = r.candidates[0] if r.candidates else None
        runner = r.candidates[1] if len(r.candidates) > 1 else None
        listing = "\n".join(
            f"      - `{c.name}` — {c.matched_paths}/{c.sampled_paths} sampled "
            f"file paths exist under the project folder, {c.count} object(s)"
            + _marker(c, r.bound)
            for c in r.candidates
        )
        margin = ""
        if top is not None and runner is not None:
            margin = (
                f"\n    Leading margin {top.matched_paths} vs "
                f"{runner.matched_paths} matched path(s): to act automatically "
                f"VCO needs BOTH more than "
                f"{runner.matched_paths * EVIDENCE_MARGIN_FACTOR:g} "
                f"(x{EVIDENCE_MARGIN_FACTOR:g}) and more than "
                f"{runner.matched_paths + EVIDENCE_MARGIN_ABS} "
                f"(+{EVIDENCE_MARGIN_ABS})."
            )
        project_blocks.append(
            f"  * project '{r.project_name}'"
            + (f" ({r.folder})" if r.folder else "")
            + f": the primary binding names `{r.bound}`; classes holding this "
            f"project's data:\n{listing}{margin}"
        )
        for c in r.candidates:
            if c.bound and c.name != r.bound:
                # Another project's binding names it; taking it would give one
                # class two readers — worse than the split being reported.
                continue
            sql_lines.append(
                # json_set MERGES into the existing config_json (R8 nit): a
                # wholesale overwrite would drop the evidence_repoint audit
                # key or a prefix-adopt sentinel the row may already carry.
                f"UPDATE project_kg_bindings SET collection_name = "
                f"'{c.name}', config_json = "
                f"json_set(coalesce(config_json, '{{}}'), "
                f"'$.manual_override', 'user-pick'), "
                f"updated_at = strftime('%s','now') * 1000 "
                f"WHERE project_id = '{r.project_id}' AND role = 'primary';"
            )

    deferral_report.add_entry(
        deferral_entry_cls(
            condition_id=AMBIGUOUS_EVIDENCE_CID,
            title=(
                f"{len(refused)} project(s): KG data is split across several "
                f"collections — pick the one to keep"
            ),
            detected=(
                "For the project(s) below, MORE THAN ONE Weaviate class "
                "demonstrably holds this project's KG objects, and no class "
                "leads by enough for VCO to decide on its own. 'Demonstrably' "
                "is the doctor's ownership bar (at least 2 distinct sampled "
                "`file_path` values existing under the project folder, and at "
                "least 80% of the sample); 'by enough' is the decisive margin "
                f"(more than x{EVIDENCE_MARGIN_FACTOR:g} AND more than "
                f"+{EVIDENCE_MARGIN_ABS} matched paths over the runner-up). "
                "NOTHING was written — the binding rows are exactly as you "
                "left them, and no Weaviate data was moved, copied or "
                "deleted.\n\n"
                + "\n".join(project_blocks)
                + "\n\nWhile the split lasts, the project reads and writes "
                "ONLY the collection its binding names: the objects in the "
                "other class are still on disk but no search reaches them."
            ),
            why_deferred=(
                "Ambiguity is not a tie to break. Re-pointing a binding at a "
                "class that holds only part of the project's corpus would "
                "move its reads and writes onto the smaller half while "
                "reporting success — the shape R38 rejected — so the heal "
                "acts only when one class dominates and asks otherwise.\n\n"
                "Nothing VCO runs can decide which collection you want, so "
                "this one will not resolve on its own. It is not immortal "
                "either: the next `python install.py --update` re-checks and "
                "drops it once ANY of these is true — the evidence stops "
                "being ambiguous (one class dominates, e.g. after merging or "
                "re-syncing), the leftover class is emptied or removed, or "
                "the binding row carries a `manual_override` sentinel "
                "recording that a human chose deliberately."
            ),
            command_to_apply=(
                "# 1. Decide which collection this project should read and "
                "write, and set it in the launcher:\n"
                "#      Projects -> <project> -> Identity / KG tab -> primary "
                "KG collection.\n"
                "#    Saving a DIFFERENT collection there records your pick "
                "on the binding row\n"
                "#    (config_json `manual_override`), and the next "
                "`install.py --update` drops\n"
                "#    this entry.\n"
                "# 2. Merge the leftover class into the one you kept, so the "
                "split (and this entry) ends:\n"
                "#      python -m vco_lib.project_init migrate-collections "
                "--help\n"
                "# 3. Re-check what each project now reads and writes:\n"
                "#      vco doctor\n"
                + (
                    "#\n"
                    "# No launcher at hand — or your pick keeps the "
                    "collection the binding\n"
                    "# already names (a same-name save writes no sentinel)? "
                    "Record the pick on the\n"
                    "# binding row yourself — the 'a human chose this' "
                    "sentinel every automated\n"
                    "# pass honours. Pick ONE line per project and run it "
                    "against your\n"
                    "# launcher.db (default: ~/.vct/launcher.db):\n"
                    + "\n".join(sql_lines)
                    if sql_lines else ""
                )
            ),
            severity="warning",
            kg_node_refs=[],
        )
    )
    return True


def _evidence_repoint_pass(
    cur, *, plan: EvidenceHealPlan,
) -> tuple[list[EvidenceRepoint], list[EvidenceRefusal]]:
    """Apply the plan's repoints, re-checking every DB-side gate live.

    The plan was computed from a read-only snapshot taken BEFORE the writer
    lock, and passes 1-3 of this run may have moved a row since. Each gate is
    therefore re-evaluated under this cursor; a miss appends a refusal and
    writes nothing.

    Returns ``(applied, refusals)``.
    """
    from vco_lib.kg_binding_read import config_has_manual_override

    applied: list[EvidenceRepoint] = []
    refusals: list[EvidenceRefusal] = []
    if not plan.repoints:
        return applied, refusals

    # Live ownership: every class ANY binding row names, at write time. The
    # scan already excluded classes bound by another project (that is what
    # `ClassEvidence.bound` means), but the scan is older than this cursor —
    # and "never point two projects at one class" is a hard constraint, not a
    # best effort.
    cur.execute("SELECT collection_name FROM project_kg_bindings")
    bound_anywhere = {row[0] for row in cur.fetchall() if row[0]}

    for repoint in plan.repoints:
        cur.execute(
            "SELECT collection_name, config_json FROM project_kg_bindings "
            "WHERE project_id = ? AND role = 'primary'",
            (repoint.project_id,),
        )
        row = cur.fetchone()
        if row is None or (row[0] or "") != repoint.old_name:
            refusals.append(
                EvidenceRefusal(
                    project_id=repoint.project_id,
                    project_name=repoint.project_name,
                    bound=repoint.old_name,
                    reason=REFUSE_ROW_CHANGED,
                    folder=repoint.folder,
                )
            )
            continue
        config_json = row[1]
        if config_has_manual_override(config_json):
            # A human's deliberate pick. Never overwritten by automation —
            # the same promise the launcher's boot sweep makes.
            refusals.append(
                EvidenceRefusal(
                    project_id=repoint.project_id,
                    project_name=repoint.project_name,
                    bound=repoint.old_name,
                    reason=REFUSE_MANUAL_OVERRIDE,
                    folder=repoint.folder,
                )
            )
            continue
        if repoint.new_name in bound_anywhere:
            refusals.append(
                EvidenceRefusal(
                    project_id=repoint.project_id,
                    project_name=repoint.project_name,
                    bound=repoint.old_name,
                    reason=REFUSE_TARGET_BOUND,
                    folder=repoint.folder,
                )
            )
            continue

        try:
            cfg = json.loads(config_json) if config_json else {}
            if not isinstance(cfg, dict):
                cfg = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            cfg = {}
        now_ms = int(time.time() * 1000)
        # The reversal record. NOT `manual_override` (see EVIDENCE_REPOINT_KEY):
        # this is machine output, and labelling it as a human pick would both
        # lie and freeze the row against every later repair.
        cfg[EVIDENCE_REPOINT_KEY] = {
            "from": repoint.old_name,
            "to": repoint.new_name,
            "tag": EVIDENCE_REPOINT_TAG,
            "at_ms": now_ms,
            "evidence": {
                "objects": repoint.object_count,
                "matched_paths": repoint.matched_paths,
                "sampled_paths": repoint.sampled_paths,
            },
        }
        cur.execute(
            "UPDATE project_kg_bindings "
            "SET collection_name = ?, config_json = ?, updated_at = ? "
            "WHERE project_id = ? AND role = 'primary'",
            (repoint.new_name, json.dumps(cfg), now_ms, repoint.project_id),
        )
        # A second project in the same plan can no longer claim this class.
        bound_anywhere.add(repoint.new_name)
        applied.append(repoint)

    return applied, refusals


def rebind_orchestrator_root_bindings(
    cur,
    *,
    project_id: str,
    canonical: str,
    now_ms: Optional[int] = None,
) -> None:
    """Rebind the orchestrator-root project's ``primary`` + ``shared`` KG
    binding rows to ``canonical`` (X-1 single-writer home).

    This is the canonical-casing rebind install.py runs when it detects the
    orchestrator-root install's shared-KG class settled on a canonical name
    different from the stored binding (V44-I). It was previously inline SQL
    in ``install.py``; moved here so ALL ``project_kg_bindings`` mutations
    live in one module.

    The caller owns the connection + commit (install.py takes an advisory
    lock around the whole install-state mutation block, of which this is one
    step, and commits after). ``now_ms`` defaults to the current epoch-ms.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    for role in ("primary", "shared"):
        cur.execute(
            "UPDATE project_kg_bindings SET collection_name = ?, updated_at = ? "
            "WHERE project_id = ? AND role = ?",
            (canonical, now_ms, project_id, role),
        )


#: app_state keys R8 converges. `orchestrator_root_kg_collection` is the pointer
#: `populate_kg_collection_access_for_project` reads (access.rs:1529);
#: `last_installed_shared_kg_collection` is the ACTUAL canonical shared name
#: install.py last seeded. On a white-label/rebind install the second changes
#: while the first stays at the machine default → the access seeder mints rows
#: for a class that doesn't exist in Weaviate.
_APP_STATE_ORCH_ROOT_KG = "orchestrator_root_kg_collection"
_APP_STATE_LAST_SHARED_KG = "last_installed_shared_kg_collection"

#: v0.2.92: the Identity-tab picker's app_state key. The Rust command
#: `set_shared_kg_collection_name` (project_identity.rs) writes ONLY this key —
#: it deliberately does not touch the two pointer keys above, and neither does
#: anything else in the convergence path, which is why the drift deferral's
#: pre-fix remedy ("pick in the Identity tab") could be followed and leave the
#: entry standing. `heal_shared_kg_pointer_drift` now reads it as the user's
#: EXPLICIT canonical choice. MUST-MATCH mirrors:
#:   * `vco_lib/config_projection.py::APP_STATE_KEY_SHARED_KG_NAME`
#:   * `launcher/src-tauri/src/commands/project_env_settings.rs::
#:     APP_STATE_KEY_SHARED_KG_NAME`
#:   * `launcher/src-tauri/vct-hub/src/config_api.rs` (app_state_get call)
_APP_STATE_PICKER_SHARED_KG = "shared_kg.collection_name"


def _read_app_state(cur, key: str) -> str:
    """Read one app_state value (stripped), or '' when absent/table-missing."""
    try:
        cur.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            return ""
        raise
    return (row[0] or "").strip() if row else ""


def pointer_drift_needs_rw(ro_cur) -> bool:
    """R8 (v0.2.76): True when :func:`heal_shared_kg_pointer_drift` owes work
    on the two shared-KG app_state pointers.

    Cheap RO probe used by install.py's self-heal detection pass to decide
    whether the RW pass is owed; install.py ORs it with the case-rebind and
    prefix-adoption probes, so this one answers ONLY for the pointer heal. A
    default install has the two keys equal → False → no RW open. Older schemas
    without ``app_state`` / these rows read as absent → equal ('') → False.

    v0.2.92: a RECORDED ``last`` is required, not merely "one of the two is
    set". ``last`` absent is the fresh-machine shape — migration 028 seeds the
    pointer on every new launcher.db while ``last`` is written only by a
    successful KG seed — and since m-R6-4 the heal returns 0 on it without a
    write or a deferral (absence is not disagreement). The gate said True for
    that shape anyway, so a brand-new install opened the writer lock (a 5s
    timeout when the hub holds it) to reach a branch that returns
    immediately. It is a gate claiming work is owed when none is, which is
    the same defect class as a heal that does nothing while its caller
    believes it did. The INVERSE shape — ``ptr`` absent, ``last`` recorded —
    still returns True: the heal converges the pointer onto ``last`` there,
    so work genuinely is owed.
    """
    try:
        ptr = _read_app_state(ro_cur, _APP_STATE_ORCH_ROOT_KG)
        last = _read_app_state(ro_cur, _APP_STATE_LAST_SHARED_KG)
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            return False
        raise
    return bool(last) and ptr != last


def converge_root_pointer_write_side(
    db_path,
    canonical: str,
    *,
    is_root: bool,
    connect_rw: Callable[..., sqlite3.Connection],
    log_event: Callable[..., None],
    now_ms: Optional[int] = None,
) -> bool:
    """R8 (v0.2.76) write-side convergence: set
    ``app_state.orchestrator_root_kg_collection`` (the key the access seeder
    reads) to ``canonical`` when we've just seeded the canonical shared
    collection. ONLY the orchestrator-ROOT install may write it (``is_root``);
    a per-project install must never repoint it. Soft-fails; idempotent (same
    value → no-op UPSERT). Returns True when a write was issued.
    """
    canonical = (canonical or "").strip()
    if not canonical or not is_root:
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    try:
        conn = connect_rw(db_path, label="R8-pointer")
        try:
            conn.execute(
                "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (_APP_STATE_ORCH_ROOT_KG, canonical, now_ms),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — soft-fail (telemetry-class write)
        return False
    log_event(
        "orchestrator_root_kg_collection", "ok",
        f"write-side convergence: pointer set to canonical shared collection "
        f"{canonical!r} (R8)",
        data={"value": canonical},
    )
    return True


def _rewrite_seed_authored_access_rows(
    cur,
    dead_rows,
    *,
    dead_name: str,
    canonical: str,
    log_event: Callable[..., None],
) -> tuple[int, int]:
    """Rewrite SEED-AUTHORED ``kg_collection_access`` rows from ``dead_name`` to
    ``canonical``, merging on PK conflict.

    ``dead_rows`` is an iterable of ``(project_id, access_level, created_at,
    updated_at)`` tuples (the rows currently at ``dead_name``). Only rows with
    ``created_at == updated_at`` (the migration-029 seed-authored predicate) are
    rewritten; user-configured rows are LEFT untouched and reported. On PK
    conflict with an existing ``(project_id, canonical)`` row, the HIGHER
    privilege is kept and the dead row deleted (never lowers privilege).

    Single home for the R8 rewrite/merge machinery — shared by BOTH the
    ``ptr != last`` convergence branch and the ``ptr == last`` dead-row sweep
    (v0.2.77 Part 2). Returns ``(rewritten_count, user_configured_left)``.
    """
    rewritten = 0
    user_configured_left = 0
    for proj_id, level, created_at, updated_at in dead_rows:
        # ACKNOWLEDGED LIMITATION (a) — R8/destructive-lens, accepted:
        # `created_at == updated_at` is a heuristic for "seed-authored, never
        # user-touched". It CANNOT distinguish a genuinely seed row from a FRESH
        # GUI grant the user made but never re-edited (both have equal
        # timestamps). This is an inherited blind spot: it fires only under the
        # narrow drift / dead-row paths that reach this heal, and even then it
        # never LOWERS privilege (the conflict branch keeps the higher of the
        # two access levels). So a misclassified fresh grant is, at worst,
        # rewritten to the canonical collection name with its access preserved —
        # not a privilege downgrade. Accepted; not worth a schema change.
        if created_at != updated_at:
            # User-configured row — never auto-rewrite; report it.
            user_configured_left += 1
            log_event(
                "7e/10", "warn",
                "[kg-heal] leaving user-configured kg_collection_access row at "
                f"dead collection {dead_name!r} (project={proj_id}); rewrite skipped",
                data={"project_id": proj_id, "dead": dead_name, "canonical": canonical},
            )
            continue
        # Seed-authored → rewrite to canonical. Handle PK (project_id,
        # collection_name) conflict with an existing (proj, canonical) row:
        # keep the HIGHER privilege, delete the dead row.
        cur.execute(
            "SELECT access_level FROM kg_collection_access "
            "WHERE project_id = ? AND collection_name = ?",
            (proj_id, canonical),
        )
        existing = cur.fetchone()
        if existing is not None:
            keep = max(
                (level, existing[0]),
                key=lambda lv: _KG_ACCESS_RANK.get(lv, 0),
            )
            # ACKNOWLEDGED LIMITATION (b) — R8/destructive-lens, accepted:
            # this stamps the DEAD row's `updated_at` onto the SURVIVING (proj,
            # canonical) row. If the survivor was itself a user-configured grant
            # (created_at != updated_at), copying the dead row's timestamp can
            # coincidentally make created_at == updated_at, flipping the
            # survivor's "user-configured" flag to "seed-authored" for future
            # heals. That is metadata drift only — the access_level is preserved
            # (higher of the two) and no privilege is lost. Accepted as a
            # cosmetic edge of the merge; not worth carrying the survivor's
            # original created_at through just to preserve the flag.
            cur.execute(
                "UPDATE kg_collection_access SET access_level = ?, updated_at = ? "
                "WHERE project_id = ? AND collection_name = ?",
                (keep, updated_at, proj_id, canonical),
            )
            cur.execute(
                "DELETE FROM kg_collection_access "
                "WHERE project_id = ? AND collection_name = ?",
                (proj_id, dead_name),
            )
        else:
            # No conflict — preserve the seed-authored timestamps so the row
            # stays seed-authored (created_at == updated_at) under the canonical
            # name.
            cur.execute(
                "UPDATE kg_collection_access SET collection_name = ? "
                "WHERE project_id = ? AND collection_name = ?",
                (canonical, proj_id, dead_name),
            )
        rewritten += 1
    return rewritten, user_configured_left


def _sweep_dead_root_own_primary_rows(
    cur,
    *,
    canonical: str,
    existing_classes: set[str],
    log_event: Callable[..., None],
    now_ms: int,
) -> int:
    """v0.2.77 Part 2 (5b) — dead-row sweep for the orchestrator-root project.

    Even when the shared-KG pointer is fully converged (``ptr == last`` and the
    canonical class is live), a stale ``kg_collection_access`` row can survive
    for the ORCHESTRATOR-ROOT project: pre-fix launcher builds seeded the root's
    OWN-PRIMARY row as ``sanitize(ORCHESTRATOR_ROOT_NAME)_KnowledgeGraph`` (the
    literal ``VibeCodedOrchestrator_KnowledgeGraph``), which is DEAD on any
    install whose canonical class was re-pointed. The R8 convergence branch
    can't reach it (that branch only fires on ``ptr != last``). The Rust
    root-cause fix stops NEW dead rows being seeded, but an already-installed
    machine keeps the old row until this sweep heals it.

    Scope — root project ONLY, and only the EXACT known-dead literal
    (v0.2.77 L2-1): this heals rows belonging to the
    ``host='orchestrator_root'`` project whose ``collection_name`` equals
    ``_dead_root_own_primary_name()`` (``VibeCodedOrchestrator_KnowledgeGraph``)
    AND is NOT a live Weaviate class. A root-project cross-project GRANT to a
    peer collection that is merely absent from Weaviate at heal time (e.g. a
    not-yet-bootstrapped ``ClientA_KnowledgeGraph``), and a regular per-project
    own collection legitimately absent from Weaviate, are the LEAVE-ALONE
    cases — neither is the known-dead literal, so neither is ever swept. The
    canonical name is derived from the (already agreeing) pointer, not from any
    project name.

    Read-only against Weaviate (membership test on the caller-supplied
    ``existing_classes`` snapshot only). Idempotent: after the sweep the root's
    dead rows are gone, so a second run finds none and returns 0. Returns the
    number of access rows changed.
    """
    # The canonical class MUST be live before we rewrite anything onto it —
    # otherwise the sweep would move rows onto ANOTHER dead name. The caller
    # gates on this too, but re-check defensively (conservative default).
    if canonical not in existing_classes:
        return 0

    # v0.2.77 L2-1: the ONLY dead literal this sweep is allowed to touch is the
    # root's own-primary name the pre-fix launcher seeded
    # (``VibeCodedOrchestrator_KnowledgeGraph``). Matching any dead
    # ``*_KnowledgeGraph`` row would ALSO consume a root-project cross-project
    # GRANT to a peer collection that happens to be absent from Weaviate at
    # heal time (e.g. a not-yet-bootstrapped ClientA_KnowledgeGraph) — silently
    # deleting an access-matrix grant with no undo trail. Restrict to the exact
    # known-dead literal so a genuine grant is never swept.
    dead_literal = _dead_root_own_primary_name()
    if dead_literal == canonical:
        # Degenerate: the canonical class IS the historical dead literal (a
        # default install that never re-pointed) — there is nothing dead to
        # sweep (a row already at `canonical` is skipped below anyway).
        return 0

    # Identify the orchestrator-root project id(s). host is the authoritative
    # signal (mirrors the Rust `is_orchestrator_root_structural_row` predicate).
    try:
        cur.execute("SELECT id FROM projects WHERE host = 'orchestrator_root'")
        root_ids = [r[0] for r in cur.fetchall() if r and r[0]]
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower() or "no such column" in str(oe).lower():
            return 0
        raise
    if not root_ids:
        return 0

    changed = 0
    for root_id in root_ids:
        try:
            cur.execute(
                "SELECT collection_name, access_level, created_at, updated_at "
                "FROM kg_collection_access WHERE project_id = ?",
                (root_id,),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError as oe:
            if "no such table" in str(oe).lower():
                return changed
            raise

        # A dead row is: seed-authored, NOT already the canonical name, and
        # NOT a live Weaviate class. Group by dead collection_name so the
        # shared rewrite/merge helper handles each dead name's PK conflicts.
        by_dead: dict[str, list] = {}
        for coll, level, created_at, updated_at in rows:
            if coll == canonical:
                continue  # already canonical — nothing to do
            if coll in existing_classes:
                continue  # a live class (e.g. own _Development) — leave alone
            # v0.2.77 L2-1: ONLY the exact known-dead own-primary literal is
            # swept. A root-project cross-project GRANT to a peer collection
            # that is merely absent from Weaviate right now (e.g. a
            # not-yet-bootstrapped ClientA_KnowledgeGraph) is NOT this literal
            # and is left untouched — matching any `*_KnowledgeGraph` would
            # silently delete such a grant. The root's own `_Development` row
            # is likewise not this literal.
            if coll != dead_literal:
                continue
            by_dead.setdefault(coll, []).append(
                (root_id, level, created_at, updated_at)
            )

        for dead_name, dead_rows in by_dead.items():
            rewritten, left = _rewrite_seed_authored_access_rows(
                cur,
                dead_rows,
                dead_name=dead_name,
                canonical=canonical,
                log_event=log_event,
            )
            if rewritten:
                log_event(
                    "7e/10", "ok",
                    f"[kg-heal] swept {rewritten} dead orchestrator-root own-primary "
                    f"kg_collection_access row(s) {dead_name!r} -> {canonical!r} "
                    "(pointer already converged)",
                    data={
                        "rewritten": rewritten,
                        "user_configured_left": left,
                        "dead": dead_name,
                        "canonical": canonical,
                        "project_id": root_id,
                    },
                )
            changed += rewritten
    return changed


def _converge_on_explicit_pick(
    cur,
    *,
    picker: str,
    ptr: str,
    last: str,
    log_event: Callable[..., None],
    now_ms: int,
) -> int:
    """v0.2.92 remedy fix: converge the shared-KG pointers onto the
    Identity-tab pick.

    The pick (``app_state['shared_kg.collection_name']``, written by the
    launcher's ``set_shared_kg_collection_name``) is the user's EXPLICIT
    canonical choice — Priority 1 in every SHARED_KG_COLLECTION resolver
    (launcher ``populate()``, hub ``config_api``, Python
    ``config_projection``). It therefore preempts the triple-agreement
    heuristic, which exists only to GUESS the canonical in the pick's
    absence. Callers must have verified ``picker in existing_classes``
    (we never converge onto a class that is not live in Weaviate).

    Three writes, all required for the convergence to SURVIVE the next
    install run (any one of them missing re-creates the drift):

      1. ``orchestrator_root_kg_collection`` := pick — the key the access
         seeder reads; the point of the heal.
      2. ``last_installed_shared_kg_collection`` := pick — install.py's
         seed-step snapshot. Without this the ``ptr != last`` probe still
         reads divergent and the deferral is re-emitted on the next run.
         (The heal writing install.py's snapshot key is deliberate: the
         snapshot records "the canonical the machine last settled on",
         and an explicit pick is exactly that.)
      3. the orchestrator-root ``role='shared'`` binding row := pick —
         UPDATE only, never INSERT (the launcher Rust is the creator of
         binding rows; this module only heals existing ones). Without
         this the next install run's seed step re-derives
         ``last`` from the stale binding and resurrects the drift.

    Seed-authored ``kg_collection_access`` rows at the dead pointer name
    are rewritten to the pick via the shared rewrite/merge helper (same
    predicate and PK-conflict rules as the triple-agreement branch).

    Returns the number of rows changed.
    """
    changed = 0

    # 1 + 2. Both pointer keys converge onto the pick.
    for key in (_APP_STATE_ORCH_ROOT_KG, _APP_STATE_LAST_SHARED_KG):
        cur.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, picker, now_ms),
        )
        changed += 1

    # 3. The root's role='shared' binding follows the pick. Only rows that
    # exist AND differ move; NULL/absent rows are left for the launcher's
    # creator path (never INSERTed here — single-writer contract).
    try:
        cur.execute("SELECT id FROM projects WHERE host = 'orchestrator_root'")
        root_ids = [r[0] for r in cur.fetchall() if r and r[0]]
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower() or "no such column" in str(oe).lower():
            root_ids = []
        else:
            raise
    binding_rebinds = 0
    for root_id in root_ids:
        cur.execute(
            "UPDATE project_kg_bindings SET collection_name = ?, updated_at = ? "
            "WHERE project_id = ? AND role = 'shared' "
            "AND collection_name IS NOT NULL AND collection_name != ?",
            (picker, now_ms, root_id, picker),
        )
        if cur.rowcount:
            binding_rebinds += cur.rowcount
            changed += cur.rowcount
    if binding_rebinds:
        log_event(
            "7e/10", "ok",
            f"[kg-heal] root role='shared' binding row(s) followed the "
            f"Identity pick -> {picker!r} ({binding_rebinds} row(s))",
            data={"picker": picker, "rebinds": binding_rebinds},
        )

    # Rewrite seed-authored access rows at the dead pointer name (same
    # rules as the triple-agreement convergence below).
    dead_ptr = ptr if ptr != picker else None
    rewritten = 0
    user_configured_left = 0
    if dead_ptr:
        try:
            cur.execute(
                "SELECT project_id, access_level, created_at, updated_at "
                "FROM kg_collection_access WHERE collection_name = ?",
                (dead_ptr,),
            )
            dead_rows = cur.fetchall()
        except sqlite3.OperationalError as oe:
            if "no such table" in str(oe).lower():
                dead_rows = []
            else:
                raise
        rewritten, user_configured_left = _rewrite_seed_authored_access_rows(
            cur,
            dead_rows,
            dead_name=dead_ptr,
            canonical=picker,
            log_event=log_event,
        )
        changed += rewritten

    log_event(
        "7e/10", "ok",
        f"[kg-heal] converged shared-KG pointers onto the Identity pick: "
        f"ptr={ptr!r} -> {picker!r}, last={last!r} -> {picker!r} "
        "(explicit user choice; access rows rewritten "
        f"{rewritten}, user-configured left {user_configured_left})",
        data={
            "old_ptr": ptr, "old_last": last, "picker": picker,
            "access_rewritten": rewritten,
            "user_configured_left": user_configured_left,
        },
    )
    return changed


def heal_shared_kg_pointer_drift(
    cur,
    *,
    existing_classes: set[str],
    log_event: Callable[..., None],
    deferral_report,
    deferral_entry_cls,
    now_ms: Optional[int] = None,
) -> int:
    """R8 (v0.2.76): converge ``orchestrator_root_kg_collection`` to the real
    canonical shared collection when it has drifted, and rewrite the stale
    ``kg_collection_access`` rows that point at the dead name.

    launcher.db-metadata ONLY: NO Weaviate object writes, NO sync enqueues, NO
    ``embed_revision`` changes. ``existing_classes`` is a read-only snapshot of
    the Weaviate schema (already fetched by the caller); the only Weaviate
    interaction anywhere in this heal is schema-existence membership, done by
    the caller.

    Convergence is DIVERGENCE-based with TRIPLE agreement (conservative):

      * ``ptr == last`` (keys agree) → strict no-op. This is the default-install
        path: both hold the machine default → nothing to do (leave-alone).
      * ``ptr != last`` AND the Identity-tab pick
        (``app_state['shared_kg.collection_name']``) names a LIVE Weaviate
        class → converge onto the pick (v0.2.92 remedy fix). The pick is the
        user's EXPLICIT canonical choice — it preempts the triple-agreement
        heuristic, which exists only to guess in the pick's absence. See
        :func:`_converge_on_explicit_pick`. (Before this leg, the deferral's
        printed remedy named the picker although no code in the convergence
        path read the picker's key — a remedy that could be followed and
        leave the entry standing.)
      * ``ptr != last`` AND all three agree:
          (1) ``last`` is non-empty AND RECORDED (v0.2.92 m-R6-4: an absent
              ``last`` returns early below — see the absence rule),
          (2) ``last``'s class EXISTS in Weaviate,
          (3) ``last`` equals the consensus of ``role='shared'`` collection
              names in ``project_kg_bindings`` (all shared rows name the same
              collection, and it equals ``last``) — OR there are NO shared
              rows at all
        → converge the pointer to ``last`` and rewrite stale
        ``kg_collection_access`` rows (see below).
      * ``ptr != last`` WITHOUT that agreement → touch nothing; emit an
        ``UPDATE_DEFERRED``-pattern entry so the user resolves it explicitly.
        A recorded pick that names a class NOT live in Weaviate is named in
        the entry's reasons (that is why following the remedy did not clear
        it), and no convergence runs onto a dead class.

    v0.2.92 (m-R6-4) — ABSENCE IS NOT DISAGREEMENT, on both evidence
    sources, because both absences are NORMAL machine states rather than
    drift:

    * ``last`` absent. Migration 028 seeds ``ptr`` with the machine default
      on EVERY fresh launcher.db, while ``last`` is written only by the
      KG-seed step of a successful install run (and that step soft-skips —
      sync subprocess failed to launch, hybrid resolver deferred, Weaviate
      down). ptr-set/last-absent is therefore a brand-new machine's ordinary
      shape, and there is nothing to converge TO and no evidence the pointer
      is wrong: leave-alone, no deferral (previously a nag with no surface
      that could clear it — the Identity-tab picker writes a different key).
    * ZERO ``role='shared'`` binding rows. The orchestrator root binds
      ``role='primary'`` only (``ensure_orchestrator_root_kg_binding``), so
      a root-only machine — a fresh install before a SECOND project is
      registered — has no shared rows for the consensus leg to consult.
      No rows is not "no consensus"; it is no evidence either way, and the
      remaining legs (``last`` recorded, ``last``'s class live) carry the
      convergence. A nonempty set that is NOT a single ``last``-matching
      consensus is still genuine ambiguity and still defers.

    Access-row rewrite: only SEED-AUTHORED rows (``created_at == updated_at``,
    the migration-029 predicate) whose ``collection_name`` equals the dead
    ``ptr`` are rewritten to ``last``; user-configured rows are LEFT and
    reported. On PK conflict with an existing (project, last) row, keep the
    HIGHER privilege and delete the dead row.

    Idempotent: on an already-converged DB ``ptr == last`` and no stale rows
    remain → returns 0 with no writes (safe against the planner's manual
    repair). Returns the number of rows changed (pointer + access rewrites).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    try:
        ptr = _read_app_state(cur, _APP_STATE_ORCH_ROOT_KG)
        last = _read_app_state(cur, _APP_STATE_LAST_SHARED_KG)
    except sqlite3.OperationalError:
        # app_state unreadable (older schema) — nothing to converge.
        return 0

    if not ptr and not last:
        return 0  # nothing recorded yet (pre-seed) — leave-alone.
    if ptr == last:
        # Keys agree — pointer is converged (default install or post-heal).
        # But a dead orchestrator-root OWN-PRIMARY access row can still survive
        # under a converged pointer (v0.2.77 Part 2 5b): pre-fix launcher builds
        # seeded the root's own-primary as the literal
        # `sanitize(ORCHESTRATOR_ROOT_NAME)_KnowledgeGraph`, which is dead on a
        # re-pointed install. The `ptr != last` convergence branch below can't
        # reach it. Run a scoped dead-row sweep (root project only) ONLY when
        # the canonical class is actually live; if it's absent we can't safely
        # rewrite onto it, so leave everything alone. Idempotent: after the
        # sweep the root's dead rows are gone → a second run finds none.
        if ptr and ptr in existing_classes:
            return _sweep_dead_root_own_primary_rows(
                cur,
                canonical=ptr,
                existing_classes=existing_classes,
                log_event=log_event,
                now_ms=now_ms,
            )
        return 0  # canonical class not live (or empty) — leave-alone.

    # Divergent. Require triple agreement before touching anything.
    if not last:
        # v0.2.92 (m-R6-4): absent `last` is not drift — see the absence rule
        # in the docstring. Only a RECORDED canonical can diverge from the
        # pointer; nothing recorded means nothing to converge to and no
        # evidence the pointer is wrong. (Both-absent already returned above;
        # this arm is ptr-set/last-absent, the fresh-DB migration-028 shape.)
        return 0

    # v0.2.92 remedy fix: the Identity-tab pick is the user's EXPLICIT
    # canonical choice, and it preempts the triple-agreement heuristic.
    # The picker command writes ONLY `shared_kg.collection_name` — it does
    # not touch the two keys compared here — so before this leg existed,
    # the deferral's printed remedy ("pick in the Identity tab") could be
    # followed to the letter and leave the entry standing: no code in the
    # convergence path read the pick. When the pick names a LIVE class it
    # IS the resolution: converge the pointer, the seed snapshot and the
    # root's shared binding onto it (see _converge_on_explicit_pick for
    # why all three are required for the convergence to survive the next
    # install run).
    try:
        picker = _read_app_state(cur, _APP_STATE_PICKER_SHARED_KG)
    except sqlite3.OperationalError:
        # app_state unreadable for this key — treat as "no pick recorded".
        picker = ""
    if picker and picker in existing_classes:
        return _converge_on_explicit_pick(
            cur,
            picker=picker,
            ptr=ptr,
            last=last,
            log_event=log_event,
            now_ms=now_ms,
        )

    reasons: list[str] = []
    if picker and picker not in existing_classes:
        # A recorded pick that names a class that is not live cannot be
        # converged onto (the access seeder would seed rows for a
        # nonexistent class). Name it in the reasons so the deferral tells
        # the user WHY following the remedy did not clear the entry.
        reasons.append(
            f"the Identity-tab pick {picker!r} names a class that is not "
            "live in Weaviate"
        )
    if last not in existing_classes:
        reasons.append(
            f"last_installed value {last!r} is not a live Weaviate class"
        )

    shared_consensus: Optional[str] = None
    if not reasons:
        try:
            cur.execute(
                "SELECT DISTINCT collection_name FROM project_kg_bindings "
                "WHERE role = 'shared' AND collection_name IS NOT NULL "
                "AND collection_name != ''"
            )
            shared_names = {r[0] for r in cur.fetchall() if r and r[0]}
        except sqlite3.OperationalError as oe:
            if "no such table" in str(oe).lower():
                shared_names = set()
            else:
                raise
        if len(shared_names) == 1:
            shared_consensus = next(iter(shared_names))
        if shared_consensus is None and shared_names:
            # v0.2.92 (m-R6-4): only a NONEMPTY set without a single
            # consensus is ambiguity. ZERO shared rows is the root-only
            # machine's default state (the root binds role='primary'), and
            # absence is not disagreement — fall through to convergence on
            # the two remaining legs. Pre-fix this arm read "found []" as
            # "no consensus" and nagged a fresh install.
            reasons.append(
                "no single-collection consensus among role='shared' bindings "
                f"(found {sorted(shared_names)})"
            )
        elif shared_consensus is not None and shared_consensus != last:
            reasons.append(
                f"shared-binding consensus {shared_consensus!r} != "
                f"last_installed {last!r}"
            )

    if reasons:
        # Diverge without agreement → touch nothing, defer to the user.
        log_event(
            "7e/10", "warn",
            "[kg-heal] shared-KG pointer drift NOT auto-converged "
            f"(ptr={ptr!r}, last={last!r}): {'; '.join(reasons)}",
            data={"ptr": ptr, "last": last, "reasons": reasons},
        )
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="shared_kg_pointer_drift_unresolved",
                title="Shared-KG canonical pointer diverged (manual review)",
                detected=(
                    f"app_state.orchestrator_root_kg_collection = {ptr!r} but "
                    f"app_state.last_installed_shared_kg_collection = {last!r}. "
                    "The two disagree and the safe-convergence preconditions "
                    f"were not met: {'; '.join(reasons)}. The access seeder "
                    "reads the first key; a wrong value seeds kg_collection_access "
                    "rows for a class that may not exist."
                ),
                why_deferred=(
                    "Auto-converging without triple agreement (last value's "
                    "class exists in Weaviate AND matches the role='shared' "
                    "binding consensus) could re-point the access matrix at the "
                    "wrong collection. Left untouched pending explicit choice."
                ),
                command_to_apply=(
                    "Pick the canonical shared collection in the launcher's "
                    "Identity tab -> 'Manage shared KG collection' (a class "
                    "that already exists in Weaviate), then re-run "
                    "`python install.py --update`. The self-heal reads that "
                    "pick (app_state['shared_kg.collection_name']) and "
                    "converges `orchestrator_root_kg_collection`, "
                    "`last_installed_shared_kg_collection` and the root's "
                    "shared binding onto it, which clears this entry. "
                    "(VCT_ORCHESTRATOR_ROOT_KG_COLLECTION re-points the "
                    "pointer for white-label installs but does not converge "
                    "the other keys; the Identity pick is the remedy this "
                    "entry clears on.)"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
        return 0

    # Triple agreement met → converge the pointer.
    changed = 0
    cur.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (_APP_STATE_ORCH_ROOT_KG, last, now_ms),
    )
    changed += 1
    log_event(
        "7e/10", "ok",
        f"[kg-heal] converged orchestrator_root_kg_collection {ptr!r} -> {last!r}",
        data={"old": ptr, "new": last},
    )

    # Rewrite stale kg_collection_access rows pointing at the dead ptr name.
    # SEED-AUTHORED rows only (created_at == updated_at per migration 029).
    try:
        cur.execute(
            "SELECT project_id, access_level, created_at, updated_at "
            "FROM kg_collection_access WHERE collection_name = ?",
            (ptr,),
        )
        dead_rows = cur.fetchall()
    except sqlite3.OperationalError as oe:
        if "no such table" in str(oe).lower():
            dead_rows = []
        else:
            raise

    rewritten, user_configured_left = _rewrite_seed_authored_access_rows(
        cur,
        dead_rows,
        dead_name=ptr,
        canonical=last,
        log_event=log_event,
    )
    changed += rewritten

    if dead_rows:
        log_event(
            "7e/10", "ok",
            f"[kg-heal] rewrote {rewritten} stale kg_collection_access row(s) "
            f"{ptr!r} -> {last!r}"
            + (f"; left {user_configured_left} user-configured row(s)"
               if user_configured_left else ""),
            data={
                "rewritten": rewritten,
                "user_configured_left": user_configured_left,
                "dead": ptr, "canonical": last,
            },
        )
    return changed


def self_heal_kg_bindings(
    deferral_report,
    *,
    db_path,
    weaviate_url: str,
    existing_classes: set[str],
    existing_by_lower: dict[str, str],
    log_event: Callable[..., None],
    connect_rw: Callable[..., sqlite3.Connection],
    deferral_entry_cls,
    run_adoption_uplifts: Optional[Callable[..., None]] = None,
    evidence_plan: Optional[EvidenceHealPlan] = None,
) -> None:
    """Heal launcher.db KG bindings / access rows against on-disk Weaviate.

    This is the extracted body of install.py's former
    ``_self_heal_kg_bindings_on_update`` RW pass (v0.2.23 B1 + v0.2.40
    cross-prefix adoption + V0243-9 access parity). The caller (install.py
    shim) has already:

      * resolved ``db_path`` (``_discover_app_state_db_path``) and confirmed
        the file exists,
      * fetched the Weaviate schema and built ``existing_classes`` /
        ``existing_by_lower``,
      * run the cheap RO detection pass and decided a rebind is owed.

    So this function opens launcher.db RW (via ``connect_rw``), runs the four
    repair passes, commits, and emits deferral entries. It never raises:
    sqlite errors soft-fail to a ``kg_binding_self_heal_db_error`` deferral;
    the caller keeps a clean ``--update`` exit either way.

    ``deferral_entry_cls`` is the caller's ``DeferralEntry`` dataclass
    (injected to avoid this module importing install.py). ``connect_rw`` is
    the caller's retry-with-backoff connector. ``run_adoption_uplifts``, when
    provided, is the smart-path uplift (W40 RT-13) invoked after prefix
    adoptions.
    """
    import sqlite3

    rebinds: list[tuple[str, str, str, str]] = []  # (proj_id, role, old, new)
    access_rebinds: list[tuple[str, str, str]] = []  # (proj_id, old, new)
    # cross-prefix adoption (second pass) — see _prefix_adopt_kg_bindings_pass.
    # adopts:           (project_id, role, old_name, new_name, row_count)
    # multi_candidates: (project_id, role, old_name, [(cand_name, row_count), ...])
    prefix_adopts: list[tuple[str, str, str, str, int]] = []
    prefix_multi_candidates: list[tuple[str, str, str, list[tuple[str, int]]]] = []
    # D18 evidence-backed primary repoints (v0.2.92) — see
    # `_evidence_repoint_pass`. Empty unless the caller resolved a plan.
    evidence_repoints: list[EvidenceRepoint] = []
    evidence_refusals: list[EvidenceRefusal] = list(
        evidence_plan.refusals if evidence_plan is not None else ()
    )
    try:
        # retry-with-backoff on a transient lock (defense in depth alongside
        # the launcher closing its managed connection + pollers standing
        # down). Exhausted retries re-raise → the outer ``except
        # sqlite3.Error`` below writes the same deferral as before.
        conn = connect_rw(db_path, label="7e/10")
        try:
            cur = conn.cursor()

            # ── 1. project_kg_bindings ────────────────────────────────
            # Natural key (project_id, role) is unaffected by a
            # collection_name rebind, so no conflict-resolver is needed.
            def _bind_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                role = row[2]
                cur.execute(
                    "UPDATE project_kg_bindings "
                    "SET collection_name = ?, updated_at = ? "
                    "WHERE project_id = ? AND role = ?",
                    (new_name, int(time.time() * 1000), project_id, role),
                )
                rebinds.append((project_id, role, old_name, new_name))

            try:
                binding_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="project_kg_bindings",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("role",),
                    do_rebind=_bind_rebind,
                )
            except sqlite3.OperationalError as oe:
                if "no such table" in str(oe).lower():
                    log_event(
                        "7e/10", "skip",
                        "project_kg_bindings table absent; nothing to self-heal",
                    )
                    return
                raise
            rebinds.extend(binding_rebinds)

            # ── 2. kg_collection_access ───────────────────────────────
            # Also rebind ``kg_collection_access`` rows whose
            # ``collection_name`` differs only in case from an on-disk
            # class. Without this, the launcher GUI's Identity tab access
            # matrix would render rows pointing at a class that doesn't
            # exist post-rename (and dangle), and the hub's
            # ``kg_access_list`` construction in config_api would see both
            # the lowercase-c grant AND the (implicit-fallback) capital-C
            # grant — confusing, and a silently-missed
            # ``access_level='none'`` signal if the user had explicitly
            # downgraded the lowercase-c entry.
            #
            # PK collision handling: kg_collection_access PK is
            # (project_id, collection_name). If (p1, "Foo", "read") exists
            # AND (p1, "foo", "write") also exists, a naive rebind would
            # violate the UNIQUE constraint. The helper detects the
            # collision before the UPDATE; on collision we KEEP the
            # higher-privilege row (write > read > none) at the canonical
            # casing and DELETE the lower-privilege duplicate.
            def _access_rebind(cur, *, project_id, old_name, new_name, row, rebinds):
                cur.execute(
                    "UPDATE kg_collection_access "
                    "SET collection_name = ? "
                    "WHERE project_id = ? AND collection_name = ?",
                    (new_name, project_id, old_name),
                )
                rebinds.append((project_id, old_name, new_name))

            def _access_resolve_conflict(
                cur, *, project_id, old_name, new_name,
                current_row, conflict_row, rebinds,
            ):
                # current_row has access_level at index 2 (extra_select_cols).
                # conflict_row likewise.
                current_access = current_row[2]
                conflict_access = conflict_row[2]
                current_rank = _KG_ACCESS_RANK.get(current_access, 0)
                conflict_rank = _KG_ACCESS_RANK.get(conflict_access, 0)
                if current_rank > conflict_rank:
                    # Lowercase-c row is higher-privilege — drop the
                    # canonical-casing duplicate, then rebind.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, new_name),
                    )
                    cur.execute(
                        "UPDATE kg_collection_access "
                        "SET collection_name = ? "
                        "WHERE project_id = ? AND collection_name = ?",
                        (new_name, project_id, old_name),
                    )
                    rebinds.append((project_id, old_name, new_name))
                else:
                    # Canonical row has equal-or-higher privilege. Drop the
                    # lowercase-c row.
                    cur.execute(
                        "DELETE FROM kg_collection_access "
                        "WHERE project_id = ? AND collection_name = ?",
                        (project_id, old_name),
                    )
                    rebinds.append(
                        (project_id, old_name, f"{new_name} (deduped)")
                    )

            try:
                acc_rebinds = _rebind_collection_names_to_on_disk_casing(
                    cur,
                    table="kg_collection_access",
                    project_id_col="project_id",
                    collection_name_col="collection_name",
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    extra_select_cols=("access_level",),
                    do_rebind=_access_rebind,
                    resolve_conflict=_access_resolve_conflict,
                )
                access_rebinds.extend(acc_rebinds)
            except sqlite3.OperationalError as oe:
                # Older launcher.db schemas may not have kg_collection_access.
                # Don't fail the binding heal — just skip the access part.
                if "no such table" not in str(oe).lower():
                    raise

            # ── 3. cross-prefix adoption (SECOND PASS) ─────────────────
            # The case-insensitive sweep above handles the casing-flip
            # scenarios. It explicitly leaves rows alone when the
            # advertised ``collection_name`` doesn't exist in Weaviate AND
            # has no case-different sibling — the "genuine missing-class"
            # branch. This pass covers a different breakage shape: a
            # manual_override cleanup updated the PRIMARY binding to a
            # custom prefix but left the SHARED binding pointing at the
            # canonical release-default prefix, so the advertised shared
            # collection never gets populated and the actual data lives
            # under the per-project prefix. Probe Weaviate for
            # *_KnowledgeGraph / *_Development classes with non-zero row
            # count; exactly ONE candidate → auto-adopt; multiple → defer;
            # zero → no-op.
            try:
                prefix_outcomes = _prefix_adopt_kg_bindings_pass(
                    cur,
                    existing_classes=existing_classes,
                    existing_by_lower=existing_by_lower,
                    weaviate_url=weaviate_url,
                )
                prefix_adopts.extend(prefix_outcomes["adopts"])
                prefix_multi_candidates.extend(
                    prefix_outcomes["multi_candidates"]
                )
            except sqlite3.OperationalError as oe:
                # Already-handled ``no such table`` from the first pass; if
                # we got here that table exists. Re-raise other operational
                # errors — they're real corruption signals.
                if "no such table" not in str(oe).lower():
                    raise

            # ── 3b. D18 evidence-backed PRIMARY repoint (v0.2.92) ─────
            # The case pass 3 skips by construction: the binding's class
            # EXISTS, but file-backed evidence says the project's objects
            # live in a DIFFERENT, unbound class. Eligibility was decided
            # read-only before the lock (`resolve_evidence_heal_plan`) from
            # `kg_binding_doctor`'s ownership rule — never from a
            # name-derived guess (R38) — and every DB-side gate is
            # re-checked here under this cursor. Runs BEFORE the access
            # parity pass below so the newly-named class gets its
            # `kg_collection_access` write row in the same transaction.
            if evidence_plan is not None and evidence_plan.repoints:
                try:
                    _applied, _refused = _evidence_repoint_pass(
                        cur, plan=evidence_plan,
                    )
                    evidence_repoints.extend(_applied)
                    evidence_refusals.extend(_refused)
                except sqlite3.OperationalError as oe:
                    if "no such table" not in str(oe).lower():
                        raise

            # ── 4. V0243-9: kg_collection_access parity self-heal ─────
            #
            # For every row in project_kg_bindings that lacks a matching
            # kg_collection_access row, INSERT-OR-IGNORE the canonical
            # access-level:
            #   role="primary"  → access_level="write"
            #   role="shared"   → access_level="read"
            #   role="archive"  → access_level="read"  (Development)
            #
            # Also backfill the matching _Development collection row: for
            # each primary KG binding whose collection ends in
            # "_KnowledgeGraph", derive the sibling "_Development"
            # collection name and INSERT-OR-IGNORE a "write" row.
            #
            # INSERT-OR-IGNORE is safe: the PK is (project_id,
            # collection_name). We never lower an existing privilege.
            _parity_inserts = 0
            try:
                # Read all existing kg_collection_access rows for lookup.
                cur.execute(
                    "SELECT project_id, collection_name, access_level "
                    "FROM kg_collection_access"
                )
                existing_access: set[tuple[str, str]] = {
                    (r[0], r[1]) for r in cur.fetchall()
                }
                # Read all project_kg_bindings rows.
                cur.execute(
                    "SELECT project_id, role, collection_name "
                    "FROM project_kg_bindings"
                )
                binding_rows = cur.fetchall()

                _ROLE_LEVEL = {"primary": "write", "shared": "read", "archive": "read"}
                # kg_collection_access schema has: project_id,
                # collection_name, access_level, created_at, updated_at.
                # These INSERTs are seed-path writes (the parity self-heal
                # asserts the system's default; they are NOT user-driven),
                # so we bind both timestamps to the SAME value. This
                # preserves the seed-path invariant ``created_at ==
                # updated_at`` so the Rust-side ``KgAccessRow::
                # is_user_configured`` predicate reads FALSE for rows we
                # land here.
                _seed_ts_ms = int(time.time() * 1000)

                for proj_id, role, coll_name in binding_rows:
                    if not coll_name:
                        continue
                    level = _ROLE_LEVEL.get(role, "read")
                    if (proj_id, coll_name) not in existing_access:
                        cur.execute(
                            "INSERT OR IGNORE INTO kg_collection_access "
                            "(project_id, collection_name, access_level, "
                            " created_at, updated_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (proj_id, coll_name, level,
                             _seed_ts_ms, _seed_ts_ms),
                        )
                        if cur.rowcount:
                            existing_access.add((proj_id, coll_name))
                            _parity_inserts += 1

                    # Backfill the sibling _Development collection for
                    # primary bindings.
                    if role == "primary" and coll_name.endswith("_KnowledgeGraph"):
                        dev_name = coll_name[:-len("_KnowledgeGraph")] + "_Development"
                        if (proj_id, dev_name) not in existing_access:
                            cur.execute(
                                "INSERT OR IGNORE INTO kg_collection_access "
                                "(project_id, collection_name, access_level, "
                                " created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?)",
                                (proj_id, dev_name, "write",
                                 _seed_ts_ms, _seed_ts_ms),
                            )
                            if cur.rowcount:
                                existing_access.add((proj_id, dev_name))
                                _parity_inserts += 1

                if _parity_inserts:
                    log_event(
                        "7e/10", "ok",
                        f"V0243-9: inserted {_parity_inserts} missing "
                        f"kg_collection_access row(s) (parity self-heal)",
                        data={"inserts": _parity_inserts},
                    )
            except sqlite3.OperationalError as oe:
                if "no such table" not in str(oe).lower():
                    raise
                # kg_collection_access table absent — older schema; skip.
                log_event(
                    "7e/10", "skip",
                    "V0243-9: kg_collection_access absent; parity self-heal skipped",
                )

            # ── 5. R8 (v0.2.76): shared-KG canonical pointer drift ────
            # Converge app_state.orchestrator_root_kg_collection to the real
            # canonical shared collection + rewrite stale kg_collection_access
            # rows. launcher.db metadata only (no Weaviate writes). Conservative:
            # a default install has ptr == last → no-op; divergence without
            # triple agreement defers instead of guessing.
            _pointer_changed = 0
            try:
                _pointer_changed = heal_shared_kg_pointer_drift(
                    cur,
                    existing_classes=existing_classes,
                    log_event=log_event,
                    deferral_report=deferral_report,
                    deferral_entry_cls=deferral_entry_cls,
                )
            except sqlite3.OperationalError as oe:
                if "no such table" not in str(oe).lower():
                    raise
                log_event(
                    "7e/10", "skip",
                    "R8: app_state absent; shared-KG pointer heal skipped",
                )

            conn.commit()

            # X-1 instrumentation (v0.2.76): count rows this heal pass
            # ACTUALLY changed. This is the KPI that lets a future release
            # demote the heal to assert-only: once ``changed`` stays 0 across
            # releases, the launcher (Rust) creator path is provably keeping
            # the binding rows canonical and the reconciliation pass is dead
            # weight. Reuses the accumulators already tracked above — no new
            # telemetry machinery. ``rebinds`` also feeds the deferral/report
            # blocks below, so this is a pure read of existing state.
            _heal_changed = (
                len(rebinds)
                + len(access_rebinds)
                + len(prefix_adopts)
                + len(evidence_repoints)
                + _parity_inserts
                + _pointer_changed
            )
            log_event(
                "7e/10", "ok",
                f"[kg-heal] changed={_heal_changed} "
                f"(binding_rebinds={len(rebinds)}, "
                f"access_rebinds={len(access_rebinds)}, "
                f"prefix_adopts={len(prefix_adopts)}, "
                f"evidence_repoints={len(evidence_repoints)}, "
                f"access_parity_inserts={_parity_inserts}, "
                f"pointer_drift={_pointer_changed})",
                data={
                    "changed": _heal_changed,
                    "binding_rebinds": len(rebinds),
                    "access_rebinds": len(access_rebinds),
                    "prefix_adopts": len(prefix_adopts),
                    "evidence_repoints": len(evidence_repoints),
                    "access_parity_inserts": _parity_inserts,
                    "pointer_drift": _pointer_changed,
                },
            )
        finally:
            conn.close()
    except sqlite3.Error as se:
        log_event(
            "7e/10", "warn",
            f"launcher.db sqlite error during self-heal: {type(se).__name__}",
            data={"db_path": str(db_path), "error": str(se)[:200]},
        )
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="kg_binding_self_heal_db_error",
                title="Could not self-heal launcher.db KG bindings (sqlite error)",
                detected=(
                    f"Tried to open launcher.db at {db_path} to detect "
                    f"case-mismatched `project_kg_bindings` rows, but the "
                    f"sqlite library raised {type(se).__name__}. The binding "
                    f"rows (if any) were NOT modified."
                ),
                why_deferred=(
                    "The launcher.db file is locked, corrupted, or "
                    "schema-mismatched. Skipping self-heal preserves user "
                    "state; the launcher's own boot path will re-validate "
                    "the schema on next start."
                ),
                command_to_apply=(
                    "Close the launcher if running, then re-run "
                    "`python install.py --update`. If the error persists, "
                    "open the launcher and let it migrate the schema first, "
                    "then re-run the update."
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )
        return

    if (
        not rebinds
        and not access_rebinds
        and not prefix_adopts
        and not prefix_multi_candidates
        and not evidence_repoints
    ):
        log_event(
            "7e/10", "ok",
            "no case-mismatched KG bindings or access rows; self-heal no-op",
        )
        return

    # RT-13: W40-adoption smart-path uplift.
    #
    # After each binding flip has been committed, run the
    # migrate_collections smart-path against every newly-adopted collection
    # to detect schema drift between the legacy-named collection and the
    # current orchestrator target schema. Applied BEFORE the deferral-entry
    # block so any rebuild deferrals are merged into the same
    # UPDATE_DEFERRED.md write. Soft-fail: migrate errors become deferral
    # entries; the binding flip already committed so this step cannot roll
    # back the adoption.
    if prefix_adopts and run_adoption_uplifts is not None:
        run_adoption_uplifts(
            prefix_adopts=prefix_adopts,
            weaviate_url=weaviate_url,
            deferral_report=deferral_report,
            db_path=db_path,
        )

    # Emit deferral entries. Case-rebinds + prefix-adopts share an
    # informational ``kg_binding_self_healed`` entry because both are
    # auto-applied metadata fixes (the target class exists in Weaviate
    # before we point the binding at it). Multi-candidate prefix situations
    # get a SEPARATE ``multi_candidate_prefix_adopt`` entry with warning
    # severity — they require user input.
    binding_count = len(rebinds)
    access_count = len(access_rebinds)
    prefix_adopt_count = len(prefix_adopts)
    if rebinds or access_rebinds or prefix_adopts:
        rebind_lines = "\n".join(
            f"  * project_id={pid} role={role}: `{old}` → `{new}`"
            for (pid, role, old, new) in rebinds
        )
        access_rebind_lines = "\n".join(
            f"  * project_id={pid}: `{old}` → `{new}`"
            for (pid, old, new) in access_rebinds
        )
        prefix_adopt_lines = "\n".join(
            f"  * project_id={pid} role={role}: `{old}` → `{new}` "
            f"(adopted populated class with {cnt} object(s))"
            for (pid, role, old, new, cnt) in prefix_adopts
        )
        title_parts = []
        if binding_count:
            title_parts.append(f"{binding_count} case-rebound binding(s)")
        if access_count:
            title_parts.append(f"{access_count} access row(s)")
        if prefix_adopt_count:
            title_parts.append(
                f"{prefix_adopt_count} cross-prefix adopted binding(s)"
            )
        title = (
            f"Self-healed {' + '.join(title_parts)} in launcher.db KG metadata"
        )
        detected_parts = []
        if binding_count:
            detected_parts.append(
                f"Found {binding_count} `project_kg_bindings` row(s) whose "
                f"`collection_name` differed only in casing from a class that "
                f"exists in Weaviate at {weaviate_url}.\n\n"
                f"Rebound binding rows:\n{rebind_lines}"
            )
        if access_count:
            detected_parts.append(
                f"Found {access_count} `kg_collection_access` row(s) whose "
                f"`collection_name` differed only in casing from a class that "
                f"exists in Weaviate (sibling rows to the binding rebinds). "
                f"These were updated in place to keep the launcher GUI's "
                f"per-project Identity tab access matrix pointing at the live "
                f"class. Rows annotated `(deduped)` were merged with a pre-"
                f"existing canonical-casing row at equal-or-higher privilege.\n\n"
                f"Rebound access rows:\n{access_rebind_lines}"
            )
        if prefix_adopt_count:
            detected_parts.append(
                f"Found {prefix_adopt_count} `project_kg_bindings` row(s) "
                f"whose advertised `collection_name` does not exist in "
                f"Weaviate, but a SINGLE populated class under a different "
                f"prefix matches the same suffix "
                f"(`_KnowledgeGraph` / `_Development`). Auto-adopted the "
                f"populated class and tagged `config_json` with "
                f"`manual_override: v0.2.40-prefix-adopt` so downstream "
                f"env-backfill picks up the new collection name on the next "
                f"`populate()` call.\n\n"
                f"Adopted bindings:\n{prefix_adopt_lines}"
            )
        detected = "\n\n".join(detected_parts) + "\n\nNo data was touched."

        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="kg_binding_self_healed",
                title=title,
                detected=detected,
                why_deferred=(
                    "This is an informational entry — the heal was applied "
                    "automatically (it's a metadata fix, not a destructive "
                    "operation, since the target class already exists in "
                    "Weaviate). The launcher.db row(s) now match the actual "
                    "Weaviate class names so writes/reads route to the live "
                    "class instead of a nonexistent variant.\n\n"
                    "Background: install.py v0.2.23 B1 (2026-05-21) flipped "
                    "the canonical shared-KG class name from "
                    "`VibecodedOrchestrator_KnowledgeGraph` (lowercase c) "
                    "to `VibeCodedOrchestrator_KnowledgeGraph` (capital C, "
                    "matching the brand spelling). Case-insensitive adoption "
                    "in `_ensure_collections` keeps the on-disk casing "
                    "unchanged; this helper aligns the launcher.db "
                    "`project_kg_bindings` AND `kg_collection_access` rows "
                    "with that on-disk casing.\n\n"
                    "install.py v0.2.40 (2026-05-30) added a second pass: "
                    "when a binding row's `collection_name` is genuinely "
                    "missing AND has no case-sibling, probe for "
                    "`*_KnowledgeGraph` / `*_Development` classes with "
                    "non-zero row count; auto-adopt when exactly one matches "
                    "(typical post-`v0.2.29-cleanup` shape where the user "
                    "rebound the PRIMARY binding to a custom prefix like "
                    "`VCODev_*` but left the SHARED binding pointing at the "
                    "release-default canonical name)."
                ),
                command_to_apply=(
                    "No action required — the heal already ran. If you want "
                    "to verify the rebound rows, open the launcher and check "
                    "the Shared KG collection name on each affected project's "
                    "Settings → Identity tab."
                ),
                severity="info",
                kg_node_refs=[],
            )
        )

    if evidence_repoints:
        # Visibility is a REQUIREMENT of this heal, not a nicety: silently
        # changing which collection a project reads and writes would be worse
        # than the defect it fixes. The entry names the old class, the new
        # class, and the evidence that chose it, and states the exact way to
        # put it back.
        repoint_lines = "\n".join(
            f"  * project '{r.project_name}' ({r.folder}): "
            f"`{r.old_name}` -> `{r.new_name}` "
            f"({r.object_count} object(s); {r.matched_paths}/"
            f"{r.sampled_paths} sampled file paths exist under the "
            f"project folder)"
            for r in evidence_repoints
        )
        refusal_note = ""
        # AMBIGUOUS refusals are deliberately not folded in here: they have
        # their own ask (`kg_binding_ambiguous_evidence`), raised from the
        # read-only plan site so it also reaches the machines this RW pass
        # never runs on — see `emit_ambiguous_evidence_entry`. Mentioning them
        # again in this record would double-report one state, and only ever on
        # the machines that happened to owe some OTHER rebind.
        _refused_now = [
            r for r in evidence_refusals if r.reason != REFUSE_AMBIGUOUS
        ]
        if _refused_now:
            refusal_note = "\n\nLeft alone (unchanged): " + "; ".join(
                f"'{r.project_name}' ({r.reason})" for r in _refused_now
            )
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="kg_binding_evidence_repointed",
                title=(
                    f"Re-pointed {len(evidence_repoints)} primary KG "
                    f"binding(s) at the class that demonstrably holds the "
                    f"project's data"
                ),
                detected=(
                    f"For the project(s) below, the registered primary KG "
                    f"binding named a class that EXISTS in Weaviate, while "
                    f"the project's own objects demonstrably live in a "
                    f"DIFFERENT class that no binding row named — the "
                    f"'ghost that already received the writes' shape "
                    f"(D18). The binding row was re-pointed at the class "
                    f"holding the data.\n\n{repoint_lines}{refusal_note}\n\n"
                    f"NO Weaviate data was moved, copied or deleted: this "
                    f"changed one launcher.db row per project. Objects "
                    f"already written into the previously-named class are "
                    f"still there, untouched — and that class stays "
                    f"drop-protected (it is kept in the keep-set the "
                    f"legacy-collection detector consults, so an automatic "
                    f"repair never turns live data into a drop candidate). "
                    f"Two follow-on effects worth knowing: the project's "
                    f"`*_Development` / `*_Diagrams` siblings are DERIVED "
                    f"from the primary name, so they follow the new class "
                    f"(exactly as they would if you picked the class in the "
                    f"Identity tab yourself); and `vco doctor` may now list "
                    f"the previously-named class under "
                    f"`kg_unclaimed_populated_classes` — that entry names "
                    f"data and never proposes deleting it."
                ),
                why_deferred=(
                    "This is an informational record — the repoint was "
                    "applied automatically because the evidence was "
                    "unambiguous: the class named above cleared the ownership "
                    "bar (at least 2 distinct sampled `file_path` values "
                    "existing under the project folder, and at least 80% of "
                    "the sample) with no rival to it, and no binding row "
                    "anywhere named it. 'No rival' means either it was the "
                    "only class to clear the bar, or it led the runner-up by "
                    "more than "
                    f"x{EVIDENCE_MARGIN_FACTOR:g} AND more than "
                    f"+{EVIDENCE_MARGIN_ABS} matched paths — a lead that "
                    "large is what the measurement says, not a tie being "
                    "broken. A closer split writes nothing and raises "
                    "`kg_binding_ambiguous_evidence` for you to settle, and "
                    "no evidence at all leaves the "
                    "`kg_binding_evidence_mismatch` diagnosis standing — a "
                    "name-derived guess is exactly the repair that would "
                    "re-stamp the ghost.\n\n"
                    "TO REVERSE: open the launcher -> the project's page -> "
                    "Identity tab and pick the previous class as the "
                    "primary KG collection (saving re-writes the binding "
                    "and re-projects the project env from it). The previous "
                    "value is also recorded verbatim in the binding row's "
                    "`config_json` under `evidence_repoint.from`."
                ),
                command_to_apply=(
                    "# No action required — the repoint already ran and is "
                    "recorded above.\n"
                    "# To verify what each project now reads and writes:\n"
                    "#   vco doctor\n"
                    "# To reverse: launcher -> the project's page -> "
                    "Identity tab -> pick the previous\n"
                    "#   primary KG collection (named above, and in the "
                    "row's config_json `evidence_repoint.from`).\n"
                    "# Objects in the previously-named class were NOT "
                    "moved; migrating them is a\n"
                    "#   separate decision: python -m vco_lib.project_init "
                    "migrate-collections --help"
                ),
                severity="info",
                kg_node_refs=[],
            )
        )

    if prefix_multi_candidates:
        # User intent is genuinely ambiguous — surface the candidates with
        # row counts and the explicit SQL to pick one.
        multi_lines = []
        for (pid, role, old_name, cands) in prefix_multi_candidates:
            cand_listing = "\n".join(
                f"      - `{name}` ({cnt} object(s))"
                for (name, cnt) in cands
            )
            multi_lines.append(
                f"  * project_id={pid} role={role}: advertised "
                f"`{old_name}` is missing; candidates with rows:\n"
                f"{cand_listing}"
            )
        multi_block = "\n".join(multi_lines)
        # Build a copy-paste SQL stanza per candidate-row for the user.
        sql_lines: list[str] = []
        for (pid, role, _old, cands) in prefix_multi_candidates:
            for (cand_name, _cnt) in cands:
                sql_lines.append(
                    # json_set MERGES into the existing config_json — same
                    # no-clobber rule as the ambiguity ask's stanza: a
                    # wholesale overwrite would drop an evidence_repoint
                    # audit key the row may already carry.
                    f"UPDATE project_kg_bindings SET collection_name = "
                    f"'{cand_name}', config_json = "
                    f"json_set(coalesce(config_json, '{{}}'), "
                    f"'$.manual_override', 'v0.2.40-prefix-adopt'), "
                    f"updated_at = strftime('%s','now') * 1000 "
                    f"WHERE project_id = '{pid}' AND role = '{role}';"
                )
        sql_block = "\n".join(sql_lines)
        deferral_report.add_entry(
            deferral_entry_cls(
                condition_id="multi_candidate_prefix_adopt",
                title=(
                    f"Multiple populated KG collections match advertised "
                    f"`collection_name` suffix — manual choice required "
                    f"({len(prefix_multi_candidates)} row(s) ambiguous)"
                ),
                detected=(
                    f"For the following `project_kg_bindings` row(s), the "
                    f"advertised `collection_name` does not exist in "
                    f"Weaviate AND more than one populated class shares "
                    f"the suffix (`_KnowledgeGraph` / `_Development`). "
                    f"The cross-prefix self-heal refuses to guess.\n\n"
                    f"{multi_block}"
                ),
                why_deferred=(
                    "Auto-adoption with multiple non-empty candidates "
                    "would risk pointing the binding at the wrong data. "
                    "Pick the collection that matches your intent and "
                    "apply the SQL below directly against launcher.db "
                    "(see `command_to_apply`). If neither matches, "
                    "rename one in Weaviate first (out of scope for "
                    "install.py)."
                ),
                command_to_apply=(
                    f"# Pick ONE of the following lines per "
                    f"(project_id, role) tuple and run it against your "
                    f"launcher.db (default: ~/.vct/launcher.db).\n"
                    f"# Then re-run `python install.py --update` to "
                    f"propagate the new binding into .claude/env / "
                    f".claude/settings.json on next launcher boot.\n"
                    f"{sql_block}"
                ),
                severity="warning",
                kg_node_refs=[],
            )
        )

    log_event(
        "7e/10", "ok",
        (
            f"self-healed {binding_count} case-binding(s) + "
            f"{access_count} access row(s) + "
            f"{prefix_adopt_count} prefix-adopt(s) + "
            f"{len(evidence_repoints)} evidence-repoint(s); "
            f"{len(prefix_multi_candidates)} ambiguous row(s) deferred"
        ),
        data={
            "rebinds": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, role, old, new) in rebinds
            ],
            "access_rebinds": [
                {"project_id": pid,
                 "old_collection_name": old,
                 "new_collection_name": new}
                for (pid, old, new) in access_rebinds
            ],
            "prefix_adopts": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "new_collection_name": new,
                 "adopted_row_count": cnt}
                for (pid, role, old, new, cnt) in prefix_adopts
            ],
            "evidence_repoints": [
                {"project_id": r.project_id, "project": r.project_name,
                 "old_collection_name": r.old_name,
                 "new_collection_name": r.new_name,
                 "object_count": r.object_count,
                 "matched_paths": r.matched_paths,
                 "sampled_paths": r.sampled_paths}
                for r in evidence_repoints
            ],
            "evidence_refusals": [
                {"project_id": r.project_id, "project": r.project_name,
                 "bound": r.bound, "reason": r.reason,
                 "candidates": [
                     {"name": c.name, "objects": c.count,
                      "matched_paths": c.matched_paths,
                      "sampled_paths": c.sampled_paths}
                     for c in r.candidates
                 ]}
                for r in evidence_refusals
            ],
            "multi_candidates": [
                {"project_id": pid, "role": role,
                 "old_collection_name": old,
                 "candidates": [
                     {"name": name, "row_count": cnt}
                     for (name, cnt) in cands
                 ]}
                for (pid, role, old, cands) in prefix_multi_candidates
            ],
        },
    )
