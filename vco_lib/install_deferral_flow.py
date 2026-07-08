# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""install.py-side deferral-report choreography (P2c-b, v0.2.75).

Extracted from ``install.py main()`` so the seed → accumulate → finalize
lifecycle of the run's :class:`~vco_lib.deferral_report.DeferralReport`
has ONE owner and main() keeps thin call sites (``flow.seed()`` /
``flow.finalize()``). The semantics are the accumulated fixes, preserved
bit-for-bit:

* **A-2 seed** (v0.2.73): at the top of the run, merge the on-disk
  report's FOREIGN entries into the run report so the end-of-run write
  cannot clobber entries other writer families persisted (project_init,
  Rust emitters, background resync children). Condition IDs install.py
  OWNS (re-detected every run — the ``owned_ids`` / ``owned_prefixes``
  sets) are NOT merged, keeping their drop-when-absent self-cleaning
  semantics.

* **A-11 single write** (v0.2.73): the historical mid-run write was
  removed — with an empty in-memory report it unlinked the on-disk file
  (and stripped the CLAUDE.md reminder) minutes before the final write,
  so a hard kill in that window lost every foreign entry.
  :meth:`InstallDeferralFlow.finalize` is the ONLY writer
  (``tests/test_deferral_foreign_preservation_v0273.py`` +
  ``tests/test_deferral_toctou_v0275.py`` hold the structural guards).

* **P1 TOCTOU close** (v0.2.75): the A-2 seed snapshots the disk at t0,
  but a detached child (e.g. the P7 resync driver failing fast) can
  read-merge-write a NEW foreign entry minutes later — a rebuild-from-
  memory write would clobber it. ``finalize()`` therefore re-merges from
  disk at the last moment before the single write: owned IDs stay
  excluded, and ``merge_from_disk``'s per-run ``mark_resolved``
  tombstones prevent resurrecting entries this run explicitly settled
  (canonical: ``codegraph_embed_resync_pending`` after the R-6 not_owed
  probe — cleared from MEMORY only; its on-disk copy still exists here
  and must NOT be re-imported).

Failure posture: the late merge inside ``finalize()`` soft-fails into
:attr:`FinalizeResult.merge_error` (the write still proceeds — a merge
failure must not also lose the run's own entries); a WRITE failure
propagates to the caller, which logs-and-continues (install completion
never blocks on the deferral file).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

from vco_lib.deferral_report import DeferralReport


@dataclass
class FinalizeResult:
    """Outcome of :meth:`InstallDeferralFlow.finalize`."""

    #: Entries preserved by the P1 pre-write disk re-merge (0 = none found).
    late_merged: int
    #: Non-``None`` when the late merge soft-failed (the write still ran).
    merge_error: Optional[str]
    #: ``DeferralReport.write`` result: ``True`` = entries present, file
    #: written; ``False`` = no entries, existing file(s) deleted (the
    #: caller writes the --update paper-trail stub in that case).
    wrote_entries: bool


class InstallDeferralFlow:
    """One owner for a single install.py run's deferral-report lifecycle.

    Wraps the run's :class:`DeferralReport` (exposed as :attr:`report` so
    the many mid-run emitters keep passing it around unchanged), the
    resolved target ``folder`` (HIGH-2: the user-project folder when
    ``--project-folder`` is passed; the orchestrator root otherwise) and
    the caller's owned-condition sets.
    """

    def __init__(
        self,
        folder: Path,
        owned_ids: Iterable[str],
        owned_prefixes: Iterable[str],
    ) -> None:
        self.folder = Path(folder)
        self.owned_ids = frozenset(owned_ids)
        self.owned_prefixes: Tuple[str, ...] = tuple(owned_prefixes)
        #: The run's accumulating report. Mid-run steps add/resolve entries
        #: directly on this object; the flow only owns seed + finalize.
        self.report = DeferralReport()

    def _merge_foreign_from_disk(self) -> int:
        """One home for the exclusion-scoped disk merge (A-2 seed and the
        P1 pre-write re-merge are the SAME operation at two moments)."""
        return self.report.merge_from_disk(
            self.folder,
            exclude_ids=self.owned_ids,
            exclude_prefixes=self.owned_prefixes,
        )

    def seed(self) -> int:
        """A-2: import FOREIGN on-disk entries into the run report.

        Returns the number of entries merged. Exceptions propagate — the
        caller treats a seed failure as best-effort (logs a warning and
        degrades to the pre-A-2 behaviour, never blocking the install).
        """
        return self._merge_foreign_from_disk()

    def finalize(self) -> FinalizeResult:
        """P1 pre-write re-merge, then the run's SINGLE authoritative write.

        The re-merge soft-fails into ``merge_error`` (best-effort — its
        failure must not also discard the run's own entries). The write
        itself may raise; the caller logs and soft-fails, preserving
        install.py's historical behaviour. This is the only ``write`` call
        in the flow — and, by the structural guards, in the whole
        install.py choreography.
        """
        late_merged = 0
        merge_error: Optional[str] = None
        try:
            late_merged = self._merge_foreign_from_disk()
        except Exception as exc:  # noqa: BLE001 — re-merge is best-effort
            merge_error = str(exc)
        wrote_entries = self.report.write(self.folder)
        return FinalizeResult(
            late_merged=late_merged,
            merge_error=merge_error,
            wrote_entries=wrote_entries,
        )
