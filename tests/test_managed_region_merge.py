"""Tests for `merge_managed_region` (Phase 1.5.B, 2026-05-25).

Covers:
  - Markers present → body between them replaced; outside content preserved.
  - Markers absent → body wrapped + prepended; existing content preserved.
  - User content above opening marker preserved.
  - User content below closing marker preserved.
  - Idempotency: round-trip with same body → byte-identical output.
  - Mismatched marker pair raises TemplateError.
  - Out-of-order markers raise TemplateError.
  - CRLF input normalised.
  - Atomic write (`_write_file_atomic`) does NOT leak `.tmp` files on
    failure (via write-time injection of an OSError).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import project_init  # noqa: E402
from vco_lib.project_init import (  # noqa: E402
    MANAGED_REGION_OPEN,
    MANAGED_REGION_CLOSE,
    TemplateError,
    merge_managed_region,
    _write_file_atomic,
)


class MarkersPresentTests(unittest.TestCase):
    """Both markers present → replace body between them."""

    def test_body_replaced_in_place(self):
        existing = (
            f"{MANAGED_REGION_OPEN}\n"
            f"old body\n"
            f"{MANAGED_REGION_CLOSE}\n"
        )
        new_body = "new body line 1\nnew body line 2"
        out = merge_managed_region(existing, new_body)
        self.assertIn("new body line 1", out)
        self.assertIn("new body line 2", out)
        self.assertNotIn("old body", out)
        self.assertIn(MANAGED_REGION_OPEN, out)
        self.assertIn(MANAGED_REGION_CLOSE, out)

    def test_content_above_opening_marker_preserved(self):
        existing = (
            f"# User header\n"
            f"\n"
            f"{MANAGED_REGION_OPEN}\n"
            f"old body\n"
            f"{MANAGED_REGION_CLOSE}\n"
        )
        out = merge_managed_region(existing, "new body")
        self.assertTrue(out.startswith("# User header\n\n"))
        self.assertIn("new body", out)

    def test_content_below_closing_marker_preserved(self):
        existing = (
            f"{MANAGED_REGION_OPEN}\n"
            f"old body\n"
            f"{MANAGED_REGION_CLOSE}\n"
            f"\n"
            f"## My custom section\n"
            f"my custom content\n"
        )
        out = merge_managed_region(existing, "new body")
        self.assertIn("## My custom section", out)
        self.assertIn("my custom content", out)
        # The user-content suffix appears AFTER the closing marker.
        suffix_idx = out.find("## My custom section")
        close_idx = out.find(MANAGED_REGION_CLOSE)
        self.assertGreater(suffix_idx, close_idx)

    def test_content_both_above_and_below_preserved(self):
        existing = (
            f"# Project Title\n"
            f"some intro\n"
            f"\n"
            f"{MANAGED_REGION_OPEN}\n"
            f"old\n"
            f"{MANAGED_REGION_CLOSE}\n"
            f"\n"
            f"## Custom\n"
            f"custom note\n"
        )
        out = merge_managed_region(existing, "fresh body")
        self.assertIn("# Project Title", out)
        self.assertIn("some intro", out)
        self.assertIn("fresh body", out)
        self.assertIn("## Custom", out)
        self.assertIn("custom note", out)
        self.assertNotIn("old", out)

    def test_idempotent_round_trip(self):
        existing = (
            f"# Project Title\n"
            f"\n"
            f"{MANAGED_REGION_OPEN}\n"
            f"managed body line 1\n"
            f"managed body line 2\n"
            f"{MANAGED_REGION_CLOSE}\n"
            f"\n"
            f"## User custom\n"
            f"trailing\n"
        )
        body = "managed body line 1\nmanaged body line 2"
        out1 = merge_managed_region(existing, body)
        out2 = merge_managed_region(out1, body)
        self.assertEqual(out1, out2)


class MarkersAbsentTests(unittest.TestCase):
    """No markers present → wrap + prepend, preserving existing content."""

    def test_empty_existing_wraps_body_only(self):
        out = merge_managed_region("", "my body")
        self.assertEqual(
            out,
            f"{MANAGED_REGION_OPEN}\nmy body\n{MANAGED_REGION_CLOSE}\n",
        )

    def test_whitespace_only_existing_treated_as_empty(self):
        out = merge_managed_region("   \n\n  \n", "my body")
        # Whitespace-only existing is treated as empty → only wrapped body.
        self.assertEqual(
            out,
            f"{MANAGED_REGION_OPEN}\nmy body\n{MANAGED_REGION_CLOSE}\n",
        )

    def test_existing_user_content_prepended_with_blank_separator(self):
        existing = "# My project\nintro line\n"
        out = merge_managed_region(existing, "body")
        # Managed region first, then a blank-line separator, then the
        # existing content verbatim.
        expected = (
            f"{MANAGED_REGION_OPEN}\n"
            f"body\n"
            f"{MANAGED_REGION_CLOSE}\n"
            f"\n"
            f"# My project\nintro line\n"
        )
        self.assertEqual(out, expected)

    def test_after_first_merge_subsequent_merge_is_idempotent(self):
        # Fresh project: no markers. First merge produces wrapped output.
        # Second merge (same body) must be byte-identical to the first.
        existing = "# My project\nintro\n"
        out1 = merge_managed_region(existing, "body")
        out2 = merge_managed_region(out1, "body")
        self.assertEqual(out1, out2)


class MalformedTests(unittest.TestCase):
    """Marker-pair drift raises TemplateError with a clear message."""

    def test_opening_marker_only_raises(self):
        existing = f"{MANAGED_REGION_OPEN}\nbody\n# missing close marker\n"
        with self.assertRaises(TemplateError) as cm:
            merge_managed_region(existing, "new body")
        msg = str(cm.exception)
        self.assertIn("opening", msg)
        self.assertIn("closing", msg)

    def test_closing_marker_only_raises(self):
        existing = f"some text\n{MANAGED_REGION_CLOSE}\nmore text\n"
        with self.assertRaises(TemplateError) as cm:
            merge_managed_region(existing, "new body")
        msg = str(cm.exception)
        self.assertIn("closing", msg)
        self.assertIn("opening", msg)

    def test_markers_out_of_order_raises(self):
        # Closing appears before opening — corrupted file.
        existing = (
            f"prelude\n"
            f"{MANAGED_REGION_CLOSE}\n"
            f"middle\n"
            f"{MANAGED_REGION_OPEN}\n"
            f"tail\n"
        )
        with self.assertRaises(TemplateError) as cm:
            merge_managed_region(existing, "new body")
        self.assertIn("out of order", str(cm.exception))


class CRLFNormalisationTests(unittest.TestCase):
    """CRLF body input normalised to LF in the output."""

    def test_crlf_body_normalised(self):
        body_crlf = "line 1\r\nline 2\r\nline 3"
        out = merge_managed_region("", body_crlf)
        self.assertNotIn("\r", out)
        self.assertIn("line 1\nline 2\nline 3", out)


class AtomicWriteTests(unittest.TestCase):
    """`_write_file_atomic` must NOT leak `.tmp` files on failure."""

    def test_no_tmp_files_leaked_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "CLAUDE.md"
            _write_file_atomic(target, b"hello")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b"hello")
            # No .tmp siblings.
            siblings = [
                p for p in Path(td).iterdir()
                if p.name != "CLAUDE.md"
            ]
            self.assertEqual(siblings, [])

    def test_no_tmp_files_leaked_on_failure(self):
        # Inject an OSError into the os.replace step. The temp file must
        # be cleaned up.
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "CLAUDE.md"
            with mock.patch(
                "vco_lib.project_init.os.replace",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    _write_file_atomic(target, b"hello")
            # No file at target; no .tmp leftovers.
            self.assertFalse(target.exists())
            siblings = list(Path(td).iterdir())
            self.assertEqual(siblings, [])


class FullMergeIntegrationTests(unittest.TestCase):
    """End-to-end: merge_managed_region + atomic write round-trip."""

    def test_merge_then_atomic_write_then_re_merge(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "CLAUDE.md"

            # Round 1: fresh project, no CLAUDE.md exists.
            merged = merge_managed_region("", "body v1")
            _write_file_atomic(target, merged.encode("utf-8"))
            content_v1 = target.read_text(encoding="utf-8")

            # User appends some custom content below the closing marker.
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("\n## My custom section\nuser-added content\n")

            # Round 2: a module-toggle re-renders the managed body.
            existing = target.read_text(encoding="utf-8")
            merged2 = merge_managed_region(existing, "body v2")
            _write_file_atomic(target, merged2.encode("utf-8"))
            final = target.read_text(encoding="utf-8")

            # Managed body updated.
            self.assertIn("body v2", final)
            self.assertNotIn("body v1", final)
            # User custom content preserved.
            self.assertIn("## My custom section", final)
            self.assertIn("user-added content", final)


if __name__ == "__main__":
    unittest.main()
