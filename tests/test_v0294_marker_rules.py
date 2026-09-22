# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The marker rule, pinned against the SHIPPED catalog rather than examples.

``routing.route`` decides where a model id goes from one property of the
string: does it contain ``claude``/``anthropic``. That rule is invisible from
any single call site, so it is pinned here against every id the gateway can
actually hand a user — the shipped ``static_catalog.json``, which is exactly
what the picker shows when a live fetch fails.

The specific regression this guards: an id that the CATALOG offers and the
ROUTER refuses. The user sees a model, picks it, and gets a 400 — which is
indistinguishable, from their chair, from the gateway being broken.
"""
from __future__ import annotations

import json
import unittest

from model_router import routing
from model_router.catalog import STATIC_CATALOG_PATH
from model_router.routing import Route, RouteError
from model_router.vendors import (
    ANTHROPIC_FAMILY,
    CLAUDE_ID_MARKERS,
    VENDORS,
    Vendor,
)


def _static_families() -> dict[str, list[str]]:
    payload = json.loads(STATIC_CATALOG_PATH.read_text(encoding="utf-8"))
    return {
        family: [row["id"] for row in block.get("models", [])]
        for family, block in payload["families"].items()
    }


def _vendor_ids(vendor_id: str, vendor: Vendor) -> list[str]:
    """The ids the picker can hand a user for this vendor when no live fetch
    answers — the same precedence ``catalog._resolve_static_tables`` applies:
    the row's declared ids win over the shipped snapshot block, so a vendor
    whose fallback is its own row (a no-discovery row, or a ``catalog_url``
    row in its keyless/fetch-failed moment) is covered from there, not from
    a snapshot block it deliberately does not have."""
    if vendor.static_ids:
        return list(vendor.static_ids)
    return _static_families().get(vendor_id, [])


class ShippedCatalogRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.families = _static_families()

    def test_the_snapshot_still_has_both_families(self) -> None:
        """Positive control: an empty catalog would make the loop vacuous."""
        self.assertIn(ANTHROPIC_FAMILY.family_id, self.families)
        for vendor_id, vendor in VENDORS.items():
            if vendor.static_ids:
                # A row whose fallback is its OWN declared list has no
                # snapshot block by design — whether it has no discovery at
                # all (``catalog_path is None``) or a ``catalog_url``
                # override whose fetch can fail. The positive control for it
                # is that those ids exist (the routing loops below draw
                # their ids from there).
                self.assertNotIn(
                    vendor_id, self.families,
                    f"{vendor_id} declares static_ids AND has a snapshot "
                    "block — the row's list shadows it into dead weight",
                )
            else:
                self.assertIn(vendor_id, self.families)
        for family, ids in self.families.items():
            self.assertTrue(ids, f"{family} block is empty")

    def test_every_first_party_id_routes_to_anthropic(self) -> None:
        for model_id in self.families[ANTHROPIC_FAMILY.family_id]:
            with self.subTest(model=model_id):
                self.assertTrue(
                    any(m in model_id.lower() for m in CLAUDE_ID_MARKERS),
                    "a first-party id without a marker would be dropped by the "
                    "client's own discovery filter",
                )
                decision = routing.route(model_id)
                assert isinstance(decision, Route)
                self.assertTrue(decision.is_anthropic)
                self.assertEqual(decision.forward_model, model_id)

    def test_every_vendor_id_routes_to_its_vendor_bare_and_namespaced(self) -> None:
        for vendor_id, vendor in VENDORS.items():
            for model_id in _vendor_ids(vendor_id, vendor):
                for spelling in (model_id, f"{vendor.namespace}{model_id}"):
                    with self.subTest(model=spelling):
                        decision = routing.route(spelling)
                        assert isinstance(decision, Route)
                        self.assertFalse(decision.is_anthropic)
                        self.assertEqual(decision.forward_model, model_id)
                        if spelling != model_id or model_id.startswith(
                            vendor.bare_id_prefixes
                        ):
                            self.assertEqual(decision.family_id, vendor_id)
                        # A bare id that carries none of this vendor's own
                        # prefixes (a shared id offered on a nested route,
                        # whose bare spelling belongs to the prefix's owner)
                        # still routes — with the forward model intact. The
                        # regression this file guards is a REFUSAL, not the
                        # cross-vendor ownership of a shared bare id.

    def test_no_shipped_id_carries_a_marker_into_a_vendor(self) -> None:
        for vendor_id, vendor in VENDORS.items():
            for model_id in _vendor_ids(vendor_id, vendor):
                with self.subTest(model=model_id):
                    self.assertFalse(
                        any(m in model_id.lower() for m in CLAUDE_ID_MARKERS),
                    )


class MarkerRuleTests(unittest.TestCase):
    def test_an_unreleased_claude_id_routes_first_party_by_the_marker(self) -> None:
        """No allow-list: a model Anthropic ships tomorrow works today."""
        decision = routing.route("claude-newmodel-9")
        assert isinstance(decision, Route)
        self.assertTrue(decision.is_anthropic)
        self.assertEqual(decision.forward_model, "claude-newmodel-9")

    def test_an_anthropic_marker_alone_is_enough(self) -> None:
        decision = routing.route("anthropic-experimental-1")
        assert isinstance(decision, Route)
        self.assertTrue(decision.is_anthropic)

    def test_a_foreign_id_is_a_local_400_that_names_it(self) -> None:
        decision = routing.route("gpt-9")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.status, 400)
        self.assertEqual(decision.reason, "unknown_model")
        self.assertIn("gpt-9", decision.message)

    def test_the_refusal_says_where_the_real_names_live(self) -> None:
        decision = routing.route("gpt-9")
        assert isinstance(decision, RouteError)
        for vendor in VENDORS.values():
            self.assertIn(vendor.namespace, decision.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
