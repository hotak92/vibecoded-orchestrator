# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Pure routing decisions — which upstream serves a model id, under what name.

No I/O, no network, no clock, no vendor literals. Everything vendor-specific
arrives as a :class:`~model_router.vendors.Vendor` row, which is what makes
"adding a vendor is a config row, not a code change" true rather than claimed:
``tests/test_model_router_routing.py`` reads this module's own source and fails
if a vendor-specific string appears in it.

Two rules in here are load-bearing and each is pinned by its own test.

**The honest-naming guard.** A model id that contains ``claude`` or
``anthropic`` is NEVER forwarded to a vendor upstream, even when the caller
spelled a vendor namespace in front of it. The reason is concrete and
documented rather than hypothetical: at least one shipped vendor's endpoint
answers any ``claude-*`` name with its own small model and returns HTTP 200,
by design and per its own documentation. Forwarding such an id would produce a
successful-looking answer from a model the user did not choose — worse than an
error, because nothing surfaces. So it is a local 400 whose message says what
happened.

**The ``[1m]`` suffix is client-side only — on BOTH routes.** Claude Code's
convention for a 1M-context variant is an ``[1m]`` suffix on the model id. It
is a CLIENT convention and nothing more: no upstream has a model whose name
ends in ``[1m]``, and a first-party id carrying it came back ``404
not_found_error`` naming that exact spelling while the same id without it
returned 200 — same endpoint, same credentials, same minute. The window
itself is bought with the ``context-1m-2025-08-07`` BETA HEADER.

So the suffix is stripped on every route, and :attr:`Route.one_m_requested`
carries the fact across because the forwarded name no longer records it.

**What the strip is actually for.** On ordinary traffic the client resolves
the suffix ITSELF: the gateway's request log shows it sending the bare id
plus the ``context-1m`` beta, so the strip never fires and the header is
already there. It fires for the paths where the client does not — a
hand-written ``curl``, ``ANTHROPIC_MODEL`` set to a suffixed id by a tool or
a user, an SDK that passes the picker string through. Those produced the
field 404 this replaces. It is a defensive normalisation of an id the client
usually normalises first, not the mechanism by which 1M works day to day; the
catalog half (advertising ``<id>[1m]``) is what the client reads to know the
window exists at all.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Collection, Mapping, Optional

from .vendors import (
    ANTHROPIC_FAMILY,
    VENDORS,
    AnthropicFamily,
    CLAUDE_ID_MARKERS,
    Vendor,
)

#: Claude Code's 1M-context-variant suffix.
ONE_M_SUFFIX = "[1m]"


@dataclass(frozen=True)
class Route:
    """Where a request goes and under what name.

    Attributes:
        upstream: base URL to forward to.
        forward_model: the model id that MUST appear in the forwarded body.
            Not a hint — :mod:`model_router.server` rewrites the JSON body so
            the bytes on the wire carry this value. Forwarding the original
            body with only the router's copy stripped is the failure that
            produces a vendor error about a model code that does not exist.
        vendor: the vendor row, or ``None`` for the first-party Claude route.
        family_id: ``vendor_id`` or the Anthropic family id; used as the
            per-family key in logs, catalog caches and ``/health``.
        is_anthropic: True for the first-party OAuth route.
        one_m_requested: the requested id carried ``[1m]``.
    """

    upstream: str
    forward_model: str
    vendor: Optional[Vendor]
    family_id: str
    is_anthropic: bool
    #: The REQUESTED id carried Claude Code's ``[1m]`` suffix. Stripped from
    #: ``forward_model`` on every route (no upstream knows the spelling), so
    #: this flag is the only surviving record that the user asked for the
    #: 1M-context variant. The server turns it into the beta header.
    one_m_requested: bool = False


@dataclass(frozen=True)
class RouteError:
    """A routing refusal. ``status`` is the LOCAL status to return."""

    status: int
    message: str
    #: Short machine-readable reason, so tests and the GUI can branch without
    #: string-matching prose: one of ``empty_model``, ``unknown_model``,
    #: ``empty_namespaced_id``, ``claude_id_to_vendor``.
    reason: str
    #: The closest valid ids, fully spelled (namespace included), for an
    #: ``unknown_model`` refusal; empty otherwise. Also in ``message``.
    suggestions: tuple[str, ...] = ()


def strip_1m(model_id: str) -> str:
    """Drop a trailing :data:`ONE_M_SUFFIX` if present. Idempotent."""
    if model_id.endswith(ONE_M_SUFFIX):
        return model_id[: -len(ONE_M_SUFFIX)]
    return model_id


def with_1m(model_id: str) -> str:
    """Add :data:`ONE_M_SUFFIX` unless it is already there. Idempotent.

    The inverse of :func:`strip_1m`, and the ONLY place the suffix is spelled
    when building an id (``advertised_id`` and the first-party catalog both
    route through here), so the two directions cannot drift apart.
    """
    return model_id if model_id.endswith(ONE_M_SUFFIX) else f"{model_id}{ONE_M_SUFFIX}"


def has_claude_marker(model_id: str) -> bool:
    """True when Claude Code's discovery filter would keep this id."""
    lowered = model_id.lower()
    return any(marker in lowered for marker in CLAUDE_ID_MARKERS)


def split_namespace(
    model_id: str,
    vendors: Mapping[str, Vendor] = VENDORS,
) -> tuple[Optional[Vendor], str]:
    """Split ``model_id`` into (vendor, remainder).

    Returns ``(None, model_id)`` when no namespace matches. When two rows
    could match, the LONGEST namespace wins, so a row whose namespace extends
    another's (a regional variant, say) cannot be shadowed by the shorter one.
    """
    best: tuple[Optional[Vendor], str] = (None, model_id)
    best_len = -1
    for vendor in vendors.values():
        if model_id.startswith(vendor.namespace) and len(vendor.namespace) > best_len:
            best = (vendor, model_id[len(vendor.namespace):])
            best_len = len(vendor.namespace)
    return best


def _bare_prefix_match(
    model_id: str,
    vendors: Mapping[str, Vendor],
) -> Optional[Vendor]:
    """Longest bare-id-prefix match, case-insensitively."""
    lowered = model_id.lower()
    winner: Optional[Vendor] = None
    winner_len = -1
    for vendor in vendors.values():
        for prefix in vendor.bare_id_prefixes:
            if lowered.startswith(prefix.lower()) and len(prefix) > winner_len:
                winner = vendor
                winner_len = len(prefix)
    return winner


#: How many suggestions an ``unknown_model`` refusal names at most.
MAX_SUGGESTIONS = 3

#: ``difflib`` similarity floor for a suggestion. Low enough that a missing
#: namespace segment or a dropped version letter still finds its id
#: (``model-4.1-flash`` -> ``model-v4.1-flash``), high enough that an
#: unrelated id is not offered as a correction.
SUGGESTION_CUTOFF = 0.6


def _known_ids(
    vendor: Vendor, extra: Optional[Mapping[str, Collection[str]]],
) -> frozenset[str]:
    """Every bare id this row is KNOWN to serve, lower-cased.

    The row's declared lists (``static_ids``, ``verified_ids``, hidden ids —
    hidden is "routable by name") plus ``extra[vendor_id]``: the ids the
    server's catalog last read from the vendor's own model list. Data only;
    no vendor is named here.
    """
    ids = {*vendor.static_ids, *vendor.verified_ids, *vendor.catalog_hide_ids}
    if extra is not None:
        ids.update(extra.get(vendor.vendor_id, ()))
    return frozenset(i.strip().lower() for i in ids if i and i.strip())


def _owns_family(vendor: Vendor, remainder: str) -> bool:
    lowered = remainder.lower()
    return any(lowered.startswith(p.lower()) for p in vendor.bare_id_prefixes)


def _suggest(
    remainder: str,
    vendors: Mapping[str, Vendor],
    extra: Optional[Mapping[str, Collection[str]]],
    one_m: bool,
) -> tuple[str, ...]:
    """The closest known ids across every row, spelled with their namespace."""
    spelled: dict[str, str] = {}
    for vendor in vendors.values():
        for model_id in _known_ids(vendor, extra):
            full = f"{vendor.namespace}{model_id}"
            spelled.setdefault(model_id, with_1m(full) if one_m else full)
    lowered = remainder.lower()
    # Exact bare match under another row first (a missing/extra namespace
    # segment is the commonest slip), then the fuzzy neighbours.
    ranked: list[str] = [spelled[lowered]] if lowered in spelled else []
    for bare in difflib.get_close_matches(
        lowered, list(spelled), n=MAX_SUGGESTIONS, cutoff=SUGGESTION_CUTOFF,
    ):
        if spelled[bare] not in ranked:
            ranked.append(spelled[bare])
    return tuple(ranked[:MAX_SUGGESTIONS])


def _unknown_for_vendor(
    vendor: Vendor,
    raw_id: str,
    remainder: str,
    vendors: Mapping[str, Vendor],
    extra: Optional[Mapping[str, Collection[str]]],
    one_m: bool,
) -> RouteError:
    suggestions = _suggest(remainder, vendors, extra, one_m)
    hint = (
        " Did you mean " + " or ".join(repr(s) for s in suggestions) + "?"
        if suggestions else ""
    )
    return RouteError(
        400,
        f"unknown model id {raw_id!r}: {remainder!r} is not a model the "
        f"{vendor.vendor_id!r} upstream ({vendor.namespace}<id>) is known to "
        "serve, so the gateway refuses it here instead of forwarding it to "
        "that vendor (which would fail with its own error, or answer with a "
        f"different model).{hint}",
        "unknown_model",
        suggestions,
    )


def _vendor_route(
    vendor: Vendor,
    raw_remainder: str,
    *,
    raw_id: str = "",
    vendors: Optional[Mapping[str, Vendor]] = None,
    known_ids: Optional[Mapping[str, Collection[str]]] = None,
) -> Route | RouteError:
    """Build a vendor route, applying all three invariants.

    The third (v0.2.100, F-W1-11a): a remainder this row is not known to
    serve — not in its declared or last-read model lists, and not in its own
    bare-id family — is REFUSED locally with the closest valid ids. The shared
    namespace used to accept anything, so ``<shared namespace><other vendor's
    id>`` (a nested namespace segment missing) went to the default vendor, failed there,
    and surfaced as a chat failure with the vendor's unrelated message. A row
    that declares neither a family nor a model list cannot be judged and is
    routed as before.
    """
    remainder = strip_1m(raw_remainder).strip()
    if not remainder:
        return RouteError(
            400,
            f"model id {vendor.namespace!r} names a namespace but no model. "
            f"Use {vendor.namespace}<model-id>.",
            "empty_namespaced_id",
        )
    if has_claude_marker(remainder):
        return RouteError(
            400,
            f"refusing to forward {remainder!r} to the {vendor.vendor_id!r} "
            "upstream: the id names a first-party Claude model, and a "
            "third-party endpoint that accepts such a name answers with its "
            "OWN model while reporting success. Use the plain id (without "
            f"{vendor.namespace!r}) to reach Anthropic, or name one of "
            f"{vendor.vendor_id!r}'s real models.",
            "claude_id_to_vendor",
        )
    known = _known_ids(vendor, known_ids)
    judgeable = bool(known or vendor.bare_id_prefixes)
    if (
        judgeable
        and remainder.lower() not in known
        and not _owns_family(vendor, remainder)
    ):
        return _unknown_for_vendor(
            vendor, raw_id or raw_remainder, remainder,
            vendors if vendors is not None else {vendor.vendor_id: vendor},
            known_ids, raw_remainder.endswith(ONE_M_SUFFIX),
        )
    return Route(
        upstream=vendor.upstream,
        forward_model=remainder,
        vendor=vendor,
        family_id=vendor.vendor_id,
        is_anthropic=False,
        one_m_requested=raw_remainder.endswith(ONE_M_SUFFIX),
    )


def route(
    model_id: str,
    vendors: Mapping[str, Vendor] = VENDORS,
    anthropic: AnthropicFamily = ANTHROPIC_FAMILY,
    known_ids: Optional[Mapping[str, Collection[str]]] = None,
) -> Route | RouteError:
    """Decide where ``model_id`` goes.

    Resolution order, first match wins:

    1. an explicit vendor namespace (``<namespace><id>``) — refused with the
       closest valid ids when the row is not known to serve the remainder
       (see :func:`_vendor_route`);
    2. a vendor's bare-id prefix (so the CLI can use the vendor's real id);
    3. a Claude marker anywhere in the id -> the first-party OAuth route,
       with ``[1m]`` stripped (no upstream model is named that; the 1M window
       travels as a beta header) and recorded in ``one_m_requested``;
    4. otherwise a local 400 that lists the namespaces that do exist and the
       closest valid ids.

    ``known_ids`` maps ``vendor_id`` to the bare ids the caller's catalog
    last read from that vendor's own model list (the server passes its
    cache; no fetch happens on the request path). Omitted, only the rows'
    declared lists count.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        return RouteError(
            400,
            "request has no 'model' field. Set ANTHROPIC_MODEL, or pick a "
            "model in the /model picker.",
            "empty_model",
        )
    model_id = model_id.strip()

    vendor, remainder = split_namespace(model_id, vendors)
    if vendor is not None:
        return _vendor_route(
            vendor, remainder, raw_id=model_id, vendors=vendors,
            known_ids=known_ids,
        )

    bare = _bare_prefix_match(model_id, vendors)
    if bare is not None:
        return _vendor_route(
            bare, model_id, raw_id=model_id, vendors=vendors,
            known_ids=known_ids,
        )

    if has_claude_marker(model_id):
        return Route(
            upstream=anthropic.upstream,
            forward_model=strip_1m(model_id),
            vendor=None,
            family_id=anthropic.family_id,
            is_anthropic=True,
            one_m_requested=model_id.endswith(ONE_M_SUFFIX),
        )

    known = ", ".join(
        sorted(f"{v.namespace}<id>" for v in vendors.values())
    ) or "(no vendor rows configured)"
    suggestions = _suggest(
        strip_1m(model_id), vendors, known_ids, model_id.endswith(ONE_M_SUFFIX),
    )
    hint = (
        " Did you mean " + " or ".join(repr(s) for s in suggestions) + "?"
        if suggestions else ""
    )
    return RouteError(
        400,
        f"no backend serves model {model_id!r}. Ids containing "
        f"{' or '.join(CLAUDE_ID_MARKERS)} go to the first-party Claude route "
        f"under your Claude login; vendor models are named {known}.{hint}",
        "unknown_model",
        suggestions,
    )


def validate_model_id(
    model_id: str,
    vendors: Mapping[str, Vendor] = VENDORS,
    anthropic: AnthropicFamily = ANTHROPIC_FAMILY,
    known_ids: Optional[Mapping[str, Collection[str]]] = None,
) -> tuple[bool, str, tuple[str, ...]]:
    """``(valid, reason, suggestions)`` for an id a CONFIG names (not a request).

    Stricter than :func:`route`, on purpose: a definition file is checked
    before anything is sent, so "the row's family would accept it" is not
    enough — the id must be one the registry KNOWS the row serves (declared
    lists, plus ``known_ids`` when the caller has a catalog). A first-party
    Claude id is valid by construction (the client validates those).
    Used by the shipped-definition contract test and by
    ``python -m vco_lib.module_gated_delivery check-agent-ids``.
    """
    decision = route(model_id, vendors, anthropic, known_ids)
    if isinstance(decision, RouteError):
        return False, decision.message, decision.suggestions
    if decision.is_anthropic or decision.vendor is None:
        return True, "first-party route", ()
    if decision.forward_model.lower() in _known_ids(decision.vendor, known_ids):
        return True, f"known to the {decision.vendor.vendor_id!r} row", ()
    suggestions = _suggest(
        decision.forward_model, vendors, known_ids, decision.one_m_requested,
    )
    return (
        False,
        f"{model_id!r} routes to the {decision.vendor.vendor_id!r} upstream, but "
        f"{decision.forward_model!r} is not in that row's known model list",
        suggestions,
    )


def advertised_id(vendor: Vendor, model_id: str, one_m: bool) -> str:
    """The id to publish in ``/v1/models`` for a vendor model.

    ``one_m`` comes from ``catalog.resolve_window``, keyed by FULL model id —
    never a family wildcard. That is not pedantry: within one shipped vendor,
    one model version has a 1M window while the previous minor version has
    200K, so a ``<family>*`` wildcard would misreport the window by 5x.
    """
    namespaced = f"{vendor.namespace}{model_id}"
    return with_1m(namespaced) if one_m else namespaced


__all__ = [
    "MAX_SUGGESTIONS",
    "ONE_M_SUFFIX",
    "Route",
    "RouteError",
    "advertised_id",
    "has_claude_marker",
    "route",
    "split_namespace",
    "strip_1m",
    "validate_model_id",
    "with_1m",
]
