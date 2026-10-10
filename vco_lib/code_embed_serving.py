# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Where a code-embedding model is SERVED from — the ONE map (v0.2.102 W6-B2).

Owner rule (2026-10-10): "CodeSage is not on Ollama, so if it's selected we use
it via its own container; if we select Jina (or qwen) for codegraph → it's run in
Ollama. Where the code embeddings are served from changes depending on the
actual model being used."

* CodeSage → the ``code_embed`` container (its ``gpu`` backend:
  sentence-transformers; the hub watchdog's design note — ``CODE_EMBED_BACKEND=
  ollama`` ⇒ the container is not part of the stack — says the same);
* an OpenAI model → the OpenAI API;
* every other code model (jina-v2-base-code, qwen3-embedding, any Ollama tag) →
  Ollama directly.

Stdlib only: ``vco_lib.compose_env`` and ``vco_lib.service_adoption`` sit on
install.py's BOOTSTRAP path (before the venv exists) and decide the container's
``CODE_EMBED_BACKEND`` / ``CODE_EMBED_MODEL`` pairing from this same map; the
client (``EmbeddingService``) routes every embed by it. The model patterns MUST
MATCH ``embedding_service.CODE_SLOT_MAP`` (codesage_embed / openai_code_embed
rows) — pinned by a parity test.
"""
from __future__ import annotations

#: install.py's hardware-ladder pick / the "Default code embedding" dropdown.
APP_STATE_KEY_DEFAULT_CODE_EMBED = "default_code_embedding"

CODE_SERVED_BY_CONTAINER = "code_embed"
CODE_SERVED_BY_OLLAMA = "ollama"
CODE_SERVED_BY_OPENAI = "openai"

#: The code_embed container's CodeSage id (sentence-transformers form). MUST
#: MATCH ``code_embedding_service/server.py`` ``GPU_DEFAULT_MODEL``.
CODE_EMBED_GPU_MODEL = "codesage/codesage-large-v2"


def code_serving_backend(model_id: str) -> str:
    """``code_embed`` (CodeSage) / ``openai`` (OpenAI models) / ``ollama``."""
    m = str(model_id or "").strip().lower()
    if m.startswith("openai-") or "text-embedding-3" in m:
        return CODE_SERVED_BY_OPENAI
    if "codesage" in m:
        return CODE_SERVED_BY_CONTAINER
    return CODE_SERVED_BY_OLLAMA


def container_pairing(code_model: str) -> "tuple[str, str] | None":
    """``(CODE_EMBED_BACKEND, CODE_EMBED_MODEL)`` for the code_embed container
    when ``code_model`` is the install's code model: CodeSage → ``gpu`` +
    the CodeSage id; an Ollama model → ``ollama`` + that model (the container
    only proxies it — the client embeds it through Ollama directly); an OpenAI
    model → None (no container role). The pair is always one the container
    can serve."""
    serving = code_serving_backend(code_model)
    if serving == CODE_SERVED_BY_CONTAINER:
        return "gpu", CODE_EMBED_GPU_MODEL
    if serving == CODE_SERVED_BY_OLLAMA and str(code_model or "").strip():
        return "ollama", str(code_model).strip()
    return None
