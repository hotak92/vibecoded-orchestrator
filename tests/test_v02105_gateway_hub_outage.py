# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — a HUB OUTAGE is not a missing key, and not a scope problem.

Field evidence (2026-10-21/22, from the SimRacing report): vct-hub was down
for ~2 days. A ``deepseek-researcher`` lane died on "503 no key found for
vendor 'qwen'" while the GLM lane survived on its serve-stale cached key.
Two defects, one cause:

* the 6 h serve-stale bound is shorter than the outage, so a key resolved
  BEFORE the hub stopped aged out mid-outage — and a key that had never been
  resolved in that process had nothing to fall back on at all. Nothing here
  was ever per-vendor: GLM survived because it had resolved earlier, qwen did
  not because it had not;
* the 503 text was the SCAFFOLDING of the scope diagnostic ("this daemon is
  running out of …, a different path from the pin … clearing
  ``VCT_MODEL_GATEWAY_SECRET_PROJECT`` restores the default") for a cause
  that had nothing to do with the scope. An operator reading it edits a
  variable; the hub stays down.

Pinned here, each with its leave-alone side:

* tier 1 unreachable → the last-known-good key is served whatever its age;
* the message names the outage FIRST, says the keychain was never consulted,
  and carries NO scope remedy;
* hub reachable again → ordinary resolution, nothing served stale;
* a DEFINITE miss (the store answered "no such key") still honours the bound.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import NoReturn
from unittest import mock

from vco_lib import agent_secrets

from model_router import secrets as S
from model_router.secrets import VendorKeyResolver
from model_router.vendors import Vendor

FAKE_KEY = "v02105-synthetic-not-a-real-key-0000"

ACME = Vendor(
    vendor_id="acme",
    display_suffix=" · Acme",
    namespace="claude-acme/",
    upstream="https://api.acme.example",
    secret_keys=("acme_primary_key", "acme_legacy_key"),
    bare_id_prefixes=("acme-",),
)

SCOPE_ENV = S.SCOPE_ENV


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hub_down_message(key: str) -> agent_secrets.SecretNotFound:
    """The exception ``agent_secrets.get`` raises when the hub is not running
    and the file store has no copy — the exact shape the gateway receives."""
    miss = agent_secrets.SecretNotFound(
        f"secret {key!r} not found (tier 1 hub: hub is not running; tier 2 "
        f"file store: ~/.vct-secrets/shared; tier 3 project .env: skipped)."
    )
    miss.hub_unreachable = True  # type: ignore[attr-defined]
    return miss


def _hub_down(key: str, project: str | None = None) -> NoReturn:
    raise _hub_down_message(key)


def _hub_up(key: str, project: str | None = None) -> str:
    return FAKE_KEY


def _definite_miss(key: str, project: str | None = None) -> NoReturn:
    raise LookupError("absent")


class ServeStaleThroughAHubOutageTests(unittest.TestCase):
    def test_the_last_good_key_survives_past_the_six_hour_bound(self) -> None:
        """The outage lasted ~2 days — 8x the bound."""
        state = {"mode": "up"}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["mode"] == "up":
                return FAKE_KEY
            _hub_down(key)

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=6 * 3600,
        )
        self.assertEqual(resolver.resolve(ACME).state, "resolved")
        state["mode"] = "down"
        clock.now += 2 * 24 * 3600  # two days of outage
        served = resolver.resolve(ACME)
        self.assertEqual(served.key, FAKE_KEY)
        self.assertEqual(served.state, "stale")
        self.assertEqual(resolver.serving_stale_ids(), ("acme",))

    def test_the_same_rule_reaches_every_vendor(self) -> None:
        """GLM survived and qwen did not because of RESOLUTION HISTORY, never
        because of a per-vendor path: two vendors, one resolver, same rule."""
        other = Vendor(
            vendor_id="other",
            display_suffix=" · Other",
            namespace="claude-other/",
            upstream="https://api.other.example",
            secret_keys=("other_key",),
            bare_id_prefixes=("other-",),
        )
        state = {"mode": "up"}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["mode"] == "up":
                return FAKE_KEY
            _hub_down(key)

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=600,
        )
        resolver.resolve(ACME)
        resolver.resolve(other)
        state["mode"] = "down"
        clock.now += 10 * 24 * 3600
        for vendor in (ACME, other):
            with self.subTest(vendor=vendor.vendor_id):
                served = resolver.resolve(vendor)
                self.assertEqual(served.key, FAKE_KEY)
                self.assertEqual(served.state, "stale")
        self.assertEqual(resolver.serving_stale_ids(), ("acme", "other"))

    def test_a_vendor_never_resolved_has_nothing_to_serve(self) -> None:
        """Honest about the limit of serve-stale: a key this process never
        read is not a key it can hold. That vendor gets the outage message."""
        result = VendorKeyResolver(getter=_hub_down).resolve(ACME)
        self.assertIsNone(result.key)
        self.assertTrue(result.hub_unreachable)

    def test_hub_up_resolves_normally_and_never_serves_stale(self) -> None:
        resolver = VendorKeyResolver(getter=_hub_up, ttl_s=300, clock=_Clock())
        result = resolver.resolve(ACME)
        self.assertEqual(result.key, FAKE_KEY)
        self.assertEqual(result.state, "resolved")
        self.assertEqual(resolver.serving_stale_ids(), ())
        self.assertFalse(result.hub_unreachable)

    def test_a_definite_miss_still_honours_the_stale_bound(self) -> None:
        """The unbounded arm is for "the store could not be ASKED". A store
        that answered "no such key" is still allowed to expire."""
        state = {"mode": "up"}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["mode"] == "up":
                return FAKE_KEY
            _definite_miss(key)

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=600,
        )
        resolver.resolve(ACME)
        state["mode"] = "down"
        clock.now += 10_000
        self.assertIsNone(resolver.resolve(ACME).key)

    def test_the_recovery_is_stated_again_after_a_hub_outage(self) -> None:
        state = {"mode": "up"}
        clock = _Clock()

        def getter(key: str, project: str | None = None) -> str:
            if state["mode"] == "up":
                return FAKE_KEY
            _hub_down(key)

        resolver = VendorKeyResolver(
            getter=getter, ttl_s=300, clock=clock, serve_stale_max_age_s=600,
        )
        resolver.resolve(ACME)
        state["mode"] = "down"
        clock.now += 10_000
        with self.assertLogs("model_router.secrets", level="INFO") as logs:
            resolver.resolve(ACME)      # enters stale service
            state["mode"] = "up"
            clock.now += 10_000
            self.assertEqual(resolver.resolve(ACME).state, "resolved")
        self.assertTrue(any("resolves again" in line for line in logs.output))


class HubOutageMessageTests(unittest.TestCase):
    def _problem(self, resolver: VendorKeyResolver) -> str:
        result = resolver.resolve(ACME)
        self.assertIsNone(result.key)
        self.assertTrue(result.hub_unreachable)
        self.assertFalse(result.transient, "a hub outage is not a busy keychain")
        return result.problem or ""

    def test_the_outage_is_named_first(self) -> None:
        problem = self._problem(VendorKeyResolver(getter=_hub_down))
        self.assertTrue(problem.startswith("vendor 'acme' key: vct-hub is NOT REACHABLE"),
                        problem)

    def test_it_says_the_key_may_exist_and_gives_no_scope_remedy(self) -> None:
        problem = self._problem(VendorKeyResolver(getter=_hub_down))
        self.assertIn("MAY WELL EXIST", problem)
        self.assertNotIn("no key found", problem)
        # A write path is still offered — the file store works without the
        # hub — but the SECRET SCOPE never is.
        self.assertIn("vct set --shared --key acme_primary_key", problem)
        self.assertNotIn(f"clearing {SCOPE_ENV}", problem)
        self.assertNotIn(f"Set {SCOPE_ENV}", problem)
        self.assertNotIn(f"{SCOPE_ENV}=", problem)

    def test_a_stale_pin_never_gets_the_clear_it_hint(self) -> None:
        """The field report verbatim: the pin pointed at a preview worktree,
        the hub was down, and the hint told the operator to clear the pin."""
        resolver = VendorKeyResolver(
            getter=_hub_down,
            project="/home/somewhere/vco-wt-int",
            install_root=lambda: "/home/somewhere/vco-dev",
        )
        problem = self._problem(resolver)
        self.assertIn("vco-wt-int", problem)          # reported, as an observation
        self.assertIn("[pin]", problem)
        self.assertNotIn("clearing", problem)
        self.assertNotIn("stale one baked", problem)

    def test_the_ordinary_miss_keeps_both_write_paths_and_the_override(self) -> None:
        """Leave-alone side: a hub that ANSWERED and has no such key is still
        a "where to put it" answer, override included. The outage arm must
        not swallow that message for the ordinary case."""
        result = VendorKeyResolver(getter=_definite_miss).resolve(ACME)
        self.assertFalse(result.hub_unreachable)
        problem = result.problem or ""
        self.assertNotIn("vct-hub is NOT REACHABLE", problem)
        self.assertIn("Secrets panel", problem)
        self.assertIn("vct set --shared --key acme_primary_key", problem)
        self.assertIn("~/.vct-secrets/shared/", problem)
        self.assertIn(SCOPE_ENV, problem)

    def test_the_outage_line_is_logged_once_per_vendor(self) -> None:
        resolver = VendorKeyResolver(getter=_hub_down, ttl_s=1, clock=_Clock())
        with self.assertLogs("model_router.secrets", level="WARNING") as logs:
            for _ in range(4):
                resolver.resolve(ACME)
        lines = [line for line in logs.output if "NOT REACHABLE" in line]
        self.assertEqual(len(lines), 1, logs.output)


class ClassificationTests(unittest.TestCase):
    def test_the_attribute_is_what_surfaces_through_the_resolver_chain(self) -> None:
        """``agent_secrets.get`` reports a hub outage as SecretNotFound (the
        file-store fallback ran) — the TYPE cannot say "nothing was asked".
        The flag is what model_router reads, and this pins the contract."""
        exc = _hub_down_message("acme_primary_key")
        self.assertIsInstance(exc, agent_secrets.SecretNotFound)
        self.assertTrue(S._is_hub_unreachable(exc))

    def test_a_real_hub_unreachable_is_recognised_by_type(self) -> None:
        """The ``allow_file_fallback=False`` shape: the exception IS a
        HubUnreachable."""
        self.assertTrue(S._is_hub_unreachable(agent_secrets.HubUnreachable("down")))

    def test_an_ordinary_miss_is_not_a_hub_outage(self) -> None:
        self.assertFalse(S._is_hub_unreachable(agent_secrets.SecretNotFound("absent")))
        self.assertFalse(S._is_hub_unreachable(LookupError("absent")))

    def test_a_busy_store_is_not_a_hub_outage(self) -> None:
        """Tier 1 answered (or timed out) — a different message and a
        different remedy, and they must not be merged."""
        self.assertFalse(
            S._is_hub_unreachable(agent_secrets.SecretStoreUnavailable("busy")),
        )
        self.assertFalse(
            S._is_hub_unreachable(agent_secrets.KeychainUnavailable("busy")),
        )


class AgentSecretsMarksTheMissTests(unittest.TestCase):
    """The vco_lib half: the resolver chain must SAY that nothing was asked.

    Both sides, because the flag is worthless if it is set on every miss: a
    hub that answers "not stored" must keep raising a plain SecretNotFound.
    """

    def _get_with(self, side_effect):
        with tempfile.TemporaryDirectory(prefix="v02105-secrets-") as tmp, \
                mock.patch.dict(
                    os.environ, {"VCT_SECRETS_DIR": str(Path(tmp) / "store")},
                ), \
                mock.patch.object(
                    agent_secrets, "_hub_get", side_effect=side_effect,
                ), \
                mock.patch.object(
                    agent_secrets, "_project_dotenv_get", lambda k, p: None,
                ):
            (Path(tmp) / "store" / "shared").mkdir(parents=True)
            with self.assertRaises(agent_secrets.SecretNotFound) as caught:
                agent_secrets.get("acme_primary_key")
        return caught.exception

    def test_a_hub_that_is_not_running_marks_the_miss(self) -> None:
        exc = self._get_with(agent_secrets.HubUnreachable("hub is not running"))
        self.assertTrue(getattr(exc, "hub_unreachable", False))

    def test_a_hub_that_answered_not_stored_does_not(self) -> None:
        exc = self._get_with(agent_secrets.SecretNotFound("not stored"))
        self.assertFalse(getattr(exc, "hub_unreachable", False))

    def test_the_miss_still_says_the_keychain_was_not_checked(self) -> None:
        exc = self._get_with(agent_secrets.HubUnreachable("hub is not running"))
        self.assertIn("The launcher hub is not running", str(exc))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()