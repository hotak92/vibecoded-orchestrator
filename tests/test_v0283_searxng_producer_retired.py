# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 (WP-B3): the SearXNG-remnant deferral producer is RETIRED.

Pre-v0.2.83, ``install.py::_check_searxng_remnants`` emitted a
``searxng_removed_from_default_install`` deferral whenever a
``claude_mcp_servers/searxng/`` dir or ``templates/searxng/settings.yml.template``
existed, and its remediation told the user to
``rm -r claude_mcp_servers/searxng``. That advice is actively HARMFUL to a
user running their OWN searxng MCP: VCO no longer ships searxng at all, so an
on-disk searxng dir / a ``searxng`` MCP entry is USER property, not a VCO
leftover.

WP-B3 removes the producer entirely (function + both call-sites). The
condition ID stays in ``install._INSTALL_OWNED_CONDITION_IDS`` so that any
STALE on-disk entry written by a pre-.83 run drop-when-absent self-clears on
the next install run's single-final-write (the owned set is re-detected every
run; an owned entry not re-emitted this run is not merged back from disk and
falls out on the rebuild-from-memory write).

Pins:
  1. structural — no ``_check_searxng_remnants`` symbol / call remains in
     install.py; the condition ID is still registered as install-owned.
  2. behavioural — a stale ``searxng_removed_from_default_install`` entry on
     disk is CLEARED by the InstallDeferralFlow seed → finalize cycle when the
     retired producer no longer re-emits it, while a FOREIGN entry alongside
     it survives (the retirement must not weaken foreign-preservation).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import install  # type: ignore  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402

_SEARXNG_CID = "searxng_removed_from_default_install"


def _entry(cid: str, title: str = "T") -> DeferralEntry:
    return DeferralEntry(
        condition_id=cid,
        title=title,
        detected="detected text",
        why_deferred="needs consent",
        command_to_apply="some-command --apply",
        severity="info",
    )


class TestSearxngProducerRetiredStructural(unittest.TestCase):
    """The producer is gone from the source; the condition ID is retained."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")

    def test_no_check_searxng_remnants_symbol(self):
        self.assertNotIn(
            "_check_searxng_remnants",
            self.source,
            "the searxng-remnant producer must be fully retired from install.py "
            "(no def, no call-site)",
        )

    def test_condition_id_kept_install_owned(self):
        # Retained so a stale pre-.83 on-disk entry drop-when-absent self-clears.
        self.assertIn(
            _SEARXNG_CID,
            install._INSTALL_OWNED_CONDITION_IDS,
            "the retired condition ID must stay in the install-owned set so a "
            "stale on-disk entry drop-when-absent self-clears",
        )

    def test_no_producer_helper_emits_the_condition(self):
        # The ID should appear ONLY inside the owned-set literal now — not as a
        # `condition_id=` on any DeferralEntry (that would mean a producer still
        # emits it).
        self.assertNotIn(
            f'condition_id="{_SEARXNG_CID}"',
            self.source,
            "no producer may still emit the retired searxng condition",
        )


class TestStaleSearxngEntrySelfClears(unittest.TestCase):
    """Behavioural: a stale on-disk searxng entry clears on the next run,
    while a co-resident FOREIGN entry survives (foreign-preservation intact)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_searxng_entry_dropped_foreign_kept(self):
        # A pre-.83 run persisted the searxng deferral, plus an unrelated
        # FOREIGN entry (e.g. a user-modified bundle preserve).
        prior = DeferralReport()
        prior.add_entry(_entry(_SEARXNG_CID, title="pre-.83 searxng deferral"))
        prior.add_entry(_entry("bundle_user_modified_preserved", title="foreign"))
        prior.write(self.folder)

        # The v0.2.83 run: the retired producer NEVER re-emits the searxng
        # entry. Simulate a whole install-flow cycle with the real ownership
        # sets — seed (A-2) excludes owned IDs, finalize re-merges + single
        # write. Nothing adds the searxng entry back into memory.
        flow = InstallDeferralFlow(
            folder=self.folder,
            owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
            owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
        )
        flow.seed()
        # (No producer re-emits searxng — that's the whole point of retirement.)
        flow.finalize()

        after = DeferralReport.read(self.folder)
        cids = {e.condition_id for e in after.entries}
        self.assertNotIn(
            _SEARXNG_CID, cids,
            "a stale searxng entry (owned, not re-emitted) must drop-when-absent "
            "self-clear on the next install-flow finalize",
        )
        self.assertIn(
            "bundle_user_modified_preserved", cids,
            "the co-resident FOREIGN entry must survive the retirement cycle",
        )

    def test_searxng_entry_would_persist_if_it_were_foreign(self):
        """Control: the self-clear depends on the ID being OWNED. If it were
        NOT in the owned set, the seed/finalize merge would preserve it (this
        is what makes retaining the ID load-bearing — the pin above proves the
        ID IS owned; this proves the mechanism it relies on)."""
        prior = DeferralReport()
        prior.add_entry(_entry(_SEARXNG_CID))
        prior.write(self.folder)

        # Empty owned set ⇒ searxng is treated as FOREIGN ⇒ preserved.
        flow = InstallDeferralFlow(
            folder=self.folder, owned_ids=set(), owned_prefixes=()
        )
        flow.seed()
        flow.finalize()

        after = DeferralReport.read(self.folder)
        self.assertIn(
            _SEARXNG_CID,
            {e.condition_id for e in after.entries},
            "premise: an entry NOT in the owned set is preserved — so keeping "
            "the retired ID in the owned set is what enables its self-clear",
        )


if __name__ == "__main__":
    unittest.main()
