# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.31's citation-monitor slug fix.

The RL citation-monitor at ``claude_mcp_servers/weaviate_mcp/server.py``
computes the Claude session-jsonl directory slug to find the
transcript files it needs to poll for Claude's post-search answer.
Pre-v0.2.31 the slug rule was inlined as
``str(workspace).replace("/", "-")`` — which missed ``_`` → ``-``
substitution and caused a 97.7% orphan-citation rate on workspaces
whose absolute paths contained underscores (``VCO_dev``, ``AI_hive``,
…). See ``.claude/context/plans/rl-citation-monitor-bug-report-2026-05-23.md``.

v0.2.31 routes the lookup through ``vct-hub`` (canonical source of
truth, computes the slug via the launcher's
``claude_session_dir_for`` helper) with a local-slug fallback that
implements the COMPLETE rule (``/`` + ``_`` + ``.`` → ``-``).

These tests pin both code paths:

* ``test_resolve_prefers_hub_response_when_available`` — when the
  hub-resolved ProjectConfig carries a ``claude_session_dir`` value,
  the MCP helper returns it verbatim regardless of the workspace
  path passed in (proves the hub is authoritative).

* ``test_resolve_falls_back_to_local_slug_when_hub_unreachable`` —
  when the hub resolver returns ``None`` (HubUnreachable, project
  not registered, free-tier install without launcher, …), the
  helper falls back to the local slug rule WITH the complete
  substitution set. Pins the v0.2.31 bug-fix.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402


class ResolveClaudeSessionDirTest(unittest.TestCase):
    """v0.2.31 — pin the hub-primary + local-fallback paths."""

    def setUp(self) -> None:
        # Reset the module-level cache so a previous test's hub stub
        # doesn't bleed into ours. The MCP caches resolve() output for
        # the lifetime of the process (cheap on the cached path); for
        # tests we want a fresh resolver result every call.
        srv._resolved_project_config = None

    def tearDown(self) -> None:
        srv._resolved_project_config = None

    def test_resolve_prefers_hub_response_when_available(self) -> None:
        """Hub-resolved ``claude_session_dir`` wins over local computation.

        The hub knows the canonical slug rule + has the registered
        ``projects.folder_path`` — it's the source of truth. The MCP
        must use whatever the hub returns, even if the local-slug
        computation would have produced a different (or no) value.
        """
        # Build a hub path that has NOTHING to do with the workspace
        # arg's local slug — proves the helper isn't secretly re-
        # computing on the side.
        hub_path = (
            Path.home() / ".claude" / "projects"
            / "-arbitrary-hub-chosen-name"
        )
        # Ensure the chosen path exists on disk so the helper's
        # `.exists()` check doesn't reject the hub answer.
        with tempfile.TemporaryDirectory() as td:
            existing_hub_dir = Path(td) / "fake-claude-projects" / "hub-slug"
            existing_hub_dir.mkdir(parents=True)

            class FakeCfg:
                claude_session_dir = str(existing_hub_dir)

            with patch.object(srv, "_try_resolve_project_config", return_value=FakeCfg()):
                result = asyncio.run(srv._resolve_claude_session_dir(
                    Path("/anything/that/doesnt/match")
                ))

            self.assertEqual(result, existing_hub_dir)

    def test_resolve_falls_back_to_local_slug_when_hub_unreachable(self) -> None:
        """When the hub returns None, the local slug rule must cover
        ``/`` + ``_`` → ``-`` so the citation monitor still works.

        This is the v0.2.31 bug-fix: the pre-fix inline computation
        only handled ``/`` → ``-`` and broke for any underscored
        workspace path (``VCO_dev``, ``AI_hive``).
        """
        # Make the hub resolver return None (e.g. HubUnreachable on
        # a free-tier install without the launcher GUI running). The
        # helper must fall back to the local slug rule.
        with tempfile.TemporaryDirectory() as td:
            # Build a workspace path with an underscore and seed a
            # fake ~/.claude/projects/<correct-slug>/ directory so the
            # helper's `.exists()` check passes.
            fake_home = Path(td)
            workspace = Path("/home/user/Desktop/PROGETTI/VCO_dev")
            expected_slug = "-home-user-Desktop-PROGETTI-VCO-dev"
            (fake_home / ".claude" / "projects" / expected_slug).mkdir(
                parents=True
            )

            with patch.object(srv, "_try_resolve_project_config", return_value=None), \
                 patch.object(Path, "home", return_value=fake_home):
                result = asyncio.run(srv._resolve_claude_session_dir(workspace))

            self.assertIsNotNone(
                result,
                "fallback path must resolve to the existing slug dir, not None",
            )
            self.assertEqual(
                result.name,
                expected_slug,
                "fallback slug must convert '_' to '-' (the v0.2.31 bug-fix); "
                f"got: {result.name}",
            )

    def test_resolve_returns_none_when_neither_hub_nor_local_dir_exists(self) -> None:
        """If both code paths produce a path that doesn't exist on disk
        (fresh workspace that's never been opened in Claude Code), the
        helper returns None so the caller skips poll attempts cleanly.
        """
        with tempfile.TemporaryDirectory() as td:
            # No projects subdir under fake_home → both code paths
            # land on a non-existent slug dir.
            fake_home = Path(td)
            workspace = Path("/home/user/some-fresh-workspace")

            with patch.object(srv, "_try_resolve_project_config", return_value=None), \
                 patch.object(Path, "home", return_value=fake_home):
                result = asyncio.run(srv._resolve_claude_session_dir(workspace))

            self.assertIsNone(result)

    def test_resolve_ignores_empty_claude_session_dir_from_hub(self) -> None:
        """A v0.2.31+ MCP paired with a pre-v0.2.31 hub sees an empty
        ``claude_session_dir`` (back-filled in the dataclass parser).
        The helper must treat empty as "no hub value" and fall through
        to local computation rather than returning Path("") or similar.
        """
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            workspace = Path("/home/user/AI_hive")
            expected_slug = "-home-user-AI-hive"
            (fake_home / ".claude" / "projects" / expected_slug).mkdir(
                parents=True
            )

            class CfgWithEmptyField:
                claude_session_dir = ""  # pre-v0.2.31 hub sentinel

            with patch.object(srv, "_try_resolve_project_config", return_value=CfgWithEmptyField()), \
                 patch.object(Path, "home", return_value=fake_home):
                result = asyncio.run(srv._resolve_claude_session_dir(workspace))

            self.assertIsNotNone(result)
            self.assertEqual(result.name, expected_slug)


if __name__ == "__main__":
    unittest.main()
