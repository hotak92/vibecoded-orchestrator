# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity check for `bundled_mcp_versions.toml`
(Phase 0, diagrams-integration plan 2026-05-24).

Mirrors `tests/test_managed_paths_consistency.py`'s triangulation
shape:

      bundled_mcp_versions.toml
              /            \\
   Rust parser       Python parser
   (vct-launcher-     (vco_lib.bundled_versions
    core::bundled_     ::load_bundled_versions)
    versions)

   third edge: an INDEPENDENT third-party parse (this test) that
   re-parses the .toml in pure Python WITHOUT calling our loader, so a
   silent contract drift in EITHER the Python OR the Rust parser is
   caught.

We do NOT call `cargo test` directly here (slow, fragile in pytest).
The Rust side's own parse correctness is exercised by `cargo test
-p vct-launcher-core bundled_versions`. THIS test guards:

  * Python `load_bundled_versions()` agrees with an independent
    pure-Python tomllib re-parse of the same bytes.
  * The Rust source file embeds the .toml via `include_str!` with the
    exact path we expect (4 levels up from
    vct-launcher-core/src/bundled_versions.rs), so the Rust binary
    parses the same file Python does.
  * The hand-typed Rust unit test `bundled_npm_loads_expected_keys`
    pins the same key set the Python parser sees.

If a future refactor changes either parser without updating the .toml,
one of these guards trips first.
"""
from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import bundled_versions  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "vco_lib" / "bundled_mcp_versions.toml"
RUST_SRC_PATH = (
    REPO_ROOT
    / "launcher" / "src-tauri" / "vct-launcher-core"
    / "src" / "bundled_versions.rs"
)


def _independent_parse(text: str) -> dict:
    """Pure-stdlib re-parse — deliberately does NOT import
    `vco_lib.bundled_versions`. If our loader drifts from raw `tomllib`,
    this trips."""
    return tomllib.loads(text)


def _normalise(parsed: dict) -> dict:
    """Return a JSON-comparable view that ignores key ordering inside
    nested dicts. TOML preserves table-key order from the source but
    `dict` equality is order-insensitive, so this is mostly a no-op —
    kept as a single normalisation seam for future format additions."""
    return parsed


class BundledVersionsParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            MANIFEST_PATH.is_file(),
            f"source-of-truth manifest missing at {MANIFEST_PATH}",
        )
        self.text = MANIFEST_PATH.read_text(encoding="utf-8")

    def test_python_loader_matches_independent_parse(self) -> None:
        """The production Python loader and an independent tomllib
        re-parse agree on the same input. Pins the parse-rule contract."""
        produced = _normalise(bundled_versions.load_bundled_versions())
        independent = _normalise(_independent_parse(self.text))
        self.assertEqual(produced, independent)

    def test_npm_keys_match_phase_0_plan(self) -> None:
        """The four npm keys named in the Phase-0 plan must be present.
        If a future bump adds keys, append here (single point of edit)."""
        parsed = _independent_parse(self.text)
        actual = set(parsed.get("npm", {}).keys())
        expected = {"mermaid_mcp", "excalidraw_mcp",
                    "mermaid_lib", "excalidraw_lib"}
        self.assertEqual(actual, expected)

    def test_each_npm_entry_shape_is_tight(self) -> None:
        """Each `[npm.<key>]` table has exactly the fields the Rust
        deserialiser expects (`package`, `version`, `shasum`). Extra
        fields are tolerated by both languages but flag any addition
        here so the cross-language schema stays explicit."""
        parsed = _independent_parse(self.text)
        for key, entry in parsed.get("npm", {}).items():
            with self.subTest(key=key):
                self.assertEqual(
                    set(entry.keys()), {"package", "version", "shasum"},
                    f"npm.{key} has unexpected fields: "
                    f"{sorted(entry.keys())}. If you added a field, "
                    f"update both PinnedPackage in bundled_versions.rs "
                    f"AND this assertion.",
                )

    def test_rust_source_includes_manifest_at_expected_path(self) -> None:
        """Pin the Rust `include_str!` path. If `bundled_versions.rs`
        moves, the include-path AND this assertion must update in
        lockstep. Same guard shape as `test_managed_paths_consistency`.

        Note (v0.2.34): the .toml moved from the repo root into
        ``vco_lib/`` so the file ships in the Python wheel. The Rust
        include path was updated in lockstep: 4 levels up (to repo
        root) then back down into ``vco_lib/``.
        """
        self.assertTrue(
            RUST_SRC_PATH.is_file(),
            f"Rust loader missing at {RUST_SRC_PATH}",
        )
        rust_src = RUST_SRC_PATH.read_text(encoding="utf-8")
        expected_include = (
            'include_str!("../../../../vco_lib/bundled_mcp_versions.toml")'
        )
        self.assertIn(
            expected_include, rust_src,
            f"Rust {RUST_SRC_PATH.name} must embed bundled_mcp_versions.toml "
            f"via include_str! with a 4-level relative path into vco_lib/. "
            f"If the .rs file moved, update both the include_str! call and "
            f"this assertion.",
        )

    def test_rust_unit_test_pins_same_key_set(self) -> None:
        """The Rust unit test `bundled_npm_loads_expected_keys` lists
        the same four keys this Python parity test expects. The Rust
        test is exercised by `cargo test -p vct-launcher-core
        bundled_versions`; here we grep the source to keep the two
        in-lockstep without spawning cargo."""
        rust_src = RUST_SRC_PATH.read_text(encoding="utf-8")
        # Find the expected-keys vec literal in the unit test.
        m = re.search(
            r'fn bundled_npm_loads_expected_keys\(\)\s*\{.*?vec!\[(?P<body>.*?)\]',
            rust_src,
            re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not find `vec![...]` literal in "
            "`bundled_npm_loads_expected_keys` unit test in "
            f"{RUST_SRC_PATH.name}",
        )
        body = m.group("body")
        rust_keys = set(re.findall(r'"([^"]+)"', body))

        parsed = _independent_parse(self.text)
        py_keys = set(parsed.get("npm", {}).keys())

        self.assertEqual(
            rust_keys, py_keys,
            f"Rust unit test expects {sorted(rust_keys)}, but the "
            f"on-disk manifest contains {sorted(py_keys)}. Update one "
            f"to match the other.",
        )

    def test_self_reference_in_managed_paths(self) -> None:
        """`bundled_mcp_versions.toml` is listed in
        `orchestrator-managed-paths.txt` so `update_orchestrator_at`
        propagates new editions of the manifest into every existing
        install. Without this, a v0.3.0 bump to the pinned versions
        would never reach users who installed before v0.3.0."""
        managed_paths_file = REPO_ROOT / "orchestrator-managed-paths.txt"
        self.assertTrue(managed_paths_file.is_file())
        contents = managed_paths_file.read_text(encoding="utf-8")
        # Use a line-anchored match so substring collisions are
        # impossible (paranoia: there are no substrings, but we keep
        # the discipline matching `_independent_parse` from
        # `test_managed_paths_consistency`).
        #
        # Path is ``vco_lib/bundled_mcp_versions.toml`` since v0.2.34
        # (moved from repo root so the .toml ships in the Python wheel).
        present = any(
            line.strip() == "vco_lib/bundled_mcp_versions.toml"
            for line in contents.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        self.assertTrue(
            present,
            "`vco_lib/bundled_mcp_versions.toml` must appear in "
            "`orchestrator-managed-paths.txt` so `update_orchestrator_at` "
            "propagates manifest edits to existing installs.",
        )


if __name__ == "__main__":
    unittest.main()
