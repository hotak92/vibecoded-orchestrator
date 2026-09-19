# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Vendor-key resolution: the fallback chain, the cache, and value containment.

The value-containment tests are the important ones. A resolved subscription
key must appear in exactly one place — the outbound request header — and
nowhere else: not in a log record, not in an exception, not in a ``repr``, not
on a command line. Each of those has been a real leak vector in some codebase,
and ``repr`` in particular is the sneaky one because pytest prints it on any
unrelated assertion failure in the same test.

The chain itself belongs to ``vco_lib.agent_secrets`` (hub -> file store ->
project ``.env``), which has its own tests. What is tested here is this
package's use of it: lazy import, first-name-wins across the declared key
names, caching, negative caching, and that a miss produces an actionable
message rather than a bare failure.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from pathlib import Path

from model_router.secrets import NEGATIVE_TTL_CAP_S, VendorKeyResolver
from model_router.vendors import VENDORS, Vendor


def _absent(key, project=None):
    """A getter for which no declared key name resolves."""
    raise LookupError("absent")

#: A synthetic value with a shape no real key has, so a leak is unmistakable.
FAKE_KEY = "wp9-synthetic-not-a-real-key-0000"

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


class ResolutionChainTests(unittest.TestCase):
    def test_first_declared_key_name_wins(self) -> None:
        seen: list[str] = []

        def getter(key, project=None):
            seen.append(key)
            return FAKE_KEY

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertEqual(result.key, FAKE_KEY)
        self.assertEqual(result.state, "resolved")
        self.assertEqual(result.resolved_from, "acme_primary_key")
        self.assertEqual(seen, ["acme_primary_key"])

    def test_falls_through_to_the_next_declared_name(self) -> None:
        def getter(key, project=None):
            if key == "acme_primary_key":
                raise KeyError("not found")
            return FAKE_KEY

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertEqual(result.key, FAKE_KEY)
        self.assertEqual(result.resolved_from, "acme_legacy_key")

    def test_an_empty_value_counts_as_a_miss(self) -> None:
        """A store that answers with whitespace has not answered."""
        def getter(key, project=None):
            return "   " if key == "acme_primary_key" else FAKE_KEY

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertEqual(result.resolved_from, "acme_legacy_key")

    def test_total_miss_is_actionable(self) -> None:
        def getter(key, project=None):
            raise LookupError("no such key")

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertIsNone(result.key)
        self.assertEqual(result.state, "missing")
        message = result.problem or ""
        self.assertIn("acme_primary_key", message)
        self.assertIn("acme_legacy_key", message)
        self.assertIn("Secrets panel", message)
        self.assertIn("vct set", message)

    def test_project_scope_is_passed_through(self) -> None:
        seen: list[object] = []

        def getter(key, project=None):
            seen.append(project)
            return FAKE_KEY

        VendorKeyResolver(getter=getter, project="Acme").resolve(ACME)
        self.assertEqual(seen, ["Acme"])

    def test_shared_scope_is_named_in_the_miss_message(self) -> None:
        def getter(key, project=None):
            raise LookupError("nope")

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertIn("VCT_MODEL_GATEWAY_SECRET_PROJECT", result.problem or "")

    def test_resolver_import_failure_is_reported_not_swallowed(self) -> None:
        """A missing ``vco_lib`` means a broken install; say so."""
        import model_router.secrets as mod

        original = mod._default_getter
        mod._default_getter = lambda: (_ for _ in ()).throw(  # type: ignore[assignment]
            ImportError("No module named 'vco_lib'"),
        )
        self.addCleanup(setattr, mod, "_default_getter", original)
        with self.assertLogs(mod.logger, level=logging.ERROR):
            result = VendorKeyResolver().resolve(ACME)
        self.assertIsNone(result.key)
        self.assertIn("broken install", result.problem or "")


class CacheTests(unittest.TestCase):
    def test_a_hit_is_cached_for_the_ttl(self) -> None:
        calls = []
        clock = _Clock()

        def getter(key, project=None):
            calls.append(key)
            return FAKE_KEY

        resolver = VendorKeyResolver(getter=getter, ttl_s=300, clock=clock)
        self.assertEqual(resolver.resolve(ACME).state, "resolved")
        clock.now += 299
        self.assertEqual(resolver.resolve(ACME).state, "cached")
        self.assertEqual(len(calls), 1)
        clock.now += 2
        self.assertEqual(resolver.resolve(ACME).state, "resolved")
        self.assertEqual(len(calls), 2)

    def test_a_miss_is_cached_only_briefly(self) -> None:
        """So a keyless machine does not turn a request burst into a resolver
        burst, while a key added in the GUI is picked up within seconds."""
        calls = []
        clock = _Clock()

        def getter(key, project=None):
            calls.append(key)
            raise LookupError("nope")

        resolver = VendorKeyResolver(getter=getter, ttl_s=3600, clock=clock)
        resolver.resolve(ACME)
        first = len(calls)
        clock.now += NEGATIVE_TTL_CAP_S - 1
        resolver.resolve(ACME)
        self.assertEqual(len(calls), first, "a miss re-queried inside its TTL")
        clock.now += 2
        resolver.resolve(ACME)
        self.assertGreater(len(calls), first)

    def test_invalidate_forces_a_re_resolution(self) -> None:
        calls = []

        def getter(key, project=None):
            calls.append(key)
            return FAKE_KEY

        resolver = VendorKeyResolver(getter=getter)
        resolver.resolve(ACME)
        resolver.invalidate(ACME.vendor_id)
        resolver.resolve(ACME)
        self.assertEqual(len(calls), 2)

    def test_cached_vendor_ids_never_touches_a_store(self) -> None:
        """``/health`` calls this; it must not be able to block."""
        def exploding(key, project=None):
            raise AssertionError("cached_vendor_ids resolved a secret")

        resolver = VendorKeyResolver(getter=exploding)
        self.assertEqual(resolver.cached_vendor_ids(), ())


class NoValueLeakTests(unittest.TestCase):
    def _resolver(self):
        return VendorKeyResolver(getter=lambda key, project=None: FAKE_KEY)

    def test_value_is_absent_from_repr(self) -> None:
        result = self._resolver().resolve(ACME)
        self.assertNotIn(FAKE_KEY, repr(result))
        self.assertIn("<set>", repr(result))

    def test_value_is_absent_from_log_records(self) -> None:
        import model_router.secrets as mod

        with self.assertLogs(mod.logger, level=logging.DEBUG) as captured:
            self._resolver().resolve(ACME)
        joined = "\n".join(captured.output)
        self.assertNotIn(FAKE_KEY, joined)
        # The NAME is logged, and names are not secrets.
        self.assertIn("acme_primary_key", joined)

    def test_value_is_absent_from_a_miss_message(self) -> None:
        def getter(key, project=None):
            # A resolver that (wrongly) put a value in its error text must not
            # be able to launder it through us.
            raise RuntimeError("upstream said: nope")

        result = VendorKeyResolver(getter=getter).resolve(ACME)
        self.assertNotIn(FAKE_KEY, result.problem or "")

    def test_no_subprocess_is_spawned_on_the_secrets_path(self) -> None:
        """The whole point of resolving in-process: a key on argv is readable
        from the process table, which is why the secrets primitive exists."""
        import subprocess

        original = subprocess.run
        subprocess.run = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
            AssertionError("the secrets path spawned a subprocess"),
        )
        self.addCleanup(setattr, subprocess, "run", original)
        self.assertEqual(self._resolver().resolve(ACME).key, FAKE_KEY)

    def test_the_secrets_module_does_not_import_subprocess(self) -> None:
        """Structural backstop for the test above: no import, no argv."""
        import ast
        from pathlib import Path

        import model_router.secrets as mod

        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        self.assertNotIn("subprocess", names)
        self.assertNotIn("os", names)


class AsyncResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_aresolve_runs_the_blocking_call_off_the_event_loop(self) -> None:
        """A blocking resolver must not stall concurrent work; that is what
        keeps ``/health`` answering while a store is wedged."""
        import time

        def slow(key, project=None):
            time.sleep(0.4)
            return FAKE_KEY

        resolver = VendorKeyResolver(getter=slow)
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        result = await resolver.aresolve(ACME)
        await task
        self.assertEqual(result.key, FAKE_KEY)
        self.assertGreater(
            ticks, 5,
            "the event loop was blocked while the key resolved",
        )

    async def test_a_cache_hit_avoids_the_thread_hop(self) -> None:
        resolver = VendorKeyResolver(getter=lambda key, project=None: FAKE_KEY)
        await resolver.aresolve(ACME)
        second = await resolver.aresolve(ACME)
        self.assertEqual(second.state, "cached")


class SecretScopeTests(unittest.TestCase):
    """R5b — the daemon must SAY when its scope cannot reach the keychain.

    The 2026-09-10 shape: a boot unit runs in the state root, which is not a
    registered project, so ``agent_secrets`` skipped tier 1 (the hub's ``/env``
    route, the only route to an OS-keychain key) and every vendor request
    answered "no key found" — while ``/health`` reported the vendor as present
    with an empty key cache, which reads like "not configured yet".
    """

    def test_the_effective_scope_is_the_install_root_when_nothing_is_pinned(
        self,
    ) -> None:
        """v0.2.95 (owner ruling 2026-09-17): the DEFAULT scope is this
        install's orchestrator root, because a shared keychain key is only
        reachable through the hub's per-project route and the root is the one
        project that is always registered.

        Until this, the default was ``Path.cwd()`` — which for a boot unit is
        the state root, unregistered, so tier 1 never ran at all. The full
        decision (shared by default, project overrides, marker honoured) is
        driven end-to-end in ``test_v0295_gateway_shared_secret_scope.py``.
        """
        resolver = VendorKeyResolver(
            getter=lambda key, project=None: FAKE_KEY,
            install_root=lambda: "/opt/vco-clone",
        )
        self.assertEqual(resolver.effective_project, "/opt/vco-clone")

    def test_the_effective_scope_is_the_cwd_when_no_clone_resolves(self) -> None:
        """The pre-v0.2.95 behaviour survives as the LAST resort, for a machine
        with no orchestrator clone — reported as such, not as a scope."""
        resolver = VendorKeyResolver(
            getter=lambda key, project=None: FAKE_KEY, install_root=lambda: None,
        )
        self.assertEqual(resolver.effective_project, str(Path.cwd()))

    def test_a_pin_is_the_effective_scope(self) -> None:
        resolver = VendorKeyResolver(project="/opt/vco", getter=lambda *a, **k: "")
        self.assertEqual(resolver.effective_project, "/opt/vco")

    def test_an_unprobed_scope_reports_unknown_not_false(self) -> None:
        """"Not probed" and "does not resolve" are different claims; reporting
        the first as the second is the "a probe that cannot run reads as
        absence" defect."""
        status = VendorKeyResolver(getter=lambda *a, **k: "").scope_status()
        self.assertIsNone(status.resolvable)
        self.assertEqual(status.to_dict()["resolvable"], None)

    def test_a_scope_that_maps_to_a_project_id_is_resolvable(self) -> None:
        resolver = VendorKeyResolver(
            project="/opt/vco", getter=lambda *a, **k: "",
            scope_prober=lambda arg: "project-uuid-1",
        )
        status = resolver.probe_scope()
        self.assertIs(status.resolvable, True)
        self.assertIsNone(status.reason)
        self.assertEqual(resolver.scope_status().project, "/opt/vco")

    def test_an_unresolvable_scope_names_the_consequence(self) -> None:
        def prober(arg):
            raise LookupError(f"no project registered at path: {arg}")

        status = VendorKeyResolver(
            project="/home/u/.vct", getter=lambda *a, **k: "", scope_prober=prober,
        ).probe_scope()

        self.assertIs(status.resolvable, False)
        self.assertIn("keychain", status.reason or "")
        self.assertIn("VCT_MODEL_GATEWAY_SECRET_PROJECT", status.reason or "")

    def test_the_scope_probe_is_cached_and_re_probed_after_the_negative_ttl(
        self,
    ) -> None:
        clock = _Clock()
        probes: list[str] = []

        def prober(arg):
            probes.append(arg)
            raise LookupError("hub unreachable")

        resolver = VendorKeyResolver(
            project="/opt/vco", getter=lambda *a, **k: "", scope_prober=prober,
            clock=clock, ttl_s=3600,
        )
        resolver.probe_scope()
        resolver.probe_scope()
        self.assertEqual(len(probes), 1, "a cached verdict must not re-probe")

        clock.now += NEGATIVE_TTL_CAP_S + 1
        resolver.probe_scope()
        self.assertEqual(
            len(probes), 2,
            "a hub that comes up later must be able to flip the verdict",
        )

    def test_a_key_miss_diagnoses_the_scope_only_when_the_daemon_asked(self) -> None:
        """A library that reached the hub behind its caller's back would make
        every test (and every embedder) do network I/O."""
        probes: list[str] = []

        quiet = VendorKeyResolver(
            getter=_absent, scope_prober=lambda arg: probes.append(arg) or "id",
        )
        quiet.resolve(ACME)
        self.assertEqual(probes, [])

        daemon = VendorKeyResolver(
            getter=_absent, probe_scope_on_miss=True,
            scope_prober=lambda arg: probes.append(arg) or "id",
        )
        daemon.resolve(ACME)
        self.assertEqual(len(probes), 1)

    def test_the_miss_message_says_the_scope_is_the_reason(self) -> None:
        def prober(arg):
            raise LookupError("no project registered at path")

        result = VendorKeyResolver(
            project="/home/u/.vct", getter=_absent, probe_scope_on_miss=True,
            scope_prober=prober,
        ).resolve(ACME)

        self.assertIn("THE SCOPE ITSELF DOES NOT RESOLVE", result.problem or "")
        self.assertNotIn(FAKE_KEY, result.problem or "")

    def test_a_resolvable_scope_adds_no_noise_to_a_miss(self) -> None:
        """LEAVE-ALONE: when the scope is fine, the miss message must not
        blame it."""
        result = VendorKeyResolver(
            project="/opt/vco", getter=_absent, probe_scope_on_miss=True,
            scope_prober=lambda arg: "project-uuid-1",
        ).resolve(ACME)

        self.assertNotIn("THE SCOPE ITSELF", result.problem or "")

    def test_a_successful_resolution_costs_no_scope_probe(self) -> None:
        probes: list[str] = []
        resolver = VendorKeyResolver(
            getter=lambda key, project=None: FAKE_KEY, probe_scope_on_miss=True,
            scope_prober=lambda arg: probes.append(arg) or "id",
        )
        self.assertEqual(resolver.resolve(ACME).key, FAKE_KEY)
        self.assertEqual(probes, [])

    def test_the_scope_status_never_leaks_a_value(self) -> None:
        def prober(arg):
            raise LookupError(f"denied while holding {FAKE_KEY[:0]}")

        status = VendorKeyResolver(
            project="/opt/vco", getter=lambda *a, **k: FAKE_KEY, scope_prober=prober,
        ).probe_scope()
        self.assertNotIn(FAKE_KEY, repr(status))
        self.assertNotIn(FAKE_KEY, str(status.to_dict()))


class ShippedVendorTests(unittest.TestCase):
    def test_the_shipped_vendor_declares_more_than_one_key_name(self) -> None:
        """Users store the same subscription key under different names; the
        first that resolves wins and the miss message names them all."""
        self.assertGreaterEqual(len(VENDORS["zai"].secret_keys), 1)
        for name in VENDORS["zai"].secret_keys:
            self.assertRegex(name, r"^[a-z0-9_]+$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
