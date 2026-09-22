# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Routing decisions, the honest-naming guard, and the "config, not code" shape.

Three things are pinned here.

**Routing itself** — namespaced ids, bare vendor ids, first-party Claude ids,
the ``[1m]`` suffix, and every refusal.

**The honest-naming guard.** A model id containing ``claude``/``anthropic`` is
never forwarded to a vendor upstream. This is not a style rule: at least one
shipped vendor's endpoint answers such a name with its own small model and
returns HTTP 200 (documented by that vendor), so forwarding would substitute a
model silently. A local 400 is the only honest outcome.

**"Adding a vendor is a config row, not a code change"** — asserted
structurally rather than claimed in a comment. Two independent checks:

* a synthetic second vendor, built ONLY from a ``Vendor`` row, routes and
  catalogs correctly with no code touched anywhere;
* ``routing.py``, ``catalog.py`` and ``secrets.py`` are read from disk and
  must contain no vendor-specific literal. ``vendors.py`` is the exception —
  it is the registry.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from model_router import routing, vendors
from model_router.routing import Route, RouteError

MODULE_DIR = Path(routing.__file__).resolve().parent

#: Strings that name a specific vendor, product or endpoint. If one of these
#: appears in a vendor-neutral module, the registry has leaked into code and
#: the next vendor stops being a one-row change.
#:
#: The bare words "claude"/"anthropic" are NOT on this list on purpose: they
#: are the client's own discovery contract (Claude Code keeps ids containing
#: them), which the namespace design is built on top of, and they are named in
#: `vendors.CLAUDE_ID_MARKERS`.
VENDOR_LITERALS = (
    "glm",
    "z.ai",
    "zai",
    "claude-gw",
    "api.anthropic.com",
    "api.z.ai",
    "moonshot",
    "kimi",
    "qwen",
    "openrouter",
)

VENDOR_NEUTRAL_MODULES = ("routing.py", "catalog.py", "secrets.py")


def _synthetic_registry() -> dict[str, vendors.Vendor]:
    """A second vendor invented entirely out of a data row."""
    return {
        "zai": vendors.VENDORS["zai"],
        "acme": vendors.Vendor(
            vendor_id="acme",
            display_suffix=" · Acme plan",
            namespace="claude-acme/",
            upstream="https://api.acme.example/anthropic",
            secret_keys=("acme_api_key",),
            bare_id_prefixes=("acme-",),
            docs_url="https://docs.acme.example/models",
        ),
    }


class VendorNeutralityTests(unittest.TestCase):
    def test_registry_is_valid(self) -> None:
        vendors.validate_registry()

    def test_shipped_scope_is_exactly_two_vendors(self) -> None:
        """Narrow on purpose: only routes proven end-to-end ship.

        A disabled row would be untested code shipped as configuration, so
        vendors that have never made a live call are absent entirely rather
        than present-and-off. The subscription route and the Token-Plan route
        are both owner-ordered (2026-09-21) and both proven; the Token-Plan
        vendor's pay-as-you-go sibling is deliberately absent until its model
        list is extracted and proven.
        """
        self.assertEqual(sorted(vendors.VENDORS), ["qwen", "zai"])

    def test_vendor_neutral_modules_carry_no_vendor_literal(self) -> None:
        for name in VENDOR_NEUTRAL_MODULES:
            source = (MODULE_DIR / name).read_text(encoding="utf-8").lower()
            for literal in VENDOR_LITERALS:
                with self.subTest(module=name, literal=literal):
                    self.assertNotIn(
                        literal, source,
                        f"{name} names {literal!r}. Vendor specifics belong in "
                        "vendors.py; a literal here means adding the next "
                        "vendor is no longer a one-row change.",
                    )

    def test_a_new_vendor_needs_only_a_registry_row(self) -> None:
        registry = _synthetic_registry()
        decision = routing.route("claude-acme/acme-large", registry)
        assert isinstance(decision, Route)
        self.assertEqual(decision.upstream, "https://api.acme.example/anthropic")
        self.assertEqual(decision.forward_model, "acme-large")
        self.assertEqual(decision.family_id, "acme")
        # ...and its bare id form works too, from the same row.
        bare = routing.route("acme-large", registry)
        assert isinstance(bare, Route)
        self.assertEqual(bare.family_id, "acme")

    def test_longest_namespace_wins(self) -> None:
        registry = dict(_synthetic_registry())
        registry["acme_eu"] = vendors.Vendor(
            vendor_id="acme_eu",
            display_suffix=" · Acme EU",
            namespace="claude-acme/eu/",
            upstream="https://eu.api.acme.example/anthropic",
            secret_keys=("acme_eu_api_key",),
            bare_id_prefixes=(),
        )
        decision = routing.route("claude-acme/eu/acme-large", registry)
        assert isinstance(decision, Route)
        self.assertEqual(decision.family_id, "acme_eu")


class RegistryValidationTests(unittest.TestCase):
    def test_namespace_without_a_discovery_marker_is_rejected(self) -> None:
        """A namespace the client filters out would drop the whole catalog."""
        bad = {
            "acme": vendors.Vendor(
                vendor_id="acme",
                display_suffix="",
                namespace="acme/",
                upstream="https://api.acme.example",
                secret_keys=("k",),
                bare_id_prefixes=(),
            )
        }
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(bad)
        self.assertIn("discovery", str(ctx.exception))

    def test_namespace_must_end_with_a_separator(self) -> None:
        bad = {
            "acme": vendors.Vendor(
                vendor_id="acme",
                display_suffix="",
                namespace="claudeacme",
                upstream="https://api.acme.example",
                secret_keys=("k",),
                bare_id_prefixes=(),
            )
        }
        with self.assertRaises(vendors.RegistryError):
            vendors.validate_registry(bad)

    def test_bare_prefix_may_not_hijack_claude_ids(self) -> None:
        bad = {
            "acme": vendors.Vendor(
                vendor_id="acme",
                display_suffix="",
                namespace="claude-acme/",
                upstream="https://api.acme.example",
                secret_keys=("k",),
                bare_id_prefixes=("claude-",),
            )
        }
        with self.assertRaises(vendors.RegistryError):
            vendors.validate_registry(bad)

    def test_key_must_match_vendor_id(self) -> None:
        row = vendors.VENDORS["zai"]
        with self.assertRaises(vendors.RegistryError):
            vendors.validate_registry({"not-zai": row})

    def _acme(self, **over) -> dict:
        base = dict(
            vendor_id="acme",
            display_suffix="",
            namespace="claude-acme/",
            upstream="https://api.acme.example",
            secret_keys=("k",),
            bare_id_prefixes=(),
        )
        base.update(over)
        return {"acme": vendors.Vendor(**base)}

    def test_a_claude_marked_static_id_is_rejected(self) -> None:
        """A declared fallback id may not carry a Claude marker.

        This is the subtle sibling of the bare-prefix rule. A static id is
        advertised VERBATIM under the vendor namespace, so a Claude-marked
        entry would put a row in the picker that `routing.route` is required
        to refuse — the user sees a model, selects it, and gets a 400. The
        two assertions below are the two halves of that: the row is rejected,
        AND the id it would have produced is one route() really does refuse.
        """
        bad = self._acme(static_ids=("claude-3-opus",))
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(bad)
        self.assertIn("static_id", str(ctx.exception))

        advertised = routing.advertised_id(
            bad["acme"], "claude-3-opus", False,
        )
        decision = routing.route(advertised, bad)
        self.assertIsInstance(decision, routing.RouteError)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_a_blank_static_id_is_rejected(self) -> None:
        with self.assertRaises(vendors.RegistryError):
            vendors.validate_registry(self._acme(static_ids=("  ",)))

    def test_ordinary_static_ids_pass_validation(self) -> None:
        """The guard rejects the trap, not the feature."""
        vendors.validate_registry(self._acme(static_ids=("acme-1", "acme-2")))

    def test_shipped_rows_declare_static_ids_exactly_where_the_fallback_is_their_own(self) -> None:
        """A row on the STANDARD discovery path keeps the shipped snapshot as
        its fallback and declares nothing.

        `static_ids` exists so a NEW vendor can be added as a config row with
        its own fallback — and for the two shapes whose fallback MUST be the
        row's own list: a vendor with no model-list endpoint at all (whose
        `static_ids` ARE the catalog), and a vendor whose list endpoint lives
        on another base (`catalog_url`), where the declared list is what the
        keyless and fetch-failed moments serve. `validate_registry` enforces
        non-empty in both directions. A standard-path row that set
        `static_ids` would stop tracking `static_catalog.json`, and the
        snapshot-vs-seed consistency checks in test_model_router_catalog.py
        would need to cover the row's list too; this test is the tripwire
        for that.
        """
        for vendor_id, row in vendors.VENDORS.items():
            if row.catalog_path is None or row.catalog_url is not None:
                self.assertTrue(row.static_ids, vendor_id)
            else:
                self.assertEqual(row.static_ids, (), vendor_id)

    def test_a_no_discovery_row_with_no_static_ids_is_rejected(self) -> None:
        """A `catalog_path is None` row with no declared ids would show an
        empty picker for that vendor, forever — there is nothing to fetch and
        nothing to fall back to."""
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(self._acme(catalog_path=None))
        self.assertIn("static_ids", str(ctx.exception))

    def test_a_catalog_url_row_without_a_declared_fallback_is_rejected(self) -> None:
        """The override rides the live path, but the keyless moment and the
        failed fetch have nothing to serve unless the row declares its own
        list — the same empty-picker failure as the no-discovery case, one
        retry window later."""
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(
                self._acme(catalog_url="https://lists.acme.example/v1/models"),
            )
        self.assertIn("static_ids", str(ctx.exception))

    def test_a_catalog_url_must_be_an_absolute_http_url(self) -> None:
        """It REPLACES `upstream + catalog_path`; a relative path here would
        be fetched as a malformed URL and read as a vendor outage."""
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(
                self._acme(
                    catalog_url="/compatible/v1/models",
                    static_ids=("acme-1",),
                ),
            )
        self.assertIn("catalog_url", str(ctx.exception))

    def test_a_catalog_url_row_with_a_declared_fallback_passes(self) -> None:
        """The guard rejects the trap, not the feature — the shipped
        Token-Plan row is this shape."""
        vendors.validate_registry(
            self._acme(
                catalog_url="https://lists.acme.example/v1/models",
                static_ids=("acme-1",),
            ),
        )

    def test_a_blank_exclude_prefix_is_rejected(self) -> None:
        """Every id starts with the empty string, so a blank entry would
        exclude the ENTIRE live list and leave the family on its fallback
        forever — with a `live`-looking fetch succeeding each retry."""
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(
                self._acme(catalog_exclude_prefixes=("  ",)),
            )
        self.assertIn("catalog_exclude_prefixes", str(ctx.exception))

    def test_rows_that_do_not_override_discovery_exclude_nothing(self) -> None:
        """The defaults are inert: the shipped standard-path row neither
        overrides its list URL nor filters a single id its endpoint sends
        (its list honesty is the truth filter's business, not a prefix
        filter's)."""
        zai = vendors.VENDORS["zai"]
        self.assertIsNone(zai.catalog_url)
        self.assertEqual(zai.catalog_exclude_prefixes, ())

    def test_two_vendors_may_not_share_a_bare_prefix(self) -> None:
        """Bare prefixes resolve longest-first, so nesting is fine but an
        EXACT collision is not: whichever row the mapping happens to iterate
        wins the other's ids, silently. The two shipped rows prove the shape
        is real, not synthetic — one serves the other's family name among
        its model ids."""
        zai = vendors.VENDORS["zai"]
        other = self._acme(bare_id_prefixes=("GLM",))["acme"]
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry({"zai": zai, "acme": other})
        self.assertIn("bare_id_prefix", str(ctx.exception))

    def test_a_nested_bare_prefix_is_not_a_collision(self) -> None:
        """The legitimate sibling of the collision rule: a strictly longer
        prefix from another vendor wins only its own ids and defers the
        rest."""
        zai = vendors.VENDORS["zai"]
        other = self._acme(bare_id_prefixes=("glm-5.3",))["acme"]
        vendors.validate_registry({"zai": zai, "acme": other})
        claimed = routing.route("glm-5.3", {"zai": zai, "acme": other})
        assert isinstance(claimed, Route)
        self.assertEqual(claimed.family_id, "acme")
        deferred = routing.route("glm-4.6", {"zai": zai, "acme": other})
        assert isinstance(deferred, Route)
        self.assertEqual(deferred.family_id, "zai")

    def test_a_claude_marked_verified_id_is_rejected(self) -> None:
        """The truth filter KEEPS only listed ids, so a Claude-marked entry
        would be the one id of its family kept — a picker row the
        honest-naming guard then refuses. The filter would actively select
        for the un-routable id."""
        bad = self._acme(verified_ids=("claude-3-opus",))
        with self.assertRaises(vendors.RegistryError) as ctx:
            vendors.validate_registry(bad)
        self.assertIn("verified_id", str(ctx.exception))

    def test_a_blank_verified_id_is_rejected(self) -> None:
        with self.assertRaises(vendors.RegistryError):
            vendors.validate_registry(self._acme(verified_ids=(" ",)))


class RouteTests(unittest.TestCase):
    def test_namespaced_vendor_id_strips_the_prefix(self) -> None:
        decision = routing.route("claude-gw/glm-5.3")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, "glm-5.3")
        self.assertFalse(decision.is_anthropic)

    def test_one_m_suffix_is_stripped_for_a_vendor(self) -> None:
        decision = routing.route("claude-gw/glm-5.3[1m]")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, "glm-5.3")

    def test_one_m_suffix_is_stripped_for_the_first_party_route_too(self) -> None:
        """``[1m]`` is a CLIENT spelling on every route, not an upstream name.

        v0.2.93 forwarded it verbatim here on the theory that it named a real
        upstream variant; a live probe returned ``404 not_found_error`` for
        the suffixed id and 200 for the same id without it. The request for
        the large window survives in ``one_m_requested``, which the server
        turns into the beta header that actually buys it.
        """
        decision = routing.route("claude-sonnet-5[1m]")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, "claude-sonnet-5")
        self.assertTrue(decision.is_anthropic)
        self.assertTrue(decision.one_m_requested)

    def test_a_plain_first_party_id_does_not_claim_the_1m_variant(self) -> None:
        decision = routing.route("claude-sonnet-5")
        assert isinstance(decision, Route)
        self.assertFalse(decision.one_m_requested)

    def test_bare_vendor_id_routes_to_the_vendor(self) -> None:
        decision = routing.route("glm-4.6")
        assert isinstance(decision, Route)
        self.assertEqual(decision.family_id, "zai")
        self.assertEqual(decision.forward_model, "glm-4.6")

    def test_the_nested_namespace_routes_and_strips(self) -> None:
        """The Token-Plan vendor EXTENDS the shared namespace rather than
        minting a parallel one, and the longest-match rule resolves it — a
        picker row, a CLI spelling, and the panel's ``claude-gw/`` prefix
        rule all keep working."""
        decision = routing.route("claude-gw/qwen/qwen3.8-max")
        assert isinstance(decision, Route)
        self.assertEqual(decision.family_id, "qwen")
        self.assertEqual(decision.forward_model, "qwen3.8-max")
        self.assertFalse(decision.is_anthropic)
        self.assertEqual(
            decision.upstream, vendors.VENDORS["qwen"].upstream,
        )

    def test_the_shared_namespace_does_not_shadow_the_nested_one(self) -> None:
        """Both spellings of the shared family id work and land on the row
        the spelling names — the whole point of longest-match resolution."""
        nested = routing.route("claude-gw/qwen/glm-5.2")
        assert isinstance(nested, Route)
        self.assertEqual(nested.family_id, "qwen")
        flat = routing.route("claude-gw/glm-5.2")
        assert isinstance(flat, Route)
        self.assertEqual(flat.family_id, "zai")

    def test_the_second_vendor_bare_prefixes_route_to_it(self) -> None:
        """A user who types the vendor's real id on the CLI is not forced to
        spell the namespace — and the OTHER upstream's model ids route to
        this row too, because that endpoint serves them."""
        for model_id in ("qwen3.8-max", "deepseek-v4-pro"):
            with self.subTest(model=model_id):
                decision = routing.route(model_id)
                assert isinstance(decision, Route), model_id
                self.assertEqual(decision.family_id, "qwen")
                self.assertEqual(decision.forward_model, model_id)
        # The subscription vendor keeps its own prefix...
        decision = routing.route("glm-5.3")
        assert isinstance(decision, Route)
        self.assertEqual(decision.family_id, "zai")
        # ...and a date-suffixed id of the other upstream still resolves
        # through this row's prefix.
        long = routing.route("deepseek-v4-flash-0731")
        assert isinstance(long, Route)
        self.assertEqual(long.family_id, "qwen")

    def test_one_m_suffix_is_stripped_on_the_nested_route_too(self) -> None:
        decision = routing.route("claude-gw/qwen/qwen3.7-plus[1m]")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, "qwen3.7-plus")
        self.assertTrue(decision.one_m_requested)

    def test_a_claude_id_under_the_nested_namespace_is_refused(self) -> None:
        decision = routing.route("claude-gw/qwen/claude-sonnet-5")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_claude_id_goes_to_the_first_party_route(self) -> None:
        decision = routing.route("claude-opus-5")
        assert isinstance(decision, Route)
        self.assertTrue(decision.is_anthropic)
        self.assertEqual(decision.upstream, vendors.ANTHROPIC_FAMILY.upstream)

    def test_unknown_model_is_a_local_400(self) -> None:
        decision = routing.route("mistral-large")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.status, 400)
        self.assertEqual(decision.reason, "unknown_model")
        self.assertIn("claude-gw/", decision.message)

    def test_empty_model_is_a_local_400(self) -> None:
        for value in ("", "   ", None, 17):
            with self.subTest(value=value):
                decision = routing.route(value)  # type: ignore[arg-type]
                assert isinstance(decision, RouteError)
                self.assertEqual(decision.reason, "empty_model")

    def test_namespace_with_no_model_is_a_local_400(self) -> None:
        decision = routing.route("claude-gw/")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "empty_namespaced_id")

    def test_whitespace_is_tolerated(self) -> None:
        decision = routing.route("  claude-gw/glm-5.3  ")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, "glm-5.3")


class HonestNamingTests(unittest.TestCase):
    """The guard against silent model substitution."""

    def test_claude_id_under_a_vendor_namespace_is_refused(self) -> None:
        decision = routing.route("claude-gw/claude-sonnet-5")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.status, 400)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_the_refusal_explains_the_substitution_risk(self) -> None:
        decision = routing.route("claude-gw/claude-sonnet-5")
        assert isinstance(decision, RouteError)
        self.assertIn("OWN model", decision.message)

    def test_every_claude_marker_variant_is_refused(self) -> None:
        for model in (
            "claude-gw/claude-fable-5",
            "claude-gw/CLAUDE-opus-5",
            "claude-gw/anthropic-experimental",
            "claude-gw/my-claude-clone",
            "claude-gw/claude-sonnet-5[1m]",
        ):
            with self.subTest(model=model):
                decision = routing.route(model)
                assert isinstance(decision, RouteError), model
                self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_the_guard_applies_to_every_vendor_not_just_the_documented_one(self) -> None:
        """A vendor that has not documented an alias table can add one."""
        registry = _synthetic_registry()
        decision = routing.route("claude-acme/claude-sonnet-5", registry)
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_no_route_ever_sends_a_claude_marker_to_a_vendor(self) -> None:
        """Exhaustive sweep over the shapes a picker or a config can produce."""
        registry = _synthetic_registry()
        candidates = [
            "claude-sonnet-5", "claude-opus-5[1m]", "anthropic.foo",
            "glm-5.3", "acme-large", "claude-gw/glm-5.3",
            "claude-gw/claude-sonnet-5", "claude-acme/anthropic-x",
            "claude-gw/", "", "mistral-large", "claude-gw/GLM-5.3",
        ]
        for model in candidates:
            decision = routing.route(model, registry)
            if isinstance(decision, Route) and not decision.is_anthropic:
                with self.subTest(model=model):
                    self.assertFalse(
                        routing.has_claude_marker(decision.forward_model),
                        f"{model!r} would forward {decision.forward_model!r} to "
                        "a vendor upstream",
                    )


class SuffixTests(unittest.TestCase):
    def test_strip_1m_is_idempotent_and_precise(self) -> None:
        self.assertEqual(routing.strip_1m("glm-5.3[1m]"), "glm-5.3")
        self.assertEqual(routing.strip_1m("glm-5.3"), "glm-5.3")
        self.assertEqual(routing.strip_1m("glm-5.3[1m][1m]"), "glm-5.3[1m]")
        self.assertEqual(routing.strip_1m("glm[1m]-5.3"), "glm[1m]-5.3")

    def test_advertised_id_adds_the_suffix_only_when_told(self) -> None:
        zai = vendors.VENDORS["zai"]
        self.assertEqual(
            routing.advertised_id(zai, "glm-5.3", True), "claude-gw/glm-5.3[1m]",
        )
        self.assertEqual(
            routing.advertised_id(zai, "glm-5.1", False), "claude-gw/glm-5.1",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
