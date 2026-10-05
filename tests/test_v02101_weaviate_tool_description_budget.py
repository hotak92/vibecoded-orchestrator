# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 — weaviate-kg tool-description budget.

Every tool's schema is ALWAYS in a session's context while the MCP server is
connected, so its ``description`` (the function docstring, which FastMCP
serialises into ``tools/list``) is a permanent token cost paid by every chat
and every subagent. The six tools once carried 13 255 chars of descriptions;
v0.2.101 trims them to what a caller needs and pins the result with a hard
ceiling so it cannot silently regrow.

The trim MOVES the reference material OUT to a knowledge node rather than
deleting it (the tiers' numeric thresholds, the "when NOT to use" essays).
That is a promise-retired-with-its-mechanism shape: a guard below asserts the
destination doc actually exists and still carries the thresholds.

Measured with :func:`ast.get_docstring` (the same dedent FastMCP applies),
never by importing the server — the module pulls in ``weaviate-client`` and
boots heavy global state, so a parse keeps this test fast and hermetic.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py"
TIER_DOC = (
    REPO_ROOT / "templates" / "knowledge" / "concepts"
    / "score-driven-retrieval-tiers.md"
)

# The six user-facing tools whose docstrings become MCP tool descriptions.
TOOL_NAMES = (
    "hybrid_search",
    "search_code_graph",
    "store_knowledge_node",
    "semantic_graph_search",
    "query_code_structure",
    "describe_excalidraw",
)

# Hard ceiling on the SUM of the six descriptions. Was 13 255 before the trim;
# the v0.2.101 texts measure 3 464 (the layer filter's accepted values in
# search_code_graph are restored — a caller-facing contract — and four
# docstrings were tightened to fit). 36 chars of headroom: a short wording
# tweak fits, a re-pasted essay does not.
DESCRIPTION_BUDGET = 3500

# The server ``instructions`` string is a second always-in-context cost.
# Measured 587; the ceiling leaves ~63 chars of headroom by the same rule
# (raised from 600 in the same change — the 587-char text left 13).
INSTRUCTIONS_BUDGET = 650

# A score-tier range like "0.42..0.55" or ">= 0.75" belongs in the tier doc,
# not in a tool description (it drifts whenever KG_TIER_* moves).
_NUMERIC_TIER_RANGE = re.compile(r"0\.\d{2}\s*(\.\.|\+|to|–|-)\s*0?\.?\d{0,2}|>=\s*0\.\d{2}")


def _tool_descriptions() -> dict[str, str]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in TOOL_NAMES:
            doc = ast.get_docstring(node)
            assert doc is not None, f"{node.name} must carry a docstring (its description)"
            found[node.name] = doc
    return found


def _server_instructions() -> str:
    """The literal first argument of the module-level ``FastMCP(...)`` call."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "mcp" for t in node.targets)
            and isinstance(node.value, ast.Call)
        ):
            for kw in node.value.keywords:
                if kw.arg == "instructions" and isinstance(kw.value, ast.Constant):
                    return kw.value.value
    raise AssertionError("module-level FastMCP(instructions=...) not found")


class WeaviateToolDescriptionBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(SERVER.is_file(), f"missing {SERVER}")
        self.descriptions = _tool_descriptions()

    def test_all_six_tools_are_present(self) -> None:
        self.assertEqual(set(self.descriptions), set(TOOL_NAMES))

    def test_total_description_chars_within_budget(self) -> None:
        total = sum(len(d) for d in self.descriptions.values())
        per_tool = {name: len(d) for name, d in self.descriptions.items()}
        self.assertLessEqual(
            total,
            DESCRIPTION_BUDGET,
            f"weaviate-kg tool descriptions total {total} chars "
            f"(> {DESCRIPTION_BUDGET}); per tool: {per_tool}. Move reference "
            f"material to knowledge/concepts/score-driven-retrieval-tiers.md.",
        )

    def test_no_numeric_tier_ranges_in_descriptions(self) -> None:
        """The `0.42..0.55`-style ranges live in the tier doc, not in a
        description — re-adding one fails here. Red-proof: paste the old
        hybrid_search docstring back → this trips."""
        for name, doc in self.descriptions.items():
            self.assertIsNone(
                _NUMERIC_TIER_RANGE.search(doc),
                f"{name} description reintroduced numeric tier thresholds; "
                f"they belong in score-driven-retrieval-tiers.md",
            )

    def test_descriptions_point_at_the_moved_reference(self) -> None:
        """The trim MOVED content — the destination must still be named, and
        must still exist (a promise retired with its mechanism)."""
        for name in ("hybrid_search", "search_code_graph", "store_knowledge_node"):
            self.assertIn(
                "score-driven-retrieval-tiers.md"
                if name != "store_knowledge_node"
                else "VOCABULARY.md",
                self.descriptions[name],
                f"{name} must point a caller at the doc holding the moved detail",
            )

    def test_score_tier_doc_exists_and_keeps_thresholds(self) -> None:
        self.assertTrue(
            TIER_DOC.is_file(),
            f"the moved tier thresholds must live in {TIER_DOC}",
        )
        text = TIER_DOC.read_text(encoding="utf-8")
        for needle in ("0.42", "0.55", "0.65", "0.75"):
            self.assertIn(
                needle, text,
                f"tier doc must still document the {needle} threshold boundary",
            )

    def test_server_instructions_within_budget(self) -> None:
        text = _server_instructions()
        self.assertLessEqual(
            len(text),
            INSTRUCTIONS_BUDGET,
            f"server instructions are {len(text)} chars (> {INSTRUCTIONS_BUDGET})",
        )
        # The core routing guidance must survive the trim.
        for needle in ("hybrid_search", "semantic_graph_search",
                       "search_code_graph", "query_code_structure"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()