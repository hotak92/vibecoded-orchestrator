# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Hardware-aware embedding / summary backend selectors (v0.2.23 C10).

Three pure decision functions that map detected hardware (VRAM, RAM,
CPU cores) + capability flags (OpenAI key available, Claude CLI
present, user consent) onto a concrete backend choice, plus the shared
CPU-capability predicate they gate their CPU fallbacks on. These are the
canonical selectors invoked by ``install.py``'s ``_choose_embedding_config``
and by the KG-summary generator (``templates/scripts/generate-kg-summary.py``).

They MUST be pure — no side effects, no probes — so the tier-boundary
regression tests (``tests/test_hardware_auto_selection.py``) can sweep the
parameter space without needing to mock subprocess / psutil / nvidia-smi
calls.

Extracted from ``install.py`` in v0.2.68 (behaviour-preserving move) to
slim the 25k-line installer and make the recurring-bug-prone hardware-tier
selection logic reusable + independently testable. ``install.py``
re-exports these names so its in-module callers (and the existing tests
that import from ``install``) keep working unchanged.

This module is a pure leaf — it depends only on stdlib / typing and must
NOT import ``install`` (``install`` imports this module, not vice-versa).

Tier boundaries are INCLUSIVE on the lower bound (``vram >= 12`` means
"12 GB exactly qualifies for the 12+ GB tier"). The spec uses "12+",
"8+", "6+", "24+" phrasing → ">=" semantics are the natural reading.

Spec source: 2026-05-21 user spec (v0.2.23 C10). See
``knowledge/concepts/hardware-tiered-backend-selection.md`` for the
rationale on why each model lands on each tier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Backend ID constants. These are the strings persisted into the
# launcher's `app_state` defaults + .env writes, so they must match what
# the rest of the codebase already understands (see EMBEDDING_CONFIGS
# entries in install.py — the IDs here are the union of `code_model` and
# `text_model` fields across the GPU / CPU / OpenAI / low_resource
# profiles).
_CODE_BACKEND_CODESAGE = "codesage-large-v2"
_CODE_BACKEND_QWEN3 = "qwen3-embedding:0.6b"
_CODE_BACKEND_JINA = "unclemusclez/jina-embeddings-v2-base-code:latest"
_CODE_BACKEND_OPENAI = "openai-text-embedding-3-small"

_KG_BACKEND_QWEN3 = "qwen3-embedding:0.6b"
_KG_BACKEND_ARCTIC = "snowflake-arctic-embed2:latest"
_KG_BACKEND_OPENAI = "openai-text-embedding-3-small"

_SUMMARY_BACKEND_CLI = "cli"           # claude CLI (Max subscription / API key)
_SUMMARY_BACKEND_QWEN35_9B = "qwen3.5:9b"
_SUMMARY_BACKEND_GEMMA = "gemma4:e4b"
_SUMMARY_BACKEND_OPENAI = "openai"     # routes via API tier with consent gate


def _cpu_meets(
    ram_gb: float,
    cores: int,
    *,
    min_ram: float,
    min_cores: int,
    strict_ram: bool = True,
) -> bool:
    """Shared CPU-capability predicate for the hardware-tier selectors.

    All three `select_*_backend` selectors gate their CPU (no-GPU /
    sub-tier-GPU) fallback on the same shape — "enough RAM AND enough
    cores to run the heavier local model" — but with DIFFERENT
    thresholds AND a different RAM-boundary semantic:

      - code + KG embedding: ``ram > 24 AND cores >= 8``  (strict RAM)
      - summary generation:  ``ram >= 12 AND cores >= 6`` (inclusive RAM)

    The strict-vs-inclusive RAM distinction is load-bearing, not
    cosmetic: the code/KG selectors deliberately use strict ``>`` on the
    24 GB boundary (v0.2.49 — a host with EXACTLY 24 GB shouldn't tier-up
    to qwen3, since qwen3-embedding on CPU-only Ollama is ~30s/embedding
    even at the boundary), whereas the summary selector uses inclusive
    ``>=`` on its 12 GB boundary (a 12 GB host CAN run gemma4:e4b for
    summary generation). So this helper PARAMETERISES the RAM comparison
    via ``strict_ram`` rather than hardcoding one or the other.

    The cores comparison is ALWAYS inclusive (``>=``) — both thresholds
    (8 for code/KG, 6 for summary) treat the exact count as qualifying.

    Args:
        ram_gb:     System RAM (GB). Coerced via ``float(... or 0.0)``
                    so a ``None`` probe-failure reads as 0 (fails the gate).
        cores:      Physical CPU cores. Coerced via ``int(... or 0)``.
        min_ram:    RAM threshold (GB).
        min_cores:  Core-count threshold (inclusive).
        strict_ram: When True (default), RAM uses strict ``>`` (code/KG
                    24 GB rule). When False, RAM uses inclusive ``>=``
                    (summary 12 GB rule).

    Returns:
        True iff the host clears BOTH the RAM and cores thresholds.
    """
    ram = float(ram_gb or 0.0)
    cpu_cores = int(cores or 0)
    ram_ok = ram > min_ram if strict_ram else ram >= min_ram
    return ram_ok and cpu_cores >= min_cores


def select_code_embedding_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    openai_key_available: bool,
    prefer_openai: bool = False,
) -> str:
    """Pick a code-embedding backend ID for the detected hardware.

    Spec (2026-05-21):
      GPU:
        - VRAM >= 12 GB → CodeSage-Large-v2
        - VRAM >=  6 GB → qwen3-embedding (1024-dim, generalist)
        - VRAM >   2 GB → Jina v2 base-code (768-dim, code-specialised)
        - else / no GPU → CPU path
      CPU (only reached when GPU path lands below "Jina via Ollama"):
        - RAM > 24 GB AND cores >= 8 → qwen3-embedding
          (strict-> on RAM since v0.2.49: a host with EXACTLY 24 GB
          shouldn't tier-up to qwen3 — qwen3-embedding on CPU-only
          Ollama is ~30s per embedding even at the boundary, confirmed
          on a contributor's 24 GB Windows box. Cores threshold stays >=8 but
          now counts PHYSICAL cores via `_probe_cpu_cores` — see its
          docstring for the SMT-counting fix.)
        - else → Jina
      OpenAI: optional override (caller passes prefer_openai=True), not
              auto-selected — it costs money per embedding.

    The ">" (strict) on the 2 GB GPU boundary is deliberate: a 2 GB card
    is below CodeSage's working set AND below Jina's comfortable RAM
    target, so it falls into the CPU bucket. >2 GB means "anything
    above 2 GB", e.g. a 4 GB card.

    Args:
        gpu_vram_gb: Detected VRAM (GB). 0.0 means "no usable GPU".
        ram_gb:      System RAM (GB).
        cores:       Logical CPU cores (psutil.cpu_count(logical=True)).
        openai_key_available: True if an OpenAI API key is configured
            (either via `--openai-key` or via the secrets system). Does
            NOT auto-pick OpenAI — only enables it as an explicit choice.
        prefer_openai: True when the caller (`--openai-key` flag, or
            the GUI's "use OpenAI for code embeddings" toggle) wants
            OpenAI even on capable hardware.

    Returns:
        One of the `_CODE_BACKEND_*` constants. Always returns
        something — there is no "None" path for code embeddings (every
        host can run Jina via Ollama as a floor).
    """
    if prefer_openai and openai_key_available:
        return _CODE_BACKEND_OPENAI

    vram = float(gpu_vram_gb or 0.0)

    if vram >= 12.0:
        return _CODE_BACKEND_CODESAGE
    if vram >= 6.0:
        return _CODE_BACKEND_QWEN3
    if vram > 2.0:
        return _CODE_BACKEND_JINA

    # CPU path: VRAM <= 2 GB OR no GPU at all.
    # v0.2.49: strict-> on RAM (was `>=`). Boundary hosts with exactly
    # 24 GB shouldn't tier-up to qwen3 — qwen3-embedding on CPU-only
    # Ollama is ~30s per embedding even at the boundary. cores
    # comparison stays `>=8` but now counts PHYSICAL cores (see
    # `_probe_cpu_cores` docstring for the v0.2.49 SMT-counting fix).
    if _cpu_meets(ram_gb, cores, min_ram=24.0, min_cores=8, strict_ram=True):
        return _CODE_BACKEND_QWEN3
    return _CODE_BACKEND_JINA


def select_kg_embedding_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    openai_key_available: bool,
    prefer_openai: bool = False,
) -> str:
    """Pick a KG / text-embedding backend ID for the detected hardware.

    Spec (2026-05-21, revised 2026-06-07 v0.2.49):
      GPU:
        - VRAM >  8 GB → qwen3-embedding (1024-dim, our default).
          v0.2.49: strict-> on the 8 GB boundary. An 8 GB card runs
          qwen3-embedding but co-existing with other GPU workloads
          (code-embedder, summary inference) at exactly 8 GB crowds
          VRAM. >8 GB gives headroom.
        - VRAM <= 8 GB → snowflake-arctic-embed2 (1024-dim, smaller
          working set — still 1024-dim so the schema slot is identical)
        - VRAM <  4 GB OR unsupported → CPU path
      CPU:
        - RAM >  24 GB AND cores >= 8 → qwen3-embedding
          (v0.2.49: strict-> on RAM, was `>=`. Boundary hosts with
          exactly 24 GB shouldn't tier-up to qwen3 — qwen3-embedding
          on CPU-only Ollama is ~30s per embedding at the boundary,
          confirmed on a contributor's 24 GB Windows box. Cores threshold stays
          `>=8` but now counts PHYSICAL cores via `_probe_cpu_cores`
          — see its docstring for the SMT-counting fix.)
        - else → arctic2
      OpenAI: optional, not auto-selected.

    The 4 GB lower bound is implicit: any GPU with <4 GB VRAM is below
    qwen3-embedding's safe working set, so we drop to the CPU path
    (where arctic2 is the small-footprint default). Cards in the 4-8 GB
    band still benefit from GPU acceleration when running arctic2.

    Args:
        gpu_vram_gb: Detected VRAM (GB). 0.0 means "no usable GPU".
        ram_gb:      System RAM (GB).
        cores:       Physical CPU cores (v0.2.49 — was logical/SMT;
                     see `_probe_cpu_cores` docstring for the switch).
        openai_key_available: True if an OpenAI API key is configured.
        prefer_openai: True when the caller wants OpenAI explicitly.

    Returns:
        One of the `_KG_BACKEND_*` constants.
    """
    if prefer_openai and openai_key_available:
        return _KG_BACKEND_OPENAI

    vram = float(gpu_vram_gb or 0.0)

    if vram > 8.0:
        return _KG_BACKEND_QWEN3
    if vram >= 4.0:
        # Mid-range GPU: arctic2 runs comfortably without crowding the
        # GPU when other models also need to load (code embedder,
        # summary inference). Same 1024-dim slot as qwen3 → no schema
        # change needed.
        return _KG_BACKEND_ARCTIC

    # CPU path (or sub-4-GB GPU treated as CPU here).
    # Same strict-> RAM / >= cores predicate as the code selector
    # (v0.2.49). See `_cpu_meets` for the strict-vs-inclusive rationale.
    if _cpu_meets(ram_gb, cores, min_ram=24.0, min_cores=8, strict_ram=True):
        return _KG_BACKEND_QWEN3
    return _KG_BACKEND_ARCTIC


def select_summary_backend(
    gpu_vram_gb: float,
    ram_gb: float,
    cores: int,
    claude_cli_available: bool,
    openai_consent: bool,
    openai_key_available: bool = False,
) -> "str | None":
    """Pick a KG-summary generation backend, or None if no path is viable.

    Spec (2026-05-21):
      claude CLI present (AND authenticated) → ALWAYS use it (highest
        quality, costs come out of the user's Max subscription).
      GPU:
        - VRAM >= 16 GB → qwen3.5:9b
        - VRAM >=  6 GB → gemma4:e4b
        - else → CPU path
      CPU:
        - RAM >= 12 GB AND cores >= 6 → gemma4:e4b
        - else → no local model viable
      OpenAI: gated on `openai_consent` (default OFF — the user has
        to explicitly opt in via Preferences). When opted in AND a key
        is configured, returns "openai" so the caller can route to the
        cheapest summary-capable model.

    Returns None when:
      - no claude CLI, AND
      - hardware can't run gemma4:e4b (sub-12 GB RAM or <6 cores AND
        no GPU >= 6 GB VRAM), AND
      - either no OpenAI consent OR no OpenAI key.

    The None case is NOT an error — install.py should record a
    `kg_summary_no_backend` deferral entry and continue. The KG
    summariser script silently no-ops on None, leaving raw KG content
    in place (search still works, just without LLM-polished
    descriptions / chunk summaries).

    Args:
        gpu_vram_gb: Detected VRAM (GB).
        ram_gb:      System RAM (GB).
        cores:       Logical CPU cores.
        claude_cli_available: True if `claude` is on PATH and
            authenticated (caller is responsible for verifying with a
            cheap smoke test; this selector takes the boolean at face
            value).
        openai_consent: True when the user has explicitly opted in
            (`app_state` key `kg_summary_openai_consent=true`).
        openai_key_available: True if an OpenAI key is configured in
            secrets. Combined with `openai_consent` to gate the OpenAI
            path.

    Returns:
        Backend ID string, or None when nothing viable is available.
        Possible strings: "cli", "qwen3.5:9b", "gemma4:e4b", "openai".
    """
    # CLI always wins when available — best quality, no local resource
    # cost, paid out of the user's subscription.
    if claude_cli_available:
        return _SUMMARY_BACKEND_CLI

    vram = float(gpu_vram_gb or 0.0)

    # GPU tiers.
    if vram >= 16.0:
        return _SUMMARY_BACKEND_QWEN35_9B
    if vram >= 6.0:
        return _SUMMARY_BACKEND_GEMMA

    # CPU tier (only when GPU is sub-6 GB or absent).
    # NOTE: inclusive (`>=`) RAM boundary here — a 12 GB host CAN run
    # gemma4:e4b — unlike the code/KG selectors' strict-> 24 GB rule.
    # `strict_ram=False` parameterises that distinction. See `_cpu_meets`.
    if _cpu_meets(ram_gb, cores, min_ram=12.0, min_cores=6, strict_ram=False):
        return _SUMMARY_BACKEND_GEMMA

    # No local path viable. Last resort: OpenAI, only if the user
    # explicitly consented AND a key is available.
    if openai_consent and openai_key_available:
        return _SUMMARY_BACKEND_OPENAI

    return None


# ─────────────────────────────────────────────────────────────────────
# v0.2.77 Part 3 (5c) — shared-memory embedding-concurrency budget
# ─────────────────────────────────────────────────────────────────────
#
# THE INCIDENT (live, 2026-07-10): a launcher "update all projects" over 6
# projects fanned out N analyzers + N kg-syncs at once against a single 16 GiB
# GPU. The code-embed service's FIXED semaphore (CODE_EMBED_MAX_CONCURRENT,
# default 4 — env-only, never hardware-derived) shed 503s under the burst, and
# 61+28 CodeFunction rows were written VECTORLESS (embed_revision=0) on two
# projects. The fix is to DERIVE the concurrency limits from the SAME hardware
# specs that pick the embedding models, so a small GPU gets a small budget.
#
# USER DESIGN RULING (verbatim semantics — implemented exactly here):
#   max_parallel = floor((system_memory / model_memory) * 0.8)
# derived in the same code that selects the embedding model from system specs.
# ADDENDUM: codegraph AND KG embedding workloads may run in parallel on
# DIFFERENT models sharing ONE memory device → ONE shared budget. Reserve
# every concurrently-active model's BASE footprint first, then allocate each
# pipeline's parallel slots from the REMAINING pool. NEVER compute independent
# per-pipeline maxima that only fit alone.
#
#   * model_memory = per-parallel-slot footprint on the GPU service (weights
#     amortized ONCE across concurrent requests + a per-request activation
#     increment) for the code-embed service; and the full loaded-instance
#     footprint per concurrently-loaded MODEL for Ollama (Ollama serializes
#     requests internally, so its "slot" IS one whole model load).
#   * system_memory = free VRAM on the GPU tier / free RAM on the CPU tiers,
#     measured by the SAME probe the ladder uses (caller passes it in — this
#     function stays pure, no probes).

# Ceiling on any single concurrency knob. A cosmically large free-memory
# reading (or a mocked huge value) must never mint an unbounded semaphore:
# past ~8 concurrent embed requests the single-GPU inference Lock in the
# code-embed service (throughput is 1 at the accelerator) makes extra queue
# depth pure latency, not throughput. Documented cap, tunable only in code.
_CONCURRENCY_CEILING = 8

# The 0.8 safety factor from the user formula: leave 20% memory headroom for
# fragmentation, framework overhead (torch/cuda context, HTTP buffers), and
# the OS. Named so the formula reads self-documenting at the call site.
_MEMORY_SAFETY_FACTOR = 0.8

# ── Model footprint table (SINGLE SOURCE — kept AS DATA in this module) ──
#
# Per the A>B>C rule (CLAUDE.md), this is the ONE authoritative footprint
# table; the Rust side consumes the RESULT via app_state (channel B: shared
# config), it does NOT mirror this table. Each entry is (base_gb, slot_gb):
#   base_gb — resident weight footprint of ONE loaded model instance.
#   slot_gb — incremental memory of one ADDITIONAL concurrent request against
#             an already-loaded model (activations + KV/pooling scratch). The
#             per-slot footprint is what the user formula divides `pool` by.
#
# Sources (rationale for each number):
#   * qwen3-embedding:0.6b   base ~1.2 GB  — gpu_policy.rs module docstring
#       VRAM table ("qwen3-embedding:0.6b ~1.2 GB"). 0.6B params. Small
#       activation → slot ~0.3 GB.
#   * codesage-large-v2      base ~2.6 GB  — gpu_policy.rs docstring
#       ("CodeSage-Large-v2 ~2.6 GB"). 1.3B params, longer code context →
#       larger activation → slot ~0.6 GB.
#   * snowflake-arctic-embed2 base ~1.1 GB — 568M params, 1024-dim (same
#       schema slot as qwen3, chosen on the 4–8 GB tier for its smaller
#       working set — see select_kg_embedding_backend). slot ~0.3 GB.
#   * jina-embeddings-v2-base-code base ~0.4 GB — 161M params, 768-dim, the
#       CPU / low-VRAM floor. slot ~0.15 GB.
#   * openai-text-embedding-3-small — remote API, ZERO local footprint; its
#       concurrency is bounded by the provider, not local memory. base/slot 0.
#
# Numbers are deliberately CONSERVATIVE (round up on base, up on slot): the
# cost of over-reserving is one fewer parallel slot; the cost of
# under-reserving is the 503-storm this whole feature prevents.
_MODEL_FOOTPRINT_GB: "dict[str, tuple[float, float]]" = {
    _CODE_BACKEND_CODESAGE: (2.6, 0.6),
    _CODE_BACKEND_QWEN3: (1.2, 0.3),      # == _KG_BACKEND_QWEN3 (same tag)
    _CODE_BACKEND_JINA: (0.4, 0.15),
    _KG_BACKEND_ARCTIC: (1.1, 0.3),
    _CODE_BACKEND_OPENAI: (0.0, 0.0),     # == _KG_BACKEND_OPENAI (remote)
}

# Fallback footprint for a backend ID not in the table (a future model added
# to the selectors but not yet sized here). Conservative: assume a mid-size
# local model so an unknown backend never mints an over-large budget.
_UNKNOWN_FOOTPRINT_GB: "tuple[float, float]" = (1.5, 0.4)


def _footprint_for(backend_id: str) -> "tuple[float, float]":
    """(base_gb, slot_gb) for a backend ID; conservative default when absent."""
    return _MODEL_FOOTPRINT_GB.get(backend_id or "", _UNKNOWN_FOOTPRINT_GB)


@dataclass(frozen=True)
class EmbeddingConcurrencyBudget:
    """Hardware-derived concurrency limits for the embedding pipelines.

    Produced by :func:`select_embedding_concurrency` from the SAME hardware
    specs the model selectors consume, so a small GPU gets a small budget and
    the update-all fan-out can never re-create the 503-storm that wrote
    vectorless rows (v0.2.77 5c incident).

    Fields:
        code_embed_max_concurrent: value for the code-embed service's
            ``CODE_EMBED_MAX_CONCURRENT`` env (in-flight requests before it
            sheds with 503). Floor 1, capped at ``_CONCURRENCY_CEILING``.
        update_all_max_parallel_projects: how many projects' embed-heavy
            background work (codegraph build + kg-sync) may run at once during
            a launcher "update all". The Rust admission semaphore reads this
            from app_state. Floor 1, capped at ``_CONCURRENCY_CEILING``.
        reserved_code_base_gb / reserved_kg_base_gb: the base footprints
            reserved for the concurrently-active code / KG models (the shared
            pool is what's left after both). Surfaced for observability /
            debugging the derived numbers.
        pool_gb: free memory remaining after reserving both base footprints
            (the pool the parallel slots are allocated from).
        system_memory_gb / device: the inputs the budget was derived from,
            echoed back for logging.
    """

    code_embed_max_concurrent: int
    update_all_max_parallel_projects: int
    reserved_code_base_gb: float
    reserved_kg_base_gb: float
    pool_gb: float
    system_memory_gb: float
    device: str = "cpu"
    notes: "tuple[str, ...]" = field(default_factory=tuple)


def _clamp_slots(raw: int) -> int:
    """Floor at 1, cap at the documented ceiling."""
    if raw < 1:
        return 1
    if raw > _CONCURRENCY_CEILING:
        return _CONCURRENCY_CEILING
    return raw


def _formula_slots(pool_gb: float, slot_gb: float) -> int:
    """The user ruling's core formula: floor((pool / slot) * 0.8).

    A non-positive / non-finite slot footprint (remote API model, or a bad
    table entry) means "no local memory constraint" → the caller decides the
    knob independently; here we return the ceiling so the clamp caps it.
    """
    if not math.isfinite(slot_gb) or slot_gb <= 0.0:
        return _CONCURRENCY_CEILING
    if not math.isfinite(pool_gb) or pool_gb <= 0.0:
        return 1
    return int(math.floor((pool_gb / slot_gb) * _MEMORY_SAFETY_FACTOR))


def select_embedding_concurrency(
    system_memory_gb: float,
    code_backend: str,
    kg_backend: str,
    *,
    device: str = "cpu",
    both_gpu_resident: bool = True,
) -> EmbeddingConcurrencyBudget:
    """Derive the shared-pool embedding-concurrency budget for this host.

    Pure function (no probes, no I/O) so the tier-boundary sweep tests can
    exercise the whole parameter space — same discipline as the model
    selectors above.

    Implements the USER DESIGN RULING exactly (see the module-level block):
    ONE shared memory pool. Reserve the base footprint of EVERY concurrently
    active model first (code model + KG model when both are GPU-resident),
    then allocate each pipeline's parallel slots from the remaining pool via
    ``floor((pool / slot_footprint) * 0.8)``. Floor 1, cap
    ``_CONCURRENCY_CEILING``.

    Args:
        system_memory_gb: FREE memory on the shared device — free VRAM on a
            GPU tier, free RAM on a CPU tier. Measured by the caller (the same
            probe the ladder uses); this function takes it as data.
        code_backend: the code-embedding backend ID (a ``_CODE_BACKEND_*``
            value from :func:`select_code_embedding_backend`).
        kg_backend: the KG/text-embedding backend ID (a ``_KG_BACKEND_*``
            value from :func:`select_kg_embedding_backend`).
        device: "cuda" | "rocm" | "metal" | "cpu" — echoed into the budget for
            logging; does not change the math (the shared-pool reservation is
            device-agnostic; the CALLER decides whether both models are
            co-resident on one device via ``both_gpu_resident``).
        both_gpu_resident: True (default) when the code + KG models occupy the
            SAME memory device simultaneously (the GPU-accelerated case, and
            the CPU case where both Ollama models load into one RAM pool) → BOTH
            base footprints are reserved from the shared pool. False when only
            one pipeline is memory-resident at a time (reserve only the larger
            base) — a caller override for exotic split-device setups; the
            default is the conservative shared-pool reservation the incident
            demands.

    Returns:
        An :class:`EmbeddingConcurrencyBudget`.
    """
    mem = float(system_memory_gb or 0.0)
    if not math.isfinite(mem) or mem < 0.0:
        mem = 0.0

    code_base, code_slot = _footprint_for(code_backend)
    kg_base, kg_slot = _footprint_for(kg_backend)

    notes: "list[str]" = []

    # ── Reserve base footprints from the ONE shared pool ────────────────
    # When both models are co-resident (the GPU incident case), reserve BOTH
    # bases. When the caller says only one is resident at a time, reserve the
    # larger base (the worst case for the pool).
    if both_gpu_resident:
        reserved_code = code_base
        reserved_kg = kg_base
    else:
        # Only one pipeline resident at a time: the pool must survive the
        # larger of the two bases. Attribute the reservation to whichever is
        # bigger; zero the other for the breakdown.
        if code_base >= kg_base:
            reserved_code, reserved_kg = code_base, 0.0
        else:
            reserved_code, reserved_kg = 0.0, kg_base
        notes.append("single-resident: reserved only the larger base")

    pool = mem - reserved_code - reserved_kg
    if pool < 0.0:
        # The device can't even hold the base weights of the active models
        # with headroom — force the minimum-viable single-slot budget and say
        # so. (The service still runs; it just can't parallelise.)
        notes.append(
            f"pool underflow: free {mem:.1f} GB < reserved "
            f"{reserved_code + reserved_kg:.1f} GB — clamped to 1 slot"
        )
        pool = 0.0

    # ── code-embed service in-flight cap ───────────────────────────────
    # The code-embed service serves the CODE model; its per-slot footprint is
    # code_slot. Requests share the amortized code weights (already reserved
    # as code_base) and each adds one code_slot of activation.
    code_embed_max_concurrent = _clamp_slots(_formula_slots(pool, code_slot))

    # ── update-all per-project cap ─────────────────────────────────────
    # Each project's update runs a codegraph analyze (code-embed service) AND
    # a kg-sync (Ollama text model). Both draw from the SAME shared pool, so a
    # project's worst-case marginal footprint while active is one code_slot +
    # one kg_slot. Deriving the project cap from that combined per-project
    # slot keeps N-projects-at-once within the pool the formula guarantees.
    per_project_slot = code_slot + kg_slot
    update_all_max_parallel_projects = _clamp_slots(
        _formula_slots(pool, per_project_slot)
    )

    return EmbeddingConcurrencyBudget(
        code_embed_max_concurrent=code_embed_max_concurrent,
        update_all_max_parallel_projects=update_all_max_parallel_projects,
        reserved_code_base_gb=round(reserved_code, 3),
        reserved_kg_base_gb=round(reserved_kg, 3),
        pool_gb=round(pool, 3),
        system_memory_gb=round(mem, 3),
        device=device or "cpu",
        notes=tuple(notes),
    )
