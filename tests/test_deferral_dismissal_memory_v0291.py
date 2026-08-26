# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-B item 5 (decision #17) — GENERALIZED dismissal memory.

Before this, exactly ONE condition had a dismissal memory
(``template_review_pending``, v0.2.83 D9), hand-implemented inside
``project_init``. Everything else re-fired on the next update no matter how
many times the user dismissed it — most visibly ``dual_ollama_detected``, which
is re-detected on EVERY run, so for a user legitimately running two Ollama
daemons the entry was immortal by construction.

The generalization that was REJECTED is as important as the one that shipped: a
"dismiss until the entry's ``detected`` text changes" key would make cosmetic
rewording of a message silently re-fire every user's dismissal. So the key is
built from registry-DECLARED stable fields, never from prose. These tests pin
both halves — the key changes when the STATE changes, and does not when only the
wording does.
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

from vco_lib import deferral_dismissal as dd  # noqa: E402
from vco_lib import deferral_registry as dr  # noqa: E402
from vco_lib.deferral_report import DeferralEntry  # noqa: E402


class KeyingTests(unittest.TestCase):
    def test_key_depends_only_on_declared_fields(self):
        """An emitter attaching extra fields must not perturb the identity —
        otherwise adding a diagnostic value would re-fire dismissals."""
        a = dd.compute_key(
            "dual_ollama_detected", {"alt_port": "11434", "canon_port": "11435"}
        )
        b = dd.compute_key(
            "dual_ollama_detected",
            {"alt_port": "11434", "canon_port": "11435", "note": "whatever"},
        )
        self.assertEqual(a, b)

    def test_key_changes_when_a_declared_field_changes(self):
        a = dd.compute_key(
            "dual_ollama_detected", {"alt_port": "11434", "canon_port": "11435"}
        )
        b = dd.compute_key(
            "dual_ollama_detected", {"alt_port": "11439", "canon_port": "11435"}
        )
        self.assertNotEqual(a, b)

    def test_key_is_stable_across_container_ordering(self):
        """Canonicalisation matters: the same mapping in a different insertion
        order is the same STATE and must hash the same."""
        a = dd.compute_key(
            "template_review_pending",
            {"reference_hashes": {"CLAUDE.md": "aa", "MEMORY.md": "bb"}},
        )
        b = dd.compute_key(
            "template_review_pending",
            {"reference_hashes": {"MEMORY.md": "bb", "CLAUDE.md": "aa"}},
        )
        self.assertEqual(a, b)

    def test_condition_without_a_declared_key_is_manual(self):
        self.assertEqual(dr.dismiss_key_fields("safe_add_skipped_env_merge"), ())
        self.assertEqual(
            dd.compute_key("safe_add_skipped_env_merge", {"anything": "x"}),
            dd.MANUAL_KEY,
        )

    def test_prose_is_never_an_input(self):
        """The rejected design, pinned: two entries with wildly different
        prose but identical declared state share one key."""
        e1 = DeferralEntry(
            condition_id="dual_ollama_detected", title="A", detected="old wording",
            why_deferred="w", command_to_apply="c", severity="info",
            dismiss_fields={"alt_port": "11434", "canon_port": "11435"},
        )
        e2 = DeferralEntry(
            condition_id="dual_ollama_detected", title="B",
            detected="completely rewritten copy for v0.2.92",
            why_deferred="different", command_to_apply="different",
            severity="info",
            dismiss_fields={"alt_port": "11434", "canon_port": "11435"},
        )
        self.assertEqual(
            dd.compute_key(e1.condition_id, e1.dismiss_fields),
            dd.compute_key(e2.condition_id, e2.dismiss_fields),
        )


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        (self.folder / ".claude").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _manifest(self) -> dict:
        return json.loads(
            (self.folder / ".claude" / ".vco-manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_record_then_suppress(self):
        fields = {"alt_port": "11434", "canon_port": "11435"}
        self.assertFalse(
            dd.dismissal_suppresses(self.folder, "dual_ollama_detected", fields)
        )
        self.assertTrue(
            dd.record_dismissal(self.folder, "dual_ollama_detected", fields)
        )
        self.assertTrue(
            dd.dismissal_suppresses(self.folder, "dual_ollama_detected", fields)
        )

    def test_state_change_re_fires(self):
        dd.record_dismissal(
            self.folder, "dual_ollama_detected",
            {"alt_port": "11434", "canon_port": "11435"},
        )
        self.assertFalse(
            dd.dismissal_suppresses(
                self.folder, "dual_ollama_detected",
                {"alt_port": "11439", "canon_port": "11435"},
            )
        )

    def test_manifest_shape_is_diagnosable(self):
        dd.record_dismissal(
            self.folder, "dual_ollama_detected",
            {"alt_port": "11434", "canon_port": "11435"},
        )
        row = self._manifest()["dismissals"]["dual_ollama_detected"]
        self.assertTrue(row["key"].startswith("sha256:"))
        self.assertEqual(
            row["fields"], {"alt_port": "11434", "canon_port": "11435"}
        )
        self.assertTrue(row["dismissed_at"].endswith("Z"))

    def test_manifest_schema_version_is_not_bumped(self):
        dd.record_dismissal(self.folder, "dual_ollama_detected", {})
        self.assertEqual(self._manifest()["schema_version"], 2)

    def test_existing_manifest_content_survives(self):
        """A dismissal must never cost the user their manifest — the file also
        carries the bundle's prior-shipped hashes."""
        (self.folder / ".claude" / ".vco-manifest.json").write_text(
            json.dumps({"schema_version": 2, "files": {"a.md": "hash"}}),
            encoding="utf-8",
        )
        dd.record_dismissal(self.folder, "dual_ollama_detected", {"alt_port": "1"})
        self.assertEqual(self._manifest()["files"], {"a.md": "hash"})

    def test_no_dismissal_recorded_never_suppresses(self):
        self.assertFalse(
            dd.dismissal_suppresses(self.folder, "dual_ollama_detected", {})
        )


class LegacyMigrationTests(unittest.TestCase):
    """A pre-v0.2.91 dismissal keeps suppressing.

    An update must never silently un-dismiss something the user already
    silenced — that would look exactly like the bug this release fixes,
    inverted.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)
        (self.folder / ".claude").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_legacy(self, hashes: dict) -> None:
        (self.folder / ".claude" / ".vco-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "dismissals": {
                        "template_review_pending": {
                            "reference_hashes": hashes,
                            "dismissed_at": "2026-07-24T00:00:00Z",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_legacy_reference_hashes_still_suppress(self):
        hashes = {"CLAUDE.md": "aaa", "CONTEXT_STATE.md": "bbb"}
        self._write_legacy(hashes)
        self.assertTrue(
            dd.dismissal_suppresses(
                self.folder, "template_review_pending",
                {"reference_hashes": hashes},
            )
        )

    def test_legacy_dismissal_re_fires_when_a_reference_changes(self):
        self._write_legacy({"CLAUDE.md": "aaa"})
        self.assertFalse(
            dd.dismissal_suppresses(
                self.folder, "template_review_pending",
                {"reference_hashes": {"CLAUDE.md": "NEW"}},
            )
        )

    def test_legacy_key_equals_the_modern_key(self):
        """The migration is only lossless if both paths hash identically."""
        hashes = {"CLAUDE.md": "aaa"}
        self._write_legacy(hashes)
        self.assertEqual(
            dd.stored_key(self.folder, "template_review_pending"),
            dd.compute_key(
                "template_review_pending", {"reference_hashes": hashes}
            ),
        )


class FieldResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_entry_carried_fields_win(self):
        entry = DeferralEntry(
            condition_id="dual_ollama_detected", title="t", detected="d",
            why_deferred="w", command_to_apply="c", severity="info",
            dismiss_fields={"alt_port": "1", "canon_port": "2"},
        )
        self.assertEqual(
            dd.fields_for(self.folder, "dual_ollama_detected", entry),
            {"alt_port": "1", "canon_port": "2"},
        )

    def test_fallback_provider_reads_the_entry_for_sidecars(self):
        entry = DeferralEntry(
            condition_id="orchestrator_user_modified_preserved",
            title="t",
            detected="upstream saved as `docs/A.md.from-upstream-5a9ae53` (x)",
            why_deferred="w", command_to_apply="c", severity="info",
        )
        self.assertEqual(
            dd.fields_for(
                self.folder, "orchestrator_user_modified_preserved", entry
            ),
            {"preserved_sidecars": ["docs/A.md.from-upstream-5a9ae53"]},
        )

    def test_no_provider_and_no_entry_yields_empty_fields(self):
        self.assertEqual(
            dd.fields_for(self.folder, "safe_add_skipped_env_merge"), {}
        )


class DismissCommandEndToEndTests(unittest.TestCase):
    """The USER-FACING path: `python -m vco_lib.project_init dismiss-deferral`.

    The v0.2.83 command only recorded a memory for ONE condition. Everything
    else was dismissed and immediately re-emitted on the next update — the
    behaviour a user experiences as "I dismissed this five times and it keeps
    coming back". These tests exercise the command itself, not the helper.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _dismiss(self, condition_id: str) -> int:
        import argparse  # noqa: PLC0415

        from vco_lib.project_init import _cmd_dismiss_deferral  # noqa: PLC0415

        return _cmd_dismiss_deferral(
            argparse.Namespace(
                folder=str(self.folder), condition_id=condition_id, json=True,
            )
        )

    def _emit(self, entry: DeferralEntry) -> None:
        from vco_lib.deferral_emit import emit  # noqa: PLC0415

        emit(self.folder, entry)

    def _dual_ollama_entry(self, alt: str, canon: str) -> DeferralEntry:
        return DeferralEntry(
            condition_id="dual_ollama_detected",
            title=f"Two Ollama instances reachable (:{alt} + :{canon})",
            detected="both responded", why_deferred="w",
            command_to_apply="c", severity="info",
            dismiss_fields={"alt_port": alt, "canon_port": canon},
        )

    def test_dismissing_records_the_key_from_the_entry(self):
        self._emit(self._dual_ollama_entry("11434", "11435"))
        self.assertEqual(self._dismiss("dual_ollama_detected"), 0)
        self.assertTrue(
            dd.dismissal_suppresses(
                self.folder, "dual_ollama_detected",
                {"alt_port": "11434", "canon_port": "11435"},
            ),
            "the dismissal must hold for the SAME port pair — otherwise the "
            "next update re-emits and the user dismisses forever",
        )

    def test_dismissal_lapses_when_the_topology_changes(self):
        self._emit(self._dual_ollama_entry("11434", "11435"))
        self._dismiss("dual_ollama_detected")
        self.assertFalse(
            dd.dismissal_suppresses(
                self.folder, "dual_ollama_detected",
                {"alt_port": "11439", "canon_port": "11435"},
            ),
            "a genuinely different environment must surface again",
        )

    def test_dismissing_a_keyless_condition_records_a_manual_dismissal(self):
        self._emit(
            DeferralEntry(
                condition_id="safe_add_skipped_env_merge", title="t",
                detected="d", why_deferred="w", command_to_apply="c",
                severity="info",
            )
        )
        self.assertEqual(self._dismiss("safe_add_skipped_env_merge"), 0)
        self.assertEqual(
            dd.stored_key(self.folder, "safe_add_skipped_env_merge"),
            dd.MANUAL_KEY,
        )

    def test_dismissing_removes_the_ledger_entry(self):
        from vco_lib.deferral_report import DeferralReport  # noqa: PLC0415

        self._emit(self._dual_ollama_entry("11434", "11435"))
        self._dismiss("dual_ollama_detected")
        self.assertEqual(DeferralReport.read(self.folder).entries, [])


class RegistryContractTests(unittest.TestCase):
    def test_declared_keys_match_what_the_code_populates(self):
        self.assertEqual(
            dr.dismiss_key_fields("dual_ollama_detected"),
            ("alt_port", "canon_port"),
        )
        self.assertEqual(
            dr.dismiss_key_fields("template_review_pending"),
            ("reference_hashes",),
        )
        self.assertEqual(
            dr.dismiss_key_fields("orchestrator_user_modified_preserved"),
            ("preserved_sidecars",),
        )

    def test_manifest_path_matches_project_init(self):
        """One manifest, one path. A drift here would write dismissals into a
        file nothing reads."""
        from vco_lib.project_init import _MANIFEST_REL  # noqa: PLC0415

        self.assertEqual(dd.MANIFEST_REL, _MANIFEST_REL)


if __name__ == "__main__":
    unittest.main()
