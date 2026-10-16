# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""GPU profile helpers (v0.2.54 gpu-audit C-3 / C-5).

Extracted from install.py per the search-before-add /
extract-before-duplicate discipline. Two pure helpers:

- :func:`gpu_vendor_preference_from_env` — read the ``VCT_GPU_VENDOR``
  override (case-insensitive, with stderr warning on unknown values).
- :func:`apply_tier_overrides` — swap an embedding-profile dict's
  ``code_model`` / ``text_model`` for hardware-tier picks while keeping
  dependent fields (``code_dims``, ``text_dims``, ``active_embedding``)
  consistent. The Ollama pull list is NOT derived here any more — v0.2.100
  moved it to :mod:`vco_lib.embedding_pull_plan`, which reads the final
  config (appending here only ever grew the list, never shrank it: L1-F12).

Neither helper references install.py module-level state; install.py's
constants are passed in as parameters so the same helper is reusable
from any caller (per-project installer, doctor command, replay tools).
"""

from __future__ import annotations

import sys
from typing import Mapping, Optional


def gpu_vendor_preference_from_env() -> Optional[str]:
    """Return the user's explicit ``VCT_GPU_VENDOR`` preference, or None.

    Case-insensitive, trimmed; empty / ``"auto"`` means no preference;
    unknown values log to stderr and are ignored. Lenient by design:
    misconfigured envs never strand an install — they fall through to
    auto-detect with a one-line warning.
    """
    import os
    raw = os.environ.get("VCT_GPU_VENDOR", "").strip().lower()
    if not raw or raw == "auto":
        return None
    if raw in ("nvidia", "amd", "metal"):
        return raw
    print(
        f"  VCT_GPU_VENDOR={raw!r} unrecognized (expected "
        "'nvidia' / 'amd' / 'metal' / 'auto'); falling through to "
        "auto-detect.",
        file=sys.stderr,
    )
    return None


#: v0.2.103 F2 — human names for the label rebuild. Keyed on the model ids
#: the profiles / the tier ladder can produce (a prefix the map doesn't know
#: falls back to the model id itself, so a future model never crashes a label).
_TIER_LABEL: "dict[str, str]" = {
    "qwen3-embedding:0.6b": "qwen3",
    "snowflake-arctic-embed2:latest": "Arctic",
    "codesage-large-v2": "CodeSage",
    "unclemusclez/jina-embeddings-v2-base-code:latest": "Jina V2",
    "text-embedding-3-small": "OpenAI",
}


def _rebuilt_description(config: dict, *, has_gpu: Optional[bool] = None) -> str:
    """The profile description rebuilt from the FINAL models (v0.2.103 F2).

    The field case: an 8 GB host gets the ``low_resource`` profile whose
    stock description says "Jina V2 code", then the tier override swaps the
    code model to qwen3 — the printed label kept promising Jina. The
    profile KEY still names the text tier; only the parenthesised body is
    regenerated, from the models actually chosen, and the SERVING phrase
    follows the final code MODEL (v0.2.103 R41 L4 — the profile's
    ``code_backend`` is deliberately not swapped, so it can name a backend
    the swapped model is not served by). The prefix ("GPU-accelerated"
    / "CPU-only" / "Low-resource" / …) is kept from the stock description,
    EXCEPT when ``has_gpu`` says the hardware contradicts it (a cpu profile
    on a 6-12 GB GPU host is not "CPU-only" — its Ollama runs on the GPU).
    """
    def label(model: object) -> str:
        text = str(model or "").strip()
        return _TIER_LABEL.get(text, text or "?")

    text = label(config.get("text_model"))
    code = label(config.get("code_model"))
    serving = ""
    try:
        from vco_lib.code_embed_serving import code_serving_backend

        serving = {
            "code_embed": "best quality",
            "openai": "via the OpenAI API",
            "ollama": "both via Ollama",
        }.get(code_serving_backend(str(config.get("code_model") or "")), "")
    except Exception:  # noqa: BLE001 — a label never raises
        serving = ""
    body = f"{text} text + {code} code"
    if serving:
        body += f", {serving}"
    stock = str(config.get("description") or "")
    prefix = stock.split(" (", 1)[0].strip() if " (" in stock else ""
    if has_gpu is True and prefix.lower().startswith("cpu-only"):
        prefix = "GPU-accelerated"
    elif has_gpu is False and prefix.lower().startswith("gpu-accelerated"):
        prefix = "CPU-only"
    return f"{prefix} ({body})" if prefix else body


def apply_tier_overrides(
    config: dict,
    *,
    code_pick: str,
    kg_pick: str,
    model_dims: Mapping[str, int],
    text_model_active_embedding: Mapping[str, str],
    has_gpu: Optional[bool] = None,
) -> None:
    """Swap the profile's stock models for the hardware-tier picks,
    keeping every dependent field consistent (v0.2.54, gpu-audit C-5).

    Mutates ``config`` in place (scalar fields only — safe on a shallow copy
    of a shared profile). The two mapping parameters are install.py's
    module-level constants (``_EMBEDDING_MODEL_DIMS``,
    ``_TEXT_MODEL_ACTIVE_EMBEDDING``) passed by reference so this helper has
    no coupling to install.py. ``has_gpu`` (v0.2.103 R41 L4) corrects the
    rebuilt label's prefix when the detected hardware contradicts the stock
    profile's wording; ``None`` keeps the stock prefix.

    See install.py's `_apply_tier_overrides` docstring for the rationale
    on why ``code_backend`` is NOT swapped.
    """
    swapped = False
    if config.get("code_model") != code_pick:
        config["code_model"] = code_pick
        dims = model_dims.get(code_pick)
        if dims is not None:
            config["code_dims"] = dims
        swapped = True
    if config.get("text_model") != kg_pick:
        config["text_model"] = kg_pick
        dims = model_dims.get(kg_pick)
        if dims is not None:
            config["text_dims"] = dims
        slot = text_model_active_embedding.get(kg_pick)
        if slot is not None:
            config["active_embedding"] = slot
        swapped = True
    if swapped:
        # v0.2.103 F2: the label follows the override — the stock
        # description still names the profile's STOCK code model otherwise
        # ("Jina V2 code" printed on a host whose code model is qwen3).
        config["description"] = _rebuilt_description(config, has_gpu=has_gpu)
