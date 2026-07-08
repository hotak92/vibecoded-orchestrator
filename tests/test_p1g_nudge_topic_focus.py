# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""P1g (v0.2.75): kg-update-nudge injects the one-topic-per-node guidance.

The nudge text now tells the agent to write ONE TOPIC PER NODE (pattern /
mechanism / decision / gotcha) and NOT release/cycle chronicle nodes —
over-general "what happened in vX.Y.Z" nodes match everything and focus
nothing. The escape-marker instructions must stay intact.

The CLAUDE.md-template guideline landed separately (commit 8c8cc626); this
pins the HOOK text on both siblings.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "templates" / "hooks"

_TOPIC_MARKER = "ONE TOPIC PER NODE"
_CHRONICLE_MARKER = "never release/cycle chronicle nodes"
_ESCAPE_MARKER = "[No KG update needed:"


def _read(name: str) -> str:
    return (HOOKS / name).read_text(encoding="utf-8")


def test_sh_nudge_has_topic_focus():
    body = _read("kg-update-nudge.sh")
    assert _TOPIC_MARKER in body, "sh nudge missing one-topic-per-node guidance"
    assert _CHRONICLE_MARKER in body, "sh nudge missing chronicle-node warning"
    # escape hatch intact
    assert _ESCAPE_MARKER in body, "sh nudge lost the escape-marker instructions"


def test_ps1_nudge_has_topic_focus():
    body = _read("kg-update-nudge.ps1")
    assert _TOPIC_MARKER in body, "ps1 nudge missing one-topic-per-node guidance"
    assert _CHRONICLE_MARKER in body, "ps1 nudge missing chronicle-node warning"
    assert _ESCAPE_MARKER in body, "ps1 nudge lost the escape-marker instructions"


def test_siblings_carry_identical_topic_sentence():
    """Must-match: both siblings carry byte-identical topic-focus text."""
    sh = _read("kg-update-nudge.sh")
    ps1 = _read("kg-update-nudge.ps1")
    sentence = (
        "ONE TOPIC PER NODE — a pattern, mechanism, decision, or gotcha; "
        "never release/cycle chronicle nodes (those belong in "
        "memory/handoffs/CHANGELOG)."
    )
    assert sentence in sh, "sh missing the canonical topic sentence"
    assert sentence in ps1, "ps1 missing the canonical topic sentence"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
