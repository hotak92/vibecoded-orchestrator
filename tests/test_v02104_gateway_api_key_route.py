# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the Claude API-key route and the opt-in subscription fallback.

Two behaviours of the model gateway:

* **``claude-gw/api/<claude id>``** — the first-party models billed to a
  Claude Console API key (shared vct-secrets slot ``ANTHROPIC_API_KEY_GATEWAY``). Same
  upstream and same first-party handling as the OAuth route; the ONLY change
  on the wire is the auth swap: no ``Authorization``, the key as
  ``x-api-key``, the OAuth beta dropped from ``anthropic-beta`` (every other
  beta kept). The client's identity headers (``user-agent``, ``x-app``, …)
  travel untouched. Picker rows exist only while the key resolves.
* **The subscription -> API-key fallback** — OFF by default (it spends
  money; ``model_gateway.api_key_fallback`` in the launcher DB turns it on). A
  first-party OAuth request of ANY slot whose answer qualifies under the
  existing OD-9 quota trigger is retried ONCE on the same model with the API
  key, before the haiku -> glm rung; an API-key refusal walks on to that rung.

Every request served with the key is visible: ledger ``auth: "api_key"``
(``fallback_from`` set on a fallback), route label ``anthropic-api``, and one
INFO line per fallback activation that never carries the key.

No network: the upstreams are the loopback stubs of
:mod:`tests.test_model_router_server`. Every key below is an obvious fake.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import sqlite3
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from model_router import api_key_route as akr
from model_router import routing
from model_router import server as srv
from model_router.config import (
    ANTHROPIC_AUTH_API_KEY,
    ANTHROPIC_AUTH_APP_STATE_KEY,
    ANTHROPIC_AUTH_MODES,
    ANTHROPIC_AUTH_OAUTH,
    API_KEY_FALLBACK_APP_STATE_KEY,
    anthropic_auth_mode,
    api_key_fallback_enabled,
)
from model_router.routing import (
    AUTH_API_KEY,
    AUTH_OAUTH,
    AUTH_VENDOR_KEY,
    Route,
    RouteError,
    api_key_fallback_route,
)
from model_router.usage import UsageLedger, build_record
from model_router.vendors import (
    ANTHROPIC_API_KEY_ROUTE,
    ANTHROPIC_FAMILY,
    OAUTH_BETA,
    VENDORS,
    RegistryError,
    validate_registry,
)
from tests.test_model_router_server import (
    FAKE_OAUTH,
    FAKE_VENDOR_KEY,
    GatewayTestBase,
    _Upstream,
)
from tests.test_v02102_gateway_routing import (
    EXHAUSTED,
    RATE_LIMITED,
    _SeqUpstream,
    _success,
)

LOGGER = "model_router.server"
REPO = Path(__file__).resolve().parents[1]

#: Obviously fake — never a real key shape.
FAKE_API_KEY = "test-not-a-real-console-key"  # vco-secret-scan: test-placeholder
API_NS = ANTHROPIC_API_KEY_ROUTE.namespace
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
CLIENT_UA = "claude-cli/9.9.9 (external, cli)"


def _header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup on a recorded request."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


# ══════════════════════════════════════════════════════════════════════════
# Pure routing
# ══════════════════════════════════════════════════════════════════════════
class ApiKeyRouteSelectionTests(unittest.TestCase):

    def test_the_namespace_selects_the_first_party_api_key_route(self) -> None:
        decision = routing.route(f"{API_NS}{OPUS}")
        assert isinstance(decision, Route)
        self.assertTrue(decision.is_anthropic)
        self.assertIsNone(decision.vendor)
        self.assertIs(decision.api_key, ANTHROPIC_API_KEY_ROUTE)
        self.assertEqual(decision.forward_model, OPUS)
        self.assertEqual(decision.upstream, ANTHROPIC_FAMILY.upstream)
        self.assertEqual(decision.auth, AUTH_API_KEY)

    def test_the_1m_suffix_is_stripped_and_remembered(self) -> None:
        decision = routing.route(f"{API_NS}{OPUS}[1m]")
        assert isinstance(decision, Route)
        self.assertEqual(decision.forward_model, OPUS)
        self.assertTrue(decision.one_m_requested)
        self.assertEqual(decision.auth, AUTH_API_KEY)

    def test_the_api_namespace_wins_over_the_shorter_vendor_namespace(self) -> None:
        # claude-gw/ is the zai namespace; without the longest-match rule the
        # id would be refused there as a Claude id sent to a vendor.
        self.assertTrue(API_NS.startswith(VENDORS["zai"].namespace))
        decision = routing.route(f"{API_NS}{HAIKU}")
        self.assertIsInstance(decision, Route)

    def test_a_non_claude_id_under_the_api_namespace_is_refused(self) -> None:
        decision = routing.route(f"{API_NS}glm-5.3")
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "non_claude_id_to_api_key")
        self.assertEqual(decision.status, 400)

    def test_an_empty_remainder_is_refused(self) -> None:
        decision = routing.route(API_NS)
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "empty_namespaced_id")

    def test_the_other_routes_keep_their_auth_kind(self) -> None:
        oauth = routing.route(OPUS)
        vendor = routing.route("claude-gw/glm-5.3")
        assert isinstance(oauth, Route) and isinstance(vendor, Route)
        self.assertEqual(oauth.auth, AUTH_OAUTH)
        self.assertIsNone(oauth.api_key)
        self.assertEqual(vendor.auth, AUTH_VENDOR_KEY)

    def test_a_family_without_the_route_offers_nothing(self) -> None:
        bare = dataclasses.replace(ANTHROPIC_FAMILY, api_key=None)
        decision = routing.route(f"{API_NS}{OPUS}", anthropic=bare)
        # Back to the pre-v0.2.104 behaviour: the shared vendor namespace
        # refuses a Claude id.
        assert isinstance(decision, RouteError)
        self.assertEqual(decision.reason, "claude_id_to_vendor")

    def test_the_fallback_route_is_the_same_model_with_the_key(self) -> None:
        oauth = routing.route(OPUS)
        assert isinstance(oauth, Route)
        fallback = api_key_fallback_route(oauth)
        assert fallback is not None
        self.assertEqual(fallback.forward_model, OPUS)
        self.assertEqual(fallback.upstream, oauth.upstream)
        self.assertEqual(fallback.auth, AUTH_API_KEY)

    def test_the_fallback_route_is_offered_only_from_oauth(self) -> None:
        api = routing.route(f"{API_NS}{OPUS}")
        vendor = routing.route("claude-gw/glm-5.3")
        assert isinstance(api, Route) and isinstance(vendor, Route)
        self.assertIsNone(api_key_fallback_route(api))
        self.assertIsNone(api_key_fallback_route(vendor))
        oauth = routing.route(OPUS)
        assert isinstance(oauth, Route)
        self.assertIsNone(api_key_fallback_route(
            oauth, dataclasses.replace(ANTHROPIC_FAMILY, api_key=None),
        ))


class ApiKeyRouteRegistryTests(unittest.TestCase):

    def test_the_shipped_registry_validates(self) -> None:
        validate_registry()

    def test_the_secret_name_is_the_owners_gateway_slot_only(self) -> None:
        # The owner's name, and ONLY it: a project's own ANTHROPIC_API_KEY /
        # anthropic_api_key must never be spent by the gateway.
        self.assertEqual(ANTHROPIC_API_KEY_ROUTE.secret_keys, ("ANTHROPIC_API_KEY_GATEWAY",))

    def test_a_namespace_equal_to_a_vendor_namespace_is_refused(self) -> None:
        clash = dataclasses.replace(
            ANTHROPIC_FAMILY,
            api_key=dataclasses.replace(
                ANTHROPIC_API_KEY_ROUTE, namespace=VENDORS["qwen"].namespace,
            ),
        )
        with self.assertRaises(RegistryError):
            validate_registry(VENDORS, clash)

    def test_a_namespace_discovery_would_drop_is_refused(self) -> None:
        bad = dataclasses.replace(
            ANTHROPIC_FAMILY,
            api_key=dataclasses.replace(ANTHROPIC_API_KEY_ROUTE, namespace="gw/api/"),
        )
        with self.assertRaises(RegistryError):
            validate_registry(VENDORS, bad)

    def test_a_credential_id_shared_with_a_vendor_is_refused(self) -> None:
        bad = dataclasses.replace(
            ANTHROPIC_FAMILY,
            api_key=dataclasses.replace(ANTHROPIC_API_KEY_ROUTE, vendor_id="zai"),
        )
        with self.assertRaises(RegistryError):
            validate_registry(VENDORS, bad)


# ══════════════════════════════════════════════════════════════════════════
# The header swap (the ONE home: server._api_key_headers)
# ══════════════════════════════════════════════════════════════════════════
class HeaderSwapTests(unittest.TestCase):

    CLIENT = {
        "Authorization": "Bearer local-host-token",
        "user-agent": CLIENT_UA,
        "x-app": "cli",
        "x-claude-code-session-id": "sess-1",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": f"claude-code-20250219, {OAUTH_BETA},context-1m-2025-08-07",
    }

    def test_auth_is_swapped_and_nothing_else_is_touched(self) -> None:
        out = akr.api_key_headers(
            self.CLIENT, ANTHROPIC_API_KEY_ROUTE, FAKE_API_KEY,
            one_m_requested=False,
        )
        self.assertIsNone(_header(out, "authorization"))
        self.assertEqual(_header(out, "x-api-key"), FAKE_API_KEY)
        betas = [b.strip() for b in (_header(out, "anthropic-beta") or "").split(",")]
        self.assertNotIn(OAUTH_BETA, betas)
        self.assertIn("claude-code-20250219", betas)
        self.assertIn("context-1m-2025-08-07", betas)
        # Client identity: byte-identical.
        for name in ("user-agent", "x-app", "x-claude-code-session-id",
                     "anthropic-version"):
            self.assertEqual(_header(out, name), self.CLIENT[name], name)

    def test_a_client_x_api_key_is_replaced_never_forwarded(self) -> None:
        out = akr.api_key_headers(
            {"X-Api-Key": "local-host-token"}, ANTHROPIC_API_KEY_ROUTE,
            FAKE_API_KEY, one_m_requested=False,
        )
        self.assertEqual(
            [v for k, v in out.items() if k.lower() == "x-api-key"], [FAKE_API_KEY],
        )

    def test_the_1m_beta_is_added_when_the_suffix_asked_for_it(self) -> None:
        out = akr.api_key_headers(
            {"anthropic-beta": OAUTH_BETA}, ANTHROPIC_API_KEY_ROUTE,
            FAKE_API_KEY, one_m_requested=True,
        )
        self.assertEqual(_header(out, "anthropic-beta"), srv.CONTEXT_1M_BETA)

    def test_no_beta_header_when_nothing_is_left(self) -> None:
        out = akr.api_key_headers(
            {"anthropic-beta": OAUTH_BETA}, ANTHROPIC_API_KEY_ROUTE,
            FAKE_API_KEY, one_m_requested=False,
        )
        self.assertIsNone(_header(out, "anthropic-beta"))


# ══════════════════════════════════════════════════════════════════════════
# The setting (launcher DB app_state) — default OFF
# ══════════════════════════════════════════════════════════════════════════
def _launcher_db(
    path: Path, value: str | None, key: str = API_KEY_FALLBACK_APP_STATE_KEY,
) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER)",
    )
    if value is not None:
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, 0)",
            (key, value),
        )
    conn.commit()
    conn.close()
    return path


class FallbackSettingTests(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="apigw-setting-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _read(self, value: str | None) -> bool:
        return api_key_fallback_enabled(
            db_path=_launcher_db(self.root / f"l-{value}.db", value),
        )

    def test_on_only_when_the_row_says_so(self) -> None:
        self.assertTrue(self._read("true"))
        self.assertTrue(self._read("1"))

    def test_off_by_default_and_on_anything_else(self) -> None:
        self.assertFalse(self._read(None))
        self.assertFalse(self._read("false"))
        self.assertFalse(self._read("0"))
        self.assertFalse(self._read("yes please"))

    def test_off_without_a_launcher_db(self) -> None:
        with mock.patch.dict(
            os.environ, {"VCT_LAUNCHER_DB_PATH": str(self.root / "missing.db")},
        ):
            self.assertFalse(api_key_fallback_enabled(db_path=self.root / "missing.db"))

    def test_the_launcher_writes_the_key_the_gateway_reads(self) -> None:
        """Parity: the GUI's constant and the gateway's key are one string."""
        loaders = (REPO / "launcher/src/lib/preferences/loaders.ts").read_text(
            encoding="utf-8",
        )
        match = re.search(
            r"export const APP_STATE_KEY_GATEWAY_API_KEY_FALLBACK = '([^']+)'",
            loaders,
        )
        assert match is not None, "the launcher constant is missing"
        self.assertEqual(match.group(1), API_KEY_FALLBACK_APP_STATE_KEY)

    def test_the_secrets_panel_suggests_the_name_the_gateway_resolves(self) -> None:
        panel = (REPO / "launcher/src/lib/components/SecretsPanel.svelte").read_text(
            encoding="utf-8",
        )
        self.assertIn(
            f"newKey = '{ANTHROPIC_API_KEY_ROUTE.secret_keys[0]}'", panel,
        )


# ══════════════════════════════════════════════════════════════════════════
# Ledger
# ══════════════════════════════════════════════════════════════════════════
class LedgerAuthFieldTests(unittest.TestCase):

    def test_the_row_carries_the_auth_kind(self) -> None:
        record = build_record(
            session="s", agent=None, parent_agent=None, requested=OPUS,
            route=srv.API_KEY_ROUTE_LABEL, forward=OPUS, stream=False,
            status=200, totals={"input_tokens": 1, "output_tokens": 1},
            usage_complete=True, window_actual=None, window_source="unknown",
            auth=AUTH_API_KEY,
        )
        self.assertEqual(json.loads(record.to_json())["auth"], "api_key")

    def test_agent_rows_carry_it_for_the_launcher_card(self) -> None:
        ledger = UsageLedger()
        record = build_record(
            session="s", agent=None, parent_agent=None, requested=OPUS,
            route="anthropic", forward=OPUS, stream=False, status=200,
            totals={"input_tokens": 1}, usage_complete=True,
            window_actual=None, window_source="unknown", auth=AUTH_API_KEY,
        )
        ledger._remember(record)
        rows = ledger.agent_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auth"], "api_key")


# ══════════════════════════════════════════════════════════════════════════
# End to end through the gateway
# ══════════════════════════════════════════════════════════════════════════
class ApiKeyGatewayBase(GatewayTestBase):
    """A gateway whose first-party family offers the API-key route, with a
    SCRIPTED first-party upstream (subscription answer, then API answer)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.first = _SeqUpstream()
        first_url = await self.first.start()
        self.addAsyncCleanup(self.first.stop)
        self.anthropic = dataclasses.replace(
            self.anthropic, upstream=first_url, api_key=ANTHROPIC_API_KEY_ROUTE,
        )
        self.qwen_up = _Upstream()
        qwen_url = await self.qwen_up.start()
        self.addAsyncCleanup(self.qwen_up.stop)
        assert self.vendor_up.server is not None
        self.zai = dataclasses.replace(
            VENDORS["zai"],
            upstream=str(self.vendor_up.server.make_url("")).rstrip("/"),
        )
        self.qwen = dataclasses.replace(VENDORS["qwen"], upstream=qwen_url)
        self.client = await self.client_with(key=True, fallback=False)

    async def client_with(self, *, key: bool, fallback: bool | None) -> Any:
        """``fallback=None`` keeps the PRODUCTION setting reader."""
        def getter(name: str, project: Any = None) -> str:
            if name == "ANTHROPIC_API_KEY_GATEWAY":
                return FAKE_API_KEY if key else ""
            return FAKE_VENDOR_KEY

        client = await self.make_client(
            vendors={"zai": self.zai, "qwen": self.qwen}, key_getter=getter,
        )
        if fallback is not None:
            setting = client.app[srv.APP_KEY].api_key_fallback
            setting.reader = lambda: fallback
            await setting.refresh()
        return client

    def headers(self) -> dict[str, str]:
        return {
            **self.auth(),
            "x-claude-code-session-id": "chat-apikey",
            "user-agent": CLIENT_UA,
            "x-app": "cli",
            "anthropic-beta": "claude-code-20250219",
        }

    def ask(self, model: str) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }

    async def rows(self, client: Any) -> list[dict]:
        await client.app[srv.APP_KEY].quiesce()
        path = self.usage_ledger_path
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]

    def script(self, *entries: tuple[int, bytes]) -> None:
        self.first.script = [
            (status, {}, raw) for status, raw in entries
        ]


class ExplicitApiKeyRouteTests(ApiKeyGatewayBase):

    async def test_the_request_carries_the_key_and_the_clients_identity(self) -> None:
        self.script((200, json.dumps(_success(OPUS)).encode()))
        resp = await self.client.post(
            "/v1/messages", json=self.ask(f"{API_NS}{OPUS}"), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.first.message_requests), 1)
        sent = self.first.message_requests[0]
        h = sent["headers"]
        self.assertEqual(_header(h, "x-api-key"), FAKE_API_KEY)
        self.assertIsNone(_header(h, "authorization"))
        self.assertNotIn(OAUTH_BETA, _header(h, "anthropic-beta") or "")
        self.assertIn("claude-code-20250219", _header(h, "anthropic-beta") or "")
        self.assertEqual(_header(h, "user-agent"), CLIENT_UA)
        self.assertEqual(_header(h, "x-app"), "cli")
        self.assertEqual(json.loads(sent["body"])["model"], OPUS)
        rows = await self.rows(self.client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auth"], "api_key")
        self.assertEqual(rows[0]["route"], srv.API_KEY_ROUTE_LABEL)
        self.assertEqual(rows[0]["fallback_from"], "")

    async def test_no_key_is_a_local_503_and_no_upstream_call(self) -> None:
        client = await self.client_with(key=False, fallback=False)
        resp = await client.post(
            "/v1/messages", json=self.ask(f"{API_NS}{OPUS}"), headers=self.headers(),
        )
        self.assertEqual(resp.status, 503)
        self.assertEqual(self.first.message_requests, [])

    async def test_the_oauth_route_is_unchanged_and_ledgered_as_oauth(self) -> None:
        self.script((200, json.dumps(_success(OPUS)).encode()))
        resp = await self.client.post(
            "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        h = self.first.message_requests[0]["headers"]
        self.assertEqual(_header(h, "authorization"), f"Bearer {FAKE_OAUTH}")
        self.assertIsNone(_header(h, "x-api-key"))
        self.assertIn(OAUTH_BETA, _header(h, "anthropic-beta") or "")
        rows = await self.rows(self.client)
        self.assertEqual(rows[0]["auth"], "oauth")
        self.assertEqual(rows[0]["route"], "anthropic")

    async def test_a_vendor_turn_is_ledgered_as_vendor_key(self) -> None:
        self.vendor_up.messages_body = _success("glm-5.3")
        resp = await self.client.post(
            "/v1/messages", json=self.ask("claude-gw/glm-5.3"), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        rows = await self.rows(self.client)
        self.assertEqual(rows[0]["auth"], "vendor_key")

    async def test_a_picked_api_key_route_keeps_its_own_refusal(self) -> None:
        """B-1 analogue: the user chose what pays — an API-key 429 on a
        PICKED haiku is relayed, never rerouted to a vendor, even with the
        fallback on."""
        client = await self.client_with(key=True, fallback=True)
        self.script((429, EXHAUSTED))
        resp = await client.post(
            "/v1/messages", json=self.ask(f"{API_NS}{HAIKU}"), headers=self.headers(),
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(len(self.first.message_requests), 1)
        self.assertEqual(self.vendor_up.message_requests, [])


class ApiKeyCatalogTests(ApiKeyGatewayBase):

    async def _ids(self, client: Any) -> dict[str, str]:
        self.first.models_payload = {"data": [{"id": "claude-opus-5"}]}
        resp = await client.get("/v1/models", headers=self.auth())
        self.assertEqual(resp.status, 200)
        return {row["id"]: row["display_name"] for row in (await resp.json())["data"]}

    async def test_rows_appear_when_the_key_resolves(self) -> None:
        # claude-opus-5 is a 1M model: the picker publishes its [1m] row, and
        # the API-key route mirrors exactly what the OAuth route publishes.
        rows = await self._ids(self.client)
        self.assertIn("claude-opus-5[1m]", rows)
        api_id = f"{API_NS}claude-opus-5[1m]"
        self.assertIn(api_id, rows)
        self.assertTrue(
            rows[api_id].endswith(ANTHROPIC_API_KEY_ROUTE.display_suffix),
        )
        # And the published id routes back to the API-key route.
        decision = routing.route(api_id)
        assert isinstance(decision, Route)
        self.assertEqual(decision.auth, AUTH_API_KEY)

    async def test_a_projects_own_anthropic_key_names_are_not_used(self) -> None:
        """Only ANTHROPIC_API_KEY_GATEWAY resolves the route: a key stored
        under the generic names publishes nothing."""
        def getter(name: str, project: Any = None) -> str:
            if name in ("ANTHROPIC_API_KEY", "anthropic_api_key"):
                return FAKE_API_KEY
            return "" if name == "ANTHROPIC_API_KEY_GATEWAY" else FAKE_VENDOR_KEY

        client = await self.make_client(
            vendors={"zai": self.zai, "qwen": self.qwen}, key_getter=getter,
        )
        rows = await self._ids(client)
        self.assertFalse([i for i in rows if i.startswith(API_NS)])

    async def test_no_rows_without_the_key(self) -> None:
        client = await self.client_with(key=False, fallback=False)
        rows = await self._ids(client)
        self.assertIn("claude-opus-5[1m]", rows)
        self.assertFalse([i for i in rows if i.startswith(API_NS)])


class SubscriptionFallbackTests(ApiKeyGatewayBase):

    async def test_on_a_quota_refusal_any_slot_retries_once_with_the_key(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.script((429, RATE_LIMITED), (200, json.dumps(_success(OPUS)).encode()))
        with self.assertLogs(LOGGER, level="INFO") as cm:
            resp = await client.post(
                "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
            )
        self.assertEqual(resp.status, 200)
        reqs = self.first.message_requests
        self.assertEqual(len(reqs), 2)
        self.assertEqual(_header(reqs[0]["headers"], "authorization"), f"Bearer {FAKE_OAUTH}")
        second = reqs[1]["headers"]
        self.assertEqual(_header(second, "x-api-key"), FAKE_API_KEY)
        self.assertIsNone(_header(second, "authorization"))
        self.assertNotIn(OAUTH_BETA, _header(second, "anthropic-beta") or "")
        self.assertEqual(_header(second, "user-agent"), CLIENT_UA)
        # Same model, same bytes.
        self.assertEqual(reqs[0]["body"], reqs[1]["body"])
        rows = await self.rows(client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auth"], "api_key")
        self.assertEqual(rows[0]["fallback_from"], f"anthropic:{OPUS}")
        self.assertEqual(rows[0]["forward"], OPUS)
        joined = "\n".join(cm.output)
        self.assertIn("retrying once with the Claude API key", joined)
        self.assertIn("note=api_key_fallback", joined)
        self.assertNotIn(FAKE_API_KEY, joined)

    async def test_off_by_default_the_refusal_is_relayed_untouched(self) -> None:
        """The gate: the PRODUCTION reader with no launcher row -> OFF."""
        with mock.patch.dict(
            os.environ,
            {"VCT_LAUNCHER_DB_PATH": str(self.root / "no-launcher.db")},
        ):
            client = await self.client_with(key=True, fallback=None)
            self.script((429, RATE_LIMITED))
            resp = await client.post(
                "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
            )
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), RATE_LIMITED)
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_on_through_the_launcher_db_row(self) -> None:
        db = _launcher_db(self.root / "launcher.db", "true")
        with mock.patch.dict(os.environ, {"VCT_LAUNCHER_DB_PATH": str(db)}):
            client = await self.client_with(key=True, fallback=None)
            self.script((429, RATE_LIMITED), (200, json.dumps(_success(OPUS)).encode()))
            resp = await client.post(
                "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.first.message_requests), 2)

    async def test_a_plain_500_is_not_a_quota_verdict(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.script((500, b'{"type":"error","error":{"type":"api_error"}}'))
        resp = await client.post(
            "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
        )
        self.assertEqual(resp.status, 500)
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_a_context_overflow_is_not_a_quota_verdict(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        overflow = (
            b'{"type":"error","error":{"type":"invalid_request_error",'
            b'"message":"prompt is too long: 210000 tokens > 200000 maximum"}}'
        )
        self.script((400, overflow))
        resp = await client.post(
            "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_no_key_relays_the_subscription_refusal(self) -> None:
        client = await self.client_with(key=False, fallback=True)
        self.script((429, EXHAUSTED))
        resp = await client.post(
            "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(await resp.read(), EXHAUSTED)
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_no_key_on_the_haiku_slot_goes_straight_to_the_rung(self) -> None:
        client = await self.client_with(key=False, fallback=True)
        self.script((429, EXHAUSTED))
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.first.message_requests), 1)
        self.assertEqual(len(self.vendor_up.message_requests), 1)

    async def test_an_api_key_refusal_falls_through_to_the_existing_ladder(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.script((429, EXHAUSTED), (429, EXHAUSTED))
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["model"], "glm-5.3-flash")
        reqs = self.first.message_requests
        self.assertEqual(len(reqs), 2)
        self.assertEqual(_header(reqs[1]["headers"], "x-api-key"), FAKE_API_KEY)
        self.assertEqual(len(self.vendor_up.message_requests), 1)
        rows = await self.rows(client)
        self.assertEqual(rows[-1]["forward"], "glm-5.3-flash")
        self.assertEqual(rows[-1]["auth"], "vendor_key")
        self.assertTrue(rows[-1]["fallback_from"])

    async def test_the_haiku_slot_tries_the_key_before_the_vendor_rung(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.script((429, EXHAUSTED), (200, json.dumps(_success(HAIKU)).encode()))
        resp = await client.post(
            "/v1/messages", json=self.ask(HAIKU), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["model"], HAIKU)
        self.assertEqual(self.vendor_up.message_requests, [])

    async def test_the_setting_is_reread_after_its_short_cache(self) -> None:
        """Serve-stale (R15 LOW-1): past the TTL the OLD value is answered
        and ONE background re-read is scheduled; its result lands next."""
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]
        setting = gateway.api_key_fallback
        self.assertTrue(gateway.api_key_fallback_on())
        setting.reader = lambda: False
        self.assertTrue(gateway.api_key_fallback_on())  # within the TTL
        later = akr.time.monotonic() + akr.LAUNCHER_SETTING_TTL_S + 1
        with mock.patch.object(akr.time, "monotonic", return_value=later):
            self.assertTrue(gateway.api_key_fallback_on())  # stale, refreshing
            first = setting._refreshing
            self.assertTrue(gateway.api_key_fallback_on())
            self.assertIs(setting._refreshing, first, "single-flight")
            assert first is not None
            await first
        self.assertFalse(gateway.api_key_fallback_on())

    async def test_the_reader_never_runs_on_the_event_loop(self) -> None:
        import threading

        client = await self.client_with(key=True, fallback=True)
        setting = client.app[srv.APP_KEY].anthropic_auth
        threads: list[int] = []
        setting.reader = lambda: threads.append(threading.get_ident()) or "oauth"
        setting.expire()
        setting.get()
        assert setting._refreshing is not None
        await setting._refreshing
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_before_any_value_the_safe_default_is_answered(self) -> None:
        """No cached value yet: ``get`` answers OAuth / OFF even when the row
        says otherwise — and the refresh it schedules then applies it."""
        client = await self.client_with(key=True, fallback=False)
        gateway = client.app[srv.APP_KEY]
        gateway.anthropic_auth.reader = lambda: ANTHROPIC_AUTH_API_KEY
        gateway.anthropic_auth.expire()
        gateway.api_key_fallback.reader = lambda: True
        gateway.api_key_fallback.expire()
        self.assertFalse(gateway.api_key_mode_on())
        self.assertFalse(gateway.api_key_fallback_on())
        for setting in (gateway.anthropic_auth, gateway.api_key_fallback):
            assert setting._refreshing is not None
            await setting._refreshing
        self.assertTrue(gateway.api_key_mode_on())
        self.assertTrue(gateway.api_key_fallback_on())

    async def test_the_gateway_primes_the_settings_before_serving(self) -> None:
        db = _launcher_db(self.root / "prime.db", "api_key", ANTHROPIC_AUTH_APP_STATE_KEY)
        with mock.patch.dict(os.environ, {"VCT_LAUNCHER_DB_PATH": str(db)}):
            client = await self.client_with(key=True, fallback=False)
        self.assertTrue(client.app[srv.APP_KEY].api_key_mode_on())

    async def test_a_broken_setting_reader_means_off(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        gateway = client.app[srv.APP_KEY]

        def boom() -> bool:
            raise RuntimeError("db gone")

        gateway.api_key_fallback.reader = boom
        with self.assertLogs("model_router", level="ERROR"):
            self.assertFalse(await gateway.api_key_fallback.refresh())
        self.assertFalse(gateway.api_key_fallback_on())


# ══════════════════════════════════════════════════════════════════════════
# A key Anthropic REFUSES (401/403) — review R15 MEDIUM-1
# ══════════════════════════════════════════════════════════════════════════
INVALID_KEY = (
    b'{"type":"error","error":{"type":"authentication_error",'
    b'"message":"invalid x-api-key"}}'
)
NO_PERMISSION = (
    b'{"type":"error","error":{"type":"permission_error",'
    b'"message":"This organization has been disabled."}}'
)
REFUSALS = ((401, INVALID_KEY), (403, NO_PERMISSION))


class ApiKeyRefusalTests(ApiKeyGatewayBase):

    async def _assert_local_answer(self, resp: Any, status: int) -> None:
        """Not Claude Code's login-failure shape: 503 api_error, no retry,
        naming the secret and the switch, never the key."""
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers.get("x-should-retry"), "false")
        body = await resp.json()
        self.assertEqual(body["error"]["type"], "api_error")
        message = body["error"]["message"]
        self.assertIn("ANTHROPIC_API_KEY_GATEWAY", message)
        self.assertIn("Anthropic: OAuth", message)
        self.assertIn(f"HTTP {status}", message)
        self.assertNotIn(FAKE_API_KEY, message)
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_explicit_route_refusal_is_a_local_non_login_error(self) -> None:
        for status, raw in REFUSALS:
            with self.subTest(status=status):
                self.first.requests.clear()
                self.script((status, raw))
                resp = await self.client.post(
                    "/v1/messages", json=self.ask(f"{API_NS}{OPUS}"),
                    headers=self.headers(),
                )
                await self._assert_local_answer(resp, status)

    async def test_api_key_mode_refusal_is_a_local_non_login_error(self) -> None:
        client = await self.client_with(key=True, fallback=False)
        setting = client.app[srv.APP_KEY].anthropic_auth
        setting.reader = lambda: ANTHROPIC_AUTH_API_KEY
        await setting.refresh()
        for status, raw in REFUSALS:
            with self.subTest(status=status):
                self.first.requests.clear()
                self.script((status, raw))
                resp = await client.post(
                    "/v1/messages", json=self.ask(HAIKU), headers=self.headers(),
                )
                await self._assert_local_answer(resp, status)
                # Not rerouted either: the user chose the key for everything.
                self.assertEqual(self.vendor_up.message_requests, [])

    async def test_fallback_refusal_walks_on_to_the_rung(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        for status, raw in REFUSALS:
            with self.subTest(status=status):
                self.first.requests.clear()
                self.vendor_up.requests.clear()
                self.script((429, EXHAUSTED), (status, raw))
                with self.assertLogs(LOGGER, level="INFO") as cm:
                    resp = await client.post(
                        "/v1/messages", json=self.ask(HAIKU), headers=self.headers(),
                    )
                self.assertEqual(resp.status, 200)
                self.assertEqual((await resp.json())["model"], "glm-5.3-flash")
                self.assertEqual(len(self.first.message_requests), 2)
                self.assertEqual(len(self.vendor_up.message_requests), 1)
                joined = "\n".join(cm.output)
                self.assertIn("refused the Claude API key", joined)
                self.assertNotIn(FAKE_API_KEY, joined)

    async def test_fallback_refusal_without_a_rung_relays_the_subscription_refusal(self) -> None:
        client = await self.client_with(key=True, fallback=True)
        for status, raw in REFUSALS:
            with self.subTest(status=status):
                self.first.requests.clear()
                self.script((429, RATE_LIMITED), (status, raw))
                resp = await client.post(
                    "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
                )
                # The SUBSCRIPTION's quota answer, not the key's 401/403.
                self.assertEqual(resp.status, 429)
                self.assertEqual(await resp.read(), RATE_LIMITED)
                self.assertEqual(len(self.first.message_requests), 2)

    async def test_health_says_rejected_until_the_key_works_again(self) -> None:
        gateway = self.client.app[srv.APP_KEY]
        await gateway._api_key_present()  # the key is cached
        self.script((401, INVALID_KEY))
        await self.client.post(
            "/v1/messages", json=self.ask(f"{API_NS}{OPUS}"), headers=self.headers(),
        )
        auth = (await (await self.client.get("/health")).json())["anthropic_auth"]
        self.assertEqual(auth["api_key"], "rejected")
        self.script((200, json.dumps(_success(OPUS)).encode()))
        await self.client.post(
            "/v1/messages", json=self.ask(f"{API_NS}{OPUS}"), headers=self.headers(),
        )
        auth = (await (await self.client.get("/health")).json())["anthropic_auth"]
        self.assertEqual(auth["api_key"], "available")

    async def test_a_replaced_key_is_no_longer_reported_rejected(self) -> None:
        gateway = self.client.app[srv.APP_KEY]
        await gateway._api_key_present()
        gateway.note_api_key_refused("test-an-older-fake-key")  # vco-secret-scan: test-placeholder
        self.assertEqual(gateway.api_key_state(), "available")
        gateway.note_api_key_refused(FAKE_API_KEY)
        self.assertEqual(gateway.api_key_state(), "rejected")


# ══════════════════════════════════════════════════════════════════════════
# The GLOBAL auth mode (homepage switch): oauth | api_key
# ══════════════════════════════════════════════════════════════════════════
class AuthModeSettingTests(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory(prefix="apigw-mode-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _read(self, value: str | None) -> str:
        return anthropic_auth_mode(db_path=_launcher_db(
            self.root / f"m-{value}.db", value, ANTHROPIC_AUTH_APP_STATE_KEY,
        ))

    def test_the_mode_round_trips_and_defaults_to_oauth(self) -> None:
        self.assertEqual(self._read("api_key"), ANTHROPIC_AUTH_API_KEY)
        self.assertEqual(self._read("oauth"), ANTHROPIC_AUTH_OAUTH)
        self.assertEqual(self._read(None), ANTHROPIC_AUTH_OAUTH)
        self.assertEqual(self._read("API_KEY "), ANTHROPIC_AUTH_API_KEY)
        self.assertEqual(self._read("second-account"), ANTHROPIC_AUTH_OAUTH)

    def test_the_launcher_command_writes_the_key_and_values_the_gateway_reads(self) -> None:
        rust = (REPO / "launcher/src-tauri/src/commands/gateway_anthropic_auth.rs").read_text(
            encoding="utf-8",
        )
        key = re.search(r'pub const ANTHROPIC_AUTH_APP_STATE_KEY: &str = "([^"]+)"', rust)
        assert key is not None, "the Rust constant is missing"
        self.assertEqual(key.group(1), ANTHROPIC_AUTH_APP_STATE_KEY)
        modes = re.search(r"pub const ANTHROPIC_AUTH_MODES: \[&str; \d+\] = \[([^\]]+)\]", rust)
        assert modes is not None, "the Rust mode list is missing"
        self.assertEqual(
            tuple(m.strip().strip('"') for m in modes.group(1).split(",") if m.strip()),
            ANTHROPIC_AUTH_MODES,
        )
        ts = (REPO / "launcher/src/lib/api/model_gateway.ts").read_text(encoding="utf-8")
        for mode in ANTHROPIC_AUTH_MODES:
            self.assertIn(f"'{mode}'", ts)


class AuthModeGatewayTests(ApiKeyGatewayBase):

    async def client_mode(self, mode: str, *, key: bool = True) -> Any:
        client = await self.client_with(key=key, fallback=False)
        setting = client.app[srv.APP_KEY].anthropic_auth
        setting.reader = lambda: mode
        await setting.refresh()
        return client

    async def test_api_key_mode_sends_every_first_party_slot_with_the_key(self) -> None:
        client = await self.client_mode(ANTHROPIC_AUTH_API_KEY)
        self.script((200, json.dumps(_success(OPUS)).encode()))
        for model in (OPUS, HAIKU, f"{OPUS}[1m]"):
            resp = await client.post(
                "/v1/messages", json=self.ask(model), headers=self.headers(),
            )
            self.assertEqual(resp.status, 200, model)
        reqs = self.first.message_requests
        self.assertEqual(len(reqs), 3)
        for req in reqs:
            h = req["headers"]
            self.assertEqual(_header(h, "x-api-key"), FAKE_API_KEY)
            self.assertIsNone(_header(h, "authorization"))
            self.assertNotIn(OAUTH_BETA, _header(h, "anthropic-beta") or "")
            self.assertIn("claude-code-20250219", _header(h, "anthropic-beta") or "")
            self.assertEqual(_header(h, "user-agent"), CLIENT_UA)
            self.assertEqual(_header(h, "x-app"), "cli")
        # The [1m] request still buys the window, as a beta.
        self.assertIn(srv.CONTEXT_1M_BETA, _header(reqs[2]["headers"], "anthropic-beta") or "")
        rows = await self.rows(client)
        self.assertEqual({r["auth"] for r in rows}, {"api_key"})
        self.assertEqual({r["route"] for r in rows}, {srv.API_KEY_ROUTE_LABEL})
        self.assertEqual({r["fallback_from"] for r in rows}, {""})

    async def test_oauth_mode_is_todays_behaviour(self) -> None:
        client = await self.client_mode(ANTHROPIC_AUTH_OAUTH)
        self.script((200, json.dumps(_success(OPUS)).encode()))
        resp = await client.post("/v1/messages", json=self.ask(OPUS), headers=self.headers())
        self.assertEqual(resp.status, 200)
        h = self.first.message_requests[0]["headers"]
        self.assertEqual(_header(h, "authorization"), f"Bearer {FAKE_OAUTH}")
        self.assertIsNone(_header(h, "x-api-key"))

    async def test_api_key_mode_without_a_key_refuses_never_uses_the_login(self) -> None:
        client = await self.client_mode(ANTHROPIC_AUTH_API_KEY, key=False)
        resp = await client.post("/v1/messages", json=self.ask(OPUS), headers=self.headers())
        self.assertEqual(resp.status, 503)
        self.assertEqual(self.first.message_requests, [])

    async def test_api_key_mode_through_the_launcher_db_row(self) -> None:
        db = _launcher_db(self.root / "mode.db", "api_key", ANTHROPIC_AUTH_APP_STATE_KEY)
        with mock.patch.dict(os.environ, {"VCT_LAUNCHER_DB_PATH": str(db)}):
            client = await self.client_with(key=True, fallback=False)
            self.script((200, json.dumps(_success(OPUS)).encode()))
            resp = await client.post(
                "/v1/messages", json=self.ask(OPUS), headers=self.headers(),
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            _header(self.first.message_requests[0]["headers"], "x-api-key"), FAKE_API_KEY,
        )

    async def test_vendor_routes_are_untouched_by_the_mode(self) -> None:
        client = await self.client_mode(ANTHROPIC_AUTH_API_KEY)
        self.vendor_up.messages_body = _success("glm-5.3")
        resp = await client.post(
            "/v1/messages", json=self.ask("claude-gw/glm-5.3"), headers=self.headers(),
        )
        self.assertEqual(resp.status, 200)
        h = self.vendor_up.message_requests[0]["headers"]
        self.assertEqual(_header(h, "authorization"), f"Bearer {FAKE_VENDOR_KEY}")

    async def test_api_key_mode_keeps_the_haiku_slots_ladder(self) -> None:
        client = await self.client_mode(ANTHROPIC_AUTH_API_KEY)
        self.script((429, EXHAUSTED))
        self.vendor_up.messages_body = _success("glm-5.3-flash")
        resp = await client.post("/v1/messages", json=self.ask(HAIKU), headers=self.headers())
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["model"], "glm-5.3-flash")
        self.assertEqual(len(self.first.message_requests), 1)

    async def test_health_reports_the_mode_and_whether_the_key_resolves(self) -> None:
        for key, expected in ((True, "available"), (False, "missing")):
            client = await self.client_mode(ANTHROPIC_AUTH_API_KEY, key=key)
            gateway = client.app[srv.APP_KEY]
            first = (await (await client.get("/health")).json())["anthropic_auth"]
            self.assertEqual(first["mode"], ANTHROPIC_AUTH_API_KEY)
            self.assertEqual(first["api_key_secret"], "ANTHROPIC_API_KEY_GATEWAY")
            self.assertIn(first["api_key"], ("unknown", expected))
            if gateway._api_key_probe is not None:
                await gateway._api_key_probe
            second = (await (await client.get("/health")).json())["anthropic_auth"]
            self.assertEqual(second["api_key"], expected, key)
            self.assertFalse(second["api_key_fallback"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
