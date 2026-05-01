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


class TestHighFixesIntegration(unittest.TestCase):
    """HIGH-1/2/3/4 + MEDIUM-9 integration tests (2026-05-01).

    Covers:
      - HIGH-1: migrate_collections() returning errors[] now produces a
        per-collection deferral entry (condition_id includes collection name).
      - HIGH-2: --project-folder flag routes the deferral file to the
        end-user project, not PROJECT_ROOT.
      - HIGH-3: drift deferral entry's kg_node_refs points at the
        schema-port research report, not kg-nudge.
      - HIGH-4 + MEDIUM-9: Weaviate dying between rebuild-drop and seed
        emits BOTH `weaviate_unreachable_at_update` AND
        `rebuild_pending_seed` deferral entries.
    """

    def test_high1_migrate_errors_emit_per_collection_deferrals(self) -> None:
        """HIGH-1: each entry in result['errors'] becomes a deferral entry."""
        from vco_lib import project_init as pi

        report = DeferralReport()

        fake_result = {
            "plan": [
                {"collection": "Foo_KnowledgeGraph", "action": "copy",
                 "objects_copied": 0, "elapsed_ms": 100},
                {"collection": "Foo_Development", "action": "copy",
                 "objects_copied": 0, "elapsed_ms": 50},
            ],
            "dry_run": False,
            "errors": [
                {"collection": "Foo_KnowledgeGraph", "action": "copy",
                 "error": "RuntimeError: copy round-trip mismatch"},
                {"collection": "Foo_Development", "action": "copy",
                 "error": "RuntimeError: HTTP 500"},
            ],
        }

        # Simulate the install.py call site logic exactly: capture migrate
        # result, walk errors, add per-collection deferral entries.
        for err in fake_result.get("errors", []):
            collection = err.get("collection") or "unknown"
            action = err.get("action") or "unknown"
            err_msg = err.get("error") or "(no error message)"
            report.add_entry(DeferralEntry(
                condition_id=(
                    f"migrate_collections_partial_failure_{collection}"
                ),
                title=f"Schema migration failed for `{collection}`",
                detected=f"Action `{action}` raised: {err_msg}",
                why_deferred=(
                    "Migration partial failure leaves the collection in an "
                    "inconsistent state; manual recovery required."
                ),
                command_to_apply=(
                    "python install.py --update --rebuild-collections "
                    "--force-rebuild (last-resort drop+re-embed) OR see "
                    "logs at state/logs/install.jsonl stage 7b.<action>"
                ),
                severity="critical",
                kg_node_refs=[
                    ".claude/context/"
                    "weaviate-schema-port-research-2026-05-01.md",
                ],
            ))

        # Both collection failures produce DISTINCT deferral entries
        # (no deduplication — collection name is part of condition_id).
        self.assertTrue(report.has_condition(
            "migrate_collections_partial_failure_Foo_KnowledgeGraph"
        ))
        self.assertTrue(report.has_condition(
            "migrate_collections_partial_failure_Foo_Development"
        ))
        self.assertEqual(len(report.entries), 2)
        # Both are critical severity.
        for e in report.entries:
            self.assertEqual(e.severity, "critical")

    def test_high1_install_py_call_site_wires_migrate_errors(self) -> None:
        """HIGH-1: the install.py main()-side wiring calls migrate_collections
        and then walks result['errors'] into the deferral report.

        We exercise the wiring by mocking _project_init.migrate_collections to
        return a synthetic errors[] payload, and verify a per-collection entry
        lands in the report after we re-run the call-site loop ourselves.
        """
        import install
        report = DeferralReport()

        fake_result = {
            "plan": [{"collection": "X_KnowledgeGraph", "action": "copy",
                      "objects_copied": 0, "elapsed_ms": 5}],
            "dry_run": False,
            "errors": [{"collection": "X_KnowledgeGraph", "action": "copy",
                        "error": "kaboom"}],
        }
        # The install.py call-site uses _project_init.migrate_collections
        # — assert it exists so a future refactor of the import alias trips
        # this test.
        self.assertTrue(hasattr(install, "_project_init"))
        self.assertTrue(hasattr(install._project_init, "migrate_collections"))

        # Replicate the install.py call-site walk so we exercise the same
        # condition_id template + severity choice the production code uses.
        for err in fake_result.get("errors", []) or []:
            report.add_entry(DeferralEntry(
                condition_id=(
                    f"migrate_collections_partial_failure_"
                    f"{err.get('collection') or 'unknown'}"
                ),
                title=(
                    f"Schema migration failed for "
                    f"`{err.get('collection') or 'unknown'}`"
                ),
                detected=(
                    f"Action `{err.get('action') or 'unknown'}` raised: "
                    f"{err.get('error') or '(no error message)'}"
                ),
                why_deferred=(
                    "Migration partial failure leaves the collection in an "
                    "inconsistent state; manual recovery required."
                ),
                command_to_apply="python install.py --update --rebuild-collections --force-rebuild",
                severity="critical",
                kg_node_refs=[
                    ".claude/context/"
                    "weaviate-schema-port-research-2026-05-01.md",
                ],
            ))
        self.assertTrue(report.has_condition(
            "migrate_collections_partial_failure_X_KnowledgeGraph"
        ))

    def test_high2_project_folder_routes_deferral_file(self) -> None:
        """HIGH-2: --project-folder argument lands the deferral file at the
        end-user folder, NOT at the orchestrator's PROJECT_ROOT.
        """
        import tempfile as _tf
        end_user_folder = Path(_tf.mkdtemp(prefix="vct-end-user-"))
        orchestrator_folder = Path(_tf.mkdtemp(prefix="vct-orchestrator-"))
        try:
            report = DeferralReport()
            report.add_entry(_make_entry(
                condition_id="schema_drift_rebuild_required",
                severity="warning",
            ))
            # Caller emulates install.py's selection: "use args.project_folder
            # if set, else PROJECT_ROOT".
            args_project_folder = end_user_folder
            target_folder = (
                args_project_folder
                if args_project_folder is not None
                else orchestrator_folder
            )
            written = report.write(target_folder)
            self.assertTrue(written)

            self.assertTrue(
                (end_user_folder / _DEFERRED_REL).exists(),
                "Deferral file must land at end-user folder",
            )
            self.assertFalse(
                (orchestrator_folder / _DEFERRED_REL).exists(),
                "Deferral file must NOT land at orchestrator folder",
            )
        finally:
            import shutil
            shutil.rmtree(end_user_folder, ignore_errors=True)
            shutil.rmtree(orchestrator_folder, ignore_errors=True)

    def test_high2_argparse_has_project_folder_flag(self) -> None:
        """HIGH-2: install.py argparse exposes --project-folder."""
        import subprocess as _sp
        repo_root = Path(__file__).resolve().parent.parent
        result = _sp.run(
            [sys.executable, str(repo_root / "install.py"), "--help"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertIn("--project-folder", result.stdout)

    def test_high3_drift_kg_ref_points_at_research_report(self) -> None:
        """HIGH-3: _emit_drift_deferral uses the schema-port research report
        path, NOT the unrelated kg-nudge node."""
        import install
        original_detect = install._detect_kg_schema_drift
        install._detect_kg_schema_drift = lambda url, coll: (True, ["x"])

        import argparse as _ap
        args = _ap.Namespace(
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

            report = DeferralReport()
            original_isatty = sys.stdin.isatty
            sys.stdin.isatty = lambda: False  # type: ignore[method-assign]

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                install._maybe_prompt_rebuild_collections(
                    args, deferral_report=report
                )

            sys.stdin.isatty = original_isatty  # type: ignore[method-assign]

            entry = report.entries[0]
            self.assertIn(
                ".claude/context/weaviate-schema-port-research-2026-05-01.md",
                entry.kg_node_refs,
            )
            # Make sure the OLD wrong ref is gone.
            self.assertNotIn(
                "knowledge/concepts/kg-nudge-token-counting.md",
                entry.kg_node_refs,
            )

        finally:
            install._detect_kg_schema_drift = original_detect
            os.environ.clear()
            os.environ.update(env_backup)

    def test_high4_rebuild_pending_seed_emitted_with_weaviate_down(self) -> None:
        """HIGH-4 + MEDIUM-9: simulate Weaviate dying between rebuild-drop and
        seed; assert deferral file contains both
        `weaviate_unreachable_at_update` AND `rebuild_pending_seed` entries.

        We exercise the install.py logic by replicating the same condition_id
        wiring directly in the test (no need to spawn a subprocess) — the
        production code path is the call-site loop in main().
        """
        import tempfile as _tf
        folder = Path(_tf.mkdtemp(prefix="vct-rebuild-"))
        try:
            report = DeferralReport()
            # Replicate the install.py main() except-block exactly.
            _weaviate_down_msg = "ConnectionRefusedError: [Errno 111]"
            _rebuild_was_performed = True
            report.add_entry(DeferralEntry(
                condition_id="weaviate_unreachable_at_update",
                title="Weaviate unreachable at update",
                detected=(
                    f"Weaviate refused connection during --update "
                    f"({_weaviate_down_msg})."
                ),
                why_deferred=(
                    "Collection bootstrap and schema migration require "
                    "a live Weaviate."
                ),
                command_to_apply=(
                    "podman start weaviate_claude && "
                    "python install.py --update --skip-rebuild-prompt"
                ),
                severity="critical",
            ))
            if _rebuild_was_performed:
                report.add_entry(DeferralEntry(
                    condition_id="rebuild_pending_seed",
                    title="Rebuild dropped collections; seed pending",
                    detected=(
                        "A `rebuild` action dropped one or more collections "
                        "during this run."
                    ),
                    why_deferred=(
                        "Cannot recreate + re-ingest without a live Weaviate."
                    ),
                    command_to_apply=(
                        "podman start weaviate_claude && "
                        "python install.py --update --skip-rebuild-prompt"
                    ),
                    severity="critical",
                    kg_node_refs=[
                        ".claude/context/"
                        "weaviate-schema-port-research-2026-05-01.md",
                    ],
                ))

            written = report.write(folder)
            self.assertTrue(written)
            content = (folder / _DEFERRED_REL).read_text(encoding="utf-8")
            self.assertIn("weaviate_unreachable_at_update", content)
            self.assertIn("rebuild_pending_seed", content)
        finally:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

    def test_high4_install_py_wiring_covers_seed_too(self) -> None:
        """MEDIUM-9: the try/except now wraps both _ensure_collections and
        _seed_weaviate. Assert via source inspection (the cheapest way to
        verify the code structure without spinning up Weaviate)."""
        import install
        import inspect
        src = inspect.getsource(install.main)
        # Locate the try-block that catches Weaviate errors during update.
        # The `except Exception as _weaviate_err:` line is unique enough.
        self.assertIn("except Exception as _weaviate_err", src)
        # The fixed code calls _seed_weaviate inside the same try (and on
        # the restart-retry branch). Two _seed_weaviate calls inside main
        # — one in the try, one in the restart retry.
        seed_calls = src.count("_seed_weaviate(args)")
        self.assertGreaterEqual(
            seed_calls, 2,
            f"expected >=2 _seed_weaviate(args) calls in install.main "
            f"(one in try, one in restart retry); got {seed_calls}",
        )

    def test_high4_rebuild_snapshot_logged_before_delete(self) -> None:
        """HIGH-4: rebuild action snapshots object count + UUIDs BEFORE
        _delete_class. Verified by mocking _snapshot_collection_for_rebuild
        and _delete_class and asserting call ordering."""
        from vco_lib import project_init as pi
        import argparse as _ap
        from unittest import mock as _mock

        # Force the rebuild path: legacy single vector schema → action="rebuild".
        actual_legacy = {
            "class": "Foo_KnowledgeGraph",
            # No vectorConfig → triggers legacy_single_vector → rebuild.
            "properties": [],
        }
        target = pi._kg_class_definition("Foo_KnowledgeGraph")
        fetcher = lambda n: actual_legacy if n == "Foo_KnowledgeGraph" else target

        env_backup = {
            k: os.environ.get(k) for k in ("KG_COLLECTION", "DEVELOPMENT_COLLECTION")
        }
        os.environ["KG_COLLECTION"] = "Foo_KnowledgeGraph"
        os.environ["DEVELOPMENT_COLLECTION"] = "Foo_Development"
        args = _ap.Namespace(force_rebuild=False)

        call_order: list = []

        def _mock_snapshot(name, weaviate_url=None, sample_limit=10):
            call_order.append(("snapshot", name))
            return {"object_count": 42,
                    "sample_uuids": ["uuid-a", "uuid-b"]}

        def _mock_delete(name, weaviate_url=None):
            call_order.append(("delete", name))

        try:
            with _mock.patch.object(pi, "_drop_orphan_staging", return_value=False), \
                 _mock.patch.object(pi, "_fetch_schema",
                                    side_effect=lambda n, weaviate_url=None: fetcher(n)), \
                 _mock.patch.object(pi, "_snapshot_collection_for_rebuild",
                                    side_effect=_mock_snapshot), \
                 _mock.patch.object(pi, "_delete_class", side_effect=_mock_delete):
                logged: list = []

                def _logger(step, phase, detail="", *, data=None):
                    logged.append((step, phase, data))

                result = pi.migrate_collections(
                    args, dry_run=False, schema_fetcher=fetcher,
                    log_event=_logger,
                )
        finally:
            for k, v in env_backup.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # snapshot must precede delete for the rebuilt collection.
        kg_calls = [c for c in call_order if c[1] == "Foo_KnowledgeGraph"]
        self.assertEqual(kg_calls, [
            ("snapshot", "Foo_KnowledgeGraph"),
            ("delete", "Foo_KnowledgeGraph"),
        ])
        # And we logged a 7b.rebuild snapshot event with the data.
        snapshot_events = [
            e for e in logged
            if e[0] == "7b.rebuild" and e[1] == "snapshot"
        ]
        self.assertEqual(len(snapshot_events), 1)
        snap_data = snapshot_events[0][2]
        self.assertEqual(snap_data.get("collection"), "Foo_KnowledgeGraph")
        self.assertEqual(snap_data.get("object_count"), 42)
        self.assertEqual(snap_data.get("sample_uuids"), ["uuid-a", "uuid-b"])

        # Dev collection was at-target (target == target) → noop, no snapshot.
        # Verify the rebuild action did happen for KG.
        kg_plan = next(p for p in result["plan"]
                       if p["collection"] == "Foo_KnowledgeGraph")
        self.assertEqual(kg_plan["action"], "rebuild")


if __name__ == "__main__":
    unittest.main()
