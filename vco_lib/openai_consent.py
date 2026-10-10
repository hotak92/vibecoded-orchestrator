# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""What VCO may spend the OpenAI key on — the EMBEDDINGS consent (v0.2.102).

Owner ruling 2026-10-10: two separate toggles for what the OpenAI key is used
for — SUMMARIES generation (``kg_summary_openai_consent``, read by
``summary_backends.openai_consent_granted``, unchanged) and EMBEDDINGS
generation (``openai_embeddings_consent``, this module), which covers BOTH the
KG text slots (an OpenAI active profile, the ``openai_text_embed`` secondary)
and the code graph (an OpenAI code model, the ``openai_code_embed``
secondary). :func:`embeddings_consent_granted` is the ONE reader; every
OpenAI embed site in ``vco_lib.embedding_service`` and the code-model resolver
ask it.

Migration (no silent grant): a summaries consent NEVER grants embeddings.
While the row is absent the answer FOLLOWS the one explicit opt-in to OpenAI
embeddings that exists — the active embedding profile is ``openai`` (the
wizard's "Use OpenAI as the default embedding provider", the Identity-tab
profile pick, ``install.py --openai-key``). Nothing is written on the user's
behalf: a written default would later override a fresh wizard opt-in. Once the
user flips the launcher toggle, the row decides.
"""
from __future__ import annotations

from typing import Callable, Optional

#: MUST MATCH ``launcher/src/lib/preferences/loaders.ts``
#: ``APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT``.
APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT = "openai_embeddings_consent"
#: The launcher toggle's label — every refusal names it.
EMBEDDINGS_TOGGLE = (
    "Preferences → OpenAI API key → \"Allow OpenAI for embeddings (KG + code graph)\""
)


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"true", "1", "yes"}


def derived_default() -> bool:
    """The consent an install that never saw the toggle already gave: the
    active embedding profile is ``openai``."""
    try:
        from vco_lib.kg_context_triple import active_embedding_profile_or_none

        return (active_embedding_profile_or_none() or "").strip().lower() == "openai"
    except Exception:  # noqa: BLE001 — an unreadable profile is "no opt-in"
        return False


def embeddings_consent_granted(
    app_state: Optional[Callable[[str], Optional[str]]] = None,
) -> bool:
    """May VCO make cost-bearing OpenAI EMBEDDING calls? The row decides
    (``true``/``1``/``yes``); an absent row falls back to :func:`derived_default`."""
    if app_state is None:
        from vco_lib.launcher_db_reader import read_app_state_value as app_state
    raw = app_state(APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT)
    if isinstance(raw, str) and raw.strip():
        return _truthy(raw)
    return derived_default()


def refusal_message(model: str, what: str) -> str:
    return (
        f"OpenAI embeddings are not allowed: {what} uses the OpenAI model {model}, "
        f"but {EMBEDDINGS_TOGGLE} is off ({APP_STATE_KEY_OPENAI_EMBEDDINGS_CONSENT}). "
        "Turn it on, or pick a local model. No cost-bearing call is made until then."
    )
