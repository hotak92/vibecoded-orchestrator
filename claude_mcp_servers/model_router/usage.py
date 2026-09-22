# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Per-chat token accounting, read off the bytes the gateway already relays.

**What this is for: CONTEXT, not cost.** The owner's requirement (2026-09-16)
is "the overall amount of tokens present in the current chat's context", per
chat, "even in presence of subagents or multiple chats going through the
gateway (each chat gets its own monitoring)" — for EVERY model the gateway
can reach, first-party or vendor. Cost telemetry was deliberately removed from
this daemon in the same cycle; nothing here prices anything, and no price
table may be added to it.

The key is a HEADER, not the body. Claude Code sends
``x-claude-code-session-id`` on every request (documented as the way "to
aggregate all requests from one session without parsing request bodies"),
``x-claude-code-agent-id`` on a subagent's requests and
``x-claude-code-parent-agent-id`` on a nested one. ``metadata.user_id`` — the
field a body-parsing implementation would reach for — carries no session id at
all, which is why this module never opens a request body to find one. Two
chats and their subagents therefore separate correctly and for free.

**The merge rule is the whole subtlety.** Anthropic's streaming API puts
``input_tokens`` / ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
(and an initial ``output_tokens``) in ``message_start``, and the FINAL
``output_tokens`` in ``message_delta`` — where newer API versions may repeat
the input fields. The Anthropic-compatible endpoint of at least one shipped
vendor reports ZEROS in ``message_start`` and the real figures only in
``message_delta`` (third-party evidence; that endpoint's usage reporting is
undocumented). One reader cannot prefer either event, so
:class:`UsageAccumulator` merges them FIELD BY FIELD, taking the last non-null
value with one exception that both shapes need: **a later zero never overwrites
an earlier positive, and a later positive always replaces an earlier zero.**
Read either event alone and one of the two vendors reports nothing.

Two invariants this module owes the relay, both structural rather than
promised:

* **it never alters or delays a byte.** :meth:`UsageAccumulator.feed` takes a
  copy of what is already on its way to the client and returns nothing; the
  caller writes the same bytes it would have written if this module did not
  exist;
* **it never breaks a request.** Every entry point is called through
  :func:`model_router.server._guarded`, which abandons the pass on any
  exception, and the ledger's disk write happens off the handler's path — a
  full disk costs a row, never an answer.

The ledger is append-only JSONL under VCO's one metrics home
(``vco_lib.paths.vct_metrics_dir()`` — the same directory as
``failures.jsonl`` and ``compactions.jsonl``, steered by ``$VCT_STATE_DIR``),
truncated to its newest rows past :data:`LEDGER_MAX_BYTES` (through
``vco_lib.atomic.rotate_tail_lines``, the one home for that) so an always-on
daemon cannot fill a disk.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# The SSE framing helpers live in ``tool_ids`` because the tool-id rewriter
# needed them first, and they are imported rather than re-typed: the boundary
# pattern in particular encodes two paid-for lessons (a lone CR is a
# terminator; a bare ``\r`` alternative backtracks a CRLF into two of them),
# and a second copy would be a second place for the next reader to get that
# wrong. v0.2.95 closed the last of it: the split LOOP moved there too
# (:func:`model_router.tool_ids.split_sse_frames`), so the rewriter and this
# reader share one implementation instead of one pattern and two loops.
from .routing import ONE_M_SUFFIX
from .tool_ids import _without_eol, split_sse_frames

logger = logging.getLogger(__name__)

#: The four usage fields both upstreams speak, in the order a reader thinks
#: about them: what was sent, what was cached, what came back.
USAGE_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)

#: The window the CLIENT budgets against, keyed on the id it requested. Claude
#: Code reads nothing else from a gateway's catalog to size its context bar:
#: an ``[1m]`` suffix means one million, anything else behind a custom base URL
#: means two hundred thousand. Both are the CLIENT's arithmetic, which is why
#: they are constants here and not a lookup — a record that reported the real
#: window only could not answer "why did compaction fire at 20%?".
CLIENT_WINDOW_1M = 1_000_000
CLIENT_WINDOW_DEFAULT = 200_000

#: Where a completed request's row lands, under :func:`_metrics_dir`.
LEDGER_BASENAME = "gateway-usage.jsonl"

#: Rotate past this. A row with every field populated measures
#: :data:`LEDGER_ROW_BYTES`, so this is ~96k requests — months of ordinary use
#: — and it is bounded at all because the daemon is long-lived and nothing
#: else prunes it. ONE file, never a ``.1`` sibling: this is a context
#: monitor, not an archive, and a scheme that keeps N generations is a
#: disk-usage decision the user never made.
LEDGER_MAX_BYTES = 50 * 1024 * 1024

#: One fully-populated row, measured — every field set, a UUID session id and
#: a UUID agent id, a namespaced ``[1m]`` model id. Used only to turn a byte
#: cap into a LINE count for :func:`vco_lib.atomic.rotate_tail_lines`, which
#: keeps lines rather than bytes.
LEDGER_ROW_BYTES = 545

#: What fraction of :data:`LEDGER_MAX_BYTES` survives a rotation. A quarter:
#: enough recent history that a monitor restarted after a rotation still has
#: context to show, and small enough that rotations are rare rather than one
#: per write. At the default cap that is ``(52428800 // 4) // 545`` =
#: **24 049 lines**, about 13.1 MiB — a quarter of the cap, as intended.
#:
#: The ratio holds for a TYPICAL row. An atypically long client-supplied model
#: id makes the surviving tail proportionally larger; the file is still
#: bounded, because the next append re-checks the cap.
LEDGER_KEEP_FRACTION = 4

#: How many sessions the in-memory "last row per chat" map holds. Bounded and
#: LRU because the process outlives any chat: a machine that opens a new
#: session every few minutes would otherwise grow this map for as long as the
#: daemon runs. 256 is far past the number of chats a person has open.
LEDGER_SESSIONS = 256

#: Largest NON-STREAMED response body buffered to read its ``usage`` block.
#: Deliberately its own constant rather than a reach into
#: :data:`model_router.tool_ids.JSON_BUFFER_LIMIT_BYTES`: that one bounds a
#: rewrite that must hold the body anyway, this one bounds a COPY taken purely
#: to read four integers, and the next reason to move one is not a reason to
#: move the other. Past the bound the copy stops and the row reports what it
#: had, which for a body this size is nothing — an honest gap, not a guess.
USAGE_BODY_LIMIT_BYTES = 8 * 1024 * 1024

#: Bytes-per-token used when the gateway has to answer ``count_tokens``
#: itself. The conventional English-text approximation, and deliberately a
#: coarse one: the claim being made is "a conversation with content in it does
#: not cost zero tokens", which is the claim the vendor's zero contradicts.
#: What the CLIENT estimates when an endpoint offers no counter at all is a
#: character count of its own, whose exact ratio is the client's business —
#: which is why the substituted figure is LABELLED
#: (:data:`COUNT_SOURCE_ESTIMATE`) rather than passed off as a vendor count.
ESTIMATE_BYTES_PER_TOKEN = 4

#: The ``count_tokens`` provenance label. Present on a VENDOR answer only —
#: the first-party route is relayed verbatim, because adding a field to an
#: Anthropic response is exactly the "worse than native" this gateway refuses.
COUNT_SOURCE_FIELD = "_vct_count_source"
COUNT_SOURCE_VENDOR = "vendor"
COUNT_SOURCE_ESTIMATE = f"estimate:bytes/{ESTIMATE_BYTES_PER_TOKEN}"

#: Topic for the substitution warning in the ONE say-it-once registry
#: (:func:`model_router.catalog._log_once`). One line per process per
#: ``(vendor_id, model)`` pair: the substitution fires on EVERY count_tokens
#: call for an affected model, and Claude Code calls it constantly.
#:
#: This was a fourth parallel set until 2026-09-22. The other three were
#: consolidated that day and this one was missed, which is the exact shape
#: the consolidation exists to stop — the registry pattern was being copied
#: faster than it was being shared.
LOG_ONCE_COUNT_SUBSTITUTION = "count_substitution"

#: Set once, when the metrics home could not be resolved. See :func:`_metrics_dir`.
_METRICS_DIR_WARNED = False


def split_sse_events(buffer: bytes, *, final: bool) -> "tuple[list[bytes], bytes]":
    """Complete SSE events in ``buffer``, and whatever is left over.

    A PROJECTION of :func:`model_router.tool_ids.split_sse_frames`, which is
    the one home for the framing rule since v0.2.95: this reader only looks at
    events, so it drops the separator the rewriter needs to re-emit. Two
    callers, one loop — the duplicate loop that used to live here is gone, and
    a change to the rule now reaches both by construction.

    ``final`` is passed through and means what it means there: a buffer ending
    in ``\\r`` is ambiguous mid-stream and a terminator at EOF.
    """
    frames, rest = split_sse_frames(buffer, final=final)
    return [event for event, _separator in frames], rest


def event_payload(raw: bytes) -> Optional[dict]:
    """The JSON object carried by one SSE event's ``data:`` line(s), or ``None``.

    Multi-line ``data:`` is joined with newlines per the EventSource spec,
    which is what the upstream's own serialiser would have written — an event
    split over two ``data:`` lines is one document, not two.
    """
    lines = [
        _without_eol(line) for line in raw.splitlines(keepends=True)
    ]
    data = [line[len(b"data:"):].strip() for line in lines if line.startswith(b"data:")]
    if not data:
        return None
    try:
        payload = json.loads(b"\n".join(data).decode("utf-8", "replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def usage_block(payload: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The ``usage`` mapping in one event or one non-streamed body.

    Top level first, then ``message.usage``: ``message_delta`` puts it at the
    top, ``message_start`` nests it inside the message it opens, and a
    non-streamed body puts it at the top. One reader for all three, because
    "which shape is this?" is a question whose wrong answer is silent.
    """
    direct = payload.get("usage")
    if isinstance(direct, Mapping):
        return direct
    message = payload.get("message")
    if isinstance(message, Mapping):
        nested = message.get("usage")
        if isinstance(nested, Mapping):
            return nested
    return None


def reported_model_id(payload: Mapping[str, Any]) -> Optional[str]:
    """The ``model`` id one event or one non-streamed body reports, or ``None``.

    Mirrors :func:`usage_block`'s walk on purpose: a stream puts the id inside
    ``message_start``'s nested message, a non-streamed body puts it at the
    top, and the two must not drift apart — issue 9's echo assertion reads
    this, and its whole value is comparing the SAME shapes the vendor writes.

    Empty string counts as absent: a vendor that echoes ``""`` said nothing,
    and silence must not read as a mismatch (the module's zero-versus-silence
    discipline, applied to an id).
    """
    direct = payload.get("model")
    if isinstance(direct, str) and direct:
        return direct
    message = payload.get("message")
    if isinstance(message, Mapping):
        nested = message.get("model")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _as_count(value: Any) -> Optional[int]:
    """A non-negative token count, or ``None`` for anything that is not one.

    ``bool`` is excluded explicitly: ``True`` is an ``int`` in Python and
    would be recorded as one token, which is a plausible-looking wrong number
    — the worst kind.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


class UsageAccumulator:
    """Merges the usage a response reports, whatever shape it reports it in.

    Fed the bytes ALREADY on their way to the client (see the module
    docstring): it copies, never edits and never holds. A stream is read event
    by event; a non-streamed body is buffered to
    :data:`USAGE_BODY_LIMIT_BYTES` and parsed at :meth:`close`.

    The merge is per FIELD, last-non-null-wins, with zero treated as "not
    reported yet" once a positive has been seen — the rule the module
    docstring derives from the two upstream shapes. A field is ``seen`` as
    soon as any non-null value arrives for it, INCLUDING a zero, which is what
    makes :attr:`complete` mean "the response told me about every field"
    rather than "every field is positive".
    """

    def __init__(self, *, stream: bool) -> None:
        self._stream = stream
        self._buffer = b""
        self._buffered_bytes = 0
        self._over_limit = False
        self._values: dict[str, int] = {}
        self._seen: set[str] = set()
        #: Did the response state its FINAL usage? See :attr:`complete`.
        self._final_seen = False
        #: The ``model`` id the response reports, last non-empty one wins
        #: (issue 9). ``None`` until an event or body carries one — absence
        #: is silence, never a mismatch.
        self._reported_model: Optional[str] = None

    # ── input ────────────────────────────────────────────────────────────
    def feed(self, chunk: bytes) -> None:
        """Observe bytes the caller is relaying. Returns nothing, by design."""
        if self._stream:
            self._buffer += chunk
            events, self._buffer = split_sse_events(self._buffer, final=False)
            for event in events:
                self._observe_event(event)
            if len(self._buffer) > USAGE_BODY_LIMIT_BYTES:
                # A stream with no event boundary in 8 MiB is malformed or
                # not SSE at all. Stop holding it: the relay is unaffected
                # (these bytes are already gone), and the row will simply say
                # what it had.
                self._over_limit = True
                self._buffer = b""
            return
        if self._over_limit:
            return
        room = USAGE_BODY_LIMIT_BYTES - self._buffered_bytes
        if len(chunk) > room:
            self._over_limit = True
            self._buffer = b""
            return
        self._buffer += chunk
        self._buffered_bytes += len(chunk)

    def close(self) -> None:
        """No more bytes are coming. Reads whatever the tail still holds.

        The leftover after the final split is read too. An event whose closing
        blank line never arrived is indistinguishable, byte for byte, from one
        whose JSON is still arriving — and the two are told apart by simply
        trying: a truncated document does not parse, so a genuinely cut stream
        contributes nothing, while a ``message_delta`` that lost only its
        terminator still reports the output tokens it carried. Those are the
        tokens the user was charged for either way.
        """
        if self._stream:
            events, self._buffer = split_sse_events(self._buffer, final=True)
            for event in events:
                self._observe_event(event)
            if self._buffer:
                self._observe_event(self._buffer)
            self._buffer = b""
            return
        if self._over_limit or not self._buffer:
            self._buffer = b""
            return
        raw, self._buffer = self._buffer, b""
        self.observe_body(raw)

    def observe_body(self, raw: bytes) -> None:
        """Read the ``usage`` of a body somebody else already buffered.

        The vendor-JSON relay path holds the whole response to rewrite tool
        ids in it, so handing those bytes straight here costs nothing and
        avoids a second copy of the same body.
        """
        try:
            payload = json.loads(raw) if raw else None
        except (ValueError, RecursionError):
            return
        if isinstance(payload, dict):
            # A non-streamed body IS the final word by construction: there is
            # no later event that could revise it.
            model = reported_model_id(payload)
            if model is not None:
                self._reported_model = model
            if self._merge(usage_block(payload)):
                self._final_seen = True

    # ── merge ────────────────────────────────────────────────────────────
    def _observe_event(self, raw: bytes) -> None:
        payload = event_payload(raw)
        if payload is None:
            return
        # Issue 9: capture the reported model wherever the event carries it —
        # ``message_start`` nests it in ``message``, some vendors repeat it at
        # the top of the delta. Last non-empty one wins.
        model = reported_model_id(payload)
        if model is not None:
            self._reported_model = model
        if self._merge(usage_block(payload)):
            # ``message_delta`` is where BOTH upstreams state the final
            # figures — Anthropic's final ``output_tokens``, the vendor's
            # everything. Its arrival is therefore the only evidence in the
            # stream that this row is the last word rather than a snapshot,
            # and it is what :attr:`complete` needs beyond the field census:
            # ``message_start`` alone reports all four fields for Anthropic,
            # so a census on its own calls a stream that died mid-answer
            # complete.
            if payload.get("type") == "message_delta":
                self._final_seen = True

    def _merge(self, usage: Optional[Mapping[str, Any]]) -> bool:
        """Merge one usage block. True when it carried at least one count."""
        if usage is None:
            return False
        merged = False
        for field_name in USAGE_FIELDS:
            value = _as_count(usage.get(field_name))
            if value is None:
                continue
            self._seen.add(field_name)
            merged = True
            if value == 0 and self._values.get(field_name):
                # The vendor that zeroes ``message_start`` and the API version
                # that repeats input fields in ``message_delta`` need opposite
                # things from a naive last-wins; this is the one rule that
                # serves both.
                continue
            self._values[field_name] = value
        return merged

    # ── output ───────────────────────────────────────────────────────────
    @property
    def complete(self) -> bool:
        """Is this row the LAST WORD on the turn's usage?

        Two conditions, and the second is the one a field census alone
        misses. Every field must have been reported at least once — a vendor
        that never emits the cache fields reads False, correctly, because the
        zeros in such a row are this module's default and not the upstream's
        statement. AND the response must have stated its final usage: a
        non-streamed body always has, a stream has once ``message_delta``
        arrives.

        Without the second condition an Anthropic stream that died halfway
        would read complete, because ``message_start`` alone reports all four
        fields. That is exactly the case a context monitor must be able to
        distrust, so it is exactly the case this flag exists to mark.
        """
        return self._final_seen and all(
            field_name in self._seen for field_name in USAGE_FIELDS
        )

    def totals(self) -> "dict[str, int]":
        """The merged counts, with unseen fields at zero."""
        return {name: self._values.get(name, 0) for name in USAGE_FIELDS}

    @property
    def saw_anything(self) -> bool:
        return bool(self._seen)

    @property
    def reported_model(self) -> Optional[str]:
        """The ``model`` id this response reported, or ``None`` for silence.

        Issue 9's echo assertion compares this against the forwarded id at
        the terminal ``access`` point in :mod:`model_router.server`. ``None``
        means no event or body carried the field — which is NOT a mismatch,
        for the same reason an unreported token count is not zero.
        """
        return self._reported_model


@dataclass(frozen=True)
class UsageRecord:
    """One completed request, as the ledger stores it.

    Frozen because a row is an observation: something that mutated after being
    handed to the writer would make the JSONL and the ``/usage`` snapshot
    disagree about the same request.

    Two windows, deliberately, and they answer different questions.
    ``window_client`` is what the CLIENT budgeted — keyed purely on the id it
    asked for, so it is 1M exactly when that id carried ``[1m]``.
    ``window_actual`` is what the model really has, per
    :func:`model_router.catalog.resolve_window`. When they disagree the client
    is compacting at the wrong point, and ``pct_client`` versus ``pct_actual``
    is that disagreement in one line.
    """

    ts: str
    session: Optional[str]
    agent: Optional[str]
    parent_agent: Optional[str]
    requested: str
    route: str
    forward: str
    stream: bool
    status: int
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    #: What the model was GIVEN this turn: fresh input plus both cache halves.
    context_tokens: int
    #: What the NEXT turn carries — this turn's input plus its output. The
    #: number a context monitor actually wants: it is the floor on the next
    #: request's ``context_tokens``.
    context_after: int
    window_client: int
    window_actual: Optional[int]
    #: Which step of the catalog resolver answered ``window_actual``.
    window_source: str
    pct_client: Optional[float]
    pct_actual: Optional[float]
    usage_complete: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def _pct(used: int, window: Optional[int]) -> Optional[float]:
    """``used`` as a percentage of ``window``, 1 dp — ``None`` when unknown.

    ``None`` rather than ``0.0`` for an unknown window: a context monitor that
    reported 0% for "I do not know how big this model is" would read as
    "plenty of room left", which is the one wrong answer that costs the user
    something.
    """
    if not window or window <= 0:
        return None
    return round(used * 100.0 / window, 1)


def client_window(requested: str) -> int:
    """The window the CLIENT budgets for ``requested``. See :data:`CLIENT_WINDOW_1M`."""
    return (
        CLIENT_WINDOW_1M
        if requested.endswith(ONE_M_SUFFIX)
        else CLIENT_WINDOW_DEFAULT
    )


def build_record(
    *,
    session: Optional[str],
    agent: Optional[str],
    parent_agent: Optional[str],
    requested: str,
    route: str,
    forward: str,
    stream: bool,
    status: int,
    totals: Mapping[str, int],
    usage_complete: bool,
    window_actual: Optional[int],
    window_source: str,
    now: Optional[datetime] = None,
) -> UsageRecord:
    """Assemble one row. Pure — no clock unless the caller declines to pass one."""
    moment = now or datetime.now(timezone.utc)
    input_tokens = int(totals.get("input_tokens", 0))
    cache_creation = int(totals.get("cache_creation_input_tokens", 0))
    cache_read = int(totals.get("cache_read_input_tokens", 0))
    output_tokens = int(totals.get("output_tokens", 0))
    context_tokens = input_tokens + cache_creation + cache_read
    context_after = context_tokens + output_tokens
    window_c = client_window(requested)
    return UsageRecord(
        ts=moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        session=session,
        agent=agent,
        parent_agent=parent_agent,
        requested=requested,
        route=route,
        forward=forward,
        stream=stream,
        status=status,
        input_tokens=input_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        output_tokens=output_tokens,
        context_tokens=context_tokens,
        context_after=context_after,
        window_client=window_c,
        window_actual=window_actual,
        window_source=window_source,
        pct_client=_pct(context_after, window_c),
        pct_actual=_pct(context_after, window_actual),
        usage_complete=usage_complete,
    )


def access_extra(totals: Mapping[str, int], *, seen: bool) -> str:
    """The usage fields appended to the access line, in ONE shape.

    ``-`` for a field the response never reported, because a zero and a
    silence are different observations and the log is the place that has to
    keep telling them apart. The whole group is dashes when the response
    reported no usage at all (an error status relayed verbatim, a stream that
    died before ``message_start``).
    """
    def show(name: str) -> str:
        if not seen:
            return "-"
        value = totals.get(name)
        return "-" if value is None else str(value)

    context = (
        "-"
        if not seen
        else str(
            totals.get("input_tokens", 0)
            + totals.get("cache_creation_input_tokens", 0)
            + totals.get("cache_read_input_tokens", 0)
        )
    )
    return (
        f"in={show('input_tokens')} "
        f"cache_c={show('cache_creation_input_tokens')} "
        f"cache_r={show('cache_read_input_tokens')} "
        f"out={show('output_tokens')} "
        f"ctx={context}"
    )


# ── count_tokens: never worse than native ────────────────────────────────
def count_tokens_estimate(payload: Optional[Mapping[str, Any]]) -> Optional[int]:
    """A bytes/4 floor for the countable content of a request, or ``None``.

    ``None`` means "there is nothing to count" — no ``messages`` and no
    ``system`` — and in that case a vendor's zero is the right answer and is
    relayed. Everything else gets at least 1: a request carrying content
    cannot cost zero tokens, and an endpoint that says so is telling the
    client something the client's own fallback would have contradicted.

    Only ``messages`` and ``system`` are measured. Tools, metadata and
    sampling parameters do consume tokens upstream, but including them would
    move this from "a floor the client already trusts" to "a competing
    estimate", and the point is to be no worse than the client's own guess.
    """
    if not isinstance(payload, Mapping):
        return None
    countable: dict[str, Any] = {}
    for key in ("messages", "system"):
        value = payload.get(key)
        if value:
            countable[key] = value
    if not countable:
        return None
    size = len(
        json.dumps(countable, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8",
        )
    )
    return max(1, math.ceil(size / ESTIMATE_BYTES_PER_TOKEN))


def guard_count_tokens(
    body: Any, estimate: Optional[int],
) -> "tuple[Any, bool]":
    """Label a vendor ``count_tokens`` answer; substitute a zero for ``estimate``.

    The incident this closes is measured, not hypothetical: on one shipped
    vendor's Anthropic-compatible endpoint, ``count_tokens`` answers
    ``{"input_tokens": 0}`` deterministically for a body the client would
    itself have estimated at a dozen tokens, while a sibling model on the same
    endpoint answers a real figure. A zero is WORSE than native — Claude Code
    falls back to a character estimate when an endpoint has no counter at all,
    so relaying the zero replaces a usable guess with a number that says the
    conversation is empty. That is the proxy invariant ("never worse than
    native") breaking, so the gateway answers with the same estimate the
    client would have made and says so in the body.

    Returns ``(body, substituted)``. Non-2xx bodies and first-party answers
    never reach here; a body that is not an object is returned untouched,
    because a shape this function does not understand is not one it may label.
    """
    if not isinstance(body, dict):
        return body, False
    reported = _as_count(body.get("input_tokens"))
    if reported is None:
        # The endpoint answered something that is not a token count. Relaying
        # it unlabelled is right: the label is a claim about a number, and
        # there is no number.
        return body, False
    if reported > 0 or estimate is None:
        patched = dict(body)
        patched[COUNT_SOURCE_FIELD] = COUNT_SOURCE_VENDOR
        return patched, False
    patched = dict(body)
    patched["input_tokens"] = estimate
    patched[COUNT_SOURCE_FIELD] = COUNT_SOURCE_ESTIMATE
    return patched, True


def note_count_substitution(vendor_id: str, model: str) -> bool:
    """Log the substitution ONCE per vendor+model. True when this call logged.

    Claude Code calls ``count_tokens`` before most turns, so an unconditional
    warning would be one log line per keystroke-scale interaction for as long
    as the model is selected.
    """
    from .catalog import _log_once

    return _log_once(
        LOG_ONCE_COUNT_SUBSTITUTION,
        (vendor_id, model),
        logging.WARNING,
        "model-gateway: %s answered count_tokens with 0 input tokens for %s on "
        "a non-empty body; substituting the gateway's own bytes/%d estimate so "
        "the client is not told its conversation is empty. Further "
        "substitutions for this model are not logged.",
        vendor_id, model, ESTIMATE_BYTES_PER_TOKEN,
        logger_=logger,
    )


# ── the ledger ───────────────────────────────────────────────────────────
def _metrics_dir() -> Optional[Path]:
    """VCO's one metrics home, or ``None`` when it cannot be resolved.

    Imported lazily, like :func:`model_router.secrets._default_getter`, for the
    reason the packaging test pins: this module must import in a venv holding
    only the ``model_router`` wheel, and ``vco_lib`` is not in it.

    An ``ImportError`` here means a BROKEN install rather than a supported
    configuration — every path that starts this daemon puts ``vco_lib`` in the
    same venv — so it is reported loudly, ONCE. It is not, however, allowed to
    take a request down: this is a context monitor, and a proxy that refused
    to proxy because it could not open its own logbook would be the worse
    failure by far. ``$VCT_STATE_DIR`` still answers if it is set, which is
    what makes the degraded state recoverable without a reinstall.

    There is deliberately no inline ``~/.vct`` reconstruction here: that
    resolver has ONE home (``vco_lib.paths.vct_root_dir``) and
    ``tests/test_vct_root_dir_consolidation.py`` enforces it.
    """
    global _METRICS_DIR_WARNED
    try:
        from vco_lib.paths import vct_metrics_dir  # noqa: PLC0415 — deliberate

        return vct_metrics_dir()
    except ImportError:
        custom = os.environ.get("VCT_STATE_DIR", "").strip()
        if custom:
            return Path(custom) / "metrics"
        if not _METRICS_DIR_WARNED:
            _METRICS_DIR_WARNED = True
            logger.warning(
                "model-gateway: vco_lib is not importable, so the usage "
                "ledger has no home and token accounting is OFF for this "
                "process. That is a broken install — reinstall, or set "
                "$VCT_STATE_DIR to choose a state root. Proxying is "
                "unaffected.",
            )
        return None


class UsageLedger:
    """Append-only JSONL of completed requests, plus the last row per chat.

    Three properties the caller relies on:

    * **it never raises into a handler.** :meth:`submit` catches everything;
      the append itself runs on the loop's executor and its failure is logged
      by the done-callback, not by the request;
    * **it is bounded in both directions.** Past :data:`LEDGER_MAX_BYTES` the
      file is truncated to its newest rows by
      :func:`vco_lib.atomic.rotate_tail_lines` — one file, no generations, the
      recent history kept; the in-memory map is LRU-capped at
      :data:`LEDGER_SESSIONS`, so a daemon with a year of uptime holds the
      same amount either way;
    * **the in-memory view is truthful IMMEDIATELY.** ``/usage`` answers from
      the map, which :meth:`submit` updates synchronously, so a monitor polling
      right after a turn never sees a stale chat because a disk write is still
      queued. ``rows_written`` counts rows that actually reached the file,
      which is the opposite convention and the honest one for a field whose
      whole job is to say whether the file has them.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        max_bytes: int = LEDGER_MAX_BYTES,
        max_sessions: int = LEDGER_SESSIONS,
    ) -> None:
        self._explicit_path = path
        self._resolved: Optional[Path] = path
        self._resolve_attempted = path is not None
        self._max_bytes = max_bytes
        self._max_sessions = max_sessions
        self._lock = threading.Lock()
        self._rows_written = 0
        self._last_write_ts: Optional[str] = None
        self._dropped = 0
        self._rotation_warned = False
        self._last_by_session: "OrderedDict[str, UsageRecord]" = OrderedDict()
        self._pending: set = set()

    # ── paths ────────────────────────────────────────────────────────────
    @property
    def path(self) -> Optional[Path]:
        """Where rows land, or ``None`` when no metrics home could be found.

        Resolved once and remembered, including the ``None``: the answer is a
        property of the install, and re-deriving it per request would repeat
        the import failure (and its warning) on every turn.
        """
        if not self._resolve_attempted:
            self._resolve_attempted = True
            home = _metrics_dir()
            self._resolved = None if home is None else home / LEDGER_BASENAME
        return self._resolved

    # ── writing ──────────────────────────────────────────────────────────
    def submit(self, record: UsageRecord, *, loop: Any = None) -> None:
        """Record ``record`` now, append it soon. Never raises, ever.

        ``loop`` is the running event loop when there is one. Without it — a
        unit test, a synchronous caller — the append happens inline, which is
        the same work on the same thread and is only unacceptable on the
        request path.
        """
        try:
            self._remember(record)
            if loop is None:
                self._append(record)
                return
            future = loop.run_in_executor(None, self._append, record)
            self._pending.add(future)
            future.add_done_callback(self._settled)
        except Exception:  # noqa: BLE001 — a monitor may never break a relay
            logger.exception(
                "model-gateway: usage ledger failed; continuing without it",
            )

    def _settled(self, future: Any) -> None:
        self._pending.discard(future)
        try:
            future.result()
        except Exception:  # noqa: BLE001 — see submit()
            logger.exception("model-gateway: usage ledger write failed")

    async def drain(self) -> None:
        """Wait for queued appends. Used at shutdown and by the tests."""
        import asyncio  # noqa: PLC0415 — only this method needs a loop

        while self._pending:
            pending = list(self._pending)
            await asyncio.gather(*pending, return_exceptions=True)
            self._pending.difference_update(pending)

    def _remember(self, record: UsageRecord) -> None:
        if record.session is None:
            # A request with no session header still reaches the FILE — it is
            # a real turn and its tokens are real. It is absent from the
            # per-chat map because that map is keyed by chat, and "no chat"
            # is not a key: bucketing every such request under one synthetic
            # id would merge unrelated callers into a fictional conversation.
            return
        with self._lock:
            self._last_by_session.pop(record.session, None)
            self._last_by_session[record.session] = record
            while len(self._last_by_session) > self._max_sessions:
                self._last_by_session.popitem(last=False)

    def _append(self, record: UsageRecord) -> None:
        target = self.path
        if target is None:
            return
        line = record.to_json() + "\n"
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(target)
            # Text mode with an explicit encoding and newline: on Windows the
            # default would translate the "\n" above into "\r\n" and every row
            # would carry a stray CR for a reader that splits on "\n".
            with open(target, "a", encoding="utf-8", newline="") as handle:
                handle.write(line)
            self._rows_written += 1
            self._last_write_ts = record.ts

    def _rotate_if_needed(self, target: Path) -> None:
        """Keep the newest :meth:`_keep_lines` rows, drop the rest. One file.

        The truncation is :func:`vco_lib.atomic.rotate_tail_lines`, which is
        the HOME for "shrink an append-only log to its tail, atomically" —
        `~/.vct/logs` and the resolver-warning logs already rotate through it.
        A second implementation here would be a second place deciding what
        data to DISCARD, which is the one kind of routine that must not drift.

        ``in_place=False`` (the default) is correct for this caller: every
        append opens and closes the file, so no descriptor outlives the
        rotation and the inode swap costs nothing — and the atomic replace
        leaves no window in which a reader sees a half-written log. The
        ``in_place=True`` case exists for a process that holds the file open,
        which this one never does.

        Soft-fail throughout, including the import: rotation is housekeeping
        and the ROW is the point. ``rotate_tail_lines`` already answers
        ``False`` rather than raising on any ``OSError``.
        """
        try:
            from vco_lib.atomic import (  # noqa: PLC0415 — deliberate, see _metrics_dir
                rotate_tail_lines,
            )
        except ImportError:
            self._warn_no_rotation()
            return
        rotate_tail_lines(
            target,
            max_bytes=self._max_bytes,
            keep_lines=self._keep_lines(),
            in_place=False,
        )

    def _keep_lines(self) -> int:
        """How many rows survive a rotation. See :data:`LEDGER_KEEP_FRACTION`.

        A LINE count, because that is what the shared rotator takes, derived
        from the byte cap through the measured row size. At least one: a cap
        smaller than a single row still has to leave the newest row readable,
        and a ``keep_lines`` of zero would empty the file on every write.
        """
        return max(
            1, (self._max_bytes // LEDGER_KEEP_FRACTION) // LEDGER_ROW_BYTES,
        )

    def _warn_no_rotation(self) -> None:
        """Say ONCE that the ledger will grow unbounded, and why."""
        if self._rotation_warned:
            return
        self._rotation_warned = True
        logger.warning(
            "model-gateway: vco_lib is not importable, so the usage ledger at "
            "%s cannot be rotated and will grow without bound. That is a "
            "broken install — reinstall. Rows are still being written and "
            "proxying is unaffected.",
            self.path,
        )

    # ── reading ──────────────────────────────────────────────────────────
    @property
    def rows_written(self) -> int:
        with self._lock:
            return self._rows_written

    @property
    def last_write_ts(self) -> Optional[str]:
        with self._lock:
            return self._last_write_ts

    def sessions(
        self, only: Optional[str] = None,
    ) -> "dict[str, dict[str, Any]]":
        """The last row per chat, newest last. ``only`` filters to one chat."""
        with self._lock:
            items: Iterable[tuple[str, UsageRecord]] = list(
                self._last_by_session.items(),
            )
        return {
            key: asdict(record)
            for key, record in items
            if only is None or key == only
        }

    def health(self) -> "dict[str, Any]":
        """The ``/health`` block. Cached state only — never touches the disk."""
        target = self.path
        return {
            "path": str(target) if target is not None else None,
            "rows_written": self.rows_written,
            "last_write_ts": self.last_write_ts,
        }


#: Request headers that identify a chat. Read case-insensitively by the
#: caller (aiohttp's ``CIMultiDict`` does that for free); named here so the
#: three spellings live in one place rather than three string literals in a
#: handler.
SESSION_HEADER = "x-claude-code-session-id"
AGENT_HEADER = "x-claude-code-agent-id"
PARENT_AGENT_HEADER = "x-claude-code-parent-agent-id"

#: Longest id kept. Generous next to a UUID (36) and small next to anything
#: abusive — see :func:`read_identity` for why a cap exists at all.
_ID_MAX_CHARS = 200


def read_identity(
    headers: Mapping[str, str],
) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """``(session, agent, parent_agent)`` from the request headers.

    Empty is absent: a header present with a blank value identifies nothing,
    and recording ``""`` as a session id would collect every such request into
    one fictional chat.

    Values are LENGTH-CAPPED. They are client-controlled and they become keys
    in a bounded map and fields in a log-adjacent file; an id of a megabyte is
    not a session, and truncating it keeps the row readable while still
    separating chats (the client's own ids are UUIDs).
    """
    def read(name: str) -> Optional[str]:
        raw = headers.get(name)
        if not isinstance(raw, str):
            return None
        value = raw.strip()
        if not value:
            return None
        return value[:_ID_MAX_CHARS]

    return read(SESSION_HEADER), read(AGENT_HEADER), read(PARENT_AGENT_HEADER)


__all__ = [
    "AGENT_HEADER",
    "CLIENT_WINDOW_1M",
    "CLIENT_WINDOW_DEFAULT",
    "COUNT_SOURCE_ESTIMATE",
    "COUNT_SOURCE_FIELD",
    "COUNT_SOURCE_VENDOR",
    "LEDGER_BASENAME",
    "LEDGER_KEEP_FRACTION",
    "LEDGER_MAX_BYTES",
    "LEDGER_ROW_BYTES",
    "LEDGER_SESSIONS",
    "PARENT_AGENT_HEADER",
    "SESSION_HEADER",
    "USAGE_BODY_LIMIT_BYTES",
    "USAGE_FIELDS",
    "UsageAccumulator",
    "UsageLedger",
    "UsageRecord",
    "access_extra",
    "build_record",
    "client_window",
    "count_tokens_estimate",
    "event_payload",
    "guard_count_tokens",
    "note_count_substitution",
    "read_identity",
    "reported_model_id",
    "split_sse_events",
    "usage_block",
]
