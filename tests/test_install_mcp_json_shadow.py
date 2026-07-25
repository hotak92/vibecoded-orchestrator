# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.89 FIX 2: stale `.mcp.json` weaviate-kg shadow quarantine.

Field report (Fabio, Windows CPU-only, v0.2.72→v0.2.88): 5 projects carried a
pre-migration top-level `<project>/.mcp.json` whose weaviate-kg env pointed at
the OLD Weaviate (:8080), a stale KG collection, and an EMPTY shared collection.
Because `.mcp.json` has PRECEDENCE over the migrated `.claude/settings.json`
for MCP env, every hybrid_search hit the wrong endpoint and cross-project
shared-KG merge was silently OFF.

`install --update` now detects a `.mcp.json` whose weaviate-kg env DEMONSTRABLY
contradicts the migrated settings.json and quarantines ONLY that block (backing
up first, preserving other MCP entries). This is a destructive-adjacent action,
so it must:

  (a) stale .mcp.json (wrong port + empty shared) → weaviate-kg block removed,
      backup exists, deferral written;
  (b) consistent .mcp.json (matches settings.json) → LEFT UNTOUCHED (the
      leave-alone leg — house rule "test the decision, not just the act");
  (c) no .mcp.json → no-op;
  (d) .mcp.json with weaviate-kg + another server → only weaviate-kg removed,
      the other server preserved verbatim.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralReport  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _write_settings(root: Path, *, weaviate_url: str, kg: str, shared: str) -> None:
    """Write a migrated `.claude/settings.json` with a top-level env block
    (the surface VCO actually writes — see _build_vco_settings_defaults)."""
    settings_dir = root / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "WEAVIATE_URL": weaviate_url,
                    "KG_COLLECTION": kg,
                    "SHARED_KG_COLLECTION": shared,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_mcp_json(root: Path, servers: dict) -> Path:
    """Write a top-level `.mcp.json` (Anthropic's project-scoped config)."""
    path = root / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return path


def _weaviate_entry(*, weaviate_url: str, kg: str, shared) -> dict:
    """A `.mcp.json` weaviate-kg entry. `shared` may be a str or, when
    omitted-in-caller, the key is left out by passing the sentinel None-marker
    via a separate helper below."""
    env = {"WEAVIATE_URL": weaviate_url, "KG_COLLECTION": kg}
    if shared is not None:
        env["SHARED_KG_COLLECTION"] = shared
    return {
        "type": "stdio",
        "command": "/usr/bin/python",
        "args": ["/some/weaviate_mcp/server.py"],
        "env": env,
    }


def _other_server_entry() -> dict:
    """A non-weaviate-kg MCP entry the quarantine must preserve verbatim."""
    return {
        "type": "stdio",
        "command": "/usr/bin/node",
        "args": ["my-custom-mcp.js"],
        "env": {"MY_KEY": "value"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class StaleMcpJsonShadowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # (a) stale .mcp.json (wrong port + empty shared) → quarantined.
    def test_stale_mcp_json_quarantined(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        mcp_path = _write_mcp_json(
            self.root,
            {
                "weaviate-kg": _weaviate_entry(
                    weaviate_url="http://localhost:8080",  # STALE port
                    kg="FabioKnowledge",  # STALE collection
                    shared="",  # empty shared → merge OFF
                )
            },
        )
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)

        # weaviate-kg block removed
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertNotIn("weaviate-kg", data.get("mcpServers", {}))

        # backup exists and contains the ORIGINAL stale block
        backups = list(self.root.glob(".mcp.json.bak-*"))
        self.assertEqual(len(backups), 1, "exactly one backup expected")
        backup_data = json.loads(backups[0].read_text(encoding="utf-8"))
        self.assertIn("weaviate-kg", backup_data["mcpServers"])
        self.assertEqual(
            backup_data["mcpServers"]["weaviate-kg"]["env"]["WEAVIATE_URL"],
            "http://localhost:8080",
        )

        # deferral written
        cids = {e.condition_id for e in report.entries}
        self.assertIn("stale_mcp_json_shadow_quarantined", cids)

    # (b) consistent .mcp.json → LEFT UNTOUCHED (the leave-alone leg).
    def test_consistent_mcp_json_left_untouched(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        mcp_path = _write_mcp_json(
            self.root,
            {
                "weaviate-kg": _weaviate_entry(
                    weaviate_url="http://localhost:8081",  # matches
                    kg="ProjectKG",  # matches
                    shared="VibeCodedOrchestrator_KnowledgeGraph",  # matches
                )
            },
        )
        before = mcp_path.read_text(encoding="utf-8")
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)

        # file byte-for-byte unchanged
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), before)
        # no backup
        self.assertEqual(list(self.root.glob(".mcp.json.bak-*")), [])
        # no deferral
        cids = {e.condition_id for e in report.entries}
        self.assertNotIn("stale_mcp_json_shadow_quarantined", cids)

    # (c) no .mcp.json → no-op.
    def test_no_mcp_json_is_noop(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)
        self.assertEqual(report.entries, [])
        self.assertFalse((self.root / ".mcp.json").exists())
        self.assertEqual(list(self.root.glob(".mcp.json.bak-*")), [])

    # (d) weaviate-kg + another server → only weaviate-kg removed.
    def test_other_server_preserved(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        other = _other_server_entry()
        mcp_path = _write_mcp_json(
            self.root,
            {
                "weaviate-kg": _weaviate_entry(
                    weaviate_url="http://localhost:8080",  # STALE
                    kg="FabioKnowledge",
                    shared="",
                ),
                "my-custom-mcp": other,
            },
        )
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        self.assertNotIn("weaviate-kg", servers)
        self.assertIn("my-custom-mcp", servers)
        # preserved VERBATIM
        self.assertEqual(servers["my-custom-mcp"], other)

    # Conservatism: settings.json missing → cannot prove staleness → no-op.
    def test_no_settings_json_leaves_mcp_untouched(self) -> None:
        # No settings.json at all.
        mcp_path = _write_mcp_json(
            self.root,
            {
                "weaviate-kg": _weaviate_entry(
                    weaviate_url="http://localhost:8080",
                    kg="FabioKnowledge",
                    shared="",
                )
            },
        )
        before = mcp_path.read_text(encoding="utf-8")
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.root.glob(".mcp.json.bak-*")), [])
        self.assertEqual(report.entries, [])

    # Conservatism: .mcp.json with NO weaviate-kg block → no-op.
    def test_mcp_json_without_weaviate_block_is_noop(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        mcp_path = _write_mcp_json(self.root, {"my-custom-mcp": _other_server_entry()})
        before = mcp_path.read_text(encoding="utf-8")
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)
        self.assertEqual(mcp_path.read_text(encoding="utf-8"), before)
        self.assertEqual(report.entries, [])

    # Only the shared-empty contradiction (port + collection match) still fires.
    def test_only_empty_shared_is_stale(self) -> None:
        _write_settings(
            self.root,
            weaviate_url="http://localhost:8081",
            kg="ProjectKG",
            shared="VibeCodedOrchestrator_KnowledgeGraph",
        )
        mcp_path = _write_mcp_json(
            self.root,
            {
                "weaviate-kg": _weaviate_entry(
                    weaviate_url="http://localhost:8081",  # matches
                    kg="ProjectKG",  # matches
                    shared="",  # ONLY the shared is empty → stale
                )
            },
        )
        report = DeferralReport()
        install._check_stale_mcp_json_shadow(self.root, report)
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        self.assertNotIn("weaviate-kg", data.get("mcpServers", {}))
        cids = {e.condition_id for e in report.entries}
        self.assertIn("stale_mcp_json_shadow_quarantined", cids)


class StaleMcpJsonPredicateTest(unittest.TestCase):
    """Unit-level coverage of the stale-detection predicate itself."""

    def test_absent_key_is_not_a_contradiction(self) -> None:
        # weaviate-kg env that omits KG/shared but matches URL → NOT stale.
        settings_env = {
            "WEAVIATE_URL": "http://localhost:8081",
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "Shared",
        }
        mcp_env = {"WEAVIATE_URL": "http://localhost:8081"}
        self.assertIsNone(
            install._mcp_json_weaviate_env_is_stale(mcp_env, settings_env)
        )

    def test_differing_url_is_stale(self) -> None:
        settings_env = {
            "WEAVIATE_URL": "http://localhost:8081",
            "KG_COLLECTION": "ProjectKG",
        }
        mcp_env = {
            "WEAVIATE_URL": "http://localhost:8080",
            "KG_COLLECTION": "ProjectKG",
        }
        reasons = install._mcp_json_weaviate_env_is_stale(mcp_env, settings_env)
        self.assertIsNotNone(reasons)
        self.assertTrue(any("WEAVIATE_URL" in r for r in reasons))


if __name__ == "__main__":
    unittest.main()
