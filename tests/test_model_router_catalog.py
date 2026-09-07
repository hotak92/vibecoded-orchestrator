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

import json
import unittest
from pathlib import Path

from model_router import catalog as cat
from model_router.context_table import load_seed
from model_router.vendors import ANTHROPIC_FAMILY, VENDORS, AnthropicFamily, Vendor

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
        known = {ANTHROPIC_FAMILY.family_id, *VENDORS}
        self.assertEqual(set(self.payload["families"]), known)

    def test_the_vendor_block_matches_the_context_seed_exactly(self) -> None:
        """A snapshot id with no seed row would be advertised with the client's
        conservative default; a seed row with no snapshot id would be a
        citation for a model the fallback never offers."""
        seed_ids = set(load_seed().rows)
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
        _, sources = await service.union(advertise_1m=lambda _id: False)
        self.assertEqual(sources, {"anthropic": cat.SOURCE_LIVE, "acme": cat.SOURCE_LIVE})

    async def test_a_failed_fetch_reports_static_not_live(self) -> None:
        """A family served from the snapshot must never claim to be live."""
        service = _service(responses={}, clock=_Clock())
        _, sources = await service.union(advertise_1m=lambda _id: False)
        # 'acme' has no snapshot block, so it is genuinely unavailable; the
        # first-party family does, so it falls back.
        self.assertEqual(sources["anthropic"], cat.SOURCE_STATIC)
        self.assertEqual(sources["acme"], cat.SOURCE_EMPTY)

    async def test_no_oauth_token_means_no_first_party_fetch_at_all(self) -> None:
        calls: list[str] = []
        service = _service(
            responses={"first.example": {"data": [{"id": "claude-x"}]}},
            clock=_Clock(), token=None, calls=calls,
        )
        await service.union(advertise_1m=lambda _id: False)
        self.assertFalse([c for c in calls if "first.example" in c])

    async def test_no_vendor_key_means_no_vendor_fetch_at_all(self) -> None:
        calls: list[str] = []
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=_Clock(), key=None, calls=calls,
        )
        await service.union(advertise_1m=lambda _id: False)
        self.assertFalse([c for c in calls if "acme.example" in c])


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_live_result_is_cached_for_the_live_ttl(self) -> None:
        calls: list[str] = []
        clock = _Clock()
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=clock, live_ttl_s=600, calls=calls,
        )
        await service.union(advertise_1m=lambda _id: False)
        vendor_calls = len([c for c in calls if "acme.example" in c])
        clock.now += 599
        await service.union(advertise_1m=lambda _id: False)
        self.assertEqual(len([c for c in calls if "acme.example" in c]), vendor_calls)
        clock.now += 2
        await service.union(advertise_1m=lambda _id: False)
        self.assertGreater(len([c for c in calls if "acme.example" in c]), vendor_calls)

    async def test_a_static_fallback_is_retried_on_the_short_ttl(self) -> None:
        """The prototype cached the fallback under the LIVE ttl, so a one-minute
        outage cost six hours of a stale picker."""
        calls: list[str] = []
        clock = _Clock()
        service = _service(
            responses={}, clock=clock, live_ttl_s=21600, static_ttl_s=300, calls=calls,
        )
        await service.union(advertise_1m=lambda _id: False)
        first = len([c for c in calls if "first.example" in c])
        clock.now += 301
        await service.union(advertise_1m=lambda _id: False)
        self.assertGreater(len([c for c in calls if "first.example" in c]), first)


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_party_ids_are_published_verbatim(self) -> None:
        service = _service(
            responses={"first.example": {"data": [{"id": "claude-x", "display_name": "X"}]}},
            clock=_Clock(),
        )
        entries, _ = await service.union(advertise_1m=lambda _id: False)
        self.assertIn(cat.CatalogEntry("claude-x", "X"), entries)

    async def test_vendor_ids_are_namespaced_and_suffixed(self) -> None:
        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}]}},
            clock=_Clock(),
        )
        entries, _ = await service.union(advertise_1m=lambda mid: mid == "acme-1")
        match = [e for e in entries if e.id.startswith("claude-acme/")]
        self.assertEqual(match[0].id, "claude-acme/acme-1[1m]")
        self.assertIn("Acme", match[0].display_name)
        self.assertIn("1M ctx", match[0].display_name)

    async def test_the_one_m_decision_comes_from_the_caller_not_a_pattern(self) -> None:
        """The table decides, by exact id; the catalog never guesses."""
        asked: list[str] = []

        def advertise(model_id: str) -> bool:
            asked.append(model_id)
            return False

        service = _service(
            responses={"acme.example": {"data": [{"id": "acme-1"}, {"id": "acme-2"}]}},
            clock=_Clock(),
        )
        await service.union(advertise_1m=advertise)
        self.assertEqual(asked, ["acme-1", "acme-2"])

    async def test_first_party_entries_are_sorted_and_vendor_order_is_stable(self) -> None:
        service = _service(
            responses={
                "first.example": {"data": [{"id": "claude-z"}, {"id": "claude-a"}]},
                "acme.example": {"data": [{"id": "acme-2"}, {"id": "acme-1"}]},
            },
            clock=_Clock(),
        )
        entries, _ = await service.union(advertise_1m=lambda _id: False)
        ids = [e.id for e in entries]
        self.assertEqual(ids[:2], ["claude-a", "claude-z"])
        self.assertEqual(ids[2:], ["claude-acme/acme-2", "claude-acme/acme-1"])

    async def test_malformed_upstream_payloads_do_not_crash(self) -> None:
        for payload in ({}, {"data": "nope"}, {"data": [None, {"no_id": 1}]}, None):
            with self.subTest(payload=payload):
                service = _service(
                    responses={"acme.example": payload}, clock=_Clock(),
                )
                entries, sources = await service.union(advertise_1m=lambda _id: False)
                self.assertIsInstance(entries, list)
                self.assertIn(sources["acme"], (cat.SOURCE_STATIC, cat.SOURCE_EMPTY))


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
        entries, sources = await service.union(advertise_1m=lambda _id: False)
        return cat.to_models_response(entries, sources)

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
        self.assertEqual(body["_vct_catalog_source"]["acme"], cat.SOURCE_STATIC)

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
        await service.union(advertise_1m=lambda _id: False)
        first = len(calls)
        clock.now += 61
        await service.union(advertise_1m=lambda _id: False)
        self.assertGreater(len(calls), first)

    async def test_a_live_catalog_still_beats_a_declared_fallback(self) -> None:
        """`static_ids` is a fallback, not an override of reality."""
        service = _service(
            responses={"api.acme.example": {"data": [{"id": "acme-live"}]}},
            clock=_Clock(),
            vendor=self.ACME_WITH_IDS,
        )
        entries, sources = await service.union(advertise_1m=lambda _id: False)
        ids = [e.id for e in entries]
        self.assertEqual(sources["acme"], cat.SOURCE_LIVE)
        self.assertIn("claude-acme/acme-live", ids)
        self.assertNotIn("claude-acme/acme-large", ids)


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
