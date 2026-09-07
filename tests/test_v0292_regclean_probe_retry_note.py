# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 regclean item 5 — the ledger stops saying "VCO retries this itself".

`deferral_probes.clear_mechanism_sentence` renders the ``paired-resolution``
family as *"auto — the component that emitted this entry clears it when its
owed work completes"*, and every surface shows the user a version of **"VCO
retries this itself"**. That is true only while the backend the retry needs
eventually comes back. A user whose code-embed service has been down since the
entry appeared reads a promise kept in form and not in substance, and nothing
on any surface says so.

Wave 4 built the reader for exactly this — `deferral_retry.retry_disposition_note`,
backed by durable ``BLOCKED`` trail rows — and correctly declined to write it
into the ledger ENTRY, because `codegraph_resync` owns that condition and
re-emits it every deferred run with last-write-wins, so a disposition written
from the dispatcher would revert in silence. It left the wiring as a recipe.

`probe_status` is `deferral_probes`' own field, and `probe_report` is the pass
that stamps it on every install/update and every bundle update. It is rendered
into `.claude/context/UPDATE_DEFERRED.md` as ``**Probe status**:`` and into the
JSON sidecar, so the correction lands there.

Scope, stated because it would be easy to overclaim: this reaches the LEDGER.
The launcher GUI still hardcodes the generic sentence at
`launcher/src/lib/deferral-ledger.ts:239` and reads no `probe_status` at all —
register item 27, owned by the launcher lane.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from vco_lib import deferral_probes as dp
from vco_lib import deferral_retry as dr

#: `auto_retryable` + a wired `retry_action` — the shape the note is FOR.
RETRYABLE_CID = "codegraph_embed_resync_pending"
#: `paired-resolution` too, but NOT auto_retryable and no handler — the shape
#: the note must stay silent about.
NO_HANDLER_CID = "residue_cleanup_pending"


class _Entry:
    """Minimal duck-typed stand-in for a `DeferralEntry`."""

    def __init__(self, condition_id: str):
        self.condition_id = condition_id
        self.detected = ""
        self.command_to_apply = ""
        self.probe_status = None


class _Report:
    def __init__(self, *entries: _Entry):
        self.entries = list(entries)


def _seed_trail(folder: Path, cid: str, statuses: "list[str]") -> Path:
    """Write an attempt trail with the given statuses, oldest first."""
    path = dr.attempts_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i, status in enumerate(statuses):
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_700_000_000 + i)),
                "condition_id": cid,
                "status": status,
                "detail": "backend unreachable",
            }) + "\n")
    return path


# --------------------------------------------------------------------------- #
# 1. The generic sentence is corrected when the trail says it should be
# --------------------------------------------------------------------------- #


def test_blocked_streak_appends_the_honest_note(tmp_path):
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * dr.BLOCKED_NOTE_THRESHOLD)

    sentence = dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path)

    assert sentence is not None
    # The registry mechanism is still stated…
    assert "auto" in sentence
    # …and no longer stands alone.
    assert "unable to retry" in sentence
    assert "until the service is back" in sentence


def test_the_cap_reached_note_says_vco_has_stopped(tmp_path):
    """The other half: a retry that burned its cap is manual work now."""
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.STARTED] * dr.MAX_ATTEMPTS)

    sentence = dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path)

    assert sentence is not None
    assert "STOPPED" in sentence
    assert "not\nsomething VCO will pick up again" in sentence or (
        "something VCO will pick up again on its own" in sentence
    )


def test_no_trail_means_no_note(tmp_path):
    """"Nothing has been tried yet" is not evidence of anything.

    An empty note is a real answer here, so the generic disposition stands —
    over-claiming in the other direction would be the same defect mirrored.
    """
    sentence = dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path)

    assert sentence == dp.clear_mechanism_sentence(RETRYABLE_CID)


def test_a_short_blocked_run_stays_generic(tmp_path):
    """Below the threshold the condition is plausibly transient."""
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * (dr.BLOCKED_NOTE_THRESHOLD - 1))

    sentence = dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path)

    assert sentence == dp.clear_mechanism_sentence(RETRYABLE_CID)


def test_a_successful_run_ends_the_streak(tmp_path):
    """"Down every time" must only be said of an unbroken run."""
    _seed_trail(
        tmp_path, RETRYABLE_CID,
        [dr.BLOCKED, dr.BLOCKED, dr.BLOCKED, dr.RETRIED],
    )

    sentence = dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path)

    assert sentence == dp.clear_mechanism_sentence(RETRYABLE_CID)


# --------------------------------------------------------------------------- #
# 2. Gating — the note is only for conditions that were promised a retry
# --------------------------------------------------------------------------- #


def test_a_condition_with_no_handler_gets_no_retry_note(tmp_path):
    """Quoting retry history at a cid nobody retries is a second wrong sentence."""
    _seed_trail(tmp_path, NO_HANDLER_CID, [dr.BLOCKED] * 5)

    assert dr.handler_name_for(NO_HANDLER_CID) is None
    sentence = dp.probe_status_sentence(NO_HANDLER_CID, None, None, folder=tmp_path)

    assert sentence == dp.clear_mechanism_sentence(NO_HANDLER_CID)


def test_folder_none_keeps_the_pre_v0292_three_argument_shape(tmp_path):
    """Back-compat: an external caller with no folder still gets a sentence."""
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * 5)

    assert dp.probe_status_sentence(RETRYABLE_CID, None, None) == (
        dp.clear_mechanism_sentence(RETRYABLE_CID)
    )


def test_a_resolved_entry_still_gets_no_status(tmp_path):
    """`verdict is False` ⇒ the entry is about to be removed; no text is owed."""
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * 5)

    assert dp.probe_status_sentence(RETRYABLE_CID, "some_probe", False, folder=tmp_path) is None


def test_the_note_is_appended_to_the_probed_arms_too(tmp_path):
    """A wired-but-blocked cid is equally mis-described by "still applies".

    That arm invites the reader to wait for a retry that is not happening, so
    the correction is not special-cased to the sentinel arm.
    """
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * dr.BLOCKED_NOTE_THRESHOLD)

    still = dp.probe_status_sentence(RETRYABLE_CID, "a_probe", True, folder=tmp_path)
    undet = dp.probe_status_sentence(RETRYABLE_CID, "a_probe", None, folder=tmp_path)

    assert still is not None and "still applies" in still and "unable to retry" in still
    assert undet is not None and "undetermined" in undet and "unable to retry" in undet


def test_retry_history_note_never_raises(tmp_path, monkeypatch):
    """An annotation must never break the pass that computes it."""
    def boom(*_a, **_kw):
        raise RuntimeError("registry on fire")

    monkeypatch.setattr(dr, "handler_name_for", boom)

    assert dp.retry_history_note(tmp_path, RETRYABLE_CID) == ""
    assert dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path) is not None


def test_a_corrupt_trail_degrades_to_the_generic_sentence(tmp_path):
    path = dr.attempts_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all\n{\n", encoding="utf-8")

    assert dp.probe_status_sentence(RETRYABLE_CID, None, None, folder=tmp_path) == (
        dp.clear_mechanism_sentence(RETRYABLE_CID)
    )


# --------------------------------------------------------------------------- #
# 3. Integration: the pass that actually stamps the ledger carries it through
# --------------------------------------------------------------------------- #


def test_probe_report_stamps_the_corrected_sentence(tmp_path):
    """`probe_report` already had the folder; this proves it now passes it on."""
    _seed_trail(tmp_path, RETRYABLE_CID, [dr.BLOCKED] * dr.BLOCKED_NOTE_THRESHOLD)
    entry = _Entry(RETRYABLE_CID)

    result = dp.probe_report(tmp_path, _Report(entry))

    assert entry.probe_status is not None
    assert "unable to retry" in entry.probe_status
    assert result.statuses[RETRYABLE_CID] == entry.probe_status
    assert result.changed == 1


def test_probe_report_leaves_an_unhandled_condition_generic(tmp_path):
    _seed_trail(tmp_path, NO_HANDLER_CID, [dr.BLOCKED] * 5)
    entry = _Entry(NO_HANDLER_CID)

    dp.probe_report(tmp_path, _Report(entry))

    assert entry.probe_status == dp.clear_mechanism_sentence(NO_HANDLER_CID)


def test_clear_mechanism_sentence_still_needs_no_folder():
    """It answers a REGISTRY question and must stay callable without a project.

    Pinned because folding the trail read into it is the tempting shortcut,
    and it would break every caller that has only a condition id — including
    `tests/test_v0291_dogfood_deferral_selfclear.py`.
    """
    assert "auto" in dp.clear_mechanism_sentence(RETRYABLE_CID)
    assert dp.clear_mechanism_sentence("a_cid_that_does_not_exist_anywhere")
