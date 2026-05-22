# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for RL-defect Fix 2 (v0.2.24, 2026-05-22): legacy
`.vscode/settings.json claude-code.env` MCP_* key detection and deferral.

The detection is hygiene-only (the keys are inert post-v0.2.12 PR-27),
but bake the user's on-disk absolute paths into the project tree.
Per user policy 2026-05-22 we never auto-overwrite user-edited files;
emit a deferral entry recommending cleanup and let the user decide.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.project_init import (  # noqa: E402
    _detect_legacy_vscode_mcp_env_keys,
    _emit_legacy_vscode_mcp_env_deferral,
)
from vco_lib.deferral_report import DeferralReport  # noqa: E402


class DetectLegacyVscodeMcpEnvKeysTest(unittest.TestCase):

    def test_no_vscode_dir_yields_action_none(self):
        with tempfile.TemporaryDirectory() as td:
            res = _detect_legacy_vscode_mcp_env_keys(Path(td))
            self.assertEqual(res["action"], "none")
            self.assertEqual(res["keys"], [])

    def test_no_claude_code_env_block_yields_action_none(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings = folder / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "files.watcherExclude": {"**/.git/**": True},
            }))
            res = _detect_legacy_vscode_mcp_env_keys(folder)
            self.assertEqual(res["action"], "none")
            self.assertEqual(res["keys"], [])

    def test_unparseable_json_yields_action_unparseable(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings = folder / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text("{ trailing-comma: 1, }")  # invalid JSON
            res = _detect_legacy_vscode_mcp_env_keys(folder)
            self.assertEqual(res["action"], "unparseable")

    def test_detects_single_mcp_weaviate_server_key(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings = folder / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "claude-code.env": {
                    "MCP_WEAVIATE_SERVER": "/home/foo/Claude/...",
                    "OTHER_KEY": "ok",
                },
            }))
            res = _detect_legacy_vscode_mcp_env_keys(folder)
            self.assertEqual(res["action"], "detected")
            self.assertEqual(res["keys"], ["MCP_WEAVIATE_SERVER"])

    def test_detects_all_four_legacy_keys_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings = folder / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "claude-code.env": {
                    "MCP_PYTHON": "/home/foo/.venv/bin/python",
                    "MCP_WEAVIATE_SERVER": "/home/foo/c/weaviate_mcp/server.py",
                    "MCP_OLLAMA_SERVER": "/home/foo/c/ollama_mcp/server.py",
                    "MCP_PYTHONPATH": "/home/foo/c/claude_mcp_servers",
                    "OTHER_KEY": "ok",
                },
            }))
            res = _detect_legacy_vscode_mcp_env_keys(folder)
            self.assertEqual(res["action"], "detected")
            self.assertEqual(
                res["keys"],
                sorted([
                    "MCP_PYTHON",
                    "MCP_WEAVIATE_SERVER",
                    "MCP_OLLAMA_SERVER",
                    "MCP_PYTHONPATH",
                ]),
            )

    def test_no_legacy_keys_only_other_env_yields_action_none(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            settings = folder / ".vscode" / "settings.json"
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                "claude-code.env": {
                    "WEAVIATE_URL": "http://localhost:8081",
                    "OLLAMA_URL": "http://localhost:11435",
                },
            }))
            res = _detect_legacy_vscode_mcp_env_keys(folder)
            self.assertEqual(res["action"], "none")


class EmitLegacyVscodeMcpEnvDeferralTest(unittest.TestCase):

    def test_emit_writes_entry_with_expected_condition_id(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            detection = {
                "action": "detected",
                "keys": ["MCP_WEAVIATE_SERVER"],
                "file": ".vscode/settings.json",
            }
            _emit_legacy_vscode_mcp_env_deferral(folder, detection)

            report = DeferralReport.read(folder)
            ids = [e.condition_id for e in report.entries]
            self.assertIn("legacy_vscode_mcp_env_keys_present", ids)
            entry = next(
                e for e in report.entries
                if e.condition_id == "legacy_vscode_mcp_env_keys_present"
            )
            self.assertEqual(entry.severity, "info")
            self.assertIn("MCP_WEAVIATE_SERVER", entry.detected)
            self.assertIn("jq", entry.command_to_apply)
            self.assertIn(".vscode/settings.json", entry.command_to_apply)

    def test_emit_includes_kg_node_reference(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            _emit_legacy_vscode_mcp_env_deferral(folder, {
                "action": "detected",
                "keys": ["MCP_PYTHON"],
                "file": ".vscode/settings.json",
            })
            report = DeferralReport.read(folder)
            entry = next(
                e for e in report.entries
                if e.condition_id == "legacy_vscode_mcp_env_keys_present"
            )
            # The deferral should reference the RL-defect KG node so
            # operators can follow the full thread.
            self.assertTrue(any(
                "rl-telemetry-silent-suppression" in ref
                for ref in entry.kg_node_refs
            ))


if __name__ == "__main__":
    unittest.main()
