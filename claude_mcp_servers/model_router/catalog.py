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
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence

from .routing import advertised_id, with_1m
from .vendors import (
    DEFAULT_ANTHROPIC_VERSION,
    OAUTH_BETA,
    AnthropicFamily,
    Vendor,
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

#: Appended to the display name of a ``[1m]`` companion entry so the picker
#: shows two distinguishable rows for one model rather than the same name
#: twice. The vendor path has its own ``· 1M ctx`` marker; this one is
#: spelled out because a first-party row has no vendor suffix beside it.
ONE_M_DISPLAY_SUFFIX = " (1M context)"


@dataclass(frozen=True)
class CatalogEntry:
    """One picker row, already namespaced and already ``[1m]``-decorated."""

    id: str
    display_name: str


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
    def _parse_models(payload: Optional[dict]) -> tuple[CatalogEntry, ...]:
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
            out.append(
                CatalogEntry(
                    id=model_id,
                    display_name=str(row.get("display_name") or model_id),
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

    async def union(
        self,
        *,
        advertise_1m: Callable[[str], bool],
    ) -> tuple[list[CatalogEntry], dict[str, str]]:
        """Build the picker list.

        Vendor ids are published under the vendor's namespace, with the
        ``[1m]`` suffix added only when the chat-model context table says so
        for that EXACT id.

        First-party ids are published verbatim AND, when the table says the
        model has a 1M window, a second time with the ``[1m]`` suffix. Both,
        not one: the plain id is the model, the suffixed one is the client's
        request for the large window on that same model, and a user who wants
        the 200K behaviour must still be able to ask for it. The claim that
        "the client knows first-party windows natively" — which is why this
        used to publish the plain id alone — is false in the one place it
        mattered: with a custom base URL the client budgets 200K for a 1M
        model unless the id carries the suffix, so a long session compacted
        at a fifth of the context the user was paying for.
        """
        sources: dict[str, str] = {}
        entries: list[CatalogEntry] = []

        first = await self._anthropic_entries()
        sources[self._anthropic.family_id] = first.source
        published: list[CatalogEntry] = []
        for entry in first.entries:
            published.append(entry)
            if advertise_1m(entry.id):
                published.append(
                    CatalogEntry(
                        id=with_1m(entry.id),
                        display_name=f"{entry.display_name}{ONE_M_DISPLAY_SUFFIX}",
                    )
                )
        entries.extend(sorted(published, key=lambda e: e.id))

        for vendor in self._vendors.values():
            family = await self._vendor_entries(vendor)
            sources[vendor.vendor_id] = family.source
            for entry in family.entries:
                one_m = advertise_1m(entry.id)
                entries.append(
                    CatalogEntry(
                        id=advertised_id(vendor, entry.id, one_m),
                        display_name=(
                            f"{entry.display_name}{vendor.display_suffix}"
                            f"{' · 1M ctx' if one_m else ''}"
                        ),
                    )
                )
        return entries, sources


def to_models_response(
    entries: Sequence[CatalogEntry],
    sources: Mapping[str, str],
) -> dict:
    """Anthropic-shaped ``/v1/models`` body plus the non-standard source map.

    ``_vct_catalog_source`` is underscore-prefixed to mark it as an extension:
    a client that does not know it ignores it, while the launcher's status
    card and a curious user both get a straight answer about whether the
    picker they are looking at came from the vendors or from the snapshot.
    """
    data = [
        {"type": "model", "id": e.id, "display_name": e.display_name}
        for e in entries
    ]
    return {
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "_vct_catalog_source": dict(sources),
    }


__all__ = [
    "ONE_M_DISPLAY_SUFFIX",
    "SOURCE_EMPTY",
    "SOURCE_LIVE",
    "SOURCE_STATIC",
    "SOURCE_UNFETCHED",
    "STATIC_CATALOG_PATH",
    "CatalogEntry",
    "CatalogService",
    "JsonFetcher",
    "to_models_response",
]
