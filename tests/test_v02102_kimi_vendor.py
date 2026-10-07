# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 lane W1-K — the Kimi Code subscription as a gateway vendor.

Owner order 2026-10-07, narrowed the same day to K3 only. What is pinned:

* the registry row: namespace, upstream, the ``x-api-key`` auth shape, the
  secret names (``KIMI_API_KEY`` is the owner's keychain slot; the Moonshot
  pay-as-you-go name is never tried), the one offered id;
* routing: the endpoint is a WILDCARD ALIAS (live 2026-10-07: any name
  answers 200 and is echoed back), so ``forward_verified_only`` forwards
  ``k3`` alone and refuses every other id — namespaced, bare, inside the
  ``k3`` family, or present on the vendor's own live list;
* the wire: the vendor key goes out as ``x-api-key`` with no scheme, the
  host token never leaves, and the CLIENT's user-agent is relayed — on
  ``/v1/messages``, on the model-list fetch a client's ``/v1/models``
  triggers, and on the quota fetch a refresh signal schedules (the
  vendor's edge 403s library user-agents, live 2026-10-07);
* the usage windows: the subscription's own quota endpoint
  (``GET /coding/v1/usages``, live probe 2026-10-07 with the owner's key —
  body saved verbatim as ``tests/fixtures/model_router/kimi-usages.json``)
  yields a 5h window derived from ``limits[].detail`` used/limit (its
  ``usages.limit_5h.used_ratio`` DISAGREED with that row in the probe and
  is deliberately not read) and monthly windows from
  ``usages.*.used_ratio``; the fetch speaks the kimi quota dialect
  (``x-api-key`` + the relayed client UA) while Z.ai keeps its own;
* the catalog: the live list publishes only ``k3[1m]`` (everything else
  withheld and reported), the keyless fallback (the shipped snapshot,
  narrowed the same way) is ``k3``, an OpenAI-style
  ``context_length`` is read where no table row speaks, and the cited table
  row still wins;
* the context table: the k3 row cites the vendor docs at 1048576 with the
  ``[1m]`` companion (docs + live probe), and names the Plus-plan cap;
* effort: every value Claude Code sends is accepted, so nothing is rewritten.

The launcher half (the seed reaching the launcher's table and the
Preferences -> Chat model context page) is pinned in Rust:
``the_shipped_seed_parses_under_the_launchers_own_parser`` in
``launcher/src-tauri/src/commands/chat_model_context.rs``.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from model_router import catalog as cat
from model_router import routing
from model_router import usage_windows as uw
from model_router.catalog import SOURCE_LIVE, SOURCE_STATIC
from model_router.context_table import ContextTable, load_seed
from model_router.effort import EFFORT_VALUES, effort_policy, translate_effort
from model_router.routing import RouteError
from model_router.secrets import VendorKeyResolver
from model_router.usage_windows import UsageWindows, parse_kimi_usages
from model_router.vendors import (
    ANTHROPIC_FAMILY,
    GATEWAY_NAMESPACE,
    VENDORS,
    RegistryError,
    validate_registry,
)
from tests.test_model_router_server import (  # noqa: E402 — shared base
    FAKE_VENDOR_KEY,
    HOST_TOKEN,
    GatewayTestBase,
    _Upstream,
)
from tests.test_model_router_usage_windows import (  # noqa: E402 — shared fakes
    ZAI_PAYLOAD_2026,
    _FakeFetch,
)

KIMI = VENDORS["kimi"]

#: The vendor's whole live list as the coordinator's probe saw its ids
#: (2026-10-07), with the ``context_length`` field the list carries.
LIVE_LIST = {
    "data": [
        {"id": "k3", "display_name": "K3", "context_length": 1048576},
        {"id": "k3-256k", "display_name": "K3", "context_length": 262144},
        {"id": "kimi-for-coding", "display_name": "K2.8 Preview",
         "context_length": 1048576},
        {"id": "kimi-for-coding-highspeed", "display_name": "K2.7 Code HighSpeed",
         "context_length": 262144},
    ],
}

#: What Claude Code sends; the vendor's edge answers 200 to this shape.
CLIENT_UA = "claude-cli/2.1.2 (external, cli)"


class RegistryRowTests(unittest.TestCase):
    def test_the_registry_with_kimi_validates(self) -> None:
        validate_registry()

    def test_namespace_extends_the_gateway_namespace(self) -> None:
        self.assertEqual(KIMI.namespace, "claude-gw/kimi/")
        self.assertTrue(KIMI.namespace.startswith(GATEWAY_NAMESPACE))

    def test_upstream_is_the_international_anthropic_base(self) -> None:
        self.assertEqual(KIMI.upstream, "https://api.kimi.ai/coding")

    def test_auth_is_x_api_key_with_no_scheme(self) -> None:
        self.assertEqual(KIMI.auth_header, "x-api-key")
        self.assertEqual(KIMI.auth_scheme, "")

    def test_only_k3_is_offered(self) -> None:
        """Owner ruling 2026-10-07: "only need K3"."""
        self.assertEqual(KIMI.verified_ids, ("k3",))
        # Standard discovery: the fallback is the shipped snapshot.
        self.assertEqual(KIMI.static_ids, ())
        self.assertEqual(KIMI.bare_id_prefixes, ("k3",))

    def test_the_measured_wildcard_alias_is_declared(self) -> None:
        self.assertTrue(KIMI.alias_trap)
        self.assertTrue(KIMI.forward_verified_only)

    def test_a_verified_only_row_without_verified_ids_is_rejected(self) -> None:
        broken = replace(KIMI, verified_ids=())
        with self.assertRaises(RegistryError):
            validate_registry({"kimi": broken})

    def test_the_list_endpoint_is_the_live_path(self) -> None:
        self.assertEqual(KIMI.catalog_path, "/v1/models")
        self.assertIsNone(KIMI.catalog_url)

    def test_the_quota_url_is_the_probed_usages_endpoint(self) -> None:
        """Live probe 2026-10-07 with the owner's key: the endpoint answered
        200 to ``x-api-key`` + a client user-agent + anthropic-version; the
        body is the kimi-usages fixture."""
        self.assertEqual(KIMI.quota_url, "https://api.kimi.ai/coding/v1/usages")
        self.assertFalse(KIMI.partial_message_start_usage)


class RoutingTests(unittest.TestCase):
    def test_namespaced_ids_route_to_kimi_under_their_real_id(self) -> None:
        for model_id, bare in (
            ("claude-gw/kimi/k3", "k3"),
            ("claude-gw/kimi/k3[1m]", "k3"),
            ("k3", "k3"),
            ("k3[1m]", "k3"),
        ):
            with self.subTest(model=model_id):
                decision = routing.route(model_id)
                self.assertNotIsInstance(decision, RouteError, decision)
                assert not isinstance(decision, RouteError)
                self.assertEqual(decision.family_id, "kimi")
                self.assertEqual(decision.upstream, KIMI.upstream)
                self.assertEqual(decision.forward_model, bare)

    def test_every_unverified_id_is_refused_locally(self) -> None:
        """The endpoint would answer each of these 200 under the requested
        name, so forwarding one is a model the user did not choose. The
        vendor's OWN live list (``known_ids``) does not change that."""
        live = {"kimi": frozenset(
            {"k3", "k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed"},
        )}
        for model_id in (
            "claude-gw/kimi/k3-256k", "k3-256k", "k3-nonsense",
            "claude-gw/kimi/kimi-for-coding", "claude-gw/kimi/nonsense-model-xyz",
            "claude-gw/kimi/kimi-for-coding-highspeed[1m]",
        ):
            for known in (None, live):
                with self.subTest(model=model_id, known=bool(known)):
                    decision = routing.route(model_id, known_ids=known)
                    self.assertIsInstance(decision, RouteError, decision)
                    assert isinstance(decision, RouteError)
                    self.assertEqual(decision.reason, "unknown_model")
                    self.assertFalse(
                        any("kimi-for-coding" in s or "k3-256k" in s
                            for s in decision.suggestions),
                        decision.suggestions,
                    )

    def test_the_refusal_suggests_the_verified_id(self) -> None:
        decision = routing.route("claude-gw/kimi/k33")
        assert isinstance(decision, RouteError)
        self.assertIn("claude-gw/kimi/k3", decision.suggestions)

    def test_a_definition_naming_an_unverified_id_is_invalid(self) -> None:
        ok, _reason, _ = routing.validate_model_id("claude-gw/kimi/k3[1m]")
        self.assertTrue(ok)
        ok, _reason, _ = routing.validate_model_id("claude-gw/kimi/kimi-for-coding")
        self.assertFalse(ok)

    def test_a_claude_id_behind_the_namespace_is_refused(self) -> None:
        decision = routing.route("claude-gw/kimi/claude-opus-5")
        self.assertIsInstance(decision, RouteError)
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_the_other_vendors_still_route_as_before(self) -> None:
        zai = routing.route("claude-gw/glm-5.3")
        qwen = routing.route("claude-gw/qwen/qwen3.8-max")
        assert not isinstance(zai, RouteError)
        assert not isinstance(qwen, RouteError)
        self.assertEqual(zai.family_id, "zai")
        self.assertEqual(qwen.family_id, "qwen")


class SecretResolutionTests(unittest.TestCase):
    def test_the_owners_keychain_slot_resolves(self) -> None:
        """The launcher keychain holds ``KIMI_API_KEY`` (shared scope)."""
        seen: list[str] = []

        def getter(key, project=None):
            seen.append(key)
            if key != "KIMI_API_KEY":
                raise KeyError(key)
            return "kimi-key"

        result = VendorKeyResolver(getter=getter).resolve(KIMI)
        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.resolved_from, "KIMI_API_KEY")
        self.assertEqual(seen, ["kimi_api_key", "KIMI_API_KEY"])

    def test_the_canonical_name_short_circuits(self) -> None:
        seen: list[str] = []

        def getter(key, project=None):
            seen.append(key)
            return "kimi-key"

        result = VendorKeyResolver(getter=getter).resolve(KIMI)
        self.assertEqual(result.resolved_from, "kimi_api_key")
        self.assertEqual(seen, ["kimi_api_key"])

    def test_the_moonshot_platform_key_is_never_asked_for(self) -> None:
        """A pay-as-you-go platform key is a different key for a different
        base URL; resolving it would send it to the coding endpoint."""
        seen: list[str] = []

        def getter(key, project=None):
            seen.append(key)
            if key != "MOONSHOT_API_KEY":
                raise KeyError(key)
            return "platform-key"

        result = VendorKeyResolver(getter=getter).resolve(KIMI)
        self.assertEqual(result.state, "missing")
        self.assertNotIn("MOONSHOT_API_KEY", seen)
        self.assertIn("KIMI_API_KEY", result.problem or "")


class KimiWireTests(GatewayTestBase):
    """The Kimi row against a recording stub upstream."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.kimi_up = _Upstream()
        url = await self.kimi_up.start()
        self.addAsyncCleanup(self.kimi_up.stop)
        self.kimi = replace(KIMI, upstream=url)
        self.kclient = await self.make_client(vendors={
            self.vendor.vendor_id: self.vendor,
            self.kimi.vendor_id: self.kimi,
        })

    async def test_messages_carry_the_key_as_x_api_key(self) -> None:
        resp = await self.kclient.post(
            "/v1/messages",
            headers={**self.auth(), "User-Agent": CLIENT_UA},
            json={"model": "claude-gw/kimi/k3", "messages": [], "max_tokens": 8},
        )
        self.assertEqual(resp.status, 200)
        sent = self.kimi_up.message_requests[0]
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(headers.get("x-api-key"), FAKE_VENDOR_KEY)
        self.assertNotIn("authorization", headers)
        self.assertNotIn(HOST_TOKEN, json.dumps(sent["headers"]))
        self.assertEqual(json.loads(sent["body"])["model"], "k3")
        self.assertEqual(sent["path"], "/v1/messages")

    async def test_the_host_token_in_x_api_key_is_replaced_not_forwarded(self) -> None:
        await self.kclient.post(
            "/v1/messages",
            headers={"x-api-key": HOST_TOKEN, "User-Agent": CLIENT_UA},
            json={"model": "claude-gw/kimi/k3[1m]", "messages": []},
        )
        sent = self.kimi_up.message_requests[0]
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(headers.get("x-api-key"), FAKE_VENDOR_KEY)
        self.assertNotIn(HOST_TOKEN, json.dumps(sent["headers"]))

    async def test_an_unverified_id_never_reaches_the_upstream(self) -> None:
        self.kimi_up.models_payload = LIVE_LIST
        # The picker read fills the live cache with all four listed ids.
        await self.kclient.get(
            "/v1/models", headers={**self.auth(), "User-Agent": CLIENT_UA},
        )
        for model in ("claude-gw/kimi/k3-256k", "claude-gw/kimi/kimi-for-coding"):
            with self.subTest(model=model):
                resp = await self.kclient.post(
                    "/v1/messages",
                    headers={**self.auth(), "User-Agent": CLIENT_UA},
                    json={"model": model, "messages": []},
                )
                self.assertEqual(resp.status, 400)
        self.assertEqual(self.kimi_up.message_requests, [])

    async def test_messages_relay_the_clients_user_agent_verbatim(self) -> None:
        await self.kclient.post(
            "/v1/messages",
            headers={**self.auth(), "User-Agent": CLIENT_UA},
            json={"model": "claude-gw/kimi/k3", "messages": []},
        )
        sent = self.kimi_up.message_requests[0]
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(headers.get("user-agent"), CLIENT_UA)

    async def test_the_model_list_fetch_carries_the_clients_user_agent(self) -> None:
        """The vendor's edge answers its list only to a client user-agent;
        the gateway's fetch goes out as the client whose /v1/models asked."""
        self.kimi_up.models_payload = LIVE_LIST
        resp = await self.kclient.get(
            "/v1/models", headers={**self.auth(), "User-Agent": CLIENT_UA},
        )
        self.assertEqual(resp.status, 200)
        fetches = [r for r in self.kimi_up.requests if r["path"] == "/v1/models"]
        self.assertEqual(len(fetches), 1)
        headers = {k.lower(): v for k, v in fetches[0]["headers"].items()}
        self.assertEqual(headers.get("user-agent"), CLIENT_UA)
        self.assertEqual(headers.get("x-api-key"), FAKE_VENDOR_KEY)

    async def test_the_live_list_publishes_k3_only_and_reports_the_rest(self) -> None:
        self.kimi_up.models_payload = LIVE_LIST
        body = await (await self.kclient.get(
            "/v1/models", headers={**self.auth(), "User-Agent": CLIENT_UA},
        )).json()
        ids = {r["id"] for r in body["data"] if r["id"].startswith("claude-gw/kimi/")}
        # The 1M window publishes the [1m] spelling alone (one model, one row).
        self.assertEqual(ids, {"claude-gw/kimi/k3[1m]"})
        hidden = " ".join(body["_vct_catalog_hidden"])
        for withheld in ("k3-256k", "kimi-for-coding", "kimi-for-coding-highspeed"):
            self.assertIn(f"claude-gw/kimi/{withheld}", hidden)
        health = await (await self.kclient.get("/health")).json()
        self.assertIn("kimi", health["vendors"])
        # The launcher's Services card renders this map verbatim.
        self.assertEqual(health["catalog_source"]["kimi"], SOURCE_LIVE)

    async def test_without_a_key_the_snapshot_serves_k3_alone(self) -> None:
        client = await self.make_client(
            vendors={self.kimi.vendor_id: self.kimi},
            key_getter=lambda key, project=None: None,
        )
        body = await (await client.get("/v1/models", headers=self.auth())).json()
        ids = {r["id"] for r in body["data"] if r["id"].startswith("claude-gw/kimi/")}
        self.assertEqual(ids, {"claude-gw/kimi/k3[1m]"})
        self.assertEqual(self.kimi_up.requests, [], "no key, no fetch")
        health = await (await client.get("/health")).json()
        self.assertEqual(health["catalog_source"]["kimi"], SOURCE_STATIC)


class CatalogWindowTests(unittest.TestCase):
    def _union(self, table: ContextTable, payload: dict, user_agent=None):
        seen: list[tuple[str, dict]] = []

        async def fetch(url, headers):
            seen.append((url, dict(headers)))
            return payload if url.startswith(KIMI.upstream) else None

        async def vendor_key(_vendor):
            return "k"

        service = cat.CatalogService(
            vendors={"kimi": KIMI},
            anthropic=ANTHROPIC_FAMILY,
            fetch_json=fetch,
            oauth_token=lambda: None,
            vendor_key=vendor_key,
            live_ttl_s=3600,
            static_ttl_s=60,
            clock=lambda: 1.0,
        )
        return asyncio.run(service.union(table=table, user_agent=user_agent)), seen

    def test_context_length_is_read_where_no_table_row_speaks(self) -> None:
        entries = cat.CatalogService._parse_models(
            {"data": [{"id": "m", "context_length": 1048576}]},
        )
        self.assertEqual(entries[0].max_input_tokens, 1048576)

    def test_max_input_tokens_still_wins_over_context_length(self) -> None:
        entries = cat.CatalogService._parse_models(
            {"data": [{"id": "m", "max_input_tokens": 5, "context_length": 9}]},
        )
        self.assertEqual(entries[0].max_input_tokens, 5)

    def test_with_no_table_row_the_live_window_publishes_the_1m_companion(self) -> None:
        """``context_length`` alone (no seed row) yields the 1M advert."""
        empty = ContextTable(rows={}, source="test-empty", path=None)
        union, _ = self._union(empty, LIVE_LIST)
        ids = {e.id for e in union.entries if e.id.startswith(KIMI.namespace)}
        self.assertEqual(ids, {"claude-gw/kimi/k3[1m]"})

    def test_the_cited_seed_row_wins_over_the_live_window(self) -> None:
        """A live figure that disagrees with the cited row does not move it:
        the table is the window authority (``resolve_window``)."""
        smaller = {"data": [{"id": "k3", "context_length": 262144}]}
        union, _ = self._union(load_seed(), smaller)
        ids = {e.id for e in union.entries if e.id.startswith(KIMI.namespace)}
        self.assertEqual(ids, {"claude-gw/kimi/k3[1m]"})
        empty = ContextTable(rows={}, source="test-empty", path=None)
        union, _ = self._union(empty, smaller)
        ids = {e.id for e in union.entries if e.id.startswith(KIMI.namespace)}
        self.assertEqual(ids, {"claude-gw/kimi/k3"}, "control: no row, live wins")

    def test_no_user_agent_sends_no_header(self) -> None:
        _, seen = self._union(load_seed(), LIVE_LIST)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("User-Agent", seen[0][1])

    def test_a_user_agent_is_relayed_onto_the_fetch(self) -> None:
        _, seen = self._union(load_seed(), LIVE_LIST, user_agent=CLIENT_UA)
        self.assertEqual(seen[0][1].get("User-Agent"), CLIENT_UA)


class SeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seed = load_seed()

    def test_the_k3_row_is_1m_with_the_companion(self) -> None:
        row = self.seed.lookup("k3", "kimi")
        assert row is not None
        self.assertEqual(row.vendor, "kimi")
        self.assertEqual(row.context_window, 1048576)
        self.assertTrue(row.window_1m, "window_1m must agree with the window")
        self.assertTrue(self.seed.advertise_1m("k3"))
        self.assertFalse(row.text_only)
        self.assertTrue(
            row.source.startswith("https://www.kimi.com/code/docs/"), row.source,
        )

    def test_the_k3_note_names_the_plus_cap_and_how_to_lower_it(self) -> None:
        """A Plus subscriber must be able to read, in the row, that their
        plan caps k3 at 256K and where to set that."""
        note = self.seed.rows["k3"].source_note
        self.assertIn("262144", note)
        self.assertIn("Chat model context", note)
        self.assertIn("401", note)
        self.assertIn("live /v1/models probe 2026-10-07", note)

    def test_no_other_kimi_row_ships(self) -> None:
        kimi_rows = {i for i, r in self.seed.rows.items() if r.vendor == "kimi"}
        self.assertEqual(kimi_rows, {"k3"})


class EffortTests(unittest.TestCase):
    def test_every_client_effort_is_forwarded_untouched(self) -> None:
        for model_id in ("k3",):
            policy = effort_policy("kimi", model_id)
            assert policy is not None
            self.assertEqual(policy.accepted, frozenset(EFFORT_VALUES))
            self.assertEqual(dict(policy.translate), {})
            self.assertEqual(dict(policy.collapses), {"medium": "high", "xhigh": "max"})
            for effort in EFFORT_VALUES:
                with self.subTest(model=model_id, effort=effort):
                    payload = {"output_config": {"effort": effort}}
                    out, changed = translate_effort(payload, "kimi", model_id)
                    self.assertFalse(changed)
                    self.assertIs(out, payload)


# ── W1-K3: the usage windows ─────────────────────────────────────────────
#: The probe body, verbatim. LIVE 2026-10-07, coordinator probe with the
#: owner's key: ``GET https://api.kimi.ai/coding/v1/usages``, headers
#: ``x-api-key`` (raw), ``user-agent: claude-cli/...`` (the edge 403s
#: library user-agents with "error code: 1010") and
#: ``anthropic-version: 2023-06-01`` -> HTTP 200 with this JSON.
KIMI_USAGES = json.loads(
    (Path(__file__).parent / "fixtures" / "model_router" / "kimi-usages.json")
    .read_text(encoding="utf-8"),
)

#: Before the probe's own 5h reset, so a parsed reading is not yet
#: "past its reset time".
KIMI_NOW = datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc).timestamp()


class KimiUsageParsingTests(unittest.TestCase):
    def _by_id(self, payload) -> dict:
        _plan, windows = parse_kimi_usages(payload, now=KIMI_NOW)
        return {w.id: w for w in windows}

    def test_the_probed_body_parses(self) -> None:
        # Order and labels: 5h first and spelt like Z.ai's, the two monthly
        # windows after it, so every existing renderer treats them alike.
        _plan, ordered = parse_kimi_usages(KIMI_USAGES, now=KIMI_NOW)
        self.assertEqual(
            [(w.id, w.label) for w in ordered],
            [("5h", "5h"), ("monthly", "mo"), ("monthly_code", "mo-code")],
        )
        windows = self._by_id(KIMI_USAGES)
        # The 5h percent comes from limits[].detail used/limit — STRINGS.
        self.assertEqual(windows["5h"].percent, 9.0)
        self.assertEqual(
            windows["5h"].resets_at,
            datetime(2026, 10, 7, 5, 13, 9, 844464, tzinfo=timezone.utc).timestamp(),
        )
        # The monthly windows come from usages.*.used_ratio (a fraction).
        self.assertEqual(windows["monthly"].percent, 0.29)
        self.assertEqual(
            windows["monthly"].resets_at,
            datetime(2026, 11, 8, tzinfo=timezone.utc).timestamp(),
        )
        # A GENUINE zero reading, not a missing one: used_ratio 0 is present
        # and parses, so it is 0.0 — unknown is reserved for absence.
        self.assertEqual(windows["monthly_code"].percent, 0.0)
        self.assertEqual(windows["5h"].source, "kimi_usages")

    def test_the_disagreeing_5h_ratio_is_deliberately_not_read(self) -> None:
        """In the probe usages.limit_5h.used_ratio (0) disagreed with
        limits[0].detail (9/100 used). The detail row wins; a payload whose
        ratio says something else must not move the 5h percent."""
        payload = json.loads(json.dumps(KIMI_USAGES))
        payload["usages"]["limit_5h"]["used_ratio"] = 0.75
        self.assertEqual(self._by_id(payload)["5h"].percent, 9.0)

    def test_malformed_bodies_are_unknown_never_raised(self) -> None:
        for payload in (
            None, "nonsense", 7, {},
            {"limits": "not-a-list", "usages": 4},
            # The right window shape but unparseable numbers.
            {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                         "detail": {"used": "nine", "limit": "100",
                                    "resetTime": "2026-10-07T05:13:09.844464Z"}}]},
            # A zero limit cannot yield a percent.
            {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                         "detail": {"used": "9", "limit": "0"}}]},
            # A different window (duration 60, or another time unit) is not
            # the 5h one.
            {"limits": [{"window": {"duration": 60, "timeUnit": "TIME_UNIT_MINUTE"},
                         "detail": {"used": "1", "limit": "2"}}]},
            {"limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_HOUR"},
                         "detail": {"used": "1", "limit": "2"}}]},
            {"usages": {"limit_month_total": {"used_ratio": "half"}},
             "limits": [{"window": None, "detail": None}]},
            {"usages": {"limit_month_total": "not-a-block"}},
            {"usages": {"limit_month_total": {"used_ratio": -0.5}}},
        ):
            with self.subTest(payload=payload):
                _plan, windows = parse_kimi_usages(payload, now=KIMI_NOW)
                self.assertEqual([w.percent for w in windows], [None, None, None])
                self.assertEqual({w.unknown_reason for w in windows},
                                 {uw.UNKNOWN_NOT_REPORTED})


class KimiQuotaDialectTests(unittest.IsolatedAsyncioTestCase):
    """The quota FETCH: x-api-key + the relayed client UA, Z.ai untouched."""

    def _make(self, vendors, fetch) -> UsageWindows:
        async def vendor_key(_vendor):
            return "vendor-key"

        return UsageWindows(
            anthropic_upstream="https://anthropic.example/",
            vendors=vendors,
            oauth_token=lambda: None,
            vendor_key=vendor_key,
            fetch=fetch,
            ledger_path=lambda: None,
            clock=lambda: KIMI_NOW,
            rng=lambda: 0.5,
        )

    async def _run(self, service, user_agent: Optional[str] = CLIENT_UA):
        self.assertTrue(service.note_activity(user_agent))
        while service.refreshing:
            await asyncio.sleep(0)
        return service.snapshot()

    @staticmethod
    def _vendor(snap, vendor_id) -> dict:
        return next(v for v in snap["vendors"] if v["id"] == vendor_id)

    async def test_the_quota_fetch_sends_x_api_key_and_the_clients_ua(self) -> None:
        quota_url = KIMI.quota_url
        assert quota_url is not None  # noqa: S101 — pinned by RegistryRowTests
        fetch = _FakeFetch({quota_url: (200, KIMI_USAGES)})
        snap = await self._run(self._make({"kimi": KIMI}, fetch))
        self.assertEqual(len(fetch.calls), 1)  # no anthropic fetch: no token
        url, headers = fetch.calls[0]
        self.assertEqual(url, quota_url)
        self.assertEqual(headers.get("x-api-key"), "vendor-key")
        self.assertEqual(headers.get("User-Agent"), CLIENT_UA)
        self.assertIn("anthropic-version", headers)
        self.assertNotIn("Authorization", headers)
        kimi = self._vendor(snap, "kimi")
        self.assertEqual(kimi["state"], "ok")
        by_id = {w["id"]: w for w in kimi["windows"]}
        self.assertEqual(by_id["5h"]["percent"], 9.0)
        self.assertEqual(by_id["monthly"]["percent"], 0.29)
        # A quota vendor gets windows, not the ledger-token fallback.
        self.assertNotIn("tokens", kimi)

    async def test_without_a_client_ua_none_is_invented(self) -> None:
        """The vendor's terms forbid passing the gateway off as a client.
        With no client UA on record the fetch goes without one; the edge's
        403 to a library UA reads as an honest unknown, never a guess."""
        quota_url = KIMI.quota_url
        assert quota_url is not None  # noqa: S101 — pinned by RegistryRowTests
        fetch = _FakeFetch({quota_url: (403, None)})
        snap = await self._run(self._make({"kimi": KIMI}, fetch), user_agent=None)
        headers = fetch.calls[0][1]
        self.assertNotIn("User-Agent", headers)
        kimi = self._vendor(snap, "kimi")
        self.assertEqual(kimi["state"], "unknown")
        self.assertIn("HTTP 403", kimi["problem"])
        self.assertEqual(kimi["windows"], [])

    async def test_zai_still_speaks_its_own_dialect(self) -> None:
        """Leave-alone: the zai row keeps the RAW Authorization key, no
        scheme, no relayed user-agent, and the Z.ai parser."""
        zai = replace(VENDORS["zai"], quota_url="https://quota.example/limit")
        fetch = _FakeFetch({"https://quota.example/limit": (200, ZAI_PAYLOAD_2026)})
        snap = await self._run(self._make({"zai": zai}, fetch))
        self.assertEqual(
            fetch.calls[0][1],
            {"Authorization": "vendor-key", "Accept-Language": "en-US,en"},
        )
        zai_snap = self._vendor(snap, "zai")
        self.assertEqual(zai_snap["state"], "ok")
        self.assertEqual(zai_snap["plan"], "pro")
        by_id = {w["id"]: w for w in zai_snap["windows"]}
        # The reading PARSED through the Z.ai parser (its source and the
        # plan only that parser yields). The fixture's resets lie in
        # September 2026, before this class's fixed October clock, so the
        # snapshot ages both to unknown — reset_passed, the module's rule,
        # not a parse failure.
        self.assertEqual(by_id["5h"]["source"], "zai_quota")
        self.assertEqual(by_id["5h"]["unknown_reason"], "reset_passed")
        self.assertEqual(by_id["weekly"]["unknown_reason"], "reset_passed")


if __name__ == "__main__":
    unittest.main()
