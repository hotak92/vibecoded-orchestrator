# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A-3 (v0.2.73): JSON sidecar is the deferral source of truth.

The pre-A-3 Markdown round-trip corrupted entries:
  * any multi-line field truncated to its first line on read (destroyed the
    ``bundle_user_modified_preserved`` preserved-files list — the entry's
    actionable payload);
  * a ``## fake_section (critical)`` line inside a field split one entry into
    three phantom entries;
  * a ``` line inside ``command_to_apply`` inverted the fence toggle.

With the JSON sidecar authoritative, every field survives verbatim. These
tests assert the round-trip is now lossless AND that:
  * the Markdown render still exists (human view + Rust restart.rs surface);
  * a Rust-style strip of a section from the Markdown is honoured on read
    (JSON reconciled against Markdown section presence);
  * P2a (v0.2.75): the reconcile-drop applies ONLY to the condition IDs
    restart.rs actually strips (``_RUST_STRIPPABLE_CONDITION_IDS``) — any
    other cid missing from a co-present Markdown means the .md is STALE
    (crashed/partial Markdown write) and the entry is KEPT;
  * an absent/corrupt/incompatible JSON falls back to the Markdown parser.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
    _DEFERRED_JSON_REL,
    _DEFERRED_REL,
)


class TestJsonSidecarRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _roundtrip(self, entry: DeferralEntry) -> DeferralEntry:
        rep = DeferralReport()
        rep.add_entry(entry)
        self.assertTrue(rep.write(self.folder))
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back), 1)
        return back.entries[0]

    def test_multiline_detected_survives(self):
        """The bundle_user_modified_preserved shape: a multi-line file list
        in ``detected`` must survive the round-trip verbatim."""
        detected = (
            "3 orchestrator-shipped files were locally modified and preserved:\n"
            "  - .claude/hooks/post-file-edit.sh\n"
            "  - .claude/hooks/kg-update-nudge.sh\n"
            "  - .claude/settings.json"
        )
        entry = DeferralEntry(
            condition_id="bundle_user_modified_preserved",
            title="Preserved user-modified files",
            detected=detected,
            why_deferred="user edits preserved; --force to accept upstream",
            command_to_apply="python install.py --update --force",
            severity="warning",
        )
        out = self._roundtrip(entry)
        self.assertEqual(out.detected, detected)
        # Every filename must be present (pre-A-3 dropped all but line 1).
        self.assertIn("post-file-edit.sh", out.detected)
        self.assertIn("kg-update-nudge.sh", out.detected)
        self.assertIn(".claude/settings.json", out.detected)

    def test_field_containing_section_header_does_not_split(self):
        """A ``## fake_section (critical)`` line inside a field must NOT
        materialise a phantom entry with an invented severity."""
        entry = DeferralEntry(
            condition_id="real_entry",
            title="Real",
            detected="The log said:\n## fake_section (critical)\nand continued.",
            why_deferred="needs consent",
            command_to_apply="do-the-thing",
            severity="info",
        )
        rep = DeferralReport()
        rep.add_entry(entry)
        rep.write(self.folder)
        back = DeferralReport.read(self.folder)
        # Exactly ONE entry, no phantom `fake_section`.
        self.assertEqual(len(back), 1)
        self.assertEqual(back.entries[0].condition_id, "real_entry")
        self.assertEqual(back.entries[0].severity, "info")
        self.assertIn("fake_section", back.entries[0].detected)

    def test_command_with_fence_survives(self):
        """A ``` line inside ``command_to_apply`` must not corrupt the
        parsed command."""
        cmd = "cat <<'EOF'\n```not-a-real-fence```\nEOF"
        entry = DeferralEntry(
            condition_id="fence_cmd",
            title="Fence in command",
            detected="short",
            why_deferred="short",
            command_to_apply=cmd,
            severity="warning",
        )
        out = self._roundtrip(entry)
        self.assertEqual(out.command_to_apply, cmd)

    def test_both_files_written_and_json_authoritative(self):
        entry = DeferralEntry(
            condition_id="c1", title="t", detected="d",
            why_deferred="w", command_to_apply="cmd", severity="warning",
        )
        rep = DeferralReport()
        rep.add_entry(entry)
        rep.write(self.folder)
        self.assertTrue((self.folder / _DEFERRED_JSON_REL).exists())
        self.assertTrue((self.folder / _DEFERRED_REL).exists())
        payload = json.loads((self.folder / _DEFERRED_JSON_REL).read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["entries"][0]["condition_id"], "c1")

    def test_empty_write_removes_both_files(self):
        entry = DeferralEntry(
            condition_id="c1", title="t", detected="d",
            why_deferred="w", command_to_apply="cmd", severity="warning",
        )
        rep = DeferralReport()
        rep.add_entry(entry)
        rep.write(self.folder)
        # Now write empty → both files gone.
        empty = DeferralReport()
        self.assertFalse(empty.write(self.folder))
        self.assertFalse((self.folder / _DEFERRED_JSON_REL).exists())
        self.assertFalse((self.folder / _DEFERRED_REL).exists())


class TestReconciliationWithMarkdown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rust_strip_of_markdown_section_honoured(self):
        """When the Markdown lacks a section the JSON carries (Rust
        restart.rs stripped it), read() drops that entry."""
        rep = DeferralReport()
        rep.add_entry(DeferralEntry(
            condition_id="launcher_restart_required", title="restart",
            detected="d", why_deferred="w", command_to_apply="c",
            severity="warning",
        ))
        rep.add_entry(DeferralEntry(
            condition_id="other_entry", title="keep", detected="d",
            why_deferred="w", command_to_apply="c", severity="info",
        ))
        rep.write(self.folder)

        # Simulate the Rust restart flow: strip the launcher_restart_required
        # section from the Markdown only (leaving the JSON untouched).
        md_path = self.folder / _DEFERRED_REL
        md = md_path.read_text()
        # Remove the launcher_restart_required section crudely.
        start = md.index("## launcher_restart_required")
        end = md.index("## other_entry")
        md = md[:start] + md[end:]
        md_path.write_text(md)

        back = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in back.entries}
        self.assertNotIn("launcher_restart_required", cids)
        self.assertIn("other_entry", cids)

    def test_non_strippable_cid_survives_stale_markdown(self):
        """P2a (v0.2.75): a fresh NON-restart cid in the JSON survives a
        co-present STALE Markdown that lacks its section header.

        Crash window this closes: write() lands the JSON, the Markdown
        write dies (crash/disk-full), an older pre-existing .md stays on
        disk → pre-P2a, every cid newly added that run was silently
        dropped on the next read (misread as a restart.rs strip)."""
        # Older on-disk state: only `old_entry`.
        old = DeferralReport()
        old.add_entry(DeferralEntry(
            condition_id="old_entry", title="old", detected="d",
            why_deferred="w", command_to_apply="c", severity="info",
        ))
        old.write(self.folder)
        stale_md = (self.folder / _DEFERRED_REL).read_text()

        # New run adds a fresh entry; JSON write lands, Markdown write
        # "dies" — simulate by restoring the stale Markdown afterwards.
        new = DeferralReport()
        new.add_entry(DeferralEntry(
            condition_id="old_entry", title="old", detected="d",
            why_deferred="w", command_to_apply="c", severity="info",
        ))
        new.add_entry(DeferralEntry(
            condition_id="bundle_user_modified_preserved", title="fresh",
            detected="d", why_deferred="w", command_to_apply="c",
            severity="warning",
        ))
        new.write(self.folder)
        (self.folder / _DEFERRED_REL).write_text(stale_md)

        back = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in back.entries}
        self.assertIn(
            "bundle_user_modified_preserved", cids,
            "non-Rust-strippable cid missing from a stale .md must be KEPT",
        )
        self.assertIn("old_entry", cids)

    def test_strippable_and_stale_mixed_markdown(self):
        """P2a: in ONE stale Markdown, `launcher_restart_required` missing
        → dropped (Rust strip honoured); any other missing cid → kept."""
        rep = DeferralReport()
        rep.add_entry(DeferralEntry(
            condition_id="launcher_restart_required", title="restart",
            detected="d", why_deferred="w", command_to_apply="c",
            severity="warning",
        ))
        rep.add_entry(DeferralEntry(
            condition_id="fresh_non_restart_cid", title="fresh",
            detected="d", why_deferred="w", command_to_apply="c",
            severity="warning",
        ))
        rep.write(self.folder)

        # A Markdown that carries NEITHER section (e.g. restart.rs stripped
        # its entry from an .md that was already stale for the other cid).
        md_path = self.folder / _DEFERRED_REL
        md_path.write_text(
            "---\ntitle: VCO Update Deferred\ncondition_ids: []\n---\n"
            "\n# VCO Update Deferred\n"
        )

        back = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in back.entries}
        self.assertNotIn("launcher_restart_required", cids,
                         "Rust-strippable cid must still honour the strip")
        self.assertIn("fresh_non_restart_cid", cids,
                      "non-strippable cid must survive the stale .md")

    def test_strippable_set_matches_restart_rs(self):
        """Cross-language guard: the frozenset names exactly the cid(s)
        restart.rs production code passes to strip_section()."""
        from vco_lib.deferral_report import _RUST_STRIPPABLE_CONDITION_IDS
        self.assertEqual(
            _RUST_STRIPPABLE_CONDITION_IDS, {"launcher_restart_required"}
        )
        rs = (
            REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
            / "restart.rs"
        )
        if not rs.is_file():
            self.skipTest("launcher sources not present in this checkout")
        src = rs.read_text(encoding="utf-8")
        # Every production strip_section call (skip `fn strip_section` and
        # test-module usages, which exercise the helper directly).
        in_tests = src.find("mod tests")
        production = src[: in_tests if in_tests != -1 else len(src)]
        import re
        stripped_cids = set(re.findall(
            r'strip_section\(&?\w+,\s*"([^"]+)"\)', production
        ))
        self.assertEqual(
            stripped_cids, set(_RUST_STRIPPABLE_CONDITION_IDS),
            "restart.rs strip_section cids drifted from "
            "_RUST_STRIPPABLE_CONDITION_IDS — update both sides together",
        )

    def test_absent_json_falls_back_to_markdown(self):
        """A report with only a Markdown file (pre-A-3) still reads."""
        rep = DeferralReport()
        rep.add_entry(DeferralEntry(
            condition_id="legacy", title="t", detected="single line",
            why_deferred="w", command_to_apply="c", severity="warning",
        ))
        rep.write(self.folder)
        # Delete the JSON sidecar → forces Markdown fallback.
        (self.folder / _DEFERRED_JSON_REL).unlink()
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back), 1)
        self.assertEqual(back.entries[0].condition_id, "legacy")

    def test_corrupt_json_falls_back_to_markdown(self):
        rep = DeferralReport()
        rep.add_entry(DeferralEntry(
            condition_id="legacy", title="t", detected="single line",
            why_deferred="w", command_to_apply="c", severity="warning",
        ))
        rep.write(self.folder)
        (self.folder / _DEFERRED_JSON_REL).write_text("{not valid json")
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back), 1)
        self.assertEqual(back.entries[0].condition_id, "legacy")

    def test_incompatible_schema_version_falls_back(self):
        rep = DeferralReport()
        rep.add_entry(DeferralEntry(
            condition_id="legacy", title="t", detected="single line",
            why_deferred="w", command_to_apply="c", severity="warning",
        ))
        rep.write(self.folder)
        (self.folder / _DEFERRED_JSON_REL).write_text(
            json.dumps({"schema_version": 999, "entries": []})
        )
        back = DeferralReport.read(self.folder)
        # Falls back to Markdown → legacy entry recovered.
        self.assertEqual(len(back), 1)


if __name__ == "__main__":
    unittest.main()
