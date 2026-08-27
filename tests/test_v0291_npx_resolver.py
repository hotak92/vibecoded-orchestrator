# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D — the ONE home for Node-CLI resolution.

What these pin:

1. The v0.2.51 Bug E ladder still behaves EXACTLY as it did inside install.py
   (the extraction must be behaviour-preserving — the fnm case it was written
   for is a real user machine, not a hypothetical).
2. ``package_run_argv``'s three-way outcome: npx → npm exec → ``None``.
3. The ``--json`` CLI contract the Rust registration badge parses.
4. install.py delegates rather than keeping a second copy.

Hermetic: every filesystem/PATH fact is injected. No Node required.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import npx_resolver  # noqa: E402


class FindNpxLadderTests(unittest.TestCase):
    """The four ladder steps, each isolated."""

    def test_step1_npx_directly_on_path(self):
        with mock.patch("shutil.which", side_effect=lambda n: {
            "npx": "/usr/bin/npx"}.get(n)):
            self.assertEqual(npx_resolver.find_npx(), "/usr/bin/npx")

    def test_step2_sibling_of_the_npm_symlink(self):
        """apt/brew layout: npm on PATH, npx beside it but not on PATH."""
        with mock.patch("shutil.which", side_effect=lambda n: {
                "npm": "/opt/node/bin/npm"}.get(n)), \
             mock.patch.object(npx_resolver, "_usable",
                               side_effect=lambda p: str(p) == "/opt/node/bin/npx"):
            self.assertEqual(npx_resolver.find_npx(), "/opt/node/bin/npx")

    def test_step4_fnm_installation_bin_wins_over_lib_shim(self):
        """The reported 2026-06-09 machine: ~/.local/bin/npm is a symlink into
        ``.../installation/bin/npm``, whose realpath is
        ``.../lib/node_modules/npm/bin/npm-cli.js``. The canonical shim at
        ``<root>/bin/npx`` must be preferred over the lib one, which does not
        always run standalone."""
        real = Path(
            "/home/u/.fnm/node-versions/v20/installation/lib/node_modules/npm/bin/npm-cli.js"
        )
        canonical = "/home/u/.fnm/node-versions/v20/installation/bin/npx"
        lib_sibling = (
            "/home/u/.fnm/node-versions/v20/installation/lib/node_modules/npm/bin/npx"
        )
        with mock.patch("shutil.which", side_effect=lambda n: {
                "npm": "/home/u/.local/bin/npm"}.get(n)), \
             mock.patch.object(Path, "resolve", lambda self: real), \
             mock.patch.object(npx_resolver, "_usable",
                               side_effect=lambda p: str(p) in (canonical, lib_sibling)):
            self.assertEqual(npx_resolver.find_npx(), canonical)

    def test_no_npm_means_no_ladder(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(npx_resolver.find_npx())
            self.assertIsNone(npx_resolver.find_npm())

    def test_windows_variants_are_probed(self):
        seen: list[str] = []

        def _usable(p):
            seen.append(Path(p).name)
            return False

        with mock.patch("shutil.which", side_effect=lambda n: {
                "npm": "C:\\node\\npm"}.get(n)), \
             mock.patch.object(npx_resolver, "_is_windows", return_value=True), \
             mock.patch.object(npx_resolver, "_usable", side_effect=_usable):
            npx_resolver.find_npx()
        self.assertIn("npx.cmd", seen)
        self.assertIn("npx.ps1", seen)


class PackageRunArgvTests(unittest.TestCase):
    def test_prefers_npx(self):
        with mock.patch.object(npx_resolver, "find_npx", return_value="/b/npx"), \
             mock.patch.object(npx_resolver, "find_npm", return_value="/b/npm"):
            self.assertEqual(
                npx_resolver.package_run_argv("claude-mermaid", "1.6.3"),
                ["/b/npx", "-y", "claude-mermaid@1.6.3"],
            )

    def test_falls_back_to_npm_exec_with_separator(self):
        """``--`` is load-bearing: without it npm swallows a later --flag."""
        with mock.patch.object(npx_resolver, "find_npx", return_value=None), \
             mock.patch.object(npx_resolver, "find_npm", return_value="/b/npm"):
            self.assertEqual(
                npx_resolver.package_run_argv("claude-mermaid", "1.6.3"),
                ["/b/npm", "exec", "--yes", "--", "claude-mermaid@1.6.3"],
            )

    def test_none_when_node_absent(self):
        with mock.patch.object(npx_resolver, "find_npx", return_value=None), \
             mock.patch.object(npx_resolver, "find_npm", return_value=None):
            self.assertIsNone(npx_resolver.package_run_argv("x", "1"))

    def test_versionless_spec(self):
        with mock.patch.object(npx_resolver, "find_npx", return_value="/b/npx"):
            self.assertEqual(
                npx_resolver.package_run_argv("pkg"), ["/b/npx", "-y", "pkg"],
            )


class ResolveCommandTests(unittest.TestCase):
    def test_npx_uses_the_ladder_not_bare_which(self):
        with mock.patch.object(npx_resolver, "find_npx", return_value="/lad/npx"):
            self.assertEqual(npx_resolver.resolve_command("npx"), "/lad/npx")

    def test_other_names_use_which(self):
        with mock.patch("shutil.which", side_effect=lambda n: {
                "node": "/usr/bin/node"}.get(n)):
            self.assertEqual(npx_resolver.resolve_command("node"), "/usr/bin/node")
            self.assertIsNone(npx_resolver.resolve_command("deno"))

    def test_empty_name(self):
        self.assertIsNone(npx_resolver.resolve_command(""))


class ProbePayloadTests(unittest.TestCase):
    def test_payload_shape_matches_the_rust_contract(self):
        with mock.patch.object(npx_resolver, "resolve_command",
                               side_effect=lambda n: {"npx": "/b/npx"}.get(n)):
            payload = npx_resolver.probe(["node"])
        self.assertEqual(payload["schema_version"], npx_resolver.SCHEMA_VERSION)
        self.assertTrue(payload["npx_present"])
        self.assertFalse(payload["npm_present"])
        self.assertEqual(payload["npx_path"], "/b/npx")
        self.assertEqual(
            payload["commands"], {"npx": "/b/npx", "npm": None, "node": None},
        )

    def test_duplicate_command_names_collapse(self):
        with mock.patch.object(npx_resolver, "resolve_command", return_value=None):
            payload = npx_resolver.probe(["npx", "npx", "node"])
        self.assertEqual(sorted(payload["commands"]), ["node", "npm", "npx"])


class CliTests(unittest.TestCase):
    def test_json_cli_runs_and_exits_zero_even_when_npx_missing(self):
        """Exit status reports "the probe RAN", never "npx exists" — the Rust
        caller must be able to distinguish a missing npx from an interpreter
        that could not start."""
        proc = subprocess.run(
            [sys.executable, "-m", "vco_lib.npx_resolver", "--json",
             "--command", "definitely-not-a-real-binary-vco"],
            capture_output=True, cwd=str(REPO_ROOT), env={
                **__import__("os").environ, "PYTHONPATH": str(REPO_ROOT),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        payload = json.loads(proc.stdout.decode())
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("definitely-not-a-real-binary-vco", payload["commands"])
        self.assertIsNone(payload["commands"]["definitely-not-a-real-binary-vco"])


class InstallPyDelegationTests(unittest.TestCase):
    """install.py must DELEGATE, not keep a second ladder."""

    def setUp(self):
        self.src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_install_py_delegates_to_the_shared_resolver(self):
        self.assertIn("_npx_resolver.find_npx()", self.src)
        self.assertIn("from vco_lib import npx_resolver as _npx_resolver", self.src)

    def test_install_py_no_longer_carries_the_ladder_body(self):
        # The distinctive marker of the copied implementation.
        self.assertNotIn('parts.index("node_modules")', self.src)

    def test_playwright_skip_message_is_honest(self):
        """RED on c67ef888: install.py told the user the MCP would
        "lazy-install when first invoked" — impossible with `command: npx` and
        no npx. The honest statement is that it cannot spawn."""
        window = self.src[self.src.index("def _install_playwright_browsers"):]
        window = window[: window.index("def _install_codegraph_treesitter")]
        # Scope to the npx-MISSING branch: the Chromium messages further down
        # legitimately say "will lazy-install Chromium on first browser call"
        # (that download genuinely can happen later — the MCP spawn cannot).
        branch = window[window.index("if not npx_path:"):]
        branch = branch[: branch.index("print(\"(this may take")]
        self.assertIn("CANNOT SPAWN", branch)
        self.assertNotIn("lazy-install", branch)


if __name__ == "__main__":
    unittest.main()
