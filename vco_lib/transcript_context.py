# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared transcript reader for hook retrieval-query enrichment (WP-E, v0.2.92).

ONE home for "recover the agent's recent conversational text from a Claude
Code JSONL transcript" — consumed by ``vco_lib.query_enrichment`` and, via
that module, by hooks, the MCP server, and CLI scripts alike (CLAUDE.md
"A>B>C" cross-language-sharing rule: this is option A, a single Python
implementation invoked in-process by every Python consumer and via a thin
CLI/subprocess boundary by shell hooks — never mirrored into bash/PowerShell
logic).

PRIVACY (binding): transcript content is user/agent conversation data under
the SAME handling discipline CLAUDE.md prescribes for secrets — never
logged, never printed to stderr, never placed in argv, never persisted to a
cache blob, never included in a telemetry record. This module itself never
logs the text it reads; on error it reports only structural facts (nothing
found), never partial content or exception payloads that might embed it.

ASYNC-WRITE CAVEAT: Claude Code writes the transcript file asynchronously,
so on PreToolUse the file can lag the live conversation by one turn — the
hook may fire before the newest assistant turn (or even the newest user
turn) has been flushed to disk. ``TurnContext.lagging`` names this
explicitly rather than leaving callers to assume freshness they cannot get.
It is set in BOTH incomplete-tail shapes — never signalled as an error, only
as "treat this as possibly stale/absent, don't rely on it":

  1. nothing could be recovered at all (empty/absent/unparseable transcript,
     the steady state for a brand-new session) → the synthetic empty result;
  2. the newest user prompt IS on disk but the assistant turn answering it
     is not yet — the COMMON PreToolUse shape, since the first tool call of
     a turn fires before any of that turn's assistant text exists. The
     newest prompt is then the freshest signal available, so it is carried
     as ``user_prompt`` and the PREVIOUS complete turn (when one is inside
     the read window) supplies the assistant text/thinking fallback.

Case 2 is the reason ``lagging`` cannot be inferred from "the result is
empty": before v0.2.92 that shape returned the previous COMPLETE turn with
``lagging=False`` and silently dropped the freshly-read prompt, so callers
enriched with the task the user had just left and had no signal saying so.
The stale turn is never returned as if it were current.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterator, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]

# Recent turns are always near the tail of the file. Bounding the read to
# the last N bytes keeps every hook invocation O(tail_bytes), never O(file
# size) — a multi-hundred-MB transcript must not turn a PreToolUse hook
# into a multi-second stall.
DEFAULT_TAIL_BYTES = 512 * 1024


@dataclass
class TurnContext:
    """One (user_prompt, assistant_output) pairing recovered from the tail.

    ``assistant_text`` / ``assistant_thinking`` are that turn's respective
    content-block types concatenated in transcript order; either may be
    empty (e.g. a turn that only issued tool calls). ``tool_use`` /
    ``tool_result`` blocks are never read into either field — enrichment
    material is conversational text only, per the WP-E spec ("thinking or
    chat, not tools").
    """

    user_prompt: str = ""
    assistant_text: str = ""
    assistant_thinking: str = ""
    assistant_uuid: Optional[str] = None
    lagging: bool = False


def _read_tail(transcript_path: Optional[PathLike], tail_bytes: int) -> str:
    """Read up to the last ``tail_bytes`` bytes of a file, decoded lossily.

    Never raises: any missing path, permission error, or I/O failure yields
    ``""`` — callers treat that identically to "no transcript available".
    """
    if not transcript_path:
        return ""
    try:
        path = os.fspath(transcript_path)
    except (TypeError, ValueError):
        return ""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    try:
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                # The first partial line after an arbitrary byte-seek is
                # very likely a truncated JSON object — drop it rather
                # than feed a broken line to json.loads on every call.
                fh.readline()
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _iter_blocks(msg: dict) -> Iterator[dict]:
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _user_prompt_text(msg: dict) -> Optional[str]:
    """Return the plain-text prompt for a genuine (human-typed) user turn.

    Claude Code also emits ``role: "user"`` entries that are purely a
    ``tool_result`` echo fed back into the model — those are NOT prompts a
    human typed, so they return ``None`` rather than being mistaken for
    one.
    """
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        texts = [b.get("text") or "" for b in blocks if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        return joined or None
    return None


def iter_turns(
    transcript_path: Optional[PathLike],
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    max_turns: Optional[int] = None,
) -> Iterator[TurnContext]:
    """Yield ``TurnContext`` objects from the transcript tail, NEWEST FIRST.

    Each turn pairs one assistant turn's text/thinking with the user prompt
    that most recently preceded it within the read window (so a tail read
    that starts mid-conversation still pairs turns correctly relative to
    each other, even though older context outside the window is simply
    absent — never fabricated).

    When the tail ends on a user prompt that no assistant entry follows —
    the async-write lag case 2 in the module docstring — the NEWEST yielded
    turn is a synthetic ``lagging=True`` turn carrying that prompt, with the
    previous complete turn's assistant text/thinking as fallback body. The
    previous turn is never yielded first in that shape, because yielding it
    would present last turn's material as this turn's.

    Never raises: any parse/IO problem yields an empty generator, which is
    semantically identical to "no context available" for every caller.
    ``max_turns`` (when given) caps how many of the newest turns are
    yielded before the generator stops early.
    """
    raw = _read_tail(transcript_path, tail_bytes)
    if not raw:
        return

    entries: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("isSidechain") is True:
            # Sub-agent sidechain entries are not the primary agent's own
            # output — excluded, mirroring claude_token_counter.py's guard.
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        entries.append(entry)

    pending_prompt = ""
    # True while the newest genuine user prompt has no assistant entry after
    # it — the async-write lag shape (module docstring case 2).
    pending_unanswered = False
    turns: list[TurnContext] = []
    current_text: list[str] = []
    current_thinking: list[str] = []
    current_uuid: Optional[str] = None
    have_current = False

    def _flush() -> None:
        nonlocal have_current, current_text, current_thinking, current_uuid
        if have_current:
            turns.append(
                TurnContext(
                    user_prompt=pending_prompt,
                    assistant_text="\n".join(current_text).strip(),
                    assistant_thinking="\n".join(current_thinking).strip(),
                    assistant_uuid=current_uuid,
                    lagging=False,
                )
            )
        current_text = []
        current_thinking = []
        current_uuid = None
        have_current = False

    for entry in entries:
        msg = entry["message"]
        role = msg.get("role")
        if role == "user":
            prompt = _user_prompt_text(msg)
            if prompt is not None:
                _flush()
                pending_prompt = prompt
                pending_unanswered = True
            continue
        if role == "assistant":
            have_current = True
            pending_unanswered = False
            current_uuid = entry.get("uuid") or current_uuid
            for block in _iter_blocks(msg):
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text") or ""
                    if t:
                        current_text.append(t)
                elif btype == "thinking":
                    t = block.get("thinking") or block.get("text") or ""
                    if t:
                        current_thinking.append(t)
                # tool_use / tool_result blocks are intentionally skipped —
                # never read into either field.
    _flush()

    if pending_unanswered:
        # Async-write lag (module docstring case 2): the freshly-read prompt
        # is the NEWEST thing on disk, so it becomes the newest turn rather
        # than being discarded in favour of the previous complete turn. That
        # previous turn — when the read window holds one — still supplies the
        # assistant body, which is the documented fallback; what it must NOT
        # do is supply the user prompt, because the user has already moved on
        # from it. ``lagging`` marks the whole result as not-yet-complete.
        previous = turns[-1] if turns else TurnContext()
        turns.append(
            TurnContext(
                user_prompt=pending_prompt,
                assistant_text=previous.assistant_text,
                assistant_thinking=previous.assistant_thinking,
                assistant_uuid=previous.assistant_uuid,
                lagging=True,
            )
        )

    if not turns:
        return

    count = 0
    for turn in reversed(turns):
        if max_turns is not None and count >= max_turns:
            return
        yield turn
        count += 1


def last_turn_context(
    transcript_path: Optional[PathLike],
    *,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> TurnContext:
    """Convenience: the single most recent turn, or an empty ``lagging`` result.

    Every incomplete-tail shape returns ``lagging=True``, never an
    exception:

      * no transcript / no recoverable content at all → the synthetic EMPTY
        ``TurnContext(lagging=True)``. Callers treat it as "nothing to add",
        never as a failure to surface.
      * the common PreToolUse case, the newest turn not yet flushed by the
        async writer → a POPULATED ``lagging=True`` turn carrying that
        newest user prompt, with the previous complete turn's assistant
        text/thinking as the fallback body (see :func:`iter_turns`).
    """
    for turn in iter_turns(transcript_path, tail_bytes=tail_bytes, max_turns=1):
        return turn
    return TurnContext(lagging=True)
