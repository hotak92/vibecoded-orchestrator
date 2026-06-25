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
