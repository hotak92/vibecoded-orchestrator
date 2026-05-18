# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.16 install-manifest uuid_scheme marker.

Validates addendum F from `v0.2.16-candidates-2026-05-18.md`:

  * Every call to `_write_install_manifest` (fresh install / --update /
    lightweight / wizard) MUST write `uuid_scheme = "v2"` into
    `state/install-manifest.json`.
  * The resulting file MUST be valid JSON.
  * Pre-v0.2.16 manifests (without `uuid_scheme`) are not regressed
    when re-written: the new run upgrades the field to "v2"
    unconditionally rather than copying the absent / "v1" prior value.

The uuid_scheme marker is what a future code-graph migration tool
reads to decide whether existing collections need `--force-recreate`
(they do, when keyed on the pre-v0.2.16 `(project, full_name)` UUID
namespace instead of the v0.2.16 `(project, file_path, full_name)`
namespace).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402


class _ProjectRootFixture:
    """Swap install.PROJECT_ROOT to a tempdir + ensure state/logs/.

    Mirrors the fixture in tests/test_install_lightweight.py — keeps
    the helper local rather than importing from a sibling test file
    (sibling-test imports are fragile across pytest discovery modes).
    """

    def __init__(self):
        self._tmp = None
        self._orig_root = None
        self.root: Path = Path()

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "state" / "logs").mkdir(parents=True)
        self._orig_root = install.PROJECT_ROOT
        install.PROJECT_ROOT = self.root
        install._PENDING_EVENTS.clear()
        return self

    def __exit__(self, *_):
        install.PROJECT_ROOT = self._orig_root
        self._tmp.cleanup()


def _make_args(**overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for _write_install_manifest.

    Only the fields the writer reads via getattr need to be populated;
    everything else defaults to absent → getattr returns the configured
    default. We populate the most common subset to keep noise low.
    """
    defaults = {
        "no_joern": False,
        "no_agents": False,
        "no_skills": False,
        "no_hooks": False,
        "no_containers": False,
        "skip_models": False,
        "no_compile": False,
        "no_lean_ctx": False,
        "cpu_only": False,
        "gpu": False,
        "low_resource": False,
        "update": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _read_manifest(root: Path) -> dict:
    path = root / "state" / "install-manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestManifestUuidScheme(unittest.TestCase):
    """uuid_scheme = "v2" is written on every install path."""

    def test_fresh_install_writes_v2(self):
        with _ProjectRootFixture() as fix:
            install._write_install_manifest(
                None, _make_args(), install_method="install.py",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")

    def test_update_writes_v2(self):
        with _ProjectRootFixture() as fix:
            install._write_install_manifest(
                None, _make_args(update=True), install_method="install.py",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")
            # Sanity: install_method auto-flips to "update" because a prior
            # manifest existed (it doesn't here — but the field is still
            # populated correctly on first --update either way).
            self.assertIn("install_method", manifest)

    def test_lightweight_writes_v2(self):
        with _ProjectRootFixture() as fix:
            install._write_install_manifest(
                None, _make_args(), install_method="lightweight",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")
            self.assertEqual(manifest["install_method"], "lightweight")

    def test_wizard_writes_v2(self):
        with _ProjectRootFixture() as fix:
            install._write_install_manifest(
                None, _make_args(), install_method="wizard",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")
            self.assertEqual(manifest["install_method"], "wizard")

    def test_manifest_is_valid_json(self):
        """File on disk must round-trip through json.load without errors."""
        with _ProjectRootFixture() as fix:
            install._write_install_manifest(
                None, _make_args(), install_method="install.py",
            )
            path = fix.root / "state" / "install-manifest.json"
            self.assertTrue(path.is_file())
            # If json.load raised, this whole test would error — explicit
            # assertion documents the intent: the writer's manual key+value
            # build never produces malformed JSON.
            with path.open("r", encoding="utf-8") as f:
                parsed = json.load(f)
            self.assertIsInstance(parsed, dict)


class TestManifestUuidSchemeUpgrade(unittest.TestCase):
    """Pre-v0.2.16 manifests (no uuid_scheme) get upgraded to v2 on rewrite.

    The field is informative and "v1" is conveyed by absence (per
    addendum F): readers MUST treat the missing key as the implicit
    pre-v0.2.16 scheme. When a v0.2.16+ writer re-runs over such a
    manifest, the upgrade to "v2" is unconditional — we don't carry
    the absent/implicit-v1 value forward.
    """

    def test_writes_v2_when_prior_manifest_has_no_uuid_scheme(self):
        with _ProjectRootFixture() as fix:
            # Pre-v0.2.16-shaped manifest on disk.
            manifest_path = fix.root / "state" / "install-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps({
                    "schema_version":  1,
                    "installed":       True,
                    "installed_at":    "2026-01-01T00:00:00Z",
                    "version":         "0.2.13",
                    "install_path":    str(fix.root),
                    # NOTE: deliberately no uuid_scheme key — old install.
                }),
                encoding="utf-8",
            )
            # Re-write via --update path.
            install._write_install_manifest(
                None, _make_args(update=True), install_method="install.py",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")
            # installed_at should be preserved across the rewrite (existing
            # contract documented in _write_install_manifest's docstring).
            self.assertEqual(
                manifest["installed_at"], "2026-01-01T00:00:00Z",
            )

    def test_overrides_prior_uuid_scheme_field(self):
        """If a corrupt prior value (e.g. "v0") exists, we still write "v2".

        The writer constructs the dict from scratch each call rather than
        copying prior values for this field — guarantees forward-only
        evolution.
        """
        with _ProjectRootFixture() as fix:
            manifest_path = fix.root / "state" / "install-manifest.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps({
                    "schema_version":  1,
                    "installed":       True,
                    "installed_at":    "2026-01-01T00:00:00Z",
                    "version":         "0.2.13",
                    "install_path":    str(fix.root),
                    "uuid_scheme":     "v0-garbage",
                }),
                encoding="utf-8",
            )
            install._write_install_manifest(
                None, _make_args(update=True), install_method="install.py",
            )
            manifest = _read_manifest(fix.root)
            self.assertEqual(manifest["uuid_scheme"], "v2")


if __name__ == "__main__":
    unittest.main()
