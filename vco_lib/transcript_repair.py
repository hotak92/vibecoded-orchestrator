# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Anthropic tool-block normalisation — the ONE deterministic rewrite.

Three callers share this module, which is why it lives in ``vco_lib`` (the
Python SSOT) rather than beside the gateway: the gateway's vendor RESPONSE
path, the gateway's Anthropic REQUEST path
(:mod:`model_router.tool_ids`), and ``vco fix-transcript``
(:mod:`vco_lib.cli.fix_transcript`) must produce byte-identical results, or a
transcript repaired by one is rejected after being touched by the other.

What it is for
--------------
A vendor route serves an Anthropic-SHAPED API with its own ids and its own
built-in tools. Anthropic's validator is stricter than the shape suggests, and
2026-09-08 established exactly how, by live probe against
``api.anthropic.com`` through the gateway (``/v1/messages/count_tokens``,
model ``claude-haiku-4-5-20251001``):

======================================================  ==========================
body                                                    result
======================================================  ==========================
``server_tool_use.id = "call_abc123"``                  400 ``String should match
                                                        pattern
                                                        '^srvtoolu_[a-zA-Z0-9_]+$'``
``server_tool_use.id = "srvtoolu_call_abc123"``,        **200**
``name = "web_search"``
``server_tool_use.name = "analyze_image"``              400 ``name: Input should be
(id already conforming)                                 'web_search', 'web_fetch',
                                                        'code_execution',
                                                        'bash_code_execution',
                                                        'text_editor_code_execution',
                                                        'tool_search_tool_regex',
                                                        'tool_search_tool_bm25'``
``web_search_tool_result`` with no matching             400 ``each
``server_tool_use`` before it                           web_search_tool_result block
                                                        must have a corresponding
                                                        server_tool_use block``
``tool_use.id = "call_5f460651ce"`` + matching          **200** (a plain
``tool_result``                                         ``tool_use`` id is NOT
                                                        pattern-checked)
------------------------------------------------------  --------------------------
2026-09-09, same endpoint and model, on the CONTENT of a result block:
------------------------------------------------------  --------------------------
``server_tool_use name="web_search"`` +                 400 ``…web_search_tool_result
``web_search_tool_result`` whose ``content`` is a       .content.list[RequestWeb
plain ``web_search_result`` (url, title, page_age),     SearchResultBlock].0
ids conforming                                          .encrypted_content: Field
                                                        required``
the same, with an ``encrypted_content`` string we       400 ``Invalid
made up                                                 `encrypted_content` in
                                                        `search_result` block``
control: one user message, no tool blocks               **200**
======================================================  ==========================

The last two rows are why a portable NAME is not enough. ``encrypted_content``
is an opaque blob Anthropic itself issues; a vendor cannot produce one, and
neither can this module — so a VENDOR-origin ``web_search`` pair has no valid
form on the Anthropic route at any id and under any name. Normalising its id
and keeping it, which the name rule alone did, leaves a 400 in the history:
the exact failure mode this module exists to prevent, arrived at by the
repair. Hence ``strip_vendor_origin`` (see :class:`TranscriptRepairer`):
Anthropic-bound, a server-tool pair the gateway can see is not Anthropic's own
goes, whatever it is called.

So the policy this module implements, and why each half of it exists:

* **Normalising the id is enough — but only for a tool name Anthropic knows.**
  Hence :data:`PORTABLE_SERVER_TOOL_NAMES`, copied verbatim from the
  validator's own error text.
* **A block whose name is not in that set cannot be represented at all**, so
  it is STRIPPED together with the result that references it. There is no
  third option: rewriting the name would claim a different tool ran, which is
  the class of lie this codebase refuses elsewhere (honest naming in
  :mod:`model_router.routing`).

The incident that paid for this: a session on a vendor route answered image
attachments with the vendor's built-in ``analyze_image``, which wrote
``server_tool_use`` blocks with ids like ``call_5f46…`` into the transcript.
Every LATER Anthropic-bound request in that session then failed with an opaque
``400 messages.447.content.1.server_tool_use.id …`` — the chat was dead, and
switching models could not revive it because the poison was in the history.

Determinism
-----------
``normalise_id`` is a pure function of the original id, so the forward
direction never needs to be remembered. The REVERSE direction does — sanitising
is lossy (``a.b`` and ``a_b`` both become ``a_b``) — which is why callers that
must hand a vendor back its own ids pass an ``id_map`` (new -> original). The
gateway keeps a bounded one per vendor; ``fix-transcript`` keeps one per file.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, MutableMapping, Optional

from vco_lib.atomic import atomic_text_stream

#: Prefix Anthropic requires on ``server_tool_use.id`` (validator pattern
#: ``^srvtoolu_[a-zA-Z0-9_]+$``, quoted from the live 400 above).
SERVER_TOOL_ID_PREFIX = "srvtoolu_"

#: Plain ``tool_use`` ids are NOT rewritten. The validator does not
#: pattern-check them — a vendor ``call_…`` id round-trips at HTTP 200, proved
#: by live probe — so rewriting them would buy nothing and cost something
#: real: every id the gateway rewrites has to be remembered so the vendor can
#: be handed its own back, and a client re-sends its whole history on every
#: turn. Rewriting only the ids that MUST change keeps that map to the handful
#: of server-tool calls in a session instead of every tool call in it.
_SERVER_TOOL_ID_RE = re.compile(r"^srvtoolu_[a-zA-Z0-9_]+$")
_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9_]")

#: Server-tool names Anthropic's validator accepts, verbatim from its own
#: error text (2026-09-08). A ``server_tool_use`` block naming anything else
#: is unrepresentable on the Anthropic route at any id, so it is stripped.
#: This is a CLIENT-CONTRACT list, like ``CLAUDE_ID_MARKERS`` in the gateway's
#: vendor registry — not a preference, and not a place to add vendor names.
PORTABLE_SERVER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "code_execution",
        "bash_code_execution",
        "text_editor_code_execution",
        "tool_search_tool_regex",
        "tool_search_tool_bm25",
    }
)

#: Block type of a CLIENT tool result — references a ``tool_use`` id.
CLIENT_TOOL_RESULT_TYPE = "tool_result"

#: The MCP connector's producer block. Never rewritten (its ids are
#: Anthropic's), but recognised so its id is RECORDED as a producer — without
#: that its result reads as an orphan and is stripped on every request.
MCP_TOOL_USE_TYPE = "mcp_tool_use"

#: What a caller wants done with a message the repair emptied.
#: ``EMPTY_DROP`` — remove the message (in-flight request: nothing downstream
#: references it, and consecutive same-role turns are accepted).
#: ``EMPTY_PLACEHOLDER`` — substitute one text block (``.jsonl``: the entry's
#: ``uuid`` is the ``parentUuid`` of the next line, so it must survive).
#: ``EMPTY_KEEP`` — leave ``[]`` (the response path, where the message IS the
#: response and there is nothing to drop it from).
EMPTY_DROP = "drop"
EMPTY_PLACEHOLDER = "placeholder"
EMPTY_KEEP = "keep"

#: Substituted for an emptied ``.jsonl`` entry. Reads as what it is: a note
#: from a tool, in the transcript, where a vendor block used to be.
EMPTY_PLACEHOLDER_TEXT = "[vendor tool block removed by vco fix-transcript]"

#: Suffix identifying a SERVER-tool result block. Matched by suffix rather
#: than by an enumerated list because the point of the exercise is vendor
#: blocks, and a vendor invents its own type names (``analyze_image_tool_result``
#: would never appear in any list we could ship). Anthropic's own members of
#: this family today: ``web_search_tool_result``, ``web_fetch_tool_result``,
#: ``code_execution_tool_result``, ``bash_code_execution_tool_result``,
#: ``text_editor_code_execution_tool_result``.
SERVER_TOOL_RESULT_SUFFIX = "_tool_result"

#: Producer -> RESULT block type, spelled out rather than derived.
#:
#: The derivation ``f"{name}_tool_result"`` looked like the tidy answer and
#: was wrong twice: Anthropic's tool-search tools BOTH report through one
#: ``tool_search_tool_result`` block (not ``tool_search_tool_regex_tool_result``
#: / ``…bm25_tool_result``), and the MCP connector's pair
#: (``mcp_tool_use`` / ``mcp_tool_result``, ids ``mcptoolu_…``) is not in the
#: server-tool NAME list at all. Deriving therefore invented two types that do
#: not exist and missed two that do — and since ``sanitise_for_anthropic``
#: runs on every first-party request, the missing ones were STRIPPED out of
#: healthy transcripts, leaving their producers dangling: a 400 manufactured
#: by the repair that exists to prevent one.
#:
#: Sources (Anthropic docs, 2026-09):
#:   web_search              -> docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool
#:   web_fetch               -> docs.claude.com/en/docs/agents-and-tools/tool-use/web-fetch-tool
#:   code_execution          -> docs.claude.com/en/docs/agents-and-tools/tool-use/code-execution-tool
#:   bash_code_execution     -> same page (code-execution container tools)
#:   text_editor_code_execution -> same page
#:   tool_search_tool_regex  -> docs.claude.com/en/docs/agents-and-tools/tool-use/tool-search-tool
#:   tool_search_tool_bm25   -> same page (ONE result type for both)
#:   mcp_tool_use            -> docs.claude.com/en/docs/agents-and-tools/mcp-connector
SERVER_TOOL_RESULT_TYPE_BY_NAME: dict[str, str] = {
    "web_search": "web_search_tool_result",
    "web_fetch": "web_fetch_tool_result",
    "code_execution": "code_execution_tool_result",
    "bash_code_execution": "bash_code_execution_tool_result",
    "text_editor_code_execution": "text_editor_code_execution_tool_result",
    "tool_search_tool_regex": "tool_search_tool_result",
    "tool_search_tool_bm25": "tool_search_tool_result",
    "mcp_tool_use": "mcp_tool_result",
}

#: Result block types Anthropic can represent. A type outside this set is
#: unrepresentable on the Anthropic route (``analyze_image_tool_result`` is
#: the field case) — but only when its reference is not Anthropic-minted, see
#: :data:`ANTHROPIC_MINTED_ID_RE`. The id was never the whole story: a block
#: whose TYPE the validator does not know is a 400 whatever its id, and a 400
#: that lives in the history is permanent.
PORTABLE_SERVER_TOOL_RESULT_TYPES: frozenset[str] = frozenset(
    SERVER_TOOL_RESULT_TYPE_BY_NAME.values()
)

#: Ids only ANTHROPIC mints: server tools (``srvtoolu_``) and the MCP
#: connector (``mcptoolu_``). A result block carrying one of these came from
#: Anthropic's own side, so an unfamiliar TYPE beside it means this code is
#: out of date — not that the block is a vendor's. It is kept: stripping a
#: server tool Anthropic shipped after this table was written would break a
#: working transcript, and the failure would look exactly like the one this
#: module fixes. Only an id that is NOT Anthropic's makes an unknown type
#: strippable — a vendor's ``call_…``, or the ``srvtoolu_vct_call_…`` this
#: module derives from one, which :func:`minted_by_anthropic` tells apart by
#: :data:`GATEWAY_ID_MARKER`.
ANTHROPIC_MINTED_ID_RE = re.compile(r"^(?:srvtoolu|mcptoolu)_[a-zA-Z0-9_]+$")

#: Inserted after the prefix in every id THIS module writes
#: (``srvtoolu_vct_call_abc``). Inside the validator's charset, so it costs
#: nothing — and it is what makes "did we write this id?" a decidable
#: question rather than a guess. Without it, an id we normalised is
#: byte-shaped exactly like one Anthropic minted, and the two gates in
#: :meth:`TranscriptRepairer._server_tool_result` have to treat a
#: vendor-origin block as first-party.
#:
#: The reverse direction is unaffected: the vendor is handed its ORIGINAL id
#: back from the map (:func:`restore_ids`), never from the shape, so the
#: marker is free.
GATEWAY_ID_MARKER = "vct_"

#: Ids this module produced: a first-party prefix followed by the marker.
#: ``toolu_`` is listed although nothing writes it today — ``normalise_id``
#: takes the prefix as an argument, so the recogniser must cover any prefix
#: it can be called with rather than only the one that currently is.
_GATEWAY_MINTED_RE = re.compile(
    rf"^(?:srvtoolu|mcptoolu|toolu)_{re.escape(GATEWAY_ID_MARKER)}"
)


def sanitise_id_body(raw: str) -> str:
    """Every character outside ``[A-Za-z0-9_]`` becomes ``_``.

    Not a hash and not a slug: the original stays readable in the rewritten
    id, which is what makes a repaired transcript diffable against its backup.
    """
    return _DISALLOWED_RE.sub("_", raw)


@lru_cache(maxsize=8)
def _pattern_for(prefix: str) -> "re.Pattern[str]":
    if prefix == SERVER_TOOL_ID_PREFIX:
        return _SERVER_TOOL_ID_RE
    return re.compile(rf"^{re.escape(prefix)}[a-zA-Z0-9_]+$")


def id_conforms(raw: object, prefix: str = SERVER_TOOL_ID_PREFIX) -> bool:
    """True when ``raw`` already satisfies Anthropic's pattern for ``prefix``."""
    if not isinstance(raw, str):
        return False
    return bool(_pattern_for(prefix).match(raw))


def normalise_id(raw: str, prefix: str = SERVER_TOOL_ID_PREFIX) -> str:
    """``prefix`` + :data:`GATEWAY_ID_MARKER` + the sanitised body.

    Idempotent and a pure function of ``raw``. An id that already carries
    ``prefix`` keeps it (so a second pass cannot produce
    ``srvtoolu_srvtoolu_…``), and the marker is stripped with it, so the
    second pass cannot produce ``srvtoolu_vct_vct_…`` either — idempotence
    is keyed on the PREFIX AND MARKER together, which is why a vendor id
    that merely begins with ``vct_`` still gets its own distinct output
    (``vct_foo`` and ``foo`` must not collide in the reverse map).

    A body that sanitises to nothing — an empty or punctuation-only id —
    falls back to a short digest of the original so the result still matches
    ``[a-zA-Z0-9_]+`` and two different degenerate ids do not collide.
    """
    if raw.startswith(prefix):
        body = raw[len(prefix):]
        if body.startswith(GATEWAY_ID_MARKER):
            body = body[len(GATEWAY_ID_MARKER):]
    else:
        body = raw
    cleaned = sanitise_id_body(body)
    if not cleaned:
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
        cleaned = f"x{digest}"
    return f"{prefix}{GATEWAY_ID_MARKER}{cleaned}"


def minted_by_anthropic(ref: object) -> bool:
    """True when ``ref`` is an id ANTHROPIC minted — not one we produced.

    :data:`ANTHROPIC_MINTED_ID_RE` alone cannot tell the two apart, because
    every id this module writes is deliberately shaped to satisfy Anthropic's
    validator and therefore matches it. :data:`GATEWAY_ID_MARKER` is what
    makes the distinction decidable: our output always carries it, so an id
    with the marker is ours, and an id with a first-party prefix and no
    marker is Anthropic's.

    The residual, and it is the only one left: a vendor that natively emits
    ``srvtoolu_…`` ids of its own is indistinguishable from Anthropic here
    and reads as first-party. Nothing observed does that — the vendor shape
    in the field is ``call_…`` — and the alternative (treating an unmarked
    ``srvtoolu_`` id as suspect) would strip Anthropic's own blocks.
    """
    if not isinstance(ref, str):
        return False
    if not ANTHROPIC_MINTED_ID_RE.match(ref):
        return False
    return not _GATEWAY_MINTED_RE.match(ref)


def is_server_tool_result_type(block_type: object) -> bool:
    """True for a server-tool RESULT block (never for a client ``tool_result``)."""
    return (
        isinstance(block_type, str)
        and block_type != CLIENT_TOOL_RESULT_TYPE
        and block_type.endswith(SERVER_TOOL_RESULT_SUFFIX)
    )


@dataclass
class RepairStats:
    """What a repair pass actually did. Every field is a count of a REAL edit."""

    #: ``server_tool_use`` / ``tool_use`` ids rewritten to a conforming shape.
    ids_rewritten: int = 0
    #: ``server_tool_use`` blocks removed (name not representable).
    blocks_stripped: int = 0
    #: result blocks removed because the block they reference was removed, or
    #: because they reference nothing that exists (an orphan is its own 400).
    results_stripped: int = 0
    #: Messages removed because the repair left them with no content at all.
    messages_dropped: int = 0
    #: Distinct non-portable tool NAMES met, plus the block TYPES stripped
    #: for being outside :data:`PORTABLE_SERVER_TOOL_RESULT_TYPES`. Both are
    #: "the thing that could not be represented", which is what the log line
    #: is for; keeping them apart would mean two fields nobody reads
    #: separately.
    stripped_names: set[str] = field(default_factory=set)
    #: Message/entry indexes touched — named in the log so the user can find
    #: the turn that broke.
    touched_indexes: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.ids_rewritten
            or self.blocks_stripped
            or self.results_stripped
            or self.messages_dropped
        )

    def merge(self, other: "RepairStats") -> None:
        self.ids_rewritten += other.ids_rewritten
        self.blocks_stripped += other.blocks_stripped
        self.results_stripped += other.results_stripped
        self.messages_dropped += other.messages_dropped
        self.stripped_names |= other.stripped_names
        self.touched_indexes.extend(other.touched_indexes)

    def summary(self) -> str:
        names = ", ".join(sorted(self.stripped_names)) or "-"
        return (
            f"ids_rewritten={self.ids_rewritten} "
            f"blocks_stripped={self.blocks_stripped} "
            f"results_stripped={self.results_stripped} "
            f"messages_dropped={self.messages_dropped} "
            f"stripped_names={names}"
        )


class TranscriptRepairer:
    """Stateful across a conversation, because ids cross message boundaries.

    A ``tool_use`` block lives in an assistant message and the ``tool_result``
    that references it lives in the NEXT user message, so the mapping cannot
    be per-message. One instance therefore repairs a whole message list (or a
    whole ``.jsonl`` session, in file order).

    Args:
        id_map: new-id -> ORIGINAL-id, updated in place. The gateway passes a
            bounded map that outlives the request so the next request to that
            vendor can hand back the vendor's own ids; ``fix-transcript``
            passes a plain dict. ``None`` means "do not remember" — correct
            for a one-way repair.
        strip_nonportable: strip ``server_tool_use`` blocks whose ``name`` is
            outside :data:`PORTABLE_SERVER_TOOL_NAMES`. False keeps them (id
            still normalised) and is the setting for a vendor-bound payload,
            where the vendor's own tool names are perfectly valid.
        strip_vendor_origin: strip EVERY server-tool pair the gateway can see
            is not Anthropic's own (:func:`minted_by_anthropic`), whatever it
            is named. True only for a payload bound for ANTHROPIC —
            :func:`model_router.tool_ids.sanitise_for_anthropic` and
            ``vco fix-transcript`` — because the 2026-09-09 probe in this
            module's header shows a vendor-origin ``web_search`` pair has no
            valid form there: its result needs an ``encrypted_content`` blob
            only Anthropic issues.

            Deliberately NOT set on the vendor RESPONSE path. The client's
            next turn on that route goes back to the same vendor, which
            understands its own blocks; stripping them at response time would
            take the tool context out of a conversation that was working. The
            destination decides, which is why this is a separate knob rather
            than more behaviour hung on ``strip_nonportable``.
    """

    def __init__(
        self,
        *,
        id_map: Optional[MutableMapping[str, str]] = None,
        strip_nonportable: bool = True,
        strip_vendor_origin: bool = False,
    ) -> None:
        self._id_map = id_map
        self._strip_nonportable = strip_nonportable
        self._strip_vendor_origin = strip_vendor_origin
        #: original -> new, for results that reference an earlier block.
        self._forward: dict[str, str] = {}
        #: ids whose producing block was stripped; their results go too.
        self._dropped: set[str] = set()
        #: final ids of producers KEPT so far, in block order. A result whose
        #: reference is in here has its producer in this transcript, which is
        #: the only thing "orphan" ever meant.
        self._producers: set[str] = set()
        self.stats = RepairStats()

    # ── the one rewrite ──────────────────────────────────────────────────
    def repair_content(
        self, blocks: Any, *, role: Optional[str] = None,
    ) -> tuple[Any, bool]:
        """Rewrite one message's ``content`` list. Returns (blocks, changed).

        A non-list content (the plain-string form) is returned untouched: it
        cannot carry a tool block. ``role`` is the OWNING message's role when
        the caller knows it; ``tool_result`` placement depends on it.
        """
        if not isinstance(blocks, list):
            return blocks, False

        stats = RepairStats()
        out: list[Any] = []
        for block in blocks:
            if not isinstance(block, dict):
                out.append(block)
                continue
            kind = block.get("type")
            if kind == "server_tool_use":
                kept = self._server_tool_use(block, stats)
                if kept is not None:
                    self._remember_producer(kept.get("id"))
                    out.append(kept)
                continue
            if kind == MCP_TOOL_USE_TYPE:
                # First-party MCP-connector traffic: never rewritten, but its
                # id IS recorded, or its result reads as an orphan.
                self._remember_producer(block.get("id"))
                out.append(block)
                continue
            if is_server_tool_result_type(kind):
                kept = self._server_tool_result(block, stats)
                if kept is not None:
                    out.append(kept)
                continue
            if kind == CLIENT_TOOL_RESULT_TYPE:
                kept = self._client_tool_result(block, stats, role=role)
                if kept is not None:
                    out.append(kept)
                continue
            out.append(block)

        self.stats.merge(stats)
        return (out, True) if stats.changed else (blocks, False)

    def repair_message(
        self, message: Any, *, empty: str = EMPTY_KEEP,
    ) -> tuple[Any, bool, bool]:
        """Rewrite ``message['content']`` in a shallow copy.

        Returns ``(message, changed, drop)``. ``drop`` is True only when the
        repair emptied the content AND ``empty`` is :data:`EMPTY_DROP`: an
        assistant turn whose ONLY blocks were a vendor built-in and its result
        has nothing left to say, and the caller that can delete a message
        (an in-flight request) should. A caller that cannot — a ``.jsonl``
        line carries ``uuid``/``parentUuid`` that the next entry references —
        passes :data:`EMPTY_PLACEHOLDER` and gets a one-line text block
        instead, which keeps the chain intact and tells the reader what
        happened. Other keys are never touched.
        """
        if not isinstance(message, dict):
            return message, False, False
        role = message.get("role")
        content, changed = self.repair_content(
            message.get("content"), role=role if isinstance(role, str) else None,
        )
        if not changed:
            return message, False, False
        if isinstance(content, list) and not content:
            if empty == EMPTY_DROP:
                return message, True, True
            if empty == EMPTY_PLACEHOLDER:
                content = [{"type": "text", "text": EMPTY_PLACEHOLDER_TEXT}]
        patched = dict(message)
        patched["content"] = content
        return patched, True, False

    def repair_messages(
        self, messages: Any, *, empty: str = EMPTY_DROP,
    ) -> tuple[Any, bool]:
        """Rewrite a whole ``messages`` array (the Messages-API request shape).

        A message the repair emptied is DROPPED by default. That is safe for
        the Messages API — consecutive same-role turns are accepted (live
        probe: ``user`` followed by ``user`` returns 200) — and it is more
        honest than forwarding a turn with no content in it.
        """
        if not isinstance(messages, list):
            return messages, False
        out: list[Any] = []
        changed = False
        for index, message in enumerate(messages):
            patched, touched, drop = self.repair_message(message, empty=empty)
            if touched:
                changed = True
                self.stats.touched_indexes.append(index)
            if drop:
                self.stats.messages_dropped += 1
                continue
            out.append(patched)
        return (out, True) if changed else (messages, False)

    # ── per-block-kind helpers ───────────────────────────────────────────
    def _remember(self, original: str, new: str) -> None:
        self._forward[original] = new
        if self._id_map is not None:
            self._id_map[new] = original

    def _remember_producer(self, block_id: object) -> None:
        """Record a KEPT producer's final id, so its result is not an orphan.

        Filled in block order, which is also the order Anthropic requires
        ("each result block must have a corresponding ``server_tool_use``
        block BEFORE it"), so membership answers both questions at once.
        """
        if isinstance(block_id, str) and block_id:
            self._producers.add(block_id)

    def _server_tool_use(
        self, block: dict, stats: RepairStats,
    ) -> Optional[dict]:
        name = block.get("name")
        raw_id = block.get("id")
        unrepresentable = (
            self._strip_nonportable
            and isinstance(name, str)
            and name not in PORTABLE_SERVER_TOOL_NAMES
        )
        # Anthropic-bound, a pair we can see is not Anthropic's own goes
        # WHATEVER it is named: a portable name does not make its RESULT
        # representable, because that needs an ``encrypted_content`` blob
        # only Anthropic issues (live probe, 2026-09-09, in the header).
        vendor_origin = self._strip_vendor_origin and not minted_by_anthropic(raw_id)
        if unrepresentable or vendor_origin:
            if isinstance(raw_id, str):
                self._dropped.add(raw_id)
                self._dropped.add(normalise_id(raw_id, SERVER_TOOL_ID_PREFIX))
            stats.blocks_stripped += 1
            if isinstance(name, str):
                stats.stripped_names.add(name)
            return None
        if not isinstance(raw_id, str) or id_conforms(raw_id, SERVER_TOOL_ID_PREFIX):
            return block
        new_id = normalise_id(raw_id, SERVER_TOOL_ID_PREFIX)
        self._remember(raw_id, new_id)
        stats.ids_rewritten += 1
        patched = dict(block)
        patched["id"] = new_id
        return patched

    def _server_tool_result(
        self, block: dict, stats: RepairStats,
    ) -> Optional[dict]:
        """Keep, remap, or strip a ``<name>_tool_result`` block.

        Three ways it can be unrepresentable, and the middle one is the one
        an id-shaped fix keeps missing: the block's own TYPE. A vendor writes
        ``analyze_image_tool_result``; Anthropic's validator knows a closed
        set of result types (:data:`PORTABLE_SERVER_TOOL_RESULT_TYPES`) and
        rejects the rest whatever id they carry — so a block whose id already
        conforms, and whose producer is not in this transcript to be stripped
        alongside it, used to sail through here and poison the session
        exactly as the id did.

        The type check is gated on the id NOT being Anthropic's, which makes
        the rule asymmetric on purpose: an unknown type beside an id
        Anthropic minted is far more likely to be a server tool it shipped
        after this table was written than a vendor's invention, and removing
        it would break a working transcript in precisely the way this module
        exists to prevent. A vendor block this module already normalised is
        NOT such a case — :data:`GATEWAY_ID_MARKER` makes it recognisable —
        and is stripped. The one residual left is stated where it is
        decided, at :func:`minted_by_anthropic`.
        """
        ref = block.get("tool_use_id")
        if isinstance(ref, str) and ref in self._dropped:
            stats.results_stripped += 1
            return None
        kind = block.get("type")
        first_party = minted_by_anthropic(ref)
        if (
            self._strip_nonportable
            and isinstance(kind, str)
            and kind not in PORTABLE_SERVER_TOOL_RESULT_TYPES
            and not first_party
        ):
            stats.results_stripped += 1
            stats.stripped_names.add(kind)
            return None
        if isinstance(ref, str) and ref in self._forward:
            stats.ids_rewritten += 1
            patched = dict(block)
            patched["tool_use_id"] = self._forward[ref]
            return patched
        if isinstance(ref, str) and ref not in self._producers and not first_party:
            # No producer for this id in this transcript, and the id is not
            # Anthropic's. An orphan server-tool result is its own 400 —
            # "must have a corresponding server_tool_use block before it" —
            # so it goes.
            #
            # Membership in ``_producers`` is the test, not the id's SHAPE:
            # a transcript this module already repaired carries our own
            # ``srvtoolu_vct_…`` ids on BOTH blocks, and shape alone would
            # read the result as vendor-origin and strip a pair that is
            # perfectly consistent. An Anthropic-minted id with no producer
            # here is kept — that is somebody else's valid content and the
            # conservative direction.
            stats.results_stripped += 1
            return None
        return block

    def _client_tool_result(
        self, block: dict, stats: RepairStats, *, role: Optional[str] = None,
    ) -> Optional[dict]:
        """The block the FIELD incident actually died on.

        The vendor writes its built-in's result as a plain ``tool_result``
        inside the ASSISTANT message (observed verbatim in the poisoned
        session: ``server_tool_use analyze_image id=call_5f46…`` followed by
        ``{"type":"tool_result","tool_use_id":"call_5f46…"}`` under the same
        ``message.id``). Two live 400s govern what happens to it:

        * stripping the producer and leaving this behind ->
          ``messages.1: `tool_result` blocks can only be in `user` messages``;
        * so does leaving it when the producer was KEPT — the placement is
          illegal in an assistant message either way, which is why the role
          test is unconditional rather than only firing for a dropped id.

        In a USER message it is ordinary client-tool traffic and stays, with
        its reference remapped if the block it names was renamed.
        """
        ref = block.get("tool_use_id")
        if isinstance(ref, str) and ref in self._dropped:
            stats.results_stripped += 1
            return None
        if role == "assistant":
            stats.results_stripped += 1
            return None
        if isinstance(ref, str) and ref in self._forward:
            stats.ids_rewritten += 1
            patched = dict(block)
            patched["tool_use_id"] = self._forward[ref]
            return patched
        return block


def _mentions_mapped_id(node: Any, id_map: MutableMapping[str, str]) -> bool:
    """Read-only probe: does ``node`` carry any id present in ``id_map``?"""
    if isinstance(node, list):
        return any(_mentions_mapped_id(item, id_map) for item in node)
    if not isinstance(node, dict):
        return False
    for key, value in node.items():
        if key in ("id", "tool_use_id") and isinstance(value, str):
            if value in id_map:
                return True
        elif _mentions_mapped_id(value, id_map):
            return True
    return False


def restore_ids(payload: Any, id_map: MutableMapping[str, str]) -> tuple[Any, int]:
    """Put the VENDOR's own ids back, everywhere they appear in ``payload``.

    The inverse of a repair pass, for the request leg back to the vendor that
    produced the ids: the gateway rewrote them on the way out, and a vendor
    that keys anything off its own ids must not be handed ours. Walks the
    whole document (bounded by the payload itself) rather than assuming a
    message shape, because the id fields are what matter and they are always
    named ``id`` / ``tool_use_id``.

    Returns ``(payload, restored_count)``; the input is not mutated when
    nothing matched. A read-only scan runs FIRST so the common case — a
    request with no rewritten id in it — costs a walk and not a full copy of
    a transcript that can be megabytes.
    """
    if not _mentions_mapped_id(payload, id_map):
        return payload, 0

    restored = 0

    def walk(node: Any) -> Any:
        nonlocal restored
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for key, value in node.items():
            if key in ("id", "tool_use_id") and isinstance(value, str):
                original = id_map.get(value)
                if original is not None and original != value:
                    restored += 1
                    out[key] = original
                    continue
                out[key] = value
                continue
            out[key] = walk(value)
        return out

    walked = walk(payload)
    return (walked, restored) if restored else (payload, 0)


# ── .jsonl session repair (``vco fix-transcript``) ───────────────────────

#: Entry types whose ``message.content`` can carry tool blocks.
REPAIRABLE_ENTRY_TYPES = frozenset({"assistant", "user"})


@dataclass
class FileRepairResult:
    """Outcome of one ``fix-transcript`` run."""

    path: Path
    entries_total: int = 0
    entries_touched: int = 0
    unparseable_lines: int = 0
    stats: RepairStats = field(default_factory=RepairStats)
    backup_path: Optional[Path] = None
    dry_run: bool = False

    def render(self) -> str:
        lines = [
            f"{self.path}",
            f"  entries            : {self.entries_total}",
            f"  entries touched    : {self.entries_touched}",
            f"  ids rewritten      : {self.stats.ids_rewritten}",
            f"  blocks stripped    : {self.stats.blocks_stripped}",
            f"  results stripped   : {self.stats.results_stripped}",
        ]
        if self.stats.stripped_names:
            lines.append(
                "  stripped tools     : "
                + ", ".join(sorted(self.stats.stripped_names))
            )
        if self.unparseable_lines:
            lines.append(
                f"  unparseable lines  : {self.unparseable_lines} (left verbatim)"
            )
        if self.dry_run:
            lines.append("  DRY RUN — nothing was written")
        elif self.backup_path is not None:
            lines.append(f"  backup             : {self.backup_path}")
        else:
            lines.append("  nothing to do — file unchanged")
        return "\n".join(lines)


def repair_entry(
    entry: Any, repairer: TranscriptRepairer,
) -> tuple[Any, bool]:
    """Repair one ``.jsonl`` entry. ``uuid``/``parentUuid``/timestamps untouched.

    Only ``message.content`` is rewritten, and only in a shallow copy, so an
    entry the repairer does not recognise survives byte-for-byte.
    """
    if not isinstance(entry, dict):
        return entry, False
    if entry.get("type") not in REPAIRABLE_ENTRY_TYPES:
        return entry, False
    message = entry.get("message")
    # EMPTY_PLACEHOLDER, never EMPTY_DROP: this entry's ``uuid`` is the next
    # line's ``parentUuid``. Deleting the line would break the chain the
    # client walks to rebuild the conversation.
    patched_message, changed, _drop = repairer.repair_message(
        message, empty=EMPTY_PLACEHOLDER,
    )
    if not changed:
        return entry, False
    patched = dict(entry)
    patched["message"] = patched_message
    return patched, True


@dataclass(frozen=True)
class LineOutcome:
    """What happened to one input line."""

    text: str
    changed: bool = False
    unparseable: bool = False
    #: False for a blank line — it is neither an entry nor a failure.
    is_entry: bool = False


def repair_lines(
    lines: Iterable[str], repairer: TranscriptRepairer,
) -> Iterator[LineOutcome]:
    """Stream one :class:`LineOutcome` per input line.

    Streaming rather than list-returning because a real session file reaches
    hundreds of megabytes; ``fix-transcript`` must never hold one in memory.
    An UNCHANGED line is yielded verbatim — not re-serialised — so a repaired
    file diffs against its backup on exactly the entries that were repaired.
    """
    for line in lines:
        body = line.rstrip("\r\n")
        terminator = line[len(body):]
        if not body.strip():
            yield LineOutcome(line)
            continue
        try:
            entry = json.loads(body)
        except ValueError:
            yield LineOutcome(line, unparseable=True, is_entry=True)
            continue
        patched, changed = repair_entry(entry, repairer)
        if not changed:
            yield LineOutcome(line, is_entry=True)
            continue
        # The ORIGINAL terminator, not a fresh "\n": a CRLF file must not come
        # back half-converted, and the last line of a file that ended without
        # a newline must not grow one.
        yield LineOutcome(
            json.dumps(patched, ensure_ascii=False) + terminator,
            changed=True,
            is_entry=True,
        )


class _NothingToDo(Exception):
    """Internal: abandon the rewrite because the pass changed nothing."""


#: How many same-second suffixes are tried before giving up. A user running
#: this eleven times inside one second is not a case worth encoding; a user
#: running it twice is (a dry run, then the real one, then a re-run after
#: reading the summary).
_MAX_BACKUP_ATTEMPTS = 10


def _free_backup_path(path: Path) -> Path:
    """``<file>.bak-<timestamp>``, suffixed if that name is already taken.

    The timestamp has SECOND granularity, so two repairs of the same file
    inside one second want the same name — and
    :func:`vco_lib.atomic.atomic_text_stream` refuses to overwrite a backup
    (rightly: the first one is the one that holds the pre-repair bytes).
    Picking a free name here is what keeps that refusal from turning a
    legitimate second run into a hard failure.

    Racy in the strict sense — another process could take the name between
    this check and the link — and that is fine: the link then raises
    ``FileExistsError``, which is a loud, correct failure rather than a
    silently destroyed backup.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    for attempt in range(2, _MAX_BACKUP_ATTEMPTS + 1):
        if not candidate.exists():
            return candidate
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{attempt}")
    return candidate


def repair_file(path: Path, *, dry_run: bool = False) -> FileRepairResult:
    """Repair a Claude Code session ``.jsonl`` in place, after backing it up.

    Streaming end to end — a real session file reaches hundreds of megabytes
    and must never be held in memory — through
    :func:`vco_lib.atomic.atomic_text_stream`, which owns BOTH renames: the
    existing file becomes ``<file>.bak-<ts>`` and the rewritten copy takes its
    place. Both are O(1), so backing up a gigabyte costs nothing.

    Nothing is renamed at all when the pass changes nothing: the rewrite is
    abandoned from inside the context manager, which cleans its tempfile up
    and leaves the directory exactly as it was. Running this on a healthy
    session is therefore a no-op you can repeat.

    A live session (Claude Code appending to the same file) is out of scope
    and cannot be made safe from here: repair a session the client has closed.

    The repaired file lands owner-only (0600, from the tempfile ``mkstemp``
    creates): a NARROWING of a private session transcript's permissions,
    never a widening. The backup keeps the original's mode — it is a hard
    link to the original inode.
    """
    result = FileRepairResult(path=path, dry_run=dry_run, stats=RepairStats())
    repairer = TranscriptRepairer(
        id_map={}, strip_nonportable=True, strip_vendor_origin=True,
    )

    def pump(handle: Optional[Any]) -> None:
        # surrogateescape, not replace: a byte sequence that is not valid
        # UTF-8 round-trips unchanged through the untouched lines instead of
        # being rewritten as U+FFFD. A repair tool that silently edits bytes
        # it was not asked to touch is not a repair tool.
        with path.open(
            "r", encoding="utf-8", errors="surrogateescape", newline="",
        ) as src:
            for outcome in repair_lines(src, repairer):
                if outcome.is_entry:
                    result.entries_total += 1
                if outcome.changed:
                    result.entries_touched += 1
                if outcome.unparseable:
                    result.unparseable_lines += 1
                if handle is not None:
                    handle.write(outcome.text)

    if dry_run:
        pump(None)
        result.stats = repairer.stats
        return result

    backup = _free_backup_path(path)
    try:
        with atomic_text_stream(
            path, backup=backup, errors="surrogateescape",
        ) as handle:
            pump(handle)
            if not result.entries_touched:
                raise _NothingToDo
    except _NothingToDo:
        result.stats = repairer.stats
        return result
    result.stats = repairer.stats
    result.backup_path = backup
    return result


__all__ = [
    "CLIENT_TOOL_RESULT_TYPE",
    "EMPTY_DROP",
    "EMPTY_KEEP",
    "EMPTY_PLACEHOLDER",
    "EMPTY_PLACEHOLDER_TEXT",
    "ANTHROPIC_MINTED_ID_RE",
    "GATEWAY_ID_MARKER",
    "MCP_TOOL_USE_TYPE",
    "PORTABLE_SERVER_TOOL_NAMES",
    "PORTABLE_SERVER_TOOL_RESULT_TYPES",
    "SERVER_TOOL_RESULT_TYPE_BY_NAME",
    "REPAIRABLE_ENTRY_TYPES",
    "SERVER_TOOL_ID_PREFIX",
    "SERVER_TOOL_RESULT_SUFFIX",
    "FileRepairResult",
    "LineOutcome",
    "RepairStats",
    "TranscriptRepairer",
    "id_conforms",
    "is_server_tool_result_type",
    "minted_by_anthropic",
    "normalise_id",
    "repair_entry",
    "repair_file",
    "repair_lines",
    "restore_ids",
    "sanitise_id_body",
]
