#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Knowledge Graph Weaviate Sync Script

Syncs knowledge graph markdown files to Weaviate collection.
Called by Claude hooks after file edits in knowledge/ directory.

Handles chunking for large files (>6k tokens) to stay within embedding model limits.

Usage:
    python .claude/scripts/sync_knowledge_graph.py <file_path>
    python .claude/scripts/sync_knowledge_graph.py --all  # Sync all knowledge files
"""

import sys

# v0.2.49 Bug L: reconfigure stdout/stderr to UTF-8 so emoji + non-ASCII
# error messages don't crash on Windows cp1252 consoles. Without this,
# `print(f"❌ ...")` raises UnicodeEncodeError on the default Windows
# Python console (which inherits the system codepage, often cp1252 on
# Western European installs). The launcher's `installer.rs` sets
# PYTHONIOENCODING=utf-8 + PYTHONUTF8=1 on every Python child it
# spawns (v0.2.27 fix), but kg-sync invokes this script directly from
# a Windows shell without those env vars. Reconfiguring here defends
# against the direct-CLI path. `errors='backslashreplace'` ensures we
# never crash even if the terminal can't render a character — it'll
# print the escape sequence instead.
#
# Python 3.7+ supports the `reconfigure` method on TextIOWrapper.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    # AttributeError: stdout/stderr not a TextIOWrapper (rare, e.g.
    # captured by pytest or redirected to a non-tty). OSError: stream
    # already detached/closed. Both are benign — fall through, and any
    # subsequent emoji print may still crash on Windows-cp1252-direct
    # invocations, but at least we tried.
    pass

import os
import re
import time
import yaml
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Mapping
import uuid

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Resolve vco_lib (lives next to claude_mcp_servers/ in the orchestrator clone).
# EmbeddingService is the v0.2.18 central dispatcher for embedding calls.
#
# weaviate_mcp is pip-installed as an editable package by install.py
# (A1, v0.2.38), so `from weaviate_mcp.chunking import Chunker` works
# without a sys.path entry.  We still need the vco_lib parent on sys.path
# because vco_lib is not yet a standalone package.
# Resolution order for vco_lib:
#   1. $VCT_ORCHESTRATOR_ROOT               (set by .claude/env)
#   2. <project_home> in-tree fallback      (orchestrator clone)
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_HOME = _SCRIPT_DIR.parent.parent  # .claude/scripts/X → .claude → project

_env_root = os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip()
if _env_root and Path(_env_root).is_dir():
    _VCO_LIB_PARENT = Path(_env_root)
else:
    _VCO_LIB_PARENT = _PROJECT_HOME
if str(_VCO_LIB_PARENT) not in sys.path:
    sys.path.insert(0, str(_VCO_LIB_PARENT))
# VCO-REWIRE-END: orchestrator-root-resolution

# v0.2.52 (Known Issue 6, Sub-issue A): silence
# ``AuthlibDeprecationWarning: authlib.jose module is deprecated`` from
# ``weaviate-client``'s transitive ``authlib`` dep during module import.
# Without this filter the warning lands in the user's terminal on every
# fresh ``install.py`` KG-seed run, which is alarming (and is the warning
# user-reported as Known Issue 6).  MUST run BEFORE ``import weaviate``.
# See ``claude_mcp_servers/weaviate_mcp/server.py`` for the matching
# filter at the MCP-server level.
import warnings as _kg_warnings
try:
    from authlib.deprecate import AuthlibDeprecationWarning as _AuthlibDeprecationWarning  # type: ignore
    _kg_warnings.filterwarnings("ignore", category=_AuthlibDeprecationWarning)
except ImportError:
    _kg_warnings.filterwarnings(
        "ignore",
        message=r".*authlib.*deprecated.*",
        category=DeprecationWarning,
    )

import weaviate
from weaviate.classes.query import Filter
from weaviate_mcp.chunking import TokenCounter, Chunker

# v0.2.18: central embedding dispatcher. Replaces the inline Ollama call
# that was hardcoded to qwen3-embedding (and threw RuntimeError when
# ACTIVE_EMBEDDING was anything else — audit finding KG-W1, 2026-04-30).
# EmbeddingService.for_project() picks the right backend (ollama / openai)
# AND the right named-vector slot (qwen3_embed / openai_text_embed /
# arctic2_embed / ...) from the environment, so this script no longer
# cares about ACTIVE_EMBEDDING or EMBEDDING_MODEL directly.
from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)

# Try to import query logger
try:
    sys.path.insert(0, str(_PROJECT_HOME / ".claude" / "logs"))
    from query_logger import ToolUsageLogger
    HAS_LOGGER = True
except Exception as e:
    HAS_LOGGER = False

# Configuration - Read from environment variables (set by MCP servers or project settings)
# Note (v0.2.18 + v0.2.52 V52-AJ): EMBEDDING_MODEL / ACTIVE_EMBEDDING are NOT
# read here directly. They are resolved by `EmbeddingService.for_project()`
# which consults (in order):
#   1. `os.environ[ACTIVE_EMBEDDING / EMBEDDING_MODEL]` — explicit caller env.
#   2. `launcher.db app_state[embedding.active_profile]` — what the
#      launcher's Identity tab + install.py preset chooser stored.
#   3. `"qwen3"` final fallback (free-tier install, no launcher).
# Install.py threads the resolved env into this script's subprocess on
# fresh / --update runs (via `_subprocess_env_with_embedding` in install.py),
# so this script sees a non-empty ACTIVE_EMBEDDING even when the user
# shell has no such env set — this is the fix for the Windows + CPU-only
# stuck-at-40-with-qwen3 install bug (v0.2.52 V52-AJ, 2026-06-09).
# Keeping the env names in `_redacted_env_snapshot()` failure log helps
# diagnose drift.
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50052"))


# v0.2.21 Step 18 (caller migration): resolve project-scoped collection
# names via the launcher's vct-hub. Falls back to env vars when the hub
# is unreachable (launcher not running, project not registered). The
# resolver emits its own rate-limited warning so callers don't need to
# log anything extra. See `.claude/context/plans/v0.2.21-resolver-design.md`.
def _resolve_collections() -> tuple[str, str]:
    """Return (kg_collection, development_collection) via hub, env-fallback.

    The hub resolver is authoritative when reachable (v0.2.21 contract: the
    launcher's per-project resolution wins over ambient env, so a stale env
    var can't misroute the normal in-project sync). The resolver is queried
    against the TARGET PROJECT ROOT:

      * Normal in-project run: the script lives under the project's
        ``.claude/scripts/``; ``KG_BASE_DIR`` is unset (or equals this tree),
        so we resolve from the script's location → the project's own config.
      * Manual cross-project seed: the script is run by hand against a
        DIFFERENT project, with ``KG_BASE_DIR`` exported to that project's
        root (the same var that already steers ``PROJECT_ROOT`` below). We
        resolve the hub against ``KG_BASE_DIR`` so the collection name matches
        the project whose ``knowledge/`` we are actually walking — not the
        orchestrator tree the script file happens to live under. Resolving the
        script's own parent tree (the prior behavior) silently routed manual
        seeds into the orchestrator's collection regardless of ``KG_COLLECTION``
        / ``KG_BASE_DIR``; keying the resolver off the target root fixes the
        file-root vs collection-name asymmetry without inverting hub precedence.

    VCT_DISABLE_HUB_RESOLVER short-circuit for the test session. See
    ``server.py::_try_resolve_project_config`` for the matching guard +
    ``tests/conftest.py`` for the autouse fixture.
    """
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("DEVELOPMENT_COLLECTION", ""),
        )
    try:
        from vco_lib.project_config import resolve  # type: ignore[import-not-found]
        # Resolve against the TARGET project root: KG_BASE_DIR when set (manual
        # cross-project seed), else the script's own project tree.
        _base = os.getenv("KG_BASE_DIR", "")
        target_root = Path(_base) if _base else Path(__file__).resolve().parent.parent.parent
        cfg = resolve(target_root)
        return (
            cfg.kg_collection or os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            cfg.development_collection or os.getenv("DEVELOPMENT_COLLECTION", ""),
        )
    except Exception:
        return (
            os.getenv("KG_COLLECTION", "KnowledgeGraph"),
            os.getenv("DEVELOPMENT_COLLECTION", ""),
        )


COLLECTION_NAME, _RESOLVED_DEV_COLLECTION = _resolve_collections()
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"

# Chunking configuration for embedding limits.
#
# v0.2.28 (2026-05-23): legacy constant kept ONLY as a fallback when
# the EmbeddingService instance isn't yet available (early-init code
# paths, error logging). The actual chunker used in the sync paths is
# now `_chunker_for(server)` below, which delegates to
# `Chunker.for_model(server.embedding_service.text_model_id)` so that
# every embedding model gets a chunk size tuned to its context window
# (see `claude_mcp_servers/weaviate_mcp/chunking.py::chunking_preset_for_model`).
#
# Pre-v0.2.28 this script hardcoded `max_tokens=2500` everywhere, which
# was correct for qwen3-embedding:0.6b (8k context, 2500 working limit)
# but wrong for 512-token models (would over-chunk by ~5x) and wasteful
# for 32k+ models (under-uses capacity). The hardcoded constant survives
# for any code path that runs before the server / EmbeddingService is
# constructed.
MAX_EMBEDDING_TOKENS = 2500  # Legacy fallback; prefer _chunker_for(server).


def _chunker_for(server) -> "Chunker":
    """Return a Chunker pre-configured for the active embedding model.

    Resolves the model id via `server.embedding_service.text_model_id`
    (the canonical channel — set by EmbeddingService.for_project() from
    env / hub). Falls back to the legacy hardcoded preset when the
    server / embedding_service is None (e.g. test harnesses that
    construct chunks directly).
    """
    try:
        model_id = server.embedding_service.text_model_id  # type: ignore[attr-defined]
    except Exception:
        model_id = ""
    if not model_id:
        # Legacy preset — matches the pre-v0.2.28 hardcoded numbers.
        return Chunker(
            min_tokens=1500,
            max_tokens=MAX_EMBEDDING_TOKENS,
            target_tokens=2500,
        )
    return Chunker.for_model(model_id)


def _max_chunk_tokens_for(server) -> int:
    """Token threshold above which a node/doc must be chunked.

    Mirrors `_chunker_for` so the "fits in one chunk?" branch decision
    and the actual chunk size come from the SAME preset. Falls back to
    the legacy `MAX_EMBEDDING_TOKENS` when the embedding service is
    not yet available.
    """
    try:
        model_id = server.embedding_service.text_model_id  # type: ignore[attr-defined]
        if model_id:
            from weaviate_mcp.chunking import chunking_preset_for_model
            _min, max_t, _tgt = chunking_preset_for_model(model_id)
            return max_t
    except Exception:
        pass
    return MAX_EMBEDDING_TOKENS

# Project root - use KG_BASE_DIR if set (multi-project support), else
# infer from this script's location (.claude/scripts/X → project root).
# Cross-OS: Path.parent.parent works on every supported platform.
_kg_base_dir = os.getenv("KG_BASE_DIR", "")
PROJECT_ROOT = Path(_kg_base_dir) if _kg_base_dir else _PROJECT_HOME
KNOWLEDGE_ROOT = PROJECT_ROOT / "knowledge"

# Development docs collection (project-scoped). Uses the same chunker, named
# vectors, and `index_null_state=True` schema as the KG collection — the only
# differences are: docs may have no frontmatter, no typed WikiLinks, no
# tags/status (we synthesize a small set from the filesystem).
# v0.2.21 Step 18: name resolved alongside COLLECTION_NAME via the hub
# (_resolve_collections above); env-fallback preserved.
DEV_COLLECTION_NAME = _RESOLVED_DEV_COLLECTION

# v0.2.70 FIX #6 (enhancement): the dev-docs root defaults to ``docs/`` but
# can be overridden via the ``DEV_DOCS_ROOT`` env var for projects that keep
# their documentation under a different folder (e.g. ``documentation/``). The
# value may be a bare subdirectory name (joined under PROJECT_ROOT) or an
# absolute path. Empty / unset → the historical ``docs/`` default, so existing
# projects are unaffected.
_dev_docs_root_env = os.getenv("DEV_DOCS_ROOT", "").strip()
if _dev_docs_root_env:
    _dev_docs_candidate = Path(_dev_docs_root_env)
    DOCS_ROOT = (
        _dev_docs_candidate
        if _dev_docs_candidate.is_absolute()
        else PROJECT_ROOT / _dev_docs_candidate
    )
else:
    DOCS_ROOT = PROJECT_ROOT / "docs"

# v0.2.18: named-vector slot is resolved per-instance from
# `EmbeddingService.text_vector_slot` (see WeaviateWrapper below). The
# old `_KG_NAMED_VECTOR_SLOTS` tuple + `_active_named_vector_for_kg()`
# qwen3-only assertion were removed — they predated the central
# dispatcher and silently broke arctic/openai installs (audit finding
# KG-W1, 2026-04-30, fixed in v0.2.18).


class WeaviateWrapper:
    """Weaviate client + EmbeddingService bundle.

    Replaces the v0.2.17 ``WeaviateWrapper`` which hardcoded
    ``qwen3-embedding:0.6b`` for every embed call. v0.2.18: the embed
    backend + active named-vector slot are resolved from environment by
    ``EmbeddingService.for_project()`` — supports ollama (qwen3 / arctic /
    mxbai / nomic), openai (text-embedding-3-small / -large), and any
    future model registered in the embedding-service slot maps.

    Lifecycle: instantiate once at script entry, call ``close()`` (or use
    as context manager) to release HTTP sessions on both the Weaviate
    client and the embedding service.
    """
    def __init__(self, weaviate_url, embedding_service, grpc_port=None):
        http_host = weaviate_url.replace("http://", "").replace("https://", "").split(":")[0]
        http_port = int(weaviate_url.split(":")[-1]) if ":" in weaviate_url else 8080

        self.client = weaviate.connect_to_custom(
            http_host=http_host,
            http_port=http_port,
            http_secure=False,
            grpc_host=http_host,
            grpc_port=grpc_port or 50051,
            grpc_secure=False
        )
        # The EmbeddingService is the single source of truth for: which
        # model to call, which named-vector slot to write, whether the
        # backend is currently reachable, and whether to fan out to
        # multiple slots (multi-slot enrichment writes).
        self.embedding_service = embedding_service

    @property
    def text_vector_slot(self) -> str:
        """Active named-vector slot for KG writes (e.g. 'qwen3_embed')."""
        return self.embedding_service.text_vector_slot

    def close(self) -> None:
        """Close the Weaviate connection (and embedding HTTP session)
        to prevent resource leaks."""
        try:
            self.client.close()
        except Exception:
            pass
        # The EmbeddingService is closed by main() — it may be shared
        # across multiple wrappers in future, so we don't auto-close it
        # here.

    def _get_embedding(self, text: str) -> List[float]:
        """Embed via the active text backend (ollama / openai / ...)."""
        return self.embedding_service.embed_text(text)

    def _get_all_kg_embeddings(self, text: str) -> Dict[str, List[float]]:
        """Embed into every CONFIGURED + REACHABLE text backend.

        Returns ``{slot_name: vector}``. Used for the enrichment-migration
        write path: when the user switches text model (e.g. qwen3 →
        openai), this populates BOTH slots on every new node so search
        continues to work with either model active.

        Empty dict on total backend failure (caller decides whether to
        skip or fail).
        """
        return self.embedding_service.embed_text_all_configured(text)


# For backward compatibility
WeaviateMCPServer = WeaviateWrapper


def _content_signature_excluding_updated(content: str) -> str:
    """Return a SHA256 of the file content excluding the `updated:` line.

    Used to detect whether a re-sync actually contains substantive changes
    or just an unchanged file passing through the post-file-edit hook. If
    the signature is unchanged, the `updated:` timestamp is not bumped —
    avoiding KG-wide timestamp churn on every install run (v0.2.14).

    STORAGE-LAYER hash, intentionally distinct from the RETRIEVAL content-
    identity hash (rl_client/content_dedup.content_sha, sha1[:12]): this one
    answers "is the stored object unchanged so I can skip the re-embed" and
    carries the deliberate "exclude the `updated:` line" nuance so a timestamp-
    only edit hashes identically. The retrieval hash answers "drop this
    duplicate before it reaches Claude". The v0.2.70 dedup triage keeps them
    separate on purpose — do NOT converge.
    """
    if not content.strip().startswith('---'):
        return _sha256_text(content)
    parts = content.split('---', 2)
    if len(parts) < 3:
        return _sha256_text(content)
    fm_text = parts[1]
    body = parts[2]
    # Strip the `updated:` line from the frontmatter for the signature.
    fm_no_updated = re.sub(r'^updated:.*$\n?', '', fm_text, flags=re.MULTILINE)
    return _sha256_text(fm_no_updated + body)


def _sha256_text(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def _build_vector_arg(
    server: "WeaviateMCPServer",
    text: str,
) -> Tuple[object, Mapping[str, List[float]]]:
    """Embed *text* and shape it for `Weaviate.collection.data.insert(vector=)`.

    Returns ``(vector_arg, slots_map)``.

    Behaviour:
      * ``DUAL_EMBEDDING_ENABLED=true`` (default) → multi-slot write.
        Calls ``server._get_all_kg_embeddings()`` which fans out to every
        reachable backend (qwen3 always tried; openai if the key is
        valid). The returned ``vector_arg`` is a ``{slot: vec}`` dict,
        and ``slots_map`` is the same dict (so the caller can log which
        slots got populated).
        Failure modes:
          - All backends fail → empty dict → raises RuntimeError so the
            caller's exception handler kicks in and counts a failure.
          - Active backend fails but a fallback succeeds → dict only has
            the fallback's slot. Search will still work with the
            fallback's model. The caller logs which slots landed.
      * ``DUAL_EMBEDDING_ENABLED=false`` (legacy) → single flat vector
        from the active backend. ``vector_arg`` is a ``list[float]``,
        ``slots_map`` is ``{slot_name: vec}`` so logging stays uniform.
    """
    if DUAL_EMBEDDING_ENABLED:
        slots = server._get_all_kg_embeddings(text)
        if not slots:
            raise RuntimeError(
                "No embedding backend produced a vector. "
                "See ~/.claude/metrics/embedding_failures.jsonl for details."
            )
        return slots, slots
    # Legacy flat-vector path (DUAL_EMBEDDING_ENABLED=false).
    vec = server._get_embedding(text)
    return vec, {server.text_vector_slot: vec}


# ──────────────────────────────────────────────────────────────────────
# v0.2.70 Part 2: pre-shipped embedding INGEST support.
#
# A future orchestrator update may ship pre-computed embeddings for the
# curated KG nodes alongside the (already-shipped) summaries, so a 3rd-party
# install does not have to re-embed all ~117 curated nodes on first sync
# (the arctic-on-CPU install-hang class). The vectors live in a per-slot
# sidecar under knowledge/:
#
#     knowledge/.node_embeddings.<slot>.json   (one file per named-vector slot)
#
# Format (schema_version 1) — see knowledge/.node_embeddings.README.md:
#     {
#       "schema_version": 1,
#       "slot": "qwen3_embed",                 # the named-vector slot these
#                                              #   vectors belong to
#       "model_id": "qwen3-embedding:0.6b",    # informational / provenance
#       "dim": 1024,                           # informational / provenance
#       "nodes": {
#         "<content_hash>": {                  # the node's full-content
#                                              #   signature (16-hex), == the
#                                              #   value sync stores as
#                                              #   `content_hash`
#           "total_chunks": 1,
#           "chunks": [
#             {"chunk_num": 1, "vector": [<float>, ...]}
#           ]
#         },
#         ...
#       }
#     }
#
# IMPORTANT — this release ships NO such file. The loader returns None when
# the sidecar is absent, so the ingest gate is a strict NO-OP and the embed
# path computes vectors exactly as before. The plumbing + its guards are
# present and tested; the data lands in a later update.
#
# Two non-negotiable guards on ingest (the cross-model invariant from the
# v0.2.70 same-active-slot ruling):
#   (a) STALENESS  — the shipped vector's content_hash MUST equal the node's
#                    CURRENT content signature. A stale vector (node edited
#                    since the vector was computed) is never ingested.
#   (b) SLOT-MATCH — the sidecar's slot MUST equal the install's ACTIVE
#                    named-vector slot. A qwen3 vector is NEVER written into
#                    an arctic install (and vice-versa) — that would mix
#                    embedding spaces and silently corrupt search.
# Either guard failing → fall back to computing the embedding (today's
# behaviour). The sidecar is loaded once and cached per (knowledge_root, slot).
# ──────────────────────────────────────────────────────────────────────

#: Cache: (knowledge_root_str, slot) -> parsed sidecar dict OR None (absent /
#: unreadable / slot-mismatch). None is cached too, so a missing sidecar is
#: probed at most once per run.
_SHIPPED_EMBED_CACHE: Dict[Tuple[str, str], Optional[dict]] = {}


def _shipped_embeddings_path(knowledge_root: Path, slot: str) -> Path:
    """Path to the per-slot shipped-embeddings sidecar under knowledge/."""
    return knowledge_root / f".node_embeddings.{slot}.json"


def _load_shipped_embeddings(knowledge_root: Path, slot: str) -> Optional[dict]:
    """Load the shipped-embeddings sidecar for *slot*, or None.

    Returns None (cached) when the sidecar is absent, unparseable, schema-
    incompatible, or declares a DIFFERENT slot than requested (slot-mismatch
    guard at the file level — a defensive second check on top of the
    filename, in case a file is mis-named). Soft-fail: never raises.
    """
    key = (str(knowledge_root), slot)
    if key in _SHIPPED_EMBED_CACHE:
        return _SHIPPED_EMBED_CACHE[key]

    result: Optional[dict] = None
    path = _shipped_embeddings_path(knowledge_root, slot)
    try:
        if path.is_file():
            import json as _json
            data = _json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and int(data.get("schema_version", 0)) == 1
                and isinstance(data.get("nodes"), dict)
                # File-level slot guard: the declared slot must match the slot
                # we were asked for. A mismatch means this file is for another
                # model — treat as absent (never cross-model ingest).
                and data.get("slot") == slot
            ):
                result = data
    except Exception:
        # Corrupt JSON, permission error, etc. — treat as absent. The embed
        # path computes vectors as usual; nothing breaks.
        result = None

    _SHIPPED_EMBED_CACHE[key] = result
    return result


def _shipped_vector_for(
    server: "WeaviateMCPServer",
    knowledge_root: Path,
    content_hash: str,
    expected_chunks: int,
) -> Optional[Tuple[object, Mapping[str, List[float]]]]:
    """Return a ready ``(vector_arg, slots_map)`` from the shipped sidecar, or None.

    Mirrors ``_build_vector_arg``'s return shape so the embed path can drop
    in the shipped vector with no other changes. Returns None (→ caller
    computes the embedding) when ANY guard fails:

      * no sidecar for the active slot (the default this release — NO-OP),
      * no entry for this node's *content_hash* (staleness guard: a vector
        computed against a now-edited node is never reused),
      * the entry's chunk count != *expected_chunks* (the node would embed as
        a different number of chunks than the shipped vectors cover),
      * any chunk vector is missing / empty / non-numeric.

    The active slot is ``server.text_vector_slot`` — so a vector is only ever
    placed in the slot whose embedding space matches the install's model. We
    return a single-slot ``{slot: vec}`` map (NOT a multi-slot fan-out): the
    shipped data only covers the active model's space, and we must never
    synthesise a vector for a slot we don't have data for.
    """
    slot = server.text_vector_slot
    data = _load_shipped_embeddings(knowledge_root, slot)
    if data is None:
        return None  # NO-OP path (this release): no sidecar → compute.

    entry = data["nodes"].get(content_hash)
    if not isinstance(entry, dict):
        return None  # staleness guard: no vector for the current content.

    chunks = entry.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return None
    if len(chunks) != expected_chunks:
        # The node would chunk differently than the shipped vectors cover —
        # don't risk a partial/mismatched ingest; compute fresh.
        return None

    # For the single-chunk caller (expected_chunks == 1) we hand back the lone
    # vector. Multi-chunk ingest is handled chunk-by-chunk by the caller via
    # `_shipped_chunk_vector` below; this function is the single-object path.
    if expected_chunks != 1:
        return None

    vec = _coerce_vector(chunks[0].get("vector"))
    if vec is None:
        return None
    return vec, {slot: vec}


def _shipped_chunk_vector(
    server: "WeaviateMCPServer",
    knowledge_root: Path,
    content_hash: str,
    chunk_num: int,
    expected_chunks: int,
) -> Optional[Tuple[object, Mapping[str, List[float]]]]:
    """Per-chunk variant of ``_shipped_vector_for`` for the multi-chunk path.

    ``chunk_num`` is 1-indexed (matches the stored ``chunk_num`` and the
    multi-chunk insert loop). Same guards as ``_shipped_vector_for``: slot
    match (via the loaded sidecar), content_hash match (staleness), total
    chunk-count match, and a present/valid vector for THIS chunk.
    """
    slot = server.text_vector_slot
    data = _load_shipped_embeddings(knowledge_root, slot)
    if data is None:
        return None

    entry = data["nodes"].get(content_hash)
    if not isinstance(entry, dict):
        return None
    chunks = entry.get("chunks")
    if not isinstance(chunks, list) or len(chunks) != expected_chunks:
        return None

    target = None
    for c in chunks:
        if isinstance(c, dict) and int(c.get("chunk_num", -1)) == chunk_num:
            target = c
            break
    if target is None:
        return None
    vec = _coerce_vector(target.get("vector"))
    if vec is None:
        return None
    return vec, {slot: vec}


def _coerce_vector(raw: object) -> Optional[List[float]]:
    """Validate + coerce a shipped vector to ``list[float]``, or None.

    Rejects empty lists and non-numeric contents (a malformed sidecar must
    fall back to computing, never insert a bad vector).
    """
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return None


def _update_frontmatter_timestamp(file_path: Path, content: str) -> str:
    """
    Update the `updated:` field in YAML frontmatter to current UTC time
    ONLY IF the file's content (excluding the `updated:` line itself) has
    changed since the last sync.

    Writes the updated content back to the file and returns it.

    Content-aware skip (v0.2.14, fix 3): the previous behavior touched
    `updated:` on EVERY sync, even for re-sync passes where the file
    bytes weren't actually changed. Result: every install.py --update
    run produced 60+ KG-node-timestamp-only commits in the working
    tree. Now we hash the (frontmatter-minus-updated + body) and
    compare to the on-disk version of the same hash. If equal,
    skip the write. The user's actual content edits via Edit/Write
    tools always change the body and will pass through unchanged.
    """
    if not content.strip().startswith('---'):
        return content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return content

    # Content-aware skip: if the file on disk has identical
    # signature-excluding-updated, we are in a pass-through re-sync.
    # Don't bump the timestamp.
    try:
        on_disk = file_path.read_text(encoding='utf-8')
        if _content_signature_excluding_updated(on_disk) == _content_signature_excluding_updated(content):
            return content
    except (OSError, UnicodeDecodeError):
        # If we can't read the file (race / permissions / encoding), fall
        # through to the unconditional update — preserves prior behavior
        # in edge cases.
        pass

    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    fm_text = parts[1]

    updated_pattern = re.compile(r'^updated:.*$', re.MULTILINE)
    if updated_pattern.search(fm_text):
        new_fm = updated_pattern.sub(f'updated: {now_iso}', fm_text)
    else:
        # Add after 'created:' line if present, else append before end of block
        created_pattern = re.compile(r'^(created:.*)$', re.MULTILINE)
        if created_pattern.search(fm_text):
            new_fm = created_pattern.sub(r'\1\nupdated: ' + now_iso, fm_text)
        else:
            new_fm = fm_text.rstrip('\n') + f'\nupdated: {now_iso}\n'

    new_content = '---' + new_fm + '---' + parts[2]
    file_path.write_text(new_content, encoding='utf-8')
    return new_content


def parse_frontmatter(content: str) -> Tuple[Optional[Dict], str]:
    """
    Parse YAML frontmatter from markdown content.

    Args:
        content: Markdown file content

    Returns:
        Tuple of (frontmatter_dict, content_without_frontmatter)
    """
    if not content.strip().startswith('---'):
        return None, content

    parts = content.split('---', 2)
    if len(parts) < 3:
        return None, content

    try:
        frontmatter = yaml.safe_load(parts[1])
        content_without_fm = parts[2].strip()
        return frontmatter, content_without_fm
    except yaml.YAMLError:
        return None, content


def validate_node_against_vocabulary(node_data: Dict, file_path: Path) -> List[str]:
    """
    Validate node against vocabulary and tag hierarchy rules.

    Args:
        node_data: Parsed node data
        file_path: Path to node file

    Returns:
        List of validation warnings (empty if all valid)
    """
    warnings = []

    # 1. Type validation
    valid_types = {"project", "concept", "tool", "research", "model", "hardware", "pattern", "insight", "guide"}
    node_type = node_data.get("node_type", "")
    if node_type not in valid_types:
        warnings.append(f"Invalid node type '{node_type}' (valid: {', '.join(sorted(valid_types))})")

    # 2. Tag validation
    tags = node_data.get("tags", [])

    # Check tag count (3-10 recommended)
    if len(tags) < 3:
        warnings.append(f"Too few tags ({len(tags)}) - recommended 3-10 tags")
    elif len(tags) > 10:
        warnings.append(f"Too many tags ({len(tags)}) - recommended 3-10 tags")

    # Check tag format
    for tag in tags:
        # Tags should be lowercase or UPPERCASE (acronyms)
        # Multi-word tags should use hyphens
        if " " in tag:
            warnings.append(f"Tag '{tag}' contains spaces - use hyphens instead")
        if "_" in tag:
            warnings.append(f"Tag '{tag}' uses underscores - use hyphens instead")
        # Check for camelCase (not allowed except for acronyms)
        if any(c.isupper() for c in tag) and not tag.isupper() and "-" not in tag:
            # Could be acronym like "AI" or camelCase like "MyTag"
            if len([c for c in tag if c.isupper()]) > 1 and not tag.isupper():
                warnings.append(f"Tag '{tag}' uses camelCase - use lowercase with hyphens")

    # Check for recommended tag categories (for technical nodes)
    if node_type in {"project", "concept", "tool", "pattern"}:
        # Should have at least 1 domain tag
        domain_tags = {"AI", "ML", "NLP", "CV", "database", "workflow", "tooling",
                      "infrastructure", "frontend", "backend", "security"}
        has_domain = any(tag in domain_tags for tag in tags)

        # Should have abstraction level (except for tools)
        abstraction_tags = {"high-level-plan", "mid-level-architecture",
                          "low-level-implementation", "function-description"}
        has_abstraction = any(tag in abstraction_tags for tag in tags)

        if not has_domain:
            warnings.append("No domain tag found (recommended: #AI, #database, #workflow, etc.)")

        if not has_abstraction and node_type != "tool":
            warnings.append("No abstraction level tag (recommended: #high-level-plan, #mid-level-architecture, #low-level-implementation)")

    # 3. External links validation (if present)
    external_links = node_data.get("external_links", "")
    if external_links:
        try:
            import json
            links = json.loads(external_links) if isinstance(external_links, str) else external_links
            if not isinstance(links, dict):
                warnings.append("external_links should be a dictionary")
        except (json.JSONDecodeError, TypeError):
            warnings.append("external_links is not valid JSON")

    return warnings


def parse_markdown_node(content: str, file_path: Path) -> Dict:
    """
    Parse markdown file to extract knowledge node data

    Args:
        content: Markdown file content
        file_path: Path to markdown file

    Returns:
        Dictionary with node data (title, tags, links, etc.)
    """
    # Parse YAML frontmatter (if present)
    frontmatter, content_body = parse_frontmatter(content)

    lines = content.strip().split('\n')

    # Extract title (from frontmatter or first # heading)
    if frontmatter and 'title' in frontmatter:
        title = frontmatter['title']
    else:
        title = file_path.stem  # Default to filename
        for line in lines:
            if line.startswith('# '):
                title = line[2:].strip()
                break

    # Extract tags (from frontmatter or inline)
    tags = []
    if frontmatter and 'tags' in frontmatter:
        # Frontmatter tags (array format) - convert all to strings
        raw_tags = frontmatter['tags'] if isinstance(frontmatter['tags'], list) else []
        tags = [str(tag) for tag in raw_tags]
    else:
        # Inline tags (Obsidian style: #tag)
        tag_pattern = r'#([a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)'
        for match in re.finditer(tag_pattern, content):
            tag = match.group(1)
            if tag not in tags:
                tags.append(tag)

    # Extract WikiLinks - supports both typed and untyped
    # Typed: [[uses::Redis]], [[implements::Pattern]]
    # Untyped: [[Redis]] (defaults to "relatedTo")
    links = []  # Untyped links (backward compatibility)
    typed_links = []  # New: Typed relationships

    # Updated pattern to capture optional relationship type
    # Matches: [[type::target]] or [[target]]
    link_pattern = r'\[\[(?:([a-zA-Z_]+)::)?([^\]]+)\]\]'

    for match in re.finditer(link_pattern, content):
        relation_type = match.group(1)  # None if untyped
        target_title = match.group(2).strip()

        if relation_type:
            # Typed relationship
            typed_link = {
                "relation_type": relation_type,
                "target_title": target_title
            }
            if typed_link not in typed_links:
                typed_links.append(typed_link)
        else:
            # Untyped (backward compatibility)
            if target_title not in links:
                links.append(target_title)

    # Node type (from frontmatter or directory)
    if frontmatter and 'type' in frontmatter:
        node_type = frontmatter['type']
    else:
        rel_path = file_path.relative_to(KNOWLEDGE_ROOT)
        node_type = str(rel_path.parts[0]) if len(rel_path.parts) > 1 else "general"

    # Temporal metadata from frontmatter
    temporal_data = {}
    if frontmatter:
        # Created/updated timestamps (YAML parses ISO timestamps as datetime objects)
        if 'created' in frontmatter and frontmatter['created'] != 'unknown':
            try:
                val = frontmatter['created']
                # If already a datetime object, use it directly
                if isinstance(val, datetime):
                    temporal_data['created'] = val.isoformat()
                else:
                    # Parse string format
                    val_str = str(val)
                    if 'T' in val_str:
                        created_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        created_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['created'] = created_dt.isoformat()
            except (ValueError, TypeError) as e:
                pass

        if 'updated' in frontmatter and frontmatter['updated'] != 'unknown':
            try:
                val = frontmatter['updated']
                if isinstance(val, datetime):
                    temporal_data['updated'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        updated_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        updated_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['updated'] = updated_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Valid from/until timestamps
        if 'valid_from' in frontmatter:
            try:
                val = frontmatter['valid_from']
                if isinstance(val, datetime):
                    temporal_data['valid_from'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_from_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_from_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_from'] = valid_from_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # `valid_until` semantics:
        #   - Frontmatter omits it OR sets it to None → "never expires"; the
        #     property is left unset (null) in the DB.
        #   - Frontmatter sets a real date → write it.
        #
        # Null is filterable because the collection is created with
        # `inverted_index_config=Configure.inverted_index(index_null_state=True)`
        # (see `_create_kg_collection`). The MCP `_stale_filter()` then uses
        # `valid_until is_none(True) | valid_until > now`. Setting that
        # config at create time is required — Weaviate doesn't allow toggling
        # it later (`Reconfigure.inverted_index` lacks `index_null_state`).
        if 'valid_until' in frontmatter and frontmatter['valid_until'] is not None:
            try:
                val = frontmatter['valid_until']
                if isinstance(val, datetime):
                    temporal_data['valid_until'] = val.isoformat()
                else:
                    val_str = str(val)
                    if 'T' in val_str:
                        valid_until_dt = datetime.fromisoformat(val_str.replace('Z', '+00:00'))
                    else:
                        valid_until_dt = datetime.strptime(val_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    temporal_data['valid_until'] = valid_until_dt.isoformat()
            except (ValueError, TypeError):
                pass

        # Status
        if 'status' in frontmatter:
            temporal_data['status'] = frontmatter['status']

    # External links from frontmatter (RDF-inspired)
    external_links = ""
    if frontmatter and 'external_links' in frontmatter:
        ext_links = frontmatter['external_links']
        if isinstance(ext_links, dict):
            # Convert dict to JSON string for storage (Weaviate TEXT field)
            import json
            external_links = json.dumps(ext_links)

    # Fallback: File timestamps for old created_at/updated_at fields
    stat = file_path.stat()
    created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    result = {
        "title": title,
        "content": content,
        "file_path": str(file_path.relative_to(PROJECT_ROOT)),
        "node_type": node_type,
        "tags": tags,
        "links": links,
        "typed_links": typed_links,  # Typed relationships
        "external_links": external_links,  # External links (DBpedia, official docs, etc.)
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat()
    }

    # Add temporal metadata if present
    result.update(temporal_data)

    return result


# v0.2.38 A4: Canonical scalar-property registry for KG collections.
#
# BOTH the fresh-create path AND the additive-migrate path inside
# `ensure_collection_exists` must stay in sync. Previously they were
# independent inline dicts — V37-C Gap 6d found that chunking props
# (chunk_num / total_chunks / source_node_id) existed in the create
# branch but not the migrate branch, causing "no such prop" failures
# on legacy collections. Hoisting into ONE constant here guarantees
# the two paths can never diverge again.
#
# Mapping: prop_name → DataType sentinel.  DataType is a lazy import
# inside ensure_collection_exists so we use string sentinels at the
# module level and resolve them at runtime (avoids importing weaviate
# at parse-time for scripts that only need the constant for inspection,
# e.g. unit tests).
#
# String sentinels match DataType attribute names: "TEXT", "INT",
# "DATE", "TEXT_ARRAY".  Non-scalar props (typed_links OBJECT_ARRAY,
# linksTo cross-reference) are handled separately because they require
# nested_properties / ReferenceProperty which can't be expressed as
# a simple name→DataType mapping.
_KG_NODE_SCALAR_PROPERTIES: dict[str, str] = {
    # Core identity
    "title":           "TEXT",
    "content":         "TEXT",
    "file_path":       "TEXT",
    "node_type":       "TEXT",
    # Multi-value arrays (TEXT_ARRAY is still a "scalar" Weaviate primitive)
    "tags":            "TEXT_ARRAY",
    "links":           "TEXT_ARRAY",
    # External links (RDF-inspired; stored as JSON text since Weaviate OBJECT
    # requires nested properties)
    "external_links":  "TEXT",
    # Legacy filesystem timestamps (back-compat)
    "created_at":      "DATE",
    "updated_at":      "DATE",
    # Canonical temporal metadata (from frontmatter, PR-24 2026-05-16)
    "created":         "DATE",
    "updated":         "DATE",
    "valid_from":      "DATE",
    "valid_until":     "DATE",
    # v0.2.17: status + content-hash for embed-skip on re-sync
    "status":          "TEXT",
    "content_hash":    "TEXT",
    # v0.2.37 Gap 6d: chunking props — MUST be present in both fresh-create
    # and additive-migrate paths to avoid "no such prop 'chunk_num'" failures
    # on legacy collections.  Validated by test_kg_schema_consistency.py.
    "chunk_num":       "INT",
    "total_chunks":    "INT",
    "source_node_id":  "TEXT",
}


def ensure_collection_exists(server: WeaviateMCPServer) -> bool:
    """
    Ensure the project's KG_COLLECTION (env-resolved, fallback "KnowledgeGraph")
    exists with proper schema.

    Named-vector slots (v0.2.18): sourced from
    `vco_lib.weaviate_schema.KG_NAMED_VECTORS` for parity with the
    `project_init.kg_class_definition` canonical path. Falls back to the
    legacy 3-slot config if the import fails (one-off script runs outside
    the orchestrator clone). Mirrors `ensure_dev_collection_exists`.

    Scalar properties sourced from `_KG_NODE_SCALAR_PROPERTIES` (v0.2.38 A4)
    so fresh-create and additive-migrate paths cannot diverge.

    Args:
        server: Weaviate MCP server instance

    Returns:
        True if collection exists or was created
    """
    try:
        from weaviate.classes.config import Configure, Property, DataType

        # Resolve DataType values from the module-level string sentinels.
        # Done once per call so tests can inspect _KG_NODE_SCALAR_PROPERTIES
        # without importing weaviate.
        _dt_map: dict[str, object] = {
            "TEXT":       DataType.TEXT,
            "INT":        DataType.INT,
            "DATE":       DataType.DATE,
            "TEXT_ARRAY": DataType.TEXT_ARRAY,
        }
        _scalar_props: dict[str, object] = {
            name: _dt_map[sentinel]
            for name, sentinel in _KG_NODE_SCALAR_PROPERTIES.items()
        }

        if server.client.collections.exists(COLLECTION_NAME):
            print(f"✓ Collection '{COLLECTION_NAME}' exists")

            # Additive-migrate path: add any scalar prop missing from an
            # existing collection (temporal + chunking + hash).  Uses the
            # same canonical list as the fresh-create path below — A4
            # invariant enforced by test_kg_schema_consistency.py.
            try:
                collection = server.client.collections.get(COLLECTION_NAME)
                config = collection.config.get()
                existing_props = {prop.name for prop in config.properties}
                existing_refs = {ref.name for ref in (config.references or [])}

                # Add every scalar prop that's missing.
                for prop_name, prop_type in _scalar_props.items():
                    if prop_name not in existing_props:
                        print(f"  Adding property: {prop_name}")
                        collection.config.add_property(
                            Property(name=prop_name, data_type=prop_type)
                        )

                # Add typed_links property if missing
                if 'typed_links' not in existing_props:
                    print(f"  Adding property: typed_links (nested objects)")
                    collection.config.add_property(
                        Property(
                            name="typed_links",
                            data_type=DataType.OBJECT_ARRAY,
                            nested_properties=[
                                Property(name="relation_type", data_type=DataType.TEXT),
                                Property(name="target_title", data_type=DataType.TEXT)
                            ]
                        )
                    )

                # Add external_links property if missing (RDF-inspired)
                if 'external_links' not in existing_props:
                    print(f"  Adding property: external_links (JSON text)")
                    collection.config.add_property(
                        Property(name="external_links", data_type=DataType.TEXT)
                    )

                # Add cross-reference property if missing
                from weaviate.classes.config import ReferenceProperty
                if 'linksTo' not in existing_refs:
                    print(f"  Adding cross-reference: linksTo")
                    collection.config.add_reference(
                        ReferenceProperty(
                            name="linksTo",
                            target_collection=COLLECTION_NAME
                        )
                    )

                print(f"✓ Schema up to date")
            except Exception as e:
                print(f"⚠️  Could not update schema: {e}")

            return True

        print(f"Creating collection '{COLLECTION_NAME}'...")

        # v0.2.18: pull the 5-slot named-vector catalog from the canonical
        # source (`vco_lib.weaviate_schema.KG_NAMED_VECTORS`) so this
        # runtime fallback creates the KG collection at the same shape as
        # `vco_lib.project_init.kg_class_definition`. Fall back to the
        # legacy 3-slot config when the import fails (one-off script runs
        # outside an orchestrator clone where vco_lib isn't on the path).
        # The migrate dispatcher's additive `copy` action picks up any
        # missing slot later when the user does run install/update.
        #
        # Mirrors the Dev-collection variant at `ensure_dev_collection_exists`
        # (landed bcacfc0). Both sites stay in lockstep with the canonical
        # `project_init.{kg,development}_class_definition` so the migrate
        # dispatcher's additive patch_props diff doesn't trip phantom
        # missing-slot loops.
        try:
            from vco_lib.weaviate_schema import KG_NAMED_VECTORS
            named_vectors = [
                Configure.NamedVectors.none(name=slot.name)
                for slot in KG_NAMED_VECTORS
            ]
        except Exception as import_err:  # noqa: BLE001 — best-effort fallback
            print(f"  ⚠️  Could not import KG_NAMED_VECTORS ({import_err}); "
                  "falling back to legacy 3-slot config")
            named_vectors = [
                Configure.NamedVectors.none(name="qwen3_embed"),     # active
                Configure.NamedVectors.none(name="ollama_embed"),    # legacy
                Configure.NamedVectors.none(name="openai_embed"),    # optional
            ]

        # Fresh-create path: build Property list from the canonical scalar
        # registry (_KG_NODE_SCALAR_PROPERTIES) so this path and the
        # additive-migrate path above are always identical in coverage.
        # Non-scalar props (typed_links, content_hash note, etc.) are
        # appended inline below.
        scalar_property_list = [
            Property(name=name, data_type=dt)
            for name, dt in _scalar_props.items()
        ]

        server.client.collections.create(
            name=COLLECTION_NAME,
            description="Claude knowledge graph nodes with semantic search (chunked for large files)",
            properties=scalar_property_list + [
                # Typed relationships as JSON objects (non-scalar — needs
                # nested_properties, cannot be expressed in the scalar registry)
                Property(
                    name="typed_links",
                    data_type=DataType.OBJECT_ARRAY,
                    nested_properties=[
                        Property(name="relation_type", data_type=DataType.TEXT),
                        Property(name="target_title", data_type=DataType.TEXT)
                    ]
                ),
            ],
            # Named vectors must match `vco_lib.weaviate_schema.KG_NAMED_VECTORS`
            # (the canonical v0.2.18 catalog). Without these the collection
            # accepts only the unnamed default vector, and per-named-vector
            # inserts fail at runtime ("collection configured without
            # multiple named vectors but received named vectors:
            # map[ollama_embed:...]"). Vectors are still computed manually
            # (Configure.NamedVectors.none).
            vectorizer_config=named_vectors,
            # `index_null_state=True` enables `is_none(True)` filters on date
            # properties (notably `valid_until`). Required for the MCP
            # `_stale_filter` to filter out expired/archived nodes at query
            # time. CANNOT be added later via Reconfigure — must be set at
            # create time. (Weaviate 1.28; verified 2026-04-30 against the
            # python client v4.)
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )

        print(f"✓ Created collection '{COLLECTION_NAME}' "
              f"({len(named_vectors)} named vectors + index_null_state=True)")
        return True

        if result["success"]:
            print(f"✓ Created collection '{COLLECTION_NAME}'")
            return True
        else:
            print(f"❌ Failed to create collection: {result.get('message')}")
            return False

    except Exception as e:
        print(f"❌ Error ensuring collection: {e}")
        return False


def ensure_dev_collection_exists(server: WeaviateMCPServer) -> bool:
    """Create the development docs collection if missing.

    Schema is a **near-subset** of the KG schema. Matches
    `vco_lib.project_init.development_class_definition` exactly — this is
    the runtime-fallback path used when `project_init` didn't get there
    first (one-off `python -m sync_knowledge_graph --all-docs` runs from a
    project that hasn't been re-installed since the v0.2.18 schema bump).
    Both write sites MUST stay in lockstep so the migrate dispatcher's
    additive patch_props diff doesn't trip a phantom missing-prop loop.

    Properties:
      - title, content, file_path (the load-bearing trio)
      - created_at, updated_at (legacy filesystem timestamps; back-compat)
      - created, updated, valid_from, valid_until (canonical temporal,
        PR-24 2026-05-16) — required by MCP `_stale_filter` (valid_until
        is_none(True) | valid_until > now)
      - status (v0.2.18 2026-05-19) — KG parity for archived-doc filter
      - content_hash (v0.2.18 2026-05-19) — KG parity, powers the
        embed-skip fast-path in `sync_doc`
      - chunk_num, total_chunks, source_node_id (chunking support)

    Explicitly NOT mirrored from KG (user direction 2026-05-19):
      - tags / links / typed_links — KG-only graph metadata
      - external_links — KG-only RDF metadata
      - node_type — redundant (every row in a Dev collection is unambiguously
        a "doc" by class name)

    Named-vector slots (v0.2.18): sourced from
    `vco_lib.weaviate_schema.KG_NAMED_VECTORS` for parity with the
    `project_init.development_class_definition` canonical path. With
    fallback to the legacy 3-slot config if the import fails (one-off
    script runs outside the orchestrator clone).

    Returns True if the collection exists or was created.
    """
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION env not set — skipping dev collection")
        return False
    try:
        from weaviate.classes.config import Configure, Property, DataType

        if server.client.collections.exists(DEV_COLLECTION_NAME):
            print(f"✓ Dev collection '{DEV_COLLECTION_NAME}' exists")
            return True

        # v0.2.18: pull the 5-slot named-vector catalog from the canonical
        # source (`vco_lib.weaviate_schema.KG_NAMED_VECTORS`) so this
        # runtime fallback creates collections at the same shape as the
        # `project_init.development_class_definition` path. Fall back to
        # the legacy 3-slot config when the import fails (one-off script
        # runs outside an orchestrator clone where vco_lib isn't on the
        # path). The migrate dispatcher's additive `copy` action picks up
        # any missing slot later when the user does run install/update.
        try:
            from vco_lib.weaviate_schema import KG_NAMED_VECTORS
            named_vectors = [
                Configure.NamedVectors.none(name=slot.name)
                for slot in KG_NAMED_VECTORS
            ]
        except Exception as import_err:  # noqa: BLE001 — best-effort fallback
            print(f"  ⚠️  Could not import KG_NAMED_VECTORS ({import_err}); "
                  "falling back to legacy 3-slot config")
            named_vectors = [
                Configure.NamedVectors.none(name="qwen3_embed"),
                Configure.NamedVectors.none(name="ollama_embed"),
                Configure.NamedVectors.none(name="openai_embed"),
            ]

        print(f"Creating dev collection '{DEV_COLLECTION_NAME}'...")
        server.client.collections.create(
            name=DEV_COLLECTION_NAME,
            description="Project development documentation (docs/) — chunked, "
                        "schema-paired with KG, auto-bound when project is "
                        "given KG access via the launcher.",
            properties=[
                Property(name="title", data_type=DataType.TEXT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="file_path", data_type=DataType.TEXT),
                # Legacy filesystem timestamps (kept for back-compat; older
                # docs were ingested with these names).
                Property(name="created_at", data_type=DataType.DATE),
                Property(name="updated_at", data_type=DataType.DATE),
                # Canonical temporal metadata — mirrors KG schema +
                # vco_lib.project_init.development_class_definition.
                # Required so the MCP `_stale_filter` (valid_until is_none
                # OR > now) doesn't fail with "no such prop" on Dev
                # collections. PR-24 (2026-05-16).
                Property(name="created", data_type=DataType.DATE),
                Property(name="updated", data_type=DataType.DATE),
                Property(name="valid_from", data_type=DataType.DATE),
                Property(name="valid_until", data_type=DataType.DATE),
                # v0.2.18 (2026-05-19): KG parity. `status` lets archived
                # docs be filtered out by `hybrid_search`; `content_hash`
                # powers the embed-skip fast-path in `sync_doc`. Must
                # match `project_init.development_class_definition`
                # exactly so the migrate dispatcher's additive patch_props
                # diff doesn't loop.
                Property(name="status", data_type=DataType.TEXT),
                Property(name="content_hash", data_type=DataType.TEXT),
                # Chunking support
                Property(name="chunk_num", data_type=DataType.INT),
                Property(name="total_chunks", data_type=DataType.INT),
                Property(name="source_node_id", data_type=DataType.TEXT),
            ],
            vectorizer_config=named_vectors,
            inverted_index_config=Configure.inverted_index(index_null_state=True),
        )
        print(f"✓ Created dev collection '{DEV_COLLECTION_NAME}' "
              f"({len(named_vectors)} named vectors + index_null_state=True)")
        return True
    except Exception as e:
        print(f"❌ Error ensuring dev collection: {e}")
        return False


def _doc_title_from_file(file_path: Path, content: str) -> str:
    """Pick a title for a doc file (no frontmatter assumed).

    Order: first H1 heading; first H2 heading; filename stem (humanized).
    """
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in content.splitlines()[:50]:
        s = line.strip()
        if s.startswith("## "):
            return s[3:].strip()
    return file_path.stem.replace("-", " ").replace("_", " ").strip().title()


def parse_doc_file(content: str, file_path: Path) -> Dict:
    """Parse a docs/ file. Returns the same shape as `parse_markdown_node`
    but with KG-specific fields (tags, links, etc.) absent or empty.

    Docs lack frontmatter. We synthesize:
      - title: first H1, falling back to H2, falling back to filename stem
      - created_at / updated_at: filesystem stat, since git history is more
        expensive to compute and the chunker doesn't need exact provenance
    """
    title = _doc_title_from_file(file_path, content)
    try:
        st = file_path.stat()
        # Both timestamps from filesystem; we don't have richer provenance
        # without git. Good enough: the index respects updated_at for
        # `days=N` recency filters.
        created_at = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
        updated_at = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except OSError:
        now = datetime.now(timezone.utc)
        created_at = now
        updated_at = now
    rel_path = ""
    try:
        rel_path = str(file_path.relative_to(PROJECT_ROOT))
    except ValueError:
        rel_path = str(file_path)
    return {
        "title": title,
        "content": content,
        "file_path": rel_path,
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        # Empty KG-specific fields — kept for symmetry with sync_node's
        # data_obj structure but never written into the dev collection
        # (its schema doesn't have them).
        "tags": [],
        "links": [],
        "typed_links": [],
        "external_links": "",
        "node_type": "doc",
    }


def sync_doc(server: WeaviateMCPServer, file_path: Path) -> bool:
    """Sync a single docs/ file to the development collection.

    Mirrors `sync_node` minus the KG-specific concerns (no frontmatter
    parsing, no WikiLink resolution, no tag-from-typed-links inference, no
    cross-references). Same chunker, same active-vector-slot logic.

    v0.2.18 (2026-05-19): mirrors the v0.2.17 KG content_hash embed-skip
    fast-path. Before re-embedding, query existing objects for this
    `file_path` and check (a) every existing chunk has a non-empty
    `content_hash` equal to the current file's hash, (b) chunk-count
    matches what we'd reproduce, and (c) the active named-vector slot
    (`server.text_vector_slot`) is populated on every chunk. When all
    three hold → skip the delete-and-re-embed entirely. Saves the entire
    Ollama embed roundtrip + Weaviate delete/insert per unchanged file.

    Conservative gating: any missing chunk-vector, any empty hash, any
    mismatched chunk-count, or any exception in the fast-path check falls
    through to the existing delete-and-re-embed path (and that path
    writes `content_hash` so the NEXT re-sync will hit the fast path).
    This handles the warm-up case where an existing v0.2.17 Dev collection
    just gained the `content_hash` property via additive patch_props but
    none of its rows have a value yet.
    """
    if not DEV_COLLECTION_NAME:
        print(f"⊘ DEVELOPMENT_COLLECTION not set — skipping {file_path}")
        return True

    start_time = time.time()

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            return False

        # Same archive-skip logic as KG (path contains 'archive/' segment).
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived doc: {reason}")
            # v0.2.70 FIX #1: delete by file_path (unique), NOT by title — a
            # title-scoped delete here removed any active doc sharing this
            # archived doc's synthesized title during a --all docs run.
            try:
                fp_value = _relative_file_path(file_path)
                removed = _delete_doc_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior dev entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior dev entry: {e}")
            return True

        content = file_path.read_text(encoding="utf-8")
        doc_data = parse_doc_file(content, file_path)

        print(f"🔄 Syncing doc: {doc_data['title']}")

        coll = server.client.collections.get(DEV_COLLECTION_NAME)

        # v0.2.18: compute content_hash BEFORE the delete-and-re-embed
        # pipeline so we can short-circuit on the unchanged-file case.
        # The hash function is the same one used by the KG path
        # (`_content_signature_excluding_updated`); for a docs/ file with
        # no frontmatter it degenerates to plain SHA-256 of the body —
        # exactly what we want.
        current_content_hash = _content_signature_excluding_updated(content)

        # Active named-vector slot for the running backend (e.g.
        # 'qwen3_embed' for Ollama qwen3, 'openai_text_embed' for OpenAI).
        # The fast-path requires this slot to be populated on every
        # existing chunk; otherwise we're in the v0.2.17 -> v0.2.18 warm-up
        # case where the user just switched backends and the new slot is
        # empty, and we MUST re-embed to populate it.
        try:
            active_slot = server.text_vector_slot
        except Exception:  # noqa: BLE001 — soft-fail on degenerate wrapper
            active_slot = ""

        # Pull existing objects WITH vectors so we can verify the active
        # slot is populated. `include_vector=True` returns `obj.vector` as
        # a dict keyed by slot name for named-vector collections.
        try:
            existing = coll.query.fetch_objects(
                filters=Filter.by_property("file_path").equal(
                    doc_data["file_path"]
                ),
                limit=100,
                return_properties=[
                    "content_hash", "chunk_num", "total_chunks",
                ],
                include_vector=True,
            )
        except Exception as fetch_err:  # noqa: BLE001
            # Older Weaviate clients / mocked clients that don't accept
            # `include_vector` keyword → fall back to the basic fetch and
            # skip the active-slot check (defer to content_hash + chunk
            # count). Any real client supports this kw since Weaviate v4.
            print(f"   (fetch_objects(include_vector=True) failed: "
                  f"{fetch_err}; falling back to hash-only check)")
            try:
                existing = coll.query.fetch_objects(
                    filters=Filter.by_property("file_path").equal(
                        doc_data["file_path"]
                    ),
                    limit=100,
                    return_properties=[
                        "content_hash", "chunk_num", "total_chunks",
                    ],
                )
            except Exception:
                existing = None  # forces fall-through to re-embed

        # EMBED-SKIP fast path. Mirrors sync_node's v0.2.17 implementation
        # with the added active-slot check (which sync_node's fast-path
        # also relies on implicitly via the chunk_count gate, but Dev gets
        # it explicit because Dev rows are more likely to have a chunk
        # written under one slot and not yet enriched under another).
        if existing is not None and existing.objects:
            try:
                existing_hashes: List[str] = []
                existing_total_chunks: List[int] = []
                active_slot_populated: List[bool] = []
                for obj in existing.objects:
                    props = obj.properties or {}
                    existing_hashes.append(props.get("content_hash", "") or "")
                    tc = props.get("total_chunks", 0)
                    try:
                        existing_total_chunks.append(int(tc) if tc is not None else 0)
                    except (TypeError, ValueError):
                        existing_total_chunks.append(0)
                    # `obj.vector` is a dict {slot: list[float]} for
                    # named-vector collections; missing/None when the
                    # fetch didn't include vectors (older client).
                    vec_field = getattr(obj, "vector", None)
                    if isinstance(vec_field, dict) and active_slot:
                        slot_vec = vec_field.get(active_slot)
                        active_slot_populated.append(
                            bool(slot_vec) and len(slot_vec) > 0
                        )
                    else:
                        # Couldn't inspect → be conservative, treat as
                        # NOT populated so we re-embed. Exception: if
                        # active_slot is empty (no wrapper info), skip
                        # the active-slot gate altogether (back to
                        # content_hash + chunk_count).
                        active_slot_populated.append(not active_slot)

                chunk_count_ok = (
                    len(existing_total_chunks) > 0
                    and all(
                        tc == len(existing_total_chunks)
                        for tc in existing_total_chunks
                    )
                )
                hashes_ok = (
                    len(existing_hashes) > 0
                    and all(h == current_content_hash for h in existing_hashes)
                    and all(h for h in existing_hashes)  # no empty strings
                )
                slots_ok = all(active_slot_populated)
                if chunk_count_ok and hashes_ok and slots_ok:
                    elapsed = time.time() - start_time
                    print(
                        f"   ⏭️  Embed-skip: content_hash matches "
                        f"({current_content_hash[:12]}…); "
                        f"{len(existing_hashes)} chunk(s) preserved "
                        f"in {active_slot or '<no-slot>'} "
                        f"({elapsed*1000:.0f} ms)"
                    )
                    return True
            except Exception as skip_err:  # noqa: BLE001
                # Soft-fail: fall through to the delete-and-re-embed path.
                print(f"   (embed-skip check failed: {skip_err}; re-embedding)")

        # Fast path didn't apply (or no existing objects). Delete old
        # versions and re-embed. The `content_hash` written below means
        # the NEXT re-sync will hit the fast path.
        if existing is not None:
            for obj in existing.objects:
                coll.data.delete_by_id(obj.uuid)

        token_count = TokenCounter.count_tokens(content)
        source_id = str(uuid.uuid4())
        # v0.2.28: per-model chunk threshold instead of hardcoded 2500.
        _max_tokens = _max_chunk_tokens_for(server)

        if token_count <= _max_tokens:
            vec_arg, slots_written = _build_vector_arg(server, content)
            data_obj = {
                "title": doc_data["title"],
                "content": doc_data["content"],
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": source_id,
                # v0.2.18 (2026-05-19): persist content_hash so the next
                # re-sync can take the embed-skip fast-path above. Same
                # value for all chunks of the same file (computed once
                # over the whole file content above).
                "content_hash": current_content_hash,
            }
            coll.data.insert(properties=data_obj, vector=vec_arg)
            print(f"   ✓ Stored doc as single chunk (vectors={sorted(slots_written)})")
            return True

        # Chunked path — mirrors `sync_node` chunked branch.
        # v0.2.28: per-model chunker preset (qwen3 → large_context;
        # arctic / 512-token → small_context; etc.) instead of hardcoded
        # 2500-token chunks regardless of model.
        chunker = _chunker_for(server)
        chunks = chunker.chunk_text(
            text=content,
            source_id=source_id,
            metadata={
                "title": doc_data["title"],
                "file_path": doc_data["file_path"],
            },
        )
        print(f"   Split into {len(chunks)} chunks", flush=True)
        last_slots: Mapping[str, List[float]] = {}
        for i, chunk in enumerate(chunks):
            vec_arg, last_slots = _build_vector_arg(server, chunk.content)
            data_obj = {
                "title": doc_data["title"],
                "content": chunk.content,
                "file_path": doc_data["file_path"],
                "created_at": doc_data["created_at"],
                "updated_at": doc_data["updated_at"],
                "chunk_num": i + 1,
                "total_chunks": len(chunks),
                "source_node_id": source_id,
                # v0.2.18 (2026-05-19): every chunk of the same file
                # shares the same content_hash (computed over the whole
                # file). The embed-skip fast-path above requires ALL
                # chunks for a file_path to carry an identical, non-empty
                # hash before it skips — writing the same value here
                # keeps that invariant.
                "content_hash": current_content_hash,
            }
            coll.data.insert(properties=data_obj, vector=vec_arg)
            # v0.2.69 FIX 3 (review SHOULD-FIX): per-chunk heartbeat. The
            # launcher's kg-sync stall watchdog re-arms on every output
            # line; without a per-chunk print here, a large multi-chunk
            # doc would embed+insert silently (N × ~30 s on a slow CPU)
            # and could exceed the watchdog window with no output —
            # false-tripping it. The KG path (`sync_node`) already prints
            # per chunk; this mirrors that on the docs path so the
            # window's "no-output ⇒ wedge" assumption holds on both.
            # `flush=True` guarantees the line is emitted even if stdout
            # isn't running unbuffered (the launcher exports
            # PYTHONUNBUFFERED, but a direct-CLI run might not).
            print(
                f"   ✓ Stored chunk {i + 1}/{len(chunks)}",
                flush=True,
            )
        print(f"   ✓ Stored {len(chunks)} chunks (vectors={sorted(last_slots)})", flush=True)
        return True
    except Exception as e:
        import traceback
        print(f"❌ Error syncing doc: {e}")
        traceback.print_exc()
        return False


def _delete_doc_by_file_path(server: WeaviateMCPServer, file_path_value: str) -> int:
    """File_path-scoped dev-collection cleanup (v0.2.70 FIX #1).

    Mirror of :func:`_delete_node_by_file_path` for the development
    collection. ``file_path`` is unique per doc, so this never collides
    with an active sibling the way the title-scoped delete did.
    """
    if not DEV_COLLECTION_NAME:
        return 0
    try:
        coll = server.client.collections.get(DEV_COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=Filter.by_property("file_path").equal(file_path_value),
            limit=100,
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


def sync_all_docs(server: WeaviateMCPServer) -> Tuple[int, int]:
    """Walk DOCS_ROOT and sync every .md to the dev collection."""
    if not DEV_COLLECTION_NAME:
        print("ℹ️  DEVELOPMENT_COLLECTION not set — skipping dev sync")
        return (0, 0)
    if not DOCS_ROOT.exists():
        print(f"ℹ️  No docs/ at {DOCS_ROOT} — skipping")
        return (0, 0)
    md_files = list(DOCS_ROOT.rglob("*.md"))
    total = len(md_files)
    print(f"📚 Found {total} markdown files in docs/")
    success = fail = 0
    # v0.2.70 FIX C: running "doc M/N" counter (flush=True), same rationale as
    # sync_all_nodes — visibility for a long re-embed, no watchdog/timeout.
    for idx, md in enumerate(sorted(md_files), start=1):
        print(f"[{idx}/{total}] {md.name}", flush=True)
        if sync_doc(server, md):
            success += 1
        else:
            fail += 1
        print(f"  → progress: {idx}/{total} docs processed "
              f"({success} ok, {fail} failed)", flush=True)
    return success, fail


def infer_tags_from_typed_links(server: WeaviateMCPServer, node_data: Dict) -> List[str]:
    """
    Infer tags from typed relationships BEFORE storing to Weaviate.

    Inference rules:
    1. Inherit capability tags from used/implemented tools
    2. Propagate domain tags through relationships

    Args:
        server: Weaviate MCP server instance
        node_data: Parsed node data with typed_links

    Returns:
        List of inferred tags
    """
    typed_links = node_data.get("typed_links", [])
    existing_tags = set(node_data.get("tags", []))
    inferred_tags = []

    if not typed_links:
        return inferred_tags

    # Relationship types that propagate properties
    CAPABILITY_RELATIONS = ["uses", "implements", "buildsOn"]
    TAG_RELATIONS = ["uses", "implements", "extends", "buildsOn"]

    try:
        collection = server.client.collections.get(COLLECTION_NAME)

        for link in typed_links:
            relation = link.get("relation_type", "")
            target_title = link.get("target_title", "")

            # Query target node
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(target_title) &
                       Filter.by_property("chunk_num").equal(1),
                limit=1,
                return_properties=["tags", "node_type"]
            )

            if not results.objects:
                continue

            target_props = results.objects[0].properties
            target_tags = target_props.get("tags", [])

            # Rule 1: Inherit capability tags from used/implemented tools
            if relation in CAPABILITY_RELATIONS:
                capability_tags = [t for t in target_tags if "-" in t]
                for cap in capability_tags:
                    if cap not in existing_tags and cap not in inferred_tags:
                        inferred_tags.append(cap)

            # Rule 2: Propagate domain tags through relationships
            if relation in TAG_RELATIONS:
                domain_tags = [t for t in target_tags if t.upper() == t or len(t) < 15]
                for tag in domain_tags:
                    if (tag not in existing_tags and
                        tag not in inferred_tags and
                        tag not in ["test", "project", "concept", "tool"]):
                        inferred_tags.append(tag)

    except Exception as e:
        # Inference is best-effort - don't fail sync if it errors
        pass

    return inferred_tags


def resolve_wikilinks_to_uuids(server: WeaviateMCPServer, wikilinks: List[str]) -> List[str]:
    """
    Resolve WikiLink titles to Weaviate UUIDs.

    Args:
        server: Weaviate MCP server instance
        wikilinks: List of WikiLink titles (e.g., ["Node Title 1", "Node Title 2"])

    Returns:
        List of UUIDs for matching nodes
    """
    if not wikilinks:
        return []

    try:
        collection = server.client.collections.get(COLLECTION_NAME)
        uuids = []

        for link_title in wikilinks:
            # Query for nodes with matching title (case-insensitive)
            # Note: For chunked nodes, we want the parent node, not chunks
            results = collection.query.fetch_objects(
                filters=Filter.by_property("title").equal(link_title) &
                       Filter.by_property("chunk_num").equal(1),  # Get first chunk (has full metadata)
                limit=1
            )

            if results.objects:
                uuids.append(str(results.objects[0].uuid))

        return uuids

    except Exception as e:
        print(f"    ⚠️  Could not resolve WikiLinks: {e}")
        return []


def _relative_file_path(file_path: Path) -> str:
    """Return the project-relative file_path string used as a node's
    Weaviate dedup key.

    MUST match the value stored in the ``file_path`` property by
    ``parse_markdown_node`` / ``parse_doc_file`` (``str(file_path.
    relative_to(PROJECT_ROOT))``) so a delete-by-file_path query hits the
    exact rows written for this file. Falls back to ``str(file_path)`` when
    the path isn't under PROJECT_ROOT (defensive — should not happen for
    files discovered under KNOWLEDGE_ROOT / DOCS_ROOT, but a symlinked or
    out-of-tree path shouldn't crash the cleanup).
    """
    try:
        return str(file_path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(file_path)


def _delete_node_by_file_path(server: WeaviateMCPServer, file_path_value: str) -> int:
    """Remove all Weaviate entries (incl. chunks) for a specific file_path.

    v0.2.70 FIX #1 (silent batch data loss): the archived-node cleanup
    previously deleted by ``title``. During a ``--all`` run, an archived node
    sharing its ``title`` with a DIFFERENT active node would delete the active
    node's rows too — so the active node silently vanished while ``sync_node``
    still returned True (counted as a success). ``file_path`` is unique per
    node, so scoping the cleanup to it removes only the archived file's own
    rows and never collides with an active sibling.

    Returns the number of objects deleted. Silent (returns 0) when the
    collection is missing or the connection is down — sync must not block
    on best-effort cleanup.
    """
    try:
        coll = server.client.collections.get(COLLECTION_NAME)
        existing = coll.query.fetch_objects(
            filters=Filter.by_property("file_path").equal(file_path_value),
            limit=100,
        )
        n = 0
        for obj in existing.objects:
            coll.data.delete_by_id(obj.uuid)
            n += 1
        return n
    except Exception:
        return 0


def _is_archived_node(file_path: Path, frontmatter: dict | None = None) -> tuple[bool, str]:
    """Decide whether a node should be excluded from Weaviate sync.

    Returns (is_archived, reason). A node is archived if either:
      - its filesystem path contains an `archive/` segment (knowledge/archive/...
        or any docs subtree), including the dot/underscore-prefixed
        conventions `.archive/` and `_archive/`, OR
      - its frontmatter `status` is `"archived"`, `"deprecated"`, or
        `"superseded"`.

    `superseded` was added 2026-05-22: nodes marked `status: superseded` (in
    favour of a canonical replacement) were silently still being synced to
    Weaviate because this check only recognised `archived|deprecated`. The
    cleanup pass that day discovered three such nodes still appearing in MCP
    results: `weaviate-usage-patterns`, `VLM_Prompt_Engineering_Best_Practices_2026`,
    `WD14_Tag_Rotation_Strategy`. Authors who write `status: superseded` mean
    "should disappear from KG queries"; honour that.

    v0.2.70 FIX #5: the path leg previously matched only the exact segment
    ``"archive"``. The widely-used dot/underscore variants ``.archive`` and
    ``_archive`` (Obsidian's hidden-folder convention; common ``_archive/``
    layouts) slipped through and got indexed. We now match those three exact
    segment forms. We deliberately do NOT do a substring match — that would
    wrongly skip legitimate dirs like ``architecture/`` or ``archived-specs/``.

    Archived nodes are kept on disk (so future-anyone can grep / read history)
    but skipped on Weaviate sync — they shouldn't return from KG queries.
    The `_stale_filter()` at query time provides a second layer (in case an
    archived node slips through with a real `valid_until` in the past), but
    upstream skipping is the cleaner default: it keeps the index lean and
    avoids paying embedding cost for content that won't surface.
    """
    parts = file_path.parts
    # Exact segment match for `archive`, `.archive`, `_archive` — NOT a
    # substring match (would catch `architecture/`, `archived-notes/`).
    _ARCHIVE_DIR_SEGMENTS = {"archive", ".archive", "_archive"}
    archive_hit = next((p for p in parts if p in _ARCHIVE_DIR_SEGMENTS), None)
    if archive_hit is not None:
        return True, f"path contains {archive_hit!r} segment ({file_path})"
    if frontmatter is not None:
        status = (frontmatter.get("status") or "").strip().lower()
        if status in ("archived", "deprecated", "superseded"):
            return True, f"frontmatter status={status!r}"
    return False, ""


# NEW-11 (2026-05-28): normalize typed_links to list-of-objects before any
# Weaviate insert.  Pre-canonicalization writers emitted list-of-strings in
# "relation::target" form; Weaviate's gRPC serializer cannot pack
# []interface{} and raises "creating primitive value for typed_links: proto:
# invalid type: []interface {}" which crashes the whole iterator.
#
# Canonical shape: [{"relation_type": str, "target_title": str}, ...]
#
# Three cases handled:
#   • list-of-objects with correct keys  → returned unchanged
#   • list-of-strings ("rel::target")    → parsed and converted
#   • anything else (single str, None…)  → warning logged, field dropped
def _normalize_typed_links(typed_links: object, context: str = "") -> list:
    """Return typed_links in the canonical list-of-objects shape.

    Args:
        typed_links: raw value from node_data (any type coming from disk).
        context: description of the node being written (for warning messages).

    Returns:
        list of {"relation_type": str, "target_title": str} dicts (may be empty).
    """
    if not typed_links:
        # None, [], empty string — treat as empty; no warning needed
        return []

    if not isinstance(typed_links, list):
        print(
            f"   ⚠ typed_links: unexpected type {type(typed_links).__name__!r} "
            f"for {context!r} — dropping field to avoid gRPC crash"
        )
        return []

    normalized: list = []
    for item in typed_links:
        if isinstance(item, dict):
            # Canonical shape — validate required keys are present
            if "relation_type" in item and "target_title" in item:
                normalized.append(item)
            else:
                print(
                    f"   ⚠ typed_links item missing required keys {list(item.keys())!r} "
                    f"for {context!r} — skipping item"
                )
        elif isinstance(item, str):
            # Legacy "relation::target" string form — parse and convert
            if "::" in item:
                relation, _, target = item.partition("::")
                normalized.append({"relation_type": relation.strip(), "target_title": target.strip()})
            else:
                # Plain string with no separator — treat as relatedTo
                print(
                    f"   ⚠ typed_links string {item!r} has no '::' separator "
                    f"for {context!r} — storing as relatedTo"
                )
                normalized.append({"relation_type": "relatedTo", "target_title": item.strip()})
        else:
            print(
                f"   ⚠ typed_links item type {type(item).__name__!r} unexpected "
                f"for {context!r} — skipping item"
            )
    return normalized


def sync_node(server: WeaviateMCPServer, file_path: Path) -> bool:
    """
    Sync a single knowledge node to Weaviate (with chunking support)

    Args:
        server: Weaviate MCP server instance
        file_path: Path to markdown file

    Returns:
        True if successful
    """
    start_time = time.time()
    chunks_created = 0
    error_msg = None

    try:
        if not file_path.exists():
            print(f"❌ File not found: {file_path}")
            error_msg = "File not found"
            return False

        # Skip archived nodes — see _is_archived_node docstring. Do this
        # BEFORE the timestamp-update side effect so editing an archived
        # node doesn't bump its `updated:` field for no reason.
        archived, reason = _is_archived_node(file_path)
        if archived:
            print(f"⊘ Skipping archived node: {reason}")
            # If it was previously synced (archived after sync), drop it
            # from Weaviate so stale content stops surfacing.
            # v0.2.70 FIX #1: delete by file_path (unique per node), NOT by
            # title — a title-scoped delete here silently removed any active
            # sibling sharing this archived node's title during a --all run.
            try:
                fp_value = _relative_file_path(file_path)
                removed = _delete_node_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior Weaviate entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            return True  # not a sync failure — intentional skip

        # Read, auto-update `updated:` timestamp, write back, then parse
        content = file_path.read_text(encoding='utf-8')
        content = _update_frontmatter_timestamp(file_path, content)
        node_data = parse_markdown_node(content, file_path)

        # Defence in depth: in case the path-based check missed a frontmatter-only
        # archive marker (e.g. status: archived but path doesn't contain 'archive/'),
        # check again after parsing. Same skip + delete behaviour.
        archived2, reason2 = _is_archived_node(file_path, frontmatter=node_data)
        if archived2:
            print(f"⊘ Skipping (frontmatter): {reason2}")
            # v0.2.70 FIX #1: delete by file_path (the stored dedup key), NOT
            # by title — see the path-based archive branch above for the
            # cross-node title-collision data-loss this prevents. node_data
            # carries the canonical relative file_path already.
            try:
                fp_value = node_data.get("file_path") or _relative_file_path(file_path)
                removed = _delete_node_by_file_path(server, fp_value)
                if removed:
                    print(f"  ↳ Removed {removed} prior Weaviate entry(ies) for '{fp_value}'")
            except Exception as e:
                print(f"  ↳ Could not remove prior Weaviate entry: {e}")
            return True

        # Validate against vocabulary (report warnings, don't block sync)
        validation_warnings = validate_node_against_vocabulary(node_data, file_path)
        if validation_warnings:
            print(f"⚠️  Vocabulary validation warnings ({len(validation_warnings)}):")
            for warning in validation_warnings[:3]:  # Show first 3
                print(f"   - {warning}")
            if len(validation_warnings) > 3:
                print(f"   ... and {len(validation_warnings) - 3} more warnings")

        # Run inference BEFORE storing (enrich with inferred tags)
        inferred_tags = infer_tags_from_typed_links(server, node_data)
        if inferred_tags:
            # Add inferred tags to node data (will be stored with original tags)
            node_data["tags"] = list(set(node_data["tags"] + inferred_tags))
            print(f"🧠 Inferred {len(inferred_tags)} tags from typed relationships")

        print(f"🔄 Syncing node: {node_data['title']} ({node_data['node_type']})")
        print(f"   Tags: {', '.join(node_data['tags']) if node_data['tags'] else 'none'}")
        total_links = len(node_data['links']) + len(node_data['typed_links'])
        typed_count = len(node_data['typed_links'])
        print(f"   Links: {total_links} connections ({typed_count} typed)")

        # v0.2.17 (plan 0.2): compute content-hash for embed-skip.
        # Uses the same signature function as the file-write skip
        # (_content_signature_excluding_updated), so a file whose only
        # delta is the `updated:` timestamp hashes identically to its
        # pre-sync state — exactly what we want for the no-op fast
        # path on every install.py --update.
        current_content_hash = _content_signature_excluding_updated(content)

        # Delete old version (by file_path)
        collection = server.client.collections.get(COLLECTION_NAME)

        # Query for existing nodes with same file_path
        where_filter = Filter.by_property("file_path").equal(node_data["file_path"])
        existing = collection.query.fetch_objects(
            filters=where_filter,
            limit=100,
            return_properties=["content_hash", "chunk_num", "total_chunks"],
        )

        # v0.2.17 (plan 0.2): EMBED-SKIP fast path. If every existing
        # object for this file_path has content_hash matching the
        # current source hash AND the count of objects matches what
        # we'd reproduce (single-chunk → 1, multi-chunk → N), skip
        # the delete-and-re-embed pipeline entirely. Saves hundreds
        # of Ollama embed calls + Weaviate roundtrips per re-sync
        # pass when content is unchanged.
        #
        # Conservative gating (Reviewer A finding E2 + original
        # design): skip ONLY when ALL existing objects' content_hash
        # matches AND at least one is non-empty AND the count of
        # existing objects matches the `total_chunks` recorded on
        # each (so a previous crash mid-chunk-write — leaving e.g.
        # 3/4 chunks with the new hash — does NOT cause a permanent
        # skip with missing chunk 4). If anything looks off, fall
        # through to the delete-and-re-embed path. Soft-fail: any
        # exception here also falls through.
        try:
            existing_hashes: List[str] = []
            existing_total_chunks: List[int] = []
            for obj in existing.objects:
                props = obj.properties or {}
                existing_hashes.append(props.get("content_hash", "") or "")
                # total_chunks may be int OR (legacy) missing/None.
                # Treat missing as 0 → forces fall-through.
                tc = props.get("total_chunks", 0)
                try:
                    existing_total_chunks.append(int(tc) if tc is not None else 0)
                except (TypeError, ValueError):
                    existing_total_chunks.append(0)

            chunk_count_ok = (
                len(existing_total_chunks) > 0
                and all(tc == len(existing_total_chunks) for tc in existing_total_chunks)
            )
            all_match = (
                len(existing_hashes) > 0
                and all(h == current_content_hash for h in existing_hashes)
                and all(h for h in existing_hashes)  # no empty strings
                and chunk_count_ok
            )
            if all_match:
                elapsed = time.time() - start_time
                print(
                    f"   ⏭️  Embed-skip: content_hash matches "
                    f"({current_content_hash[:12]}…); "
                    f"{len(existing_hashes)} chunk(s) preserved "
                    f"({elapsed*1000:.0f} ms)"
                )
                # Return success without delete/embed/insert. The
                # caller's success_count/fail_count tally still
                # counts this as a successful sync — the data is
                # already in Weaviate.
                return True
        except Exception as skip_err:  # noqa: BLE001 — soft-fail by design
            # Fall through to delete-and-re-embed. Log so future
            # debugging knows the fast path tried but didn't apply.
            print(f"   (embed-skip check failed: {skip_err}; re-embedding)")

        deleted_count = 0
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)
            deleted_count += 1

        if deleted_count > 0:
            print(f"   ✓ Deleted {deleted_count} old version(s)")

        # Check if content needs chunking
        token_count = TokenCounter.count_tokens(content)
        print(f"   Content size: {token_count} tokens")
        # v0.2.28: per-model chunk threshold instead of hardcoded 2500.
        _max_tokens = _max_chunk_tokens_for(server)

        if token_count <= _max_tokens:
            # Single chunk - store as-is
            print(f"   Storing as single object")

            # v0.2.70 Part 2: try a pre-shipped embedding FIRST (NO-OP this
            # release — no sidecar is shipped, so this returns None and we
            # compute below). Both guards (content_hash staleness + active-
            # slot match) live inside _shipped_vector_for. expected_chunks=1
            # for the single-object path.
            _shipped = _shipped_vector_for(
                server, KNOWLEDGE_ROOT, current_content_hash, expected_chunks=1
            )
            if _shipped is not None:
                vec_arg, slots_written = _shipped
                print(
                    f"   📦 Ingested shipped vector "
                    f"(slot={server.text_vector_slot}, no embed call)"
                )
            else:
                # v0.2.18: build vector arg via EmbeddingService. With
                # DUAL_EMBEDDING_ENABLED=true (default) this fans out to every
                # reachable text backend so multiple slots get populated.
                vec_arg, slots_written = _build_vector_arg(server, content)

            # Prepare data object
            data_obj = {
                "title": node_data["title"],
                "content": node_data["content"],
                "file_path": node_data["file_path"],
                "node_type": node_data["node_type"],
                "tags": node_data["tags"],
                "links": node_data["links"],
                # NEW-11 (2026-05-28): guard against legacy list-of-strings form
                # that crashes Weaviate gRPC serialiser with "invalid type:
                # []interface {}".  Canonical shape: list-of-objects.
                "typed_links": _normalize_typed_links(
                    node_data["typed_links"], context=node_data.get("title", "")
                ),
                "external_links": node_data["external_links"],  # External links (RDF)
                "created_at": node_data["created_at"],
                "updated_at": node_data["updated_at"],
                "chunk_num": 1,
                "total_chunks": 1,
                "source_node_id": str(uuid.uuid4()),
                # v0.2.17 (plan 0.2): persist content_hash so the next
                # re-sync can skip the embed pipeline when unchanged.
                "content_hash": current_content_hash,
            }

            # Add temporal metadata if present
            for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                if field in node_data:
                    data_obj[field] = node_data[field]

            # Insert into the configured named-vector slots. With multi-
            # slot writes (DUAL_EMBEDDING_ENABLED=true, the default since
            # v0.2.18) every reachable backend's vector lands in its own
            # slot — so a project switched from qwen3 → openai still has
            # qwen3_embed populated and search-with-qwen3 keeps working
            # during the transition.
            #
            # NEVER cross-write a vector under a name that implies a
            # different model: each backend's vectors go ONLY into the
            # slot whose embedding-space matches. The EmbeddingService
            # multi-slot fan-out enforces this — same model → same slot.
            #
            # Note for Weaviate 1.31+: `Reconfigure.NamedVectors.add()`
            # lets us add new named vectors after creation. The Wave A
            # `vco_lib.weaviate_schema.add_named_vector_slot` helper uses
            # this when available; the schema-creation block in
            # `ensure_collection_exists` declares every slot up-front for
            # older Weaviate versions where post-create adds aren't
            # supported.
            obj_uuid = collection.data.insert(
                properties=data_obj,
                vector=vec_arg
            )

            chunks_created = 1
            print(f"   ✓ Stored node with UUID: {str(obj_uuid)[:8]}... (vectors={sorted(slots_written)})")

            # Create cross-references for WikiLinks
            if node_data["links"]:
                target_uuids = resolve_wikilinks_to_uuids(server, node_data["links"])
                if target_uuids:
                    for target_uuid in target_uuids:
                        try:
                            collection.data.reference_add(
                                from_uuid=obj_uuid,
                                from_property="linksTo",
                                to=target_uuid
                            )
                        except Exception as e:
                            # Silently skip if reference already exists or target not found
                            pass
                    print(f"   ✓ Created {len(target_uuids)} cross-references")

        else:
            # Multiple chunks needed
            print(f"   ⚠️  Content exceeds {_max_tokens} tokens - chunking required")

            # Generate source_node_id for all chunks
            source_node_id = str(uuid.uuid4())

            # v0.2.28: per-model chunker preset instead of hardcoded
            # max_tokens=2500 (which was qwen3-specific). The same
            # `_max_tokens` value used in the gate above drives the
            # chunker's max — they MUST stay in sync.
            chunker = _chunker_for(server)

            chunks = chunker.chunk_text(
                text=content,
                source_id=source_node_id,
                metadata={
                    "title": node_data["title"],
                    "file_path": node_data["file_path"],
                    "node_type": node_data["node_type"]
                }
            )

            print(f"   Split into {len(chunks)} chunks")

            # Store each chunk
            last_slots: Mapping[str, List[float]] = {}
            _total_chunks = len(chunks)
            for i, chunk in enumerate(chunks):
                # v0.2.70 Part 2: per-chunk shipped-vector ingest (NO-OP this
                # release). Same guards as the single-object path. The shipped
                # entry must cover EXACTLY this node's chunk count; if the node
                # would chunk differently than the shipped vectors expect, every
                # chunk falls back to compute (the count guard is enforced
                # inside _shipped_chunk_vector, so we never mix shipped + freshly
                # computed chunk vectors for the same node).
                _shipped = _shipped_chunk_vector(
                    server,
                    KNOWLEDGE_ROOT,
                    current_content_hash,
                    chunk_num=chunk.chunk_number + 1,
                    expected_chunks=_total_chunks,
                )
                if _shipped is not None:
                    vec_arg, last_slots = _shipped
                else:
                    # v0.2.18: embed via EmbeddingService (multi-slot when
                    # DUAL_EMBEDDING_ENABLED — see _build_vector_arg).
                    vec_arg, last_slots = _build_vector_arg(server, chunk.content)

                # Prepare data object (tags, links, typed_links, external_links shared across all chunks)
                data_obj = {
                    "title": node_data["title"],
                    "content": chunk.content,
                    "file_path": node_data["file_path"],
                    "node_type": node_data["node_type"],
                    "tags": node_data["tags"],
                    "links": node_data["links"],
                    # NEW-11 (2026-05-28): same guard as single-chunk path above.
                    "typed_links": _normalize_typed_links(
                        node_data["typed_links"], context=node_data.get("title", "")
                    ),
                    "external_links": node_data["external_links"],  # External links (RDF)
                    "created_at": node_data["created_at"],
                    "updated_at": node_data["updated_at"],
                    "chunk_num": chunk.chunk_number + 1,  # 1-indexed
                    "total_chunks": chunk.total_chunks,
                    "source_node_id": source_node_id,
                    # v0.2.17 (plan 0.2): every chunk of the same file
                    # shares the same content_hash (computed over the
                    # whole file). The embed-skip check in sync_node
                    # requires ALL chunks for a file_path to carry an
                    # identical, non-empty hash before it skips —
                    # writing the same value here keeps that invariant.
                    "content_hash": current_content_hash,
                }

                # Add temporal metadata if present
                for field in ['created', 'updated', 'valid_from', 'valid_until', 'status']:
                    if field in node_data:
                        data_obj[field] = node_data[field]

                # Insert with the v0.2.18 multi-slot vector arg from
                # _build_vector_arg. See single-chunk path comment for
                # the rationale.
                obj_uuid = collection.data.insert(
                    properties=data_obj,
                    vector=vec_arg
                )

                # Create cross-references only from first chunk (represents the main node)
                if chunk.chunk_number == 0 and node_data["links"]:
                    target_uuids = resolve_wikilinks_to_uuids(server, node_data["links"])
                    if target_uuids:
                        for target_uuid in target_uuids:
                            try:
                                collection.data.reference_add(
                                    from_uuid=obj_uuid,
                                    from_property="linksTo",
                                    to=target_uuid
                                )
                            except Exception as e:
                                pass
                        print(f"   ✓ Created {len(target_uuids)} cross-references")

                chunks_created += 1
                # v0.2.69 FIX 3 (review SHOULD-FIX): per-chunk heartbeat
                # feeds the launcher's re-armed-per-line stall watchdog.
                # `flush=True` guarantees prompt emission even on a
                # direct-CLI run that doesn't inherit PYTHONUNBUFFERED.
                print(
                    f"   ✓ Stored chunk {chunk.chunk_number + 1}/{chunk.total_chunks} ({chunk.token_count} tokens)",
                    flush=True,
                )

            if last_slots:
                print(f"   ✓ All chunks written to vectors={sorted(last_slots)}")

        print(f"✅ Successfully synced {node_data['title']}")
        return True

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error syncing node: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Log usage
        if HAS_LOGGER:
            duration_ms = (time.time() - start_time) * 1000
            # v0.2.40 H1: stamp the resolved project name on the
            # tool_usage.jsonl row so per-event metadata matches the
            # KG / code-graph project identifier. Pre-fix the entry's
            # ``project`` field always fell back to the logger's
            # ``"claude-orchestrator"`` default, regardless of which
            # workspace the script ran in. Resolution is best-effort
            # via the canonical helper in ``vco_lib.paths`` — None
            # preserves the historic default downstream.
            try:
                from vco_lib.paths import resolve_project_name as _resolve_project_name
                _project = _resolve_project_name()
            except Exception:
                _project = None
            ToolUsageLogger.log_kg_sync(
                file_path=str(file_path),
                chunks_created=chunks_created,
                duration_ms=duration_ms,
                success=error_msg is None,
                error=error_msg,
                project=_project,
            )


def sync_all_nodes(server: WeaviateMCPServer) -> Tuple[int, int]:
    """
    Sync all knowledge graph markdown files

    Args:
        server: Weaviate MCP server instance

    Returns:
        (success_count, fail_count)
    """
    success_count = 0
    fail_count = 0

    # Find all .md files in knowledge/
    md_files = list(KNOWLEDGE_ROOT.rglob("*.md"))

    # Exclude meta files (schema/reference documentation, not searchable content)
    EXCLUDED_FILES = {'TAG_HIERARCHY.md', 'VOCABULARY.md'}
    md_files = [f for f in md_files if f.name not in EXCLUDED_FILES]

    total = len(md_files)
    print(f"📚 Found {total} markdown files in knowledge/")
    print()

    # v0.2.70 FIX C: emit a running "node M/N" counter (flush=True) so a long
    # full re-embed (e.g. an arctic model-swap over thousands of shared-KG
    # nodes) shows forward motion on install.py's inherited stdout — the cure
    # for "appears hung" is visibility, NOT a watchdog/timeout. Pure feedback:
    # no timer, no kill. Per-chunk heartbeats inside sync_node remain the
    # finer-grained signal for big single nodes.
    for idx, md_file in enumerate(sorted(md_files), start=1):
        print(f"[{idx}/{total}] {md_file.name}", flush=True)
        if sync_node(server, md_file):
            success_count += 1
        else:
            fail_count += 1
        print(f"  → progress: {idx}/{total} nodes processed "
              f"({success_count} ok, {fail_count} failed)", flush=True)
        print()  # Blank line between nodes

    return success_count, fail_count


def _classify_sync_target(raw: str) -> Tuple[Path, bool, bool]:
    """Classify an explicit sync-target path as knowledge / docs / neither.

    v0.2.70 FIX #6: a symlink physically located under ``docs/`` (or
    ``knowledge/``) whose TARGET lives outside the tree used to be rejected.
    The old code ran ``Path(raw).resolve()`` first — which rewrites a symlink
    to its out-of-tree target — then checked ``relative_to(DOCS_ROOT)``, so
    the file was reported "not in knowledge/ or docs/ — skipping".

    We classify by the path's LOCATION first. ``os.path.abspath`` normalises
    ``..`` / cwd lexically WITHOUT resolving the final component's symlink, so
    a link sitting under ``docs/`` is recognised by where the user placed it.
    The resolved form is checked as a fallback so a user who passes a path
    THROUGH a symlinked ancestor (e.g. a symlinked repo root) still matches.

    Returns ``(file_path, in_knowledge, in_docs)``. ``file_path`` is the
    location path when that form is in-tree (so the stored ``file_path``
    property reflects the docs/ location and ``read_text()`` follows the link
    to load content), otherwise the resolved path.
    """
    loc = Path(os.path.abspath(raw))
    try:
        resolved = Path(raw).resolve()
    except OSError:
        resolved = loc

    def _under(cand: Path, root: Path) -> bool:
        try:
            cand.relative_to(root)
            return True
        except ValueError:
            return False

    loc_in_knowledge = _under(loc, KNOWLEDGE_ROOT)
    loc_in_docs = _under(loc, DOCS_ROOT)
    res_in_knowledge = _under(resolved, KNOWLEDGE_ROOT)
    res_in_docs = _under(resolved, DOCS_ROOT)

    in_knowledge = loc_in_knowledge or res_in_knowledge
    in_docs = loc_in_docs or res_in_docs

    # Prefer the location path when it is itself in-tree; otherwise the
    # resolved path carried us in-tree (symlinked-ancestor case).
    if loc_in_knowledge or loc_in_docs:
        file_path = loc
    else:
        file_path = resolved

    return file_path, in_knowledge, in_docs


def main():
    """Main entry point.

    Routes by path:
      - file under knowledge/  → sync_node (KG collection)
      - file under docs/       → sync_doc (development collection)
      - --all                  → sync_all_nodes + sync_all_docs
      - --all-docs             → sync_all_docs only (dev collection bootstrap)
    """
    if len(sys.argv) < 2:
        print("Usage: sync_knowledge_graph.py <file_path>")
        print("       sync_knowledge_graph.py --all              (knowledge/ + docs/)")
        print("       sync_knowledge_graph.py --all-docs         (docs/ only)")
        print("       sync_knowledge_graph.py <f1> <f2> ...      (explicit file list)")
        sys.exit(1)

    embedding_service = None
    try:
        # v0.2.18: construct EmbeddingService at script entry. Probes all
        # configured backends once; raises NoEmbeddingBackendError when
        # zero are reachable (auto-writes ~/.claude/metrics/embedding_failures.jsonl
        # + .claude/context/EMBEDDING_FAILURES.md for Claude diagnostic).
        try:
            embedding_service = EmbeddingService.for_project(PROJECT_ROOT)
        except NoEmbeddingBackendError as e:
            # Soft-fail at the install seed boundary (same pattern as the
            # KG-summary "no backend available" deferral). Emit a deferral
            # entry so install.py can surface it via UPDATE_DEFERRED.md and
            # exit 0 — KG sync simply won't happen this run.
            _emit_sync_deferral_no_backend(PROJECT_ROOT, e)
            print(f"⚠️  KG sync skipped: {e}", file=sys.stderr)
            print("   See .claude/context/EMBEDDING_FAILURES.md + ~/.claude/metrics/embedding_failures.jsonl",
                  file=sys.stderr)
            sys.exit(0)

        # Initialize Weaviate client + bind to the embedding service
        server = WeaviateMCPServer(
            weaviate_url=WEAVIATE_URL,
            embedding_service=embedding_service,
            grpc_port=GRPC_PORT
        )

        # Ensure both collections exist (dev one only if env var set)
        if not ensure_collection_exists(server):
            print("❌ Cannot proceed without KG collection")
            sys.exit(1)
        ensure_dev_collection_exists(server)  # graceful no-op if env unset

        print()

        # Sync files
        if sys.argv[1] == "--all":
            kg_success, kg_fail = sync_all_nodes(server)
            doc_success, doc_fail = sync_all_docs(server)
            total_success = kg_success + doc_success
            total_fail = kg_fail + doc_fail
            print(f"📊 KG:   {kg_success} succeeded, {kg_fail} failed")
            print(f"📊 Docs: {doc_success} succeeded, {doc_fail} failed")
            sys.exit(0 if total_fail == 0 else 1)
        elif sys.argv[1] == "--all-docs":
            doc_success, doc_fail = sync_all_docs(server)
            print(f"📊 Docs: {doc_success} succeeded, {doc_fail} failed")
            sys.exit(0 if doc_fail == 0 else 1)
        elif len(sys.argv) > 2 or (len(sys.argv) == 2 and not sys.argv[1].startswith("--")):
            # v0.2.42 CI-10: accept a list of file paths as positional args.
            # When multiple files are given, sync only those files rather than
            # the full tree — used by install.py's content-hash diff gate to
            # sync only the files that changed since the last install.
            # Single-file path (the original behaviour) also falls through here
            # when it has no `--` prefix.
            raw_args = sys.argv[1:]
            success_count = 0
            fail_count = 0
            for raw in raw_args:
                file_path, in_knowledge, in_docs = _classify_sync_target(raw)
                if in_knowledge:
                    ok = sync_node(server, file_path)
                elif in_docs:
                    ok = sync_doc(server, file_path)
                else:
                    print(f"ℹ️  {raw}: not in knowledge/ or docs/ — skipping")
                    continue
                if ok:
                    success_count += 1
                else:
                    fail_count += 1

            if len(raw_args) > 1:
                print(f"📊 List: {success_count} succeeded, {fail_count} failed")
            sys.exit(0 if fail_count == 0 else 1)

    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        try:
            server.close()
        except Exception:
            pass
        if embedding_service is not None:
            try:
                embedding_service.close()
            except Exception:
                pass


def _emit_sync_deferral_no_backend(install_root: Path, exc: Exception) -> None:
    """Soft-fail deferral when no embedding backend is reachable at seed time.

    Adds an entry to ``<install_root>/.claude/context/UPDATE_DEFERRED.md``
    so install.py / the launcher can surface the issue. Idempotent (the
    DeferralReport uses last-write-wins per ``condition_id``). Soft-fail
    on any IO / import error — we're already in an error path.
    """
    try:
        # Import locally because vco_lib is on sys.path now (added at top
        # of file), but the deferral_report module isn't strictly needed
        # in the happy path — keep it lazy.
        from vco_lib.deferral_report import DeferralEntry, DeferralReport
        entry = DeferralEntry(
            condition_id="kg_sync_no_embedding_backend",
            title="KG sync skipped: no embedding backend reachable",
            detected=(
                "sync_knowledge_graph.py at install/seed time could not "
                "reach any configured embedding backend (Ollama / CodeEmbed / "
                f"OpenAI). Error: {exc}"
            ),
            why_deferred=(
                "Soft-fail policy: install must never block on transient "
                "service unavailability. Knowledge-graph search will be "
                "empty until the next sync run succeeds. See "
                "~/.claude/metrics/embedding_failures.jsonl for the "
                "per-backend diagnostic written by EmbeddingService."
            ),
            command_to_apply=(
                "# Restart embedding services then re-run the seed:\n"
                "podman start vco_ollama vco_code_embed   # or: docker start ...\n"
                "python templates/scripts/sync_knowledge_graph.py --all"
            ),
            severity="warning",
            kg_node_refs=[
                "knowledge/concepts/embedding-service-v0218.md",
            ],
        )
        report = DeferralReport.read(install_root)
        report.add_entry(entry)
        report.write(install_root)
    except Exception as inner:
        # Soft-fail — don't escalate. The failure JSONL written by
        # NoEmbeddingBackendError already captures the diagnostic.
        print(f"   (deferral emit failed: {inner})", file=sys.stderr)


if __name__ == "__main__":
    main()
