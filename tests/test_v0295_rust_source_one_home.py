# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 ship-gate MINOR-7 — one home for "read this ``.rs`` file as CODE".

The finding: ``tests/test_v0291_binary_delivery_chain.py`` grew a PRIVATE
``read_code`` (a per-line ``//`` cutter with its own quote tracker) while
``tests/common/rust_source.py`` already answered the same question for six
other lints, and the v0.2.95 retarget of
``tests/test_v0295_rendered_file_conflicts.py`` scanned Rust with raw
``read_text`` plus a THIRD stripper inlined at one call site
(``line.split("//", 1)[0]``). Three readers, one concern.

What this module pins is the BEHAVIOUR the one home provides, on synthetic
sources rather than on the live Rust tree — deliberately, because the two
shapes below are precisely the ones the live tree does not happen to contain
today, and a pin that depends on that stays green for the wrong reason:

* a token that appears ONLY in a comment must not satisfy an ``assertIn``
  (this project's "never guard wiring with a source scan" rule — a name in a
  comment satisfies it);
* a token inside a STRING LITERAL that also contains ``//`` must survive. This
  is the specific lie the private cutter told: it cut at the first unquoted
  ``//`` it believed it had found, and the inlined ``split("//", 1)[0]`` cut
  at the first ``//`` with no quote state at all — so
  ``"https://host/CLAUDE.md"`` read as ``"https:`` and a hard-coded path hid
  behind a URL-shaped literal, turning a guard into a false negative.

The identity assertions at the end are not source scans: they compare the
FUNCTION OBJECTS the two modules actually call.
"""
from __future__ import annotations

import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.common.rust_source import read_rust_code


#: Temp dirs held for the life of the process (the test run), so a written
#: sample is not collected out from under the assertion that reads it.
_TMPDIRS: "list[TemporaryDirectory[str]]" = []


def _rs(body: str) -> Path:
    """Write ``body`` to a throwaway ``.rs`` file and return its path."""
    tmp = TemporaryDirectory()
    _TMPDIRS.append(tmp)
    path = Path(tmp.name) / "sample.rs"
    path.write_text(body, encoding="utf-8")
    return path


class TheOneHomeReadsRustAsCode(unittest.TestCase):
    def test_a_line_comment_cannot_satisfy_a_scan(self) -> None:
        path = _rs(
            "pub fn a() {\n"
            "    // resolve_rendered_files_keep_local_at was REMOVED here\n"
            "    other();\n"
            "}\n"
        )
        code = read_rust_code(path)
        self.assertNotIn("resolve_rendered_files_keep_local_at", code)
        self.assertIn("other();", code)

    def test_a_block_comment_cannot_satisfy_a_scan_either(self) -> None:
        """The private cutter handled line comments only. A block comment —
        including a NESTED one, which Rust allows — kept every name inside it
        visible to an ``assertIn``."""
        path = _rs(
            "pub fn a() {\n"
            "    /* begin_orchestrator_update_or_refuse /* still */ gone */\n"
            "    other();\n"
            "}\n"
        )
        code = read_rust_code(path)
        self.assertNotIn("begin_orchestrator_update_or_refuse", code)
        self.assertIn("other();", code)

    def test_a_string_literal_containing_a_double_slash_survives(self) -> None:
        """THE regression the private strippers caused, in one line.

        A guard asking "is this path hard-coded in Rust?" must still see the
        path when it sits inside a URL-shaped literal. Cutting at the first
        ``//`` (with no quote state, as the inlined ``split`` did) deleted the
        rest of the line and answered NO — a silent false negative in a gate
        whose entire job is catching hard-coded paths."""
        path = _rs('let u = "https://example.test/CLAUDE.md";\n')
        code = read_rust_code(path)
        self.assertIn("CLAUDE.md", code)
        self.assertIn("https://example.test/CLAUDE.md", code)

    def test_a_raw_string_with_slashes_survives(self) -> None:
        path = _rs('let u = r#"weight // 2 = CLAUDE.md"#;\n')
        code = read_rust_code(path)
        self.assertIn("CLAUDE.md", code)

    def test_a_trailing_comment_leaves_the_code_before_it(self) -> None:
        path = _rs("    stop_hub_and_rename_binaries_aside(); // why\n")
        code = read_rust_code(path)
        self.assertIn("stop_hub_and_rename_binaries_aside();", code)
        self.assertNotIn("why", code)


class BothRetargetedModulesUseThatHome(unittest.TestCase):
    """Function-object identity, not a source scan: whatever these modules
    call when they read Rust must BE the shared home's callable."""

    def test_the_binary_delivery_chain_lint_calls_it(self) -> None:
        module = importlib.import_module("tests.test_v0291_binary_delivery_chain")
        self.assertIs(module.read_code, read_rust_code)

    def test_the_rendered_file_lint_calls_it(self) -> None:
        module = importlib.import_module("tests.test_v0295_rendered_file_conflicts")
        self.assertIs(module.read_rust_code, read_rust_code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
