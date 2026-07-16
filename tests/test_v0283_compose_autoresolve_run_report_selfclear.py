# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.83 B-1: compose-override auto-resolution must SURVIVE the run-report
seed→finalize choreography (no resurrection).

Mechanism the fix closes:

* ``compose_override_filename_conflict`` (and its siblings
  ``compose_override_renamed`` / ``_rename_failed``) are FOREIGN to install.py
  — ``project_init`` emits them, so they are NOT in
  ``install._INSTALL_OWNED_CONDITION_IDS`` / ``_PREFIXES``.
* ``InstallDeferralFlow.seed()`` (A-2) imports any FOREIGN on-disk entry into
  the RUN report at the top of ``main()``.
* ``_detect_and_rename_legacy_compose_override`` can auto-resolve a stale
  conflict ON DISK this run (reconcile → ``resolve_conditions``; or the
  identical/semantic-equal suppression paths). But
  ``resolve_conditions`` mark_resolves on a THROWAWAY ``DeferralReport``
  created inside ``locked_report`` — that instance's tombstone dies with the
  context manager. The run report still holds the entry in memory.
* ``InstallDeferralFlow.finalize()`` (P1 pre-write re-merge) then rewrites the
  in-memory run-report entry back to disk → the conflict RESURRECTS on every
  update, and ``auto-resolutions.jsonl`` grows a misleading row each run.

The fix (mirroring the R-6 owed-probe at ``install.py`` line ~16273): the
producer returns an additive ``auto_resolved_condition_ids`` key, and the
install.py caller replays each into the RUN report via ``mark_resolved`` — which
drops the in-memory copy AND tombstones the ID so the P1 late merge cannot
re-import the (already-cleared) on-disk copy.

This file drives the FULL choreography (seed → producer → caller replay →
finalize) against the REAL ``InstallDeferralFlow`` and the REAL
``_INSTALL_OWNED_CONDITION_IDS``, and asserts the entry stays cleared.

Pre-fix proof: ``test_without_replay_the_entry_resurrects`` reproduces the bug
by SKIPPING the replay — it must show resurrection (the exact pre-fix
behaviour). ``test_replay_keeps_conflict_cleared_through_finalize`` runs the
real caller replay and asserts the entry stays gone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The real vco_lib.deferral_emit lands on the merged tree; the fake helper
# no-ops to it. Import it BEFORE project_init so the function-level import in
# the producer resolves the real (or faithful-fake) module.
from tests._v0283_deferral_emit_fake import install_fake_deferral_emit  # noqa: E402

install_fake_deferral_emit()

import install  # type: ignore  # noqa: E402
from vco_lib import project_init  # noqa: E402
from vco_lib.deferral_report import DeferralEntry, DeferralReport  # noqa: E402
from vco_lib.install_deferral_flow import InstallDeferralFlow  # noqa: E402

_CONFLICT_CID = "compose_override_filename_conflict"
_LEGACY = "docker-compose.override.yml"
_CANONICAL = "compose.override.yaml"


def _infra(root: Path) -> Path:
    d = root / "infrastructure"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stale_conflict_entry() -> DeferralEntry:
    return DeferralEntry(
        condition_id=_CONFLICT_CID,
        title="Both legacy and canonical compose override files present",
        detected="a prior run deferred a filename conflict",
        why_deferred="human must compare the pair",
        command_to_apply="diff -u ...",
        severity="warning",
    )


def _new_flow(folder: Path) -> InstallDeferralFlow:
    return InstallDeferralFlow(
        folder=folder,
        owned_ids=install._INSTALL_OWNED_CONDITION_IDS,
        owned_prefixes=install._INSTALL_OWNED_CONDITION_PREFIXES,
    )


def _replay_caller_resolution(report, producer_result) -> None:
    """Mirror install.py's B-1 call-site: replay auto_resolved_condition_ids."""
    for cid in (producer_result or {}).get("auto_resolved_condition_ids", ()):
        report.mark_resolved(cid)


def test_conflict_cid_is_foreign_to_install() -> None:
    # Premise: the resurrection only happens because the conflict is FOREIGN
    # (seeded into the run report and re-merged at finalize). If it were owned
    # it would drop-when-absent and never need the mark_resolved replay.
    assert _CONFLICT_CID not in install._INSTALL_OWNED_CONDITION_IDS
    from vco_lib.deferral_report import condition_is_owned

    assert not condition_is_owned(
        _CONFLICT_CID,
        install._INSTALL_OWNED_CONDITION_IDS,
        install._INSTALL_OWNED_CONDITION_PREFIXES,
    )


def test_replay_keeps_conflict_cleared_through_finalize(tmp_path: Path) -> None:
    # A prior run deferred a compose filename conflict.
    prior = DeferralReport()
    prior.add_entry(_stale_conflict_entry())
    prior.write(tmp_path)
    assert DeferralReport.read(tmp_path).has_condition(_CONFLICT_CID)

    # This run: the pair is now byte-identical (the mirror re-synced), so the
    # producer reconciles the stale conflict ON DISK. Set up the identical pair.
    infra = _infra(tmp_path)
    body = b"services:\n  weaviate: {}\n"
    (infra / _LEGACY).write_bytes(body)
    (infra / _CANONICAL).write_bytes(body)

    # Seed (A-2) imports the FOREIGN conflict into the run report.
    flow = _new_flow(tmp_path)
    flow.seed()
    assert flow.report.has_condition(_CONFLICT_CID), (
        "premise: seed imports the FOREIGN conflict into the run report"
    )

    # The producer runs (clears the on-disk conflict via reconciliation) and
    # returns the auto-resolved condition IDs.
    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)
    assert result is not None
    assert _CONFLICT_CID in result.get("auto_resolved_condition_ids", []), (
        "producer must report the reconciled condition id (B-1 additive key)"
    )

    # The install.py caller replays the resolution into the RUN report.
    _replay_caller_resolution(flow.report, result)
    assert not flow.report.has_condition(_CONFLICT_CID), (
        "the replay must drop the conflict from the run report in memory"
    )

    # finalize()'s P1 pre-write re-merge sees no on-disk copy (producer cleared
    # it) AND the tombstone blocks resurrection even if a copy lingered.
    flow.finalize()

    after = DeferralReport.read(tmp_path)
    assert not after.has_condition(_CONFLICT_CID), (
        "B-1: the auto-resolved compose conflict must NOT resurrect through "
        "the seed→finalize choreography"
    )


def test_without_replay_the_entry_resurrects(tmp_path: Path) -> None:
    """Pre-fix reproduction (skips the caller replay).

    This is the EXACT pre-B-1 behaviour: without replaying the producer's
    resolution into the run report, the seeded in-memory conflict is rewritten
    to disk at finalize — the resurrection the fix eliminates. Kept as a control
    so a future regression that drops the replay is caught.
    """
    prior = DeferralReport()
    prior.add_entry(_stale_conflict_entry())
    prior.write(tmp_path)

    infra = _infra(tmp_path)
    body = b"services:\n  weaviate: {}\n"
    (infra / _LEGACY).write_bytes(body)
    (infra / _CANONICAL).write_bytes(body)

    flow = _new_flow(tmp_path)
    flow.seed()

    # Producer clears on disk, but we DELIBERATELY skip the caller replay.
    project_init._detect_and_rename_legacy_compose_override(tmp_path)
    # No _replay_caller_resolution(...) — reproduce the bug.

    flow.finalize()

    after = DeferralReport.read(tmp_path)
    assert after.has_condition(_CONFLICT_CID), (
        "control: without the mark_resolved replay, the seeded conflict "
        "resurrects at finalize (this is the bug B-1 fixes)"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
