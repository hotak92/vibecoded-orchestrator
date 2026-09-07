# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Collapse ALREADY-STORED duplicate cross-reference beacons in the code graph.

Why this exists
---------------
``data.reference_add`` is not idempotent: Weaviate stores every call as its own
beacon. Until v0.2.92 the analyzer re-added every discovered edge on every
re-analysis with no existence check, so an unchanged file multiplied its stored
edges on each pass — unbounded growth.

:func:`vco_lib.codegraph_references.add_missing_reference_edges` stops the
GROWTH (re-analysis is now add-if-absent, therefore idempotent). It deliberately
does NOT shrink what is already stored — that is a mutation of live user data
and belongs behind explicit consent, which is this module. Its docstring names
the split; this module is the other half.

Reads already collapse duplicates for display
(:func:`vco_lib.codegraph_references.dedup_ref_targets`), so query ANSWERS are
already correct. What remains is dead weight: payload size, resolve cost and
disk. **There is therefore no urgency that justifies a risky repair** — every
ambiguous case in this module resolves to "do nothing and say so".

The safety invariant (enforced per row at RUNTIME, not merely tested)
--------------------------------------------------------------------
    The SET of distinct target UUIDs a row points at is IDENTICAL before and
    after. Only exact duplicates collapse.

It is computed for every row, re-computed against a fresh read immediately
before the write, and (by default) confirmed against the stored row afterwards.
A row that would lose a distinct target is SKIPPED and REPORTED — never
written. A test alone would not be enough: this tool runs against strangers'
live databases, where the guard has to hold for data we have never seen.

Why raw beacon strings rather than ``data.reference_replace``
------------------------------------------------------------
``reference_replace(to=[uuid, ...])`` funnels through
``weaviate.util._to_beacons(uuids, target_collection="")`` and therefore writes
CLASS-LESS beacons — ``weaviate://localhost/<uuid>`` — while every beacon
stored by the analyzer is class-qualified
(``weaviate://localhost/<Prefix>_CodeModule/<uuid>``). Verified against
weaviate-client 4.21.0. Round-tripping through UUIDs would silently rewrite the
stored representation of ~240k surviving edges as a side effect of a cleanup
that was supposed to remove duplicates and change nothing else.

So the collapse keeps the FIRST-SEEN beacon string verbatim for each distinct
target UUID and PUTs those strings back. Identity is the target UUID (so a
mixed-form pair collapses correctly); representation is whatever was already
there. The endpoint is the same one ``reference_replace`` uses —
``PUT /v1/objects/<class>/<uuid>/references/<prop>`` — reached through the
repo's shared :func:`vco_lib.weaviate_helpers.http_request` primitive.

Fail-closed, per row
--------------------
The sibling fix chose add-if-absent over replace precisely because the
analyzer's cache scan soft-fails, and ``replace`` on a partial view DELETES
edges it merely failed to see. The same hazard applies here, so every read that
cannot be positively interpreted routes to SKIP:

* reference value is not a list                      → skip (unreadable-value)
* any entry is not a ``{"beacon": str}``             → skip (unreadable-beacon)
* any beacon does not end in a parsable UUID         → skip (unreadable-beacon)
* the collapse would leave zero targets              → skip (would-empty)
* the distinct set would change                      → skip (invariant)
* a page read fails                                  → the collection is marked
  truncated and pagination stops; rows already handled were each handled on
  their own positive read, so they stay handled.

Crash safety: each row is ONE ``PUT``. Weaviate applies it whole or not at all,
so an interrupted run leaves every row either untouched or fully collapsed —
never partial. Re-running resumes: collapsed rows report ``already-unique`` and
are not written again.

Dangling targets — a row this tool CANNOT repair
-----------------------------------------------
Found on live data, not anticipated: ``POST .../references/<prop>`` (what the
analyzer used) accepted beacons that ``PUT .../references/<prop>`` refuses. The
PUT validates target existence and rejects the ENTIRE list with HTTP 422 —
``validate reference: no object with id <uuid> found`` — when any beacon points
at a row that has since been deleted (a pruned module, say). Verified on this
machine: the rejected row was left byte-for-byte untouched, which is the
behaviour we want, but it means such a row keeps its duplicates.

Removing the dead beacon would collapse it — and would CHANGE the distinct
target set, which is the one thing this tool promises never to do. Garbage-
collecting dead edges is a different operation with its own safety argument, so
it is deliberately NOT done here, not even behind a flag. These rows are
reported as ``dangling-target`` skips naming the missing UUID; the remedy is to
re-run the code-graph analysis for that project (which re-creates the target row
if its file still exists, or prunes the edge if it does not) and then re-run
this tool. Note the read path already hides these edges — ``dependencies`` on a
row with 8 stored targets, one dead, answers with 7.

CLI::

    python -m vco_lib.codegraph_ref_dedup --project MyProject        # dry run
    python -m vco_lib.codegraph_ref_dedup --project MyProject --apply
    python -m vco_lib.codegraph_ref_dedup --all-projects --json

Dry-run is the DEFAULT; ``--apply`` is the only thing that writes.

Exit codes: ``0`` clean · ``1`` errors or an incomplete read · ``2`` could not
run · ``3`` completed, but rows remain owed (skips).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid as uuid_module
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    cast,
)

from vco_lib import weaviate_helpers as _wh

# REUSE, never mirror: the cross-reference topology (which base carries which
# reference properties) already has one home. Importing the private name is
# deliberate — a second copy of this table would be the fourth call site of one
# concern (CLAUDE.md § "search before you add, extract before you duplicate"),
# and a table that drifts from the analyzer's ReferenceProperty blocks is worse
# than a private import.
from vco_lib.codegraph_vector_copy import _REFERENCE_NAMES

__all__ = [
    "ACTION_COLLAPSE",
    "ACTION_LEAVE",
    "ACTION_SKIP",
    "REASON_ALREADY_UNIQUE",
    "REASON_CHANGED_SINCE_SCAN",
    "REASON_DANGLING_TARGET",
    "REASON_INVARIANT",
    "REASON_NO_BEACONS",
    "REASON_UNREADABLE_BEACON",
    "REASON_UNREADABLE_VALUE",
    "REASON_WOULD_EMPTY",
    "SKIP_REMEDIES",
    "CollectionReport",
    "RefDedupError",
    "RowPlan",
    "RunReport",
    "beacon_target_uuid",
    "collections_for_prefix",
    "list_code_collections",
    "main",
    "plan_row",
    "prefixes_of",
    "put_references",
    "reference_props_for",
    "repair_collection",
    "resolve_prefix",
    "run",
    "target_exists",
]

#: Weaviate object-list page size. 100 keeps a single response bounded even for
#: rows carrying five figures of beacons (the worst row measured on the
#: maintainer machine held 11,496 on one property).
DEFAULT_PAGE_SIZE = 100

#: The five code-graph class basenames, longest-suffix-first so
#: ``endswith`` never mis-attributes.
_CODE_BASES: Tuple[str, ...] = tuple(sorted(_REFERENCE_NAMES, key=len, reverse=True))

# ── Per-row outcomes ────────────────────────────────────────────────────────
ACTION_COLLAPSE = "collapse"  # duplicates present, safe to write
ACTION_LEAVE = "leave"        # nothing to do; MUST NOT be written
ACTION_SKIP = "skip"          # cannot act safely; MUST be reported

REASON_ALREADY_UNIQUE = "already-unique"
REASON_NO_BEACONS = "no-beacons"
REASON_UNREADABLE_VALUE = "unreadable-value"
REASON_UNREADABLE_BEACON = "unreadable-beacon"
REASON_WOULD_EMPTY = "would-empty"
REASON_INVARIANT = "distinct-set-would-change"
REASON_CHANGED_SINCE_SCAN = "changed-since-scan"
REASON_DANGLING_TARGET = "dangling-target"
REASON_DUPLICATES = "duplicates"

#: What a caller should DO about each named skip. Printed under the totals so
#: the report answers "now what?" without the reader guessing.
SKIP_REMEDIES: Dict[str, str] = {
    REASON_DANGLING_TARGET: (
        "a beacon points at a deleted row, so Weaviate rejects the whole "
        "replace. Re-run the code-graph analysis for this project, then re-run "
        "this tool. Nothing was written to these rows."),
    REASON_UNREADABLE_VALUE: (
        "the stored value was not a beacon list; left untouched on purpose. "
        "Report it — this shape was not seen on any known install."),
    REASON_UNREADABLE_BEACON: (
        "a beacon could not be parsed, so the row's targets could not be "
        "positively enumerated; left untouched rather than guessed at."),
    REASON_CHANGED_SINCE_SCAN: (
        "the row was collapsed by something else between the scan and the "
        "write (another run, or a concurrent analyzer). Nothing owed."),
    REASON_INVARIANT: (
        "collapsing would have changed the row's distinct target set. This "
        "should be impossible — please report it."),
    REASON_WOULD_EMPTY: (
        "collapsing would have emptied the row. Refused."),
}

_BEACON_SCHEME = "weaviate://"

#: Weaviate's rejection when a beacon names a row that no longer exists.
_DANGLING_RE = re.compile(
    r"no object with id ([0-9a-fA-F-]{36}) found")


class RefDedupError(RuntimeError):
    """Raised when the tool cannot proceed safely (loud, never a silent zero).

    Deliberately NOT used for per-row trouble: a row we cannot read is skipped
    and reported, because one odd row must not abort a repair of 57,000 others.
    This is for "we could not even enumerate the collections" class failures,
    where returning an empty list would look exactly like "nothing to repair".
    """


# ---------------------------------------------------------------------------
# Pure layer — no I/O, every branch unit-testable
# ---------------------------------------------------------------------------


def reference_props_for(collection: str) -> Tuple[str, ...]:
    """Reference-property names carried by ``collection``.

    Accepts a per-project class (``MyProject_CodeModule``) or a bare base
    (``CodeModule``). Returns ``()`` for anything that is not a code-graph
    class, which is how non-code collections are excluded without a second
    name-shape rule living here.
    """
    for base in _CODE_BASES:
        if collection == base or collection.endswith("_" + base):
            return tuple(_REFERENCE_NAMES[base])
    return ()


def beacon_target_uuid(beacon: Any) -> Optional[str]:
    """Target object UUID encoded in a Weaviate beacon, or ``None``.

    ``weaviate://localhost/<Class>/<uuid>`` and the class-less legacy form
    ``weaviate://localhost/<uuid>`` both yield ``<uuid>``; the UUID is
    canonicalised (lowercase, dashed) so the two spellings of one target
    collapse together.

    ``None`` means "not positively parsable" and always routes the whole row
    to SKIP — we never guess at the identity of an edge we are about to
    rewrite.
    """
    if not isinstance(beacon, str):
        return None
    text = beacon.strip()
    if not text or "/" not in text:
        return None
    if not text.lower().startswith(_BEACON_SCHEME):
        return None
    tail = text.rsplit("/", 1)[-1]
    # Strip a query/fragment if a client ever appends one.
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    try:
        return str(uuid_module.UUID(tail))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class RowPlan:
    """What to do with ONE ``(row, reference property)`` pair.

    ``beacons`` is the exact write payload for an :data:`ACTION_COLLAPSE`
    plan — first-seen beacon STRINGS, ordered, one per distinct target. It is
    empty for every other action, so a caller cannot accidentally write a
    leave/skip plan.
    """

    collection: str
    uuid: str
    prop: str
    action: str
    reason: str
    before: int
    after: int
    beacons: Tuple[str, ...] = ()
    target_uuids: Tuple[str, ...] = ()
    detail: str = ""

    @property
    def removable(self) -> int:
        """Beacons this plan would remove (0 unless it collapses)."""
        if self.action != ACTION_COLLAPSE:
            return 0
        return max(0, self.before - self.after)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "collection": self.collection,
            "uuid": self.uuid,
            "property": self.prop,
            "action": self.action,
            "reason": self.reason,
            "before": self.before,
            "after": self.after,
            "removable": self.removable,
        }
        if self.detail:
            out["detail"] = self.detail
        return out


def _leave(collection: str, uid: str, prop: str, reason: str,
           before: int = 0, detail: str = "") -> RowPlan:
    return RowPlan(collection, uid, prop, ACTION_LEAVE, reason,
                   before, before, detail=detail)


def _skip(collection: str, uid: str, prop: str, reason: str,
          before: int = 0, detail: str = "") -> RowPlan:
    return RowPlan(collection, uid, prop, ACTION_SKIP, reason,
                   before, before, detail=detail)


def _ordered_unique(
    entries: Sequence[Tuple[str, str]]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``[(beacon, target_uuid)]`` → ``(targets, beacons)``, first-seen order.

    One target UUID keeps the FIRST beacon string that named it, so the stored
    representation of a surviving edge is byte-identical to what was there
    (see the module docstring on class-qualified vs class-less beacons).

    A named function rather than an inline loop so the two defensive branches
    in :func:`plan_row` that depend on it — "the distinct set changed" and
    "the collapse would empty the row" — are reachable in a test. A guard whose
    failure path cannot be exercised is a guard nobody has checked.
    """
    seen: Dict[str, str] = {}
    for raw, target in entries:
        if target not in seen:
            seen[target] = raw
    targets = tuple(seen)
    return targets, tuple(seen[t] for t in targets)


def plan_row(collection: str, uid: str, prop: str, value: Any) -> RowPlan:
    """Decide what to do with one stored reference value. Pure; never raises.

    ``value`` is the RAW REST representation of the property — a list of
    ``{"beacon": ..., "href": ...}`` dicts, or ``None`` when the row carries no
    edge for this property.

    Every non-collapse outcome is explicit and named, because "did nothing"
    and "could not read it" are different answers and a repair tool that
    conflates them is not trustworthy.
    """
    if value is None:
        return _leave(collection, uid, prop, REASON_NO_BEACONS)
    if not isinstance(value, (list, tuple)):
        # Fail closed: an unexpected shape means we cannot enumerate the
        # row's targets, and a write would be a guess.
        return _skip(collection, uid, prop, REASON_UNREADABLE_VALUE,
                     detail=f"value is {type(value).__name__}, expected list")
    entries: List[Tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            return _skip(collection, uid, prop, REASON_UNREADABLE_BEACON,
                         before=len(value),
                         detail=f"entry is {type(item).__name__}, expected dict")
        raw = item.get("beacon")
        target = beacon_target_uuid(raw)
        if target is None:
            return _skip(collection, uid, prop, REASON_UNREADABLE_BEACON,
                         before=len(value),
                         detail=f"unparsable beacon {raw!r}")
        entries.append((cast(str, raw), target))

    before = len(entries)
    if before == 0:
        return _leave(collection, uid, prop, REASON_NO_BEACONS)

    kept_targets, kept_beacons = _ordered_unique(entries)
    after = len(kept_beacons)

    if after == before:
        return RowPlan(collection, uid, prop, ACTION_LEAVE,
                       REASON_ALREADY_UNIQUE, before, after,
                       target_uuids=kept_targets)

    # ── THE SAFETY INVARIANT, enforced at runtime ──────────────────────────
    # By construction the sets are equal; it is asserted anyway because this
    # runs against databases nobody has inspected, and because the day someone
    # changes the dedup key (beacon string instead of target UUID, say) this
    # is the line that turns a silent data loss into a reported skip.
    if set(t for _, t in entries) != set(kept_targets):
        return _skip(collection, uid, prop, REASON_INVARIANT, before=before,
                     detail=(f"{len(set(t for _, t in entries))} distinct "
                             f"before vs {len(set(kept_targets))} after"))

    if after == 0:
        # Unreachable while `before > 0`, kept because the one thing this tool
        # must never do is PUT an empty reference list.
        return _skip(collection, uid, prop, REASON_WOULD_EMPTY, before=before)

    return RowPlan(collection, uid, prop, ACTION_COLLAPSE, REASON_DUPLICATES,
                   before, after, beacons=kept_beacons,
                   target_uuids=kept_targets)


def prefixes_of(collections: Sequence[str]) -> List[str]:
    """Distinct project class-prefixes present in ``collections``, sorted.

    ``MyProject_CodeModule`` → ``MyProject``. A bare, unprefixed legacy class
    (``CodeModule``) contributes the empty prefix, rendered as ``"(unprefixed)"``
    by the CLI rather than silently dropped.
    """
    out = set()
    for name in collections:
        for base in _CODE_BASES:
            if name == base:
                out.add("")
                break
            if name.endswith("_" + base):
                out.add(name[: -(len(base) + 1)])
                break
    return sorted(out)


def collections_for_prefix(prefix: str,
                           collections: Sequence[str]) -> List[str]:
    """The code-graph classes belonging to ``prefix``, sorted.

    Exact prefix match only — ``Vct`` never picks up ``Vct_coordination``'s
    classes, because the separator is part of the comparison.
    """
    wanted = {
        (base if prefix == "" else f"{prefix}_{base}") for base in _CODE_BASES
    }
    return sorted(c for c in collections if c in wanted)


def resolve_prefix(project: str, collections: Sequence[str]) -> str:
    """Map a user-supplied ``--project`` value onto a live class prefix.

    Tried in order, first hit wins, each verified against classes that ACTUALLY
    EXIST on the server:

    1. the value used verbatim as the prefix (what a user reads off the
       collection names, and what the launcher stores as
       ``collection_prefix``);
    2. :func:`vco_lib.codegraph_naming.canonical_class_prefix` — the
       underscore-PRESERVING rule the analyzer uses to build code-graph class
       names (``vct_coordination`` → ``Vct_coordination``);
    3. :func:`vco_lib.codegraph_naming.sanitize_for_weaviate_class` — the
       underscore-DROPPING rule, because installs exist whose code classes were
       minted by the KG-side sanitizer.

    Raises :class:`RefDedupError` listing every live prefix when nothing
    matches. Guessing a prefix would point a WRITE at another project's rows,
    so there is no fallback arm.
    """
    from vco_lib.codegraph_naming import (
        canonical_class_prefix,
        sanitize_for_weaviate_class,
    )

    candidates: List[str] = [project]
    for derive in (canonical_class_prefix, sanitize_for_weaviate_class):
        try:
            derived = derive(project)
        except Exception:  # noqa: BLE001 — a rule that rejects the name just
            continue       # means that rule offers no candidate.
        if derived and derived not in candidates:
            candidates.append(derived)

    for candidate in candidates:
        if collections_for_prefix(candidate, collections):
            return candidate

    live = prefixes_of(collections)
    raise RefDedupError(
        f"no code-graph collections for project {project!r} "
        f"(tried prefixes: {', '.join(repr(c) for c in candidates)}). "
        f"Available prefixes: {', '.join(p or '(unprefixed)' for p in live) or 'none'}"
    )


# ---------------------------------------------------------------------------
# I/O layer — one seam, mockable, loud on failure
# ---------------------------------------------------------------------------


def _http_request(method: str, url: str, *, body: Any = None,
                  timeout: float = 60.0) -> Tuple[int, bytes]:
    """Module-level delegator to :func:`vco_lib.weaviate_helpers.http_request`.

    Same mock-seam convention as ``collection_repair._http_request``. ``body``
    is widened to ``Any``: the reference endpoint takes a JSON ARRAY while the
    shared helper annotates ``dict``. ``json.dumps`` serialises both — the
    annotation is the only thing narrower than reality, hence the cast.
    """
    return _wh.http_request(
        method, url, body=cast("Optional[dict]", body), timeout=timeout
    )


RequestFn = Callable[..., Tuple[int, bytes]]


def _get_json(url: str, request: RequestFn, what: str) -> Any:
    status, raw = request("GET", url)
    if status != 200:
        raise RefDedupError(f"{what}: HTTP {status} from {url} — {raw[:200]!r}")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RefDedupError(f"{what}: unparsable response from {url}: {exc}")


def list_code_collections(base_url: str, *,
                          request: Optional[RequestFn] = None) -> List[str]:
    """Every code-graph class on the server, sorted.

    LOUD on failure: a non-200, an unparsable body or a payload with no
    ``classes`` array raises :class:`RefDedupError`, and a transport exception
    from ``request`` propagates unchanged. An empty list therefore means the
    server was READ and holds no repairable class — never "the server could not
    be reached", which here would be indistinguishable from "nothing needs
    repairing" (the silent-zero antipattern the repo has a KG node about).

    v0.2.92 W18 — the contract above replaces a justification-by-contrast with
    two sibling helpers "which return ``[]``". Both halves of that sentence had
    become false (one helper was deleted for having no callers, the other now
    raises), and the whole repo answers "which classes exist?" tri-state, so
    there is nothing left to contrast with: this function's own contract is the
    documentation.

    NOT YET re-homed onto ``weaviate_helpers.probe_class_listing`` — which would
    delete this module's last raw ``/v1/schema`` fetch — because that helper
    CATCHES transport exceptions to build its ``unknown`` state, while
    ``tests/test_v0292_codegraph_ref_dedup.py::test_transport_exception_is_not_swallowed``
    pins ``OSError`` propagating out of here verbatim. The re-home plus that
    test's one-line update belong together, in the hands of whoever holds that
    test file; the recipe is in the W18 lane report.
    """
    req = request or _http_request
    payload = _get_json(base_url.rstrip("/") + "/v1/schema", req, "schema fetch")
    classes = payload.get("classes") if isinstance(payload, dict) else None
    if not isinstance(classes, list):
        raise RefDedupError("schema fetch: no 'classes' array in response")
    names = [
        c.get("class") for c in classes
        if isinstance(c, dict) and isinstance(c.get("class"), str)
    ]
    return sorted(n for n in names if n and reference_props_for(n))


def fetch_page(base_url: str, collection: str, *, after: Optional[str],
               limit: int, request: RequestFn) -> List[dict]:
    """One page of objects, cursor-paginated. Raises on any read failure."""
    url = (f"{base_url.rstrip('/')}/v1/objects"
           f"?class={collection}&limit={int(limit)}")
    if after:
        url += f"&after={after}"
    payload = _get_json(url, request, f"list {collection}")
    objects = payload.get("objects") if isinstance(payload, dict) else None
    if objects is None:
        return []
    if not isinstance(objects, list):
        raise RefDedupError(f"list {collection}: 'objects' is not a list")
    return [o for o in objects if isinstance(o, dict)]


def target_exists(base_url: str, beacon: str, *, request: RequestFn,
                  cache: Dict[str, Optional[bool]]) -> Optional[bool]:
    """Does the row a beacon points at still exist?  ``None`` = "cannot tell".

    Answers the question the DRY RUN could not: a row whose beacons include a
    deleted target will be rejected wholesale at write time, so predicting it
    is the difference between "4,169 rows would collapse" and the truth, which
    was that every one of them would be refused.

    Cached by ``class/uuid`` across the whole run — dead targets repeat heavily
    (one missing module accounted for 186 rows on this machine), so the cost is
    bounded by DISTINCT targets, not by rows.

    ``None`` for a class-less beacon (no collection to address) and for any
    non-200/404 answer: unknown is not "missing", and this diagnostic must
    never manufacture a skip it cannot justify.
    """
    tail = beacon.replace(_BEACON_SCHEME, "", 1)
    tail = tail.split("/", 1)[-1] if "/" in tail else tail
    if "/" not in tail:
        return None                      # class-less: nothing to address
    key = tail
    if key in cache:
        return cache[key]
    try:
        status, _ = request(
            "GET", f"{base_url.rstrip('/')}/v1/objects/{key}")
    except Exception:  # noqa: BLE001 — a probe failure is "unknown", not "gone"
        cache[key] = None
        return None
    result: Optional[bool] = True if status == 200 else (
        False if status == 404 else None)
    cache[key] = result
    return result


def fetch_object(base_url: str, collection: str, uid: str, *,
                 request: RequestFn) -> dict:
    """One object by UUID — the authoritative single-row read. Raises on
    failure."""
    url = f"{base_url.rstrip('/')}/v1/objects/{collection}/{uid}"
    payload = _get_json(url, request, f"get {collection}/{uid}")
    if not isinstance(payload, dict):
        raise RefDedupError(f"get {collection}/{uid}: response is not an object")
    return payload


def put_references(base_url: str, collection: str, uid: str, prop: str,
                   beacons: Sequence[str], *, request: RequestFn) -> None:
    """Replace ``prop``'s beacon list wholesale. THE only write in this module.

    ``PUT /v1/objects/<class>/<uuid>/references/<prop>`` — the same endpoint
    ``weaviate.collections.data.reference_replace`` uses, but carrying the
    caller's verbatim beacon strings instead of class-less rebuilds.

    An EMPTY ``beacons`` is refused here, at the I/O boundary, rather than only
    in the planner. An empty PUT clears the property, which is the one
    irreversible mistake this tool could make; putting the guard at the
    chokepoint means no future caller can route around it.
    """
    if not beacons:
        raise RefDedupError(
            f"refusing to PUT an empty reference list to "
            f"{collection}/{uid}.{prop} — that would DELETE every edge"
        )
    body = [{"beacon": b} for b in beacons]
    url = (f"{base_url.rstrip('/')}/v1/objects/{collection}/{uid}"
           f"/references/{prop}")
    status, raw = request("PUT", url, body=body)
    if status not in (200, 204):
        raise RefDedupError(
            f"reference replace {collection}/{uid}.{prop}: HTTP {status} "
            f"— {raw[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def classify_write_rejection(message: str) -> Optional[RowPlan]:
    """Turn a KNOWN Weaviate write rejection into a named skip, else ``None``.

    Only the dangling-target rejection is classified, because it is the only
    one observed on real data and the only one with a remedy a user can act on.
    Everything else stays an ``error`` — a repair tool that files unknown
    failures under a friendly label is lying about what it knows.

    Returns a template plan carrying the reason/detail; the caller fills in
    collection/uuid/prop and counts.
    """
    match = _DANGLING_RE.search(message or "")
    if not match:
        return None
    return RowPlan("", "", "", ACTION_SKIP, REASON_DANGLING_TARGET, 0, 0,
                   detail=f"target {match.group(1)} no longer exists")


@dataclass
class CollectionReport:
    """Per-collection tally. ``truncated`` means the view was INCOMPLETE.

    Two beacon accountings, deliberately distinct — conflating them was the
    first thing that misled a reader of this tool's own output:

    * ``beacons_seen`` / ``beacons_seen_unique`` — EVERY readable beacon in the
      collection, including rows that need no change. This is "how big is the
      edge store, and how big would it be with no duplicates".
    * ``beacons_before`` / ``beacons_after`` — only the rows that change. This
      is "what this run is about to touch".

    ``beacons_removed`` is the same number either way, which is the invariant
    that keeps the two views honest.
    """

    collection: str
    rows_inspected: int = 0
    pairs_inspected: int = 0
    rows_changed: int = 0
    beacons_seen: int = 0
    beacons_seen_unique: int = 0
    beacons_before: int = 0
    beacons_after: int = 0
    beacons_removed: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    skipped_rows: List[Dict[str, Any]] = field(default_factory=list)
    changed_rows: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    truncated: bool = False
    writes: int = 0

    def note_skip(self, plan: RowPlan) -> None:
        self.skipped[plan.reason] = self.skipped.get(plan.reason, 0) + 1
        self.skipped_rows.append(plan.as_dict())

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "collection": self.collection,
            "rows_inspected": self.rows_inspected,
            "pairs_inspected": self.pairs_inspected,
            "rows_changed": self.rows_changed,
            "beacons_seen": self.beacons_seen,
            "beacons_seen_unique": self.beacons_seen_unique,
            "beacons_before": self.beacons_before,
            "beacons_after": self.beacons_after,
            "beacons_removed": self.beacons_removed,
            "writes": self.writes,
            "skipped": dict(self.skipped),
            "skipped_total": self.skipped_total,
            "skipped_rows": self.skipped_rows,
            "changed_rows": self.changed_rows,
            "errors": self.errors,
            "truncated": self.truncated,
        }


@dataclass
class RunReport:
    """Whole-run rollup."""

    apply: bool
    weaviate_url: str
    scope: str
    collections: List[CollectionReport] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    write_cap_reached: bool = False
    checked_targets: bool = False

    def _sum(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.collections)

    @property
    def rows_inspected(self) -> int:
        return self._sum("rows_inspected")

    @property
    def rows_changed(self) -> int:
        return self._sum("rows_changed")

    @property
    def beacons_seen(self) -> int:
        return self._sum("beacons_seen")

    @property
    def beacons_seen_unique(self) -> int:
        return self._sum("beacons_seen_unique")

    @property
    def beacons_removed(self) -> int:
        return self._sum("beacons_removed")

    @property
    def beacons_before(self) -> int:
        return self._sum("beacons_before")

    @property
    def beacons_after(self) -> int:
        return self._sum("beacons_after")

    @property
    def skipped_total(self) -> int:
        return sum(c.skipped_total for c in self.collections)

    @property
    def error_total(self) -> int:
        return len(self.errors) + sum(len(c.errors) for c in self.collections)

    @property
    def truncated_collections(self) -> List[str]:
        return [c.collection for c in self.collections if c.truncated]

    def by_project(self) -> List[Dict[str, Any]]:
        """Per-PROJECT rollup, ascending by work remaining.

        The report used to be per-collection only, which meant choosing a small
        project to try first — the sane way to approach a 57,000-row repair —
        required exporting the JSON and grouping it by hand. Ascending order is
        the point: the first row is the canary.
        """
        acc: Dict[str, Dict[str, int]] = {}
        for col in self.collections:
            prefix = col.collection.rsplit("_", 1)[0]
            row = acc.setdefault(prefix, {
                "rows_changed": 0, "beacons_removed": 0,
                "rows_inspected": 0, "skipped": 0, "errors": 0})
            row["rows_changed"] += col.rows_changed
            row["beacons_removed"] += col.beacons_removed
            row["rows_inspected"] += col.rows_inspected
            row["skipped"] += col.skipped_total
            row["errors"] += len(col.errors)
        out = [dict(project=k, **v) for k, v in acc.items()]
        out.sort(key=lambda r: (r["beacons_removed"], r["rows_changed"],
                                r["skipped"], r["project"]))
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "apply": self.apply,
            "weaviate_url": self.weaviate_url,
            "scope": self.scope,
            "checked_targets": self.checked_targets,
            "totals": {
                "rows_inspected": self.rows_inspected,
                "rows_changed": self.rows_changed,
                "beacons_seen": self.beacons_seen,
                "beacons_seen_unique": self.beacons_seen_unique,
                "beacons_before": self.beacons_before,
                "beacons_after": self.beacons_after,
                "beacons_removed": self.beacons_removed,
                "skipped": self.skipped_total,
                "errors": self.error_total,
                "truncated_collections": self.truncated_collections,
                "write_cap_reached": self.write_cap_reached,
            },
            "projects": self.by_project(),
            "collections": [c.as_dict() for c in self.collections],
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Repair engine
# ---------------------------------------------------------------------------


def _apply_plan(base_url: str, plan: RowPlan, *, request: RequestFn,
                recheck: bool, verify: bool) -> Tuple[RowPlan, Optional[str]]:
    """Write ONE collapsed row. Returns ``(effective_plan, error)``.

    ``effective_plan`` is what actually happened: with ``recheck`` on (the
    default) the row is re-read and RE-PLANNED immediately before the write, so
    the payload always derives from a read taken microseconds earlier rather
    than from a page fetched up to 100 rows ago. If the fresh read no longer
    warrants a collapse — a concurrent analyzer touched it, or a previous run
    already did — the returned plan carries that action and NOTHING is written.
    """
    effective = plan
    if recheck:
        obj = fetch_object(base_url, plan.collection, plan.uuid,
                           request=request)
        fresh_value = (obj.get("properties") or {}).get(plan.prop)
        fresh = plan_row(plan.collection, plan.uuid, plan.prop, fresh_value)
        if fresh.action != ACTION_COLLAPSE:
            reason = (REASON_CHANGED_SINCE_SCAN
                      if fresh.reason == REASON_ALREADY_UNIQUE
                      else fresh.reason)
            return (RowPlan(plan.collection, plan.uuid, plan.prop,
                            fresh.action, reason, fresh.before, fresh.after,
                            detail=fresh.detail), None)
        effective = fresh

    # Re-assert the invariant on the exact bytes about to be written. Cheap,
    # and it is the last line of defence before an irreversible write.
    if len(set(effective.target_uuids)) != len(effective.beacons):
        return (_skip(plan.collection, plan.uuid, plan.prop, REASON_INVARIANT,
                      before=effective.before,
                      detail="payload/target mismatch at write time"), None)

    put_references(base_url, effective.collection, effective.uuid,
                   effective.prop, effective.beacons, request=request)

    if verify:
        obj = fetch_object(base_url, effective.collection, effective.uuid,
                           request=request)
        stored = (obj.get("properties") or {}).get(effective.prop)
        after_plan = plan_row(effective.collection, effective.uuid,
                              effective.prop, stored)
        expected = set(effective.target_uuids)
        got = set(after_plan.target_uuids)
        if after_plan.action == ACTION_SKIP:
            return (effective, f"post-write read unreadable: {after_plan.reason}")
        if got != expected:
            lost = sorted(expected - got)
            gained = sorted(got - expected)
            return (effective,
                    f"post-write target set differs (lost={lost}, "
                    f"gained={gained}); re-run the analyzer to restore "
                    f"any lost edge")
        if after_plan.before != effective.after:
            return (effective,
                    f"post-write beacon count {after_plan.before} != "
                    f"expected {effective.after}")
    return (effective, None)


def repair_collection(
    base_url: str,
    collection: str,
    *,
    props: Optional[Sequence[str]] = None,
    apply: bool = False,
    request: Optional[RequestFn] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    recheck: bool = True,
    verify: bool = True,
    write_budget: Optional[List[int]] = None,
    check_targets: bool = False,
    target_cache: Optional[Dict[str, Optional[bool]]] = None,
    on_row: Optional[Callable[[RowPlan], None]] = None,
) -> CollectionReport:
    """Scan (and optionally repair) one collection. Never raises for row-level
    trouble — every problem lands in the returned report.

    ``write_budget`` is a one-element mutable counter shared across
    collections so ``--max-writes`` caps a whole run, not each collection.
    """
    req = request or _http_request
    cache: Dict[str, Optional[bool]] = (
        target_cache if target_cache is not None else {})
    report = CollectionReport(collection=collection)
    wanted = tuple(props) if props is not None else reference_props_for(collection)
    if not wanted:
        return report

    after: Optional[str] = None
    while True:
        try:
            objects = fetch_page(base_url, collection, after=after,
                                 limit=page_size, request=req)
        except Exception as exc:  # noqa: BLE001 — fail closed, keep going
            report.errors.append(f"page read failed (after={after}): {exc}")
            report.truncated = True
            break
        if not objects:
            break
        for obj in objects:
            uid = obj.get("id")
            if not isinstance(uid, str) or not uid:
                report.errors.append("object with no id in page; cannot address it")
                report.truncated = True
                continue
            report.rows_inspected += 1
            properties = obj.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            for prop in wanted:
                plan = plan_row(collection, uid, prop, properties.get(prop))
                report.pairs_inspected += 1
                if on_row is not None:
                    on_row(plan)
                if plan.action == ACTION_SKIP:
                    report.note_skip(plan)
                    continue
                # Collection-wide accounting covers rows that need no change,
                # so the report can say how big the edge store actually is —
                # not just how much this run touches.
                report.beacons_seen += plan.before
                report.beacons_seen_unique += plan.after
                if plan.action != ACTION_COLLAPSE:
                    continue
                if check_targets:
                    dead = next(
                        (b for b in plan.beacons
                         if target_exists(base_url, b, request=req,
                                          cache=cache) is False),
                        None)
                    if dead is not None:
                        # Predicting the rejection beats discovering it: in
                        # dry-run it makes the estimate honest, and in apply
                        # it saves a PUT that Weaviate would refuse anyway.
                        report.note_skip(RowPlan(
                            collection, uid, prop, ACTION_SKIP,
                            REASON_DANGLING_TARGET, plan.before, plan.before,
                            detail=(f"target "
                                    f"{beacon_target_uuid(dead)} no longer "
                                    f"exists")))
                        continue
                if (write_budget is not None and apply
                        and write_budget[0] <= 0):
                    # Budget exhausted: stop WRITING but keep the accounting
                    # honest — the row is still reported as needing work.
                    report.changed_rows.append(
                        dict(plan.as_dict(), applied=False,
                             note="write cap reached"))
                    continue
                if not apply:
                    report.rows_changed += 1
                    report.beacons_before += plan.before
                    report.beacons_after += plan.after
                    report.beacons_removed += plan.removable
                    report.changed_rows.append(
                        dict(plan.as_dict(), applied=False))
                    continue
                try:
                    effective, err = _apply_plan(
                        base_url, plan, request=req,
                        recheck=recheck, verify=verify)
                except Exception as exc:  # noqa: BLE001
                    known = classify_write_rejection(str(exc))
                    if known is not None:
                        # A rejection we understand and have a remedy for.
                        # Weaviate validates the whole list before applying
                        # any of it, so the row is untouched (verified on
                        # live data, 2026-09-02).
                        report.note_skip(RowPlan(
                            collection, uid, prop, ACTION_SKIP, known.reason,
                            plan.before, plan.before, detail=known.detail))
                        continue
                    report.errors.append(
                        f"{collection}/{uid}.{prop}: {exc}")
                    continue
                if effective.action != ACTION_COLLAPSE:
                    if effective.action == ACTION_SKIP:
                        report.note_skip(effective)
                    else:
                        report.skipped[effective.reason] = (
                            report.skipped.get(effective.reason, 0) + 1)
                        report.skipped_rows.append(effective.as_dict())
                    continue
                report.writes += 1
                if write_budget is not None:
                    write_budget[0] -= 1
                report.rows_changed += 1
                report.beacons_before += effective.before
                report.beacons_after += effective.after
                report.beacons_removed += effective.removable
                entry = dict(effective.as_dict(), applied=True)
                if err:
                    entry["verification_error"] = err
                    report.errors.append(
                        f"{collection}/{uid}.{prop}: {err}")
                report.changed_rows.append(entry)
            after = uid
        if len(objects) < page_size:
            break
    return report


def run(
    *,
    weaviate_url: Optional[str] = None,
    project: Optional[str] = None,
    all_projects: bool = False,
    apply: bool = False,
    props: Optional[Sequence[str]] = None,
    request: Optional[RequestFn] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    recheck: bool = True,
    verify: bool = True,
    max_writes: Optional[int] = None,
    check_targets: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> RunReport:
    """Scan (and optionally repair) one project or every project.

    Exactly one of ``project`` / ``all_projects`` must select a scope — there
    is no implicit "everything", because a stranger's first invocation should
    not silently address every project on their machine.
    """
    if bool(project) == bool(all_projects):
        raise RefDedupError(
            "choose a scope: --project <name> or --all-projects")
    base = (weaviate_url or _wh.weaviate_url_default()).rstrip("/")
    req = request or _http_request
    live = list_code_collections(base, request=req)
    if not live:
        raise RefDedupError(
            f"no code-graph collections exist on {base} — nothing to repair "
            "(if you expected some, check WEAVIATE_URL)")

    if project:
        prefix = resolve_prefix(project, live)
        targets = collections_for_prefix(prefix, live)
        scope = f"project {project!r} (prefix {prefix or '(unprefixed)'})"
    else:
        targets = list(live)
        scope = f"all projects ({len(prefixes_of(live))} prefixes)"

    report = RunReport(apply=apply, weaviate_url=base, scope=scope,
                       checked_targets=check_targets)
    budget = [max_writes] if isinstance(max_writes, int) else None
    # One existence cache for the WHOLE run: a dead target is usually shared by
    # many rows across several collections.
    cache: Dict[str, Optional[bool]] = {}
    for name in targets:
        if progress:
            progress(name)
        report.collections.append(repair_collection(
            base, name, props=props, apply=apply, request=req,
            page_size=page_size, recheck=recheck, verify=verify,
            check_targets=check_targets, target_cache=cache,
            write_budget=cast("Optional[List[int]]", budget)))
    if budget is not None and budget[0] <= 0:
        report.write_cap_reached = True
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


#: Errors are never fully silenced by ``--max-rows-listed``: a cap meant to
#: shorten a per-row listing must not hide the reason a repair did not happen.
_MIN_ERRORS_SHOWN = 10


def _render(report: RunReport, *, max_rows: int, out: Any) -> None:
    mode = "APPLY" if report.apply else "DRY RUN (nothing written)"
    err_cap = max(max_rows, _MIN_ERRORS_SHOWN)
    print(f"code-graph reference dedup — {mode}", file=out)
    print(f"  server: {report.weaviate_url}", file=out)
    print(f"  scope : {report.scope}", file=out)
    print("", file=out)
    # `stored`/`unique` are collection-wide (every readable beacon);
    # `removed` is what a collapse takes out. `chg` rows are the subset
    # being written.
    header = (f"{'collection':<40} {'rows':>7} {'pairs':>7} {'chg':>6} "
              f"{'stored':>10} {'unique':>8} {'removed':>10} {'skip':>5} "
              f"{'err':>4}")
    print(header, file=out)
    print("-" * len(header), file=out)
    for col in report.collections:
        if (col.rows_changed == 0 and col.skipped_total == 0
                and not col.errors and not col.truncated):
            continue
        print(f"{col.collection:<40} {col.rows_inspected:>7} "
              f"{col.pairs_inspected:>7} {col.rows_changed:>6} "
              f"{col.beacons_seen:>10} {col.beacons_seen_unique:>8} "
              f"{col.beacons_removed:>10} {col.skipped_total:>5} "
              f"{len(col.errors):>4}", file=out)
        shown = 0
        for row in col.changed_rows:
            if shown >= max_rows:
                break
            mark = "applied" if row.get("applied") else "would collapse"
            note = f"  [{row['note']}]" if row.get("note") else ""
            verr = (f"  !! {row['verification_error']}"
                    if row.get("verification_error") else "")
            print(f"    {row['uuid']} .{row['property']:<16} "
                  f"{row['before']:>7} -> {row['after']:<6} "
                  f"({row['removable']} removed, {mark}){note}{verr}", file=out)
            shown += 1
        if len(col.changed_rows) > shown:
            print(f"    … and {len(col.changed_rows) - shown} more changed "
                  f"row(s) not listed (--max-rows-listed / --json for all)",
                  file=out)
        if col.skipped:
            for reason, count in sorted(col.skipped.items()):
                print(f"    SKIPPED {count} row(s): {reason}", file=out)
            for row in col.skipped_rows[:max_rows]:
                detail = f" — {row['detail']}" if row.get("detail") else ""
                print(f"      {row['uuid']} .{row['property']} "
                      f"({row['reason']}{detail})", file=out)
            hidden = len(col.skipped_rows) - min(max_rows,
                                                 len(col.skipped_rows))
            if hidden:
                more = "more " if max_rows else ""
                print(f"      … and {hidden} {more}skipped row(s) not listed "
                      f"(--json for all)", file=out)
        if col.truncated:
            print("    !! INCOMPLETE VIEW — a page read failed; rows beyond "
                  "it were not inspected", file=out)
        for err in col.errors[:err_cap]:
            print(f"    !! {err}", file=out)
        if len(col.errors) > err_cap:
            print(f"    !! … and {len(col.errors) - err_cap} more error(s) "
                  f"(--json for all)", file=out)

    print("", file=out)
    verb = "collapsed" if report.apply else "would collapse"
    print(f"TOTAL: {report.rows_inspected} rows inspected, "
          f"{report.beacons_seen} beacons stored across "
          f"{report.beacons_seen_unique} distinct edges; "
          f"{report.rows_changed} row(s) {verb}, "
          f"{report.beacons_removed} beacon(s) removed, "
          f"{report.skipped_total} skipped, {report.error_total} error(s)",
          file=out)
    projects = [p for p in report.by_project()
                if p["rows_changed"] or p["skipped"] or p["errors"]]
    if len(projects) > 1:
        print("", file=out)
        print("By project (ascending — the first is the safest to try first):",
              file=out)
        for proj in projects:
            print(f"  {proj['project']:<34} {proj['rows_changed']:>7} row(s), "
                  f"{proj['beacons_removed']:>9} beacon(s), "
                  f"{proj['skipped']:>6} skipped, {proj['errors']:>4} error(s)",
                  file=out)
    reasons: Dict[str, int] = {}
    for col in report.collections:
        for reason, count in col.skipped.items():
            reasons[reason] = reasons.get(reason, 0) + count
    for reason, count in sorted(reasons.items()):
        remedy = SKIP_REMEDIES.get(reason, "no remedy recorded — please report")
        print(f"SKIPPED {count} row(s) — {reason}: {remedy}", file=out)
    if report.truncated_collections:
        print(f"INCOMPLETE: {len(report.truncated_collections)} collection(s) "
              f"were not fully read: "
              f"{', '.join(report.truncated_collections)}", file=out)
    if report.write_cap_reached:
        print("NOTE: --max-writes cap was reached; re-run to continue.",
              file=out)
    if report.rows_changed and not report.checked_targets:
        # The gap this line closes: on real data 4,169 of 4,169 "would
        # collapse" rows were in fact rejected at write time, and the dry run
        # had no way to say so. Now it at least says the check exists.
        print("NOTE: rows whose beacons include a DELETED target are refused "
              "by Weaviate and cannot be collapsed. This run did not check. "
              "Add --check-targets to find them in advance (one read per "
              "distinct target, cached).", file=out)
    if not report.apply and report.rows_changed:
        print("Nothing was written. Re-run with --apply to collapse.", file=out)
    if report.apply and report.rows_changed:
        # How to check this run without taking the tool's word for it.
        print("To verify: re-run WITHOUT --apply; it must report 0 rows to "
              "collapse.", file=out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m vco_lib.codegraph_ref_dedup",
        description=(
            "Collapse duplicate code-graph cross-reference beacons to their "
            "ordered-unique set. Dry-run by default."),
        epilog=(
            "The set of distinct targets each row points at is identical "
            "before and after; only exact duplicates are removed. Rows that "
            "cannot be read positively are skipped and reported, never "
            "written."),
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project", help="repair ONE project (name or class prefix)")
    scope.add_argument("--all-projects", action="store_true",
                       help="repair every project on this server")
    parser.add_argument("--apply", action="store_true",
                        help="actually write (default: dry run)")
    parser.add_argument("--weaviate-url", default=None,
                        help="default: $WEAVIATE_URL or http://localhost:8081")
    parser.add_argument("--props", default=None,
                        help="comma-separated reference properties to consider "
                             "(default: every reference property the "
                             "collection carries)")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-writes", type=int, default=None,
                        help="stop writing after N rows (canary runs)")
    parser.add_argument("--no-recheck", action="store_true",
                        help="skip the authoritative re-read immediately "
                             "before each write (faster, wider race window)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the post-write read-back")
    parser.add_argument("--check-targets", action="store_true",
                        help="before counting a row as collapsible, confirm "
                             "each of its targets still exists. Weaviate "
                             "refuses a replace that names a deleted row, so "
                             "without this a dry run can promise work that "
                             "will be rejected. Costs one read per distinct "
                             "target (cached across the run).")
    parser.add_argument("--max-rows-listed", type=int, default=10,
                        help="per-collection cap on per-row lines (default 10)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable report on stdout")
    args = parser.parse_args(list(argv) if argv is not None else None)

    props = None
    if args.props:
        props = [p.strip() for p in args.props.split(",") if p.strip()]

    # Human output goes to stderr in --json mode so stdout stays a clean
    # machine contract (v0.2.84 lesson: stdout IS an interface).
    out = sys.stderr if args.json else sys.stdout
    if args.apply:
        print("WARNING: writing to live collections. Do NOT run a code-graph "
              "analysis concurrently — a row collapsed while the analyzer is "
              "adding edges can lose the edge it just added.", file=out)
    try:
        report = run(
            weaviate_url=args.weaviate_url,
            project=args.project,
            all_projects=args.all_projects,
            apply=args.apply,
            props=props,
            page_size=max(1, args.page_size),
            recheck=not args.no_recheck,
            verify=not args.no_verify,
            max_writes=args.max_writes,
            check_targets=args.check_targets,
            progress=(lambda n: print(f"  scanning {n} …", file=out)),
        )
    except RefDedupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report.as_dict(), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
    _render(report, max_rows=max(0, args.max_rows_listed), out=out)

    # Exit codes are a machine contract, and "some work is owed" is NOT the
    # same event as "something went wrong". Conflating them made a clean run
    # with 2 unrepairable rows look like a failure during this tool's own
    # first live use, which cost a detour to find out nothing had broken.
    #   0 — clean, nothing owed
    #   1 — errors, or a collection could not be fully read
    #   2 — could not run at all (bad scope, server unreachable)
    #   3 — completed cleanly, but rows remain owed (skips)
    if report.error_total or report.truncated_collections:
        return 1
    if report.skipped_total:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
