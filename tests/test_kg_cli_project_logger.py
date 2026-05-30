# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Regression tests for v0.2.40 H1 — CLI scripts pass project= to ToolUsageLogger.

Pre-fix the three KG CLI scripts (``templates/scripts/search_knowledge.py``,
``get_node_info.py``, ``sync_knowledge_graph.py``) invoked
``ToolUsageLogger.log_kg_search`` / ``log_kg_info`` / ``log_kg_sync``
without the ``project=`` kwarg, so every tool_usage.jsonl row's
``project`` field fell back to the logger's historic
``"claude-orchestrator"`` default regardless of which workspace ran
the command.

Post-fix each call site resolves the canonical project name via
``vco_lib.paths.resolve_project_name()`` and passes the resolved value
through ``project=``. When resolution yields ``None`` (no hub, no env
vars), ``None`` is passed and the logger's historic default kicks in
— so the change is purely additive when no project context exists.

Tests:

  T1: ``resolve_project_name()`` picks up CODE_GRAPH_PROJECT / PROJECT_NAME
      env vars when set, returns None when neither is set.
  T2: ``ToolUsageLogger.log_kg_*`` calls write the passed project value
      into the JSONL entry; None preserves the historic default.
  T3: Static contract — each of the three CLI scripts passes the
      ``project=`` kwarg at its ``ToolUsageLogger.log_kg_*`` call site.
      Guards against regression if a future edit drops the kwarg.

The three CLI scripts are sourced from ``templates/scripts/`` (the
canonical source for the per-project ``.claude/scripts/`` copy that
``install-bundle --update`` propagates).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class ResolveProjectNameTests(unittest.TestCase):
    """T1 — ``vco_lib.paths.resolve_project_name`` env-fallback contract."""

    def setUp(self) -> None:
        # Force the hub branch to fail so we exercise the env-var path
        # deterministically. Otherwise a running launcher on the dev
        # machine would inject its own value into the test.
        self._env_patcher = mock.patch.dict(
            os.environ,
            {"CODE_GRAPH_PROJECT": "", "PROJECT_NAME": "", "VCT_HUB_PORT": "1"},
            clear=False,
        )
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()

    def _resolve(self) -> "str | None":
        # Re-import inside the test so each call picks up the patched
        # env. The helper itself reads ``os.environ`` at call time so
        # this is just paranoia, but keeps the test self-contained.
        from vco_lib.paths import resolve_project_name
        return resolve_project_name()

    def test_code_graph_project_env_wins_over_project_name(self) -> None:
        """T1a — CODE_GRAPH_PROJECT env var is preferred over PROJECT_NAME."""
        with mock.patch.dict(
            os.environ,
            {"CODE_GRAPH_PROJECT": "VCODev", "PROJECT_NAME": "VibeCoded Dev"},
        ):
            # Force the hub path to soft-fail by pointing it at a port
            # nothing is listening on. The helper's ``except Exception:``
            # swallows the resulting error and falls through to env.
            with mock.patch(
                "vco_lib.project_config.resolve",
                side_effect=RuntimeError("hub down"),
            ):
                self.assertEqual(self._resolve(), "VCODev")

    def test_project_name_env_used_when_code_graph_project_unset(self) -> None:
        """T1b — falls back to PROJECT_NAME when CODE_GRAPH_PROJECT is empty."""
        with mock.patch.dict(
            os.environ,
            {"CODE_GRAPH_PROJECT": "", "PROJECT_NAME": "VibeCoded Dev"},
        ):
            with mock.patch(
                "vco_lib.project_config.resolve",
                side_effect=RuntimeError("hub down"),
            ):
                self.assertEqual(self._resolve(), "VibeCoded Dev")

    def test_returns_none_when_nothing_set(self) -> None:
        """T1c — returns None when neither env var is set (no anchor)."""
        # Env vars already cleared in setUp; just stub the hub.
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=RuntimeError("hub down"),
        ):
            self.assertIsNone(self._resolve())

    def test_empty_string_env_treated_as_unset(self) -> None:
        """T1d — explicit empty string env values are not used.

        Matches the documented contract: empty == unset (same rule the
        rest of the resolver chain follows).
        """
        with mock.patch.dict(
            os.environ,
            {"CODE_GRAPH_PROJECT": "   ", "PROJECT_NAME": ""},
        ):
            with mock.patch(
                "vco_lib.project_config.resolve",
                side_effect=RuntimeError("hub down"),
            ):
                self.assertIsNone(self._resolve())

    def test_hub_value_preferred_over_env(self) -> None:
        """T1e — when the hub responds with code_graph_project, use it.

        Even if env vars also point at a (potentially-stale) name, the
        hub is the canonical per-project source of truth.
        """
        with mock.patch.dict(
            os.environ,
            {"CODE_GRAPH_PROJECT": "FromEnv", "PROJECT_NAME": "FromEnv"},
        ):
            stub_cfg = mock.MagicMock()
            stub_cfg.code_graph_project = "FromHub"
            with mock.patch(
                "vco_lib.project_config.resolve",
                return_value=stub_cfg,
            ):
                self.assertEqual(self._resolve(), "FromHub")


class ToolUsageLoggerProjectFieldTests(unittest.TestCase):
    """T2 — JSONL rows carry the project= value the caller passed.

    Sanity-check on the existing ``ToolUsageLogger`` API: it has always
    accepted ``project: Optional[str] = None`` and stamped the value (or
    the historic default) on the row. We test it here so a future
    refactor that changes the default behaviour doesn't silently break
    the H1 contract.
    """

    def setUp(self) -> None:
        self._mcp_dir = REPO_ROOT / "claude_mcp_servers"
        if str(self._mcp_dir) not in sys.path:
            sys.path.insert(0, str(self._mcp_dir))

    def _read_one(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        self.assertEqual(len(lines), 1, f"expected exactly one row in {path}")
        return json.loads(lines[0])

    def test_log_kg_search_stamps_project_when_passed(self) -> None:
        from weaviate_mcp.query_logger import ToolUsageLogger
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tool_usage.jsonl"
            with mock.patch(
                "weaviate_mcp.query_logger.TOOL_USAGE_LOG", log
            ):
                ToolUsageLogger.log_kg_search(
                    query="test",
                    project="VCODev",
                )
            row = self._read_one(log)
            self.assertEqual(row["project"], "VCODev")
            self.assertEqual(row["tool"], "kg-search")

    def test_log_kg_info_stamps_project_when_passed(self) -> None:
        from weaviate_mcp.query_logger import ToolUsageLogger
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tool_usage.jsonl"
            with mock.patch(
                "weaviate_mcp.query_logger.TOOL_USAGE_LOG", log
            ):
                ToolUsageLogger.log_kg_info(
                    node_title="Some Node",
                    project="VCODev",
                )
            row = self._read_one(log)
            self.assertEqual(row["project"], "VCODev")
            self.assertEqual(row["tool"], "kg-info")

    def test_log_kg_sync_stamps_project_when_passed(self) -> None:
        from weaviate_mcp.query_logger import ToolUsageLogger
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tool_usage.jsonl"
            with mock.patch(
                "weaviate_mcp.query_logger.TOOL_USAGE_LOG", log
            ):
                ToolUsageLogger.log_kg_sync(
                    file_path="knowledge/foo.md",
                    project="VCODev",
                )
            row = self._read_one(log)
            self.assertEqual(row["project"], "VCODev")
            self.assertEqual(row["tool"], "kg-sync")

    def test_project_none_falls_back_to_historic_default(self) -> None:
        """T2 — passing ``project=None`` preserves the historic
        ``"claude-orchestrator"`` default. The H1 fix never breaks the
        no-context path; it only stamps real values when available.
        """
        from weaviate_mcp.query_logger import ToolUsageLogger
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "tool_usage.jsonl"
            with mock.patch(
                "weaviate_mcp.query_logger.TOOL_USAGE_LOG", log
            ):
                ToolUsageLogger.log_kg_search(
                    query="test",
                    project=None,
                )
            row = self._read_one(log)
            # Pre-H1 behaviour preserved.
            self.assertEqual(row["project"], "claude-orchestrator")


class CliCallSiteContractTests(unittest.TestCase):
    """T3 — static guarantee that each CLI script passes ``project=``.

    Source-text scan rather than runtime exec because the three scripts
    talk to a live Weaviate instance and we can't stand one up inside
    a unit test cheaply. The contract we need to preserve is structural:
    each ``ToolUsageLogger.log_kg_*`` call site must include the
    ``project=`` kwarg. A regression that removes the kwarg from any
    site would silently re-introduce the H1 bug.
    """

    SCRIPTS = (
        "templates/scripts/search_knowledge.py",
        "templates/scripts/get_node_info.py",
        "templates/scripts/sync_knowledge_graph.py",
    )

    def test_each_script_passes_project_kwarg_at_logger_call(self) -> None:
        for rel in self.SCRIPTS:
            with self.subTest(script=rel):
                script_path = REPO_ROOT / rel
                self.assertTrue(
                    script_path.is_file(),
                    f"missing source file: {rel}",
                )
                src = script_path.read_text(encoding="utf-8")
                # The post-H1 shape: the file imports resolve_project_name
                # from vco_lib.paths somewhere near the ToolUsageLogger
                # call site. We assert both the import AND the kwarg
                # passing, so a half-applied refactor (one without the
                # other) is caught.
                self.assertIn(
                    "from vco_lib.paths import resolve_project_name",
                    src,
                    f"{rel}: missing resolve_project_name import",
                )
                self.assertIn(
                    "project=_project",
                    src,
                    f"{rel}: ToolUsageLogger.log_kg_* call must pass project=",
                )


if __name__ == "__main__":
    unittest.main()
