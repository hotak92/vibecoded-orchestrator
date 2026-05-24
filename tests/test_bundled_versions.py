# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Unit tests for `vco_lib.bundled_versions` (Phase 0,
diagrams-integration plan 2026-05-24).

Pins the Python loader against the real on-disk
`bundled_mcp_versions.toml` plus exercises the
missing-file / malformed-toml / forward-compat branches with tempfiles.
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import bundled_versions  # noqa: E402


class LoadBundledVersionsTests(unittest.TestCase):
    def test_real_manifest_loads_with_expected_npm_keys(self) -> None:
        """The on-disk `bundled_mcp_versions.toml` parses cleanly and
        exposes the four pinned npm keys named in the Phase-0 plan."""
        versions = bundled_versions.load_bundled_versions()
        npm = versions.get("npm", {})
        expected_keys = {"mermaid_mcp", "excalidraw_mcp",
                         "mermaid_lib", "excalidraw_lib"}
        self.assertEqual(set(npm.keys()), expected_keys)

    def test_each_npm_entry_has_required_fields(self) -> None:
        versions = bundled_versions.load_bundled_versions()
        for key, entry in versions.get("npm", {}).items():
            with self.subTest(key=key):
                self.assertIn("package", entry, f"{key} missing 'package'")
                self.assertIn("version", entry, f"{key} missing 'version'")
                self.assertIn("shasum", entry, f"{key} missing 'shasum'")
                # No floating versions: '^' / '~' / '*' / 'latest' are
                # forbidden by the pinning contract.
                self.assertFalse(
                    entry["version"].startswith(("^", "~", "*")),
                    f"{key} version {entry['version']!r} uses floating range",
                )
                self.assertNotEqual(
                    entry["version"], "latest",
                    f"{key} version must be a concrete pin, not 'latest'",
                )

    def test_chromium_section_signals_playwright_reuse(self) -> None:
        versions = bundled_versions.load_bundled_versions()
        self.assertEqual(
            versions.get("chromium", {}).get("reuse_playwright"),
            True,
            "[chromium] reuse_playwright must be true — Phase 0 design "
            "explicitly shares Playwright's Chromium download.",
        )

    def test_missing_file_raises_runtime_error(self) -> None:
        bogus = Path("/nonexistent/bundled_mcp_versions.toml")
        with self.assertRaises(RuntimeError) as cm:
            bundled_versions.load_bundled_versions(bogus)
        # Error names the path + the upstream repo so the user can
        # recover (same recovery wording as `_load_orchestrator_managed_paths`).
        self.assertIn(str(bogus), str(cm.exception))
        self.assertIn("vibecoded-orchestrator", str(cm.exception))

    def test_malformed_toml_raises_toml_decode_error(self) -> None:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write("this is not [ valid toml { syntax\n")
            tmp_path = Path(tmp.name)
        self.addCleanup(tmp_path.unlink)
        with self.assertRaises(tomllib.TOMLDecodeError):
            bundled_versions.load_bundled_versions(tmp_path)

    def test_unknown_top_level_section_is_preserved(self) -> None:
        """Forward-compat: extra sections (e.g. a future `[pip]` block)
        must round-trip through the loader unchanged so a new section
        can land in the .toml without bumping the loader signature."""
        body = textwrap.dedent("""
            [npm.x]
            package = "x"
            version = "1.0.0"
            shasum  = "0000000000000000000000000000000000000000"

            [future_section_that_does_not_exist_yet]
            foo = "bar"
            count = 42
        """)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)
        self.addCleanup(tmp_path.unlink)

        versions = bundled_versions.load_bundled_versions(tmp_path)
        self.assertIn("npm", versions)
        self.assertIn("future_section_that_does_not_exist_yet", versions)
        self.assertEqual(
            versions["future_section_that_does_not_exist_yet"]["count"], 42,
        )

    def test_missing_npm_section_yields_empty_npm_dict(self) -> None:
        """Empty manifest is structurally valid TOML. The loader should
        not invent an `npm` key; callers iterate `versions.get("npm", {})`
        and handle the empty case themselves (see `_resolve_pinned_package`)."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write("")
            tmp_path = Path(tmp.name)
        self.addCleanup(tmp_path.unlink)
        versions = bundled_versions.load_bundled_versions(tmp_path)
        self.assertEqual(versions, {})

    def test_manifest_path_points_to_repo_root(self) -> None:
        """`manifest_path()` exposes the resolved absolute path used by
        the default branch of `load_bundled_versions`. The file must be
        a sibling of install.py at the repo root."""
        path = bundled_versions.manifest_path()
        self.assertTrue(
            path.is_absolute(),
            f"manifest_path() returned non-absolute: {path}",
        )
        self.assertEqual(path.name, "bundled_mcp_versions.toml")
        self.assertEqual(path.parent, REPO_ROOT)
        self.assertTrue(path.is_file(), f"{path} does not exist on disk")


if __name__ == "__main__":
    unittest.main()
