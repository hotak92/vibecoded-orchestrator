# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity for ``mcp_scan_rules.toml`` (v0.2.83 WP-B4).

Mirrors ``tests/test_bundled_versions_parity.py``'s triangulation shape:

          mcp_scan_rules.toml
                /          \\
       Rust parser     Python parser
       (vct-launcher-   (vco_lib.mcp_scan_rules
        core::mcp_       ::load_mcp_scan_rules)
        scan_rules)

     third edge: an INDEPENDENT pure-tomllib re-parse (this test) that does
     NOT call our loader, so a silent contract drift in EITHER the Python OR
     the Rust parser is caught.

We do NOT call ``cargo test`` here (slow, fragile in pytest). The Rust side's
own parse + compiled-copy locks are exercised by
``cargo test -p vct-launcher-core mcp_scan_rules`` and
``cargo test -p vct-launcher-temp --lib mcp_registration`` (the compiled
ALLOWED_ENV_KEYS / DEFAULT_MCP_ENTRY_NAMES ⇄ table drift tests) plus the
dedicated integration test ``launcher/src-tauri/tests/mcp_scan_rules_parity.rs``.
THIS test guards:

  * Python ``load_mcp_scan_rules()`` agrees with an independent tomllib
    re-parse of the same bytes (parse-rule contract).
  * The two Python consumers (``install_mcp`` allowlist / entry-names /
    needles / deprecated registry) equal the table.
  * The Rust source embeds the .toml via ``include_str!`` at the exact
    expected path, and reads the needle set through the loader — so the Rust
    binary parses the same file Python does.
  * The compiled Rust ``ALLOWED_ENV_KEYS`` / ``DEFAULT_MCP_ENTRY_NAMES``
    literals equal the table (grep the source, so a drift trips without
    spawning cargo).

PENDING FOLLOW-UP (NOT asserted here, by design): ``mcp_scan_rules.toml`` is
NOT yet listed in ``orchestrator-managed-paths.txt``. Adding it (so
``update_orchestrator_at`` propagates table edits to existing installs)
requires a coordinated 3-line change that touches ``installer.rs`` — a file
outside WP-B4's ownership. It is reported as a sequenced follow-up. Until
then the table still ships from install time (it lives in the repo / wheel);
only the incremental propagation of table EDITS to already-installed clones
is deferred. See the WP-B4 report.

If a future refactor changes either parser (or a compiled copy) without
updating the .toml, one of these guards trips first.
"""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import mcp_scan_rules  # noqa: E402

TABLE_PATH = REPO_ROOT / "vco_lib" / "mcp_scan_rules.toml"
RUST_LOADER_PATH = (
    REPO_ROOT
    / "launcher" / "src-tauri" / "vct-launcher-core"
    / "src" / "mcp_scan_rules.rs"
)
RUST_REGISTRATION_PATH = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "mcp_registration.rs"
)


def _independent_parse() -> dict:
    """Pure-stdlib re-parse — deliberately does NOT import our loader."""
    return tomllib.loads(TABLE_PATH.read_text(encoding="utf-8"))


def _rust_string_array(source: str, symbol: str) -> list[str]:
    """Extract the string-literal elements of a Rust ``&[&str]`` array
    literal assigned to ``symbol`` (e.g. ``const ALLOWED_ENV_KEYS: &[&str] =
    &[ "A", "B" ];``). Order-preserving."""
    m = re.search(
        rf"{re.escape(symbol)}\s*:\s*&\[&str\]\s*=\s*&?\[(?P<body>.*?)\]",
        source,
        re.DOTALL,
    )
    assert m is not None, f"could not find `{symbol}` &[&str] literal in Rust source"
    return re.findall(r'"([^"]+)"', m.group("body"))


class McpScanRulesParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            TABLE_PATH.is_file(),
            f"source-of-truth table missing at {TABLE_PATH}",
        )

    # ── Parse-rule contract ────────────────────────────────────────────
    def test_python_loader_matches_independent_parse(self) -> None:
        produced = mcp_scan_rules.load_mcp_scan_rules()
        independent = _independent_parse()
        self.assertEqual(produced, independent)

    def test_format_version_is_1(self) -> None:
        self.assertEqual(_independent_parse().get("format_version"), 1)

    # ── Python consumers equal the table ───────────────────────────────
    def test_install_mcp_allowlist_matches_table(self) -> None:
        from vco_lib import install_mcp

        table = tuple(_independent_parse()["env"]["allowed_global_keys"])
        self.assertEqual(tuple(install_mcp._ALLOWED_GLOBAL_ENV_KEYS), table)

    def test_install_mcp_entry_names_match_table(self) -> None:
        from vco_lib import install_mcp

        table = tuple(_independent_parse()["entries"]["default_names"])
        self.assertEqual(tuple(install_mcp._DEFAULT_MCP_ENTRY_NAMES), table)

    def test_install_mcp_needles_match_table(self) -> None:
        from vco_lib import install_mcp

        table = set(_independent_parse()["env"]["secret_shaped_needles"])
        self.assertEqual(set(install_mcp._SECRET_SHAPED_SUBSTRINGS), table)

    def test_install_mcp_deprecated_registry_matches_table(self) -> None:
        from vco_lib import install_mcp

        table = _independent_parse().get("deprecated", {})
        registry = install_mcp._DEPRECATED_DEFAULT_MCPS
        self.assertEqual(set(registry.keys()), set(table.keys()))
        for name, info in table.items():
            self.assertEqual(registry[name]["removed_in"], info.get("removed_in", ""))
            self.assertEqual(registry[name]["reason"], info.get("reason", ""))
            self.assertEqual(
                registry[name]["opt_in_manifest"], info.get("opt_in_manifest", "")
            )

    def test_builder_emit_order_matches_table_entry_names(self) -> None:
        """The Python entry BUILDER emits entries in the table's
        [entries].default_names order (the builder self-asserts this, but we
        double-check end-to-end here)."""
        import tempfile

        from vco_lib import install_mcp

        table = list(_independent_parse()["entries"]["default_names"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True)
            (root / "claude_mcp_servers" / "search_mcp").mkdir(parents=True)
            py = root / ".venv" / "bin" / "python"
            entries = install_mcp._build_python_mcp_entries(
                root, py, 8081, 11435, 50052, 11440
            )
            emitted = [name for name, _, _ in entries]
        self.assertEqual(emitted, table)

    # ── Rust side reads the same file ──────────────────────────────────
    def test_rust_loader_includes_table_at_expected_path(self) -> None:
        self.assertTrue(RUST_LOADER_PATH.is_file(), f"missing {RUST_LOADER_PATH}")
        src = RUST_LOADER_PATH.read_text(encoding="utf-8")
        expected_include = 'include_str!("../../../../vco_lib/mcp_scan_rules.toml")'
        self.assertIn(
            expected_include, src,
            "mcp_scan_rules.rs must embed the table via include_str! with a "
            "4-level relative path into vco_lib/. If the .rs file moves, "
            "update both the include_str! call and this assertion.",
        )

    def test_rust_registration_reads_needles_from_loader(self) -> None:
        src = RUST_REGISTRATION_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "mcp_scan_rules::secret_shaped_needles()", src,
            "mcp_registration.rs must read needles from the shared loader",
        )

    def test_rust_compiled_allowlist_matches_table(self) -> None:
        src = RUST_REGISTRATION_PATH.read_text(encoding="utf-8")
        rust_keys = _rust_string_array(src, "ALLOWED_ENV_KEYS")
        table_keys = list(_independent_parse()["env"]["allowed_global_keys"])
        self.assertEqual(
            rust_keys, table_keys,
            "Rust ALLOWED_ENV_KEYS (compiled) drifted from the table "
            "[env].allowed_global_keys. Update both in one commit.",
        )

    def test_rust_compiled_entry_names_match_table(self) -> None:
        src = RUST_REGISTRATION_PATH.read_text(encoding="utf-8")
        rust_names = _rust_string_array(src, "DEFAULT_MCP_ENTRY_NAMES")
        table_names = list(_independent_parse()["entries"]["default_names"])
        self.assertEqual(
            rust_names, table_names,
            "Rust DEFAULT_MCP_ENTRY_NAMES (compiled) drifted from the table "
            "[entries].default_names. Update both in one commit.",
        )

    # NOTE: a `test_table_is_in_managed_paths` assertion is deliberately
    # ABSENT — adding mcp_scan_rules.toml to orchestrator-managed-paths.txt
    # requires editing installer.rs (out of WP-B4 ownership). Reported as a
    # sequenced follow-up; see the module docstring.


if __name__ == "__main__":
    unittest.main()
