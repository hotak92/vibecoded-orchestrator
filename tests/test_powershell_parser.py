# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""V52-O.11.N (v0.2.53 Track E) — PowerShell parser unit tests.

Tests the pure-function helpers ``_strip_powershell_comments`` +
``_parse_powershell_functions`` — including the v0.2.75 (P1b) deep-indent
regression. P2f stage 2 (v0.2.76): the helpers moved VERBATIM from
``templates/scripts/analyze_code_graph.py`` to
``vco_lib/codegraph_lang/powershell.py`` — this guard is retargeted to the
new home (assertions unchanged). The wired-in extractor
``analyze_powershell_file`` lives in the same module and depends on
Weaviate / EmbeddingService via ``ctx``; covered separately by the golden
suite + integration tests.
"""

from __future__ import annotations

import unittest


def _load_module():
    """The helpers now live in an import-safe vco_lib module (no
    weaviate-client / EmbeddingService import cost) — the old
    partial-prelude ``exec`` loader is no longer needed."""
    from vco_lib.codegraph_lang import powershell
    return powershell


class PowerShellCommentStrippingTests(unittest.TestCase):
    """Exercises ``_strip_powershell_comments``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_single_line_comments_stripped(self) -> None:
        src = (
            "function Foo {\n"
            "  # this is a comment\n"
            "  return 1\n"
            "}\n"
        )
        out = self.mod._strip_powershell_comments(src)
        self.assertNotIn("this is a comment", out)
        # Code line should survive.
        self.assertIn("return 1", out)

    def test_block_comments_stripped(self) -> None:
        src = (
            "<#\n"
            ".SYNOPSIS\n"
            "Does the thing.\n"
            "#>\n"
            "function Foo {}\n"
        )
        out = self.mod._strip_powershell_comments(src)
        self.assertNotIn("SYNOPSIS", out)
        self.assertNotIn("Does the thing", out)
        self.assertIn("function Foo", out)

    def test_region_markers_stripped(self) -> None:
        src = (
            "#region Helpers\n"
            "function Foo {}\n"
            "#endregion\n"
        )
        out = self.mod._strip_powershell_comments(src)
        self.assertNotIn("#region", out)
        self.assertNotIn("#endregion", out)
        self.assertIn("function Foo", out)

    def test_block_comment_with_inner_hash_does_not_break(self) -> None:
        # Defense: a block comment containing `#` shouldn't bleed
        # into the single-line stripper (block-strip runs first).
        src = "<# inner # symbol #>\nfunction Foo {}\n"
        out = self.mod._strip_powershell_comments(src)
        self.assertNotIn("inner", out)
        self.assertIn("function Foo", out)


class PowerShellFunctionParsingTests(unittest.TestCase):
    """Exercises ``_parse_powershell_functions``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def test_bare_function_decl(self) -> None:
        src = (
            "function Get-Foo {\n"
            "  return 'foo'\n"
            "}\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]["name"], "Get-Foo")
        self.assertIsNone(decls[0]["scope"])
        self.assertEqual(decls[0]["kind"], "function")
        self.assertEqual(decls[0]["params"], [])

    def test_function_with_parens(self) -> None:
        # PowerShell tolerates `function Name(...)` shape too.
        src = (
            "function Get-Bar() {\n"
            "  return 'bar'\n"
            "}\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]["name"], "Get-Bar")

    def test_function_with_param_block(self) -> None:
        src = (
            "function Invoke-Thing {\n"
            "  param(\n"
            "    [Parameter()] [string]$Name,\n"
            "    [Parameter()] [int]$Count\n"
            "  )\n"
            "  Write-Output \"$Name x $Count\"\n"
            "}\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]["name"], "Invoke-Thing")
        self.assertEqual(decls[0]["params"], ["Name", "Count"])

    def test_scope_prefix_captured(self) -> None:
        src = (
            "function global:Set-Setting {\n"
            "  $Global:Foo = 1\n"
            "}\n"
            "function script:Helper { return 2 }\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        by_name = {d["name"]: d for d in decls}
        self.assertEqual(by_name["Set-Setting"]["scope"], "global")
        self.assertEqual(by_name["Helper"]["scope"], "script")

    def test_filter_keyword_treated_as_function(self) -> None:
        src = (
            "filter Get-Big {\n"
            "  if ($_.Length -gt 100) { $_ }\n"
            "}\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0]["name"], "Get-Big")
        self.assertEqual(decls[0]["kind"], "filter")

    def test_deeply_indented_nested_function_does_not_crash(self) -> None:
        """v0.2.75 (P1b) regression: a declaration indented by >= 8
        whitespace chars (a nested function inside a scriptblock — the
        idiomatic hook shape) crashed the parser with IndexError. The
        `^[ \\t]*` prefix is PART of the regex match, so the old
        kind-detection slice (`cleaned[line_start:m.start() + 8]`) saw only
        whitespace, `.strip().split()[0]` blew up, the per-file handler
        caught it, and `_invalidate_module_row` re-stamped the module row to
        embed_revision=0 on EVERY run — an immortal convergence loop (the
        row was counted owed forever while the walk could never finish the
        file). The kind now comes from the regex `kind` capture group.
        """
        src = (
            "$sb = {\n"
            "    if ($true) {\n"
            "        function Test-Truthy($v) {\n"          # 8-space indent
            "            return [bool]$v\n"
            "        }\n"
            "\t\t\tfilter Select-Big { if ($_.n -gt 3) { $_ } }\n"  # tabs
            "    }\n"
            "}\n"
            "function Top-Level { return 1 }\n"
        )
        decls = self.mod._parse_powershell_functions(src)  # must not raise
        by_name = {d["name"]: d for d in decls}
        self.assertIn("Test-Truthy", by_name)
        self.assertEqual(by_name["Test-Truthy"]["kind"], "function")
        self.assertIn("Select-Big", by_name)
        self.assertEqual(by_name["Select-Big"]["kind"], "filter")
        self.assertIn("Top-Level", by_name)

    def test_function_decl_inside_block_comment_ignored(self) -> None:
        # Defense: docstring-style example shouldn't masquerade as a
        # real declaration.
        src = (
            "<#\n"
            ".EXAMPLE\n"
            "function Bogus { return 1 }\n"
            "#>\n"
            "function Real { return 2 }\n"
        )
        decls = self.mod._parse_powershell_functions(src)
        names = [d["name"] for d in decls]
        self.assertEqual(names, ["Real"], f"Expected only 'Real', got {names}")


if __name__ == "__main__":
    unittest.main()
