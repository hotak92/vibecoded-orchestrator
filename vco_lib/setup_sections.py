# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""SETUP-ONLY block lifecycle — acknowledgement state + the renderer's rule.

The orchestrator-root ``CLAUDE.md`` ships first-run help wrapped in HTML-comment
markers::

    <!-- BEGIN: SETUP-ONLY (...) -->
    ... content ...
    <!-- END: SETUP-ONLY -->

Once the setup is done that text is noise, and ``cleanup-setup-sections.py``
strips it. But those blocks live INSIDE the AUTO-rendered region of
``templates/ORCHESTRATOR-CLAUDE.md.template``, and ``rendered_root_files``
replaces that whole region from the template on every update — so a removal was
always undone, and nothing recorded that the user had already acted.

This module is the ONE home for the lifecycle the fix needs:

* :func:`find_blocks` / :func:`strip_blocks` — the marker rule (line-oriented,
  nesting-aware), shared by the renderer and the cleanup script so the two can
  never disagree about where a block starts or ends;
* the acknowledgement file (``.claude/state/setup-sections-ack.json``) —
  a small JSON set of block content hashes the user has acted on, written
  atomically;
* :func:`pending_rendered_blocks` — the clear-probe predicate: the blocks a
  rendered file still carries that are not acknowledged;
* :func:`emit_pending_deferral` — the ``first_run_setup_pending`` ledger row
  emitted at render time while any block is rendered-and-unacknowledged, and
  cleared once all rendered blocks are acknowledged.

Why a content hash, not a boolean flag: a later release that CHANGES a block
(a genuinely new setup step) must re-arm the reminder, and a changed block has
a new hash. An unchanged block keeps its hash and stays removed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vco_lib.deferral_report import DeferralEntry

#: The deferral condition emitted while a rendered SETUP-ONLY block is
#: unacknowledged (declared in ``vco_lib/deferral_conditions.toml``). Declared
#: as a module constant so the source-scanning completeness gate
#: (``tests/test_deferral_registry_completeness_v0291.py``) resolves it.
CONDITION_ID = "first_run_setup_pending"

#: The acknowledgement file, relative to the orchestrator root. ``.claude/state/``
#: is git-ignored, so writing it never dirties the tree an update is about to
#: merge.
ACK_REL = Path(".claude") / "state" / "setup-sections-ack.json"

#: Schema version of the acknowledgement file.
ACK_FORMAT_VERSION = 1

#: The remedy the reminder tells the user to run, relative to the root. Its
#: presence is also how "this tree is a MANAGED install, not a bare clone" is
#: decided (see :func:`is_managed_install_root`).
CLEANUP_SCRIPT_REL = Path(".claude") / "scripts" / "cleanup-setup-sections.py"

#: The block delimiters, identical to the ones the cleanup script shipped with.
#: The BEGIN marker may carry a parenthetical note after ``SETUP-ONLY``; the
#: ``[^>]*`` tail absorbs it up to the ``-->`` close.
BEGIN_RE = re.compile(r"<!--\s*BEGIN:\s*SETUP-ONLY[^>]*-->")
END_RE = re.compile(r"<!--\s*END:\s*SETUP-ONLY[^>]*-->")

#: The template/label pairs :func:`build_entry` names in the reminder, one per
#: render path (v0.2.101 12a: the lifecycle serves BOTH the orchestrator root
#: and user projects; the defaults are the root's, the project render path in
#: ``vco_lib.project_templates`` passes the ``PROJECT_*`` pair).
ROOT_TEMPLATE_NAME = "templates/ORCHESTRATOR-CLAUDE.md.template"
ROOT_LABEL = "the orchestrator root"
PROJECT_TEMPLATE_NAME = "templates/CLAUDE.md.template"
PROJECT_ROOT_LABEL = "this project's root"


@dataclass(frozen=True)
class SetupBlock:
    """One SETUP-ONLY region: its exact text (markers included) and its hash."""

    text: str
    sha256: str


def block_sha256(block_text: str) -> str:
    """Content hash of one block's exact text, markers included.

    Hashing the region verbatim (not just the prose between the markers) means
    a change to the marker's own descriptive note also re-arms the reminder —
    which is the honest reading of "the block changed".
    """
    return hashlib.sha256(block_text.encode("utf-8")).hexdigest()


def _scan(text: str) -> Iterable[tuple[int, int, SetupBlock]]:
    """Yield ``(start_line, end_line, block)`` for every block, document order.

    Line-oriented and nesting-aware (a defensive depth counter, since a nested
    SETUP-ONLY pair would otherwise truncate the outer block). Raises
    ``ValueError`` on an unmatched marker — a malformed document is a defect
    the caller must surface, never a silent partial strip.
    """
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if BEGIN_RE.search(lines[i]):
            j = i + 1
            depth = 1
            while j < len(lines) and depth > 0:
                if BEGIN_RE.search(lines[j]):
                    depth += 1
                elif END_RE.search(lines[j]):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                raise ValueError(
                    f"Unmatched SETUP-ONLY BEGIN at line {i + 1} — no closing END found"
                )
            seg = "".join(lines[i:j + 1])
            yield i, j, SetupBlock(seg, block_sha256(seg))
            i = j + 1
        else:
            if END_RE.search(lines[i]):
                raise ValueError(
                    f"Unmatched SETUP-ONLY END at line {i + 1} — no opening BEGIN above"
                )
            i += 1


def find_blocks(text: str) -> tuple[SetupBlock, ...]:
    """Every SETUP-ONLY block in ``text``, document order."""
    return tuple(block for _start, _end, block in _scan(text))


def strip_blocks(
    text: str, should_remove: Callable[[SetupBlock], bool]
) -> tuple[str, tuple[SetupBlock, ...]]:
    """Remove every block for which ``should_remove`` is true.

    Returns ``(new_text, removed_blocks)``. A removed block also consumes a
    single following blank separator line, so the document does not accumulate
    stray blank lines when the same removal runs again (idempotency hygiene —
    the shipped script's behaviour, kept identical here so the render path and
    the cleanup script produce byte-identical output).

    The renderer passes ``lambda b: b.sha256 in acknowledged``; the cleanup
    script passes ``lambda b: True``. The marker walk is :func:`_scan`'s — the
    SAME one :func:`find_blocks` uses — so the two can never disagree about a
    block's bounds.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    removed: list[SetupBlock] = []
    cursor = 0
    for start, end, block in _scan(text):
        out.extend(lines[cursor:start])
        if should_remove(block):
            removed.append(block)
            cursor = end + 1
            if cursor < len(lines) and lines[cursor].strip() == "":
                cursor += 1
        else:
            out.extend(lines[start:end + 1])
            cursor = end + 1
    out.extend(lines[cursor:])
    return "".join(out), tuple(removed)


# ---------------------------------------------------------------------------
# The acknowledgement file
# ---------------------------------------------------------------------------

def ack_path(folder: Path) -> Path:
    """Absolute path of the acknowledgement file for ``folder``."""
    return Path(folder) / ACK_REL


def is_managed_install_root(folder: Path) -> bool:
    """True when ``folder`` is an orchestrator root the bundle has installed into.

    The reminder must be ACTIONABLE: it tells the reader to run
    ``python .claude/scripts/cleanup-setup-sections.py``. A bare source clone
    (or any tree that merely holds ``templates/``) has no such script, so
    raising the row there would name a command the user cannot run — and would
    manufacture a "Pending VCO action" on a tree where nothing is installed.
    That is exactly the v0.2.92 invariant ("a fresh render makes no pending
    claim": ``tests/test_v0292_deferral_reminder_single_owner.py``), and gating
    on the remedy's presence keeps both true: install.py's step 5b installs
    ``.claude/scripts/`` BEFORE step 4c renders, so a real install (fresh or
    updating) is always managed by the time this is asked.
    """
    return (Path(folder) / CLEANUP_SCRIPT_REL).is_file()


def acknowledged_hashes(folder: Path) -> frozenset[str]:
    """The block hashes recorded as acted-on.

    NEVER raises: a missing, unreadable, corrupt or wrongly-typed file is
    treated as "nothing acknowledged" (every block re-renders) rather than
    crashing the render — the conservative direction, since an unreadable ack
    must not silently suppress the reminder.
    """
    path = ack_path(folder)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    try:
        data = json.loads(raw)
    except ValueError:
        return frozenset()
    if not isinstance(data, dict):
        return frozenset()
    items = data.get("acknowledged")
    if not isinstance(items, list):
        return frozenset()
    return frozenset(h for h in items if isinstance(h, str) and h)


def record_acknowledged(folder: Path, hashes: Iterable[str]) -> bool:
    """Union ``hashes`` into the ack file with an atomic write.

    Returns ``True`` on success (including the no-op when ``hashes`` is empty),
    ``False`` when the file could not be written — the cleanup script treats a
    ``False`` as fatal, because a removal the ack did not record would be undone
    by the next update.
    """
    new = frozenset(h for h in hashes if isinstance(h, str) and h)
    if not new:
        return True
    path = ack_path(folder)
    try:
        from vco_lib.atomic import atomic_write_json

        merged = acknowledged_hashes(folder) | new
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "format_version": ACK_FORMAT_VERSION,
                "acknowledged": sorted(merged),
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        return True
    except Exception:  # noqa: BLE001 — reported to the caller as a False
        return False


# ---------------------------------------------------------------------------
# The rule shared by the renderer and the clear probe
# ---------------------------------------------------------------------------

def pending_blocks(
    text: str, acknowledged: frozenset[str]
) -> tuple[SetupBlock, ...]:
    """Blocks in ``text`` whose hash is NOT acknowledged (what must be rendered).

    A fresh install (nothing acknowledged) returns every block; once a block's
    hash is acknowledged its removal survives re-rendering; a block whose
    content changed has a new hash and renders again.
    """
    return tuple(b for b in find_blocks(text) if b.sha256 not in acknowledged)


def apply_to_render(
    folder: Path, rendered_text: str
) -> tuple[str, Optional[tuple[SetupBlock, ...]]]:
    """The renderer-side lifecycle step — ONE home for BOTH render paths.

    Strips every block whose content hash is acknowledged (so a removal the
    cleanup script recorded survives the re-render) and reports the blocks
    that remain (the caller feeds them to :func:`emit_pending_deferral` after
    the write). ``vco_lib.rendered_root_files.render_entry`` (the orchestrator
    root) and ``vco_lib.project_templates`` (a project's CLAUDE.md, v0.2.101
    12a) both call this instead of inlining the sequence, so the two paths can
    never drift in how an acknowledgement is honoured.

    Returns ``(text, pending)``. ``pending`` is ``None`` ONLY when the markers
    are malformed (the text is then returned exactly as it came in and the
    caller must leave the ledger alone — a partial strip of a malformed
    document is the silent-data-loss shape this module exists to prevent);
    ``()`` means the render carries no unacknowledged block, which is what
    clears the deferral row.
    """
    try:
        acknowledged = acknowledged_hashes(folder)
        text, _stripped = strip_blocks(
            rendered_text, lambda b: b.sha256 in acknowledged
        )
        return text, find_blocks(text)
    except (OSError, ValueError):
        return rendered_text, None


def pending_rendered_blocks(folder: Path) -> Optional[tuple[SetupBlock, ...]]:
    """The clear-probe predicate: unacknowledged SETUP-ONLY blocks in the
    folder's rendered ``CLAUDE.md``.

    ``()`` — positive evidence there is nothing left to act on: the file has no
    unacknowledged block (either it was never there, or the renderer stripped
    the acknowledged ones). ``None`` — could not look (unreadable file or a
    malformed marker pair), so a caller must NOT treat it as "over".
    """
    target = Path(folder) / "CLAUDE.md"
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return pending_blocks(text, acknowledged_hashes(folder))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The deferral row
# ---------------------------------------------------------------------------

def build_entry(
    folder: Path,
    blocks: tuple[SetupBlock, ...],
    *,
    template_name: str = ROOT_TEMPLATE_NAME,
    root_label: str = ROOT_LABEL,
) -> "DeferralEntry":
    """Build the ``first_run_setup_pending`` :class:`DeferralEntry`.

    The prose is addressed to the reading agent ("For your Claude assistant"),
    which is the surface the ledger's human render reaches: act on each block,
    then run the cleanup script, which records the acknowledgement and clears
    this row.

    ``template_name`` / ``root_label`` parametrise the two render paths the
    lifecycle serves (v0.2.101 12a): the defaults describe the orchestrator
    root; a project's CLAUDE.md render passes :data:`PROJECT_TEMPLATE_NAME`
    and :data:`PROJECT_ROOT_LABEL`. The remedy command is the same at both —
    the cleanup script resolves its target relative to its own location, so
    ``cd <folder>`` + the relative script path works at either root.
    """
    from vco_lib.deferral_report import DeferralEntry

    count = len(blocks)
    plural = "block" if count == 1 else "blocks"
    hashes = ", ".join(b.sha256[:12] for b in blocks)
    detected = (
        f"CLAUDE.md still contains {count} un-acknowledged SETUP-ONLY "
        f"{plural} rendered from {template_name} "
        f"(content hash(es): {hashes})."
    )
    why = (
        "For your Claude assistant (read this before continuing the user's "
        "task): these are first-run help sections (the project-scoping nudge "
        "and, on the orchestrator root, the first-run setup steps and the "
        f"installation-verification checklist). Act on each {plural} (finish "
        "or decline it) and then run the cleanup command below from "
        f"{root_label}. That records an acknowledgement keyed by each block's "
        "content hash, so the removed blocks STAY removed across future "
        "updates, and this entry clears. A block whose content changes in a "
        "later release renders again with a new hash and re-arms this reminder."
    )
    command = (
        f"cd {Path(folder)}\n"
        "python .claude/scripts/cleanup-setup-sections.py"
    )
    return DeferralEntry(
        condition_id=CONDITION_ID,
        title="First-run setup sections are still in CLAUDE.md",
        detected=detected,
        why_deferred=why,
        command_to_apply=command,
        severity="warning",
    )


def emit_pending_deferral(
    folder: Path,
    blocks: tuple[SetupBlock, ...],
    *,
    template_name: str = ROOT_TEMPLATE_NAME,
    root_label: str = ROOT_LABEL,
) -> None:
    """Emit (or clear) ``first_run_setup_pending`` for ``folder``.

    ``blocks`` non-empty ⇒ the row is written (re-emission keeps the first
    ``detected_at``). Empty ⇒ the row is resolved, but only when it is actually
    present, so a clean render of a root that never had the condition does not
    create the ledger lock file. Best-effort: the emitter soft-fails internally.

    A no-op outside a MANAGED install root (see
    :func:`is_managed_install_root`): a tree that has not had the bundle
    installed has no cleanup script to run, so raising a "pending action"
    there would name a command the user cannot run. The gate holds for a user
    project exactly as for the root — ``templates/scripts/**`` ships into
    ``<project>/.claude/scripts/``, so a bundled project IS a managed install
    root by the time its CLAUDE.md renders.

    ``template_name`` / ``root_label`` name the render path in the row's
    prose (see :func:`build_entry`); a project's CLAUDE.md render passes the
    ``PROJECT_*`` pair.
    """
    from vco_lib.deferral_emit import emit_entries, resolve_conditions
    from vco_lib.deferral_report import DeferralReport

    folder = Path(folder)
    if not is_managed_install_root(folder):
        # A bare clone / synthetic tree that merely holds ``templates/``: no
        # bundle, no remedy to run, so no claim (and nothing to clear).
        return
    if blocks:
        emit_entries(
            folder,
            (build_entry(folder, blocks, template_name=template_name,
                         root_label=root_label),),
            keep_first_detected=True,
        )
        return
    try:
        present = DeferralReport.read(folder).has_condition(CONDITION_ID)
    except Exception:  # noqa: BLE001 — unreadable ledger ⇒ nothing to clear
        return
    if present:
        resolve_conditions(folder, [CONDITION_ID])