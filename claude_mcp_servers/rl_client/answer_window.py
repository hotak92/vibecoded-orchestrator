# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Answer-window extraction for RL citation detection — shared home (v0.2.70).

ONE home for the logic that turns a Claude Code session transcript into the
"answer window" the citation cosine is computed against. Used by BOTH:

  * the in-process MCP monitor (``weaviate_mcp.server._rl_answer_monitor``),
    which shrinks to a thin caller of these helpers, and
  * the turn-end Stop-hook drain (``scripts/rl_drain_citations.py``), which
    recovers hook-path citations the doomed asyncio monitor never could.

D5.1 signal contract (do NOT change): the "answer" = assistant TEXT + THINKING +
tool_use INPUT only. Tool RETURNS are EXCLUDED — RL trains on what the LLM
EMITS, not on shell output / file dumps / API JSON that would drown the signal.

Pure functions: no I/O, no async, no server.py import. The transcript-loading
and KG-position helpers are also here so the drain doesn't have to reach into
server.py for them (the modularity rule: one concern, one home).
"""

from __future__ import annotations

import json as _json
import os as _os
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path  # used in string annotations (load_messages arg)

__all__ = [
    "DEFAULT_ANSWER_THRESHOLD_TOKENS",
    "TOOL_CONTENT_LIMIT",
    "KG_SEARCH_TOOLS",
    "load_messages",
    "load_messages_cached",
    "stat_signature",
    "find_kg_positions",
    "extract_answer_window",
    "match_position_for_query",
    "match_position_by_timestamp",
    "token_estimate",
]

# Default accumulation/truncation CAP, in tokens. The citation gate uses the
# same value (see weaviate_mcp.server._RL_MIN_ANSWER_TOKENS_FOR_CITATION). 1
# token ≈ 4 chars (qwen3 BPE empirical average).
DEFAULT_ANSWER_THRESHOLD_TOKENS: int = 25_000
# Per tool_use input cap, in chars — bounds any single tool call's contribution.
TOOL_CONTENT_LIMIT: int = 20_000

# KG search tool names as they appear in transcripts (with + without prefix).
# v0.2.73 RL-2: the CODE search tool is included so code retrievals get
# transcript positions (find_kg_positions) and become citable once code
# citation staging lands. Structural lookups (query_code_structure) are
# deliberately excluded — they carry no semantic candidates to cite.
KG_SEARCH_TOOLS: frozenset[str] = frozenset({
    "hybrid_search", "semantic_graph_search",
    "search_code_graph",
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
    "mcp__weaviate-kg__search_code_graph",
})


def load_messages(transcript_path: "str | Path") -> list[dict]:
    """Load all JSONL messages from a transcript file. Soft-fail to []."""
    messages: list[dict] = []
    try:
        with open(transcript_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(_json.loads(line))
                except _json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return messages


# ---- v0.2.73 Concern-B: read-bounding, ONE shared home ---------------------
#
# The answer monitor (rl_enrichment._rl_answer_monitor) polls every
# _RL_MONITOR_POLL_INTERVAL seconds and, pre-Concern-B, re-read + re-JSON-parsed
# the ENTIRE (growing) transcript on EVERY poll — O(file_size × poll_count) on a
# long session, most of it wasted on IDLE polls where Claude produced no new
# output. The drain (rl_drain_citations) reads once per Stop, and the paid
# online-training path sits downstream of the same window — but per the
# modularity ruling the read-bounding lives in ONE place all callers share, not
# forked per caller.
#
# The fix is a process-global parse cache keyed on the file's (mtime, size)
# signature: an UNCHANGED transcript returns the already-parsed message list
# without re-reading or re-parsing a single byte; a CHANGED transcript re-parses
# in full and refreshes the cache. Because a cache hit returns the SAME parsed
# messages a full ``load_messages`` would, and a miss falls straight through to
# ``load_messages``, the extracted answer window is BYTE-IDENTICAL to the
# pre-Concern-B full-read path for every caller (monitor / drain / online).
# Pure-stdlib; no paid-module import; identical on Windows (os.stat is portable).

# path(str) -> (mtime, size, messages). Bounded so a long-lived MCP subprocess
# serving many transcripts can't grow it without limit (LRU-ish: insertion-order
# pop of the oldest entry once over the cap). The cache holds parsed lists, which
# dominate memory, so the cap is modest.
_MESSAGE_CACHE: "dict[str, tuple[float, int, list[dict]]]" = {}
_MESSAGE_CACHE_MAX = 32


def stat_signature(transcript_path: "str | Path") -> "tuple[float, int] | None":
    """Return ``(st_mtime, st_size)`` for a transcript, or None if unstattable.

    The cheap change-detector shared by the monitor's idle-poll short-circuit
    and the cached loader below: two polls with the same signature saw the same
    bytes, so there is nothing new to read, parse, or recount. Soft-fail to None
    (treated as "changed / unknown" by callers so they never skip on error)."""
    try:
        st = _os.stat(transcript_path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def load_messages_cached(transcript_path: "str | Path") -> list[dict]:
    """``load_messages`` with a process-global (mtime, size) parse cache.

    ONE shared read-bounding home (Concern B). Returns byte-identical parsed
    messages to ``load_messages`` — a cache HIT hands back the list a full
    re-parse would have produced (the file is unchanged), a MISS re-parses in
    full and refreshes the cache. All callers (MCP monitor, Stop-hook drain,
    online-training path) route through this so none re-parses an unchanged
    transcript. Soft-fail: an unstattable path falls back to a plain
    ``load_messages`` (never caches, never skips).
    """
    key = str(transcript_path)
    sig = stat_signature(transcript_path)
    if sig is not None:
        cached = _MESSAGE_CACHE.get(key)
        if cached is not None and (cached[0], cached[1]) == sig:
            return cached[2]
    messages = load_messages(transcript_path)
    if sig is not None:
        _MESSAGE_CACHE[key] = (sig[0], sig[1], messages)
        # LRU bound — pop oldest insertion-order entry until size <= max.
        while len(_MESSAGE_CACHE) > _MESSAGE_CACHE_MAX:
            _MESSAGE_CACHE.pop(next(iter(_MESSAGE_CACHE)))
    return messages


def find_kg_positions(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (msg_idx, blk_idx) for every KG-search tool_use block."""
    positions: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        for blk_idx, block in enumerate(content):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in KG_SEARCH_TOOLS
            ):
                positions.append((msg_idx, blk_idx))
    return positions


def token_estimate(text: str) -> int:
    """Char→token estimate (1 token ≈ 4 chars). Cheap, dependency-free.

    The MCP path prefers the real ``TokenCounter`` when available; this is the
    portable fallback the drain uses without importing the chunker.
    """
    return len(text) // 4


def extract_answer_window(
    messages: list[dict],
    start_msg_idx: int,
    start_blk_idx: int,
    *,
    threshold_tokens: int = DEFAULT_ANSWER_THRESHOLD_TOKENS,
    tool_content_limit: int = TOOL_CONTENT_LIMIT,
) -> tuple[str, bool]:
    """Extract Claude's answer after the KG search at (start_msg_idx, start_blk_idx).

    Accumulates, from the search position to end-of-transcript:
      * all ``text`` blocks,
      * all ``thinking`` blocks (useful RL scratch signal),
      * for EVERY ``tool_use`` block: ``"<name>: <json(input)>"`` (tool
        OUTPUTS / ``toolUseResult`` are excluded — they live on user-type
        messages which are skipped).

    Human turns are NOT a stop condition — subsequent assistant blocks count as
    the SAME accumulation (the V52-N behaviour the F-QUEUE accumulate-don't-drop
    ruling depends on: the durable pending file keeps growing across turns).

    Stop conditions (whichever first):
      * token-equivalent accumulation ≥ ``threshold_tokens`` → (truncated, True)
      * end of transcript → (window, False)  ← caller leaves the pending file
        for the next Stop so a longer accumulation can still cite.

    Returns (text, complete).
    """
    parts: list[str] = []
    total_chars = 0
    threshold_chars = threshold_tokens * 4
    for msg_idx in range(start_msg_idx, len(messages)):
        msg = messages[msg_idx]
        if msg.get("type", "") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        for blk_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            # Skip blocks up to and including the search tool_use itself.
            if msg_idx == start_msg_idx and blk_idx <= start_blk_idx:
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)
                    total_chars += len(text)
            elif btype == "thinking":
                text = block.get("thinking", "")
                if text:
                    parts.append(text)
                    total_chars += len(text)
            elif btype == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                try:
                    input_serialized = _json.dumps(tool_input, default=str)
                except Exception:
                    input_serialized = str(tool_input)
                snippet = f"{tool_name}: {input_serialized}"[:tool_content_limit]
                if snippet:
                    parts.append(snippet)
                    total_chars += len(snippet)
            if total_chars >= threshold_chars:
                return "".join(parts)[:threshold_chars], True

    return "".join(parts), False


def match_position_for_query(
    messages: list[dict],
    kg_positions: list[tuple[int, int]],
    query_snippet: str,
    pos_idx: "int | None" = None,
) -> "tuple[int, int] | None":
    """Map a staged (query, seq) to its KG-call position in the transcript.

    Ported verbatim from the in-MCP monitor's matcher so the drain and the
    monitor agree byte-for-byte on which answer window belongs to which
    retrieval. Primary key = query fingerprint; ``pos_idx`` (0-based seq) is the
    tiebreak when the same query appears multiple times (parallel chats /
    repeated searches). Falls back to the last query match, then to the seq
    index when no query snippet is available.
    """
    if query_snippet:
        query_matches: list[tuple[int, int, int]] = []
        for i, (mi, bi) in enumerate(kg_positions):
            try:
                blk = messages[mi].get("message", {}).get("content", [])[bi]
                blk_query = (
                    blk.get("input", {}).get("query", "")
                    if isinstance(blk, dict) else ""
                )
            except (IndexError, AttributeError, TypeError):
                continue
            if query_snippet in blk_query or blk_query in query_snippet:
                query_matches.append((i, mi, bi))
        if query_matches:
            if pos_idx is not None:
                exact = [(i, mi, bi) for (i, mi, bi) in query_matches if i == pos_idx]
                if exact:
                    return (exact[0][1], exact[0][2])
            return (query_matches[-1][1], query_matches[-1][2])
        return None
    if pos_idx is not None and pos_idx < len(kg_positions):
        return kg_positions[pos_idx]
    return None


def _message_ts_ms(msg: dict) -> "int | None":
    """Epoch-ms of a transcript message's top-level ISO-8601 ``timestamp``.

    Claude Code stamps every transcript line with e.g. ``2026-07-11T06:10:02.138Z``.
    Returns the value in epoch milliseconds, or None when the field is absent /
    unparseable (soft-fail — the caller treats None as "no timestamp signal")."""
    ts = msg.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # ``fromisoformat`` handles the trailing ``Z`` only from Py3.11; normalise
        # it to ``+00:00`` so older interpreters parse the same string.
        norm = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        dt = _datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def match_position_by_timestamp(
    messages: list[dict],
    ts_ms: "int | float | None",
) -> "tuple[int, int] | None":
    """Anchor an answer window by TIME rather than by transcript query-match.

    The hook-cohort fallback (v0.2.77 9-bis): a hook-staged retrieval carries a
    hook-derived query that never appears as a KG ``tool_use`` in the transcript,
    so ``match_position_for_query`` can never locate it and its citation label
    is lost. When that match fails, anchor instead on the FIRST assistant message
    whose transcript ``timestamp`` is at/after the retrieval's ``ts_ms`` — that is
    the start of the answer the retrieval fed into. Returns ``(msg_idx, -1)`` so
    ``extract_answer_window`` accumulates from block 0 of that message onward (its
    skip predicate is ``blk_idx <= start_blk_idx``; ``-1`` skips nothing). The
    existing 25k gate + terminal floor in the drain then apply UNCHANGED — this
    only changes WHERE the window starts, never whether/when it is emitted.

    Returns None when ``ts_ms`` is missing or no assistant message is stamped
    at/after it (e.g. the answer isn't flushed yet — the drain leaves the file to
    retry, exactly as an unmatched query does today).
    """
    if not isinstance(ts_ms, (int, float)) or ts_ms <= 0:
        return None
    for msg_idx, msg in enumerate(messages):
        if msg.get("type") != "assistant":
            continue
        msg_ts = _message_ts_ms(msg)
        if msg_ts is not None and msg_ts >= ts_ms:
            return (msg_idx, -1)
    return None
