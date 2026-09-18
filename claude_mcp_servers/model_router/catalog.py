# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The union ``/v1/models`` catalog.

Claude Code queries the gateway's model list once at startup (with
``CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1``) and shows what comes back in
the ``/model`` picker. This module builds that list from the live upstreams and
falls back when a fetch fails.

There are two fallback sources and one rule between them: a vendor row may
carry its own ids in ``Vendor.static_ids``, and when it does they win over the
shipped ``static_catalog.json`` block for that vendor. ``_resolve_static_tables``
holds the reasoning. Every shipped row leaves the field empty, so today's
behaviour is snapshot-only; the field exists so that adding a vendor stays a
config change end to end, fallback included.

Vendor-neutral by construction: every upstream, credential and namespace
arrives as a :class:`~model_router.vendors.Vendor` row or the
:class:`~model_router.vendors.AnthropicFamily` descriptor, and
``tests/test_model_router_routing.py`` fails if a vendor-specific literal
appears in this module's source.

Two behaviours that differ from the field prototype, both deliberate:

* **A static fallback is retried far sooner than a live catalog.** The
  prototype cached the fallback under the same six-hour TTL as a live result,
  so a one-minute vendor outage cost six hours of a stale picker. Here a
  family being served from the snapshot is retried on the short TTL.
* **The source is reported, never inferred.** The response carries
  ``_vct_catalog_source`` per family and ``/health`` carries the same values,
  so "the picker looks short today" has an answer that does not require
  reading the log.

What the CLIENT actually reads
------------------------------
Exactly three fields per row: ``id``, ``display_name``, ``description``
(Anthropic's llm-gateway-protocol, "Model discovery"; verified 2026-09-16).
It keeps a row only when the ``id`` contains ``claude`` or ``anthropic``,
which is what the vendor namespace is for, and it renders ``description``
collapsed to one line in the picker. It reads no window field at all: **the
context window it budgets is keyed on the ID** — an id carrying ``[1m]`` is
budgeted at 1M, and behind a gateway anything else is budgeted at 200K.

Three consequences shape this module:

* the ``[1m]`` row is the ONLY way to buy the client's 1M budget, so which
  rows get one is a decision and not a cosmetic (:func:`resolve_window`);
* ``description`` is the one place a row can tell the user the truth about
  its window, including when the client's assumption for that row is WRONG;
* the upstream window fields are relayed anyway (:class:`CatalogEntry`), for
  the proxy invariant and for the consumers that are not Claude Code — but
  nothing about the context bar depends on them.

Windows, and the rule against guessing
--------------------------------------
A row's window resolves in one order — table, upstream, family floor — and
the answer says WHICH (``_vct_window_source`` per row). The floor is the
owner's "auto-update" requirement made concrete: a model that ships tomorrow,
which no table names and whose upstream may state nothing, inherits the
window of the previous version in its own family rather than reading as
unverified. It only ever inherits DOWNWARD in time (newer from older), never
across families and never across vendors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence

# ``ONE_M_WINDOW`` (what the client budgets for an id carrying ``[1m]``, and
# the threshold at which a row EARNS that suffix) lives with the table that
# decides windows, not here: `context_table` answers the same question for the
# settings writer, and two spellings of 1 000 000 in one package is the
# duplication the modularity rule forbids. Re-exported in ``__all__`` below,
# so every existing `catalog.ONE_M_WINDOW` reader is unaffected.
from .context_table import ONE_M_WINDOW, ContextTable
from .model_family import ModelIdParts, is_older_sibling, parse_model_id
from .routing import ONE_M_SUFFIX, advertised_id, with_1m
from .vendors import (
    DEFAULT_ANTHROPIC_VERSION,
    OAUTH_BETA,
    AnthropicFamily,
    Vendor,
    vendor_display_name,
)

logger = logging.getLogger(__name__)

#: Shipped snapshot, beside this module (so it travels inside the wheel).
STATIC_CATALOG_PATH = Path(__file__).resolve().parent / "static_catalog.json"

#: ``source`` values reported per family. ``unfetched`` and ``unavailable`` are
#: deliberately different words: the first means nobody has asked for the
#: catalog yet (the state ``/health`` reports before the picker has opened),
#: the second means a fetch happened and neither the upstream nor the shipped
#: snapshot produced a single model. Collapsing them would make a brand-new
#: daemon look broken, or a genuinely broken family look merely idle.
SOURCE_LIVE = "live"
SOURCE_STATIC = "static"
SOURCE_UNFETCHED = "unfetched"
SOURCE_EMPTY = "unavailable"

#: Publish only the newest version of each model family. The owner's
#: requirement (2026-09-10): a picker that lists Fable 5.1 AND Fable 5 AND
#: three superseded versions of one vendor model is a list to read past, not
#: a list to choose from.
CATALOG_FILTER_LATEST = "latest"

#: Publish every version both upstreams return. The escape hatch for the case
#: that makes latest-only safe to default: anyone who needs a previous version
#: — to reproduce a result, or because a newer one regressed — has a knob
#: rather than a lost capability.
CATALOG_FILTER_ALL = "all"

#: Accepted values of ``VCT_MODEL_GATEWAY_CATALOG``, in the order the error
#: message lists them.
CATALOG_FILTERS = (CATALOG_FILTER_LATEST, CATALOG_FILTER_ALL)

DEFAULT_CATALOG_FILTER = CATALOG_FILTER_LATEST

#: Appended to the display name of a ``[1m]`` companion entry so the picker
#: shows two distinguishable rows for one model rather than the same name
#: twice. It is spelled out because a first-party row has no vendor suffix
#: beside it — and it is also the phrase the plain row's ``description``
#: points at ("pick the (1M context) row"), so the two cannot drift.
ONE_M_DISPLAY_SUFFIX = " (1M context)"

#: A VENDOR row carries the suffix ON itself rather than as a second row, so
#: its display name says so inline instead of gaining a twin.
VENDOR_ONE_M_DISPLAY_MARKER = " · 1M ctx"

#: What Claude Code budgets for a model id it does not recognise when it is
#: pointed at a gateway — a plain first-party id included. Anthropic's
#: model-config doc ("Correct the window for a gateway or custom model ID").
#: This is an assumption the CLIENT makes about an ID; nothing the gateway
#: publishes in a row can change it, which is why a row whose real window
#: differs says so in its ``description``.
CLIENT_DEFAULT_WINDOW = 200_000


#: ``WindowResolution.source`` values. Reported per row as
#: ``_vct_window_source`` so "why does this row say 200K?" is answerable
#: without re-deriving the decision.
WINDOW_TABLE = "table"
WINDOW_UPSTREAM = "upstream"
WINDOW_DELETED = "deleted"
WINDOW_UNKNOWN = "unknown"
#: Prefix of the inherited source; the suffix names the id inherited FROM, so
#: a wrong inheritance is visible in the response rather than only in a log.
WINDOW_INHERITED_PREFIX = "inherited:"

#: Rendered in place of a number nobody has stated. Deliberately a word and
#: not a plausible default: a made-up window is the failure this whole
#: resolution order exists to avoid, and it must read as absent in the picker.
UNVERIFIED_WINDOW = "unverified"

#: Ids already named in an inheritance log line, so a picker refresh every few
#: hours does not reprint the same sentence. Process-wide and unbounded in
#: principle, bounded in practice by the number of models two upstreams ship.
_INHERITED_LOGGED: set[tuple[str, str]] = set()

#: Ids already named in a table-disagreement warning. Same reasoning.
_WINDOW_DISAGREEMENT_LOGGED: set[str] = set()


@dataclass(frozen=True)
class CatalogEntry:
    """One picker row, already namespaced and already ``[1m]``-decorated.

    **The window fields are a relay, not the context bar.** Claude Code reads
    ``id``, ``display_name`` and ``description`` from a gateway's
    ``/v1/models`` and nothing else; the window it budgets is keyed on the ID
    (an ``[1m]`` suffix means 1M, anything else behind a gateway means 200K).
    So publishing ``max_input_tokens`` does not fix a context bar and dropping
    it never broke one — the earlier note in this docstring claiming it did
    was wrong, and the ``[1m]`` companion rows are the mechanism that actually
    corrects the budget.

    They are carried anyway, for two reasons that stand on their own: the
    proxy invariant (what upstream states about a model must survive the hop —
    this gateway is not entitled to narrow its own upstream's answer), and the
    consumers that are not Claude Code, which do read them.

    ``None`` means "upstream did not say", never "zero": an absent field is
    omitted from the response rather than published as a wrong number, so a
    client falls back to its own default exactly as it would natively.
    """

    id: str
    display_name: str
    #: Context window in tokens, as upstream states it (``max_input_tokens``).
    max_input_tokens: Optional[int] = None
    #: Maximum output tokens for one response (``max_tokens``).
    max_tokens: Optional[int] = None
    #: Upstream's release timestamp. Also a tie-break in the latest-only
    #: filter, so "which is the newest model in this family?" survives two
    #: ids whose version numbers do not order them.
    created_at: Optional[str] = None
    #: Upstream's capability block, relayed verbatim. ``compare=False``: a
    #: nested dict must not make two otherwise-identical rows unequal, and
    #: this field is never an identity.
    capabilities: Optional[Mapping[str, object]] = field(
        default=None, compare=False,
    )
    #: The one row field the picker DISPLAYS besides the name. Built by
    #: :func:`describe_row`; empty on an entry assembled by hand.
    description: str = ""
    #: Which step of :func:`resolve_window` answered. Published as
    #: ``_vct_window_source``. ``compare=False`` for the same reason as
    #: ``capabilities``: it is provenance, never identity.
    window_source: str = field(default=WINDOW_UNKNOWN, compare=False)


@dataclass(frozen=True)
class WindowResolution:
    """A window, an output cap, and the step that produced them."""

    window: Optional[int]
    max_output: Optional[int]
    source: str


@dataclass(frozen=True)
class FamilyFloor:
    """The window a newer model inherits from an older one in its family."""

    model_id: str
    window: int
    max_output: Optional[int]


@dataclass(frozen=True)
class CatalogUnion:
    """What :meth:`CatalogService.union` answers with.

    A dataclass rather than a widening tuple: ``entries, sources = await
    union(...)`` read correctly right up to the day a third value was needed,
    and a tuple that grows silently breaks every caller at once, at runtime,
    with an unpacking error that names nothing.
    """

    entries: list[CatalogEntry]
    sources: dict[str, str]
    #: Ids the latest-only filter withheld. Published so a short picker has an
    #: answer that is not "the gateway lost my model" — the same reasoning as
    #: ``_vct_catalog_source``.
    hidden: list[str]


def _positive(value: Optional[int]) -> Optional[int]:
    """``None`` for anything that is not a usable token count."""
    if value is None or value <= 0:
        return None
    return value


def resolve_window(
    entry: CatalogEntry,
    *,
    table: ContextTable,
    family_floor: Optional[FamilyFloor],
) -> WindowResolution:
    """Decide one row's window. Table, then upstream, then the family floor.

    The order encodes who is entitled to be believed about a window:

    1. **a tombstone** — the user deleted this row in the GUI, and a deletion
       that anything downstream could undo is not a deletion. Answers
       ``deleted`` with no window, ahead of every other source;
    2. **the table, on the EXACT bare id** — the manual override, cited, and
       the only authority for a vendor whose windows must never be guessed
       (a standing instruction for the shipped one). A row is believed whole,
       damaged numbers included: a table that says zero is a data defect to
       fix in the table, not a licence to go looking elsewhere;
    3. **upstream's own ``max_input_tokens``** — the model's publisher, for a
       model nobody has tabulated;
    4. **the family floor** — the previous version of this same model, when
       neither of the above has anything to say. This is the auto-update
       requirement: a model that ships tomorrow is advertised at AT LEAST its
       predecessor's window instead of reading unverified until somebody
       edits a file. It is a floor and not a guess — the claim being made is
       "no smaller than the model it replaces", which is the conservative
       direction and the one a vendor has never violated;
    5. **nothing** — ``unknown``, and the row says ``unverified`` in the
       picker. The gateway does not invent a window.

    ``family_floor`` is supplied by the caller rather than computed here
    because it is a property of a GROUP, and mixing "what do I know about
    this row" with "what do I know about its siblings" is what would make
    this order impossible to read.
    """
    bare_id = parse_model_id(entry.id).bare_id

    if bare_id in table.tombstones:
        return WindowResolution(None, None, WINDOW_DELETED)

    row = table.lookup(bare_id)
    if row is not None:
        return WindowResolution(
            _positive(row.context_window), _positive(row.max_output), WINDOW_TABLE,
        )

    upstream = _positive(entry.max_input_tokens)
    if upstream is not None:
        return WindowResolution(
            upstream, _positive(entry.max_tokens), WINDOW_UPSTREAM,
        )

    if family_floor is not None:
        key = (bare_id, family_floor.model_id)
        if key not in _INHERITED_LOGGED:
            _INHERITED_LOGGED.add(key)
            logger.info(
                "model-gateway: %s states no context window and no table row "
                "names it; inheriting %d tokens from %s, the previous version "
                "in its family. Add a cited row to the chat-model context "
                "table to replace this with a verified figure.",
                bare_id, family_floor.window, family_floor.model_id,
            )
        return WindowResolution(
            family_floor.window,
            family_floor.max_output,
            f"{WINDOW_INHERITED_PREFIX}{family_floor.model_id}",
        )

    return WindowResolution(None, None, WINDOW_UNKNOWN)


def _family_floor(
    entry: CatalogEntry,
    *,
    parts: Mapping[str, ModelIdParts],
    resolved: Mapping[str, WindowResolution],
    siblings: Sequence[CatalogEntry],
) -> Optional[FamilyFloor]:
    """The largest window among OLDER members of ``entry``'s own family.

    Three restrictions, each of which is the whole point of one of them:

    * **older only.** Inheritance runs newer-from-older and never the reverse.
      A 200K model that ships after a 1M one must not drag the 1M row down,
      and a 1M model that ships after a 200K one must not lend its window
      backwards to a model that never had it.
    * **same family, same catalog family.** ``siblings`` is always one
      upstream's entries, so one vendor's ``<name>`` can never see another
      vendor's, and the family name separates ``<name>`` from
      ``<name>-<variant>`` within one vendor.
    * **verified sources only.** A floor is built from ``table`` and
      ``upstream`` answers, never from another inherited one. Otherwise one
      unverified figure would propagate along a whole family and read, at the
      far end, exactly like a fact.
    """
    mine = parts[entry.id]
    best: Optional[FamilyFloor] = None
    for other in siblings:
        if other.id == entry.id:
            continue
        if not is_older_sibling(parts[other.id], mine):
            continue
        answer = resolved[other.id]
        if answer.source not in (WINDOW_TABLE, WINDOW_UPSTREAM):
            continue
        if answer.window is None:
            continue
        if best is None or answer.window > best.window:
            best = FamilyFloor(
                model_id=parse_model_id(other.id).bare_id,
                window=answer.window,
                max_output=answer.max_output,
            )
    return best


def resolve_family_windows(
    entries: Sequence[CatalogEntry],
    *,
    table: ContextTable,
    parts: Mapping[str, ModelIdParts],
) -> dict[str, WindowResolution]:
    """Resolve every row of ONE upstream, floors included.

    Two passes, because a floor can only be built out of answers that are
    already settled: pass one asks every row what it knows on its own, pass
    two offers a floor to the rows that answered ``unknown``. A single pass
    would make the result depend on the order the upstream happened to list
    its models in.
    """
    resolved = {
        entry.id: resolve_window(entry, table=table, family_floor=None)
        for entry in entries
    }
    for entry in entries:
        if resolved[entry.id].source != WINDOW_UNKNOWN:
            continue
        floor = _family_floor(
            entry, parts=parts, resolved=resolved, siblings=entries,
        )
        if floor is None:
            continue
        resolved[entry.id] = resolve_window(
            entry, table=table, family_floor=floor,
        )
    return resolved


def short_tokens(tokens: Optional[int]) -> str:
    """``1M`` / ``200K`` / ``128K``, or :data:`UNVERIFIED_WINDOW`.

    Decimal units, matching how both upstreams state their own figures (the
    context seed records the same: "the vendor's own decimal-K figures, not
    powers of two"). A row that says 200K when the vendor's page says 200K is
    a row nobody has to convert.
    """
    usable = _positive(tokens)
    if usable is None:
        return UNVERIFIED_WINDOW
    for unit, scale in (("M", 1_000_000), ("K", 1_000)):
        if usable >= scale:
            return f"{usable / scale:.1f}".rstrip("0").rstrip(".") + unit
    return str(usable)


def _window_qualifier(*, window: Optional[int], one_m_row: bool) -> str:
    """The one sentence that fires when the CLIENT will get this row wrong.

    The client budgets by id: 1M for a row carrying ``[1m]``, 200K for every
    other row behind a gateway. When that matches the row's real window there
    is nothing to say, and saying something anyway would turn the useful case
    into noise. Two cases do not match and both cost the user something real:

    * a row smaller than the client's default — the indicator reads full late
      and compaction fires after the upstream has already started refusing;
    * a 1M model's PLAIN row — usable, but at a fifth of the window the user
      is paying for, and the fix is one row further down the picker.

    The band between (a window above the default but below 1M, on a plain
    row) is deliberately silent: the client under-budgets there, but there is
    no second row to point at — this module only advertises ``[1m]`` at 1M —
    and neither sentence above would be true of it.
    """
    usable = _positive(window)
    if usable is None:
        return ""
    assumed = ONE_M_WINDOW if one_m_row else CLIENT_DEFAULT_WINDOW
    if usable == assumed:
        return ""
    if not one_m_row and usable < CLIENT_DEFAULT_WINDOW:
        return (
            f" · client budgets {short_tokens(CLIENT_DEFAULT_WINDOW)} — "
            "compaction fires late"
        )
    if not one_m_row and usable >= ONE_M_WINDOW:
        return (
            f" · {short_tokens(CLIENT_DEFAULT_WINDOW)} budget on this row; "
            f"pick the {ONE_M_DISPLAY_SUFFIX.strip()} row for the full window"
        )
    return ""


def describe_row(
    *,
    label: str,
    window: Optional[int],
    max_output: Optional[int],
    one_m_row: bool,
) -> str:
    """The ``description`` field — the one line the picker shows per row.

    Whose subscription answers, what the window is, what one response may be,
    and (only when they disagree) what the client will assume instead. Which
    subscription is first because it is the question a mixed picker raises on
    every single row, and the display name cannot always carry it.
    """
    return (
        f"{label} · {short_tokens(window)} context · "
        f"{short_tokens(max_output)} output"
        f"{_window_qualifier(window=window, one_m_row=one_m_row)}"
    )


def _warn_on_table_disagreement(
    bare_id: str, *, table: ContextTable, window: Optional[int],
) -> None:
    """A table row whose ``window_1m`` flag contradicts its own window.

    The two are independent fields in a hand-editable file, so they can
    disagree, and the disagreement is invisible: the flag used to decide the
    advert on its own, so a row reading ``{context_window: 200000,
    window_1m: true}`` advertised a 1M variant of a 200K model and nothing
    said so. The advert now follows the WINDOW — the number that is cited —
    and the contradiction is reported rather than resolved silently.
    """
    row = table.lookup(bare_id)
    if row is None:
        return
    derived = (window or 0) >= ONE_M_WINDOW
    if row.window_1m == derived or bare_id in _WINDOW_DISAGREEMENT_LOGGED:
        return
    _WINDOW_DISAGREEMENT_LOGGED.add(bare_id)
    logger.warning(
        "model-gateway: chat-model context row %r says window_1m=%s but "
        "context_window=%s; the advertised %s suffix follows the WINDOW "
        "(%s). Fix the row in %s — one of the two fields is wrong.",
        bare_id, row.window_1m, row.context_window,
        ONE_M_DISPLAY_SUFFIX.strip(),
        "advertised" if derived else "withheld",
        table.path,
    )


@dataclass(frozen=True)
class _Candidate:
    """One upstream row, resolved but not yet filtered or rendered.

    Exists so the latest-only filter can run on rows whose ``[1m]`` decision
    and published id are already settled: a row is hidden under the id the
    user would have SEEN, not under the vendor's internal spelling, and a
    hidden list in a different vocabulary from ``data`` answers nothing.
    """

    entry: CatalogEntry
    parts: ModelIdParts
    window: WindowResolution
    one_m: bool
    published_id: str
    display_name: str


def _latest_key(candidate: _Candidate) -> tuple[tuple[int, ...], str, str]:
    """The ordering the latest-only filter maximises over one family.

    Version first (the generation a user reads in the name), then upstream's
    ``created_at``, then the id's own date. The two tie-breaks exist because
    a version number alone cannot order every pair an upstream ships — a
    re-released id at the same version, or two dated ids in one generation —
    and a tie that resolves arbitrarily would make the picker's contents
    depend on the order a fetch happened to return.
    """
    return (
        candidate.parts.version,
        candidate.entry.created_at or "",
        candidate.parts.date or "",
    )


def _latest_only(
    candidates: Sequence[_Candidate], *, catalog_filter: str,
) -> tuple[list[_Candidate], list[_Candidate]]:
    """Split into (kept, hidden) by family. Input order is preserved.

    ``all`` keeps everything and hides nothing, which is the honest shape of
    the escape hatch: the knob must not merely re-order a truncated list.
    """
    if catalog_filter != CATALOG_FILTER_LATEST:
        # ``all`` — and anything unrecognised. :func:`resolve_catalog_filter`
        # refuses an unknown value at startup, so this branch is reachable
        # only from a direct call, where failing OPEN (withhold nothing) is
        # the answer that cannot cost somebody a model they wanted.
        return list(candidates), []
    winners: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        best = winners.get(candidate.parts.family)
        if best is None or _latest_key(candidate) > _latest_key(candidates[best]):
            winners[candidate.parts.family] = index
    kept_indices = set(winners.values())
    kept = [c for i, c in enumerate(candidates) if i in kept_indices]
    hidden = [c for i, c in enumerate(candidates) if i not in kept_indices]
    return kept, hidden


def _render(
    candidate: _Candidate, *, label: str, model_id: str, display_name: str,
) -> CatalogEntry:
    """One published row. Upstream's own fields are relayed unchanged."""
    return CatalogEntry(
        id=model_id,
        display_name=display_name,
        max_input_tokens=candidate.entry.max_input_tokens,
        max_tokens=candidate.entry.max_tokens,
        created_at=candidate.entry.created_at,
        capabilities=candidate.entry.capabilities,
        description=describe_row(
            label=label,
            window=candidate.window.window,
            max_output=candidate.window.max_output,
            # The client's assumption is keyed on the ID it is handed, so the
            # question is about THIS row's spelling — not about whether the
            # model has a 1M window somewhere in the list.
            one_m_row=model_id.endswith(ONE_M_SUFFIX),
        ),
        window_source=candidate.window.source,
    )


def _publish_family(
    *,
    entries: Sequence[CatalogEntry],
    table: ContextTable,
    label: str,
    vendor: Optional[Vendor],
    catalog_filter: str,
) -> tuple[list[CatalogEntry], list[str]]:
    """Render ONE upstream's rows: resolve, decide ``[1m]``, filter, describe.

    One function for both routes because the two differ in exactly two
    places — the published spelling of an id and how the 1M variant is
    offered (a companion ROW for first-party, a suffix ON the row for a
    vendor) — and a second copy would have to be kept in step with this one
    every time the window rules move.
    """
    if not entries:
        return [], []

    parts = {entry.id: parse_model_id(entry.id) for entry in entries}
    resolved = resolve_family_windows(entries, table=table, parts=parts)

    candidates: list[_Candidate] = []
    for entry in entries:
        answer = resolved[entry.id]
        one_m = (answer.window or 0) >= ONE_M_WINDOW
        _warn_on_table_disagreement(
            parts[entry.id].bare_id, table=table, window=answer.window,
        )
        if vendor is None:
            published_id = entry.id
            display_name = entry.display_name
        else:
            published_id = advertised_id(vendor, entry.id, one_m)
            display_name = (
                f"{entry.display_name}{vendor.display_suffix}"
                f"{VENDOR_ONE_M_DISPLAY_MARKER if one_m else ''}"
            )
        candidates.append(
            _Candidate(
                entry=entry,
                parts=parts[entry.id],
                window=answer,
                one_m=one_m,
                published_id=published_id,
                display_name=display_name,
            )
        )

    kept, hidden = _latest_only(candidates, catalog_filter=catalog_filter)

    published: list[CatalogEntry] = []
    for candidate in kept:
        published.append(
            _render(
                candidate,
                label=label,
                model_id=candidate.published_id,
                display_name=candidate.display_name,
            )
        )
        if vendor is None and candidate.one_m:
            # The first-party companion. A hidden base never reaches here, so
            # a companion can never outlive the row it belongs to.
            published.append(
                _render(
                    candidate,
                    label=label,
                    model_id=with_1m(candidate.entry.id),
                    display_name=(
                        f"{candidate.entry.display_name}{ONE_M_DISPLAY_SUFFIX}"
                    ),
                )
            )
    return published, [candidate.published_id for candidate in hidden]


@dataclass
class _FamilyCache:
    entries: tuple[CatalogEntry, ...] = ()
    source: str = SOURCE_UNFETCHED
    fetched_at: float = 0.0


#: An async fetcher: (url, headers) -> parsed JSON, or None on any failure.
JsonFetcher = Callable[[str, Mapping[str, str]], Awaitable[Optional[dict]]]


def _load_static() -> dict[str, tuple[CatalogEntry, ...]]:
    """Read the shipped snapshot. A damaged snapshot is loud, not silent."""
    try:
        payload = json.loads(STATIC_CATALOG_PATH.read_text(encoding="utf-8"))
        families = payload["families"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.error(
            "model-gateway: shipped static catalog %s is unreadable (%s). "
            "A family whose live fetch fails, and which does not declare its "
            "own static_ids, will report no models at all. "
            "This is a damaged install: re-run `python install.py`.",
            STATIC_CATALOG_PATH, exc,
        )
        return {}
    out: dict[str, tuple[CatalogEntry, ...]] = {}
    for family_id, block in families.items():
        if not isinstance(block, dict):
            continue
        rows = block.get("models") or []
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or "").strip()
            if not model_id:
                continue
            entries.append(
                CatalogEntry(
                    id=model_id,
                    display_name=str(row.get("display_name") or model_id),
                )
            )
        out[family_id] = tuple(entries)
    return out


class CatalogService:
    """Per-family catalog cache with a shipped fallback.

    Args:
        vendors: the vendor registry.
        anthropic: the first-party family descriptor.
        fetch_json: async JSON fetcher. Injected so tests drive the whole
            surface without a network and without patching a client library.
        oauth_token: callable returning the current Claude bearer, or ``None``.
        vendor_key: async callable returning a vendor's key, or ``None``.
        live_ttl_s / static_ttl_s: see the module docstring.
    """

    def __init__(
        self,
        *,
        vendors: Mapping[str, Vendor],
        anthropic: AnthropicFamily,
        fetch_json: JsonFetcher,
        oauth_token: Callable[[], Optional[str]],
        vendor_key: Callable[[Vendor], Awaitable[Optional[str]]],
        live_ttl_s: int,
        static_ttl_s: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._vendors = vendors
        self._anthropic = anthropic
        self._fetch_json = fetch_json
        self._oauth_token = oauth_token
        self._vendor_key = vendor_key
        self._live_ttl_s = max(1, int(live_ttl_s))
        self._static_ttl_s = max(1, int(static_ttl_s))
        self._clock = clock
        self._static = self._resolve_static_tables(_load_static(), vendors)
        self._cache: dict[str, _FamilyCache] = {}
        #: Ids the last :meth:`union` withheld. Kept so ``/health`` can report
        #: the count without building a catalog — the same rule ``sources``
        #: follows, and for the same reason: a liveness probe must not fetch.
        self._hidden: tuple[str, ...] = ()

    @staticmethod
    def _resolve_static_tables(
        shipped: dict[str, tuple[CatalogEntry, ...]],
        vendors: Mapping[str, Vendor],
    ) -> dict[str, tuple[CatalogEntry, ...]]:
        """Fold each vendor's own ``static_ids`` into the fallback tables.

        **Precedence: a non-empty ``Vendor.static_ids`` wins outright over the
        shipped ``static_catalog.json`` block for that vendor.** The reason is
        that the alternative makes the field unreliable rather than merely
        lower-priority: if the shipped file won, then whether a declared
        fallback did anything would depend on whether a block for that vendor
        happened to exist — the same "set it and nothing happens" defect the
        field is supposed to avoid, moved one layer down. Precedence by
        specificity is also the honest reading of intent: ``static_ids`` is
        hand-written next to the vendor definition by whoever added the
        vendor, whereas the JSON is a release-managed snapshot. Empty is the
        shipped default and defers to the JSON, so this changes nothing for
        the vendors that ship today.

        The cost of the choice, stated plainly: a row declaring ``static_ids``
        stops picking up snapshot refreshes for that vendor. That is why the
        shadowing case logs — a silent override is the version of this that
        would waste somebody's afternoon.

        Ids double as display names here. The vendor's ``display_suffix`` is
        appended later in :meth:`union`, exactly as for a shipped or live
        entry, so a declared fallback is presented identically to any other.
        """
        resolved = dict(shipped)
        for vendor in vendors.values():
            if not vendor.static_ids:
                continue
            if vendor.vendor_id in resolved and resolved[vendor.vendor_id]:
                logger.info(
                    "model-gateway: vendor %r declares %d static_ids; these "
                    "take precedence over the %d shipped snapshot entries for "
                    "that vendor. Clear static_ids on the vendor row to go "
                    "back to the shipped snapshot.",
                    vendor.vendor_id,
                    len(vendor.static_ids),
                    len(resolved[vendor.vendor_id]),
                )
            resolved[vendor.vendor_id] = tuple(
                CatalogEntry(id=model_id, display_name=model_id)
                for model_id in vendor.static_ids
            )
        return resolved

    def sources(self) -> dict[str, str]:
        """Per-family source WITHOUT fetching anything. Safe from ``/health``."""
        known = [self._anthropic.family_id, *self._vendors.keys()]
        return {
            family_id: self._cache.get(family_id, _FamilyCache()).source
            for family_id in known
        }

    def hidden_count(self) -> int:
        """How many rows the last :meth:`union` withheld. Never fetches.

        Zero before the picker has ever opened, which is indistinguishable
        from "nothing was hidden" and deliberately so: the field answers "is
        my picker short because of the filter?", and before any catalog exists
        the answer is no.
        """
        return len(self._hidden)

    def _fresh(self, family_id: str) -> Optional[_FamilyCache]:
        entry = self._cache.get(family_id)
        if entry is None or not entry.entries:
            return None
        ttl = self._live_ttl_s if entry.source == SOURCE_LIVE else self._static_ttl_s
        if self._clock() - entry.fetched_at < ttl:
            return entry
        return None

    def _fallback(self, family_id: str) -> _FamilyCache:
        entries = self._static.get(family_id, ())
        return _FamilyCache(
            entries=entries,
            source=SOURCE_STATIC if entries else SOURCE_EMPTY,
            fetched_at=self._clock(),
        )

    @staticmethod
    def _upstream_int(value: object) -> Optional[int]:
        """A positive integer, or ``None`` for everything else.

        ``bool`` is excluded explicitly because it is an ``int`` in Python and
        ``max_input_tokens: true`` would otherwise relay as a one-token
        window — a wrong number published with the same confidence as a right
        one, which is the single outcome this whole module refuses.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value > 0 else None

    @classmethod
    def _parse_models(cls, payload: Optional[dict]) -> tuple[CatalogEntry, ...]:
        """Upstream's rows, with the fields it states and no others.

        Every optional field is captured only in the shape it is useful in —
        a positive int, a non-empty string, a mapping — and is ``None``
        otherwise, so "upstream said nothing" and "upstream said something
        unusable" reach the response as the same honest absence. The shipped
        snapshot states none of them, which is expected: it is a list of ids.
        """
        if not isinstance(payload, dict):
            return ()
        rows = payload.get("data")
        if not isinstance(rows, list):
            return ()
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or "").strip()
            if not model_id:
                continue
            created_at = row.get("created_at")
            capabilities = row.get("capabilities")
            out.append(
                CatalogEntry(
                    id=model_id,
                    display_name=str(row.get("display_name") or model_id),
                    max_input_tokens=cls._upstream_int(row.get("max_input_tokens")),
                    max_tokens=cls._upstream_int(row.get("max_tokens")),
                    created_at=(
                        created_at.strip()
                        if isinstance(created_at, str) and created_at.strip()
                        else None
                    ),
                    capabilities=(
                        capabilities if isinstance(capabilities, Mapping) else None
                    ),
                )
            )
        return tuple(out)

    async def _anthropic_entries(self) -> _FamilyCache:
        cached = self._fresh(self._anthropic.family_id)
        if cached is not None:
            return cached
        token = self._oauth_token()
        payload = None
        if token:
            url = f"{self._anthropic.upstream}{self._anthropic.catalog_path}"
            if self._anthropic.catalog_query:
                url = f"{url}?{self._anthropic.catalog_query}"
            payload = await self._fetch_json(
                url,
                {
                    "Authorization": f"Bearer {token}",
                    "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
                    "anthropic-beta": OAUTH_BETA,
                },
            )
        entries = self._parse_models(payload)
        result = (
            _FamilyCache(entries, SOURCE_LIVE, self._clock())
            if entries
            else self._fallback(self._anthropic.family_id)
        )
        self._cache[self._anthropic.family_id] = result
        return result

    async def _vendor_entries(self, vendor: Vendor) -> _FamilyCache:
        cached = self._fresh(vendor.vendor_id)
        if cached is not None:
            return cached
        key = await self._vendor_key(vendor)
        payload = None
        if key:
            payload = await self._fetch_json(
                f"{vendor.upstream}{vendor.catalog_path}",
                {
                    vendor.auth_header: f"{vendor.auth_scheme}{key}",
                    "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
                },
            )
        entries = self._parse_models(payload)
        result = (
            _FamilyCache(entries, SOURCE_LIVE, self._clock())
            if entries
            else self._fallback(vendor.vendor_id)
        )
        self._cache[vendor.vendor_id] = result
        return result

    @property
    def first_party_label(self) -> str:
        """The vendor label a first-party row carries in its description.

        Derived from the family descriptor rather than written out, so the
        catalog never holds a second name for a route that already has one.
        ``vendor_display_name`` does the same job on the other side.
        """
        return self._anthropic.family_id.capitalize()

    async def union(
        self,
        *,
        table: ContextTable,
        catalog_filter: str = DEFAULT_CATALOG_FILTER,
    ) -> CatalogUnion:
        """Build the picker list.

        Vendor ids are published under the vendor's namespace; first-party
        ids verbatim AND, for a 1M model, a second time with the ``[1m]``
        suffix. Both, not one: the plain id is the model, the suffixed one is
        the client's request for the large window on that same model, and a
        user who wants the 200K behaviour must still be able to ask for it.
        The claim that "the client knows first-party windows natively" —
        which is why this used to publish the plain id alone — is false in
        the one place it mattered: with a custom base URL the client budgets
        200K for a 1M model unless the id carries the suffix, so a long
        session compacted at a fifth of the context the user was paying for.

        **Which rows get ``[1m]`` follows the resolved WINDOW**, not the
        table's flag. That is what makes the advert auto-update: a 1M model
        that Anthropic starts returning tomorrow, with its window in its own
        ``/v1/models`` row, gets its companion with nobody editing a file.
        The table is still the authority where it has a row (and the only
        authority for the vendor whose windows must never be guessed); it is
        no longer the ONLY input.

        ``catalog_filter`` is the owner's latest-only requirement:
        ``latest`` (the default) publishes the newest version of each family
        and reports the rest in :attr:`CatalogUnion.hidden`; ``all``
        publishes everything, which is the escape hatch for anyone who needs
        a previous version and the reason the filter is a knob rather than a
        rule.
        """
        sources: dict[str, str] = {}
        entries: list[CatalogEntry] = []
        hidden: list[str] = []

        # Every family is fetched CONCURRENTLY. Sequentially, each stalling
        # vendor added its own timeout to the picker's wait — two vendors down
        # meant 20 s before the user saw a model list that was going to be the
        # static fallback anyway. Concurrently the wait is the slowest single
        # fetch, and each family still falls back on its own.
        #
        # ``gather`` without ``return_exceptions``: both helpers already
        # answer with a fallback family instead of raising (that is what their
        # ``source`` field records), so an exception here would be a defect
        # worth surfacing rather than a vendor being down.
        vendors = list(self._vendors.values())
        first, *vendor_families = await asyncio.gather(
            self._anthropic_entries(),
            *(self._vendor_entries(vendor) for vendor in vendors),
        )

        sources[self._anthropic.family_id] = first.source
        published, withheld = _publish_family(
            entries=first.entries,
            table=table,
            label=self.first_party_label,
            vendor=None,
            catalog_filter=catalog_filter,
        )
        # First-party rows are sorted by id so a plain row and its companion
        # sit together; vendor rows keep the order their upstream listed.
        entries.extend(sorted(published, key=lambda e: e.id))
        hidden.extend(withheld)

        for vendor, family in zip(vendors, vendor_families):
            sources[vendor.vendor_id] = family.source
            published, withheld = _publish_family(
                entries=family.entries,
                table=table,
                label=vendor_display_name(vendor),
                vendor=vendor,
                catalog_filter=catalog_filter,
            )
            entries.extend(published)
            hidden.extend(withheld)

        self._hidden = tuple(hidden)
        return CatalogUnion(entries=entries, sources=sources, hidden=hidden)


def _model_row(entry: CatalogEntry) -> dict:
    """One ``data`` row.

    ``id``, ``display_name`` and ``description`` are what Claude Code reads.
    The window fields are RELAYED — present only when upstream stated them,
    omitted rather than published as ``null`` so a consumer that checks for
    the key can tell "upstream said nothing" from "upstream said none". None
    of them sizes Claude Code's context bar; the ``[1m]`` spelling of the id
    does (see :class:`CatalogEntry`). They are here for the proxy invariant
    and for the consumers that are not Claude Code.
    """
    row: dict = {
        "type": "model",
        "id": entry.id,
        "display_name": entry.display_name,
        "description": entry.description,
    }
    for key, value in (
        ("max_input_tokens", entry.max_input_tokens),
        ("max_tokens", entry.max_tokens),
        ("created_at", entry.created_at),
        ("capabilities", entry.capabilities),
    ):
        if value is not None:
            row[key] = value
    row["_vct_window_source"] = entry.window_source
    return row


def to_models_response(
    entries: Sequence[CatalogEntry],
    sources: Mapping[str, str],
    hidden: Sequence[str] = (),
) -> dict:
    """Anthropic-shaped ``/v1/models`` body plus the non-standard extensions.

    Every ``_vct_``-prefixed key is underscore-prefixed to mark it as an
    extension: a client that does not know it ignores it, while the
    launcher's status card and a curious user get a straight answer.

    * ``_vct_catalog_source`` — per family: live, snapshot, or neither.
    * ``_vct_catalog_hidden`` — the ids the latest-only filter withheld, so
      "my model is missing" resolves without reading a log or guessing at a
      knob. Sorted, because the order rows were filtered in is not
      information anyone can use.
    * ``_vct_window_source`` (per row) — which step decided that row's window.
    """
    data = [_model_row(entry) for entry in entries]
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "_vct_catalog_source": dict(sources),
        "_vct_catalog_hidden": sorted(hidden),
    }


__all__ = [
    # Re-exported from ``config``: the vocabulary a caller of ``union`` needs
    # is the vocabulary of the knob that drives it, and making a consumer
    # import half its arguments from another module is how the two spellings
    # of one value get invented.
    "CATALOG_FILTER_ALL",
    "CATALOG_FILTER_LATEST",
    "DEFAULT_CATALOG_FILTER",
    "CLIENT_DEFAULT_WINDOW",
    "ONE_M_DISPLAY_SUFFIX",
    "ONE_M_WINDOW",
    "SOURCE_EMPTY",
    "SOURCE_LIVE",
    "SOURCE_STATIC",
    "SOURCE_UNFETCHED",
    "STATIC_CATALOG_PATH",
    "UNVERIFIED_WINDOW",
    "VENDOR_ONE_M_DISPLAY_MARKER",
    "WINDOW_DELETED",
    "WINDOW_INHERITED_PREFIX",
    "WINDOW_TABLE",
    "WINDOW_UNKNOWN",
    "WINDOW_UPSTREAM",
    "CatalogEntry",
    "CatalogService",
    "CatalogUnion",
    "FamilyFloor",
    "JsonFetcher",
    "WindowResolution",
    "describe_row",
    "resolve_family_windows",
    "resolve_window",
    "short_tokens",
    "to_models_response",
]
