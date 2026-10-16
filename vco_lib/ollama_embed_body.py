# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The ONE home for the body of a VCO Ollama EMBED request — stdlib only.

Split out of ``vco_lib.embedding_providers.ollama`` in v0.2.103 (Windows field
run, finding F1). The code_embed container's Ollama backend must send the SAME
body as every other VCO caller (owner ruling 2026-10-08: one runner per model,
which needs identical load options), but the container ships WITHOUT vco_lib
by design (its build context is the service directory). A>B>C, option B
(shared config): install resolves the body here, on the host, and projects it
into the container's environment as :data:`CONTAINER_BODY_ENV_KEY`
(``vco_lib.compose_env.compose_substitution_env`` → ``infrastructure/.env`` →
``docker-compose.yml``); the service merges the prompt into that template and
REFUSES when the template is missing or names another model — never a
silently different ``num_ctx``.

This module must stay importable without third-party packages: compose_env
sits on install.py's bootstrap path, before the venv exists, and the provider
module (which imports ``requests``) re-exports everything below.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: The env key that carries the resolved body template into the code_embed
#: container. MUST MATCH ``claude_mcp_servers/code_embedding_service/server.py``
#: ``OLLAMA_BODY_ENV_KEY`` and the ``code_embed`` environment entry in
#: ``infrastructure/docker-compose.yml`` (a parity test pins all three).
CONTAINER_BODY_ENV_KEY = "CODE_EMBED_OLLAMA_BODY"

# v0.2.47 RL-7.5 (2026-06-04): default num_ctx we send to Ollama when the
# caller doesn't override. Used when the model isn't registered in
# MODEL_TOKEN_LIMITS (chunking.py). Conservative — matches the pre-v0.2.47
# default so unknown models still embed at 8k rather than the Ollama
# default of 4k (which silently truncates inputs > 4096 tokens).
_NUM_CTX_FALLBACK: int = 8192


def _num_ctx_for_model(model: str) -> int:
    """Resolve the ``num_ctx`` to send to Ollama for a given embedding model.

    Reads ``claude_mcp_servers.weaviate_mcp.chunking.MODEL_TOKEN_LIMITS``
    when importable (the canonical source of truth for "what num_ctx do
    we want for this model"), falling back to ``_NUM_CTX_FALLBACK``
    otherwise. The chunking module is the SoT because chunk sizes must
    match the context window we actually request — if they're out of
    sync, oversized chunks get silently truncated by Ollama at num_ctx
    and the embedding signal degrades.

    Soft-fail: the lookup is best-effort. Any import error or missing
    entry falls through to ``_NUM_CTX_FALLBACK`` (8192).
    """
    try:
        from claude_mcp_servers.weaviate_mcp.chunking import MODEL_TOKEN_LIMITS
    except Exception:
        return _NUM_CTX_FALLBACK
    val = MODEL_TOKEN_LIMITS.get(model)
    if val is None:
        # Partial match (e.g. caller passed "qwen3-embedding" while the dict
        # has both "qwen3-embedding" AND "qwen3-embedding:0.6b").
        for key, registered in MODEL_TOKEN_LIMITS.items():
            if key in model or model in key:
                val = registered
                break
    return int(val) if val is not None else _NUM_CTX_FALLBACK


# v0.2.77 Part 9 task 6: keep the embedding model resident in Ollama between
# calls. Ollama's default keep_alive is ~5 min; after any idle gap the next
# embed pays a ~1.9 s model reload (measured in the hook-latency audit
# 2026-07-11). Sending keep_alive on every embed request pins the model so the
# recurring reload tax vanishes. This is a REQUEST-level fix (works regardless
# of the compose env default) and it is IDEMPOTENT — Ollama refreshes the TTL on
# every request that carries it.
#
# v0.2.102 W4-E: default dropped from "24h" to "10m" (owner ruling 2026-10-08,
# after a live measurement: ONE KG search pinned qwen3-embedding 2.4 GB +
# snowflake-arctic-embed2 1.15 GB in VRAM for 24 h each — 8.3 GB idle total).
# 10 m accepts the ~1.9 s reload after a genuinely idle gap in exchange for
# releasing idle VRAM, while still covering the multi-second cadence of a
# search/sync session (models stay resident across consecutive calls). The user
# can override via VCO_OLLAMA_KEEP_ALIVE (any value Ollama accepts: a duration
# like "30m"/"2h", "-1" for never-evict, or "0" to opt back into immediate
# unload). An empty override string means "send no keep_alive" (defer to
# Ollama's own default / the OLLAMA_KEEP_ALIVE server env) — an explicit
# opt-out.
_KEEP_ALIVE_DEFAULT = "10m"


def _keep_alive(environ: "Any" = None) -> str | None:
    """Resolve the keep_alive value to attach to embed requests.

    Returns the string to send, or ``None`` to send no keep_alive field
    (explicit opt-out via an empty ``VCO_OLLAMA_KEEP_ALIVE``). Respecting an
    explicit user override is required by the task-6 spec. ``environ``
    defaults to this process's environment.
    """
    val = (os.environ if environ is None else environ).get("VCO_OLLAMA_KEEP_ALIVE")
    if val is None:
        return _KEEP_ALIVE_DEFAULT
    val = val.strip()
    if val == "":
        # Explicit opt-out: caller wants Ollama's own default behaviour.
        return None
    return val


def _with_keep_alive(body: dict[str, Any]) -> dict[str, Any]:
    """Return ``body`` with ``keep_alive`` added when one is configured.

    Mutates a copy, never the caller's dict. Adding the key is a no-op when
    the user opted out (``_keep_alive()`` returns ``None``).
    """
    ka = _keep_alive()
    if ka is None:
        return body
    out = dict(body)
    out["keep_alive"] = ka
    return out


def embed_request_body(
    model: str,
    *,
    prompt: "str | None" = None,
    input: "str | list[str] | None" = None,  # noqa: A002 — mirrors Ollama's field
    num_ctx: "int | None" = None,
) -> dict[str, Any]:
    """ONE home for the body of a VCO Ollama EMBED request (v0.2.102 W4-E).

    Owner ruling 2026-10-08: projects must share ONE copy of each embedding
    model. Ollama loads a SECOND runner of the same model whenever load
    options differ, so every caller must send an IDENTICAL body shape per
    model: explicit ``num_ctx`` resolved from the ONE table
    (``chunking.MODEL_TOKEN_LIMITS`` via :func:`_num_ctx_for_model`),
    ``truncate: False`` (v0.2.92 — refuse, never silently drop the tail), and
    the shared ``keep_alive`` policy (:func:`_with_keep_alive`). The canonical
    adapter (:class:`OllamaAdapter`) and every inline fallback leg
    (weaviate-MCP, code-embed service, migrate/search scripts) build their
    request through THIS function so the shapes cannot drift apart.

    Exactly one of ``prompt`` (legacy single-item endpoint) or ``input``
    (modern ``/api/embed`` endpoint, str or batch) must be given.
    """
    if (prompt is None) == (input is None):
        raise ValueError("pass exactly one of prompt= or input=")
    if num_ctx is None:
        num_ctx = _num_ctx_for_model(model)
    body: dict[str, Any] = {
        "model": model,
        "options": {"num_ctx": num_ctx},
        # v0.2.92: an explicit over-window refusal beats a 200 that dropped
        # the tail (see the long note in OllamaAdapter.embed).
        "truncate": False,
    }
    if prompt is not None:
        body["prompt"] = prompt
    else:
        body["input"] = input
    return _with_keep_alive(body)

#: The ``infrastructure/.env`` key that PERSISTS the container's keep_alive
#: knob (review V02103-FINAL4 L3, coordinator ruling 2026-10-16): install
#: writes the resolved ``VCO_OLLAMA_KEEP_ALIVE`` here when its environment sets
#: it, and a later install whose environment does not set it reuses this value
#: (:func:`vco_lib.compose_env.container_keep_alive_environ`).
CONTAINER_KEEP_ALIVE_ENV_KEY = "CODE_EMBED_OLLAMA_KEEP_ALIVE"


def container_body_template(model: str, *, environ: "Any" = None) -> str:
    """The embed body for ``model`` WITHOUT its prompt, as compact JSON — the
    value of :data:`CONTAINER_BODY_ENV_KEY`.

    The service adds ``"prompt": <text>`` and posts the result, so its body is
    exactly ``embed_request_body(model, prompt=<text>)``. Compact separators
    and no ``$`` / ``#`` / whitespace keep the value one literal token for
    every ``.env`` reader on the path (compose-go, python-dotenv,
    ``vco_lib.envfile``)."""
    # Strict: the soft-fail in :func:`_num_ctx_for_model` (table unimportable
    # → 8192) is right for a live embed call but would bake a wrong window
    # into the container's config for its whole life. The table ships in the
    # same checkout, so an import failure here is a broken install — loud.
    from claude_mcp_servers.weaviate_mcp.chunking import (  # noqa: PLC0415
        MODEL_TOKEN_LIMITS,
    )

    if not MODEL_TOKEN_LIMITS:
        raise RuntimeError("chunking.MODEL_TOKEN_LIMITS is empty — broken install")

    body = embed_request_body(model, prompt="")
    body.pop("prompt")
    # keep_alive from ``environ`` (default: this process's environment).
    # compose_env passes the ONE resolved source — the install env's
    # ``VCO_OLLAMA_KEEP_ALIVE`` if set, else the value persisted in
    # infrastructure/.env, else nothing (shipped default) — to BOTH the writer
    # and the drift check, so an update run without the variable never sees
    # drift (review V02103-FINAL4 L3).
    body.pop("keep_alive", None)
    ka = _keep_alive(environ)
    if ka is not None:
        body["keep_alive"] = ka
    return json.dumps(body, separators=(",", ":"), sort_keys=True)
