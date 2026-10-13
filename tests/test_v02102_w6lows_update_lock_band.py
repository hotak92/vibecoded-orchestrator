# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-LOWS (R3 C-3) — a start token in the "cannot tell" band keeps
the update holder ALIVE; only a token that cannot be compared at all falls
back to the legacy start-time rule.

``process_identity_matches`` answers None for two different situations: the
tokens were compared and the gap landed in the wall-token band (over 1 s,
under 3 s), or no comparison was possible (different kinds, process not
inspectable). ``update_lock.decide`` used to send both to the legacy
"started after the claim" rule, which can read a LIVE holder as a reused pid
after a clock step — the opposite of what the CHANGELOG promises for the band.
"""
from __future__ import annotations

import os

import pytest

from vco_lib import deferral_probes as dp
from vco_lib import update_lock as ul

HOLDER = 4242


def _write(root, body: str) -> None:
    (root / ul.LOCK_BASENAME).write_text(body, encoding="utf-8")


def _probes(*, started: float):
    # alive holder, NOT an ancestor of this process
    return dict(alive=lambda pid: True, started=lambda pid: started,
                lineage=lambda: [os.getpid(), 1])


# ── the comparison home: four outcomes, not three ──────────────────────────


@pytest.mark.parametrize(("recorded", "current", "want"), [
    ("wall:100.000", "wall:100.500", dp.IDENTITY_SAME),
    ("wall:100.000", "wall:101.000", dp.IDENTITY_SAME),
    ("wall:100.000", "wall:102.000", dp.IDENTITY_BAND),
    ("wall:100.000", "wall:102.999", dp.IDENTITY_BAND),
    ("wall:100.000", "wall:103.000", dp.IDENTITY_REUSED),
    ("proc:7", "proc:7", dp.IDENTITY_SAME),
    ("proc:7", "proc:8", dp.IDENTITY_REUSED),
    ("proc:7", "wall:7.000", dp.IDENTITY_UNKNOWN),
    ("wall:x", "wall:1.000", dp.IDENTITY_UNKNOWN),
    ("wall:1.000", None, dp.IDENTITY_UNKNOWN),
    ("", "wall:1.000", dp.IDENTITY_UNKNOWN),
])
def test_identity_verdict_separates_band_from_unknown(monkeypatch, recorded, current, want):
    monkeypatch.setattr(dp, "process_start_token", lambda pid: current)
    assert dp.process_identity_verdict(HOLDER, recorded) == want
    # The boolean view is unchanged for its existing callers.
    legacy = {dp.IDENTITY_SAME: True, dp.IDENTITY_REUSED: False}.get(want)
    assert dp.process_identity_matches(HOLDER, recorded) is legacy


# ── decide(): band → alive; unknown → time rule (both arms each) ───────────


def test_band_token_keeps_the_holder_alive_even_if_it_started_after_the_claim(tmp_path):
    """started=10_000 > claimed 200 + slack: the legacy rule would say
    "reused". A band verdict means the token was compared and could not be
    told apart, so the holder stays alive → REFUSE."""
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    probes = _probes(started=10_000.0) | {"identity": lambda pid, rec: ul.IDENTITY_CANNOT_TELL}
    d = ul.check(tmp_path, **probes)
    assert d.verdict == ul.REFUSE, d
    assert "reused" not in d.reason


def test_band_token_with_an_early_start_also_refuses(tmp_path):
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    probes = _probes(started=150.0) | {"identity": lambda pid, rec: ul.IDENTITY_CANNOT_TELL}
    assert ul.check(tmp_path, **probes).verdict == ul.REFUSE


def test_uncomparable_token_still_uses_the_time_rule(tmp_path):
    """None (no comparison possible) keeps the legacy rule, both arms."""
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    none = {"identity": lambda pid, rec: None}
    assert ul.check(tmp_path, **(_probes(started=10_000.0) | none)).verdict == ul.PROCEED
    assert ul.check(tmp_path, **(_probes(started=150.0) | none)).verdict == ul.REFUSE


def test_real_comparison_band_refuses_after_a_clock_step(tmp_path, monkeypatch):
    """No injected identity: the real same_instance → deferral_probes path.
    The holder's current wall token is 2 s off the recorded one (band) and
    its start time reads far after the claim (a forward clock step) → REFUSE."""
    monkeypatch.setattr(dp, "process_start_token", lambda pid: "wall:7.000")
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    d = ul.check(tmp_path, **_probes(started=10_000.0))
    assert d.verdict == ul.REFUSE, d


def test_real_comparison_kind_mismatch_uses_the_time_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "process_start_token", lambda pid: "proc:9")
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    assert ul.check(tmp_path, **_probes(started=10_000.0)).verdict == ul.PROCEED
    assert ul.check(tmp_path, **_probes(started=150.0)).verdict == ul.REFUSE


def test_real_comparison_clear_difference_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "process_start_token", lambda pid: "wall:50.000")
    _write(tmp_path, f"{HOLDER}\n200\nwall:5.000\n")
    d = ul.check(tmp_path, **_probes(started=150.0))
    assert d.verdict == ul.PROCEED and "reused" in d.reason, d
