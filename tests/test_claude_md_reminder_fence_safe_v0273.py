# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-4 (v0.2.73): CLAUDE.md reminder splice/strip must be fence-aware and
line-start-anchored, and do nothing on ambiguity.

Pre-A-4, ``existing.find(_REMINDER_BEGIN)`` matched the FIRST literal
occurrence anywhere in the file — including a marker QUOTED inside a code
fence (this repo's shareable CLAUDE.md documents the markers). It then paired
that quoted begin with a later REAL end and deleted all user content between
them. These tests assert:

  * quoted markers inside a fence are ignored (user content survives);
  * a real block is still replaced in place (idempotent refresh);
  * strip removes only the real block;
  * an orphan begin (real begin, no real end) → do nothing (no deletion).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    _REMINDER_BEGIN,
    _REMINDER_END,
    _reminder_block,
    _splice_reminder_into_claude_md,
    _strip_reminder_from_claude_md,
)


class TestFenceSafeSplice(unittest.TestCase):
    def test_quoted_marker_in_fence_is_not_matched_on_splice(self):
        """A CLAUDE.md that DOCUMENTS the markers inside a code fence must
        not have the block spliced across the user's quoted example."""
        precious = "PRECIOUS USER CONTENT THAT MUST SURVIVE"
        existing = (
            "# My Project\n\n"
            "Here is how VCO's reminder works:\n\n"
            "```\n"
            f"{_REMINDER_BEGIN}\n"
            "some documented text\n"
            f"{_REMINDER_END}\n"
            "```\n\n"
            f"{precious}\n"
        )
        out = _splice_reminder_into_claude_md(existing)
        # The user's precious content must still be present.
        self.assertIn(precious, out)
        # The quoted example inside the fence must still be intact.
        self.assertIn("some documented text", out)
        # A real block should have been prepended at the top (no frontmatter).
        self.assertTrue(out.lstrip().startswith(_REMINDER_BEGIN))

    def test_quoted_marker_in_fence_is_not_matched_on_strip(self):
        """Strip must not delete content between a quoted begin (in fence)
        and a later marker."""
        precious = "PRECIOUS USER CONTENT"
        existing = (
            "# My Project\n\n"
            "```\n"
            f"{_REMINDER_BEGIN}\n"
            "documented\n"
            f"{_REMINDER_END}\n"
            "```\n\n"
            f"{precious}\n"
        )
        out = _strip_reminder_from_claude_md(existing)
        # No REAL block present → strip is a no-op; content untouched.
        self.assertEqual(out, existing)
        self.assertIn(precious, out)

    def test_real_block_after_fenced_quote_is_stripped_cleanly(self):
        """A fenced quoted marker EARLY + a real block LATER: strip removes
        only the real block, leaving the fenced quote and user content."""
        existing = (
            "# Project\n\n"
            "```\n"
            f"{_REMINDER_BEGIN}\n"
            "doc example\n"
            f"{_REMINDER_END}\n"
            "```\n\n"
            "USER SECTION ONE\n\n"
            f"{_reminder_block()}"
            "USER SECTION TWO\n"
        )
        out = _strip_reminder_from_claude_md(existing)
        self.assertIn("doc example", out)
        self.assertIn("USER SECTION ONE", out)
        self.assertIn("USER SECTION TWO", out)
        # The real block's unique prose must be gone.
        self.assertNotIn("Pending VCO action", out)

    def test_idempotent_refresh_of_real_block(self):
        """Splicing twice replaces the block in place (no duplication)."""
        existing = "# Project\n\nsome content\n"
        once = _splice_reminder_into_claude_md(existing)
        twice = _splice_reminder_into_claude_md(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(_REMINDER_BEGIN), 1)

    def test_splice_after_frontmatter(self):
        existing = "---\ntitle: X\n---\n\n# Heading\ncontent\n"
        out = _splice_reminder_into_claude_md(existing)
        # Frontmatter preserved, block after it, heading preserved.
        self.assertTrue(out.startswith("---\ntitle: X\n---\n"))
        self.assertIn(_REMINDER_BEGIN, out)
        self.assertIn("# Heading", out)

    def test_orphan_begin_marker_does_nothing_on_splice(self):
        """A real begin marker with NO matching end → ambiguous → the file
        is returned unchanged (no splice across user content)."""
        existing = (
            f"{_REMINDER_BEGIN}\n"
            "USER WROTE THIS AFTER AN ORPHAN BEGIN\n"
            "and more user content\n"
        )
        out = _splice_reminder_into_claude_md(existing)
        self.assertEqual(out, existing)

    def test_orphan_begin_marker_does_nothing_on_strip(self):
        existing = (
            f"{_REMINDER_BEGIN}\n"
            "USER WROTE THIS AFTER AN ORPHAN BEGIN\n"
        )
        out = _strip_reminder_from_claude_md(existing)
        self.assertEqual(out, existing)

    def test_full_roundtrip_no_frontmatter(self):
        existing = "# Project\n\nbody text\n"
        spliced = _splice_reminder_into_claude_md(existing)
        self.assertIn(_REMINDER_BEGIN, spliced)
        stripped = _strip_reminder_from_claude_md(spliced)
        self.assertEqual(stripped, existing)


if __name__ == "__main__":
    unittest.main()
