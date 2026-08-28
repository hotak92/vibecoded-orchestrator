# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 decision #26 — the update commands' single-flight guard is WIRED.

The guard's *behaviour* is unit-tested in Rust
(``commands/single_flight.rs::tests``: second concurrent claim refused,
sequential re-run allowed, claim released on panic, keys isolated). What
those tests cannot reach is the CALL SITE: ``update_all_projects`` and
``update_orchestrator_at`` are ``#[command] async fn``s taking
``State<'_, Db>`` / ``Window``, so no unit test can invoke them. A guard that
is perfect and unreferenced is exactly the shape this cycle keeps finding
(a control writing a table nobody reads), so the wiring gets its own gate.

This is a SOURCE-TEXT gate, and those fail toward green
(``knowledge/concepts/source-text-gates-fail-toward-green-2026-08-27.md``).
Three of that note's rules are applied here:

1. **Match over CODE only.** The marker is searched in text whose comments
   and string literals have been blanked by the cross-line lexer from
   ``test_v0291_no_bare_prints_in_rust_crates`` — reused, not re-implemented.
   Without that, this very docstring, or the explanatory comment above each
   call site (both of which name ``begin_or_refuse``), would satisfy the
   locator while the actual call was deleted.
2. **A meta-test that proves the naive locator is fooled.** If the code-only
   filter silently stopped filtering, every assertion below would still pass.
   ``test_naive_locator_is_fooled_by_a_comment`` fails when that happens.
3. **Assert the scanner still SEES the constructs it polices.** The
   function-body extractor is checked against a known-present anchor and a
   known-absent one, so a signature rename cannot turn this file into a
   vacuous pass.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_v0291_no_bare_prints_in_rust_crates import (  # noqa: E402
    _ST_CODE,
    _strip_line,
)

PROJECTS_V2 = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "projects_v2.rs"
)
INSTALLER = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "installer.rs"
)
SINGLE_FLIGHT = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "single_flight.rs"
)

#: A column-0 Rust item start — the boundary that ends a function body for
#: the purposes of this scan. Independent of any brace counting, so a
#: mis-lexed brace cannot silently extend a body over the whole file.
_TOP_LEVEL_ITEM = re.compile(
    r"^(?:pub\b|fn\b|const\b|static\b|struct\b|enum\b|impl\b|trait\b"
    r"|type\b|mod\b|use\b|async\b|unsafe\b|extern\b|#\[)"
)


def code_only_lines(path: Path) -> list[str]:
    """The file's lines with comments and string literals blanked out."""
    state: tuple = (_ST_CODE,)
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped, state = _strip_line(line, state)
        out.append(stripped)
    return out


def function_body(lines: list[str], signature_prefix: str) -> list[str]:
    """Lines from the item declaring `signature_prefix` up to the next
    column-0 item. Returns [] when the signature is not found — callers
    assert non-emptiness so a rename fails loudly instead of vacuously.
    """
    start = None
    for i, line in enumerate(lines):
        if line.startswith(signature_prefix):
            start = i
            break
    if start is None:
        return []
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if _TOP_LEVEL_ITEM.match(line):
            break
        body.append(line)
    return body


class SingleFlightWiring(unittest.TestCase):
    """Both guarded commands claim the flight, with their own key."""

    def test_update_all_projects_claims_the_flight(self) -> None:
        body = function_body(
            code_only_lines(PROJECTS_V2), "pub async fn update_all_projects("
        )
        self.assertTrue(
            body,
            "update_all_projects signature not found — the scan would pass "
            "vacuously; fix the prefix if the signature changed.",
        )
        text = "\n".join(body)
        self.assertIn(
            "single_flight::begin_or_refuse",
            text,
            "update_all_projects performs a real bundle install per project; "
            "it must refuse a second concurrent run (plan §F #26).",
        )
        self.assertIn("OP_UPDATE_ALL_PROJECTS", text)

    def test_update_orchestrator_at_claims_the_flight(self) -> None:
        body = function_body(
            code_only_lines(INSTALLER), "pub async fn update_orchestrator_at("
        )
        self.assertTrue(
            body,
            "update_orchestrator_at signature not found — the scan would pass "
            "vacuously; fix the prefix if the signature changed.",
        )
        text = "\n".join(body)
        self.assertIn(
            "single_flight::begin_or_refuse",
            text,
            "update_orchestrator_at copies a whole orchestrator tree over the "
            "target; MenuBar's loop guard is frontend-only (plan §F #26).",
        )
        self.assertIn("OP_UPDATE_ORCHESTRATOR_AT", text)

    def test_the_two_commands_use_distinct_keys(self) -> None:
        """One shared key would let an orchestrator-clone refresh block a
        project bundle reconcile. The two are deliberately separate
        operations (the do-not-merge boundary) — the guard must not
        re-couple them.
        """
        all_projects = "\n".join(
            function_body(
                code_only_lines(PROJECTS_V2), "pub async fn update_all_projects("
            )
        )
        orchestrator = "\n".join(
            function_body(
                code_only_lines(INSTALLER), "pub async fn update_orchestrator_at("
            )
        )
        self.assertNotIn("OP_UPDATE_ORCHESTRATOR_AT", all_projects)
        self.assertNotIn("OP_UPDATE_ALL_PROJECTS", orchestrator)

    def test_guard_keys_are_defined_and_distinct(self) -> None:
        text = SINGLE_FLIGHT.read_text(encoding="utf-8")
        self.assertIn('OP_UPDATE_ALL_PROJECTS: &str = "update_all_projects"', text)
        self.assertIn(
            'OP_UPDATE_ORCHESTRATOR_AT: &str = "update_orchestrator_at"', text
        )


class ScannerSelfCheck(unittest.TestCase):
    """The scan must not be able to pass vacuously."""

    def test_naive_locator_is_fooled_by_a_comment(self) -> None:
        """Proves the code-only filter is load-bearing.

        A raw substring search finds the marker inside a comment and inside a
        string literal; the filtered search does not. If this test starts
        failing because BOTH find it, the stripper has stopped stripping and
        every wiring assertion above has quietly become a prose check.
        """
        fixture = (
            "// this comment mentions single_flight::begin_or_refuse\n"
            'let msg = "single_flight::begin_or_refuse";\n'
            "let unrelated = 1;\n"
        )
        self.assertIn("single_flight::begin_or_refuse", fixture)

        state: tuple = (_ST_CODE,)
        filtered_lines = []
        for line in fixture.splitlines():
            stripped, state = _strip_line(line, state)
            filtered_lines.append(stripped)
        filtered = "\n".join(filtered_lines)
        self.assertNotIn(
            "single_flight::begin_or_refuse",
            filtered,
            "the comment/string stripper is not stripping — the wiring "
            "assertions in this file would pass on prose alone",
        )

    def test_body_extractor_bounds_at_the_next_item(self) -> None:
        """A body must END. Without the column-0 boundary the 'body' would be
        the rest of a 12k-line file, and the marker from ANY later function
        would satisfy the wiring assertions.
        """
        lines = code_only_lines(PROJECTS_V2)
        body = function_body(lines, "pub async fn update_all_projects(")
        self.assertTrue(body)
        self.assertLess(
            len(body),
            len(lines),
            "the extractor ran to EOF — the boundary regex did not match",
        )
        self.assertLess(
            len(body), 600, f"body suspiciously long ({len(body)} lines)"
        )

    def test_missing_signature_returns_empty_not_whole_file(self) -> None:
        lines = code_only_lines(PROJECTS_V2)
        self.assertEqual(
            function_body(lines, "pub async fn this_function_does_not_exist("),
            [],
        )


if __name__ == "__main__":
    unittest.main()
