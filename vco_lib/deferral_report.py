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
from typing import List, Optional

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
# treat JSON as authoritative BUT drop any entry whose ``## <cid>`` header is
# absent from a co-present Markdown file — that means the Rust restart flow
# (or a manual edit) removed it. This keeps JSON authoritative for content
# while honouring the one cross-language mutation that touches the Markdown.
_DEFERRED_JSON_REL = Path(".claude") / "context" / "UPDATE_DEFERRED.json"

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
    lines = existing.splitlines(keepends=True)
    in_fence = False
    fence_marker: Optional[str] = None  # track ``` vs ~~~ style
    begin_at: Optional[int] = None  # char offset of begin marker line
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
        if stripped == _REMINDER_BEGIN:
            begin_at = offset
        elif stripped == _REMINDER_END and begin_at is not None:
            # end marker line ends at offset + len(line); we want the offset
            # just past the marker text (exclude the trailing newline so the
            # caller controls newline trimming).
            end = offset + len(line.rstrip("\n"))
            return (begin_at, end)
        offset += len(line)

    if begin_at is not None:
        # Real begin found but no matching real end → ambiguous. Do nothing.
        return ("ambiguous",)
    return None


def _splice_reminder_into_claude_md(existing: str) -> str:
    """Return ``existing`` with the reminder block injected idempotently.

    Three insertion points (in priority order):

    1. If a previous reminder block exists (real, line-start markers outside
       fences): replace it in place — preserves position chosen on the
       original insertion, prevents block migration on every install.
    2. Else if ``existing`` opens with YAML frontmatter (``---\n...\n---\n``):
       prepend the block immediately AFTER the closing fence, separated by a
       blank line.
    3. Else: prepend the block at the very top, separated by a blank line
       from whatever follows.

    A-4: matching is fence-aware + line-start only. On an ambiguous begin
    (real begin, no real end) we DO NOT splice — we return the file
    unchanged so no user content is destroyed (the orphan marker stays; the
    user can clean it). This prefers a missing refresh over data loss.
    """
    block = _reminder_block()

    span = _find_reminder_marker_span(existing)
    if span == ("ambiguous",):
        # Conservative: leave the file exactly as-is rather than risk
        # splicing across user content.
        return existing
    if isinstance(span, tuple) and len(span) == 2 and isinstance(span[0], int):
        start, end = span
        after = existing[end:]
        # Strip a single leading newline so re-injections don't accumulate
        # blank lines.
        if after.startswith("\n"):
            after = after[1:]
        return existing[:start] + block + after

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

    No-op (returns the original string) when no real block is found or when
    the begin marker is ambiguous (real begin, no real end). Cleans up the
    blank-line separator that ``_splice_reminder_into_claude_md`` inserts on
    each side of the block, in either insertion case.

    A-4: uses the same fence-aware line-start locator as the splicer, so a
    marker quoted inside a code fence never triggers a delete.
    """
    span = _find_reminder_marker_span(existing)
    if span is None or span == ("ambiguous",):
        # No real block, or an orphan begin — preserve the file. The user
        # can clean an orphan manually.
        return existing
    # Past the guard, span is the concrete (start, end) int pair — the
    # ("ambiguous",) 1-tuple and None variants are already returned above.
    # Bind as int so the slices below type cleanly (the union's ambiguous
    # arm can't reach here).
    start: int = span[0]  # type: ignore[assignment]
    end: int = span[1]  # type: ignore[index]

    before = existing[:start]
    after = existing[end:]

    # Trim the trailing newline the splicer added immediately after the
    # block AND the blank-line separator (if any).
    if after.startswith("\n"):
        after = after[1:]
    if after.startswith("\n"):
        after = after[1:]

    # Trim the blank-line separator the splicer inserted before the block —
    # only the SECOND-to-last newline (the blank line itself), leaving the
    # newline that ends the preceding logical line intact.
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
# A-3: JSON sidecar (source of truth) serialisation
# ---------------------------------------------------------------------------

def _entry_to_dict(entry: DeferralEntry) -> dict:
    """Render one entry to a JSON-safe dict. Every field is preserved
    losslessly (multi-line strings survive — no Markdown round-trip)."""
    return {
        "condition_id": entry.condition_id,
        "title": entry.title,
        "detected": entry.detected,
        "why_deferred": entry.why_deferred,
        "command_to_apply": entry.command_to_apply,
        "severity": entry.severity,
        "kg_node_refs": list(entry.kg_node_refs),
        "detected_at": entry.detected_at,
    }


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
    return DeferralEntry(
        condition_id=cid,
        title=str(d.get("title", cid.replace("_", " ").title())),
        detected=str(d.get("detected", "")),
        why_deferred=str(d.get("why_deferred", "")),
        command_to_apply=str(d.get("command_to_apply", "")),
        severity=sev,
        kg_node_refs=[str(r) for r in refs],
        detected_at=str(d.get("detected_at", _now_iso())),
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
        _ensure_claude_md_reminder(folder)

        return True

    @classmethod
    def read(cls, folder: Path) -> "DeferralReport":
        """Parse an existing deferral report and return a populated instance.

        A-3: the JSON sidecar (``UPDATE_DEFERRED.json``) is the SOURCE OF
        TRUTH. Resolution order:

        1. **JSON sidecar present + parseable** → authoritative content.
           Reconcile against the Markdown: if a co-present Markdown file
           lacks a section for a ``condition_id`` the JSON carries, that
           entry was stripped by the Rust ``restart.rs`` flow (or a manual
           edit) — drop it so the two views agree. If the Markdown is
           absent, take the JSON verbatim.
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
                ):
                    # Section was stripped from the Markdown by an external
                    # editor (restart.rs). Treat as resolved — drop it.
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

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)
