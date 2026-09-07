# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92: the CLAUDE.md deferral-reminder block has exactly ONE owner.

The bug
-------
Two emitters owned the same wrapped block and could not see each other:

* ``templates/ORCHESTRATOR-CLAUDE.md.template`` carried a literal copy at
  line 2, so every AUTO-region render re-emitted it — static text, no live
  counts, and UNCONDITIONAL (it asserted "Pending VCO action:
  UPDATE_DEFERRED.md exists" on installs that had no ledger at all).
* ``vco_lib.deferral_report._splice_reminder_into_claude_md`` injects its own
  copy and is the only emitter that knows whether a ledger exists and what
  its live actionable/informational counts are.

Whenever a CLAUDE.md reached "no block" while a ledger was live — a resolved
ledger strips the block, and any later emit (install finalize, or a detached
child through ``vco_lib.deferral_emit``) splices before the next render —
the splice fell through to its prepend case and landed ABOVE the AUTO region.
The next render then re-added the template's copy INSIDE it.  From that point
the splice forever refreshed the top copy and the render forever re-emitted
the lower one; ``_find_reminder_marker_span`` returned only the FIRST pair, so
neither emitter could ever see the other's copy.  Self-sustaining: it survived
every subsequent install.

The fix
-------
1. The template stops carrying the block (single owner = the splice).
2. The splice COLLAPSES a pre-existing multi-pair state — first pair replaced
   in place, every later pair removed — so installs already in the doubled
   state are repaired rather than merely frozen.
3. The strip removes EVERY pair, so an already-damaged install whose ledger
   has emptied ends at zero blocks instead of one orphaned static claim.

Both halves matter for a different user: (2) repairs someone whose ledger is
live, (3) repairs someone whose ledger has been resolved.  Without them an
update that only stops NEW duplication leaves existing users broken forever,
because nothing else in the system would ever remove the second copy.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.deferral_report import (  # noqa: E402
    _REMINDER_BEGIN,
    _REMINDER_END,
    DeferralEntry,
    DeferralReport,
    _find_all_reminder_marker_spans,
    _find_reminder_marker_span,
    _splice_reminder_into_claude_md,
    _strip_reminder_from_claude_md,
)

ORCH_TEMPLATE = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"

# The block the template used to carry (verbatim, v0.2.50 Track A through
# v0.2.91).  Kept here so the historical-replay tests below stay meaningful
# forever — they must not depend on the shipped template still containing it
# (it must not).
LEGACY_TEMPLATE_BLOCK = (
    f"{_REMINDER_BEGIN}\n"
    "**Pending VCO action**: `.claude/context/UPDATE_DEFERRED.md` exists.\n"
    "Read it at session start — it contains commands to resolve\n"
    "unresolved VCO install actions.\n"
    "\n"
    "To remove THIS reminder block: once the deferral is resolved (e.g.\n"
    "via `--update --force`), VCO's next install run will delete\n"
    "UPDATE_DEFERRED.md AND strip this block. Manual cleanup if needed:\n"
    "delete everything between the HTML-comment markers wrapping this\n"
    "block.\n"
    f"{_REMINDER_END}\n"
)


def _entry(condition_id: str = "v0292_reminder_probe") -> DeferralEntry:
    return DeferralEntry(
        condition_id=condition_id,
        title="Probe condition",
        detected="Something was detected.",
        why_deferred="Cannot auto-fix.",
        command_to_apply="python install.py --update --force",
        severity="warning",
        kg_node_refs=[],
        detected_at="2026-09-01T12:00:00Z",
    )


def _pairs(text: str) -> int:
    """Count REAL (unfenced, line-start) begin/end pairs."""
    return len(_find_all_reminder_marker_spans(text)[0])


# ---------------------------------------------------------------------------
# Group T — the template is no longer an emitter
# ---------------------------------------------------------------------------

class TestTemplateIsNotAnEmitter(unittest.TestCase):
    """RED before the fix: the shipped template carried the block at line 2."""

    def test_template_emits_no_reminder_block(self):
        body = ORCH_TEMPLATE.read_text(encoding="utf-8")
        spans, dangling = _find_all_reminder_marker_spans(body)
        self.assertEqual(
            (len(spans), dangling), (0, False),
            "templates/ORCHESTRATOR-CLAUDE.md.template must NOT carry a "
            "deferral-reminder block: vco_lib/deferral_report.py is its single "
            "owner (only that emitter knows whether a ledger exists and what "
            "its live counts are). A template copy is unconditional and, once "
            "a splice-owned copy lands outside the AUTO region, doubles the "
            "block permanently.",
        )

    def test_template_still_opens_with_the_auto_marker(self):
        """Pin (green both ways): removing the block must not disturb the
        AUTO region contract install.py merges against."""
        first = ORCH_TEMPLATE.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(
            first.startswith("<!-- BEGIN: AUTO"),
            f"template must still open with the AUTO marker, got {first!r}",
        )


# ---------------------------------------------------------------------------
# Group S — splice: insertion, refresh, collapse
# ---------------------------------------------------------------------------

class TestSpliceInsertAndRefresh(unittest.TestCase):
    """Pins (green both ways) — the behaviour the collapse must not break."""

    def test_no_block_no_frontmatter_inserts_exactly_one_at_top(self):
        existing = "# My Project\n\nBody.\n"
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        self.assertTrue(out.startswith(_REMINDER_BEGIN))
        self.assertIn("# My Project", out)
        self.assertIn("Body.", out)

    def test_no_block_with_frontmatter_inserts_after_the_fence(self):
        existing = "---\ntitle: X\n---\n\n# Heading\n"
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        self.assertTrue(out.startswith("---\ntitle: X\n---\n"))
        self.assertLess(out.find(_REMINDER_BEGIN), out.find("# Heading"))

    def test_single_block_is_refreshed_in_place(self):
        head = "# Project\n\nIntro paragraph.\n\n"
        stale = f"{_REMINDER_BEGIN}\nSTALE BODY FROM AN OLDER VCO\n{_REMINDER_END}\n"
        tail = "\n## Later section\n"
        existing = head + stale + tail
        out = _splice_reminder_into_claude_md(existing, [_entry()])

        self.assertEqual(_pairs(out), 1)
        # Position preserved (the docstring's stated intent).
        self.assertEqual(out.find(_REMINDER_BEGIN), existing.find(_REMINDER_BEGIN))
        # Content refreshed.
        self.assertNotIn("STALE BODY FROM AN OLDER VCO", out)
        self.assertIn("Pending VCO action", out)
        # Neighbours untouched.
        self.assertIn("Intro paragraph.", out)
        self.assertIn("## Later section", out)


class TestSpliceCollapsesDamagedState(unittest.TestCase):
    """RED before the fix: the splice refreshed the first pair and left the
    rest, so a doubled CLAUDE.md stayed doubled forever."""

    def _damaged(self, copies: int) -> str:
        """N reminder blocks with the user's own prose between them.

        Sentinels are deliberately non-overlapping (``BLOCKBODY-i`` vs
        ``USERPROSE-i``) so "the block body is gone" and "the user's text
        survived" cannot be confused by a substring match.
        """
        parts = ["# Project\n\n"]
        for i in range(copies):
            parts.append(
                f"{_REMINDER_BEGIN}\nBLOCKBODY-{i}\n{_REMINDER_END}\n\n"
                f"USERPROSE-{i}\n\n"
            )
        parts.append("## Tail section\n")
        return "".join(parts)

    def test_two_blocks_collapse_to_exactly_one(self):
        existing = self._damaged(2)
        self.assertEqual(_pairs(existing), 2)  # fixture sanity
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        self.assertEqual(out.count(_REMINDER_BEGIN), 1)
        self.assertEqual(out.count(_REMINDER_END), 1)

    def test_collapse_keeps_the_first_copy_at_its_original_offset(self):
        """The survivor is the FIRST block, at its original offset.

        Position-preserving (this function's own promise) and
        marker-agnostic: choosing "the copy outside the AUTO region" would
        need this generic splicer to know install.py's AUTO fences AND
        project_init's VCO_MANAGED fences — two conventions from two layers.
        """
        existing = self._damaged(2)
        first_at = existing.find(_REMINDER_BEGIN)
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(out.find(_REMINDER_BEGIN), first_at)
        # The survivor is the refreshed block, not a stale copy.
        self.assertNotIn("BLOCKBODY-0", out)
        self.assertNotIn("BLOCKBODY-1", out)
        self.assertIn("Pending VCO action", out)

    def test_collapse_preserves_user_content_between_the_copies(self):
        existing = self._damaged(2)
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertIn("USERPROSE-0", out)
        self.assertIn("USERPROSE-1", out)
        self.assertIn("# Project", out)
        self.assertIn("## Tail section", out)

    def test_three_blocks_also_collapse_to_one(self):
        """Not special-cased at two: N copies collapse to one."""
        existing = self._damaged(3)
        self.assertEqual(_pairs(existing), 3)
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        for i in range(3):
            self.assertIn(f"USERPROSE-{i}", out)
            self.assertNotIn(f"BLOCKBODY-{i}", out)

    def test_collapse_is_idempotent(self):
        once = _splice_reminder_into_claude_md(self._damaged(2), [_entry()])
        twice = _splice_reminder_into_claude_md(once, [_entry()])
        self.assertEqual(once, twice)

    def test_fenced_quoted_markers_are_never_collapsed(self):
        """A CLAUDE.md that DOCUMENTS the markers inside a fence keeps its
        example; only the two real copies collapse."""
        existing = (
            "# Project\n\n"
            "```\n"
            f"{_REMINDER_BEGIN}\n"
            "documented example\n"
            f"{_REMINDER_END}\n"
            "```\n\n"
            f"{_REMINDER_BEGIN}\nREAL ONE\n{_REMINDER_END}\n\n"
            "MIDDLE\n\n"
            f"{_REMINDER_BEGIN}\nREAL TWO\n{_REMINDER_END}\n\n"
            "END\n"
        )
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        self.assertIn("documented example", out)
        self.assertIn("MIDDLE", out)
        self.assertIn("END", out)
        # The fenced quote's literal markers survive verbatim.
        self.assertEqual(out.count(_REMINDER_BEGIN), 2)  # fenced + real


class TestMalformedMarkersStillRefused(unittest.TestCase):
    """Pins (green both ways): the A-4 ambiguity refusal is unchanged.

    Deleting content we cannot parse is worse than skipping a refresh, so a
    dangling begin SUPPRESSES the collapse rather than triggering it.
    """

    def test_orphan_begin_only_splices_nothing(self):
        existing = f"{_REMINDER_BEGIN}\nUSER CONTENT AFTER AN ORPHAN BEGIN\n"
        self.assertEqual(
            _splice_reminder_into_claude_md(existing, [_entry()]), existing,
        )

    def test_orphan_begin_only_strips_nothing(self):
        existing = f"{_REMINDER_BEGIN}\nUSER CONTENT AFTER AN ORPHAN BEGIN\n"
        self.assertEqual(_strip_reminder_from_claude_md(existing), existing)

    def test_orphan_end_alone_is_ignored(self):
        existing = f"# Project\n\n{_REMINDER_END}\n\nBody.\n"
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(_pairs(out), 1)
        self.assertTrue(out.startswith(_REMINDER_BEGIN))
        self.assertIn("Body.", out)

    def test_dangling_begin_after_two_pairs_suppresses_the_collapse(self):
        """Two real pairs PLUS a stray begin: refresh the first, mangle
        nothing. Same as the pre-v0.2.92 behaviour — strictly no worse."""
        existing = (
            f"{_REMINDER_BEGIN}\nONE\n{_REMINDER_END}\n\n"
            "USER A\n\n"
            f"{_REMINDER_BEGIN}\nTWO\n{_REMINDER_END}\n\n"
            "USER B\n\n"
            f"{_REMINDER_BEGIN}\n"
            "USER TEXT UNDER A STRAY BEGIN\n"
        )
        out = _splice_reminder_into_claude_md(existing, [_entry()])
        self.assertEqual(
            _pairs(out), 2, "extras must NOT be collapsed while ambiguous",
        )
        self.assertIn("USER A", out)
        self.assertIn("USER B", out)
        self.assertIn("USER TEXT UNDER A STRAY BEGIN", out)
        self.assertIn("Pending VCO action", out)

    def test_dangling_begin_after_two_pairs_suppresses_the_strip(self):
        existing = (
            f"{_REMINDER_BEGIN}\nONE\n{_REMINDER_END}\n\n"
            "USER A\n\n"
            f"{_REMINDER_BEGIN}\nTWO\n{_REMINDER_END}\n\n"
            f"{_REMINDER_BEGIN}\nSTRAY TAIL\n"
        )
        out = _strip_reminder_from_claude_md(existing)
        self.assertEqual(
            _pairs(out), 1, "only the first block goes while ambiguous",
        )
        self.assertIn("USER A", out)
        self.assertIn("STRAY TAIL", out)

    def test_single_span_locator_contract_is_unchanged(self):
        """Pin: the back-compat locator still answers first-pair /
        ambiguous / None exactly as it did before it became a delegate."""
        pair = f"{_REMINDER_BEGIN}\nX\n{_REMINDER_END}\n"

        self.assertIsNone(_find_reminder_marker_span("# nothing here\n"))
        self.assertEqual(
            _find_reminder_marker_span(f"{_REMINDER_BEGIN}\ndangling\n"),
            ("ambiguous",),
        )

        two = f"A\n{pair}B\n{pair}C\n"
        span = _find_reminder_marker_span(two)
        self.assertIsInstance(span, tuple)
        self.assertEqual(span[0], two.find(_REMINDER_BEGIN))

        # A stray begin AFTER a complete pair stays invisible (early-return
        # semantics of the pre-v0.2.92 locator).
        self.assertEqual(
            _find_reminder_marker_span(f"{pair}{_REMINDER_BEGIN}\ntail\n")[0], 0,
        )


# ---------------------------------------------------------------------------
# Group R — strip removes EVERY copy
# ---------------------------------------------------------------------------

class TestStripRemovesEveryCopy(unittest.TestCase):

    def test_strip_single_block_restores_byte_for_byte(self):
        """Pin (green both ways): the round-trip contract."""
        original = "# Project 🚀\n\nBody with café and 中文.\n"
        spliced = _splice_reminder_into_claude_md(original, [_entry()])
        self.assertEqual(_strip_reminder_from_claude_md(spliced), original)

    def test_strip_removes_all_copies(self):
        """RED before the fix: only the first copy was removed."""
        existing = (
            "# Project\n\n"
            f"{_REMINDER_BEGIN}\nONE\n{_REMINDER_END}\n\n"
            "USER A\n\n"
            f"{_REMINDER_BEGIN}\nTWO\n{_REMINDER_END}\n\n"
            "USER B\n"
        )
        out = _strip_reminder_from_claude_md(existing)
        self.assertEqual(_pairs(out), 0)
        self.assertNotIn(_REMINDER_BEGIN, out)
        self.assertNotIn(_REMINDER_END, out)
        self.assertIn("USER A", out)
        self.assertIn("USER B", out)


class TestSeparatorArithmetic(unittest.TestCase):
    """v0.2.92: removal takes back ONE blank line, not two.

    The collapse path removes blocks this module did NOT insert (the ones the
    template used to render), so the removal rule can no longer assume the
    splicer's own "blank line on each side" shape.  Taking one back per side
    removed a separator nobody added.

    Perfect fidelity is unreachable — a frontmatter file WITH a blank line
    after its closing fence and one WITHOUT splice to identical bytes — so
    these tests pin WHICH side of that trade is taken.
    """

    def test_frontmatter_round_trip_is_lossless(self):
        """RED before the fix: the blank line after the closing fence was
        eaten on every strip."""
        original = "---\ntitle: X\n---\n\n# Heading\ncontent\n"
        spliced = _splice_reminder_into_claude_md(original, [_entry()])
        self.assertEqual(_strip_reminder_from_claude_md(spliced), original)

    def test_frontmatter_without_a_blank_line_gains_one(self):
        """The other half of the trade, pinned so it cannot flip silently.

        Case 2 splices the same bytes whether or not the source had a blank
        line after the fence, so the strip has to pick one shape to restore.
        It picks the blank-line shape; a source that had none gets one back.
        Cosmetic — it renders identically.
        """
        original = "---\ntitle: X\n---\n# Heading\n"
        spliced = _splice_reminder_into_claude_md(original, [_entry()])
        self.assertEqual(
            _strip_reminder_from_claude_md(spliced),
            "---\ntitle: X\n---\n\n# Heading\n",
        )

    def test_removal_keeps_the_user_paragraph_break(self):
        """RED before the fix: the two paragraphs were merged."""
        existing = f"para one\n\n{_REMINDER_BEGIN}\nBODY\n{_REMINDER_END}\n\npara two\n"
        self.assertEqual(
            _strip_reminder_from_claude_md(existing), "para one\n\npara two\n",
        )

    def test_top_of_file_round_trip_is_still_lossless(self):
        """Pin (green both ways): the case-3 contract the shipped suite
        already relies on."""
        original = "# Project\n\nbody text\n"
        spliced = _splice_reminder_into_claude_md(original, [_entry()])
        self.assertEqual(_strip_reminder_from_claude_md(spliced), original)


class TestLedgerLifecycleOnDisk(unittest.TestCase):
    """End-to-end through ``DeferralReport.write`` — the surface install.py
    and every detached emitter actually call."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0292-reminder-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _claude_md(self) -> Path:
        return self.tmp / "CLAUDE.md"

    def test_no_block_and_no_ledger_yields_zero_blocks(self):
        """The leave-alone case: nothing pending ⇒ nothing claimed, no crash."""
        original = "# Project\n\nJust the user's own instructions.\n"
        self._claude_md().write_text(original, encoding="utf-8")

        report = DeferralReport()
        self.assertFalse(report.write(self.tmp))

        body = self._claude_md().read_text(encoding="utf-8")
        self.assertEqual(_pairs(body), 0)
        self.assertNotIn("Pending VCO action", body)
        self.assertEqual(body, original, "an empty ledger must not touch CLAUDE.md")

    def test_ledger_present_yields_exactly_one_block(self):
        self._claude_md().write_text("# Project\n\nBody.\n", encoding="utf-8")
        report = DeferralReport()
        report.add_entry(_entry())
        self.assertTrue(report.write(self.tmp))
        body = self._claude_md().read_text(encoding="utf-8")
        self.assertEqual(_pairs(body), 1)

    def test_damaged_file_with_live_ledger_collapses_to_one(self):
        """RED before the fix: stayed at two."""
        self._claude_md().write_text(
            "# Project\n\n"
            f"{_REMINDER_BEGIN}\nOLD SPLICE COPY\n{_REMINDER_END}\n\n"
            "USER CONTENT\n\n"
            f"{LEGACY_TEMPLATE_BLOCK}\n"
            "MORE USER CONTENT\n",
            encoding="utf-8",
        )
        report = DeferralReport()
        report.add_entry(_entry())
        report.write(self.tmp)

        body = self._claude_md().read_text(encoding="utf-8")
        self.assertEqual(_pairs(body), 1)
        self.assertIn("USER CONTENT", body)
        self.assertIn("MORE USER CONTENT", body)

    def test_damaged_file_with_resolved_ledger_collapses_to_zero(self):
        """RED before the fix: one static copy survived and kept claiming a
        pending action on a project with no ledger."""
        self._claude_md().write_text(
            "# Project\n\n"
            f"{_REMINDER_BEGIN}\nOLD SPLICE COPY\n{_REMINDER_END}\n\n"
            "USER CONTENT\n\n"
            f"{LEGACY_TEMPLATE_BLOCK}\n"
            "MORE USER CONTENT\n",
            encoding="utf-8",
        )
        self.assertFalse(DeferralReport().write(self.tmp))

        body = self._claude_md().read_text(encoding="utf-8")
        self.assertEqual(_pairs(body), 0)
        self.assertNotIn("Pending VCO action", body)
        self.assertIn("USER CONTENT", body)
        self.assertIn("MORE USER CONTENT", body)

    def test_missing_claude_md_never_crashes(self):
        report = DeferralReport()
        report.add_entry(_entry())
        self.assertTrue(report.write(self.tmp))
        self.assertFalse(self._claude_md().exists())
        self.assertFalse(DeferralReport().write(self.tmp))


# ---------------------------------------------------------------------------
# Group I — the two emitters, run in sequence, against the real renderer
# ---------------------------------------------------------------------------

class TestRenderAndSpliceInterplay(unittest.TestCase):
    """The regression that would have caught the original bug: run the REAL
    AUTO-region renderer (``install._materialize_orchestrator_self_claude_md``)
    and the REAL splice in the order an install runs them, twice."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vct-v0292-render-"))
        (self.tmp / "templates").mkdir(parents=True)
        self.template = self.tmp / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
        shutil.copy2(ORCH_TEMPLATE, self.template)
        import install  # noqa: E402,PLC0415 — heavy module, imported lazily

        self.install = install

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self) -> None:
        """Run the real step-4c renderer, silently."""
        with mock.patch.object(
            self.install, "_log_install_event", lambda *a, **k: None,
        ), contextlib.redirect_stdout(io.StringIO()):
            self.install._materialize_orchestrator_self_claude_md(self.tmp)

    def _splice(self) -> None:
        """Run the real end-of-run deferral write (non-empty ledger)."""
        report = DeferralReport()
        report.add_entry(_entry())
        report.write(self.tmp)

    def _strip(self) -> None:
        """Run the real end-of-run deferral write with an EMPTY ledger."""
        DeferralReport().write(self.tmp)

    def _body(self) -> str:
        return (self.tmp / "CLAUDE.md").read_text(encoding="utf-8")

    def _install_old_template(self) -> None:
        """Restore the pre-v0.2.92 template (block at line 2)."""
        lines = ORCH_TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True)
        self.template.write_text(
            lines[0] + LEGACY_TEMPLATE_BLOCK + "".join(lines[1:]),
            encoding="utf-8",
        )

    # -- fresh install ---------------------------------------------------

    def test_fresh_render_makes_no_pending_claim(self):
        """RED before the fix: a clone that had never deferred anything got
        'Pending VCO action: UPDATE_DEFERRED.md exists' on day zero."""
        self._render()
        body = self._body()
        self.assertEqual(_pairs(body), 0)
        self.assertNotIn("Pending VCO action", body)

    def test_fresh_clone_placeholder_render_makes_no_pending_claim(self):
        """The REAL fresh-install path for someone cloning the public repo.

        The repo ships a tracked ``CLAUDE.md`` placeholder that has NO AUTO
        markers, so install.py takes its "full rewrite" branch rather than the
        "created" branch ``test_fresh_render_makes_no_pending_claim`` covers.
        RED before the fix: day-zero clone, no ledger, and the file still
        announced a pending VCO action.
        """
        (self.tmp / "CLAUDE.md").write_text(
            "# VibeCoded Orchestrator\n\n"
            "This file is auto-materialized by `install.py`.\n",
            encoding="utf-8",
        )
        self._render()
        body = self._body()
        self.assertEqual(_pairs(body), 0)
        self.assertNotIn("Pending VCO action", body)
        # The full-rewrite branch really did run (AUTO markers now present).
        self.assertIn("<!-- BEGIN: AUTO", body)

    def test_fresh_install_then_first_deferral_yields_one_block(self):
        self._render()
        self._splice()
        body = self._body()
        self.assertEqual(_pairs(body), 1)
        self.assertIn("Pending VCO action", body)
        # Above the AUTO region ⇒ the next render cannot wipe it.
        self.assertLess(body.find(_REMINDER_BEGIN), body.find("<!-- BEGIN: AUTO"))

    def test_render_then_splice_twice_is_byte_stable(self):
        """Pin (green both ways): no growth, no drift across two full install
        cycles.

        This one does NOT catch the original bug on its own — pre-fix, two
        clean cycles are also stable, because the template's copy is present
        from the first render and the splice just refreshes it in place. The
        duplicate needed the ledger to EMPTY first; that is
        ``test_resolve_then_emit_then_render_does_not_duplicate`` below, which
        is the test that would actually have caught it.
        """
        self._render()
        self._splice()
        after_one = self._body()

        self._render()
        self._splice()
        after_two = self._body()

        self.assertEqual(after_one, after_two)
        self.assertEqual(_pairs(after_two), 1)

    def test_resolve_then_emit_then_render_does_not_duplicate(self):
        """The exact sequence that MINTED the duplicate.

        resolve (strip → zero blocks) → a later emit splices above the AUTO
        region → the next render re-adds the template's copy inside it.
        RED before the fix: two blocks, permanently.
        """
        self._render()
        self._splice()
        self._strip()
        self.assertEqual(_pairs(self._body()), 0)

        self._splice()          # detached emitter, before the next render
        self._render()          # next install's step 4c
        self._splice()          # that install's finalize

        self.assertEqual(_pairs(self._body()), 1)

    # -- already-damaged install ----------------------------------------

    def test_historical_replay_produces_the_damaged_state(self):
        """Fixture proof: with the OLD template in place the sequence above
        really does mint a second copy. (Green both ways by construction —
        it exercises the legacy template, not the shipped one.)"""
        self._install_old_template()
        self._render()
        self._splice()
        self._strip()
        self._splice()
        self._render()
        self.assertEqual(
            _pairs(self._body()), 2,
            "historical replay must reproduce the doubled state",
        )

    def test_damaged_install_is_repaired_by_the_next_update(self):
        """The already-damaged axis: a user whose CLAUDE.md ALREADY carries
        two blocks, whose ledger is still live. RED before the fix."""
        self._install_old_template()
        self._render()
        self._splice()
        self._strip()
        self._splice()
        self._render()
        damaged = self._body()
        self.assertEqual(_pairs(damaged), 2)
        # User content added outside the AUTO region must survive the repair.
        (self.tmp / "CLAUDE.md").write_text(
            damaged + "\n## MY OWN SECTION\nhand-written notes\n",
            encoding="utf-8",
        )

        # The update: shipped template + real renderer, then the finalize.
        shutil.copy2(ORCH_TEMPLATE, self.template)
        self._render()
        self._splice()

        repaired = self._body()
        self.assertEqual(_pairs(repaired), 1)
        self.assertIn("## MY OWN SECTION", repaired)
        self.assertIn("hand-written notes", repaired)
        self.assertLess(
            repaired.find(_REMINDER_BEGIN), repaired.find("<!-- BEGIN: AUTO"),
        )

    def test_damaged_install_is_repaired_by_the_splice_alone(self):
        """Same damage, repaired WITHOUT a render — the path a detached
        emitter (codegraph resync, embedding-failure) takes between
        installs. RED before the fix."""
        self._install_old_template()
        self._render()
        self._splice()
        self._strip()
        self._splice()
        self._render()
        self.assertEqual(_pairs(self._body()), 2)

        self._splice()
        self.assertEqual(_pairs(self._body()), 1)

    def test_damaged_install_with_resolved_ledger_ends_at_zero(self):
        """Same damage, but the user's ledger is empty by then. RED before
        the fix: the template copy survived the strip and kept claiming a
        pending action forever."""
        self._install_old_template()
        self._render()
        self._splice()
        self._strip()
        self._splice()
        self._render()
        self.assertEqual(_pairs(self._body()), 2)

        shutil.copy2(ORCH_TEMPLATE, self.template)
        self._render()
        self._strip()

        body = self._body()
        self.assertEqual(_pairs(body), 0)
        self.assertNotIn("Pending VCO action", body)

    def test_repaired_file_matches_a_freshly_rendered_one(self):
        """After repair, a damaged install's file is byte-identical to a
        clean install's — no residue of the second copy."""
        self._install_old_template()
        self._render()
        self._splice()
        self._strip()
        self._splice()
        self._render()
        shutil.copy2(ORCH_TEMPLATE, self.template)
        self._render()
        self._splice()
        repaired = self._body()

        clean_dir = Path(tempfile.mkdtemp(prefix="vct-v0292-clean-"))
        try:
            (clean_dir / "templates").mkdir(parents=True)
            shutil.copy2(
                ORCH_TEMPLATE,
                clean_dir / "templates" / "ORCHESTRATOR-CLAUDE.md.template",
            )
            with mock.patch.object(
                self.install, "_log_install_event", lambda *a, **k: None,
            ), contextlib.redirect_stdout(io.StringIO()):
                self.install._materialize_orchestrator_self_claude_md(clean_dir)
            report = DeferralReport()
            report.add_entry(_entry())
            report.write(clean_dir)
            clean = (clean_dir / "CLAUDE.md").read_text(encoding="utf-8")
        finally:
            shutil.rmtree(clean_dir, ignore_errors=True)

        # The template interpolates {{ORCHESTRATOR_ROOT}}, so the two files
        # legitimately differ by their install root. Normalise that away —
        # everything else must match byte for byte.
        marker = "<<INSTALL_ROOT>>"
        self.assertEqual(
            repaired.replace(str(self.tmp), marker),
            clean.replace(str(clean_dir), marker),
        )


if __name__ == "__main__":
    unittest.main()
