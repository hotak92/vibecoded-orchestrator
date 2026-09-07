# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""An "unchanged" chunker-revision check must not rewrite the sentinel.

`write_last_revision` stamps a fresh `updated_at` on every call, so stamping an
`"unchanged"` outcome made two consecutive bundle runs emit DIFFERENT bytes.
That surfaced as an install-idempotency failure only when the two runs straddled
a wall-clock second — a load-dependent flake over a deterministic defect, which
is the worst shape for a gate to have: it passes on a fast machine and reddens
on a busy one, so it reads as infrastructure noise.

Asserted on CONTENT with a CONTROLLED clock. The first version of this test
seeded, called the gate, and compared bytes — and passed against the unfixed
module, because both writes landed in the same wall-clock second. A test that
only reddens when the machine is slow reproduces the very defect it is meant to
pin. `_now_iso` is therefore stubbed to return a DIFFERENT value on each call,
so an unfixed module must produce different bytes every time.
"""
from __future__ import annotations

import json
from pathlib import Path

from vco_lib import chunker_revision


def _seed(folder: Path, revision: str) -> None:
    (folder / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    chunker_revision.write_last_revision(folder, revision)


def _ticking_clock(monkeypatch) -> None:
    """Make every `_now_iso()` call return a distinct timestamp.

    Without this the test is load-dependent: two writes inside one second are
    byte-identical, so the unfixed module looks correct on a fast machine.
    """
    counter = {"n": 0}

    def _tick() -> str:
        counter["n"] += 1
        return f"2026-09-03T00:00:{counter['n']:02d}Z"

    monkeypatch.setattr(chunker_revision, "_now_iso", _tick)


def test_unchanged_outcome_leaves_the_sentinel_byte_identical(tmp_path, monkeypatch):
    """The regression: a no-op check rewrote the file with a new timestamp."""
    _ticking_clock(monkeypatch)
    monkeypatch.setattr(
        "vco_lib.project_init.current_chunker_revision", lambda: "rev-abc", raising=False
    )
    _seed(tmp_path, "rev-abc")
    sentinel = chunker_revision.state_path(tmp_path)
    before = sentinel.read_bytes()

    outcome = chunker_revision.gate(tmp_path)

    assert outcome == "unchanged", outcome
    assert sentinel.read_bytes() == before, (
        "an 'unchanged' check rewrote the sentinel — two consecutive bundle runs "
        "will differ whenever they straddle a wall-clock second"
    )


def test_a_changed_revision_still_stamps(tmp_path, monkeypatch):
    """The leave-alone case must not cost us the act: a real change still records."""
    _ticking_clock(monkeypatch)
    monkeypatch.setattr(
        "vco_lib.project_init.current_chunker_revision", lambda: "rev-NEW", raising=False
    )
    monkeypatch.setattr(
        "vco_lib.project_init._emit_chunker_revision_resync_deferral",
        lambda *a, **k: None,
        raising=False,
    )
    _seed(tmp_path, "rev-OLD")
    sentinel = chunker_revision.state_path(tmp_path)

    outcome = chunker_revision.gate(tmp_path)

    assert outcome == "resync-emitted", outcome
    assert json.loads(sentinel.read_text())["revision"] == "rev-NEW", (
        "a genuine revision change must still be recorded — the idempotency fix "
        "must not silence the stamp it exists to make meaningful"
    )
