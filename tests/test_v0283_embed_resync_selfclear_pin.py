# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 (WP-B3 / B-F8): regression PIN — codegraph embed-resync self-clear.

``codegraph_embed_resync_pending`` is a FOREIGN "owed work" ledger entry
(DELIBERATELY excluded from ``install._INSTALL_OWNED_CONDITION_IDS`` — see the
maintenance note there). It clears in exactly ONE way: the R-6 owed-probe
POSITIVELY confirms zero stale rows (``not_owed``), and
``install._trigger_codegraph_embed_resync`` calls
``deferral_report.mark_resolved("codegraph_embed_resync_pending")``. The
``mark_resolved`` tombstone then blocks the P1 pre-write re-merge in
``InstallDeferralFlow.finalize`` from resurrecting the still-on-disk copy.

The mechanism landed in v0.2.73 (R-6) and its TOCTOU-safety was pinned by
``tests/test_deferral_toctou_v0275.py``. WP-B3's B-F8 requires an EXPLICIT pin
that the whole self-clear path works end-to-end through the real trigger shim
+ the real install-flow finalize — this file provides it (no production change;
the probe was verified working in source, so this pins behaviour rather than
fixing it).

If this test ever fails, do NOT patch it here — the probe / trigger /
tombstone live in ``vco_lib/codegraph_resync.py`` +
``vco_lib/install_deferral_flow.py`` +
``vco_lib/deferral_report.py`` (WP-B1's files). Report to the coordinator.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402

_RESYNC_CID = "codegraph_embed_resync_pending"


def _resync_entry() -> DeferralEntry:
    return DeferralEntry(
        condition_id=_RESYNC_CID,
        title="Code-graph embed resync pending",
        detected="stale rows at an older embed revision",
        why_deferred="background resync owed",
        command_to_apply="python install.py --update",
        severity="info",
    )


class _NotOwedResult:
    """Mimic vco_lib.codegraph_resync.ResyncTriggerResult(status='not_owed')."""
    status = "not_owed"
    message = "all rows at current embed revision"
    pid = None
    deferral = None


class TestEmbedResyncSelfClearPin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resync_entry_is_owed_probe_foreign_and_never_owned(self):
        # Premise the self-clear relies on: the resync ledger entry is FOREIGN
        # (NOT in install's owned set) — its ONLY clearing path is an explicit
        # mark_resolved on the not_owed probe.
        self.assertNotIn(
            _RESYNC_CID,
            install._INSTALL_OWNED_CONDITION_IDS,
            "codegraph_embed_resync_pending must stay FOREIGN — it is owed-work, "
            "not a re-detected-per-run condition (see the maintenance note).",
        )

    def test_not_owed_probe_clears_and_does_not_resurrect(self):
        # Prior run persisted the FOREIGN resync ledger entry on disk.
        prior = DeferralReport()
        prior.add_entry(_resync_entry())
        prior.write(self.folder)

        # The v0.2.83 install run: seed (A-2) imports the FOREIGN entry.
        flow = InstallDeferralFlow(
            folder=self.folder,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()
        self.assertTrue(
            flow.report.has_condition(_RESYNC_CID),
            "premise: the resync entry is FOREIGN and seeded into memory",
        )

        # The R-6 owed-probe returns not_owed → the trigger shim resolves it.
        # Patch spawn_background_resync at the module install.py imports it FROM
        # (install.py does `from vco_lib.codegraph_resync import
        # spawn_background_resync` at function level).
        with mock.patch(
            "vco_lib.codegraph_resync.spawn_background_resync",
            return_value=_NotOwedResult(),
        ):
            # Also neutralize the venv-python / project-name resolution side
            # trips — they don't affect the not_owed branch, but keep the shim
            # from touching the filesystem unexpectedly.
            with mock.patch.object(
                install, "_derive_orchestrator_project_name", return_value="Proj"
            ):
                install._trigger_codegraph_embed_resync(flow.report)

        # The in-memory copy is gone AND tombstoned.
        self.assertFalse(
            flow.report.has_condition(_RESYNC_CID),
            "the not_owed probe must mark_resolve the resync entry in memory",
        )

        # finalize()'s P1 pre-write re-merge sees the still-on-disk copy — the
        # tombstone MUST block resurrection — then the single final write lands.
        flow.finalize()

        after = DeferralReport.read(self.folder)
        self.assertNotIn(
            _RESYNC_CID,
            {e.condition_id for e in after.entries},
            "the probe-resolved resync entry must NOT resurrect through the P1 "
            "late merge in InstallDeferralFlow.finalize (tombstone honored)",
        )

    def test_control_without_probe_the_entry_persists(self):
        """Leave-alone control: with NO not_owed probe (no trigger call), the
        FOREIGN resync entry is preserved across seed→finalize — proving the
        self-clear is driven by the explicit probe, not by an unconditional
        drop (which would lose genuinely-owed work)."""
        prior = DeferralReport()
        prior.add_entry(_resync_entry())
        prior.write(self.folder)

        flow = InstallDeferralFlow(
            folder=self.folder,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()
        # No trigger, no mark_resolved — the probe did NOT confirm not_owed.
        flow.finalize()

        after = DeferralReport.read(self.folder)
        self.assertIn(
            _RESYNC_CID,
            {e.condition_id for e in after.entries},
            "without a positive not_owed probe, the owed-work ledger entry must "
            "survive (never dropped speculatively)",
        )


if __name__ == "__main__":
    unittest.main()
