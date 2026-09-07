# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Deferral report writer for non-auto-resolvable update conditions.

When ``install.py --update`` encounters a condition it cannot safely fix
automatically (schema rebuild required, Weaviate unreachable after restart
attempt, compose-overlay ambiguity), it accumulates a ``DeferralEntry`` and
writes ``.claude/context/UPDATE_DEFERRED.md`` at the end of the run.

Claude Code reads this file on the next session start so the user and the
model know exactly what's pending and the exact command to apply each fix.

Self-cleaning contract
----------------------
- ``report.write(folder)`` → returns True and writes the file when entries
  are present; returns False and **deletes** the file when entries are empty.
- ``install.py --update --apply-deferred`` attempts to apply each pending
  entry, marks resolved ones, and re-writes (or deletes) the file.

Atomic-write guarantee
-----------------------
All writes go to a temp file in the same directory as the target, then
``os.replace()`` (POSIX-atomic, cross-OS on same filesystem).

Format
------
Structured Markdown with YAML frontmatter listing condition IDs + a
``## <condition_id> (<severity>)`` section per entry.  Human-readable by
design; the YAML frontmatter is machine-parseable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from vco_lib.atomic import atomic_write_text

# Relative path inside any managed project folder.
_DEFERRED_REL = Path(".claude") / "context" / "UPDATE_DEFERRED.md"

# A-3 (v0.2.73): machine-readable sidecar — the SOURCE OF TRUTH for the
# deferral report. The Markdown file (``_DEFERRED_REL``) is a HUMAN-READABLE
# RENDER of the same entries, kept byte-compatible with the Rust
# ``restart.rs::extract_section/strip_section`` parser (which edits the
# Markdown to clear the ``launcher_restart_required`` section on restart).
#
# WHY a JSON sidecar: the Markdown round-trip corrupted entries (multi-line
# fields truncated to their first line on read; a ``## fake (crit)`` line
# inside a field split one entry into three; a ``` line inside a command
# inverted the fence toggle). The single most common real deferral —
# ``bundle_user_modified_preserved`` — renders its preserved-files list on
# continuation lines of ``detected``; the Markdown parser dropped every line
# but the first, silently destroying the entry's actionable payload on the
# next read-merge-write by ANY emitter. JSON has no such ambiguity.
#
# RECONCILIATION with the Rust editor: ``restart.rs`` clears a section from
# the Markdown but does NOT know about the JSON sidecar. So on ``read`` we
# treat JSON as authoritative BUT drop an entry whose ``## <cid>`` header is
# absent from a co-present Markdown file — that means the Rust restart flow
# removed it. P2a (v0.2.75): the drop applies ONLY to the condition IDs the
# Rust side actually strips (``_RUST_STRIPPABLE_CONDITION_IDS``); for every
# other ID a missing Markdown header means the Markdown is STALE (e.g. the
# JSON write landed but the Markdown write died mid-run, leaving an older
# .md on disk) and the entry is KEPT — pre-P2a that crash window silently
# dropped every cid newly added that run on the next read.
_DEFERRED_JSON_REL = Path(".claude") / "context" / "UPDATE_DEFERRED.json"

# P2a (v0.2.75): the exact condition IDs the Rust launcher strips from the
# MARKDOWN ONLY (leaving the JSON sidecar untouched). The read()
# reconcile-drop below applies exclusively to these IDs.
#
# MUST MATCH launcher/src-tauri/src/commands/restart.rs
# ``clear_restart_deferral()`` — the single production
# ``strip_section(&content, "launcher_restart_required")`` call site. If
# restart.rs (or any other Rust editor) ever strips an additional section
# from the Markdown without touching the JSON, add its condition ID here,
# otherwise the strip is silently resurrected from the JSON on the next
# Python read. (Rust editors that delete the WHOLE .md must delete BOTH
# files — e.g. installer.rs::clear_update_resume_deferral_if_solo dual-
# unlinks the .md AND UPDATE_DEFERRED.json (v0.2.75 Part 3b, mirroring
# restart.rs's F2 sweep). They do not go through this reconcile arm: with
# no co-present Markdown, a surviving JSON would be taken verbatim and
# resurrect the cleared entry — hence the dual-file rule.)
_RUST_STRIPPABLE_CONDITION_IDS = frozenset({
    "launcher_restart_required",
})

# Sidecar schema version — bump when the JSON shape changes so old readers
# can detect/skip an incompatible sidecar and fall back to the Markdown.
_JSON_SCHEMA_VERSION = 1

# Sentinel lines that delimit YAML frontmatter.
_FM_OPEN = "---"
_FM_CLOSE = "---"

# Separator between entry sections in the body.
_SECTION_SEP = "---"

# Allowed severity values (ordered worst→best for max computation).
SEVERITY_ORDER = ("critical", "warning", "info")

# ---------------------------------------------------------------------------
# CLAUDE.md reminder block (item 2 / Gap 10, 2026-05-13)
#
# When a deferral is written, project_init.DeferralReport.write() also
# injects a wrapped reminder block into the project's CLAUDE.md so future
# Claude sessions opening the project see "go read UPDATE_DEFERRED.md"
# at session start. Block is removed when the deferral is unlinked.
#
# Marker pattern mirrors install.py's `<!-- vct-merge-pending -->` block
# (install.py:1035) — wrapped HTML comments are idempotent-rewrite-friendly
# and survive Markdown renderers (they don't show in the rendered output).
# ---------------------------------------------------------------------------

_CLAUDE_MD_REL = Path("CLAUDE.md")
_REMINDER_BEGIN = "<!-- vco-deferral-reminder-begin -->"
_REMINDER_END = "<!-- vco-deferral-reminder-end -->"

# ---------------------------------------------------------------------------
# v0.2.92 WP-15: the REST of the VCO-owned Markdown-region vocabulary.
#
# This module is the ONE home for "which HTML-comment markers does VCO inject
# into a project's CLAUDE.md?" — it already owned the deferral-reminder pair,
# and `strip_vco_owned_regions` below needs all three families in one place.
# `vco_lib.project_init` re-exports the managed-region pair under its
# historical names so existing importers are unaffected.
#
#   1. reminder pair   — spliced in by this module when a ledger exists.
#   2. AUTO pair       — install.py's `_materialize_orchestrator_self_claude_md`
#                        wraps the ORCHESTRATOR render so a re-render replaces
#                        only the block.  The BEGIN marker carries a
#                        parenthesised provenance note, so it is matched by
#                        PREFIX, not by equality.
#   3. managed pair    — project_init's `merge_managed_region` wraps the
#                        PROJECT render so a re-render replaces only the body.
#                        These two are single MARKER LINES around content that
#                        is itself meaningful, so only the lines are stripped.
# ---------------------------------------------------------------------------
MANAGED_REGION_OPEN = "<!-- >>>VCO_MANAGED>>> -->"
MANAGED_REGION_CLOSE = "<!-- <<<VCO_MANAGED<<< -->"
_AUTO_REGION_BEGIN_PREFIX = "<!-- BEGIN: AUTO"
_AUTO_REGION_END = "<!-- END: AUTO -->"

# Leading frontmatter detector: ``^---\n<body>\n---\n``. The trailing
# newline after the closing fence is captured so we can splice the
# reminder block after it cleanly.
_LEADING_FRONTMATTER_RE = re.compile(
    r"\A---\n.*?^---\n", re.DOTALL | re.MULTILINE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_disposition(entry: Any) -> str:
    """THE disposition rule for one entry: explicit → registry → action_required.

    Works on a real :class:`DeferralEntry` (via
    :attr:`~DeferralEntry.resolved_disposition`) AND on any duck-typed
    entry-shaped object that only carries ``condition_id`` — the doctor's
    injectable ``resolve_deferral_report`` seam produces exactly those, and
    before this existed the doctor could not use the entry-aware partition at
    all, which is why it stayed on the cid-only copy that ignored explicit
    dispositions. One rule, every entry shape.

    MUST MATCH ``deferral_ledger.rs::resolve_disposition`` (the launcher's
    compiled mirror; parity-locked by
    ``tests/test_v0291_deferral_ledger_parity.py`` +
    ``launcher/src-tauri/tests/deferral_registry_parity.rs``).
    """
    resolved = getattr(entry, "resolved_disposition", None)
    if isinstance(resolved, str) and resolved:
        return resolved
    explicit = _coerce_disposition(getattr(entry, "disposition", None))
    if explicit:
        return explicit
    try:
        from vco_lib.deferral_registry import disposition_for

        return disposition_for(getattr(entry, "condition_id", "") or "")
    except Exception:  # noqa: BLE001 — conservative on any registry problem
        return "action_required"


def partition_entries(report_or_entries: Any) -> tuple:
    """THE partition, for any entry-bearing thing. ``(actionable, informational)``.

    One function every surface calls, whatever it is holding: a
    :class:`DeferralReport`, a plain list of entries, or a duck-typed report a
    caller's injectable seam produced (the doctor's ``resolve_deferral_report``
    is exactly that). It delegates to :meth:`DeferralReport.split_by_disposition`
    so the rule itself has a single implementation.

    Why it exists (v0.2.91 dogfood fix): the partition had TWO homes. The
    entry-aware one honours ``resolved_disposition`` — an entry's EXPLICIT
    ``disposition`` first, the registry second. The registry-only
    ``deferral_registry.split_by_disposition(cids)`` sees condition IDs and
    nothing else, so an escalated record counted as a record there and as
    actionable everywhere else. wave-2 MINOR-3 fixed the CLAUDE.md reminder and
    left ``vco_lib.doctor`` on the cid-only copy, which is how the shipped
    doctor could report a count the ledger it had just read disagreed with.
    A cid-only partition is still correct for registry-level questions (the
    cross-language parity tests use it); it is never correct for ENTRIES.

    Never raises: an unusable input yields ``([], [])``, which every caller
    already treats as "nothing to say".
    """
    try:
        entries = list(getattr(report_or_entries, "entries", report_or_entries) or [])
    except Exception:  # noqa: BLE001 — unusable input ⇒ nothing to partition
        return ([], [])
    try:
        scratch = DeferralReport()
        for entry in entries:
            scratch.add_entry(entry)
        return scratch.split_by_disposition()
    except Exception:  # noqa: BLE001 — never break the surface that asked
        return ([], [])


def _disposition_split_line(entries: List["DeferralEntry"]) -> str:
    """One line summarising the ledger by DISPOSITION, or "" when unavailable.

    v0.2.91 WP-B surfacing rider. The reminder block used to be binary — it
    existed or it didn't — so a session opening a project with three stale
    "already done, no action needed" records read exactly like one with a
    blocking migration. Splitting the count lets a Claude session triage
    without opening the file.

    Soft-fail by design: if the registry can't be read the block simply keeps
    its pre-v0.2.91 shape rather than breaking the write.

    wave-2 MINOR-3: counts come from :meth:`DeferralReport.split_by_disposition`
    — the ONE partition — not from the registry-only
    ``deferral_registry.split_by_disposition(cids)``. The registry helper sees
    only condition IDs, so it silently ignored an entry's EXPLICIT
    ``disposition`` (which ``resolved_disposition`` prefers, and which is what
    the ledger renders and the GUI reads). An escalated record then showed as
    actionable in the ledger and as "0 actionable" in this line. The
    ``split_by_disposition`` docstring promises the surfaces cannot disagree;
    routing both through it is what makes that promise true.
    """
    if not entries:
        return ""
    actionable, informational = partition_entries(entries)
    if not actionable and not informational:
        return ""
    return (
        f"Currently: **{len(actionable)} actionable**, "
        f"{len(informational)} informational/record"
        f"{'' if len(informational) == 1 else 's'}.\n"
    )


def _reminder_block(entries: Optional[List["DeferralEntry"]] = None) -> str:
    """Render the wrapped CLAUDE.md reminder block.

    Idempotency contract (matches install.py's vct-merge-pending pattern):
    the prose inside MUST NOT contain literal _REMINDER_BEGIN /
    _REMINDER_END markers, otherwise the find-and-replace path would
    miscount. References to those markers are oblique ("the HTML-comment
    markers wrapping this block").

    ``entries`` (v0.2.91) adds the actionable/informational split line. It is
    optional so the block keeps rendering identically for callers that have no
    entry list to hand.
    """
    split = _disposition_split_line(entries or [])
    return (
        f"{_REMINDER_BEGIN}\n"
        "**Pending VCO action**: `.claude/context/UPDATE_DEFERRED.md` exists.\n"
        "Read it at session start — it contains commands to resolve\n"
        "unresolved VCO install actions.\n"
        f"{split}"
        "\n"
        "To remove THIS reminder block: once the deferral is resolved (e.g.\n"
        "via `--update --force`), VCO's next install run will delete\n"
        "UPDATE_DEFERRED.md AND strip this block. Manual cleanup if needed:\n"
        "delete everything between the HTML-comment markers wrapping this\n"
        "block.\n"
        f"{_REMINDER_END}\n"
    )


def _find_marker_spans(
    existing: str,
    is_begin: Callable[[str], bool],
    is_end: Optional[Callable[[str], bool]] = None,
):
    """Fence-aware line-start span scanner. THE one scanner in this module.

    v0.2.92 WP-15 extracted the predicates out of
    :func:`_find_all_reminder_marker_spans` so the VCO-owned-region stripper
    could locate the AUTO and managed-region markers with the SAME A-4 rules
    instead of growing a second scanner (a second scanner is how the fenced-
    marker bug would come back for the new families). The reminder wrapper
    below is now the only caller that hard-codes the reminder pair.

    ``is_begin`` / ``is_end`` receive the line ALREADY stripped of surrounding
    whitespace and answer "is this line the marker?" — so a marker embedded in
    a longer line, or quoted mid-sentence, never counts.

    ``is_end=None`` selects SINGLE-LINE mode: every ``is_begin`` hit is its own
    one-line span and ``dangling`` is always False. That is what the
    managed-region pair needs — those markers are standalone lines, not the
    fences of a block — and it keeps them on the same fence-aware rules
    instead of a bare ``str.find``.

    Returns ``(spans, dangling)`` — see the wrapper's docstring for the
    contract, which is unchanged.
    """
    lines = existing.splitlines(keepends=True)
    in_fence = False
    fence_marker: Optional[str] = None  # track ``` vs ~~~ style
    begin_at: Optional[int] = None  # char offset of pending begin marker line
    spans: List[tuple] = []  # (start, end) char offsets, document order
    offset = 0
    for line in lines:
        stripped = line.strip()
        # Fenced code-block toggle: a line whose FIRST non-space content is
        # ``` or ~~~ (info string allowed after). Track the fence char so a
        # nested ``` inside a ~~~ block doesn't mis-toggle.
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence_marker = stripped[:3]
            offset += len(line)
            continue
        if in_fence:
            if stripped.startswith(fence_marker or "```"):
                in_fence = False
                fence_marker = None
            offset += len(line)
            continue
        # Outside a fence: match markers only when they ARE the line (after
        # stripping surrounding whitespace) — a marker quoted mid-sentence
        # or inside a longer line does not count.
        if is_begin(stripped):
            if is_end is None:
                # Single-line mode: the marker line IS the span.
                spans.append((offset, offset + len(line.rstrip("\n"))))
            else:
                begin_at = offset
        elif is_end is not None and is_end(stripped) and begin_at is not None:
            # end marker line ends at offset + len(line); we want the offset
            # just past the marker text (exclude the trailing newline so the
            # caller controls newline trimming).
            spans.append((begin_at, offset + len(line.rstrip("\n"))))
            begin_at = None
        offset += len(line)

    return spans, begin_at is not None


def _find_all_reminder_marker_spans(existing: str):
    """Locate EVERY real reminder block in ``existing``, in document order.

    v0.2.92: the scanning half of the single-owner fix. Returns
    ``(spans, dangling)``:

        spans    — list of ``(start, end)`` char-offset pairs, one per
                   COMPLETE begin/end pair found at a real (unfenced,
                   line-start) position, in document order.  ``start`` =
                   index of the begin marker line; ``end`` = index just past
                   the end marker text (its trailing newline excluded, so the
                   caller owns newline trimming).
        dangling — True when a real begin marker was still unmatched at EOF.

    Matching rules are the A-4 (v0.2.73) rules, unchanged: a marker counts
    only when it IS the line (after stripping surrounding whitespace) AND it
    sits outside a fenced code block.  A marker QUOTED inside a fence (this
    repo's shareable CLAUDE.md documents them) is invisible here, which is
    what stops a quoted begin from being paired with a later real end and
    deleting every line between them.

    A second begin before the pending one closes REPLACES the pending begin
    (the pre-v0.2.92 single-span locator did the same via plain assignment);
    the abandoned begin is left alone rather than paired across user content.

    WHY a multi-span locator exists at all: two emitters used to own this
    block — this module and ``templates/ORCHESTRATOR-CLAUDE.md.template`` —
    and neither could see the other, so a CLAUDE.md that reached "no block"
    (a resolved ledger strips it) and was then spliced BEFORE the next
    template render ended up with a splice-owned copy above the AUTO region
    and a template-owned copy inside it.  A first-pair-only locator refreshes
    one and renders the other forever.  The template no longer carries a
    copy; this locator is how installs already in the doubled state get
    collapsed back to one.

    v0.2.92 WP-15: the loop moved to :func:`_find_marker_spans`; this is now
    the reminder-pair binding of that ONE scanner. Rules and return contract
    are byte-for-byte the same (pinned by
    ``tests/test_v0292_deferral_reminder_single_owner.py``).
    """
    return _find_marker_spans(
        existing,
        lambda stripped: stripped == _REMINDER_BEGIN,
        lambda stripped: stripped == _REMINDER_END,
    )


def _remove_reminder_span(existing: str, start: int, end: int) -> str:
    """Excise ONE located block plus the separator whitespace the splicer added.

    Extracted from ``_strip_reminder_from_claude_md`` (v0.2.92) so the strip
    path and the splice's collapse path share ONE removal rule — a second
    inline copy would have been the third generation of this same block of
    slicing.  Offsets must come from :func:`_find_all_reminder_marker_spans`.

    Separator arithmetic: removal takes back the block's own newline plus
    exactly ONE blank line, preferring the leading side.

    That is one fewer than the pre-v0.2.92 rule, which took a blank line from
    BOTH sides.  The splicer only ever ADDS one per side while CONSUMING the
    blanks that were already adjacent, so the two-sided rule removed a
    separator nobody inserted: it ate the blank line after a file's YAML
    frontmatter on every strip, and — now that the collapse path also removes
    blocks this module did not insert — it would have merged a user's own
    paragraphs on either side of one.

    Perfect fidelity is NOT achievable here and the choice is a deliberate
    trade.  A frontmatter file WITH a blank line after its closing fence and
    one WITHOUT splice to the SAME bytes — case 2 strips the tail's leading
    newlines — so no removal rule can restore both.  This one restores the
    blank-line shape (standard Markdown, and what the KG node format uses)
    and leaves the other with one extra blank line, which renders
    identically.  The pre-v0.2.92 rule made the opposite choice AND lost the
    paragraph break in the collapse case.  ``TestSeparatorArithmetic`` in
    tests/test_v0292_deferral_reminder_single_owner.py pins both halves so
    the trade cannot flip silently.
    """
    before = existing[:start]
    after = existing[end:]

    # The block's own terminating newline (``end`` stops at the marker text).
    if after.startswith("\n"):
        after = after[1:]

    # Then exactly ONE blank-line separator. Prefer the leading side, where
    # only the SECOND-to-last newline goes (the blank line itself), leaving
    # the newline that ends the preceding logical line intact.
    if before.endswith("\n\n"):
        before = before[:-1]
    elif after.startswith("\n"):
        after = after[1:]

    return before + after


def _find_reminder_marker_span(existing: str):
    """A-4 (v0.2.73): locate the reminder block by LINE-START markers that
    live OUTSIDE fenced code blocks.

    Returns:
        (start, end)     — char offsets: ``start`` = index of the begin
                           marker line, ``end`` = index just past the end
                           marker line (exclusive of its trailing newline).
        ("ambiguous",)   — a begin marker was found at a real (unfenced,
                           line-start) position but no matching real end
                           marker follows it → the caller must do nothing
                           and log, to avoid deleting user content.
        None             — no real reminder block present.

    WHY: the pre-A-4 ``existing.find(_REMINDER_BEGIN)`` matched the FIRST
    literal occurrence anywhere — including a marker QUOTED inside a code
    fence (this repo's own shareable CLAUDE.md documents the markers). It
    would then pair that quoted begin with a later real end and delete all
    user content between them. Matching only line-start markers outside
    fences removes that class of silent destruction.
    """
    spans, dangling = _find_all_reminder_marker_spans(existing)
    if spans:
        # Historical contract: the FIRST complete pair wins, and a stray
        # begin AFTER it is invisible (the pre-v0.2.92 locator returned on
        # the first pair and never scanned further).
        return spans[0]
    if dangling:
        # Real begin found but no matching real end → ambiguous. Do nothing.
        return ("ambiguous",)
    return None


def _splice_reminder_into_claude_md(
    existing: str, entries: Optional[List["DeferralEntry"]] = None
) -> str:
    """Return ``existing`` with the reminder block injected idempotently.

    Three insertion points (in priority order):

    1. If one or more previous reminder blocks exist (real, line-start
       markers outside fences): replace the FIRST in place — preserves the
       position chosen on the original insertion, prevents block migration on
       every install — and REMOVE every later one (v0.2.92 collapse).
    2. Else if ``existing`` opens with YAML frontmatter (``---\n...\n---\n``):
       prepend the block immediately AFTER the closing fence, separated by a
       blank line.
    3. Else: prepend the block at the very top, separated by a blank line
       from whatever follows.

    A-4: matching is fence-aware + line-start only. On an ambiguous begin
    (real begin, no real end) we DO NOT splice — we return the file
    unchanged so no user content is destroyed (the orphan marker stays; the
    user can clean it). This prefers a missing refresh over data loss.

    v0.2.92 — WHY the collapse, and WHY the FIRST copy is the survivor.

    Installs exist in the wild whose CLAUDE.md carries TWO blocks: one
    splice-owned (live counts) and one that the orchestrator CLAUDE.md
    template used to render unconditionally.  Neither emitter could see the
    other, so every install refreshed one and re-rendered the other and the
    duplication was self-sustaining.  The template no longer carries a copy;
    without this collapse those users would keep the doubled block forever,
    because nothing else in the system would ever remove it.

    The survivor is the FIRST block, for three reasons:

    * It is the position-preserving choice this function already promises —
      the same rule that keeps the block from migrating on every install.
    * It is marker-agnostic.  Picking "the copy outside the AUTO region"
      would require this module to know install.py's
      ``<!-- BEGIN: AUTO -->`` fences AND project_init's
      ``<!-- >>>VCO_MANAGED>>> -->`` fences — two conventions from two
      layers, neither of which this generic splicer has any business
      knowing.
    * In the real damaged shape the first copy IS the render-stable one: a
      splice that found no block prepends at the very top (case 2/3), i.e.
      ABOVE the AUTO region, while the template's copy sat INSIDE it.
      Should the survivor nonetheless land inside a managed region on some
      hand-edited file, the state is self-correcting: the next render
      replaces that region wholesale with template text that carries no
      block, and the next splice re-inserts at case 2/3 — outside it.

    Extra copies are NOT removed when a dangling begin marker is also
    present: that file is already ambiguous, and this function's older
    contract in that situation was to refresh the first pair and touch
    nothing else.  Healing an ambiguous file is worth less than the
    guarantee that we never cut across content we cannot parse.
    """
    block = _reminder_block(entries)

    spans, dangling = _find_all_reminder_marker_spans(existing)

    if spans:
        keep_start, keep_end = spans[0]
        # Collapse: drop every later block, highest offset first so the
        # surviving block's offsets stay valid. Skipped while ambiguous.
        extras = [] if dangling else spans[1:]
        out = existing
        for start, end in reversed(extras):
            out = _remove_reminder_span(out, start, end)
        after = out[keep_end:]
        # Strip a single leading newline so re-injections don't accumulate
        # blank lines.
        if after.startswith("\n"):
            after = after[1:]
        return out[:keep_start] + block + after

    if dangling:
        # Conservative: leave the file exactly as-is rather than risk
        # splicing across user content.
        return existing

    # Case 2: frontmatter — splice after closing fence.
    fm_match = _LEADING_FRONTMATTER_RE.match(existing)
    if fm_match:
        head = existing[: fm_match.end()]
        tail = existing[fm_match.end():]
        # Normalise: ensure exactly one blank line between fm and block,
        # and one between block and tail.
        if tail.startswith("\n"):
            tail = tail.lstrip("\n")
        sep = "" if head.endswith("\n") else "\n"
        return f"{head}{sep}\n{block}\n{tail}"

    # Case 3: no frontmatter — prepend at top.
    tail = existing.lstrip("\n")
    return f"{block}\n{tail}" if tail else block


def _strip_reminder_from_claude_md(existing: str) -> str:
    """Return ``existing`` with EVERY wrapped reminder block removed.

    No-op (returns the original string) when no real block is found or when
    the begin marker is ambiguous (real begin, no real end). Cleans up the
    blank-line separator that ``_splice_reminder_into_claude_md`` inserts on
    each side of the block, in either insertion case.

    A-4: uses the same fence-aware line-start locator as the splicer, so a
    marker quoted inside a code fence never triggers a delete.

    v0.2.92: removes ALL real blocks, not just the first. This is the repair
    path for an install already carrying the doubled block whose ledger is
    now EMPTY — ``write()`` calls here, and a first-block-only strip would
    leave the second copy asserting "Pending VCO action" on a project with
    no ledger at all, permanently.  Same dangling-begin conservatism as the
    splicer: while a stray begin is present, only the first block goes.
    """
    spans, dangling = _find_all_reminder_marker_spans(existing)
    if not spans:
        # No real block, or an orphan begin only — preserve the file. The
        # user can clean an orphan manually.
        return existing
    if dangling:
        spans = spans[:1]

    # Highest offset first: removing a later block cannot shift the offsets
    # of an earlier one.
    out = existing
    for start, end in reversed(spans):
        out = _remove_reminder_span(out, start, end)
    return out


def _remove_marker_line(existing: str, start: int, end: int) -> str:
    """Excise ONE single marker LINE (and only its own newline).

    Companion to :func:`_remove_reminder_span`, which is written for a BLOCK
    the splicer surrounded with blank-line separators. The managed-region pair
    is not a block: the two markers sit flush against content that must
    survive, so taking a blank-line separator here would silently merge the
    user's paragraph with the template body. Offsets come from
    :func:`_find_marker_spans`.
    """
    after = existing[end:]
    if after.startswith("\n"):
        after = after[1:]
    return existing[:start] + after


def strip_vco_owned_regions(text: str) -> str:
    """Return ``text`` with every VCO-INJECTED region removed.

    **Compare-only. NEVER write the result back to a user file.** The AUTO
    region IS the orchestrator root's whole CLAUDE.md body and the managed
    region wraps a project's whole rendered body — persisting this output
    would destroy both. The mutating strippers are
    :func:`_strip_reminder_from_claude_md` (reminder pair only) and
    :func:`_strip_claude_md_reminder`; this one exists so a DIFF can compare
    like against like.

    v0.2.92 WP-15 (why this exists): ``project_init``'s
    ``_install_project_level_templates`` flags a project's CLAUDE.md as
    "meaningfully differs from the shipping reference" and emits
    ``template_review_pending``. Three of the differences it was counting are
    VCO's OWN injected content, not user divergence:

      1. the deferral-reminder block this module splices in whenever a ledger
         exists — so *every project carrying any deferral at all* was reported
         as diverged, permanently (the code's own comment conceded it fires
         "for essentially every established project, forever" and v0.2.83
         answered the NOISE with dismissal memory rather than the CAUSE);
      2. the ``<!-- >>>VCO_MANAGED>>> -->`` marker LINES that
         ``merge_managed_region`` wraps around the render at create time,
         while the reference sidecar is written UNwrapped — so a project VCO
         itself created, never touched by a human, diverged on its second
         bundle run by construction;
      3. an AUTO region, which is install.py's orchestrator-root render.

    Each family is removed with the rule that matches its shape: the reminder
    and AUTO pairs are BLOCKS (removed whole, via the shared span remover that
    also takes back the separator whitespace their inserters added); the
    managed pair is two MARKER LINES around content that stays.

    All three use the ONE fence-aware scanner (:func:`_find_marker_spans`), so
    a marker QUOTED inside a fenced code block — this repo's own shareable
    CLAUDE.md documents all three families — is invisible here and cannot
    swallow the user's prose between it and a later real marker.

    Idempotent: applying this to its own output returns the same string.
    """
    out = _strip_reminder_from_claude_md(text)

    # AUTO region: a BLOCK. The BEGIN marker carries a provenance note
    # (`<!-- BEGIN: AUTO (rendered by install.py from ...) -->`), so it is
    # matched by prefix + the comment terminator rather than by equality.
    spans, dangling = _find_marker_spans(
        out,
        lambda s: s.startswith(_AUTO_REGION_BEGIN_PREFIX) and s.endswith("-->"),
        lambda s: s == _AUTO_REGION_END,
    )
    if dangling:
        # Same conservatism as the reminder stripper: while a stray BEGIN is
        # present, only the first complete pair goes.
        spans = spans[:1]
    for start, end in reversed(spans):
        out = _remove_reminder_span(out, start, end)

    # Managed-region pair: two MARKER LINES; the body between them is the
    # rendered template and IS the thing being compared, so it stays.
    lines, _ = _find_marker_spans(
        out,
        lambda s: s in (MANAGED_REGION_OPEN, MANAGED_REGION_CLOSE),
    )
    # Highest offset first — removing a later line cannot shift an earlier one.
    for start, end in reversed(lines):
        out = _remove_marker_line(out, start, end)
    return out


def _ensure_claude_md_reminder(
    folder: Path, entries: Optional[List["DeferralEntry"]] = None
) -> None:
    """Inject (or refresh) the reminder block in ``<folder>/CLAUDE.md``.

    No-op if CLAUDE.md is missing — the project-bootstrapper owns CLAUDE.md
    creation, not the deferral writer. Best-effort: never raises into the
    caller (an install run shouldn't fail just because the user holds an
    exclusive lock on CLAUDE.md).
    """
    target = folder / _CLAUDE_MD_REL
    try:
        if not target.exists():
            return
        existing = target.read_text(encoding="utf-8")
        updated = _splice_reminder_into_claude_md(existing, entries)
        if updated != existing:
            _atomic_write_text(target, updated)
    except OSError:
        # Best-effort: leave CLAUDE.md untouched if I/O fails.
        return


def _strip_claude_md_reminder(folder: Path) -> None:
    """Remove the reminder block from ``<folder>/CLAUDE.md``.

    No-op if CLAUDE.md is missing or contains no block. Best-effort.
    """
    target = folder / _CLAUDE_MD_REL
    try:
        if not target.exists():
            return
        existing = target.read_text(encoding="utf-8")
        updated = _strip_reminder_from_claude_md(existing)
        if updated != existing:
            _atomic_write_text(target, updated)
    except OSError:
        return


def _atomic_write_text(target: Path, content: str) -> None:
    """Atomic text write via temp file + os.replace in the same dir.

    Thin delegate to :func:`vco_lib.atomic.atomic_write_text` (v0.2.54
    Track J consolidation). Preserves UTF-8 (Unicode emoji-safe — some
    deferral entries carry emoji) and additionally gains the shared
    helper's fsync-before-rename crash-safety, which the previous
    inline copy lacked."""
    atomic_write_text(target, content)


@dataclass
class DeferralEntry:
    """One non-auto-resolvable condition detected during ``--update``."""

    condition_id: str
    """URL-safe slug uniquely identifying the condition type, e.g.
    ``schema_drift_rebuild_required``."""

    title: str
    """Short human-readable title, e.g. "Schema rebuild required"."""

    detected: str
    """What was detected; a one-to-three sentence description."""

    why_deferred: str
    """Why install.py could not auto-fix this condition."""

    command_to_apply: str
    """Exact CLI command the user (or ``--apply-deferred``) should run."""

    severity: str = "warning"
    """One of ``info``, ``warning``, ``critical``."""

    kg_node_refs: List[str] = field(default_factory=list)
    """Paths to relevant ``knowledge/concepts/*.md`` nodes for context."""

    detected_at: str = field(default_factory=_now_iso)
    """ISO-8601 UTC timestamp of detection."""

    disposition: Optional[str] = None
    """v0.2.91 WP-B — the DISPOSITION tier (``action_required`` |
    ``auto_retryable`` | ``environmental`` | ``informational_record``).

    Severity says how LOUD an entry is; disposition says what the reader OWES
    it. The two were conflated: ``kg_access_phantom_repaired`` (a record of a
    completed repair), ``safe_add_skipped_env_merge`` (a permanent by-design
    skip) and ``orchestrator_user_modified_preserved`` (genuinely pending work)
    all rendered as ``info`` and looked identical.

    ``None`` means "not set explicitly" — :attr:`resolved_disposition` then
    consults the registry, and falls back to ``action_required`` for an
    unregistered id. Callers should read that property, never this field."""

    dismiss_fields: dict = field(default_factory=dict)
    """v0.2.91 WP-B — current values of this condition's registry-declared
    ``dismiss_key`` fields (see :mod:`vco_lib.deferral_dismissal`).

    Machine bookkeeping, not user-facing: carried losslessly in the JSON
    sidecar, deliberately NOT rendered into the Markdown (the .md is a human
    render, and the md→entry fallback path is already lossy by design)."""

    probe_status: Optional[str] = None
    """v0.2.91 dogfood fix — one honest sentence saying HOW this entry can end.

    The ledger used to be silent about lifecycle: an entry whose probe returned
    "could not determine" and an entry with no automatic clear at all rendered
    identically to one that is simply still true, so an immortal row was
    indistinguishable from a live one — the exact "silent NotProbed-forever"
    state the registry exists to end.

    Set by the ONE annotator (:func:`vco_lib.deferral_probes.probe_report`) on
    every re-probe pass, derived from the registry's ``clear_probe`` plus the
    probe's tri-state verdict. ``None`` = not annotated yet (older ledgers, or a
    writer family that never re-probes), which renders exactly as it did before
    this field existed."""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(
                f"Invalid severity {self.severity!r}; must be one of "
                f"{SEVERITY_ORDER}"
            )
        if self.disposition is not None:
            from vco_lib.deferral_registry import CLASSES

            if self.disposition not in CLASSES:
                raise ValueError(
                    f"Invalid disposition {self.disposition!r}; must be one of "
                    f"{CLASSES} (or None to resolve from the registry)"
                )

    @property
    def resolved_disposition(self) -> str:
        """The effective disposition: explicit → registry → ``action_required``.

        Registry-driven resolution is what lets the launcher's Rust emitters
        get a correct tier for free — they never pass one over the Python
        bridge, and they should not have to.
        """
        if self.disposition:
            return self.disposition
        try:
            from vco_lib.deferral_registry import disposition_for

            return disposition_for(self.condition_id)
        except Exception:  # noqa: BLE001 — conservative on any registry problem
            return "action_required"


def _severity_max(entries: List[DeferralEntry]) -> str:
    """Return the highest-priority severity across all entries."""
    for sev in SEVERITY_ORDER:
        if any(e.severity == sev for e in entries):
            return sev
    return "info"


def condition_is_owned(
    condition_id: str,
    owned_ids,
    owned_prefixes=(),
) -> bool:
    """Return True when *condition_id* belongs to the caller's OWNED set.

    A-2 (v0.2.73): ``UPDATE_DEFERRED.md`` has multiple writer families
    (install.py, ``vco_lib.project_init``, several Rust emitters, background
    resync children). A writer that rebuilds the file from a fresh in-memory
    report may only apply drop-when-absent semantics to the condition IDs it
    OWNS (i.e. re-detects on every run); every other entry is FOREIGN and
    must be preserved verbatim.

    ``owned_ids`` is a collection of exact condition IDs; ``owned_prefixes``
    covers dynamically-suffixed families (e.g. ``schema_migration_failed_*``).
    """
    if condition_id in owned_ids:
        return True
    return any(condition_id.startswith(p) for p in owned_prefixes if p)


def safe_emit_entry(
    report: Any,
    *,
    condition_id: str,
    title: str,
    detected: str,
    why_deferred: str,
    command_to_apply: str,
    severity: str = "warning",
    kg_node_refs: Optional[List[str]] = None,
    dismiss_fields: Optional[dict] = None,
    log_event: Optional[Callable[..., None]] = None,
    log_step: str = "",
) -> bool:
    """Build a :class:`DeferralEntry` and add it to an in-memory report,
    with the standard guard + soft-fail wrapper.

    This is the ONE factory for the in-memory ``_emit_*_deferral`` family
    (v0.2.77 Part 7a cluster F convergence). Every such emitter used to
    hand-write the identical three-part boilerplate:

        1. ``if report is None: return``            (no-op guard)
        2. ``try: entry = DeferralEntry(...); report.add_entry(entry)``
        3. ``except Exception as exc: <soft-fail log>``  (never break the caller)

    Each hand-written copy was a divergence risk: a new emitter could forget
    the None-guard, use a different log step, or drop the try/except. Routing
    through this helper makes each per-site emitter shrink to a data-only call.

    Args:
        report: a ``DeferralReport`` (or None — no-op, returns False). Typed
            ``Any`` so callers don't need to import DeferralReport just to
            satisfy the annotation (install.py passes ``DeferralReport | None``).
        condition_id..kg_node_refs: forwarded verbatim to :class:`DeferralEntry`.
        log_event: optional ``(step, phase, detail, *, data=None)`` logger
            (install.py passes ``_log_install_event``). Called with a ``warn``
            phase if entry construction / add raises. Its own failures are
            swallowed.
        log_step: the step label passed to ``log_event`` on the soft-fail path.

    Returns:
        True when the entry was added, False on the None-guard or on a
        swallowed exception. (Most call-sites ignore the return — it's there
        for the rare emitter that wants to branch on "did it land".)
    """
    if report is None:
        return False
    try:
        entry = DeferralEntry(
            condition_id=condition_id,
            title=title,
            detected=detected,
            why_deferred=why_deferred,
            command_to_apply=command_to_apply,
            severity=severity,
            kg_node_refs=list(kg_node_refs) if kg_node_refs else [],
            dismiss_fields=dict(dismiss_fields) if dismiss_fields else {},
        )
        report.add_entry(entry)
        return True
    except Exception as exc:  # noqa: BLE001 — soft-fail by design
        if log_event is not None:
            try:
                log_event(
                    log_step, "warn",
                    f"could not emit {condition_id} deferral: {exc}",
                )
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Markdown serialisation helpers
# ---------------------------------------------------------------------------

def _render_frontmatter(entries: List[DeferralEntry]) -> str:
    ids_yaml = ", ".join(e.condition_id for e in entries)
    sev_max = _severity_max(entries)
    generated = _now_iso()
    return (
        f"---\n"
        f"title: VCO Update Deferred\n"
        f"generated_at: {generated}\n"
        f"condition_ids: [{ids_yaml}]\n"
        f"severity_max: {sev_max}\n"
        f"---\n"
    )


def _render_header() -> str:
    return (
        "\n"
        "# VCO Update Deferred\n"
        "\n"
        "The last `install.py --update` detected conditions it could not "
        "auto-resolve safely. Each section below names a condition and the "
        "exact command to apply it.\n"
    )


def _render_entry(entry: DeferralEntry) -> str:
    kg_lines = ""
    if entry.kg_node_refs:
        refs = "\n".join(f"- `{ref}`" for ref in entry.kg_node_refs)
        kg_lines = f"\n**Cross-references**:\n{refs}\n"

    # v0.2.91 dogfood fix: rendered ONLY when set, so every ledger written
    # before this field existed (and every writer family that does not
    # re-probe) renders byte-identically to v0.2.90.
    status_line = (
        f"**Probe status**: {entry.probe_status}\n\n"
        if entry.probe_status
        else ""
    )

    # v0.2.91 WP-B: the `## <cid> (<sev>)` HEADER SHAPE IS UNTOUCHED — both
    # `_SECTION_RE` here and the Rust `restart.rs::extract_section` /
    # `strip_section` parse it, and WP-F's banner work depends on
    # extract_section keeping up with new entries. Disposition rides as an
    # ordinary `**Field**:` line, which the field parser round-trips and
    # unknown-field-tolerant readers ignore.
    return (
        f"\n"
        f"## {entry.condition_id} ({entry.severity})\n"
        f"\n"
        f"**Title**: {entry.title}\n"
        f"\n"
        f"**Disposition**: {entry.resolved_disposition}\n"
        f"\n"
        f"{status_line}"
        f"**Detected**: {entry.detected}\n"
        f"\n"
        f"**Why deferred**: {entry.why_deferred}\n"
        f"\n"
        f"**To apply**:\n"
        f"```bash\n"
        f"{entry.command_to_apply}\n"
        f"```\n"
        f"{kg_lines}"
        f"\n"
        f"**Detected at**: {entry.detected_at}\n"
        f"\n"
        f"{_SECTION_SEP}\n"
    )


# ---------------------------------------------------------------------------
# Markdown parser helpers (for read() back-compat)
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^## (?P<cid>[^\s(]+)\s+\((?P<sev>[^)]+)\)\s*$", re.MULTILINE
)
_FIELD_RE = re.compile(r"^\*\*(?P<key>[^*]+)\*\*:\s*(?P<val>.+)$")
_FM_RE = re.compile(
    r"^---\n(?P<body>.*?)^---\n", re.DOTALL | re.MULTILINE
)
_CONDITION_IDS_RE = re.compile(r"condition_ids:\s*\[(?P<ids>[^\]]*)\]")
_GENERATED_AT_RE = re.compile(r"generated_at:\s*(?P<ts>[^\n]+)")
_SEVERITY_MAX_RE = re.compile(r"severity_max:\s*(?P<sev>[^\n]+)")


def _parse_frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    if not m:
        return {}
    body = m.group("body")
    result: dict = {}
    m2 = _CONDITION_IDS_RE.search(body)
    if m2:
        result["condition_ids"] = [s.strip() for s in m2.group("ids").split(",") if s.strip()]
    m3 = _GENERATED_AT_RE.search(body)
    if m3:
        result["generated_at"] = m3.group("ts").strip()
    m4 = _SEVERITY_MAX_RE.search(body)
    if m4:
        result["severity_max"] = m4.group("sev").strip()
    return result


def _parse_entries(text: str) -> List[DeferralEntry]:
    """Parse all entry sections from the file body."""
    # Strip frontmatter first.
    text_body = _FM_RE.sub("", text, count=1)

    entries: List[DeferralEntry] = []
    positions = [m.start() for m in _SECTION_RE.finditer(text_body)]
    positions.append(len(text_body))

    for i, start in enumerate(positions[:-1]):
        end = positions[i + 1]
        chunk = text_body[start:end]

        header_m = _SECTION_RE.match(chunk.lstrip("\n"))
        if not header_m:
            continue

        cid = header_m.group("cid")
        sev = header_m.group("sev").strip()

        # Extract labelled fields.
        fields: dict = {}
        for line in chunk.splitlines():
            fm = _FIELD_RE.match(line.strip())
            if fm:
                fields[fm.group("key").strip()] = fm.group("val").strip()

        # Extract command (fenced code block).
        cmd = ""
        in_fence = False
        for line in chunk.splitlines():
            if line.strip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                cmd = (cmd + "\n" + line).strip()

        # Extract cross-references bullet list.
        refs: List[str] = []
        after_refs = False
        for line in chunk.splitlines():
            if "**Cross-references**" in line:
                after_refs = True
                continue
            if after_refs:
                stripped = line.strip()
                if stripped.startswith("- `") and stripped.endswith("`"):
                    refs.append(stripped[3:-1])
                elif stripped.startswith("**") or stripped.startswith("##"):
                    after_refs = False

        # v0.2.91: an unknown/legacy Disposition value is DROPPED rather than
        # raising — the Markdown is a human-editable render, and a typo there
        # must not make the whole report unreadable. The registry then supplies
        # the tier via `resolved_disposition`.
        raw_disposition = fields.get("Disposition")
        disposition = _coerce_disposition(raw_disposition)

        entries.append(
            DeferralEntry(
                condition_id=cid,
                title=fields.get("Title", cid.replace("_", " ").title()),
                detected=fields.get("Detected", ""),
                why_deferred=fields.get("Why deferred", ""),
                command_to_apply=cmd,
                severity=sev if sev in SEVERITY_ORDER else "warning",
                kg_node_refs=refs,
                detected_at=fields.get("Detected at", _now_iso()),
                disposition=disposition,
                probe_status=(fields.get("Probe status") or None),
            )
        )

    return entries


# ---------------------------------------------------------------------------
# A-3: JSON sidecar (source of truth) serialisation
# ---------------------------------------------------------------------------

def _coerce_disposition(raw: Any) -> Optional[str]:
    """Normalise a parsed disposition value; unknown/absent ⇒ ``None``.

    ``None`` is the correct "not set" answer — :attr:`DeferralEntry.resolved_disposition`
    then asks the registry, so an entry written by an older VCO (or by a Rust
    emitter that never passes one) still renders with the right tier instead of
    being pinned to a stale literal.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        from vco_lib.deferral_registry import CLASSES
    except Exception:  # noqa: BLE001 — registry unreadable ⇒ treat as unset
        return None
    return value if value in CLASSES else None


def _entry_to_dict(entry: DeferralEntry) -> dict:
    """Render one entry to a JSON-safe dict. Every field is preserved
    losslessly (multi-line strings survive — no Markdown round-trip).

    v0.2.91: ``disposition`` + ``dismiss_fields`` are ADDITIVE and optional, so
    ``schema_version`` stays 1 — ``_entry_from_dict`` already ignores unknown
    keys, which means an OLDER VCO reading a NEWER sidecar degrades to the
    pre-v0.2.91 behaviour instead of rejecting the file. Absent keys are OMITTED
    rather than written as nulls so the sidecar of a report with no dispositions
    is byte-identical to what v0.2.90 wrote."""
    out = {
        "condition_id": entry.condition_id,
        "title": entry.title,
        "detected": entry.detected,
        "why_deferred": entry.why_deferred,
        "command_to_apply": entry.command_to_apply,
        "severity": entry.severity,
        "kg_node_refs": list(entry.kg_node_refs),
        "detected_at": entry.detected_at,
    }
    if entry.disposition:
        out["disposition"] = entry.disposition
    if entry.dismiss_fields:
        out["dismiss_fields"] = dict(entry.dismiss_fields)
    if entry.probe_status:
        out["probe_status"] = entry.probe_status
    return out


def _entry_from_dict(d: dict) -> Optional[DeferralEntry]:
    """Build a :class:`DeferralEntry` from a sidecar dict.

    Returns ``None`` when the dict is missing the load-bearing
    ``condition_id`` or carries an invalid severity — a malformed sidecar
    entry is skipped rather than crashing the whole read (conservative).
    """
    cid = d.get("condition_id")
    if not cid or not isinstance(cid, str):
        return None
    sev = d.get("severity", "warning")
    if sev not in SEVERITY_ORDER:
        sev = "warning"
    refs = d.get("kg_node_refs") or []
    if not isinstance(refs, list):
        refs = []
    dismiss = d.get("dismiss_fields")
    if not isinstance(dismiss, dict):
        dismiss = {}
    return DeferralEntry(
        condition_id=cid,
        title=str(d.get("title", cid.replace("_", " ").title())),
        detected=str(d.get("detected", "")),
        why_deferred=str(d.get("why_deferred", "")),
        command_to_apply=str(d.get("command_to_apply", "")),
        severity=sev,
        kg_node_refs=[str(r) for r in refs],
        detected_at=str(d.get("detected_at", _now_iso())),
        disposition=_coerce_disposition(d.get("disposition")),
        dismiss_fields={str(k): v for k, v in dismiss.items()},
        probe_status=(
            str(d["probe_status"]).strip() or None
            if isinstance(d.get("probe_status"), str)
            else None
        ),
    )


def _render_json_sidecar(entries: List[DeferralEntry]) -> str:
    """Render the authoritative JSON sidecar for ``entries``."""
    payload = {
        "schema_version": _JSON_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "severity_max": _severity_max(entries),
        "entries": [_entry_to_dict(e) for e in entries],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _parse_json_sidecar(text: str) -> Optional[List[DeferralEntry]]:
    """Parse the JSON sidecar. Returns ``None`` (not ``[]``) when the file
    is unparseable or carries an incompatible ``schema_version`` — the
    caller then falls back to the Markdown parser. An empty but valid
    sidecar returns ``[]`` (zero entries, distinct from "unusable")."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    ver = payload.get("schema_version")
    if ver != _JSON_SCHEMA_VERSION:
        # Unknown/newer schema — don't guess; fall back to Markdown.
        return None
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return None
    out: List[DeferralEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        entry = _entry_from_dict(item)
        if entry is not None:
            out.append(entry)
    return out


def _markdown_condition_ids_present(text: str) -> set:
    """Return the set of ``condition_id`` values whose ``## <cid> (<sev>)``
    section header is present in the Markdown ``text``.

    Used to reconcile the JSON source of truth against a Rust
    ``restart.rs`` edit that stripped a section from the Markdown (the one
    cross-language mutation that touches the ``.md`` without knowing about
    the JSON sidecar)."""
    return {m.group("cid") for m in _SECTION_RE.finditer(text)}


# ---------------------------------------------------------------------------
# DeferralReport
# ---------------------------------------------------------------------------

class DeferralReport:
    """Accumulate deferral entries and write/read the structured Markdown file.

    Usage (writer side)::

        report = DeferralReport()
        report.add_entry(DeferralEntry(condition_id="schema_drift_rebuild_required", ...))
        report.write(PROJECT_ROOT)   # writes .claude/context/UPDATE_DEFERRED.md

    Usage (reader side)::

        report = DeferralReport.read(PROJECT_ROOT)
        for entry in report.entries:
            print(entry.condition_id, entry.command_to_apply)
    """

    def __init__(self) -> None:
        self._entries: List[DeferralEntry] = []
        # P1 (v0.2.75): condition IDs this report instance EXPLICITLY
        # resolved this run (tombstones). :meth:`merge_from_disk` skips
        # them so a late merge (e.g. install.py's pre-final-write TOCTOU
        # merge) cannot resurrect an entry whose on-disk copy still exists
        # only because the single end-of-run write hasn't happened yet.
        # Canonical case: ``codegraph_embed_resync_pending`` is FOREIGN
        # (never in install.py's owned set), cleared from MEMORY by the
        # R-6 owed-probe via mark_resolved — without the tombstone a
        # naive merge would re-import the stale disk copy and the ledger
        # would never clear.
        self._resolved_this_run: set = set()

    # ------------------------------------------------------------------
    # Public accumulation API
    # ------------------------------------------------------------------

    def add_entry(self, entry: DeferralEntry) -> None:
        """Accumulate an entry; last write for a given condition_id wins."""
        self._entries = [e for e in self._entries if e.condition_id != entry.condition_id]
        self._entries.append(entry)
        # A re-add supersedes a prior mark_resolved: the condition is live
        # again, so it must not be tombstoned out of a later disk merge.
        self._resolved_this_run.discard(entry.condition_id)

    def mark_resolved(self, condition_id: str) -> None:
        """Drop all entries matching *condition_id* (resolved; next write removes them).

        Also tombstones the ID for this run so :meth:`merge_from_disk`
        cannot re-import a stale on-disk copy (P1, v0.2.75). Recorded
        unconditionally — resolving an ID that was never in memory (e.g.
        a probe confirming an on-disk ledger entry is settled) must still
        prevent its resurrection at merge time.
        """
        self._entries = [e for e in self._entries if e.condition_id != condition_id]
        self._resolved_this_run.add(condition_id)

    def merge_from_disk(
        self,
        folder: Path,
        *,
        exclude_ids=(),
        exclude_prefixes=(),
    ) -> int:
        """Merge the on-disk report's FOREIGN entries into this report (A-2).

        Seeds a fresh writer-side report from ``<folder>/.claude/context/
        UPDATE_DEFERRED.md`` so a later :meth:`write` does not clobber entries
        emitted by OTHER writer families (project_init, Rust emitters,
        background resync children). Classification:

        * ``condition_id`` matched by ``exclude_ids`` / ``exclude_prefixes``
          (the caller's OWNED set — re-detected every run) → NOT merged; the
          caller's drop-when-absent semantics stay intact for those.
        * already present in this report (the current run re-detected it) →
          NOT merged; the in-memory entry is fresher.
        * resolved by THIS report instance this run (``mark_resolved``
          tombstone, P1 v0.2.75) → NOT merged; the on-disk copy is stale —
          it survives only because the caller's single end-of-run write
          hasn't landed yet. Re-importing it would resurrect an entry the
          run explicitly settled (e.g. ``codegraph_embed_resync_pending``
          after the R-6 not_owed probe) and the ledger would never clear.
        * everything else (FOREIGN) → appended verbatim.

        Returns the number of entries merged. Never raises — a read/parse
        failure logs nothing here (the caller owns logging) and returns 0,
        which is indistinguishable from "no foreign entries"; callers that
        need to detect the failure should read the file themselves first.
        """
        try:
            on_disk = DeferralReport.read(folder)
        except Exception:  # noqa: BLE001 — unparseable file → nothing to merge
            return 0
        merged = 0
        for entry in on_disk.entries:
            cid = entry.condition_id
            if condition_is_owned(cid, exclude_ids, exclude_prefixes):
                continue
            if cid in self._resolved_this_run:
                continue
            if self.has_condition(cid):
                continue
            self._entries.append(entry)
            merged += 1
        return merged

    @property
    def entries(self) -> List[DeferralEntry]:
        return list(self._entries)

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, folder: Path) -> bool:
        """Atomic-write the deferral report to ``<folder>/.claude/context/UPDATE_DEFERRED.md``.

        Side effect (item 2 / Gap 10, 2026-05-13): when writing a
        non-empty deferral, also injects a wrapped reminder block into
        ``<folder>/CLAUDE.md`` so future Claude sessions see "go read
        UPDATE_DEFERRED.md" at session start. When deleting the deferral
        (empty entries), strips the block from CLAUDE.md too.

        Both CLAUDE.md helpers are best-effort: a missing or unwritable
        CLAUDE.md does NOT cause this method to fail or raise.

        Returns:
            True  — entries present, file written.
            False — no entries; existing file deleted (if any).
        """
        target = folder / _DEFERRED_REL
        json_target = folder / _DEFERRED_JSON_REL

        if not self._entries:
            if target.exists():
                target.unlink()
            # A-3: remove the JSON source of truth too so the two views
            # stay consistent (empty ⇒ both absent).
            if json_target.exists():
                json_target.unlink()
            # Strip the reminder block since the deferral is gone.
            _strip_claude_md_reminder(folder)
            return False

        target.parent.mkdir(parents=True, exist_ok=True)

        # A-3: JSON sidecar is the SOURCE OF TRUTH — write it first so a
        # crash between the two writes leaves the authoritative copy intact
        # (the Markdown is a re-derivable render).
        atomic_write_text(json_target, _render_json_sidecar(self._entries))

        content = (
            _render_frontmatter(self._entries)
            + _render_header()
            + "".join(_render_entry(e) for e in self._entries)
        )

        # Atomic write via the shared vco_lib.atomic helper (temp file
        # in the same directory, fsync, then os.replace()). Markdown is the
        # human render + the surface Rust ``restart.rs`` edits.
        atomic_write_text(target, content)

        # Inject/refresh the wrapped reminder block in CLAUDE.md.
        _ensure_claude_md_reminder(folder, self._entries)

        return True

    @classmethod
    def read(cls, folder: Path) -> "DeferralReport":
        """Parse an existing deferral report and return a populated instance.

        A-3: the JSON sidecar (``UPDATE_DEFERRED.json``) is the SOURCE OF
        TRUTH. Resolution order:

        1. **JSON sidecar present + parseable** → authoritative content.
           Reconcile against the Markdown: if a co-present Markdown file
           lacks a section for a ``condition_id`` the JSON carries AND
           that ID is one the Rust ``restart.rs`` flow strips
           (``_RUST_STRIPPABLE_CONDITION_IDS``, P2a v0.2.75), the entry
           was cleared by the restart flow — drop it so the two views
           agree. For any OTHER ID a missing Markdown header means the
           Markdown is stale (crashed/partial Markdown write) — KEEP the
           entry; the next write() re-renders the Markdown from JSON.
           If the Markdown is absent, take the JSON verbatim.
        2. **JSON absent / unparseable / incompatible schema** → fall back
           to the legacy Markdown parser (back-compat for reports written
           before A-3, and for the round-trip-lossy path).
        3. **Neither present** → empty report.
        """
        target = folder / _DEFERRED_REL
        json_target = folder / _DEFERRED_JSON_REL
        report = cls()

        json_entries: Optional[List[DeferralEntry]] = None
        if json_target.exists():
            try:
                json_entries = _parse_json_sidecar(
                    json_target.read_text(encoding="utf-8")
                )
            except OSError:
                json_entries = None

        if json_entries is not None:
            # JSON is authoritative. Reconcile against a co-present Markdown
            # (the surface Rust edits) so a restart-cleared section is
            # honoured even though Rust doesn't touch the JSON.
            md_present_cids: Optional[set] = None
            if target.exists():
                try:
                    md_text = target.read_text(encoding="utf-8")
                    md_present_cids = _markdown_condition_ids_present(md_text)
                except OSError:
                    md_present_cids = None
            for entry in json_entries:
                if (
                    md_present_cids is not None
                    and entry.condition_id not in md_present_cids
                    and entry.condition_id in _RUST_STRIPPABLE_CONDITION_IDS
                ):
                    # Section was stripped from the Markdown by the Rust
                    # restart flow (the only editor that strips a section
                    # from the .md without touching the JSON). Treat as
                    # resolved — drop it. Any OTHER cid missing from the
                    # Markdown = stale/partial .md → keep (P2a v0.2.75).
                    continue
                report._entries.append(entry)
            return report

        # Fallback: no usable JSON sidecar → parse the Markdown.
        if not target.exists():
            return report
        text = target.read_text(encoding="utf-8")
        for entry in _parse_entries(text):
            report._entries.append(entry)
        return report

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def has_condition(self, condition_id: str) -> bool:
        return any(e.condition_id == condition_id for e in self._entries)

    def entry_for(self, condition_id: str) -> Optional[DeferralEntry]:
        """The entry for ``condition_id``, or ``None``.

        Used by the re-probe pass, which hands the entry to a probe so the
        probe can read the state the emitter recorded (e.g. the preserved
        sidecar paths) instead of re-deriving it.
        """
        for entry in self._entries:
            if entry.condition_id == condition_id:
                return entry
        return None

    def split_by_disposition(self) -> tuple[List[DeferralEntry], List[DeferralEntry]]:
        """Partition entries into ``(actionable, informational)``.

        Actionable = ``action_required`` + ``auto_retryable`` (still owed work,
        even when VCO can do it itself). Informational = ``environmental`` +
        ``informational_record``. The GUI ledger panel (WP-I) and the CLAUDE.md
        reminder split both read this ONE partition so their counts can never
        disagree.
        """
        actionable: List[DeferralEntry] = []
        informational: List[DeferralEntry] = []
        for entry in self._entries:
            # v0.2.91 dogfood fix: through the ONE rule (:func:`entry_disposition`)
            # so a duck-typed entry — the doctor's injectable seam produces
            # those — partitions identically to a real one instead of forcing
            # that caller onto a second, cid-only implementation.
            if entry_disposition(entry) in ("action_required", "auto_retryable"):
                actionable.append(entry)
            else:
                informational.append(entry)
        return actionable, informational

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)
