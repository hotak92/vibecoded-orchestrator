# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared work-units counter for Claude Code transcript JSONL.

# v10.1 (2026-05-01) — work-units counter for KG-update nudge

The hook that uses this module is the KG-update-nudge: it fires on
UserPromptSubmit when the assistant has accumulated substantial *work*
without writing a knowledge-graph node. So we want a "work-done" proxy,
NOT a billing/cost-accuracy metric.

## Formula

For each requestId-deduped logical API request:

    work_units_per_request =
        output_tokens                                       # what the model produced (direct)
      + edit_or_write_body_chars            * 0.30          # files authored (NO cap)
      + min(read_result_chars, 80_000)      * 0.30          # files read
      + min(web_result_chars, 80_000)       * 0.30          # WebSearch / WebFetch
      + min(agent_response_chars, 50_000)   * 0.30          # sub-agent answers
      + min(bash_result_chars, 30_000)      * 0.30          # Bash / Grep / Glob output

Threshold: 175k first-fire / 50k subsequent-interval (literal user spec).

Per-call caps prevent any single tool result from dominating the threshold —
one big read can't trigger a fire by itself; multiple research moves must
accumulate. `chars * 0.30` is the rough Anthropic chars→tokens ratio for
the agentic mixed-content workload (English prose + code + JSON tool
results); 0.25 (pure prose) was under-counting code/JSON intake by 20-60%.
output_tokens is used directly as integer tokens.

## Reset path (out of scope here)

The hook itself, NOT this module, decides when to reset baseline. Reset
triggers (in `kg-update-nudge.sh`):
  - PostToolUse `Write` / `Edit` to **/knowledge/**/*.md
  - PostToolUse `mcp__weaviate-kg__store_knowledge_node`
  - SessionStart source=compact (post-compaction baseline reset)
  - Escape-marker in latest assistant text (see Marker Detection below)

## Marker detection

The assistant can emit `[No KG update needed: <reason>]` to declare
"I searched the KG, found nothing worth adding". The hook treats this
as a baseline-reset event.

v10.1 SELF-SUPPRESSION FIX: the hook's nudge body itself contains the
literal example syntax (so the agent can see how to use it). Without
guard, scanning ANY assistant text for the marker pattern would match
echoes of the nudge prompt → silent self-suppression. Fix: strip
nudge-body fingerprints from assistant text BEFORE the marker regex
runs. The fingerprint is a stable substring the hook always prints and
which never legitimately appears in assistant prose:
"Counter resets on Write or Edit to knowledge/**/*.md OR
store_knowledge_node calls." Any paragraph containing this fingerprint
is dropped from the marker-scan input.

## Field reliability

Verified 2026-05-01 against live transcript + Anthropic public docs:
  - input_tokens: streaming placeholder, ~75% are 0 or 1 — UNRELIABLE
  - output_tokens: undercounted ~10-17x vs statusbar (uniform per model
    → OK as a *relative* threshold signal) — RELIABLE for work-counting
  - cache_creation_input_tokens: matches statusbar 1x — RELIABLE for
    cost (kept exposed via ScanResult for cost-tracker), NOT for work
  - cache_read_input_tokens: matches statusbar 1x for cache hits

Claude Code emits ~3 JSONL entries per actual API request during streaming;
all three entries share the same requestId. Without dedup, totals inflate
~3x. v10.1 dedups BOTH usage AND tool_use credits by requestId (v10
regressed on this for tool_use bodies; B12 fix below).

## Sub-agent / sidechain note

Sub-agent transcripts live in separate `~/.claude/projects/<proj>/<uuid>.jsonl`
files with `isSidechain: true` at the top level. This scanner walks
ONLY the parent transcript (the one passed in). The defensive
`isSidechain == False` filter at line-process time guards against
future regression if anyone extends the scanner to walk sidechains.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Iterator


# ── v10.1 caps + chars→tokens conversion ───────────────────────────────────
CHARS_TO_TOKENS = 0.30                      # mixed prose+code+JSON ratio
EDIT_BODY_NO_CAP = True                     # bounded by what model authored
READ_RESULT_CAP_CHARS = 80_000              # files read (Read tool)
WEB_RESULT_CAP_CHARS = 80_000               # WebSearch + WebFetch + KG search
AGENT_RESPONSE_CAP_CHARS = 50_000           # Agent / Task tool reply
BASH_RESULT_CAP_CHARS = 30_000              # Bash / Grep / Glob (mostly noise)
# Image vision intake: vision pricing is per-tile; ~1500 tokens flat per
# image is a reasonable proxy (5-10 tiles × ~150 tokens/tile + alpha).
IMAGE_BLOCK_TOKENS = 1500


# ── tool-name allow-lists ──────────────────────────────────────────────────
# B4 fix: MultiEdit handled with edits[] sum, even though Claude Code v2.1.x
# may have removed it from the canonical tool list — defensive coverage.
EDIT_TOOL_NAMES = {"Edit", "Write", "ctx_edit", "NotebookEdit", "MultiEdit"}
READ_TOOL_NAMES = {"Read", "ctx_read"}
# B9 fix: weaviate-kg search MCPs ARE intake (the project's primary research
# tools per CLAUDE.md). Excluded: store_knowledge_node (it's the reset trigger).
WEB_TOOL_NAMES = {
    "WebSearch", "WebFetch",
    # search MCP: only `search_papers` survives v0.2.11 (web_search,
    # search_code, fetch_page were dropped — see PR-14a). The removed
    # names are no longer in this set; if they appear in an old log
    # replay, they fall through to the default tool-category bucket.
    "mcp__search__search_papers",
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
    "mcp__weaviate-kg__search_code_graph",
    "mcp__weaviate-kg__query_code_structure",
}
AGENT_TOOL_NAMES = {"Agent", "Task"}
# B6 fix: Bash output IS intake; capped tighter than Read because it's
# more often noise (status checks, short results).
BASH_TOOL_NAMES = {"Bash", "Grep", "Glob"}


# ── nudge-body strip patterns (B13: self-suppression fix) ──────────────────
# These match echoes of the hook's literal nudge body in assistant text.
# Strip them before scanning for the escape marker so the example syntax
# inside the nudge doesn't self-trigger a false-positive marker match.
# The fingerprint is a stable substring the hook always prints and which
# never legitimately appears in assistant prose.
_NUDGE_FINGERPRINT_LINE = (
    "Counter resets on Write or Edit to knowledge/**/*.md OR "
    "store_knowledge_node calls."
)


def _strip_nudge_echoes(text: str) -> str:
    """Drop any paragraph that contains the hook's nudge-body fingerprint.

    The fingerprint sentence is unique to the hook's literal output and
    won't appear in legitimate assistant prose. Any paragraph containing
    it is dropped from the marker-scan input — including the example
    `[No KG update needed: ...]` syntax that lives inside that paragraph
    and would otherwise self-suppress.

    Conservative: split on blank-line paragraphs; drop entire paragraphs
    that contain the fingerprint. Cheap and false-positive-resistant.
    """
    if _NUDGE_FINGERPRINT_LINE not in text:
        return text
    # Split on blank-line paragraph boundaries (also handles \r\n).
    paragraphs = re.split(r"\n\s*\n", text)
    kept = [p for p in paragraphs if _NUDGE_FINGERPRINT_LINE not in p]
    return "\n\n".join(kept)


@dataclass
class UsageFields:
    """Canonical token fields extracted from a Claude Code usage dict."""

    input_tokens: int = 0           # UNRELIABLE — streaming placeholder
    output_tokens: int = 0           # uniform under-count; OK as threshold signal
    cache_creation_input_tokens: int = 0   # cost-accurate, NOT work-accurate
    cache_read_input_tokens: int = 0       # RELIABLE — cache hits


def extract_usage_fields(usage: dict | None) -> UsageFields:
    """Pull the four canonical fields from a usage dict; missing → 0."""
    if not isinstance(usage, dict):
        return UsageFields()
    return UsageFields(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
    )


@dataclass
class ScanResult:
    """Aggregated session totals from a transcript scan.

    `work_units_total` is the v10.1 work-done proxy (used by kg-update-nudge).
    `cache_creation_total` and `cache_read_total` are kept for cost-tracker
    callers — do NOT use cache_creation as a work signal.
    """

    work_units_total: int = 0          # v10.1 — the work-done counter
    cache_creation_total: int = 0      # cost-only, do not use for work
    cache_read_total: int = 0          # cost-only
    output_tokens_total: int = 0       # diagnostic / debugging
    deduped_requests: int = 0
    raw_entries_with_usage: int = 0
    seen_request_ids: set[str] = field(default_factory=set)
    # Per-request char tallies, accumulated when the next request's tool_result
    # blocks reveal what was returned. Keyed by tool_use_id (correlates the
    # assistant's tool_use to the user's tool_result reply).
    _pending_intake: dict[str, "_PendingIntake"] = field(default_factory=dict)


@dataclass
class _PendingIntake:
    """In-flight tool_use whose tool_result hasn't arrived yet."""
    request_id: str = ""
    tool_kind: str = ""    # 'read' | 'web' | 'agent' | 'bash'


def _edit_body_chars(tool_name: str, tool_input: dict) -> int:
    """Extract the body-chars an Edit-class tool authored.

    B3 fix: NotebookEdit uses `new_source`, not `new_string`/`content`.
    B4 fix: MultiEdit input has `edits: [{old_string, new_string, ...}, ...]`.
    Sum all `new_string` entries.
    """
    if not isinstance(tool_input, dict):
        return 0

    # MultiEdit: sum every edit's new_string
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        if isinstance(edits, list):
            return sum(
                len(e.get("new_string") or "")
                for e in edits
                if isinstance(e, dict)
            )
        return 0

    # All other Edit-class tools: try the canonical fallback chain
    for k in ("content", "new_string", "new_source"):
        v = tool_input.get(k)
        if isinstance(v, str):
            return len(v)
    return 0


class TranscriptScanner:
    """Scan a Claude Code JSONL transcript with requestId dedup.

    Computes `work_units_total` (v10.1) — the recommended signal for the
    KG-update nudge. See module docstring for formula.
    """

    MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024  # 256 MB hard cap to avoid OOM

    def scan(
        self,
        transcript_path: str | os.PathLike,
        on_assistant_message: callable | None = None,
    ) -> ScanResult:
        """Scan a transcript file. Returns aggregated totals.

        on_assistant_message: optional callback receiving (entry_dict, msg_dict,
                              running_work_units). Used by kg-update-nudge
                              to detect escape markers in assistant text.
        """
        result = ScanResult()
        path = os.fspath(transcript_path)
        if not path or not os.path.exists(path):
            return result
        try:
            if os.path.getsize(path) >= self.MAX_TRANSCRIPT_BYTES:
                return result
        except OSError:
            return result

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    self._process_line(line, result, on_assistant_message)
        except OSError:
            pass

        # Pending intakes whose tool_result never arrived (e.g. session ended
        # mid-call) — discarded; do not credit speculative chars.
        result._pending_intake.clear()
        return result

    def _process_line(
        self,
        line: str,
        result: ScanResult,
        on_assistant_message: callable | None,
    ) -> None:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return
        msg = entry.get("message")
        if not isinstance(msg, dict):
            return

        # Defensive guard: if the entry came from a sub-agent sidechain,
        # ignore it. Parent transcript walks shouldn't see sidechain entries
        # today, but this is cheap insurance against a future regression.
        if entry.get("isSidechain") is True:
            return

        # The requestId we attribute work to. Each unique requestId is one
        # logical API call (Claude Code emits ~3 JSONL entries per call
        # during streaming; we dedup by gating BOTH usage and tool_use
        # credits on `is_new_request`).
        req_id = entry.get("requestId") or msg.get("id") or ""
        # B10 fix: empty requestId means we can't dedup → skip both usage
        # and tool_use accounting. This avoids inflating work via duplicate
        # streaming entries that lack req_id (rare but possible).
        is_new_request = bool(req_id) and (req_id not in result.seen_request_ids)
        if is_new_request:
            result.seen_request_ids.add(req_id)

        # ── usage accounting (one per unique requestId) ───────────────────
        usage = msg.get("usage")
        if isinstance(usage, dict):
            result.raw_entries_with_usage += 1
            if is_new_request:
                fields_ = extract_usage_fields(usage)
                result.work_units_total += fields_.output_tokens
                result.output_tokens_total += fields_.output_tokens
                result.cache_creation_total += fields_.cache_creation_input_tokens
                result.cache_read_total += fields_.cache_read_input_tokens
                result.deduped_requests += 1

        # ── tool_use intake / authorship accounting (assistant turns) ─────
        # B12 fix: gate the tool_use walk by `is_new_request` to prevent
        # 3× over-credit on streaming-duplicated entries.
        if is_new_request and msg.get("role") == "assistant":
            for block in _iter_blocks(msg):
                if block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name") or ""
                tool_use_id = block.get("id") or ""
                tool_input = block.get("input") or {}

                if tool_name in EDIT_TOOL_NAMES:
                    body_chars = _edit_body_chars(tool_name, tool_input)
                    if body_chars > 0:
                        result.work_units_total += int(body_chars * CHARS_TO_TOKENS)
                elif tool_name in READ_TOOL_NAMES:
                    if tool_use_id:
                        result._pending_intake[tool_use_id] = _PendingIntake(
                            request_id=req_id, tool_kind="read")
                elif tool_name in WEB_TOOL_NAMES:
                    if tool_use_id:
                        result._pending_intake[tool_use_id] = _PendingIntake(
                            request_id=req_id, tool_kind="web")
                elif tool_name in AGENT_TOOL_NAMES:
                    if tool_use_id:
                        result._pending_intake[tool_use_id] = _PendingIntake(
                            request_id=req_id, tool_kind="agent")
                elif tool_name in BASH_TOOL_NAMES:
                    if tool_use_id:
                        result._pending_intake[tool_use_id] = _PendingIntake(
                            request_id=req_id, tool_kind="bash")

        # ── tool_result intake (user turns) ───────────────────────────────
        # B7 fix: skip is_error results (failure-loops shouldn't inflate work).
        if msg.get("role") == "user":
            for block in _iter_blocks(msg):
                if block.get("type") != "tool_result":
                    continue
                if block.get("is_error") is True:
                    continue
                tool_use_id = block.get("tool_use_id") or ""
                pending = result._pending_intake.pop(tool_use_id, None)
                if pending is None:
                    continue
                content = block.get("content")
                # B8 fix: handle text + image + document blocks. Text uses
                # char count × 0.30; image uses flat per-block token estimate.
                chars, image_tokens = _content_chars_and_images(content)
                if pending.tool_kind == "read":
                    cap = READ_RESULT_CAP_CHARS
                elif pending.tool_kind == "web":
                    cap = WEB_RESULT_CAP_CHARS
                elif pending.tool_kind == "agent":
                    cap = AGENT_RESPONSE_CAP_CHARS
                elif pending.tool_kind == "bash":
                    cap = BASH_RESULT_CAP_CHARS
                else:
                    continue
                if chars > 0:
                    chars = min(chars, cap)
                    result.work_units_total += int(chars * CHARS_TO_TOKENS)
                if image_tokens > 0:
                    result.work_units_total += image_tokens

        # ── escape-hatch / forensic callback ──────────────────────────────
        if on_assistant_message and msg.get("role") == "assistant":
            on_assistant_message(entry, msg, result.work_units_total)


def iter_assistant_text(msg: dict) -> Iterator[str]:
    """Yield top-level text fragments from an assistant message.

    For escape-marker detection, callers should pipe each yielded string
    through `_strip_nudge_echoes` first to suppress hook-body false matches.
    """
    content = msg.get("content")
    if isinstance(content, str):
        yield content
        return
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text") or ""
                if t:
                    yield t


def iter_assistant_text_for_marker_scan(msg: dict) -> Iterator[str]:
    """Like `iter_assistant_text` but with nudge-body fingerprint stripping
    applied. Use this when scanning for the `[No KG update needed: ...]`
    escape marker to avoid self-suppression by quoted nudge examples.
    """
    for t in iter_assistant_text(msg):
        cleaned = _strip_nudge_echoes(t)
        if cleaned:
            yield cleaned


# ── helpers ─────────────────────────────────────────────────────────────────

def _iter_blocks(msg: dict) -> Iterator[dict]:
    """Yield each content block (dict) in an assistant or user message."""
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _content_chars_and_images(content) -> tuple[int, int]:
    """Sum text chars + flat-token-estimate for image blocks across a
    tool_result `content` field.

    Returns (text_chars, image_tokens).

    The field may be a string (treated as text) or a list of typed blocks
    {type: 'text'|'image'|'document', ...}.
    """
    if isinstance(content, str):
        return (len(content), 0)
    if not isinstance(content, list):
        return (0, 0)
    text_chars = 0
    image_tokens = 0
    for c in content:
        if not isinstance(c, dict):
            continue
        block_type = c.get("type")
        if block_type == "text":
            t = c.get("text")
            if isinstance(t, str):
                text_chars += len(t)
        elif block_type == "image":
            # Vision intake: flat per-image estimate. Even for large images,
            # Anthropic's pricing is per-tile and capped, not per-byte.
            image_tokens += IMAGE_BLOCK_TOKENS
        elif block_type == "document":
            # Document blocks: text-extracted content lives in source.data
            # (PDFs, etc.). Treat as text chars.
            src = c.get("source") or {}
            if isinstance(src, dict):
                data = src.get("data")
                if isinstance(data, str):
                    text_chars += len(data)
        else:
            # Unknown block types: try common fallbacks without exploding.
            inner = c.get("content")
            if isinstance(inner, str):
                text_chars += len(inner)
    return (text_chars, image_tokens)
