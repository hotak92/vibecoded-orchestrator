# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Claude Orchestrator Weaviate MCP Server

Semantic search and knowledge graph navigation for local projects.

Collections searched transparently based on env vars (KG_COLLECTION,
SHARED_KG_COLLECTION, DEVELOPMENT_COLLECTION) — agents don't need to
specify which collection to search.

Core Tools:
- hybrid_search: Combined semantic + keyword across KG + docs (use this first)
- semantic_graph_search: Semantic + WikiLink traversal (GraphRAG)
- get_node_connections: Navigate WikiLink relationships
- store_knowledge_node: Persist knowledge nodes
- search_code_graph: Find code entities by concept/purpose
- query_code_structure: Query dependencies, callers, inheritance, interactions

Connection:
- HTTP: localhost:8081 (configurable via WEAVIATE_URL)
- gRPC: localhost:50052 (configurable via GRPC_PORT)
- Ollama: localhost:11435 (configurable via OLLAMA_URL)
"""

import os
import sys
import json
import logging
import re
import asyncio
import uuid
from typing import Any, Optional, List, Dict
from pathlib import Path
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP
import weaviate
from weaviate.classes.query import Filter, MetadataQuery
import aiohttp

# Import Chunker for splitting large node content before embedding.
# Two import styles: relative (when used as a package) and direct (when run as script).
try:
    from .chunking import Chunker
except ImportError:
    from chunking import Chunker  # noqa: E402 — server.py run directly via python

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Default truncation limit in Claude Code is ~25K chars.
# v2.1.91+ supports _meta["anthropic/maxResultSizeChars"] override (up to 500K).
_MAX_RESULT_SIZE = 200_000  # 200K — generous but not wasteful


def _large_result(data: dict, indent: int = 2) -> str:
    """Serialize a dict to JSON for tool return.

    Use for tools that can return large payloads (hybrid_search detail=full,
    semantic_graph_search, search_code_graph with expand_hops, etc.).
    """
    return json.dumps(data, indent=indent)

# Initialize FastMCP server
mcp = FastMCP(
    "weaviate-kg",
    instructions=(
        "Semantic knowledge graph and code graph. "
        "ALWAYS call hybrid_search BEFORE using Grep or Read for conceptual, architectural, or pattern questions — "
        "it searches semantic embeddings across KG + project docs and finds results that literal grep cannot. "
        "Only fall back to Grep for exact literal strings (variable names, error messages). "
        "Tool order: hybrid_search (concepts) → semantic_graph_search (relationships) → "
        "search_code_graph (code by purpose) → query_code_structure (callers, deps, inheritance). "
        "store_knowledge_node persists new knowledge (scope='project' default, scope='shared' for cross-project)."
    )
)

# Global state
weaviate_client = None
WEAVIATE_URL = os.getenv("WEAVIATE_URL", "http://localhost:8081")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11435")

# RL training integration (transparent, best-effort)
# RL_SERVER_URL: HTTP endpoint of rl_server.py (MultiagentOrchestrator).
#   When reachable: nodes are reranked and cached; online training fires after each KG search.
#   When unreachable: MCP returns Weaviate-order top-k; no training.
RL_SERVER_URL = os.getenv("RL_SERVER_URL", "http://localhost:11439")
# Over-fetch multiplier: fetch this many × limit from Weaviate, pass all to RL server for reranking.
_RL_OVERFETCH = 2
# Per-process call counter — used to order calls within a session (maps seq → transcript position).
_rl_call_seq: int = 0
# KG search tool names as they appear in session transcripts (with and without mcp__ prefix).
_KG_SEARCH_TOOLS: frozenset[str] = frozenset({
    "hybrid_search", "semantic_graph_search",
    "mcp__weaviate-kg__hybrid_search",
    "mcp__weaviate-kg__semantic_graph_search",
})
# Monitor config: poll every N seconds, stop when answer window reaches this size OR a new
# human turn appears after the search.  Timeout is a hard ceiling.
_RL_MONITOR_POLL_INTERVAL: float = 2.0
_RL_MONITOR_ANSWER_THRESHOLD: int = 64_000   # chars
_RL_TOOL_CONTENT_LIMIT: int = 20_000         # per Write/Edit, chars
_RL_MONITOR_TIMEOUT: float = 600.0           # 10 min hard ceiling
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
EMBEDDING_SOURCE = os.getenv("EMBEDDING_SOURCE", "ollama")
# Legacy text embedding model (kept for backward compat — old named vectors stay populated)
LEGACY_TEXT_EMBEDDING_MODEL = os.getenv("LEGACY_TEXT_EMBEDDING_MODEL", "snowflake-arctic-embed2:latest")
# Dual-embedding support: when enabled, objects are stored with named vectors
# ("ollama_embed", "openai_embed") instead of a single flat vector.
# Enabled by default for fresh installs. Existing collections need migration
# first — see migrate_embeddings tool. Set to "false" to use legacy single-vector mode.
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"
# Active embedding for search queries:
#   KG: "qwen3" (default), "ollama" (legacy arctic), "openai"
#   Code: "codesage" (default), "ollama" (legacy jina), "openai"
ACTIVE_EMBEDDING = os.getenv("ACTIVE_EMBEDDING", "qwen3")
# OpenAI embedding config (only used when ACTIVE_EMBEDDING=openai or DUAL_EMBEDDING_ENABLED=true)
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Code embedding service URL (CodeSage-Large-v2 via FastAPI, or Ollama-compatible endpoint)
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# Named vector schemes for different collection types.
# Each scheme maps named-vector names to their dimension count.
# Old vectors (ollama_embed, ollama_code_embed) are preserved for backward compatibility
# and allow switching back to legacy models at any time.
VECTOR_SCHEMES: dict[str, dict[str, int]] = {
    "kg": {
        "qwen3_embed": 1024,    # qwen3-embedding:0.6b (NEW — active default)
        "ollama_embed": 1024,    # snowflake-arctic-embed2 (legacy, preserved)
        "openai_embed": 1536,    # text-embedding-3-small
    },
    "code": {
        "codesage_embed": 2048,    # CodeSage-Large-v2 via code embedding service (NEW — active default)
        "ollama_code_embed": 768,  # jina-embeddings-v2-base-code (legacy, preserved)
        "openai_embed": 1536,      # text-embedding-3-small
    },
}

# Collections that use the "code" vector scheme (all others default to "kg")
CODE_SCHEME_COLLECTIONS = {
    "CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction",
}
# B8 (2026-05-01): WEAVIATE_GRPC_PORT is canonical (VCT prefix convention,
# matches install.py:5179 .env key). GRPC_PORT is the legacy .claude/settings.json
# key (install.py:5301) kept as a read-time alias for back-compat. Prefer
# WEAVIATE_GRPC_PORT when both are set.
GRPC_PORT = int(os.getenv("WEAVIATE_GRPC_PORT", "").strip() or os.getenv("GRPC_PORT", "").strip() or "50052")

# Node formats sidecar — pre-generated descriptions/summaries for progressive disclosure.
# Loaded lazily on first use. Maps file_path → {title, description, summary, generated_at}.
KG_BASE_DIR = os.getenv("KG_BASE_DIR", "")
_node_formats_cache: dict | None = None
# Per-collection sidecar cache. Keys are collection names; values are the
# parsed sidecar dicts (or {} if no sidecar was found at the resolved path).
# This lets shared-KG results pull descriptions/summaries from the bundled
# vibecoded-orchestrator/knowledge/.node_formats.json while project results
# still use their own per-project sidecar.
_node_formats_by_collection: dict[str, dict] = {}


def _load_node_formats() -> dict:
    """Load the project-KG .node_formats.json sidecar. Cached after first load.

    Looks under KG_BASE_DIR/knowledge/ first, then the cwd-relative path. This
    is the legacy entry point used everywhere that doesn't carry a collection
    hint. For collection-aware lookups, prefer ``_load_node_formats_for_collection``.
    """
    global _node_formats_cache
    if _node_formats_cache is not None:
        return _node_formats_cache

    # Try KG_BASE_DIR/knowledge/.node_formats.json, then cwd-relative
    candidates = []
    if KG_BASE_DIR:
        candidates.append(os.path.join(KG_BASE_DIR, "knowledge", ".node_formats.json"))
    candidates.append(os.path.join(os.getcwd(), "knowledge", ".node_formats.json"))

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    _node_formats_cache = json.loads(fh.read())
                logger.info(f"Loaded node formats from {path} ({len(_node_formats_cache)} entries)")
                return _node_formats_cache
            except Exception as e:
                logger.warning(f"Failed to load node formats from {path}: {e}")

    _node_formats_cache = {}
    return _node_formats_cache


def _load_node_formats_for_collection(collection_name: str) -> dict:
    """Load the .node_formats.json sidecar appropriate for a given collection.

    Resolution rules:
      - Project KG (KG_COLLECTION) → uses the project sidecar (same as
        ``_load_node_formats``: KG_BASE_DIR/knowledge/.node_formats.json or
        cwd/knowledge/.node_formats.json).
      - Shared KG (SHARED_KG_COLLECTION) → uses the SHARED sidecar bundled
        with the orchestrator install. Resolution order:
          1. $SHARED_KG_NODE_FORMATS env override (absolute path, for tests).
          2. _SERVER_INFERRED_BASE/knowledge/.node_formats.json — the
             orchestrator's own knowledge/ directory shipped with this server.
      - Anything else (DEVELOPMENT_COLLECTION, code-graph collections, etc.)
        → empty dict; sidecar tiers don't apply.

    Caches per-collection to avoid re-reading on every result. Returns {} on
    miss / parse error so callers can safely treat the result as a dict.
    """
    if collection_name in _node_formats_by_collection:
        return _node_formats_by_collection[collection_name]

    candidates: list[str] = []
    if collection_name == KG_COLLECTION:
        # Same resolution as _load_node_formats — keep them in lockstep.
        if KG_BASE_DIR:
            candidates.append(os.path.join(KG_BASE_DIR, "knowledge", ".node_formats.json"))
        candidates.append(os.path.join(os.getcwd(), "knowledge", ".node_formats.json"))
    elif SHARED_KG_COLLECTION and collection_name == SHARED_KG_COLLECTION:
        env_override = os.getenv("SHARED_KG_NODE_FORMATS", "")
        if env_override:
            candidates.append(env_override)
        candidates.append(str(_SERVER_INFERRED_BASE / "knowledge" / ".node_formats.json"))
    # else: no sidecar resolution for unknown collections — fall through.

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.loads(fh.read())
                _node_formats_by_collection[collection_name] = data
                logger.info(
                    f"Loaded node formats for {collection_name} from {path} ({len(data)} entries)"
                )
                return data
            except Exception as e:
                logger.warning(f"Failed to load node formats from {path}: {e}")

    _node_formats_by_collection[collection_name] = {}
    return {}


def _log_detail_choice(query: str, detail: str, result_count: int) -> None:
    """Log the detail level chosen by the agent for RL training.

    Expansion signals:
    - titles: no bonus (agent didn't expand)
    - descriptions: small bonus (+0.3) — agent chose summary level
    - full: large bonus (+0.8) — agent chose full expansion
    """
    bonus_map = {"titles": 0.0, "descriptions": 0.3, "full": 0.8}
    log_entry = {
        "type": "retrieval_expansion",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "detail_level": detail,
        "result_count": result_count,
        "rl_bonus": bonus_map.get(detail, 0.0),
    }
    # Append to JSONL log (same dir as tool usage logs)
    log_dir = os.path.join(KG_BASE_DIR or os.getcwd(), ".claude", "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{datetime.now().strftime('%Y-%m-%d')}_retrieval_expansion.jsonl")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass  # Best-effort logging, never block search


def _get_node_format(file_path: str, level: str, collection_name: str | None = None) -> str | None:
    """Get a pre-generated format for a node.

    Args:
        file_path: Relative path (e.g., 'knowledge/tools/leanctx.md')
        level: 'description', 'summary', or 'chunk_summaries'
        collection_name: Optional collection the result came from. When given,
            the matching per-collection sidecar is consulted (project vs shared).
            When omitted, falls back to the legacy single-sidecar lookup so
            existing callers keep working.

    Returns:
        The formatted text (or dict for 'chunk_summaries'), or None if not available.
    """
    if collection_name:
        db = _load_node_formats_for_collection(collection_name)
        entry = db.get(file_path, {})
        val = entry.get(level) if isinstance(entry, dict) else None
        if val is not None:
            return val
        # If the collection-aware lookup misses but the legacy sidecar has it,
        # fall through. This covers the case where a result came from a
        # collection without sidecar support but file_path happens to map into
        # the project sidecar (rare but cheap to handle).
    db = _load_node_formats()
    entry = db.get(file_path, {})
    return entry.get(level) if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Score-driven retrieval verbosity tiers
# ---------------------------------------------------------------------------
# Thresholds calibrated 2026-04-10 on 18 relevant + 20 irrelevant queries
# (canonical source: previously inline in claude_mcp_servers/scripts/rl_kg_search.py).
#
# Tier semantics (RL-reranked or 1-distance score, range 0..1, higher=better):
#   < 0.42   → discard (noise from unrelated topics)
#   0.42..0.55 → "summary"      (LLM description from sidecar, or 200-char content)
#   0.55..0.65 → "single_chunk" (matched chunk, up to ~2000 chars)
#   0.65..0.75 → "three_chunks" (matched + neighbours, 3 chunks centred on hit)
#   >= 0.75  → "full"           (whole node, capped at 7 nearest chunks)
#
# Tunable at runtime via env-var overrides (kept as module constants so tests and
# rl_kg_search can override without monkey-patching imports).
_TIER_THRESHOLDS: dict[str, float] = {
    "min":          float(os.getenv("KG_TIER_MIN",          "0.42")),
    "single_chunk": float(os.getenv("KG_TIER_SINGLE_CHUNK", "0.55")),
    "three_chunks": float(os.getenv("KG_TIER_THREE_CHUNKS", "0.65")),
    "full":         float(os.getenv("KG_TIER_FULL",         "0.75")),
}

# Per-tier chunk window (how many chunks to assemble from a chunked node)
_TIER_CHUNK_WINDOW: dict[str, int] = {
    "single_chunk": 1,
    "three_chunks": 3,
    "full":         7,
}


def _get_result_verbosity_by_score(score: float) -> str:
    """Return one of: 'discard' | 'summary' | 'single_chunk' | 'three_chunks' | 'full'.

    Score is normalised 0..1, higher=better. See _TIER_THRESHOLDS for the cutoffs.
    """
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s < _TIER_THRESHOLDS["min"]:
        return "discard"
    if s < _TIER_THRESHOLDS["single_chunk"]:
        return "summary"
    if s < _TIER_THRESHOLDS["three_chunks"]:
        return "single_chunk"
    if s < _TIER_THRESHOLDS["full"]:
        return "three_chunks"
    return "full"


def _chunk_summaries_header(
    file_path: str,
    shown_chunk_nums: list[int] | None = None,
    collection_name: str | None = None,
) -> str:
    """Build a small header listing per-chunk summaries from the sidecar.

    Used when assembling partial multi-chunk content (single_chunk / three_chunks
    tiers) so the agent gets a one-line orientation of every chunk in the source
    node, even those not included in the assembled body.

    Args:
        file_path: Relative path (e.g. 'knowledge/concepts/foo.md')
        shown_chunk_nums: Chunk numbers actually being assembled below the header.
            When provided, the header marks shown chunks with ▶ and unshown with ·
            so the agent can request additional chunks deliberately.

    Returns:
        Header string ending in two newlines, or "" if no chunk_summaries exist.
    """
    if not file_path:
        return ""
    chunk_summaries = _get_node_format(file_path, "chunk_summaries", collection_name)
    if not isinstance(chunk_summaries, dict) or not chunk_summaries:
        return ""

    shown = set(shown_chunk_nums or [])
    lines = ["[Chunk map:"]
    # Keys are stringified ints "1", "2", … — sort numerically when possible.
    def _key(k: str) -> int:
        try:
            return int(k)
        except (TypeError, ValueError):
            return 0
    for k in sorted(chunk_summaries.keys(), key=_key):
        try:
            n = int(k)
        except (TypeError, ValueError):
            n = 0
        marker = "▶" if n in shown else "·"
        snippet = (chunk_summaries[k] or "").strip().splitlines()
        first_line = snippet[0] if snippet else ""
        if len(first_line) > 140:
            first_line = first_line[:137] + "…"
        lines.append(f"  {marker} {k}: {first_line}")
    lines.append("]")
    return "\n".join(lines) + "\n\n"


def _fetch_node_chunks(coll, title: str, hit_chunk: int, total: int, max_chunks: int):
    """Fetch up to ``max_chunks`` content chunks centred on ``hit_chunk``.

    Returns a list of (chunk_num, content) tuples sorted by chunk_num. Empty list
    on failure (collection unavailable, no chunks, etc.).

    Mirrors the inline ``_fetch_chunks`` previously embedded in rl_kg_search.py.
    """
    try:
        objs = coll.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=(total or max_chunks) + 1,
        )
        chunk_list: list[tuple[int, str]] = []
        for obj in objs.objects:
            cn = obj.properties.get("chunk_num", 0) or 0
            chunk_list.append((cn, obj.properties.get("content", "") or ""))
        chunk_list.sort(key=lambda x: x[0])
        if max_chunks >= len(chunk_list):
            return chunk_list
        # Centre window on hit_chunk
        hit_idx = next((i for i, (cn, _) in enumerate(chunk_list) if cn == hit_chunk), 0)
        half = max_chunks // 2
        start = max(0, min(hit_idx - half, len(chunk_list) - max_chunks))
        return chunk_list[start:start + max_chunks]
    except Exception as exc:
        logger.debug("Chunk fetch failed for '%s': %s", title, exc)
        return []


def _format_result_by_tier(
    result: dict,
    tier: str,
    sidecar_db: dict | None = None,
    coll=None,
) -> dict | None:
    """Format a single search result at the requested verbosity tier.

    Args:
        result: Result dict from _format_obj or merged hybrid_search (must include
            at minimum: title, node_type, file_path, content; optional: tags,
            score, chunk_number, total_chunks).
        tier: One of 'discard' | 'titles' | 'summary' | 'single_chunk' |
            'three_chunks' | 'full' | 'descriptions' (legacy alias for 'summary').
        sidecar_db: Optional pre-loaded sidecar dict. When omitted, the cached
            sidecar is used via _get_node_format. Pass an explicit dict in tests.
        coll: Optional Weaviate collection handle. Required for multi-chunk
            assembly (single_chunk / three_chunks / full tiers). When omitted,
            those tiers fall back to the 300-char content snippet.

    Returns:
        Formatted result dict, or None when tier == 'discard'.
    """
    if tier == "discard":
        return None

    fp = result.get("file_path", "") or ""
    title = result.get("title", "")
    node_type = result.get("node_type", "")
    tags = result.get("tags", []) or []
    score = result.get("score")
    content = result.get("content", "") or ""
    total_chunks = result.get("total_chunks") or 1
    hit_chunk = result.get("chunk_number") or 1
    # Source collection — set by _format_obj when known. Used to pick the
    # right sidecar (project vs shared) for descriptions/summaries/chunk maps.
    source_collection = result.get("source_collection") or result.get("collection")

    # Helper to read sidecar respecting an injected db override (used by tests).
    def _sc(level: str):
        if sidecar_db is not None:
            entry = sidecar_db.get(fp, {}) if isinstance(sidecar_db, dict) else {}
            return entry.get(level) if isinstance(entry, dict) else None
        return _get_node_format(fp, level, source_collection)

    base = {
        "title": title,
        "node_type": node_type,
        "file_path": fp,
        "tags": tags,
    }
    if score is not None:
        base["score"] = score
    # tier echoed back so callers (and tests) can see what was chosen.
    base["tier"] = tier

    if tier == "titles":
        return base

    if tier in ("summary", "descriptions"):
        # Decision: prefer description (longer, ~6 lines), fall back to summary
        # (1-2 lines), then to truncated content. This fixes BUG-SIDECAR-DESC-FALLBACK
        # — the old hybrid_search loop skipped 'summary' entirely.
        desc = _sc("description")
        summary = _sc("summary")
        if desc:
            base["description"] = desc
        elif summary:
            base["summary"] = summary
        else:
            base["content"] = content[:200] if len(content) > 200 else content
        return base

    # single_chunk / three_chunks / full — multi-chunk assembly when possible
    window = _TIER_CHUNK_WINDOW.get(tier, 1)
    chunks = _fetch_node_chunks(coll, title, hit_chunk, total_chunks, window) if coll is not None else []
    if chunks and total_chunks and total_chunks > 1:
        shown_nums = [cn for cn, _ in chunks]
        # Whole-node summary header when partial view (all-tiers below 'full' for multi-chunk).
        node_summary = _sc("summary")
        header_parts: list[str] = []
        is_partial = len(chunks) < (total_chunks or len(chunks))
        if is_partial and node_summary:
            header_parts.append(f"[Node summary: {node_summary}]")
        # Chunk map header for single_chunk/three_chunks (orient agent across full node).
        if tier in ("single_chunk", "three_chunks"):
            chunk_map = _chunk_summaries_header(
                fp, shown_chunk_nums=shown_nums, collection_name=source_collection
            )
            if chunk_map:
                # _chunk_summaries_header already trails with \n\n; strip for join below.
                header_parts.append(chunk_map.rstrip())
        body = "\n".join(c for _, c in chunks)
        prefix = ("\n\n".join(header_parts) + "\n\n") if header_parts else ""
        base["content"] = f"{prefix}{body}"
        base["chunks_shown"] = len(chunks)
        base["chunks_total"] = total_chunks
        return base

    # Single-chunk node (or coll unavailable) — return content as-is.
    if tier == "single_chunk":
        # For multi-chunk nodes where we couldn't fetch, prepend node summary if available.
        node_summary = _sc("summary") if (total_chunks and total_chunks > 1) else None
        if node_summary:
            base["content"] = f"[Node summary: {node_summary}]\n\n{content}"
        else:
            base["content"] = content
        return base

    # three_chunks / full fallback when chunk fetch failed: return full snippet.
    base["content"] = content
    return base


KG_COLLECTION = os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph")
# Cross-project shared collection. Defaults to "VibeCodedTools_KnowledgeGraph"
# (the bundled cross-project KG seeded at install time from
# vibecoded-orchestrator/knowledge/). Per-project opt-out via
# SHARED_KG_OPT_OUT=true (see below).
_SHARED_KG_DEFAULT = "VibeCodedTools_KnowledgeGraph"
_SHARED_KG_RAW = os.getenv("SHARED_KG_COLLECTION", _SHARED_KG_DEFAULT)
# Per-project opt-out: when true, the shared collection is treated as if
# unset for THIS process (no shared-collection queries, no shared writes via
# scope='shared' fallback). Default false (opt-in by default).
SHARED_KG_OPT_OUT = os.getenv("SHARED_KG_OPT_OUT", "").lower() in ("1", "true", "yes")
SHARED_KG_COLLECTION = "" if SHARED_KG_OPT_OUT else _SHARED_KG_RAW
# Project-specific documentation collection (e.g. ProjectName_development).
# When set, hybrid_search also searches this collection automatically.
# Auto-pairing convention: the launcher should set `KG_COLLECTION=Foo` AND
# `DEVELOPMENT_COLLECTION=Foo_development` together. We do NOT auto-derive
# here — `write_project_env_files` (Rust) and `_ensure_collections` (install.py)
# are the canonical writers; the server just reads. semantic_graph_search
# uses KG_COLLECTION only — docs have no WikiLinks so graph traversal can't
# find useful neighbors there.
DEVELOPMENT_COLLECTION = os.getenv("DEVELOPMENT_COLLECTION", "")
# Base directory for KG markdown files. When set, store_knowledge_node will
# write the .md file if it doesn't already exist (file_path is relative to this dir).
KG_BASE_DIR = os.getenv("KG_BASE_DIR", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Fallback base directory: the orchestrator project root inferred from this server's location.
# server.py lives at  <project>/claude_mcp_servers/weaviate_mcp/server.py
# so parent.parent.parent = <project>/
_SERVER_INFERRED_BASE: Path = Path(__file__).resolve().parent.parent.parent

# Valid subfolders inside knowledge/ — used for path auto-correction.
_KNOWLEDGE_SUBFOLDERS: frozenset[str] = frozenset({
    "concepts", "coordination", "hardware", "insights", "models", "notes",
    "patterns", "projects", "research", "techniques", "tools", "training", "user",
})
# Canonical node_type → knowledge subfolder mapping.
_NODE_TYPE_TO_FOLDER: dict[str, str] = {
    "project":       "projects",
    "concept":       "concepts",
    "tool":          "tools",
    "model":         "models",
    "hardware":      "hardware",
    "research":      "research",
    "coordination":  "coordination",
}


def _normalize_kg_file_path(file_path: str, node_type: str, title: str) -> tuple[str, list[str]]:
    """Auto-correct file_path so it always lands inside knowledge/.

    Returns (corrected_path, list_of_adjustments_made).
    Absolute paths are returned unchanged — they are assumed intentional.
    """
    adjustments: list[str] = []
    fp = (file_path or "").strip()

    # Empty path → derive from title and node_type
    if not fp:
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        folder = _NODE_TYPE_TO_FOLDER.get(node_type, "concepts")
        fp = f"knowledge/{folder}/{slug}.md"
        adjustments.append(f"derived from title+node_type → {fp}")
        return fp, adjustments

    # Absolute paths are intentional — leave them alone
    if Path(fp).is_absolute():
        return fp, adjustments

    # Ensure .md extension
    if not fp.endswith(".md"):
        fp += ".md"
        adjustments.append("added .md extension")

    parts = Path(fp).parts  # e.g. ("knowledge", "concepts", "foo.md")

    # Already rooted under knowledge/ with a known subfolder → trust it
    if parts[0] == "knowledge" and len(parts) >= 2 and parts[1] in _KNOWLEDGE_SUBFOLDERS:
        return fp, adjustments

    # Starts directly with a known knowledge subfolder (missing "knowledge/" prefix)
    if parts[0] in _KNOWLEDGE_SUBFOLDERS:
        fp = f"knowledge/{fp}"
        adjustments.append("prepended 'knowledge/' prefix")
        return fp, adjustments

    # Bare filename or unrecognised path → prepend knowledge/<node_type_folder>/
    folder = _NODE_TYPE_TO_FOLDER.get(node_type, "concepts")
    fp = f"knowledge/{folder}/{fp}"
    adjustments.append(f"prepended 'knowledge/{folder}/' from node_type={node_type!r}")
    return fp, adjustments


# Default project for code graph queries.
# Set PROJECT_NAME (or CODE_GRAPH_PROJECT) in .vscode/settings.json for each project.
# Priority: CODE_GRAPH_PROJECT env > PROJECT_NAME env.
# Must always be set — every project's .vscode/settings.json must include PROJECT_NAME.
CODE_GRAPH_PROJECT = os.getenv("CODE_GRAPH_PROJECT") or os.getenv("PROJECT_NAME", "")


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as Weaviate collection prefix."""
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def _code_collection(base: str) -> str:
    """Return per-project code graph collection name.

    Uses CODE_GRAPH_PROJECT env var as prefix. Falls back to bare name
    for backward compatibility if not set.
    """
    if CODE_GRAPH_PROJECT:
        prefix = _sanitize_collection_prefix(CODE_GRAPH_PROJECT)
        return f"{prefix}_{base}"
    return base


# Maximum approximate token count for a single Weaviate insert.
# qwen3-embedding supports 32k tokens but we keep a conservative 2000-token limit
# for chunk granularity (legacy snowflake-arctic-embed2 limit; also good for retrieval).
# 2000 tokens ≈ 8 000 chars (1 token ≈ 4 chars).
_MAX_SINGLE_CHUNK_TOKENS = 2000


def get_weaviate_client():
    """Get or create Weaviate client"""
    global weaviate_client
    if weaviate_client is None:
        http_host = WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0]
        http_port = int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8081

        weaviate_client = weaviate.connect_to_custom(
            http_host=http_host,
            http_port=http_port,
            http_secure=False,
            grpc_host=http_host,
            grpc_port=GRPC_PORT,
            grpc_secure=False
        )
        logger.info(f"✓ Connected to Weaviate at {WEAVIATE_URL}")
    return weaviate_client


async def get_ollama_embedding(text: str) -> list[float] | None:
    """Get embedding from Ollama using the active text model (qwen3-embedding by default, 1024-dim).

    Passes num_ctx=8192 to override Ollama's default 4096-token context window,
    which is too small for qwen3-embedding's actual 32k capacity.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={
                "model": EMBEDDING_MODEL,
                "prompt": text,
                "options": {"num_ctx": 8192},
            },
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={"model": LEGACY_TEXT_EMBEDDING_MODEL, "prompt": text},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    logger.warning("Legacy text embedding failed: HTTP %s", response.status)
                    return None
                data = await response.json()
                return data["embedding"]
    except Exception as e:
        logger.warning("Legacy text embedding error: %s", e)
        return None


async def get_openai_embedding(text: str) -> list[float] | None:
    """Get embedding from OpenAI API (1536-dim for text-embedding-3-small).

    Returns None if OPENAI_API_KEY is not set or if the request fails.
    """
    if not OPENAI_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": OPENAI_EMBEDDING_MODEL, "input": text},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    logger.warning("OpenAI embedding failed: %s", await response.text())
                    return None
                data = await response.json()
                return data["data"][0]["embedding"]
    except Exception as e:
        logger.warning("OpenAI embedding error: %s", e)
        return None


async def get_embedding(text: str) -> list[float] | None:
    """Get embedding using the active provider.

    Returns:
        - list[float]: Embedding vector
        - None: If using Weaviate's internal vectorizer
    """
    if EMBEDDING_SOURCE == "weaviate":
        return None  # Let Weaviate's text2vec module handle it

    if ACTIVE_EMBEDDING == "openai":
        vec = await get_openai_embedding(text)
        if vec:
            return vec
        # Fall back to Ollama if OpenAI fails
        logger.warning("OpenAI embedding failed, falling back to Ollama")

    return await get_ollama_embedding(text)


async def _get_both_embeddings(text: str) -> tuple[list[float] | None, list[float] | None]:
    """Get both Ollama (qwen3) and OpenAI embeddings concurrently.

    Returns (ollama_vec, openai_vec). Either may be None on failure.
    Legacy compatibility: the 'ollama_vec' here is now from qwen3-embedding.
    """
    ollama_vec, openai_vec = await asyncio.gather(
        get_ollama_embedding(text),
        get_openai_embedding(text),
    )
    return ollama_vec, openai_vec


async def _get_all_kg_embeddings(text: str) -> dict[str, list[float]]:
    """Get all KG embedding variants concurrently.

    Returns dict mapping named-vector names to their embeddings.
    Generates: qwen3_embed (new primary), ollama_embed (legacy), openai_embed.
    """
    qwen3_vec, legacy_vec, openai_vec = await asyncio.gather(
        get_ollama_embedding(text),         # qwen3-embedding (new primary)
        get_legacy_text_embedding(text),     # snowflake-arctic-embed2 (legacy)
        get_openai_embedding(text),
    )
    vectors: dict[str, list[float]] = {}
    if qwen3_vec:
        vectors["qwen3_embed"] = qwen3_vec
    if legacy_vec:
        vectors["ollama_embed"] = legacy_vec
    if openai_vec:
        vectors["openai_embed"] = openai_vec
    return vectors


async def _get_all_code_embeddings(text: str) -> dict[str, list[float]]:
    """Get all code embedding variants concurrently.

    Returns dict mapping named-vector names to their embeddings.
    Generates: codesage_embed (new primary), ollama_code_embed (legacy), openai_embed.
    """
    codesage_vec, legacy_vec, openai_vec = await asyncio.gather(
        get_code_embedding(text),            # CodeSage-Large-v2 (new primary)
        get_legacy_code_embedding(text),     # jina-v2-base-code (legacy)
        get_openai_embedding(text),
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

    Strips any project prefix (e.g. 'MultiagentOrchestrator_CodeFunction' -> 'CodeFunction')
    before checking CODE_SCHEME_COLLECTIONS.
    """
    # Strip project prefix: everything after last '_' that matches a known base name
    base = collection_name
    if "_" in collection_name:
        suffix = collection_name.rsplit("_", 1)[-1]
        # Check if suffix matches a code collection base name
        for code_coll in CODE_SCHEME_COLLECTIONS:
            if collection_name.endswith(code_coll):
                return "code"
    if base in CODE_SCHEME_COLLECTIONS:
        return "code"
    return "kg"


def _primary_named_vector(scheme: str) -> str:
    """Return the primary (first) named vector name for a scheme."""
    return next(iter(VECTOR_SCHEMES[scheme]))


async def _get_search_vector(text: str, scheme: str = "kg") -> tuple[list[float] | None, str]:
    """Get embedding for search, returns (vector, target_vector_name).

    Every collection on disk is named-vector — the DUAL-off branch was
    dead code (audit fix 2026-04-30). target_vector_name is always the
    slot name matching the model that produced the vector; never the
    empty string.

    ACTIVE_EMBEDDING controls which model is used for search:
      KG scheme:   "qwen3" (default) | "ollama" (legacy arctic) | "openai"
      Code scheme:  "codesage" (default) | "ollama" (legacy jina) | "openai"

    Args:
        text: Text to embed.
        scheme: 'kg' or 'code' — determines which model and target vector name.
    """
    if ACTIVE_EMBEDDING == "openai" and OPENAI_API_KEY:
        vec = await get_openai_embedding(text)
        if vec:
            return vec, "openai_embed"
        # Audit fix (2026-04-30): on openai failure, do NOT silently fall
        # through to qwen3/arctic below — that mixes embedding spaces and
        # surfaces poor results without any signal to the caller. Log the
        # failure and return None; the caller will see a clear error.
        logger.warning(
            "_get_search_vector: ACTIVE_EMBEDDING=openai but OpenAI call "
            "failed; refusing to fall back to legacy embedder (would "
            "produce results from a different vector space). Caller will "
            "receive None."
        )
        return None, ""

    if scheme == "code":
        if ACTIVE_EMBEDDING in ("codesage", "qwen3"):
            # Use new CodeSage model (default for code)
            vec = await get_code_embedding(text)
            target = "codesage_embed"
        else:
            # Legacy: Jina via Ollama
            vec = await get_legacy_code_embedding(text)
            target = "ollama_code_embed"
    else:
        if ACTIVE_EMBEDDING in ("qwen3", "codesage"):
            # Use new Qwen3-Embedding (default for KG)
            vec = await get_ollama_embedding(text)
            target = "qwen3_embed"
        else:
            # Legacy: Arctic via Ollama
            vec = await get_legacy_text_embedding(text)
            target = "ollama_embed"
    # NOTE: previously this returned (vec, "" if not DUAL_EMBEDDING_ENABLED).
    # The DUAL-off branch was dead code — every collection on disk is
    # named-vector — and `target_vector=""` would query an unnamed slot
    # that doesn't exist on dual-vector collections (audit fix, 2026-04-30).
    return vec, target


async def count_tokens_async(text: str) -> int:
    """
    Count tokens using Ollama qwen3.5:0.8b tokenizer.
    Falls back to character approximation (len // 4) if Ollama is unavailable.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OLLAMA_URL}/api/tokenize",
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
    """Get code embedding from the code embedding service (CodeSage-Large-v2, 2048-dim).

    Calls the FastAPI code embedding service which supports both GPU (sentence-transformers)
    and Ollama backends. Falls back to Ollama-compatible endpoint at CODE_EMBED_SERVICE_URL.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CODE_EMBED_SERVICE_URL}/api/embeddings",
                json={"model": "", "prompt": text},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["embedding"]
                else:
                    logger.error("Code embedding service failed: HTTP %s", response.status)
                    return None
    except Exception as e:
        logger.error("Code embedding service error: %s", e)
        return None


async def get_legacy_code_embedding(text: str) -> list[float] | None:
    """Get code embedding from legacy Jina model via Ollama (768-dim).

    Used to populate the old 'ollama_code_embed' named vector for backward compatibility.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OLLAMA_URL}/api/embeddings",
                json={
                    "model": "unclemusclez/jina-embeddings-v2-base-code:latest",
                    "prompt": text
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["embedding"]
                else:
                    logger.warning("Legacy code embedding failed: HTTP %s", response.status)
                    return None
    except Exception as e:
        logger.warning("Legacy code embedding error: %s", e)
        return None


def serialize_datetime(value):
    """Convert datetime objects to ISO format strings for JSON serialization"""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


_CHUNK_HEADER_RE = re.compile(r'^\[chunk (\d+)/(\d+)\]\n\n', re.MULTILINE)


def _parse_chunk_header(content: str) -> tuple[int, int] | None:
    """
    Parse '[chunk N/total]' prefix from stored content.

    Returns (chunk_number_1indexed, total_chunks) or None if not a chunk.
    """
    m = _CHUNK_HEADER_RE.match(content)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _format_obj(obj, collection_name: str, distance: float | None = None) -> dict:
    """
    Format a Weaviate object into the standard result dict.

    Parses '[chunk N/total]' prefix from content (if present) and exposes
    chunk_number, total_chunks, and source_id as first-class fields so callers
    can do reliable dedup and neighbour fetching without re-parsing the prefix.
    """
    content = obj.properties.get("content", "")
    dist = distance if distance is not None else (
        obj.metadata.distance if obj.metadata else None
    )
    title = obj.properties.get("title", "Untitled")

    # Parse chunk metadata — prefer schema properties, fall back to content prefix
    parsed = _parse_chunk_header(content)
    chunk_number = obj.properties.get("chunk_num") or (parsed[0] if parsed else None)
    total_chunks = obj.properties.get("total_chunks") or (parsed[1] if parsed else None)
    source_id = obj.properties.get("source_node_id") or title

    return {
        "title": title,
        "node_type": obj.properties.get("node_type", "unknown"),
        "content": content[:300] + "..." if len(content) > 300 else content,
        "tags": obj.properties.get("tags", []),
        "file_path": obj.properties.get("file_path", ""),
        "created_at": serialize_datetime(obj.properties.get("created_at", "")),
        "updated_at": serialize_datetime(obj.properties.get("updated_at", "")),
        "distance": dist,
        "collection": collection_name,
        # Chunk metadata (None for un-chunked nodes)
        "source_id": source_id,
        "chunk_number": chunk_number,
        "total_chunks": total_chunks,
    }


def _fetch_adjacent_chunks(coll, title: str, hit_num: int, total: int,
                           collection_name: str) -> list[dict]:
    """
    Fetch the chunk immediately before (hit_num-1) and after (hit_num+1) for
    the same source node identified by *title*.

    Prefers property-based filter (source_id + chunk_number) for efficiency.
    Falls back to title filter + content-prefix parsing for backward
    compatibility with objects that lack explicit chunk properties.

    Returns formatted result dicts with distance=None (exact neighbour fetch).
    """
    target_nums = set()
    if hit_num > 1:
        target_nums.add(hit_num - 1)
    if hit_num < total:
        target_nums.add(hit_num + 1)
    if not target_nums:
        return []

    neighbours = []

    # Strategy 1: Property-based filter (fast, exact) for new-format objects
    try:
        for target_num in target_nums:
            prop_filter = (
                Filter.by_property("source_node_id").equal(title)
                & Filter.by_property("chunk_num").equal(target_num)
            )
            result = coll.query.fetch_objects(filters=prop_filter, limit=1)
            for obj in result.objects:
                neighbours.append(_format_obj(obj, collection_name, distance=None))
    except Exception:
        # source_node_id / chunk_num properties may not exist on this collection;
        # fall through to content-prefix fallback below.
        pass

    # If property-based fetch found all targets, return early
    found_nums = {nb.get("chunk_number") for nb in neighbours}
    remaining = target_nums - found_nums
    if not remaining:
        return neighbours

    # Strategy 2: Fallback -- title filter + content-prefix parsing (old objects)
    try:
        all_objs = coll.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=total,
        )
        for obj in all_objs.objects:
            content = obj.properties.get("content", "")
            parsed = _parse_chunk_header(content)
            if parsed and parsed[0] in remaining:
                neighbours.append(_format_obj(obj, collection_name, distance=None))
    except Exception as exc:
        logger.debug("Adjacent chunk fetch failed for '%s': %s", title, exc)

    return neighbours


def _enrich_with_adjacent_chunks(coll, results: list[dict], collection_name: str) -> list[dict]:
    """
    For each chunked result in *results*, fetch adjacent chunks (N-1, N+1)
    and merge them into the list with deduplication by (title, chunk_number).

    Args:
        coll: Weaviate collection handle
        results: List of formatted result dicts (from _format_obj)
        collection_name: Collection name for formatting

    Returns:
        Combined list: original results + neighbour chunks, deduplicated.
    """
    seen: set[tuple[str, int | None]] = set()
    combined: list[dict] = []

    for r in results:
        key = (r.get("title", ""), r.get("chunk_number"))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    # Fetch neighbours for chunked hits
    for r in list(combined):
        cn = r.get("chunk_number")
        tc = r.get("total_chunks")
        if cn is not None and tc is not None:
            neighbours = _fetch_adjacent_chunks(
                coll, r["title"], cn, tc, collection_name
            )
            for nb in neighbours:
                nb_key = (nb.get("title", ""), nb.get("chunk_number"))
                if nb_key not in seen:
                    seen.add(nb_key)
                    combined.append(nb)

    return combined


def _rl_load_messages(transcript_path: "Path") -> list[dict]:
    """Load all JSONL messages from a transcript file."""
    messages: list[dict] = []
    try:
        with open(transcript_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return messages


def _rl_find_kg_positions(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (msg_idx, blk_idx) for every KG search tool_use block in the transcript."""
    positions: list[tuple[int, int]] = []
    for msg_idx, msg in enumerate(messages):
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        for blk_idx, block in enumerate(content):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in _KG_SEARCH_TOOLS
            ):
                positions.append((msg_idx, blk_idx))
    return positions


def _rl_extract_answer_window(
    messages: list[dict],
    start_msg_idx: int,
    start_blk_idx: int,
) -> tuple[str, bool]:
    """
    Extract text produced by Claude after the KG search at (start_msg_idx, start_blk_idx).

    Scans forward through the transcript collecting text/thinking blocks AND
    Write/Edit tool content until either:
      - A new human turn appears (= Claude stopped responding)   → complete=True
      - Accumulated text exceeds _RL_MONITOR_ANSWER_THRESHOLD   → complete=True (truncated)
      - End of transcript                                        → complete=False (still writing)

    Write/Edit inclusion: agents frequently write findings to files rather than
    explaining them in chat. Write includes the full ``content``; Edit includes
    only ``new_string`` (the added lines, not the removed context).

    VS Code transcripts use type="user" for both real human messages and tool results.
    A real human turn has message.role == "human"; tool results have toolUseResult set.
    Only real human turns signal that Claude has finished responding.

    Returns (text, complete).
    """
    parts: list[str] = []
    total_chars = 0
    for msg_idx in range(start_msg_idx, len(messages)):
        msg = messages[msg_idx]
        msg_type = msg.get("type", "")

        # A real human turn after the search = Claude finished responding.
        # VS Code transcripts use type="user" + role="user" for actual human messages;
        # some versions use role="human". Tool results also use type="user" but have
        # toolUseResult set — those are NOT stop signals.
        if (
            msg_type == "user"
            and msg_idx > start_msg_idx
            and msg.get("message", {}).get("role") in ("human", "user")
            and not msg.get("toolUseResult")
        ):
            return "".join(parts), True

        if msg_type != "assistant":
            continue

        content = msg.get("message", {}).get("content", [])
        for blk_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            # Skip blocks up to and including the search tool_use itself
            if msg_idx == start_msg_idx and blk_idx <= start_blk_idx:
                continue
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "")
                parts.append(text)
                total_chars += len(text)
            elif btype == "thinking":
                # Include thinking blocks (useful signal for RL)
                text = block.get("thinking", "")
                parts.append(text)
                total_chars += len(text)
            elif btype == "tool_use":
                # Include Write/Edit content — agents often write findings
                # to files instead of (or in addition to) the chat response.
                # Without this, nodes that were genuinely useful but whose
                # content was written to a file get zero reward signal.
                # Truncate to 20K chars per tool call to avoid budget blow-out;
                # max-over-chunks cosine means the relevant chunk still dominates.
                tool_name = block.get("name", "")
                tool_input = block.get("input", {})
                if tool_name == "Write":
                    text = tool_input.get("content", "")[:_RL_TOOL_CONTENT_LIMIT]
                    if text:
                        parts.append(text)
                        total_chars += len(text)
                elif tool_name == "Edit":
                    # Only new_string — the added content, not old_string
                    text = tool_input.get("new_string", "")[:_RL_TOOL_CONTENT_LIMIT]
                    if text:
                        parts.append(text)
                        total_chars += len(text)
            if total_chars >= _RL_MONITOR_ANSWER_THRESHOLD:
                return "".join(parts)[:_RL_MONITOR_ANSWER_THRESHOLD], True

    return "".join(parts), False


def _rl_find_all_transcripts() -> "list[Path]":
    """Return all .jsonl transcripts in the project slug dir, newest first."""
    from pathlib import Path as _Path
    projects_dir = _Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    slug = str(_SERVER_INFERRED_BASE).replace("/", "-")
    slug_dir = projects_dir / slug
    if not slug_dir.exists():
        return []
    return sorted(slug_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


async def _rl_answer_monitor(task_id: str, seq: int, query: str) -> None:
    """
    Background asyncio task: poll the session transcript until Claude's answer
    after the KG search is available, then POST to rl_server /rl_update.

    Firing condition (whichever comes first):
      - A new human turn appears after the search (= response complete)
      - Answer window exceeds _RL_MONITOR_ANSWER_THRESHOLD chars
      - Hard timeout _RL_MONITOR_TIMEOUT seconds

    The `seq` value is the 1-based call counter for this MCP process; it maps to
    the (seq-1)'th KG search position in the transcript (0-based rank).

    Works across parallel chats: scans all transcripts in the project slug dir and
    picks the one that contains this query at the expected seq position, preventing
    cross-contamination between simultaneously open VS Code windows.
    """
    from pathlib import Path as _Path

    deadline = asyncio.get_event_loop().time() + _RL_MONITOR_TIMEOUT
    pos_idx = seq - 1  # 0-based index into kg_positions list
    query_snippet = query[:120]  # used to verify we're reading the right transcript

    # Phase 1 + 2 combined: find the right transcript and poll for completion
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_RL_MONITOR_POLL_INTERVAL)

        # Scan all transcripts for the one that contains our query at pos_idx
        candidates = _rl_find_all_transcripts()
        # Also try CLAUDE_SESSION_ID fallback (CLI mode)
        if not candidates:
            session_id = os.getenv("CLAUDE_SESSION_ID", "")
            if session_id:
                projects_dir = _Path.home() / ".claude" / "projects"
                for f in sorted(projects_dir.rglob(f"{session_id}.jsonl")):
                    candidates = [f]
                    break

        for candidate in candidates:
            messages = _rl_load_messages(candidate)
            kg_positions = _rl_find_kg_positions(messages)

            # Find matching position by query fingerprint, scanning newest-first.
            # seq is used as a tiebreaker when the same query appears multiple times
            # (parallel chats): prefer the occurrence whose index from the end equals
            # (total_kg_calls_in_transcript - pos_idx - 1), i.e. the seq-th from the end.
            # Primary key: query match. Fallback to last match if seq doesn't align.
            matched_pos: "tuple[int,int] | None" = None
            if query_snippet:
                query_matches = []
                for i, (mi, bi) in enumerate(kg_positions):
                    msg = messages[mi]
                    blk = msg.get("message", {}).get("content", [])[bi]
                    blk_query = blk.get("input", {}).get("query", "") if isinstance(blk, dict) else ""
                    if query_snippet in blk_query or blk_query in query_snippet:
                        query_matches.append((i, mi, bi))
                if query_matches:
                    # seq-based tiebreak: prefer match whose absolute index == pos_idx
                    exact = [(i, mi, bi) for (i, mi, bi) in query_matches if i == pos_idx]
                    best = exact[0] if exact else query_matches[-1]  # fall back to last match
                    matched_pos = (best[1], best[2])
            elif pos_idx < len(kg_positions):
                matched_pos = kg_positions[pos_idx]

            if matched_pos is None:
                continue

            start_msg_idx, start_blk_idx = matched_pos
            # Right transcript — check if answer is complete
            answer, complete = _rl_extract_answer_window(messages, start_msg_idx, start_blk_idx)
            if complete and answer.strip():
                # POST to rl_server
                try:
                    payload = {"task_ids": [task_id], "agent_output": answer}
                    timeout = aiohttp.ClientTimeout(total=5.0)
                    async with aiohttp.ClientSession(timeout=timeout) as sess:
                        async with sess.post(f"{RL_SERVER_URL}/rl_update", json=payload) as resp:
                            if resp.status == 200:
                                logger.debug(
                                    "RL monitor %s: trained on %d chars (transcript %s)",
                                    task_id[:8], len(answer), candidate.name[:8],
                                )
                            else:
                                logger.debug("RL monitor %s: rl_update returned %d", task_id[:8], resp.status)
                except Exception as exc:
                    logger.debug("RL monitor %s: rl_update failed (%s)", task_id[:8], exc)
                return
            # Found the right transcript but answer not complete yet — stop scanning candidates
            break

    logger.debug("RL monitor %s: timed out after %.0fs", task_id[:8], _RL_MONITOR_TIMEOUT)


async def _rl_cache_and_rerank(
    task_id: str,
    query: str,
    all_nodes: list[dict],
    limit: int,
) -> list[dict]:
    """
    Rerank nodes via rl_server and spawn a background monitor for online training.

    Returns reranked top-k from rl_server, or the first `limit` nodes (Weaviate order)
    if the server is unreachable or returns an error.

    Spawns _rl_answer_monitor as an asyncio background task: it will poll the session
    transcript until Claude's answer is available, then POST to /rl_update for training.

    Tier gating: free tier skips the RL server entirely and returns Weaviate's cosine
    ordering. Pro/MAO tiers use RL reranking (requires rl_server running on port 11439
    from the separate orchestrator-rl repo, started by the launcher after activation).
    """
    # Feature gate: free tier → skip RL, return Weaviate order.
    try:
        from VCThelpers.license import feature_enabled
        if not feature_enabled("rl_retrieval"):
            logger.debug("RL retrieval gated off for current tier — using Weaviate order")
            return all_nodes[:limit]
    except ImportError:
        # VCThelpers not available (pure free install) → free tier behavior.
        return all_nodes[:limit]

    global _rl_call_seq
    _rl_call_seq += 1
    seq = _rl_call_seq

    # Spawn answer monitor (fire-and-forget, doesn't block Claude's response)
    asyncio.create_task(_rl_answer_monitor(task_id, seq, query))

    # Try rl_server for reranking.
    try:
        payload = {"task_id": task_id, "query": query, "nodes": all_nodes, "limit": limit}
        timeout = aiohttp.ClientTimeout(total=3.0)   # tight timeout — don't block Claude
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{RL_SERVER_URL}/cache_nodes", json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    top_k = data.get("top_k", [])
                    if top_k:
                        logger.debug("RL reranked %d→%d nodes for task %s", len(all_nodes), len(top_k), task_id[:8])
                        return top_k
    except Exception as exc:
        logger.debug("RL server unreachable (%s) — using Weaviate order", exc)

    # Fallback: Weaviate-distance order, sliced to limit.
    return all_nodes[:limit]


async def search_single_collection(collection_name: str, query: str, limit: int, filters=None) -> list:
    """
    Search a collection and return formatted results.

    For chunked nodes (content prefixed with '[chunk N/total]'), also fetches
    the immediately preceding and following chunks so callers receive full
    context without needing a second query.  Dedup is by (title, chunk_number).
    """
    try:
        client = get_weaviate_client()
        coll = client.collections.get(collection_name)

        # Search with near_vector (Ollama embeddings) or near_text (Weaviate vectorizer)
        if EMBEDDING_SOURCE == "weaviate":
            nv_kwargs = dict(query=query, limit=limit, return_metadata=["distance"])
            if filters:
                nv_kwargs["filters"] = filters
            response = coll.query.near_text(**nv_kwargs)
        else:
            vector, target_name = await _get_search_vector(query)
            nv_kwargs = dict(near_vector=vector, limit=limit, return_metadata=["distance"])
            if filters:
                nv_kwargs["filters"] = filters
            if target_name:
                nv_kwargs["target_vector"] = target_name
            response = coll.query.near_vector(**nv_kwargs)

        # Primary hits
        results: list[dict] = []
        seen: set[tuple[str, int | None]] = set()   # (title, chunk_number) dedup

        for obj in response.objects:
            formatted = _format_obj(obj, collection_name, obj.metadata.distance)
            key = (formatted["title"], formatted["chunk_number"])
            if key not in seen:
                seen.add(key)
                results.append(formatted)

        # Neighbour chunks for any chunked primary hits
        neighbour_candidates: list[dict] = []
        for r in list(results):
            if r["chunk_number"] is not None:
                neighbours = _fetch_adjacent_chunks(
                    coll, r["title"], r["chunk_number"], r["total_chunks"],
                    collection_name,
                )
                neighbour_candidates.extend(neighbours)

        for nb in neighbour_candidates:
            key = (nb["title"], nb["chunk_number"])
            if key not in seen:
                seen.add(key)
                results.append(nb)

        return results
    except Exception as e:
        logger.warning(f"Failed to search collection {collection_name}: {e}")
        return []


def _stale_filter(include_stale: bool = False):
    """Filter that excludes nodes whose `valid_until` is in the past.

    Returns a Weaviate `Filter` matching only nodes that are either:
      - missing `valid_until` (active by default), OR
      - have `valid_until` greater than now.

    Returns None when `include_stale=True` (no filter). The filter is
    applied AT QUERY TIME — before reranking, before result counting —
    so stale nodes never leave Weaviate. Tests, audit jobs, or research
    that genuinely needs archived nodes should pass `include_stale=True`.

    Why query-time and not post-fetch:
      - RL reranker would otherwise score stale candidates
      - `limit=N` would return fewer than N valid results after a Python pass
      - tier counts (auto-mode) would be wrong

    Schema requirement: the collection MUST be created with
    `inverted_index_config=Configure.inverted_index(index_null_state=True)`.
    Without that, the IsNull leg errors with "Nullstate must be indexed to
    be filterable!" — and Weaviate doesn't allow toggling that flag after
    creation, so collections created without it must be deleted and
    rebuilt. See `sync_knowledge_graph.py::ensure_collection_exists` and
    `analyze_code_graph.py::_inverted_index_config`.

    Pass a datetime object (not ISO string) — the Python client serializes
    to valueDate, which the date-typed property requires.
    """
    if include_stale:
        return None
    now = datetime.now(timezone.utc)
    return (
        Filter.by_property("valid_until").is_none(True)
        | Filter.by_property("valid_until").greater_than(now)
    )


@mcp.tool()
async def semantic_graph_search(
    query: str,
    limit: int = 5,
    depth: int = 2,
    detail: str = "auto",
    include_stale: bool = False,
) -> str:
    """
    Semantic search with WikiLink graph traversal (GraphRAG). Finds concepts
    related to the query AND their connected neighbors via typed WikiLinks
    (uses, implements, extends, buildsOn, relatedTo).

    Use when exploring how concepts relate to each other, tracing dependency
    chains, or understanding the broader context around a topic. Returns both
    primary matches and their graph neighbors.

    When to use: "what depends on X?", "what concepts are related to Y?",
    "show me the network around Z". Best for exploring connections.
    When NOT to use: simple factual lookups — use hybrid_search instead.

    Args:
        query: Natural language query describing the concept to explore
        limit: Max primary results (default: 5). Connected nodes are additional.
        depth: Graph traversal depth (default: 2, max: 3). Higher depth finds
               more distant connections but returns more results.
        detail: Verbosity tier per result (default "auto"). See hybrid_search
            for the full tier semantics. Auto-mode applies per-result score
            tiering to primary results. Connected nodes always use the
            "summary" tier — see Decision note below.

    Returns:
        JSON with primary_results (direct matches) + connected_nodes (graph
        neighbors discovered via WikiLink traversal). Each result carries title,
        file_path, node_type, score, tier, and content at the chosen detail.
    """
    client = get_weaviate_client()
    coll = client.collections.get(KG_COLLECTION)

    fetch_limit = limit * _RL_OVERFETCH

    # Stale-filter applied at query time, before RL rerank + result counting.
    stale = _stale_filter(include_stale=include_stale)

    # Determine all collections to search. Mirrors hybrid_search: project KG +
    # shared KG (when configured and not opted out). We do NOT include
    # DEVELOPMENT_COLLECTION here — graph traversal relies on WikiLinks which
    # are a knowledge-graph convention, not present in dev docs.
    collections_to_search: list[str] = [KG_COLLECTION]
    if SHARED_KG_COLLECTION and SHARED_KG_COLLECTION != KG_COLLECTION:
        collections_to_search.append(SHARED_KG_COLLECTION)

    # Per-collection handle cache (shared with the connected-node lookup below).
    coll_handles: dict[str, object] = {KG_COLLECTION: coll}

    def _coll_for(name: str):
        if not name:
            return None
        if name in coll_handles:
            return coll_handles[name]
        try:
            handle = client.collections.get(name)
            coll_handles[name] = handle
            return handle
        except Exception as exc:
            logger.debug("semantic_graph_search: collection '%s' unavailable (%s)", name, exc)
            coll_handles[name] = None
            return None

    # Run the semantic search across each collection and collect raw
    # ``(obj, collection_name)`` pairs so we can later rebuild WikiLinks from
    # the actual hit objects (regardless of source collection).
    all_formatted: list[dict] = []
    raw_primary: list[tuple[object, str]] = []
    for coll_name in collections_to_search:
        handle = _coll_for(coll_name)
        if handle is None:
            continue
        try:
            if EMBEDDING_SOURCE == "weaviate":
                nt_kwargs = dict(query=query, limit=fetch_limit, return_metadata=["distance"])
                if stale is not None:
                    nt_kwargs["filters"] = stale
                primary = handle.query.near_text(**nt_kwargs)
            else:
                vector, target_name = await _get_search_vector(query)
                nv_kwargs = dict(
                    near_vector=vector, limit=fetch_limit, return_metadata=["distance"]
                )
                if target_name:
                    nv_kwargs["target_vector"] = target_name
                if stale is not None:
                    nv_kwargs["filters"] = stale
                primary = handle.query.near_vector(**nv_kwargs)
        except Exception as exc:
            logger.warning(f"semantic_graph_search: error searching {coll_name}: {exc}")
            continue

        # Format all over-fetched results from this collection
        coll_formatted = [
            _format_obj(obj, coll_name, obj.metadata.distance)
            for obj in primary.objects
        ]
        coll_formatted = _enrich_with_adjacent_chunks(handle, coll_formatted, coll_name)
        all_formatted.extend(coll_formatted)
        for obj in primary.objects:
            raw_primary.append((obj, coll_name))

    # Preserve a normalised score (1 - distance) so per-result tiering works.
    for r in all_formatted:
        if "score" not in r:
            d = r.get("distance")
            r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

    # Sort merged candidates by score so RL sees a clean list (top-k semantics).
    all_formatted.sort(key=lambda x: x.get("score", 0.0), reverse=True)

    # RL: rerank + cache using all over-fetched nodes; return top-k primary results.
    task_id = str(uuid.uuid4())
    primary_results = await _rl_cache_and_rerank(task_id, query, all_formatted, limit)
    for r in primary_results:
        if "score" not in r:
            d = r.get("distance")
            r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

    # Apply tiering to primary results (mirrors hybrid_search behaviour).
    # Use per-result collection so chunk fetch and sidecar lookup go to the
    # right place when results come from the shared KG.
    legacy_aliases = {"descriptions": "summary"}
    primary_formatted: list[dict] = []
    for r in primary_results:
        if detail == "auto":
            tier = _get_result_verbosity_by_score(r.get("score", 0.0) or 0.0)
        else:
            tier = legacy_aliases.get(detail, detail)
        if tier == "discard":
            continue
        result_coll_name = r.get("collection") or KG_COLLECTION
        entry = _format_result_by_tier(
            r, tier, sidecar_db=None, coll=_coll_for(result_coll_name)
        )
        if entry is not None:
            primary_formatted.append(entry)

    # Extract WikiLinks only from the top-k returned to Claude. We sort the
    # raw_primary list by distance so the "top-k" heuristic is honoured even
    # though we merged across collections.
    raw_primary.sort(key=lambda pair: pair[0].metadata.distance if pair[0].metadata else 1.0)
    connected_titles = set()
    for obj, _src in raw_primary[:limit]:
        content = obj.properties.get("content", "")
        wikilinks = re.findall(r'\[\[(?:[^:]+::)?([^\]]+)\]\]', content)
        connected_titles.update(wikilinks)

    # Query connected nodes (exact title match — these are graph neighbours,
    # not search hits, so they have no native distance/score). Search BOTH
    # collections for each title; first hit wins (project KG takes priority
    # because it appears first in collections_to_search).
    connected_raw: list[dict] = []
    connected_seen_titles: set[str] = set()
    if connected_titles and depth > 1:
        for title in list(connected_titles)[:10]:
            for coll_name in collections_to_search:
                if title in connected_seen_titles:
                    break
                handle = _coll_for(coll_name)
                if handle is None:
                    continue
                try:
                    results = handle.query.fetch_objects(
                        filters=Filter.by_property("title").equal(title),
                        limit=1
                    )
                    if results.objects:
                        obj = results.objects[0]
                        formatted = _format_obj(obj, coll_name, distance=None)
                        # Per-collection chunk enrichment so adjacent chunks
                        # come from the right collection.
                        formatted = _enrich_with_adjacent_chunks(
                            handle, [formatted], coll_name
                        )[0]
                        connected_raw.append(formatted)
                        connected_seen_titles.add(title)
                except Exception as e:
                    logger.warning(f"Failed to fetch connected node '{title}' from {coll_name}: {e}")

    # Decision: connected nodes always render at "summary" tier.
    #
    # Auditing the cost of re-fetching each connected node via near_text/near_vector
    # to obtain a real score: that's an extra Weaviate roundtrip per neighbour
    # (≤10 in practice). On a warm GRPC connection that's ~30-80ms each →
    # +300-800ms latency for graph traversal that is meant to be cheap.
    # The neighbours are *already* selected by graph topology (they were
    # WikiLinked from a relevant primary), so a low semantic score does not
    # mean low relevance — it just means the title isn't lexically close to
    # the query. A flat "summary" tier is both faster and semantically more
    # honest.
    connected_nodes: list[dict] = []
    for r in connected_raw:
        # Connected nodes have no score; treat tier as "summary" unless the
        # caller explicitly requested "titles" or "full" globally (then mirror it).
        if detail in ("titles", "full"):
            tier = detail
        else:
            tier = "summary"
        result_coll_name = r.get("collection") or KG_COLLECTION
        entry = _format_result_by_tier(
            r, tier, sidecar_db=None, coll=_coll_for(result_coll_name)
        )
        if entry is not None:
            connected_nodes.append(entry)

    logger.info(
        f"semantic_graph_search: {len(primary_formatted)} primary + "
        f"{len(connected_nodes)} connected across {collections_to_search}"
    )
    return _large_result({
        "success": True,
        "primary_results": primary_formatted,
        "connected_nodes": connected_nodes,
        "query": query,
        "depth": depth,
        "detail": detail,
        "collections_searched": collections_to_search,
    })


async def _hybrid_search_single_collection(
    coll_name: str,
    query: str,
    fetch_limit: int,
    weaviate_filter,
    date_filter,
) -> dict:
    """Run hybrid (semantic + keyword) search on one collection, return combined dict keyed by (title, chunk)."""
    client = get_weaviate_client()
    coll = client.collections.get(coll_name)

    effective_filter = weaviate_filter
    if date_filter is not None:
        effective_filter = (effective_filter & date_filter) if effective_filter else date_filter

    # Semantic search
    if EMBEDDING_SOURCE == "weaviate":
        if effective_filter:
            semantic_results = coll.query.near_text(query=query, limit=fetch_limit, filters=effective_filter, return_metadata=["distance"])
        else:
            semantic_results = coll.query.near_text(query=query, limit=fetch_limit, return_metadata=["distance"])
    else:
        vector, target_name = await _get_search_vector(query)
        nv_kwargs = dict(near_vector=vector, limit=fetch_limit, return_metadata=["distance"])
        if effective_filter:
            nv_kwargs["filters"] = effective_filter
        if target_name:
            nv_kwargs["target_vector"] = target_name
        semantic_results = coll.query.near_vector(**nv_kwargs)

    # Keyword search
    if effective_filter:
        keyword_results = coll.query.bm25(query=query, limit=fetch_limit, filters=effective_filter, return_metadata=["score"])
    else:
        keyword_results = coll.query.bm25(query=query, limit=fetch_limit, return_metadata=["score"])

    semantic_formatted = [
        _format_obj(obj, coll_name, obj.metadata.distance)
        for obj in semantic_results.objects
    ]
    semantic_formatted = _enrich_with_adjacent_chunks(coll, semantic_formatted, coll_name)

    combined = {}
    for r in semantic_formatted:
        key = (r["title"], r.get("chunk_number"))
        combined[key] = {
            "title": r["title"],
            "node_type": r.get("node_type", "unknown"),
            "content": r.get("content", ""),
            "tags": r.get("tags", []),
            "file_path": r.get("file_path", ""),
            "collection": coll_name,
            "semantic_distance": r.get("distance") if r.get("distance") is not None else 1.0,
            "keyword_score": 0.0,
            "sources": ["semantic"],
            "chunk_number": r.get("chunk_number"),
            "total_chunks": r.get("total_chunks"),
            "source_id": r.get("source_id"),
        }

    for obj in keyword_results.objects:
        formatted_kw = _format_obj(obj, coll_name)
        key = (formatted_kw["title"], formatted_kw.get("chunk_number"))
        score = obj.metadata.score if hasattr(obj.metadata, 'score') else 0.0
        if key in combined:
            combined[key]["keyword_score"] = score
            combined[key]["sources"].append("keyword")
        else:
            combined[key] = {
                "title": formatted_kw["title"],
                "node_type": formatted_kw.get("node_type", "unknown"),
                "content": formatted_kw.get("content", ""),
                "tags": formatted_kw.get("tags", []),
                "file_path": formatted_kw.get("file_path", ""),
                "collection": coll_name,
                "semantic_distance": 1.0,
                "keyword_score": score,
                "sources": ["keyword"],
                "chunk_number": formatted_kw.get("chunk_number"),
                "total_chunks": formatted_kw.get("total_chunks"),
                "source_id": formatted_kw.get("source_id"),
            }

    for item in combined.values():
        sem_score = 1.0 - item["semantic_distance"]
        item["combined_score"] = (sem_score + item["keyword_score"]) / 2

    return combined


@mcp.tool()
async def hybrid_search(
    query: str,
    limit: int = 5,
    node_type: str = None,
    tags: list[str] = None,
    days: int = None,
    detail: str = "auto",
    include_stale: bool = False,
) -> str:
    """
    Combined semantic + keyword search across KG and project docs.
    Use this as the DEFAULT and FIRST search tool for any conceptual,
    architectural, pattern, or knowledge query. Do NOT use Grep or Read for
    conceptual questions — this tool searches semantic embeddings and finds
    results that literal string matching cannot.

    Automatically searches project KG, shared KG, and project docs. No need
    to specify collections — scoping is handled transparently via env vars.
    Pass days=N to filter by recency (replaces search_recent_work).

    When to use: asking "how does X work?", "what patterns exist for Y?",
    "what was decided about Z?", or any question about concepts, architecture,
    decisions, or project knowledge.

    When NOT to use: searching for exact literal strings like variable names,
    error messages, or specific file paths — use Grep for those instead.

    Args:
        query: Natural language query describing what you want to find
        limit: Max results to return (default: 5)
        node_type: Filter by type (project, concept, tool, model, hardware, research)
        tags: Filter by tags (e.g., ["AI", "python"])
        days: If set, only return nodes updated in the last N days
        detail: Verbosity tier per result. Default "auto" — selected per result by
            relevance score (calibrated thresholds, see _TIER_THRESHOLDS):
              - score < 0.42  → discarded (noise)
              - 0.42..0.55    → "summary" (LLM description, ~6 lines)
              - 0.55..0.65    → "single_chunk" (matched chunk, ~2000 chars)
              - 0.65..0.75    → "three_chunks" (matched + neighbours)
              - >= 0.75       → "full" (whole node, up to 7 nearest chunks)
            Explicit overrides apply uniformly to all results:
              - "titles"        → title + file_path + node_type only
              - "summary"       → LLM description / summary / 200-char content
              - "single_chunk"  → matched chunk only
              - "three_chunks"  → 3 chunks centred on hit
              - "full"          → whole node (or 300-char snippet for unchunked)
            Legacy aliases (kept for backward compat):
              - "descriptions"  → "summary"
              - (old) "full"    → unchanged behaviour, now also assembles chunks
                                  for chunked nodes when available

    Returns:
        JSON with deduplicated results ranked by combined semantic + keyword score.
        Each result includes title, file_path, node_type, score (0..1), tier (the
        verbosity actually applied), and content at the requested detail level.
    """
    # Build type/tag filter
    filters = []
    if node_type:
        filters.append(Filter.by_property("node_type").equal(node_type))
    if tags:
        for tag in tags:
            filters.append(Filter.by_property("tags").contains_any([tag]))

    # Stale-filter — exclude archived/expired nodes BEFORE rerank+counting.
    # Caller passes `include_stale=True` to disable (audits, history queries).
    stale = _stale_filter(include_stale=include_stale)
    if stale is not None:
        filters.append(stale)

    weaviate_filter = None
    if filters:
        weaviate_filter = filters[0]
        for f in filters[1:]:
            weaviate_filter = weaviate_filter & f

    # Optional date filter
    date_filter = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        date_filter = Filter.by_property("updated_at").greater_than(cutoff)

    fetch_limit = limit * _RL_OVERFETCH

    # Determine all collections to search
    collections_to_search: list[str] = [KG_COLLECTION]
    if SHARED_KG_COLLECTION and SHARED_KG_COLLECTION != KG_COLLECTION:
        collections_to_search.append(SHARED_KG_COLLECTION)
    if DEVELOPMENT_COLLECTION and DEVELOPMENT_COLLECTION not in collections_to_search:
        collections_to_search.append(DEVELOPMENT_COLLECTION)

    # Search all collections and merge by (title, chunk) key, keeping best score per key
    merged: dict = {}
    for coll_name in collections_to_search:
        try:
            coll_combined = await _hybrid_search_single_collection(
                coll_name, query, fetch_limit, weaviate_filter, date_filter
            )
            for key, item in coll_combined.items():
                if key not in merged or item["combined_score"] > merged[key]["combined_score"]:
                    merged[key] = item
        except Exception as e:
            logger.warning(f"hybrid_search: error searching {coll_name}: {e}")

    # Sort all over-fetched candidates by combined score
    all_results = sorted(merged.values(), key=lambda x: x["combined_score"], reverse=True)

    # Preserve combined_score → score (BUG-SCORE-DROP fix). RL server may
    # overwrite this with its own normalised score; if not, the merged
    # combined_score (already 0..1, higher=better) is used as the surface score.
    for r in all_results:
        if "score" not in r and "combined_score" in r:
            r["score"] = r["combined_score"]

    # RL: rerank + cache using all candidates; return top-k.
    task_id = str(uuid.uuid4())
    results = await _rl_cache_and_rerank(task_id, query, all_results, limit)

    # Ensure score survives the RL hop too (RL server returns its own dicts; if
    # it dropped the score field, fall back to combined_score from the input).
    for r in results:
        if "score" not in r:
            r["score"] = r.get("combined_score", 0.0)

    # Get collection handles for multi-chunk assembly. We need ONE handle per
    # source collection because results may come from KG_COLLECTION OR
    # SHARED_KG_COLLECTION; fetching chunks from the wrong collection returns
    # nothing and forces a snippet fallback. Cache handles to avoid repeat
    # client lookups inside the loop.
    coll_handles: dict[str, object] = {}
    try:
        client = get_weaviate_client()
    except Exception as exc:
        logger.debug("hybrid_search: weaviate client unavailable (%s)", exc)
        client = None

    def _coll_for(name: str):
        if not name or client is None:
            return None
        if name in coll_handles:
            return coll_handles[name]
        try:
            handle = client.collections.get(name)
            coll_handles[name] = handle
            return handle
        except Exception as exc:
            logger.debug("hybrid_search: collection '%s' unavailable (%s)", name, exc)
            coll_handles[name] = None
            return None

    # Apply detail level. "auto" → per-result tier from score; explicit value →
    # uniform across all results.
    formatted: list[dict] = []
    legacy_aliases = {"descriptions": "summary"}
    for r in results:
        if detail == "auto":
            score = r.get("score", 0.0) or 0.0
            tier = _get_result_verbosity_by_score(score)
        else:
            tier = legacy_aliases.get(detail, detail)
        # Decision: skip discarded results outright; the agent never sees noise.
        if tier == "discard":
            continue
        # Decision: when explicit detail == "full" was requested historically, the
        # behaviour was "300-char snippet". The new "full" tier additionally
        # assembles chunks for chunked nodes — strictly more useful, no regression
        # for unchunked nodes (still returns the snippet via the fallback path).
        # Pick the chunk-fetch collection from the result's source — without
        # this, shared-KG hits would fall back to snippet because their chunks
        # don't live in KG_COLLECTION.
        result_coll = r.get("collection") or KG_COLLECTION
        entry = _format_result_by_tier(r, tier, sidecar_db=None, coll=_coll_for(result_coll))
        if entry is not None:
            formatted.append(entry)
    results = formatted

    # Log detail level for RL training signal
    _log_detail_choice(query, detail, len(results))

    logger.info(f"hybrid_search: {len(results)} results (detail={detail}) for '{query}' across {collections_to_search}")
    return _large_result({
        "success": True,
        "query": query,
        "count": len(results),
        "detail": detail,
        "results": results,
        "collections_searched": collections_to_search,
        "methods_used": ["semantic", "keyword"]
    })


def get_node_connections(
    title: str
) -> str:
    """
    Extract WikiLink relationships for a specific node.

    Use for exploring specific node's connections and building knowledge maps.

    Args:
        title: Node title (exact match)

    Returns:
        JSON with typed connections (relationship_type, target_node)
    """
    client = get_weaviate_client()
    coll = client.collections.get(KG_COLLECTION)

    # Get node
    results = coll.query.fetch_objects(
        filters=Filter.by_property("title").equal(title),
        limit=1
    )

    if not results.objects:
        return json.dumps({
            "success": False,
            "error": f"Node '{title}' not found"
        }, indent=2)

    obj = results.objects[0]
    content = obj.properties.get("content", "")

    # Extract WikiLinks
    wikilinks_raw = re.findall(r'\[\[([^\]]+)\]\]', content)

    # Parse typed WikiLinks
    connections = []
    for link in wikilinks_raw:
        if "::" in link:
            rel_type, target = link.split("::", 1)
            connections.append({"type": rel_type, "target": target})
        else:
            connections.append({"type": "relatedTo", "target": link})

    logger.info(f"get_node_connections: {len(connections)} connections for '{title}'")
    return json.dumps({
        "success": True,
        "node": {
            "title": obj.properties.get("title", ""),
            "node_type": obj.properties.get("node_type", ""),
            "tags": obj.properties.get("tags", [])
        },
        "connections": connections,
        "connection_count": len(connections)
    }, indent=2)


@mcp.tool()
async def store_knowledge_node(
    title: str,
    content: str,
    node_type: str,
    tags: list[str],
    links: list[str],
    file_path: str = "",
    scope: str = "project",
) -> str:
    """
    Create/update a knowledge node.

    Args:
        title: Node title (unique per file)
        content: Full markdown content
        node_type: Type (project, concept, tool, model, hardware, research)
        tags: Tags without # (e.g., ["AI", "VRAM"])
        links: Typed WikiLinks in "relationshipType::Target" format
        file_path: Relative path from KG_BASE_DIR (e.g., "knowledge/concepts/VRAM_Management.md")
                   OR absolute path (e.g., "/home/user/project/knowledge/concepts/VRAM_Management.md").
                   Absolute paths work even when KG_BASE_DIR is not configured.
                   If omitted, path is auto-derived from title and node_type.
        scope: "project" (default) — writes to KG_COLLECTION (project-scoped).
               "shared" — writes to SHARED_KG_COLLECTION (cross-project knowledge).
               Falls back to KG_COLLECTION if SHARED_KG_COLLECTION is not configured.

    Returns:
        JSON with success status and file_written flag
    """
    try:
        client = get_weaviate_client()
        # Determine target collection based on scope
        target_collection_name = KG_COLLECTION
        if scope == "shared" and SHARED_KG_COLLECTION and SHARED_KG_COLLECTION != KG_COLLECTION:
            target_collection_name = SHARED_KG_COLLECTION
        collection = client.collections.get(target_collection_name)

        # --- Auto-correct file_path before anything else -------------------------
        # Fixes common mistakes: missing knowledge/ prefix, bare filenames,
        # missing .md extension, empty string.  Absolute paths are untouched.
        # -------------------------------------------------------------------------
        file_path, path_adjustments = _normalize_kg_file_path(file_path, node_type, title)
        if path_adjustments:
            logger.info(f"file_path adjusted: {path_adjustments}")

        # --- Resolve file paths BEFORE touching Weaviate -------------------------
        # md_path     : absolute Path used for disk I/O
        # rel_file_path: relative path stored in Weaviate (must stay relative so
        #               cleanup_orphaned_nodes.py and sync_knowledge_graph.py can
        #               resolve it against the knowledge/ directory)
        #
        # Priority for md_path:
        #   1. Absolute file_path → use directly
        #   2. Relative + KG_BASE_DIR → KG_BASE_DIR / file_path
        #   3. Relative + no KG_BASE_DIR → _SERVER_INFERRED_BASE / file_path
        #
        # rel_file_path: always relative (strip base prefix from absolute inputs).
        # -------------------------------------------------------------------------
        md_path: Optional[Path] = None
        rel_file_path: str = file_path  # default: use as-is if already relative

        if file_path:
            fp = Path(file_path)
            if fp.is_absolute():
                md_path = fp
                # Compute relative path for Weaviate storage
                base = Path(KG_BASE_DIR) if KG_BASE_DIR else _SERVER_INFERRED_BASE
                try:
                    rel_file_path = str(md_path.relative_to(base))
                except ValueError:
                    # Absolute path not under known base — store as-is (best effort)
                    rel_file_path = file_path
                    logger.warning(
                        f"Absolute file_path '{file_path}' is not under base '{base}'; "
                        f"storing absolute path in Weaviate (may affect cleanup script)"
                    )
            elif KG_BASE_DIR:
                md_path = Path(KG_BASE_DIR) / file_path
            else:
                md_path = _SERVER_INFERRED_BASE / file_path
                logger.info(
                    f"KG_BASE_DIR not set — using inferred project root: {md_path}"
                )
            # rel_file_path stays as file_path for relative inputs (already correct)

        # Delete existing (match by title)
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("title").equal(title),
            limit=100
        )
        for obj in existing.objects:
            collection.data.delete_by_id(obj.uuid)

        now = datetime.now(timezone.utc).isoformat()

        properties = {
            "title": title,
            "content": content,
            "file_path": rel_file_path,   # always relative — safe for cleanup/sync scripts
            "node_type": node_type,
            "tags": tags,
            "links": links,
            "created_at": now,
            "updated_at": now
        }

        # --- Chunk large content so every portion gets an accurate embedding ---
        # Small nodes (≤ _MAX_SINGLE_CHUNK_TOKENS ≈ 8 000 chars / 2000 tokens) are
        # stored as a single Weaviate object — identical to the previous behaviour.
        # Large nodes are split by Chunker; each chunk becomes its own object,
        # sharing the same title/tags/links/file_path metadata but carrying only
        # its slice of content.  The content field is prefixed with an ordering
        # header ("[chunk N/total]\n\n") so all chunks can be reassembled in order
        # without requiring schema changes in Weaviate.
        # -----------------------------------------------------------------------
        token_count = await count_tokens_async(content)

        if token_count <= _MAX_SINGLE_CHUNK_TOKENS:
            # Single-object insert — store explicit chunk properties for consistency
            properties["chunk_num"] = 1
            properties["total_chunks"] = 1
            properties["source_node_id"] = title
            if EMBEDDING_SOURCE == "weaviate":
                collection.data.insert(properties=properties)
            elif DUAL_EMBEDDING_ENABLED:
                vectors = await _get_all_kg_embeddings(content)
                collection.data.insert(
                    properties=properties,
                    vector=vectors if vectors else None,
                )
            else:
                vector = await get_embedding(content)
                collection.data.insert(properties=properties, vector=vector)
            chunk_count = 1
        else:
            # Multi-chunk insert: split then embed each chunk independently
            chunker = Chunker.for_model(EMBEDDING_MODEL)
            raw_chunks = chunker.chunk_text(content, source_id=title)
            chunk_count = len(raw_chunks)
            for chunk in raw_chunks:
                # Prefix stored content with ordering header (no schema changes needed)
                chunk_stored = (
                    f"[chunk {chunk.chunk_number + 1}/{chunk.total_chunks}]\n\n"
                    f"{chunk.content}"
                )
                chunk_props = dict(properties)
                chunk_props["content"] = chunk_stored
                # Explicit chunk properties for efficient retrieval
                chunk_props["chunk_num"] = chunk.chunk_number + 1   # 1-indexed
                chunk_props["total_chunks"] = chunk.total_chunks
                chunk_props["source_node_id"] = title
                if EMBEDDING_SOURCE == "weaviate":
                    collection.data.insert(properties=chunk_props)
                elif DUAL_EMBEDDING_ENABLED:
                    vectors = await _get_all_kg_embeddings(chunk.content)
                    collection.data.insert(
                        properties=chunk_props,
                        vector=vectors if vectors else None,
                    )
                else:
                    # Embed the raw chunk text (without header) for clean vectors
                    vector = await get_embedding(chunk.content)
                    collection.data.insert(properties=chunk_props, vector=vector)

        logger.info(
            f"✓ Stored '{title}' in {chunk_count} chunk(s) "
            f"(file_path in Weaviate: {rel_file_path})"
        )

        # --- Write / update .md file (upsert semantics) --------------------------
        # Write if file is new OR content has changed.
        # Prevents "Weaviate-only" nodes that would be lost on the next full sync.
        # -------------------------------------------------------------------------
        file_written = False
        file_write_error = None

        if md_path is not None:
            try:
                md_path.parent.mkdir(parents=True, exist_ok=True)
                existed = md_path.exists()
                current = md_path.read_text(encoding="utf-8") if existed else None
                if not existed or current != content:
                    md_path.write_text(content, encoding="utf-8")
                    file_written = True
                    action = "Updated" if existed else "Wrote"
                    logger.info(f"✓ {action} file '{md_path}'")
                else:
                    logger.info(f"File '{md_path}' unchanged — skipping write")
            except Exception as fe:
                file_write_error = str(fe)
                logger.warning(f"Failed to write file '{md_path}': {fe}")

        result = {
            "success": True,
            "action": "created",
            "title": title,
            "file_path": rel_file_path,
            "file_written": file_written,
            "chunks_stored": chunk_count,
        }
        if md_path is not None:
            result["absolute_path"] = str(md_path)
        if path_adjustments:
            result["path_adjustments"] = path_adjustments
        if file_write_error:
            result["file_error"] = file_write_error
        elif md_path is not None and not file_written:
            result["file_note"] = "file already up to date"
        elif md_path is None:
            result["file_note"] = "no file_path provided, Weaviate-only"
        return json.dumps(result, indent=2)

    except Exception as e:
        logger.error(f"Error storing node: {e}")
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


@mcp.tool()
async def search_code_graph(
    query: str,
    scope: str = "all",
    limit: int = 8,
    expand_hops: int = 0,
    layer: str = None,
    project: str = None,
    detail: str = "auto",
) -> str:
    """
    Find code entities (functions, classes, modules, APIs) by describing what
    they do in natural language. Searches semantic embeddings of code, not
    literal text — so "authentication middleware" finds auth-related functions
    even if they don't contain those exact words.

    Use this BEFORE grep when looking for code by purpose or concept. Use Grep
    only when you know the exact symbol name or string.

    When to use: "find the function that handles X", "where is Y implemented?",
    "what code deals with Z?". Best for discovering code by intent.
    When NOT to use: searching for exact function/variable names — use Grep.

    Args:
        query: Natural language description of the code you're looking for
        scope: "all" (default) — all entity types; "code" — functions/classes/modules
               only; "interaction" — service boundaries (APIs, cross-service calls) only
        limit: Max results (default: 8).
        expand_hops: 0 (default) — no expansion; 1 or 2 — follow call/interaction
                     edges from seed nodes to discover related code
        layer: Filter by architectural layer (API, Service, Data, UI, Utility)
        project: Project name override. Omit to use workspace default.
        detail: Verbosity per result (default "auto"):
            - "auto"   → top 4 (highest score) get full details, the rest are
                         metadata-only refs. Backward-compatible with the
                         pre-tiering behaviour.
            - "titles" → metadata-only refs for every result (cheapest)
            - "full"   → full details for every result (most expensive)
            Code graph has no .node_formats sidecar so the auto-mode tiering is
            position-based (top-k) rather than score-threshold-based; the score
            is still surfaced in every result for clients to filter on.

    Returns:
        JSON with code entities, each including file_path, score, and (for
        full-tier results) full_name/signature/doc. Metadata refs for the rest.
    """
    _SCOPES: dict[str, list[str]] = {
        "all":         ["CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction"],
        "code":        ["CodeFunction", "CodeClass", "CodeModule"],
        "interaction": ["CodeAPI", "CodeInteraction"],
    }

    # Resolve project: explicit arg > env default > no filter
    # Pass project="" to explicitly search all projects regardless of env default
    if project is not None:
        effective_project = project if project else None
    else:
        effective_project = CODE_GRAPH_PROJECT or None

    # Helper to get per-project collection name for a given project
    def _project_collection(base: str) -> str:
        if effective_project:
            prefix = _sanitize_collection_prefix(effective_project)
            return f"{prefix}_{base}"
        return base

    # Map base names to per-project collection names
    collections = [_project_collection(b) for b in _SCOPES.get(scope, _SCOPES["all"])]
    # Keep a reverse map for formatting (per-project name -> base name)
    _base_for = {_project_collection(b): b for b in _SCOPES.get(scope, _SCOPES["all"])}

    try:
        query_embedding = await get_code_embedding(query)
        if not query_embedding:
            return json.dumps({"success": False, "error": "Failed to generate query embedding"}, indent=2)

        client = get_weaviate_client()

        # Gather candidates from each collection
        candidates: list[dict] = []
        for coll_name in collections:
            try:
                coll = client.collections.get(coll_name)
                kwargs: dict = dict(
                    near_vector=query_embedding,
                    limit=limit,
                    return_metadata=MetadataQuery(distance=True, score=True),
                )
                # Use named vector target if dual embedding is enabled
                if DUAL_EMBEDDING_ENABLED:
                    if ACTIVE_EMBEDDING in ("qwen3", "codesage"):
                        kwargs["target_vector"] = "codesage_embed"
                    else:
                        kwargs["target_vector"] = "ollama_code_embed"
                # Build filters: project + optional layer
                active_filters = []
                if effective_project:
                    active_filters.append(Filter.by_property("project").equal(effective_project))
                if layer and _base_for.get(coll_name, coll_name) in ("CodeFunction", "CodeClass"):
                    active_filters.append(Filter.by_property("layer").equal(layer.lower()))
                if active_filters:
                    combined = active_filters[0]
                    for f in active_filters[1:]:
                        combined = combined & f
                    kwargs["filters"] = combined
                resp = coll.query.near_vector(**kwargs)
                for obj in resp.objects:
                    p = obj.properties
                    distance = obj.metadata.distance if (hasattr(obj.metadata, "distance") and obj.metadata.distance is not None) else 1.0
                    score = 1.0 - distance
                    # Store base name (e.g. "CodeFunction") for formatting, not per-project name
                    base_name = _base_for.get(coll_name, coll_name)
                    candidates.append({"_c": base_name, "_s": score, "_d": distance, "_p": p})
            except Exception as e:
                logger.warning(f"search_code_graph: {coll_name} failed: {e}")

        # Sort by score, take top `limit`
        candidates.sort(key=lambda x: x["_s"], reverse=True)
        candidates = candidates[:limit]

        def _file_path(coll_name: str, p: dict) -> str:
            """Best-effort source file path from stored properties."""
            # Explicit file_path property (present if analyzer stored it)
            if p.get("file_path"):
                return p["file_path"]
            # CodeModule has 'path' as the file path
            if p.get("path"):
                return p["path"]
            # CodeFunction/CodeClass: full_name encodes module path (dots), no explicit file
            if p.get("full_name"):
                parts = p["full_name"].split(".")
                # Drop last 1 part (function/method name) or last 2 (class.method)
                # Best effort: return module portion
                return "/".join(parts[:-1]) if len(parts) > 1 else p["full_name"]
            return ""

        # Determine per-result verbosity. Backward-compatible default ("auto")
        # mirrors the legacy "top 4 full / rest refs" heuristic. Explicit
        # detail values apply uniformly. Decision: code graph has no sidecar,
        # so we cannot do score-threshold tiering — we use position-based
        # tiering (rank order) for "auto" instead. The score is still in every
        # result for client-side filtering.
        if detail not in ("auto", "titles", "full"):
            # Map legacy/unknown values onto sane defaults rather than 500ing.
            detail = "auto"

        def _is_full_tier(idx: int) -> bool:
            if detail == "full":
                return True
            if detail == "titles":
                return False
            # auto: top 4 get full, rest get refs
            return idx < 4

        results = []
        for i, r in enumerate(candidates):
            coll_name, p, score, dist = r["_c"], r["_p"], r["_s"], r["_d"]
            base = {
                "collection": coll_name,
                "score": f"{score:.3f}",
                "distance": f"{dist:.3f}",
                "file_path": _file_path(coll_name, p),
            }

            if _is_full_tier(i):
                # Full details
                if coll_name == "CodeFunction":
                    doc = p.get("doc", "")
                    base.update({
                        "full_name": p.get("full_name", ""),
                        "signature": p.get("signature", ""),
                        "doc": doc[:200] + "..." if len(doc) > 200 else doc,
                        "location": f"{p.get('start_line','?')}-{p.get('end_line','?')}",
                        "is_async": p.get("is_async", False),
                    })
                elif coll_name == "CodeClass":
                    doc = p.get("doc", "")
                    base.update({
                        "full_name": p.get("full_name", ""),
                        "signature": p.get("signature", ""),
                        "doc": doc[:200] + "..." if len(doc) > 200 else doc,
                        "methods": p.get("methods", []),
                        "method_count": len(p.get("methods", [])),
                        "location": f"{p.get('start_line','?')}-{p.get('end_line','?')}",
                    })
                elif coll_name == "CodeModule":
                    summary = p.get("module_summary", "")
                    base.update({
                        "path": p.get("path", ""),
                        "language": p.get("language", ""),
                        "loc": p.get("loc", 0),
                        "summary": summary[:200] + "..." if len(summary) > 200 else summary,
                    })
                elif coll_name == "CodeAPI":
                    desc = p.get("api_description", "")
                    base.update({
                        "endpoint": p.get("endpoint", ""),
                        "method": p.get("method", ""),
                        "description": desc[:200] + "..." if len(desc) > 200 else desc,
                        "parameters": p.get("parameters", []),
                    })
                elif coll_name == "CodeInteraction":
                    desc = p.get("description", "")
                    base.update({
                        "interaction_type": p.get("interaction_type", ""),
                        "direction": p.get("direction", ""),
                        "protocol": p.get("protocol", ""),
                        "endpoint": p.get("endpoint", ""),
                        "confidence": p.get("confidence", ""),
                        "description": desc[:200] + "..." if len(desc) > 200 else desc,
                    })
            else:
                # Metadata-only ref for lower-ranked results
                if coll_name in ("CodeFunction", "CodeClass"):
                    base["full_name"] = p.get("full_name", "")
                elif coll_name == "CodeModule":
                    base["path"] = p.get("path", "")
                    base["language"] = p.get("language", "")
                elif coll_name == "CodeAPI":
                    base["endpoint"] = p.get("endpoint", "")
                    base["method"] = p.get("method", "")
                elif coll_name == "CodeInteraction":
                    base["endpoint"] = p.get("endpoint", "")
                    base["protocol"] = p.get("protocol", "")
                    base["interaction_type"] = p.get("interaction_type", "")

            results.append(base)

        # --- Subgraph expansion ---
        effective_hops = max(0, min(expand_hops, 2))
        if effective_hops > 0 and candidates:
            try:
                func_coll = client.collections.get(_project_collection("CodeFunction"))
                ix_coll = client.collections.get(_project_collection("CodeInteraction"))

                # Seed UUIDs and full_names from the initial vector query so we can
                # re-fetch with the references/properties needed for expansion.
                # We re-query by full_name / path to get UUIDs reliably.
                visited_full_names: set[str] = set()
                expansion_queue: list[tuple[str, str, int]] = []  # (coll_name, identifier, hop)

                for r in candidates:
                    coll_name, p = r["_c"], r["_p"]
                    if coll_name == "CodeFunction":
                        fn = p.get("full_name", "")
                        if fn:
                            visited_full_names.add(fn)
                            expansion_queue.append(("CodeFunction", fn, 0))
                    elif coll_name == "CodeModule":
                        path_val = p.get("path") or p.get("file_path", "")
                        if path_val:
                            expansion_queue.append(("CodeModule", path_val, 0))

                expanded_results: list[dict] = []

                for hop in range(1, effective_hops + 1):
                    next_queue: list[tuple[str, str, int]] = []

                    for coll_name, identifier, _prev_hop in expansion_queue:
                        if len(results) + len(expanded_results) >= limit:
                            break

                        if coll_name == "CodeFunction":
                            # 1. Follow outbound calls: fetch the function's `calls` text-array
                            try:
                                fn_filter = Filter.by_property("full_name").equal(identifier)
                                if effective_project:
                                    fn_filter = fn_filter & Filter.by_property("project").equal(effective_project)
                                fn_resp = func_coll.query.fetch_objects(
                                    filters=fn_filter,
                                    limit=1,
                                )
                                if fn_resp.objects:
                                    fn_obj = fn_resp.objects[0]
                                    called_names: list[str] = fn_obj.properties.get("calls") or []
                                    for callee_name in called_names:
                                        if callee_name in visited_full_names:
                                            continue
                                        if len(results) + len(expanded_results) >= limit:
                                            break
                                        # Fetch the callee node
                                        callee_resp = func_coll.query.fetch_objects(
                                            filters=Filter.by_property("full_name").equal(callee_name),
                                            limit=1,
                                        )
                                        if callee_resp.objects:
                                            cp = callee_resp.objects[0].properties
                                            expanded_results.append({
                                                "collection": "CodeFunction",
                                                "full_name": cp.get("full_name", callee_name),
                                                "signature": cp.get("signature", ""),
                                                "file_path": cp.get("file_path") or cp.get("path", ""),
                                                "expanded": True,
                                                "hop": hop,
                                            })
                                            visited_full_names.add(callee_name)
                                            next_queue.append(("CodeFunction", callee_name, hop))
                            except Exception as _e:
                                logger.debug(f"expand hop {hop} CodeFunction calls: {_e}")

                            # 2. Follow CodeInteraction edges where source_function → this function
                            try:
                                fn_resp2 = func_coll.query.fetch_objects(
                                    filters=Filter.by_property("full_name").equal(identifier),
                                    limit=1,
                                )
                                if fn_resp2.objects:
                                    src_uuid = str(fn_resp2.objects[0].uuid)
                                    ix_resp = ix_coll.query.fetch_objects(
                                        filters=Filter.by_ref("source_function").by_id().equal(src_uuid),
                                        limit=20,
                                    )
                                    for ix_obj in ix_resp.objects:
                                        if len(results) + len(expanded_results) >= limit:
                                            break
                                        ixp = ix_obj.properties
                                        ep = ixp.get("endpoint", "")
                                        key = f"ix:{ep}"
                                        if key in visited_full_names:
                                            continue
                                        visited_full_names.add(key)
                                        expanded_results.append({
                                            "collection": "CodeInteraction",
                                            "interaction_type": ixp.get("interaction_type", ""),
                                            "direction": ixp.get("direction", ""),
                                            "protocol": ixp.get("protocol", ""),
                                            "endpoint": ep,
                                            "file_path": ixp.get("file_path", ""),
                                            "expanded": True,
                                            "hop": hop,
                                        })
                            except Exception as _e:
                                logger.debug(f"expand hop {hop} CodeFunction interactions: {_e}")

                        elif coll_name == "CodeModule":
                            # Follow CodeInteraction edges where source_module → this module
                            try:
                                mod_coll = client.collections.get(_project_collection("CodeModule"))
                                mod_resp = mod_coll.query.fetch_objects(
                                    filters=Filter.by_property("path").equal(identifier),
                                    limit=1,
                                )
                                if mod_resp.objects:
                                    src_uuid = str(mod_resp.objects[0].uuid)
                                    ix_resp = ix_coll.query.fetch_objects(
                                        filters=Filter.by_ref("source_module").by_id().equal(src_uuid),
                                        limit=20,
                                    )
                                    for ix_obj in ix_resp.objects:
                                        if len(results) + len(expanded_results) >= limit:
                                            break
                                        ixp = ix_obj.properties
                                        ep = ixp.get("endpoint", "")
                                        key = f"mod_ix:{identifier}:{ep}"
                                        if key in visited_full_names:
                                            continue
                                        visited_full_names.add(key)
                                        expanded_results.append({
                                            "collection": "CodeInteraction",
                                            "interaction_type": ixp.get("interaction_type", ""),
                                            "direction": ixp.get("direction", ""),
                                            "protocol": ixp.get("protocol", ""),
                                            "endpoint": ep,
                                            "file_path": ixp.get("file_path", ""),
                                            "expanded": True,
                                            "hop": hop,
                                        })
                            except Exception as _e:
                                logger.debug(f"expand hop {hop} CodeModule interactions: {_e}")

                    expansion_queue = next_queue
                    if not expansion_queue:
                        break

                results.extend(expanded_results)
            except Exception as expand_err:
                logger.warning(f"search_code_graph: subgraph expansion failed: {expand_err}")
        # --- end subgraph expansion ---

        return _large_result({
            "success": True,
            "query": query,
            "scope": scope,
            "expand_hops": effective_hops,
            "detail": detail,
            "count": len(results),
            "results": results,
        })

    except Exception as e:
        logger.error(f"Error in code graph search: {e}")
        return json.dumps({"success": False, "error": str(e)}, indent=2)


@mcp.tool()
def query_code_structure(
    query_type: str,
    target: str,
    project: str = None
) -> str:
    """
    Query exact code structure and relationships using the code graph. Unlike
    search_code_graph (semantic/fuzzy), this returns precise structural data:
    what calls what, what depends on what, inheritance chains, call paths.

    Use this when you already know the entity name and want to understand its
    relationships. Use search_code_graph first if you need to discover the
    entity by description.

    When to use: "what calls function X?", "what does module Y depend on?",
    "find the call path from A to B", "what classes extend Z?".
    When NOT to use: discovering code by concept — use search_code_graph.

    Args:
        query_type: The kind of structural query to run:
            - "dependencies": what this module imports
            - "imports": reverse of dependencies — who imports this module
            - "callers": what functions call this function
            - "methods": methods belonging to a class
            - "extends": what classes this class inherits from
            - "interactions": cross-service calls (HTTP, gRPC, etc.)
            - "path": shortest call path between two functions (format:
              "source.func->dest.func", BFS up to depth 6)
            - "composes"/"composed_by": composition relationships
            - "type_users": functions using a given type in annotations
        target: The code entity to query (full_name for functions/classes,
                file path for modules, arrow-separated pair for "path")
        project: Optional project name filter. Omit for workspace default.

    Returns:
        JSON with the structural query results (entity names, file paths,
        relationship details).
    """
    try:
        client = get_weaviate_client()

        # Resolve project: explicit arg > env default > no filter
        # Pass project="" to explicitly search all projects regardless of env default
        effective_project = project if project is not None else (CODE_GRAPH_PROJECT or None)

        # Per-project collection name resolution (uses effective_project, not env)
        def _proj_coll(base: str) -> str:
            if effective_project:
                prefix = _sanitize_collection_prefix(effective_project)
                return f"{prefix}_{base}"
            return base

        def with_project(f):
            """AND an existing filter with the project filter if applicable."""
            if effective_project:
                return f & Filter.by_property("project").equal(effective_project)
            return f

        if query_type == "dependencies" or query_type == "imports":
            # Module-level queries
            coll = client.collections.get(_proj_coll("CodeModule"))

            if query_type == "dependencies":
                response = coll.query.fetch_objects(
                    filters=with_project(Filter.by_property("path").equal(target)),
                    limit=1,
                    return_references=["imports"]
                )

                if not response.objects:
                    return json.dumps({"success": False, "error": f"Module '{target}' not found"}, indent=2)

                imports = response.objects[0].references.get("imports", [])
                results = [{"path": imp.properties.get("path"), "file_path": imp.properties.get("path", "")} for imp in imports]

            else:  # imports
                response = coll.query.fetch_objects(
                    filters=with_project(Filter.by_property("imports").contains_any([target])),
                    limit=20
                )
                results = [{"path": obj.properties.get("path"), "file_path": obj.properties.get("path", "")} for obj in response.objects]

        elif query_type == "methods":
            # List methods in a class
            coll = client.collections.get(_proj_coll("CodeClass"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=1
            )

            if not response.objects:
                return json.dumps({"success": False, "error": f"Class '{target}' not found"}, indent=2)

            class_file_path = response.objects[0].properties.get("file_path") or response.objects[0].properties.get("path", "")
            methods = response.objects[0].properties.get("methods", [])
            results = [{"name": method, "file_path": class_file_path} for method in methods]

        elif query_type == "extends":
            # Find base classes
            coll = client.collections.get(_proj_coll("CodeClass"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=1,
                return_references=["extends"]
            )

            if not response.objects:
                return json.dumps({"success": False, "error": f"Class '{target}' not found"}, indent=2)

            extends = response.objects[0].references.get("extends", [])
            results = [{
                "name": base.properties.get("name"),
                "full_name": base.properties.get("full_name"),
                "file_path": base.properties.get("file_path") or base.properties.get("path", ""),
            } for base in extends]

        elif query_type == "callers":
            # Find all functions that call the target function
            coll = client.collections.get(_proj_coll("CodeFunction"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("call_names").contains_any([target])),
                limit=50
            )
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "signature": obj.properties.get("signature", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in response.objects
            ]

        elif query_type == "interactions":
            # Find outbound cross-service interactions from a function or module.
            # 1. Try matching target as CodeFunction full_name first, then CodeModule path.
            # 2. Filter CodeInteraction by source_function/source_module reference.
            interactions_coll = client.collections.get(_proj_coll("CodeInteraction"))

            func_coll = client.collections.get(_proj_coll("CodeFunction"))
            func_resp = func_coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=1
            )

            if func_resp.objects:
                source_uuid = str(func_resp.objects[0].uuid)
                ix_resp = interactions_coll.query.fetch_objects(
                    filters=Filter.by_ref("source_function").by_id().equal(source_uuid),
                    limit=50
                )
            else:
                mod_coll = client.collections.get(_proj_coll("CodeModule"))
                mod_resp = mod_coll.query.fetch_objects(
                    filters=with_project(Filter.by_property("path").equal(target)),
                    limit=1
                )
                if not mod_resp.objects:
                    return json.dumps({
                        "success": False,
                        "error": f"Function or module '{target}' not found"
                    }, indent=2)
                source_uuid = str(mod_resp.objects[0].uuid)
                ix_resp = interactions_coll.query.fetch_objects(
                    filters=Filter.by_ref("source_module").by_id().equal(source_uuid),
                    limit=50
                )

            results = []
            for obj in ix_resp.objects:
                props = obj.properties
                results.append({
                    "interaction_type": props.get("interaction_type", ""),
                    "direction": props.get("direction", ""),
                    "protocol": props.get("protocol", ""),
                    "endpoint": props.get("endpoint", ""),
                    "confidence": props.get("confidence", ""),
                    "raw_target": props.get("raw_target", ""),
                    "source_project": props.get("source_project", ""),
                    "description": props.get("description", "")
                })

        elif query_type == "path":
            # BFS path-finding through CodeFunction.calls (text-array property)
            # target format: "source_full_name->dest_full_name"
            if "->" not in target:
                return json.dumps({
                    "success": False,
                    "error": "path query requires target in format 'source_full_name->dest_full_name'"
                }, indent=2)

            source_name, dest_name = target.split("->", 1)
            source_name = source_name.strip()
            dest_name = dest_name.strip()

            func_coll = client.collections.get(_proj_coll("CodeFunction"))

            # BFS state: queue of (current_full_name, path_so_far)
            from collections import deque
            bfs_queue: deque[tuple[str, list[dict]]] = deque()

            # Seed: fetch source node to confirm it exists and get file_path
            src_filter = Filter.by_property("full_name").equal(source_name)
            if effective_project:
                src_filter = src_filter & Filter.by_property("project").equal(effective_project)
            src_resp = func_coll.query.fetch_objects(filters=src_filter, limit=1)
            if not src_resp.objects:
                return json.dumps({
                    "success": False,
                    "error": f"Source function '{source_name}' not found"
                }, indent=2)

            src_file = src_resp.objects[0].properties.get("file_path") or src_resp.objects[0].properties.get("path", "")
            bfs_queue.append((source_name, [{"full_name": source_name, "file_path": src_file, "hop": 0}]))

            visited: set[str] = {source_name}
            found_path: list[dict] | None = None
            max_depth = 6

            while bfs_queue and found_path is None:
                current_name, current_path = bfs_queue.popleft()
                current_hop = len(current_path) - 1

                if current_hop >= max_depth:
                    continue

                # Fetch current node's outbound calls
                cur_filter = Filter.by_property("full_name").equal(current_name)
                if effective_project:
                    cur_filter = cur_filter & Filter.by_property("project").equal(effective_project)
                cur_resp = func_coll.query.fetch_objects(filters=cur_filter, limit=1)
                if not cur_resp.objects:
                    continue

                calls_list: list[str] = cur_resp.objects[0].properties.get("calls") or []

                for callee_name in calls_list:
                    if callee_name == dest_name:
                        # Found destination — fetch its file_path
                        dest_filter = Filter.by_property("full_name").equal(dest_name)
                        dest_resp = func_coll.query.fetch_objects(filters=dest_filter, limit=1)
                        dest_file = ""
                        if dest_resp.objects:
                            dest_file = dest_resp.objects[0].properties.get("file_path") or dest_resp.objects[0].properties.get("path", "")
                        found_path = current_path + [{"full_name": dest_name, "file_path": dest_file, "hop": current_hop + 1}]
                        break

                    if callee_name not in visited:
                        visited.add(callee_name)
                        # Fetch callee file_path for path node
                        callee_filter = Filter.by_property("full_name").equal(callee_name)
                        callee_resp = func_coll.query.fetch_objects(filters=callee_filter, limit=1)
                        callee_file = ""
                        if callee_resp.objects:
                            callee_file = callee_resp.objects[0].properties.get("file_path") or callee_resp.objects[0].properties.get("path", "")
                        bfs_queue.append((
                            callee_name,
                            current_path + [{"full_name": callee_name, "file_path": callee_file, "hop": current_hop + 1}]
                        ))

            if found_path is None:
                return json.dumps({
                    "success": True,
                    "query_type": "path",
                    "target": target,
                    "path_found": False,
                    "count": 0,
                    "results": [],
                }, indent=2)

            results = found_path

        elif query_type == "composes":
            # Find what classes a given class composes (has as field types).
            coll = client.collections.get(_proj_coll("CodeClass"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=1
            )

            if not response.objects:
                return json.dumps({"success": False, "error": f"Class '{target}' not found"}, indent=2)

            composes = response.objects[0].properties.get("composes", []) or []
            results = [{"composed_class": name} for name in composes]

        elif query_type == "composed_by":
            # Find classes that compose (contain as a field) the given class name.
            coll = client.collections.get(_proj_coll("CodeClass"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("composes").contains_any([target])),
                limit=50
            )
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in response.objects
            ]

        elif query_type == "type_users":
            # Find functions that reference a given type name in their annotations.
            coll = client.collections.get(_proj_coll("CodeFunction"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("type_uses").contains_any([target])),
                limit=50
            )
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "signature": obj.properties.get("signature", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in response.objects
            ]

        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown query type: {query_type}. Supported: dependencies, imports, callers, methods, extends, interactions, path, composes, composed_by, type_users"
            }, indent=2)

        return _large_result({
            "success": True,
            "query_type": query_type,
            "target": target,
            "count": len(results),
            "results": results
        })

    except Exception as e:
        logger.error(f"Error in code structure query: {e}")
        return json.dumps({
            "success": False,
            "error": str(e)
        }, indent=2)


async def nl_code_query(
    question: str,
    model: str = "claude/haiku",
    project: str = None
) -> str:
    """
    Translate a natural language question about code structure into a structured query and execute it.

    Uses a local or cloud LLM to interpret the question and call query_code_structure() automatically.

    Examples:
      "What does the authenticate function call?" → callers query on authenticate
      "What classes inherit from BaseModel?" → extends query
      "What does orchestrator/agents/blackboard.py import?" → dependencies query
      "Find a path from handle_request to send_response" → path query

    Args:
        question: Natural language question about code structure
        model: Model to use for NL interpretation. Format: "claude/haiku", "ollama/qwen3.5:0.8b",
               "ollama/qwen3.5:9b". Default: "claude/haiku" (uses ANTHROPIC_API_KEY env var).
               Ollama models are free and run locally.
        project: Optional project name filter (same as query_code_structure)
    """
    _CLAUDE_MODEL_MAP = {
        "claude/haiku": "claude-haiku-4-5-20251001",
        "claude/sonnet": "claude-sonnet-4-6",
        "claude/opus": "claude-opus-4-6",
    }

    system_prompt = (
        "You are a code structure query interpreter. Given a natural language question about code, "
        "extract the query_type and target for query_code_structure().\n\n"
        "Available query_type values and their target formats:\n"
        "- dependencies: target = file path (e.g. 'orchestrator/agents/blackboard.py')\n"
        "- imports: target = module path that is imported (e.g. 'orchestrator.utils')\n"
        "- callers: target = full function name (e.g. 'orchestrator.agents.blackboard.claim')\n"
        "- methods: target = full class name (e.g. 'orchestrator.agents.blackboard.Blackboard')\n"
        "- extends: target = full class name (e.g. 'orchestrator.agents.base.BaseAgent')\n"
        "- interactions: target = full function name or file path\n"
        "- path: target = 'source_full_name->dest_full_name' (e.g. 'module.foo->module.bar')\n\n"
        "Respond ONLY with a JSON object with exactly two keys: query_type and target.\n"
        "Example: {\"query_type\": \"callers\", \"target\": \"orchestrator.agents.blackboard.claim\"}"
    )
    user_prompt = f"Question: {question}"

    try:
        if model.startswith("claude/"):
            anthropic_model = _CLAUDE_MODEL_MAP.get(model, "claude-haiku-4-5-20251001")
            if not ANTHROPIC_API_KEY:
                return json.dumps({
                    "success": False,
                    "error": "ANTHROPIC_API_KEY env var not set; use an ollama/ model instead",
                    "question": question,
                }, indent=2)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": anthropic_model,
                        "max_tokens": 256,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_prompt}],
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return json.dumps({
                            "success": False,
                            "error": f"Anthropic API error {resp.status}: {body[:200]}",
                            "question": question,
                        }, indent=2)
                    data = await resp.json()
                    raw_text = data["content"][0]["text"]

        elif model.startswith("ollama/"):
            ollama_model = model[len("ollama/"):]
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={
                        "model": ollama_model,
                        "prompt": f"{system_prompt}\n\n{user_prompt}",
                        "stream": False,
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        return json.dumps({
                            "success": False,
                            "error": f"Ollama API error {resp.status}: {body[:200]}",
                            "question": question,
                        }, indent=2)
                    data = await resp.json()
                    raw_text = data.get("response", "")

        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown model prefix in '{model}'. Use 'claude/' or 'ollama/'.",
                "question": question,
            }, indent=2)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Model call failed: {e}",
            "question": question,
        }, indent=2)

    # Extract JSON from the model response (may be wrapped in markdown fences)
    json_match = re.search(r'\{[^{}]+\}', raw_text, re.DOTALL)
    if not json_match:
        return json.dumps({
            "success": False,
            "error": f"Could not parse JSON from model response: {raw_text[:300]}",
            "question": question,
        }, indent=2)

    try:
        parsed = json.loads(json_match.group())
        query_type = str(parsed["query_type"]).strip()
        target = str(parsed["target"]).strip()
    except (KeyError, json.JSONDecodeError) as e:
        return json.dumps({
            "success": False,
            "error": f"JSON missing query_type/target: {e}. Raw: {raw_text[:300]}",
            "question": question,
        }, indent=2)

    # Execute the interpreted query
    result_str = query_code_structure(query_type, target, project)
    try:
        result = json.loads(result_str)
    except json.JSONDecodeError:
        result = {"raw": result_str}

    return json.dumps({
        "question": question,
        "interpreted_as": {"query_type": query_type, "target": target},
        "model_used": model,
        **result,
    }, indent=2)


async def migrate_embeddings(
    collection_name: str,
    vector_scheme: str | None = None,
) -> str:
    """Migrate a collection from single flat vector to named vectors.

    The vector scheme determines which named vectors are created:
      - "kg":   qwen3_embed (1024d) + ollama_embed (1024d, legacy) + openai_embed (1536d)
      - "code": codesage_embed (2048d) + ollama_code_embed (768d, legacy) + openai_embed (1536d)

    If vector_scheme is None, it is auto-detected from the collection name
    (Code* collections -> "code", everything else -> "kg").

    This recreates the collection with named vector configuration, re-inserts all
    objects with their existing vector mapped to the scheme's primary named vector,
    and optionally generates 'openai_embed' if OPENAI_API_KEY is set.

    WARNING: This deletes and recreates the collection. Back up first.

    Args:
        collection_name: Name of the Weaviate collection to migrate
        vector_scheme: "kg" or "code" (auto-detected if None)
    """
    from weaviate.classes.config import Configure, Property, DataType

    # Resolve scheme
    scheme = vector_scheme or _scheme_for_collection(collection_name)
    if scheme not in VECTOR_SCHEMES:
        return json.dumps({"error": f"Unknown vector_scheme '{scheme}'. Valid: {list(VECTOR_SCHEMES.keys())}"})

    scheme_vectors = VECTOR_SCHEMES[scheme]
    primary_vector_name = _primary_named_vector(scheme)

    client = get_weaviate_client()

    if not client.collections.exists(collection_name):
        return json.dumps({"error": f"Collection '{collection_name}' does not exist"})

    coll = client.collections.get(collection_name)

    # Read all objects with their vectors
    logger.info("migrate_embeddings: reading all objects from '%s' (scheme=%s)...", collection_name, scheme)
    all_objects = []
    for obj in coll.iterator(include_vector=True):
        all_objects.append({
            "properties": dict(obj.properties),
            "vector": obj.vector.get("default") if isinstance(obj.vector, dict) else obj.vector,
        })

    total = len(all_objects)
    logger.info("migrate_embeddings: read %d objects from '%s'", total, collection_name)

    # Get existing collection config to preserve properties
    config = coll.config.get()

    def _clone_prop(prop_obj) -> Property:
        """Clone a Property/NestedProperty preserving nested structure."""
        nested = getattr(prop_obj, "nested_properties", None)
        kwargs = {
            "name": prop_obj.name,
            "data_type": prop_obj.data_type,
            "description": getattr(prop_obj, "description", None),
        }
        if nested:
            kwargs["nested_properties"] = [_clone_prop(np) for np in nested]
        return Property(**kwargs)

    existing_props = [_clone_prop(prop) for prop in config.properties]

    # Build named vector config from scheme
    vectorizer_config = [
        Configure.NamedVectors.none(name=vec_name)
        for vec_name in scheme_vectors
    ]

    # Delete and recreate with named vectors. Preserve the
    # index_null_state=True invariant (the stale-filter relies on
    # `valid_until is_none(True)` being filterable; see _stale_filter
    # docstring). This setting CANNOT be added later via Reconfigure.
    client.collections.delete(collection_name)
    client.collections.create(
        name=collection_name,
        properties=existing_props,
        vectorizer_config=vectorizer_config,
        inverted_index_config=Configure.inverted_index(index_null_state=True),
    )

    new_coll = client.collections.get(collection_name)

    # Re-insert objects
    inserted = 0
    openai_generated = 0
    for obj_data in all_objects:
        props = obj_data["properties"]
        vectors = {}

        # Map existing flat vector to the scheme's primary named vector
        if obj_data["vector"]:
            vectors[primary_vector_name] = obj_data["vector"]

        # Generate OpenAI embedding if API key is available and scheme includes it
        content = props.get("content", "")
        if "openai_embed" in scheme_vectors and OPENAI_API_KEY and content:
            openai_vec = await get_openai_embedding(content[:8000])
            if openai_vec:
                vectors["openai_embed"] = openai_vec
                openai_generated += 1

        new_coll.data.insert(
            properties=props,
            vector=vectors if vectors else None,
        )
        inserted += 1

        if inserted % 50 == 0:
            logger.info("migrate_embeddings: %d/%d inserted", inserted, total)

    return json.dumps({
        "status": "success",
        "collection": collection_name,
        "vector_scheme": scheme,
        "named_vectors": list(scheme_vectors.keys()),
        "primary_vector": primary_vector_name,
        "total_objects": total,
        "inserted": inserted,
        "openai_embeddings_generated": openai_generated,
        "note": "Set DUAL_EMBEDDING_ENABLED=true to use named vectors for new inserts/searches",
    }, indent=2)


async def backfill_embeddings(
    collection_name: str,
    provider: str = "openai",
    batch_size: int = 50,
) -> str:
    """Generate missing embeddings for objects in a collection.

    Use after migrating to named vectors to fill in the secondary embedding
    (e.g., generate openai_embed for objects that only have ollama_embed).

    Requires DUAL_EMBEDDING_ENABLED=true and the collection to already use named vectors.

    Args:
        collection_name: Name of the Weaviate collection
        provider: Embedding provider to backfill ("openai" or "ollama")
        batch_size: Number of objects to process before logging progress
    """
    if not DUAL_EMBEDDING_ENABLED:
        return json.dumps({
            "error": "DUAL_EMBEDDING_ENABLED must be true to use backfill_embeddings"
        })

    valid_providers = ("ollama", "openai", "qwen3", "codesage", "legacy_ollama", "legacy_code")
    if provider not in valid_providers:
        return json.dumps({"error": f"Invalid provider: {provider}. Valid: {valid_providers}"})

    if provider == "openai" and not OPENAI_API_KEY:
        return json.dumps({"error": "OPENAI_API_KEY not set, cannot generate OpenAI embeddings"})

    # Determine the correct target vector name and embedding function based on
    # the collection's scheme and requested provider
    scheme = _scheme_for_collection(collection_name)
    if provider == "openai":
        target_vector_name = "openai_embed"
        embed_fn = get_openai_embedding
    elif provider == "qwen3":
        target_vector_name = "qwen3_embed"
        embed_fn = get_ollama_embedding  # uses EMBEDDING_MODEL (qwen3-embedding)
    elif provider == "codesage":
        target_vector_name = "codesage_embed"
        embed_fn = get_code_embedding  # uses CODE_EMBED_SERVICE_URL
    elif provider == "legacy_code":
        target_vector_name = "ollama_code_embed"
        embed_fn = get_legacy_code_embedding
    elif provider in ("ollama", "legacy_ollama"):
        if scheme == "code":
            target_vector_name = "ollama_code_embed"
            embed_fn = get_legacy_code_embedding
        else:
            target_vector_name = "ollama_embed"
            embed_fn = get_legacy_text_embedding

    client = get_weaviate_client()
    if not client.collections.exists(collection_name):
        return json.dumps({"error": f"Collection '{collection_name}' does not exist"})

    coll = client.collections.get(collection_name)

    total = 0
    updated = 0
    skipped = 0
    errors = 0

    for obj in coll.iterator(include_vector=True):
        total += 1

        # Check if this object already has the target named vector
        existing_vectors = obj.vector if isinstance(obj.vector, dict) else {}
        if target_vector_name in existing_vectors and existing_vectors[target_vector_name]:
            skipped += 1
            continue

        content = obj.properties.get("content", "")
        if not content:
            skipped += 1
            continue

        vec = await embed_fn(content[:8000])
        if vec is None:
            errors += 1
            continue

        # Update the object with the new named vector
        try:
            coll.data.update(
                uuid=obj.uuid,
                vector={target_vector_name: vec},
            )
            updated += 1
        except Exception as e:
            logger.warning("backfill_embeddings: failed to update %s: %s", obj.uuid, e)
            errors += 1

        if (updated + errors) % batch_size == 0:
            logger.info(
                "backfill_embeddings: processed %d/%d (updated=%d, skipped=%d, errors=%d)",
                total, total, updated, skipped, errors,
            )

    return json.dumps({
        "status": "success",
        "collection": collection_name,
        "provider": provider,
        "target_vector": target_vector_name,
        "total_objects": total,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }, indent=2)


if __name__ == "__main__":
    logger.info(f"Starting Claude Orchestrator Weaviate MCP Server")
    logger.info(f"Primary Collection: {KG_COLLECTION}")
    if SHARED_KG_OPT_OUT:
        logger.info(f"Shared Collection: opted out via SHARED_KG_OPT_OUT (would have been '{_SHARED_KG_RAW}')")
    else:
        logger.info(f"Shared Collection: {SHARED_KG_COLLECTION if SHARED_KG_COLLECTION else 'None'}")
    logger.info(f"Dual Embedding: {DUAL_EMBEDDING_ENABLED} (active: {ACTIVE_EMBEDDING})")
    logger.info(f"Weaviate: {WEAVIATE_URL}")
    logger.info(f"Code Graph Project: {CODE_GRAPH_PROJECT if CODE_GRAPH_PROJECT else '(all projects)'}")
    logger.info(f"Code Graph Collections: {_code_collection('Code*')}")

    # Run server with stdio transport
    asyncio.run(mcp.run_stdio_async())
