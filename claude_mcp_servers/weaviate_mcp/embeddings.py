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
import time

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
# v0.2.98: VCO's OWN slot only — the shared `openai_api_key` through the
# canonical chain (launcher keychain → shared file store). The env var and any
# per-project binding are NOT sources: a project's key is the project's.
from vco_lib.openai_key import resolve_openai_api_key  # noqa: E402

OPENAI_API_KEY = resolve_openai_api_key()
# Code embedding service URL (CodeSage-Large-v2 via FastAPI, or an
# Ollama-compatible endpoint) — through the ONE client resolver
# (``vco_lib.code_embed_image.service_base_url``, v0.2.97 lane Y): explicit →
# ``CODE_EMBED_SERVICE_URL`` → ``CODE_EMBED_URL`` (the client alias) →
# ``http://localhost:<CODE_EMBED_PORT|11440>``. This was a fourth inline copy
# that ignored both the alias and ``CODE_EMBED_PORT``, so a service moved with
# either knob was probed at the compiled default.
from vco_lib.code_embed_image import service_base_url  # noqa: E402

CODE_EMBED_SERVICE_URL = service_base_url()


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


#: How often a cached EmbeddingService re-reads the code graph's recorded model.
_RECORD_RECHECK_SECS = 30.0


def _code_record_changed(svc, prefix: str) -> bool:
    """True when the launcher DB now records a different code model for
    ``prefix`` than the one ``svc`` resolved at construction. Soft-fails to
    False (a DB hiccup must not churn the service)."""
    try:
        from vco_lib.code_embed_model import CodeModelResolution, read_code_model_record

        res = getattr(svc, "code_model_resolution", None)
        if not prefix or not isinstance(res, CodeModelResolution):
            return False
        rec = read_code_model_record(prefix)
        before = res.recorded.model if res.recorded else None
        return before != (rec.model if rec else None)
    except Exception:  # noqa: BLE001
        return False


def _get_embedding_service():
    """Return a cached EmbeddingService instance for this MCP session.

    Returns None when:
      * vco_lib isn't importable (HAS_EMBEDDING_SERVICE=False), OR
      * Construction raised NoEmbeddingBackendError recently (within the
        10-second retry window — avoids hammering already-down services).

    On None, the WRITE/backfill helpers below fall through to their inline
    HTTP bodies, each of which embeds with its OWN named model into that
    model's slot (a transport fallback, never a model substitution). The
    SEARCH paths never do: they raise the loud
    ``vco_lib.embed_outage.EmbeddingBackendUnavailable`` (owner ruling
    2026-10-10 — a query embedded by another model searches noise). The
    construction error is kept on ``server._embed_service_construction_error``
    so that loud text can quote it.
    """
    from . import server
    if not server.HAS_EMBEDDING_SERVICE:
        return None
    import time as _time

    if server._cached_embed_service is not None:
        # v0.2.102 W6-B2: a long-lived MCP must not keep querying the PRE-flip
        # space after a fill completes (or a re-walk records another model) —
        # re-read the cheap DB record at most every _RECORD_RECHECK_SECS and
        # rebuild the service when it changed.
        now = _time.monotonic()
        if now - getattr(server, "_embed_record_checked_at", 0.0) < _RECORD_RECHECK_SECS:
            return server._cached_embed_service
        server._embed_record_checked_at = now
        if not _code_record_changed(server._cached_embed_service,
                                    getattr(server, "CODE_GRAPH_PROJECT", None) or ""):
            return server._cached_embed_service
        server.logger.info("weaviate-kg: the code graph's recorded model changed — "
                           "rebuilding the embedding service")
        server._cached_embed_service = None
    if (_time.monotonic() - server._embed_service_construction_failed_at) < server._EMBED_SERVICE_RETRY_WINDOW:
        return None  # in the retry window — don't probe again yet

    try:
        # v0.2.102 W6-B2: the code leg embeds in the space THIS project's
        # code graph is recorded as indexed in (vco_lib.code_embed_model).
        server._cached_embed_service = server.EmbeddingService.for_project(
            codegraph_prefix=getattr(server, "CODE_GRAPH_PROJECT", None) or None)
        return server._cached_embed_service
    except server.NoEmbeddingBackendError as e:
        server.logger.warning(
            "EmbeddingService construction failed (NoEmbeddingBackendError): %s. "
            "Falling back to legacy inline helpers for the next %.0fs.",
            e,
            server._EMBED_SERVICE_RETRY_WINDOW,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        server._embed_service_construction_error = str(e)
        return None
    except Exception as e:
        server.logger.warning(
            "EmbeddingService construction failed (%s) — falling back to "
            "legacy inline helpers.",
            e,
        )
        server._embed_service_construction_failed_at = _time.monotonic()
        server._embed_service_construction_error = str(e)
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
    # v0.2.102 W4-E: the body comes from the ONE shared builder
    # (vco_lib.embedding_providers.ollama.embed_request_body) so this leg
    # sends the IDENTICAL load options per model as the canonical adapter —
    # one runner shared across projects, never a second copy. Import is
    # soft — a broken vco_lib is loud elsewhere; here we degrade to the
    # pre-R40 conservative shape rather than break the embed.
    try:
        from vco_lib.embedding_providers.ollama import embed_request_body
    except Exception:
        def embed_request_body(model: str, *, prompt=None, input=None, num_ctx=None):  # type: ignore[misc,assignment]
            # Degraded standalone shape (vco_lib missing = broken install):
            # explicit 2048 floor (stated choice, not an omission — an UNSET
            # num_ctx means Ollama's 2048 default and silent tail loss).
            return {
                "model": model,
                "prompt": prompt,
                "options": {"num_ctx": num_ctx if num_ctx is not None else 2048},
                "truncate": False,
            }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{server.OLLAMA_URL}/api/embeddings",
            json=embed_request_body(server.EMBEDDING_MODEL, prompt=text),
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
    # v0.2.102 W4-E: ONE shared body builder — identical load options per
    # model as the canonical adapter (see get_ollama_embedding).
    try:
        from vco_lib.embedding_providers.ollama import embed_request_body
    except Exception:
        def embed_request_body(model: str, *, prompt=None, input=None, num_ctx=None):  # type: ignore[misc,assignment]
            return {
                "model": model,
                "prompt": prompt,
                "options": {"num_ctx": num_ctx if num_ctx is not None else 2048},
                "truncate": False,
            }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.OLLAMA_URL}/api/embeddings",
                json=embed_request_body(server.LEGACY_TEXT_EMBEDDING_MODEL, prompt=text),
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
        # Owner ruling 2026-10-10: never another embedding model — an OpenAI
        # failure is NOT answered with an Ollama vector (a different space
        # stored under the OpenAI slot). None = the caller's loud degrade.
        vec = await server.get_openai_embedding(text)
        if not vec:
            server.logger.error(
                "OpenAI embedding failed; no other embedding model is used "
                "(the vectors would not be comparable)")
        return vec or None

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


def _active_text_slot_name() -> str:
    """The ACTIVE text slot name of the SESSION-cached EmbeddingService.

    PURE read of ``server._cached_embed_service`` — never CONSTRUCTS a service
    (unlike ``_get_embedding_service``), so the KG write path can call it per
    chunk with no side effects, no probe, and no retry-window state changes.
    Returns ``""`` when the cache is cold or the slot is unavailable.

    Used by ``store_knowledge_node`` to derive the SECONDARY-only view of the
    per-call truncation record (the legacy ``secondary_truncated_slots``
    property keeps its pre-v0.2.92 meaning); the complete record is persisted
    as ``truncated_slots`` (see ``rl_enrichment.TRUNCATED_SLOTS_PROP``). A
    cold cache here means the capture itself could not have run through the
    service (the inline fallback ran, whose record is empty), so the
    active-slot exclusion is a no-op and the empty record stays honest.

    v0.2.94: the read itself lives in ``vco_lib.kg_vector_slot`` — the ONE
    home shared with kg-sync's writer and the KG readers. ``slot_for_service``
    keeps this function's two load-bearing properties exactly: it never
    constructs a service, and it never raises.
    """
    from vco_lib.kg_vector_slot import slot_for_service

    from . import server
    return slot_for_service(getattr(server, "_cached_embed_service", None))


async def _get_all_kg_embeddings_tagged(
    text: str,
) -> "tuple[dict[str, list[float]], list[str]]":
    """Like ``_get_all_kg_embeddings`` but also returns the truncated-slot tag.

    Returns ``(slots, truncated_slot_names)`` where the second element is the
    sorted list of EVERY configured slot — the ACTIVE one included — whose
    vector was embedded from a bounded leading sub-window on THIS call (R3-2;
    widened v0.2.92, m-R6-1, from secondary-only to the complete record).
    ``store_knowledge_node`` persists that list verbatim as the
    ``truncated_slots`` chunk property and derives the secondary-only
    ``secondary_truncated_slots`` property from it (active slot dropped), so
    stored vectors can be partitioned truncated-vs-full from stored data
    alone — the ACTIVE slot included, which is the point of m-R6-1.

    The tag is captured ATOMICALLY with the vectors via
    ``EmbeddingService.embed_text_all_configured_tagged`` (inside the same
    ``to_thread`` boundary), so a concurrent write can't reset the per-instance
    truncated record between the embed and the read. The legacy inline-gather
    fallback (EmbeddingService unavailable) has no per-model num_ctx bounding
    and therefore reports an empty record — nothing was sub-windowed on that
    path.
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
    # Inline fallback path bounds nothing → nothing was sub-windowed.
    return (await _get_all_kg_embeddings(text)), []


async def complete_kg_write_vectors(
    vectors: "dict[str, list[float]]", truncated: "list[str]", text: str, collection: str,
) -> "dict[str, list[float]]":
    """v0.2.102 W6-I — finish a KG write's vectors (``store_knowledge_node``):
    through ``vco_lib.kg_embed_model.complete_write_vectors`` whenever a
    service is in hand. The ACTIVE (target) slot is REQUIRED — a row holding
    only another model's vector is never stored (``KgWriteVectorMissing``
    propagates, loud; W6-H's "fallback-only write") — and while ``collection``
    fills toward a newly selected model the RECORDED model's vector rides
    along, so the rewritten node stays in the slot searches read.
    ``truncated`` (the call's truncation record) gains the carry slot when
    its vector came from a bounded window. No service → returned as-is (the
    inline transport fallback names no target)."""
    from vco_lib.kg_embed_model import complete_write_vectors

    from . import server
    svc = server._get_embedding_service()
    if svc is None:
        return vectors
    return await asyncio.to_thread(
        complete_write_vectors, svc, dict(vectors or {}), text, collection, truncated)


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
    # The active slot is ALWAYS written; only the secondary fan-out is gated.
    #
    # W6-J (review F2): the active vector is labelled with the slot of the
    # model that PRODUCED it. `get_ollama_embedding` embeds
    # `server.EMBEDDING_MODEL` — qwen3 only on a qwen3 profile — so the
    # hardcoded "qwen3_embed" label this leg used to write stored another
    # model's vector under the qwen3 slot on a non-qwen3 profile. A failure
    # of that call raises (it answers a vector or nothing), so a row whose
    # active slot never came back is never stored here either. The label
    # comes from the ONE text slot map (`kg_embed_model.text_model_slot`,
    # the same resolver the record/fill machinery uses), and a secondary
    # vector never overwrites the active slot (a profile whose model maps to
    # the legacy `ollama_embed` slot — mxbai, nomic — would otherwise have
    # its own vector replaced by the legacy arctic one).
    from vco_lib.embedding_service import _resolve_write_all_slots
    from vco_lib.kg_embed_model import text_model_slot
    _write_all = _resolve_write_all_slots()

    if _write_all:
        active_vec, legacy_vec, openai_vec = await asyncio.gather(
            server.get_ollama_embedding(text),         # the ACTIVE text model
            server.get_legacy_text_embedding(text),     # snowflake-arctic-embed2 (legacy)
            server.get_openai_embedding(text),
        )
    else:
        # Toggle OFF: only the active slot. Mirrors the primary path's
        # single-entry return.
        active_vec = await server.get_ollama_embedding(text)
        legacy_vec = None
        openai_vec = None
    vectors: dict[str, list[float]] = {}
    if active_vec:
        vectors[text_model_slot(server.EMBEDDING_MODEL)[0]] = active_vec
    if legacy_vec:
        vectors.setdefault("ollama_embed", legacy_vec)
    if openai_vec:
        vectors.setdefault("openai_embed", openai_vec)
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

    Tolerates a project prefix (e.g. 'MyProject_CodeFunction') by matching the
    CODE_SCHEME_COLLECTIONS base names as a SUFFIX.
    """
    from . import server
    base = collection_name
    if "_" in collection_name:
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


async def _get_search_vector(
    text: str, scheme: str = "kg", task: str | None = None
) -> tuple[list[float] | None, str]:
    """Get embedding for search, returns (vector, target_vector_name).

    v0.2.18: routes through EmbeddingService which resolves BOTH the
    embedding backend AND the named-vector slot from env in one place.
    Pre-v0.2.18 this branched on ACTIVE_EMBEDDING here, which duplicated
    the slot-resolution logic already living in the Wave-A
    EmbeddingService TEXT_SLOT_MAP / CODE_SLOT_MAP.

    v0.2.101: this is a SEARCH path, so it embeds through the QUERY-side
    methods (``embed_text_query`` / ``embed_code_query``) which apply the
    active model's query-prefix — documents still go through ``embed_text`` /
    ``embed_code`` unprefixed. ``task`` selects the per-use instruction
    sentence (see ``chunking.QUERY_TASKS``); ``None`` uses the scheme default.

    No legacy fallback (owner ruling 2026-10-10): when the service is
    unavailable or its backend stays down past the transient retry, this
    raises ``vco_lib.embed_outage.EmbeddingBackendUnavailable`` carrying the
    loud text (backend, container, watchdog state, remedy) — the pre-v0.2.102
    ACTIVE_EMBEDDING branch embedded with whatever the env named (a code
    query with CodeSage on a jina-indexed graph) and searched noise.

    Args:
        text: Text to embed.
        scheme: 'kg' or 'code' — determines text vs code backend.
        task: QUERY_TASKS key (KG search / hook injection / code-nl / code-sim).
    """
    from vco_lib.embed_outage import embed_query_or_raise

    from . import server
    svc = server._get_embedding_service()
    vec = await asyncio.to_thread(
        embed_query_or_raise, scheme, svc, text, task,
        construction_error=getattr(server, "_embed_service_construction_error", ""),
    )
    assert svc is not None  # embed_query_or_raise raised for a missing service
    return vec, (svc.code_vector_slot if scheme == "code" else svc.text_vector_slot)


async def _kg_search_vector(
    text: str, collection: str, task: str | None = None, *, steady=None,
) -> tuple[list[float] | None, str]:
    """The query vector + slot for searching ONE KG collection (v0.2.102 W6-I).

    The collection is searched with the model it is RECORDED as complete in
    (``vco_lib.kg_embed_model``). In the steady state that is the service's
    own model and this is exactly ``steady`` — the CALLER's own
    :func:`_get_search_vector` binding (so a seam patched on the caller's
    module is honoured; default: ``server``'s). While a newly selected model's slot
    fills, the query is embedded with the recorded model instead — through
    the same loud query-embed entry, never another model."""
    from vco_lib.kg_embed_model import recorded_query_model, sibling_text_service

    from . import server
    model = await asyncio.to_thread(recorded_query_model, collection) if collection else ""
    if model:
        from vco_lib.embed_outage import embed_query_or_raise

        svc = server._get_embedding_service()
        qs = sibling_text_service(svc, model) if svc is not None else None
        vec = await asyncio.to_thread(
            embed_query_or_raise, "kg", qs, text, task,
            construction_error=getattr(server, "_embed_service_construction_error", ""),
        )
        assert qs is not None  # embed_query_or_raise raised for a missing service
        return vec, qs.text_vector_slot
    fn = steady or server._get_search_vector
    if task is None:
        return await fn(text)
    return await fn(text, task=task)


async def count_tokens_async(text: str) -> int:
    """
    Count tokens in the chunker's ONE budget unit (D16, v0.2.92).

    Delegates to ``chunking.TokenCounter.count_tokens`` — deterministic
    character arithmetic. Pre-v0.2.92 this called Ollama's ``/api/tokenize``
    with a CHAT model (``qwen3.5:0.8b``): wrong unit for an embedding-window
    budget, and the endpoint no longer exists in current Ollama (404 on
    0.20.2), so the call always fell through to the approximation anyway.
    One home: this function and ``TokenCounter`` cannot disagree.
    """
    from .chunking import TokenCounter
    return TokenCounter.count_tokens(text)


async def get_code_embedding(
    text: str, tolerance_secs: float | None = None
) -> list[float] | None:
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
    ``svc.embed_code_query`` for ALL slots — mirroring the CLI
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
    # v0.2.102 W6-B2 (owner ruling 2026-10-10): this vector lands in the
    # CodeSage slot, so the container must SERVE CodeSage — on a 6-12 GB / AMD
    # host its ``backend=ollama`` proxies another model, whose vector would be
    # stored as CodeSage's. An unreported name (down / restarting) is not
    # judged here: the inline leg's transient retry owns that.
    served = await asyncio.to_thread(_container_model_name, server.CODE_EMBED_SERVICE_URL)
    if served and "codesage" not in served.lower():
        server.logger.error(
            "get_code_embedding: the code_embed container at %s serves %r, not "
            "CodeSage — no CodeSage vector is produced (never another model's)",
            server.CODE_EMBED_SERVICE_URL, served,
        )
        return None
    return await server._inline_code_embed_http(text, tolerance_secs=tolerance_secs)


def _container_model_name(url: str) -> "str | None":
    """The model the code_embed container reports on ``/health`` (None when
    it does not answer). Soft-fails to None."""
    try:
        import requests

        from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter

        with requests.Session() as session:
            return CodeEmbedAdapter(url, session, timeout=5.0).model_name
    except Exception:  # noqa: BLE001
        return None


async def _inline_code_embed_http(
    text: str, tolerance_secs: float | None = None
) -> list[float] | None:
    """Direct CodeEmbed HTTP call (pre-v0.2.18 fallback path).

    Shared by ``get_code_embedding`` (codesage backfill) and
    ``get_code_query_embedding`` (svc-None / non-503 fallback) so the raw-HTTP
    fallback has ONE home.

    v0.2.102 r2: honours the service's 503 + Retry-After contract through the
    ONE shared retry helper (:mod:`vco_lib.retry_after` — the SAME module
    ``EmbeddingService._retry_once_on_503`` uses; no second copy of the
    policy). A 503 (the server's queue wait was exceeded, or its CUDA-OOM load
    budget is exhausted / cooling down) is retried until the tolerance elapses
    instead of surfacing "Failed to generate query embedding" on the first
    shot. A non-503 HTTP status and any connection error still return None
    immediately (the service is down or refuses the input — retrying changes
    nothing). The per-request timeout MUST exceed the server's queue wait
    (``CODE_EMBED_QUEUE_WAIT_SECS``, default 600 s) — one home:
    ``CODE_EMBED_CLIENT_TIMEOUT_SECS`` (660 s).

    v0.2.102 r3: ``tolerance_secs`` lets the caller pick WHICH tolerance —
    batch callers (``get_code_embedding``, the analyzer / backfill gather)
    pass nothing and get ``VCO_EMBED_503_TOLERANCE_SECS`` (default 420 s);
    the INTERACTIVE query path passes the interactive bound
    (``VCO_EMBED_503_INTERACTIVE_TOLERANCE_SECS``, default 90 s) so a
    persistently-503 backend fails the tool call fast instead of blocking
    the user's turn for minutes.
    """
    from . import server
    # vco_lib import is a hard requirement of the whole MCP process (the
    # loud-fail discipline — server.py itself imports vco_lib.embedding_service);
    # deliberately OUTSIDE the try below so a broken install is never masked as
    # a None embedding.
    from vco_lib.retry_after import (
        CODE_EMBED_CLIENT_TIMEOUT_SECS,
        DEFAULT_503_FALLBACK_BACKOFFS,
        compute_503_delay,
        is_connection_error,
        parse_retry_after,
        resolve_503_delay_scale,
        resolve_503_tolerance,
        resolve_refused_tolerance,
    )

    scale = resolve_503_delay_scale()
    legacy_bounded = scale == 0.0
    tolerance = (
        resolve_503_tolerance() if tolerance_secs is None else tolerance_secs
    )
    started = time.monotonic()
    deadline = started + tolerance
    # v0.2.102 W4-G (coordinator ruling 2026-10-09): the service being DOWN
    # (refused / no listener) is not the service being BUSY (503, queued).
    # An interactive caller — every caller that passes `tolerance_secs` (see
    # r3 above) — waits on a refusal only for the short bound from the ONE
    # policy home (`resolve_refused_tolerance`), then fails loudly; batch
    # callers keep the long tolerance for both kinds.
    refused_tolerance = min(
        tolerance, resolve_refused_tolerance(interactive=tolerance_secs is not None)
    )
    refused_deadline = started + refused_tolerance
    attempt = 0
    while True:
        attempt += 1
        retry_after: "float | None" = None
        refused = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{server.CODE_EMBED_SERVICE_URL}/api/embeddings",
                    json={"model": "", "prompt": text},
                    timeout=aiohttp.ClientTimeout(total=CODE_EMBED_CLIENT_TIMEOUT_SECS)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data["embedding"]
                    if response.status != 503:
                        server.logger.error(
                            "Code embedding service failed: HTTP %s", response.status
                        )
                        return None
                    retry_after = parse_retry_after(
                        response.headers.get("Retry-After")
                    )
                    server.logger.info(
                        "Code embedding service returned 503 (queued past the "
                        "wait bound / GPU busy?): attempt %d, Retry-After=%s",
                        attempt, retry_after,
                    )
        except Exception as e:
            # v0.2.102 W4-E item 6: a CONNECTION error (service restarting,
            # e.g. mid model-load) is transient-retryable on the same bounded
            # schedule as a 503 — never a silent first-shot drop. Any other
            # exception still fails immediately (the service refused the
            # input; retrying changes nothing).
            if not is_connection_error(e):
                server.logger.error("Code embedding service error: %s", e)
                return None
            server.logger.info(
                "Code embedding service connection error (restarting?): "
                "attempt %d — %s", attempt, e,
            )
            refused = True
        # 503 → bounded retry on the shared policy (same helper + knobs as
        # EmbeddingService._retry_once_on_503).
        if legacy_bounded:
            if attempt > len(DEFAULT_503_FALLBACK_BACKOFFS):
                server.logger.error(
                    "Code embedding service still 503 after %d attempts "
                    "(zero-delay mode); giving up",
                    attempt,
                )
                return None
            delay = 0.0
        else:
            if time.monotonic() >= deadline:
                server.logger.error(
                    "Code embedding service still 503 after %.0fs of retries; "
                    "giving up",
                    tolerance,
                )
                return None
            if refused and time.monotonic() >= refused_deadline:
                server.logger.error(
                    "Code embedding service unreachable (connection refused / "
                    "no listener) for %.0fs; giving up",
                    refused_tolerance,
                )
                return None
            delay = compute_503_delay(
                attempt, retry_after, DEFAULT_503_FALLBACK_BACKOFFS, scale
            )
            if refused:
                delay = min(delay, max(0.0, refused_deadline - time.monotonic()))
        if delay > 0:
            await asyncio.sleep(delay)


async def get_code_query_embedding(text: str, task: str | None = None) -> list[float] | None:
    """Embed a SEARCH QUERY into the active code-vector space.

    v0.2.73 C-5: the CLI≡MCP invariant requires the MCP to embed queries
    exactly as the CLI does. The CLI's
    ``query_code_graph.py::generate_code_embedding`` routes ALL slots
    through ``svc.embed_code_query`` (which resolves the slot's backend —
    CodeSage :11440, qwen3 → Ollama :11435, jina → Ollama, openai) and
    only falls back to raw CodeEmbed HTTP when EmbeddingService is
    unavailable. This function MIRRORS that contract so
    ``search_code_graph`` produces the same query vector the CLI would.

    v0.2.101: ``embed_code_query`` applies the active code model's query
    prefix (client-side on the OpenAI/Ollama legs; the SERVICE applies it for
    the CodeSage/jina legs — see ``EmbeddingService.embed_code_query``).
    Documents still embed through ``embed_code`` (unprefixed).

    MUST MATCH ``templates/scripts/query_code_graph.py::generate_code_embedding``.

    v0.2.102 W6-B2 (owner ruling 2026-10-10): ONE entry,
    ``vco_lib.embed_outage.embed_query_or_raise`` — the interactive 503
    bound, the restart wait, and NO inline CodeEmbed fallback (that endpoint
    serves CodeSage; a jina/qwen3/OpenAI-indexed graph queried with it
    searches noise). A backend that stays down raises
    ``EmbeddingBackendUnavailable`` — ``search_code_graph`` returns its loud
    text instead of "Failed to generate query embedding".
    """
    from vco_lib.embed_outage import embed_query_or_raise

    from . import server
    svc = server._get_embedding_service()
    return await asyncio.to_thread(
        embed_query_or_raise, "code", svc, text, task,
        construction_error=getattr(server, "_embed_service_construction_error", ""),
    )


def _active_code_query_slot() -> str:
    """Return the active code-vector slot for a query's ``target_vector``.

    v0.2.73 C-6: mirrors the CLI's
    ``query_code_graph.py::_active_code_vector_slot`` EXACTLY. svc None
    raises the loud ``EmbeddingBackendUnavailable`` (v0.2.102 W6-B2: the old
    ``"codesage_embed"`` default named a slot the graph may not be indexed in).

    MUST MATCH ``templates/scripts/query_code_graph.py::_active_code_vector_slot``.
    """
    from . import server
    svc = server._get_embedding_service()
    if svc is None:
        from vco_lib.embed_outage import query_unavailable

        raise query_unavailable(
            "code", None,
            construction_error=getattr(server, "_embed_service_construction_error", ""))
    return svc.code_vector_slot


async def get_legacy_code_embedding(text: str) -> list[float] | None:
    """Get code embedding from legacy Jina model via Ollama (768-dim).

    Used to populate the old 'ollama_code_embed' named vector for backward compatibility.
    """
    from . import server
    # v0.2.102 W4-E: ONE shared body builder — identical load options per
    # model as the canonical adapter (see get_ollama_embedding).
    try:
        from vco_lib.embedding_providers.ollama import embed_request_body
    except Exception:
        def embed_request_body(model: str, *, prompt=None, input=None, num_ctx=None):  # type: ignore[misc,assignment]
            return {
                "model": model,
                "prompt": prompt,
                "options": {"num_ctx": num_ctx if num_ctx is not None else 2048},
                "truncate": False,
            }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{server.OLLAMA_URL}/api/embeddings",
                json=embed_request_body(
                    "unclemusclez/jina-embeddings-v2-base-code:latest",
                    prompt=text,
                ),
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
