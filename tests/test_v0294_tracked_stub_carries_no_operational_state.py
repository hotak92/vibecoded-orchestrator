# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The tracked orchestrator-root `CLAUDE.md` stub carries no rendered state.

`vco_lib/deferral_report.py` renders a "Pending VCO action" reminder block
into a PROJECT's CLAUDE.md while that project's ledger has rows. The
checkout is not a project — but two tools mistook it for one (a launcher
booted from a binary inside it; the MCP server falling back to its module
root during the test suite), rendered the block into the tracked stub, and
`git add -A` at the v0.2.92 release committed it. It shipped in two
releases. The privacy gate now refuses the marker in any tracked file; this
test says the same thing from inside the suite, so a leak shows up in the
run that caused it rather than at tag time.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_the_tracked_claude_md_stub_has_no_deferral_reminder_block() -> None:
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    for needle in ("vco-deferral-reminder-begin", "Pending VCO action", "UPDATE_DEFERRED.md exists"):
        assert needle not in text, (
            f"CLAUDE.md carries rendered operational state ({needle!r}): something "
            f"treated this checkout as an install root and wrote its ledger here"
        )


def test_the_checkout_has_no_deferral_ledger_left_by_the_suite() -> None:
    """A ledger under the checkout's `.claude/context/` is the same leak one
    layer down (git-ignored, so it hides). It must not exist after the suite's
    own containment (CLAUDE_PROJECT_DIR pinned to a scratch project)."""
    ledger = REPO / ".claude" / "context" / "UPDATE_DEFERRED.md"
    assert not ledger.exists(), (
        f"{ledger} exists — a writer resolved the checkout as a project/install root"
    )
