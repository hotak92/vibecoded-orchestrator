# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Contract + parity tests for the KG access-list prefix sanitizer.

v0.2.77 Part 7a-bis (task 1): the KG-collection prefix resolver has THREE
copies that must agree on the same rule (the underscore-DROPPING PascalCase
rule that the WRITER — ``vco_lib.codegraph_naming.sanitize_for_weaviate_class``
— stamps ``<prefix>_KnowledgeGraph`` / ``_Development`` / ``_Diagrams``
collections with):

  * WRITER SSOT:  ``vco_lib.codegraph_naming.sanitize_for_weaviate_class``
                  (re-exported as ``vco_lib.project_init.sanitize_for_weaviate_class``).
  * READER (MCP): ``weaviate_mcp.server._sanitize_collection_prefix``
                  (converged to the SSOT since v0.2.34 cr-b2).
  * READER (CLI): ``claude_mcp_servers/scripts/kg_access.py::
                  sanitize_collection_prefix`` (this file's subject — the last
                  copy still on the OLD divergent ``re.sub([^a-zA-Z0-9_],"_") +
                  upper-first`` rule until 7a-bis).

The divergence was REAL and reproduced: a spaced project name
``'My Cool App'`` writes its KG under ``MyCoolApp_KnowledgeGraph`` (writer
drops the spaces, PascalCases), but the CLI reader's old rule produced
``My_Cool_App`` → it fanned out to ``My_Cool_App_KnowledgeGraph`` — a
different (usually empty, potentially wrong-tenant) collection. Launcher-
managed access lists carry canonical prefixes from ``kg_collection_access``
rows so they were safe; a hand-built ``VCT_KG_ACCESS_LIST`` with a raw spaced
name silently mis-resolved.

No ambiguity / no try-both fallback is needed: the SSOT rule is IDEMPOTENT on
its own outputs AND on the legacy underscore form — ``'My_Cool_App'`` →
``'MyCoolApp'`` (underscores dropped), then stable. So the legacy underscore
collection name and the canonical name are NOT two live collections to
disambiguate: the underscore form was never a real writer output for a spaced
name; it was purely an artifact of the diverged reader. Converging the reader
onto the SSOT resolves both forms onto the single writer-created collection.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HELPER_DIR = PROJECT_ROOT / "claude_mcp_servers" / "scripts"
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(HELPER_DIR), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _fresh_kg_access():
    if "kg_access" in sys.modules:
        del sys.modules["kg_access"]
    return importlib.import_module("kg_access")


# Domain covering: spaced names, hyphen/dot names, already-canonical
# (PascalCase, no underscore), legacy underscore forms, all-caps acronym
# runs, lowercase, and pathological (empty / all-non-alnum / leading digit).
_DOMAIN = [
    "My Cool App",
    "MyCoolApp",
    "My_Cool_App",
    "VibeCoded Orchestrator",
    "VibeCodedOrchestrator",
    "Foo-Bar",
    "Foo_Bar",
    "Camel_Case",
    "foo bar",
    "VCODev",
    "Alpha",
    "alpha",
    "foo-bar.baz",
    "Vibe_Coded_Tools",
    "",
    "   ",
    "123abc",
    "!!!",
]


class WriterSsotConvergenceTests(unittest.TestCase):
    """The CLI reader must produce EXACTLY the writer SSOT output for
    every input in the domain."""

    def setUp(self) -> None:
        self.kg_access = _fresh_kg_access()
        from vco_lib.codegraph_naming import sanitize_for_weaviate_class

        self.writer = sanitize_for_weaviate_class

    def test_cli_reader_matches_writer_ssot(self) -> None:
        for name in _DOMAIN:
            with self.subTest(name=name):
                self.assertEqual(
                    self.kg_access.sanitize_collection_prefix(name),
                    self.writer(name),
                    f"CLI reader diverged from writer SSOT for {name!r}",
                )


class McpMirrorParityTests(unittest.TestCase):
    """The CLI reader and the MCP reader must be byte-behaviour-identical
    (they are documented mirrors; this pins the mirror against drift)."""

    def setUp(self) -> None:
        self.kg_access = _fresh_kg_access()
        # Import the MCP server fresh; it may pull heavy deps but we only
        # call the pure sanitizer.
        for mod in list(sys.modules):
            if mod.startswith("weaviate_mcp"):
                del sys.modules[mod]
        self.server = importlib.import_module("weaviate_mcp.server")

    def test_cli_matches_mcp_for_every_input(self) -> None:
        for name in _DOMAIN:
            with self.subTest(name=name):
                self.assertEqual(
                    self.kg_access.sanitize_collection_prefix(name),
                    self.server._sanitize_collection_prefix(name),
                    f"CLI/MCP sanitizer mirror drift for {name!r}",
                )


class IdempotencyTests(unittest.TestCase):
    """The reader is idempotent on its own outputs AND collapses the legacy
    underscore form — the property that removes the try-both ambiguity."""

    def setUp(self) -> None:
        self.kg_access = _fresh_kg_access()

    # Pathological inputs (empty / all-symbol / leading-digit) resolve to
    # the LOWERCASE sentinel "vct", which is itself not a fixed point
    # (``sanitize_for_weaviate_class("vct") == "Vct"``). This one-step
    # sentinel-casing is inherent to the writer SSOT (matching it is
    # REQUIRED for parity), not a mirror bug — so idempotency is asserted
    # over the valid-prefix domain, where it holds exactly.
    _VALID_IDEMPOTENT_DOMAIN = [
        n for n in _DOMAIN if n.strip() and n not in ("!!!", "123abc")
    ]

    def test_idempotent_on_own_output(self) -> None:
        for name in self._VALID_IDEMPOTENT_DOMAIN:
            with self.subTest(name=name):
                once = self.kg_access.sanitize_collection_prefix(name)
                twice = self.kg_access.sanitize_collection_prefix(once)
                self.assertEqual(once, twice, f"not idempotent for {name!r}")

    def test_sentinel_is_stable_after_one_step(self) -> None:
        # Documents the sentinel-casing property explicitly.
        self.assertEqual(self.kg_access.sanitize_collection_prefix("!!!"), "vct")
        self.assertEqual(self.kg_access.sanitize_collection_prefix("vct"), "Vct")
        self.assertEqual(self.kg_access.sanitize_collection_prefix("Vct"), "Vct")

    def test_legacy_underscore_collapses_to_canonical(self) -> None:
        # The pre-7a-bis reader would have produced 'My_Cool_App'; the
        # converged reader collapses it onto the writer collection prefix.
        self.assertEqual(
            self.kg_access.sanitize_collection_prefix("My_Cool_App"),
            "MyCoolApp",
        )
        self.assertEqual(
            self.kg_access.sanitize_collection_prefix("Vibe_Coded_Tools"),
            "VibeCodedTools",
        )


class ActTestSpacedNameResolvesWriterCollection(unittest.TestCase):
    """ACT: a raw spaced project name in VCT_KG_ACCESS_LIST resolves to the
    SAME ``<prefix>_KnowledgeGraph`` collection the writer creates."""

    def setUp(self) -> None:
        import os

        os.environ["VCT_KG_ACCESS_LIST"] = "My Cool App"
        self.kg_access = _fresh_kg_access()
        self.addCleanup(os.environ.pop, "VCT_KG_ACCESS_LIST", None)

    def test_spaced_peer_resolves_writer_created_collection(self) -> None:
        from vco_lib.project_init import derive_project_kg_name

        writer_collection = derive_project_kg_name("My Cool App")
        self.assertEqual(writer_collection, "MyCoolApp_KnowledgeGraph")
        self.assertEqual(
            self.kg_access.kg_peer_collections(),
            [writer_collection],
        )


class CodeGraphSanitizerConvergenceTests(unittest.TestCase):
    """The CLI code-graph sanitizer must match the writer SSOT
    (``canonical_class_prefix``, underscore-PRESERVING) AND the MCP mirror
    (``server._code_sanitize_collection_prefix``) for every input.

    This is a SEPARATE rule from the KG sanitizer: it PRESERVES underscores
    and maps hyphens/dots to underscores, whereas the KG rule drops them.
    """

    # canonical_class_prefix RAISES on empty / leading-digit / all-symbol
    # names; the reader must not crash. Domain excludes the raising cases
    # for the direct writer-match (they're covered by the no-crash test).
    _VALID_DOMAIN = [
        "My Cool App",
        "MyCoolApp",
        "My_Cool_App",
        "VibeCoded Orchestrator",
        "VibeCodedOrchestrator",
        "Foo-Bar",
        "Foo_Bar",
        "Camel_Case",
        "foo bar",
        "VCODev",
        "Alpha",
        "alpha",
        "foo-bar.baz",
        "Vibe_Coded_Tools",
    ]

    def setUp(self) -> None:
        self.kg_access = _fresh_kg_access()
        from vco_lib.codegraph_naming import canonical_class_prefix

        self.writer = canonical_class_prefix

    def test_cli_code_reader_matches_writer_ssot(self) -> None:
        for name in self._VALID_DOMAIN:
            with self.subTest(name=name):
                self.assertEqual(
                    self.kg_access.code_sanitize_collection_prefix(name),
                    self.writer(name),
                    f"CLI code-graph reader diverged from writer for {name!r}",
                )

    def test_preserves_underscores_and_hyphens(self) -> None:
        # The distinguishing behaviour vs the KG rule.
        self.assertEqual(
            self.kg_access.code_sanitize_collection_prefix("Foo-Bar"), "Foo_Bar"
        )
        self.assertEqual(
            self.kg_access.code_sanitize_collection_prefix("Camel_Case"),
            "Camel_Case",
        )
        # Spaces still drop (PascalCase word join).
        self.assertEqual(
            self.kg_access.code_sanitize_collection_prefix("My Cool App"),
            "MyCoolApp",
        )

    def test_cli_code_matches_mcp_mirror(self) -> None:
        for mod in list(sys.modules):
            if mod.startswith("weaviate_mcp"):
                del sys.modules[mod]
        server = importlib.import_module("weaviate_mcp.server")
        for name in self._VALID_DOMAIN:
            with self.subTest(name=name):
                self.assertEqual(
                    self.kg_access.code_sanitize_collection_prefix(name),
                    server._code_sanitize_collection_prefix(name),
                    f"CLI/MCP code-graph mirror drift for {name!r}",
                )

    def test_does_not_crash_on_raising_names(self) -> None:
        # canonical_class_prefix raises for these; the reader must degrade
        # gracefully (fall back to the dropping rule → 'vct' sentinel).
        for name in ("", "   ", "123abc", "!!!"):
            with self.subTest(name=name):
                # Should not raise.
                self.kg_access.code_sanitize_collection_prefix(name)

    def test_code_graph_query_uses_preserving_rule(self) -> None:
        # ACT (code-graph): a hyphenated project resolves to the
        # underscore-PRESERVING collection the analyzer wrote.
        pairs = self.kg_access.code_graph_collections_to_query(
            "Foo-Bar", bases=("CodeFunction",)
        )
        self.assertEqual(pairs, [("Foo_Bar_CodeFunction", "Foo-Bar")])


class LeaveAloneCanonicalTests(unittest.TestCase):
    """LEAVE-ALONE: canonical prefixes keep resolving exactly as before —
    no accidental churn for the common launcher-managed case."""

    def setUp(self) -> None:
        self.kg_access = _fresh_kg_access()

    def test_canonical_prefix_passthrough(self) -> None:
        for canonical in (
            "Alpha",
            "VibeCodedOrchestrator",
            "MyCoolApp",
            "VCODev",
        ):
            with self.subTest(canonical=canonical):
                self.assertEqual(
                    self.kg_access.sanitize_collection_prefix(canonical),
                    canonical,
                )


if __name__ == "__main__":
    unittest.main()
