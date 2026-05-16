# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for PR-37 (v0.2.12): launcher GUI maintenance Tauri commands.

This is a *shape* test: we parse `launcher/src-tauri/src/lib.rs` and
verify the 7 new `commands::maintenance::*` commands are registered in
the `tauri::generate_handler!` block, and that the corresponding
front-end `invoke()` call-sites in the new Svelte components reference
the same names.

Rationale: the Rust-side compile already validates types + cargo tests
exercise the parsing + detection helpers. What ONLY a cross-cutting
check catches is a Svelte invoke that names a command the Rust
generate_handler! macro doesn't register — which would compile but
fail at runtime with `Command X not found`. The test guards that
invariant for the PR-37 surface.

References:
  * `.claude/context/plans/pr37-gui-maintenance-panel.md`
  * `launcher/src-tauri/src/commands/maintenance.rs` (inline Rust
    tests cover schema-probe parsing + stale-entry detector).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"
MAINTENANCE_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "maintenance.rs"
)
MOD_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "mod.rs"
COMPONENTS_DIR = REPO_ROOT / "launcher" / "src" / "lib" / "components"
MCP_PAGE = REPO_ROOT / "launcher" / "src" / "routes" / "mcp" / "+page.svelte"
SERVICES_PAGE = REPO_ROOT / "launcher" / "src" / "routes" / "services" / "+page.svelte"

# Canonical list of the 7 Tauri commands PR-37 adds. If you add a new
# maintenance command, update BOTH this list AND lib.rs's
# generate_handler! block.
EXPECTED_COMMANDS = [
    "mcp_registration_status",
    "rerun_mcp_registration",
    "schema_migration_status",
    "issue_schema_migration_consent_token",
    "run_schema_migrations",
    "stale_mcp_entries",
    "rewrite_stale_mcp_entries",
]


class MaintenanceCommandShapeTests(unittest.TestCase):
    """Static shape checks — no Tauri runtime / cargo invocation."""

    def setUp(self) -> None:
        self.assertTrue(LIB_RS.is_file(), f"missing {LIB_RS}")
        self.assertTrue(MAINTENANCE_RS.is_file(), f"missing {MAINTENANCE_RS}")
        self.assertTrue(MOD_RS.is_file(), f"missing {MOD_RS}")
        self.lib_rs_text = LIB_RS.read_text(encoding="utf-8")
        self.maintenance_rs_text = MAINTENANCE_RS.read_text(encoding="utf-8")
        self.mod_rs_text = MOD_RS.read_text(encoding="utf-8")

    def test_maintenance_module_declared_in_mod_rs(self) -> None:
        """`pub mod maintenance;` must be declared in commands/mod.rs."""
        self.assertIn(
            "pub mod maintenance;",
            self.mod_rs_text,
            "commands::maintenance module must be declared in commands/mod.rs",
        )

    def test_all_commands_defined_in_maintenance_rs(self) -> None:
        """Every command in EXPECTED_COMMANDS must have a `pub async fn`
        definition in maintenance.rs with a `#[command]` attribute above
        it (Tauri's command-registration discipline).
        """
        for cmd in EXPECTED_COMMANDS:
            with self.subTest(command=cmd):
                # `pub async fn <name>(` — allow whitespace.
                pattern = rf"pub\s+async\s+fn\s+{re.escape(cmd)}\s*\("
                self.assertRegex(
                    self.maintenance_rs_text,
                    pattern,
                    f"`{cmd}` must be defined as `pub async fn` in maintenance.rs",
                )

    def test_all_commands_registered_in_generate_handler(self) -> None:
        """Every PR-37 command must appear in the generate_handler! block
        in lib.rs as `commands::maintenance::<name>,`.
        """
        for cmd in EXPECTED_COMMANDS:
            with self.subTest(command=cmd):
                needle = f"commands::maintenance::{cmd},"
                self.assertIn(
                    needle,
                    self.lib_rs_text,
                    f"`{needle}` must be present in lib.rs::generate_handler! block",
                )

    def test_no_unexpected_maintenance_commands_in_handler(self) -> None:
        """Defensive: catch typos / orphans. Every
        `commands::maintenance::<name>` in lib.rs must be in
        EXPECTED_COMMANDS (so an outdated test list fails loudly).
        """
        registered = re.findall(
            r"commands::maintenance::([a-z_]+)\b", self.lib_rs_text
        )
        for name in registered:
            with self.subTest(command=name):
                self.assertIn(
                    name,
                    EXPECTED_COMMANDS,
                    f"`commands::maintenance::{name}` in lib.rs is not in EXPECTED_COMMANDS "
                    f"— either a typo or the test list is stale",
                )

    def test_modals_and_sections_exist(self) -> None:
        """The 4 new Svelte components ship in
        launcher/src/lib/components/.
        """
        expected = [
            "McpMaintenanceSection.svelte",
            "ServicesSchemaSection.svelte",
            "StaleMcpModal.svelte",
            "SchemaMigrationModal.svelte",
        ]
        for fname in expected:
            with self.subTest(file=fname):
                self.assertTrue(
                    (COMPONENTS_DIR / fname).is_file(),
                    f"missing {COMPONENTS_DIR / fname}",
                )

    def test_mcp_page_mounts_mcp_maintenance_section(self) -> None:
        """The /mcp page imports and renders McpMaintenanceSection."""
        self.assertTrue(MCP_PAGE.is_file(), f"missing {MCP_PAGE}")
        text = MCP_PAGE.read_text(encoding="utf-8")
        self.assertIn(
            "McpMaintenanceSection",
            text,
            "MCP page must import McpMaintenanceSection (PR-37)",
        )

    def test_services_page_mounts_services_schema_section(self) -> None:
        """The /services page imports and renders ServicesSchemaSection."""
        self.assertTrue(SERVICES_PAGE.is_file(), f"missing {SERVICES_PAGE}")
        text = SERVICES_PAGE.read_text(encoding="utf-8")
        self.assertIn(
            "ServicesSchemaSection",
            text,
            "Services page must import ServicesSchemaSection (PR-37)",
        )

    def test_modal_invokes_match_registered_command_names(self) -> None:
        """Every command name `invoke<...>('<name>'...)` referenced by
        the PR-37 Svelte components must appear in EXPECTED_COMMANDS.
        Guards against typos like `rerun_mcp_register` vs
        `rerun_mcp_registration` that would compile-pass but fail at
        runtime with "Command not found".
        """
        files = [
            COMPONENTS_DIR / "McpMaintenanceSection.svelte",
            COMPONENTS_DIR / "ServicesSchemaSection.svelte",
            COMPONENTS_DIR / "StaleMcpModal.svelte",
            COMPONENTS_DIR / "SchemaMigrationModal.svelte",
        ]
        # Match: invoke<Foo>('command_name' or invoke('command_name'
        pattern = re.compile(r"invoke\s*(?:<[^>]+>)?\s*\(\s*['\"]([a-z_]+)['\"]")
        for f in files:
            with self.subTest(file=f.name):
                self.assertTrue(f.is_file(), f"missing {f}")
                text = f.read_text(encoding="utf-8")
                invocations = pattern.findall(text)
                self.assertGreater(
                    len(invocations),
                    0,
                    f"{f.name} must invoke at least one Tauri command",
                )
                for cmd in invocations:
                    self.assertIn(
                        cmd,
                        EXPECTED_COMMANDS,
                        f"{f.name} invokes `{cmd}` which is not in EXPECTED_COMMANDS — "
                        f"either typo or a non-PR-37 command (move the import / "
                        f"split the file in that case)",
                    )

    def test_consent_token_command_present_for_schema_modal(self) -> None:
        """The schema-migration consent flow requires the issuance
        command. Defensive — earlier drafts had the FE generate the
        UUID locally, which broke the single-source-of-truth contract.
        """
        text = (COMPONENTS_DIR / "SchemaMigrationModal.svelte").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "issue_schema_migration_consent_token",
            text,
            "SchemaMigrationModal must fetch consent token from backend (not FE-generated)",
        )

    def test_stale_modal_uses_per_entry_consent_vec(self) -> None:
        """The stale-MCP rewrite contract is a Vec<(name, bool)> —
        catch a regression where it becomes a flat list of names (which
        would lose the audit trail of unchecked entries).
        """
        text = (COMPONENTS_DIR / "StaleMcpModal.svelte").read_text(encoding="utf-8")
        # Look for the consent payload construction.
        self.assertIn(
            "rewrite_stale_mcp_entries",
            text,
            "StaleMcpModal must invoke rewrite_stale_mcp_entries",
        )
        # The payload key must be `consent` (matches Rust command param name).
        self.assertIn(
            "consent",
            text,
            "StaleMcpModal must pass `consent` payload key to rewrite_stale_mcp_entries",
        )


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
