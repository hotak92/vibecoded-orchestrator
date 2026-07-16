# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 (WP-B5): uninstall-scrub migration onto the shared rule table.

The uninstaller (``install.py`` Step 4) removes VCO's OWN MCP entries from the
user's global ``~/.claude.json`` and leaves the user's third-party MCPs alone.
Before WP-B5 the scrub name set was a HAND-TYPED literal in install.py that had
DRIFTED — it was missing ``mermaid`` / ``excalidraw`` / ``playwright``, so
uninstall left VCO's own diagram + browser MCP entries behind. WP-B5:

  * The scrub name set is sourced from ``vco_lib/mcp_scan_rules.toml``
    ([bundled].uninstall_scrub_names) — a DISTINCT list from the registration
    set (it carries the backend ``code-embedding`` id and the Pro-tier
    ``vct-coordination`` id whose scrub rationale differs).
  * The decision of which on-disk entries to remove lives in a pure function
    ``vco_lib.install_mcp.uninstall_scrub_mcp_names(install_root, claude_json)``.
  * BEHAVIOR CHANGE: uninstall now ALSO scrubs mermaid/excalidraw/playwright —
    but ONLY when the on-disk entry is positively VCO-shaped. A user's own
    mermaid / excalidraw / playwright (npx-based or at a path outside the
    install root) SURVIVES.

These are DESTRUCTIVE-GATE tests: every branch that gates the delete gets both
an ACT case (VCO-shaped entry scrubbed) and a LEAVE-ALONE case (user entry
survives). Hermetic: the ``~/.claude.json`` is a ``tmp_path`` file, never the
real user home; ``install_root`` is a tmp dir.
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

from vco_lib import mcp_scan_rules  # noqa: E402
from vco_lib.install_mcp import (  # noqa: E402
    _UNINSTALL_SCRUB_MCP_NAMES,
    _UNINSTALL_SCRUB_SHAPE_GATED,
    _is_vco_shaped_playwright_entry,
    _mcp_entry_path_inside_install_root,
    uninstall_scrub_mcp_names,
)


class _ScrubTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.install_root = self.root / "install"
        self.install_root.mkdir()
        self.claude_json = self.root / ".claude.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, servers: dict) -> None:
        self.claude_json.write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )

    def _scrub(self) -> list[str]:
        return uninstall_scrub_mcp_names(self.install_root, self.claude_json)

    # Path shapes ---------------------------------------------------------
    def _vco_python(self) -> str:
        # A venv-python path INSIDE the install root (how VCO writes
        # weaviate-kg / mermaid / excalidraw commands).
        return str(self.install_root / ".venv" / "bin" / "python")

    def _vco_wrapper_entry(self, module: str) -> dict:
        return {
            "type": "stdio",
            "command": self._vco_python(),
            "args": ["-m", module],
            "env": {"PYTHONPATH": str(self.install_root / "claude_mcp_servers")},
        }


# ── Table sourcing (the name set is the DISTINCT scrub list) ──────────────
class TestScrubSetSourcedFromTable(_ScrubTestBase):
    def test_scrub_names_equal_table(self) -> None:
        self.assertEqual(
            tuple(_UNINSTALL_SCRUB_MCP_NAMES),
            mcp_scan_rules.uninstall_scrub_mcp_names(),
        )

    def test_scrub_set_includes_the_previously_missing_three(self) -> None:
        # The drift the migration fixes: mermaid/excalidraw/playwright were
        # absent from the hand-typed install.py set.
        for name in ("mermaid", "excalidraw", "playwright"):
            self.assertIn(name, _UNINSTALL_SCRUB_MCP_NAMES)

    def test_scrub_set_retains_vco_exclusive_ids(self) -> None:
        for name in (
            "weaviate-kg", "ollama", "search",
            "code-embedding", "vct-coordination",
        ):
            self.assertIn(name, _UNINSTALL_SCRUB_MCP_NAMES)

    def test_shape_gated_is_exactly_the_dual_use_three(self) -> None:
        self.assertEqual(
            set(_UNINSTALL_SCRUB_SHAPE_GATED),
            {"mermaid", "excalidraw", "playwright"},
        )


# ── ACT: VCO-shaped entries get scrubbed ──────────────────────────────────
class TestActScrubsVcoShapedEntries(_ScrubTestBase):
    def test_vco_exclusive_ids_removed_by_name(self) -> None:
        # The five VCO-exclusive ids are removed by name (no shape gate) —
        # the pre-v0.2.83 behavior, unchanged.
        self._write({
            "weaviate-kg": {"command": self._vco_python(), "args": ["srv.py"]},
            "ollama": {"command": "/opt/legacy/ollama-mcp"},
            "search": {"command": str(self.install_root / "claude_mcp_servers" / "search_mcp" / "wrapper.sh")},
            "code-embedding": {"command": "whatever"},
            "vct-coordination": {"command": "npx", "args": ["-y", "@vct/coord"]},
        })
        removed = self._scrub()
        for name in (
            "weaviate-kg", "ollama", "search",
            "code-embedding", "vct-coordination",
        ):
            self.assertIn(name, removed)

    def test_vco_shaped_mermaid_and_excalidraw_removed(self) -> None:
        # VCO writes mermaid/excalidraw as venv-python -m ...proxy INSIDE the
        # install root → positively VCO-shaped → scrubbed.
        self._write({
            "mermaid": self._vco_wrapper_entry(
                "claude_mcp_servers.wrappers.mermaid_proxy"
            ),
            "excalidraw": self._vco_wrapper_entry(
                "claude_mcp_servers.wrappers.excalidraw_proxy"
            ),
        })
        removed = self._scrub()
        self.assertIn("mermaid", removed)
        self.assertIn("excalidraw", removed)

    def test_vco_shaped_playwright_npx_fingerprint_removed(self) -> None:
        # VCO's playwright is `npx -y @playwright/mcp@latest` (no install-root
        # path). The exact fingerprint IS positively VCO-shaped → scrubbed.
        self._write({
            "playwright": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest"],
                "env": {},
            },
        })
        self.assertIn("playwright", self._scrub())

    def test_full_vco_install_all_eight_scrubbed(self) -> None:
        # A realistic post-install ~/.claude.json: every VCO entry present +
        # VCO-shaped → all removed, none left behind.
        self._write({
            "weaviate-kg": {"command": self._vco_python(), "args": ["srv.py"]},
            "search": {"command": str(self.install_root / "claude_mcp_servers" / "search_mcp" / "wrapper.sh")},
            "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
            "mermaid": self._vco_wrapper_entry("claude_mcp_servers.wrappers.mermaid_proxy"),
            "excalidraw": self._vco_wrapper_entry("claude_mcp_servers.wrappers.excalidraw_proxy"),
            "ollama": {"command": self._vco_python()},
            "code-embedding": {"command": self._vco_python()},
            "vct-coordination": {"command": self._vco_python()},
        })
        removed = set(self._scrub())
        self.assertEqual(removed, set(_UNINSTALL_SCRUB_MCP_NAMES))


# ── LEAVE-ALONE: user entries survive ─────────────────────────────────────
class TestLeaveAloneUserEntries(_ScrubTestBase):
    def test_user_npx_mermaid_survives(self) -> None:
        # A user's own mermaid via npx (no install-root path, not VCO's
        # wrapper) is NOT VCO-shaped → survives uninstall.
        self._write({
            "mermaid": {"command": "npx", "args": ["-y", "@someone/mermaid-mcp"]},
        })
        self.assertNotIn("mermaid", self._scrub())

    def test_user_mermaid_at_outside_path_survives(self) -> None:
        self._write({
            "mermaid": {"command": "/home/dev/my-mermaid/serve.py"},
        })
        self.assertNotIn("mermaid", self._scrub())

    def test_user_excalidraw_outside_path_survives(self) -> None:
        self._write({
            "excalidraw": {
                "command": "/opt/excalidraw-mcp/node",
                "args": ["/opt/excalidraw-mcp/index.js"],
            },
        })
        self.assertNotIn("excalidraw", self._scrub())

    def test_user_playwright_variant_survives(self) -> None:
        # A user's playwright with a DIFFERENT invocation (missing -y, or a
        # pinned different tag) does not match VCO's fingerprint → survives.
        for args in (
            ["@playwright/mcp"],                     # no -y
            ["-y", "@playwright/mcp@1.2.3"],         # pinned tag
            ["-y", "@acme/playwright-fork@latest"],  # different package
        ):
            with self.subTest(args=args):
                self._write({"playwright": {"command": "npx", "args": args}})
                self.assertNotIn("playwright", self._scrub())

    def test_user_playwright_at_absolute_path_outside_root_survives(self) -> None:
        self._write({
            "playwright": {"command": "/usr/local/bin/my-playwright-mcp"},
        })
        self.assertNotIn("playwright", self._scrub())

    def test_wholly_unrelated_user_mcps_never_touched(self) -> None:
        # Names that are not VCO scrub names at all — never considered.
        self._write({
            "searxng": {"command": "/usr/local/bin/searxng-mcp"},
            "jira": {"command": "/opt/jira-mcp/serve"},
            "gmail": {"command": "npx", "args": ["-y", "@acme/gmail-mcp"]},
        })
        removed = self._scrub()
        self.assertEqual(removed, [])

    def test_user_shaped_entries_survive_alongside_vco_ones(self) -> None:
        # The spicy mixed case: VCO's mermaid AND a user's own mermaid can't
        # coexist under the same key, so use the realistic form — VCO-shaped
        # excalidraw removed, user's npx mermaid + user's playwright kept.
        self._write({
            "excalidraw": self._vco_wrapper_entry(
                "claude_mcp_servers.wrappers.excalidraw_proxy"
            ),
            "mermaid": {"command": "npx", "args": ["-y", "@someone/mermaid-mcp"]},
            "playwright": {"command": "/opt/mine/pw-mcp"},
            "weaviate-kg": {"command": self._vco_python(), "args": ["srv.py"]},
            "my-custom": {"command": "/home/dev/custom/serve"},
        })
        removed = set(self._scrub())
        self.assertEqual(removed, {"excalidraw", "weaviate-kg"})
        self.assertNotIn("mermaid", removed)
        self.assertNotIn("playwright", removed)
        self.assertNotIn("my-custom", removed)


# ── Predicate units ───────────────────────────────────────────────────────
class TestShapePredicates(_ScrubTestBase):
    def test_path_inside_install_root_true_for_vco_command(self) -> None:
        self.assertTrue(
            _mcp_entry_path_inside_install_root(
                {"command": self._vco_python()}, self.install_root
            )
        )

    def test_path_inside_install_root_true_for_vco_first_arg(self) -> None:
        self.assertTrue(
            _mcp_entry_path_inside_install_root(
                {"command": "python", "args": [str(self.install_root / "srv.py")]},
                self.install_root,
            )
        )

    def test_path_inside_install_root_false_for_outside_path(self) -> None:
        self.assertFalse(
            _mcp_entry_path_inside_install_root(
                {"command": "/opt/elsewhere/serve"}, self.install_root
            )
        )

    def test_path_inside_install_root_false_for_npx(self) -> None:
        self.assertFalse(
            _mcp_entry_path_inside_install_root(
                {"command": "npx", "args": ["-y", "@x/y"]}, self.install_root
            )
        )

    def test_playwright_fingerprint_exact_match(self) -> None:
        self.assertTrue(
            _is_vco_shaped_playwright_entry(
                {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
            )
        )

    def test_playwright_fingerprint_rejects_variants(self) -> None:
        self.assertFalse(
            _is_vco_shaped_playwright_entry(
                {"command": "npx", "args": ["@playwright/mcp"]}
            )
        )
        self.assertFalse(
            _is_vco_shaped_playwright_entry({"command": "node", "args": []})
        )


# ── Robustness: malformed / absent input never raises ─────────────────────
class TestScrubRobustness(_ScrubTestBase):
    def test_missing_claude_json_returns_empty(self) -> None:
        # No file at all.
        self.assertEqual(self._scrub(), [])

    def test_malformed_json_returns_empty(self) -> None:
        self.claude_json.write_text("{not json", encoding="utf-8")
        self.assertEqual(self._scrub(), [])

    def test_no_mcp_servers_key_returns_empty(self) -> None:
        self.claude_json.write_text(json.dumps({"other": 1}), encoding="utf-8")
        self.assertEqual(self._scrub(), [])

    def test_non_dict_entry_for_shape_gated_survives(self) -> None:
        # A shape-gated name whose value isn't a dict can't be proven VCO —
        # leave it alone (conservative default).
        self._write({"mermaid": "not-a-dict"})
        self.assertEqual(self._scrub(), [])


if __name__ == "__main__":
    unittest.main()
