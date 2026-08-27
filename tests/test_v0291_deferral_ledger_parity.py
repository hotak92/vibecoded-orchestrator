# SPDX-License-Identifier: AGPL-3.0-or-later
# Part of VibeCoded Orchestrator.
"""v0.2.91 WP-I — cross-language pins for the deferral-ledger GUI backend.

``launcher/src-tauri/src/commands/deferral_ledger.rs`` renders the ledger from
the JSON sidecar and the retry trail, both of which Python writes. Three facts
have to agree across the two languages and CANNOT be resolved at run time,
because the launcher is the REPAIR tool — it must render this panel on an
install whose venv is broken, which is exactly when a user needs it:

1. the sidecar ``schema_version`` the reader accepts;
2. the retry attempt CAP (the jsonl trail does not carry it, so the panel has
   to know the number to say "VCO has stopped trying");
3. the retry status vocabulary — in particular that ``inconclusive`` is its own
   state and not a synonym for ``failed``.

That makes them tier-(C) mirrors under the A>B>C rule, and a tier-C mirror
without a parity test is just a divergence waiting for its first release. This
file is that test: it SOURCE-SCANS the Rust constants and compares them against
the live Python values, so a change on either side that forgets the other fails
CI rather than shipping a panel that quietly disagrees with the driver.

Also pinned: the two on-disk paths the reader hard-codes, and the disposition
PARTITION (``action_required`` + ``auto_retryable`` = actionable) that the
panel's "Action needed" GROUP depends on — the promise in
``DeferralReport.split_by_disposition``'s docstring is that the GUI ledger and
the CLAUDE.md reminder can never disagree, and this is what holds the Rust half
of it.

Fourth mirror (user decision, 2026-08-27): the MenuBar BADGE counts
``action_required`` ONLY, while the group keeps ``auto_retryable``. That splits
one number into two across THREE languages — Rust computes
``action_required_count``, TypeScript's ``badgeCount`` derives it FE-side, and
Python owns the class name — so it gets the same source-scan treatment.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_RS = (
    REPO_ROOT
    / "launcher"
    / "src-tauri"
    / "src"
    / "commands"
    / "deferral_ledger.rs"
)
LEDGER_TS = REPO_ROOT / "launcher" / "src" / "lib" / "deferral-ledger.ts"


def _rust_source() -> str:
    return LEDGER_RS.read_text(encoding="utf-8")


def _ts_declaration(name: str, source: str) -> str:
    """An `export function <name>` declaration + body.

    Delimited by the NEXT top-level declaration or doc-comment rather than by
    brace matching: these functions carry multi-line object return types, so the
    first `{` after the parameter list is not the body's.
    """
    start = source.find(f"export function {name}(")
    assert start != -1, (
        f"{name}() was not found in {LEDGER_TS} — if it was renamed, repoint "
        "this pin in the same commit rather than deleting it"
    )
    rest = source[start + 1 :]
    end = re.search(r"\n(?:export |/\*\*)", rest)
    stop = end.start() if end else len(rest)
    return source[start : start + 1 + stop]


def _const_u(name: str, source: str) -> int:
    """Value of a `pub(crate) const <name>: uN = <int>;` declaration."""
    m = re.search(
        rf"const\s+{re.escape(name)}\s*:\s*u\d+\s*=\s*(\d+)\s*;", source
    )
    assert m is not None, f"{name} not found in {LEDGER_RS}"
    return int(m.group(1))


def _const_str(name: str, source: str) -> str:
    """Value of a `pub(crate) const <name>: &str = "...";` declaration."""
    m = re.search(
        rf'const\s+{re.escape(name)}\s*:\s*&str\s*=\s*"([^"]*)"\s*;', source
    )
    assert m is not None, f"{name} not found in {LEDGER_RS}"
    return m.group(1)


class DeferralLedgerParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(
            LEDGER_RS.is_file(),
            f"the WP-I ledger command module is missing at {LEDGER_RS}",
        )
        self.src = _rust_source()

    def test_sidecar_schema_version_matches_python(self) -> None:
        from vco_lib.deferral_report import _JSON_SCHEMA_VERSION

        self.assertEqual(
            _const_u("SIDECAR_SCHEMA_VERSION", self.src),
            _JSON_SCHEMA_VERSION,
            "the ledger reader accepts a different sidecar schema than the "
            "writer produces — bump both in the same commit",
        )

    def test_retry_cap_matches_python(self) -> None:
        from vco_lib.deferral_retry import MAX_ATTEMPTS

        self.assertEqual(
            _const_u("RETRY_MAX_ATTEMPTS", self.src),
            MAX_ATTEMPTS,
            "the panel would show the wrong 'VCO has stopped trying' threshold",
        )

    def test_retry_status_vocabulary_matches_python(self) -> None:
        from vco_lib import deferral_retry as dr

        pairs = {
            "RETRY_STATUS_STARTED": dr.STARTED,
            "RETRY_STATUS_RETRIED": dr.RETRIED,
            "RETRY_STATUS_FAILED": dr.FAILED,
            "RETRY_STATUS_INCONCLUSIVE": dr.INCONCLUSIVE,
            "RETRY_STATUS_SKIPPED": dr.SKIPPED,
        }
        for rust_name, py_value in pairs.items():
            with self.subTest(status=rust_name):
                self.assertEqual(_const_str(rust_name, self.src), py_value)

    def test_inconclusive_is_a_distinct_state_on_both_sides(self) -> None:
        """The honest verdict the driver refuses to collapse.

        ``inconclusive`` means the handler RAN, exited 0, and its condition is
        still in the ledger. Rendering it as ``failed`` would claim knowledge
        the driver explicitly declined to claim.
        """
        from vco_lib import deferral_retry as dr

        self.assertNotEqual(dr.INCONCLUSIVE, dr.FAILED)
        self.assertNotEqual(
            _const_str("RETRY_STATUS_INCONCLUSIVE", self.src),
            _const_str("RETRY_STATUS_FAILED", self.src),
        )
        # And the Rust reader must count them into SEPARATE fields.
        self.assertIn("out.inconclusive = out.inconclusive", self.src)
        self.assertIn("out.failed = out.failed", self.src)

    def test_reader_paths_match_the_python_writers(self) -> None:
        from vco_lib.deferral_report import _DEFERRED_JSON_REL
        from vco_lib.deferral_retry import ATTEMPTS_FILENAME, attempts_path

        # Sidecar: `.claude/context/UPDATE_DEFERRED.json`
        sidecar_parts = list(_DEFERRED_JSON_REL.parts)
        self.assertEqual(sidecar_parts, [".claude", "context", "UPDATE_DEFERRED.json"])
        for part in sidecar_parts:
            self.assertIn(
                f'"{part}"',
                self.src,
                f"the Rust sidecar path is missing the {part!r} segment",
            )

        # Retry trail: `.claude/logs/deferral-retries.jsonl`
        trail_parts = attempts_path(Path("/x")).relative_to(Path("/x")).parts
        self.assertEqual(trail_parts, (".claude", "logs", ATTEMPTS_FILENAME))
        self.assertIn(f'"{ATTEMPTS_FILENAME}"', self.src)
        self.assertIn('"logs"', self.src)

    def test_actionable_partition_matches_split_by_disposition(self) -> None:
        """The Rust partition literal is the same pair Python splits on."""
        from vco_lib.deferral_registry import registered_patterns, split_by_disposition

        m = re.search(
            r"matches!\(disposition,\s*\"([a-z_]+)\"\s*\|\s*\"([a-z_]+)\"\s*\)",
            self.src,
        )
        assert m is not None, (
            "is_actionable_disposition's partition literal was not found in "
            f"{LEDGER_RS} — if the shape changed, update this pin in the same "
            "commit rather than deleting it"
        )
        rust_pair = {m.group(1), m.group(2)}

        # Derive Python's pair empirically from the shipped table rather than
        # re-asserting the literal: whatever `split_by_disposition` actually
        # buckets as actionable IS the contract.
        cids = [p.replace("*", "x") for p in registered_patterns()]
        actionable, _ = split_by_disposition(cids)
        from vco_lib.deferral_registry import disposition_for

        py_pair = {disposition_for(c) for c in actionable}
        self.assertEqual(
            rust_pair,
            py_pair,
            "the GUI badge would count a different set than the CLAUDE.md "
            "reminder — the one thing split_by_disposition promises it cannot",
        )

    def test_badge_counts_action_required_only_on_both_sides(self) -> None:
        """USER DECISION (2026-08-27): the badge is NARROWER than the group.

        ``auto_retryable`` is work VCO retries by itself; badging it nags the
        user about something already in hand. It stays IN the "Action needed"
        group — the panel is the honest inventory — but it does not badge.

        Three surfaces have to agree on that or the MenuBar and the panel start
        contradicting each other in front of the user, so all three are scanned:
        Rust's wire count, TypeScript's FE-derived count, and the class name
        Python owns.
        """
        from vco_lib.deferral_registry import DEFAULT_CLASS

        badge_class = "action_required"
        self.assertEqual(
            DEFAULT_CLASS,
            badge_class,
            "the conservative default and the badge class are the same tier; if "
            "one moved, this pin needs rewriting, not deleting",
        )

        # ── Rust: a badge-specific count keyed on a STRICT equality ──────
        self.assertIn(
            "action_required_count",
            self.src,
            "the Rust view must expose the badge count as its own field, "
            "distinct from actionable_count (the group size)",
        )
        m = re.search(
            r"fn is_badge_disposition\(disposition: &str\) -> bool \{\s*"
            r'disposition == "([a-z_]+)"',
            self.src,
        )
        assert m is not None, (
            "is_badge_disposition was not found as a strict equality in "
            f"{LEDGER_RS} — a two-arm `matches!` there would both widen the "
            "badge and give the partition scan above a decoy to match"
        )
        self.assertEqual(m.group(1), badge_class)

        # ── TypeScript: badgeCount filters on the same class ─────────────
        ts = LEDGER_TS.read_text(encoding="utf-8")
        badge_body = _ts_declaration("badgeCount", ts)
        self.assertIn(
            badge_class,
            badge_body,
            "badgeCount must filter on the disposition class, not on the "
            "actionable partition",
        )
        self.assertNotIn(
            "actionNeeded",
            badge_body,
            "badgeCount must NOT delegate to groupEntries().actionNeeded — that "
            "is the group, which deliberately includes auto_retryable",
        )

        # ── …and the GROUP still holds auto_retryable (the other half of
        #    the decision: unbadged, but never hidden) ─────────────────────
        group_body = _ts_declaration("groupEntries", ts)
        self.assertIn(
            "e.actionable",
            group_body,
            "groupEntries must keep splitting on the actionable partition; "
            "narrowing it too would HIDE auto_retryable entries instead of "
            "just unbadging them",
        )
        self.assertNotIn(
            badge_class,
            group_body,
            "groupEntries must not key on the BADGE class — that is the "
            "over-correction: auto_retryable would vanish from the panel "
            "instead of merely not badging",
        )

    def test_reader_never_parses_the_markdown(self) -> None:
        """The .md is a lossy human render; the sidecar is the SSOT.

        A Rust markdown parser here would silently corrupt exactly the
        multi-line `command_to_apply` blocks the panel exists to display.
        """
        self.assertNotIn("UPDATE_DEFERRED.md", self.src)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
