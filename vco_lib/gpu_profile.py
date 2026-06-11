# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""GPU profile helpers (v0.2.54 gpu-audit C-3 / C-5).

Extracted from install.py per the search-before-add /
extract-before-duplicate discipline. Two pure helpers:

- :func:`gpu_vendor_preference_from_env` — read the ``VCT_GPU_VENDOR``
  override (case-insensitive, with stderr warning on unknown values).
- :func:`apply_tier_overrides` — swap an embedding-profile dict's
  ``code_model`` / ``text_model`` for hardware-tier picks while keeping
  dependent fields (``code_dims``, ``text_dims``, ``active_embedding``,
  ``embedding_models`` pull list) consistent.

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


def apply_tier_overrides(
    config: dict,
    *,
    code_pick: str,
    kg_pick: str,
    model_dims: Mapping[str, int],
    text_model_active_embedding: Mapping[str, str],
    ollama_served_models: set[str],
) -> None:
    """Swap the profile's stock models for the hardware-tier picks,
    keeping every dependent field consistent (v0.2.54, gpu-audit C-5).

    Mutates ``config`` in place. The three mapping/set parameters are
    install.py's module-level constants (``_EMBEDDING_MODEL_DIMS``,
    ``_TEXT_MODEL_ACTIVE_EMBEDDING``, ``_OLLAMA_SERVED_EMBEDDING_MODELS``)
    passed by reference so this helper has no coupling to install.py.

    See install.py's `_apply_tier_overrides` docstring for the rationale
    on why ``code_backend`` is NOT swapped.
    """
    if config.get("code_model") != code_pick:
        config["code_model"] = code_pick
        dims = model_dims.get(code_pick)
        if dims is not None:
            config["code_dims"] = dims
        if (code_pick in ollama_served_models
                and code_pick not in config.get("embedding_models", [])):
            # Copy-on-write: config is a SHALLOW copy of the module-level
            # EMBEDDING_CONFIGS profile — appending in place would mutate
            # the shared profile list across calls.
            config["embedding_models"] = (
                list(config.get("embedding_models", [])) + [code_pick]
            )
    if config.get("text_model") != kg_pick:
        config["text_model"] = kg_pick
        dims = model_dims.get(kg_pick)
        if dims is not None:
            config["text_dims"] = dims
        slot = text_model_active_embedding.get(kg_pick)
        if slot is not None:
            config["active_embedding"] = slot
        if (kg_pick in ollama_served_models
                and kg_pick not in config.get("embedding_models", [])):
            config["embedding_models"] = (
                list(config.get("embedding_models", [])) + [kg_pick]
            )
