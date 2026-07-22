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
  6. TTL-sweeps abandoned pending files (60-min default).

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


def _gate_tokens() -> int:
    """Resolve the citation token gate, honouring the same env override the
    MCP monitor uses so a tuned floor applies to both paths."""
    try:
        return int(os.getenv("RL_MIN_ANSWER_TOKENS_FOR_CITATION", "25000"))
    except (TypeError, ValueError):
        return 25000


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
    try:
        return float(os.getenv("RL_TERMINAL_CITATION_AGE_SECONDS", "1800"))
    except (TypeError, ValueError):
        return 1800.0


def _terminal_floor_tokens() -> int:
    try:
        return int(os.getenv("RL_TERMINAL_CITATION_MIN_TOKENS", "2000"))
    except (TypeError, ValueError):
        return 2000


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
    try:
        return int(os.getenv("RL_SOFT_CITATION_MIN_TOKENS", "200"))
    except (TypeError, ValueError):
        return 200


def drain_session(
    session_id: str,
    transcript_path: "str | None",
    *,
    project_root: "str | None" = None,
    compute_fn=None,
    token_count_fn=None,
) -> dict:
    """Drain all pending citations for ``session_id``.

    Args:
        session_id: From Stop stdin (the per-conversation key).
        transcript_path: From Stop stdin; the JSONL to read the answer from.
            When None we cannot compute (no answer source) — only TTL-sweep.
        project_root: Override for tests; defaults to CLAUDE_PROJECT_DIR / cwd.
        compute_fn: Injectable ``(task_id, answer, ctx) -> result|None`` for
            tests; defaults to the shared ``citation_compute.compute_citation``.
        token_count_fn: Injectable ``(text) -> int`` for tests; defaults to the
            char/4 estimate (the drain avoids importing the heavy chunker).

    Returns a summary dict: ``{computed, left, swept, skipped}``.
    """
    from claude_mcp_servers.rl_client.answer_window import (
        load_messages_cached,
        find_kg_positions,
        extract_answer_window,
        match_position_for_query,
        match_position_by_timestamp,
        token_estimate,
    )
    from claude_mcp_servers.rl_client.citation_pending import (
        list_pending_for_session,
        read_pending,
        delete_pending,
        sweep_expired,
    )

    if compute_fn is None:
        from claude_mcp_servers.rl_client.citation_compute import compute_citation as compute_fn
    if token_count_fn is None:
        token_count_fn = token_estimate

    gate = _gate_tokens()
    summary = {"computed": 0, "left": 0, "swept": 0, "skipped": 0}

    # TTL sweep first — drop abandoned files regardless of transcript state.
    summary["swept"] = sweep_expired(project_root)

    pending = list_pending_for_session(session_id, project_root)
    if not pending:
        return summary

    # v0.2.73 (Concern-B): shared cached loader (one home with the MCP monitor);
    # the drain reads once per Stop so the (mtime,size) cache is mostly a miss
    # here, but routing through the SAME function keeps the parsed-message shape
    # — and thus the extracted answer window — byte-identical across both paths.
    messages = load_messages_cached(transcript_path) if transcript_path else []
    kg_positions = find_kg_positions(messages) if messages else []

    for path in pending:
        payload = read_pending(path)
        if not isinstance(payload, dict):
            # Unreadable — leave for the TTL sweep rather than deleting blind.
            summary["skipped"] += 1
            continue
        # Only process files for THIS session (list with empty session_id
        # returns all; each must self-identify).
        if session_id and payload.get("session_id") not in (session_id, ""):
            continue

        ctx = payload.get("ctx")
        task_id = payload.get("task_id") or ""
        if not isinstance(ctx, dict) or not task_id:
            summary["skipped"] += 1
            continue

        if not messages:
            # No transcript to read the answer from — leave for next Stop/TTL.
            summary["left"] += 1
            continue

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
                summary["left"] += 1
                continue

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
            _aged = (
                answer.strip()
                and _age_s is not None
                and _age_s >= _terminal_age_seconds()
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
                summary["left"] += 1
                continue

        # At/above a gate → compute + write + delete (one-shot).
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
        if result is not None:
            summary["computed"] += 1
            delete_pending(path)
        else:
            # Compute soft-failed (no embedding service / no signal). Leave the
            # file for retry; the TTL sweep eventually reclaims it.
            summary["left"] += 1

    return summary


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description="Drain deferred RL citations at turn-end")
    parser.add_argument("--session-id", default="", help="Stop-hook session_id")
    parser.add_argument("--transcript-path", default="", help="Stop-hook transcript_path")
    args = parser.parse_args(argv)

    try:
        summary = drain_session(
            args.session_id,
            args.transcript_path or None,
        )
        # Stdout is discarded by the async Stop hook; print for manual runs.
        print(
            f"rl_drain_citations: computed={summary['computed']} "
            f"left={summary['left']} swept={summary['swept']} "
            f"skipped={summary['skipped']}"
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must not break Stop
        print(f"rl_drain_citations: soft-fail ({exc})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
