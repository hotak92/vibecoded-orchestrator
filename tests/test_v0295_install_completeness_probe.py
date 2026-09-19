# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.95 WP-2 — ``install_completeness``, the half-executed-install detector.

The state under test, in one sentence: the launcher's ``apply_launcher_update``
/ ``force_resync_launcher`` / ``update_orchestrator_at`` advance the whole
source tree, rebuild only the launcher, never run ``install.py``, and then call
``manifest.rs::refresh_install_manifest``, which re-asserts ``installed: true``
over them — so the completion marker attests work nothing did, over a venv /
hooks / templates / MCP-registration / KG / schema set still at the old
version.

What that writer records decides which leg of the probe can fire, so the
fixtures below are split along the same line (v0.2.95 WP-1):

* a PRE-0.2.95 launcher re-read ``version`` from the files it had just pulled
  — the VERSION leg, and that population arrives by upgrading INTO 0.2.95;
* a 0.2.95+ launcher writes neither ``version`` nor ``completed_at``, advances
  ``source_commit``, and stamps ``post_source_only: true``. Versions then agree
  by construction, and the COMMIT leg is silent whenever ``.git/HEAD`` did not
  move or could not be read — which is `update_orchestrator_at`'s file-copy and
  a packed/worktree HEAD respectively;
* so the FLAG leg is the only one that survives both, and
  ``test_the_flag_convicts_when_the_two_records_agree`` is the ship-gate case
  that was reading ``ok`` — "rests on a real installer run" — for a manifest
  that says the opposite in as many words.

Before this probe nothing looked. Verified at HEAD while writing these tests:

* ``grep -c install-manifest vco_lib/doctor.py`` → **0**. No probe read the
  manifest at all.
* ``_currency_finding`` appends the install age to a PROBLEM summary only, and
  after one of those three paths the checkout is 0-behind upstream, so the
  currency verdict is ``ok`` and the age is dropped.
* ``probe_last_update_run`` reports the stale date as ``STATUS_OK`` — reported,
  never judged, and ``test_age_alone_is_never_graded_a_problem`` in
  ``tests/test_v0292_n2d_source_currency.py`` PINS that as a design decision.

This probe does not contradict that decision: it never reads age as evidence.
It compares two independent records of one fact — which source this install was
built from — where one record (``.claude/.vco-manifest.json``) is written ONLY
by the bundle engine an installer run reaches. ``AgeIsStillNeverEvidenceTests``
holds the line.

Hermetic: every fixture is a temp directory with the two marker dirs
``looks_like_orchestrator_root`` requires. No Weaviate, no hub, no network. The
one subprocess is ``python install.py --help`` (no ``--update`` in argv, so
install.py's pre-argparse singleton-lock peek never fires), proving the printed
remedy names a real flag — a printed command is shipped code.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.common.child_env import child_env
from vco_lib import deferral_registry, doctor
from vco_lib.deferral_report import DeferralReport
from vco_lib.install_deferral_flow import InstallDeferralFlow

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A full SHA as ``install.py::_read_git_rev`` records it, and the short form
#: ``vco_lib.vco_version.resolve`` records in the bundle manifest.
FULL_SHA_OLD = "86d204114bf0845e445ee891a4f33a0cc8acef1f"
SHORT_SHA_OLD = "86d20411"
FULL_SHA_NEW = "6d47e60a1f2b3c4d5e6f708192a3b4c5d6e7f809"
SHORT_SHA_NEW = "6d47e60a"


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _orchestrator_root(base: Path) -> Path:
    """The two markers ``vco_lib.paths.looks_like_orchestrator_root`` requires."""
    root = base / "root"
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / "vco_lib" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _write_install_manifest(root: Path, **fields) -> None:
    payload = {
        "schema_version": 1,
        "installed": True,
        "installed_at": _ts(400),
        "completed_at": _ts(2),
        "version": "0.2.95",
        "source_commit": FULL_SHA_NEW,
        "source_branch": "main",
        "install_method": "update",
    }
    payload.update(fields)
    payload = {k: v for k, v in payload.items() if v is not _ABSENT}
    path = root / "state" / "install-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_bundle_manifest(root: Path, **fields) -> None:
    payload = {
        "schema_version": 2,
        "installed_at": _ts(2),
        "vco_version": "0.2.95",
        "vco_commit": SHORT_SHA_NEW,
        "files": {},
    }
    payload.update(fields)
    payload = {k: v for k, v in payload.items() if v is not _ABSENT}
    path = root / ".claude" / ".vco-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_install_log(root: Path, *, session_ok_days_ago: float | None) -> None:
    logs = root / "state" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": _ts((session_ok_days_ago or 0) + 0.01),
            "actor": "install.py",
            "step": "session",
            "phase": "start",
            "detail": "install.py update mode",
        }
    ]
    if session_ok_days_ago is not None:
        rows.append(
            {
                "ts": _ts(session_ok_days_ago),
                "actor": "install.py",
                "step": "session",
                "phase": "ok",
                "detail": "update finished cleanly",
            }
        )
    (logs / "install.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


class _Absent:
    """Sentinel: drop this key from the fixture payload entirely."""


_ABSENT = _Absent()


def _probe(root: Path) -> doctor.Finding:
    """The probe's ONE finding for an orchestrator root.

    Exactly one, always: the not-applicable case (a plain user project) is
    asserted directly against the probe in
    ``test_a_user_project_gets_no_finding_at_all``, so a helper that quietly
    returned ``None`` here would only hide a regression.
    """
    findings = doctor.probe_install_completeness(root, doctor.DoctorResolvers(), {})
    assert len(findings) == 1, findings
    return findings[0]


class HalfExecutedStatesFireTests(unittest.TestCase):
    """The three surfaces the map's §5 register lists as undetected."""

    def test_launcher_update_that_crossed_a_version_is_a_problem(self):
        """H1: `apply_launcher_update` pulled 0.2.94 → 0.2.95 and rebuilt only
        the launcher. The bundle engine never ran, so `.vco-manifest.json`
        still records 0.2.94 while the marker claims 0.2.95 is installed."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="launcher_update", completed_at=_ts(1),
            )
            _write_bundle_manifest(
                root, vco_version="0.2.94", vco_commit=SHORT_SHA_OLD,
            )
            _write_install_log(root, session_ok_days_ago=30)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertEqual(finding.condition_id, doctor.CID_INSTALL_MARKER_UNBACKED)
        self.assertEqual(finding.fix, doctor.FIX_DEFER)
        self.assertEqual(finding.detail["convicted_on"], "version")
        self.assertTrue(finding.detail["marker_written_by_launcher_path"])
        self.assertIn("0.2.95", finding.summary)
        self.assertIn("0.2.94", finding.summary)
        self.assertIn("launcher_update", finding.summary)
        self.assertIn("install.py --update", finding.command)

    def test_launcher_update_inside_one_version_window_is_a_problem(self):
        """The COMMON shape, and the one a version comparison alone misses:
        several commits pulled inside the 0.2.95 window. Both manifests say
        0.2.95; only the commits disagree — and the bundle (hooks, templates,
        agents) is at the old commit."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="launcher_update", completed_at=_ts(1),
            )
            _write_bundle_manifest(
                root, vco_version="0.2.95", vco_commit=SHORT_SHA_OLD,
            )
            _write_install_log(root, session_ok_days_ago=9)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertEqual(finding.detail["convicted_on"], "commit")
        self.assertFalse(finding.detail["versions_disagree"])
        self.assertIs(finding.detail["commits_disagree"], True)
        self.assertIn(SHORT_SHA_NEW, finding.summary)
        self.assertIn(SHORT_SHA_OLD, finding.summary)

    def test_the_version_leg_convicts_on_its_own(self):
        """Isolates the version comparison: the commit leg is UNKNOWABLE here
        (a packed ref left `source_commit` empty), so only the version
        disagreement can produce this verdict."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=_ABSENT,
                install_method="launcher_update", completed_at=_ts(1),
            )
            _write_bundle_manifest(
                root, vco_version="0.2.94", vco_commit=SHORT_SHA_OLD,
            )
            _write_install_log(root, session_ok_days_ago=30)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertEqual(finding.detail["convicted_on"], "version")
        self.assertIsNone(finding.detail["commits_disagree"])

    def test_orchestrator_update_at_a_target_clone_is_a_problem(self):
        """H3: `update_orchestrator_at` file-copies a newer tree into another
        clone and refreshes that clone's marker. `.claude/.vco-manifest.json`
        is gitignored, so the gitignore-aware copy never carries the source
        clone's copy over — the target keeps its own, older record."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="orchestrator_update", completed_at=_ts(0.5),
            )
            _write_bundle_manifest(
                root, vco_version="0.2.91", vco_commit=SHORT_SHA_OLD,
            )
            _write_install_log(root, session_ok_days_ago=120)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertIn("orchestrator_update", finding.summary)

    def test_the_flag_convicts_when_the_two_records_agree(self):
        """The writer NAMES the state; the reader must not ignore the name.

        `update_orchestrator_at` file-copies a newer tree into a target clone
        and refreshes that clone's marker. The copy does not move the target's
        `.git/HEAD`, so `refresh_install_manifest` re-reads the commit it had
        already recorded — and since WP-1 it does not write `version` at all.
        Both comparison legs therefore see records that AGREE, and this state
        read `ok` ("rests on a real installer run") until the flag leg landed.
        `post_source_only` is the only thing on disk that separates it from a
        healthy install."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="orchestrator_update", completed_at=_ts(3),
                post_source_only=True,
            )
            _write_bundle_manifest(
                root, vco_version="0.2.95", vco_commit=SHORT_SHA_NEW,
            )
            _write_install_log(root, session_ok_days_ago=30)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertEqual(finding.condition_id, doctor.CID_INSTALL_MARKER_UNBACKED)
        self.assertEqual(finding.fix, doctor.FIX_DEFER)
        self.assertEqual(finding.detail["convicted_on"], "post_source_only")
        # The fixture IS the agreeing case: neither comparison can convict.
        self.assertFalse(finding.detail["versions_disagree"])
        self.assertIs(finding.detail["commits_disagree"], False)
        self.assertIs(finding.detail["post_source_only"], True)
        self.assertIn("post_source_only", finding.summary)
        self.assertIn("orchestrator_update", finding.summary)
        self.assertIn("install.py --update", finding.command)

    def test_a_launcher_update_whose_head_could_not_be_read_still_fires(self):
        """The second real input, and it is a DIFFERENT one: `manifest.rs`
        inserts `source_commit` only on `Some`, and `read_git_rev` answers
        `None` for a packed ref or a worktree `.git` FILE. Here the pull DID
        move the tree, but the recorded commit did not move with it — so it
        still matches the bundle manifest's, versions agree (WP-1), and the
        flag is again the only leg with anything to say."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_OLD,
                install_method="launcher_update", completed_at=_ts(0.5),
                post_source_only=True,
            )
            _write_bundle_manifest(
                root, vco_version="0.2.95", vco_commit=SHORT_SHA_OLD,
            )
            _write_install_log(root, session_ok_days_ago=12)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertEqual(finding.detail["convicted_on"], "post_source_only")
        self.assertIs(finding.detail["commits_disagree"], False)
        self.assertIn("launcher_update", finding.summary)

    def test_a_lightweight_reinstall_at_a_new_version_is_a_problem_too(self):
        """Not in the brief, but the rule covers it and the state is real
        (the map's H7): `--lightweight` skips the bundle by design and still
        writes `installed: true` at the CURRENT source version. So the marker
        outruns the installer here as well — and the summary must NOT blame a
        launcher path, because none was involved."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="lightweight", completed_at=_ts(1),
            )
            _write_bundle_manifest(root, vco_version="0.2.94",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=1)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertFalse(finding.detail["marker_written_by_launcher_path"])
        self.assertNotIn("launcher's", finding.summary)

    def test_a_marker_with_no_installer_session_at_all_says_so(self):
        """A clone that has been file-synced but never installed has an
        `installed: true` marker and no `session ok` row anywhere. That is
        corroboration for the conviction the manifests already carry — it is
        never the conviction itself (see `UncertaintyNeverConvictsTests`)."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", install_method="orchestrator_update",
            )
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_PROBLEM)
        self.assertIn("no completed install.py session is recorded", finding.summary)
        self.assertIsNone(finding.detail["last_install_session_ok"])


class HealthyStatesStayOkTests(unittest.TestCase):
    """A false problem sends a healthy install's owner into an update they do
    not need. Each of these is a state that must never read as broken."""

    def test_a_real_installer_run_leaves_both_records_agreeing(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root)  # version 0.2.95, full SHA
            _write_bundle_manifest(root)   # version 0.2.95, short SHA prefix
            _write_install_log(root, session_ok_days_ago=2)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertEqual(finding.condition_id, "")
        self.assertEqual(finding.command, "")
        self.assertIn("rests on a real installer run", finding.summary)

    def test_a_resync_that_pulled_nothing_is_not_a_problem(self):
        """THE false-positive guard. `force_resync_launcher` writes
        `install_method: launcher_update` unconditionally — including when the
        pull moved nothing. The marker's AUTHOR is therefore never the
        conviction; only a disagreement between the two records is, or the
        writer's own `post_source_only`.

        Scope note, because the fixture cannot carry it: what this pins is that
        the METHOD NAME acquits. It does not — and cannot — pin the no-op
        resync, because nothing readable from the manifest distinguishes "the
        pull moved nothing" from "a tree was copied in without moving
        `.git/HEAD`": both leave two agreeing records. That invariant is the
        WRITER's, and v0.2.95 put it there: `refresh_install_manifest` is
        reached only when the source tree actually advanced, the
        already-up-to-date branch passing `ArtefactSource::Unchanged` so it
        records nothing (ship-gate round 2, MAJOR-2, `self_update.rs`). A
        reader cannot hold that invariant, and guessing at it here would be the
        proxy this probe refuses to become."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, install_method="launcher_update", completed_at=_ts(0.1),
            )
            _write_bundle_manifest(root)
            _write_install_log(root, session_ok_days_ago=5)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertTrue(finding.detail["marker_written_by_launcher_path"])

    def test_a_marker_lagging_its_installer_is_acquitted(self):
        """install.py logs `session ok` and only THEN writes the manifest, and
        that write soft-fails. A session stamp strictly LATER than
        `completed_at` therefore means the marker is BEHIND the work, not ahead
        of it: stale bookkeeping, nothing owed."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.94", source_commit=FULL_SHA_OLD,
                completed_at=_ts(40),
            )
            _write_bundle_manifest(root, vco_version="0.2.95",
                                   vco_commit=SHORT_SHA_NEW)
            _write_install_log(root, session_ok_days_ago=1)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIn("lags the installer", finding.summary)

    def test_a_flagged_marker_that_lags_its_installer_is_still_acquitted(self):
        """Placement, pinned: the flag leg sits AFTER the acquittal, not before.

        A surviving `post_source_only` beside a LATER `session ok` has exactly
        one cause — install.py ran, logged the session, and its (soft-fail)
        manifest write did not land, so the launcher's older record stands. The
        run happened; the marker is BEHIND the work. Convicting here would send
        a user whose install is complete into another one. Measured on the live
        root, a healthy run's `session ok` precedes `completed_at` by ~4
        seconds, which is why this ordering is an acquittal and never a
        trigger."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, install_method="launcher_update", completed_at=_ts(40),
                post_source_only=True,
            )
            _write_bundle_manifest(root)
            _write_install_log(root, session_ok_days_ago=1)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIn("lags the installer", finding.summary)
        # Recorded even where it does not decide: the report must show the
        # reader what it saw and chose not to convict on.
        self.assertIs(finding.detail["post_source_only"], True)

    def test_a_v_prefixed_attested_version_still_matches(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="v0.2.95")
            _write_bundle_manifest(root, vco_version="0.2.95")
            _write_install_log(root, session_ok_days_ago=2)
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_OK)


class UncertaintyNeverConvictsTests(unittest.TestCase):
    """Positive evidence only. Every unknowable leg reports `unknown` or `ok`."""

    def test_a_missing_install_manifest_is_unknown(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_bundle_manifest(root, vco_version="0.2.90")
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)
        self.assertIn("no completed install is recorded", finding.summary)

    def test_installed_false_is_unknown(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, installed=False, version="0.2.95")
            _write_bundle_manifest(root, vco_version="0.2.90")
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)

    def test_a_corrupt_install_manifest_is_unknown_not_a_problem(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            path = root / "state" / "install-manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json", encoding="utf-8")
            _write_bundle_manifest(root, vco_version="0.2.90")
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)

    def test_a_missing_bundle_manifest_is_unknown(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95")
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)
        self.assertIn("records no source", finding.summary)

    def test_a_corrupt_bundle_manifest_is_unknown(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95")
            (root / ".claude" / ".vco-manifest.json").write_text(
                "[]", encoding="utf-8")
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_UNKNOWN)

    def test_an_absent_post_source_only_never_convicts(self):
        """What ABSENCE of the flag means, pinned so nobody re-reads it as
        innocence — or as guilt.

        The key is missing from every manifest written before v0.2.95, from
        every manifest install.py writes at any version, and from one a
        PRE-0.2.95 launcher wrote on a source-only path. So it is NO EVIDENCE:
        convicting on it would convict every healthy install in the field, and
        those populations are precisely what the version and commit legs are
        for. This fixture is the conviction fixture
        (`test_the_flag_convicts_when_the_two_records_agree`) with the one key
        removed — the same two agreeing records, the same launcher-written
        marker, the same absence of any later installer session."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_NEW,
                install_method="orchestrator_update", completed_at=_ts(3),
                post_source_only=_ABSENT,
            )
            _write_bundle_manifest(
                root, vco_version="0.2.95", vco_commit=SHORT_SHA_NEW,
            )
            _write_install_log(root, session_ok_days_ago=30)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIs(finding.detail["post_source_only"], False)
        self.assertNotIn("convicted_on", finding.detail)
        self.assertIn("rests on a real installer run", finding.summary)

    def test_only_the_writers_literal_true_is_read_as_the_flag(self):
        """One writer produces this key and it writes JSON `true`. Anything
        else on disk — a hand edit, a foreign tool, a future field that reuses
        the name — is not the writer speaking, so it is read as absent rather
        than guessed at. (`1 is True` is False in Python, deliberately: a
        truthy value is not the writer's word either.)"""
        for value in (False, "true", "yes", 1, 0, None, [], {}):
            with self.subTest(value=value):
                with TemporaryDirectory() as td:
                    root = _orchestrator_root(Path(td))
                    _write_install_manifest(
                        root, version="0.2.95", source_commit=FULL_SHA_NEW,
                        install_method="orchestrator_update",
                        completed_at=_ts(3), post_source_only=value,
                    )
                    _write_bundle_manifest(
                        root, vco_version="0.2.95", vco_commit=SHORT_SHA_NEW,
                    )
                    _write_install_log(root, session_ok_days_ago=30)
                    finding = _probe(root)

                self.assertEqual(finding.status, doctor.STATUS_OK)
                self.assertIs(finding.detail["post_source_only"], False)

    def test_a_legacy_sha_vco_version_is_never_compared_as_semver(self):
        """Pre-v0.2.92 bundle manifests carry a git short SHA in
        `vco_version`. `recorded_manifest_version` hands that back as the
        COMMIT with `version=None`, so the version leg has nothing to compare
        and must not convict on `"0.2.95" != "86d20411"`."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(
                root, version="0.2.95", source_commit=FULL_SHA_OLD)
            _write_bundle_manifest(
                root, vco_version=SHORT_SHA_OLD, vco_commit=_ABSENT)
            _write_install_log(root, session_ok_days_ago=3)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIsNone(finding.detail["built_version"])
        self.assertEqual(finding.detail["built_commit"], SHORT_SHA_OLD)

    def test_a_packed_ref_leaves_no_source_commit_and_convicts_nothing(self):
        """`install.py::_read_git_rev` returns `None` for a PACKED ref, so a
        perfectly healthy clone can record no `source_commit` at all."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, source_commit=_ABSENT)
            _write_bundle_manifest(root, vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=3)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIsNone(finding.detail["commits_disagree"])

    def test_a_non_object_name_in_vco_commit_convicts_nothing(self):
        """`recorded_manifest_version` passes the dedicated `vco_commit` field
        through UNVALIDATED, so a hand-edited or legacy `"unknown"` reaches the
        comparison. It is not an object name; it cannot disagree with one."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, source_commit=FULL_SHA_NEW)
            _write_bundle_manifest(root, vco_commit="unknown")
            _write_install_log(root, session_ok_days_ago=3)
            finding = _probe(root)

        self.assertEqual(finding.status, doctor.STATUS_OK)
        self.assertIsNone(finding.detail["commits_disagree"])

    def test_a_user_project_gets_no_finding_at_all(self):
        """Not-applicable is a fourth state, distinct from `unknown` — the same
        one `probe_source_currency` names. Inventing an `unknown` for every
        user project would be noise, not evidence."""
        with TemporaryDirectory() as td:
            plain = Path(td) / "p"
            (plain / ".claude").mkdir(parents=True)
            self.assertEqual(
                doctor.probe_install_completeness(
                    plain, doctor.DoctorResolvers(), {}),
                [],
            )


class AgeIsStillNeverEvidenceTests(unittest.TestCase):
    """Guards the NEIGHBOURING design decision this probe must not undo.

    `probe_last_update_run` refuses to grade the install age because age has
    two causes — deliberately pinned, or silently not updating — and one
    reading cannot separate them (`test_age_alone_is_never_graded_a_problem`,
    tests/test_v0292_n2d_source_currency.py). This probe judges a different
    thing: a DISAGREEMENT between two records, which has one cause.
    """

    def test_a_three_year_old_install_whose_records_agree_is_ok(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, completed_at=_ts(1100))
            _write_bundle_manifest(root, installed_at=_ts(1100))
            _write_install_log(root, session_ok_days_ago=1100)
            finding = _probe(root)
        self.assertEqual(finding.status, doctor.STATUS_OK)

    def test_the_neighbour_still_reports_that_age_without_judging_it(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, completed_at=_ts(1100))
            _write_bundle_manifest(root, installed_at=_ts(1100))
            _write_install_log(root, session_ok_days_ago=1100)
            neighbour = doctor.probe_last_update_run(
                root, doctor.DoctorResolvers(), {})[0]
        self.assertEqual(neighbour.status, doctor.STATUS_OK)
        self.assertIn("1100 day(s) ago", neighbour.summary)


class RegistrationAndLifecycleTests(unittest.TestCase):
    def test_the_probe_is_registered_full_scope_only(self):
        fn, scopes = doctor.PROBES["install_completeness"]
        self.assertIs(fn, doctor.probe_install_completeness)
        self.assertEqual(scopes, (doctor.SCOPE_FULL,))

    def test_boot_scope_does_not_run_it(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            report = doctor.run_doctor(root, scope=doctor.SCOPE_BOOT)
        self.assertNotIn(
            "install_completeness", {f.probe for f in report.findings})

    def test_a_full_pass_runs_it(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95")
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=30)
            report = doctor.run_doctor(root, scope=doctor.SCOPE_FULL)
        mine = [f for f in report.findings if f.probe == "install_completeness"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].status, doctor.STATUS_PROBLEM)

    def test_the_condition_is_registered_with_a_real_clear_mechanism(self):
        cid = doctor.CID_INSTALL_MARKER_UNBACKED
        self.assertEqual(deferral_registry.disposition_for(cid), "action_required")
        self.assertEqual(
            deferral_registry.clear_probe_for(cid), "owned-drop-when-absent")
        self.assertIn(cid, deferral_registry.install_owned_ids())
        self.assertIn(cid, doctor.DOCTOR_OWNED_CIDS)

    def test_a_problem_produces_exactly_one_ledger_entry(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95",
                                    install_method="launcher_update")
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=30)
            report = doctor.run_doctor(root, scope=doctor.SCOPE_FULL)
        entries = [
            e for e in doctor.deferral_entries_for(report)
            if e.condition_id == doctor.CID_INSTALL_MARKER_UNBACKED
        ]
        self.assertEqual(len(entries), 1)
        self.assertIn("install.py --update", entries[0].command_to_apply)
        self.assertEqual(entries[0].resolved_disposition, "action_required")

    def test_an_ok_reading_produces_no_ledger_entry(self):
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root)
            _write_bundle_manifest(root)
            _write_install_log(root, session_ok_days_ago=2)
            report = doctor.run_doctor(root, scope=doctor.SCOPE_FULL)
        self.assertNotIn(
            doctor.CID_INSTALL_MARKER_UNBACKED,
            {e.condition_id for e in doctor.deferral_entries_for(report)},
        )

    def test_the_entry_is_dropped_by_the_run_that_fixes_it(self):
        """The clear path, exercised end-to-end rather than asserted.

        `owned-drop-when-absent` means: install.py's `InstallDeferralFlow`
        excludes OWNED ids from its seed-from-disk merge, and `finalize()` is
        the run's single authoritative write — so an owned entry that this
        run's doctor pass does not re-emit simply is not written back. Here
        the root starts in the half-executed state with the entry on disk,
        the two records are then reconciled (what `install.py --update`'s
        bundle + manifest writes do), and one flow cycle drops it.
        """
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95",
                                    install_method="launcher_update")
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=30)

            # 1. The half-executed state lands the entry on disk.
            before = doctor.run_doctor(root, scope=doctor.SCOPE_FULL)
            doctor.emit_findings(root, before)
            on_disk = {e.condition_id for e in DeferralReport.read(root).entries}
            self.assertIn(doctor.CID_INSTALL_MARKER_UNBACKED, on_disk)

            # A foreign entry rides along, to prove the drop is scoped.
            foreign = DeferralReport()
            foreign.merge_from_disk(root)
            foreign.add_entry(_foreign_entry())
            foreign.write(root)

            # 2. An installer run reconciles both records...
            _write_bundle_manifest(root, vco_version="0.2.95",
                                   vco_commit=SHORT_SHA_NEW)
            _write_install_log(root, session_ok_days_ago=0)

            # ...and its deferral flow performs the single write.
            flow = InstallDeferralFlow(
                root,
                owned_ids=deferral_registry.install_owned_ids(),
                owned_prefixes=deferral_registry.install_owned_prefixes(),
            )
            flow.seed()
            after = doctor.run_doctor(root, scope=doctor.SCOPE_FULL)
            doctor.emit_findings(root, after, sink=flow.report)
            flow.finalize()

            settled = {e.condition_id for e in DeferralReport.read(root).entries}

        self.assertEqual(
            [f.status for f in after.findings if f.probe == "install_completeness"],
            [doctor.STATUS_OK],
        )
        self.assertNotIn(doctor.CID_INSTALL_MARKER_UNBACKED, settled)
        self.assertIn("a_foreign_condition", settled)


def _foreign_entry():
    from vco_lib.deferral_report import DeferralEntry

    return DeferralEntry(
        condition_id="a_foreign_condition",
        title="Written by somebody else",
        detected="proves the owned drop is scoped, not a wipe",
        why_deferred="fixture",
        command_to_apply="echo nothing",
    )


class PrintedCommandIsShippedCodeTests(unittest.TestCase):
    def test_the_remedy_names_a_real_install_py_flag(self):
        """`--update` is asserted against the LIVE argparse of the shipped
        install.py, not against a source grep. `--help` is used (and
        `--update` deliberately kept OUT of argv) because install.py peeks for
        `--update` BEFORE argparse to take a singleton lock — a lock this test
        has no business taking."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--help"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
            env=child_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("--update", proc.stdout)

    def test_the_remedy_is_rendered_through_the_portability_home(self):
        """Two lines, never `cd X && Y`: PowerShell 5.1 — the default shell on
        Windows — rejects `&&`. `remedy_shell` is the one home for that rule."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95",
                                    install_method="launcher_update")
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=30)
            finding = _probe(root)
        lines = [ln for ln in finding.command.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2, finding.command)
        self.assertTrue(lines[0].startswith("cd "))
        self.assertEqual(lines[1], "python install.py --update")
        self.assertNotIn("&&", finding.command)


class ReadOnlyTests(unittest.TestCase):
    def test_the_probe_writes_nothing(self):
        """Additive and read-only: a health check that repaired state would be
        a different work package, on files this one does not own."""
        with TemporaryDirectory() as td:
            root = _orchestrator_root(Path(td))
            _write_install_manifest(root, version="0.2.95",
                                    install_method="launcher_update")
            _write_bundle_manifest(root, vco_version="0.2.90",
                                   vco_commit=SHORT_SHA_OLD)
            _write_install_log(root, session_ok_days_ago=30)

            def snapshot():
                return {
                    str(p.relative_to(root)): p.read_bytes()
                    for p in sorted(root.rglob("*")) if p.is_file()
                }

            before = snapshot()
            _probe(root)
            after = snapshot()

        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
