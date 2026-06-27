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

__all__ = [
    "DEFAULT_ANSWER_THRESHOLD_TOKENS",
    "TOOL_CONTENT_LIMIT",
    "KG_SEARCH_TOOLS",
    "load_messages",
    "find_kg_positions",
    "extract_answer_window",
    "match_position_for_query",
    "token_estimate",
]

# Default accumulation/truncation CAP, in tokens. The citation gate uses the
# same value (see weaviate_mcp.server._RL_MIN_ANSWER_TOKENS_FOR_CITATION). 1
# token ≈ 4 chars (qwen3 BPE empirical average).
DEFAULT_ANSWER_THRESHOLD_TOKENS: int = 25_000
# Per tool_use input cap, in chars — bounds any single tool call's contribution.
TOOL_CONTENT_LIMIT: int = 20_000

# KG search tool names as they appear in transcripts (with + without prefix).
KG_SEARCH_TOOLS: frozenset[str] = frozenset({
    "hybrid_search", "semantic_graph_search",
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
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
