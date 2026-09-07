# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Text Chunking Utilities for Claude MCP Weaviate Server

Unified chunking implementation with:
- ONE deterministic, conservative token-counting unit (D16, v0.2.92):
  ``len(text) // CHARS_PER_TOKEN_TEXT`` — see TokenCounter's docstring for
  why no "real" tokenizer is used and why under-filling is the contract
- Chunk budgets bounded by the R39 retrieval-quality policy ceiling
  (``CHUNK_TOKEN_POLICY_CEILING``), which the EMBEDDING model's own input
  window (its ``num_ctx`` in MODEL_TOKEN_LIMITS, D16) may only clamp
  FURTHER DOWNWARD — never a chat model's context, never the capacity
  alone
- Flexible metadata support
- Document and generic text chunking
"""

import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone


UTC = timezone.utc


@dataclass
class Chunk:
    """A chunk of text with metadata"""
    content: str
    chunk_number: int
    total_chunks: int
    token_count: int
    source_id: str
    metadata: Dict[str, Any]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Weaviate storage"""
        import json
        return {
            "content": self.content,
            "chunk_number": self.chunk_number,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            "source_id": self.source_id,
            "metadata_json": json.dumps(self.metadata),
            "created_at": self.created_at
        }


@dataclass
class DocumentChunk:
    """A chunk of a document with specific document metadata (backward compatibility)"""
    content: str
    chunk_number: int
    total_chunks: int
    token_count: int
    source_document_id: str
    source_document_title: str
    is_first: bool
    is_last: bool
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Weaviate storage"""
        return {
            "content": self.content,
            "chunk_number": self.chunk_number,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            "source_document_id": self.source_document_id,
            "source_document_title": self.source_document_title,
            "is_first": self.is_first,
            "is_last": self.is_last,
            "created_at": self.created_at
        }


class TokenCounter:
    """
    Token counter in the chunker's ONE budget unit (D16, v0.2.92).

    Counts are ``len(text) // CHARS_PER_TOKEN_TEXT`` — deterministic
    character arithmetic. No tokenizer, no network, no hidden class state.

    WHY not a "real" tokenizer: these counts are compared against budgets
    that are capacities of the EMBEDDING model's window (the
    ``MODEL_TOKEN_LIMITS`` num_ctx, per standing ruling R29), and the
    embedding model's own tokenizer is not callable in-process — Ollama
    exposes no token-count endpoint, and counting via an embed call would
    pay the full embedding cost per sentence. The only alternatives are a
    WRONG tokenizer or this approximation:

      * Pre-v0.2.92 this class constructed a langchain chat client on a
        CHAT model id read from the environment (default
        ``qwen3.5:0.8b``) and called ``get_num_tokens``. That never used
        the chat model's tokenizer either: langchain_core silently
        substitutes its GPT-2 fallback (which warns "Token counts may be
        inaccurate") — a third
        unit that measured 16-87% ABOVE ``qwen3-embedding:0.6b`` truth on
        this repo's content, and made chunk boundaries depend on whether
        an optional package (``langchain_ollama``, absent from every
        venv/requirements this repo ships) happened to be installed.
      * The chat-vs-embedding tokenizer divergence D16 named is real but
        small for the shipped pair (both Qwen-family: 1.2-5.4%); the
        dominant measured defect was the BUDGET side — the xlarge tier max
        (13 500) sat 32% above qwen3-embedding's 10 240 num_ctx, so a
        max-packed chunk (13 697 true tokens) was silently truncated to
        the first 10 239 at embed time (Ollama pins prompt_eval_count at
        the window and returns HTTP 200 — content loss with no error).

    UNDER-FILL, NEVER OVER-FILL (the contract): against the measured
    chars/token ratios of the shipped embedding models (2026-09-04:
    qwen3-embedding 3.94-4.28, jina-v2-base-code 3.79-3.98 on real repo
    content) this unit is within ±2% on long text, and
    ``_BUDGET_SAFETY_MARGIN_RATIO`` (see below) absorbs that error with
    room — a chunk sized by this counter strictly undershoots the
    embedding model's real window. Never read these counts as exact
    embedding-model tokens; they are the budget's unit, nothing more.
    """

    @staticmethod
    def count_tokens(text: str) -> int:
        """
        Count tokens in text, in the chunker's budget unit

        Args:
            text: Text to count tokens for

        Returns:
            ``len(text) // CHARS_PER_TOKEN_TEXT`` (0 for empty input)
        """
        if not text:
            return 0
        return len(text) // CHARS_PER_TOKEN_TEXT

    @staticmethod
    def count_dict_tokens(data: Dict[str, Any]) -> int:
        """
        Count tokens in a dictionary (recursively)

        Args:
            data: Dictionary to count tokens for

        Returns:
            Total number of tokens
        """
        total = 0
        for key, value in data.items():
            # Add tokens for key
            total += TokenCounter.count_tokens(str(key))

            # Add tokens for value
            if isinstance(value, dict):
                total += TokenCounter.count_dict_tokens(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        total += TokenCounter.count_dict_tokens(item)
                    else:
                        total += TokenCounter.count_tokens(str(item))
            else:
                total += TokenCounter.count_tokens(str(value))

        return total


# v0.2.47 RL-7.5 / chunker-preset-overhaul (2026-06-04): re-cast MODEL_TOKEN_LIMITS
# as **`num_ctx` we send to Ollama**,
# NOT the model's architectural max input. This is the chunker routing key —
# we want chunks to fit comfortably inside the context window we actually
# request from the backend, not the theoretical ceiling.
#
# Conservative per-model `num_ctx`:
#   * snowflake-arctic-embed2 stays at 4k (low-VRAM contributors). Model
#     architecturally supports 8k via RoPE but our Ollama config caps at 4k.
#   * qwen3-embedding:0.6b set to 10k. Model architecturally supports 32k
#     (verified via `ollama show qwen3-embedding:0.6b`) but at 0.6B params
#     the quality drop-off past ~10k is steep — keep room for headroom.
#   * jina-embeddings-v2-base-code: 2k. v2 was TRAINED at 512 even though
#     architecture supports 8k via ALiBi; jumping to 8k gives degraded
#     embedding quality for the modest gain in chunk size. 2k is a safe
#     middle ground for low-power code-search.
#   * codesage-large-v2: 1 024 (wiring-audit W1, 2026-09-05 — MEASURED, not
#     the architectural cap). The served window is what the model snapshot's
#     sentence_bert_config.json declares (max_seq_length=1024), NOT
#     config.json's max_position_embeddings=2048: sentence-transformers
#     serves CodeSage at 1 024 and silently truncates the tail at HTTP 200.
#     Verified live on the code-embed container: appending 500 tokens past
#     position 1 024 leaves the vector IDENTICAL (cos = 1.0000 at every
#     probe ≥ 1 024; below it the tail moves the vector). The previous
#     2 048 entry budgeted 2.1× the served window, so every maximal entity
#     — and every part of a split one — had roughly half its text never
#     influence its vector, with no warning and no tag.
#   * text-embedding-3-small: 8k (OpenAI documented 8191 cap).
#   * bge-m3:latest, embeddinggemma:300m-bf16, granite-embedding:278m-fp16:
#     NEW entries — verified via `ollama show` 2026-06-04.
#
# To raise any of these: bump the value here AND ensure the embedding adapter
# sends the new `num_ctx` to Ollama (see vco_lib/embedding_providers/ollama.py).
# Chars-per-token heuristics — ONE home (v0.2.92 duplication-merge, wave-3
# register #50). Three modules used to declare their own: `code_truncation.py`
# (3.5, "conservative for code"), `vco_lib/embedding_service.py` (4, English
# text) and `vco_lib/query_enrichment.py` (4, so an enrichment budget and a
# token count "stay consistent with each other" — which they only do if the
# number lives once). Two values on purpose: code tokenises denser than prose,
# and an OVER-estimate of chars-per-token re-introduces the silent Ollama
# truncation these budgets exist to prevent, so each is the conservative
# (smaller) side of its domain's real ratio.
CHARS_PER_TOKEN_TEXT: int = 4       # English prose; real ratio ~3.5-4.5 (int: callers size str buffers with it)
CHARS_PER_TOKEN_CODE: float = 3.5   # source code; denser than prose

# D16 (v0.2.92, standing ruling R29): the fraction of an embedding model's
# num_ctx a chunk budget may occupy. Chunk budgets derive from the model's
# OWN window — int(num_ctx * (1 - this margin)) — never from a chat model's
# context and never from a preset tier entry that sits above the window
# (the xlarge tier's 13 500 vs qwen3-embedding's 10 240 was the overflow
# that silently truncated max-packed chunks by ~25% at embed time).
#
# Safety bound: a chunk of B counter-units is B * CHARS_PER_TOKEN_TEXT
# chars, i.e. B * 4 / ratio true embedding-model tokens, so it fits the
# window iff ratio >= 4 * (1 - margin). At 0.1 that bound is 3.6
# chars/token; measured minima on real repo content (2026-09-04):
# qwen3-embedding 3.94, jina-v2-base-code 3.79 — both above it with room.
# CodeSage WAS measured directly (2026-09-05, wiring-audit W1 — tokenised
# with its own tokenizer inside the code-embed container; the old claim
# that it "could not be measured" was false and its 3.12 figure
# unevidenced): real repo Python measures 3.43–4.04 chars/token whole-file
# (aggregate 3.89) and 2.96–3.15 on budget-window slices (aggregate
# ~3.47). CodeSage therefore sits BELOW the 3.6 bound — a maximal 921-unit
# part (3 684 chars) does NOT reliably fit the served 1 024-token window
# on real code, and the margin is NOT a guarantee for the code tier. What
# makes an over-dense part honest is the CodeEmbed service REFUSING it
# (HTTP 400, W1) and the caller shrinking on that refusal
# (`_embed_shrinking_on_overflow`), which tags the result — the same net
# `truncate: false` provides on the Ollama side.
#
# ONE HOME: this is the only declared num_ctx safety margin in the tree —
# vco_lib/query_enrichment.py imports it (its former private
# ``_BUDGET_SAFETY_MARGIN_RATIO = 0.1`` literal was exactly the drifted-
# duplicate shape that produced the code_truncation defect).
_BUDGET_SAFETY_MARGIN_RATIO: float = 0.1

# R39 (2026-09-04): the maximum size of ANY chunk, as a matter of RETRIEVAL
# QUALITY — deliberately BELOW the embedding models' context windows and NOT
# derived from them. An oversized chunk matches on a *fragment* and then
# returns the whole thing: partial matches retrieve massive, mostly-
# irrelevant results that crowd the answer window, so smaller chunks make
# matching sharper. qwen3-embedding's window (MODEL_TOKEN_LIMITS: 10 240;
# model arch 32k) is wider than this ceiling ON PURPOSE — do NOT read the
# gap as unused capacity to "optimise" into; it is load-bearing. This value
# also pairs with the num_ctx actually sent to Ollama (R40): the window is
# sized to the workload (~8k chunks), not to the model's ceiling.
CHUNK_TOKEN_POLICY_CEILING: int = 8_192

# The model dict is the single source of truth.
MODEL_TOKEN_LIMITS: dict[str, int] = {
    # Text embedding models
    "snowflake-arctic-embed2:latest": 4_096,     # was 2_048; bump to 4k
    "snowflake-arctic-embed2": 4_096,
    "snowflake-arctic-embed2:568m": 4_096,        # explicit-size variant
    "qwen3-embedding:0.6b": 10_240,               # was 8_192; bump to 10k (model arch supports 32k)
    "qwen3-embedding": 10_240,
    "text-embedding-3-small": 8_191,              # OpenAI documented cap
    "bge-m3:latest": 8_192,                       # NEW (verified via ollama show)
    "bge-m3": 8_192,
    "embeddinggemma:300m-bf16": 2_048,            # NEW (Modelfile pins num_ctx=2048)
    "embeddinggemma": 2_048,
    "granite-embedding:278m-fp16": 512,           # NEW (small model, 512 architectural cap)
    "granite-embedding": 512,
    # Code embedding models
    "unclemusclez/jina-embeddings-v2-base-code:latest": 2_048,  # was 8_192; v2 trained at 512
    "jina-embeddings-v2-base-code": 2_048,
    # SERVED window (sentence_bert_config.json max_seq_length), not the
    # 2 048 architectural cap in config.json — see the measured note above.
    "codesage/codesage-large-v2": 1_024,
    "codesage-large-v2": 1_024,
}

# Chunker revision sentinel. Bumped whenever MODEL_TOKEN_LIMITS or
# CHUNKING_PRESETS change in a way that produces different chunk
# boundaries — i.e. existing Weaviate rows are stale and recall
# degrades.
#
# CONSUMER (R2-4, 2026-07-22): the launcher reads this string on every boot via
# vco_lib.project_init.current_chunker_revision() and compares it against the
# persisted last-seen value (app_state `chunker.last_seen_revision`). On change
# it writes an UPDATE_DEFERRED.md entry telling the user to run
# `.claude/scripts/kg-sync --all` + `code-graph-analyze <folder>
# --from-resolver --force-recreate` to re-chunk under the new presets
# (`--from-resolver` because `--force-recreate` DROPS the five
# `<prefix>_Code*` classes and the analyzer's last identity rung is the
# folder BASENAME — v0.2.92 BLOCKER-2). See
# launcher/src-tauri/src/commands/chunker_revision_deferral.rs::
# write_chunker_deferral_if_revision_changed (the revision consumer) and
# vco_lib/project_init.py::_emit_chunker_revision_resync_deferral (the emitter).
# This SUPERSEDES the earlier "manual user action" note — bumping the string
# below now actually fires the deferral. (The older semver-boundary check,
# CHUNKER_BUMP_VERSION = "0.2.46", still exists but only ever fired across the
# one-off v0.2.46 launcher-version crossing; the v0.2.75 `--force`-flag fix
# applies to both emitters — the real drop+rebuild flag is `--force-recreate`,
# guarded by tests/test_deferral_command_argparse_sweep.py.)
#
# Revision history:
#   v0.2.92.1 (2026-09-05, wiring-audit W1 — CodeSage served-window
#     correction): MODEL_TOKEN_LIMITS["codesage-large-v2"] 2 048 → 1 024.
#     The 2 048 was the model's ARCHITECTURAL cap (config.json
#     max_position_embeddings), but sentence-transformers serves the
#     snapshot at sentence_bert_config.json's max_seq_length = 1 024 and
#     silently truncates the tail at HTTP 200 (measured live: appending
#     500 tokens past position 1 024 leaves the vector identical). Every
#     code-entity budget derived from the SSOT halves: the truncation
#     budget 7 168 → 3 584 chars, and the over-budget split tier clamps
#     from (550, 1600, 1100) to (550, 921, 921). Code-graph boundaries
#     change for every codesage-tier install — the resync deferral this
#     bump fires is the remedy (code-graph-analyze --force-recreate),
#     which the v0.2.92 chunker_preset_overhaul_pending entry already
#     prescribes, so the correction costs no ADDITIONAL user action.
#     Text-model budgets are untouched.
#     model's window, capped by the R39 retrieval-quality policy):
#     * Every tier tuple is CLAMPED to min(CHUNK_TOKEN_POLICY_CEILING 8 192,
#       int(num_ctx * (1 - margin))) — the xlarge tier max (13 500) sat
#       32% ABOVE qwen3-embedding's 10 240 num_ctx; a max-packed chunk
#       measured 13 697 true tokens and Ollama silently embedded only the
#       first 10 239 (HTTP 200, prompt_eval_count pinned at the window).
#       qwen3-class max is now the 8 192 R39 policy (retrieval quality:
#       oversized chunks match on fragments); every other tier already fit
#       both bounds and is byte-identical.
#     * Unknown-model / empty-list fallback: large_context → small_context
#       (conservative under-fill — an unknown window is a genuine unknown).
#     * TokenCounter's optional langchain chat-model path REMOVED: it never
#       used the chat tokenizer anyway (langchain_core silently substituted
#       its GPT-2 fallback — a third unit, 16-87% above embedding truth) and
#       made chunk boundaries depend on whether an optional package was
#       installed. The counter is now one deterministic chars-based unit
#       everywhere (it already was, in every venv this repo ships).
#     BOUNDARY IMPACT: qwen3-class installs re-chunk any content sized
#     8 192..13 500 units (smaller chunks, MORE of them — nothing is lost);
#     unknown-model installs get small-tier boundaries. Known-model installs
#     on every other tier are byte-unchanged.
#   v0.2.88 (2026-07-22, WP-O rework — no-functionality-loss rule):
#     REVERTED the v0.2.87 min-across-slots clamp. The ACTIVE slot's chunk
#     boundaries must NEVER drop below the single-write baseline — a dual-write
#     install must produce active-slot data ≥ identical to a single-write install.
#     v0.2.87 clamped boundaries to the TIGHTEST slot (e.g. arctic 4 096), which
#     DEGRADED the active qwen3 slot's chunk fidelity — forbidden. So active-slot
#     chunk sizing now follows the ACTIVE model's OWN preset again (UNCLAMPED,
#     identical to single-write); the SECONDARY slots absorb the degradation
#     instead — embedding_service.embed_text_all_configured embeds each secondary
#     from a BOUNDED, EXPLICITLY-TAGGED leading sub-window when the chunk exceeds
#     that secondary's num_ctx (svc.last_secondary_truncated records which slots),
#     never a silent Ollama/OpenAI truncation and never a clamp on the active
#     chunk. BOUNDARY IMPACT: for the dual-write installs that ran v0.2.87 (arctic-
#     or openai-secondary), active-slot boundaries now REVERT from the tight tier
#     back to the active model's own tier → boundaries change for those users →
#     the revision consumer surfaces a re-sync. SINGLE-model / non-dual installs:
#     v0.2.87 was already byte-identical to them, and this rework keeps that — so
#     NO change for the common install. ``chunking_preset_for_models`` /
#     ``Chunker.for_models`` are retained (used by tests + as a min-across-slots
#     utility) but are NO LONGER wired into the KG active-slot write path.
#   v0.2.87 (2026-07-22): [SUPERSEDED by v0.2.88] dual-write min-across-slots chunk
#     budget — clamped active-slot boundaries to the tightest configured slot's
#     num_ctx. Reverted above because it reduced active-slot fidelity below the
#     single-write baseline.
#   v0.2.47.5 (2026-06-04): re-cast as num_ctx (was: model architectural max).
#     Quadrupled qwen3 chunks (1500 → 13500 max tokens). 5-tier presets.
#   pre-v0.2.47.5: 3-tier presets, MODEL_TOKEN_LIMITS = model architectural max.
_CHUNKER_REVISION: str = "v0.2.92.1"


# Default chunking presets by model class.
# (min_tokens, max_tokens, target_tokens). Five tiers for fine-grained
# routing across the 512..16k+ range. Each preset packs the target around
# 60-70% of `max_tokens` so the chunker has room to fit a paragraph boundary
# without spilling into a hard truncate.
#
# v0.2.47 RL-7.5 tunings (user-locked 2026-06-04):
#   * xsmall_context: (170, 400, 330)        ~512  num_ctx (granite-embedding)
#   * small_context:  (550, 1600, 1100)      ~2k   num_ctx (jina, codesage, embeddinggemma;
#                                                codesage @ 1 024 num_ctx clamps to (550, 921, 921))
#   * medium_context: (1100, 3200, 2500)     ~4k   num_ctx (arctic2)
#   * large_context:  (2200, 6400, 4600)     ~8k   num_ctx (openai, bge-m3)
#   * xlarge_context: (4600, 13500, 9500)    ~16k+ num_ctx (qwen3-embedding @ 10k)
#
# v0.2.92 (D16 + R39): these are the RAW tier shapes; what the resolvers
# return is each tuple CLAMPED to min(CHUNK_TOKEN_POLICY_CEILING, the
# model's own num_ctx budget) (see ``_preset_for_limit``). The xlarge row
# was shaped for a ~16k window but routes for qwen3-embedding at a
# 10 240 num_ctx — unclamped it let chunks exceed the window and silently
# truncate at embed time; the R39 policy ceiling then pulls the effective
# max further down to 8 192 (retrieval quality — oversized chunks match on
# fragments). The table values stay as locked (pinned by
# test_v0247_chunker_overhaul); the clamp is applied at resolution, not by
# editing history.
CHUNKING_PRESETS: dict[str, tuple[int, int, int]] = {
    "xsmall_context": (170,  400,   330),
    "small_context":  (550,  1600,  1100),
    "medium_context": (1100, 3200,  2500),
    "large_context":  (2200, 6400,  4600),
    "xlarge_context": (4600, 13500, 9500),
}


def chunking_preset_for_model(model_name: str) -> tuple[int, int, int]:
    """Return (min_tokens, max_tokens, target_tokens) for a given model.

    The model's MODEL_TOKEN_LIMITS value is the ``num_ctx`` we actually
    send to Ollama (NOT the model's architectural max input). Tier
    boundaries are picked so each preset packs comfortably inside its
    associated num_ctx window — and, since v0.2.92 (D16 + R39), are CLAMPED
    to ``min(CHUNK_TOKEN_POLICY_CEILING, int(num_ctx *
    (1 - _BUDGET_SAFETY_MARGIN_RATIO)))``: the R39 retrieval-quality policy
    bounds the chunk size, the model's own window can only tighten it
    further, and no tier entry can budget above either.

    Falls back to ``small_context`` (the tightest general tier) when the
    name matches no registered entry: an unknown model's real window is a
    genuine unknown, and the conservative direction is to UNDER-fill —
    an over-budgeted chunk is silently truncated at the embedding model's
    num_ctx with no error, which loses content at index time. (Pre-v0.2.92
    this was ``large_context``; the v0.2.47 rationale "safe default for
    unknown 8k-class models" guessed large, which over-fills any unknown
    model with a smaller real window.)
    """
    return _preset_for_limit(_num_ctx_for_model(model_name))


def _num_ctx_for_model(model_name: str) -> "int | None":
    """Resolve a model's ``num_ctx`` (MODEL_TOKEN_LIMITS value) with partial match.

    Returns None when the name matches no registered entry (the caller then
    applies the conservative ``small_context`` default). Extracted so both
    the single-model and multi-model preset resolvers share ONE lookup rule
    (no drift).
    """
    limit = MODEL_TOKEN_LIMITS.get(model_name)
    if limit is None:
        for key, val in MODEL_TOKEN_LIMITS.items():
            if key in model_name or model_name in key:
                limit = val
                break
    return limit


def _preset_for_limit(limit: "int | None") -> tuple[int, int, int]:
    """Map a ``num_ctx`` to its chunking preset tier, clamped to the policy
    ceiling and the window — whichever is TIGHTER.

    R39 (2026-09-04): the chunk-size ceiling is a RETRIEVAL-QUALITY policy
    (``CHUNK_TOKEN_POLICY_CEILING``, deliberately below the models'
    windows); the window (``int(limit * (1 - margin))``, D16/R29) enters
    only as a DOWNWARD clamp. ``effective = min(policy, capacity)`` —
    never the capacity itself. A tier tuple above either bound (the
    xlarge tier's 13 500 max vs qwen3-embedding's 10 240 num_ctx window /
    8 192 policy) silently truncated max-packed chunks at embed time.
    Tiers that fit both are returned byte-identical.

    ``None`` (unknown model / no models at all) → ``small_context``: the
    conservative under-fill default, matching the sibling R29 fix in
    ``vco_lib/query_enrichment._resolve_budget``.
    """
    if limit is None:
        return CHUNKING_PRESETS["small_context"]
    if limit <= 512:
        tier = "xsmall_context"
    elif limit <= 2048:
        tier = "small_context"
    elif limit <= 4096:
        tier = "medium_context"
    elif limit <= 8192:
        tier = "large_context"
    else:
        tier = "xlarge_context"
    min_t, max_t, target_t = CHUNKING_PRESETS[tier]
    budget = min(
        int(limit * (1 - _BUDGET_SAFETY_MARGIN_RATIO)),
        CHUNK_TOKEN_POLICY_CEILING,
    )
    return (min(min_t, budget), min(max_t, budget), min(target_t, budget))


def chunking_preset_for_models(model_names: "list[str]") -> tuple[int, int, int]:
    """Return the preset sized to the TIGHTEST ``num_ctx`` across ``model_names``.

    NOT WIRED INTO THE ACTIVE-SLOT WRITE PATH (WP-O rework, v0.2.88): the KG
    active-slot chunker is sized to the ACTIVE model ALONE (unclamped) so its
    fidelity never drops below the single-write baseline. This min-across-slots
    helper is RETAINED as a utility (tests + any future consumer that genuinely
    wants the tightest budget) but the dual-write degradation is now handled on the
    SECONDARY side — ``EmbeddingService.embed_text_all_configured`` embeds each
    secondary from a bounded, tagged sub-window rather than clamping the shared
    chunk. Do NOT re-wire this into ``store_knowledge_node`` chunk sizing: that
    reintroduces the active-fidelity regression this rework removed.

    Historical rationale (kept for the utility's own contract): under
    ``DUAL_EMBEDDING_WRITE_ALL_SLOTS`` the SAME chunk row is embedded into EVERY
    configured text slot, and a chunk sized to a WIDE active model overflows a
    NARROWER secondary's num_ctx. This helper returns the tier that fits the
    tightest slot — the answer to "what single budget fits ALL slots" — which the
    WP-O rework deliberately does NOT use for the active slot (it fits the
    secondaries individually instead).

    Contract:
      * Empty list → ``small_context`` (D16, v0.2.92: the same conservative
        under-fill default as the single-model unknown-model fallback —
        it was ``large_context`` before, which over-filled any unknown
        model with a smaller real window).
      * One model → identical to ``chunking_preset_for_model`` (min of a
        singleton is itself, known OR unknown) — so the non-dual path is
        byte-unchanged for known models.
      * Unknown model in the set → contributes NOTHING to the min (it has
        no num_ctx): it can never WIDEN the budget beyond a known tighter
        slot, and a set of only unknowns resolves to the conservative
        ``small_context`` default. (Pre-v0.2.92 an unknown was treated as
        the ``large_context`` num_ctx 8 192 — a guessed-large value.)

    Resolution is on ``num_ctx`` (the MODEL_TOKEN_LIMITS value), NOT on the preset
    tuple, so the tightest actual context window governs even when two models map
    to the same tier.
    """
    known_ctxs = [
        ctx for ctx in (_num_ctx_for_model(name) for name in model_names)
        if ctx is not None
    ]
    if not known_ctxs:
        return _preset_for_limit(None)
    return _preset_for_limit(min(known_ctxs))


class Chunker:
    """
    Chunk text into optimal pieces

    Strategy:
    - Prefer splitting on sentence boundaries (.)
    - Fallback to double newline (\\n\\n)
    - Target: 1500 tokens per chunk (configurable per model)
    - Min: 1000 tokens (unless end of text)
    - Max: 2000 tokens
    - Use chunking_preset_for_model() to auto-configure for specific embedding models
    """

    def __init__(self, min_tokens: int = 1000, max_tokens: int = 2000, target_tokens: int = 1500):
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.target_tokens = target_tokens

    @classmethod
    def for_model(cls, model_name: str) -> "Chunker":
        """Create a Chunker with preset token limits for the given embedding model."""
        min_t, max_t, target_t = chunking_preset_for_model(model_name)
        return cls(min_tokens=min_t, max_tokens=max_t, target_tokens=target_t)

    @classmethod
    def for_models(cls, model_names: "list[str]") -> "Chunker":
        """Create a Chunker sized to the TIGHTEST slot across ``model_names``.

        NOT WIRED INTO THE ACTIVE-SLOT WRITE PATH (WP-O rework, v0.2.88): the KG
        active-slot chunker is sized to the ACTIVE model ALONE (unclamped, via
        ``Chunker.for_model``) so its fidelity never drops below the single-write
        baseline. This min-across-slots factory is RETAINED as a utility (tests +
        any future consumer that genuinely wants the tightest budget); the
        dual-write degradation is now absorbed on the SECONDARY side —
        ``EmbeddingService.embed_text_all_configured`` embeds each secondary from a
        bounded, tagged sub-window rather than clamping the shared chunk. Do NOT
        re-wire this into ``store_knowledge_node`` chunk sizing: that reintroduces
        the active-fidelity regression this rework removed. See
        ``chunking_preset_for_models`` for the full rationale.

        Delegates to ``chunking_preset_for_models`` (the SSOT min-across-slots
        resolver). With a single model this is identical to ``for_model``.
        """
        min_t, max_t, target_t = chunking_preset_for_models(model_names)
        return cls(min_tokens=min_t, max_tokens=max_t, target_tokens=target_t)

    def chunk_text(
        self,
        text: str,
        source_id: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Chunk]:
        """
        Chunk text into optimal pieces

        Args:
            text: Text to chunk
            source_id: Unique identifier for source
            metadata: Optional metadata to attach to chunks

        Returns:
            List of Chunk objects
        """
        text = text.strip()
        if not text:
            return []

        raw_chunks = self._split_text(text)
        chunks = []
        total_chunks = len(raw_chunks)

        for i, chunk_text in enumerate(raw_chunks):
            chunks.append(Chunk(
                content=chunk_text.strip(),
                chunk_number=i,
                total_chunks=total_chunks,
                token_count=TokenCounter.count_tokens(chunk_text),
                source_id=source_id,
                metadata=metadata or {},
                created_at=datetime.now(UTC).isoformat()
            ))

        return chunks

    def chunk_document(
        self,
        text: str,
        document_id: str,
        document_title: str
    ) -> List[DocumentChunk]:
        """
        Chunk a document into optimal pieces (backward compatibility)

        Args:
            text: Document text to chunk
            document_id: Unique document identifier
            document_title: Document title

        Returns:
            List of DocumentChunk objects
        """
        text = text.strip()
        if not text:
            return []

        raw_chunks = self._split_text(text)
        chunks = []
        total_chunks = len(raw_chunks)

        for i, chunk_text in enumerate(raw_chunks):
            chunks.append(DocumentChunk(
                content=chunk_text.strip(),
                chunk_number=i,
                total_chunks=total_chunks,
                token_count=TokenCounter.count_tokens(chunk_text),
                source_document_id=document_id,
                source_document_title=document_title,
                is_first=(i == 0),
                is_last=(i == total_chunks - 1),
                created_at=datetime.now(UTC).isoformat()
            ))

        return chunks

    def _split_text(self, text: str) -> List[str]:
        """Split text into chunks respecting boundaries"""
        chunks = []
        current_chunk = ""
        current_tokens = 0

        sentences = self._split_into_sentences(text)

        for sentence in sentences:
            sentence_tokens = TokenCounter.count_tokens(sentence)

            if sentence_tokens > self.max_tokens:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                    current_tokens = 0

                sub_chunks = self._split_on_newlines(sentence)
                for sub_chunk in sub_chunks:
                    chunks.append(sub_chunk)
                continue

            potential_tokens = current_tokens + sentence_tokens

            if potential_tokens > self.max_tokens:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = sentence
                current_tokens = sentence_tokens
            elif potential_tokens > self.target_tokens and current_tokens >= self.min_tokens:
                chunks.append(current_chunk)
                current_chunk = sentence
                current_tokens = sentence_tokens
            else:
                current_chunk += " " + sentence if current_chunk else sentence
                current_tokens = potential_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences"""
        pattern = r'(?<=[.!?])\s+(?=[A-Z])'
        sentences = re.split(pattern, text)
        return [s.strip() for s in sentences if s.strip()]

    def _split_on_newlines(self, text: str) -> List[str]:
        """Split text on double newlines"""
        chunks = []
        current = ""
        current_tokens = 0

        paragraphs = text.split('\n\n')

        for para in paragraphs:
            para_tokens = TokenCounter.count_tokens(para)

            if para_tokens > self.max_tokens:
                if current:
                    chunks.append(current)
                    current = ""
                    current_tokens = 0

                char_limit = self.max_tokens * CHARS_PER_TOKEN_TEXT
                for i in range(0, len(para), char_limit):
                    chunks.append(para[i:i+char_limit])
                continue

            potential_tokens = current_tokens + para_tokens

            if potential_tokens > self.max_tokens:
                if current:
                    chunks.append(current)
                current = para
                current_tokens = para_tokens
            else:
                current += "\n\n" + para if current else para
                current_tokens = potential_tokens

        if current:
            chunks.append(current)

        return chunks


# Convenience aliases for backward compatibility
DocumentChunker = Chunker


def chunk_text(
    text: str,
    source_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    min_tokens: int = 1000,
    max_tokens: int = 2000
) -> List[Chunk]:
    """Convenience function to chunk text"""
    chunker = Chunker(min_tokens=min_tokens, max_tokens=max_tokens)
    return chunker.chunk_text(text, source_id, metadata)
