"""Tests for vco_lib.containers — canonical container-name registry.

Guards the v0.2.15 fix for the "maintainer-machine leak": install.py +
hooks + MCP servers used to hardcode `weaviate_claude` /
`ollama_claude` / `code_embed_claude` as the fallback container name
to look for or restart. Those names only ever existed on the
maintainer's own pre-VCO machine. VCO has only ever shipped:

  * v0.1.x: `weaviate` / `ollama` / `code_embed` (unprefixed)
  * v0.2.x: `vco_weaviate` / `vco_ollama` / `vco_code_embed`
            (with `vct_code_embed` as a v0.2.x transitional alias)

These tests pin the canonical names, the alias order, and the
runtime-probe behaviour against accidental regression.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.containers import (  # noqa: E402
    CANONICAL_CONTAINERS,
    HISTORICAL_ALIASES,
    UnknownServiceError,
    all_known_names,
    canonical_name,
    find_existing_container,
)


# ---------------------------------------------------------------------------
# Canonical-name pinning
# ---------------------------------------------------------------------------


class CanonicalNameTests(unittest.TestCase):
    def test_weaviate_canonical_is_vco_weaviate(self):
        self.assertEqual(canonical_name("weaviate"), "vco_weaviate")

    def test_ollama_canonical_is_vco_ollama(self):
        self.assertEqual(canonical_name("ollama"), "vco_ollama")

    def test_code_embed_canonical_is_vco_code_embed(self):
        """v0.2.15 rename: vct_code_embed -> vco_code_embed."""
        self.assertEqual(canonical_name("code_embed"), "vco_code_embed")

    def test_unknown_service_raises(self):
        with self.assertRaises(UnknownServiceError):
            canonical_name("does_not_exist")

    def test_canonical_dict_keys_match_alias_keys(self):
        """If we forget to add a service to HISTORICAL_ALIASES (or
        vice-versa), the two dicts will drift and runtime callers will
        crash with a KeyError. Pin them together here."""
        self.assertEqual(
            set(CANONICAL_CONTAINERS.keys()),
            set(HISTORICAL_ALIASES.keys()),
            "CANONICAL_CONTAINERS and HISTORICAL_ALIASES disagree on the "
            "set of known services",
        )


# ---------------------------------------------------------------------------
# Historical-aliases content
# ---------------------------------------------------------------------------


class HistoricalAliasesTests(unittest.TestCase):
    def test_weaviate_aliases_include_maintainer_legacy(self):
        """The maintainer-machine name `weaviate_claude` must stay in
        the alias list — some pre-VCO installs still have it."""
        self.assertIn("weaviate_claude", HISTORICAL_ALIASES["weaviate"])

    def test_weaviate_aliases_include_v01x_unprefixed(self):
        """v0.1.x shipped the unprefixed `weaviate`."""
        self.assertIn("weaviate", HISTORICAL_ALIASES["weaviate"])

    def test_ollama_aliases_include_maintainer_legacy(self):
        self.assertIn("ollama_claude", HISTORICAL_ALIASES["ollama"])

    def test_ollama_aliases_include_v01x_unprefixed(self):
        self.assertIn("ollama", HISTORICAL_ALIASES["ollama"])

    def test_code_embed_aliases_include_v02x_vct_prefix(self):
        """v0.2.x transitional name (pre-v0.2.15 rename). Must remain
        in aliases so existing installs migrate cleanly."""
        self.assertIn("vct_code_embed", HISTORICAL_ALIASES["code_embed"])

    def test_code_embed_aliases_include_v01x_unprefixed(self):
        self.assertIn("code_embed", HISTORICAL_ALIASES["code_embed"])

    def test_code_embed_aliases_include_maintainer_legacy(self):
        self.assertIn(
            "code_embed_claude", HISTORICAL_ALIASES["code_embed"],
        )

    def test_canonical_name_never_in_historical_aliases(self):
        """`all_known_names()` prepends the canonical. If it also appears
        in the alias list, we'd waste a probe (and the dedup logic in
        all_known_names papers over it but it's a sign of registry
        confusion)."""
        for service, canonical in CANONICAL_CONTAINERS.items():
            self.assertNotIn(
                canonical, HISTORICAL_ALIASES[service],
                f"{canonical!r} is canonical for {service!r} and should "
                "not be duplicated in HISTORICAL_ALIASES",
            )

    def test_code_embed_aliases_ordering_most_recent_first(self):
        """Per the registry contract: aliases sorted most-recent-first
        so find_existing_container prefers the freshest legacy over the
        deepest one. For code_embed: vct (v0.2.x) -> unprefixed (v0.1.x)
        -> _claude (maintainer-era)."""
        aliases = HISTORICAL_ALIASES["code_embed"]
        # vct_code_embed should appear before code_embed_claude.
        self.assertLess(
            aliases.index("vct_code_embed"),
            aliases.index("code_embed_claude"),
            "v0.2.x vct_code_embed must rank before maintainer-era "
            "code_embed_claude in HISTORICAL_ALIASES",
        )


# ---------------------------------------------------------------------------
# all_known_names() ordering + dedup
# ---------------------------------------------------------------------------


class AllKnownNamesTests(unittest.TestCase):
    def test_canonical_appears_first(self):
        for service, canonical in CANONICAL_CONTAINERS.items():
            names = all_known_names(service)
            self.assertEqual(
                names[0], canonical,
                f"all_known_names({service!r}) should start with the "
                f"canonical name {canonical!r}, got {names[0]!r}",
            )

    def test_weaviate_full_ordering(self):
        self.assertEqual(
            all_known_names("weaviate"),
            ["vco_weaviate", "weaviate", "weaviate_claude"],
        )

    def test_code_embed_full_ordering(self):
        self.assertEqual(
            all_known_names("code_embed"),
            [
                "vco_code_embed",       # canonical (v0.2.15)
                "vct_code_embed",        # v0.2.x transitional
                "code_embed",            # v0.1.x unprefixed
                "code_embed_claude",     # maintainer-era pre-VCO
            ],
        )

    def test_dedup_preserves_order(self):
        """Defensive: if someone adds the canonical to HISTORICAL_ALIASES
        by mistake, all_known_names should drop the dupe without
        reordering the rest."""
        # We can't trivially monkey-patch the module dict without leaking
        # into other tests, so just sanity-check the dedup runs on the
        # actual data: every list element is unique.
        for service in CANONICAL_CONTAINERS:
            names = all_known_names(service)
            self.assertEqual(
                len(names), len(set(names)),
                f"all_known_names({service!r}) has duplicates: {names}",
            )

    def test_unknown_service_raises(self):
        with self.assertRaises(UnknownServiceError):
            all_known_names("not_a_service")


# ---------------------------------------------------------------------------
# find_existing_container() behaviour
# ---------------------------------------------------------------------------


class FindExistingContainerTests(unittest.TestCase):
    def test_unknown_service_raises_not_silent_none(self):
        """Typos in the service name should fail loudly — not silently
        return None like a hostile host."""
        with self.assertRaises(UnknownServiceError):
            find_existing_container("not_a_service")

    def test_returns_none_when_runtime_missing(self):
        """When neither podman nor docker is on PATH, return None."""
        with patch("vco_lib.containers.shutil.which", return_value=None):
            # Override env so VCT_CONTAINER_RUNTIME doesn't bypass the
            # shutil.which path.
            with patch.dict(os.environ, {"VCT_CONTAINER_RUNTIME": "auto"}):
                self.assertIsNone(find_existing_container("weaviate"))

    def test_returns_none_when_no_matching_container(self):
        """Runtime is present but every probe returns non-zero (no
        container by that name exists) → return None."""
        # Fake runtime is on PATH.
        def fake_which(name):
            return f"/fake/{name}" if name == "podman" else None

        # Every subprocess.run returns rc != 0 (no container exists).
        class FakeCompleted:
            returncode = 1

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run",
                return_value=FakeCompleted(),
            ):
                with patch.dict(
                    os.environ,
                    {"VCT_CONTAINER_RUNTIME": "podman"},
                ):
                    self.assertIsNone(find_existing_container("weaviate"))

    def test_returns_canonical_when_canonical_exists(self):
        """When the canonical container exists, it wins over aliases."""
        def fake_which(name):
            return f"/fake/{name}" if name == "podman" else None

        class FakeCompleted:
            def __init__(self, rc):
                self.returncode = rc

        # First probe (vco_weaviate) returns 0; subsequent should not
        # be reached. We verify by failing if anything except the first
        # is queried.
        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            # cmd is [bin, "container", "exists", name]
            name = cmd[-1]
            if name == "vco_weaviate":
                return FakeCompleted(0)
            return FakeCompleted(1)

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": "podman"},
                ):
                    result = find_existing_container("weaviate")
        self.assertEqual(result, "vco_weaviate")
        self.assertEqual(
            call_count["n"], 1,
            "find_existing_container kept probing after canonical hit",
        )

    def test_falls_through_to_legacy_alias(self):
        """When only a legacy alias exists, return that alias."""
        def fake_which(name):
            return f"/fake/{name}" if name == "podman" else None

        class FakeCompleted:
            def __init__(self, rc):
                self.returncode = rc

        def fake_run(cmd, **kwargs):
            name = cmd[-1]
            # Only `weaviate_claude` exists on this host.
            return FakeCompleted(0 if name == "weaviate_claude" else 1)

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": "podman"},
                ):
                    result = find_existing_container("weaviate")
        self.assertEqual(result, "weaviate_claude")

    def test_real_host_finds_vco_weaviate_if_present(self):
        """Smoke test against the real host. Skipped if podman is
        missing, so this still passes in container-less CI."""
        if shutil.which("podman") is None:
            self.skipTest("podman not on PATH")
        # Don't assert a specific name — the result depends on what
        # the developer has installed. Just verify the call works AND
        # returns either a recognised name or None.
        result = find_existing_container("weaviate")
        if result is not None:
            self.assertIn(result, all_known_names("weaviate"))


# ---------------------------------------------------------------------------
# Runtime-selection contract
# ---------------------------------------------------------------------------


class RuntimeSelectionTests(unittest.TestCase):
    """The runtime selection behaviour matches install.py's contract:
    VCT_CONTAINER_RUNTIME wins, "auto" defers to caller, unset = caller,
    unknown values fall through to caller. Pinned so the in-process
    helper can't drift from install.py."""

    def test_env_var_podman_overrides_docker_default(self):
        # When the env says podman and shutil reports podman present,
        # the probe should go through podman.
        def fake_which(name):
            return f"/fake/{name}" if name in ("podman", "docker") else None

        seen_bins: list[str] = []

        class FakeCompleted:
            returncode = 1

        def fake_run(cmd, **kwargs):
            seen_bins.append(cmd[0])
            return FakeCompleted()

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": "podman"},
                ):
                    find_existing_container("weaviate", runtime="docker")

        # All probes must have used the podman binary, not docker.
        # `_resolve_runtime` returns the bare runtime name (matching how
        # install.py invokes runtimes — via $PATH lookup, not full path).
        self.assertTrue(
            seen_bins,
            "no subprocess invocations recorded — fake_run never fired",
        )
        self.assertTrue(
            all(b == "podman" for b in seen_bins),
            f"VCT_CONTAINER_RUNTIME=podman did not override "
            f"runtime='docker'; probes ran via {seen_bins}",
        )

    def test_env_var_auto_uses_caller_default(self):
        # auto = no preference = caller's `runtime` argument wins.
        def fake_which(name):
            return f"/fake/{name}" if name in ("podman", "docker") else None

        seen_bins: list[str] = []

        class FakeCompleted:
            returncode = 1

        def fake_run(cmd, **kwargs):
            seen_bins.append(cmd[0])
            return FakeCompleted()

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": "auto"},
                ):
                    find_existing_container("weaviate", runtime="docker")

        self.assertTrue(
            all(b == "docker" for b in seen_bins),
            f"VCT_CONTAINER_RUNTIME=auto should have deferred to "
            f"caller's runtime='docker'; probes ran via {seen_bins}",
        )


if __name__ == "__main__":
    unittest.main()
