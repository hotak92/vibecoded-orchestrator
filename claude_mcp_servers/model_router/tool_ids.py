# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tool-id normalisation at the gateway boundary — request, response, SSE.

The deterministic rewrite itself lives in :mod:`vco_lib.transcript_repair`,
which is the SSOT shared with ``vco fix-transcript``: a transcript the gateway
never touched and a transcript repaired offline must come out identical, so
there is exactly one implementation and this module is the adapter that puts
it on the three code paths a proxy has.

  ============  ==========================================================
  path          what happens here
  ============  ==========================================================
  vendor        ``server_tool_use`` / ``tool_use`` ids are normalised so the
  RESPONSE      bytes that land in the CLIENT'S transcript already satisfy
                Anthropic's validator, and a ``server_tool_use`` naming a
                tool Anthropic cannot represent is stripped with its result.
                Fixing it here is what makes the session survivable: a
                transcript is append-only, so a poisoned turn is re-sent by
                every later request until the session is abandoned.
  vendor        the vendor is handed back its OWN ids, from the bounded map
  REQUEST       recorded on the way out — the gateway's rewriting is not the
                vendor's business, and a vendor that keys anything off its
                ids must not see ours.
  Anthropic     an OLDER session, poisoned before this code shipped, still
  REQUEST       carries the vendor ids. They are normalised in flight (and
                unrepresentable blocks stripped) so the user gets an answer
                instead of an opaque 400 they cannot act on.
  ============  ==========================================================

Streaming gets the same treatment through :class:`SseIdRewriter`, because the
ids arrive in ``content_block_start`` events and a streamed turn is the normal
case for Claude Code — a normaliser that only handled buffered JSON would fix
nothing in the field.

Vendor-neutral: nothing here names a vendor, a model or an endpoint.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from typing import Any, Iterator, MutableMapping, Optional

from vco_lib.transcript_repair import (
    RepairStats,
    TranscriptRepairer,
    restore_ids,
)

logger = logging.getLogger(__name__)

#: Ids remembered per vendor, newest last. Bounded because a long session
#: produces one entry per tool call and the process is long-lived: an
#: unbounded map is a slow leak with no upper bound but uptime.
DEFAULT_ID_MAP_SIZE = 4096

#: Stop transforming an SSE stream that has produced this much without a
#: single event boundary. A proxy must not be turned into a memory sink by a
#: malformed upstream; past this point the bytes are relayed verbatim, which
#: is the same thing the gateway did before this module existed.
SSE_BUFFER_LIMIT_BYTES = 32 * 1024 * 1024

#: SSE frame separator: two line terminators, where a terminator is CRLF, LF
#: **or a lone CR** — all three are line ends per the EventSource spec, and a
#: pattern that knows only ``\n`` turns a stream framed any other way into one
#: unsplittable buffer that grows to :data:`SSE_BUFFER_LIMIT_BYTES` and is
#: then relayed unrepaired.
#:
#: ``\r(?!\n)`` rather than a bare ``\r``, and the lookahead is load-bearing:
#: with a bare alternative the regex engine BACKTRACKS a single ``\r\n`` into
#: "CR, then LF" — two terminators — and every ordinary CRLF line ending in
#: the middle of an event reads as an event boundary. The lookahead says what
#: is actually meant: a CR is a terminator on its own only when an LF does not
#: follow it.
#:
#: The remaining ambiguity — a buffer ending in ``\r``, which is either a lone
#: CR or the first half of a CRLF whose second half has not arrived — cannot
#: be settled by any pattern and is resolved by waiting, in
#: :meth:`SseIdRewriter.feed`.
_EVENT_BOUNDARY = re.compile(rb"(?:\r\n|\r(?!\n)|\n){2}")

#: Largest JSON response body that is buffered for rewriting. Beyond it the
#: body is relayed unchanged (with a warning): a rewrite is worth a copy of a
#: chat response, not of an arbitrary payload.
JSON_BUFFER_LIMIT_BYTES = 8 * 1024 * 1024


class BoundedIdMap(MutableMapping[str, str]):
    """new-id -> original-id, capped, evicting the oldest insertion first.

    Only SERVER-tool ids are ever rewritten, so only they are in here: plain
    ``tool_use`` ids are left alone (Anthropic does not pattern-check them),
    which keeps this map to the handful of built-in calls in a session rather
    than one entry per tool call.

    That distinction matters for eviction, because a client re-sends its WHOLE
    history on every turn: an id from turn 3 is still on the wire at turn 300,
    so an entry's useful lifetime is the session, not the next request. FIFO
    is therefore the honest policy for a bounded map — the oldest ids are the
    ones a session is least likely to still be citing — and the cap only bites
    on a session with more than :data:`DEFAULT_ID_MAP_SIZE` server-tool calls,
    where the worst case is that the vendor sees a normalised id instead of
    its own, not a broken request.
    """

    def __init__(self, max_entries: int = DEFAULT_ID_MAP_SIZE) -> None:
        self._max = max(1, int(max_entries))
        self._data: "OrderedDict[str, str]" = OrderedDict()

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __setitem__(self, key: str, value: str) -> None:
        if key in self._data:
            self._data[key] = value
            return
        self._data[key] = value
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    @property
    def max_entries(self) -> int:
        return self._max


def normalise_vendor_response(
    payload: Any, id_map: MutableMapping[str, str],
) -> tuple[Any, RepairStats]:
    """Rewrite a vendor's buffered ``/v1/messages`` response in place-by-copy."""
    if not isinstance(payload, dict):
        return payload, RepairStats()
    repairer = TranscriptRepairer(id_map=id_map, strip_nonportable=True)
    # A Messages response IS the assistant turn, so the role is not in doubt
    # — and it decides whether a ``tool_result`` block may stay.
    content, changed = repairer.repair_content(
        payload.get("content"), role="assistant",
    )
    if not changed:
        return payload, repairer.stats
    patched = dict(payload)
    patched["content"] = content
    return patched, repairer.stats


def restore_vendor_ids(
    payload: Any, id_map: MutableMapping[str, str],
) -> tuple[Any, int]:
    """Undo our rewriting on the way back to the vendor that produced the ids."""
    if not id_map:
        return payload, 0
    return restore_ids(payload, id_map)


def sanitise_for_anthropic(payload: Any) -> tuple[Any, RepairStats]:
    """Make an inherited transcript acceptable to Anthropic's validator.

    ``id_map=None``: this direction is one-way. Nothing downstream will ever
    ask us to turn a normalised id back into the vendor id it came from, and
    remembering them would be a leak with no reader.
    """
    if not isinstance(payload, dict):
        return payload, RepairStats()
    repairer = TranscriptRepairer(
        id_map=None, strip_nonportable=True, strip_vendor_origin=True,
    )
    messages, changed = repairer.repair_messages(payload.get("messages"))
    if not changed:
        return payload, repairer.stats
    patched = dict(payload)
    patched["messages"] = messages
    return patched, repairer.stats


def _without_eol(line: bytes) -> bytes:
    """A line's content, with its terminator (CR, LF or CRLF) removed.

    ``rstrip`` of both characters is safe on a ``data:`` line because JSON
    escapes control characters, so a raw CR or LF cannot be the last byte of
    the payload itself.
    """
    return line.rstrip(b"\r\n")


def _line_eol(line: bytes) -> bytes:
    """The terminator ``line`` ends with — possibly empty (the last line)."""
    return line[len(_without_eol(line)):]


class _Unchanged:
    """Sentinel: emit the original bytes. Distinct from ``None`` = suppress."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<unchanged>"


_UNCHANGED = _Unchanged()


class SseIdRewriter:
    """Streaming counterpart of :func:`normalise_vendor_response`.

    Feed it upstream bytes, write what it returns. Three properties matter and
    each has a test:

    * **an event that needs no change is re-emitted VERBATIM** — the original
      bytes, not a re-serialisation — so a stream with nothing to fix is
      byte-identical to the passthrough the gateway did before;
    * **indexes stay contiguous.** Dropping a content block would otherwise
      leave a hole in the ``index`` sequence, and a client that treats indexes
      as positions rather than keys would mis-assemble the turn. Remaining
      blocks are renumbered, and every later ``content_block_delta`` /
      ``content_block_stop`` follows the same map;
    * **events for a dropped block are suppressed**, not emitted against a
      block the client never opened.
    """

    def __init__(
        self,
        *,
        id_map: Optional[MutableMapping[str, str]] = None,
        strip_nonportable: bool = True,
    ) -> None:
        self._repairer = TranscriptRepairer(
            id_map=id_map, strip_nonportable=strip_nonportable,
        )
        self._buffer = b""
        self._index_map: dict[int, int] = {}
        self._dropped_indexes: set[int] = set()
        self._next_index = 0
        self._passthrough = False

    @property
    def stats(self) -> RepairStats:
        return self._repairer.stats

    @property
    def blocks_suppressed(self) -> int:
        return len(self._dropped_indexes)

    def _drain(self, *, final: bool) -> bytearray:
        """Split every COMPLETE event out of the buffer and rewrite it.

        ``final`` says whether more bytes can still arrive. It settles the
        one thing a pattern cannot: a buffer ending in ``\\r`` is either a
        lone-CR terminator or the first half of a CRLF. Mid-stream the answer
        is "wait one chunk"; at EOF nothing more is coming, so the CR IS a
        terminator and the last event is split and rewritten like every other
        one.
        """
        out = bytearray()
        while True:
            match = _EVENT_BOUNDARY.search(self._buffer)
            if match is None:
                break
            if (
                not final
                and match.end() == len(self._buffer)
                and self._buffer.endswith(b"\r")
            ):
                # Ambiguous until the next byte. Waiting one chunk costs
                # nothing; guessing splits a CRLF in half and puts a stray LF
                # at the head of the next event.
                break
            event = self._buffer[: match.start()]
            separator = match.group(0)
            self._buffer = self._buffer[match.end():]
            rewritten = self._rewrite_event(event)
            # A suppressed event takes its separator with it: emitting the
            # blank line alone would put a stray empty event on the wire.
            # The ORIGINAL separator is re-emitted, so a CRLF stream stays
            # CRLF — matching only "\n\n" would have buffered a CRLF stream
            # to the 32 MB limit and then relayed it unrepaired.
            if rewritten:
                out += rewritten + separator
        return out

    def feed(self, chunk: bytes) -> bytes:
        """Consume upstream bytes; return the bytes to write to the client."""
        if self._passthrough:
            return chunk
        self._buffer += chunk
        out = self._drain(final=False)
        if len(self._buffer) > SSE_BUFFER_LIMIT_BYTES:
            logger.warning(
                "model-gateway: SSE event exceeded %d bytes without a "
                "boundary; relaying the rest of this stream unchanged",
                SSE_BUFFER_LIMIT_BYTES,
            )
            self._passthrough = True
            out += self._buffer
            self._buffer = b""
        return bytes(out)

    def flush(self) -> bytes:
        """Whatever is left when the upstream closed. Never dropped silently.

        EOF is information: it removes the trailing-CR ambiguity :meth:`feed`
        waits on, so the split runs once more with the lone-CR reading and a
        CR-framed stream's LAST event is rewritten like every other one
        rather than relayed unrepaired. Whatever remains after that — a
        genuinely incomplete event, or the whole buffer once the stream went
        to passthrough — is emitted VERBATIM: an unrewritten tail is a
        cosmetic id, a dropped one is a truncated turn.
        """
        if not self._passthrough:
            out = self._drain(final=True)
        else:
            out = bytearray()
        tail, self._buffer = self._buffer, b""
        return bytes(out) + tail

    # ── one event ────────────────────────────────────────────────────────
    def _rewrite_event(self, raw: bytes) -> bytes:
        # keepends: every line carries its OWN terminator, so an untouched
        # line is re-emitted byte for byte and a rewritten one keeps the
        # ending its neighbours use. splitlines also splits on a lone CR,
        # which is the whole point of the boundary change beside it.
        lines = raw.splitlines(keepends=True)
        data_positions = [
            i for i, line in enumerate(lines)
            if _without_eol(line).startswith(b"data:")
        ]
        if not data_positions:
            return raw
        joined = "\n".join(
            _without_eol(lines[i])[len(b"data:"):].strip().decode("utf-8", "replace")
            for i in data_positions
        )
        try:
            payload = json.loads(joined)
        except ValueError:
            return raw
        if not isinstance(payload, dict):
            return raw

        verdict = self._transform(payload)
        if verdict is _UNCHANGED:
            return raw
        if verdict is None:
            return b""  # suppressed; the caller drops the separator too
        return self._reserialise(lines, data_positions, verdict)

    @staticmethod
    def _reserialise(
        lines: list[bytes], data_positions: list[int], payload: dict,
    ) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        first = data_positions[0]
        # Keep THIS line's own ending, whatever it is: in a CRLF stream every
        # other line still carries its \r, and a single LF line in the middle
        # is the kind of inconsistency a strict parser is entitled to reject.
        # The lines already carry their terminators (keepends), so the join is
        # empty and an untouched line round-trips exactly.
        eol = _line_eol(lines[first])
        rest = set(data_positions[1:])
        out = []
        for i, line in enumerate(lines):
            if i == first:
                out.append(b"data: " + body + eol)
            elif i in rest:
                continue
            else:
                out.append(line)
        return b"".join(out)

    def _transform(self, payload: dict) -> Any:
        """Return the new payload, ``_UNCHANGED``, or ``None`` to suppress."""
        kind = payload.get("type")
        if kind == "content_block_start":
            return self._on_block_start(payload)
        if kind in ("content_block_delta", "content_block_stop"):
            return self._on_indexed(payload)
        return _UNCHANGED

    def _on_block_start(self, payload: dict) -> Any:
        original_index = payload.get("index")
        block = payload.get("content_block")
        repaired, changed = self._repairer.repair_content(
            [block], role="assistant",
        )
        if not repaired:
            if isinstance(original_index, int):
                self._dropped_indexes.add(original_index)
            return None
        out_index = self._next_index
        self._next_index += 1
        renumbered = (
            isinstance(original_index, int) and original_index != out_index
        )
        if isinstance(original_index, int):
            self._index_map[original_index] = out_index
        if not changed and not renumbered:
            return _UNCHANGED
        patched = dict(payload)
        patched["content_block"] = repaired[0]
        if isinstance(original_index, int):
            patched["index"] = out_index
        return patched

    def _on_indexed(self, payload: dict) -> Any:
        original_index = payload.get("index")
        if not isinstance(original_index, int):
            return _UNCHANGED
        if original_index in self._dropped_indexes:
            return None
        mapped = self._index_map.get(original_index, original_index)
        if mapped == original_index:
            return _UNCHANGED
        patched = dict(payload)
        patched["index"] = mapped
        return patched


__all__ = [
    "DEFAULT_ID_MAP_SIZE",
    "RepairStats",
    "JSON_BUFFER_LIMIT_BYTES",
    "SSE_BUFFER_LIMIT_BYTES",
    "BoundedIdMap",
    "SseIdRewriter",
    "normalise_vendor_response",
    "restore_vendor_ids",
    "sanitise_for_anthropic",
]
