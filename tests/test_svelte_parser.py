# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.B (v0.2.53 Track E) — Svelte parser unit tests.

Tests the pure-function helpers ``_extract_svelte_script_blocks`` +
``_parse_svelte_functions``. P2f stage 2 (v0.2.76): the helpers moved
VERBATIM from ``templates/scripts/analyze_code_graph.py`` to
``vco_lib/codegraph_lang/svelte.py`` — this guard is retargeted to the new
home (assertions unchanged). The wired-in extractor ``analyze_svelte_file``
lives in the same module and depends on Weaviate / EmbeddingService via
``ctx`` — covered separately by the golden suite + integration tests; this
module exercises the parsing logic in isolation, which is where the regex
fragility lives.
"""

from __future__ import annotations

import unittest


def _load_module():
    """The helpers now live in an import-safe vco_lib module (no
    weaviate-client / EmbeddingService import cost) — the old
    partial-prelude ``exec`` loader is no longer needed."""
    from vco_lib.codegraph_lang import svelte
    return svelte


class SvelteScriptBlockExtractionTests(unittest.TestCase):
    """Exercises ``_extract_svelte_script_blocks``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_no_script_block_returns_empty(self) -> None:
        src = "<div>Template only</div>"
        self.assertEqual(self.mod._extract_svelte_script_blocks(src), [])

    def test_single_default_script_block(self) -> None:
        src = (
            "<script>\n"
            "  let count = 0;\n"
            "</script>\n"
            "<div>{count}</div>"
        )
        blocks = self.mod._extract_svelte_script_blocks(src)
        self.assertEqual(len(blocks), 1)
        body, is_module, start_off = blocks[0]
        self.assertIn("let count", body)
        self.assertFalse(is_module)
        self.assertGreater(start_off, 0)

    def test_module_script_block_flagged(self) -> None:
        src = (
            '<script context="module">\n'
            "  export const shared = 42;\n"
            "</script>\n"
            "<script>\n"
            "  let instance = 1;\n"
            "</script>"
        )
        blocks = self.mod._extract_svelte_script_blocks(src)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0][1], "module-context block should be flagged")
        self.assertFalse(blocks[1][1], "default block should NOT be flagged")

    def test_unclosed_script_tolerated(self) -> None:
        # Real-world tolerance: a corrupt file may have an opening
        # <script> with no closing tag (e.g. mid-edit save). Parser
        # should not crash; should treat the rest of the file as the
        # body.
        src = "<script>\nlet x = 1;\n"
        blocks = self.mod._extract_svelte_script_blocks(src)
        self.assertEqual(len(blocks), 1)
        self.assertIn("let x", blocks[0][0])


class SvelteFunctionParsingTests(unittest.TestCase):
    """Exercises ``_parse_svelte_functions``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_function_decl_extracted(self) -> None:
        src = (
            "<script>\n"
            "  function increment() {\n"
            "    count++;\n"
            "  }\n"
            "  async function loadData(id) {\n"
            "    return await fetch('/api/' + id);\n"
            "  }\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        names = [d["name"] for d in decls]
        self.assertIn("increment", names)
        self.assertIn("loadData", names)
        inc = next(d for d in decls if d["name"] == "increment")
        self.assertEqual(inc["kind"], "function")
        self.assertFalse(inc["is_async"])
        load = next(d for d in decls if d["name"] == "loadData")
        self.assertTrue(load["is_async"], "async loadData should be flagged")

    def test_export_function_kind(self) -> None:
        src = (
            "<script>\n"
            "  export function helper() {}\n"
            "  function private_helper() {}\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        kinds = {d["name"]: d["kind"] for d in decls}
        self.assertEqual(kinds["helper"], "export")
        self.assertEqual(kinds["private_helper"], "function")

    def test_arrow_function_export(self) -> None:
        src = (
            "<script>\n"
            "  export const handler = (event) => {\n"
            "    console.log(event);\n"
            "  };\n"
            "  export const asyncHandler = async () => fetch('/x');\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        names = {d["name"]: d for d in decls}
        self.assertIn("handler", names)
        self.assertEqual(names["handler"]["kind"], "arrow_export")
        self.assertFalse(names["handler"]["is_async"])
        self.assertIn("asyncHandler", names)
        self.assertTrue(names["asyncHandler"]["is_async"])

    def test_reactive_declarations(self) -> None:
        src = (
            "<script>\n"
            "  let count = 0;\n"
            "  $: doubled = count * 2;\n"
            "  $: tripled = count * 3;\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        reactive_names = [d["name"] for d in decls if d["kind"] == "reactive"]
        self.assertEqual(set(reactive_names), {"doubled", "tripled"})

    def test_module_context_reactives_skipped(self) -> None:
        # Reactive `$:` only works in the default script; module-
        # context scripts execute once and have no reactivity. We
        # should NOT pick up `$:` declarations in module-context
        # blocks.
        src = (
            '<script context="module">\n'
            "  $: shouldNotBeFlagged = 1;\n"
            "  export function realExport() {}\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        reactive_decls = [d for d in decls if d["kind"] == "reactive"]
        self.assertEqual(
            len(reactive_decls),
            0,
            "$: in module-context should not be flagged as reactive",
        )
        # But the real export should still be captured.
        self.assertEqual(
            [d["name"] for d in decls if d["kind"] == "export"],
            ["realExport"],
        )

    def test_results_sorted_by_offset(self) -> None:
        # Determinism guarantee: results sorted by start offset so
        # tests / downstream consumers see a predictable ordering.
        src = (
            "<script>\n"
            "  function third() {}\n"
            "  function first() {}\n"
            "</script>\n"
            "<script context='module'>\n"
            "  export function fourth() {}\n"
            "</script>\n"
            "<script>\n"
            "  function fifth() {}\n"
            "</script>"
        )
        decls = self.mod._parse_svelte_functions(src)
        offsets = [d["start_offset"] for d in decls]
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main()
