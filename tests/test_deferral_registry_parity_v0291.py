# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity for ``deferral_conditions.toml`` (v0.2.91 WP-B).

Same triangulation shape as ``tests/test_mcp_scan_rules_parity.py``::

           deferral_conditions.toml
                  /            \\
         Rust loader        Python loader
    (vct-launcher-core::   (vco_lib.deferral_registry)
      deferral_registry)

      third edge: an INDEPENDENT pure-tomllib re-parse (this test) that does
      NOT call either loader, so a silent contract drift in EITHER is caught.

We do NOT shell out to cargo here (slow, fragile under pytest). The Rust side's
own parse + embedded-copy lock live in
``cargo test -p vct-launcher-core --lib deferral_registry`` and
``launcher/src-tauri/tests/deferral_registry_parity.rs``. THIS test guards the
Python half plus the structural facts about the Rust source that make the two
loaders provably read the same bytes with the same rules.
"""
from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import deferral_registry as dr  # noqa: E402

TABLE_PATH = REPO_ROOT / "vco_lib" / "deferral_conditions.toml"
RUST_LOADER_PATH = (
    REPO_ROOT
    / "launcher" / "src-tauri" / "vct-launcher-core"
    / "src" / "deferral_registry.rs"
)
RUST_PARITY_TEST = (
    REPO_ROOT / "launcher" / "src-tauri" / "tests" / "deferral_registry_parity.rs"
)


def _independent_parse() -> dict:
    """Re-parse the table with bare tomllib — no VCO code involved."""
    return tomllib.loads(TABLE_PATH.read_text(encoding="utf-8"))


class TableShapeTests(unittest.TestCase):
    def setUp(self):
        self.raw = _independent_parse()

    def test_table_exists_and_declares_format_version_1(self):
        self.assertTrue(TABLE_PATH.is_file())
        self.assertEqual(self.raw["format_version"], 1)

    def test_every_row_has_the_required_keys(self):
        for pattern, row in self.raw["conditions"].items():
            for key in ("class", "owner", "clear_probe", "emit_surfaces"):
                self.assertIn(key, row, f"{pattern} is missing {key}")

    def test_loader_sees_every_row_in_the_file(self):
        self.assertEqual(
            set(dr.registered_patterns()), set(self.raw["conditions"]),
        )

    def test_loader_class_matches_the_raw_file(self):
        for pattern, row in self.raw["conditions"].items():
            spec = next(
                s for s in dr.all_specs() if s.pattern == pattern
            )
            self.assertEqual(spec.condition_class, row["class"], pattern)
            self.assertEqual(spec.clear_probe, row["clear_probe"], pattern)
            self.assertEqual(spec.owner, row["owner"], pattern)

    def test_only_star_wildcards_are_used(self):
        """The Rust mirror implements `*` ONLY. A `?`/`[...]` row would resolve
        differently across the two languages — a divergence that parses fine
        and behaves wrong."""
        for pattern in self.raw["conditions"]:
            self.assertNotIn("?", pattern)
            self.assertNotIn("[", pattern)

    def test_glob_rows_declare_match_glob(self):
        for pattern, row in self.raw["conditions"].items():
            has_star = "*" in pattern
            self.assertEqual(
                row.get("match", "exact") == "glob", has_star, pattern,
            )


class LookupSemanticsTests(unittest.TestCase):
    """The rules the two loaders must implement identically."""

    def test_exact_beats_glob(self):
        # `deprecated_mcp_removal_summary` is an EXACT row; `deprecated_mcp_*`
        # is a glob that also matches it. Exact must win, or an actionable
        # removal failure would be tiered as a record.
        self.assertEqual(
            dr.disposition_for("deprecated_mcp_removal_declined"),
            "action_required",
        )
        self.assertEqual(
            dr.disposition_for("deprecated_mcp_ollama"), "informational_record",
        )

    def test_longest_glob_wins(self):
        self.assertEqual(
            dr.disposition_for("stale_unit_retired_vct_hub"),
            "informational_record",
        )
        self.assertEqual(
            dr.disposition_for("stale_unit_retired_vct_hub_backup_failed"),
            "action_required",
        )

    def test_unregistered_defaults_to_action_required(self):
        self.assertEqual(dr.disposition_for("not_in_the_table"), "action_required")
        self.assertFalse(dr.matches_registered_pattern("not_in_the_table"))


class RustMirrorStructureTests(unittest.TestCase):
    """Structural facts that make the mirror provably read the same bytes."""

    def setUp(self):
        self.rust = RUST_LOADER_PATH.read_text(encoding="utf-8")

    def test_rust_embeds_the_table_at_the_expected_relative_path(self):
        self.assertIn(
            'include_str!("../../../../vco_lib/deferral_conditions.toml")',
            self.rust,
        )

    def test_rust_supports_the_same_format_version(self):
        self.assertIn("const SUPPORTED_FORMAT_VERSION: u32 = 1;", self.rust)

    def test_rust_default_class_matches_python(self):
        self.assertIn(
            f'pub const DEFAULT_CLASS: &str = "{dr.DEFAULT_CLASS}";', self.rust,
        )

    def test_rust_declares_the_same_four_classes(self):
        for cls in dr.CLASSES:
            self.assertIn(f'"{cls}"', self.rust)

    def test_rust_sorts_globs_by_descending_literal_length(self):
        """The lookup ORDER is a semantic, not an implementation detail: get it
        wrong and `stale_unit_retired_*` swallows the `_backup_failed` row."""
        self.assertIn("literal_length(&b.pattern)", self.rust)
        self.assertIn(".cmp(&literal_length(&a.pattern))", self.rust)

    def test_rust_tolerates_the_python_only_retry_action_key(self):
        """v0.2.91 WP-H adds ``retry_action`` to some rows. Retries are
        dispatched from PYTHON (one dispatcher, per the WP's own shape), so
        the Rust mirror neither reads nor needs the key — it must simply
        IGNORE it, exactly as it already ignores ``notes``.

        The guard is structural: serde's `deny_unknown_fields` on
        ``RawCondition`` would turn every retry-carrying row into a parse
        error at launcher boot, silently disabling the whole registry on the
        Rust side. Its ABSENCE is therefore load-bearing, not an oversight —
        this test says so out loud so nobody "tightens" it later.
        """
        self.assertNotIn("deny_unknown_fields", self.rust)
        self.assertIn("`notes` is documentation only", self.rust)

    def test_a_rust_parity_test_exists(self):
        self.assertTrue(
            RUST_PARITY_TEST.is_file(),
            "the Rust half of the parity pair is missing",
        )
        text = RUST_PARITY_TEST.read_text(encoding="utf-8")
        self.assertIn("embedded_registry_matches_on_disk_table", text)


class OwnershipDerivationTests(unittest.TestCase):
    """Both loaders derive install ownership from the SAME rows."""

    def test_owned_ids_are_exactly_the_owned_drop_rows(self):
        raw = _independent_parse()["conditions"]
        expected = {
            p for p, row in raw.items()
            if row["clear_probe"] == "owned-drop-when-absent" and "*" not in p
        }
        self.assertEqual(set(dr.install_owned_ids()), expected)

    def test_owned_prefixes_skip_interior_wildcards(self):
        """`stale_unit_retired_*_backup_failed` must NOT become a prefix —
        `condition_is_owned` does a `startswith`, and a prefix with an interior
        wildcard would never match anything."""
        self.assertIn("stale_unit_retired_", dr.install_owned_prefixes())
        self.assertNotIn(
            "stale_unit_retired_*_backup_failed", dr.install_owned_prefixes()
        )
        for prefix in dr.install_owned_prefixes():
            self.assertNotIn("*", prefix)


if __name__ == "__main__":
    unittest.main()
