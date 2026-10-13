# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 (W6-AB): the gateway never turns a busy secret store into a
failed chat or a false "no key found".

2026-10-13 ~04:00: ``claude-gw/qwen/qwen3.8-max`` answered 503 ``no key
found for vendor 'qwen' … Put it in the SHARED scope`` while the key was in
the OS keychain — the hub had timed out (``Read timed out. (read
timeout=5.0)``). Pinned here with the resolver's REAL exception types:

* ``SecretStoreUnavailable`` (the hub did not answer) is TRANSIENT, exactly
  like ``KeychainUnavailable``: never "no key found", never "vct set";
* a transient failure serves the last-known-good key even PAST the
  serve-stale bound — the bound exists to stop serving a key the store says
  is gone, and a store that could not be asked said nothing;
* with a last-known-good key, an expired cache entry is refreshed in the
  BACKGROUND — the request never waits on a slow store;
* with no key at all, the client gets 503 + ``Retry-After`` and an accurate
  "retrying is safe" message.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from typing import NoReturn

from model_router.secrets import TRANSIENT_RETRY_S, VendorKeyResolver
from model_router.vendors import Vendor
from tests.test_model_router_server import GatewayTestBase

FAKE_KEY = "w6ab-synthetic-not-a-real-key-0000"

ACME = Vendor(
    vendor_id="acme",
    display_suffix=" · Acme",
    namespace="claude-acme/",
    upstream="https://api.acme.example",
    secret_keys=("acme_primary_key", "acme_legacy_key"),
    bare_id_prefixes=("acme-",),
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _store_unavailable(key: str, project: str | None = None) -> NoReturn:
    from vco_lib.agent_secrets import SecretStoreUnavailable

    raise SecretStoreUnavailable(
        f"secret store unavailable: vct-hub is running but did not answer the "
        f"read of {key!r} in time"
    )


class StoreUnavailableIsTransientTests(unittest.TestCase):
    def test_hub_timeout_is_transient_not_no_key_found(self) -> None:
        result = VendorKeyResolver(getter=_store_unavailable).resolve(ACME)
        self.assertIsNone(result.key)
        self.assertTrue(result.transient)
        message = result.problem or ""
        self.assertIn("retrying is safe", message)
        self.assertIn("secret store busy", message)
        self.assertNotIn("no key found", message)
        self.assertNotIn("vct set", message)
        self.assertNotIn("Put it in", message)

    def test_transient_serves_last_good_past_the_stale_bound(self) -> None:
        state = {"ok": True}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["ok"]:
                return FAKE_KEY
            _store_unavailable(key)

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=600,
        )
        self.assertEqual(resolver.resolve(ACME).state, "resolved")
        state["ok"] = False
        clock.now += 10_000  # far past the 600 s bound
        served = resolver.resolve(ACME)
        self.assertEqual(served.key, FAKE_KEY)
        self.assertEqual(served.state, "stale")

    def test_a_definite_miss_still_honours_the_stale_bound(self) -> None:
        state = {"ok": True}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["ok"]:
                return FAKE_KEY
            raise LookupError("absent")

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=600,
        )
        resolver.resolve(ACME)
        state["ok"] = False
        clock.now += 10_000
        self.assertIsNone(resolver.resolve(ACME).key)


class BackgroundRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_entry_with_last_good_is_served_without_waiting(self) -> None:
        clock = _Clock()
        gate = threading.Event()
        calls: list[str] = []
        state = {"slow": False}

        def getter(key: str, project: str | None = None) -> str:
            calls.append(key)
            if state["slow"]:
                gate.wait(5)
            return FAKE_KEY

        resolver = VendorKeyResolver(getter=getter, ttl_s=300, clock=clock)
        self.assertEqual((await resolver.aresolve(ACME)).state, "resolved")
        state["slow"] = True
        clock.now += 301
        try:
            result = await asyncio.wait_for(resolver.aresolve(ACME), timeout=1.0)
        finally:
            gate.set()
        self.assertEqual(result.key, FAKE_KEY)
        await resolver.wait_for_refreshes()
        self.assertEqual(len(calls), 2, "exactly one background refresh")
        self.assertEqual((await resolver.aresolve(ACME)).state, "cached")

    async def test_refresh_is_single_flight(self) -> None:
        clock = _Clock()
        gate = threading.Event()
        calls: list[str] = []
        state = {"slow": False}

        def getter(key: str, project: str | None = None) -> str:
            calls.append(key)
            if state["slow"]:
                gate.wait(5)
            return FAKE_KEY

        resolver = VendorKeyResolver(getter=getter, ttl_s=300, clock=clock)
        await resolver.aresolve(ACME)
        state["slow"] = True
        clock.now += 301
        for _ in range(5):
            self.assertEqual((await resolver.aresolve(ACME)).key, FAKE_KEY)
        gate.set()
        await resolver.wait_for_refreshes()
        self.assertEqual(len(calls), 2)

    async def test_no_last_good_still_waits_for_the_first_resolution(self) -> None:
        resolver = VendorKeyResolver(getter=lambda key, project=None: FAKE_KEY)
        result = await resolver.aresolve(ACME)
        self.assertEqual(result.state, "resolved")


class GatewayRetryAfterTests(GatewayTestBase):
    async def test_store_unavailable_answers_503_with_retry_after(self) -> None:
        client = await self.make_client(key_getter=_store_unavailable)
        resp = await client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers.get("Retry-After"), str(TRANSIENT_RETRY_S))
        body = await resp.json()
        message = body["error"]["message"]
        self.assertIn("retrying is safe", message)
        self.assertNotIn("no key found", message)
        self.assertNotIn("vct set", message)

    async def test_a_definite_miss_has_no_retry_after(self) -> None:
        client = await self.make_client(
            key_getter=lambda key, project=None: (_ for _ in ()).throw(
                LookupError("absent"),
            ),
        )
        resp = await client.post(
            "/v1/messages",
            headers=self.auth(),
            json={"model": "claude-gw/glm-5.3", "messages": []},
        )
        self.assertEqual(resp.status, 503)
        self.assertIsNone(resp.headers.get("Retry-After"))
