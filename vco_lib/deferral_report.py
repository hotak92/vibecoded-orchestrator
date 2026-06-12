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

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from vco_lib.atomic import atomic_write_text

# Relative path inside any managed project folder.
_DEFERRED_REL = Path(".claude") / "context" / "UPDATE_DEFERRED.md"

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

# Leading frontmatter detector: ``^---\n<body>\n---\n``. The trailing
# newline after the closing fence is captured so we can splice the
# reminder block after it cleanly.
_LEADING_FRONTMATTER_RE = re.compile(
    r"\A---\n.*?^---\n", re.DOTALL | re.MULTILINE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reminder_block() -> str:
    """Render the wrapped CLAUDE.md reminder block.

    Idempotency contract (matches install.py's vct-merge-pending pattern):
    the prose inside MUST NOT contain literal _REMINDER_BEGIN /
    _REMINDER_END markers, otherwise the find-and-replace path would
    miscount. References to those markers are oblique ("the HTML-comment
    markers wrapping this block").
    """
    return (
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


def _splice_reminder_into_claude_md(existing: str) -> str:
    """Return ``existing`` with the reminder block injected idempotently.

    Three insertion points (in priority order):

    1. If a previous reminder block exists (find the begin/end markers):
       replace it in place — preserves position chosen on the original
       insertion, prevents block migration on every install.
    2. Else if ``existing`` opens with YAML frontmatter (``---\n...\n---\n``):
       prepend the block immediately AFTER the closing fence, separated
       by a blank line.
    3. Else: prepend the block at the very top, separated by a blank line
       from whatever follows.

    The trailing blank line is normalised so we don't accumulate empty
    lines on repeat injections.
    """
    block = _reminder_block()

    # Case 1: existing block — replace in place.
    start = existing.find(_REMINDER_BEGIN)
    if start != -1:
        end_rel = existing[start:].find(_REMINDER_END)
        if end_rel != -1:
            end = start + end_rel + len(_REMINDER_END)
            after = existing[end:]
            # Strip a single leading newline so re-injections don't
            # accumulate blank lines.
            if after.startswith("\n"):
                after = after[1:]
            return existing[:start] + block + after
        # Begin marker without a matching end — treat as corrupt; fall
        # through and prepend a fresh block. The orphan begin marker
        # stays in place; the user can manually clean it. We prefer
        # corrupt + accurate over silent loss.

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
    """Return ``existing`` with the wrapped reminder block removed.

    No-op (returns the original string) when no block is found. Cleans
    up the blank-line separator that ``_splice_reminder_into_claude_md``
    inserts on each side of the block, in either insertion case (block at
    top-of-file or block after frontmatter).
    """
    start = existing.find(_REMINDER_BEGIN)
    if start == -1:
        return existing
    end_rel = existing[start:].find(_REMINDER_END)
    if end_rel == -1:
        # Orphan begin marker: don't try to guess the end — preserve the
        # file. The user can clean it manually.
        return existing
    end = start + end_rel + len(_REMINDER_END)

    before = existing[:start]
    after = existing[end:]

    # Trim the trailing newline the splicer added immediately after the
    # block AND the blank-line separator (if any).
    if after.startswith("\n"):
        after = after[1:]
    if after.startswith("\n"):
        after = after[1:]

    # Trim the blank-line separator the splicer inserted before the
    # block — only the SECOND-to-last newline (the blank line itself),
    # leaving the newline that ends the preceding logical line intact.
    if before.endswith("\n\n"):
        before = before[:-1]

    return before + after


def _ensure_claude_md_reminder(folder: Path) -> None:
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
        updated = _splice_reminder_into_claude_md(existing)
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

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(
                f"Invalid severity {self.severity!r}; must be one of "
                f"{SEVERITY_ORDER}"
            )


def _severity_max(entries: List[DeferralEntry]) -> str:
    """Return the highest-priority severity across all entries."""
    for sev in SEVERITY_ORDER:
        if any(e.severity == sev for e in entries):
            return sev
    return "info"


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

    return (
        f"\n"
        f"## {entry.condition_id} ({entry.severity})\n"
        f"\n"
        f"**Title**: {entry.title}\n"
        f"\n"
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
            )
        )

    return entries


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

    # ------------------------------------------------------------------
    # Public accumulation API
    # ------------------------------------------------------------------

    def add_entry(self, entry: DeferralEntry) -> None:
        """Accumulate an entry; last write for a given condition_id wins."""
        self._entries = [e for e in self._entries if e.condition_id != entry.condition_id]
        self._entries.append(entry)

    def mark_resolved(self, condition_id: str) -> None:
        """Drop all entries matching *condition_id* (resolved; next write removes them)."""
        self._entries = [e for e in self._entries if e.condition_id != condition_id]

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

        if not self._entries:
            if target.exists():
                target.unlink()
            # Strip the reminder block since the deferral is gone.
            _strip_claude_md_reminder(folder)
            return False

        target.parent.mkdir(parents=True, exist_ok=True)

        content = (
            _render_frontmatter(self._entries)
            + _render_header()
            + "".join(_render_entry(e) for e in self._entries)
        )

        # Atomic write via the shared vco_lib.atomic helper (temp file
        # in the same directory, fsync, then os.replace()).
        atomic_write_text(target, content)

        # Inject/refresh the wrapped reminder block in CLAUDE.md.
        _ensure_claude_md_reminder(folder)

        return True

    @classmethod
    def read(cls, folder: Path) -> "DeferralReport":
        """Parse an existing deferral report file and return a populated instance.

        If the file does not exist, returns an empty ``DeferralReport``.
        """
        target = folder / _DEFERRED_REL
        report = cls()
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

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)
