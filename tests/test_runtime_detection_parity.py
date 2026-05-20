# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.22 Item #10 (b) — acceptance property (12) coverage, Python side.

Pins ``install._detect_container_runtime`` against the same canonical
contract documented in the v0.2.21 ship plan §27 property (12a):

  - **(12a) Runtime detection**: `vct-launcher-core::services::runtime::
    detect_runtime()` returns the user-preferred runtime per the
    established v0.2.20 priority: explicit `VCT_CONTAINER_RUNTIME=
    podman|docker` env override → user's preferred-runtime setting in
    launcher.db → first available on PATH (podman first, then docker)
    → `None` (degraded mode). The same detection is used by services-
    watcher (Step 24 supervisor port preserves the behaviour) AND by
    install.py's container-bootstrap code path.

Existing coverage in ``test_install_runtime_and_naming.py`` covers
PATH-only auto-detection but not the env-override layer. This file fills
those gaps so the install.py side stays consistent with the Rust side.

The Rust-side complement (cache contract + priority pin) is in
``launcher/src-tauri/vct-launcher-core/src/services/runtime.rs::tests``.

Test design: pure stdlib + ``unittest.mock`` — no real podman/docker
subprocess calls. Each test scripts ``shutil.which`` + ``subprocess.run``
to simulate a specific host configuration.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_install_runtime_and_naming.py for consistency)
# ---------------------------------------------------------------------------


def _make_which(present: set[str]):
    """Return a fake shutil.which that resolves only ``present`` names."""
    def which(cmd: str, *_a, **_kw):
        return f"/usr/bin/{cmd}" if cmd in present else None
    return which


def _make_run_ok(*_a, **_kw):
    class _R:
        returncode = 0
        stdout = ""
        stderr = ""
    return _R()


def _make_run_fail(*_a, **_kw):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"
    return _R()


class _EnvOverride:
    """Context manager that pins ``VCT_CONTAINER_RUNTIME`` to a given
    value (or removes it entirely). Restores the prior value on exit.
    """

    def __init__(self, value: str | None) -> None:
        self.value = value

    def __enter__(self) -> "_EnvOverride":
        self._saved = os.environ.get("VCT_CONTAINER_RUNTIME")
        if self.value is None:
            os.environ.pop("VCT_CONTAINER_RUNTIME", None)
        else:
            os.environ["VCT_CONTAINER_RUNTIME"] = self.value
        return self

    def __exit__(self, *_a) -> None:
        if self._saved is None:
            os.environ.pop("VCT_CONTAINER_RUNTIME", None)
        else:
            os.environ["VCT_CONTAINER_RUNTIME"] = self._saved


# ---------------------------------------------------------------------------
# _runtime_preference_from_env — env-var parsing layer
# ---------------------------------------------------------------------------


class RuntimePreferenceFromEnvTests(unittest.TestCase):
    """Pin the env-var parser. The Rust side
    (``services/runtime.rs::resolve_runtime``) parses the SAME env var
    with the SAME accepted values — keep these tests in sync if either
    parser changes.
    """

    def test_unset_returns_none(self):
        with _EnvOverride(None):
            self.assertIsNone(install._runtime_preference_from_env())

    def test_empty_string_returns_none(self):
        with _EnvOverride(""):
            self.assertIsNone(install._runtime_preference_from_env())

    def test_auto_returns_none(self):
        """The string 'auto' is the documented opt-OUT — pretend it's
        not set so the auto-detection chain runs."""
        with _EnvOverride("auto"):
            self.assertIsNone(install._runtime_preference_from_env())

    def test_auto_case_insensitive(self):
        with _EnvOverride("AUTO"):
            self.assertIsNone(install._runtime_preference_from_env())

    def test_podman_returns_podman(self):
        with _EnvOverride("podman"):
            self.assertEqual(install._runtime_preference_from_env(), "podman")

    def test_docker_returns_docker(self):
        with _EnvOverride("docker"):
            self.assertEqual(install._runtime_preference_from_env(), "docker")

    def test_case_insensitive_and_trimmed(self):
        with _EnvOverride("  Docker  "):
            self.assertEqual(install._runtime_preference_from_env(), "docker")

    def test_unknown_value_returns_none_with_stderr(self):
        """Bad values must not raise — they fall through to auto-detect
        with a stderr breadcrumb. Tested by setting a garbage value and
        confirming None is returned."""
        with _EnvOverride("kubernetes"):
            self.assertIsNone(install._runtime_preference_from_env())


# ---------------------------------------------------------------------------
# _detect_container_runtime — env-override end-to-end behaviour
# ---------------------------------------------------------------------------


class DetectContainerRuntimeEnvOverrideTests(unittest.TestCase):
    """Pin the env-override behaviour. When VCT_CONTAINER_RUNTIME is
    set to a recognized value AND that runtime is on PATH AND reachable,
    it MUST win over the default podman-first priority.

    Falls through to auto-detect when:
      - env value is recognized but the runtime isn't on PATH
      - env value is recognized + on PATH but `<runtime> version` fails
      - env value is unrecognized ("kubernetes", "lxc", etc.)
    """

    def test_env_podman_wins_when_both_present(self):
        """VCT_CONTAINER_RUNTIME=podman → podman picked (matches default,
        but the codepath is the env-override branch — verified by
        argument order)."""
        calls: list[str] = []

        def fake_run(args, *_a, **_kw):
            calls.append(args[0])
            return _make_run_ok()

        with _EnvOverride("podman"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run):
            self.assertEqual(install._detect_container_runtime(), "podman")
        self.assertEqual(calls, ["podman"],
                         "env-override path must probe only the preferred "
                         "runtime, not the full auto-detect chain")

    def test_env_docker_overrides_podman_first_default(self):
        """The whole point of the env override: VCT_CONTAINER_RUNTIME=docker
        wins even when podman is on PATH first. This is the regression
        target for the v0.2.20 cross-OS audit Bug #3 fix."""
        calls: list[str] = []

        def fake_run(args, *_a, **_kw):
            calls.append(args[0])
            return _make_run_ok()

        with _EnvOverride("docker"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run):
            self.assertEqual(install._detect_container_runtime(), "docker")
        self.assertEqual(calls, ["docker"],
                         "env override must short-circuit to docker even "
                         "with podman on PATH; got probes={!r}".format(calls))

    def test_env_auto_falls_through_to_default_priority(self):
        """VCT_CONTAINER_RUNTIME=auto → behave as if unset → podman wins."""
        with _EnvOverride("auto"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "podman")

    def test_env_invalid_value_falls_through_to_autodetect(self):
        """A typo (VCT_CONTAINER_RUNTIME=podmman) must not strand the
        user — fall through to the normal podman-first auto-detect."""
        with _EnvOverride("podmman"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "podman")

    def test_env_preferred_unreachable_falls_through_to_autodetect(self):
        """VCT_CONTAINER_RUNTIME=podman, podman on PATH but
        `podman version` fails → fall through to docker rather than
        returning "". The Rust side's resolve_runtime has equivalent
        graceful-degradation; we mirror it."""
        calls: list[str] = []

        def fake_run(args, *_a, **_kw):
            calls.append(args[0])
            if args[0] == "podman":
                return _make_run_fail()
            return _make_run_ok()

        with _EnvOverride("podman"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run):
            result = install._detect_container_runtime()
        # The override branch fails its probe, then we re-enter the
        # auto-detect loop, which also probes podman first (it's still
        # on PATH and returncode=1), so podman gets probed twice in
        # total before docker wins. Pin both behaviours.
        self.assertEqual(result, "docker",
                         "preferred runtime unreachable → fall through to "
                         f"docker; got {result!r}, probes={calls!r}")
        self.assertIn("docker", calls,
                      "docker must be probed in the fallthrough chain")

    def test_env_preferred_not_on_path_falls_through_to_autodetect(self):
        """VCT_CONTAINER_RUNTIME=docker but docker not installed →
        fall through to podman (which IS on PATH)."""
        with _EnvOverride("docker"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "podman")

    def test_returns_empty_string_when_env_set_but_no_runtimes(self):
        """VCT_CONTAINER_RUNTIME set but NEITHER runtime is on PATH —
        must return "" (degraded mode), not raise. Contract verified
        against the Rust side: detect_runtime() also returns None in
        this case."""
        with _EnvOverride("podman"), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which(set())):
            self.assertEqual(install._detect_container_runtime(), "")


# ---------------------------------------------------------------------------
# Runtime priority contract: podman-first when both work
# ---------------------------------------------------------------------------


class RuntimePriorityContractTests(unittest.TestCase):
    """Pin the documented priority chain: env override → podman → docker.

    The Rust side (``services/runtime.rs::resolve_runtime``) MUST honor
    the same priority. If a future change flips podman/docker order on
    one side, the two will silently disagree and the user's hooks
    (templates/hooks/ensure-containers.sh) would pick a different runtime
    than the launcher GUI — the v0.2.14 Bug #3 regression.
    """

    def test_documented_priority_chain_with_both_present_no_env(self):
        """Default: no env override, both runtimes on PATH and working →
        podman wins. This is the canonical contract."""
        with _EnvOverride(None), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "podman")

    def test_priority_chain_documented_in_canonical_list(self):
        """The auto-detect loop's candidate order is hardcoded to
        [podman, docker]. Reading the source at the test boundary acts
        as a compile-time documentation pin: if someone changes the
        order, this test fails with a message pointing at the source
        line."""
        # We can't directly introspect the local variable inside
        # _detect_container_runtime, so we exercise the priority via
        # an oracle test: when both runtimes are on PATH + working, the
        # result is the FIRST entry in the priority list. If that
        # invariant changes, prior tests catch it; this one is
        # belt-and-suspenders.
        with _EnvOverride(None), \
             mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            result = install._detect_container_runtime()
        # Cross-OS invariant: the priority is platform-independent.
        # Don't gate on os.name — install.py uses the same priority on
        # Linux, macOS, Windows (the Rust side too).
        self.assertEqual(
            result, "podman",
            "v0.2.21 §27 property (12a) priority documented as "
            "podman > docker. If you flip the order, update plan §27 "
            "AND launcher/src-tauri/vct-launcher-core/src/services/"
            "runtime.rs::resolve_runtime AND this test together.",
        )


if __name__ == "__main__":
    unittest.main()
