# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-10 — the gateway refuses an UNKNOWN model id (F-W1-11a), and
the per-route window override mechanism stays honest (F-W1-19), and the
retired ``glm-5.2`` is refused on every route (owner ruling 2026-10-01).

F-W1-11a, owner-observed: two hand-written definitions named
``claude-gw/qwen3.8-max`` and ``claude-gw/deepseek-4.1-flash`` (the nested
``qwen/`` segment missing; the ``v`` missing). The shared ``claude-gw/``
namespace accepted anything, so both went to the DEFAULT vendor, which
answered 400 about a model it never had, and the session died with a
StopFailure naming the wrong problem. The gateway now refuses such an id
locally with the closest valid ids — and never forwards it anywhere.

The router is exercised through its pure function AND through the real
aiohttp app with stub upstreams (the ``tests/test_model_router_server.py``
harness) — the live gateway on this machine is never contacted.
"""
from __future__ import annotations

import dataclasses
import unittest

from model_router import routing
from model_router.context_table import (
    ContextTable,
    ModelContext,
    SOURCE_EXPORT,
    load_seed,
)
from model_router.routing import Route, RouteError
from model_router.vendors import VENDORS

from tests.test_model_router_server import GatewayTestBase


class UnknownIdRefusalTests(unittest.TestCase):
    def test_a_missing_nested_namespace_is_refused_with_the_right_id(self) -> None:
        decision = routing.route("claude-gw/qwen3.8-max")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.status, 400)
        self.assertEqual(decision.reason, "unknown_model")
        self.assertEqual(decision.suggestions[0], "claude-gw/qwen/qwen3.8-max")
        self.assertIn("did you mean", decision.message.lower())
        self.assertIn("claude-gw/qwen/qwen3.8-max", decision.message)

    def test_a_misspelled_id_gets_its_closest_neighbour_and_keeps_1m(self) -> None:
        decision = routing.route("claude-gw/deepseek-4.1-flash[1m]")
        assert isinstance(decision, RouteError)
        self.assertIn("claude-gw/qwen/deepseek-v4.1-flash[1m]", decision.suggestions)

    def test_an_id_no_row_knows_is_refused_not_sent_to_the_default_vendor(self) -> None:
        decision = routing.route("claude-gw/totally-made-up")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "unknown_model")

    def test_every_valid_spelling_still_routes(self) -> None:
        for model_id, family in (
            ("claude-gw/glm-5.3", "zai"), ("claude-gw/glm-5.3[1m]", "zai"),
            ("claude-gw/qwen/glm-5.3", "qwen"),    # declared on the qwen row
            ("claude-gw/qwen/glm-5.3[1m]", "qwen"),
            ("claude-gw/qwen/qwen3.8-max[1m]", "qwen"),
            ("claude-gw/qwen/deepseek-v4.1-flash[1m]", "qwen"),
            ("qwen3.8-max", "qwen"), ("glm-4.6", "zai"),
        ):
            with self.subTest(model=model_id):
                decision = routing.route(model_id)
                assert isinstance(decision, Route), decision
                self.assertEqual(decision.family_id, family)

    def test_an_id_the_vendors_own_list_published_is_never_refused(self) -> None:
        """A picker row comes from the catalog cache; the router trusts it."""
        refused = routing.route("claude-gw/qwen/kimi-k9")
        assert isinstance(refused, RouteError)
        allowed = routing.route(
            "claude-gw/qwen/kimi-k9", known_ids={"qwen": {"kimi-k9"}})
        assert isinstance(allowed, Route)
        self.assertEqual(allowed.forward_model, "kimi-k9")

    def test_a_row_declaring_nothing_cannot_be_judged_and_still_routes(self) -> None:
        acme = dataclasses.replace(
            VENDORS["zai"], vendor_id="acme", namespace="claude-acme/",
            bare_id_prefixes=(), verified_ids=(), static_ids=(),
            catalog_hide_ids=())
        decision = routing.route("claude-acme/anything", {"acme": acme})
        assert isinstance(decision, Route)

    def test_validate_model_id_is_stricter_than_routing(self) -> None:
        ok, _, _ = routing.validate_model_id("claude-gw/qwen/qwen3.8-max[1m]")
        self.assertTrue(ok)
        # Family-shaped but not a model the row is known to serve: routable,
        # but a CONFIG naming it is flagged with the closest id.
        ok, reason, suggestions = routing.validate_model_id(
            "claude-gw/qwen/deepseek-4.1-flash")
        self.assertFalse(ok, reason)
        self.assertIn("claude-gw/qwen/deepseek-v4.1-flash", suggestions)


class UnknownIdEndToEndTests(GatewayTestBase):
    """Through the real app: refused locally, NO upstream request."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        base = VENDORS["qwen"]
        url = str(self.vendor_up.server.make_url("")).rstrip("/")  # type: ignore[union-attr]
        self.qwen = dataclasses.replace(
            base, upstream=url, catalog_url=f"{url}/v1/models")
        self.client = await self.make_client(vendors={
            self.vendor.vendor_id: self.vendor, "qwen": self.qwen,
        })

    async def test_unknown_id_is_a_local_400_naming_the_fix(self) -> None:
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/qwen3.8-max", "messages": []},
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("claude-gw/qwen/qwen3.8-max", body["error"]["message"])
        self.assertEqual(self.vendor_up.message_requests, [])
        self.assertEqual(self.anthropic_up.message_requests, [])

    async def test_a_catalog_published_id_routes_once_the_catalog_was_read(self) -> None:
        self.vendor_up.models_payload = {"data": [{"id": "kimi-k9"}]}
        resp = await self.client.get("/v1/models", headers=self.auth())
        self.assertEqual(resp.status, 200)
        resp = await self.client.post(
            "/v1/messages", headers=self.auth(),
            json={"model": "claude-gw/qwen/kimi-k9", "messages": []},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.vendor_up.message_requests), 1)


class Glm52RetiredTests(unittest.TestCase):
    """Owner 2026-10-01: glm-5.2 is not provided at all, only glm-5.3."""

    def test_every_spelling_is_refused_and_suggests_glm_5_3(self) -> None:
        for model_id in (
            "claude-gw/glm-5.2", "claude-gw/glm-5.2[1m]", "glm-5.2",
            "claude-gw/qwen/glm-5.2", "claude-gw/qwen/glm-5.2[1m]",
            "claude-gw/GLM-5.2",
        ):
            with self.subTest(model=model_id):
                decision = routing.route(model_id)
                assert isinstance(decision, RouteError), decision
                self.assertEqual(decision.status, 400)
                self.assertEqual(decision.reason, "unknown_model")
                self.assertTrue(
                    any("glm-5.3" in s for s in decision.suggestions),
                    decision.suggestions,
                )
                self.assertFalse(
                    any("glm-5.2" in s for s in decision.suggestions),
                    "a retired id is never offered back",
                )

    def test_a_vendor_list_that_still_carries_it_does_not_make_it_routable(self) -> None:
        """The live lists of both vendors still name glm-5.2; the router is
        handed that as ``known_ids`` and must still refuse."""
        known = {"zai": {"glm-5.2", "glm-5.3"}, "qwen": {"glm-5.2", "glm-5.3"}}
        for model_id in ("claude-gw/glm-5.2", "claude-gw/qwen/glm-5.2", "glm-5.2"):
            with self.subTest(model=model_id):
                decision = routing.route(model_id, known_ids=known)
                self.assertIsInstance(decision, RouteError)

    def test_both_rows_retire_it_and_neither_declares_it(self) -> None:
        for vendor in VENDORS.values():
            with self.subTest(vendor=vendor.vendor_id):
                self.assertIn("glm-5.2", vendor.retired_ids)
                for declared in (
                    *vendor.static_ids, *vendor.verified_ids,
                    *vendor.catalog_hide_ids,
                ):
                    self.assertNotEqual(declared, "glm-5.2")

    def test_glm_5_3_is_one_m_on_both_routes_with_the_suffix(self) -> None:
        seed = load_seed()
        for spelling in ("claude-gw/glm-5.3", "claude-gw/qwen/glm-5.3"):
            with self.subTest(model=spelling):
                row = seed.lookup_id(spelling)
                assert row is not None
                self.assertEqual(row.context_window, 1_000_000)
                self.assertTrue(row.window_1m)
                self.assertTrue(seed.advertise_1m(spelling))
        self.assertIsNone(seed.lookup_id("claude-gw/glm-5.2"))

    def test_the_retired_id_is_not_in_the_static_snapshot(self) -> None:
        import json
        from pathlib import Path
        import model_router
        snap = json.loads(
            (Path(model_router.__file__).parent / "static_catalog.json").read_text(),
        )
        ids = {m["id"] for fam in snap["families"].values() for m in fam["models"]}
        self.assertNotIn("glm-5.2", ids)
        self.assertIn("glm-5.3", ids)


class PerRouteWindowOverrideTests(unittest.TestCase):
    """F-W1-19 mechanism, kept alive on a synthetic row: no shipped model
    needs a per-route override today (glm-5.3 is 1M on both routes), but a
    vendor that documents a smaller window for a shared model must still be
    able to say so, and a launcher export must not hide it."""

    def _rows(self):
        cap = ModelContext(
            model_id="shared-1", vendor="qwen", context_window=198_000,
            max_output=0, window_1m=False, source="https://docs.example/cap")
        seed_row = ModelContext(
            model_id="shared-1", vendor="zai", context_window=1_000_000,
            max_output=128_000, window_1m=True, source="https://docs.example/z",
            vendor_overrides={"qwen": cap})
        return seed_row, cap

    def test_the_named_vendor_gets_its_own_figure(self) -> None:
        seed_row, cap = self._rows()
        table = ContextTable(
            rows={"shared-1": seed_row}, source=SOURCE_EXPORT, path=None)
        self.assertEqual(table.lookup("shared-1", "qwen"), cap)
        self.assertEqual(table.lookup("shared-1", "zai"), seed_row)

    def test_a_launcher_export_row_cannot_hide_the_endpoint_figure(self) -> None:
        seed_row, cap = self._rows()
        exported = ModelContext(
            model_id="shared-1", vendor="zai", context_window=1_000_000,
            max_output=128_000, window_1m=True, source="https://docs.example/z")
        table = ContextTable(
            rows={"shared-1": exported}, source=SOURCE_EXPORT, path=None,
            fallback_rows={"shared-1": seed_row})
        self.assertEqual(table.lookup("shared-1", "qwen"), cap)
        self.assertEqual(table.lookup("shared-1", "zai"), exported)


if __name__ == "__main__":
    unittest.main()
