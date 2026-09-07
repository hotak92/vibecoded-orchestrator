# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 WP-14 — the support-request ask, generated from reality.

THE FAILURE THIS CLOSES (WFT C4)
--------------------------------
``docs/post-install/UPDATE-RECOVERY.md`` instructed users, unconditionally and
under a heading reading *"Manual recipe (any version; needs no working
launcher)"*, to copy ``~/.vct/update.log`` aside as step 0 "because it says
what actually failed". That file is written by ``vct-updater`` and ONLY after a
binary-swap handoff completes — so for anyone whose update never completed (the
population the recipe exists for) it cannot exist, and a user with nothing to
copy reasonably concludes their diagnostics are broken. The document asked for
an artefact its own failure mode precludes.

The probe answers from the filesystem instead of from the document, and every
ABSENT row carries the condition under which it would have appeared.

TWO REACHABLE STATES, DELIBERATELY
----------------------------------
``ok`` (the machine could be inspected) and ``unknown`` (it could not). There is
no ``problem`` tier and inventing one would be wrong: which diagnostics exist is
never itself a defect — a fresh install has none, a CLI-only user has no
launcher log. Both reachable states are asserted below, including the
``unknown`` one.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import doctor  # noqa: E402


class DiagnosticListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name) / "vct"
        self.root = Path(self._tmp.name) / "root"
        (self.root / ".claude" / "context").mkdir(parents=True)
        (self.root / "vco_lib").mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"VCT_STATE_DIR": str(self.state)})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self):
        return doctor.probe_diagnostic_files(self.root, doctor.DoctorResolvers(), {})

    def _write(self, path: Path, body: str = "x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    # ── the ask is generated, not transcribed ───────────────────────────

    def test_a_fresh_machine_lists_everything_as_absent_without_alarm(self):
        findings = self._run()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        self.assertIn("no diagnostic file exists", findings[0].summary)
        self.assertEqual(findings[0].detail["present"], [])
        self.assertEqual(len(findings[0].detail["absent"]), 6)

    def test_update_log_absence_is_explained_never_reported_as_broken(self):
        """The C4 sentence, in the code that renders it."""
        finding = self._run()[0]
        row = next(r for r in finding.detail["absent"] if r["id"] == "update_log")
        self.assertIn("ONLY after a binary swap", row["when"])
        self.assertIn("absence is normal", row["when"])
        self.assertNotEqual(finding.status, doctor.STATUS_PROBLEM)

    def test_present_files_are_listed_with_size_and_mtime(self):
        self._write(self.state / "launcher-update-state.json", '{"a":1}')
        self._write(self.root / "state" / "logs" / "install.jsonl", "{}\n")
        finding = self._run()[0]
        present_ids = {r["id"] for r in finding.detail["present"]}
        self.assertEqual(present_ids, {"launcher_update_state", "install_log"})
        for row in finding.detail["present"]:
            for f in row["files"]:
                self.assertGreater(f["size_bytes"], 0)
                self.assertRegex(f["modified"], r"^\d{4}-\d{2}-\d{2}T")
                self.assertTrue(Path(f["path"]).is_absolute())
        self.assertIn("install.jsonl", finding.summary)

    def test_the_launcher_log_is_matched_as_a_DATED_GLOB(self):
        """`logging.rs` writes `launcher.<date>.log`, never `launcher.log`.

        Naming a fixed `launcher.log` would be a third instance of the very
        defect this probe closes: a document (now a probe) asking for a
        filename no writer produces. The glob is what makes the ask real.
        """
        self._write(self.state / "logs" / "launcher.2026-09-01.log", "boot\n")
        self._write(self.state / "logs" / "launcher.2026-09-02.log", "boot\n")
        self._write(self.state / "logs" / "hub.2026-09-02.log", "hub\n")
        finding = self._run()[0]
        launcher = next(r for r in finding.detail["present"] if r["id"] == "launcher_log")
        self.assertEqual(len(launcher["files"]), 2)
        self.assertTrue(
            any(r["id"] == "hub_log" for r in finding.detail["present"]))

    def test_an_unrelated_log_in_the_shared_dir_is_not_claimed(self):
        """`<vct_root>/logs/` is SHARED — a live install had 1,692 files in it,
        1,600+ of them the Python retry driver's. The probe reports the
        artefacts it can name, and does not sweep the directory."""
        self._write(self.state / "logs" / "deferral-retry-20260827-002543.log", "x")
        finding = self._run()[0]
        self.assertEqual(finding.detail["present"], [])
        listed = str(finding.detail)
        self.assertNotIn("deferral-retry", listed)

    def test_the_probe_creates_nothing(self):
        """R8's dogfood observable: `<vct_root>/logs/` holding no
        `launcher.*`/`hub.*` file is a deliberate live observable — the log
        file's FIRST appearance is the proof that N2-R's sink shipped. A
        health check that materialises the directory it inspects would destroy
        that evidence."""
        self.assertFalse(self.state.exists())
        self._run()
        self.assertFalse(
            self.state.exists(), "the probe created the state directory")
        self.assertFalse((self.root / "state").exists())

    # ── the unknown arm ─────────────────────────────────────────────────

    def test_an_unresolvable_state_dir_still_reports_what_it_can(self):
        with mock.patch("vco_lib.paths.vct_root_dir", side_effect=RuntimeError("no")):
            findings = self._run()
        self.assertEqual(findings[0].status, doctor.STATUS_OK)
        ids = {r["id"] for r in findings[0].detail["absent"]}
        self.assertEqual(ids, {"install_log", "deferral_ledger"},
                         "the vct-root rows drop out; the folder rows remain")

    def test_no_inspectable_location_at_all_is_unknown(self):
        with mock.patch.object(doctor, "diagnostic_file_candidates", return_value=[]):
            findings = self._run()
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)
        self.assertIn("could not be inspected", findings[0].summary)

    def test_an_unreadable_location_is_unknown_not_absent(self):
        rows = [{"id": "x", "dir": self.state / "logs", "pattern": "*.log",
                 "when": "n/a"}]
        with mock.patch.object(doctor, "diagnostic_file_candidates", return_value=rows), \
             mock.patch.object(Path, "is_dir", side_effect=OSError("EACCES")):
            findings = self._run()
        self.assertEqual(findings[0].status, doctor.STATUS_UNKNOWN)

    def test_a_stat_failure_on_one_file_does_not_lose_the_rest(self):
        self._write(self.state / "launcher-update-state.json", "{}")
        self._write(self.root / "state" / "logs" / "install.jsonl", "{}")
        real_stat = Path.stat

        def _flaky(self_path, *a, **kw):
            if self_path.name == "install.jsonl":
                raise OSError("EIO")
            return real_stat(self_path, *a, **kw)

        with mock.patch.object(Path, "stat", _flaky):
            finding = self._run()[0]
        ids = {r["id"] for r in finding.detail["present"]}
        self.assertEqual(ids, {"launcher_update_state"})


class DocsParityTests(unittest.TestCase):
    """The probe and the recovery doc must name the SAME artefacts.

    Two lists that drift are how `update.log` came to be documented as a
    general log in the first place. This is the cheap structural lock: every
    id the probe can report appears in the doc's always-present table, and the
    doc's post-v0.2.92 paths are the ones the probe resolves.
    """

    def setUp(self):
        self.doc = (REPO_ROOT / "docs" / "post-install"
                    / "UPDATE-RECOVERY.md").read_text(encoding="utf-8")

    def test_every_artefact_the_probe_names_is_in_the_doc_table(self):
        with TemporaryDirectory() as td:
            rows = doctor.diagnostic_file_candidates(Path(td))
        needles = {
            "launcher_log": "logs/launcher.",
            "hub_log": "logs/hub.",
            "install_log": "state/logs/install.jsonl",
            "deferral_ledger": "UPDATE_DEFERRED.md",
            "update_log": "update.log",
            "launcher_update_state": "launcher-update-state.json",
        }
        self.assertEqual({r["id"] for r in rows}, set(needles))
        for rid, needle in needles.items():
            self.assertIn(needle, self.doc, f"{rid} is absent from the doc table")

    def test_the_doc_no_longer_asks_for_update_log_unconditionally(self):
        recipe = self.doc[self.doc.index("**Manual recipe**"):]
        step0 = recipe[: recipe.index("# 1.")]
        self.assertIn("update.log", step0)
        self.assertRegex(
            step0, r"(?i)ONLY after a binary swap|legitimately absent|whatever of these EXISTS",
            "step 0 must say that some of these are legitimately absent",
        )

    def test_the_launcher_log_stem_matches_the_rust_writer(self):
        rust = (REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src"
                / "logging.rs").read_text(encoding="utf-8")
        self.assertIn('format!("{stem}.{stamp}.log")', rust)
        self.assertIn('vct_root_dir().join("logs")', re.sub(r"\s+", " ", rust))
        with TemporaryDirectory() as td:
            rows = {r["id"]: r for r in doctor.diagnostic_file_candidates(Path(td))}
        self.assertEqual(rows["launcher_log"]["pattern"], "launcher.*.log")
        self.assertEqual(rows["hub_log"]["pattern"], "hub.*.log")

    def test_the_install_log_path_matches_install_py(self):
        install = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn('PROJECT_ROOT / "state" / "logs"', install)
        self.assertIn('log_dir / "install.jsonl"', install)
        self.assertEqual(doctor.INSTALL_LOG_REL, ("state", "logs", "install.jsonl"))

    def test_the_session_ok_marker_matches_install_py(self):
        install = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        self.assertIn('_log_install_event("session", "ok"', install)
        self.assertEqual(doctor.INSTALL_SESSION_STEP, "session")
        self.assertEqual(doctor.INSTALL_SESSION_OK, "ok")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
