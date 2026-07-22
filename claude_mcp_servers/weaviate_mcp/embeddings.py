# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Embedding-generation layer for the Weaviate MCP server.

v0.2.73 M-1: extracted VERBATIM from ``server.py`` (functions were
previously inline in the ~10k-line monolith). Behaviour is unchanged —
this is a pure move+import refactor with ZERO logic changes.

What lives here:
  * the lazy/cached ``EmbeddingService`` accessor (``_get_embedding_service``)
    and its two module-level cache globals (``_cached_embed_service`` /
    ``_embed_service_construction_failed_at``);
  * every text/code embed helper (``get_ollama_embedding``,
    ``get_openai_embedding``, ``get_code_embedding``, ``_get_search_vector``,
    ``_get_all_kg_embeddings`` …) that used to hang off ``server``;
  * the small scheme helpers (``_scheme_for_collection`` /
    ``_primary_named_vector``) and ``count_tokens_async``.

Coupling notes (why some references go through the ``server`` module object
rather than a plain import):

  * Config constants (``ACTIVE_EMBEDDING``, ``OLLAMA_URL``, ``EMBEDDING_MODEL``
    …) are resolved ONCE at ``server`` import time (env / vct-hub) and never
    mutated afterwards, so importing their *values* here at load time is
    behaviour-identical. They come in via ``from . import server`` inside the
    accessors that need them — a call-time import that also sidesteps the
    circular-import edge (``server`` imports THIS module near the end of its
    own body).
  * The EmbeddingService cache (``_cached_embed_service``) and the two
    patchable helpers (``_get_embedding_service`` /
    ``_inline_code_embed_http``) are referenced through the ``server`` module
    object so the existing test suite — which does
    ``server._cached_embed_service = fake`` /
    ``patch("…server._get_embedding_service")`` /
    ``server._inline_code_embed_http = _fake_http`` and calls the helpers via
    ``server.<fn>`` — keeps observing the same live attribute. ``server``
    re-exports every function below into its own namespace, so
    ``server.get_ollama_embedding`` etc. remain valid callables + patch
    targets. The single source of truth for the mutable cache stays on the
    ``server`` module; this module reads/writes it via ``server.<name>``.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp


# ─── Embedding config constants (v0.2.75 P3g / M-1 remainder) ────────────
# These pure-``os.getenv`` embedding-config constants DEFINE here (this is the
# embedding layer) rather than on ``server.py``; ``server`` re-exports them into
# its own namespace so bare ``server.<name>`` reads + test patches on the server
# object keep working. Only the getenv-resolved config that has NO dependency on
# a server-side resolver moves — ``ACTIVE_EMBEDDING`` (vct-hub-resolved via
# ``server._config_field``) and the broadly-read search config
# (``EMBEDDING_MODEL`` / ``EMBEDDING_SOURCE`` / ``DUAL_EMBEDDING_ENABLED`` /
# ``OLLAMA_URL``) stay on ``server`` where the resolver + read surface live.
# Legacy text embedding model (kept for backward compat — old named vectors stay populated)
LEGACY_TEXT_EMBEDDING_MODEL = os.getenv("LEGACY_TEXT_EMBEDDING_MODEL", "snowflake-arctic-embed2:latest")
# OpenAI embedding config (only used when ACTIVE_EMBEDDING=openai or DUAL_EMBEDDING_ENABLED=true)
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Code embedding service URL (CodeSage-Large-v2 via FastAPI, or Ollama-compatible endpoint)
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")


# ─── EmbeddingService accessor ──────────────────────────────────────────
#
# v0.2.18: Lazy + cached EmbeddingService accessor.
#
# Why lazy: the MCP server is long-running (one process per Claude Code
# session). Constructing at import time would probe backends before the
# Weaviate/Ollama containers have settled, producing a stale
# NoEmbeddingBackendError that survives until the next session restart.
#
# Why cached: the service owns an HTTP connection pool; one instance
# amortises TLS+keep-alive across every embed call this MCP makes for
# the rest of the session.
#
# Why not module-level singleton: per the v0.2.18 locked design
# decision, EmbeddingService is per-project — but this MCP server IS
# pinned to one project for its lifetime (KG_COLLECTION env is set per-
# project by the launcher), so "per-MCP-instance" satisfies the
# per-project constraint.
#
# Concurrency: the cached service is initialised under the asyncio
# event loop's natural serialisation. Two near-simultaneous tool calls
# may both try to construct the service; the second's `for_project()`
# is a few-ms re-probe of already-running backends, and the cache write
# is idempotent. No lock needed.
#
# M-1 coupling: the cache globals ``_cached_embed_service`` /
# ``_embed_service_construction_failed_at`` / ``_EMBED_SERVICE_RETRY_WINDOW``
# live on ``server`` (tests poke ``server._cached_embed_service``), so this
# accessor reads/writes them via the ``server`` module object.


def _get_embedding_service():
    """Return a cached EmbeddingService instance for this MCP session.

    Returns None when:
      * vco_lib isn't importable (HAS_EMBEDDING_SERVICE=False), OR
      * Construction raised NoEmbeddingBackendError recently (within the
        10-second retry window — avoids hammering already-down services).

    On None, the legacy inline helpers below fall through to their
    original HTTP-call bodies, which preserves backward compatibility
    for half-migrated installs and lets each helper produce its own
    "service unreachable" error rather than masking it with a generic
    one.
    """
    from . import server
    if not server.HAS_EMBEDDING_SERVICE:
        return None
    if server._cached_embed_service is not None:
        return server._cached_embed_service

    import time as _time
    if (_time.monotonic() - server._embed_service_construction_failed_at) < server._EMBED_SERVICE_RETRY_WINDOW:
        return None  # in the retry window — don't probe again yet

    try:
        server._cached_embed_service = server.EmbeddingService.for_project()
        return server._cached_embed_service
    except server.NoEmbeddingBackendError as e:
        server.logger.warning(
            "EmbeddingService construction failed (NoEmbeddingBackendError): %s. "
            "Falling back to legacy inline helpers for the next %.0fs.",
            e,
            server._EMBED_SERVICE_RETRY_WINDOW,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        return None
    except Exception as e:
        server.logger.warning(
            "EmbeddingService construction failed (%s) — falling back to "
            "legacy inline helpers.",
            e,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        return None


async def get_ollama_embedding(text: str) -> list[float] | None:
    """Get embedding from Ollama using the active text model.

    v0.2.18: prefers EmbeddingService (which picks ollama or openai
    based on env). Falls through to the inline Ollama call when the
    service isn't available — preserves the pre-v0.2.18 contract that
    THIS helper specifically hits Ollama (used by `backfill_embeddings`
    when the user explicitly asked for `provider="qwen3"` or
    `"legacy_ollama"`, where falling through to OpenAI would be wrong).
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None and "openai" not in svc.text_vector_slot:
        # Active text backend IS Ollama — safe to dispatch through the
        # service. Run sync embed in a thread so we don't block the
        # event loop.
        try:
            return await asyncio.to_thread(svc.embed_text, text)
        except Exception as e:
            server.logger.warning("EmbeddingService.embed_text failed (%s); falling back to inline Ollama", e)
    # Inline fallback: direct Ollama call (preserves pre-v0.2.18 path).
    # num_ctx=8192 overrides Ollama's 4096 default, matching qwen3-
    # embedding's actual capacity.
    # v0.2.77 Part 9 task 6: keep_alive pins the model resident so the hook
    # path's inline embed doesn't re-pay the ~1.9 s model reload after any idle
    # gap. Reuse the ONE resolver in vco_lib.embedding_providers.ollama (no
    # inline copy). Import is soft — a broken vco_lib is loud elsewhere; here we
    # degrade to "no keep_alive" rather than break the embed.
    try:
        from vco_lib.embedding_providers.ollama import _with_keep_alive as _ka
    except Exception:
        def _ka(body):  # type: ignore[misc]
            return body
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{server.OLLAMA_URL}/api/embeddings",
            json=_ka({
                "model": server.EMBEDDING_MODEL,
                "prompt": text,
                "options": {"num_ctx": 8192},
            }),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status != 200:
                text_body = await response.text()
                raise Exception(f"Failed to get Ollama embedding: {text_body}")
            data = await response.json()
            return data["embedding"]


async def get_legacy_text_embedding(text: str) -> list[float] | None:
    """Get embedding from legacy text model (snowflake-arctic-embed2, 1024-dim).

    Used to populate the old 'ollama_embed' named vector for backward compatibility.
    """
    from . import server
    try:
        from vco_lib.embedding_providers.ollama import _with_keep_alive as _ka
    except Exception:
        def _ka(body):  # type: ignore[misc]
            return body
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.OLLAMA_URL}/api/embeddings",
                # task 6: keep_alive on the legacy text embed too.
                json=_ka({"model": server.LEGACY_TEXT_EMBEDDING_MODEL, "prompt": text}),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    server.logger.warning("Legacy text embedding failed: HTTP %s", response.status)
                    return None
                data = await response.json()
                return data["embedding"]
    except Exception as e:
        server.logger.warning("Legacy text embedding error: %s", e)
        return None


async def get_openai_embedding(text: str) -> list[float] | None:
    """Get embedding from OpenAI API.

    v0.2.18: prefers EmbeddingService when the active text OR code slot
    points at OpenAI. Falls through to inline OpenAI HTTP call so
    `backfill_embeddings(provider="openai")` still works on projects
    whose ACTIVE_EMBEDDING is not openai.

    Returns None if no key configured or all paths fail.
    """
    from . import server
    if not server.OPENAI_API_KEY:
        return None
    svc = server._get_embedding_service()
    if svc is not None and (
        "openai" in svc.text_vector_slot or "openai" in svc.code_vector_slot
    ):
        try:
            # OpenAI slot resolution — text first, then code. Both paths
            # use the same OpenAI adapter inside the service, so the
            # choice only affects which model id we pass.
            if "openai" in svc.text_vector_slot:
                return await asyncio.to_thread(svc.embed_text, text)
            return await asyncio.to_thread(svc.embed_code, text)
        except Exception as e:
            server.logger.warning(
                "EmbeddingService OpenAI dispatch failed (%s); falling back to inline OpenAI call",
                e,
            )

    # Inline fallback (pre-v0.2.18 path): direct OpenAI HTTP call.
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {server.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": server.OPENAI_EMBEDDING_MODEL, "input": text},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    server.logger.warning("OpenAI embedding failed: %s", await response.text())
                    return None
                data = await response.json()
                return data["data"][0]["embedding"]
    except Exception as e:
        server.logger.warning("OpenAI embedding error: %s", e)
        return None


async def get_embedding(text: str) -> list[float] | None:
    """Get embedding using the active provider.

    Returns:
        - list[float]: Embedding vector
        - None: If using Weaviate's internal vectorizer
    """
    from . import server
    if server.EMBEDDING_SOURCE == "weaviate":
        return None  # Let Weaviate's text2vec module handle it

    if server.ACTIVE_EMBEDDING == "openai":
        vec = await server.get_openai_embedding(text)
        if vec:
            return vec
        # Fall back to Ollama if OpenAI fails
        server.logger.warning("OpenAI embedding failed, falling back to Ollama")

    return await server.get_ollama_embedding(text)


async def _get_both_embeddings(text: str) -> tuple[list[float] | None, list[float] | None]:
    """Get both Ollama (qwen3) and OpenAI embeddings concurrently.

    Returns (ollama_vec, openai_vec). Either may be None on failure.
    Legacy compatibility: the 'ollama_vec' here is now from qwen3-embedding.
    """
    from . import server
    ollama_vec, openai_vec = await asyncio.gather(
        server.get_ollama_embedding(text),
        server.get_openai_embedding(text),
    )
    return ollama_vec, openai_vec


async def _get_all_kg_embeddings_tagged(
    text: str,
) -> "tuple[dict[str, list[float]], list[str]]":
    """Like ``_get_all_kg_embeddings`` but also returns the truncated-slot tag.

    Returns ``(slots, secondary_truncated_slots)`` where the second element is the
    sorted list of SECONDARY slot names whose vector was embedded from a bounded
    leading sub-window on THIS call (R3-2). ``store_knowledge_node`` persists that
    list as the ``secondary_truncated_slots`` chunk property so stored secondary
    (e.g. arctic) vectors can be partitioned truncated-vs-full from stored data.

    The tag is captured ATOMICALLY with the vectors via
    ``EmbeddingService.embed_text_all_configured_tagged`` (inside the same
    ``to_thread`` boundary), so a concurrent write can't reset the per-instance
    truncated record between the embed and the read. The legacy inline-gather
    fallback (EmbeddingService unavailable) has no per-model num_ctx bounding and
    therefore reports an empty truncated list — no secondary was sub-windowed on
    that path.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None:
        try:
            slots, truncated = await asyncio.to_thread(
                svc.embed_text_all_configured_tagged, text
            )
            if slots:
                return slots, list(truncated)
            server.logger.warning(
                "_get_all_kg_embeddings_tagged: EmbeddingService returned no "
                "slots; falling back to inline gather"
            )
        except Exception as e:
            server.logger.warning(
                "_get_all_kg_embeddings_tagged via EmbeddingService failed (%s); "
                "falling back to inline gather", e
            )
    # Inline fallback path bounds nothing → no secondary was truncated.
    return (await _get_all_kg_embeddings(text)), []


async def _get_all_kg_embeddings(text: str) -> dict[str, list[float]]:
    """Get all KG embedding variants — every reachable text backend.

    v0.2.18: routes through EmbeddingService.embed_text_all_configured()
    which fans out to qwen3 + openai + legacy-arctic (any reachable
    backend) and returns a ``{slot_name: vector}`` dict. The legacy
    inline path (3-way asyncio.gather) is preserved as fallback for
    half-migrated installs.

    Used by `store_knowledge_node` to populate every configured slot on
    every write — so search-after-model-switch keeps working. For the
    truncated-tag-carrying variant the write path uses, see
    ``_get_all_kg_embeddings_tagged`` (R3-2).
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None:
        try:
            slots = await asyncio.to_thread(svc.embed_text_all_configured, text)
            if slots:
                return slots
            # Empty dict → every backend failed. Fall through to the
            # legacy path so the inline `gather` can surface its own
            # per-backend errors via the caller's logger.
            server.logger.warning(
                "_get_all_kg_embeddings: EmbeddingService returned no slots; "
                "falling back to inline gather"
            )
        except Exception as e:
            server.logger.warning(
                "_get_all_kg_embeddings via EmbeddingService failed (%s); "
                "falling back to inline gather", e
            )

    # Inline fallback (pre-v0.2.18 path): direct gather. Reached ONLY when the
    # EmbeddingService is unavailable (rare on current installs).
    #
    # KG-5 (v0.2.75): gate the SECONDARY slots (legacy ollama + openai) on
    # DUAL_EMBEDDING_WRITE_ALL_SLOTS, exactly like the primary path
    # (embedding_service.py::embed_text_all_configured, which returns only the
    # active slot when the toggle is OFF). Pre-KG-5 this degraded fallback
    # ALWAYS populated every reachable slot, so a project that deliberately
    # left dual-write OFF still got the secondary slots written whenever the
    # service happened to be down — an inconsistency with the primary path.
    # The active slot (qwen3_embed here) is ALWAYS written; only the secondary
    # fan-out is gated.
    from vco_lib.embedding_service import _resolve_write_all_slots
    _write_all = _resolve_write_all_slots()

    if _write_all:
        qwen3_vec, legacy_vec, openai_vec = await asyncio.gather(
            server.get_ollama_embedding(text),         # qwen3-embedding (new primary)
            server.get_legacy_text_embedding(text),     # snowflake-arctic-embed2 (legacy)
            server.get_openai_embedding(text),
        )
    else:
        # Toggle OFF: only the active slot. Mirrors the primary path's
        # single-entry return.
        qwen3_vec = await server.get_ollama_embedding(text)
        legacy_vec = None
        openai_vec = None
    vectors: dict[str, list[float]] = {}
    if qwen3_vec:
        vectors["qwen3_embed"] = qwen3_vec
    if legacy_vec:
        vectors["ollama_embed"] = legacy_vec
    if openai_vec:
        vectors["openai_embed"] = openai_vec
    return vectors


async def _get_all_code_embeddings(text: str) -> dict[str, list[float]]:
    """Get all code embedding variants — every reachable code backend.

    v0.2.18: routes through EmbeddingService.embed_code_all_configured()
    which fans out to codesage + openai + legacy-jina (any reachable
    backend). Legacy inline gather preserved as fallback.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None:
        try:
            slots = await asyncio.to_thread(svc.embed_code_all_configured, text)
            if slots:
                return slots
            server.logger.warning(
                "_get_all_code_embeddings: EmbeddingService returned no slots; "
                "falling back to inline gather"
            )
        except Exception as e:
            server.logger.warning(
                "_get_all_code_embeddings via EmbeddingService failed (%s); "
                "falling back to inline gather", e
            )

    # Inline fallback (pre-v0.2.18 path).
    codesage_vec, legacy_vec, openai_vec = await asyncio.gather(
        server.get_code_embedding(text),            # CodeSage-Large-v2 (new primary)
        server.get_legacy_code_embedding(text),     # jina-v2-base-code (legacy)
        server.get_openai_embedding(text),
    )
    vectors: dict[str, list[float]] = {}
    if codesage_vec:
        vectors["codesage_embed"] = codesage_vec
    if legacy_vec:
        vectors["ollama_code_embed"] = legacy_vec
    if openai_vec:
        vectors["openai_embed"] = openai_vec
    return vectors


def _scheme_for_collection(collection_name: str) -> str:
    """Return the vector scheme key ('kg' or 'code') for a collection.

    Strips any project prefix (e.g. 'MyProject_CodeFunction' -> 'CodeFunction')
    before checking CODE_SCHEME_COLLECTIONS.
    """
    from . import server
    # Strip project prefix: everything after last '_' that matches a known base name
    base = collection_name
    if "_" in collection_name:
        suffix = collection_name.rsplit("_", 1)[-1]
        # Check if suffix matches a code collection base name
        for code_coll in server.CODE_SCHEME_COLLECTIONS:
            if collection_name.endswith(code_coll):
                return "code"
    if base in server.CODE_SCHEME_COLLECTIONS:
        return "code"
    return "kg"


def _primary_named_vector(scheme: str) -> str:
    """Return the primary (first) named vector name for a scheme."""
    from . import server
    return next(iter(server.VECTOR_SCHEMES[scheme]))


async def _get_search_vector(text: str, scheme: str = "kg") -> tuple[list[float] | None, str]:
    """Get embedding for search, returns (vector, target_vector_name).

    v0.2.18: routes through EmbeddingService which resolves BOTH the
    embedding backend AND the named-vector slot from env in one place.
    Pre-v0.2.18 this branched on ACTIVE_EMBEDDING here, which duplicated
    the slot-resolution logic already living in the Wave-A
    EmbeddingService TEXT_SLOT_MAP / CODE_SLOT_MAP.

    Falls through to the legacy ACTIVE_EMBEDDING-branching path when
    the service is unavailable.

    Args:
        text: Text to embed.
        scheme: 'kg' or 'code' — determines text vs code backend.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None:
        try:
            if scheme == "code":
                vec = await asyncio.to_thread(svc.embed_code, text)
                target = svc.code_vector_slot
            else:
                vec = await asyncio.to_thread(svc.embed_text, text)
                target = svc.text_vector_slot
            return vec, target
        except Exception as e:
            server.logger.warning(
                "_get_search_vector via EmbeddingService failed (%s); "
                "falling back to legacy ACTIVE_EMBEDDING branching", e
            )

    # Legacy fallback path (pre-v0.2.18): branches on ACTIVE_EMBEDDING.
    if server.ACTIVE_EMBEDDING == "openai" and server.OPENAI_API_KEY:
        vec = await server.get_openai_embedding(text)
        if vec:
            return vec, "openai_embed"
        # Audit fix (2026-04-30): on openai failure, do NOT silently fall
        # through to qwen3/arctic below — that mixes embedding spaces and
        # surfaces poor results without any signal to the caller. Log the
        # failure and return None; the caller will see a clear error.
        server.logger.warning(
            "_get_search_vector: ACTIVE_EMBEDDING=openai but OpenAI call "
            "failed; refusing to fall back to legacy embedder (would "
            "produce results from a different vector space). Caller will "
            "receive None."
        )
        return None, ""

    if scheme == "code":
        if server.ACTIVE_EMBEDDING in ("codesage", "qwen3"):
            # Use new CodeSage model (default for code)
            vec = await server.get_code_embedding(text)
            target = "codesage_embed"
        else:
            # Legacy: Jina via Ollama
            vec = await server.get_legacy_code_embedding(text)
            target = "ollama_code_embed"
    else:
        if server.ACTIVE_EMBEDDING in ("qwen3", "codesage"):
            # Use new Qwen3-Embedding (default for KG)
            vec = await server.get_ollama_embedding(text)
            target = "qwen3_embed"
        else:
            # Legacy: Arctic via Ollama
            vec = await server.get_legacy_text_embedding(text)
            target = "ollama_embed"
    return vec, target


async def count_tokens_async(text: str) -> int:
    """
    Count tokens using Ollama qwen3.5:0.8b tokenizer.
    Falls back to character approximation (len // 4) if Ollama is unavailable.
    """
    from . import server
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.OLLAMA_URL}/api/tokenize",
                json={"model": "qwen3.5:0.8b", "content": text},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return len(data.get("tokens", []))
    except Exception:
        pass
    # Fallback: 1 token ≈ 4 chars
    return len(text) // 4


async def get_code_embedding(text: str) -> list[float] | None:
    """Get a CodeSage-space code embedding.

    This helper is the CODESAGE-SPECIFIC provider used by the
    ``backfill_embeddings(provider="codesage")`` caller and the
    ``_get_all_code_embeddings`` inline-fallback gather. It prefers
    EmbeddingService ONLY when the active code slot is `codesage_embed`
    (same backend the inline call targets); otherwise it deliberately
    falls through to the inline CodeEmbed HTTP call so a project with
    active=openai-code can still request a codesage vector for backfill.

    ⚠️  Do NOT use this to embed a SEARCH QUERY. For query embedding use
    ``get_code_query_embedding`` (v0.2.73 C-5) which routes through
    ``svc.embed_code`` for ALL slots — mirroring the CLI
    (``query_code_graph.py::generate_code_embedding``). Using this
    codesage-biased helper for queries broke the CLI≡MCP invariant on
    every non-CodeSage slot (qwen3 / jina): the CLI embedded via the
    resolved slot model, the MCP embedded via whatever :11440 served,
    so the two surfaces produced different query vectors and different
    results.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None and svc.code_vector_slot == "codesage_embed":
        try:
            return await asyncio.to_thread(svc.embed_code, text)
        except Exception as e:
            server.logger.warning(
                "EmbeddingService.embed_code failed (%s); falling back to inline CodeEmbed call",
                e,
            )
    return await server._inline_code_embed_http(text)


async def _inline_code_embed_http(text: str) -> list[float] | None:
    """Direct CodeEmbed HTTP call (pre-v0.2.18 fallback path).

    Shared by ``get_code_embedding`` (codesage backfill) and
    ``get_code_query_embedding`` (svc-None fallback) so the raw-HTTP
    fallback has ONE home.
    """
    from . import server
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.CODE_EMBED_SERVICE_URL}/api/embeddings",
                json={"model": "", "prompt": text},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["embedding"]
                else:
                    server.logger.error("Code embedding service failed: HTTP %s", response.status)
                    return None
    except Exception as e:
        server.logger.error("Code embedding service error: %s", e)
        return None


async def get_code_query_embedding(text: str) -> list[float] | None:
    """Embed a SEARCH QUERY into the active code-vector space.

    v0.2.73 C-5: the CLI≡MCP invariant requires the MCP to embed queries
    exactly as the CLI does. The CLI's
    ``query_code_graph.py::generate_code_embedding`` routes ALL slots
    through ``svc.embed_code`` (which resolves the slot's backend —
    CodeSage :11440, qwen3 → Ollama :11435, jina → Ollama, openai) and
    only falls back to raw CodeEmbed HTTP when EmbeddingService is
    unavailable. This function MIRRORS that contract so
    ``search_code_graph`` produces the same query vector the CLI would.

    MUST MATCH ``templates/scripts/query_code_graph.py::generate_code_embedding``.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is not None:
        try:
            return await asyncio.to_thread(svc.embed_code, text)
        except Exception as e:
            server.logger.warning(
                "EmbeddingService.embed_code failed (%s); falling back to inline CodeEmbed call",
                e,
            )
    # Legacy fallback (svc unavailable): direct CodeEmbed HTTP call.
    return await server._inline_code_embed_http(text)


def _active_code_query_slot() -> str:
    """Return the active code-vector slot for a query's ``target_vector``.

    v0.2.73 C-6: mirrors the CLI's
    ``query_code_graph.py::_active_code_vector_slot`` EXACTLY — svc present
    → its resolved ``code_vector_slot``; svc None → ``"codesage_embed"``
    (the CLI's unconditional pre-v0.2.18 fallback). Kept as a named helper
    so both search + any future code-query call site cannot re-fork the
    fallback.

    MUST MATCH ``templates/scripts/query_code_graph.py::_active_code_vector_slot``.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is None:
        return "codesage_embed"
    return svc.code_vector_slot


async def get_legacy_code_embedding(text: str) -> list[float] | None:
    """Get code embedding from legacy Jina model via Ollama (768-dim).

    Used to populate the old 'ollama_code_embed' named vector for backward compatibility.
    """
    from . import server
    try:
        from vco_lib.embedding_providers.ollama import _with_keep_alive as _ka
    except Exception:
        def _ka(body):  # type: ignore[misc]
            return body
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.OLLAMA_URL}/api/embeddings",
                # task 6: keep_alive on the legacy code embed too.
                json=_ka({
                    "model": "unclemusclez/jina-embeddings-v2-base-code:latest",
                    "prompt": text
                }),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["embedding"]
                else:
                    server.logger.warning("Legacy code embedding failed: HTTP %s", response.status)
                    return None
    except Exception as e:
        server.logger.warning("Legacy code embedding error: %s", e)
        return None
