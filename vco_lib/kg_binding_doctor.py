# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""READ-ONLY evidence scan: does a project's KG data live where its binding says?

D18 re-closure (v0.2.92, Fable review round 6, MAJOR-R6-1). The update-time
prefix-adopt pass (:func:`vco_lib.kg_binding_heal._prefix_adopt_kg_bindings_pass`)
rebinds a primary binding whose class is ABSENT from Weaviate — but it skips,
by construction, any row whose class EXISTS. A ghost class that has already
received the project's writes exists (the MCP creates a class on first store),
so the recorded field symptom — a project reading and writing a ghost
collection while the binding says otherwise — is exactly the case that pass
does not touch. This module is the MEASURING half of the closure: it never
writes a binding (R38: the previous "fix" here would have re-stamped the
ghost), it only states, with positive evidence, where a project's objects
actually are.

It is also the sole eligibility input to the WRITING half —
:func:`vco_lib.kg_binding_heal.plan_evidence_repoints`, which re-points a
primary binding when one class clears the ownership bar below with no rival
to it — alone, or leading the runner-up by its own decisive margin over THESE
measured numbers — and it is not the one already bound. That consumer holds no
copy of the rule: the bar lives here, once, so the diagnosis and the repair can
never disagree about the same machine. Everything the heal refuses (a split
with no decisive leader, nothing clearing the bar, an unreadable backend) stays
reported by this scan and repaired by hand.

The three values (per registered project)
-----------------------------------------
1. ``bound``    — the launcher-DB primary-KG binding's ``collection_name``
                  (``project_kg_bindings`` role ``primary``; only projects
                  with a real binding row participate — a name-DERIVED
                  primary is a guess with nothing to compare against).
2. ``evidence`` — the classes Weaviate demonstrably holds this project's
                  objects in. The signal is FILE-BACKED, not name-based: a
                  class counts only when it is populated AND at least
                  :data:`OWNERSHIP_MATCH_FRACTION` of a bounded sample of
                  its distinct ``file_path`` values exist as files under the
                  project's registered folder (and at least
                  :data:`MIN_MATCHED_PATHS` of them). KG nodes store
                  PROJECT-RELATIVE paths (``knowledge/concepts/x.md``), so
                  anchoring the sample at the project folder identifies the
                  owner without trusting any class NAME — which is the whole
                  point, since the defect is a wrong name. The ownership bar
                  exists because relative paths COLLIDE across projects
                  (shared nodes, copied notes, template files): measured on
                  a healthy machine those coincidences run at 1 of 100
                  sampled paths, and the defect shapes match at ~100%.
3. ``expected`` — ``project_identity.expected_kg_primary_class(name)``, the
                  ONE home for the sanitize-plus-suffix naming rule.

A verdict MISMATCHES when a class that NO project's binding row names (any
role) demonstrably holds the project's objects: an unbound ghost with the
data in it.

The unclaimed dimension (v0.2.92, reported-not-fixed item 3)
------------------------------------------------------------
The same pass also reports the INVERSE gap: a populated ``*_KnowledgeGraph``
class that no binding row names AND whose sampled paths anchor to no
registered project's folder — data with no reader, e.g. a removed project's
leftover class (the field find: ``Agape_KnowledgeGraph``, 84 objects, no
binding row anywhere, source files gone). That class appears in NO verdict
(the verdicts are keyed by registered project), so without this dimension
it is invisible to every surface. The inputs are the ones the scan already
fetched — this is a coverage extension of the SAME detector, not a new one.

Exemptions, all conservative (each can only under-report, never nag):
* the canonical shared class named by ``app_state`` key
  ``orchestrator_root_kg_collection`` (default
  :data:`DEFAULT_SHARED_KG_COLLECTION` when the key is absent, mirroring the
  Rust getter and migration 028) — the shared class's lifecycle belongs to
  the shared-KG surfaces (R8 heal, legacy detector, picker), and a machine
  whose root project is not yet registered would otherwise read its own
  freshly-seeded shared corpus as "unclaimed";
* a class that is evidence for ANY registered project (claimed via the
  ownership bar) or named by ANY binding row;
* a class whose sample carries no usable relative paths — the anchor check
  could not run, and unknown is not unclaimed. A binding that merely differs from ``expected`` while its own
class holds the data is NOT a mismatch — a deliberately custom-named binding
is a supported state (the Identity-tab picker exists for it) and crying wolf
there would bury the real signal. Classes bound by OTHER projects are
excluded from a project's ghost set because two projects can legitimately
share relative paths (template-shipped files): without the exclusion they
would accuse each other's classes on every healthy machine.

Reads and writes
----------------
Everything here is a READ: one ``GET /v1/schema``, one Aggregate count and
one bounded ``Get`` per ``*_KnowledgeGraph`` class, one read-only
``launcher.db`` open (:func:`vco_lib.project_identity.resolve_snapshot` — the
single DB-open helper), and ``stat`` calls under project folders. No binding
is ever written, renamed or re-stamped by this module.

Positive evidence only
----------------------
* Weaviate unreachable (schema listing ``unknown``) → the scan returns
  ``None``: a backend that cannot be looked at is NEVER rendered as "the
  class is missing" or as healthy agreement. The doctor probe emits NOTHING
  on ``None`` — not even an ``unknown`` finding.
* A count of ``None`` (Aggregate failed) skips the class entirely — unknown
  is not zero.
* A sample of ``None`` (Get failed) skips the class — same rule.
* A class whose matching objects all sit beyond the sample window
  (:data:`SAMPLE_LIMIT` objects) is invisible. That is the honest cost of a
  bounded probe and it is recorded in ``KNOWN_ISSUES.md`` with the other
  residuals; the field shapes this probe exists for (a ghost that received
  the project's writes) put those writes well inside any sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

#: How many objects of each candidate class are inspected for a file-backed
#: match. Weaviate's GraphQL Get default ceiling is 100; staying at it keeps
#: the probe one request per class. See the module docstring for what sits
#: beyond this window.
SAMPLE_LIMIT = 100

#: Minimum DISTINCT matching paths before a class may count as evidence at
#: all — one shared relative path between two projects is noise, not ownership
#: (measured: 1-of-100 cross-matches on a healthy machine). A ghost that has
#: received exactly ONE write is therefore below the evidence floor: the
#: diagnostic waits for a second data point before it speaks.
MIN_MATCHED_PATHS = 2

#: The machine's canonical shared KG class name, used ONLY as the unclaimed
#: exemption default. MUST mirror the Rust getter's compiled-in default
#: (``app_state.rs::DEFAULT_ORCHESTRATOR_ROOT_KG_COLLECTION``) and migration
#: 028's seed value — three names for one fact is the drift the access-matrix
#: audit already paid for once.
DEFAULT_SHARED_KG_COLLECTION = "VibeCodedOrchestrator_KnowledgeGraph"

#: app_state key holding the canonical shared class (see the exemption above).
_APP_STATE_ORCH_ROOT_KG = "orchestrator_root_kg_collection"

#: Fraction of a class's DISTINCT sampled paths that must exist under the
#: project folder before the class counts as holding the project's data.
#: Calibrated on the generative distinction, not on a number: a true ghost is
#: written ONLY by the project's own pipeline, so essentially every object it
#: holds is the project's (~100% match); an unrelated project's class matches
#: at whatever fraction of its notes were COPIED into this folder (measured:
#: 30/58 = 52% on a heavy note-copying dogfood machine — excluded; 1/100
#: coincidences — excluded). The 80% bar sits above copy-overlap and below
#: the deletion-diluted ghost (a ghost whose files were later >20% deleted
#: or moved on disk drops under it — a recorded residual).
OWNERSHIP_MATCH_FRACTION = 0.8


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassEvidence:
    """One class that demonstrably holds a project's objects.

    "Demonstrably" is the OWNERSHIP RULE, calibrated on live data: a class
    counts as holding the project's data when at least
    :data:`MIN_MATCHED_PATHS` DISTINCT sampled paths exist as files under
    the project folder AND they make up at least
    :data:`OWNERSHIP_MATCH_FRACTION` of the sample. Measured on a real
    7-project machine, coincidental cross-matches (a shared node whose
    relative path exists in two projects) run at 1 of 100 sampled paths, and
    an unbound leftover class of ANOTHER project matched at 30 of 58 (a
    folder holding copies of its notes) — both far below the bar. The defect
    shapes this probe exists for (a ghost that received the project's
    writes; the pre-rename class holding its nodes) match at essentially
    100%, because the class IS the project's corpus — nothing but the
    project's own pipeline ever writes to a ghost.
    """

    name: str
    #: Object count from the Aggregate endpoint (``> 0`` — the scan never
    #: records an empty class, and never records a class it could not count).
    count: int
    #: True when SOME project's ``project_kg_bindings`` row names this class
    #: (any role). A bound class is accounted for somewhere; an UNBOUND one
    #: holding a project's data is the ghost this probe exists to name.
    bound: bool
    #: How many DISTINCT sampled ``file_path`` values exist under the project
    #: folder, out of how many sampled (:attr:`sampled_paths`).
    matched_paths: int
    sampled_paths: int


@dataclass(frozen=True)
class ProjectBindingVerdict:
    """The three values, for one registered project."""

    project_id: str
    project_name: str
    folder: str
    #: Value 1 — the launcher-DB primary binding's ``collection_name``.
    bound: str
    #: Value 3 — the name-derived primary class (naming rule SSOT).
    expected: str
    #: Value 2 — every populated class whose sampled objects carry a
    #: ``file_path`` that exists under ``folder`` (bound classes included;
    #: the report should say where the data is, not only where it leaks).
    evidence: tuple[ClassEvidence, ...]

    @property
    def unbound_evidence(self) -> tuple[ClassEvidence, ...]:
        """Evidence classes no binding row names — the ghost candidates."""
        return tuple(e for e in self.evidence if not e.bound)

    @property
    def mismatch(self) -> bool:
        """True when an unbound class demonstrably holds this project's data."""
        return bool(self.unbound_evidence)


@dataclass(frozen=True)
class UnclaimedClass:
    """One populated KG class no registered project accounts for.

    "Unaccounted" is the two-legged positive finding below: no binding row
    anywhere names it, and no registered project's folder anchors its sampled
    paths (it failed every project's ownership bar). Its data is unreadable
    by construction — nothing resolves to the class — which is why the
    REPORT names it while never proposing what to do with it (see the
    doctor's entry: diagnosis only, no drop command).
    """

    name: str
    #: Object count from the Aggregate endpoint (``> 0`` by construction).
    count: int


@dataclass(frozen=True)
class BindingEvidenceScan:
    """Every binding-backed project's verdict, or nothing (see the scan)."""

    verdicts: tuple[ProjectBindingVerdict, ...]
    #: Populated classes no binding names and no folder anchors (v0.2.92).
    #: Defaulted so pre-existing constructors (tests, the registry probe)
    #: stay source-compatible; the scan itself always passes it explicitly.
    unclaimed: tuple[UnclaimedClass, ...] = ()

    @property
    def mismatches(self) -> tuple[ProjectBindingVerdict, ...]:
        return tuple(v for v in self.verdicts if v.mismatch)


# ---------------------------------------------------------------------------
# IO defaults — module-level and resolved at CALL time so tests can monkeypatch
# (the same contract ``weaviate_helpers.probe_class_listing`` gives).
# ---------------------------------------------------------------------------


def _list_classes_default(weaviate_url: Optional[str]) -> Optional[list[str]]:
    """Tri-state class listing: names (possibly empty) or ``None``=unknown."""
    from vco_lib.weaviate_helpers import probe_class_listing

    result = probe_class_listing(weaviate_url)
    if result.is_unknown():
        return None
    return list(result.require())


def _count_objects_default(
    class_name: str, weaviate_url: Optional[str]
) -> Optional[int]:
    """:func:`vco_lib.weaviate_helpers.http_count_objects` — None-on-failure."""
    from vco_lib.weaviate_helpers import http_count_objects

    return http_count_objects(class_name, weaviate_url)


def _sample_file_paths_default(
    class_name: str, weaviate_url: Optional[str]
) -> Optional[tuple[str, ...]]:
    """Bounded ``file_path`` sample from one class, or ``None``=could not look.

    One GraphQL ``Get`` through :func:`vco_lib.weaviate_helpers.post_graphql_safe`
    (transport + GraphQL-errors array handled in the ONE place). A class that
    vanished between the listing and this call surfaces as a GraphQL error →
    ``None`` → the class is skipped, never treated as empty.
    """
    from vco_lib.weaviate_helpers import post_graphql_safe, weaviate_url_default

    base = (weaviate_url or weaviate_url_default()).rstrip("/")
    data = post_graphql_safe(
        base,
        {
            "query": (
                f"{{ Get {{ {class_name}(limit: {SAMPLE_LIMIT}) "
                f"{{ file_path }} }} }}"
            )
        },
        ctx=f"kg_binding_doctor:{class_name}",
        timeout=10.0,
    )
    if not isinstance(data, dict):
        return None
    get = data.get("Get")
    if not isinstance(get, dict):
        return None
    objects = get.get(class_name)
    if not isinstance(objects, list):
        return None
    out: list[str] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        fp = obj.get("file_path")
        if isinstance(fp, str) and fp.strip():
            out.append(fp.strip())
    return tuple(out)


def _file_exists_default(folder: str, rel_path: str) -> bool:
    """True when ``rel_path`` is project-relative AND exists under ``folder``.

    Reuses ``collection_repair._classify_file_path`` — the repo's ONE
    "is this Weaviate ``file_path`` a plain project-relative path" rule
    (absolute, drive-letter and ``..``-carrying values are "defensive" and
    must not be anchored at ANY project folder: an absolute path from a
    different project would match everyone). Reaching into the private
    helper is deliberate, the same deliberate reach ``vco_lib.doctor``
    makes into ``vco_lib.cli.verify``: the alternative is a second
    implementation of the same rule, which the modularity rule forbids.
    """
    from vco_lib.collection_repair import _classify_file_path

    if _classify_file_path(rel_path) != "relative":
        return False
    try:
        return (Path(folder) / rel_path).is_file()
    except OSError:  # noqa: PERF203 — one unreadable folder is not a verdict
        return False


def _shared_pointer_exempt_default(db_path: Optional[Path]) -> str:
    """The canonical shared class name, for the unclaimed exemption.

    Reads ``app_state.orchestrator_root_kg_collection`` the same way the
    Rust getter does (missing key → compiled-in default, which is also what
    migration 028 seeds on every fresh DB). Never raises and never returns
    empty: an unreadable key falls back to the default, which can only
    UNDER-report unclaimed classes (the conservative direction), never invent
    one.
    """
    try:
        from vco_lib.launcher_db_reader import _open_db_readonly

        conn = _open_db_readonly(db_path)
    except Exception:  # noqa: BLE001 — exemption read must never break the scan
        return DEFAULT_SHARED_KG_COLLECTION
    if conn is None:
        return DEFAULT_SHARED_KG_COLLECTION
    try:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = ?",
            (_APP_STATE_ORCH_ROOT_KG,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — pre-app_state schemas
        return DEFAULT_SHARED_KG_COLLECTION
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    value = (row[0] or "").strip() if row else ""
    return value or DEFAULT_SHARED_KG_COLLECTION


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def scan_kg_binding_evidence(
    *,
    db_path: Optional[Path] = None,
    weaviate_url: Optional[str] = None,
    list_classes: Optional[Callable[[], Optional[list[str]]]] = None,  # pyright: ignore[reportRedeclaration] — shadowed on purpose by the call-time default below
    count_objects: Optional[Callable[[str], Optional[int]]] = None,  # pyright: ignore[reportRedeclaration] — shadowed on purpose by the call-time default below
    sample_paths: Optional[Callable[[str], Optional[tuple[str, ...]]]] = None,  # pyright: ignore[reportRedeclaration] — shadowed on purpose by the call-time default below
    file_exists: Optional[Callable[[str, str], bool]] = None,
) -> Optional[BindingEvidenceScan]:
    """Compare every registered project's primary binding against its data.

    Returns:
        * a :class:`BindingEvidenceScan` — including when Weaviate holds no
          classes at all and when no project has a binding row (both are
          LOOKABLE states: an empty scan is vacuous agreement, and the
          self-resolving lifecycle needs the clean reading to clear a prior
          entry);
        * ``None`` when the comparison could not be LOOKED at — launcher.db
          unreadable, or the Weaviate schema listing unknown (unreachable,
          non-200, unparsable). Probe failure is not evidence: the caller
          emits NOTHING, neither a problem nor an "all agreed".

    Never raises and never writes: read-only DB open, HTTP reads, ``stat``.
    Every IO seam is injectable for hermetic tests; the defaults are resolved
    at call time so monkeypatching this module's ``*_default`` functions also
    works.
    """
    from vco_lib.project_identity import (
        PRIMARY_KG_SUFFIX,
        SOURCE_BINDING,
        expected_kg_primary_class,
        resolve_snapshot,
    )

    if list_classes is None:
        def list_classes() -> Optional[list[str]]:  # noqa: F811 — call-time default
            return _list_classes_default(weaviate_url)
    if count_objects is None:
        def count_objects(name: str) -> Optional[int]:  # noqa: F811
            return _count_objects_default(name, weaviate_url)
    if sample_paths is None:
        def sample_paths(name: str) -> Optional[tuple[str, ...]]:  # noqa: F811
            return _sample_file_paths_default(name, weaviate_url)
    if file_exists is None:
        file_exists = _file_exists_default

    snapshot = resolve_snapshot(db_path=db_path)
    if not snapshot.resolvable:
        return None

    listing = list_classes()
    if listing is None:
        return None
    kg_classes = sorted(
        {
            name
            for name in listing
            if isinstance(name, str) and name.endswith(PRIMARY_KG_SUFFIX)
        }
    )

    # Ownership map: every class ANY binding row names (role-unfiltered), and
    # which projects name it. `bound_kg_collections` is verbatim and
    # role-UNFILTERED by contract (project_identity F-4), so this cannot miss
    # a row the way a role-filtered projection would.
    owners: dict[str, set[str]] = {}
    for ident in snapshot.projects:
        for coll in ident.bound_kg_collections:
            owners.setdefault(coll, set()).add(ident.project_id or "")

    # One pass over the class family: count + sample fetched ONCE per class,
    # shared by every project's verdict.
    samples: dict[str, tuple[int, tuple[str, ...]]] = {}
    for cls in kg_classes:
        count = count_objects(cls)
        if count is None or count <= 0:
            # None = could not count (skip — unknown is not zero); 0 = the
            # class holds nobody's data (skip — an empty class cannot be
            # evidence of anything).
            continue
        sample = sample_paths(cls)
        if sample is None:
            continue  # could not look inside — not evidence either way
        samples[cls] = (count, sample)

    verdicts: list[ProjectBindingVerdict] = []
    for ident in snapshot.projects:
        bound = (ident.kg_primary or "").strip()
        folder = (ident.folder_path or "").strip()
        if ident.kg_primary_source != SOURCE_BINDING or not bound or not folder:
            # No primary binding row (a name-derived primary is a guess with
            # nothing to compare — the heal pass owns bindingless rows), or no
            # folder to anchor evidence at. Skip: not a verdict either way.
            continue
        evidence: list[ClassEvidence] = []
        for cls, (count, sample) in samples.items():
            if not sample:
                continue  # populated but no usable file_path values — no anchor
            distinct = set(sample)
            matched = sum(
                1 for fp in distinct if file_exists(folder, fp)
            )
            if (
                matched < MIN_MATCHED_PATHS
                or matched < OWNERSHIP_MATCH_FRACTION * len(distinct)
            ):
                # Below the ownership bar: coincidental shared paths or
                # copied-note overlap, not ownership (see ClassEvidence for
                # the calibration).
                continue
            evidence.append(
                ClassEvidence(
                    name=cls,
                    count=count,
                    bound=cls in owners,
                    matched_paths=matched,
                    sampled_paths=len(distinct),
                )
            )
        verdicts.append(
            ProjectBindingVerdict(
                project_id=ident.project_id or "",
                project_name=ident.name,
                folder=folder,
                bound=bound,
                expected=expected_kg_primary_class(ident.name),
                evidence=tuple(sorted(evidence, key=lambda e: e.name)),
            )
        )

    # v0.2.92 (reported-not-fixed item 3): the unclaimed dimension. A class
    # counts when it is populated, sampled with at least one usable relative
    # path (the anchor check actually RAN — an unusable sample is unknown,
    # not unclaimed), named by NO binding row, claimed by NO verdict, and not
    # the exempted canonical shared class. Every exclusion is the
    # conservative direction: it can only leave a class unreported, never
    # report one that is accounted for.
    claimed = {e.name for v in verdicts for e in v.evidence}
    exempt = _shared_pointer_exempt_default(db_path)
    unclaimed = tuple(
        UnclaimedClass(name=cls, count=count)
        for cls, (count, sample) in sorted(samples.items())
        if sample
        and cls not in owners
        and cls not in claimed
        and cls != exempt
    )
    return BindingEvidenceScan(
        verdicts=tuple(verdicts), unclaimed=unclaimed
    )


def render_three_values(verdict: ProjectBindingVerdict) -> str:
    """The one-line three-value statement the finding and entry both print.

    ONE renderer for both surfaces so the CLI line and the ledger entry can
    never disagree about what the comparison showed.
    """
    parts = []
    for ev in verdict.evidence:
        owner = "bound" if ev.bound else "UNBOUND"
        parts.append(
            f"{ev.name} ({ev.count} objects, {ev.matched_paths}/"
            f"{ev.sampled_paths} sampled paths here, {owner})"
        )
    where = "; ".join(parts) if parts else "no class matched this project's files"
    return (
        f"project '{verdict.project_name}' ({verdict.folder}): "
        f"binding={verdict.bound} · expected-from-name={verdict.expected} · "
        f"data-found-in=[{where}]"
    )


def jsonable_verdicts(scan: BindingEvidenceScan) -> list[dict[str, Any]]:
    """The ``Finding.detail`` payload shape for the doctor's JSON report.

    One renderer (with :func:`render_three_values` covering the prose side)
    so the machine-readable report and the human line cannot disagree.
    """
    return [
        {
            "project": v.project_name,
            "folder": v.folder,
            "bound": v.bound,
            "expected": v.expected,
            "evidence": [
                {
                    "class": e.name,
                    "count": e.count,
                    "bound": e.bound,
                    "matched_paths": e.matched_paths,
                    "sampled_paths": e.sampled_paths,
                }
                for e in v.evidence
            ],
            "unbound": [e.name for e in v.unbound_evidence],
        }
        for v in scan.verdicts
    ]


__all__ = [
    "BindingEvidenceScan",
    "ClassEvidence",
    "DEFAULT_SHARED_KG_COLLECTION",
    "MIN_MATCHED_PATHS",
    "OWNERSHIP_MATCH_FRACTION",
    "ProjectBindingVerdict",
    "SAMPLE_LIMIT",
    "UnclaimedClass",
    "jsonable_verdicts",
    "render_three_values",
    "scan_kg_binding_evidence",
]
