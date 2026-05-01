"""Tests for vco_lib.deferral_report — PR 6 of project-init/update overhaul.

Covers:
  - Round-trip: write entries -> read back -> assert equality.
  - File deleted when last entry resolved.
  - Atomic write (temp + rename).
  - YAML frontmatter parses correctly.
  - Duplicate condition_id: latest entry wins.
  - Markdown format is human-readable (snapshot test).
  - install.py integration: drift detected on --update -> deferral.md created
    with schema_drift_rebuild_required entry.
"""

from __future__ import annotations

import os
import subprocess
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
    _DEFERRED_REL,
    _parse_frontmatter,
)


def _make_entry(
    condition_id: str = "test_condition",
    title: str = "Test condition",
    detected: str = "Something was detected.",
    why_deferred: str = "Cannot auto-fix.",
    command_to_apply: str = "python install.py --update --fix",
    severity: str = "warning",
    kg_node_refs: list | None = None,
    detected_at: str = "2026-05-01T12:00:00Z",
) -> DeferralEntry:
    return DeferralEntry(
        condition_id=condition_id,
        title=title,
        detected=detected,
        why_deferred=why_deferred,
        command_to_apply=command_to_apply,
        severity=severity,
        kg_node_refs=kg_node_refs or [],
        detected_at=detected_at,
    )


class TestRoundTrip(unittest.TestCase):
    """write -> read -> assert equality."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_single_entry_round_trip(self) -> None:
        entry = _make_entry(
            condition_id="schema_drift_rebuild_required",
            title="Schema rebuild required",
            detected="KG_COLLECTION `MyProject_KnowledgeGraph` is on an older version.",
            why_deferred="Schema rebuild touches Weaviate state.",
            command_to_apply="python install.py --update --rebuild-collections",
            severity="warning",
            kg_node_refs=["knowledge/concepts/kg-nudge-token-counting.md"],
            detected_at="2026-05-01T15:42:00Z",
        )
        report = DeferralReport()
        report.add_entry(entry)
        written = report.write(self.folder)
        self.assertTrue(written)

        restored = DeferralReport.read(self.folder)
        self.assertEqual(len(restored), 1)
        r = restored.entries[0]
        self.assertEqual(r.condition_id, entry.condition_id)
        self.assertEqual(r.title, entry.title)
        self.assertIn("KnowledgeGraph", r.detected)
        self.assertEqual(r.command_to_apply, entry.command_to_apply)
        self.assertEqual(r.severity, entry.severity)
        self.assertEqual(r.kg_node_refs, entry.kg_node_refs)
        self.assertEqual(r.detected_at, entry.detected_at)

    def test_multiple_entries_round_trip(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("cond_a", severity="info", detected_at="2026-05-01T10:00:00Z"))
        report.add_entry(_make_entry("cond_b", severity="critical", detected_at="2026-05-01T11:00:00Z"))
        report.write(self.folder)

        restored = DeferralReport.read(self.folder)
        ids = {e.condition_id for e in restored.entries}
        self.assertIn("cond_a", ids)
        self.assertIn("cond_b", ids)
        self.assertEqual(len(restored), 2)

    def test_empty_report_returns_false_and_no_file(self) -> None:
        report = DeferralReport()
        result = report.write(self.folder)
        self.assertFalse(result)
        self.assertFalse((self.folder / _DEFERRED_REL).exists())


class TestFileDeletedWhenResolved(unittest.TestCase):
    """File is deleted when all entries are resolved (mark_resolved + write)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_file_deleted_when_last_entry_resolved(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("solo_condition"))
        report.write(self.folder)
        target = self.folder / _DEFERRED_REL
        self.assertTrue(target.exists())

        report.mark_resolved("solo_condition")
        report.write(self.folder)
        self.assertFalse(target.exists())

    def test_partial_resolution_keeps_file(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("keep_me"))
        report.add_entry(_make_entry("resolve_me"))
        report.write(self.folder)

        report.mark_resolved("resolve_me")
        report.write(self.folder)

        target = self.folder / _DEFERRED_REL
        self.assertTrue(target.exists())
        restored = DeferralReport.read(self.folder)
        ids = {e.condition_id for e in restored.entries}
        self.assertIn("keep_me", ids)
        self.assertNotIn("resolve_me", ids)

    def test_write_false_deletes_existing_file(self) -> None:
        """write() with zero entries should delete the file if it existed."""
        report = DeferralReport()
        report.add_entry(_make_entry("temp"))
        report.write(self.folder)
        target = self.folder / _DEFERRED_REL
        self.assertTrue(target.exists())

        empty_report = DeferralReport()
        result = empty_report.write(self.folder)
        self.assertFalse(result)
        self.assertFalse(target.exists())


class TestAtomicWrite(unittest.TestCase):
    """Atomic write: temp file in same dir + os.replace."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_no_partial_file_on_success(self) -> None:
        """After a successful write, no .tmp files should remain."""
        report = DeferralReport()
        report.add_entry(_make_entry())
        report.write(self.folder)

        parent = (self.folder / _DEFERRED_REL).parent
        tmp_files = list(parent.glob("*.tmp"))
        self.assertEqual(tmp_files, [], f"Stale .tmp files found: {tmp_files}")

    def test_final_file_exists_after_write(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry())
        report.write(self.folder)
        self.assertTrue((self.folder / _DEFERRED_REL).exists())

    def test_content_is_valid_utf8(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry())
        report.write(self.folder)
        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        self.assertIn("VCO Update Deferred", content)


class TestYamlFrontmatter(unittest.TestCase):
    """YAML frontmatter parses correctly."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_frontmatter_condition_ids(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("schema_drift_rebuild_required"))
        report.add_entry(_make_entry("weaviate_unreachable_at_update"))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        self.assertIn("schema_drift_rebuild_required", fm.get("condition_ids", []))
        self.assertIn("weaviate_unreachable_at_update", fm.get("condition_ids", []))

    def test_frontmatter_severity_max_critical_wins(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("a", severity="info"))
        report.add_entry(_make_entry("b", severity="critical"))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        self.assertEqual(fm.get("severity_max"), "critical")

    def test_frontmatter_generated_at_present(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry())
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        fm = _parse_frontmatter(content)
        self.assertIn("generated_at", fm)
        self.assertRegex(fm["generated_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class TestDuplicateConditionId(unittest.TestCase):
    """Duplicate condition_id: last add_entry() wins."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_latest_entry_wins_on_duplicate(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("dup_cond", detected="First detection."))
        report.add_entry(_make_entry("dup_cond", detected="Second detection (latest)."))

        self.assertEqual(len(report), 1)
        self.assertIn("Second", report.entries[0].detected)

    def test_round_trip_deduplicated(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("dup_cond", detected="First."))
        report.add_entry(_make_entry("dup_cond", detected="Latest."))
        report.write(self.folder)

        restored = DeferralReport.read(self.folder)
        self.assertEqual(len(restored), 1)
        self.assertIn("Latest", restored.entries[0].detected)


class TestMarkdownFormat(unittest.TestCase):
    """Snapshot test: output is human-readable structured Markdown."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.folder = Path(self._tmpdir)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_output_has_frontmatter_block(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("schema_drift_rebuild_required"))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        lines = content.splitlines()
        self.assertEqual(lines[0], "---")
        self.assertTrue(any(l.startswith("title:") for l in lines[:10]))
        self.assertTrue(any(l.startswith("condition_ids:") for l in lines[:10]))

    def test_output_has_section_per_entry(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry("schema_drift_rebuild_required", severity="warning"))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        self.assertIn("## schema_drift_rebuild_required (warning)", content)
        self.assertIn("**Detected**:", content)
        self.assertIn("**Why deferred**:", content)
        self.assertIn("**To apply**:", content)
        self.assertIn("**Detected at**:", content)

    def test_output_has_code_fence_for_command(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry(command_to_apply="python install.py --update --rebuild-collections"))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        self.assertIn("```bash", content)
        self.assertIn("python install.py --update --rebuild-collections", content)
        self.assertIn("```", content)

    def test_output_has_kg_refs_when_present(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry(
            kg_node_refs=["knowledge/concepts/kg-nudge-token-counting.md"],
        ))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        self.assertIn("Cross-references", content)
        self.assertIn("knowledge/concepts/kg-nudge-token-counting.md", content)

    def test_output_no_kg_refs_section_when_empty(self) -> None:
        report = DeferralReport()
        report.add_entry(_make_entry(kg_node_refs=[]))
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        self.assertNotIn("Cross-references", content)

    def test_output_ascii_clean(self) -> None:
        """No special unicode characters in rendered output."""
        report = DeferralReport()
        report.add_entry(_make_entry())
        report.write(self.folder)

        content = (self.folder / _DEFERRED_REL).read_text(encoding="utf-8")
        # Allow printable ASCII + newlines + common punctuation; reject
        # multi-byte sequences that would choke Windows cp1252 consoles.
        for i, ch in enumerate(content):
            self.assertLess(
                ord(ch), 128,
                f"Non-ASCII char {ch!r} (ord {ord(ch)}) at position {i}",
            )


class TestSeverityValidation(unittest.TestCase):
    """DeferralEntry rejects invalid severity values."""

    def test_invalid_severity_raises(self) -> None:
        with self.assertRaises(ValueError):
            DeferralEntry(
                condition_id="x",
                title="X",
                detected="d",
                why_deferred="w",
                command_to_apply="cmd",
                severity="catastrophic",  # invalid
            )

    def test_valid_severities_accepted(self) -> None:
        for sev in ("info", "warning", "critical"):
            entry = DeferralEntry(
                condition_id="x",
                title="X",
                detected="d",
                why_deferred="w",
                command_to_apply="cmd",
                severity=sev,
            )
            self.assertEqual(entry.severity, sev)


class TestInstallPyIntegration(unittest.TestCase):
    """install.py integration: drift on --update emits deferral entry.

    This test imports install.py helpers directly and calls
    _maybe_prompt_rebuild_collections with a mocked drift detector and
    a DeferralReport, verifying the correct entry is accumulated.
    """

    def _mock_drift_detect(self, url: str, collection: str):
        """Always returns drift with two missing features."""
        return True, ["index_null_state=True", "named_vector: openai_embed"]

    def test_noninteractive_drift_emits_deferral(self) -> None:
        import install

        original_detect = install._detect_kg_schema_drift
        install._detect_kg_schema_drift = self._mock_drift_detect

        import argparse
        args = argparse.Namespace(
            update=True,
            rebuild_collections=False,
            skip_rebuild_prompt=False,
            yes=False,
        )

        env_backup = os.environ.copy()
        os.environ["KG_COLLECTION"] = "MyProject_KnowledgeGraph"
        os.environ["WEAVIATE_URL"] = "http://localhost:8081"

        try:
            import io
            import contextlib

            report = DeferralReport()
            # Simulate non-interactive shell by patching sys.stdin.isatty
            original_isatty = sys.stdin.isatty
            sys.stdin.isatty = lambda: False  # type: ignore[method-assign]

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = install._maybe_prompt_rebuild_collections(
                    args, deferral_report=report
                )

            sys.stdin.isatty = original_isatty  # type: ignore[method-assign]

            self.assertFalse(result, "Should defer (not rebuild) in non-interactive mode")
            self.assertTrue(
                report.has_condition("schema_drift_rebuild_required"),
                "Expected schema_drift_rebuild_required entry in report",
            )
            entry = report.entries[0]
            self.assertEqual(entry.severity, "warning")
            self.assertIn("rebuild-collections", entry.command_to_apply)

        finally:
            install._detect_kg_schema_drift = original_detect
            os.environ.clear()
            os.environ.update(env_backup)

    def test_drift_resolved_interactively_no_deferral(self) -> None:
        """When user says 'y' to rebuild interactively, no deferral entry emitted."""
        import install

        original_detect = install._detect_kg_schema_drift
        install._detect_kg_schema_drift = self._mock_drift_detect

        import argparse
        args = argparse.Namespace(
            update=True,
            rebuild_collections=False,
            skip_rebuild_prompt=False,
            yes=False,
        )

        env_backup = os.environ.copy()
        os.environ["KG_COLLECTION"] = "Test_KnowledgeGraph"
        os.environ["WEAVIATE_URL"] = "http://localhost:8081"

        try:
            import io
            import contextlib
            import unittest.mock

            report = DeferralReport()
            # Simulate interactive shell + user says 'y'
            original_isatty = sys.stdin.isatty
            sys.stdin.isatty = lambda: True  # type: ignore[method-assign]

            with unittest.mock.patch("builtins.input", return_value="y"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = install._maybe_prompt_rebuild_collections(
                        args, deferral_report=report
                    )

            sys.stdin.isatty = original_isatty  # type: ignore[method-assign]

            self.assertTrue(result, "User said 'y' -- should rebuild")
            self.assertFalse(
                report.has_condition("schema_drift_rebuild_required"),
                "No deferral expected when user consented to rebuild",
            )

        finally:
            install._detect_kg_schema_drift = original_detect
            os.environ.clear()
            os.environ.update(env_backup)

    def test_deferral_file_written_on_update_with_drift(self) -> None:
        """End-to-end: DeferralReport.write creates the file at correct path."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        folder = Path(tmpdir)

        try:
            report = DeferralReport()
            report.add_entry(DeferralEntry(
                condition_id="schema_drift_rebuild_required",
                title="Schema rebuild required",
                detected="KG_COLLECTION `X_KnowledgeGraph` schema is on an older version.",
                why_deferred="Non-interactive shell without --yes.",
                command_to_apply="python install.py --update --rebuild-collections",
                severity="warning",
            ))
            written = report.write(folder)
            self.assertTrue(written)

            target = folder / _DEFERRED_REL
            self.assertTrue(target.exists())
            content = target.read_text(encoding="utf-8")
            self.assertIn("schema_drift_rebuild_required", content)

        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
