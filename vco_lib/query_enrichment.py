# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Query-budget SSOT + backward-enrichment for hook/MCP/CLI retrieval queries.

WP-E (v0.2.92). Implements the user's spec verbatim (DECISIONS-2026-09-03 R30):

    under the embedding model's token capacity -> walk BACKWARDS through the
    agent's prior output (thinking + chat, NEVER tool_use/tool_result),
    appending the last user prompt too, until the budget is full.

    over capacity -> chunk instead (the EXISTING machinery in
    ``claude_mcp_servers.rl_client.query_chunking``); do NOT enrich, there is
    nothing to fill.

This is the ONE shared component hooks, the MCP server, and CLI scripts all
call (R31: never mirror this logic into bash/PowerShell — a thin
``--transcript <path>`` argv flag is the cross-language boundary; the shell
side only locates the transcript file, this module does every byte of text
handling in-process).

PRIVACY (binding, same discipline as secrets): the composed enriched text is
never logged, never put in argv/``ps``, never persisted to a cache blob, and
never sent to telemetry. ``EnrichedQuery.digest`` is a SHA-1 of the ADDED
text only, meant for telemetry/log lines that need to observe "something was
added" without ever carrying the something — which is exactly what the one
observation row per decision (:func:`_emit_observation`, ``digest`` + counts,
no strings) carries into the existing JSONL metrics home.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from claude_mcp_servers.weaviate_mcp.chunking import (
    CHUNKING_PRESETS,
    TokenCounter,
    _BUDGET_SAFETY_MARGIN_RATIO,  # noqa: SLF001 -- ONE home, see comment below
    _num_ctx_for_model,  # noqa: SLF001 -- shared lookup rule, see comment below
    chunking_preset_for_models,
)
from vco_lib.paths import vct_metrics_dir
from vco_lib.transcript_context import last_turn_context

# chunking.py is the read-only SSOT for WP-E (owned by no one this cycle) —
# reusing its private ``_num_ctx_for_model`` here (rather than
# re-implementing MODEL_TOKEN_LIMITS partial-match lookup a second time) is
# the "one home" rule in practice: this becomes a THIRD reader of that one
# lookup (alongside ``chunking_preset_for_model`` and
# ``chunking_preset_for_models``), never a second mirrored implementation.

# ---------------------------------------------------------------------------
# Env knobs (R24: every knob has a reader AND a test proving it changes the
# output). Env values are read fresh on every call (never cached at import
# time) and, when present/parseable, take precedence over whatever the
# caller passed — they are operator-level overrides, not per-call defaults.
# ---------------------------------------------------------------------------
ENV_ENABLE = "VCO_QUERY_ENRICH"                 # "off" disables unconditionally
ENV_SHORT_THRESHOLD = "VCO_QUERY_ENRICH_SHORT_TOKENS"
ENV_SHARE = "VCO_QUERY_ENRICH_SHARE"

DEFAULT_SHORT_THRESHOLD_TOKENS = 24
DEFAULT_SHARE = 0.5

# ``share`` is a FRACTION of the chunker's target chunk size, so anything
# outside [0, 1] is meaningless. R24 says a documented knob needs a reader; an
# UNCLAMPED documented knob also needs a range, or the document only describes
# the values someone happened to try. Applied to the RESOLVED value, so the
# kwarg is clamped as well as the env var.
#   * SHARE_MAX is the behavioural end: >1 asks for more than a whole target
#     chunk of conversation, which abandons the share ceiling and leans the
#     entire decision on the num_ctx cap. Visible wherever target < budget
#     (codesage: target 1100, budget 1843).
#   * SHARE_MIN is defensive: ``max(0, ...)`` below already absorbs a negative
#     ceiling into "never enrich". Clamping states the range once instead of
#     leaving it implied by an arithmetic accident.
SHARE_MIN = 0.0
SHARE_MAX = 1.0

# Leaves headroom below the embedding model's num_ctx so the enriched query
# strictly undercuts it even given the tail-slice approximation below (which
# estimates chars-per-token rather than re-counting after every trim, to bound
# Ollama round trips per hook call). ONE home (D16, v0.2.92): imported from
# chunking.py above — the same margin the chunker clamps its preset budgets
# with, so the query path and the indexing path leave the SAME headroom under
# the SAME windows. A local literal here is exactly the drifted-duplicate
# shape the code_truncation defect came from; do not reintroduce one.

# Matches TokenCounter's own approximation fallback (chunking.py:
# ``len(text) // 4``) so a truncation decision made here and a token count
# made there stay consistent with each other rather than inventing a second,
# differently-calibrated heuristic.
# v0.2.92 (register #50): the number lives ONCE in
# `claude_mcp_servers.weaviate_mcp.chunking.CHARS_PER_TOKEN_TEXT`; read lazily
# at the use site (vco_lib does not import the MCP package at module level).
#
# D16 RESOLVED (v0.2.92): ``TokenCounter.count_tokens`` no longer consults
# any chat model — it is ONE deterministic conservative unit
# (``len(text) // CHARS_PER_TOKEN_TEXT``). The two paths then bound
# differently, each by its own ruling: the INDEXING path (chunking preset
# clamps) is bounded by ``min(CHUNK_TOKEN_POLICY_CEILING 8 192,
# int(num_ctx * (1 - margin)))`` — the R39 retrieval-quality policy, with
# the window only tightening it further — while THIS query path (R29)
# sizes on the embedding model's window alone, ``int(num_ctx * (1 -
# margin))``, with the ONE margin imported above. Measured on this repo's
# content (2026-09-04) the unit sits within −1.5 %…+7 % of
# ``qwen3-embedding:0.6b`` truth, so with the 10 % margin a budget-sized
# text strictly undershoots the real num_ctx — under-fill, never
# over-fill. The counts are still NOT exact embedding-model tokens (the
# embedding model's tokenizer is not callable in-process; counting via an
# embed call would pay the embedding cost per sentence); read them as the
# budget's unit, nothing more.


def _approx_chars_per_token() -> int:
    from claude_mcp_servers.weaviate_mcp.chunking import CHARS_PER_TOKEN_TEXT
    return CHARS_PER_TOKEN_TEXT


def _env_off(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() == "off"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class EnrichedQuery:
    """Result of :func:`build_query` — see PLAN §5.2 for the fixed contract."""

    text: str
    enriched: bool
    sources: Tuple[str, ...] = field(default_factory=tuple)
    trigger_tokens: int = 0
    added_tokens: int = 0
    budget_tokens: int = 0
    digest: str = ""


#: Stream name under :func:`vco_lib.paths.vct_metrics_dir` — the SAME JSONL
#: telemetry home the hooks already write ``costs.jsonl`` / ``failures.jsonl``
#: / ``kg_update_tokens.jsonl`` into. Not a new channel, one more file in the
#: existing one.
OBSERVATION_STREAM = "query_enrichment.jsonl"


def _emit_observation(result: "EnrichedQuery") -> "EnrichedQuery":
    """Append one JSONL row describing ``result``; return ``result`` unchanged.

    THE SILENCE DETECTOR. ``digest`` / ``sources`` / ``added_tokens`` were
    specified as observability fields and then had no consumer at all, which
    for THIS feature is the whole delivery risk: enrichment that never fires
    is byte-identical, from the outside, to enrichment that fires perfectly —
    the hook still injects, the query still returns results, nothing errors.
    One row per decision makes "did it ever fire, and on what" answerable with
    ``grep``, instead of by re-reading the code and hoping.

    PRIVACY: counts and the SHA-1 digest only. The composed text, the trigger,
    the transcript path and every transcript-derived string stay out — this
    row is exactly the "observe that something was added without carrying the
    something" use the module docstring reserves ``digest`` for.

    Lives here rather than at the three consumer call sites (``rl_kg_search.py``,
    ``query_code_graph.py``, ``hook_dual_search.py``) for the one-home reason:
    a per-consumer emit is three copies to keep in step, and the fourth
    consumer would ship unobserved.

    Best-effort: any failure to write is swallowed. A telemetry line must
    never be able to break a PreToolUse hook.
    """
    try:
        path = vct_metrics_dir() / OBSERVATION_STREAM
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "enriched": result.enriched,
            "sources": list(result.sources),
            "trigger_tokens": result.trigger_tokens,
            "added_tokens": result.added_tokens,
            "budget_tokens": result.budget_tokens,
            "digest": result.digest,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 -- best-effort telemetry, never fatal
        pass
    return result


def _tail_slice_by_tokens(text: str, max_tokens: int) -> str:
    """Keep the END of ``text`` (recency ≈ relevance) within ``max_tokens``.

    Approximate: no second ``TokenCounter`` round trip after slicing — one
    count per candidate field is the latency budget this module accepts.
    """
    if max_tokens <= 0 or not text:
        return ""
    approx_chars = int(max_tokens * _approx_chars_per_token())
    if approx_chars >= len(text):
        return text
    return text[-approx_chars:]


def _resolve_budget(embedding_models: Optional[Sequence[str]]) -> Tuple[int, int, bool]:
    """Return ``(budget_tokens, target_tokens, conservative)``.

    ``budget_tokens`` is the EMBEDDING model's own input window (its
    ``num_ctx``, i.e. the MODEL_TOKEN_LIMITS value) minus the safety margin —
    standing ruling R29: *the enrichment budget is the EMBEDDING model's input
    window, not the chat model's context*. Sizing it on the CHUNKING PRESET's
    ``max_tokens`` instead (as WP-E first shipped) overshoots, because a
    preset tier is deliberately generous relative to the window it routes for:
    qwen3-embedding has ``num_ctx`` 10 240 but lands in ``xlarge_context``,
    whose raw max is 13 500 — a 12 150-token "budget", 19% ABOVE the window the
    text is actually embedded into. (Since the v0.2.92 D16+R39 clamp the
    RESOLVED xlarge tuple for qwen3 is 4 600/8 192/8 192 — the resolved
    preset max and the R39 ceiling coincide for qwen3, but that is the
    policy ceiling, not this function's ruling; sizing on a preset remains
    the wrong derivation.) Nothing overflowed only because the
    ``share`` cap happened to bind first; that is a coincidence of the default
    value, not a bound. Presets still supply ``target_tokens``, which is what a
    preset is for.

    Multi-model resolution takes the MIN ``num_ctx`` across the list, matching
    ``chunking_preset_for_models``' tightest-slot rule — the budget must fit
    EVERY configured slot, not the widest.

    ``conservative`` is True when any model name is unknown (or the list is
    empty) — the WP-E spec's deliberately-tighter fallback: unknown model
    capacity is a genuine unknown, not a "guess large" (that is
    ``chunking_preset_for_model``'s own default, appropriate for chunking
    decisions but explicitly NOT for enrichment, per PLAN §3 WP-E item 2).
    There is no num_ctx to size on in that branch, so it keeps the
    ``small_context`` max as a stand-in — 1 600, comfortably under that tier's
    ~2 k window — and now applies the SAME safety margin as the known branch,
    which the original skipped, leaving the fallback the only path with no
    headroom at all.
    """
    names = list(embedding_models or [])
    ctxs = [_num_ctx_for_model(n) for n in names]
    unknown = (not names) or any(c is None for c in ctxs)
    if unknown:
        small_min, small_max, small_target = CHUNKING_PRESETS["small_context"]
        del small_min
        return int(small_max * (1 - _BUDGET_SAFETY_MARGIN_RATIO)), small_target, True
    _min, _preset_max, target = chunking_preset_for_models(names)
    del _min, _preset_max
    min_num_ctx = min(c for c in ctxs if c is not None)
    budget = int(min_num_ctx * (1 - _BUDGET_SAFETY_MARGIN_RATIO))
    return budget, target, False


def build_query(
    trigger: str,
    *,
    transcript_path: Optional[str] = None,
    embedding_models: Optional[Sequence[str]] = None,
    short_threshold_tokens: int = DEFAULT_SHORT_THRESHOLD_TOKENS,
    share: float = DEFAULT_SHARE,
    enabled: bool = True,
) -> EnrichedQuery:
    """Enrich ``trigger`` with recent conversation text when it is SHORT.

    See module docstring / PLAN §5.2 for the full contract. Never raises:
    any transcript-read failure degrades to "no enrichment available", the
    same outcome as a genuinely absent transcript.
    """
    trigger = trigger or ""
    trigger_tokens = TokenCounter.count_tokens(trigger) if trigger else 0
    budget_tokens, target_tokens, conservative = _resolve_budget(embedding_models)

    effective_enabled = enabled and not _env_off(ENV_ENABLE)
    effective_threshold = _env_int(ENV_SHORT_THRESHOLD, short_threshold_tokens)
    effective_share = min(max(_env_float(ENV_SHARE, share), SHARE_MIN), SHARE_MAX)

    def _unenriched() -> EnrichedQuery:
        # Every decline path funnels through here, so the observation row is
        # emitted once per decision without a per-branch copy.
        return _emit_observation(EnrichedQuery(
            text=trigger,
            enriched=False,
            sources=(),
            trigger_tokens=trigger_tokens,
            added_tokens=0,
            budget_tokens=budget_tokens,
            digest="",
        ))

    if not effective_enabled:
        return _unenriched()

    # Conservative (unknown-model) rule: skip enrichment unless the trigger
    # is genuinely empty (in which case there is no query at all otherwise,
    # so the conservative budget is still used to produce *something*).
    if conservative and trigger_tokens > 0:
        return _unenriched()

    if not conservative and trigger_tokens >= effective_threshold:
        return _unenriched()

    max_added_tokens = max(0, min(budget_tokens - trigger_tokens, effective_share * target_tokens))
    max_added_tokens = int(max_added_tokens)
    if max_added_tokens <= 0:
        return _unenriched()

    turn = last_turn_context(transcript_path)

    parts: list[str] = []
    sources: list[str] = []
    remaining = max_added_tokens

    # Order fixed by PLAN §3 WP-E item 2: last user prompt, then assistant
    # chat text, then assistant thinking — trigger itself is prepended last
    # (it is never truncated; it is always the caller's original query).
    candidates = (
        ("user_prompt", turn.user_prompt),
        ("assistant_text", turn.assistant_text),
        ("assistant_thinking", turn.assistant_thinking),
    )
    for tag, field_text in candidates:
        if remaining <= 0:
            break
        if not field_text:
            continue
        field_tokens = TokenCounter.count_tokens(field_text)
        if field_tokens <= remaining:
            chosen = field_text
            used = field_tokens
        else:
            chosen = _tail_slice_by_tokens(field_text, remaining)
            used = remaining
        if not chosen:
            continue
        parts.append(chosen)
        sources.append(tag)
        remaining -= used

    if not parts:
        return _unenriched()

    added_text = "\n\n".join(parts)
    added_tokens = max_added_tokens - remaining
    digest = hashlib.sha1(added_text.encode("utf-8", errors="replace")).hexdigest()
    final_text = f"{trigger}\n\n{added_text}" if trigger else added_text

    return _emit_observation(EnrichedQuery(
        text=final_text,
        enriched=True,
        sources=tuple(sources),
        trigger_tokens=trigger_tokens,
        added_tokens=added_tokens,
        budget_tokens=budget_tokens,
        digest=digest,
    ))
