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
client (``EmbeddingService``) routes every embed by it. The OpenAI code-model
patterns have ONE home, :data:`OPENAI_CODE_SLOT_ROWS`: this map matches them and
``embedding_service.CODE_SLOT_MAP`` builds its ``openai_code_embed`` rows from
them (v0.2.102 W6-B2 — they used to be two hand-kept copies).
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


#: ``(model substring, dim)`` of every OpenAI code model — the ONE home.
#: ``embedding_service.CODE_SLOT_MAP`` turns these into its
#: ``openai_code_embed`` rows; :func:`code_serving_backend` matches them.
OPENAI_CODE_SLOT_ROWS: "tuple[tuple[str, int], ...]" = (
    ("text-embedding-3-large", 3072),
    ("text-embedding-3-small", 1536),
    ("openai-", 1536),
)


def is_openai_code_model(model_id: str) -> bool:
    m = str(model_id or "").strip().lower()
    return any(sub in m for sub, _dim in OPENAI_CODE_SLOT_ROWS)


def code_serving_backend(model_id: str) -> str:
    """``code_embed`` (CodeSage) / ``openai`` (OpenAI models) / ``ollama``."""
    m = str(model_id or "").strip().lower()
    if is_openai_code_model(m):
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


# ─── Is the code_embed container part of THIS install? (v0.2.102 W6-B2) ────

#: The marker ``compose_env.compose_substitution_env`` writes to
#: ``infrastructure/.env``. MUST MATCH ``infra_watchdog.rs``
#: ``INFRA_ENV_CODE_EMBED_IN_STACK`` (the hub watchdog reads it instead of
#: re-deriving the decision — A>B>C).
CODE_EMBED_IN_STACK_KEY = "CODE_EMBED_IN_STACK"

#: The Preferences pin key (``code_embed_model.APP_STATE_KEY_CODE_EMBED_OVERRIDE``).
_APP_STATE_KEY_PIN = "code_embed_backend_override"


def code_models_in_use(install_model: str = "", *, environ=None,
                       app_state=None, binding_rows=None) -> "list[str]":
    """Every code model this install embeds with or may be asked to serve:
    the install's tier pick, a ``CODE_EMBED_MODEL`` override, the Preferences
    pin and default, and per code graph its RECORDED model (built graphs only
    — a never-built seed's placeholder serves nothing) and its fill TARGET.
    Readers default to launcher.db (read-only, soft-fail: no DB = no rows)."""
    import os as _os

    env = _os.environ if environ is None else environ
    if app_state is None or binding_rows is None:
        try:
            from vco_lib import launcher_db_reader as _ldr
        except Exception:  # noqa: BLE001 — no reader ⇒ install pick + env only
            _ldr = None
        if app_state is None:
            app_state = (_ldr.read_app_state_value if _ldr is not None
                         else (lambda _k: None))
        if binding_rows is None:
            binding_rows = (_ldr.list_codegraph_binding_rows() if _ldr is not None
                            else None) or []
    models = [install_model, env.get("CODE_EMBED_MODEL", "")]
    for key in (_APP_STATE_KEY_PIN, APP_STATE_KEY_DEFAULT_CODE_EMBED):
        try:
            models.append(app_state(key) or "")
        except Exception:  # noqa: BLE001
            pass
    for row in binding_rows:
        if row.get("last_analyzed_at"):
            models.append(str(row.get("embedding_model") or ""))
        cfg = row.get("config") or {}
        models.append(str(cfg.get("code_model_target") or ""))
        fill = cfg.get("code_model_fill") or {}
        if isinstance(fill, dict):
            models.append(str(fill.get("target") or ""))
    return [m.strip() for m in models if str(m or "").strip()]


def code_embed_in_stack(has_gpu: bool, models) -> bool:
    """THE decision (owner rule 2026-10-10 — the container exists iff CodeSage
    is selected): install / compose start ``code_embed`` — and the hub
    watchdog supervises it, through the ``CODE_EMBED_IN_STACK`` marker — only
    on a GPU host where a model in use is served by the container. jina /
    qwen3 graphs are embedded by Ollama, OpenAI ones by OpenAI."""
    return bool(has_gpu) and any(
        code_serving_backend(m) == CODE_SERVED_BY_CONTAINER for m in models or ()
    )
