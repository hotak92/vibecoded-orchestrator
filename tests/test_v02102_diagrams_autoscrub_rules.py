# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 — the diagrams MCP retirement reaches the ``search``-style
auto-scrub (owner ruling 2026-10-07).

v0.2.101 stopped REGISTERING the ``mermaid`` / ``excalidraw`` wrapper MCPs
on fresh installs but deliberately kept existing entries. v0.2.102 deletes
the wrapper modules outright, so a leftover entry guarantees a failing MCP
subprocess every session — the exact condition ``auto_scrub`` exists for
(the ``search`` precedent, v0.2.101).

These tests pin:
  1. both ``[deprecated.*]`` rows: ``removed_in`` = v0.2.102,
     ``auto_scrub`` = true, a non-empty reason, and the matching
     ``settings_allow_patterns`` entry;
  2. the names STAY in ``[bundled].all_names`` /
     ``uninstall_scrub_names`` / ``uninstall_scrub_shape_gated`` —
     classification and the shape-gated uninstall must keep recognising
     them (a user's own third-party mermaid/excalidraw MCP is never
     touched by name alone);
  3. the Python scrub writer picks both names up (generic
     ``[deprecated.*]`` iteration — no per-name code);
  4. neither settings template registers the pre-diagram hook or carries
     ``mcp__mermaid__*`` / ``mcp__excalidraw__*`` allow patterns.

Each assertion was RED on the pre-change tree (the rows did not exist).
"""
from __future__ import annotations

import json
from pathlib import Path

from vco_lib import mcp_scan_rules
from vco_lib import install_mcp

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}


def test_mermaid_and_excalidraw_are_declared_deprecated_with_auto_scrub():
    dep = mcp_scan_rules.deprecated_default_mcps()
    for name in ("mermaid", "excalidraw"):
        assert name in dep, (
            f"{name} must carry a [deprecated.{name}] row in "
            f"vco_lib/mcp_scan_rules.toml (module deleted in v0.2.102)"
        )
        info = dep[name]
        assert info["removed_in"] == "v0.2.102", (name, info)
        assert info["auto_scrub"] is True, (
            f"{name}: auto_scrub must be true — the wrapper module is "
            f"deleted, a leftover entry fails to spawn every session"
        )
        assert isinstance(info["reason"], str) and info["reason"].strip()


def test_retired_settings_allow_patterns_cover_both_diagram_mcps():
    patterns = mcp_scan_rules.retired_settings_allow_patterns()
    assert "mcp__mermaid__*" in patterns, patterns
    assert "mcp__excalidraw__*" in patterns, patterns


def test_diagram_names_stay_in_bundled_and_scrub_sets():
    """Classification + uninstall keep the names (owner: keep the
    recognition code; never drop user data or a third-party same-name
    MCP)."""
    assert set(("mermaid", "excalidraw")) <= set(
        mcp_scan_rules.bundled_mcp_names()
    )
    assert set(("mermaid", "excalidraw")) <= set(
        mcp_scan_rules.uninstall_scrub_mcp_names()
    )
    assert set(("mermaid", "excalidraw")) <= set(
        mcp_scan_rules.uninstall_scrub_shape_gated_mcp_names()
    )


def test_auto_scrub_writer_covers_both_diagram_names():
    """The scrub writer's auto set is derived from the table (no per-name
    branch), so both new rows flow through
    ``install_mcp.auto_scrub_mcp_entries``."""
    auto = [
        name
        for name, info in install_mcp._DEPRECATED_DEFAULT_MCPS.items()
        if info.get("auto_scrub", False)
    ]
    assert "mermaid" in auto and "excalidraw" in auto, auto


def test_settings_templates_never_registered_the_diagram_hooks():
    for os_name, path in TEMPLATES.items():
        doc = json.loads(path.read_text(encoding="utf-8"))
        blob = json.dumps(doc)
        assert "pre-diagram-path-validation" not in blob, os_name
        assert "mcp__mermaid__" not in blob, os_name
        assert "mcp__excalidraw__" not in blob, os_name
