# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 — MCP surface retirement (diagram wrappers) + search-MCP deletion.

Owner ruling 2026-10-04 (PLAN-V0300 item 15):

* the diagram wrapper MCPs (`mermaid`, `excalidraw`) RETIRE from default
  shipping — no longer registered on install — while an install that already
  has the entry KEEPS it (the update never removes it);
* the `search` (paper-search) MCP is DELETED outright, and its stale entry in
  an existing install is removed AUTOMATICALLY by the ordinary update
  (`auto_scrub = true` in `mcp_scan_rules.toml`) with one notice line — no
  consent prompt, no deferral.

These tests pin the Python side of that surface. The Rust side
(`mcp_registration.rs`) is pinned by its own unit tests; the two builders are
drift-locked by `tests/test_mcp_scan_rules_parity.py`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import install_mcp, mcp_scan_rules  # noqa: E402

# Text surfaces that asserted the old (false) "registered but default-disabled"
# promise, or named the deleted search MCP.
ORCHESTRATOR_TEMPLATE = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
DIAGRAMS_TAB = REPO_ROOT / "launcher" / "src" / "lib" / "project-state" / "DiagramsTab.svelte"
PERMISSIONS_TAB = REPO_ROOT / "launcher" / "src" / "lib" / "project-state" / "PermissionsTab.svelte"


def _builder_names(root: Path) -> list[str]:
    py = root / ".venv" / "bin" / "python"
    (root / "claude_mcp_servers" / "weaviate_mcp").mkdir(parents=True, exist_ok=True)
    from vco_lib.service_endpoints import urls_from_rows

    entries = install_mcp._build_python_mcp_entries(root, py, urls_from_rows({}))
    return [n for n, _, _ in entries]


class RetirementRegistryTests(unittest.TestCase):
    def test_default_entries_exclude_search_and_diagram_mcps(self) -> None:
        names = set(mcp_scan_rules.default_mcp_entry_names())
        self.assertEqual(names, {"weaviate-kg", "playwright"})
        for retired in ("search", "mermaid", "excalidraw"):
            self.assertNotIn(retired, names)

    def test_builder_emits_only_the_two_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            names = _builder_names(Path(td) / "install")
        self.assertEqual(names, ["weaviate-kg", "playwright"])

    def test_retired_names_stay_bundled_and_scrubbable(self) -> None:
        """A legacy install keeps its entry → it must stay a BUNDLED name (so
        `is_user_added` classification is right) and a SCRUB name (so uninstall
        removes it)."""
        bundled = set(mcp_scan_rules.bundled_mcp_names())
        scrub = set(mcp_scan_rules.uninstall_scrub_mcp_names())
        for retired in ("search", "mermaid", "excalidraw"):
            self.assertIn(retired, bundled, f"{retired} must stay bundled (legacy rows survive)")
            self.assertIn(retired, scrub, f"{retired} must stay scrubbable on uninstall")

    def test_default_disabled_is_empty_after_diagram_retirement(self) -> None:
        self.assertEqual(tuple(mcp_scan_rules.default_disabled_mcp_names()), ())

    def test_only_search_is_deprecated_not_the_diagram_mcps(self) -> None:
        """Owner: the update must NOT remove the diagram MCPs. Deprecation is
        the mechanism that prompts removal, so they must NOT be in it."""
        deprecated = install_mcp._DEPRECATED_DEFAULT_MCPS
        self.assertIn("search", deprecated)
        self.assertEqual(deprecated["search"]["removed_in"], "v0.2.101")
        # No opt-in manifest: the vct-search module manifest was deleted too.
        self.assertEqual(deprecated["search"].get("opt_in_manifest", ""), "")
        for keep in ("mermaid", "excalidraw"):
            self.assertNotIn(keep, deprecated, f"{keep} must NOT be deprecated (owner keeps it)")


class SearchMcpRemovalTests(unittest.TestCase):
    def test_module_and_manifest_are_gone(self) -> None:
        self.assertFalse(
            (REPO_ROOT / "claude_mcp_servers" / "search_mcp").exists(),
            "the search_mcp module must be deleted",
        )
        self.assertFalse(
            (REPO_ROOT / "launcher" / "bundled_manifests" / "vct-search.json").exists(),
            "the vct-search manifest must be deleted",
        )

    def test_stale_search_entry_is_surfaced_by_the_deprecation_scan(self) -> None:
        """A live `search` entry whose command is inside install_root is what an
        existing install carries; the scan must surface it (so the update tells
        the user once). Red-proof: delete [deprecated.search] from the table →
        this returns [] and the test fails."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "search": {
                                "type": "stdio",
                                "command": str(root / "claude_mcp_servers" / "search_mcp" / "wrapper.sh"),
                                "args": [],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            found = install_mcp._scan_deprecated_mcp_entries(root, claude_json)
        self.assertEqual([name for name, *_ in found], ["search"])

    def test_a_users_own_search_entry_is_left_alone(self) -> None:
        """A `search` entry with a command OUTSIDE install_root is the user's
        own — the scan must not surface it for removal."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps(
                    {"mcpServers": {"search": {"command": "/usr/local/bin/my-search"}}}
                ),
                encoding="utf-8",
            )
            found = install_mcp._scan_deprecated_mcp_entries(root, claude_json)
        self.assertEqual(found, [])

    def test_diagram_mcp_entries_are_never_surfaced_for_removal(self) -> None:
        """An existing VCO-shaped mermaid/excalidraw entry must survive the
        update: the scan must not classify it as deprecated."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers" / "wrappers").mkdir(parents=True)
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "mermaid": {
                                "command": str(root / ".venv" / "bin" / "python"),
                                "args": ["-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
                            },
                            "excalidraw": {
                                "command": str(root / ".venv" / "bin" / "python"),
                                "args": ["-m", "claude_mcp_servers.wrappers.excalidraw_proxy"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            found = install_mcp._scan_deprecated_mcp_entries(root, claude_json)
        self.assertEqual(found, [])


class AutoScrubTests(unittest.TestCase):
    """Owner ruling (PLAN-V0300 item 15): the update REMOVES the orphaned
    `search` entry automatically (delete now + scrub), telling the user once.
    `mermaid`/`excalidraw` are NOT scrubbed (users keep those)."""

    def _vco_search(self, root: Path) -> dict:
        return {
            "type": "stdio",
            "command": str(root / ".venv" / "bin" / "python"),
            "args": [str(root / "claude_mcp_servers" / "search_mcp" / "server.py")],
        }

    def test_auto_scrub_removes_a_vco_shaped_search_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers").mkdir(parents=True)
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "search": self._vco_search(root),
                            "my-user-mcp": {"command": "/usr/bin/my-mcp"},
                        },
                        "numStartups": 5,
                    }
                ),
                encoding="utf-8",
            )
            removed = install_mcp.auto_scrub_mcp_entries(root, claude_json)
            self.assertEqual(removed, ["search"])
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            self.assertNotIn("search", data["mcpServers"])
            # Every other key survives.
            self.assertEqual(data["mcpServers"]["my-user-mcp"]["command"], "/usr/bin/my-mcp")
            self.assertEqual(data["numStartups"], 5)

    def test_auto_scrub_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers").mkdir(parents=True)
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps({"mcpServers": {"search": self._vco_search(root)}}),
                encoding="utf-8",
            )
            self.assertEqual(install_mcp.auto_scrub_mcp_entries(root, claude_json), ["search"])
            before = claude_json.read_bytes()
            # Second run: nothing to do — must not rewrite the file.
            self.assertEqual(install_mcp.auto_scrub_mcp_entries(root, claude_json), [])
            self.assertEqual(claude_json.read_bytes(), before)

    def test_auto_scrub_leaves_diagram_entries_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers" / "wrappers").mkdir(parents=True)
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "mermaid": {
                                "command": str(root / ".venv" / "bin" / "python"),
                                "args": ["-m", "claude_mcp_servers.wrappers.mermaid_proxy"],
                            },
                            "excalidraw": {
                                "command": str(root / ".venv" / "bin" / "python"),
                                "args": ["-m", "claude_mcp_servers.wrappers.excalidraw_proxy"],
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(install_mcp.auto_scrub_mcp_entries(root, claude_json), [])
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            self.assertIn("mermaid", data["mcpServers"])
            self.assertIn("excalidraw", data["mcpServers"])

    def test_auto_scrub_leaves_a_users_own_search_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            root.mkdir()
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps({"mcpServers": {"search": {"command": "/usr/local/bin/my-search"}}}),
                encoding="utf-8",
            )
            self.assertEqual(install_mcp.auto_scrub_mcp_entries(root, claude_json), [])
            data = json.loads(claude_json.read_text(encoding="utf-8"))
            self.assertIn("search", data["mcpServers"])

    def test_remove_mcp_entries_touches_only_the_named_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / ".claude.json"
            target.write_text(
                json.dumps(
                    {
                        "mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}},
                        "projects": {"/p": {"disabledMcpServers": ["z"]}},
                    }
                ),
                encoding="utf-8",
            )
            removed, errors = install_mcp.remove_mcp_entries(target, ["a", "missing"])
            self.assertEqual(errors, [])
            self.assertEqual(removed, ["a"])
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("a", data["mcpServers"])
            self.assertIn("b", data["mcpServers"])
            self.assertEqual(data["projects"]["/p"]["disabledMcpServers"], ["z"])

    def test_install_update_removes_search_and_prints_a_notice(self) -> None:
        """The install.py wrapper: removes the entry, prints ONE notice line,
        and is idempotent (second run prints nothing, writes nothing)."""
        import os

        import install  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            root = Path(td) / "install"
            (root / "claude_mcp_servers").mkdir(parents=True)
            claude_json = home / ".claude.json"
            claude_json.write_text(
                json.dumps({"mcpServers": {"search": self._vco_search(root)}}),
                encoding="utf-8",
            )
            prev = os.environ.get("VCT_USER_HOME_OVERRIDE")
            os.environ["VCT_USER_HOME_OVERRIDE"] = str(home)
            try:
                lines: list[str] = []
                removed = install._auto_scrub_removed_mcp_entries(root, output_fn=lines.append)
                self.assertEqual(removed, ["search"])
                self.assertEqual(len(lines), 1, lines)
                self.assertIn("search", lines[0])
                data = json.loads(claude_json.read_text(encoding="utf-8"))
                self.assertNotIn("search", data["mcpServers"])

                # Second run: nothing removed, no notice.
                lines2: list[str] = []
                self.assertEqual(
                    install._auto_scrub_removed_mcp_entries(root, output_fn=lines2.append), []
                )
                self.assertEqual(lines2, [])
            finally:
                if prev is None:
                    os.environ.pop("VCT_USER_HOME_OVERRIDE", None)
                else:
                    os.environ["VCT_USER_HOME_OVERRIDE"] = prev

    def test_detect_skips_auto_scrub_entries(self) -> None:
        """`search` must NOT also produce a consent-prompt deferral — it is
        removed, not deferred."""
        from vco_lib.deferral_report import DeferralReport

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "install"
            (root / "claude_mcp_servers").mkdir(parents=True)
            claude_json = Path(td) / ".claude.json"
            claude_json.write_text(
                json.dumps({"mcpServers": {"search": self._vco_search(root)}}),
                encoding="utf-8",
            )
            report = DeferralReport()
            install_mcp._detect_deprecated_mcp_entries(root, claude_json, report)
            cids = [e.condition_id for e in report.entries]
            self.assertNotIn("deprecated_mcp_search", cids)


class FalsePromiseTextTests(unittest.TestCase):
    """Every text that claimed the retired/false mechanism must be rewritten
    in the SAME change (a promise retired with its mechanism)."""

    def test_orchestrator_template_drops_the_default_disabled_claim(self) -> None:
        text = ORCHESTRATOR_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn(
            "registered but default-disabled per project", text.lower(),
            "the orchestrator template must not claim the diagram MCPs are "
            "registered-but-default-disabled (they are no longer registered)",
        )
        self.assertNotIn("search_papers", text)

    def test_diagrams_tab_drops_the_mcp_registration_claim(self) -> None:
        text = DIAGRAMS_TAB.read_text(encoding="utf-8")
        self.assertNotIn(
            "diagrams MCP is not registered", text,
            "the Diagrams tab module toggle no longer gates MCP registration",
        )
        self.assertNotIn("register the Mermaid / Excalidraw MCPs", text)

    def test_dead_diagram_mcp_gui_surface_is_gone(self) -> None:
        """v0.2.101: the per-tool MCP-grant GUI + its Tauri commands existed
        only for the mermaid/excalidraw wrapper MCPs. They must be gone from
        the GUI, lib.rs, and the TS types."""
        perms = PERMISSIONS_TAB.read_text(encoding="utf-8")
        for gone in ("PER_TOOL_CAPABLE_MCPS", "list_project_mcp_tools",
                     "set_project_mcp_tool_enabled", "seed_project_mcp_tool_grants"):
            self.assertNotIn(gone, perms, f"PermissionsTab still references {gone}")
        lib = (REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
        for gone in ("diagrams_cmd::list_project_mcp_tools",
                     "diagrams_cmd::set_project_mcp_tool_enabled",
                     "diagrams_cmd::seed_project_mcp_tool_grants"):
            self.assertNotIn(gone, lib, f"lib.rs still registers {gone}")
        types = (REPO_ROOT / "launcher" / "src" / "lib" / "types" / "project-state.ts").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("export interface McpToolGrant", types)

    def test_permissions_tab_names_the_real_channel(self) -> None:
        text = PERMISSIONS_TAB.read_text(encoding="utf-8")
        self.assertNotIn(
            "disabledMcpjsonServers", text,
            "the per-project toggle must not claim the settings-file "
            "disabledMcpjsonServers key (governs .mcp.json servers only)",
        )
        self.assertIn(
            "disabledMcpServers", text,
            "the per-project toggle must name the real ~/.claude.json channel",
        )


class RetiredAllowEntryScrubTests(unittest.TestCase):
    """SF-B (round A review B): the deleted search MCP also leaves
    `mcp__search__*` behind — in the shipped settings templates (removed
    there) and in every existing project's `.claude/settings.json` allow
    list (dropped by the bundle update's settings merge, driven by the SAME
    one-home rule table as the ~/.claude.json registration scrub)."""

    def test_shipped_templates_carry_no_search_allow_entry(self) -> None:
        for name in ("settings.json.linux.template", "settings.json.windows.template"):
            text = (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "mcp__search__", text,
                f"{name} still grants the deleted search MCP's tools",
            )

    def test_rule_table_declares_the_retired_allow_pattern(self) -> None:
        self.assertEqual(
            mcp_scan_rules.retired_settings_allow_patterns(),
            ("mcp__search__*",),
        )

    def test_bundle_update_drops_the_retired_allow_entry(self) -> None:
        """An existing project whose allow list still carries the dead
        pattern: the ordinary bundle-update settings merge removes it (and
        nothing else)."""
        import json as _json

        from vco_lib.bundle_settings_io import merge_settings_template

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            template = root / "settings.json.linux.template"
            template.write_text(_json.dumps({
                "permissions": {"allow": ["Skill", "mcp__weaviate-kg__*"]},
            }), encoding="utf-8")
            target = root / "proj" / ".claude" / "settings.json"
            target.parent.mkdir(parents=True)
            target.write_text(_json.dumps({
                "permissions": {
                    "allow": [
                        "Skill",
                        "Bash(pytest *)",
                        "mcp__weaviate-kg__*",
                        "mcp__search__*",
                    ]
                },
            }), encoding="utf-8")

            def _write(path: Path, data: bytes):
                path.write_bytes(data)
                return None

            status, _redirect = merge_settings_template(
                template, target, dry_run=False, write=_write,
                project_root=root / "proj",
            )
            self.assertEqual(status, "merged")
            merged = _json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(
                merged["permissions"]["allow"],
                ["Skill", "Bash(pytest *)", "mcp__weaviate-kg__*"],
                "the retired pattern is gone; every user entry stays",
            )

    def test_merge_leaves_a_clean_allow_list_untouched(self) -> None:
        import json as _json

        from vco_lib.bundle_settings_io import merge_settings_template

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            template = root / "tpl"
            template.write_text(_json.dumps({
                "permissions": {"allow": ["Skill"]},
            }), encoding="utf-8")
            target = root / "proj" / ".claude" / "settings.json"
            target.parent.mkdir(parents=True)
            target.write_text(_json.dumps({
                "permissions": {"allow": ["Skill", "Bash(my-tool *)"]},
            }), encoding="utf-8")

            status, _redirect = merge_settings_template(
                template, target, dry_run=False,
                write=lambda p, d: (p.write_bytes(d), None)[1],
                project_root=root / "proj",
            )
            self.assertEqual(status, "unchanged")
            self.assertEqual(
                _json.loads(target.read_text(encoding="utf-8"))
                ["permissions"]["allow"],
                ["Skill", "Bash(my-tool *)"],
            )


if __name__ == "__main__":
    unittest.main()