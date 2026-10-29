#!/usr/bin/env python3
# Copyright (C) 2026 VibeCoded Tools — AGPL-3.0-or-later
"""Turn-end deferred-citation drain (F-QUEUE, v0.2.70).

Invoked by the Stop hook (``templates/hooks/stop-drain-citations.{sh,ps1}``)
with the session_id + transcript_path Claude Code delivers on Stop stdin. For
every staged pending file belonging to the session it:

  1. Loads the transcript ONCE.
  2. Maps each pending file's (query, seq) to its KG-call position. For a
     hook-source payload whose hook-derived query has no matching KG tool_use
     in the transcript (the ~87% hook cohort), falls back to a TIMESTAMP
     anchor — the first assistant message at/after the retrieval's ts_ms —
     so that cohort's citation labels are recovered rather than lost
     (v0.2.77 9-bis).
  3. Extracts the cumulative answer window from that position to end-of-
     transcript (accumulates across human turns — the V52-N behaviour).
  4. ⚠️ ACCUMULATE-DON'T-DROP: if the window is still BELOW the token gate,
     LEAVES the pending file for the next Stop (does NOT compute, does NOT
     delete) so it keeps accumulating into subsequent turns.
  5. At/above the gate: computes the citation via the shared
     ``citation_compute.compute_citation`` (one home with the MCP monitor),
     writes the event, then deletes the pending file (one-shot).
  6. NO scoring deadline (v0.2.102 W6-RL2, owner rulings R7-3 / R7-4). A
     file waits for its OWN chat's end: the final-window (terminal /
     soft-label) floors apply only when that chat has really ended — a
     subagent's SubagentStop drain, the main chat's SessionEnd drain
     (``--session-end``), or any later drain finalizing a session whose end
     was recorded. A citation is scored only against its own chat's
     transcript (same transcripts folder); a file whose chat cannot be
     found stays unscored and is logged, never scored against another.
  7. Garbage-collects only PROVABLY abandoned files (transcript idle for
     ``RL_CITATION_ABANDONED_DAYS``, default 7 days, and no live owner):
     each is scored first when its transcript is readable, else recorded
     as unscored (``citation_pending.collect_abandoned``).

This RECOVERS hook-path citations (≈72% of all retrievals) the doomed in-process
asyncio monitor never could. NO new hub route, NO new launcher.db table — the
compute reuses the existing ``POST /api/v1/rl/events`` via the telemetry writer.

The ONE thing most likely to be gotten wrong (per the queue design): transcript
timing. The Stop hook fires the instant Claude stops; the last assistant text
block may not be flushed yet. We treat a below-gate window as "retry later"
(leave the file), never "lost".
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Allow ``from claude_mcp_servers...`` imports when run as a bare script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO_ROOT, os.path.join(_HERE, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.env_values import env_int  # noqa: E402 — after the path bootstrap above

logger = logging.getLogger(__name__)


def _gate_tokens() -> int:
    """Resolve the citation token gate, honouring the same env override the
    MCP monitor uses so a tuned floor applies to both paths."""
    return env_int("RL_MIN_ANSWER_TOKENS_FOR_CITATION", 25000)


# v0.2.73 RL-4 — terminal-session citation floor.
#
# Sessions whose cumulative answer window never reaches the 25k gate were
# NEVER labeled. The terminal floor gives such a window a second chance: once
# the window is FINAL (nothing more will accumulate into it) and clears the
# LOWER RL_TERMINAL_CITATION_MIN_TOKENS floor (default 2000 — enough chunks
# for max-over-chunks cosine to discriminate), the drain computes + writes +
# deletes instead of leaving it.
#
# v0.2.102 W6-RL2 (owner ruling R7-4): "final" means the owning chat has
# really ended — SubagentStop, SessionEnd, or a recorded session end. Until
# then a sub-gate window keeps accumulating, however old the file is. The
# file-age trigger (RL_TERMINAL_CITATION_AGE_SECONDS, default 1800 s) that
# applied these floors once a file was half an hour old is SUPERSEDED by
# those end events: a fixed age forced partial scores on chats that were
# still running (agents run for hours), so the key is no longer read.

def _terminal_floor_tokens() -> int:
    return env_int("RL_TERMINAL_CITATION_MIN_TOKENS", 2000)


# G5 (2026-07-22) — soft-label floor for final-but-tiny answer windows.
#
# A pending file whose answer NEVER clears the 2 000-token terminal floor used
# to be left every drain until it was deleted UNLABELED. That censored the
# corpus toward long answers and starved the mcp_interactive cohort (5
# citations ever). Instead of dropping such a retrieval entirely, once its
# window is final (W6-RL2: its chat ended) we compute a SOFT-labeled citation
# from whatever answer
# window exists (as long as it clears a much lower floor — enough text for a
# max-over-chunks cosine to mean anything). The event is marked ``soft_label:true``
# + ``fire_reason=soft_terminal`` so the trainer can down-weight it (it is a
# weaker signal than a full-window label) while still learning the retrieval's
# below-threshold cosine maxima instead of seeing nothing.
def _soft_label_floor_tokens() -> int:
    return env_int("RL_SOFT_CITATION_MIN_TOKENS", 200)


def _stamp_agent_transcript(path, payload: dict, transcript_path: str) -> None:
    """Record the agent's own transcript in its pending file, so a later
    (parent) Stop drain can retry the file against the RIGHT answer source.
    Best-effort: a failed write only means the parent drain leaves the file
    until the session's end finds the agent transcript beside the parent's."""
    try:
        from claude_mcp_servers.rl_client.citation_pending import (
            AGENT_WINDOW_FINAL_KEY,
            write_pending,
        )

        payload["agent_transcript_path"] = transcript_path
        # The agent's own drain stamps it: its window is final now (R5-9).
        payload.pop(AGENT_WINDOW_FINAL_KEY, None)
        write_pending(path, payload)
    except Exception:  # noqa: BLE001 — telemetry must not break the drain
        pass


def _score_payload(
    payload: dict,
    messages: list,
    kg_positions: list,
    *,
    final_window: bool,
    session_id: str,
    gate: int,
    compute_fn,
    token_count_fn,
) -> "tuple[str, str]":
    """Score ONE claimed pending payload against an already-loaded answer
    transcript — the one scoring path, shared by every drain pass and the
    collector (v0.2.102 W6-R6FIX, review R6-2).

    ``final_window``: the owning chat has really ended (a stopped subagent,
    an ended session, an abandoned transcript), so nothing will accumulate
    into the window and a below-gate window takes the terminal/soft floors
    now. Otherwise only the full gate fires (W6-RL2: never because time
    passed).

    Returns ``(outcome, reason)``: ``("computed", fire_reason)`` when a
    citation was written; ``("skipped", reason)`` for a payload that can
    never be scored; ``("left", reason)`` otherwise (a live chat's file is
    handed back; a finalized one may be recorded as unscored). Never raises
    for a ``compute_fn`` failure (that is ``("left", "compute_failed")``).
    """
    from claude_mcp_servers.rl_client.answer_window import (
        extract_answer_window,
        match_position_by_timestamp,
        match_position_for_query,
    )

    ctx = payload.get("ctx")
    task_id = payload.get("task_id") or ""
    if not isinstance(ctx, dict) or not task_id:
        return "skipped", "bad_payload"

    if not messages:
        # No transcript to read the answer from — leave for the next drain.
        return "left", "no_transcript"

    query_snippet = (payload.get("query") or "")[:120]
    seq = payload.get("seq")
    pos_idx = (seq - 1) if isinstance(seq, int) and seq > 0 else None
    matched = match_position_for_query(
        messages, kg_positions, query_snippet, pos_idx
    )
    if matched is None:
        # v0.2.77 9-bis — hook-cohort citation-label recovery.
        #
        # The hook path (task_type "pre_edit_kg_search", source "hook") stages
        # a hook-DERIVED query that never appears verbatim as a KG tool_use in
        # the transcript, so match_position_for_query can NEVER locate it and
        # ~87% of retrievals (the hook cohort) died unlabeled — defeating F-QUEUE's stated purpose of recovering exactly that
        # cohort. When the query-match fails, anchor the answer window by
        # TIMESTAMP: the first assistant message stamped at/after the
        # retrieval's ts_ms is the start of the answer it fed into. The window
        # then flows through the SAME gate + terminal floor below — no schema
        # change, no opt-out change, only a different anchor for a payload the
        # query-matcher structurally can't serve.
        #
        # G5 (2026-07-22): the timestamp fallback now ALSO covers the ``mcp``
        # source. Previously it was hook-only, so an mcp_interactive retrieval
        # whose query-match failed (or whose in-process monitor never fired —
        # answers rarely reach the 25k gate) was left to die unlabeled:
        # mcp_interactive had FIVE citation events ever vs. 1 028 hook, the
        # single worst under-labeled cohort. The mcp monitor consumes its own
        # pending file when its write lands, so a surviving mcp pending file
        # here means the monitor did NOT score it — exactly the case the drain
        # must recover.
        # Timestamp anchoring is safe for mcp too (the retrieval's ts_ms is
        # stamped identically); the terminal floor below still gates it so a
        # too-short answer accumulates rather than writing noise.
        if payload.get("source") in ("hook", "mcp"):
            matched = match_position_by_timestamp(messages, payload.get("ts_ms"))
        if matched is None:
            # Could not locate this search in the transcript yet — leave it.
            return "left", "no_anchor"

    start_msg_idx, start_blk_idx = matched
    answer, _complete = extract_answer_window(messages, start_msg_idx, start_blk_idx)
    tok = token_count_fn(answer) if answer else 0

    fire_reason = "stop_drain"
    soft_label = False
    if not answer.strip() or tok < gate:
        # v0.2.73 RL-4 / W6-RL2: a FINAL window (the owning chat ended —
        # SubagentStop, SessionEnd, a recorded session end, an abandoned
        # transcript) that clears the LOWER terminal floor is computed now.
        # A live chat's window keeps the accumulate-don't-drop behaviour,
        # however old the file is.
        _final = bool(answer.strip()) and final_window
        _terminal = _final and tok >= _terminal_floor_tokens()
        if _terminal:
            fire_reason = "terminal_floor"
        elif _final and tok >= _soft_label_floor_tokens():
            # G5: the final window will never clear the terminal floor. Emit
            # a SOFT-labeled citation from the below-threshold window (its
            # cosine maxima ARE a signal, just a weaker one) rather than
            # dropping the retrieval unlabeled. Marked so the trainer can
            # down-weight it.
            fire_reason = "soft_terminal"
            soft_label = True
        else:
            # ⚠️ ACCUMULATE-DON'T-DROP: below every floor → keep the file so
            # it can keep accumulating into subsequent turns. Never compute,
            # never delete here. Compute+delete happens only at/above a gate
            # (25k, or the terminal / soft-label floor once the window is
            # final).
            return "left", "below_floor"

    # At/above a gate → compute + write (the caller finishes: one-shot).
    # v0.2.73 RL-6/RL-9: stamp the riders onto ctx so the citation event
    # stores where/why it fired. session_id prefers the value staged in
    # the ctx (RL-9 stage-time resolution), then the pending payload —
    # explicit falsy check (NOT setdefault): a staged EMPTY string must
    # not shadow the payload's real session id.
    if not ctx.get("session_id"):
        ctx["session_id"] = payload.get("session_id") or session_id or ""
    ctx["fire_reason"] = fire_reason
    ctx["window_tokens"] = tok
    # G5: propagate the soft-label marker into the ctx so the citation event
    # carries it (compute_citation forwards ctx fire_reason; the writer stores
    # fire_reason verbatim, and the trainer can key its down-weight on
    # fire_reason == "soft_terminal").
    if soft_label:
        ctx["soft_label"] = True
    try:
        result = compute_fn(task_id, answer, ctx, write=True)
    except Exception:
        result = None
    if result is None:
        # Compute soft-failed (no embedding service / no signal). The drain
        # leaves the file for retry; only the collector ever gives up on it.
        return "left", "compute_failed"
    return "computed", fire_reason


def _query_in_transcript(messages: list, kg_positions: list, snippet: str,
                         cli_queries: "frozenset[str] | None" = None) -> bool:
    """True when one of the transcript's KG retrievals carries exactly this
    staged query (its first 120 characters, as staged). How a pending file
    staged WITHOUT a session id is attributed to its chat (v0.2.102 W6-RL2,
    owner ruling R7-3): the MCP path stages the literal query of the chat's
    own ``hybrid_search`` call, the ``kg-search`` CLI (v0.2.105) the query of
    the chat's own Bash / PowerShell ``kg-search search <query>`` call —
    neither process is told which chat (or subagent) calls it — so the chat
    whose transcript holds the call is the chat that produced the retrieval.
    Exact prefix equality, not the matcher's substring rule, so a short query
    never claims another chat's longer one. ``cli_queries``: this transcript's
    :func:`answer_window.find_cli_kg_queries`, when the caller cached it."""
    snippet = (snippet or "")[:120]
    if not snippet.strip():
        return False
    for mi, bi in kg_positions:
        try:
            blk = messages[mi].get("message", {}).get("content", [])[bi]
            q = blk.get("input", {}).get("query", "") if isinstance(blk, dict) else ""
        except (IndexError, AttributeError, TypeError):
            continue
        if isinstance(q, str) and q and q[:120] == snippet:
            return True
    if cli_queries is None:
        from claude_mcp_servers.rl_client.answer_window import find_cli_kg_queries

        cli_queries = find_cli_kg_queries(messages)
    return snippet in cli_queries


# A finalized file (its chat ended) whose score failed for one of these
# reasons will never score: nothing more is coming into its window. It is
# removed and recorded as unscored. Any other reason (an unreadable or empty
# transcript, a failed compute) is left for a later drain / the collector.
_FINAL_UNSCORABLE = frozenset({"below_floor", "no_anchor", "bad_payload"})


def drain_session(
    session_id: str,
    transcript_path: "str | None",
    *,
    agent_id: str = "",
    session_end: bool = False,
    project_root: "str | None" = None,
    compute_fn=None,
    token_count_fn=None,
) -> dict:
    """Drain all pending citations for ``session_id``.

    Args:
        session_id: From Stop / SessionEnd stdin (the per-conversation key).
        transcript_path: From the hook stdin; the JSONL to read the answer
            from. When None we cannot compute (no answer source).
        agent_id: (v0.2.102 W6-C2, census F12) set by a SubagentStop drain:
            drain ONLY that subagent's pending files, against
            ``transcript_path`` = the payload's ``agent_transcript_path``.
            A subagent's hook events carry the PARENT's session_id and
            transcript_path, so its staged retrievals used to be scored
            against the parent's answer. Routing rules, per pending file:

            * agent drain (``agent_id`` set): files tagged with that agent
              only; the transcript is FINAL (the subagent has stopped), so a
              below-gate window takes the terminal/soft floors immediately
              instead of waiting to accumulate — no later turn will add to
              it. A file left behind is stamped with the agent transcript.
            * parent drain (``agent_id`` empty): untagged files as before;
              an agent-tagged file is scored ONLY against its stamped
              ``agent_transcript_path`` (final semantics) and is otherwise
              left — never against the parent's.
        session_end: (v0.2.102 W6-RL2, owner ruling R7-4) set by the
            SessionEnd drain: the main chat has really ended. Its end is
            RECORDED first (``citation_pending.mark_session_ended``) so a
            drain that cannot finish still leaves the record, then its files
            are scored with the final-window floors; an agent file not yet
            stamped is looked up beside the session transcript (the session's
            agents are over too). Nothing is deleted unscored here: a file
            this drain cannot score is left, and the next drain of ANY
            session finalizes it (the transcript is then fully flushed).
        project_root: Override for tests; defaults to CLAUDE_PROJECT_DIR / cwd.
        compute_fn: Injectable ``(task_id, answer, ctx) -> result|None`` for
            tests; defaults to the shared ``citation_compute.compute_citation``.
        token_count_fn: Injectable ``(text) -> int`` for tests; defaults to the
            char/4 estimate (the drain avoids importing the heavy chunker).

    Same chat only (owner ruling R7-3): a file with a session id belongs to
    that session's drains alone; a file staged without one (the MCP path)
    belongs to the chat whose transcript holds its query
    (:func:`_query_in_transcript`). Other sessions' files are touched only by
    two passes, and only against their OWN transcripts (same folder, never a
    search): the finalize pass, for sessions whose end was recorded, and the
    collector, for provably abandoned files (``collect_abandoned``). No pass
    ever applies the final floors because time passed.

    Every scorer finishes a landed write through
    ``citation_pending.finish_scored``, which also consumes a re-stage of the
    same task that landed during the compute (review R7-1: one event).

    Returns a summary dict: ``{computed, left, skipped, readopted, finalized,
    finalized_unscored, collected, collected_scored, collected_unscored}``
    (``readopted`` = stale claims handed back; ``finalized`` = ended
    sessions' files scored, ``finalized_unscored`` = ended sessions' files
    that can never score, removed and recorded in
    ``.claude/logs/rl-citations-unscored.jsonl``; ``collected`` = abandoned
    files removed, ``collected_scored`` of them scored first).
    """
    from claude_mcp_servers.rl_client.answer_window import (
        load_messages_cached,
        find_kg_positions,
        find_cli_kg_queries,
        token_estimate,
    )
    from claude_mcp_servers.rl_client.citation_pending import (
        NO_SESSION_PREFIX,
        AGENT_WINDOW_FINAL_KEY,
        list_pending_for_session,
        read_pending,
        read_pending_header,
        delete_pending,
        claim_pending,
        release_claim,
        finish_scored,
        readopt_stale_claims,
        agent_transcript_for,
        session_transcript_for,
        mark_session_ended,
        clear_session_ended,
        ended_sessions,
        session_has_pending,
        record_unscored,
        collect_abandoned,
        pending_dir,
    )

    started = time.time()
    if compute_fn is None:
        from claude_mcp_servers.rl_client.citation_compute import compute_citation as compute_fn
    if token_count_fn is None:
        token_count_fn = token_estimate

    if agent_id:
        # The staged tag went through the ONE agent-id rule; compare like
        # with like (a raw hook value could differ only by sanitisation).
        from vco_lib.hook_agent_key import sanitize_agent_id

        agent_id = sanitize_agent_id(agent_id)

    gate = _gate_tokens()
    summary = {"computed": 0, "left": 0, "skipped": 0, "readopted": 0,
               "finalized": 0, "finalized_unscored": 0,
               "collected": 0, "collected_scored": 0, "collected_unscored": 0}
    known = transcript_path or ""

    # v0.2.73 (Concern-B) / W6-MEM: shared cached loader (one home with the
    # MCP monitor); the drain is a one-shot process so its cache starts empty,
    # but routing through the SAME function keeps the (slim) parsed-message
    # shape — and thus the extracted answer window — byte-identical across
    # both paths. W6-C2 F12: one parse per DISTINCT transcript.
    _loaded: dict = {}

    def _load(tp: str):
        if tp not in _loaded:
            msgs = load_messages_cached(tp) if tp else []
            _loaded[tp] = (msgs, find_kg_positions(msgs) if msgs else [])
        return _loaded[tp]

    # v0.2.105: each loaded transcript's CLI kg-search queries, computed once
    # on first use (keyed by the list's id: ``_loaded`` keeps it alive).
    _cli_by_id: dict = {}

    def _holds_query(messages: list, kg_positions: list, query: str) -> bool:
        key = id(messages)
        if key not in _cli_by_id:
            _cli_by_id[key] = find_cli_kg_queries(messages) if messages else frozenset()
        return _query_in_transcript(messages, kg_positions, query, _cli_by_id[key])

    def _answer_source(payload: dict) -> str:
        """The transcript of the chat that staged ``payload`` — the agent's
        stamped transcript, else the agent transcript beside its session's,
        else its session's transcript in this drain's transcripts folder; a
        file with no session id is this drain's chat only when this chat's
        transcript holds its query (so the collector sees that chat as live).
        ``""`` when that chat cannot be found."""
        file_session = str(payload.get("session_id") or "")
        file_agent = str(payload.get("agent_id") or "")
        if file_agent:
            stamped = str(payload.get("agent_transcript_path") or "")
            if stamped:
                return stamped
            sess = session_transcript_for(known, file_session) if file_session else ""
            return agent_transcript_for(sess, file_agent) if sess else ""
        if file_session:
            return session_transcript_for(known, file_session)
        messages, kg_positions = _load(known)
        return known if _holds_query(
            messages, kg_positions, str(payload.get("query") or "")) else ""

    def _ended_owner(payload: object) -> "tuple[str, str]":
        """``(session, transcript)`` of the ENDED session a file staged
        without a session id belongs to: the session with an end record whose
        own transcript (same folder, :func:`session_transcript_for` — never a
        search elsewhere) holds the file's exact query. ``("", "")`` when no
        ended session holds it, or the file has a session / agent of its own
        (those resolve through :func:`_answer_source`). Review R9 F2: such a
        file can sit under the MCP monitor's claim while its chat's SessionEnd
        drain lists the folder; without this, no drain ever attributed it
        again and the collector logged it ``no_transcript``."""
        if not isinstance(payload, dict) or payload.get("session_id") or payload.get("agent_id"):
            return "", ""
        query = str(payload.get("query") or "")
        for sid in ended_sessions(project_root):
            tp = session_transcript_for(known, sid)
            if tp:
                messages, kg_positions = _load(tp)
                if _holds_query(messages, kg_positions, query):
                    return sid, tp
        return "", ""

    def _collector_source(payload: dict) -> str:
        """The collector's resolver: the running chat's rule first, then an
        ended session's own transcript for a file staged without a session id
        (review R9 F2), before the collector falls back to ``no_transcript``."""
        return _answer_source(payload) or _ended_owner(payload)[1]

    def _score_final(_claimed, payload: dict, source: str,
                     owner: str = "") -> "str | None":
        """Score a file whose chat has ended (finalize pass, collector):
        final floors, its own transcript. ``owner`` names the ended session a
        file without a session id was attributed to. ``None`` = a citation
        landed."""
        messages, kg_positions = _load(source)
        if not owner and source != known:
            owner = _ended_owner(payload)[0]
        outcome, reason = _score_payload(
            payload, messages, kg_positions,
            final_window=True,
            session_id=str(payload.get("session_id") or "") or owner or session_id,
            gate=gate, compute_fn=compute_fn, token_count_fn=token_count_fn,
        )
        return None if outcome == "computed" else reason

    def _finalize_claimed(path, claimed, payload: dict, source: str, owner: str) -> bool:
        """Score one claimed file of an ended session with the final floors
        and finish it. Returns whether the claim was consumed (scored, or
        removed as never-scorable and recorded); ``False`` = hand it back."""
        reason = _score_final(claimed, payload, source, owner)
        if reason is None:
            summary["finalized"] += 1
            finish_scored(claimed, path)
            return True
        if reason in _FINAL_UNSCORABLE:
            summary["finalized_unscored"] += 1
            delete_pending(claimed)
            record_unscored(path.parent, path.name, payload, reason,
                            when="session_ended")
            return True
        return False

    def _holds_nosession(sid: str, tp: str) -> bool:
        """Whether a file staged without a session id — pending OR under
        another process's claim (the MCP monitor's) — belongs to ended
        session ``sid``: its query is in ``sid``'s own transcript ``tp``.
        ``True`` when ``tp`` is unknown and such files exist (the record is
        kept rather than lost: a later drain that knows the transcript folder
        attributes them). The end record stays while this holds, so a file
        the monitor hands back is finalized by the next drain."""
        d = pending_dir(project_root)
        if d is None or not d.exists():
            return False
        try:
            files = list(d.glob(f"{NO_SESSION_PREFIX}__*.json*"))
        except OSError:
            return True
        if not files:
            return False
        if not tp:
            return True
        messages, kg_positions = _load(tp)
        for f in files:
            payload = read_pending_header(f)
            if (isinstance(payload, dict) and not payload.get("session_id")
                    and not payload.get("agent_id")
                    and _holds_query(messages, kg_positions,
                                             str(payload.get("query") or ""))):
                return True
        return False

    # W6-C2 fix round (review R4c-1): a claim whose drain died mid-compute is
    # handed back first, so the crash costs a retry, never the citation.
    summary["readopted"] = readopt_stale_claims(project_root)

    # W6-RL2: the main chat's end is recorded FIRST, so a SessionEnd drain
    # that cannot finish still leaves it for the next drain. A plain Stop
    # drain means the session is live (a resumed chat): its old end record
    # goes — unless it is newer than this drain (a SessionEnd that followed
    # this very turn while this drain was starting).
    if session_id and not agent_id:
        if session_end:
            mark_session_ended(session_id, project_root)
        else:
            clear_session_ended(session_id, project_root, older_than=started)

    pending = list_pending_for_session(session_id, project_root) if session_id else []
    # Files staged without a session id (the MCP path): ours only when this
    # chat's transcript holds their query (attributed below).
    pending += list_pending_for_session(NO_SESSION_PREFIX, project_root)

    for path in pending:
        # Pre-filter on an UNCLAIMED read, so a drain never renames a file it
        # will not touch (another session's, another agent's).
        # v0.2.105: the routing fields only (read_pending_header), never the
        # multi-megabyte ctx of a file this drain may not touch.
        peek = read_pending_header(path)
        if not isinstance(peek, dict):
            # Unreadable — leave it (the collector records it, eventually).
            summary["skipped"] += 1
            continue
        file_session = str(peek.get("session_id") or "")
        peek_agent = str(peek.get("agent_id") or "")
        if file_session:
            if file_session != session_id:
                continue  # another chat's file: never scored here (R7-3)
            if agent_id and peek_agent != agent_id:
                continue
        else:
            if peek_agent and peek_agent != agent_id:
                continue
            if not _answer_source(peek):
                continue  # not this chat's retrieval (its query is not here)
        if (not agent_id and peek_agent and not peek.get("agent_transcript_path")
                and not session_end):
            # Review R5-3: the parent would only leave an unstamped agent
            # file (below). Claiming it first would hide it from the *.json
            # glob for that moment — and if the agent's ONE SubagentStop
            # drain lists the directory then, the file is never stamped.
            # Never claim it while the session is live.
            summary["left"] += 1
            continue
        # W6-C2 fix round (review R4c-1): CLAIM before compute. The agent's
        # SubagentStop drain and the parent's Stop drain (or two overlapping
        # async Stop drains) can list the same file; whoever renames it
        # first owns it, the other no longer sees it. Every leave path below
        # renames it back (finally); a landed compute finishes it.
        claimed = claim_pending(path)
        if claimed is None:
            continue  # another drain owns it right now (or just consumed it)
        try:
            payload = read_pending(claimed)
            if not isinstance(payload, dict):
                summary["skipped"] += 1
                continue
            if str(payload.get("session_id") or "") != file_session:
                continue  # re-staged under another session meanwhile

            # W6-C2 F12: pick the answer source by WHO staged the retrieval.
            file_agent = str(payload.get("agent_id") or "")
            if agent_id:
                if file_agent not in (agent_id, ""):
                    continue  # another agent's (or the main chat's) file
                answer_source = known
                final_window = True
                if answer_source and (
                        file_agent != agent_id
                        or payload.get("agent_transcript_path") != answer_source
                        or payload.get(AGENT_WINDOW_FINAL_KEY) is False):
                    # An unattributed (no session id) retrieval this agent's
                    # transcript holds becomes the agent's file, so a parent
                    # drain can find it again if it is left.
                    payload["agent_id"] = agent_id
                    _stamp_agent_transcript(claimed, payload, answer_source)
            elif file_agent:
                answer_source = str(payload.get("agent_transcript_path") or "")
                if not answer_source and session_end:
                    answer_source = _answer_source(payload)
                if not answer_source:
                    # The agent's transcript is unknown, and the parent's is
                    # the WRONG answer. Leave it.
                    summary["left"] += 1
                    continue
                # Review R5-9: a brief stamped while its agent may still be
                # running (agent_window_final false) accumulates like the
                # main chat's window until the session ends.
                final_window = session_end or payload.get(AGENT_WINDOW_FINAL_KEY) is not False
            else:
                answer_source = known
                final_window = session_end
            messages, kg_positions = _load(answer_source)

            # ONE scoring path (the finalize pass and the collector call it too).
            outcome, _reason = _score_payload(
                payload, messages, kg_positions,
                final_window=final_window,
                session_id=session_id, gate=gate,
                compute_fn=compute_fn, token_count_fn=token_count_fn,
            )
            if outcome == "computed":
                summary["computed"] += 1
                finish_scored(claimed, path)
                claimed = None  # consumed: nothing to hand back
            elif outcome == "skipped":
                summary["skipped"] += 1
            else:
                summary["left"] += 1
        finally:
            if claimed is not None:
                release_claim(claimed, path)

    # W6-RL2: finalize the leftovers of sessions whose end was recorded —
    # final floors, each against its OWN transcript (same folder). A file that
    # can never score is removed and recorded; one whose transcript cannot be
    # read, or whose compute failed, is left for a later drain.
    if not agent_id:
        own = session_id
        for sid in ended_sessions(project_root):
            if sid == own:
                continue  # this session's own end: finalized by the next drain
            for path in list_pending_for_session(sid, project_root):
                claimed = claim_pending(path)
                if claimed is None:
                    continue
                try:
                    payload = read_pending(claimed)
                    if not isinstance(payload, dict):
                        continue  # unreadable: the collector records it
                    source = _answer_source(payload)
                    if not source:
                        logger.info(
                            "rl_drain_citations: %s ended but its transcript is "
                            "not found; the citation stays unscored", path.name)
                        continue
                    if _finalize_claimed(path, claimed, payload, source, ""):
                        claimed = None
                finally:
                    if claimed is not None:
                        release_claim(claimed, path)
            # Review R9 F2: files staged without a session id (the MCP path)
            # whose exact query is in THIS ended session's own transcript
            # (same folder only) are its files too — final floors.
            sess_tp = session_transcript_for(known, sid)
            if sess_tp:
                sess_msgs, sess_kg = _load(sess_tp)
                for path in list_pending_for_session(NO_SESSION_PREFIX, project_root):
                    peek = read_pending_header(path)
                    if (not isinstance(peek, dict) or peek.get("session_id")
                            or peek.get("agent_id")
                            or not _holds_query(
                                sess_msgs, sess_kg, str(peek.get("query") or ""))):
                        continue  # not this ended chat's retrieval
                    claimed = claim_pending(path)
                    if claimed is None:
                        continue  # the monitor / a drain holds it: kept below
                    try:
                        payload = read_pending(claimed)
                        if (not isinstance(payload, dict) or payload.get("session_id")
                                or payload.get("agent_id")
                                or not _holds_query(
                                    sess_msgs, sess_kg, str(payload.get("query") or ""))):
                            continue  # re-staged as someone else's meanwhile
                        if _finalize_claimed(path, claimed, payload, sess_tp, sid):
                            claimed = None
                    finally:
                        if claimed is not None:
                            release_claim(claimed, path)
            if (not session_has_pending(sid, project_root)
                    and not _holds_nosession(sid, sess_tp)):
                clear_session_ended(sid, project_root)

    # W6-RL2: the only time bound left — garbage collection of PROVABLY
    # abandoned files (transcript idle >= RL_CITATION_ABANDONED_DAYS, no live
    # owner), each scored first when its transcript is readable.
    gc_counts: dict = {}
    summary["collected"] = collect_abandoned(
        project_root, live_session=session_id,
        source_fn=_collector_source, score_fn=_score_final, counts=gc_counts)
    summary["collected_scored"] = gc_counts.get("scored", 0)
    summary["collected_unscored"] = gc_counts.get("unscored", 0)
    return summary


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Drain deferred RL citations at turn-end")
    parser.add_argument("--session-id", default="", help="Stop-hook session_id")
    parser.add_argument("--transcript-path", default="", help="Stop-hook transcript_path")
    parser.add_argument(
        "--agent-id", default="",
        help="SubagentStop: drain only this subagent's files (with "
             "--transcript-path = its agent_transcript_path)")
    parser.add_argument(
        "--adopt-brief-tool-use-id", default="",
        help="PostToolUse:Agent (main chat): re-tag the citations staged for "
             "this Agent call's brief with --agent-id instead of draining "
             "(--transcript-path = the PARENT's transcript)")
    parser.add_argument(
        "--session-end", action="store_true",
        help="SessionEnd: the main chat has ended; record it and score its "
             "files with the final floors (W6-RL2)")
    parser.add_argument(
        "--agent-finished", action="store_true",
        help="with --adopt-brief-tool-use-id: the agent ran in the foreground "
             "and has finished, so its transcript is final")
    args = parser.parse_args(argv)

    if args.adopt_brief_tool_use_id:
        try:
            from claude_mcp_servers.rl_client.citation_pending import adopt_brief_citations

            n = adopt_brief_citations(
                args.session_id, args.adopt_brief_tool_use_id, args.agent_id,
                agent_finished=args.agent_finished,
                parent_transcript=args.transcript_path)
            print(f"rl_drain_citations: adopted={n}")
        except Exception as exc:  # noqa: BLE001 — telemetry must not break the hook
            print(f"rl_drain_citations: adopt soft-fail ({exc})", file=sys.stderr)
        return 0

    try:
        summary = drain_session(
            args.session_id,
            args.transcript_path or None,
            agent_id=args.agent_id,
            session_end=args.session_end,
        )
        # Stdout is discarded by the detached hook run except for the per-run
        # log it is redirected to; print for that log and manual runs.
        print("rl_drain_citations: " + " ".join(
            f"{k}={v}" for k, v in summary.items()))
    except Exception as exc:  # noqa: BLE001 — telemetry must not break Stop
        print(f"rl_drain_citations: soft-fail ({exc})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
