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
_RENAMED_CID = "compose_override_renamed"
_RENAME_FAILED_CID = "compose_override_rename_failed"
_LEGACY = "docker-compose.override.yml"
_CANONICAL = "compose.override.yaml"


def _stale_record(cid: str) -> DeferralEntry:
    return DeferralEntry(
        condition_id=cid,
        title=f"stale {cid}",
        detected="a prior run emitted this one-shot notice",
        why_deferred="informational",
        command_to_apply="noop",
        severity="info",
    )


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


# ---------------------------------------------------------------------------
# N-2 (v0.2.83): the two one-shot informational records — compose_override_renamed
# and compose_override_rename_failed — now RECONCILE when their condition no
# longer holds, AND (applying the B-1 lesson) survive the seed→finalize
# choreography because they are FOREIGN, non-install-owned IDs replayed via the
# same auto_resolved_condition_ids channel.
# ---------------------------------------------------------------------------

def test_renamed_and_rename_failed_are_foreign_to_install() -> None:
    from vco_lib.deferral_report import condition_is_owned

    for cid in (_RENAMED_CID, _RENAME_FAILED_CID):
        assert cid not in install._INSTALL_OWNED_CONDITION_IDS
        assert not condition_is_owned(
            cid,
            install._INSTALL_OWNED_CONDITION_IDS,
            install._INSTALL_OWNED_CONDITION_PREFIXES,
        ), f"{cid} must be FOREIGN for the N-2 resurrection risk to apply"


def _run_selfclear_scenario(tmp_path: Path, stale_cid: str) -> None:
    """Prior run left ``stale_cid`` on disk; THIS run has no legacy file (the
    action settled), so the producer reconciles it. Drive the full
    seed → producer → replay → finalize choreography and assert the record does
    NOT resurrect."""
    prior = DeferralReport()
    prior.add_entry(_stale_record(stale_cid))
    prior.write(tmp_path)
    assert DeferralReport.read(tmp_path).has_condition(stale_cid)

    # No legacy file this run → the settled one-shot condition no longer holds.
    _infra(tmp_path)  # empty dir, no legacy/canonical files

    flow = _new_flow(tmp_path)
    flow.seed()
    assert flow.report.has_condition(stale_cid), (
        "premise: seed imports the FOREIGN one-shot record into the run report"
    )

    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)
    assert result is not None, "producer must return a dict when it reconciles"
    assert stale_cid in result.get("auto_resolved_condition_ids", []), (
        f"producer must report {stale_cid} as reconciled (N-2 scope)"
    )

    _replay_caller_resolution(flow.report, result)
    assert not flow.report.has_condition(stale_cid), (
        "the replay must drop the stale record from the run report in memory"
    )

    flow.finalize()

    after = DeferralReport.read(tmp_path)
    assert not after.has_condition(stale_cid), (
        f"N-2: the reconciled {stale_cid} must NOT resurrect through "
        "seed→finalize (foreign ID replayed via auto_resolved_condition_ids)"
    )


def test_stale_renamed_record_selfclears_through_finalize(tmp_path: Path) -> None:
    _run_selfclear_scenario(tmp_path, _RENAMED_CID)


def test_stale_rename_failed_record_selfclears_through_finalize(
    tmp_path: Path,
) -> None:
    _run_selfclear_scenario(tmp_path, _RENAME_FAILED_CID)


def test_without_replay_stale_renamed_resurrects(tmp_path: Path) -> None:
    """Control: without the caller replay, the seeded stale renamed record is
    rewritten to disk at finalize — the N-2 resurrection the replay eliminates.
    """
    prior = DeferralReport()
    prior.add_entry(_stale_record(_RENAMED_CID))
    prior.write(tmp_path)

    _infra(tmp_path)

    flow = _new_flow(tmp_path)
    flow.seed()
    project_init._detect_and_rename_legacy_compose_override(tmp_path)
    # DELIBERATELY skip the replay → reproduce the resurrection.
    flow.finalize()

    after = DeferralReport.read(tmp_path)
    assert after.has_condition(_RENAMED_CID), (
        "control: without the mark_resolved replay, the seeded renamed record "
        "resurrects at finalize (the N-2 resurrection the replay fixes)"
    )


def test_active_renamed_this_run_is_not_reconciled(tmp_path: Path) -> None:
    """Leave-alone: when the producer ACTUALLY renames a legacy file THIS run,
    the fresh compose_override_renamed record must be EMITTED, not reconciled
    away (it is in active_condition_ids)."""
    infra = _infra(tmp_path)
    (infra / _LEGACY).write_bytes(b"services: {}\n")  # legacy present, no canonical

    result = project_init._detect_and_rename_legacy_compose_override(tmp_path)
    assert result is not None
    assert result["action"] == "renamed"
    # The fresh notice is emitted on disk, NOT in the auto_resolved set.
    assert DeferralReport.read(tmp_path).has_condition(_RENAMED_CID)
    assert _RENAMED_CID not in result.get("auto_resolved_condition_ids", []), (
        "a rename that HAPPENED this run must emit the notice, not reconcile it"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
