# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for install.py container-runtime detection and dev-collection naming.

Fills coverage gaps not handled by test_install_shared_containers.py:

  * `_detect_container_runtime` — regression for H6 (podman-first universal
    preference). Order of `shutil.which` lookup must always check podman
    BEFORE docker.
  * `_derive_project_dev_name` — mirror of the existing KG-name tests, for
    the per-project `<X>_Development` naming used by adopt mode (commit
    98f962f / 0571422).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# _detect_container_runtime — podman-first ordering (H6 regression)
# ---------------------------------------------------------------------------


def _make_which(present: set[str]):
    """Return a fake shutil.which that resolves only `present` names."""
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


class DetectContainerRuntimeTests(unittest.TestCase):

    def test_prefers_podman_when_both_present(self):
        """Regression for H6: when both podman and docker work, podman wins."""
        with mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "podman")

    def test_falls_back_to_docker_if_podman_absent(self):
        with mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"docker"})), \
             mock.patch.object(install.subprocess, "run",
                               side_effect=_make_run_ok):
            self.assertEqual(install._detect_container_runtime(), "docker")

    def test_returns_empty_when_neither_present(self):
        with mock.patch.object(install.shutil, "which",
                               side_effect=_make_which(set())):
            self.assertEqual(install._detect_container_runtime(), "")

    def test_falls_back_to_docker_when_podman_version_fails(self):
        """podman is on PATH but `podman version` fails (e.g. machine not
        started). We must continue to docker rather than aborting.
        """
        calls: list[str] = []

        def fake_run(args, *_a, **_kw):
            calls.append(args[0])
            if args[0] == "podman":
                return _make_run_fail()
            return _make_run_ok()

        with mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run):
            self.assertEqual(install._detect_container_runtime(), "docker")
        self.assertEqual(calls, ["podman", "docker"],
                         "podman must be probed first, then docker")

    def test_handles_podman_oserror_gracefully(self):
        """A broken podman binary (OSError) must not crash the detector."""
        def fake_run(args, *_a, **_kw):
            if args[0] == "podman":
                raise OSError("Permission denied")
            return _make_run_ok()

        with mock.patch.object(install.shutil, "which",
                               side_effect=_make_which({"podman", "docker"})), \
             mock.patch.object(install.subprocess, "run", side_effect=fake_run):
            self.assertEqual(install._detect_container_runtime(), "docker")


# ---------------------------------------------------------------------------
# _derive_project_dev_name — mirrors KG name conversion (commit 0571422)
# ---------------------------------------------------------------------------


class DeriveProjectDevNameTests(unittest.TestCase):

    def test_simple_basename(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/myapp")),
            "Myapp_Development",
        )

    def test_basename_with_hyphens(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/vibecoded-orchestrator")),
            "VibecodedOrchestrator_Development",
        )

    def test_basename_with_underscores(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/test_install")),
            "TestInstall_Development",
        )

    def test_basename_with_special_chars_only_falls_back(self):
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/...")),
            "vct_Development",
        )

    def test_basename_starting_with_digit_falls_back(self):
        """`1foo` would yield `1foo_Development` — invalid in Weaviate
        (must start with a letter). Fallback prefix is required.
        """
        self.assertEqual(
            install._derive_project_dev_name(Path("/x/y/1foo")),
            "vct_Development",
        )

    def test_kg_and_dev_names_share_basename_pascal(self):
        """KG and Development names must share the same Pascal-cased prefix.

        Adopt mode relies on this: probing for `<X>_KnowledgeGraph` and
        `<X>_Development` together must use a single derived stem.
        """
        root = Path("/some/project/Hello-World_42")
        kg = install._derive_project_kg_name(root)
        dev = install._derive_project_dev_name(root)
        kg_prefix = kg[: -len("_KnowledgeGraph")]
        dev_prefix = dev[: -len("_Development")]
        self.assertEqual(kg_prefix, dev_prefix,
                         f"KG prefix {kg_prefix!r} != dev prefix {dev_prefix!r}")


if __name__ == "__main__":
    unittest.main()
