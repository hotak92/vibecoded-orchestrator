# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-B item 2 — DISPOSITION on the wire.

Severity says how loud an entry is; disposition says what the reader owes it.
Conflating them is why a record of a completed repair rendered identically to a
blocking migration. These tests pin the wire contract that separates them:

* the Markdown gains a round-trip-safe `**Disposition**:` line, and the
  `## <cid> (<sev>)` HEADER SHAPE stays byte-identical (Python `_SECTION_RE`
  and Rust `restart.rs::extract_section` both parse it — WP-F's banner work
  depends on extract_section still finding new entries);
* the JSON sidecar carries it ADDITIVELY at `schema_version` 1, so an older
  VCO reading a newer sidecar degrades instead of rejecting;
* an absent disposition resolves through the registry, and an UNREGISTERED
  condition resolves to `action_required` — never quietly into a records fold.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    DeferralEntry,
    DeferralReport,
    _SECTION_RE,
)


def _entry(cid: str, **kw) -> DeferralEntry:
    base = dict(
        condition_id=cid,
        title=f"title for {cid}",
        detected="detected text",
        why_deferred="why",
        command_to_apply="do a thing",
    )
    base.update(kw)
    return DeferralEntry(**base)


class DispositionResolutionTests(unittest.TestCase):
    def test_registry_supplies_the_tier_when_none_is_set(self):
        """A Rust emitter never passes a disposition over the Python bridge.
        It must still get the right tier — that is the whole point of
        resolving through the registry instead of a wire default."""
        self.assertEqual(
            _entry("kg_access_phantom_repaired").resolved_disposition,
            "informational_record",
        )
        self.assertEqual(
            _entry("bundle_user_modified_preserved").resolved_disposition,
            "action_required",
        )
        self.assertEqual(
            _entry("dual_ollama_detected").resolved_disposition, "environmental",
        )

    def test_unregistered_condition_is_action_required(self):
        self.assertEqual(
            _entry("brand_new_unregistered_cid").resolved_disposition,
            "action_required",
        )

    def test_explicit_disposition_wins_over_the_registry(self):
        e = _entry("kg_access_phantom_repaired", disposition="action_required")
        self.assertEqual(e.resolved_disposition, "action_required")

    def test_invalid_explicit_disposition_is_rejected(self):
        with self.assertRaises(ValueError):
            _entry("x", disposition="not_a_tier")


class MarkdownWireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *entries) -> str:
        report = DeferralReport()
        for e in entries:
            report.add_entry(e)
        report.write(self.folder)
        return (
            self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        ).read_text(encoding="utf-8")

    def test_disposition_line_is_rendered(self):
        md = self._write(_entry("kg_access_phantom_repaired", severity="info"))
        self.assertIn("**Disposition**: informational_record", md)

    def test_section_header_shape_is_unchanged(self):
        """The `## <cid> (<sev>)` header is a CROSS-LANGUAGE parse target.
        Changing it would silently break the Rust restart-banner extraction."""
        md = self._write(
            _entry("launcher_restart_required", severity="warning"),
            _entry("kg_access_phantom_repaired", severity="info"),
        )
        headers = [
            (m.group("cid"), m.group("sev")) for m in _SECTION_RE.finditer(md)
        ]
        self.assertEqual(
            headers,
            [
                ("launcher_restart_required", "warning"),
                ("kg_access_phantom_repaired", "info"),
            ],
        )

    def test_markdown_round_trip_preserves_disposition(self):
        """Read-back via the MARKDOWN fallback (no JSON sidecar) must keep the
        tier — the .md is what a user edits by hand and what Rust strips."""
        self._write(_entry("kg_access_phantom_repaired", severity="info"))
        (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.json").unlink()
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back.entries), 1)
        self.assertEqual(
            back.entries[0].resolved_disposition, "informational_record"
        )

    def test_unknown_disposition_line_is_ignored_not_fatal(self):
        """A hand-edited typo must not make the whole ledger unreadable."""
        self._write(_entry("bundle_user_modified_preserved"))
        md_path = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.md"
        md_path.write_text(
            md_path.read_text(encoding="utf-8").replace(
                "**Disposition**: action_required", "**Disposition**: banana"
            ),
            encoding="utf-8",
        )
        (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.json").unlink()
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back.entries), 1)
        self.assertIsNone(back.entries[0].disposition)
        self.assertEqual(back.entries[0].resolved_disposition, "action_required")


class JsonSidecarWireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _sidecar(self) -> dict:
        return json.loads(
            (self.folder / ".claude" / "context" / "UPDATE_DEFERRED.json")
            .read_text(encoding="utf-8")
        )

    def test_schema_version_stays_1(self):
        report = DeferralReport()
        report.add_entry(_entry("dual_ollama_detected", severity="info"))
        report.write(self.folder)
        self.assertEqual(self._sidecar()["schema_version"], 1)

    def test_absent_fields_are_omitted_not_nulled(self):
        """An entry with no explicit disposition and no dismiss fields writes
        the SAME keys v0.2.90 wrote — a pure addition, not a reshape."""
        report = DeferralReport()
        report.add_entry(_entry("bundle_user_modified_preserved"))
        report.write(self.folder)
        keys = set(self._sidecar()["entries"][0])
        self.assertEqual(
            keys,
            {
                "condition_id", "title", "detected", "why_deferred",
                "command_to_apply", "severity", "kg_node_refs", "detected_at",
            },
        )

    def test_dismiss_fields_round_trip(self):
        report = DeferralReport()
        report.add_entry(
            _entry(
                "dual_ollama_detected",
                severity="info",
                dismiss_fields={"alt_port": "11434", "canon_port": "11435"},
            )
        )
        report.write(self.folder)
        self.assertEqual(
            self._sidecar()["entries"][0]["dismiss_fields"],
            {"alt_port": "11434", "canon_port": "11435"},
        )
        back = DeferralReport.read(self.folder)
        self.assertEqual(
            back.entries[0].dismiss_fields,
            {"alt_port": "11434", "canon_port": "11435"},
        )

    def test_unknown_sidecar_keys_are_ignored(self):
        """Forward-compat: a sidecar from a NEWER VCO must still read."""
        report = DeferralReport()
        report.add_entry(_entry("bundle_user_modified_preserved"))
        report.write(self.folder)
        path = self.folder / ".claude" / "context" / "UPDATE_DEFERRED.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["entries"][0]["some_future_key"] = {"nested": True}
        path.write_text(json.dumps(payload), encoding="utf-8")
        back = DeferralReport.read(self.folder)
        self.assertEqual(len(back.entries), 1)


class SplitAndReminderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_split_groups_auto_retryable_with_actionable(self):
        """`auto_retryable` is work still OWED — VCO can just do it itself. It
        must not hide in the records fold, or a KG that never got seeded would
        look like a completed action."""
        report = DeferralReport()
        report.add_entry(_entry("bundle_user_modified_preserved"))
        report.add_entry(_entry("kg_sync_no_embedding_backend"))
        report.add_entry(_entry("kg_access_phantom_repaired", severity="info"))
        report.add_entry(_entry("dual_ollama_detected", severity="info"))
        actionable, informational = report.split_by_disposition()
        self.assertEqual(
            [e.condition_id for e in actionable],
            ["bundle_user_modified_preserved", "kg_sync_no_embedding_backend"],
        )
        self.assertEqual(
            [e.condition_id for e in informational],
            ["kg_access_phantom_repaired", "dual_ollama_detected"],
        )

    def test_claude_md_reminder_carries_the_split(self):
        """The reminder block used to be binary — present or absent. A session
        opening a project with three stale records read exactly like one with a
        blocking migration."""
        (self.folder / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
        report = DeferralReport()
        report.add_entry(_entry("bundle_user_modified_preserved"))
        report.add_entry(_entry("kg_access_phantom_repaired", severity="info"))
        report.add_entry(_entry("dual_ollama_detected", severity="info"))
        report.write(self.folder)
        claude_md = (self.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("**1 actionable**", claude_md)
        self.assertIn("2 informational/records", claude_md)

    def test_reminder_split_is_singular_for_one_record(self):
        (self.folder / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
        report = DeferralReport()
        report.add_entry(_entry("kg_access_phantom_repaired", severity="info"))
        report.write(self.folder)
        claude_md = (self.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("1 informational/record.", claude_md)

    def test_reminder_split_honours_an_explicit_disposition(self):
        """wave-2 MINOR-3 (RED-PROOF): the reminder line and the report
        partition must be ONE partition.

        `_disposition_split_line` counted through the REGISTRY-ONLY
        `deferral_registry.split_by_disposition(cids)`, which sees only the
        condition id — so an entry carrying an EXPLICIT disposition (the field
        `resolved_disposition` prefers, and the tier the ledger renders and the
        GUI reads) was counted at its registry tier instead. A record whose
        emitter deliberately escalated it to `action_required` therefore
        rendered in the ledger as actionable while the CLAUDE.md reminder told
        the session "0 actionable" — the exact disagreement the
        `split_by_disposition` docstring promises cannot happen."""
        (self.folder / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
        report = DeferralReport()
        # Registry tier for this cid is `informational_record`; the entry
        # overrides it.
        escalated = _entry(
            "kg_access_phantom_repaired",
            severity="info",
            disposition="action_required",
        )
        self.assertEqual(escalated.resolved_disposition, "action_required")
        report.add_entry(escalated)
        report.write(self.folder)

        actionable, informational = report.split_by_disposition()
        self.assertEqual([e.condition_id for e in actionable],
                         ["kg_access_phantom_repaired"])
        self.assertEqual(informational, [])

        claude_md = (self.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("**1 actionable**", claude_md)
        self.assertIn("0 informational/records", claude_md)

    def test_reminder_split_honours_an_explicit_demotion(self):
        """The mirror leg: an entry demoted below its registry tier must not be
        counted as actionable by the reminder either."""
        (self.folder / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
        report = DeferralReport()
        report.add_entry(_entry(
            "bundle_user_modified_preserved",
            disposition="informational_record",
        ))
        report.write(self.folder)
        claude_md = (self.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("**0 actionable**", claude_md)
        self.assertIn("1 informational/record.", claude_md)

    def test_reminder_block_is_stripped_when_the_ledger_empties(self):
        (self.folder / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
        report = DeferralReport()
        report.add_entry(_entry("bundle_user_modified_preserved"))
        report.write(self.folder)
        DeferralReport().write(self.folder)
        claude_md = (self.folder / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertNotIn("Pending VCO action", claude_md)


if __name__ == "__main__":
    unittest.main()
