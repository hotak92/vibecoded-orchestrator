# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Catalog assembly, caching, and the shipped static snapshot.

The end-to-end catalog behaviour is exercised over real sockets in
``test_model_router_server.py``; this file pins the unit-level rules that are
awkward to drive through HTTP — clock-driven cache expiry, the snapshot's own
contents, and the reported-source tri-state.

The static snapshot is DATA, and it makes a claim: "these ids existed when the
snapshot was taken". The tests below hold it to the two consistency rules that
matter — its vendor block must match the chat-model context seed (otherwise a
picker built from the snapshot advertises windows the table cannot key), and
every family it names must be a family the gateway actually routes.
"""
from __future__ import annotations

import asyncio
import dataclasses

import json
import unittest
from pathlib import Path

from model_router import catalog as cat
from model_router.context_table import ContextTable, ModelContext, load_seed
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS, AnthropicFamily, Vendor


from tests.common.qwen_catalog import (  # noqa: E402
    QWEN_CHAT_NINE,
    QWEN_EXCLUDED_SIX,
    QWEN_LIVE_MODELS,
    assert_matches_shipped_row,
)


def table_of(
    windows: dict[str, int] | None = None,
    *,
    tombstones: tuple[str, ...] = (),
) -> ContextTable:
    """A context table from ``{model_id: context_window}``.

    Cited, because an uncited row is dropped by the real parser and a fixture
    that skipped the citation would test a code path no export can reach.
    """
    return ContextTable(
        rows={
            model_id: ModelContext(
                model_id=model_id,
                vendor="acme",
                context_window=window,
                max_output=window // 8,
                window_1m=window >= cat.ONE_M_WINDOW,
                source="https://docs.acme.example/cited",
            )
            for model_id, window in (windows or {}).items()
        },
        source="test",
        path=None,
        tombstones=frozenset(tombstones),
    )


#: No row for any id: every window resolves from upstream, the family floor,
#: or not at all. The state a machine with no export and no seed row is in.
NO_TABLE = table_of()

ACME = Vendor(
    vendor_id="acme",
    display_suffix=" · Acme",
    namespace="claude-acme/",
    upstream="https://api.acme.example",
    secret_keys=("acme_key",),
    bare_id_prefixes=("acme-",),
)
ACME_FAMILY = AnthropicFamily(
    family_id="anthropic",
    upstream="https://first.example",
    catalog_path="/v1/models",
    catalog_query="limit=100",
    docs_url="",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _service(
    *,
    responses: dict[str, object],
    clock: _Clock,
    live_ttl_s: int = 3600,
    static_ttl_s: int = 60,
    token: str | None = "tok",
    key: str | None = "key",
    calls: list[str] | None = None,
    vendor: Vendor = ACME,
) -> cat.CatalogService:
    async def fetch(url, headers):
        if calls is not None:
            calls.append(url)
        for needle, payload in responses.items():
            if needle in url:
                return payload
        return None

    async def vendor_key(vendor):
        return key

    return cat.CatalogService(
        vendors={vendor.vendor_id: vendor},
        anthropic=ACME_FAMILY,
        fetch_json=fetch,
        oauth_token=lambda: token,
        vendor_key=vendor_key,
        live_ttl_s=live_ttl_s,
        static_ttl_s=static_ttl_s,
        clock=clock,
    )


class StaticSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(
            cat.STATIC_CATALOG_PATH.read_text(encoding="utf-8"),
        )

    def test_snapshot_ships_beside_the_module(self) -> None:
        self.assertTrue(cat.STATIC_CATALOG_PATH.is_file())
        self.assertEqual(cat.STATIC_CATALOG_PATH.parent.name, "model_router")

    def test_every_family_in_the_snapshot_is_a_family_we_route(self) -> None:
        """SUBSET, not equality: a vendor whose fallback is its OWN declared
        list — a no-discovery row (``catalog_path is None``), or a row
        overriding discovery with ``catalog_url`` — is routed but
        deliberately absent from the snapshot, so a snapshot block for it
        could only drift from the row. The reverse direction stays exact: a
        family the snapshot names but nothing routes is a fallback that can
        never serve."""
        known = {ANTHROPIC_FAMILY.family_id, *VENDORS}
        self.assertTrue(set(self.payload["families"]) <= known)
        self.assertIn("zai", self.payload["families"])
        # The static-ids vendor is exactly the expected absence.
        self.assertNotIn("qwen", self.payload["families"])

    def test_the_static_ids_vendor_never_touches_the_snapshot(self) -> None:
        """Its fallback catalog is the vendor row; ``_resolve_static_tables``
        must fold the row's ids in as its table for that family, with no
        shipped block to shadow or defer to."""
        resolved = cat.CatalogService._resolve_static_tables(
            cat._load_static(), VENDORS,
        )
        self.assertEqual(
            sorted(entry.id for entry in resolved["qwen"]),
            sorted(VENDORS["qwen"].static_ids),
        )

    def test_the_vendor_block_matches_the_context_seed_exactly(self) -> None:
        """A snapshot id with no seed row would be advertised with the client's
        conservative default; a seed row with no snapshot id would be a
        citation for a model the fallback never offers."""
        # Only the vendor's rows: the seed also carries first-party Claude
        # rows for the settings writer's [1m] decoration, and those are not
        # part of any vendor's fallback catalog.
        seed_ids = {mid for mid, row in load_seed().rows.items() if row.vendor == "zai"}
        snapshot_ids = {
            row["id"] for row in self.payload["families"]["zai"]["models"]
        }
        self.assertEqual(snapshot_ids, seed_ids)

    def test_first_party_entries_have_no_invented_display_names(self) -> None:
        for row in self.payload["families"]["anthropic"]["models"]:
            self.assertEqual(set(row), {"id"})

    def test_every_snapshot_id_survives_client_side_discovery_once_namespaced(self) -> None:
        from model_router.routing import advertised_id, has_claude_marker

        for row in self.payload["families"]["anthropic"]["models"]:
            self.assertTrue(has_claude_marker(row["id"]), row["id"])
        for row in self.payload["families"]["zai"]["models"]:
            self.assertTrue(
                has_claude_marker(advertised_id(VENDORS["zai"], row["id"], False)),
            )

    def test_loader_returns_entries_for_both_families(self) -> None:
        loaded = cat._load_static()
        self.assertTrue(loaded["anthropic"])
        self.assertTrue(loaded["zai"])


class SourceReportingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sources_start_unfetched(self) -> None:
        service = _service(responses={}, clock=_Clock())
        self.assertEqual(
            service.sources(), {"anthropic": cat.SOURCE_UNFETCHED, "acme": cat.SOURCE_UNFETCHED},
        )

    async def test_a_live_answer_reports_live(self) -> None:
        service = _service(
            responses={
                "first.example": {"data": [{"id": "claude-x"}]},
                "acme.example": {"data": [{"id": "acme-1"}]},
            },
            clock=_Clock(),
        )
        catalog = await service.union(table=NO_TABLE)
        self.assertEqual(
            catalog.sources,
            {"anthropic": cat.SOURCE_LIVE, "acme": cat.SOURCE_LIVE},
        )

    async def test_a_failed_fetch_reports_static_not_live(self) -> None:
        """A family served from the snapshot must never claim to be live."""
        service = _service(responses={}, clock=_Clock())
        catalog = await service.union(table=NO_TABLE)
        # 'acme' has no snapshot block, so it is genuinely unavailable; the
        # first-party family does, so it falls back.
        self.assertEqual(catalog.sources["anthropic"], cat.SOURCE_STATIC)
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_EMPTY)

    async def test_no_oauth_token_means_no_first_party_fetch_at_all(self) -> None:
        calls: list[str] = []
        service = _service(
            responses={"first.example": {"data": [{"id": "claude-x"}]}},
            clock=_Clock(), token=None, calls=calls,
        )
        await service.union(table=NO_TABLE)
        self.assertFalse([c for c in calls if "first.example" in c])

    async def test_no_vendor_key_means_no_vendor_fetch_at_all(self) -> None:
        calls: list[str] = []
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=_Clock(), key=None, calls=calls,
        )
        await service.union(table=NO_TABLE)
        self.assertFalse([c for c in calls if "acme.example" in c])


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_live_result_is_cached_for_the_live_ttl(self) -> None:
        calls: list[str] = []
        clock = _Clock()
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=clock, live_ttl_s=600, calls=calls,
        )
        await service.union(table=NO_TABLE)
        vendor_calls = len([c for c in calls if "acme.example" in c])
        clock.now += 599
        await service.union(table=NO_TABLE)
        self.assertEqual(len([c for c in calls if "acme.example" in c]), vendor_calls)
        clock.now += 2
        await service.union(table=NO_TABLE)
        self.assertGreater(len([c for c in calls if "acme.example" in c]), vendor_calls)

    async def test_a_static_fallback_is_retried_on_the_short_ttl(self) -> None:
        """The prototype cached the fallback under the LIVE ttl, so a one-minute
        outage cost six hours of a stale picker."""
        calls: list[str] = []
        clock = _Clock()
        service = _service(
            responses={}, clock=clock, live_ttl_s=21600, static_ttl_s=300, calls=calls,
        )
        await service.union(table=NO_TABLE)
        first = len([c for c in calls if "first.example" in c])
        clock.now += 301
        await service.union(table=NO_TABLE)
        self.assertGreater(len([c for c in calls if "first.example" in c]), first)


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_party_ids_are_published_verbatim(self) -> None:
        service = _service(
            responses={"first.example": {"data": [{"id": "claude-x", "display_name": "X"}]}},
            clock=_Clock(),
        )
        catalog = await service.union(table=NO_TABLE)
        row = next(e for e in catalog.entries if e.id == "claude-x")
        self.assertEqual(row.display_name, "X")

    async def test_vendor_ids_are_namespaced_and_suffixed(self) -> None:
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=_Clock(),
        )
        catalog = await service.union(table=table_of({"acme-1": cat.ONE_M_WINDOW}))
        match = [e for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(match[0].id, "claude-acme/acme-1[1m]")
        self.assertIn("Acme", match[0].display_name)
        self.assertIn("1M ctx", match[0].display_name)

    async def test_the_one_m_decision_is_the_window_not_a_name_pattern(self) -> None:
        """The advert follows the RESOLVED WINDOW, by exact id, never a shape.

        Two ids one character apart, one tabulated at 1M and one not: a rule
        that keyed on a family prefix would give both the suffix and overstate
        the second by 5x. This is the case the exact-key rule exists for, at
        the catalog layer rather than the table's.
        """
        service = _service(
            responses={
                "first.example": {"data": [{"id": "claude-x"}]},
                "acme.example": {"data": [{"id": "acme-1"}, {"id": "acme-1x"}]},
            },
            clock=_Clock(),
        )
        catalog = await service.union(table=table_of({"acme-1": cat.ONE_M_WINDOW}))
        ids = [e.id for e in catalog.entries]
        self.assertIn("claude-acme/acme-1[1m]", ids)
        self.assertIn("claude-acme/acme-1x", ids)
        self.assertNotIn("claude-acme/acme-1x[1m]", ids)
        # ...and the first-party row, which no table names, gets none either.
        self.assertEqual([i for i in ids if i.startswith("claude-x")], ["claude-x"])

    async def test_first_party_entries_are_sorted_and_vendor_order_is_stable(self) -> None:
        service = _service(
            responses={
                "first.example": {"data": [{"id": "claude-z"}, {"id": "claude-a"}]},
                "acme.example": {"data": [{"id": "acme-2"}, {"id": "acme-1"}]},
            },
            clock=_Clock(),
        )
        # ``all``: ``acme-1`` and ``acme-2`` are one family two versions
        # apart, so the default filter would publish one of them and this
        # test would be about the filter instead of about ordering.
        catalog = await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        ids = [e.id for e in catalog.entries]
        self.assertEqual(ids[:2], ["claude-a", "claude-z"])
        self.assertEqual(ids[2:], ["claude-acme/acme-2", "claude-acme/acme-1"])

    async def test_malformed_upstream_payloads_do_not_crash(self) -> None:
        for payload in ({}, {"data": "nope"}, {"data": [None, {"no_id": 1}]}, None):
            with self.subTest(payload=payload):
                service = _service(
                    responses={"acme.example": payload}, clock=_Clock(),
                )
                catalog = await service.union(table=NO_TABLE)
                self.assertIsInstance(catalog.entries, list)
                self.assertIn(
                    catalog.sources["acme"],
                    (cat.SOURCE_STATIC, cat.SOURCE_EMPTY),
                )


class ResponseShapeTests(unittest.TestCase):
    def test_response_is_anthropic_shaped(self) -> None:
        body = cat.to_models_response(
            [cat.CatalogEntry("a", "A"), cat.CatalogEntry("b", "B")],
            {"anthropic": cat.SOURCE_LIVE},
        )
        self.assertEqual([r["id"] for r in body["data"]], ["a", "b"])
        self.assertEqual(body["first_id"], "a")
        self.assertEqual(body["last_id"], "b")
        self.assertFalse(body["has_more"])
        self.assertTrue(all(r["type"] == "model" for r in body["data"]))

    def test_the_source_map_is_marked_as_an_extension(self) -> None:
        """An underscore prefix so a client that does not know it ignores it,
        while the launcher card and a curious user get a straight answer."""
        body = cat.to_models_response([], {"anthropic": cat.SOURCE_STATIC})
        self.assertEqual(body["_vct_catalog_source"], {"anthropic": "static"})

    def test_an_empty_catalog_has_null_bounds_not_a_crash(self) -> None:
        body = cat.to_models_response([], {})
        self.assertIsNone(body["first_id"])
        self.assertIsNone(body["last_id"])

    def test_the_three_fields_the_client_reads_are_never_omitted(self) -> None:
        """``id``, ``display_name`` and ``description`` — and nothing else in
        a row reaches Claude Code at all. The window fields below them are a
        relay for other consumers; see ``test_v0295_gateway_catalog_windows``
        for the rest of the row's contract.
        """
        body = cat.to_models_response([cat.CatalogEntry("a", "A")], {})
        self.assertLessEqual(
            {"type", "id", "display_name", "description"}, set(body["data"][0]),
        )


class StaticLoaderDamageTests(unittest.TestCase):
    def test_an_unreadable_snapshot_is_loud_and_empty_not_silent(self) -> None:
        import logging

        original = cat.STATIC_CATALOG_PATH
        try:
            cat.STATIC_CATALOG_PATH = Path("/nonexistent/static_catalog.json")
            with self.assertLogs(cat.logger, level=logging.ERROR) as captured:
                loaded = cat._load_static()
            self.assertEqual(loaded, {})
            self.assertIn("damaged install", "\n".join(captured.output))
        finally:
            cat.STATIC_CATALOG_PATH = original


class VendorDeclaredFallbackTests(unittest.IsolatedAsyncioTestCase):
    """``Vendor.static_ids`` — a vendor row carrying its own fallback catalog.

    The point of the field is that adding a vendor stays a CONFIG change all
    the way through, fallback included: a new row can ship a usable picker
    without anyone editing ``static_catalog.json``. These tests hold it to the
    standard that makes it a feature rather than a declaration — it has to
    change something a user can see.
    """

    ACME_WITH_IDS = Vendor(
        vendor_id="acme",
        display_suffix=" · Acme",
        namespace="claude-acme/",
        upstream="https://api.acme.example",
        secret_keys=("acme_key",),
        bare_id_prefixes=("acme-",),
        static_ids=("acme-large", "acme-small"),
    )

    async def _fallback_payload(self, vendor: Vendor) -> dict:
        """Drive a full ``/v1/models`` body with every live fetch failing."""
        service = _service(responses={}, clock=_Clock(), vendor=vendor)
        catalog = await service.union(table=NO_TABLE)
        return cat.to_models_response(
            catalog.entries, catalog.sources, catalog.hidden,
        )

    @staticmethod
    def _vendor_rows(body: dict) -> list[dict]:
        """Just this vendor's rows.

        The first-party family falls back to its own shipped block in these
        runs, so ``data`` is never vendor-only — asserting on the whole list
        would pin unrelated snapshot contents into these tests.
        """
        return [
            row for row in body["data"]
            if row["id"].startswith("claude-acme/")
        ]

    async def test_declared_ids_are_served_when_the_live_fetch_fails(self) -> None:
        body = await self._fallback_payload(self.ACME_WITH_IDS)
        self.assertEqual(
            [row["id"] for row in self._vendor_rows(body)],
            ["claude-acme/acme-large", "claude-acme/acme-small"],
        )
        # ``declared``, not ``static``: the source names WHERE the served
        # list came from, and this one is the row's own declaration rather
        # than the shipped snapshot (which the family does not have).
        self.assertEqual(body["_vct_catalog_source"]["acme"], cat.SOURCE_DECLARED)

    async def test_the_declared_fallback_differs_from_no_declaration(self) -> None:
        """The observable-difference test the field exists to pass.

        Same registry shape, same failed fetches, one field changed — the
        picker payload has to differ, or the knob is decoration.
        """
        with_ids = await self._fallback_payload(self.ACME_WITH_IDS)
        without = await self._fallback_payload(ACME)
        self.assertNotEqual(
            self._vendor_rows(with_ids), self._vendor_rows(without),
        )
        self.assertEqual(self._vendor_rows(without), [])
        # The two states are also told apart honestly: a vendor with neither a
        # declaration nor a snapshot block is `unavailable`, not `static`.
        self.assertEqual(without["_vct_catalog_source"]["acme"], cat.SOURCE_EMPTY)
        # The unrelated first-party family is unaffected either way.
        self.assertEqual(
            with_ids["_vct_catalog_source"]["anthropic"],
            without["_vct_catalog_source"]["anthropic"],
        )

    async def test_a_declared_fallback_still_gets_the_display_suffix(self) -> None:
        """Presented like any other entry — the user sees WHICH subscription."""
        rows = self._vendor_rows(await self._fallback_payload(self.ACME_WITH_IDS))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row["display_name"].endswith(" · Acme"), row)

    async def test_a_declared_fallback_is_retried_on_the_short_ttl(self) -> None:
        """It is a fallback, so it must not pin the picker like a live result.

        Regression guard for the prototype's defect at the new call site: a
        vendor-declared list is still `static`, so a brief vendor outage must
        not cost a full live-TTL window.
        """
        clock = _Clock()
        calls: list[str] = []
        service = _service(
            responses={}, clock=clock, calls=calls,
            live_ttl_s=3600, static_ttl_s=60,
            vendor=self.ACME_WITH_IDS,
        )
        await service.union(table=NO_TABLE)
        first = len(calls)
        clock.now += 61
        await service.union(table=NO_TABLE)
        self.assertGreater(len(calls), first)

    async def test_a_live_catalog_still_beats_a_declared_fallback(self) -> None:
        """`static_ids` is a fallback, not an override of reality."""
        service = _service(
            responses={"api.acme.example": {"data": [{"id": "acme-live"}]}},
            clock=_Clock(),
            vendor=self.ACME_WITH_IDS,
        )
        catalog = await service.union(table=NO_TABLE)
        ids = [e.id for e in catalog.entries]
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_LIVE)
        self.assertIn("claude-acme/acme-live", ids)
        self.assertNotIn("claude-acme/acme-large", ids)


class DeclaredCatalogTests(unittest.IsolatedAsyncioTestCase):
    """A vendor with NO model-list endpoint — ``catalog_path is None``.

    The far edge of ``static_ids``-as-catalog: there is nothing to retry and
    no key needed to list, and the source reported is ``declared``. (A
    ``catalog_url`` row whose fetch failed also reports ``declared`` — the
    source names the row's own list as what is being served — but THAT one
    retries; see ``CatalogUrlOverrideTests``.)
    """

    ACME_NO_DISCOVERY = Vendor(
        vendor_id="acme",
        display_suffix=" · Acme",
        namespace="claude-acme/",
        upstream="https://api.acme.example",
        secret_keys=("acme_key",),
        bare_id_prefixes=("acme-",),
        catalog_path=None,
        static_ids=("acme-large", "acme-small"),
    )

    async def _catalog(self, *, vendor=None, calls=None, clock=None, key="key",
                       catalog_filter=cat.DEFAULT_CATALOG_FILTER):
        service = _service(
            responses={"api.acme.example": {"data": [{"id": "acme-live"}]}},
            clock=clock or _Clock(),
            calls=calls,
            key=key,
            vendor=vendor or self.ACME_NO_DISCOVERY,
        )
        return await service.union(
            table=NO_TABLE, catalog_filter=catalog_filter,
        )

    async def test_no_fetch_and_no_key_happens_at_all(self) -> None:
        """The endpoint has no list route (asking it would 404 upstream) and
        the picker must not require the key to exist to show the vendor's
        declared models. (The first-party family fetches normally — only
        THIS vendor's urls are pinned here.)"""
        calls: list[str] = []
        catalog = await self._catalog(calls=calls, key=None)
        self.assertEqual(
            [url for url in calls if "api.acme.example" in url], [],
        )
        self.assertTrue(catalog.entries)

    async def test_the_source_is_declared_not_static(self) -> None:
        catalog = await self._catalog()
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_DECLARED)

    async def test_the_declared_ids_are_served_verbatim(self) -> None:
        catalog = await self._catalog(catalog_filter=cat.CATALOG_FILTER_ALL)
        vendor_rows = [
            e for e in catalog.entries if e.id.startswith("claude-acme/")
        ]
        self.assertEqual(
            sorted(e.id for e in vendor_rows),
            ["claude-acme/acme-large", "claude-acme/acme-small"],
        )
        for entry in vendor_rows:
            self.assertTrue(
                entry.display_name.endswith(" · Acme"), entry.display_name,
            )

    async def test_a_declared_catalog_is_not_retried_after_the_ttl(self) -> None:
        """Nothing to retry: the TTL re-serve of a static fallback exists to
        re-attempt a live fetch, and this vendor has none. A fetch attempt
        here would be a request to a 404 route on every picker refresh."""
        clock = _Clock()
        calls: list[str] = []
        service = _service(
            responses={}, clock=clock, calls=calls,
            vendor=self.ACME_NO_DISCOVERY,
        )
        await service.union(table=NO_TABLE)
        clock.now += 3601
        await service.union(table=NO_TABLE)
        self.assertEqual(
            [url for url in calls if "api.acme.example" in url], [],
        )

    async def test_the_latest_filter_still_applies_to_declared_ids(self) -> None:
        """Declared is not exempt from the owner's latest-only rule."""
        vendor = Vendor(
            vendor_id="acme",
            display_suffix=" · Acme",
            namespace="claude-acme/",
            upstream="https://api.acme.example",
            secret_keys=("acme_key",),
            bare_id_prefixes=("acme-",),
            catalog_path=None,
            static_ids=("acme-1", "acme-2"),
        )
        catalog = await self._catalog(vendor=vendor)
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(ids, ["claude-acme/acme-2"])
        self.assertIn("claude-acme/acme-1", catalog.hidden)


#: The live model list the Token-Plan subscription's compatible-mode endpoint
#: served on the 2026-09-22 probe (ids verbatim, envelope as fetched) — five
#: non-chat modalities among fifteen ids.
class CatalogUrlOverrideTests(unittest.IsolatedAsyncioTestCase):
    """``Vendor.catalog_url`` — a live list on another base than the messages one.

    The row rides the IDENTICAL live path (``live`` source, family floors,
    the auto ``[1m]`` advert, the truth filter), with its ``static_ids`` as
    the ``declared`` fallback for the keyless and fetch-failed moments.
    """

    ACME_OVERRIDE = Vendor(
        vendor_id="acme",
        display_suffix=" · Acme",
        namespace="claude-acme/",
        upstream="https://api.acme.example",
        secret_keys=("acme_key",),
        bare_id_prefixes=("acme-",),
        catalog_url="https://lists.acme.example/compatible/v1/models",
        static_ids=("acme-large", "acme-small"),
    )

    LIVE = {"data": [{"id": "acme-live"}]}

    async def test_the_fetch_uses_the_absolute_url_not_upstream_plus_path(
        self,
    ) -> None:
        calls: list[str] = []
        service = _service(
            responses={"lists.acme.example": self.LIVE},
            clock=_Clock(), calls=calls, vendor=self.ACME_OVERRIDE,
        )
        catalog = await service.union(table=NO_TABLE)
        self.assertEqual(
            [url for url in calls if "acme.example" in url],
            ["https://lists.acme.example/compatible/v1/models"],
        )
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_LIVE)
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(ids, ["claude-acme/acme-live"])

    async def test_keyless_serves_the_declared_ids_without_a_fetch(self) -> None:
        """The picker must not require the key to exist: no key, no fetch,
        the row's declared list under the ``declared`` source."""
        calls: list[str] = []
        service = _service(
            responses={"lists.acme.example": self.LIVE},
            clock=_Clock(), calls=calls, key=None, vendor=self.ACME_OVERRIDE,
        )
        catalog = await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        self.assertEqual([url for url in calls if "acme.example" in url], [])
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_DECLARED)
        ids = sorted(e.id for e in catalog.entries if e.id.startswith("claude-acme/"))
        self.assertEqual(ids, ["claude-acme/acme-large", "claude-acme/acme-small"])

    async def test_a_failed_fetch_falls_back_to_the_declared_source(self) -> None:
        service = _service(
            responses={}, clock=_Clock(), vendor=self.ACME_OVERRIDE,
        )
        catalog = await service.union(table=NO_TABLE)
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_DECLARED)
        ids = sorted(e.id for e in catalog.entries if e.id.startswith("claude-acme/"))
        self.assertEqual(ids, ["claude-acme/acme-large", "claude-acme/acme-small"])

    async def test_the_declared_fallback_is_retried_on_the_short_ttl(self) -> None:
        """It is a fallback, so the live fetch comes back on the short TTL —
        ``declared`` after a FAILED fetch is a retry state, unlike the
        no-discovery branch which has nothing to retry."""
        clock = _Clock()
        calls: list[str] = []
        service = _service(
            responses={}, clock=clock, calls=calls,
            live_ttl_s=3600, static_ttl_s=60, vendor=self.ACME_OVERRIDE,
        )
        await service.union(table=NO_TABLE)
        first = len([c for c in calls if "lists.acme.example" in c])
        self.assertEqual(first, 1)
        clock.now += 61
        await service.union(table=NO_TABLE)
        self.assertGreater(
            len([c for c in calls if "lists.acme.example" in c]), first,
        )

    async def test_the_shipped_row_falls_back_to_its_nine_declared_ids(self) -> None:
        """The shipped Token-Plan row HAS discovery (``catalog_url``), so
        this drives its fallback: the injected fetch answers nothing, the
        row's declared ids answer instead, under the ``declared`` source.
        Four rows survive as the owner's final advertised qwen list
        (older same-family siblings hide under the latest filter; the two
        curated-hidden ids hide under both filters), and ``all`` publishes
        the nine declared minus the curated-hidden two. The shared glm ids
        are published with the ``[1m]`` suffix — the context table keys on
        the bare id and those ids' verified window is 1M, whatever endpoint
        serves them."""
        default_catalog = await _service(
            responses={}, clock=_Clock(), vendor=VENDORS["qwen"],
        ).union(table=load_seed())
        ids = [
            e.id for e in default_catalog.entries
            if e.id.startswith("claude-gw/qwen/")
        ]
        self.assertEqual(len(ids), 4)
        self.assertIn("claude-gw/qwen/glm-5.3[1m]", ids)
        self.assertEqual(default_catalog.sources["qwen"], cat.SOURCE_DECLARED)

        full_catalog = await _service(
            responses={}, clock=_Clock(), vendor=VENDORS["qwen"],
        ).union(table=load_seed(), catalog_filter=cat.CATALOG_FILTER_ALL)
        all_ids = [
            e.id for e in full_catalog.entries
            if e.id.startswith("claude-gw/qwen/")
        ]
        self.assertEqual(
            sorted(all_ids),
            sorted(
                "claude-gw/qwen/" + model_id
                + ("[1m]" if model_id in ("glm-5.2", "glm-5.3") else "")
                for model_id in VENDORS["qwen"].static_ids
                if model_id not in VENDORS["qwen"].catalog_hide_ids
            ),
        )
        self.assertEqual(full_catalog.sources["qwen"], cat.SOURCE_DECLARED)

    def test_the_shipped_static_ids_are_exactly_the_live_chat_set(self) -> None:
        """The declared fallback is curated from the same probe: the fifteen
        live ids minus the six excluded (five non-chat modalities plus the
        dated deepseek snapshot), with no drift in either direction — a
        stale fallback would silently differ from the list the
        endpoint actually serves."""
        assert_matches_shipped_row(self, VENDORS["qwen"])


class CatalogExcludePrefixTests(unittest.IsolatedAsyncioTestCase):
    """``Vendor.catalog_exclude_prefixes`` — non-chat ids the live list carries.

    Exclusion is NOT withholding: an excluded id was never a chat model to
    choose, so it lands in neither ``data`` nor ``hidden``. And it applies
    to LIVE entries only — the row's declared ``static_ids`` are curated by
    whoever wrote the row, and filtering them would second-guess the
    declaration.
    """

    def _vendor(self, **over) -> Vendor:
        base = dict(
            vendor_id="acme", display_suffix=" · Acme", namespace="claude-acme/",
            upstream="https://api.acme.example", secret_keys=("acme_key",),
            bare_id_prefixes=("acme-",),
            catalog_url="https://lists.acme.example/v1/models",
            catalog_exclude_prefixes=("acme-x", "wan"),
            static_ids=("acme-keep",),
        )
        base.update(over)
        return Vendor(**base)

    async def test_the_shipped_row_publishes_the_chat_ids_and_excludes_the_rest(
        self,
    ) -> None:
        for catalog_filter in (cat.CATALOG_FILTER_ALL, cat.DEFAULT_CATALOG_FILTER):
            with self.subTest(catalog_filter=catalog_filter):
                calls: list[str] = []
                service = _service(
                    responses={"compatible-mode": QWEN_LIVE_MODELS},
                    clock=_Clock(), calls=calls, vendor=VENDORS["qwen"],
                )
                catalog = await service.union(
                    table=NO_TABLE, catalog_filter=catalog_filter,
                )
                self.assertEqual(catalog.sources["qwen"], cat.SOURCE_LIVE)
                self.assertEqual(
                    [url for url in calls if "qwencloudapi" in url],
                    [VENDORS["qwen"].catalog_url],
                )
                published = {
                    e.id for e in catalog.entries
                    if e.id.startswith("claude-gw/qwen/")
                }
                chat = {"claude-gw/qwen/" + m for m in QWEN_CHAT_NINE}
                hidden_two = {
                    "claude-gw/qwen/qwen3.7-plus",
                    "claude-gw/qwen/deepseek-v4-pro",
                }
                self.assertTrue(published <= chat - hidden_two, published - chat)
                if catalog_filter == cat.CATALOG_FILTER_ALL:
                    self.assertEqual(published, chat - hidden_two,
                                     "all minus the curated-hidden two")
                for hid in hidden_two:
                    self.assertIn(hid, catalog.hidden)
                listed = published | set(catalog.hidden)
                for excluded in QWEN_EXCLUDED_SIX:
                    for spelling in (
                        excluded,
                        f"claude-gw/qwen/{excluded}",
                        f"claude-gw/qwen/{excluded}[1m]",
                    ):
                        self.assertNotIn(spelling, listed)

    async def test_excluded_ids_never_reach_the_family_floors(self) -> None:
        """An excluded id must not lend its window to a kept sibling: the
        exclusion runs before window resolution, exactly like the truth
        filter. Both ids below are family ``acme-x`` (5 before 6); the older
        one states 64K and is excluded, so the kept row must resolve
        ``unknown`` rather than inherit from a model the catalog dropped."""
        service = _service(
            responses={
                "lists.acme.example": {"data": [
                    {"id": "acme-5x", "max_input_tokens": 64_000},
                    {"id": "acme-6x"},
                ]},
            },
            clock=_Clock(),
            vendor=self._vendor(catalog_exclude_prefixes=("acme-5",)),
        )
        catalog = await service.union(table=NO_TABLE)
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_LIVE)
        kept = next(e for e in catalog.entries if e.id == "claude-acme/acme-6x")
        self.assertEqual(kept.window_source, cat.WINDOW_UNKNOWN)

    async def test_exclusion_applies_to_live_entries_only(self) -> None:
        """The declared fallback serves a static id that MATCHES an exclude
        prefix — the curation already happened when the row was written."""
        vendor = self._vendor(
            catalog_exclude_prefixes=("acme-x",),
            static_ids=("acme-x-old", "acme-keep"),
        )
        # Live side: the matching id is dropped before anything sees it.
        live = _service(
            responses={
                "lists.acme.example": {"data": [
                    {"id": "acme-x-live"}, {"id": "acme-live2"},
                ]},
            },
            clock=_Clock(), vendor=vendor,
        )
        catalog = await live.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_LIVE)
        self.assertEqual(
            sorted(e.id for e in catalog.entries if e.id.startswith("claude-acme/")),
            ["claude-acme/acme-live2"],
        )
        # Declared side: no filtering of the curated list.
        declared = _service(responses={}, clock=_Clock(), vendor=vendor)
        fallback = await declared.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        self.assertEqual(fallback.sources["acme"], cat.SOURCE_DECLARED)
        self.assertEqual(
            sorted(e.id for e in fallback.entries if e.id.startswith("claude-acme/")),
            ["claude-acme/acme-keep", "claude-acme/acme-x-old"],
        )

    async def test_exclusion_is_case_insensitive(self) -> None:
        service = _service(
            responses={
                "lists.acme.example": {"data": [
                    {"id": "ACME-X-Upper"}, {"id": "WAN-image"}, {"id": "acme-1"},
                ]},
            },
            clock=_Clock(),
            vendor=self._vendor(),
        )
        catalog = await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(ids, ["claude-acme/acme-1"])

    async def test_an_empty_exclude_tuple_is_a_no_op(self) -> None:
        """The default: odd-shaped ids pass through untouched. The shipped
        standard-path row declares no exclusions either — its live list is
        the truth filter's business, not a prefix filter's."""
        self.assertEqual(ACME.catalog_exclude_prefixes, ())
        self.assertEqual(VENDORS["zai"].catalog_exclude_prefixes, ())
        service = _service(
            responses={
                "acme.example": {"data": [
                    {"id": "auto"}, {"id": "wan2.7-image"}, {"id": "acme-1"},
                ]},
            },
            clock=_Clock(),
        )
        catalog = await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_LIVE)
        ids = sorted(e.id for e in catalog.entries if e.id.startswith("claude-acme/"))
        self.assertEqual(
            ids, ["claude-acme/acme-1", "claude-acme/auto", "claude-acme/wan2.7-image"],
        )


class VerifiedIdsTests(unittest.IsolatedAsyncioTestCase):
    """``Vendor.verified_ids`` — the truth filter over a vendor's list.

    A vendor whose endpoint LISTS ids that reroute server-side to other
    models: the picker must not offer an id that answers as something else.
    The filter runs before window resolution and the latest-only filter, so
    an unverified id cannot feed a family floor or pose as the newest of a
    family it is not really in.
    """

    VERIFIED = ("acme-2",)

    def _vendor(self, **over) -> Vendor:
        base = dict(
            vendor_id="acme", display_suffix=" · Acme", namespace="claude-acme/",
            upstream="https://api.acme.example", secret_keys=("acme_key",),
            bare_id_prefixes=("acme-",),
        )
        base.update(over)
        return Vendor(**base)

    async def _union(self, vendor, *, responses=None):
        service = _service(
            responses=(
                {"api.acme.example": {"data": [
                    {"id": "acme-1", "max_input_tokens": 64000},
                    {"id": "acme-2"},
                    {"id": "acme-3"},
                ]}}
                if responses is None else responses
            ),
            clock=_Clock(),
            vendor=vendor,
        )
        return await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )

    async def test_live_entries_off_the_list_are_withheld(self) -> None:
        catalog = await self._union(
            self._vendor(verified_ids=self.VERIFIED),
        )
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(ids, ["claude-acme/acme-2"])

    async def test_withheld_ids_land_in_hidden_under_namespaced_spelling(self) -> None:
        catalog = await self._union(
            self._vendor(verified_ids=self.VERIFIED),
        )
        self.assertIn("claude-acme/acme-1", catalog.hidden)
        self.assertIn("claude-acme/acme-3", catalog.hidden)

    async def test_the_filter_applies_to_the_static_fallback_too(self) -> None:
        """A fallback list is as capable of carrying a dead id as a live
        one; the truth claim is about the endpoint, not the source."""
        vendor = self._vendor(
            verified_ids=self.VERIFIED,
            static_ids=("acme-1", "acme-2", "acme-3"),
        )
        service = _service(responses={}, clock=_Clock(), vendor=vendor)
        catalog = await service.union(
            table=NO_TABLE, catalog_filter=cat.CATALOG_FILTER_ALL,
        )
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(ids, ["claude-acme/acme-2"])
        # The fallback list is the row's own static_ids, so the source is
        # ``declared`` — the truth filter above it is source-agnostic.
        self.assertEqual(catalog.sources["acme"], cat.SOURCE_DECLARED)

    async def test_a_row_without_verified_ids_filters_nothing(self) -> None:
        catalog = await self._union(self._vendor())
        ids = [e.id for e in catalog.entries if e.id.startswith("claude-acme/")]
        self.assertEqual(
            sorted(ids),
            ["claude-acme/acme-1", "claude-acme/acme-2", "claude-acme/acme-3"],
        )

    async def test_a_withheld_id_does_not_feed_a_family_floor(self) -> None:
        """``acme-1`` states a 64K window; ``acme-2`` states none. With both
        kept, ``acme-2`` inherits its older sibling's window. With the older
        sibling withheld by the truth filter, inheritance from a model that
        does not answer as itself would be a window claim sourced from a
        fiction — so the kept row must resolve ``unknown`` instead."""
        filtered = await self._union(
            self._vendor(verified_ids=self.VERIFIED),
        )
        kept = next(e for e in filtered.entries if e.id == "claude-acme/acme-2")
        self.assertEqual(kept.window_source, cat.WINDOW_UNKNOWN)

        unfiltered = await self._union(self._vendor())
        inheriting = next(
            e for e in unfiltered.entries if e.id == "claude-acme/acme-2"
        )
        self.assertTrue(
            inheriting.window_source.startswith(cat.WINDOW_INHERITED_PREFIX),
            inheriting.window_source,
        )

    async def test_the_withhold_log_is_deduped_per_vendor_and_set(self) -> None:
        """A picker refresh reprints nothing while the withheld set is
        unchanged, and the day it grows the new line is the operator's
        signal."""
        vendor = self._vendor(verified_ids=self.VERIFIED)
        service = _service(
            responses={"api.acme.example": {"data": [
                {"id": "acme-1"}, {"id": "acme-2"},
            ]}},
            clock=_Clock(),
            vendor=vendor,
        )
        with self.assertLogs(cat.logger, level="INFO") as first:
            await service.union(table=NO_TABLE)
            await service.union(table=NO_TABLE)
        lines = [
            line for line in first.output if "withhold" in line
        ]
        self.assertEqual(len(lines), 1)

    async def test_the_shipped_subscription_row_publishes_exactly_the_verified_ids(self) -> None:
        """The owner ruling (2026-09-21) as an observable: that endpoint
        still LISTS its older ids but they reroute server-side to other
        models, so the picker offers exactly the two verified ones — under
        the default latest-only filter AND under ``all``, which is the
        point: ``all`` is the "I need a previous version" escape hatch, and
        an id that answers as a different model is not a version of
        anything."""
        listed = {
            "data": [
                {"id": model_id}
                for model_id in (
                    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1",
                    "glm-5", "glm-5-turbo", "glm-4.7",
                )
            ],
        }
        for catalog_filter in (cat.DEFAULT_CATALOG_FILTER, cat.CATALOG_FILTER_ALL):
            with self.subTest(catalog_filter=catalog_filter):
                service = _service(
                    responses={"api.z.ai": listed},
                    clock=_Clock(),
                    vendor=VENDORS["zai"],
                )
                catalog = await service.union(
                    table=NO_TABLE, catalog_filter=catalog_filter,
                )
                ids = {
                    e.id for e in catalog.entries
                    if e.id.startswith("claude-gw/")
                }
                self.assertEqual(
                    ids,
                    {"claude-gw/glm-5.3", "claude-gw/glm-5.3-flash"},
                )
                self.assertIn("claude-gw/glm-5.2", catalog.hidden)


class StaticPrecedenceTests(unittest.TestCase):
    """The documented rule: a non-empty ``static_ids`` wins over the snapshot.

    Driven through ``_resolve_static_tables`` directly with a synthetic
    snapshot, so the rule is pinned independently of whatever
    ``static_catalog.json`` happens to contain today.
    """

    SHIPPED = {"acme": (cat.CatalogEntry("from-snapshot", "From Snapshot"),)}

    def _row(self, **over) -> Vendor:
        base = dict(
            vendor_id="acme", display_suffix="", namespace="claude-acme/",
            upstream="https://api.acme.example", secret_keys=("k",),
            bare_id_prefixes=(),
        )
        base.update(over)
        return Vendor(**base)

    def _resolve(self, vendor: Vendor) -> dict:
        return cat.CatalogService._resolve_static_tables(
            dict(self.SHIPPED), {vendor.vendor_id: vendor},
        )

    def test_a_declaring_row_wins_over_the_shipped_block(self) -> None:
        resolved = self._resolve(self._row(static_ids=("declared",)))
        self.assertEqual([e.id for e in resolved["acme"]], ["declared"])

    def test_an_empty_declaration_defers_to_the_shipped_block(self) -> None:
        """The shipped default, so today's behaviour is snapshot-driven."""
        self.assertEqual(
            [e.id for e in self._resolve(ACME)["acme"]], ["from-snapshot"],
        )

    def test_other_families_are_untouched(self) -> None:
        shipped = dict(self.SHIPPED)
        shipped["anthropic"] = (cat.CatalogEntry("first-party", "First Party"),)
        resolved = cat.CatalogService._resolve_static_tables(
            shipped, {"acme": self._row(static_ids=("declared",))},
        )
        self.assertEqual([e.id for e in resolved["anthropic"]], ["first-party"])

    def test_shadowing_a_shipped_block_is_logged_not_silent(self) -> None:
        """The cost of this precedence is that a declaring row stops tracking
        the snapshot. A silent override is the version that wastes an
        afternoon, so the shadowing case says so."""
        with self.assertLogs(cat.logger, level="INFO") as captured:
            self._resolve(self._row(static_ids=("declared",)))
        joined = "\n".join(captured.output)
        self.assertIn("acme", joined)
        self.assertIn("static_ids", joined)

    def test_declaring_where_no_shipped_block_exists_does_not_log(self) -> None:
        """Nothing was overridden, so there is nothing to report."""
        row = self._row(
            vendor_id="newcomer", namespace="claude-new/", static_ids=("n-1",),
        )
        with self.assertNoLogs(cat.logger, level="INFO"):
            cat.CatalogService._resolve_static_tables(
                dict(self.SHIPPED), {"newcomer": row},
            )




class CatalogHideIdsTests(unittest.TestCase):
    """The curated-hide list (``catalog_hide_ids``, owner ruling 2026-09-22)
    pinned on its four load-bearing edges. Curation is neither exclusion
    (absent entirely) nor truth-withholding (rerouted ids): hidden under
    both catalog filters, reported in ``_vct_catalog_hidden``, routable by
    name — and resolution still sees hidden ids, so family floors stay
    honest while the picker narrows."""

    def _union(self, hide, responses=None):
        vendor = dataclasses.replace(VENDORS["qwen"], catalog_hide_ids=hide)
        return asyncio.run(
            _service(
                responses=responses if responses is not None else {"compatible-mode": QWEN_LIVE_MODELS},
                clock=_Clock(), vendor=vendor,
            ).union(table=load_seed())
        )

    def test_hiding_a_family_newest_promotes_its_older_sibling(self):
        union = self._union(("qwen3.8-max",))
        published = {e.id for e in union.entries}
        self.assertIn("claude-gw/qwen/qwen3.7-max", published,
                      "curation narrows a family, it does not freeze it out")
        self.assertIn("claude-gw/qwen/qwen3.8-max", union.hidden)

    def test_hide_matching_is_case_insensitive(self):
        union = self._union(("QWEN3.7-PLUS",))
        self.assertIn("claude-gw/qwen/qwen3.7-plus", union.hidden)
        self.assertNotIn("claude-gw/qwen/qwen3.7-plus",
                         {e.id for e in union.entries})

    def test_first_party_ids_are_inert_to_a_vendor_hide_list(self):
        union = self._union(("qwen3.8-max", "glm-5.3"))
        published = {e.id for e in union.entries}
        # Spelled as the picker advertises them: a 1M model shows its
        # ``[1m]`` row by default, the 200K one shows its plain row.
        for first_party in (
            "claude-opus-5[1m]", "claude-sonnet-5[1m]", "claude-haiku-4-5-20251001",
        ):
            self.assertIn(first_party, published,
                          "a vendor hide list must never reach first party")

    def test_a_hidden_cited_sibling_still_feeds_the_family_floor(self):
        """Asymmetry vs the truth filter, pinned so the ORDER is what fails.

        Truth-withheld ids are removed BEFORE resolution (an unverified id
        must not lend a window); curated-hidden ids are removed AFTER, so a
        hidden sibling can still be the floor a newer id inherits from.

        This needs a SYNTHETIC family, and the first version of this test did
        not have one: it asserted on shipped ids where both glm rows carry
        their own 1M table row, so moving the hide before resolution left it
        green. The discriminating shape is a family where the hidden sibling
        is the ONLY holder of the larger window:

          acme-1  table 200K
          acme-2  table 1M   <- curated-hidden
          acme-3  live, no table row

        acme-3 must inherit 1M FROM acme-2. Run the hide first and acme-2 is
        gone before resolution, so acme-3 falls back to acme-1 at 200K — a
        model advertised at a fifth of its window.
        """
        table = table_of({"acme-1": 200_000, "acme-2": cat.ONE_M_WINDOW})
        payload = {"data": [{"id": i} for i in ("acme-1", "acme-2", "acme-3")]}
        vendor = dataclasses.replace(ACME, catalog_hide_ids=("acme-2",))
        union = asyncio.run(
            _service(
                responses={"api.acme.example": payload},
                clock=_Clock(), vendor=vendor,
            ).union(table=table, catalog_filter=cat.CATALOG_FILTER_ALL)
        )
        rows = {e.id: e for e in union.entries}
        self.assertIn("claude-acme/acme-3[1m]", rows)
        self.assertEqual(
            rows["claude-acme/acme-3[1m]"].window_source,
            f"{cat.WINDOW_INHERITED_PREFIX}acme-2",
            "the floor must come from the HIDDEN sibling, not from acme-1",
        )
        self.assertIn("claude-acme/acme-2[1m]", union.hidden)
        self.assertNotIn("claude-acme/acme-2[1m]", rows)



class WindowRowsTests(unittest.TestCase):
    """``window_rows`` decides how many rows ONE first-party model occupies.

    The knob is deliberately narrow: it touches first-party 1M models and
    nothing else. A vendor offers its 1M variant as a suffix ON its single
    row, and a 200K first-party model has no companion to choose between —
    both must be inert to it, or "show me fewer rows" would start deleting
    models from the picker."""

    def _union(self, **kwargs):
        return asyncio.run(
            _service(
                responses={"compatible-mode": QWEN_LIVE_MODELS},
                clock=_Clock(), vendor=VENDORS["qwen"],
            ).union(table=load_seed(), **kwargs)
        )

    def test_a_non_1m_first_party_row_is_untouched(self):
        """The knob withholds a SECOND spelling. A model that never had one
        must keep the only row it has."""
        published = {e.id for e in self._union().entries}
        self.assertIn("claude-haiku-4-5-20251001", published)

    def test_vendor_rows_are_inert_to_the_knob(self):
        one_row = {e.id for e in self._union().entries}
        both = {
            e.id
            for e in self._union(window_rows=cat.WINDOW_ROWS_BOTH).entries
        }
        vendor_one = {i for i in one_row if i.startswith("claude-gw/")}
        vendor_both = {i for i in both if i.startswith("claude-gw/")}
        self.assertEqual(vendor_one, vendor_both)
        self.assertTrue(vendor_one, "precondition: the fixture has vendor rows")

    def test_the_withheld_plain_row_is_reported_not_silently_dropped(self):
        union = self._union()
        self.assertIn("claude-opus-5", union.hidden)
        self.assertNotIn("claude-opus-5", {e.id for e in union.entries})

    def test_both_restores_the_plain_row_and_stops_reporting_it(self):
        union = self._union(window_rows=cat.WINDOW_ROWS_BOTH)
        ids = {e.id for e in union.entries}
        self.assertIn("claude-opus-5", ids)
        self.assertIn("claude-opus-5[1m]", ids)
        self.assertNotIn("claude-opus-5", union.hidden)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
