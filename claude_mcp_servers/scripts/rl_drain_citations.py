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
     so that cohort's citation labels are recovered rather than lost at TTL
     (v0.2.77 9-bis).
  3. Extracts the cumulative answer window from that position to end-of-
     transcript (accumulates across human turns — the V52-N behaviour).
  4. ⚠️ ACCUMULATE-DON'T-DROP: if the window is still BELOW the token gate,
     LEAVES the pending file for the next Stop (does NOT compute, does NOT
     delete) so it keeps accumulating into subsequent turns.
  5. At/above the gate: computes the citation via the shared
     ``citation_compute.compute_citation`` (one home with the MCP monitor),
     writes the event, then deletes the pending file (one-shot).
  6. TTL-sweeps abandoned pending files (60-min default): each expiring
     file is claimed, scored through the SAME scoring function when its
     transcript is readable (soft/terminal floors), and only then deleted;
     one that cannot be scored is logged as an unscored expiry (R6-2).

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
import os
import sys
import time

# Allow ``from claude_mcp_servers...`` imports when run as a bare script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO_ROOT, os.path.join(_HERE, "..")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib.env_values import env_float, env_int  # noqa: E402 — after the path bootstrap above


def _gate_tokens() -> int:
    """Resolve the citation token gate, honouring the same env override the
    MCP monitor uses so a tuned floor applies to both paths."""
    return env_int("RL_MIN_ANSWER_TOKENS_FOR_CITATION", 25000)


# v0.2.73 RL-4 — terminal-session citation floor.
#
# Sessions whose cumulative answer window never reaches the 25k gate were
# NEVER labeled: the drain left the pending file every Stop and the TTL sweep
# (60 min) eventually deleted it → the training corpus was censored toward
# long sessions. The terminal floor gives an aging pending file a second
# chance: once it is older than RL_TERMINAL_CITATION_AGE_SECONDS (default
# 1800 s — half the TTL, so the terminal compute always precedes the sweep)
# AND its window clears the LOWER RL_TERMINAL_CITATION_MIN_TOKENS floor
# (default 2000 — enough chunks for max-over-chunks cosine to discriminate),
# the drain computes + writes + deletes instead of leaving it to die.
#
# This deliberately preserves the accumulate-don't-drop ruling for YOUNG
# files: a session that WOULD reach 25k later still gets the full window;
# only files at real risk of TTL death take the lower floor.

def _terminal_age_seconds() -> float:
    return env_float("RL_TERMINAL_CITATION_AGE_SECONDS", 1800.0)


def _terminal_floor_tokens() -> int:
    return env_int("RL_TERMINAL_CITATION_MIN_TOKENS", 2000)


# G5 (2026-07-22) — soft-label floor for dying-but-tiny answer windows.
#
# An aging pending file whose answer NEVER clears the 2 000-token terminal floor
# used to be left every drain until the TTL sweep deleted it UNLABELED. That
# censored the corpus toward long answers and starved the mcp_interactive cohort
# (5 citations ever). Instead of dropping such a retrieval entirely, once the file
# is at terminal age we compute a SOFT-labeled citation from whatever answer
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
    for the TTL sweep."""
    try:
        from vco_lib.atomic import atomic_write_json
        from claude_mcp_servers.rl_client.citation_pending import AGENT_WINDOW_FINAL_KEY

        payload["agent_transcript_path"] = transcript_path
        # The agent's own drain stamps it: its window is final now (R5-9).
        payload.pop(AGENT_WINDOW_FINAL_KEY, None)
        atomic_write_json(path, payload, indent=None, fsync=False)
    except Exception:  # noqa: BLE001 — telemetry must not break the drain
        pass


def _score_payload(
    payload: dict,
    messages: list,
    kg_positions: list,
    *,
    final_window: bool,
    expiring: bool,
    session_id: str,
    gate: int,
    compute_fn,
    token_count_fn,
) -> "tuple[str, str]":
    """Score ONE claimed pending payload against an already-loaded answer
    transcript — the one scoring path, shared by the Stop drain and the TTL
    sweep (v0.2.102 W6-R6FIX, review R6-2).

    ``final_window``: nothing will accumulate into the window (a stopped
    subagent's transcript), so a below-gate window takes the terminal/soft
    floors now. ``expiring``: the file is at its TTL and is deleted after
    this call whatever happens, so it is treated as aged (the same floors)
    even when ``RL_TERMINAL_CITATION_AGE_SECONDS`` was tuned above the TTL.

    Returns ``(outcome, reason)``: ``("computed", fire_reason)`` when a
    citation was written; ``("skipped", reason)`` for a payload that can
    never be scored; ``("left", reason)`` otherwise (the drain hands such a
    file back, the sweep records it as an unscored expiry). Never raises
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
        # No transcript to read the answer from — leave for next Stop/TTL.
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
        # ~87% of retrievals (the hook cohort) died unlabeled at the TTL sweep
        # — defeating F-QUEUE's stated purpose of recovering exactly that
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
        # answers rarely reach the 25k gate) was left to die at the TTL sweep:
        # mcp_interactive had FIVE citation events ever vs. 1 028 hook, the
        # single worst under-labeled cohort. The mcp monitor deletes its own
        # pending file on fire, so a surviving mcp pending file here means the
        # monitor did NOT fire — exactly the case the drain must recover.
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
        # v0.2.73 RL-4: terminal-session floor. An AGING pending file
        # (older than the terminal age, i.e. at real risk of dying at the
        # TTL sweep un-labeled) whose window clears the LOWER terminal
        # floor is computed NOW instead of left. Young files keep the
        # accumulate-don't-drop behaviour unchanged.
        _age_s = None
        _ts_ms = payload.get("ts_ms")
        if isinstance(_ts_ms, (int, float)) and _ts_ms > 0:
            _age_s = (time.time() * 1000.0 - float(_ts_ms)) / 1000.0
        # W6-C2 F12: a stopped subagent's window is FINAL — nothing will
        # accumulate into it, so it takes the terminal floors now. R6-2: so
        # does a file at its TTL — this is its last chance to be scored.
        _aged = bool(answer.strip()) and (
            final_window
            or expiring
            or (_age_s is not None and _age_s >= _terminal_age_seconds())
        )
        _terminal = _aged and tok >= _terminal_floor_tokens()
        if _terminal:
            fire_reason = "terminal_floor"
        elif _aged and tok >= _soft_label_floor_tokens():
            # G5: the window will never clear the terminal floor but is aging
            # toward a TTL death. Emit a SOFT-labeled citation from the
            # below-threshold window (its cosine maxima ARE a signal, just a
            # weaker one) rather than dropping the retrieval unlabeled. Marked
            # so the trainer can down-weight it.
            fire_reason = "soft_terminal"
            soft_label = True
        else:
            # ⚠️ ACCUMULATE-DON'T-DROP: below every floor → keep the file so
            # it can keep accumulating into subsequent turns. Never compute,
            # never delete here. Compute+delete happens only at/above a gate
            # (25k, terminal floor, or the soft-label floor at terminal age)
            # or TTL.
            return "left", "below_floor"

    # At/above a gate → compute + write (the caller deletes: one-shot).
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
        # leaves the file for retry; at the TTL it is an unscored expiry.
        return "left", "compute_failed"
    return "computed", fire_reason


def drain_session(
    session_id: str,
    transcript_path: "str | None",
    *,
    agent_id: str = "",
    project_root: "str | None" = None,
    compute_fn=None,
    token_count_fn=None,
) -> dict:
    """Drain all pending citations for ``session_id``.

    Args:
        session_id: From Stop stdin (the per-conversation key).
        transcript_path: From Stop stdin; the JSONL to read the answer from.
            When None we cannot compute (no answer source) — only TTL-sweep.
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
              left for the TTL sweep — never against the parent's.
        project_root: Override for tests; defaults to CLAUDE_PROJECT_DIR / cwd.
        compute_fn: Injectable ``(task_id, answer, ctx) -> result|None`` for
            tests; defaults to the shared ``citation_compute.compute_citation``.
        token_count_fn: Injectable ``(text) -> int`` for tests; defaults to the
            char/4 estimate (the drain avoids importing the heavy chunker).

    Returns a summary dict: ``{computed, left, swept, skipped, readopted,
    swept_scored, swept_unscored}`` (``readopted`` = stale claims handed
    back, see ``citation_pending``; ``swept`` = expired files removed, of
    which ``swept_scored`` were scored first and ``swept_unscored`` were
    recorded as unscored expiries — review R6-2).
    """
    from claude_mcp_servers.rl_client.answer_window import (
        load_messages_cached,
        find_kg_positions,
        token_estimate,
    )
    from claude_mcp_servers.rl_client.citation_pending import (
        list_pending_for_session,
        read_pending,
        delete_pending,
        sweep_expired,
        claim_pending,
        release_claim,
        readopt_stale_claims,
        agent_transcript_for,
        session_transcript_for,
        AGENT_WINDOW_FINAL_KEY,
    )

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
    summary = {"computed": 0, "left": 0, "swept": 0, "skipped": 0, "readopted": 0,
               "swept_scored": 0, "swept_unscored": 0}

    # v0.2.73 (Concern-B) / W6-MEM: shared cached loader (one home with the
    # MCP monitor); the drain is a one-shot process so its cache starts empty,
    # but routing through the SAME function keeps the (slim) parsed-message
    # shape — and thus the extracted answer window — byte-identical across
    # both paths. W6-C2 F12: one parse per DISTINCT transcript (the parent's,
    # and each stamped subagent transcript a parent drain meets).
    _loaded: dict = {}

    def _load(tp: str):
        if tp not in _loaded:
            msgs = load_messages_cached(tp) if tp else []
            _loaded[tp] = (msgs, find_kg_positions(msgs) if msgs else [])
        return _loaded[tp]

    def _score_expiring(_claimed, payload: dict) -> "str | None":
        """TTL-sweep scorer (review R6-2): score an expiring file through
        :func:`_score_payload` before the sweep deletes it. The answer source
        is picked by WHO staged the retrieval, exactly as below: an agent
        file reads the agent's stamped transcript (or, unstamped, the one
        beside its session's transcript); a main-chat file reads its
        session's transcript (this drain's own, or the sibling
        ``<session_id>.jsonl`` of another session). ``None`` = a citation
        was written; otherwise the reason it could not be."""
        file_session = str(payload.get("session_id") or "")
        # A file staged without a session id belongs to whichever session
        # drains it — the same rule the main loop applies below.
        sess_tp = session_transcript_for(transcript_path or "", file_session)
        file_agent = str(payload.get("agent_id") or "")
        if file_agent:
            source = str(payload.get("agent_transcript_path") or "")
            if not source and sess_tp:
                source = agent_transcript_for(sess_tp, file_agent)
        else:
            source = sess_tp
        if not source:
            return "no_transcript"
        messages, kg_positions = _load(source)
        outcome, reason = _score_payload(
            payload, messages, kg_positions,
            final_window=bool(file_agent), expiring=True,
            session_id=file_session or session_id, gate=gate,
            compute_fn=compute_fn, token_count_fn=token_count_fn,
        )
        return None if outcome == "computed" else reason

    # W6-C2 fix round (review R4c-1): a claim whose drain died mid-compute is
    # handed back first, so the crash costs a retry, never the citation.
    summary["readopted"] = readopt_stale_claims(project_root)
    # TTL sweep first. Review R6-2: each expiring file is CLAIMED (so no
    # other drain or the MCP monitor can score it too), scored through the
    # one scoring path when its transcript is readable, and only then
    # removed; one with no score is recorded as an unscored expiry.
    sweep_counts: dict = {}
    summary["swept"] = sweep_expired(project_root, score_fn=_score_expiring,
                                     counts=sweep_counts)
    summary["swept_scored"] = sweep_counts.get("scored", 0)
    summary["swept_unscored"] = sweep_counts.get("unscored", 0)

    pending = list_pending_for_session(session_id, project_root)
    if not pending:
        return summary

    for path in pending:
        # Pre-filter on an UNCLAIMED read, so a drain never renames a file it
        # will not touch (another session's, another agent's).
        peek = read_pending(path)
        if not isinstance(peek, dict):
            # Unreadable — leave for the TTL sweep rather than deleting blind.
            summary["skipped"] += 1
            continue
        if session_id and peek.get("session_id") not in (session_id, ""):
            continue
        if agent_id and str(peek.get("agent_id") or "") != agent_id:
            continue
        if not agent_id and peek.get("agent_id") and not peek.get("agent_transcript_path"):
            # Review R5-3: the parent would only leave an unstamped agent
            # file (below). Claiming it first would hide it from the *.json
            # glob for that moment — and if the agent's ONE SubagentStop
            # drain lists the directory then, the file is never stamped and
            # dies unscored at the TTL sweep. Never claim it.
            summary["left"] += 1
            continue
        # W6-C2 fix round (review R4c-1): CLAIM before compute. The agent's
        # SubagentStop drain and the parent's Stop drain (or two overlapping
        # async Stop drains) can list the same file; whoever renames it
        # first owns it, the other no longer sees it. Without the claim both
        # computed it and both wrote a citation. Every leave path below
        # renames it back (finally); a successful compute deletes it.
        claimed = claim_pending(path)
        if claimed is None:
            continue  # another drain owns it right now (or just consumed it)
        try:
            payload = read_pending(claimed)
            if not isinstance(payload, dict):
                summary["skipped"] += 1
                continue
            # Only process files for THIS session (list with empty session_id
            # returns all; each must self-identify).
            if session_id and payload.get("session_id") not in (session_id, ""):
                continue

            # W6-C2 F12: pick the answer source by WHO staged the retrieval.
            file_agent = str(payload.get("agent_id") or "")
            final_window = False
            if agent_id:
                if file_agent != agent_id:
                    continue  # another agent's (or the main chat's) file
                answer_source = transcript_path or ""
                final_window = True
                if answer_source and (
                        payload.get("agent_transcript_path") != answer_source
                        or payload.get(AGENT_WINDOW_FINAL_KEY) is False):
                    _stamp_agent_transcript(claimed, payload, answer_source)
            elif file_agent:
                answer_source = str(payload.get("agent_transcript_path") or "")
                if not answer_source:
                    # The agent's own drain never ran: its transcript is unknown,
                    # and the parent's is the WRONG answer. Leave it (TTL sweep).
                    summary["left"] += 1
                    continue
                # Review R5-9: a brief stamped while its agent may still be
                # running (agent_window_final false) accumulates like the
                # main chat's window; a finished agent's window is final.
                final_window = payload.get(AGENT_WINDOW_FINAL_KEY) is not False
            else:
                answer_source = transcript_path or ""
            messages, kg_positions = _load(answer_source)

            # ONE scoring path (the TTL sweep below calls the same function).
            outcome, _reason = _score_payload(
                payload, messages, kg_positions,
                final_window=final_window, expiring=False,
                session_id=session_id, gate=gate,
                compute_fn=compute_fn, token_count_fn=token_count_fn,
            )
            if outcome == "computed":
                summary["computed"] += 1
                delete_pending(claimed)
                claimed = None  # consumed: nothing to hand back
            elif outcome == "skipped":
                summary["skipped"] += 1
            else:
                summary["left"] += 1
        finally:
            if claimed is not None:
                release_claim(claimed, path)

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
        )
        # Stdout is discarded by the async Stop hook; print for manual runs.
        print(
            f"rl_drain_citations: computed={summary['computed']} "
            f"left={summary['left']} swept={summary['swept']} "
            f"skipped={summary['skipped']} readopted={summary['readopted']} "
            f"swept_scored={summary['swept_scored']} "
            f"swept_unscored={summary['swept_unscored']}"
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break Stop
        print(f"rl_drain_citations: soft-fail ({exc})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
