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
import subprocess
import sys
import tempfile
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
    classify_container_probe,
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

        # Every subprocess.run returns the "no such container" failure
        # both runtimes print for a name that is absent.
        class FakeCompleted:
            returncode = 1
            stderr = "Error: no such container vco_weaviate"
            stdout = ""

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
            def __init__(self, rc, stderr=""):
                self.returncode = rc
                self.stderr = stderr
                self.stdout = ""

        # First probe (vco_weaviate) returns 0; subsequent should not
        # be reached. We verify by failing if anything except the first
        # is queried.
        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            # cmd is [bin, "container", "inspect", "--format", fmt, name]
            name = cmd[-1]
            if name == "vco_weaviate":
                return FakeCompleted(0)
            return FakeCompleted(1, f"Error: no such container {name}")

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
            def __init__(self, rc, stderr=""):
                self.returncode = rc
                self.stderr = stderr
                self.stdout = ""

        def fake_run(cmd, **kwargs):
            name = cmd[-1]
            # Only `weaviate_claude` exists on this host.
            if name == "weaviate_claude":
                return FakeCompleted(0)
            return FakeCompleted(1, f"Error: no such container {name}")

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
# Container-inspect probe (v0.2.97): Docker has NO `container exists`
# subcommand, so the old probe answered "not found" for every Docker
# lookup. The probe is now `container inspect --format {{.Name}}`, with a
# tri-state classifier (`classify_container_probe`).
# ---------------------------------------------------------------------------


class ContainerInspectProbeTests(unittest.TestCase):
    """The existence probe must work on BOTH runtimes.

    The fake runner below implements `container inspect` and REFUSES
    `container exists` (it fails the test if the source ever issues that
    subcommand) — the exact shape of Docker's CLI, where the old probe
    silently returned "not found" for every lookup."""

    def _run_lookup(self, runtime, responses):
        """`find_existing_container("weaviate")` pinned to `runtime`,
        with `responses` mapping container name → (rc, stderr, stdout).
        Names absent from the map are probed with rc 1 / empty output.
        Returns (result, [argv of every probe])."""
        argvs: list[list[str]] = []

        def fake_which(name):
            return f"/fake/{name}" if name in ("podman", "docker") else None

        def fake_run(cmd, **kwargs):
            argvs.append(list(cmd))
            if cmd[1:3] != ["container", "inspect"]:
                self.fail(
                    f"probe must be '<runtime> container inspect' (works on "
                    f"docker AND podman), got: {cmd!r}"
                )
            rc, stderr, stdout = responses.get(cmd[-1], (1, "", ""))
            return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": runtime},
                ):
                    result = find_existing_container("weaviate")
        return result, argvs

    def test_docker_finds_container_via_inspect(self):
        result, argvs = self._run_lookup(
            "docker", {"vco_weaviate": (0, "", "/vco_weaviate\n")},
        )
        self.assertEqual(result, "vco_weaviate")
        self.assertEqual(argvs[0][0], "docker")

    def test_docker_not_found_probes_every_alias(self):
        """Docker's 'Error: No such container: <name>' phrasing is a
        POSITIVE not-found, so the search falls through the whole alias
        list and answers None."""
        result, argvs = self._run_lookup(
            "docker",
            {name: (1, f"Error: No such container: {name}", "")
             for name in all_known_names("weaviate")},
        )
        self.assertIsNone(result)
        self.assertEqual(
            [a[-1] for a in argvs], all_known_names("weaviate"),
        )

    def test_docker_daemon_error_is_soft_failed_not_recorded_not_found(self):
        """A non-'no such' failure (daemon down) is an ERROR: the lookup
        soft-fails to None (never claims found), and the classifier
        keeps it apart from not_found."""
        daemon_down = (
            1,
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
            "Is the docker daemon running?",
            "",
        )
        result, argvs = self._run_lookup(
            "docker",
            {name: daemon_down for name in all_known_names("weaviate")},
        )
        self.assertIsNone(result)
        self.assertTrue(argvs, "no probe ran")
        self.assertEqual(
            classify_container_probe(
                subprocess.CompletedProcess(["docker"], 1, "", daemon_down[1])
            ),
            "error",
        )

    def test_podman_not_found_phrasing_classified_not_found(self):
        result, argvs = self._run_lookup(
            "podman",
            {name: (1, f"Error: no such container {name}", "")
             for name in all_known_names("weaviate")},
        )
        self.assertIsNone(result)
        self.assertEqual(
            [a[-1] for a in argvs], all_known_names("weaviate"),
        )

    def test_podman_finds_container_via_inspect(self):
        result, _ = self._run_lookup(
            "podman", {"vco_weaviate": (0, "", "/vco_weaviate\n")},
        )
        self.assertEqual(result, "vco_weaviate")


class ClassifyContainerProbeTests(unittest.TestCase):
    """The tri-state classifier, pinned on both runtimes' phrasings."""

    def _res(self, rc, stderr="", stdout=""):
        return subprocess.CompletedProcess(["runtime"], rc, stdout, stderr)

    def test_exit_zero_is_exists(self):
        for stderr in ("", "some warning"):
            self.assertEqual(
                classify_container_probe(self._res(0, stderr)), "exists",
            )

    def test_docker_no_such_container_is_not_found(self):
        self.assertEqual(
            classify_container_probe(self._res(
                1, "Error: No such container: vco_weaviate",
            )),
            "not_found",
        )

    def test_podman_no_such_container_is_not_found(self):
        self.assertEqual(
            classify_container_probe(self._res(
                1, "Error: no such container vco_weaviate",
            )),
            "not_found",
        )

    def test_no_such_object_is_not_found(self):
        """API-level phrasing (podman remote / older docker)."""
        self.assertEqual(
            classify_container_probe(self._res(1, "Error: no such object")),
            "not_found",
        )

    def test_daemon_down_is_error_not_not_found(self):
        for stderr in (
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
            "Error: failed to connect: dial unix /run/podman/podman.sock: "
            "connect: no such file or directory — note: 'no such file' here "
            "is the SOCKET, not a container, and must stay an error",
            "",
        ):
            self.assertEqual(
                classify_container_probe(self._res(1, stderr)), "error",
                f"stderr {stderr!r} must classify as error",
            )


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
            stderr = "Error: no such container"
            stdout = ""

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
            stderr = "Error: no such container"
            stdout = ""

        def fake_run(cmd, **kwargs):
            seen_bins.append(cmd[0])
            return FakeCompleted()

        with patch("vco_lib.containers.shutil.which", side_effect=fake_which):
            with patch(
                "vco_lib.containers.subprocess.run", side_effect=fake_run,
            ):
                with patch.dict(
                    os.environ, {"VCT_CONTAINER_RUNTIME": "auto"},
                ), tempfile.TemporaryDirectory() as root:
                    # An install root with no runtime.txt: nothing pins.
                    find_existing_container("weaviate", runtime="docker", install_root=Path(root))

        self.assertTrue(
            all(b == "docker" for b in seen_bins),
            f"VCT_CONTAINER_RUNTIME=auto should have deferred to "
            f"caller's runtime='docker'; probes ran via {seen_bins}",
        )



class PinnedRuntimeLookupTests(unittest.TestCase):
    """Review round 7: a container lookup follows THE pin rule
    (`containers.runtime_pin`: VCT_CONTAINER_RUNTIME → runtime.txt → auto).
    A pinned runtime that is missing or down means "not found" — never a
    container of the OTHER runtime, whose containers sit on other volumes.
    Unpinned, the caller's runtime is tried and the other one only when it is
    not installed (auto-detection keeps its fallback)."""

    def _lookup(self, *, env_runtime, recorded, on_path, exists=(),
                miss_stderr="Error: no such container {name}"):
        """Run the lookup with `env_runtime` (None = unset), a scratch install
        root whose runtime.txt records `recorded` (None = no file), `on_path`
        binaries, and containers `exists` (name set) answering on ANY
        runtime. `miss_stderr` is the failure output for absent names (the
        default is the shared "no such container" miss; the daemon-down test
        passes a socket error instead). Returns (result, the binaries the
        probes ran)."""
        seen: list[str] = []

        class Done:
            def __init__(self, rc, stderr=""):
                self.returncode = rc
                self.stderr = stderr
                self.stdout = ""

        def fake_run(cmd, **kwargs):
            seen.append(cmd[0])
            if cmd[-1] in exists:
                return Done(0)
            return Done(1, miss_stderr.format(name=cmd[-1]))

        env = {k: v for k, v in os.environ.items() if k != "VCT_CONTAINER_RUNTIME"}
        if env_runtime is not None:
            env["VCT_CONTAINER_RUNTIME"] = env_runtime
        with tempfile.TemporaryDirectory() as root:
            if recorded is not None:
                txt = Path(root) / "state" / "install" / "runtime.txt"
                txt.parent.mkdir(parents=True)
                txt.write_text(recorded + "\n", encoding="utf-8")
            with patch("vco_lib.containers.shutil.which",
                       side_effect=lambda n: f"/fake/{n}" if n in on_path else None), \
                    patch("vco_lib.containers.subprocess.run", side_effect=fake_run), \
                    patch.dict(os.environ, env, clear=True):
                result = find_existing_container("weaviate", install_root=Path(root))
        return result, seen

    def test_an_env_pin_to_a_missing_runtime_never_asks_the_other(self):
        result, seen = self._lookup(env_runtime="podman", recorded=None, on_path={"docker"},
                                    exists={"vco_weaviate"})
        self.assertIsNone(result)
        self.assertEqual(seen, [], "the unpinned runtime was asked")

    def test_a_runtime_txt_pin_to_a_missing_runtime_never_asks_the_other(self):
        result, seen = self._lookup(env_runtime=None, recorded="docker", on_path={"podman"},
                                    exists={"vco_weaviate"})
        self.assertIsNone(result)
        self.assertEqual(seen, [], "the unpinned runtime was asked")

    def test_a_pinned_runtime_that_is_down_finds_nothing_and_asks_only_itself(self):
        result, seen = self._lookup(
            env_runtime=None, recorded="docker", on_path={"podman", "docker"},
            exists=(),
            # Daemon DOWN (socket refused), not a per-name miss — the
            # probe ERROR path, which must soft-fail the same way.
            miss_stderr="Cannot connect to the Docker daemon at "
                        "unix:///var/run/docker.sock. Is the docker daemon running?",
        )
        self.assertIsNone(result)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"docker"}, seen)

    def test_the_env_pin_wins_over_runtime_txt(self):
        result, seen = self._lookup(env_runtime="podman", recorded="docker",
                                    on_path={"podman", "docker"}, exists={"vco_weaviate"})
        self.assertEqual(result, "vco_weaviate")
        self.assertEqual(set(seen), {"podman"})

    def test_unpinned_auto_detection_keeps_its_fallback(self):
        result, seen = self._lookup(env_runtime="auto", recorded=None, on_path={"docker"},
                                    exists={"vco_weaviate"})
        self.assertEqual(result, "vco_weaviate")
        self.assertEqual(set(seen), {"docker"})


if __name__ == "__main__":
    unittest.main()
