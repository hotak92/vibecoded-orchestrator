# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.23 regression — slug-vs-prefix field contract in the code-graph path.

Context
-------
The hub's ``GET /api/v1/projects/{id}/config`` returns TWO superficially
similar fields:

  * ``code_graph_project``           — legacy alias of ``project_slug``
                                       (returned for callers that key on
                                       the slug, e.g. codegraph-access
                                       matrix joins). May be
                                       hyphen/underscore-mixed.
  * ``code_graph_collection_prefix`` — canonical Weaviate prefix sourced
                                       from ``project_codegraph_bindings.collection_prefix``.

These DIVERGE when the project slug is not already a valid Weaviate
class prefix (e.g. ``orchestrator-root`` → analyzer sanitises to
``Orchestrator_root`` but the binding row says
``VibeCodedOrchestrator``).

Pre-v0.2.23 the analyzer + hooks read ``code_graph_project`` and then
ran it through ``_sanitize_collection_prefix``, producing a DIFFERENT
prefix from the binding row. Symptom: every incremental write since
the project rename landed in zombie collections
(``Orchestrator_root_Code*``) while consumers + the launcher GUI
queried the canonical prefix (``VibeCodedOrchestrator_Code*``) and
saw 0 results.

This test pins the v0.2.23 contract: the analyzer's ``--from-resolver``
branch MUST use ``code_graph_collection_prefix``, NOT
``code_graph_project``, for the write-target prefix.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class AnalyzerFromResolverPrefixContract(unittest.TestCase):
    """Pin the analyzer to read code_graph_collection_prefix, not the slug alias."""

    def test_template_analyzer_uses_collection_prefix_in_from_resolver_branch(self):
        """templates/scripts/analyze_code_graph.py is the shipped source of truth.

        The ``--from-resolver`` branch (the only one that calls the hub) must
        assign ``project_name`` from ``cfg.code_graph_collection_prefix``,
        not from ``cfg.code_graph_project``. Pre-v0.2.23 it did the latter
        — that bug silently routed incremental writes to zombie
        collections after any project rename.
        """
        path = PROJECT_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        text = path.read_text(encoding="utf-8")

        # Locate the --from-resolver block. We anchor on the unique
        # ``if args.from_resolver:`` line, then scan the next ~40 lines.
        marker = "if args.from_resolver:"
        idx = text.find(marker)
        self.assertGreater(idx, 0, f"{path}: missing '--from-resolver' branch")
        block_end = text.find("\n    if not project_name:", idx)
        self.assertGreater(block_end, idx, f"{path}: malformed branch")
        block = text[idx:block_end]

        # The CORRECT assignment uses code_graph_collection_prefix.
        self.assertIn(
            "cfg.code_graph_collection_prefix",
            block,
            f"{path}: --from-resolver branch must assign from "
            "cfg.code_graph_collection_prefix (the binding-row truth), "
            "not cfg.code_graph_project (the slug alias).",
        )
        # The OLD bug — assigning from the slug alias — must not return.
        # An incidental docstring mention is fine; an actual `cfg.code_graph_project`
        # *assignment* is not.
        bad_assignment = re.search(
            r"project_name\s*=\s*cfg\.code_graph_project\b", block
        )
        self.assertIsNone(
            bad_assignment,
            f"{path}: --from-resolver branch must NOT assign project_name "
            "from cfg.code_graph_project (pre-v0.2.23 bug regression).",
        )


class WeaviateMcpServerPrefixContract(unittest.TestCase):
    """v0.2.23 W3 (2026-05-21) — pin the Weaviate MCP server's CODE_GRAPH_PROJECT
    initialization to read code_graph_collection_prefix, NOT the slug alias.

    The MCP server constructs `CODE_GRAPH_PROJECT` at module-import time from
    the hub-resolver response and uses it as the Weaviate collection prefix
    for code-graph reads/writes (`_code_collection(base)` returns
    `f"{prefix}_{base}"`). If sourced from the slug, post-rename projects
    silently query zombie collections.
    """

    def test_mcp_server_uses_collection_prefix_in_module_init(self):
        path = (
            PROJECT_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
        )
        text = path.read_text(encoding="utf-8")

        # Locate the module-init block. Anchor on the unique
        # `_cfg_for_cgp = _try_resolve_project_config()` line, then scan the
        # next ~8 lines (the assignment lives just below).
        marker = "_cfg_for_cgp = _try_resolve_project_config()"
        idx = text.find(marker)
        self.assertGreater(idx, 0, f"{path}: missing module-init marker")
        block = text[idx:idx + 600]

        # CORRECT assignment: from cfg.code_graph_collection_prefix.
        self.assertIn(
            "cfg_for_cgp.code_graph_collection_prefix",
            block,
            f"{path}: module-init MUST source CODE_GRAPH_PROJECT from "
            "cfg.code_graph_collection_prefix (the binding-row truth), not "
            "cfg.code_graph_project (the slug alias).",
        )
        # OLD bug — assigning CODE_GRAPH_PROJECT from the slug — must not
        # return. An incidental comment mentioning the legacy field is fine
        # (the docstring documents the v0.2.23 fix); an actual assignment
        # `CODE_GRAPH_PROJECT = _cfg_for_cgp.code_graph_project` is not.
        bad_assignment = re.search(
            r"CODE_GRAPH_PROJECT\s*=\s*_cfg_for_cgp\.code_graph_project\b",
            block,
        )
        self.assertIsNone(
            bad_assignment,
            f"{path}: module-init must NOT assign CODE_GRAPH_PROJECT from "
            "cfg.code_graph_project (pre-v0.2.23 W3 bug regression).",
        )


class HookResolverFieldContract(unittest.TestCase):
    """No shipped hook resolves a code-graph write target from the slug alias.

    v0.2.107 (CG-3): no hook resolves the code-graph prefix in shell any more
    — the per-edit hook and the Stop drain's queue half are retired, and the
    one writer (``vco_lib.codegraph_head_catchup``) reads the prefix in
    Python from ``code_graph_collection_prefix``. What stays pinned: no hook
    asks the resolver for the slug alias ``code_graph_project`` (it routes
    writes to a divergent zombie prefix after a rename), and the catch-up
    reads the canonical field.
    """

    def test_no_hook_asks_for_the_slug_alias(self):
        hooks = PROJECT_ROOT / "templates" / "hooks"
        for path in sorted(list(hooks.rglob("*.sh")) + list(hooks.rglob("*.ps1"))):
            code = "\n".join(
                ln for ln in path.read_text(encoding="utf-8").splitlines()
                if not ln.lstrip().startswith("#")
            )
            self.assertIsNone(
                re.search(r"-{1,2}[Ff]ield\s+[\"']?code_graph_project\b", code),
                f"{path}: asks the resolver for the slug alias code_graph_project",
            )

    def test_the_catchup_reads_the_canonical_prefix_field(self):
        text = (PROJECT_ROOT / "vco_lib" / "codegraph_head_catchup.py").read_text(
            encoding="utf-8")
        self.assertIn('"code_graph_collection_prefix"', text)
        self.assertNotIn('"code_graph_project"', text)


# v0.2.23's source-text pin of the replace()→insert() fallback lived here. It
# could not see that the not-found signal changed (Weaviate 1.37 + client 4.21:
# a 404, not "no object with id"), so every new entity was dropped while it
# stayed green. Superseded by the behavioural tests in
# tests/test_analyze_code_graph_extras.py (real exception, real _dedup_insert).


class HubAliasFieldDocumentation(unittest.TestCase):
    """Pin the hub-side docstring so future devs don't re-introduce the bug."""

    def test_config_api_warns_against_code_graph_project_as_write_target(self):
        """``config_api.rs`` must document that ``code_graph_project`` is
        the slug alias, and explicitly warn against using it as the
        Weaviate write-target prefix.
        """
        path = PROJECT_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "config_api.rs"
        if not path.exists():
            self.skipTest("config_api.rs not in this layout")
        text = path.read_text(encoding="utf-8")
        # The warning must mention the divergence risk between the slug
        # alias and the canonical prefix.
        self.assertIn("DO NOT use this as the Weaviate write-target prefix", text)
        self.assertIn("code_graph_collection_prefix", text)


if __name__ == "__main__":
    unittest.main()
