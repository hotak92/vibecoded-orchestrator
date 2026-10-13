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

Pure functions apart from the transcript loaders (file I/O plus one
process-global, lock-guarded parse cache); no async, no server.py import. The
transcript-loading and KG-position helpers are also here so the drain doesn't have to reach into
server.py for them (the modularity rule: one concern, one home).
"""

from __future__ import annotations

import json as _json
import os as _os
import threading as _threading
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path  # used in string annotations (load_messages arg)
from typing import Any as _Any

__all__ = [
    "DEFAULT_ANSWER_THRESHOLD_TOKENS",
    "TOOL_CONTENT_LIMIT",
    "KG_SEARCH_TOOLS",
    "load_messages",
    "load_messages_cached",
    "slim_message",
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


# Sentinel for "this line carries no message" (blank / unparseable). A JSON
# ``null`` line IS a message to the pre-W6-MEM loader (it appended ``None``), so
# ``None`` cannot double as the skip marker.
_NO_MESSAGE = object()


def _parse_line(raw: bytes) -> object:
    """Parse ONE transcript line; ``_NO_MESSAGE`` for blank/unparseable lines.

    The one line parser shared by the full loader and the incremental cache, so
    both agree on which lines become messages. ``json.loads`` takes the raw
    bytes (UTF-8/16/32 auto-detected); invalid JSON *and* undecodable bytes are
    skipped (both are ``ValueError`` subclasses) — a corrupt line never aborts
    the rest of the transcript.
    """
    line = raw.strip()
    if not line:
        return _NO_MESSAGE
    try:
        return _json.loads(line)
    except ValueError:
        return _NO_MESSAGE


def load_messages(transcript_path: "str | Path") -> list[dict]:
    """Load all JSONL messages from a transcript file, unmodified. Soft-fail to [].

    The full, faithful parse (every field of every line). The long-lived MCP
    monitor and the Stop-hook drain read through ``load_messages_cached``
    instead, which keeps only the fields the answer-window consumers read.
    """
    messages: list = []
    try:
        with open(transcript_path, "rb") as fh:
            for line in fh:
                msg = _parse_line(line)
                if msg is not _NO_MESSAGE:
                    messages.append(msg)
    except OSError:
        pass
    return messages


# ---- W6-MEM: slim messages — keep only what the consumers read --------------
#
# The consumers of a loaded transcript are exactly the functions of this module
# (``find_kg_positions``, ``extract_answer_window``, ``match_position_for_query``,
# ``match_position_by_timestamp``) plus ``rl_enrichment._rl_human_turn_after``.
# Between them they read: the message's ``type`` and ``timestamp``, and
# ``message.content`` — a string, or a block list whose blocks are read for
# ``type`` plus ``text`` / ``thinking`` / tool_use ``name`` + ``input``. Nothing
# else. Tool RESULTS (user-type ``tool_result`` blocks and the top-level
# ``toolUseResult`` echo), thinking signatures, usage, ids and every other
# envelope field are the bulk of a long transcript's bytes and are never read.
#
# Block LISTS keep their length (an unread block becomes a ``{"type": ...}``
# stub, never removed), so every (msg_idx, blk_idx) position is the same as in
# the full parse. Message objects that are not dicts are kept as they are, so a
# malformed transcript behaves exactly as it did under the full parse.
_KEPT_BLOCK_FIELDS: "dict[str, tuple[str, ...]]" = {
    "text": ("text",),
    "thinking": ("thinking",),
    "tool_use": ("name", "input"),
}
_CONTENT_MESSAGE_TYPES = frozenset({"assistant", "user"})


def _slim_block(block: _Any) -> _Any:
    if not isinstance(block, dict):
        return block
    out: dict = {}
    if "type" in block:
        out["type"] = block["type"]
    btype = block.get("type")
    for field in _KEPT_BLOCK_FIELDS.get(btype, ()) if isinstance(btype, str) else ():
        if field in block:
            out[field] = block[field]
    return out


def slim_message(msg: _Any) -> _Any:
    """Reduce a parsed transcript message to the fields the consumers read."""
    if not isinstance(msg, dict):
        return msg
    out: dict = {}
    for key in ("type", "timestamp"):
        if key in msg:
            out[key] = msg[key]
    if msg.get("type") in _CONTENT_MESSAGE_TYPES and "message" in msg:
        inner = msg["message"]
        if isinstance(inner, dict):
            slim_inner: dict = {}
            if "content" in inner:
                content = inner["content"]
                if isinstance(content, list):
                    content = [_slim_block(b) for b in content]
                slim_inner["content"] = content
            out["message"] = slim_inner
        else:
            out["message"] = inner
    return out


# ---- v0.2.73 Concern-B + W6-MEM: read-bounding, ONE shared home -------------
#
# The answer monitor (rl_enrichment._rl_answer_monitor) polls every
# _RL_MONITOR_POLL_INTERVAL seconds; the drain (rl_drain_citations) reads once
# per Stop. Per the modularity ruling the read-bounding lives in ONE place all
# callers share, not forked per caller.
#
# v0.2.73 added a process-global parse cache keyed on (mtime, size) that
# re-parsed the WHOLE file on any change and was bounded by entry COUNT only.
# On a 1.5 GB transcript that meant a multi-GB parsed list per entry, a full
# re-parse every time the busy transcript grew (the old list held until the new
# one was built), and — since the monitor called it inside the event loop —
# every MCP tool call stalled for the length of each parse (W6-MEM, 2026-10-13:
# one MCP process at 6.5 GB). The W6-MEM cache instead:
#
#   * parses INCREMENTALLY: Claude Code transcripts are append-only JSONL, so an
#     entry remembers the byte offset of the last complete line it consumed and
#     a grown file costs only its new bytes. It re-parses from byte 0 only when
#     the file was truncated or replaced: size below the consumed offset, a new
#     (st_dev, st_ino), or the byte before the offset no longer a newline;
#   * never consumes a partial final line (a writer mid-append): the fragment is
#     parsed tentatively and returned when it is complete JSON — exactly what a
#     full parse would return — but its bytes are re-read next time;
#   * stores SLIM messages (``slim_message``) and is bounded in estimated BYTES
#     (``memory_bounds.transcript_cache_max_bytes``) as well as in entries;
#   * is thread-safe: the monitor now runs the load in a worker thread
#     (``asyncio.to_thread``), so one lock serialises cache access — two
#     monitors polling the same transcript share one parse instead of racing.
#
# The returned list is the cached object and grows IN PLACE on later appends;
# that is safe for every consumer because the transcript is append-only (a
# later message never changes an earlier index). Pure-stdlib; identical on
# Windows (os.stat st_ino is a stable file id on NTFS).

# Entry-count cap, kept beside the byte cap: a long-lived MCP serving many small
# subagent transcripts is bounded by this, a few huge ones by the byte cap.
_MESSAGE_CACHE_MAX = 32


class _CacheEntry:
    """One transcript's incremental parse state."""

    __slots__ = ("file_id", "offset", "size", "mtime", "messages", "est_bytes",
                 "tail_msg")

    def __init__(self, file_id: "tuple[int, int]") -> None:
        self.file_id = file_id
        self.offset = 0          # bytes consumed (end of the last complete line)
        self.size = -1           # st_size at the last read
        self.mtime = -1.0        # st_mtime at the last read
        self.messages: list = []
        self.est_bytes = 0       # estimated retained bytes of ``messages``
        # The parsed partial final line (or _NO_MESSAGE); never counted as
        # consumed, so its bytes are re-read once the writer completes it.
        self.tail_msg: object = _NO_MESSAGE


# path(str) -> _CacheEntry; dict insertion order is the LRU order (a hit moves
# the key to the end).
_MESSAGE_CACHE: "dict[str, _CacheEntry]" = {}
_CACHE_LOCK = _threading.Lock()


def stat_signature(transcript_path: "str | Path") -> "tuple[float, int] | None":
    """Return ``(st_mtime, st_size)`` for a transcript, or None if unstattable.

    The cheap change-detector of the monitor's idle-poll short-circuit: two
    polls with the same signature saw the same bytes, so there is nothing new
    to read, parse, or recount. Soft-fail to None (treated as "changed /
    unknown" by callers so they never skip on error)."""
    try:
        st = _os.stat(transcript_path)
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _read_appended(entry: _CacheEntry, fh, start: int) -> None:
    """Consume complete lines from ``start``; remember a trailing fragment."""
    from claude_mcp_servers.rl_client.memory_bounds import approx_heap_bytes

    fh.seek(start)
    offset = start
    tail = b""
    for line in fh:
        if not line.endswith(b"\n"):
            tail = line
            break
        offset += len(line)
        msg = _parse_line(line)
        if msg is _NO_MESSAGE:
            continue
        slim = slim_message(msg)
        entry.messages.append(slim)
        entry.est_bytes += approx_heap_bytes(slim) + 8
    entry.offset = offset
    if tail:
        msg = _parse_line(tail)
        entry.tail_msg = _NO_MESSAGE if msg is _NO_MESSAGE else slim_message(msg)
    else:
        entry.tail_msg = _NO_MESSAGE


def _evict_locked(keep_key: str) -> None:
    """Drop least-recently-used entries over the count or byte cap.

    ``keep_key`` (the entry just served) is never evicted: dropping the
    transcript a monitor is polling would make the very next poll re-parse the
    whole file — the same memory transiently, plus the CPU, on every poll."""
    from claude_mcp_servers.rl_client.memory_bounds import transcript_cache_max_bytes

    cap = transcript_cache_max_bytes()
    total = sum(e.est_bytes for e in _MESSAGE_CACHE.values())
    for key in list(_MESSAGE_CACHE):
        if len(_MESSAGE_CACHE) <= _MESSAGE_CACHE_MAX and total <= cap:
            break
        if key == keep_key:
            continue
        total -= _MESSAGE_CACHE.pop(key).est_bytes


def load_messages_cached(transcript_path: "str | Path") -> list[dict]:
    """Slim transcript messages through the process-global incremental cache.

    ONE shared read-bounding home (Concern B, W6-MEM). Returns
    ``[slim_message(m) for m in load_messages(path)]`` — the same messages, in
    the same order, with the same block positions, carrying every field the
    answer-window consumers read — while parsing each appended byte once. All
    callers (MCP monitor, Stop-hook drain) route through this. Soft-fail: an
    unstattable path falls back to a plain uncached load; a read error drops
    the entry (the next call starts over) and returns what was parsed.
    """
    key = str(transcript_path)
    try:
        st = _os.stat(transcript_path)
    except OSError:
        return [slim_message(m) for m in load_messages(transcript_path)]
    file_id = (st.st_dev, st.st_ino)
    with _CACHE_LOCK:
        entry = _MESSAGE_CACHE.pop(key, None)
        try:
            if entry is not None and entry.size == st.st_size and entry.mtime == st.st_mtime \
                    and entry.file_id == file_id:
                pass  # unchanged since the last read — nothing to parse
            else:
                with open(transcript_path, "rb") as fh:
                    reuse = (
                        entry is not None
                        and entry.file_id == file_id
                        and st.st_size >= entry.offset
                    )
                    if reuse and entry is not None and entry.offset > 0:
                        fh.seek(entry.offset - 1)
                        reuse = fh.read(1) == b"\n"
                    if not reuse or entry is None:
                        entry = _CacheEntry(file_id)
                    _read_appended(entry, fh, entry.offset)
                entry.size = st.st_size
                entry.mtime = st.st_mtime
        except OSError:
            return list(entry.messages) if entry is not None else []
        _MESSAGE_CACHE[key] = entry
        _evict_locked(key)
        if entry.tail_msg is _NO_MESSAGE:
            return entry.messages
        return entry.messages + [entry.tail_msg]


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


def _chars_per_token_text() -> int:
    """The chars-per-token ratio, read from its ONE home (register #50).

    Imported lazily, at the use site, so this module's own import stays as
    pure as its docstring claims — the same shape
    ``vco_lib.embedding_service._chars_per_token_text`` and
    ``vco_lib.query_enrichment._approx_chars_per_token`` already use for this
    exact constant. No local fallback literal: a second copy is the thing
    being removed, and every context that can import ``answer_window`` can
    import its sibling ``weaviate_mcp.chunking`` (the drain reaches both
    through ``claude_mcp_servers``).
    """
    from claude_mcp_servers.weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT
    return CHARS_PER_TOKEN_TEXT


def token_estimate(text: str) -> int:
    """Char→token estimate. Cheap: arithmetic, no tokenizer, no Ollama call.

    Same unit as the chunker's ``TokenCounter`` (D16, v0.2.92: both are
    ``len(text) // CHARS_PER_TOKEN_TEXT``), so a drain-side estimate and an
    index-side count are always on the same scale. The RATIO comes from
    ``chunking``, which is a plain module-constant read.
    """
    return len(text) // _chars_per_token_text()


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
    threshold_chars = threshold_tokens * _chars_per_token_text()
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
