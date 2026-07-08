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

Shared KG access (symmetric since v0.2.46, asymmetric-by-default):
- DEFAULT (fresh project): READ + WRITE both enabled. The headline value
  prop of the orchestrator is that knowledge accumulates across all
  projects, so reads are on by default; writes are on so per-project
  insights can be promoted to the shared corpus.
- READ paths (hybrid_search, semantic_graph_search) query the shared
  collection when SHARED_KG_COLLECTION is set AND SHARED_KG_READ_DISABLED
  is not true. Setting SHARED_KG_READ_DISABLED=true on a project excludes
  its hybrid_search / semantic_graph_search fan-out from the shared
  collection (the project still sees its own primary KG + any peer
  matrix entries). New in v0.2.46 — pre-v0.2.46 reads were unconditional.
- WRITE paths (store_knowledge_node with scope='shared', or any write whose
  resolved target is SHARED_KG_COLLECTION) consult SHARED_KG_WRITE_DISABLED.
  When true, writes are REFUSED with a clear error rather than silently
  rerouted to the project KG — silent reroutes used to mislead callers
  into thinking their cross-project insight had landed in the shared KG.
- Legacy alias: SHARED_KG_OPT_OUT (boolean) is honoured as a write-only
  alias for SHARED_KG_WRITE_DISABLED for ~3 releases (2026-05 → 2026-08).
  The new key wins when both are set. No legacy alias exists for the read
  gate (SHARED_KG_READ_DISABLED is canonical from v0.2.46 onward).

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
import functools
import uuid
import warnings
from typing import Any, Optional, List, Dict
from pathlib import Path
from datetime import datetime, timezone, timedelta

# v0.2.52 (Known Issue 6, Sub-issue A): silence the
# ``AuthlibDeprecationWarning: authlib.jose module is deprecated`` noise
# that ``weaviate-client``'s transitive ``authlib`` dependency emits during
# module import.  VCO never uses authlib's OIDC / JOSE code paths
# (we only talk to local Weaviate via HTTP+gRPC without OAuth2 tokens),
# so this warning is pure boilerplate during install / KG-seed and scares
# users on their first run.  We import the warning class directly when
# possible so the filter is precisely scoped — if authlib happens to not
# be installed (someone stubbed weaviate-client out), the fallback uses
# the broader ``DeprecationWarning`` category but still filters by message
# regex so we don't accidentally silence unrelated DeprecationWarnings.
# IMPORTANT: this MUST run BEFORE ``import weaviate`` below, because the
# warning fires at module load time on weaviate's authlib imports.
try:
    from authlib.deprecate import AuthlibDeprecationWarning  # type: ignore
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except ImportError:
    # authlib not installed — fall back to message-scoped filter.  Matches
    # both the current "authlib.jose module is deprecated" text and any
    # future authlib deprecations that surface via the same channel.
    warnings.filterwarnings(
        "ignore",
        message=r".*authlib.*deprecated.*",
        category=DeprecationWarning,
    )

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

# v0.2.72 (P1/P2 integration): the SHARED code-retrieval pipeline. Both this
# MCP (`search_code_graph`) and the CLI (`query_code_graph.py::search_by_concept`)
# call `run_code_retrieval_pipeline` so floor/rerank/collapse/tier can't diverge.
try:
    from .code_ranking import (
        run_code_retrieval_pipeline,
        resolve_retrieval_floor,
        resolve_post_rerank_floor,
    )
except ImportError:
    from code_ranking import (  # noqa: E402 — server.py run directly via python
        run_code_retrieval_pipeline,
        resolve_retrieval_floor,
        resolve_post_rerank_floor,
    )

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PR-42 (v0.2.12): install a SIGHUP handler so the launcher (or the user
# running `kill -HUP <pid>`) can ask this MCP to pick up an updated
# `.claude/settings.json env`. The handler exits cleanly with code 0;
# Claude Code respawns us on the next request with fresh env. See
# claude_mcp_servers/_lib/sighup_handler.py for the full design rationale.
# Import is best-effort: when the MCP is run via
# `python <install>/claude_mcp_servers/weaviate_mcp/server.py` the parent
# dir (claude_mcp_servers/) needs to be on sys.path for `_lib` to resolve.
try:
    from _lib.sighup_handler import register_sighup_exit_handler  # type: ignore
except ImportError:
    # Ensure the parent dir is on sys.path then retry once.
    _parent_dir = str(Path(__file__).resolve().parent.parent)
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)
    try:
        from _lib.sighup_handler import register_sighup_exit_handler  # type: ignore
    except ImportError:
        # _lib missing entirely (e.g. partial install) — soft-fail. The
        # MCP still works, it just won't auto-reload env on SIGHUP. The
        # launcher's manual "Reload MCPs" button falls back to a hard
        # kill in that case.
        def register_sighup_exit_handler(_logger):  # type: ignore[no-redef]
            return False
register_sighup_exit_handler(logger)

# v0.2.18: EmbeddingService (vco_lib) — centralised dispatcher for
# embed calls and slot resolution. Replaces the per-helper Ollama /
# CodeEmbed / OpenAI HTTP calls that used to live as
# `get_ollama_embedding` / `get_code_embedding` / `_get_all_kg_embeddings`
# / `_get_search_vector` here. Those helpers still exist as thin
# adapters that call EmbeddingService — keeping the function names
# preserves every callsite in this module unchanged.
#
# Import is graceful: if vco_lib isn't on sys.path (rare; partial
# install), the helpers fall through to their pre-v0.2.18 inline
# HTTP-call bodies. This lets the MCP boot on a half-migrated install
# instead of crashing at import time.
try:
    _vco_lib_parent = Path(__file__).resolve().parent.parent.parent
    if str(_vco_lib_parent) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent))
    from vco_lib.embedding_service import (
        EmbeddingService,
        NoEmbeddingBackendError,
    )
    HAS_EMBEDDING_SERVICE = True
except Exception as _embed_import_err:  # pragma: no cover (rare half-install)
    logger.warning(
        "EmbeddingService import failed (%s) — falling back to legacy "
        "inline embed helpers. Run install.py --update to refresh vco_lib.",
        _embed_import_err,
    )
    HAS_EMBEDDING_SERVICE = False
    EmbeddingService = None  # type: ignore[assignment]
    NoEmbeddingBackendError = Exception  # type: ignore[assignment]

# v0.2.34 cr-b2 (2026-05-25): canonical sanitiser for Weaviate class
# prefixes. The diagrams-collection naming bug (silent cross-project
# visibility break for any project with non-alphanumeric chars) was
# caused by `_sanitize_collection_prefix` re-implementing a divergent
# rule. Lock onto Python's source-of-truth instead — see the docstring
# on `_sanitize_collection_prefix` below for the full rationale.
# Import is graceful for the same reason as EmbeddingService: a
# half-installed env shouldn't crash the MCP at first call.
try:
    from vco_lib.project_init import (
        sanitize_for_weaviate_class as _canonical_sanitize_for_weaviate_class,
    )
    _HAS_CANONICAL_SANITIZER = True
except Exception as _sanitiser_import_err:  # pragma: no cover (rare half-install)
    logger.warning(
        "vco_lib.project_init.sanitize_for_weaviate_class import failed (%s) "
        "— falling back to inline implementation. Run install.py --update "
        "to refresh vco_lib.",
        _sanitiser_import_err,
    )
    _HAS_CANONICAL_SANITIZER = False
    _canonical_sanitize_for_weaviate_class = None  # type: ignore[assignment]

# v0.2.74 (BLOCKER-1): the code-graph reader prefix is the underscore-PRESERVING
# `canonical_class_prefix` (the SSOT the ANALYZER writes with + launcher.db
# `project_codegraph_bindings.collection_prefix` uses), NOT the underscore-
# DROPPING `sanitize_for_weaviate_class` used for diagrams/KG. Masked until now
# only because `VibeCodedOrchestrator` has no underscore — but ANY underscored
# code prefix made the MCP READ a different class than the analyzer WROTE →
# silent 0-results + a latent duplicate-generator. Import the preserving rule
# separately; a code-graph-ONLY sanitizer (`_code_sanitize_collection_prefix`,
# below) routes the Code* class-name construction through it, while diagrams/KG
# stay on the dropping `_sanitize_collection_prefix` (pinned by
# `tests/test_diagrams_class_name_parity.py` + the Rust mirror — do NOT merge).
try:
    from vco_lib.project_naming import (
        canonical_class_prefix as _canonical_class_prefix,
    )
    _HAS_CODE_CANONICAL_PREFIX = True
except Exception as _code_prefix_import_err:  # pragma: no cover (rare half-install)
    logger.warning(
        "vco_lib.project_naming.canonical_class_prefix import failed (%s) — "
        "code-graph collection resolution falls back to the underscore-dropping "
        "rule (correct only for non-underscored project names). Run install.py "
        "--update to refresh vco_lib.",
        _code_prefix_import_err,
    )
    _HAS_CODE_CANONICAL_PREFIX = False
    _canonical_class_prefix = None  # type: ignore[assignment]


# ─── v0.2.21 Step 18: per-project config resolver ───────────────────────
#
# Module-level config constants used to be read directly from os.getenv
# at import time. v0.2.21 routes them through the launcher's vct-hub via
# vco_lib.project_config.resolve(), with env-var fallback when the hub
# is unreachable (launcher not running, project not registered, stale
# token after a launcher restart). The resolved values populate the
# module-level KG_COLLECTION / SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION
# / ACTIVE_EMBEDDING / CODE_GRAPH_PROJECT constants below.
#
# The MCP is long-running (hours to days). We resolve ONCE at import
# time — the hub's TTL semantics keep callers fresh-enough (cf. parent
# plan §8.3). A SIGHUP triggers process exit + relaunch (sighup_handler
# above), so a launcher GUI edit propagates within the next request.
try:
    _vco_lib_parent_for_pc = Path(__file__).resolve().parent.parent.parent
    if str(_vco_lib_parent_for_pc) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent_for_pc))
    from vco_lib.project_config import resolve as _resolve_project_config  # type: ignore[import-not-found]
    from vco_lib.project_config import ResolverError as _ResolverError  # type: ignore[import-not-found]
    _HAS_PROJECT_CONFIG = True
except Exception as _pc_import_err:  # pragma: no cover (rare half-install)
    logger.warning(
        "vco_lib.project_config import failed (%s) — falling back to env "
        "vars for KG_COLLECTION / SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION "
        "/ ACTIVE_EMBEDDING / CODE_GRAPH_PROJECT. Run install.py --update.",
        _pc_import_err,
    )
    _HAS_PROJECT_CONFIG = False
    _resolve_project_config = None  # type: ignore[assignment]
    _ResolverError = Exception  # type: ignore[assignment]


_resolved_project_config = None  # cached resolve() result (or None if unreachable)

# v0.2.74 T5-1: the CLAUDE_PROJECT_DIR this process was SPAWNED with, captured
# ONCE at module import. Every module-level collection constant
# (KG_COLLECTION / SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION / ...) is
# resolved from THIS value (via the hub-over-env chain) and never refreshed.
# The per-tool-call backstop (`_assert_workspace_unchanged`) compares the
# LIVE env against this snapshot and refuses-loud on divergence, so a Claude
# client that binds to a stale-workspace subprocess never silently gets
# wrong-project results. Empty string when Claude Code spawned us without the
# env (CLI / non-workspace launch) — the backstop then no-ops (nothing to
# diverge from). See vco_lib/mcp_singleton.py for the spawn-time reaper that
# is the primary fix; this is the correctness backstop.
_MODULE_LOAD_WORKSPACE: str = os.environ.get("CLAUDE_PROJECT_DIR", "")


def _try_resolve_project_config():
    """Best-effort: resolve the project config via vct-hub once.

    Returns the ProjectConfig dataclass or None. On None, every consumer
    falls back to its existing os.getenv() default. The resolver client
    emits its own rate-limited warning on the fall-through path
    (Step 17); this MCP doesn't need to log anything extra.

    v0.2.47 RL-6c follow-up: when ``VCT_DISABLE_HUB_RESOLVER=1`` is set in
    the environment, this short-circuits to None so test fixtures get
    pure env-fallback behavior. Without this guard, tests that
    monkey-patch ``KG_COLLECTION`` / ``SHARED_KG_COLLECTION`` /
    ``DIAGRAMS_COLLECTION`` env vars and reload the module had their
    injection silently overridden by whatever the live vct-hub on the
    dev machine reported. The env var is set once per test session via
    ``tests/conftest.py``'s autouse fixture. Production runs leave it
    unset, preserving the hub-first resolution semantics.
    """
    global _resolved_project_config
    if _resolved_project_config is not None:
        return _resolved_project_config
    if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
        return None
    if not _HAS_PROJECT_CONFIG or _resolve_project_config is None:
        return None
    try:
        # NEW-6 (2026-05-28): prefer CLAUDE_PROJECT_DIR over Path(__file__)
        # so MCP subprocesses launched against different workspaces resolve
        # to different projects. Pre-fix: every project's MCP resolved to
        # whichever workspace server.py physically lives in (the global
        # ~/.claude.json registration's path), causing telemetry mislabeling
        # and wrong KG collection routing for any non-default project.
        _workspace = os.environ.get('CLAUDE_PROJECT_DIR', '')
        if _workspace and Path(_workspace).is_dir():
            _project_root = Path(_workspace).resolve()
        else:
            _project_root = Path(__file__).resolve().parent.parent.parent
        _resolved_project_config = _resolve_project_config(_project_root)
        return _resolved_project_config
    except Exception:
        # Hub unreachable, project not registered, etc. — silent fall-
        # through to env. The resolver client already logged the cause
        # at its rate-limited warning level.
        return None


def _config_field(
    field_name: str,
    env_name: str,
    default: str,
    empty_means_unset: bool = False,
) -> str:
    """Resolve a single config field via the hub; fall back to env.

    Used at module load to populate the KG_COLLECTION etc. constants.
    Cheap on the cached path — _try_resolve_project_config() memoises.

    Args:
        field_name: Attribute name on the ProjectConfig dataclass.
        env_name: Env-var name to read on the env-fallback path.
        default: Value to return if neither hub nor env resolves.
        empty_means_unset: When True, an explicit empty-string env value
            (e.g. ``KG_COLLECTION=""``) is treated the same as "unset" and
            falls through to the default. When False (legacy), an empty
            env value is returned literally — used for keys where empty
            carries semantic meaning (e.g. DEVELOPMENT_COLLECTION's empty
            default disables docs-fanout in hybrid_search).

    v0.2.27: ``empty_means_unset=True`` added for KG_COLLECTION-shape
    fields where an empty literal would propagate to Weaviate queries
    and cause schema-fail with a confusing error. The bug surfaced when
    ``.vscode/settings.json claude-code.env`` (an inert surface on
    Linux per PR-27) wrote ``KG_COLLECTION=""`` and the MCP picked it up.
    """
    cfg = _try_resolve_project_config()
    if cfg is not None:
        try:
            value = getattr(cfg, field_name, "")
            if value:
                return str(value)
        except Exception:
            pass
    raw = os.getenv(env_name, default)
    if empty_means_unset and isinstance(raw, str) and not raw.strip():
        return default
    return raw


# Default truncation limit in Claude Code is ~25K chars.
# v2.1.91+ supports _meta["anthropic/maxResultSizeChars"] override (up to 500K).
_MAX_RESULT_SIZE = 200_000  # 200K — generous but not wasteful


def _large_result(data: dict, indent: int = 2) -> str:
    """Serialize a dict to JSON for tool return.

    Use for tools that can return large payloads (hybrid_search detail=full,
    semantic_graph_search, search_code_graph with expand_hops, etc.).
    """
    return json.dumps(data, indent=indent)


# ─── v0.2.49 Step F Phase 8 helpers ─────────────────────────────────────
# Helpers consumed by `store_knowledge_node`'s access-matrix gate block
# (Phase 8 / item #21). Lifted here from inline to keep the gate readable.


def _emit_gate_crash_metric(project_id: str, collection: str, exc_str: str) -> None:
    """v0.2.49 Step F MF6: emit a dropped-write metric row when the
    access-matrix gate itself crashes (not just the resolver's expected
    fail-open path).

    The resolver's fail-open contract handles network / 4xx / 5xx /
    malformed responses without raising. If an exception DOES reach this
    helper, it means a bug in `vco_lib.access_resolver` or its caller —
    the user needs to know the gate is degraded.

    Mirror of `_emit_metric` in vco_lib/access_resolver.py. Never raises;
    silent on I/O failure so a broken metric path doesn't break the
    fail-open contract on top of a broken resolver.
    """
    try:
        import time as _time
        state_dir = os.environ.get("VCT_STATE_DIR")
        if state_dir:
            cache_dir = os.path.join(state_dir, "cache")
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".vct", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        jsonl_path = os.path.join(cache_dir, "dropped_writes.jsonl")
        row = {
            "ts": int(_time.time()),
            "project_id": project_id,
            "collection": collection,
            "reason": "gate_crash",
            "exception": exc_str[:500],  # cap to bound JSONL row size
            "fail_open": True,
        }
        with open(jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        # Metric-emit failure must not break the fail-open contract.
        pass


# ──────────────────────────────────────────────────────────────────────
# v0.2.49 SB1: gate-skipped (empty VCT_PROJECT_ID) surfaces
#
# The Phase-8 WRITE gate has a silent-bypass when VCT_PROJECT_ID is
# missing from the MCP environment — the gate's empty-PID branch falls
# through to allow without any audit trail. SB1 closes that hole by
# adding two visibility surfaces (per the user's 2026-06-08 Q1
# directive — silent-allow stays the default; remediation lands in
# UPDATE_DEFERRED.md, not stderr):
#
#   1. dropped_writes.jsonl row with reason='gate_skipped_no_project_id'
#      (audit-trail surface, mirrors gate_crash shape).
#   2. UPDATE_DEFERRED.md entry with actionable remediation commands
#      (user-facing surface).
#
# Both are idempotent within a server lifetime via a module-level set
# keyed by session_id so a kg-sync burst doesn't spam either surface.
# ──────────────────────────────────────────────────────────────────────

_GATE_SKIPPED_SESSIONS_SEEN: set[str] = set()
"""Per-process dedup set: once a session has had ONE gate_skipped
emission for empty-PID, further occurrences within the same server
lifetime are suppressed at the deferral-write level (the
dropped_writes.jsonl metric still fires per-call because audit-trail
granularity matters for triage).
"""


def _emit_gate_skipped_metric(collection: str) -> None:
    """v0.2.49 SB1: emit a dropped-write metric row for the empty-PID
    branch (VCT_PROJECT_ID missing → gate would silently allow).

    Mirror of ``_emit_gate_crash_metric`` shape; only the ``reason``
    discriminator differs. Always fires per-call (no dedup) so the
    JSONL is the authoritative count of how many writes hit the
    silent-bypass path. The companion deferral writer
    (``_emit_gate_skipped_deferral``) IS deduped per session — the two
    surfaces have different consumers / cardinalities by design.

    Never raises; silent on I/O failure so a broken metric path doesn't
    break the silent-allow contract that the gate's empty-PID branch
    relies on.
    """
    try:
        import time as _time
        state_dir = os.environ.get("VCT_STATE_DIR")
        if state_dir:
            cache_dir = os.path.join(state_dir, "cache")
        else:
            cache_dir = os.path.join(os.path.expanduser("~"), ".vct", "cache")
        os.makedirs(cache_dir, exist_ok=True)
        jsonl_path = os.path.join(cache_dir, "dropped_writes.jsonl")
        row = {
            "ts": int(_time.time()),
            "project_id": "",  # empty by definition (this branch fires when missing)
            "collection": collection,
            "reason": "gate_skipped_no_project_id",
            "fail_open": True,
        }
        with open(jsonl_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        # Metric-emit failure must not break the silent-allow contract.
        pass


def _resolve_project_root_for_deferral() -> Optional[Path]:
    """Best-effort: locate the project root for SB1's deferral write.

    Resolution order (mirrors the rest of server.py):
      1. ``$CLAUDE_PROJECT_DIR`` env (set by Claude Code per-workspace)
      2. ``$KG_BASE_DIR`` env (set by ``.claude/env`` / settings.json)
      3. ``Path(__file__).resolve().parent.parent.parent`` — the
         orchestrator's own root when this MCP runs from a clone.

    Returns None when no candidate resolves to an existing directory —
    the SB1 deferral writer treats that as "skip" (silent-allow is the
    contract; we don't want a missing project dir to break the write).
    """
    try:
        candidates: list[str] = []
        workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
        if workspace:
            candidates.append(workspace)
        kg_base = os.environ.get("KG_BASE_DIR", "")
        if kg_base:
            candidates.append(kg_base)
        # Module-local fallback.
        try:
            candidates.append(str(Path(__file__).resolve().parent.parent.parent))
        except Exception:
            pass
        for c in candidates:
            try:
                p = Path(c).resolve()
            except (OSError, RuntimeError):
                continue
            if p.is_dir():
                return p
    except Exception:
        # All path resolution paths failed — fall through to None.
        pass
    return None


def _emit_gate_skipped_deferral(collection: str) -> None:
    """v0.2.49 SB1: append an UPDATE_DEFERRED.md entry pointing the
    user at the remediation path for the gate's empty-PID branch.

    Per user Q1 (2026-06-08): silent-allow stays the default for the
    gate's empty-PID path; the deferral file is the user-facing surface
    that surfaces the actionable remediation (re-register the project
    or re-run install.py --update so .claude/env carries
    VCT_PROJECT_ID).

    Idempotency: deduped per-server-process via
    ``_GATE_SKIPPED_SESSIONS_SEEN`` — only the FIRST call per session
    writes; subsequent calls are no-ops. The deferral writer itself is
    upsert-by-condition_id under the
    ``vco_lib.deferral_report.DeferralReport`` contract so even a
    repeat call (cross-process) wouldn't accumulate duplicates within
    a single UPDATE_DEFERRED.md file.

    Never raises; silent on I/O failure so the silent-allow contract
    isn't broken by a missing project dir / unwritable file.
    """
    # Per-session dedup. The session_id is a stable identifier for the
    # life of the MCP subprocess; once we've written the deferral once,
    # further empty-PID writes within the same kg-sync burst are silent.
    session_key = os.environ.get("VCT_SESSION_ID") or os.environ.get(
        "CLAUDE_SESSION_ID", ""
    ) or f"pid:{os.getpid()}"
    if session_key in _GATE_SKIPPED_SESSIONS_SEEN:
        return
    _GATE_SKIPPED_SESSIONS_SEEN.add(session_key)

    project_root = _resolve_project_root_for_deferral()
    if project_root is None:
        # Nowhere to write the deferral — skip silently.
        return

    try:
        from vco_lib.deferral_report import (
            DeferralEntry,
            DeferralReport,
        )
    except Exception:
        # vco_lib not on sys.path — silent skip. The metric still fired.
        return

    try:
        report = DeferralReport.read(project_root)
        # Upsert: DeferralReport.add_entry de-dups on condition_id, so
        # even if a prior session left the entry behind it gets refreshed
        # rather than duplicated.
        entry = DeferralEntry(
            condition_id="gate_skipped_no_project_id",
            title=(
                "Phase-8 access-matrix gate skipped (VCT_PROJECT_ID "
                "missing from MCP env)"
            ),
            detected=(
                "The MCP server reached store_knowledge_node with no "
                "VCT_PROJECT_ID env. The Phase-8 WRITE gate cannot "
                "identify this project against the hub's access matrix, "
                "so writes are proceeding via the silent-allow path. "
                "The write itself was permitted; this entry records the "
                "remediation so future writes go through the gate "
                f"properly. (target collection: {collection})"
            ),
            why_deferred=(
                "Seeding VCT_PROJECT_ID requires either an orchestrator "
                "install run (which queries launcher.db for the "
                "project's UUID) or a Launcher GUI project "
                "re-registration. Both are user-initiated; the MCP "
                "server cannot self-heal."
            ),
            command_to_apply=(
                "# Option A — orchestrator-root install / update:\n"
                "python install.py --update\n"
                "\n"
                "# Option B — per-project (pre-v0.2.49 install): re-register the\n"
                "# project via Launcher GUI → Projects → Identity tab. The\n"
                "# launcher's apply_project_env pass seeds VCT_PROJECT_ID\n"
                "# into <project>/.claude/env from launcher.db."
            ),
            severity="warning",
        )
        report.add_entry(entry)
        report.write(project_root)
    except Exception:
        # Any I/O failure here must not break the silent-allow contract.
        pass


def _fetch_writable_collections_for_project(project_id: str) -> list[str]:
    """v0.2.49 Step F MF7+Q2: return the list of Weaviate collections
    where the project has `access_level == 'write'` per the launcher's
    access matrix. Used by `store_knowledge_node`'s deny-branch to
    enrich the error response with actionable remediation.

    Source: vct-hub `GET /api/v1/projects/{id}/access?level=write`
    endpoint. This endpoint lands in the SAME v0.2.49 cycle (main
    chat's lane, sibling to the matrix `/access/{collection}` endpoint
    that this server.py already consumes via vco_lib.access_resolver).

    Until that endpoint lands in main chat's branch, this function
    returns an empty list — the caller's remediation string falls back
    to a generic "re-register the project / open Manage access" hint.

    Never raises: the deny-branch can't crash on enrichment. On any
    failure (hub unreachable, endpoint missing, malformed response),
    return [].
    """
    if not project_id:
        return []
    try:
        # Discover hub port + token via existing patterns.
        import urllib.request  # local import — keep top-of-module lean
        import urllib.error

        state_dir = os.environ.get("VCT_STATE_DIR") or os.path.join(
            os.path.expanduser("~"), ".vct"
        )

        port = os.environ.get("VCT_HUB_PORT")
        if not port:
            try:
                with open(os.path.join(state_dir, "hub.port"), encoding="utf-8") as fh:
                    port = fh.read().strip()
            except OSError:
                port = "7700"

        token = os.environ.get("VCT_HUB_TOKEN")
        if not token:
            try:
                with open(os.path.join(state_dir, "hub.token"), encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                return []  # no token → can't query

        url = f"http://127.0.0.1:{port}/api/v1/projects/{project_id}/access?level=write"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return []
            body = json.loads(resp.read().decode("utf-8"))
            # Expected shape (per main chat's new endpoint, mirrors
            # the /access/{collection} pattern): {"collections": [str, ...]}
            collections = body.get("collections")
            if isinstance(collections, list):
                return [c for c in collections if isinstance(c, str)]
            return []
    except Exception:
        # Any failure → empty list → generic remediation. Never raise
        # back to the deny-branch caller.
        return []

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

# RL training integration (transparent, best-effort).
#
# As of Stream 1 (2026-05-19), the direct HTTP wiring to ``rl_server.py``
# has moved into the ``rl_client`` package. The legacy ``rl_server/``
# sub-package was relocated to the private ``paid-modules/vct-rl-reranker``
# repo and ships as a paid container; this MCP talks to it via
# ``RLClient`` instead of importing the server code directly.
#
# Free tier: ``RL_SERVER_URL`` / ``RL_SERVER_PORT`` unset → ``RLClient``
# runs in "disabled mode" and ``cache_nodes`` returns inputs unchanged.
# Pro/MAO tier with container running → real reranking + online training.
#
# ``RL_SERVER_URL`` retained as a back-compat env var read by
# ``rl_client.client._resolve_base_url``. Default kept at the legacy
# 11439 only for back-compat with installs that explicitly set this
# variable; the canonical channel today is ``RL_SERVER_PORT`` (set
# by the launcher's ``allocate_rl_port`` flow).
RL_SERVER_URL = os.getenv("RL_SERVER_URL", "http://localhost:11439")
# ─── RL reranking + telemetry state (v0.2.75 P3g / M-1 remainder) ────────
# The RL mutable caches + tuning constants now DEFINE in ``rl_state`` (this
# module is importer-only for them). They are re-exported into server's
# namespace below so the public contract is unchanged bit-for-bit:
#   * mutable containers (``_rl_client_instances``, ``_rl_telemetry_writers``,
#     ``_rl_node_content_cache``, ``_rl_monitor_tasks``) re-export BY REFERENCE
#     — ``server.<name>`` IS the ``rl_state`` object, so every in-place mutation
#     (``rl_client.search_pipeline``, the tests) is observed on both;
#   * scalar tuning constants re-export by value — ``rl_enrichment`` reads them
#     via the ``server`` proxy, so a ``monkeypatch.setattr(srv, "_RL_*", …)``
#     still rebinds the surface the moved functions read (the patch surface is,
#     and stays, ``server``; only the DEFINITION home moved).
# Relative when imported as a package; absolute when run directly as a script
# (mirrors the ``from .embeddings`` / ``from .rl_enrichment`` guards — REQUIRED
# so the MCP starts under the launcher's bare-script ``python .../server.py``).
try:
    from . import rl_state as _rl_state  # noqa: E402 — package-relative
except ImportError:
    import rl_state as _rl_state  # type: ignore  # noqa: E402 — run directly
# Over-fetch multiplier: fetch this many × limit from Weaviate, pass all to RL
# server for reranking. (v0.2.75 P3f promotes this to KG_OVERFETCH_MULTIPLIER.)
_RL_OVERFETCH = _rl_state._RL_OVERFETCH
_RL_MAX_LINKED = _rl_state._RL_MAX_LINKED
_rl_call_seq = _rl_state._rl_call_seq  # re-export of the counter's current value
_rl_monitor_tasks = _rl_state._rl_monitor_tasks  # by-reference (set, mutated in place)
_rl_node_content_cache = _rl_state._rl_node_content_cache  # by-reference (dict)
_RL_NODE_CACHE_MAX = _rl_state._RL_NODE_CACHE_MAX
_RL_MONITOR_POLL_INTERVAL = _rl_state._RL_MONITOR_POLL_INTERVAL
_RL_MONITOR_ANSWER_THRESHOLD_TOKENS = _rl_state._RL_MONITOR_ANSWER_THRESHOLD_TOKENS
_RL_MONITOR_ANSWER_THRESHOLD = _rl_state._RL_MONITOR_ANSWER_THRESHOLD
_RL_TOOL_CONTENT_LIMIT = _rl_state._RL_TOOL_CONTENT_LIMIT
_RL_MONITOR_TIMEOUT = _rl_state._RL_MONITOR_TIMEOUT
_RL_MONITOR_FORCE_FLUSH_SENTINEL = _rl_state._RL_MONITOR_FORCE_FLUSH_SENTINEL
_RL_MIN_ANSWER_TOKENS_FOR_CITATION = _rl_state._RL_MIN_ANSWER_TOKENS_FOR_CITATION
_RL_MIN_ANSWER_CHARS_FOR_CITATION = _rl_state._RL_MIN_ANSWER_CHARS_FOR_CITATION
_RL_LITERAL_CITED_MIN_TITLE_LEN = _rl_state._RL_LITERAL_CITED_MIN_TITLE_LEN
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
EMBEDDING_SOURCE = os.getenv("EMBEDDING_SOURCE", "ollama")
# Dual-embedding support: when enabled, objects are stored with named vectors
# ("ollama_embed", "openai_embed") instead of a single flat vector.
# Enabled by default for fresh installs. Existing collections need migration
# first — see migrate_embeddings tool. Set to "false" to use legacy single-vector mode.
DUAL_EMBEDDING_ENABLED = os.getenv("DUAL_EMBEDDING_ENABLED", "true").lower() == "true"
# Active embedding for search queries:
#   KG: "qwen3" (default), "ollama" (legacy arctic), "openai"
#   Code: "codesage" (default), "ollama" (legacy jina), "openai"
# v0.2.21 Step 18: resolved via vct-hub with env-fallback (see _config_field
# above). Hub failure degrades silently to os.getenv.
ACTIVE_EMBEDDING = _config_field("active_embedding", "ACTIVE_EMBEDDING", "qwen3")
# v0.2.75 P3g / M-1 remainder: the pure-getenv embedding config
# (LEGACY_TEXT_EMBEDDING_MODEL / OPENAI_EMBEDDING_MODEL / OPENAI_API_KEY /
# CODE_EMBED_SERVICE_URL) DEFINES in ``embeddings`` (the embedding layer);
# re-exported here so bare ``server.<name>`` reads + test patches on the server
# object keep working. Imported early (embeddings has no module-level ``server``
# dependency — its ``server.<name>`` reads are call-time — so this is safe before
# the late functions-re-export block). Bare-script fallback mirrors that block.
try:
    from . import embeddings as _embeddings  # noqa: E402 — package-relative
except ImportError:
    import embeddings as _embeddings  # type: ignore  # noqa: E402 — run directly
LEGACY_TEXT_EMBEDDING_MODEL = _embeddings.LEGACY_TEXT_EMBEDDING_MODEL
OPENAI_EMBEDDING_MODEL = _embeddings.OPENAI_EMBEDDING_MODEL
OPENAI_API_KEY = _embeddings.OPENAI_API_KEY
CODE_EMBED_SERVICE_URL = _embeddings.CODE_EMBED_SERVICE_URL

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
# Code-graph summary sidecar (.code_formats.json) — v0.2.73 M2 consumer
# ---------------------------------------------------------------------------
# The code analogue of the KG .node_formats.json sidecar. Generated by
# templates/scripts/generate-code-summary.py (resync rider + manual CLI);
# consumed by the code tier renderer so the `summary` tier and the chunk-map
# header get LLM one-liners/summaries instead of raw body snippets.
#
# FROZEN v1 shape (metadata plan §3 D1):
#   key   = f"{file_path}::{full_name}"          (one entry per ENTITY)
#   entry = {full_name, file_path, collection, one_liner, summary,
#            generated_at, content_hash, backend,
#            total_chunks?, chunk_summaries?}
#   chunk_summaries = {str(chunk_num): "one sentence", ...} — keys are the
#   stringified stored `chunk_num` property values (0-indexed for code).
#
# Strictly per-project (no per-collection variant): peer fan-out rows simply
# miss the lookup → None, and the renderer keeps the pre-sidecar behaviour.
_code_formats_cache: dict | None = None


def _load_code_formats() -> dict:
    """Load the project's .claude/.code_formats.json sidecar. Cached.

    Resolution mirrors ``_load_node_formats``: ``KG_BASE_DIR/.claude/`` first,
    then the cwd-relative path. Returns {} on miss / parse error so callers
    can treat the result as a dict unconditionally.
    """
    global _code_formats_cache
    if _code_formats_cache is not None:
        return _code_formats_cache

    candidates = []
    if KG_BASE_DIR:
        candidates.append(os.path.join(KG_BASE_DIR, ".claude", ".code_formats.json"))
    candidates.append(os.path.join(os.getcwd(), ".claude", ".code_formats.json"))

    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    _code_formats_cache = json.loads(fh.read())
                logger.info(
                    f"Loaded code formats from {path} ({len(_code_formats_cache)} entries)"
                )
                return _code_formats_cache
            except Exception as e:
                logger.warning(f"Failed to load code formats from {path}: {e}")

    _code_formats_cache = {}
    return _code_formats_cache


def _get_code_format(file_path: str, full_name: str, level: str):
    """Get a pre-generated format for a code entity.

    Args:
        file_path: The row's stored ``file_path`` property (NOT the
            synthesized display fallback) — must match the analyzer-stored
            value the generator keyed on.
        full_name: The entity's ``full_name`` property.
        level: 'one_liner', 'summary', or 'chunk_summaries'.

    Returns:
        The formatted text (or dict for 'chunk_summaries'), or None when the
        sidecar / entry / level is missing.
    """
    if not file_path or not full_name:
        return None
    db = _load_code_formats()
    entry = db.get(f"{file_path}::{full_name}", {})
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
def _safe_float(env_name: str, default: str) -> float:
    """Parse a float from an env var, tolerating a user typo.

    D-12 (v0.2.73): KG_TIER_*/CODE_TIER_* are user-documented tunables
    (CLAUDE.md). Previously `float(os.getenv(...))` ran at MODULE SCOPE, so
    a locale-comma or typo (e.g. KG_TIER_MIN=0,42) raised ValueError during
    import → the ENTIRE weaviate-kg MCP failed to start and every KG +
    codegraph tool disappeared behind a startup traceback the hook contract
    mostly hides. A tunable knob must not be a kill switch: on a bad value
    we log a WARNING and fall back to the calibrated default.
    """
    raw = os.getenv(env_name)
    if raw is None or raw.strip() == "":
        return float(default)
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(
            "%s=%r is not a valid float; using default %s. "
            "(Tier knobs must be a plain decimal like 0.42 — a comma or "
            "stray character here would otherwise crash the whole MCP.)",
            env_name, raw, default,
        )
        return float(default)


_TIER_THRESHOLDS: dict[str, float] = {
    "min":          _safe_float("KG_TIER_MIN",          "0.42"),
    "single_chunk": _safe_float("KG_TIER_SINGLE_CHUNK", "0.55"),
    "three_chunks": _safe_float("KG_TIER_THREE_CHUNKS", "0.65"),
    "full":         _safe_float("KG_TIER_FULL",         "0.75"),
}

# v0.2.72 (P4): CODE-path tier thresholds — a SEPARATE, lower-calibrated gate
# than the KG thresholds above. Code cosine similarities run materially lower
# than KG's (CodeSage-embedded source vs qwen3-embedded prose), so the KG
# min=0.42 would DISCARD good code matches — this is exactly the v0.2.70 Bug B
# (code post_rerank_floor is 0.22, so the code tier `min` MUST be <= 0.22 or
# genuine code hits get thrown away before they can render).
#
# v0.2.72 pre-gate recalibration (measured-band rationale):
#   * NL on-topic queries land ~0.28-0.42 against CodeSage vectors — with the
#     old single_chunk=0.45 they ALL rendered as summary-only. single_chunk at
#     0.32 lets a mid-band on-topic hit render a real chunk of code.
#   * 0.48 is the top of the NL band / lower neighbour band → three_chunks.
#   * 0.62 sits just above the measured 0.59 good-code→code floor → full.
#   score < 0.22        → discard (default; in auto mode the EFFECTIVE `min`
#                          derives from resolve_post_rerank_floor(slot) at
#                          call time — see make_code_tier_fn(min_gate=...) —
#                          so a GUI floor override changes what renders)
#   0.22..0.32          → summary (signature + doc / first chunk for code)
#   0.32..0.48          → single_chunk
#   0.48..0.62          → three_chunks
#   >= 0.62             → full (up to 7 chunks)
# Overridable via CODE_TIER_* env vars (parallel to KG_TIER_*).
_CODE_TIER_THRESHOLDS: dict[str, float] = {
    "min":          _safe_float("CODE_TIER_MIN",          "0.22"),
    "single_chunk": _safe_float("CODE_TIER_SINGLE_CHUNK", "0.32"),
    "three_chunks": _safe_float("CODE_TIER_THREE_CHUNKS", "0.48"),
    "full":         _safe_float("CODE_TIER_FULL",         "0.62"),
}

# Per-tier chunk window (how many chunks to assemble from a chunked node)
_TIER_CHUNK_WINDOW: dict[str, int] = {
    "single_chunk": 1,
    "three_chunks": 3,
    "full":         7,
}


def _get_result_verbosity_by_score(
    score: float,
    thresholds: dict[str, float] | None = None,
) -> str:
    """Return one of: 'discard' | 'summary' | 'single_chunk' | 'three_chunks' | 'full'.

    Score is normalised 0..1, higher=better.

    ``thresholds`` (v0.2.72 P4) selects the tier gate. Default ``None`` →
    ``_TIER_THRESHOLDS`` (the KG gate, min=0.42) — so KG callers that pass no
    ``thresholds`` get byte-identical v0.2.71 behaviour. The CODE path passes
    ``_CODE_TIER_THRESHOLDS`` (min=0.22) so good code matches aren't discarded.
    """
    t = thresholds if thresholds is not None else _TIER_THRESHOLDS
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 0.0
    if s < t["min"]:
        return "discard"
    if s < t["single_chunk"]:
        return "summary"
    if s < t["three_chunks"]:
        return "single_chunk"
    if s < t["full"]:
        return "three_chunks"
    return "full"


# Total chunk budget shared across all results in a single auto-mode call.
# Defaults to 2×7 + 2×3 = 20 chunks — enough for the top 2 results to render
# at "full" tier (7 chunks each) plus the next 2 at "three_chunks" (3 each),
# with anything else degrading to summary/single_chunk regardless of score.
#
# Why a *shared* budget rather than per-result caps:
# - Score-gating still decides what each result is *allowed* to render at;
#   the budget only enforces how many results can use the expensive tiers.
# - A result with fewer chunks than its tier window costs less, naturally
#   freeing budget for later results (e.g. a 1-chunk top hit at "full"
#   only consumes 1, leaving 19 for results 2..N).
# - Caps the total bytes a single auto-mode call can emit at roughly
#   N_CHUNKS × ~8000 chars/chunk = ~160 KB worst case (default 20).
#   Defense-in-depth against the freeze threshold (~14 MB observed).
#
# Bypassed for explicit detail values (full, three_chunks, etc.) — the
# caller asked for uniform output, so we honour it without budget logic.
_HYBRID_CHUNK_BUDGET: int = int(os.getenv("KG_HYBRID_CHUNK_BUDGET", "20"))


def _resolve_hybrid_alpha() -> float:
    """Cosine (vector) weight for relativeScoreFusion in hybrid_search.

    Weaviate's `alpha` convention: alpha=1.0 → pure vector, 0.0 → pure
    keyword. Default 0.6 = cosine-dominant (semantic meaning is the primary
    signal; BM25 keyword overlap is the booster/tiebreaker). Overridable via
    KG_HYBRID_ALPHA; malformed / out-of-range values clamp to [0,1] and fall
    back to 0.6 on parse failure so a bad env var can never break search.
    """
    raw = os.getenv("KG_HYBRID_ALPHA")
    if raw is None:
        return 0.6
    try:
        a = float(raw)
    except (TypeError, ValueError):
        return 0.6
    return max(0.0, min(1.0, a))


_HYBRID_ALPHA: float = _resolve_hybrid_alpha()

# Tier downgrade chain: if budget can't cover the score-allowed tier, drop
# one step. summary always succeeds (cost 0).
_TIER_DOWNGRADE: dict[str, str] = {
    "full":         "three_chunks",
    "three_chunks": "single_chunk",
    "single_chunk": "summary",
}


def _tier_chunk_cost(tier: str, total_chunks: int) -> int:
    """How many chunks this tier would consume from the budget for a given node.

    A node with fewer chunks than the tier window costs only what it has —
    e.g. "full" on a 1-chunk node costs 1, not 7. Tiers that don't assemble
    chunks (titles, summary, discard) cost 0.
    """
    window = _TIER_CHUNK_WINDOW.get(tier, 0)
    if window == 0:
        return 0
    try:
        tc = int(total_chunks) if total_chunks else 1
    except (TypeError, ValueError):
        tc = 1
    return min(window, max(1, tc))


def _allocate_tier_within_budget(
    score: float,
    total_chunks: int,
    remaining_budget: int,
    thresholds: dict[str, float] | None = None,
) -> tuple[str, int]:
    """Pick a tier for one result given its score and the remaining shared budget.

    Returns (tier, chunks_consumed). The tier is the highest one the score
    permits AND the budget can fund. If the score-allowed tier doesn't fit,
    we downgrade through three_chunks → single_chunk → summary until
    something fits. Summary always fits (cost 0).

    A "discard" score (below the gate's ``min``) returns ("discard", 0)
    regardless of budget — discarded results never render.

    ``thresholds`` (v0.2.72 P4) is threaded to ``_get_result_verbosity_by_score``
    so the CODE path can pass ``_CODE_TIER_THRESHOLDS``. Default ``None`` keeps
    the KG gate — KG callers pass no new arg → identical v0.2.71 behaviour.
    """
    score_allowed = _get_result_verbosity_by_score(score, thresholds)
    if score_allowed == "discard":
        return ("discard", 0)

    tier = score_allowed
    while True:
        cost = _tier_chunk_cost(tier, total_chunks)
        if cost <= remaining_budget:
            return (tier, cost)
        tier = _TIER_DOWNGRADE.get(tier, "summary")
        if tier == "summary":
            return ("summary", 0)


# search_code_graph tier policy (Option A, 2026-05-07).
# See claude-orchestrator commit fbed0e0+ for design rationale.
#
# Ranks 1-2 (full_xl): untruncated content + function_body / class_body.
# Ranks 3-4 (full_l): truncated at CODE_TRUNC_CHARS (default 1200, was 200).
# Ranks 5+ (ref): metadata-only refs.
#
# Top-2 also get adjacent siblings in the same source file (CodeFunction /
# CodeClass only): up to CODE_SIBLINGS_RANK_1 (default 7) for rank 1,
# CODE_SIBLINGS_RANK_2 (default 5) for rank 2 — counts include the seed.
#
# Subgraph expansion is now capped independently at CODE_EXPANSION_LIMIT
# (default 8) instead of sharing the seed `limit` budget.
CODE_SIBLINGS_RANK_1: int = int(os.getenv("CODE_SIBLINGS_RANK_1", "7"))
CODE_SIBLINGS_RANK_2: int = int(os.getenv("CODE_SIBLINGS_RANK_2", "5"))
CODE_TRUNC_CHARS: int = int(os.getenv("CODE_TRUNC_CHARS", "1200"))
CODE_EXPANSION_LIMIT: int = int(os.getenv("CODE_EXPANSION_LIMIT", "8"))


# ---------------------------------------------------------------------------
# Shared rank-tier formatter for code-graph results.
#
# Both `search_code_graph` (MCP) and `.claude/scripts/query_code_graph.py`
# (CLI used by the pre-edit hook) format Weaviate code-graph hits with the
# same rank-tier policy documented at `# search_code_graph tier policy`
# above. Before the helper extraction (2026-05-10) the policy was inlined
# in both places, with the CLI silently drifting to a simpler "top-4 full,
# rest titles" model. The helper below is the single source of truth so
# the pre-edit hook injects context formatted identically to what MCP
# returns interactively.
# ---------------------------------------------------------------------------


def _truncate_code_field(s: str, n: int) -> str:
    """Truncation helper used by the rank-tier formatter.

    Returns the first ``n`` characters with ``...`` appended when the input
    exceeds ``n``; otherwise returns the input verbatim.
    """
    return s[:n] + "..." if len(s) > n else s


def _code_result_file_path(coll_name: str, p: dict) -> str:
    """Best-effort source file path from a code-graph object's properties.

    Mirrors the behaviour of the previous inline `_file_path` closure in
    `search_code_graph` so the helper can be called without a closure
    over the local `_file_path` function.
    """
    if p.get("file_path"):
        return p["file_path"]
    if p.get("path"):
        return p["path"]
    if p.get("full_name"):
        parts = p["full_name"].split(".")
        return "/".join(parts[:-1]) if len(parts) > 1 else p["full_name"]
    return ""


def _format_code_result_full(coll_name: str, p: dict, *, untruncated: bool) -> dict:
    """Build the full-tier fields for a code-graph result.

    untruncated=True: top-2 ranks — include function_body / class_body
    untruncated, all doc/summary/description fields untruncated.
    untruncated=False: ranks 3-4 — same fields as top-2 EXCEPT no
    function_body/class_body (those are large), and doc/summary/
    description truncated at CODE_TRUNC_CHARS (default 1200, was 200).
    """
    out: dict = {}
    if coll_name == "CodeFunction":
        doc = p.get("doc", "") or ""
        out["full_name"] = p.get("full_name", "")
        out["signature"] = p.get("signature", "")
        out["doc"] = doc if untruncated else _truncate_code_field(doc, CODE_TRUNC_CHARS)
        out["location"] = f"{p.get('start_line','?')}-{p.get('end_line','?')}"
        out["is_async"] = p.get("is_async", False)
        if untruncated:
            body = p.get("function_body", "") or ""
            if body:
                out["function_body"] = body
    elif coll_name == "CodeClass":
        doc = p.get("doc", "") or ""
        out["full_name"] = p.get("full_name", "")
        out["signature"] = p.get("signature", "")
        out["doc"] = doc if untruncated else _truncate_code_field(doc, CODE_TRUNC_CHARS)
        out["methods"] = p.get("methods", [])
        out["method_count"] = len(p.get("methods", []))
        out["location"] = f"{p.get('start_line','?')}-{p.get('end_line','?')}"
        if untruncated:
            body = p.get("class_body", "") or ""
            if body:
                out["class_body"] = body
    elif coll_name == "CodeModule":
        summary = p.get("module_summary", "") or ""
        out["path"] = p.get("path", "")
        out["language"] = p.get("language", "")
        out["loc"] = p.get("loc", 0)
        out["summary"] = summary if untruncated else _truncate_code_field(summary, CODE_TRUNC_CHARS)
    elif coll_name == "CodeAPI":
        desc = p.get("api_description", "") or ""
        out["endpoint"] = p.get("endpoint", "")
        out["method"] = p.get("method", "")
        out["description"] = desc if untruncated else _truncate_code_field(desc, CODE_TRUNC_CHARS)
        out["parameters"] = p.get("parameters", [])
    elif coll_name == "CodeInteraction":
        desc = p.get("description", "") or ""
        out["interaction_type"] = p.get("interaction_type", "")
        out["direction"] = p.get("direction", "")
        out["protocol"] = p.get("protocol", "")
        out["endpoint"] = p.get("endpoint", "")
        out["confidence"] = p.get("confidence", "")
        out["description"] = desc if untruncated else _truncate_code_field(desc, CODE_TRUNC_CHARS)
    return out


def _format_code_result_ref(coll_name: str, p: dict) -> dict:
    """Metadata-only ref for lower-ranked results, expansions, and siblings."""
    out: dict = {}
    if coll_name in ("CodeFunction", "CodeClass"):
        out["full_name"] = p.get("full_name", "")
    elif coll_name == "CodeModule":
        out["path"] = p.get("path", "")
        out["language"] = p.get("language", "")
    elif coll_name == "CodeAPI":
        out["endpoint"] = p.get("endpoint", "")
        out["method"] = p.get("method", "")
    elif coll_name == "CodeInteraction":
        out["endpoint"] = p.get("endpoint", "")
        out["protocol"] = p.get("protocol", "")
        out["interaction_type"] = p.get("interaction_type", "")
    return out


def _resolve_code_tier(rank: int, detail: str) -> str:
    """Map (rank, detail) → tier label.

    Returns one of "full_xl" (top-2 untruncated), "full_l" (rank 3-4
    truncated), or "ref" (metadata-only). ``detail="full"`` and
    ``detail="titles"`` collapse all ranks to a single tier; ``"auto"``
    uses the rank-position policy.

    Pathological case: when rank exceeds the result limit (e.g. caller
    asks for limit=2 but iterates further), ranks ≥ 4 keep returning
    "ref" — there is no upper bound on rank in the policy.
    """
    if detail == "titles":
        return "ref"
    if detail == "full":
        return "full_xl"
    if rank < 2:
        return "full_xl"
    if rank < 4:
        return "full_l"
    return "ref"


def _format_code_result_by_rank(
    properties: dict,
    collection: str,
    rank: int,
    *,
    detail: str = "auto",
    score: float | None = None,
    distance: float | None = None,
    sibling_fetcher=None,
) -> dict:
    """Render one code-graph result per the rank-tier policy.

    Single source of truth shared between `search_code_graph` (MCP) and
    `query_code_graph.py` (CLI / pre-edit hook). Both paths emit the
    same shape so the LLM sees identical context regardless of which
    surface fetched it.

    Args:
        properties: Weaviate object's ``properties`` dict.
        collection: Base collection name.
        rank: 0-based rank in the merged result list. Negative values
            clamp to 0; values ≥ 4 collapse to the metadata-ref tier.
        detail: ``"auto"`` (rank-driven), ``"full"``, or ``"titles"``.
            Unknown values normalise to ``"auto"``.
        score: Similarity score (1 - distance), pre-computed by caller.
        distance: Raw distance from Weaviate metadata.
        sibling_fetcher: Optional callable
            ``(file_path, hit_start_line, max_total, exclude_full_name)
            -> list[dict]`` for top-2 sibling enrichment. ``None``
            skips the sibling lookup.

    Returns:
        Dict with ``collection``, ``score``/``distance`` (when
        provided), ``file_path``, ``tier``, plus tier-specific fields
        merged in by collection. Siblings appear under ``siblings``
        when applicable.
    """
    if rank < 0:
        rank = 0
    if detail not in ("auto", "titles", "full"):
        detail = "auto"

    tier = _resolve_code_tier(rank, detail)
    file_path = _code_result_file_path(collection, properties)

    base: dict = {"collection": collection}
    if score is not None:
        base["score"] = f"{score:.3f}"
    if distance is not None:
        base["distance"] = f"{distance:.3f}"
    base["file_path"] = file_path
    base["tier"] = tier

    if tier == "full_xl":
        base.update(_format_code_result_full(collection, properties, untruncated=True))
    elif tier == "full_l":
        base.update(_format_code_result_full(collection, properties, untruncated=False))
    else:
        base.update(_format_code_result_ref(collection, properties))

    # v0.2.73 M2/M4: identity enrichment mirrors _format_code_result_by_tier —
    # sidecar one-liner + analyzer-stamped n_callers when present. Both keyed
    # on the REAL stored file_path (sidecar keys on the analyzer value, not
    # the synthesized display fallback). Absent sidecar / older rows → no
    # fields, byte-identical pre-M2 output.
    if collection in ("CodeFunction", "CodeClass"):
        _real_fp = properties.get("file_path") or ""
        _fn = properties.get("full_name", "")
        _n_callers = properties.get("n_callers")
        if _n_callers is not None:
            try:
                base["n_callers"] = int(_n_callers)
            except (TypeError, ValueError):
                pass
        _one_liner = _get_code_format(_real_fp, _fn, "one_liner")
        if isinstance(_one_liner, str) and _one_liner:
            base["one_liner"] = _one_liner

    # Sibling-fetch eligibility: only when the result has a REAL `file_path`
    # property (not the synthesized fallback `_code_result_file_path` derives
    # from `full_name` when the real field is missing). The synthesized form
    # ("alpha" derived from "alpha.foo") is fine for display but is not a
    # valid source-file path to filter same-file code-graph siblings against
    # — passing it to the fetcher results in wasted collection round-trips
    # against fixtures or partially-indexed entries, AND breaks tests that
    # mock a one-collection-per-call contract.
    real_file_path = properties.get("file_path")
    if (
        detail == "auto"
        and rank < 2
        and collection in ("CodeFunction", "CodeClass")
        and real_file_path
        and sibling_fetcher is not None
    ):
        try:
            hit_line = int(properties.get("start_line") or 0)
        except (TypeError, ValueError):
            hit_line = 0
        max_total = CODE_SIBLINGS_RANK_1 if rank == 0 else CODE_SIBLINGS_RANK_2
        try:
            sibs = sibling_fetcher(file_path, hit_line, max_total, properties.get("full_name", ""))
        except Exception as exc:  # noqa: BLE001 — sibling lookup is best-effort
            logger.debug("sibling_fetcher failed: %s", exc)
            sibs = []
        if sibs:
            base["siblings"] = sibs

    return base


def _code_body_field(collection: str) -> str:
    """Return the body property name for a code collection (P4 helper)."""
    if collection == "CodeFunction":
        return "function_body"
    if collection == "CodeClass":
        return "class_body"
    if collection == "CodeModule":
        return "module_summary"
    if collection == "CodeAPI":
        return "api_description"
    if collection == "CodeInteraction":
        return "description"
    return "content"


def _format_code_result_by_tier(
    properties: dict,
    collection: str,
    tier: str,
    *,
    score: float | None = None,
    distance: float | None = None,
    chunk_fetcher=None,
) -> dict:
    """Render one code-graph result by SCORE-TIER (v0.2.72 P4).

    The score-tier code renderer — parallel to the KG tier renderer, and a
    sibling of the rank-based ``_format_code_result_by_rank``. Assembles
    1/3/7 chunks per tier via ``_TIER_CHUNK_WINDOW`` (single_chunk=1,
    three_chunks=3, full=7). Designed to be called by the integrator (or
    T-FLOOR's ``run_code_retrieval_pipeline`` via an injected callable);
    ``search_code_graph``'s body is NOT rewired here.

    v0.2.73 M2: code now has its OWN sidecar (``.claude/.code_formats.json``,
    keyed ``file_path::full_name``). The ``summary`` tier prefers the sidecar
    ``summary``, then the stored ``doc``, then the body snippet; every tier
    attaches the sidecar ``one_liner`` when present; three_chunks/full
    assembly prepends the ▶/· chunk map from ``chunk_summaries``. Missing
    sidecar → byte-identical pre-M2 output.

    Args:
        properties: the WINNING chunk's Weaviate properties (already carries
            ``chunk_num``/``total_chunks`` from _format_obj, plus the body field
            and signature/full_name).
        collection: base collection name (CodeFunction / CodeClass / ...).
        tier: one of "summary" | "single_chunk" | "three_chunks" | "full".
        score / distance: display metadata.
        chunk_fetcher: optional callable
            ``(full_name, hit_chunk_num, total_chunks, max_chunks, file_path)
            -> list[dict]`` returning up to ``max_chunks`` chunk-property dicts
            (matched + neighbours) for the three_chunks / full tiers. ``None`` →
            the tier degrades to single_chunk (just the matched chunk's body).
            ``file_path`` (C-8) scopes the fetch to the winning row's source
            file so two same-``full_name`` entities in different files cannot
            interleave chunk bodies.

    Returns:
        A result dict with ``collection``/``tier``/``file_path`` + tier body.
    """
    body_field = _code_body_field(collection)
    file_path = _code_result_file_path(collection, properties)

    out: dict = {"collection": collection, "tier": tier, "file_path": file_path}
    if score is not None:
        out["score"] = f"{score:.3f}"
    if distance is not None:
        out["distance"] = f"{distance:.3f}"

    # Identity fields always present so the agent can locate the entity.
    # M2/M4 sidecar + metadata lookups key on the row's REAL stored
    # `file_path` property (not the synthesized display fallback derived
    # from full_name) — the generator keyed the sidecar on the stored value.
    real_file_path = properties.get("file_path") or ""
    full_name = ""
    doc = ""
    if collection in ("CodeFunction", "CodeClass"):
        full_name = properties.get("full_name", "")
        out["full_name"] = full_name
        out["signature"] = properties.get("signature", "")
        # R1 (design audit): surface the stored `doc` (docstring) — the
        # analyzer extracts and stores it, but the tier renderer dropped it.
        # Signature + doc reads better than signature + raw code snippet.
        doc = properties.get("doc") or ""
        if doc:
            out["doc"] = doc
        # v0.2.73 M4: surface `n_callers` (analyzer-stamped fan-in count) in
        # the identity block. Graceful when the property is absent/NULL
        # (rows analyzed before the 5→6 schema bump).
        n_callers = properties.get("n_callers")
        if n_callers is not None:
            try:
                out["n_callers"] = int(n_callers)
            except (TypeError, ValueError):
                pass
        # v0.2.73 M2: attach the sidecar one-liner on EVERY tier — a
        # one-line orientation is cheap and high-value even at
        # single_chunk / three_chunks. None (no sidecar) → field omitted.
        one_liner = _get_code_format(real_file_path, full_name, "one_liner")
        if isinstance(one_liner, str) and one_liner:
            out["one_liner"] = one_liner

    if tier == "summary":
        # v0.2.73 M2: lookup order is code-sidecar `summary` → stored doc →
        # body snippet. (Pre-M2 this probed the KG sidecar, which is always
        # empty for code file_paths — replaced with the code sidecar.)
        sidecar = _get_code_format(real_file_path, full_name, "summary")
        if isinstance(sidecar, str) and sidecar:
            out["summary"] = sidecar
        elif doc:
            # R1: PREFER the stored doc over the raw first-chunk body snippet
            # for the summary tier — signature + docstring is the better
            # ~400-char orientation than signature + truncated code.
            out["summary"] = _truncate_code_field(doc, 400)
        else:
            body = properties.get(body_field, "") or ""
            out["summary"] = _truncate_code_field(_strip_chunk_header_text(body), 400)
        return out

    matched_body = _strip_chunk_header_text(properties.get(body_field, "") or "")

    if tier == "single_chunk" or chunk_fetcher is None:
        out[body_field] = matched_body
        out["chunks_shown"] = 1
        return out

    # three_chunks / full → assemble a window of chunks around the hit.
    window = _TIER_CHUNK_WINDOW.get(tier, 1)
    try:
        hit_chunk = int(properties.get("chunk_num") or 0)
    except (TypeError, ValueError):
        hit_chunk = 0
    try:
        total = int(properties.get("total_chunks") or 1)
    except (TypeError, ValueError):
        total = 1
    try:
        # C-8 (v0.2.75 P2b): thread the WINNING row's file_path so the fetcher
        # scopes chunk assembly to the SAME source file. Two entities with the
        # same `full_name` in different files (a common stem like `__init__` /
        # `run` / `main`) otherwise interleave each other's chunk bodies at the
        # three_chunks/full tiers. This is the ONE shared call site (both the
        # MCP `_fetch_code_chunks` and the CLI `_code_chunk_fetcher` route
        # through here), so the fix reaches both surfaces identically.
        chunks = chunk_fetcher(
            properties.get("full_name", ""), hit_chunk, total, window,
            real_file_path,
        ) or []
    except Exception as exc:  # noqa: BLE001 — best-effort; degrade to single
        logger.debug("code chunk_fetcher failed: %s", exc)
        chunks = []

    if not chunks:
        # v0.2.73 M2: even on the degrade-to-matched fallback, a multi-chunk
        # entity with sidecar data gets the chunk map (only the hit shown).
        header = _code_chunk_summaries_header(
            real_file_path, full_name, [hit_chunk],
        )
        out[body_field] = (header + matched_body) if header else matched_body
        out["chunks_shown"] = 1
        return out

    assembled = "\n\n".join(
        _strip_chunk_header_text(c.get(body_field, "") or "") for c in chunks
    )
    # v0.2.73 M2: prepend the chunk-map header (▶ shown / · unshown) built
    # from the code sidecar's per-chunk summaries. "" when no sidecar data —
    # byte-identical output for entities without summaries.
    shown_nums: list[int] = []
    for c in chunks:
        try:
            shown_nums.append(int(c.get("chunk_num") or 0))
        except (TypeError, ValueError):
            shown_nums.append(0)
    header = _code_chunk_summaries_header(real_file_path, full_name, shown_nums)
    body_out = assembled or matched_body
    out[body_field] = (header + body_out) if header else body_out
    out["chunks_shown"] = len(chunks)
    return out


# ---------------------------------------------------------------------------
# SHARED code-retrieval-pipeline adapters (v0.2.72 integration, review SEV-1).
#
# `weaviate_mcp.code_ranking.run_code_retrieval_pipeline` operates on the
# NORMALISED candidate shape `{"_c": base, "_s": semantic, "_p": props, ...}`
# and, after reranking, stamps `_rerank`/`_boost`. Its `collapse_fn`/`tier_fn`
# injection points receive those candidate dicts. But the generalized
# `_collapse_to_one_per_node` / `_allocate_tier_within_budget` helpers above
# read FLAT dicts (`file_path`/`full_name`/`combined_score`/`chunk_num` at top
# level). These two factories are the ADAPTER: they flatten `_p` → top-level +
# map `_rerank` → the collapse's `score_field`, so the pure pipeline composes
# with the server-side helpers WITHOUT `code_ranking.py` importing `server`.
#
# BOTH `search_code_graph` (MCP) AND `query_code_graph.py::search_by_concept`
# (CLI/hook) build their pipeline collapse_fn/tier_fn from THESE factories, so
# the two surfaces cannot diverge on collapse/tier behaviour (the hard
# invariant). Do NOT reimplement per-surface.
# ---------------------------------------------------------------------------


def make_code_collapse_fn(*, dedup_kind: str = "code"):
    """Return a `collapse_fn(rows) -> rows` for `run_code_retrieval_pipeline`.

    Collapses multi-chunk matches of one code entity into a single entry,
    keyed on ``(file_path, full_name)`` — reading those from each candidate's
    ``_p`` props, and ranking survivors by the pipeline's ``_rerank`` (the
    boosted score) so the P2 relationship boost drives which chunk wins.
    Preserves ``_s``/``_p``/``_rerank``/``_boost`` on every kept row so the
    downstream tier-formatter still finds the props.
    """
    def collapse_fn(rows: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for r in rows:
            p = r.get("_p") or {}
            # F1 (pre-gate audit, SEV-1): per-collection identity fallback.
            # CodeModule carries `path` (not file_path/full_name); CodeAPI
            # carries `endpoint`+`method`. Without the fallback both flatten
            # to the ("","") key, bucket into ONE group, and only the single
            # best row survives (modules + APIs even merged together).
            # C-7 (v0.2.75 P2b): widen the API/Interaction fallback identity.
            # CodeAPI/CodeInteraction carry no file_path (the V52-O.4 stamp is
            # Function/Class-only), so their collapse key was `("", method+
            # endpoint)`. Same-endpoint rows that differ in `handler` (APIs) or
            # `interaction_type`/`direction`/`raw_target`/`protocol`
            # (interactions) all flattened to ONE key → only the single best row
            # survived (real distinct edges silently dropped). Fold the
            # distinguishing fields into the fallback name so those rows keep
            # separate identities. Function/Class rows are unaffected (they hit
            # the `full_name` branch first). `handler` is a reference property —
            # str() tolerates a UUID/list/None uniformly (identity only).
            _fallback_name = p.get("full_name") or p.get("path")
            if not _fallback_name:
                _method = str(p.get("method", "") or "")
                _endpoint = str(p.get("endpoint", "") or "")
                _bits = [f"{_method} {_endpoint}".strip()]
                # CodeAPI discriminator.
                _handler = p.get("handler")
                if _handler:
                    _bits.append(f"handler={_handler}")
                # CodeInteraction discriminators.
                for _fld in ("interaction_type", "direction", "raw_target", "protocol"):
                    _v = p.get(_fld)
                    if _v:
                        _bits.append(f"{_fld}={_v}")
                _fallback_name = "|".join(b for b in _bits if b).strip()
            entry = {
                **r,  # preserve _s / _p / _rerank / _boost / _c / _d / _src
                "file_path": p.get("file_path") or p.get("path") or "",
                "full_name": _fallback_name,
                "chunk_num": p.get("chunk_num"),
                # collapse ranks on THIS field → use the boosted score so P2
                # ordering (not raw semantic) decides the surviving chunk.
                "combined_score": r.get("_rerank", r.get("_s", 0.0)),
            }
            # F2 (pre-gate audit, SEV-2): carry the body fields to the top
            # level so the content-identity fingerprint
            # (content_dedup.code_content_text) hashes the REAL body instead
            # of an empty string — an empty fingerprint made every flattened
            # row fall back to the pure-name identity key, which deduped
            # distinct same-name entities across files.
            for _bf in (
                "function_body", "class_body", "module_summary",
                "api_description", "signature",
            ):
                _bv = p.get(_bf)
                if _bv and _bf not in entry:
                    entry[_bf] = _bv
            flat.append(entry)
        collapsed = _collapse_to_one_per_node(
            flat,
            score_field="combined_score",
            key_fields=("file_path", "full_name"),
            chunk_field="chunk_num",
            dedup_kind=dedup_kind,
        )
        # _collapse_to_one_per_node re-sorts by score_field desc; that field is
        # `_rerank` (mapped above), so the pipeline's reranked order is kept.
        return collapsed
    return collapse_fn


def make_code_tier_fn(
    remaining_budget: int = _HYBRID_CHUNK_BUDGET,
    thresholds: dict[str, float] | None = None,
    *,
    min_gate: float | None = None,
):
    """Return a `tier_fn(rows) -> rows` for `run_code_retrieval_pipeline`.

    Annotates each row with a ``_tier`` (summary/single_chunk/three_chunks/full
    or ``discard``) via the shared budget allocator, using the CODE-calibrated
    ``thresholds`` (``_CODE_TIER_THRESHOLDS`` when ``None``) so good code matches
    at 0.22–0.42 aren't discarded by the KG 0.42 gate. Order-preserving; the
    caller's format loop reads ``_tier`` and calls
    ``_format_code_result_by_tier``. ``discard`` rows are dropped (they never
    render — the ONE row-dropping tier_fn allowance in the pipeline contract).

    ``min_gate`` (F4, pre-gate audit): the effective ``min`` threshold derived
    at CALL time — both surfaces pass ``resolve_post_rerank_floor(slot)`` so a
    user/GUI floor override (e.g. lowering to 0.15) actually changes what
    renders in auto mode. Without it the import-time ``_CODE_TIER_THRESHOLDS
    ["min"]`` (0.22) silently re-discarded the 0.15-0.22 rows the pipeline had
    just let through. The OTHER tiers still come from ``thresholds``. ``None``
    → the static default (back-compat for direct callers/tests).
    """
    if thresholds is None:
        thresholds = _CODE_TIER_THRESHOLDS
    if min_gate is not None:
        try:
            thresholds = {**thresholds, "min": float(min_gate)}
        except (TypeError, ValueError):
            pass  # unparseable gate → keep the static default (soft-fail)

    def tier_fn(rows: list[dict]) -> list[dict]:
        budget = remaining_budget
        kept: list[dict] = []
        for r in rows:
            score = float(r.get("_rerank", r.get("_s", 0.0)))
            p = r.get("_p") or {}
            try:
                total = int(p.get("total_chunks") or 1)
            except (TypeError, ValueError):
                total = 1
            tier, cost = _allocate_tier_within_budget(
                score, total_chunks=total, remaining_budget=budget,
                thresholds=thresholds,
            )
            if tier == "discard":
                continue
            r["_tier"] = tier
            budget -= cost
            kept.append(r)
        return kept
    return tier_fn


def _self_project_chunk_fetcher(row: dict, effective_project, fetcher):
    """F5 (pre-gate audit, SEV-3): gate the chunk fetcher to SELF-project rows.

    The chunk fetchers on both surfaces (`_fetch_code_chunks` in the MCP,
    `_code_chunk_fetcher` in the CLI) query the SELF project's collections.
    When the winning row is a PEER hit (cross-project fan-out via
    VCT_CODE_GRAPH_ACCESS_LIST), assembling its three_chunks/full window from
    the self collections stitches together the WRONG project's chunks (or a
    same-named entity's). Returning ``None`` here makes the tier renderer
    degrade to single_chunk — the matched chunk only, which IS the peer's own
    content.

    Shared by BOTH render loops (MCP `search_code_graph` + CLI
    `search_by_concept`) so the two surfaces cannot diverge (the hard
    invariant). ``row["_src"]`` carries the row's source-project filter value
    ("" for self/bare-collection rows).
    """
    src = (row.get("_src") or "") if isinstance(row, dict) else ""
    if not src or not effective_project or src == effective_project:
        return fetcher
    return None


def _strip_chunk_header_text(text: str) -> str:
    """Remove a leading ``[chunk N/total]`` header from a code chunk body.

    Mirror of ``analyze_code_graph._strip_chunk_header`` (must match the
    header format ``server._parse_chunk_header`` accepts). No-op when absent.
    """
    return _CHUNK_HEADER_RE.sub("", text, count=1)


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
    return _render_chunk_map(chunk_summaries, shown_chunk_nums)


def _render_chunk_map(chunk_summaries, shown_chunk_nums: list[int] | None) -> str:
    """Render a chunk_summaries dict as the ▶/· chunk-map header text.

    Shared by the KG header (``_chunk_summaries_header``) and the code header
    (``_code_chunk_summaries_header``) — one concern, one home. Returns ""
    when ``chunk_summaries`` is not a non-empty dict.
    """
    if not isinstance(chunk_summaries, dict) or not chunk_summaries:
        return ""

    shown = set(shown_chunk_nums or [])
    lines = ["[Chunk map:"]
    # Keys are stringified ints — sort numerically when possible.
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


def _code_chunk_summaries_header(
    file_path: str,
    full_name: str,
    shown_chunk_nums: list[int] | None = None,
) -> str:
    """Code analogue of ``_chunk_summaries_header`` (v0.2.73 M2).

    Reads ``chunk_summaries`` from the code sidecar entry keyed on
    ``file_path::full_name`` and renders the ▶ (shown) / · (unshown) chunk
    map so the agent gets a one-line orientation of every chunk of a
    multi-chunk code entity, even those not assembled below. Returns ""
    when the sidecar / entry / chunk_summaries is missing (byte-identical
    output for entities without summaries).
    """
    if not file_path or not full_name:
        return ""
    chunk_summaries = _get_code_format(file_path, full_name, "chunk_summaries")
    return _render_chunk_map(chunk_summaries, shown_chunk_nums)


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
        # Coverage hint (2026-06-15): tell the caller explicitly when the
        # returned chunks already cover the ENTIRE node, so it doesn't waste a
        # Read on the source file. Only emitted when coverage is 100% — when
        # is_partial, the absence of the hint signals "more exists on disk".
        if not is_partial:
            base["coverage"] = "complete"
            base["retrieval_hint"] = (
                "Full node provided (all chunks). No further Read of the source "
                "file is needed."
            )
        return base

    # Single-chunk node (or coll unavailable) — return content as-is.
    if tier == "single_chunk":
        # For multi-chunk nodes where we couldn't fetch, prepend node summary if available.
        node_summary = _sc("summary") if (total_chunks and total_chunks > 1) else None
        if node_summary:
            base["content"] = f"[Node summary: {node_summary}]\n\n{content}"
        else:
            base["content"] = content
            # Single-chunk node returned whole at single_chunk tier → complete.
            if not (total_chunks and total_chunks > 1):
                base["coverage"] = "complete"
                base["retrieval_hint"] = (
                    "Full node provided (single-chunk node). No further Read of "
                    "the source file is needed."
                )
        return base

    # three_chunks / full fallback when chunk fetch failed: return full snippet.
    base["content"] = content
    # Coverage hint (2026-06-15): a single-chunk node (total_chunks <= 1) is
    # returned in its entirety here — this is the common `full`-tier case for
    # the many KG nodes that embed as one chunk (large-context embedders like
    # qwen3 fit ~13.5k tokens / ~50KB per chunk). Tell the caller so it doesn't
    # redundantly Read the source file. For a MULTI-chunk node that reached this
    # fallback because chunk-fetch FAILED, coverage is NOT complete (content is
    # the single matched chunk's snippet) → no hint, so the caller knows to Read.
    if not (total_chunks and total_chunks > 1):
        base["coverage"] = "complete"
        base["retrieval_hint"] = (
            "Full node provided (single-chunk node). No further Read of the "
            "source file is needed."
        )
    return base


# v0.2.21 Step 18: resolved via vct-hub (with env-fallback). Hub failure
# degrades to os.getenv("KG_COLLECTION", "ClaudeKnowledgeGraph") which
# preserves pre-v0.2.21 behaviour.
#
# v0.2.27: ``empty_means_unset=True`` — an explicit empty string in the
# env (e.g. from a stale ``.vscode/settings.json claude-code.env`` block)
# falls through to the default rather than being used literally. An empty
# collection name would propagate to Weaviate queries and cause
# schema-fail with a confusing error message — see the "every configured
# collection schema-failed" bug from 2026-05-22.
KG_COLLECTION = _config_field(
    "kg_collection",
    "KG_COLLECTION",
    "ClaudeKnowledgeGraph",
    empty_means_unset=True,
)
# Cross-project shared collection. Defaults to
# "VibeCodedOrchestrator_KnowledgeGraph" (since v0.2.23 B1; was
# "VibecodedOrchestrator_KnowledgeGraph" v0.2.12–v0.2.22, itself renamed
# from "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E — see
# `vco_lib/project_init.py::_LEGACY_SHARED_KG_NAME` for the pre-v0.2.12
# legacy alias and `_LEGACY_SHARED_KG_NAME_LOWERCASE_C` for the v0.2.12–
# v0.2.22 alias). The bundled cross-project KG is seeded at install time
# from vibecoded-orchestrator/knowledge/.
#
# Asymmetric access (2026-05-01): SHARED_KG_COLLECTION is ALWAYS exposed to
# read paths when set. There is no per-project read opt-out — every project
# always reads the shared KG. The per-project gate below restricts WRITES
# only.
_SHARED_KG_DEFAULT = "VibeCodedOrchestrator_KnowledgeGraph"
# v0.2.21 Step 18: resolved via vct-hub when reachable; env-fallback
# otherwise. When the hub is reachable, an explicit empty value means
# "no shared KG binding for this project" and we honour it AS-IS — the
# asymmetric-access design (module docstring) says a project that's
# been explicitly unbound stays unbound. The default only applies on
# env-fallback (hub unreachable), matching pre-v0.2.21 behaviour.
_cfg_for_shared_kg = _try_resolve_project_config()
if _cfg_for_shared_kg is not None:
    _SHARED_KG_RAW = _cfg_for_shared_kg.shared_kg_collection
else:
    _SHARED_KG_RAW = os.getenv("SHARED_KG_COLLECTION", _SHARED_KG_DEFAULT)
SHARED_KG_COLLECTION = _SHARED_KG_RAW

# Per-project WRITE gate. When true, store_knowledge_node refuses writes
# whose resolved target is SHARED_KG_COLLECTION (scope='shared' or explicit
# match). Reads are unaffected. Default false (writes allowed by default).
#
# Legacy alias: SHARED_KG_OPT_OUT used to gate BOTH reads and writes. We
# now honour it as a write-only fallback when SHARED_KG_WRITE_DISABLED is
# unset, so existing per-project env files keep restricting writes after
# the rename. Removal targeted for ~3 releases (2026-08).
def _resolve_shared_kg_write_disabled() -> bool:
    """Resolve the write-disabled flag from env, honouring the legacy alias.

    Precedence:
      1. SHARED_KG_WRITE_DISABLED (canonical key) — wins if set, even when
         set to a falsy spelling like "false" / "0" / "" so users can
         explicitly RE-ENABLE writes on a project that previously had the
         legacy SHARED_KG_OPT_OUT=true.
      2. SHARED_KG_OPT_OUT (legacy alias) — read only when the canonical
         key is unset (i.e. literally absent from the environment).
      3. False otherwise (default: writes allowed).

    Returns:
        True if shared-KG writes are disabled, False otherwise.
    """
    canonical = os.environ.get("SHARED_KG_WRITE_DISABLED")
    if canonical is not None:
        return canonical.strip().lower() in ("1", "true", "yes")
    legacy = os.environ.get("SHARED_KG_OPT_OUT")
    if legacy is not None:
        return legacy.strip().lower() in ("1", "true", "yes")
    return False


SHARED_KG_WRITE_DISABLED = _resolve_shared_kg_write_disabled()
# Back-compat module attribute. Existing tests and callers may still read
# `SHARED_KG_OPT_OUT` from this module — keep the symbol pointing at the
# resolved write-disabled value so its observable semantics match the new
# write gate (legacy callers that relied on this to gate reads will, by
# design, now read the shared KG anyway).
SHARED_KG_OPT_OUT = SHARED_KG_WRITE_DISABLED


# Per-project READ gate (v0.2.46 — Decision B). Symmetric mirror of the
# WRITE gate above. When true, ``_kg_collections_to_search`` drops
# ``SHARED_KG_COLLECTION`` from the fan-out list — hybrid_search /
# semantic_graph_search stop fanning out to the shared KG for this
# project. The project's own primary KG + any peers granted via the
# access matrix remain searchable.
#
# No legacy alias: ``SHARED_KG_READ_DISABLED`` is canonical from v0.2.46
# onward. Pre-v0.2.46 the read path was unconditional (the asymmetric
# model), so there's no historical key to honour. ``SHARED_KG_OPT_OUT``
# is a WRITE-only alias (see comment above) — it does NOT gate reads.
#
# Default false (reads allowed). Asymmetric-by-default semantic: fresh
# projects READ the shared KG. Users who want strict isolation flip both
# SHARED_KG_READ_DISABLED and SHARED_KG_WRITE_DISABLED to true.
def _resolve_shared_kg_read_disabled() -> bool:
    """Resolve the read-disabled flag from the environment.

    Precedence:
      1. ``SHARED_KG_READ_DISABLED`` (canonical key) — wins if set.
      2. False otherwise (default: reads allowed).

    Returns:
        True if shared-KG reads are disabled for this project,
        False otherwise.
    """
    canonical = os.environ.get("SHARED_KG_READ_DISABLED")
    if canonical is not None:
        return canonical.strip().lower() in ("1", "true", "yes")
    return False


SHARED_KG_READ_DISABLED = _resolve_shared_kg_read_disabled()
# Project-specific documentation collection (e.g. ProjectName_development).
# When set, hybrid_search also searches this collection automatically.
# Auto-pairing convention: the launcher should set `KG_COLLECTION=Foo` AND
# `DEVELOPMENT_COLLECTION=Foo_development` together. We do NOT auto-derive
# here — `write_project_env_files` (Rust) and `_ensure_collections` (install.py)
# are the canonical writers; the server just reads. semantic_graph_search
# uses KG_COLLECTION only — docs have no WikiLinks so graph traversal can't
# find useful neighbors there.
# v0.2.21 Step 18: resolved via vct-hub (with env-fallback to "").
DEVELOPMENT_COLLECTION = _config_field(
    "development_collection", "DEVELOPMENT_COLLECTION", ""
)

# Phase 1.5.C (diagrams): per-project diagrams collection. When set,
# hybrid_search auto-includes it so Claude can discover Mermaid /
# Excalidraw diagrams alongside KG and Development docs without learning
# a new tool. Results from this collection carry ``result_kind="diagram"``
# (see ``_format_obj``) so the launcher / Claude can route the click
# target appropriately (open in DiagramsTab, not as a .md file).
#
# Canonical convention (mirrors KG_COLLECTION + DEVELOPMENT_COLLECTION):
# ``<Basename>_Diagrams``. The launcher's per-project Identity tab will
# project this into ``.claude/settings.json`` ``env``. Unset → diagrams
# are silently skipped (zero crash, zero log noise), which is the
# correct behaviour for projects that don't use the diagrams module.
#
# Resolved via vct-hub when reachable (post Phase 1.5.A wiring), with
# env-fallback to the empty string. ``empty_means_unset=True`` so a
# stale empty env value doesn't poison ``hybrid_search`` (same
# defensive coerce as KG_COLLECTION — see v0.2.27 fix).
DIAGRAMS_COLLECTION = _config_field(
    "diagrams_collection",
    "DIAGRAMS_COLLECTION",
    "",
    empty_means_unset=True,
)


# ─── v0.2.27: Resolution-source tracking + startup logging ──────────────
#
# When users hit a "every configured collection schema-failed" error,
# the surfaced collection names alone aren't enough to debug — they need
# to know WHERE each name came from (hub-resolved? from env? from the
# bundled default?). This tracker records the source per key at module
# load, so error messages + log lines can reference it.
#
# The 2026-05-22 bug that motivated this: MCP attempted
# ``VibeCodedOrchestrator_KnowledgeGraph`` (a default) even though the
# user's ``.vscode/settings.json claude-code.env`` declared
# ``VCODev_KnowledgeGraph``. Without source tracking, the user couldn't
# tell that the VS Code surface wasn't propagating to MCP subprocesses
# on Linux (a known limitation since PR-27 v0.2.12).
def _resolve_source_for(field_name: str, env_name: str, resolved: str, default: str) -> str:
    """Determine where the resolved value came from.

    Returns one of: "hub" | "env" | "default" | "default(empty-env-coerced)".
    Pure function; called once per config field at module load.
    """
    cfg = _try_resolve_project_config()
    if cfg is not None:
        try:
            hub_value = getattr(cfg, field_name, "")
            if hub_value and str(hub_value) == resolved:
                return "hub"
        except Exception:
            pass
    raw_env = os.environ.get(env_name)
    if raw_env is None:
        return "default"
    if raw_env.strip() == "" and resolved == default:
        # Empty-string env coerced to default by empty_means_unset semantic.
        return "default(empty-env-coerced)"
    if raw_env == resolved:
        return "env"
    # Fall-through: hub returned a different value than env, or empty env
    # was honoured literally (SHARED_KG_COLLECTION semantic).
    return "env" if raw_env == resolved else "default"


_KG_COLLECTION_SOURCE = _resolve_source_for(
    "kg_collection", "KG_COLLECTION", KG_COLLECTION, "ClaudeKnowledgeGraph"
)
_SHARED_KG_COLLECTION_SOURCE = _resolve_source_for(
    "shared_kg_collection",
    "SHARED_KG_COLLECTION",
    SHARED_KG_COLLECTION,
    _SHARED_KG_DEFAULT,
)
_DEVELOPMENT_COLLECTION_SOURCE = _resolve_source_for(
    "development_collection", "DEVELOPMENT_COLLECTION", DEVELOPMENT_COLLECTION, ""
)

# Loud startup log so users debugging "wrong collection name" can grep
# logs for "weaviate-kg: resolved" and see exactly which name + source.
# Logged at INFO (the MCP's default level) — not WARNING, because the
# common case is correctly-resolved values; warnings would be noise.
# Only escalate to WARNING when we're falling back to the bundled
# defaults (signals likely env-propagation problem).
def _log_collection_resolution() -> None:
    """One-shot startup log of the resolved collection names + sources."""
    logger.info(
        "weaviate-kg: resolved collections (kg=%r src=%s, shared=%r src=%s, dev=%r src=%s)",
        KG_COLLECTION,
        _KG_COLLECTION_SOURCE,
        SHARED_KG_COLLECTION,
        _SHARED_KG_COLLECTION_SOURCE,
        DEVELOPMENT_COLLECTION,
        _DEVELOPMENT_COLLECTION_SOURCE,
    )
    fallback_keys = []
    if _KG_COLLECTION_SOURCE in ("default", "default(empty-env-coerced)"):
        fallback_keys.append(
            f"KG_COLLECTION→'{KG_COLLECTION}'"
            f"{' (empty env coerced)' if _KG_COLLECTION_SOURCE == 'default(empty-env-coerced)' else ''}"
        )
    if _SHARED_KG_COLLECTION_SOURCE == "default":
        fallback_keys.append(f"SHARED_KG_COLLECTION→'{SHARED_KG_COLLECTION}'")
    if fallback_keys:
        logger.warning(
            "weaviate-kg: using bundled defaults for %s — set these env vars "
            "in .claude/settings.json `env` to point at your project's "
            "collections (NOT .vscode/settings.json claude-code.env, which "
            "does not propagate to MCP subprocesses on Linux; see PR-27 / "
            "v0.2.12). The launcher GUI's per-project Identity tab writes "
            "the canonical file.",
            ", ".join(fallback_keys),
        )


_log_collection_resolution()


# ─── v0.2.40 W40-C: warn when SHARED_KG_COLLECTION names a missing class ───
#
# The MCP subprocess cannot read launcher.db (no SQLite + no filesystem-
# canonical-DB-path resolution in this scope) so it stays env-only for
# SHARED_KG_COLLECTION resolution (Priority-5 fallback per the design doc).
# What we CAN do is surface misconfig: if the env value names a Weaviate
# class that doesn't exist, the user has a stale env (typical after a
# launcher.db rebind to a new prefix without an env regen) or a flat-out
# wrong value.
#
# Loud-but-soft: emit a structured WARNING via the existing logger. Don't
# crash, don't change behaviour — the existing search-time failures
# (`every configured collection schema-failed`) ALREADY surface the issue
# loudly, but at search-time, after the user has tried 3-4 hybrid_searches
# and lost trust. Surfacing the problem on first connect means the user
# sees the misconfig in the MCP log immediately on session start
# (effectively, on the first MCP tool call that touches Weaviate).
#
# Invocation: deferred to first successful `get_weaviate_client()` call
# (guarded by `_shared_kg_probe_done`) rather than module-load time, so
# (a) a slow / cold Weaviate doesn't block MCP startup, and (b) we don't
# fire before the global `weaviate_client` is populated. One-shot per
# process lifetime.
def _warn_if_shared_kg_class_missing() -> None:
    """Probe Weaviate for SHARED_KG_COLLECTION; warn if absent.

    v0.2.40 W40-C: diagnostic-only. The MCP cannot self-heal (no
    launcher.db access from a subprocess that doesn't know where the
    DB lives) but it can flag the misconfig so the user understands
    why cross-project search returns empty.

    Called from `get_weaviate_client()` immediately after the first
    successful connect, behind a one-shot guard (`_shared_kg_probe_done`)
    so it runs once per process lifetime.

    Behaviour:
      * SHARED_KG_COLLECTION empty / unset → skip (legitimate no-shared
        configuration).
      * SHARED_KG_COLLECTION set, class exists in Weaviate → skip
        (happy path, no log noise).
      * SHARED_KG_COLLECTION set, class absent → emit a structured
        warning with the env-var name + the resolved source.
      * Weaviate unreachable / schema probe transient error → skip
        silently (the search-time error path surfaces the problem;
        doubling up here is noise).
    """
    if not SHARED_KG_COLLECTION or not SHARED_KG_COLLECTION.strip():
        return
    # Use the already-cached client (caller is `get_weaviate_client`
    # after the connect succeeded). Avoid re-entering `get_weaviate_client`
    # which would recurse into this probe.
    global weaviate_client
    client = weaviate_client
    if client is None:
        # Defensive: should not happen because the caller just set it,
        # but a concurrent close/reset could race. Skip silently.
        return
    try:
        if client.collections.exists(SHARED_KG_COLLECTION):
            return
    except Exception:
        # Schema probe failed for transient reasons (gRPC blip, etc.).
        # Don't escalate — the next real query will retry and surface
        # any persistent failure mode through the proper error path.
        return

    logger.warning(
        "weaviate-kg: SHARED_KG_COLLECTION=%r but no such class exists "
        "in Weaviate. Set SHARED_KG_COLLECTION env to the actual class "
        "name in .claude/settings.json `env`, or re-run launcher boot "
        "to trigger auto-adoption / env regen. Cross-project search "
        "will return empty until this is resolved. (Source: %s)",
        SHARED_KG_COLLECTION,
        _SHARED_KG_COLLECTION_SOURCE,
    )


# NOTE: The probe is invoked at module-end (after `get_weaviate_client`
# is defined) — not here — because the probe depends on the
# `get_weaviate_client` symbol which is declared further down. See the
# call site near the end of the module-load block.


# ─── Multi-source access matrix (P1-D, 2026-05-08) ─────────────────────────
#
# The launcher's GUI access matrix is propagated into env vars that the MCP
# server (and bundled hooks) read at process start to fan-out KG / code-graph
# searches across peer projects. Without these vars, the matrix was a
# launcher-internal feature with no runtime effect.
#
# Format (set by `write_project_env_files` in Rust, in `.claude/env`
# AND `.claude/settings.json env` — PR-27 / v0.2.12 removed the historical
# third surface `.vscode/settings.json claude-code.env` after sentinel
# testing on `/proc/<mcp_pid>/environ` proved it does NOT propagate to
# MCP subprocesses on Linux. Users editing the VS Code key for KG/code-
# graph routing report "settings didn't take" — they need to use the
# canonical `.claude/settings.json env` channel instead, or the launcher
# GUI which writes both files):
#
#   VCT_KG_ACCESS_LIST=PeerA,PeerB,PeerC
#       Comma-separated peer project NAMES (already sanitized to the
#       collection-prefix shape). Empty / unset = no peers granted (the
#       default).
#   VCT_CODE_GRAPH_ACCESS_LIST=PeerA,PeerB
#       Same shape, for the code-graph access matrix.
#
# Each peer maps to one KG collection (`<Peer>_KnowledgeGraph`) for KG and
# 5 collections (`<Peer>_CodeFunction`, `<Peer>_CodeClass`, ...) for code.
# Resolution is done via `_sanitize_collection_prefix` which is idempotent
# for already-sanitized inputs.

def _parse_csv_env(name: str) -> list[str]:
    """Parse a comma-separated env var, dropping empties and stripping
    whitespace. Returns [] when the var is unset or empty."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _kg_peer_collections() -> list[str]:
    """Return the list of peer-project KG collection names this process
    should search.

    v0.2.21 Step 18 (caller migration): prefer the launcher's
    hub-resolved ``kg_access_list`` (already-canonical collection names,
    no sanitization needed) over the legacy ``VCT_KG_ACCESS_LIST`` CSV
    env var. Falls back to the env CSV when the hub is unreachable
    (parent plan §8.11 — VCT_KG_ACCESS_LIST is the env-fallback path).

    The hub returns the FULL access list (self + shared + peers, all
    canonical Weaviate class names); we filter to peers only so the
    caller of ``_kg_collections_to_search`` can prepend self/shared in
    their canonical positions. Pre-v0.2.21 env-fallback path keeps the
    sanitize-then-suffix logic for the legacy CSV format.
    """
    # Hub-first path: kg_access_list is canonical class names.
    _cfg_for_peers = _try_resolve_project_config()
    if _cfg_for_peers is not None:
        out: list[str] = []
        seen: set[str] = set()
        for coll in _cfg_for_peers.kg_access_list:
            # Skip self / shared — caller of _kg_collections_to_search
            # prepends those in their canonical order.
            if coll == KG_COLLECTION or coll == SHARED_KG_COLLECTION:
                continue
            if coll in seen or not coll:
                continue
            seen.add(coll)
            out.append(coll)
        return out

    # Env-fallback path (pre-v0.2.21 / hub unreachable).
    peers = _parse_csv_env("VCT_KG_ACCESS_LIST")
    out = []
    seen = set()
    for p in peers:
        # `_sanitize_collection_prefix` is idempotent for already-sanitized
        # input; this lets the launcher pass either form (it always passes
        # the sanitized form, but we tolerate both for forward-compat with
        # tooling that builds its own VCT_KG_ACCESS_LIST).
        prefix = _sanitize_collection_prefix(p)
        if not prefix:
            continue
        coll = f"{prefix}_KnowledgeGraph"
        if coll in seen:
            continue
        seen.add(coll)
        out.append(coll)
    return out


def _diagrams_peer_collections() -> list[str]:
    """Return the list of peer-project diagrams-collection names this
    process should also search.

    Source-of-truth (v0.2.34 A7):

      * Hub: ``ProjectConfig.diagrams_access_list`` — already-canonical
        Weaviate class names (e.g. ``Foo_Diagrams``). Consumed as-is.
      * Env-fallback: ``VCT_DIAGRAMS_ACCESS_LIST`` — CSV of peer
        project names (the launcher's display names); each is
        sanitised through ``_sanitize_collection_prefix`` and the
        ``_Diagrams`` suffix is appended.

    Discrete from the KG access matrix. v0.2.34 A7 removed the
    ``VCT_KG_ACCESS_LIST`` env-fallback that Phase 1.5.C left in
    place. Empty list now means **no peers** — granting only KG
    access no longer leaks diagram visibility, and granting only
    diagram access is now reachable (previously invisible to the MCP
    because there was no KG row to piggyback on). See
    ``knowledge/concepts/config-projection-contract-2026-05-24.md``
    for the canonical-key registration.

    The hub-side value is populated by ``vct-hub::project_config``
    via a JOIN over ``diagram_access`` + ``projects``; the env-side
    value is populated by ``vco_lib.config_projection`` via
    ``_fetch_diagram_access_list`` (same JOIN, returning ``p.name``).
    The two surfaces agree on **what peers** are visible; they
    disagree only on **format** (hub: canonical class names; env:
    raw project names) — both consumed correctly by the branches
    below.
    """
    # Hub-first path: if the hub exposes ``diagrams_access_list``, use
    # it. Falls back to env CSV otherwise.
    _cfg = _try_resolve_project_config()
    if _cfg is not None:
        try:
            hub_list = list(getattr(_cfg, "diagrams_access_list", []) or [])
        except Exception:
            hub_list = []
        if hub_list:
            out: list[str] = []
            seen: set[str] = set()
            for coll in hub_list:
                if not coll or not isinstance(coll, str):
                    continue
                if coll == DIAGRAMS_COLLECTION:
                    continue
                if coll in seen:
                    continue
                seen.add(coll)
                out.append(coll)
            return out

    # Env-fallback path. v0.2.34 A7: read VCT_DIAGRAMS_ACCESS_LIST
    # exclusively — no KG-list fallback. Empty / unset env var means
    # no peers; the MCP returns [] and the per-project DIAGRAMS_COLLECTION
    # is the only collection searched (see _diagrams_collections_to_search).
    peers = _parse_csv_env("VCT_DIAGRAMS_ACCESS_LIST")
    out: list[str] = []
    seen: set[str] = set()
    for p in peers:
        prefix = _sanitize_collection_prefix(p)
        if not prefix:
            continue
        coll = f"{prefix}_Diagrams"
        if coll in seen:
            continue
        if coll == DIAGRAMS_COLLECTION:
            continue
        seen.add(coll)
        out.append(coll)
    return out


def _diagrams_collections_to_search() -> list[str]:
    """Return the union of diagrams collections this process should
    fan-out across: self + peers (from the hub's
    ``diagrams_access_list`` or, when the hub is unreachable, the
    ``VCT_DIAGRAMS_ACCESS_LIST`` env var). No shared diagrams
    collection — diagrams are project-scoped by design (unlike the
    shared KG). v0.2.34 A7 removed the ``VCT_KG_ACCESS_LIST``
    fallback this used to honour during Phase 1.5.C.

    Returns ``[]`` when ``DIAGRAMS_COLLECTION`` is unset, which is the
    correct behaviour for projects that don't use the diagrams module
    (``hybrid_search`` then silently skips the diagrams fan-out, no
    log noise, no schema-fail).
    """
    if not DIAGRAMS_COLLECTION or not DIAGRAMS_COLLECTION.strip():
        return []
    out: list[str] = [DIAGRAMS_COLLECTION]
    for coll in _diagrams_peer_collections():
        if not coll or not coll.strip():
            continue
        if coll == DIAGRAMS_COLLECTION:
            continue
        if coll not in out:
            out.append(coll)
    return out


def _kg_collections_to_search(
    include_dev: bool = False, include_diagrams: bool = False
) -> list[str]:
    """Return the union of KG collections this process should fan-out
    across: self + shared (when configured + distinct) + every peer in
    `VCT_KG_ACCESS_LIST`. Order: self first, shared second, peers after.
    Caller may pass `include_dev=True` to also include
    `DEVELOPMENT_COLLECTION` (only `hybrid_search` does — graph traversal
    skips dev docs, see existing comment at the call site). Caller may
    pass `include_diagrams=True` to also include the per-project
    `DIAGRAMS_COLLECTION` + diagram-access peers (Phase 1.5.C — only
    `hybrid_search` does; graph traversal skips diagrams for the same
    reason it skips dev docs — they have no WikiLinks).

    Defensive filtering (v0.2.27): empty / whitespace-only collection
    names are dropped. KG_COLLECTION should never be empty (the resolver
    coerces empty env to the bundled default), but the access matrix is
    user-controlled and could carry an empty entry; without this filter
    that empty propagates to Weaviate as an unresolvable class name and
    schema-fails.
    """
    out: list[str] = []
    if KG_COLLECTION and KG_COLLECTION.strip():
        out.append(KG_COLLECTION)
    # v0.2.46 Decision B — SHARED_KG_READ_DISABLED is the symmetric mirror
    # of SHARED_KG_WRITE_DISABLED. When true the shared collection drops
    # out of the fan-out so hybrid_search / semantic_graph_search stop
    # searching it for this project. Same single-line gate covers both
    # callers (``hybrid_search`` + ``semantic_graph_search``) because both
    # route through this helper.
    if (
        SHARED_KG_COLLECTION
        and SHARED_KG_COLLECTION.strip()
        and SHARED_KG_COLLECTION != KG_COLLECTION
        and not SHARED_KG_READ_DISABLED
    ):
        out.append(SHARED_KG_COLLECTION)
    for coll in _kg_peer_collections():
        if not coll or not coll.strip():
            continue
        if coll == KG_COLLECTION or coll == SHARED_KG_COLLECTION:
            continue
        if coll not in out:
            out.append(coll)
    if (
        include_dev
        and DEVELOPMENT_COLLECTION
        and DEVELOPMENT_COLLECTION.strip()
        and DEVELOPMENT_COLLECTION not in out
    ):
        out.append(DEVELOPMENT_COLLECTION)
    if include_diagrams:
        for coll in _diagrams_collections_to_search():
            if coll and coll not in out:
                out.append(coll)
    return out


def _describe_collection_source(coll_name: str) -> str:
    """Tag a collection name with its resolution source for error messages.

    Used by ``_format_failed_collections_hint`` to surface WHERE the
    failing collection name came from (self/shared/peer/dev + the env
    or hub-resolved origin). Pure function; safe to call from hot
    error paths.
    """
    if coll_name == KG_COLLECTION:
        return f"self/KG_COLLECTION src={_KG_COLLECTION_SOURCE}"
    if coll_name == SHARED_KG_COLLECTION:
        return f"shared/SHARED_KG_COLLECTION src={_SHARED_KG_COLLECTION_SOURCE}"
    if coll_name == DEVELOPMENT_COLLECTION:
        return f"dev/DEVELOPMENT_COLLECTION src={_DEVELOPMENT_COLLECTION_SOURCE}"
    return "peer/VCT_KG_ACCESS_LIST"


def _format_failed_collections_hint(failed: list[str]) -> str:
    """Format a debug-friendly listing of failed collections + sources.

    Returns a string like::

        VibeCodedOrchestrator_KnowledgeGraph [self/KG_COLLECTION src=default(empty-env-coerced)],
        VibeCodedOrchestrator_Development [peer/VCT_KG_ACCESS_LIST]

    Example resolution for orchestrator-root (post-v0.2.44)::

        KG_COLLECTION        = VibeCodedOrchestrator_KnowledgeGraph  [self/KG_COLLECTION]
        SHARED_KG_COLLECTION = VibeCodedOrchestrator_KnowledgeGraph  [self/SHARED_KG_COLLECTION]
        (Both env keys hold the same canonical name; one physical collection
         serves both primary and shared roles for orchestrator-root.)

    Shown after the truncated raw list in WeaviateSchemaError messages
    so users debugging a "no results" bug can see at a glance whether
    the MCP picked up the right env vars.
    """
    if not failed:
        return ""
    annotated = [f"{c} [{_describe_collection_source(c)}]" for c in failed[:6]]
    suffix = "…" if len(failed) > 6 else ""
    return ", ".join(annotated) + suffix
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


# Default project / collection prefix for code graph queries.
# v0.2.23 W3 (2026-05-21) — slug-vs-prefix fix: source the resolved value
# from `cfg.code_graph_collection_prefix` (the canonical Weaviate prefix
# from `project_codegraph_bindings.collection_prefix`), NOT
# `cfg.code_graph_project` (a slug alias that diverges from the binding
# row when the project's slug isn't already a valid Weaviate class
# prefix — e.g. `orchestrator-root` would re-sanitise to
# `Orchestrator_root` ≠ `VibeCodedOrchestrator` binding-row truth).
# Symptom of the pre-fix bug: silent 0-result MCP searches +
# writes-to-zombie-collections after any project rename.
#
# Fall-through honours the historical env precedence CODE_GRAPH_PROJECT
# > PROJECT_NAME (for environments without a hub-resolvable config).
_cfg_for_cgp = _try_resolve_project_config()
if _cfg_for_cgp is not None and _cfg_for_cgp.code_graph_collection_prefix:
    CODE_GRAPH_PROJECT = _cfg_for_cgp.code_graph_collection_prefix
else:
    CODE_GRAPH_PROJECT = os.getenv("CODE_GRAPH_PROJECT") or os.getenv("PROJECT_NAME", "")


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as a Weaviate collection prefix.

    **Canonical rule** (cross-language, locked 2026-05-25 by cr-b2):
    delegates to ``vco_lib.project_init.sanitize_for_weaviate_class`` —
    the documented source-of-truth per ``derive_project_collection_names``'s
    docstring. Replaces the pre-cr-b2 underscore-replace implementation
    which diverged from the canonical Python rule (and from the
    indexer's writer-side naming) for any project name containing
    non-alphanumeric characters (spaces, hyphens, dots). The
    divergence silently broke cross-project diagrams visibility on
    first invocation — the indexer wrote under one class, this MCP
    searched a different one, the hub's ``diagrams_access_list``
    pointed at a third.

    Rule (must match ``sanitize_for_weaviate_class``):
      1. Split on any non-alphanumeric run (``[^A-Za-z0-9]+``).
      2. PascalCase each surviving part (uppercase first char,
         preserve rest).
      3. Concatenate (NO joiner — no underscore between parts).
      4. If nothing survives OR result starts with a non-letter,
         fall back to ``"vct"`` (lowercase — Weaviate uppercases on
         POST regardless).

    The Rust mirror is
    ``launcher/src-tauri/vct-hub/src/config_api.rs::sanitize_diagrams_class_prefix``;
    cross-language parity is pinned by
    ``tests/test_diagrams_class_name_parity.py`` /
    ``launcher/src-tauri/tests/diagrams_class_name_parity.rs``
    consuming the shared JSON fixture at
    ``tests/fixtures/diagrams_class_name_parity.json``.

    Fallback: if ``vco_lib`` isn't importable (half-installed env),
    re-implements the same rule inline. The fallback path is
    behaviour-identical to the imported function — kept so the MCP
    boots on partial installs rather than crashing at first call.
    """
    if _HAS_CANONICAL_SANITIZER:
        try:
            return _canonical_sanitize_for_weaviate_class(name)
        except Exception:
            # Defensive: never let a sanitiser exception take the MCP down.
            # Falls through to the inline implementation below.
            pass

    # Inline fallback — behaviour-identical to
    # `sanitize_for_weaviate_class`. Kept for partial-install resilience.
    base = name or ""
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", base) if p]
    if not parts:
        return "vct"
    pascal = "".join(p[:1].upper() + p[1:] for p in parts)
    if not pascal or not pascal[0].isalpha():
        return "vct"
    return pascal


def _code_sanitize_collection_prefix(name: str) -> str:
    """CODE-GRAPH-ONLY prefix sanitizer (v0.2.74, BLOCKER-1).

    The underscore-PRESERVING rule (`canonical_class_prefix`) that the ANALYZER
    writes Code* classes with and that launcher.db `project_codegraph_bindings.
    collection_prefix` records. This is DELIBERATELY DIFFERENT from the shared
    `_sanitize_collection_prefix` (underscore-DROPPING), which diagrams + KG use
    and which is pinned by `test_diagrams_class_name_parity.py` + the Rust
    mirror. Routing the code-graph class-name construction through THIS resolver
    (instead of repointing the shared one) keeps the reader's class name equal
    to the writer's for ANY project name — including underscored ones — without
    splitting diagrams/KG resolution.

    Fallback: when `canonical_class_prefix` isn't importable (half-install), fall
    back to the dropping sanitizer — correct only for non-underscored names, but
    keeps the MCP booting rather than crashing (same posture as the shared one).
    """
    if _HAS_CODE_CANONICAL_PREFIX:
        try:
            return _canonical_class_prefix(name)
        except Exception:  # never let a sanitiser exception take the MCP down
            pass
    # Half-install fallback: the dropping rule (parity holds for names with no
    # underscore, which is the common case; underscored names degrade to the
    # pre-fix behaviour until vco_lib is refreshed).
    return _sanitize_collection_prefix(name)


def _code_collection(base: str) -> str:
    """Return per-project code graph collection name.

    Uses CODE_GRAPH_PROJECT env var as prefix. Falls back to bare name
    for backward compatibility if not set. v0.2.74: uses the code-graph-only
    underscore-PRESERVING sanitizer so the read class == the analyzer's write
    class for underscored project names (BLOCKER-1).
    """
    if CODE_GRAPH_PROJECT:
        prefix = _code_sanitize_collection_prefix(CODE_GRAPH_PROJECT)
        return f"{prefix}_{base}"
    return base


# Maximum approximate token count for a single Weaviate insert.
# qwen3-embedding supports 32k tokens but we keep a conservative 2000-token limit
# for chunk granularity (legacy snowflake-arctic-embed2 limit; also good for retrieval).
# 2000 tokens ≈ 8 000 chars (1 token ≈ 4 chars).
_MAX_SINGLE_CHUNK_TOKENS = 2000


class WeaviateUnreachable(Exception):
    """Raised when Weaviate is not reachable (HTTP refused / gRPC down).

    Carries a user-actionable message in `.user_msg` for the MCP layer to
    surface to the agent. The bare exception message stays terse; the
    full hint (commands to verify + recover) is in user_msg.
    """
    def __init__(self, msg: str, user_msg: str):
        super().__init__(msg)
        self.user_msg = user_msg


class WeaviateSchemaError(Exception):
    """Raised when the underlying Weaviate operation fails due to schema
    mismatch (class missing, property missing, indexNullState missing),
    NOT a connection problem. Triggers _reset_weaviate_client_cache() so
    the MCP picks up a freshly-migrated schema on the next call (PR-41,
    Issue A from mcp-instability-vs-public-repo-2026-05-16.md).

    The user_msg hints at the relevant migration script:
    scripts/migrate-development-temporal-props.{sh,ps1} for property
    issues, scripts/migrate-shared-kg-schema.{sh,ps1} for class/index
    issues.
    """
    def __init__(self, msg: str, user_msg: str = ""):
        super().__init__(msg)
        self.user_msg = user_msg or msg


class WeaviateAuthError(Exception):
    """Raised when the underlying Weaviate operation fails due to auth
    (401, 403, invalid API key). Distinct from connection failures (which
    suggest container restart) and schema failures (which suggest
    migration). Does NOT trigger cache reset — auth errors are
    persistent across reconnects (PR-41, Issue F from
    mcp-instability-vs-public-repo-2026-05-16.md).
    """
    def __init__(self, msg: str, user_msg: str = ""):
        super().__init__(msg)
        self.user_msg = user_msg or msg


class WeaviateWorkspaceDriftError(Exception):
    """v0.2.74 T5-1 backstop: raised when a tool call's LIVE
    ``CLAUDE_PROJECT_DIR`` diverges from the value this MCP subprocess was
    SPAWNED with (``_MODULE_LOAD_WORKSPACE``).

    All module-level collection constants (KG_COLLECTION /
    SHARED_KG_COLLECTION / DEVELOPMENT_COLLECTION / CODE_GRAPH_PROJECT / ...)
    are resolved ONCE at import from the spawn-time workspace and read in
    dozens of places; they cannot be hot-swapped safely mid-process. So when
    a client binds to a stale-workspace subprocess (the T5-1 double-process
    hazard the ``vco_lib.mcp_singleton`` reaper primarily prevents), the
    CORRECT response is to REFUSE LOUD — name both paths, tell the user to
    restart the MCP — rather than silently serve the wrong project's
    collections and return 0 hits. Distinct from the connection / schema /
    auth error classes so the wrapper surfaces it verbatim instead of
    misclassifying it as a Weaviate outage.
    """
    def __init__(self, msg: str, user_msg: str = ""):
        super().__init__(msg)
        self.user_msg = user_msg or msg


def _assert_workspace_unchanged(tool_name: str = "hybrid_search") -> None:
    """v0.2.74 T5-1 guard: refuse-loud if THIS PROCESS'S env workspace mutates.

    Compares the current ``CLAUDE_PROJECT_DIR`` against the value captured at
    module import (``_MODULE_LOAD_WORKSPACE``) and raises
    ``WeaviateWorkspaceDriftError`` (naming both paths + "restart the MCP")
    when they differ.

    HONESTY NOTE (Fable-review F4) — scope this correctly: both values are
    read from THE SAME process's environment, and a stdio MCP subprocess's env
    never changes in normal operation, so this check CANNOT detect the
    cross-session stale-peer scenario at runtime (a client holding a pipe to a
    wrong-workspace process — that process's own env is self-consistent). The
    ACTIVE mitigation for stale peers is the spawn-time reaper
    (``vco_lib.mcp_singleton``), whose parenthood rule covers every real drift
    scenario (a stale handle is always a pipe to a process the client's own
    parent spawned). What THIS guard actually protects:
      * exotic in-process env mutation (an embedded/test harness, a future
        hot-reload path, a wrapper that mutates ``os.environ``), and
      * the documented, testable refuse-loud CONTRACT for drift (the error
        shape clients/tests rely on).
    True per-call drift detection would require client-supplied workspace
    info (MCP ``roots``) — a protocol-level follow-up, not an env compare.

    No-op (never raises) when:
      * the module-load workspace was empty (CLI / non-workspace spawn — no
        baseline to diverge from), OR
      * the live env is empty (a hook / script call that didn't re-set the
        env — we trust the spawn-time value), OR
      * the two canonicalize equal (symlink / trailing-slash noise).

    Soft on its OWN failure: a bug in path canonicalization must not break a
    search, so path resolution errors fall through to "no drift".
    """
    load_ws = _MODULE_LOAD_WORKSPACE
    if not load_ws:
        return  # spawned without a workspace baseline — nothing to compare
    live_ws = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not live_ws:
        return  # call arrived without the env — trust the spawn-time value
    try:
        load_norm = str(Path(load_ws).resolve())
        live_norm = str(Path(live_ws).resolve())
    except Exception:  # noqa: BLE001 — canonicalization bug must not break search
        load_norm, live_norm = load_ws.rstrip("/\\"), live_ws.rstrip("/\\")
    if load_norm == live_norm:
        return
    msg = (
        f"{tool_name}: workspace drift detected — this weaviate-kg MCP "
        f"subprocess was spawned for CLAUDE_PROJECT_DIR={load_norm!r} "
        f"(collections resolved: KG_COLLECTION={KG_COLLECTION!r}, "
        f"SHARED_KG_COLLECTION={SHARED_KG_COLLECTION!r}) but this call "
        f"arrived with CLAUDE_PROJECT_DIR={live_norm!r}. The module-level "
        f"collection constants are frozen at import and cannot be "
        f"hot-swapped, so serving this call would fan out over the WRONG "
        f"project's collections. RESTART the weaviate-kg MCP (or your Claude "
        f"Code session) so a fresh subprocess resolves collections for "
        f"{live_norm!r}. (If you see two weaviate_mcp/server.py processes, "
        f"the stale one should have been reaped at spawn — check the "
        f"'reap_stale_weaviate_mcp' startup log.)"
    )
    logger.warning("workspace-drift refuse-loud: %s", msg)
    raise WeaviateWorkspaceDriftError(msg)


def _workspace_drift_response(exc: "WeaviateWorkspaceDriftError", query: str = "") -> str:
    """Structured refuse-loud response for a workspace-drift error.

    Mirrors the shape of ``_weaviate_unreachable_response`` so the caller
    surface is consistent: a JSON envelope Claude can read, with an
    ``error_class`` discriminator and the actionable ``user_msg``.
    """
    return _large_result({
        "error": True,
        "error_class": "WeaviateWorkspaceDriftError",
        "query": query,
        "results": [],
        "message": exc.user_msg,
    })


_shared_kg_probe_done: bool = False


def get_weaviate_client():
    """Get or create Weaviate client.

    Raises WeaviateUnreachable on connection failure with an actionable
    user_msg pointing at the most likely root causes (port-binding
    desync, container down, healthcheck flap).
    """
    global weaviate_client, _shared_kg_probe_done
    if weaviate_client is None:
        http_host = WEAVIATE_URL.replace("http://", "").replace("https://", "").split(":")[0]
        http_port = int(WEAVIATE_URL.split(":")[-1]) if ":" in WEAVIATE_URL else 8081

        try:
            weaviate_client = weaviate.connect_to_custom(
                http_host=http_host,
                http_port=http_port,
                http_secure=False,
                grpc_host=http_host,
                grpc_port=GRPC_PORT,
                grpc_secure=False
            )
        except Exception as exc:
            # Loud-fail v2 (2026-05-08 silent-zero antipattern fix). Don't
            # cache failed client (global stays None so the next call retries;
            # transient port-binding glitches recover automatically).
            weaviate_client = None
            raise WeaviateUnreachable(str(exc), _build_unreachable_hint(exc)) from exc
        logger.info(f"✓ Connected to Weaviate at {WEAVIATE_URL}")

        # v0.2.40 W40-C: one-shot diagnostic — on first successful
        # Weaviate connect, probe SHARED_KG_COLLECTION existence and
        # surface a structured WARNING if the env value points at a
        # missing class. Deferred to here (rather than module-load
        # time) because `get_weaviate_client` may not be ready at
        # module-import time on cold-start machines. Guarded by a
        # one-shot flag so we don't re-probe on every call.
        if not _shared_kg_probe_done:
            _shared_kg_probe_done = True
            try:
                _warn_if_shared_kg_class_missing()
            except Exception:
                # Diagnostic — never let it interfere with the
                # caller's real work.
                pass
    return weaviate_client


def _weaviate_unreachable_response(exc: WeaviateUnreachable, query: str = "") -> str:
    """Format WeaviateUnreachable as the structured failure response that
    MCP search tools (hybrid_search / semantic_graph_search /
    search_code_graph / query_code_structure) return verbatim. Loud-fail
    per 2026-05-08 silent-zero antipattern fix.
    """
    return json.dumps({
        "success": False,
        "error": "Weaviate unreachable",
        "error_class": "WeaviateUnreachable",
        "query": query,
        "hint": exc.user_msg,
    }, indent=2)


def _weaviate_schema_error_response(exc: "WeaviateSchemaError", query: str = "") -> str:
    """Format WeaviateSchemaError as a structured failure response.
    Distinct error_class lets downstream agents react with the right
    recovery (run migration script, NOT restart container). PR-41.
    """
    return json.dumps({
        "success": False,
        "error": "Weaviate schema error",
        "error_class": "WeaviateSchemaError",
        "query": query,
        "hint": exc.user_msg,
    }, indent=2)


def _weaviate_auth_error_response(exc: "WeaviateAuthError", query: str = "") -> str:
    """Format WeaviateAuthError as a structured failure response. Distinct
    error_class lets downstream agents react with the right recovery
    (check API key in settings, NOT restart container). PR-41.
    """
    return json.dumps({
        "success": False,
        "error": "Weaviate auth error",
        "error_class": "WeaviateAuthError",
        "query": query,
        "hint": exc.user_msg,
    }, indent=2)


def _build_unreachable_hint(exc: Exception) -> str:
    """Build the actionable user_msg shown to the agent on loud-fail."""
    # Canonical container name is `vco_weaviate` (v0.2.x+). Older installs
    # may have `weaviate` (v0.1.x unprefixed) or `weaviate_claude`
    # (pre-VCO maintainer-machine name). The authoritative registry is
    # in vco_lib/containers.py — see CANONICAL_CONTAINERS["weaviate"] and
    # HISTORICAL_ALIASES["weaviate"]. We don't import vco_lib here to
    # keep the MCP server free of repo-root sys.path coupling; this
    # string is intentionally a copy of the canonical value.
    return (
        f"Weaviate unreachable at {WEAVIATE_URL} (gRPC :{GRPC_PORT}). "
        "This is the loud-fail behaviour added 2026-05-08 — earlier the MCP "
        "would silently return success:true count:0 on connection refused, "
        "which let multi-hour sessions run thinking the KG was empty. "
        "Common causes + fixes:\n"
        "  1. Container down: `podman ps | grep weaviate` and check status.\n"
        "  2. Host port unbound while container reports 'running' (Podman state-DB "
        "desync): `curl -sf http://localhost:8081/v1/meta` from host. If it fails, "
        "force-recreate: `podman rm -f vco_weaviate && podman-compose up -d weaviate` "
        "(legacy installs may use `weaviate` or `weaviate_claude` in place of "
        "`vco_weaviate` — check `podman ps -a --format '{{.Names}}'`).\n"
        "  3. Stuck healthcheck restart-loop (pre-2026-05-08 compose used the "
        "strict `/v1/.well-known/ready` endpoint that 503s during legitimate "
        "operations): verify compose.yaml's healthcheck uses `/v1/meta` + "
        "`start_period: 60s`.\n"
        "Underlying error: " + str(exc)[:200]
    )


_SCHEMA_ERROR_PATTERNS = (
    "could not find class",
    "class not found",
    "no such prop",
    "no such property",
    "build inverted filter",
    "nested query",
)

_AUTH_ERROR_PATTERNS = (
    "unauthorized",
    "forbidden",
    "401",
    "403",
    "invalid api key",
    "authentication failed",
    "authentication",
)

_CONNECTION_ERROR_PATTERNS = (
    "connection refused",
    "unavailable",
    "failed to connect",
    "connection error",
    "cannot connect",
    # "grpc" alone is too aggressive: every WeaviateQueryError stringifies
    # with a "protocol GRPC search" prefix even for non-transport failures.
    # Restrict to phrases that genuinely indicate gRPC transport breakage.
    "grpc transport",
    "grpc connection",
    "grpc unavailable",
    "grpc server is down",
    "grpc handshake",
)


def _build_schema_error_hint(exc: Exception, lower_msg: str) -> str:
    """Build a user-friendly hint for schema errors pointing at the right
    migration script (PR-41 Issue F)."""
    if "no such prop" in lower_msg or "no such property" in lower_msg:
        return (
            f"Schema error: {exc}. The collection is missing a required "
            f"property. Run scripts/migrate-development-temporal-props.sh "
            f"to add temporal properties (created/updated/valid_from/"
            f"valid_until) to existing *_Development collections."
        )
    if "build inverted filter" in lower_msg or "nested query" in lower_msg:
        return (
            f"Schema error: {exc}. The collection lacks "
            f"invertedIndexConfig.indexNullState=True. Weaviate <=1.30 "
            f"cannot retroactively add this; run "
            f"scripts/migrate-shared-kg-schema.sh to drop + recreate the "
            f"shared KG with the correct schema (content is re-synced "
            f"from knowledge/**/*.md)."
        )
    # "could not find class" / "class not found"
    return (
        f"Schema error: {exc}. The expected class is not in the Weaviate "
        f"schema. If you just ran a migration, the MCP's client cache "
        f"will be reset on retry. If the class was never created, run "
        f"install.py --update to recreate it, OR use the launcher GUI's "
        f"Identity tab 'Manage shared KG collection' picker to designate "
        f"an existing orchestrator-shaped class as canonical."
    )


def _build_auth_error_hint(exc: Exception, lower_msg: str) -> str:
    """Build a user-friendly hint for auth errors (PR-41 Issue F)."""
    return (
        f"Authentication error: {exc}. Check WEAVIATE_API_KEY in your "
        f".claude/settings.json env block, or remove it if your local "
        f"Weaviate doesn't require auth (default for podman-managed "
        f"vco_weaviate container)."
    )


def _classify_weaviate_failure(exc: Exception):
    """Classify a Weaviate exception into one of:
      - WeaviateUnreachable (connection-class — container down, port
        unbound)
      - WeaviateSchemaError (schema-class — class missing, property
        missing, index missing → cache reset + migration hint)
      - WeaviateAuthError (auth-class — invalid API key, 401/403)
      - None (everything else — payload errors, internal bugs — passed
        through with original message)

    PR-41 (2026-05-16) refined the previous binary
    `WeaviateQueryError → WeaviateUnreachable` mapping (which produced
    misleading hints about "container down" for schema bugs) into a
    three-way detection ordered most-specific-first: schema patterns
    checked BEFORE the connection-class branch so a WeaviateQueryError
    with schema-shaped message doesn't get mis-classified.

    Callers should catch each type explicitly and react:
      - WeaviateUnreachable → _reset_weaviate_client_cache() + retry
        once; if still failing, emit "container down" hint.
      - WeaviateSchemaError → _reset_weaviate_client_cache() + retry
        once (in case the schema was just migrated); if still failing,
        emit "run migration script" hint. This is the actual
        user-impacting fix for Issue A (drop+recreate shared KG no
        longer requires manual `pkill -f weaviate_mcp`).
      - WeaviateAuthError → DO NOT reset cache (auth errors persist);
        emit "check API key" hint.
      - None → pass through; usually a real bug or a Weaviate internal.
    """
    if isinstance(exc, (WeaviateUnreachable, WeaviateSchemaError, WeaviateAuthError)):
        return exc

    msg = str(exc).lower()

    # SchemaError — most specific. Run first so a WeaviateQueryError with
    # schema-shaped message doesn't fall into the unreachable branch.
    if any(p in msg for p in _SCHEMA_ERROR_PATTERNS):
        return WeaviateSchemaError(
            str(exc),
            _build_schema_error_hint(exc, msg),
        )

    # AuthError — distinct from connection failures. Checked before
    # connection patterns because some HTTP layers stringify
    # auth failures with both '401' and 'connection error' in the same
    # message (e.g. proxy responses).
    if any(p in msg for p in _AUTH_ERROR_PATTERNS):
        return WeaviateAuthError(
            str(exc),
            _build_auth_error_hint(exc, msg),
        )

    # Then the existing connection-class detection. The patterns below
    # preserve loud-fail-v2 behaviour for actual outages (2026-05-08
    # silent-zero antipattern fix); only the ordering changed.
    try:
        from weaviate.exceptions import (
            WeaviateBaseError,
            WeaviateConnectionError,
            WeaviateGRPCUnavailableError,
            WeaviateQueryError,
        )
    except ImportError:
        if any(p in msg for p in _CONNECTION_ERROR_PATTERNS):
            return WeaviateUnreachable(str(exc), _build_unreachable_hint(exc))
        return None

    if isinstance(exc, (WeaviateConnectionError, WeaviateGRPCUnavailableError)):
        return WeaviateUnreachable(str(exc), _build_unreachable_hint(exc))

    # WeaviateQueryError: ONLY classify as unreachable if msg explicitly
    # signals connection issues. Schema-shaped queries already caught
    # above; auth-shaped queries already caught above.
    if isinstance(exc, WeaviateQueryError):
        if any(p in msg for p in _CONNECTION_ERROR_PATTERNS):
            return WeaviateUnreachable(str(exc), _build_unreachable_hint(exc))
        # Otherwise: pass through unchanged (auth errors caught above,
        # schema errors caught above; remainder are real query bugs and
        # should propagate with their original message rather than be
        # wrapped as something they aren't).
        return None

    if isinstance(exc, WeaviateBaseError):
        if any(p in msg for p in _CONNECTION_ERROR_PATTERNS):
            return WeaviateUnreachable(str(exc), _build_unreachable_hint(exc))

    # Final fallback: a generic (non-weaviate-class) exception with a
    # clear connection-shaped message still classifies as Unreachable.
    # This preserves the ImportError-branch behaviour above for the
    # common case (test mocks, third-party wrappers).
    if any(p in msg for p in _CONNECTION_ERROR_PATTERNS):
        return WeaviateUnreachable(str(exc), _build_unreachable_hint(exc))

    return None


def _reset_weaviate_client_cache() -> None:
    """Drop the cached client so the next call forces a fresh connect.
    Called from search-tool exception handlers when WeaviateUnreachable
    is detected so transient port-binding glitches recover automatically
    on the next user-driven retry.

    V52-I Fix A (2026-06-09): also clears the per-collection
    `valid_until` schema cache (`_collection_has_valid_until.cache_clear`)
    because schema-altering events (drop+recreate, additive migrate)
    typically coincide with client-cache resets. The reset is a no-op
    when the cache function isn't yet defined (during module init).
    """
    global weaviate_client
    if weaviate_client is not None:
        try:
            weaviate_client.close()
        except Exception:
            pass
        weaviate_client = None
    # Forward-reference safe: `_reset_valid_until_cache` is defined later
    # in this module but resolution happens at CALL time, not def time.
    try:
        _reset_valid_until_cache()
    except NameError:
        # Module still being initialized — cache function not yet bound.
        # Fine: there's nothing cached yet anyway.
        pass


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
_cached_embed_service: "EmbeddingService | None" = None
_embed_service_construction_failed_at: float = 0.0  # epoch seconds
_EMBED_SERVICE_RETRY_WINDOW = 10.0  # don't re-probe failed construction more than once per 10s


# ─── Embedding-generation layer (v0.2.73 M-1) ─────────────────────────────
#
# The 17 embed helpers below were extracted VERBATIM into
# ``weaviate_mcp.embeddings`` (behaviour-preserving move+import). They are
# re-exported into THIS module's namespace so every existing call-site
# (`_get_search_vector(...)`, `get_ollama_embedding(...)`, …), every
# cross-module `from ...server import <fn>` (rl_client.embed_regen), and
# every test that patches `server.<fn>` continues to resolve unchanged.
#
# The EmbeddingService cache globals ``_cached_embed_service`` /
# ``_embed_service_construction_failed_at`` / ``_EMBED_SERVICE_RETRY_WINDOW``
# stay ABOVE (tests poke ``server._cached_embed_service``); the extracted
# accessor reads/writes them via the ``server`` module object.
# Two import styles (mirrors the ``from .chunking``/``from .code_ranking``
# guards above): relative when server is imported as a package
# (``weaviate_mcp.server``), absolute when it is run DIRECTLY as a script
# (``python .../weaviate_mcp/server.py`` — the launcher's actual MCP invocation,
# where ``__package__`` is empty and a bare relative import raises
# "attempted relative import with no known parent package"). The
# ``except ImportError`` fallback is REQUIRED for M-1's extracted modules —
# without it the weaviate-kg MCP fails to start for every user.
try:
    from .embeddings import (  # noqa: E402 — re-export after config constants above
        _get_embedding_service,
        get_ollama_embedding,
        get_legacy_text_embedding,
        get_openai_embedding,
        get_embedding,
        _get_both_embeddings,
        _get_all_kg_embeddings,
        _get_all_code_embeddings,
        _scheme_for_collection,
        _primary_named_vector,
        _get_search_vector,
        count_tokens_async,
        get_code_embedding,
        _inline_code_embed_http,
        get_code_query_embedding,
        _active_code_query_slot,
        get_legacy_code_embedding,
    )
except ImportError:
    from embeddings import (  # type: ignore  # noqa: E402 — server.py run directly
        _get_embedding_service,
        get_ollama_embedding,
        get_legacy_text_embedding,
        get_openai_embedding,
        get_embedding,
        _get_both_embeddings,
        _get_all_kg_embeddings,
        _get_all_code_embeddings,
        _scheme_for_collection,
        _primary_named_vector,
        _get_search_vector,
        count_tokens_async,
        get_code_embedding,
        _inline_code_embed_http,
        get_code_query_embedding,
        _active_code_query_slot,
        get_legacy_code_embedding,
    )


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

    # Parse chunk metadata — prefer schema properties, fall back to content prefix.
    # F-G (v0.2.70): the ``or`` chains used to fall through to None when the
    # stored ``chunk_num``/``total_chunks`` were 0 (legacy single-chunk nodes
    # stored with 0 rather than 1). A None here propagates into the citation /
    # n_emb / sibling-fetch paths and can make a single-chunk node behave as if
    # it has no chunk identity. Maintainer ruling: a single-chunk node must be
    # NON-BLOCKING — absent/0/1 are all a VALID single-chunk node, never "skip".
    # So we read the property explicitly (preserving an explicit 0/None) and
    # normalise a missing/0 chunk to "chunk 1 of 1" at the END.
    parsed = _parse_chunk_header(content)
    _raw_chunk_num = obj.properties.get("chunk_num")
    _raw_total = obj.properties.get("total_chunks")
    chunk_number = (
        _raw_chunk_num if _raw_chunk_num not in (None, 0)
        else (parsed[0] if parsed else None)
    )
    total_chunks = (
        _raw_total if _raw_total not in (None, 0)
        else (parsed[1] if parsed else None)
    )
    # Normalise a single-chunk node (no chunk header + no usable chunk props) to
    # the canonical "chunk 1 of 1" so downstream (sibling refetch, citation
    # n_emb, adjacent-chunk fetch) treats it as a first-class node rather than
    # an identity-less one. Multi-chunk nodes (header parsed OR props >1) keep
    # their real numbers untouched.
    if chunk_number is None and total_chunks is None:
        chunk_number = 1
        total_chunks = 1
    source_id = obj.properties.get("source_node_id") or title

    # Phase 1.5.C: discriminator so callers can route diagram results
    # differently from KG / Development results. The diagrams collection
    # is per-project (DIAGRAMS_COLLECTION) plus any peers in the
    # diagrams-access matrix; every other collection is treated as
    # knowledge. Default to "knowledge" so any future collection added
    # without code changes here still surfaces as a clickable .md file
    # rather than mis-routing.
    diagrams_peers = (
        _diagrams_collections_to_search()
        if DIAGRAMS_COLLECTION
        else []
    )
    if collection_name and (
        collection_name == DIAGRAMS_COLLECTION
        or collection_name in diagrams_peers
    ):
        result_kind = "diagram"
    else:
        result_kind = "knowledge"

    # v0.2.70 Stream D-1: the Development docs collection (<PROJECT>_Development)
    # deliberately has NO `node_type` property (an explicit 2026-05-19 decision —
    # do NOT add the property; that reverses the decision and trips
    # test_v52_ag_schema_versions). So a docs result would default to "unknown"
    # and render as `type:unknown` in injected hook blocks. Formatter-only fix:
    # default node_type to "doc" ONLY for Development-collection results. True KG
    # nodes that legitimately lack a type keep "unknown" (a KG-appropriate
    # default), never silently "doc". Gated on the module-level
    # DEVELOPMENT_COLLECTION (the docs class for this project).
    _default_node_type = (
        "doc"
        if (collection_name and DEVELOPMENT_COLLECTION and collection_name == DEVELOPMENT_COLLECTION)
        else "unknown"
    )
    # v0.2.70 over-collapse fix: fingerprint the FULL body BEFORE truncation.
    # The `content` field below is truncated to `content[:300] + "..."` for
    # display, but the content-identity dedup (`_collapse_to_one_per_node`'s
    # second pass + the hook path's combine_kg_results) must key on the REAL
    # body — otherwise two legitimately-distinct same-title nodes sharing their
    # first 300 chars but differing in the tail would collapse to one, silently
    # dropping a node from Claude's context. The shared dedup helper prefers
    # this precomputed sha over re-hashing the truncated display field.
    # `content_sha` mirrors the seen-store sha1(body)[:12] convention via the
    # one Python home (rl_client.content_dedup.content_sha).
    try:
        from claude_mcp_servers.rl_client.content_dedup import (
            content_sha as _content_sha,
        )
        _full_content_sha = _content_sha((content or "").strip())
    except Exception:  # noqa: BLE001 — never break formatting on a dedup import
        _full_content_sha = ""
    return {
        "title": title,
        "node_type": obj.properties.get("node_type") or _default_node_type,
        "content": content[:300] + "..." if len(content) > 300 else content,
        # Full-body fingerprint (untruncated) for content-identity dedup. See
        # the comment above + content_dedup.content_identity_key's truncation
        # guard. Empty string when content is empty (helper then falls back to
        # identity keying so a content-less node is never collapsed/dropped).
        "content_sha": _full_content_sha,
        "tags": obj.properties.get("tags", []),
        "file_path": obj.properties.get("file_path", ""),
        "created_at": serialize_datetime(obj.properties.get("created_at", "")),
        "updated_at": serialize_datetime(obj.properties.get("updated_at", "")),
        "distance": dist,
        "collection": collection_name,
        # Phase 1.5.C discriminator — "knowledge" | "diagram".
        "result_kind": result_kind,
        # Chunk metadata (None for un-chunked nodes)
        "source_id": source_id,
        "chunk_number": chunk_number,
        "total_chunks": total_chunks,
        # v0.2.47 RL-6b-2: surface KG wikilink titles so the RL enrichment
        # helper can fetch linked-node embeddings. Code collections + diagrams
        # don't have a `links` property → defaults to empty list. The KG
        # schema's `links: text[]` is populated by sync_knowledge_graph from
        # the markdown frontmatter / wikilink parse.
        "links": obj.properties.get("links", []),
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

    KG-2 (v0.2.73): neighbours are only RENDERED at the `three_chunks` and
    `full` tiers — the `summary` / `single_chunk` tiers show just the matched
    chunk. Fetching neighbours for a result that will land below the
    `three_chunks` tier is a wasted Weaviate round-trip whose rows the tier
    budget then discards. So we only fetch neighbours for results whose score
    is at least the `single_chunk` threshold (one tier below three_chunks —
    a conservative margin that still fetches for a result an RL rerank might
    bump UP into a neighbour-rendering tier, but skips the clearly-summary
    band). Results with no score (score unavailable) keep the old behaviour
    (fetch) so we never regress a legitimately-needed neighbour.

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

    # KG-2: the gate below which neighbour-fetch is pure waste. Use the KG
    # gate's single_chunk threshold as the conservative floor (three_chunks
    # is the first tier that renders neighbours; single_chunk gives a
    # one-tier rerank margin).
    _neighbour_floor = _TIER_THRESHOLDS["single_chunk"]

    def _score_of(r: dict) -> float | None:
        # score_cosine (1.0 - distance) is what results carry at enrich time
        # (before combined_score / _rerank are computed downstream); the
        # others cover the reranked / code-path shapes if this helper is
        # ever reused post-rerank.
        for k in ("combined_score", "_rerank", "score_cosine", "score", "_s"):
            v = r.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    # Fetch neighbours for chunked hits that could actually render them.
    for r in list(combined):
        cn = r.get("chunk_number")
        tc = r.get("total_chunks")
        if cn is None or tc is None:
            continue
        sc = _score_of(r)
        if sc is not None and sc < _neighbour_floor:
            # Will render at summary/single_chunk → neighbours discarded; skip.
            continue
        neighbours = _fetch_adjacent_chunks(
            coll, r["title"], cn, tc, collection_name
        )
        for nb in neighbours:
            nb_key = (nb.get("title", ""), nb.get("chunk_number"))
            if nb_key not in seen:
                seen.add(nb_key)
                combined.append(nb)

    return combined


# ─── RL reranking + telemetry-enrichment layer (v0.2.73 M-1) ──────────────
#
# The 37 RL helpers below were extracted VERBATIM into
# ``weaviate_mcp.rl_enrichment`` (behaviour-preserving move). They are
# re-exported into THIS module's namespace so every existing call-site
# (`_rl_cache_and_rerank(...)`, `_rl_enrich_nodes_with_linked_embs(...)`,
# `_emit_code_retrieval_telemetry(...)`, …), every cross-module
# `from ...server import <fn>` (rl_client.*), and every test that patches
# `server.<rl_fn>` continues to resolve unchanged. The RL mutable caches
# (`_rl_client_instances`, `_rl_telemetry_writers`, the `_rl_*_instance`
# tombstones, `_rl_node_content_cache`, `_rl_call_seq`, `_rl_monitor_tasks`)
# and the `_CODE_STRUCTURE_TELEMETRY_MAX_NODES` / `DUAL_RL_LOG_ENABLED_ENV`
# constants stay defined ON THIS module (below / above) — rl_client and the
# tests reach them via `srv.<name>`; the extracted functions read them via
# `server.<name>`.
# Relative when imported as a package; absolute when run directly as a script
# (see the ``from .embeddings`` guard above — same REQUIRED fallback so the MCP
# starts under the launcher's ``python .../weaviate_mcp/server.py`` invocation).
_RL_ENRICHMENT_EXPORTS = (
    "_rl_load_messages", "_rl_find_kg_positions", "_rl_extract_answer_window",
    "_resolve_claude_session_dir", "_rl_find_all_transcripts_in_dir",
    "_rl_find_all_transcripts", "_rl_is_literal_cited",
    "_rl_compute_and_write_citations", "_rl_force_flush_sentinel_path",
    "_rl_check_force_flush", "_rl_clear_force_flush", "_rl_human_turn_after",
    "_rl_delete_own_pending_file", "_rl_answer_monitor", "_get_rl_client",
    "_embedding_dim_for", "_extract_obj_vector", "_cosine",
    "_get_rl_telemetry_writer", "_resolve_code_embedding_triple",
    "_emit_code_structure_telemetry", "_emit_code_retrieval_telemetry",
    "_stage_code_citation_pending", "_get_rl_telemetry_writer_for",
    "_other_model_for_source", "_embed_text_in_other_model",
    "_reset_rl_telemetry_writers", "_rl_pack_linked_embs_for_node",
    "_rl_regenerate_node_vector", "_rl_refetch_node_vector",
    "_rl_find_representative_obj", "_rl_attach_other_slot_for_node",
    "_rl_enrich_nodes_with_linked_embs", "_resolve_dual_rl_log_enabled",
    "_resolve_dual_rl_log_inputs", "_slot_short_source", "_rl_cache_and_rerank",
    # X-4 (v0.2.75): enrichment fan-out gate (TTL-cached skip predicate).
    "_rl_enrichment_gate_open", "_rl_enrichment_consumer_exists",
    "_rl_enrich_gate_reset_for_test",
)
try:
    from . import rl_enrichment as _rl_enrichment  # noqa: E402 — package-relative
except ImportError:
    import rl_enrichment as _rl_enrichment  # type: ignore  # noqa: E402 — run directly
for _name in _RL_ENRICHMENT_EXPORTS:  # re-export into server's namespace
    globals()[_name] = getattr(_rl_enrichment, _name)
del _name


# ----------------------------------------------------------------------
# RLClient + telemetry-writer lazy singletons (Stream 1 / v0.2.20)
#
# These replace the inline ``aiohttp`` POSTs that used to live in
# ``_rl_cache_and_rerank`` and ``_rl_answer_monitor``. Constructing the
# client is cheap (no HTTP until first call); we still lazily build a
# per-process instance so the env vars (set by the launcher's
# allocate_rl_port flow) are read at first use, not at import time.
# ----------------------------------------------------------------------

# v0.2.75 P3g / M-1 remainder: these caches + tombstones DEFINE in ``rl_state``
# (imported near the config block above as ``_rl_state``); re-exported here so
# ``srv._rl_client_instances`` etc. remain the SAME live objects (by-reference
# for the dicts — every ``rl_client``/test mutation is observed on both).
# Their rationale (RT-1 client re-key on active_embedding, F2 writer re-key on
# (project, embedding_source), the two read-only back-compat tombstones) lives
# beside the definitions in ``rl_state``.
_rl_client_instances = _rl_state._rl_client_instances  # by-reference (dict)
_rl_telemetry_writers = _rl_state._rl_telemetry_writers  # by-reference (dict)
_rl_telemetry_writer_instance = _rl_state._rl_telemetry_writer_instance  # tombstone (None)
_rl_client_instance = _rl_state._rl_client_instance  # tombstone (None)
_CODE_STRUCTURE_TELEMETRY_MAX_NODES = _rl_state._CODE_STRUCTURE_TELEMETRY_MAX_NODES
DUAL_RL_LOG_ENABLED_ENV = _rl_state.DUAL_RL_LOG_ENABLED_ENV


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


# V52-I Fix A (2026-06-09): per-collection cache of whether `valid_until`
# property exists. The MCP fans hybrid_search / semantic_graph_search across
# {project KG, shared KG, peer KGs, _Development, _Diagrams} but only the
# *_Development collections AND the per-project KG (via sync_knowledge_graph
# additive migrate path) are guaranteed to have `valid_until`. Shared KG and
# both Diagrams classes are MISSING the property on existing installs (see
# gap matrix in V52-I plan). Attaching the stale filter to those collections
# triggers a Weaviate schema error → MCP records a false-positive
# `partial_fan_out_schema_missing` telemetry event (30 events in corpus).
#
# This cache lets `_stale_filter_for(collection_name)` ask "does this
# collection actually have the property?" once and reuse the answer for
# the lifetime of the MCP subprocess. lru_cache(maxsize=64) is plenty —
# even a power user with 10 peer projects sits at <20 distinct collections.
#
# Fix B (companion in vco_lib/project_init.py + the migrate script) closes
# the gap on FRESH installs by adding the props at create-time. Fix A is
# the defensive runtime layer that protects existing installs without
# requiring a schema migration.
@functools.lru_cache(maxsize=64)
def _collection_has_valid_until(collection_name: str) -> bool:
    """Return True iff the collection's Weaviate schema has a `valid_until`
    property. Cached for the MCP subprocess lifetime.

    Conservative on probe failure: returns False so callers SKIP the stale
    filter (preferring "some stale nodes leak through" over "every query
    against this collection schema-fails"). Probe failures are logged at
    DEBUG so they don't spam under normal operation.

    Why probe instead of attempting+rescuing the filter: Weaviate raises
    the schema error from inside `near_vector` / `bm25`, which would
    require wrapping every fan-out call site in retry logic. The probe is
    a single cheap `collection.config.get()` per collection, called once.
    """
    if not collection_name:
        return False
    try:
        client = get_weaviate_client()
        coll = client.collections.get(collection_name)
        config = coll.config.get()
        return any(p.name == "valid_until" for p in (config.properties or []))
    except Exception as exc:  # noqa: BLE001 — soft-fail intentional
        logger.debug(
            "_collection_has_valid_until: probe failed for '%s' (%s); "
            "treating as missing-property",
            collection_name, exc,
        )
        return False


def _reset_valid_until_cache() -> None:
    """Clear the `valid_until` schema cache.

    Called from `_reset_weaviate_client_cache` after schema-altering
    operations (drop+recreate, migrate scripts) so the next search re-probes
    against the current schema state. Safe to call unconditionally.
    """
    try:
        _collection_has_valid_until.cache_clear()
    except Exception:  # noqa: BLE001 — defensive; cache_clear shouldn't fail
        pass


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

    NOTE (V52-I, 2026-06-09): prefer `_stale_filter_for(collection_name)`
    at all NEW call sites. This bare `_stale_filter` assumes every
    collection in the fan-out has `valid_until` — that's true for
    *_Development and per-project KG (via sync-script migrate) but NOT
    for shared KG or *_Diagrams on existing installs. Sites that fan out
    across heterogeneous collections must use `_stale_filter_for` to
    avoid `partial_fan_out_schema_missing` false-positives.

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


def _stale_filter_for(collection_name: str, include_stale: bool = False):
    """Schema-aware variant of `_stale_filter`.

    Returns None (caller treats as "no filter") when either:
      - `include_stale=True` (explicit caller override), OR
      - the collection's schema doesn't have `valid_until` (per the cache
        in `_collection_has_valid_until`).

    Otherwise returns the same `Filter` shape as `_stale_filter`. Use
    this at every fan-out call site that searches across heterogeneous
    collections (project KG + shared KG + Diagrams + Development) — the
    bare `_stale_filter` is unsafe there because shared KG and Diagrams
    collections lack `valid_until` on existing installs.

    V52-I Fix A (2026-06-09).
    """
    if include_stale:
        return None
    if not _collection_has_valid_until(collection_name):
        return None
    return _stale_filter(include_stale=False)


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
    # Loud-fail wrapper (2026-05-08 silent-zero antipattern fix v2).
    # Catches BOTH connection-time and query-time Weaviate failures.
    try:
        # v0.2.74 T5-1 backstop (defense-in-depth): refuse-loud on a stale
        # subprocess whose module-load workspace no longer matches the live
        # CLAUDE_PROJECT_DIR, rather than fanning out over the wrong project's
        # collections (the double-MCP-subprocess drift). Same guard as
        # hybrid_search — the reaper prevents the stale process, this is the
        # per-call belt-and-suspenders.
        _assert_workspace_unchanged("semantic_graph_search")
        client = get_weaviate_client()
        return await _semantic_graph_search_body(client, query, limit, depth, detail, include_stale)
    except WeaviateWorkspaceDriftError as exc:
        # Surface the refuse-loud message verbatim. Do NOT reset the client
        # cache (this is not a Weaviate outage) — the fix is a subprocess
        # restart, not a reconnect.
        return _workspace_drift_response(exc, query=query)
    except WeaviateUnreachable as exc:
        _reset_weaviate_client_cache()
        return _weaviate_unreachable_response(exc, query=query)
    except WeaviateSchemaError as exc:
        # PR-41 Issue A: drop+recreate of a collection invalidates the
        # cached client's schema view; reset the cache so the next call
        # re-fetches schema metadata.
        _reset_weaviate_client_cache()
        return _weaviate_schema_error_response(exc, query=query)
    except WeaviateAuthError as exc:
        # PR-41 Issue F: auth errors persist across reconnects; do NOT
        # reset the cache (would just churn the connection).
        return _weaviate_auth_error_response(exc, query=query)
    except Exception as exc:
        classified = _classify_weaviate_failure(exc)
        if isinstance(classified, WeaviateUnreachable):
            _reset_weaviate_client_cache()
            return _weaviate_unreachable_response(classified, query=query)
        if isinstance(classified, WeaviateSchemaError):
            _reset_weaviate_client_cache()
            return _weaviate_schema_error_response(classified, query=query)
        if isinstance(classified, WeaviateAuthError):
            return _weaviate_auth_error_response(classified, query=query)
        raise


async def _semantic_graph_search_body(
    client,
    query: str,
    limit: int,
    depth: int,
    detail: str,
    include_stale: bool,
) -> str:
    """Implementation body for semantic_graph_search. Extracted so the
    public tool can wrap the entire query path with the v2 loud-fail
    handler without indenting 300 lines."""
    coll = client.collections.get(KG_COLLECTION)

    fetch_limit = limit * _RL_OVERFETCH

    # V52-I Fix A (2026-06-09): stale-filter is now computed PER-COLLECTION
    # via `_stale_filter_for(coll_name, include_stale=...)` — see the
    # for-loop body below. The single-shot `stale = _stale_filter(...)`
    # pattern broke when shared KG / peer KGs / Diagrams collections lacked
    # `valid_until` (Weaviate emits a schema error, which the fan-out's
    # except classifier records as `partial_fan_out_schema_missing` —
    # 30 false-positives in our corpus). Per-collection gating eliminates
    # that. `include_stale` flows through unchanged.

    # Determine all collections to search. Mirrors hybrid_search: project KG +
    # shared KG (when configured) + peer-project KGs from the launcher's
    # access matrix (VCT_KG_ACCESS_LIST, P1-D 2026-05-08). The shared KG
    # read is unconditional — there is NO per-project read opt-out
    # (asymmetric semantic, see module docstring). We do NOT include
    # DEVELOPMENT_COLLECTION here — graph traversal relies on WikiLinks
    # which are a knowledge-graph convention, not present in dev docs.
    collections_to_search: list[str] = _kg_collections_to_search(include_dev=False)

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
    #
    # v0.2.24 (RL-defect-2026-05-22): error handling mirrors
    # hybrid_search_body — classify per-collection failures so:
    #   - schema-missing → skip + record (don't kill fan-out)
    #   - unreachable / auth → bubble (instance-level)
    # When EVERY collection schema-failed, log a degraded-mode telemetry
    # event before re-raising.
    all_formatted: list[dict] = []
    raw_primary: list[tuple[object, str]] = []
    failed_collections_schema: list[str] = []
    successful_collections: list[str] = []
    # v0.2.47 RL-6b-2: hoist query_vector / query_target to FUNCTION scope
    # so they're defined even if `collections_to_search` is empty (in which
    # case the for-loop body never runs and the post-collapse references
    # at the RL enrich + _rl_cache_and_rerank sites would otherwise hit
    # NameError). Pre-v0.2.47 this was latent — every reachable code path
    # set `query_vector` inside the loop, but a fresh install with no
    # configured KG collections would not. Initializing them here makes
    # the no-op-collections-search path explicit and crash-free.
    query_vector: list[float] | None = None
    query_target: str = ""
    for coll_name in collections_to_search:
        handle = _coll_for(coll_name)
        if handle is None:
            continue
        # v0.2.31 telemetry audit fix (Item 2.4 — was 7.4% missing emb):
        # capture the query vector (target_name) so we can attach node
        # embeddings + cos_qn to the candidate dicts BEFORE they flow
        # into _rl_cache_and_rerank → log_retrieval. ``query_vector``
        # may be None on near_text path (Weaviate-vectoriser mode); in
        # that case we skip emb enrichment.
        # (Per-iteration re-init — the function-scope defaults above
        # only apply when the loop never runs.)
        query_vector = None
        query_target = ""
        # V52-I Fix A: per-collection schema-aware stale filter — skip when
        # the collection's schema lacks `valid_until` (shared KG / Diagrams
        # on existing installs). See `_collection_has_valid_until` cache.
        stale = _stale_filter_for(coll_name, include_stale=include_stale)
        try:
            if EMBEDDING_SOURCE == "weaviate":
                nt_kwargs = dict(query=query, limit=fetch_limit, return_metadata=["distance"])
                if stale is not None:
                    nt_kwargs["filters"] = stale
                primary = handle.query.near_text(**nt_kwargs)
            else:
                vector, target_name = await _get_search_vector(query)
                query_vector = vector
                query_target = target_name or ""
                nv_kwargs = dict(
                    near_vector=vector,
                    limit=fetch_limit,
                    return_metadata=["distance"],
                    include_vector=True,
                )
                if target_name:
                    nv_kwargs["target_vector"] = target_name
                if stale is not None:
                    nv_kwargs["filters"] = stale
                primary = handle.query.near_vector(**nv_kwargs)
        except Exception as exc:
            classified = _classify_weaviate_failure(exc)
            if isinstance(classified, WeaviateUnreachable):
                _reset_weaviate_client_cache()
                raise classified from exc
            if isinstance(classified, WeaviateAuthError):
                raise classified from exc
            if isinstance(classified, WeaviateSchemaError):
                logger.warning(
                    "semantic_graph_search: skipping collection '%s' (schema error: %s)",
                    coll_name, classified,
                )
                failed_collections_schema.append(coll_name)
                continue
            logger.warning(f"semantic_graph_search: error searching {coll_name}: {exc}")
            continue

        # Format all over-fetched results from this collection
        coll_formatted = [
            _format_obj(obj, coll_name, obj.metadata.distance)
            for obj in primary.objects
        ]
        # V52-J Edit 3 / V52-Q (2026-06-09): attach raw cosine score
        # (= 1.0 - Weaviate distance) on each formatted dict so the v3
        # telemetry envelope carries BOTH the fused score (later set as
        # ``score`` by the RL rerank / fallback) AND the raw per-Weaviate
        # cosine. The offline trainer uses both: fused score for ranking
        # supervision, raw cosine for embedding-quality drift detection.
        # Soft-fail per-result; non-float distance falls back to 0.0.
        for r in coll_formatted:
            d = r.get("distance")
            if isinstance(d, (int, float)):
                r["score_cosine"] = 1.0 - d
        # v0.2.31 telemetry audit fix: enrich formatted dicts with node
        # embedding + cos_qn so log_retrieval gets non-empty fields.
        # Soft-fail per-result: a malformed obj.vector must never
        # crash the search path.
        if query_vector is not None:
            for r, obj in zip(coll_formatted, primary.objects):
                try:
                    node_emb = _extract_obj_vector(obj, query_target)
                    if node_emb:
                        r["emb"] = node_emb
                        r["cos_qn"] = _cosine(query_vector, node_emb)
                except Exception as enrich_exc:  # noqa: BLE001
                    logger.debug(
                        "semantic_graph_search: emb enrichment skipped for one node (%s)",
                        enrich_exc,
                    )
        coll_formatted = _enrich_with_adjacent_chunks(handle, coll_formatted, coll_name)
        all_formatted.extend(coll_formatted)
        for obj in primary.objects:
            raw_primary.append((obj, coll_name))
        successful_collections.append(coll_name)

    # If EVERY collection in the fan-out schema-failed → instance-level
    # problem; bubble after logging a degraded-mode telemetry event.
    if not successful_collections and failed_collections_schema:
        _reset_weaviate_client_cache()
        try:
            failure_task_id = str(uuid.uuid4())
            await _rl_cache_and_rerank(
                failure_task_id, query, [], limit,
                failure_mode="all_collections_schema_missing",
                failed_collections=failed_collections_schema,
            )
        except Exception as exc:
            logger.debug(
                "semantic_graph_search: failure telemetry log_retrieval failed (%s); continuing",
                exc,
            )
        # v0.2.27: annotate each failed collection with its resolution
        # source so users can tell at a glance whether the MCP picked up
        # the right env vars or fell back to a bundled default. See
        # _describe_collection_source + the resolution log line emitted at
        # module load ("weaviate-kg: resolved collections").
        annotated_failed = _format_failed_collections_hint(failed_collections_schema)
        hint_suffix = (
            " — if names look unexpected, the resolved KG_COLLECTION / "
            "SHARED_KG_COLLECTION env vars likely don't match your project. "
            "Canonical channel: .claude/settings.json `env`. Check the "
            "'weaviate-kg: resolved collections' log line at server startup "
            "for what this MCP subprocess actually sees."
        )
        raise WeaviateSchemaError(
            "semantic_graph_search: every configured collection schema-failed "
            f"({len(failed_collections_schema)} attempted: "
            f"{annotated_failed})"
            + hint_suffix
        )

    # Preserve a normalised score (1 - distance) so per-result tiering works.
    for r in all_formatted:
        if "score" not in r:
            d = r.get("distance")
            r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

    # Sort merged candidates by score so RL sees a clean list (top-k semantics).
    all_formatted.sort(key=lambda x: x.get("score", 0.0), reverse=True)

    # Collapse multi-chunk matches of the same node BEFORE the RL rerank, for
    # the same reason hybrid_search does (retrieval_rl.py keys on title; two
    # chunks of the same node would silently collide in `signed[title]`).
    all_formatted = _collapse_to_one_per_node(all_formatted, score_field="score")

    # v0.2.47 RL-6b-2: enrich each node with `n_emb` / `linked_embs` /
    # `linked_type_names` / `cos_qn` / `cos_ql` / `cos_nl` BEFORE the RL
    # path sees the candidates. Same shape as the sibling hybrid_search
    # wiring — one batched Weaviate fetch per collection (grouped by
    # `collection` field on each node dict). Soft-fail throughout.
    # NB: `query_vector` and `query_target` are leaked from the
    # per-collection for-loop above (Python's last-iteration binding,
    # same convention as the existing `query_emb=query_vector` arg
    # below). When the fan-out had zero successful collections both
    # remain at their initial None / "" sentinels; the helper degrades
    # to a no-op-with-empty-fields write.
    # v0.2.71 Sweep-C: resolve the dual-RL-log other-slot inputs ONCE (gated on
    # dual-log AND dual-write env). None → bare single-log path. The other-slot
    # query vector comes from the canonical embed fan-out; the per-node other
    # vectors are attached by the enrich call below from the SAME fetched objects.
    _dual_inputs = await _resolve_dual_rl_log_inputs(query, query_target)
    try:
        _rl_enrich_nodes_with_linked_embs(
            all_formatted,
            query_emb=query_vector,
            active_slot=query_target,
            model_name=EMBEDDING_MODEL,
            other_slot=(_dual_inputs or {}).get("other_slot", ""),
            other_query_emb=(_dual_inputs or {}).get("other_query_emb"),
            other_model_name=(_dual_inputs or {}).get("other_model", ""),
            backfill_other=_dual_inputs is not None,
        )
    except Exception as exc:
        logger.debug(
            "semantic_graph_search: RL enrich failed (%s); proceeding without linked_embs",
            exc,
        )

    # RL: rerank + cache using all over-fetched nodes; return top-k primary results.
    # v0.2.24: propagate partial-fan-out schema failures so telemetry
    # records which collections were unavailable.
    task_id = str(uuid.uuid4())
    _partial_failure_mode = (
        "partial_fan_out_schema_missing" if failed_collections_schema else None
    )
    primary_results = await _rl_cache_and_rerank(
        task_id, query, all_formatted, limit,
        failure_mode=_partial_failure_mode,
        failed_collections=failed_collections_schema or None,
        query_emb=query_vector,
        dual_log_inputs=_dual_inputs,
    )
    for r in primary_results:
        if "score" not in r:
            d = r.get("distance")
            r["score"] = (1.0 - d) if isinstance(d, (int, float)) else 0.0

    # Apply tiering to primary results (mirrors hybrid_search behaviour,
    # including shared chunk budget for auto-mode). Use per-result collection
    # so chunk fetch and sidecar lookup go to the right place when results
    # come from the shared KG.
    legacy_aliases = {"descriptions": "summary"}
    primary_formatted: list[dict] = []
    if detail == "auto":
        ordered = sorted(primary_results, key=lambda r: r.get("score", 0.0) or 0.0, reverse=True)
        budget = _HYBRID_CHUNK_BUDGET
        for r in ordered:
            score = r.get("score", 0.0) or 0.0
            total_chunks = r.get("total_chunks") or 1
            tier, cost = _allocate_tier_within_budget(score, total_chunks, budget)
            if tier == "discard":
                continue
            budget -= cost
            result_coll_name = r.get("collection") or KG_COLLECTION
            entry = _format_result_by_tier(
                r, tier, sidecar_db=None, coll=_coll_for(result_coll_name)
            )
            if entry is not None:
                primary_formatted.append(entry)
    else:
        tier = legacy_aliases.get(detail, detail)
        for r in primary_results:
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
    include_stale: bool = False,
) -> dict:
    """Run hybrid (semantic + keyword) search on one collection, return combined dict keyed by (title, chunk).

    V52-I Fix A (2026-06-09): the stale-filter (valid_until > now | is_none)
    is applied here, gated by `_stale_filter_for(coll_name)` — the gate
    returns None when this collection's schema lacks `valid_until`
    (shared KG + *_Diagrams on existing installs), avoiding the
    schema error that produced 30 false-positive partial-fan-out events.
    """
    client = get_weaviate_client()
    coll = client.collections.get(coll_name)

    effective_filter = weaviate_filter
    # V52-I Fix A: schema-aware stale filter — only attach when the
    # collection actually has `valid_until`. See `_stale_filter_for`
    # docstring + the module-level _collection_has_valid_until cache.
    stale = _stale_filter_for(coll_name, include_stale=include_stale)
    if stale is not None:
        effective_filter = (effective_filter & stale) if effective_filter else stale
    if date_filter is not None:
        effective_filter = (effective_filter & date_filter) if effective_filter else date_filter

    # Semantic search
    # v0.2.31 telemetry audit fix (Item 2.4 — was 7.4% missing emb on
    # log_retrieval): on the near_vector path, ask Weaviate to return
    # the per-object vector AND capture the query vector so we can
    # attach `emb` + `cos_qn` to formatted candidates before they
    # flow into _rl_cache_and_rerank → log_retrieval. ``query_vector``
    # is None on the Weaviate-vectoriser (near_text) path, in which
    # case we skip emb enrichment — the path doesn't return raw
    # vectors anyway.
    query_vector: list[float] | None = None
    query_target: str = ""
    if EMBEDDING_SOURCE == "weaviate":
        if effective_filter:
            semantic_results = coll.query.near_text(query=query, limit=fetch_limit, filters=effective_filter, return_metadata=["distance"])
        else:
            semantic_results = coll.query.near_text(query=query, limit=fetch_limit, return_metadata=["distance"])
    else:
        vector, target_name = await _get_search_vector(query)
        query_vector = vector
        query_target = target_name or ""
        nv_kwargs = dict(
            near_vector=vector,
            limit=fetch_limit,
            return_metadata=["distance"],
            include_vector=True,
        )
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
    # V52-J Edit 3 / V52-Q (2026-06-09): attach raw cosine score
    # (= 1.0 - Weaviate distance) on each formatted dict. See the
    # mirror site in semantic_graph_search for rationale: the
    # offline trainer wants BOTH the fused ``score`` AND the raw
    # per-Weaviate cosine carried through telemetry.
    for r in semantic_formatted:
        d = r.get("distance")
        if isinstance(d, (int, float)):
            r["score_cosine"] = 1.0 - d
    # v0.2.31 telemetry audit fix: enrich semantic_formatted with node
    # embeddings + cos_qn from the matched obj.vector. Skipped when on
    # the near_text path (no raw vectors available). Per-result
    # soft-fail so a malformed vector never breaks the search path.
    if query_vector is not None:
        for r, obj in zip(semantic_formatted, semantic_results.objects):
            try:
                node_emb = _extract_obj_vector(obj, query_target)
                if node_emb:
                    r["emb"] = node_emb
                    r["cos_qn"] = _cosine(query_vector, node_emb)
            except Exception as enrich_exc:  # noqa: BLE001
                logger.debug(
                    "hybrid_search: emb enrichment skipped for one node (%s)",
                    enrich_exc,
                )
    semantic_formatted = _enrich_with_adjacent_chunks(coll, semantic_formatted, coll_name)

    combined = {}
    for r in semantic_formatted:
        key = (r["title"], r.get("chunk_number"))
        entry = {
            "title": r["title"],
            # v0.2.70 Stream D-1: `r` comes from _format_obj, which already
            # resolves node_type ("doc" for Development-collection results, the
            # real type otherwise). This "unknown" fallback only fires if the
            # key were absent — it never is — so it's a dead default kept for
            # safety. Do NOT re-default to "doc" here; the docs gate lives in
            # _format_obj (single source).
            "node_type": r.get("node_type", "unknown"),
            "content": r.get("content", ""),
            # v0.2.70 over-collapse fix: carry the full-body fingerprint from
            # _format_obj so the downstream _collapse_to_one_per_node content
            # pass keys on the REAL body, not the truncated `content` display
            # field. Without this the rebuilt entry would lose content_sha and
            # the collapse would fall back to hashing the truncated body.
            "content_sha": r.get("content_sha", ""),
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
        # v0.2.31 telemetry audit fix: propagate emb + cos_qn from the
        # near_vector path into the merged candidate dict so they
        # survive into _rl_cache_and_rerank → log_retrieval.
        if r.get("emb") is not None:
            entry["emb"] = r["emb"]
        if r.get("cos_qn") is not None:
            entry["cos_qn"] = r["cos_qn"]
        # V52-J Edit 3 / V52-Q: propagate raw cosine score the same way.
        if r.get("score_cosine") is not None:
            entry["score_cosine"] = r["score_cosine"]
        combined[key] = entry

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
                # v0.2.70 Stream D-1 (N-1): formatted_kw is _format_obj output,
                # which always sets node_type ("doc" for Development-collection
                # results, the real type otherwise). This "unknown" fallback is a
                # dead default (the key is never absent) — kept for safety. Do
                # NOT re-default to "doc" here; the docs gate lives in
                # _format_obj (single home). Mirrors the note at the semantic
                # site above.
                "node_type": formatted_kw.get("node_type", "unknown"),
                "content": formatted_kw.get("content", ""),
                # v0.2.70 over-collapse fix: see the semantic-site mirror above —
                # propagate the full-body fingerprint so the collapse content
                # pass never keys on the truncated display body.
                "content_sha": formatted_kw.get("content_sha", ""),
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

    # ── Fusion: relativeScoreFusion (2026-06-15) ──────────────────────────
    #
    # PRIOR BUG: combined_score = (sem_score + keyword_score) / 2 averaged a
    # bounded cosine half (1 - distance ∈ [0,1]) with the RAW BM25 keyword
    # score, which is UNBOUNDED (BM25 = Σ IDF(term)·tf-saturation; empirically
    # 2–8+ on this corpus). The fused value therefore routinely exceeded 1.0,
    # silently defeating the auto-tier thresholds (_TIER_THRESHOLDS, documented
    # as "0..1") — a strong keyword match always landed ≥0.75 → `full` tier
    # regardless of semantic relevance.
    #
    # FIX: mirror Weaviate's relativeScoreFusion (the engine default since
    # v1.24, which this hand-rolled dual-query path bypasses). Min-max normalize
    # EACH modality across this query's candidate set → [0,1], then average.
    # This keeps BM25's *relative magnitude* within the result set (unlike
    # rankedFusion, which keeps only rank) while making the fused score bounded
    # and threshold-comparable. The absolute cross-query meaning of raw BM25 is
    # not lost here because it never existed — raw BM25 is only comparable
    # within one query's results (its scale depends on query length + corpus
    # IDF). `score_cosine` (raw cosine, untouched above) remains the absolute
    # signal the RL trainer consumes.
    #
    # alpha = cosine (vector) weight, matching Weaviate's `alpha` convention
    # (alpha=1.0 → pure vector, 0.0 → pure keyword). Default 0.6 makes the
    # SEMANTIC signal slightly dominant over the LEXICAL one: cosine captures
    # what the query MEANS, BM25 captures which words literally appear. For a
    # semantic KG/code retrieval system the meaning is the primary signal and
    # keyword is the booster/tiebreaker, so cosine gets 60% and BM25 40%.
    # Overridable via KG_HYBRID_ALPHA without a code change (clamped to [0,1]).
    # Single-candidate or all-equal sets: min==max → that modality contributes
    # a flat 0.0 after normalization; if BOTH modalities are flat (one
    # candidate), fall back to its cosine so a lone exact hit still scores.
    _ALPHA = _HYBRID_ALPHA  # module-level, env-overridable (see definition)
    sem_scores = {k: (1.0 - v["semantic_distance"]) for k, v in combined.items()}
    kw_scores = {k: v.get("keyword_score", 0.0) for k, v in combined.items()}

    def _minmax_norm(values: dict) -> dict:
        if not values:
            return {}
        lo, hi = min(values.values()), max(values.values())
        span = hi - lo
        if span <= 0:
            # All equal (incl. single candidate) → no relative information in
            # this modality; contribute 0 so the other modality decides.
            return {k: 0.0 for k in values}
        return {k: (x - lo) / span for k, x in values.items()}

    sem_norm = _minmax_norm(sem_scores)
    kw_norm = _minmax_norm(kw_scores)

    single_candidate = len(combined) == 1
    for key, item in combined.items():
        fused = _ALPHA * sem_norm.get(key, 0.0) + (1.0 - _ALPHA) * kw_norm.get(key, 0.0)
        # Degenerate single-candidate case: both modalities normalize flat to
        # 0.0, which would discard a lone exact match. Fall back to its raw
        # cosine (already bounded [0,1]) so it tiers sensibly on its own merit.
        if single_candidate:
            fused = max(0.0, sem_scores.get(key, 0.0))
        item["combined_score"] = fused

    return combined


def _collapse_to_one_per_node(
    results: list[dict],
    score_field: str = "combined_score",
    *,
    key_fields: tuple[str, ...] = ("file_path", "title"),
    chunk_field: str = "chunk_number",
    dedup_kind: str = "kg",
) -> list[dict]:
    """
    Collapse multi-chunk matches of the same node into a single entry, keeping
    the highest-scoring chunk's full record and recording how many chunks of
    that node matched the query.

    Generalized (v0.2.72 P4) so BOTH the KG path (default args → identical
    v0.2.71 behaviour) and the CODE path share one collapse:
      * KG (default): key_fields=("file_path","title"), chunk_field=
        "chunk_number", dedup_kind="kg" — keys on (file_path, title) exactly
        as before; the content-identity second pass runs with kind="kg".
      * CODE: pass key_fields=("file_path","full_name"), chunk_field=
        "chunk_num", dedup_kind="code" — code chunks share a full_name; the
        content-identity pass runs with kind="code" (code_identity_key +
        code body fields).

    KG-SAFETY: every added parameter is keyword-only with a default equal to
    the v0.2.71 hard-coded value, so the four KG hot-path callers
    (server.py hybrid_search / semantic_graph_search at 5975/6041/6714/6809)
    pass no new kwargs and get byte-identical output. The KG parity test
    (tests/test_collapse_tier_generalized.py) asserts this.

    Why: the per-collection dedup keys on (title, chunk_number) — correct for
    merging semantic+keyword hits on the SAME chunk. But two different chunks
    of the same node both survive into the candidate list. To the agent this
    looks like duplicates; to the RL server (which keys EVERYTHING on title),
    the second chunk's signal silently overwrites the first's in
    retrieval_rl.py — corrupting both online training and offline logs.

    Collapsing here, upstream of _rl_cache_and_rerank, gives:
      - Clean user-visible top-K (no apparent duplicates)
      - Clean RL candidate pool (one entry per node title)
      - Clean offline training log (one record per node)
      - New `chunks_matched` field — useful learning signal: a node matched
        on N chunks is plausibly a stronger semantic match than one matched
        on a single chunk, even at the same combined_score.

    Key: (file_path or '', title) — file_path disambiguates rare cross-collection
    title collisions (e.g. same node title in shared and project KG).

    Returns: collapsed list, sorted by score_field desc. Each kept entry
    carries the WINNING chunk's score, content, and metadata, plus:
      - chunks_matched: int — number of distinct chunks of this node in input
      - best_chunk_number: int|None — the surviving chunk's number (None for
        unchunked nodes)
    """
    by_node: dict[tuple, dict] = {}
    counts: dict[tuple, int] = {}
    for r in results:
        # Key on the caller-chosen fields. Default ("file_path","title") is the
        # v0.2.71 KG key; code passes ("file_path","full_name").
        key = tuple((r.get(f) or "") for f in key_fields)
        # F1 (pre-gate audit, SEV-1): an ALL-EMPTY key is un-collapsible —
        # key such rows by object identity (mirrors code_ranking.py's
        # `("__id__", id(c))` guard in run_code_retrieval_pipeline's _dedup_key)
        # so rows whose props carry none of the key fields NEVER bucket
        # together into one survivor. Protects the KG path too (a row with
        # neither file_path nor title stays distinct).
        if not any(key):
            key = ("__id__", id(r))
        counts[key] = counts.get(key, 0) + 1
        existing = by_node.get(key)
        if existing is None or r.get(score_field, 0.0) > existing.get(score_field, 0.0):
            by_node[key] = r
    collapsed = []
    for key, r in by_node.items():
        r = dict(r)  # don't mutate caller's dict
        r["chunks_matched"] = counts[key]
        # best_chunk_number reads the caller's chunk field (KG "chunk_number",
        # code "chunk_num"). Kept under the SAME output key so downstream
        # consumers (RL, formatters) read it uniformly.
        r["best_chunk_number"] = r.get(chunk_field)
        collapsed.append(r)
    collapsed.sort(key=lambda x: x.get(score_field, 0.0), reverse=True)

    # v0.2.70 concern-2: a SECOND collapse on CONTENT IDENTITY. The (file_path,
    # title) key above keeps two rows that are the SAME node living in two
    # collections — the canonical case is one node present in BOTH the project
    # KG and the shared KG (same title + same body, different file_path). To the
    # agent those are one identical block injected twice. The shared helper
    # collapses entries that share BOTH a name AND a content fingerprint; its
    # over-collapse guard keeps two genuinely-distinct-title nodes that merely
    # share a body separate. Score-sorted above, so the first survivor per
    # content-identity is the highest-scoring representative.
    try:
        from claude_mcp_servers.rl_client.content_dedup import (
            dedup_by_content_identity,
        )
        # dedup_kind defaults to "kg" (unchanged); code passes "code" so the
        # content-identity pass uses code_identity_key + code body fields.
        collapsed = dedup_by_content_identity(collapsed, kind=dedup_kind)
    except Exception:  # noqa: BLE001 — never break retrieval on a dedup import
        pass
    return collapsed


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
    # Loud-fail wrapper (2026-05-08 silent-zero antipattern fix v2).
    # Catches BOTH connection-time (get_weaviate_client raises
    # WeaviateUnreachable) and query-time (cached client + Weaviate
    # stopped mid-session → WeaviateQueryError raised from inside
    # _hybrid_search_single_collection's per-collection except, which we
    # changed below to classify+raise instead of swallow).
    try:
        return await _hybrid_search_body(
            query, limit, node_type, tags, days, detail, include_stale,
        )
    except WeaviateWorkspaceDriftError as exc:
        # v0.2.74 T5-1 backstop: surface the refuse-loud message verbatim.
        # Do NOT reset the client cache (this is not a Weaviate outage) —
        # the fix is a subprocess restart, not a reconnect.
        return _workspace_drift_response(exc, query=query)
    except WeaviateUnreachable as exc:
        _reset_weaviate_client_cache()
        return _weaviate_unreachable_response(exc, query=query)
    except WeaviateSchemaError as exc:
        # PR-41 Issue A: schema migrations invalidate cached schema.
        _reset_weaviate_client_cache()
        return _weaviate_schema_error_response(exc, query=query)
    except WeaviateAuthError as exc:
        # PR-41 Issue F: do NOT reset cache on auth errors.
        return _weaviate_auth_error_response(exc, query=query)
    except Exception as exc:
        classified = _classify_weaviate_failure(exc)
        if isinstance(classified, WeaviateUnreachable):
            _reset_weaviate_client_cache()
            return _weaviate_unreachable_response(classified, query=query)
        if isinstance(classified, WeaviateSchemaError):
            _reset_weaviate_client_cache()
            return _weaviate_schema_error_response(classified, query=query)
        if isinstance(classified, WeaviateAuthError):
            return _weaviate_auth_error_response(classified, query=query)
        raise


async def _hybrid_search_body(
    query: str,
    limit: int,
    node_type: str,
    tags: list,
    days: int,
    detail: str,
    include_stale: bool,
) -> str:
    """Implementation body for hybrid_search. Extracted so the public tool
    can wrap the entire query path with v2 loud-fail handling."""
    # v0.2.74 T5-1 backstop: refuse-loud if this call's live CLAUDE_PROJECT_DIR
    # diverged from the value this subprocess resolved its collections for.
    # Raises WeaviateWorkspaceDriftError (caught by the hybrid_search wrapper)
    # rather than silently fanning out over the wrong project's collections.
    _assert_workspace_unchanged("hybrid_search")
    # Build type/tag filter (NON-stale terms only; stale is mixed in
    # per-collection below — see V52-I Fix A).
    filters = []
    if node_type:
        filters.append(Filter.by_property("node_type").equal(node_type))
    if tags:
        for tag in tags:
            filters.append(Filter.by_property("tags").contains_any([tag]))

    weaviate_filter = None
    if filters:
        weaviate_filter = filters[0]
        for f in filters[1:]:
            weaviate_filter = weaviate_filter & f

    # V52-I Fix A (2026-06-09): the stale-filter is applied PER-COLLECTION
    # inside `_hybrid_search_single_collection` via `_stale_filter_for`,
    # NOT pre-combined here. Reason: not every collection in the fan-out
    # has `valid_until` (shared KG + *_Diagrams on existing installs lack
    # it). Pre-combining the stale clause produced 30 false-positive
    # `partial_fan_out_schema_missing` telemetry events. The per-collection
    # path lets each collection get the filter only if its schema supports
    # it. `include_stale` flows through unchanged.

    # Optional date filter
    date_filter = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        date_filter = Filter.by_property("updated_at").greater_than(cutoff)

    fetch_limit = limit * _RL_OVERFETCH

    # NEW-8 hotfix (2026-05-28 post-merge): capture the query vector once at
    # the top of hybrid_search so we can pass it to `_rl_cache_and_rerank`
    # for `query_emb=...`. Without this, the call at line ~4360 raises
    # `NameError: name 'query_vector' is not defined` at runtime — the
    # V38-MCP NEW-8 fix added `query_emb=query_vector` here mirroring the
    # `_semantic_graph_search_body` pattern but forgot that hybrid_search's
    # body never had `query_vector` in scope. None on the Weaviate-vectoriser
    # path (no raw vectors returned); the writer handles None gracefully.
    query_vector: list[float] | None = None
    # v0.2.47 RL-6b-2: ALSO capture the target named-vector slot so the
    # v3 enrichment helper knows which slot to pull from each fetched
    # object's `.vector` dict. The slot stays the same across every
    # collection in this fan-out (it's the active embedding's slot —
    # the per-collection schema decides whether to honor it, not which
    # slot to read).
    query_target: str = ""
    if EMBEDDING_SOURCE != "weaviate":
        try:
            _vec, _slot = await _get_search_vector(query)
            query_vector = _vec
            query_target = _slot or ""
        except Exception as exc:
            # Best-effort capture — vector unavailable means downstream
            # log_retrieval omits the field, but search itself proceeds.
            logger.debug("hybrid_search: query_vector capture failed (%s); proceeding without query_emb", exc)

    # Determine all collections to search: self + shared + peers (from
    # VCT_KG_ACCESS_LIST, P1-D 2026-05-08) + DEVELOPMENT_COLLECTION when
    # configured + DIAGRAMS_COLLECTION (+ diagram-access peers) when
    # configured (Phase 1.5.C). Single source of truth:
    # `_kg_collections_to_search`. Diagrams add a `result_kind="diagram"`
    # discriminator in `_format_obj` so Claude / the launcher can route
    # the click target (Read for .mmd vs describe_excalidraw for
    # .excalidraw vs the normal file open for KG nodes).
    collections_to_search: list[str] = _kg_collections_to_search(
        include_dev=True, include_diagrams=True,
    )

    # Search all collections and merge by (title, chunk) key, keeping best score per key.
    #
    # v0.2.24 (RL-defect-2026-05-22): missing-class errors are now
    # per-collection skips rather than fan-out-killing bubbles. A
    # hardcoded shared-KG default that doesn't exist on the user's
    # Weaviate must NOT kill the whole fan-out — other collections
    # (the user's project KG, peers, dev docs) may still resolve. Only
    # bubble the schema error when EVERY collection schema-failed
    # (Weaviate up but the schema is empty / instance-level issue).
    #
    # Unreachable / auth errors STILL bubble immediately — those apply
    # to the Weaviate instance as a whole, not one collection.
    merged: dict = {}
    failed_collections_schema: list[str] = []
    successful_collections: list[str] = []

    # KG-1 (v0.2.73): fan out to the collections CONCURRENTLY instead of
    # awaiting each in sequence. The three collections (project KG + shared
    # KG + dev/diagrams) are independent Weaviate round-trips, so total
    # latency is now max(per-collection) not sum. Each coroutine still gets
    # its own per-collection error classification below — a schema-missing
    # shared class is skipped, while an instance-level unreachable/auth
    # error bubbles (post-gather, after cache reset).
    # NEW-4 (v0.2.75): bound the fan-out concurrency. KG-1 made this a
    # concurrent gather (fine at 3-6 collections), but a wide
    # VCT_KG_ACCESS_LIST fans N concurrent Weaviate queries — enough to
    # saturate connections / thread pool on a large access matrix. A
    # Semaphore(4) caps in-flight collection queries while keeping the
    # latency win for the common 3-6 case (all fit under the limit, so no
    # serialization there).
    _fanout_sem = asyncio.Semaphore(4)

    async def _bounded_single_collection(coll_name: str):
        async with _fanout_sem:
            return await _hybrid_search_single_collection(
                coll_name, query, fetch_limit, weaviate_filter, date_filter,
                include_stale=include_stale,
            )

    _coll_results = await asyncio.gather(
        *[
            _bounded_single_collection(coll_name)
            for coll_name in collections_to_search
        ],
        return_exceptions=True,
    )
    for coll_name, coll_combined in zip(collections_to_search, _coll_results):
        if not isinstance(coll_combined, Exception):
            for key, item in coll_combined.items():
                if key not in merged or item["combined_score"] > merged[key]["combined_score"]:
                    merged[key] = item
            successful_collections.append(coll_name)
            continue
        e = coll_combined
        # Loud-fail v2: don't swallow Weaviate-unreachable. Connection-time
        # failures fire from get_weaviate_client(); query-time failures
        # (cached client + Weaviate stopped mid-session) fire here.
        classified = _classify_weaviate_failure(e)
        if isinstance(classified, WeaviateUnreachable):
            _reset_weaviate_client_cache()
            raise classified from e
        if isinstance(classified, WeaviateAuthError):
            # Auth errors persist; don't churn the connection.
            raise classified from e
        if isinstance(classified, WeaviateSchemaError):
            # v0.2.24: per-collection schema error → skip + record,
            # don't bubble. The shared-KG class may be missing on
            # this machine while the project KG resolves fine.
            logger.warning(
                "hybrid_search: skipping collection '%s' (schema error: %s)",
                coll_name, classified,
            )
            failed_collections_schema.append(coll_name)
            continue
        logger.warning(f"hybrid_search: error searching {coll_name}: {e}")

    # If EVERY collection in the fan-out schema-failed → instance-level
    # problem; bubble. But first log a degraded-mode retrieval event so
    # offline training sees the query distribution + failure rate even
    # when no nodes were retrieved.
    if not successful_collections and failed_collections_schema:
        _reset_weaviate_client_cache()
        # Best-effort failure telemetry. _rl_cache_and_rerank handles
        # empty-nodes + failure_mode and ALWAYS calls log_retrieval, so
        # we get the audit trail before the exception bubbles.
        try:
            failure_task_id = str(uuid.uuid4())
            await _rl_cache_and_rerank(
                failure_task_id, query, [], limit,
                failure_mode="all_collections_schema_missing",
                failed_collections=failed_collections_schema,
            )
        except Exception as exc:
            logger.debug(
                "hybrid_search: failure telemetry log_retrieval failed (%s); continuing",
                exc,
            )
        # v0.2.27: see semantic_graph_search counterpart for rationale.
        annotated_failed = _format_failed_collections_hint(failed_collections_schema)
        hint_suffix = (
            " — if names look unexpected, the resolved KG_COLLECTION / "
            "SHARED_KG_COLLECTION env vars likely don't match your project. "
            "Canonical channel: .claude/settings.json `env`. Check the "
            "'weaviate-kg: resolved collections' log line at server startup "
            "for what this MCP subprocess actually sees."
        )
        raise WeaviateSchemaError(
            "hybrid_search: every configured collection schema-failed "
            f"({len(failed_collections_schema)} attempted: "
            f"{annotated_failed})"
            + hint_suffix
        )

    # Sort all over-fetched candidates by combined score
    all_results = sorted(merged.values(), key=lambda x: x["combined_score"], reverse=True)

    # Collapse multi-chunk matches of the same node into one entry per node.
    # Runs before the RL rerank so the candidate pool the RL server sees has
    # one record per node-title (retrieval_rl.py keys on title — see notes in
    # _collapse_to_one_per_node). Strict superset of the previous behaviour:
    # unchunked nodes pass through untouched; multi-chunk-matched nodes
    # collapse to their best chunk + a chunks_matched signal.
    all_results = _collapse_to_one_per_node(all_results)

    # Preserve combined_score → score (BUG-SCORE-DROP fix). RL server may
    # overwrite this with its own normalised score; if not, the merged
    # combined_score (already 0..1, higher=better) is used as the surface score.
    for r in all_results:
        if "score" not in r and "combined_score" in r:
            r["score"] = r["combined_score"]

    # v0.2.47 RL-6b-2: enrich each node with `n_emb` / `linked_embs` /
    # `linked_type_names` / `cos_qn` / `cos_ql` / `cos_nl` BEFORE the RL
    # path sees the candidates. One batched Weaviate fetch per collection
    # (grouped by `collection` field on each node dict). Soft-fail
    # throughout: missing query_vector or Weaviate-unreachable leaves
    # the nodes as-is and the v3 retrieval event ships with whatever
    # was already attached by the search-time near_vector enrichment
    # (typically `emb` + `cos_qn`).
    # v0.2.71 Sweep-C: resolve the dual-RL-log other-slot inputs ONCE (gated on
    # dual-log AND dual-write env). None → bare single-log path.
    _dual_inputs = await _resolve_dual_rl_log_inputs(query, query_target)
    try:
        _rl_enrich_nodes_with_linked_embs(
            all_results,
            query_emb=query_vector,
            active_slot=query_target,
            model_name=EMBEDDING_MODEL,
            other_slot=(_dual_inputs or {}).get("other_slot", ""),
            other_query_emb=(_dual_inputs or {}).get("other_query_emb"),
            other_model_name=(_dual_inputs or {}).get("other_model", ""),
            backfill_other=_dual_inputs is not None,
        )
    except Exception as exc:
        logger.debug("hybrid_search: RL enrich failed (%s); proceeding without linked_embs", exc)

    # RL: rerank + cache using all candidates; return top-k.
    # v0.2.24: propagate any per-collection schema failures from the
    # fan-out so the telemetry event records WHICH collections failed
    # (helps diagnose hardcoded-default-vs-actual-Weaviate drift).
    task_id = str(uuid.uuid4())
    _partial_failure_mode = (
        "partial_fan_out_schema_missing" if failed_collections_schema else None
    )
    results = await _rl_cache_and_rerank(
        task_id, query, all_results, limit,
        failure_mode=_partial_failure_mode,
        failed_collections=failed_collections_schema or None,
        query_emb=query_vector,
        dual_log_inputs=_dual_inputs,
    )

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

    # Apply detail level. "auto" → per-result tier from score, with shared
    # chunk budget across all results (see _allocate_tier_within_budget).
    # Explicit value (e.g. detail="full") → uniform tier, no budget.
    formatted: list[dict] = []
    legacy_aliases = {"descriptions": "summary"}
    if detail == "auto":
        # Score-ordered allocation. Results coming out of _rl_cache_and_rerank
        # are already top-k score-ordered, but re-sort defensively.
        ordered = sorted(results, key=lambda r: r.get("score", 0.0) or 0.0, reverse=True)
        budget = _HYBRID_CHUNK_BUDGET
        for r in ordered:
            score = r.get("score", 0.0) or 0.0
            total_chunks = r.get("total_chunks") or 1
            tier, cost = _allocate_tier_within_budget(score, total_chunks, budget)
            if tier == "discard":
                continue
            budget -= cost
            # Pick the chunk-fetch collection from the result's source — without
            # this, shared-KG hits would fall back to snippet because their chunks
            # don't live in KG_COLLECTION.
            result_coll = r.get("collection") or KG_COLLECTION
            entry = _format_result_by_tier(r, tier, sidecar_db=None, coll=_coll_for(result_coll))
            if entry is not None:
                formatted.append(entry)
    else:
        # Explicit detail — uniform across all results, no budget.
        # Decision: when explicit detail == "full" was requested historically, the
        # behaviour was "300-char snippet". The new "full" tier additionally
        # assembles chunks for chunked nodes — strictly more useful, no regression
        # for unchunked nodes (still returns the snippet via the fallback path).
        tier = legacy_aliases.get(detail, detail)
        for r in results:
            if tier == "discard":
                continue
            result_coll = r.get("collection") or KG_COLLECTION
            entry = _format_result_by_tier(r, tier, sidecar_db=None, coll=_coll_for(result_coll))
            if entry is not None:
                formatted.append(entry)
    results = formatted

    # Log detail level for RL training signal
    _log_detail_choice(query, detail, len(results))

    # v0.2.74 T5-1: stamp PID + resolved KG_COLLECTION on the per-call line so
    # multi-subprocess drift (two weaviate_mcp servers scoped to different
    # workspaces) is diagnosable straight from the logs — a "0 results" report
    # can be cross-checked against which PID / collection actually served it.
    logger.info(
        f"hybrid_search[pid={os.getpid()} kg={KG_COLLECTION!r}]: "
        f"{len(results)} results (detail={detail}) for '{query}' "
        f"across {collections_to_search}"
    )
    # v0.2.31 module-deprecation surface (Layer 2): when the launcher has
    # injected the four `VCT_RL_MODULE_*` env vars into
    # `.claude/settings.json env`, prepend a deprecation banner to the
    # response so Claude sees it on every turn that hits hybrid_search.
    # NOT throttled — Claude's context is per-turn (see spec § Throttling).
    # NOT confined to RL reranker — the env vars are the canonical
    # cross-module deprecation channel (single message at a time today;
    # future paid modules write the SAME keys when their poller fires).
    try:
        from rl_client import _deprecation_warning as _rl_dep_warning
        dep_banner = _rl_dep_warning()
    except Exception:  # noqa: BLE001 — best-effort surface, never block
        dep_banner = None

    response: dict = {
        "success": True,
        "query": query,
        "count": len(results),
        "detail": detail,
        "results": results,
        "collections_searched": collections_to_search,
        "methods_used": ["semantic", "keyword"],
    }
    if dep_banner:
        # Two-field carry: a structured field for programmatic consumers
        # and a leading `notice` line for human-readable presentation
        # when Claude renders the JSON inline. Both carry the SAME
        # string so an aggressively-summarising consumer can't lose it.
        response = {"deprecation_warning": dep_banner, "notice": dep_banner, **response}
    return _large_result(response)


# ===========================================================================
# Phase 1.5.C: describe_excalidraw
# ===========================================================================
#
# Companion tool to hybrid_search. When a diagram result lands with
# result_kind="diagram" and file_path ends in .excalidraw, Claude can't
# usefully `Read(file_path)` — Excalidraw scenes are JSON blobs full of
# coordinates that don't ground the conversation. Instead, describe the
# scene by name + extracted text labels + element-type counts.
#
# The Mermaid case is simpler: .mmd is text-readable source, so the
# existing `Read(file_path)` is enough — no companion tool needed.
#
# Implementation: lightweight wrapper around
# vco_lib.diagram_indexer.parse_excalidraw. The STUB shipped alongside
# this branch (until Phase 1.5.A merges) is feature-complete for this
# tool's needs.
@mcp.tool()
async def describe_excalidraw(file_path: str) -> str:
    """
    Describe an Excalidraw scene by its text labels and element shape.

    Use this for .excalidraw files when ``hybrid_search`` returns a
    diagram (``result_kind="diagram"``) you want to inspect — gives you
    the scene name, all text labels, and a count of each element type
    without needing to see the canvas. For .mmd (Mermaid) diagrams,
    just ``Read(file_path)`` — those are plain text.

    Args:
        file_path: Absolute path to an ``.excalidraw`` file. Returned
            by ``hybrid_search`` as the ``file_path`` field of a
            diagram result.

    Returns:
        JSON with::

            {
                "success": true,
                "scene_name": "Auth Flow" | null,
                "text_labels": ["Login", "Submit", ...],
                "element_counts": {"rectangle": 4, "text": 2, ...},
                "file_path": "..."
            }

        On error (file missing, not JSON, not an .excalidraw file)
        returns ``{"success": false, "error": "..."}``.
    """
    payload: dict = {
        "file_path": file_path,
    }
    try:
        path = Path(file_path)
    except TypeError as exc:
        payload.update({"success": False, "error": f"invalid file_path: {exc}"})
        return _large_result(payload)

    if path.suffix.lower() != ".excalidraw":
        payload.update({
            "success": False,
            "error": (
                f"not an Excalidraw file: {path.suffix or '<no suffix>'}. "
                f"For .mmd (Mermaid) diagrams, use Read(file_path) — they "
                f"are plain text. describe_excalidraw only handles "
                f".excalidraw scenes."
            ),
        })
        return _large_result(payload)

    if not path.exists():
        payload.update({"success": False, "error": f"file not found: {path}"})
        return _large_result(payload)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        payload.update({"success": False, "error": f"cannot read file: {exc}"})
        return _large_result(payload)

    try:
        scene = json.loads(raw)
    except ValueError as exc:
        payload.update({
            "success": False,
            "error": f"file is not valid JSON: {exc}",
        })
        return _large_result(payload)

    if not isinstance(scene, dict):
        payload.update({
            "success": False,
            "error": (
                f"Excalidraw scene must be a JSON object at the top "
                f"level; got {type(scene).__name__}."
            ),
        })
        return _large_result(payload)

    # Import locally to keep the MCP startup time stable when this
    # tool is never called. The STUB has no side effects; Phase 1.5.A's
    # real implementation will likely also be import-cheap, but we
    # localise here to be safe.
    try:
        from vco_lib.diagram_indexer import parse_excalidraw
    except ImportError as exc:
        payload.update({
            "success": False,
            "error": (
                f"vco_lib.diagram_indexer is not importable: {exc}. "
                f"Phase 1.5.A STUB should be shipped alongside this MCP."
            ),
        })
        return _large_result(payload)

    meta = parse_excalidraw(scene)
    payload.update({
        "success": True,
        "scene_name": meta.scene_name,
        "text_labels": list(meta.text_labels),
        "element_counts": dict(meta.element_counts),
    })
    return _large_result(payload)


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
               Refuses (does NOT silently fall back) when
               SHARED_KG_WRITE_DISABLED=true for this project — see the
               module docstring for the asymmetric read/write semantic.

    Returns:
        JSON with success status and file_written flag
    """
    try:
        # v0.2.74 T5-1 backstop (defense-in-depth): refuse-loud on subprocess
        # workspace drift BEFORE resolving KG_COLLECTION — a WRITE fanning out to
        # the WRONG project's KG (stale module-load collection constants) would
        # CORRUPT another project's knowledge, strictly worse than a wrong read.
        # The reaper prevents the stale process; this is the per-call guard.
        _assert_workspace_unchanged("store_knowledge_node")
        client = get_weaviate_client()
        # Determine target collection based on scope
        target_collection_name = KG_COLLECTION
        targets_shared = (
            scope == "shared"
            and SHARED_KG_COLLECTION
            and SHARED_KG_COLLECTION != KG_COLLECTION
        )
        if targets_shared:
            target_collection_name = SHARED_KG_COLLECTION

        # Asymmetric write gate (2026-05-01). Refuse — don't silently reroute —
        # when the resolved target is the shared collection AND the write
        # gate is on. Resolving the gate at call time (not import time) means
        # the env var is honoured even when overridden mid-session, and lets
        # the test suite reload the value without re-importing the module.
        #
        # v0.2.44 fix-now-6: gate on requested SCOPE, not on the resolved target
        # name. After the orchestrator-root rebind (KG == SHARED), the previous
        # name-equality predicate would fire for scope='project' writes too,
        # blocking ALL writes when the gate is on. The semantic intent of the
        # gate is "block CROSS-PROJECT shared writes", which corresponds to
        # scope='shared' regardless of whether the physical collection happens
        # to be the same as the per-project one.
        if scope == "shared" and SHARED_KG_COLLECTION:
            if _resolve_shared_kg_write_disabled():
                return json.dumps({
                    "status": "error",
                    "error": (
                        "Shared KG writes are disabled for this project. "
                        "Set SHARED_KG_WRITE_DISABLED=false to enable, or "
                        "use scope='project' for the per-project KG."
                    ),
                    "target_collection": target_collection_name,
                    "scope": scope,
                    "file_written": False,
                }, indent=2)

        # v0.2.49 Phase 8 (item #21): access-matrix write gate.
        #
        # Consults the launcher's vct-hub for the project's access level
        # on `target_collection_name`. Fail-open: hub unreachable / 404 /
        # malformed → resolver returns "write" + emits WARNING + logs
        # dropped-write metric. Only "read" or "none" actually blocks the
        # write here.
        #
        # The asymmetric SHARED_KG_WRITE_DISABLED gate above remains the
        # PER-PROJECT-OVERRIDE for shared writes; this matrix gate is the
        # FINE-GRAINED per-(project, collection) policy. They compose:
        # SHARED_KG_WRITE_DISABLED fires first (coarse opt-out), then the
        # matrix check fires for everything else (per-collection write
        # permission per the launcher's GUI access matrix).
        #
        # Step F fixes (MF5 + MF6 + MF7+Q2):
        # - MF5: the ImportError catch is split out from the broad try/
        #   except so it only catches the resolver-not-installed case
        #   (pre-v0.2.49 path), not transitive ImportErrors raised by a
        #   future modification of vco_lib.access_resolver's own imports
        #   (e.g. if it ever adds `from requests import ...`).
        # - MF6: a resolver bug that surfaces as an exception emits a
        #   dropped_writes.jsonl row with reason='gate_crash' so the
        #   silent fail-open becomes visible.
        # - MF7+Q2: on deny, the response carries `writable_collections`
        #   (the project's other write-permission rows) so the LLM /
        #   user has actionable signal — "you can write to X, Y, Z;
        #   adjust GUI to enable target_collection."
        _access_resolver_available = False
        try:
            from vco_lib.access_resolver import check_access_level  # noqa: F401
            _access_resolver_available = True
        except ImportError as imp_err:
            # MF5: ONLY treat "the access_resolver module itself isn't on
            # the path" as the pre-v0.2.49 path. Any other ImportError
            # (e.g. a transitive import inside the resolver failing
            # because of a future dependency change) is a real bug, not
            # legacy-path — re-raise it so it's visible in logs.
            if "access_resolver" not in str(imp_err):
                raise
            # Pre-v0.2.49 install path: skip the gate, fall through to
            # the legacy "matrix is read-only" behavior.

        if _access_resolver_available:
            project_id_for_gate = os.environ.get("VCT_PROJECT_ID", "")
            if not project_id_for_gate:
                # v0.2.49 SB1: empty-PID branch was a silent-bypass
                # (gate effectively disabled). Per the user's 2026-06-08
                # Q1 directive, silent-allow stays the default; the two
                # visibility surfaces (metric + deferral) carry the
                # remediation. The write itself proceeds — the gate
                # only blocks on explicit "read" / "none" verdicts, not
                # on missing identity.
                #
                # Order is metric-first (always fires) then deferral
                # (deduped per session) so the JSONL row lands even
                # when the deferral write hits a missing-project-dir
                # / unwritable .claude/context branch.
                _emit_gate_skipped_metric(target_collection_name)
                _emit_gate_skipped_deferral(target_collection_name)
            else:
                try:
                    matrix_level = check_access_level(project_id_for_gate, target_collection_name)
                except Exception as gate_exc:
                    # MF6: resolver bug → emit dropped-write metric so
                    # the silent fail-open becomes visible. Don't break
                    # the fail-open contract on top of an already-broken
                    # resolver; log + metric + continue with write.
                    logger.warning(
                        "access matrix gate crashed; falling open: %s", gate_exc
                    )
                    _emit_gate_crash_metric(project_id_for_gate, target_collection_name, str(gate_exc))
                    matrix_level = "write"  # fail-open

                if matrix_level != "write":
                    # MF7+Q2: enrich the deny response with the list of
                    # collections the project DOES have write access to,
                    # so the LLM / user has actionable remediation
                    # instead of just "denied."
                    #
                    # Source: hub's GET /api/v1/projects/{id}/access?level=write
                    # endpoint (lands in this same v0.2.49 cycle — main
                    # chat's lane). Until that endpoint exists, this
                    # helper returns an empty list and the response
                    # falls back to the generic remediation string.
                    writable_collections = _fetch_writable_collections_for_project(
                        project_id_for_gate
                    )
                    if writable_collections:
                        remediation = (
                            f"You currently have write access on: "
                            f"{', '.join(writable_collections)}. "
                            f"To gain write access to '{target_collection_name}', "
                            f"adjust via Launcher GUI → Identity → Manage access."
                        )
                    else:
                        remediation = (
                            "No collections currently have write access for this "
                            "project — re-register the project via Launcher GUI → "
                            "Projects, or adjust the access matrix in the Identity tab."
                        )
                    return json.dumps({
                        "status": "error",
                        "error": (
                            f"Access matrix denies write on '{target_collection_name}' "
                            f"(level={matrix_level}). {remediation}"
                        ),
                        "target_collection": target_collection_name,
                        "matrix_level": matrix_level,
                        "writable_collections": writable_collections,
                        "remediation": remediation,
                        "scope": scope,
                        "file_written": False,
                    }, indent=2)

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
        #   3. Relative + CLAUDE_PROJECT_DIR (D-10) → that project root
        #   4. Relative + neither → _SERVER_INFERRED_BASE / file_path
        #      (LAST resort — writes into the orchestrator clone; flagged)
        #
        # rel_file_path: always relative (strip base prefix from absolute inputs).
        # -------------------------------------------------------------------------
        md_path: Optional[Path] = None
        rel_file_path: str = file_path  # default: use as-is if already relative
        wrote_outside_project: bool = False

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
                # D-10 (v0.2.73): before falling back to the SERVER-inferred
                # base (which lands the .md inside the ORCHESTRATOR CLONE, not
                # the user's project — a boundary violation the tool then
                # reports as success), prefer CLAUDE_PROJECT_DIR via the same
                # resolver the deferral writer uses. This is the documented
                # env-propagation footgun class (inert .vscode channel,
                # subagent env loss): a relative file_path + unset KG_BASE_DIR
                # should still resolve to the project when CLAUDE_PROJECT_DIR
                # is present.
                _proj_root = _resolve_project_root_for_deferral()
                if _proj_root is not None:
                    md_path = _proj_root / file_path
                    logger.info(
                        f"KG_BASE_DIR not set — resolved via CLAUDE_PROJECT_DIR/"
                        f"KG_BASE_DIR fallback chain to project root: {md_path}"
                    )
                else:
                    md_path = _SERVER_INFERRED_BASE / file_path
                    wrote_outside_project = True
                    logger.warning(
                        "KG_BASE_DIR and CLAUDE_PROJECT_DIR both unset — writing "
                        "the .md into the ORCHESTRATOR CLONE (%s), NOT the user's "
                        "project. The Weaviate row targets the project's "
                        "KG_COLLECTION, so this stray file is clone-side sync "
                        "noise. Set KG_BASE_DIR or pass an absolute file_path.",
                        md_path,
                    )
            # rel_file_path stays as file_path for relative inputs (already correct)

        # C-7 (v0.2.75): normalize the STORED spelling to canonical POSIX
        # (forward-slash) at WRITE, and match BOTH spellings at DELETE. A node
        # written on Windows could land in Weaviate with a backslash
        # `knowledge\concepts\foo.md` while a later POSIX write derives
        # `knowledge/concepts/foo.md`; the exact-equal delete below then missed
        # the drifted old rows, stranding duplicates that surface as repeated
        # retrieval hits. `rel_file_path` is normalized here so `properties[...]`
        # (built below) stores the canonical form going forward.
        if rel_file_path:
            rel_file_path = rel_file_path.replace("\\", "/")

        # Locate existing rows for THIS node (v0.2.73 D-1): scope the match to
        # `title AND file_path`, mirroring sync_knowledge_graph.py's
        # `_delete_node_by_file_path` (the v0.2.70 P1 fix). Title is NOT unique
        # across the collection — an archived and an active node can share a
        # title at different file_paths — so a title-only delete silently
        # removed the OTHER node's rows (its .md survived on disk but it
        # vanished from retrieval until a full kg-sync --all). When
        # rel_file_path is somehow empty (defensive; _normalize_kg_file_path
        # auto-derives one), fall back to the legacy title-only match (the
        # ratified C-7 fallback: better a title-only cleanup than deleting
        # nothing and accumulating duplicate rows).
        _delete_filter = Filter.by_property("title").equal(title)
        if rel_file_path:
            # C-7: match BOTH the canonical POSIX spelling AND the backslash
            # variant so a Windows-written old row (or any separator drift) is
            # still reconciled. Use an OR of two EXACT `.equal()` filters (not
            # `contains_any`, which is token-based and would not match a full
            # path string exactly) — `.equal()` is the established exact-match
            # pattern for file_path (sync_knowledge_graph.py uses it too).
            _backslash_variant = rel_file_path.replace("/", "\\")
            if _backslash_variant != rel_file_path:
                _path_filter = Filter.any_of([
                    Filter.by_property("file_path").equal(rel_file_path),
                    Filter.by_property("file_path").equal(_backslash_variant),
                ])
            else:
                _path_filter = Filter.by_property("file_path").equal(rel_file_path)
            _delete_filter = _delete_filter & _path_filter
        # v0.2.73 D-2: collect the stale row ids NOW (before any insert, so the
        # scoped filter can't match the fresh rows) but do NOT delete yet.
        # Pre-D-2 the delete ran here — BEFORE embeddings were fetched — so a
        # routine embed failure (Ollama down / timeout) destroyed the existing
        # node: the tool returned success=false AND the previous rows were
        # already gone until a manual resync. New order: embed + insert the
        # new rows first, delete the stale rows LAST ("conservative defaults
        # on best-effort paths"). Worst case on a mid-insert failure is
        # temporary duplicate rows (old + partial new), which the next
        # successful upsert or kg-sync cleans up — strictly better than data
        # loss.
        # C-7: loop past limit=100 — a heavily-chunked node (or accumulated
        # drift) can exceed 100 rows; a single fetch capped at 100 would leave
        # the overflow stranded. Page until a short batch returns.
        _stale_uuids = []
        _fetch_offset = 0
        _FETCH_PAGE = 100
        while True:
            _batch = collection.query.fetch_objects(
                filters=_delete_filter,
                limit=_FETCH_PAGE,
                offset=_fetch_offset,
            )
            _batch_objs = _batch.objects
            _stale_uuids.extend(obj.uuid for obj in _batch_objs)
            if len(_batch_objs) < _FETCH_PAGE:
                break
            _fetch_offset += _FETCH_PAGE
            # Defensive bound: never page forever (corrupt cursor / duplicate
            # rows) — 50 pages = 5000 rows is far past any real node.
            if _fetch_offset >= 50 * _FETCH_PAGE:
                break

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
            # Multi-chunk insert: split then embed each chunk independently.
            # v0.2.73 D-2: embed ALL chunks BEFORE inserting any, so a mid-loop
            # embed failure (the routine outage case) aborts with ZERO new rows
            # written and the stale rows still pending deletion below — no
            # partial-chunk state, no data loss.
            chunker = Chunker.for_model(EMBEDDING_MODEL)
            raw_chunks = chunker.chunk_text(content, source_id=title)
            chunk_count = len(raw_chunks)
            prepared_inserts: list[tuple[dict, "list | dict | None"]] = []
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
                    prepared_inserts.append((chunk_props, None))
                elif DUAL_EMBEDDING_ENABLED:
                    vectors = await _get_all_kg_embeddings(chunk.content)
                    prepared_inserts.append(
                        (chunk_props, vectors if vectors else None)
                    )
                else:
                    # Embed the raw chunk text (without header) for clean vectors
                    vector = await get_embedding(chunk.content)
                    prepared_inserts.append((chunk_props, vector))
            for chunk_props, chunk_vec in prepared_inserts:
                if EMBEDDING_SOURCE == "weaviate":
                    collection.data.insert(properties=chunk_props)
                else:
                    collection.data.insert(properties=chunk_props, vector=chunk_vec)

        # v0.2.73 D-2: delete the stale rows LAST — the new rows are safely in.
        for _stale_uuid in _stale_uuids:
            collection.data.delete_by_id(_stale_uuid)

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
        if wrote_outside_project:
            # D-10: make the boundary violation visible in the tool result,
            # not just a log line the hook contract hides.
            result["file_note"] = (
                "wrote outside project: KG_BASE_DIR and CLAUDE_PROJECT_DIR "
                "both unset, so the .md landed in the orchestrator clone. "
                "Set KG_BASE_DIR or pass an absolute file_path."
            )
        if path_adjustments:
            result["path_adjustments"] = path_adjustments
        if file_write_error:
            result["file_error"] = file_write_error
        elif md_path is not None and not file_written:
            result["file_note"] = "file already up to date"
        elif md_path is None:
            result["file_note"] = "no file_path provided, Weaviate-only"
        return json.dumps(result, indent=2)

    except WeaviateWorkspaceDriftError as exc:
        # v0.2.74 T5-1 backstop: surface the refuse-loud message verbatim so the
        # write is NOT silently misrouted to the wrong project's KG. Must precede
        # the generic Exception handler (it IS an Exception subclass).
        logger.error("store_knowledge_node refused (workspace drift): %s", exc)
        return json.dumps({
            "success": False,
            "error": str(exc),
            "error_class": "WeaviateWorkspaceDriftError",
        }, indent=2)
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
        layer: Filter by architectural layer — lowercase values: api, service,
               data, ui, utility (mixed-case input is lowercased before
               matching). If the filter yields zero candidates (the `layer`
               property is unpopulated on many indexes), the search re-runs
               without it and the response carries a "note" explaining that.
        project: Project name override. Omit to use workspace default.
        detail: Verbosity per result (default "auto"):
            - "auto"   → score-tiered per result via the code-calibrated gate
                         (_CODE_TIER_THRESHOLDS, env-overridable CODE_TIER_*):
                           score < min          → dropped (min derives from the
                                                  post-rerank floor at call
                                                  time, default 0.22)
                           min..0.32            → "summary" (signature + doc)
                           0.32..0.48           → "single_chunk" (matched chunk)
                           0.48..0.62           → "three_chunks" (hit + neighbours)
                           >= 0.62              → "full" (up to 7 chunks)
                         A shared chunk budget degrades late results to
                         cheaper tiers regardless of score.
            - "titles" → metadata-only refs for every result (cheapest)
            - "full"   → full details for every result (most expensive)

    Returns:
        JSON with code entities, each including file_path, score, tier (the
        verbosity actually applied in auto mode), and tier-dependent content
        (full_name/signature/doc/body chunks). Metadata refs for cheap tiers.
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

    # P1-D (2026-05-08): build the list of project prefixes to search.
    # When `effective_project` is set, fan out across self + every peer in
    # VCT_CODE_GRAPH_ACCESS_LIST. When None ("search all projects"), fall
    # back to the bare-collection path (no per-project prefix, no project
    # filter). The peer list is consumed only when an effective_project is
    # set — otherwise the caller is explicitly asking for cross-tenant
    # search and we don't want to re-scope it.
    base_names = _SCOPES.get(scope, _SCOPES["all"])
    if effective_project:
        # v0.2.74 (BLOCKER-1): code-graph fan-out uses the underscore-PRESERVING
        # sanitizer (matches the analyzer's write class), NOT the diagrams/KG
        # dropping rule.
        self_prefix = _code_sanitize_collection_prefix(effective_project)
        prefixes: list[tuple[str, str]] = [(self_prefix, effective_project)]
        for peer in _parse_csv_env("VCT_CODE_GRAPH_ACCESS_LIST"):
            peer_prefix = _code_sanitize_collection_prefix(peer)
            if not peer_prefix or peer_prefix == self_prefix:
                continue
            # Dedupe by prefix; the second tuple element carries the
            # project-property filter value (which is the un-sanitized
            # name as stored by the analyzer in the `project` property).
            if any(p == peer_prefix for p, _ in prefixes):
                continue
            prefixes.append((peer_prefix, peer))
    else:
        prefixes = [("", "")]  # bare collections, no filter

    # Build per-(prefix, base) collection-name list and reverse-map:
    #   collection_name -> (base, project_filter_value or "")
    # `project_filter_value` is the value to pass to the
    # `Filter.by_property("project").equal(...)` clause; "" disables the
    # filter (cross-tenant search path).
    collections: list[str] = []
    coll_meta: dict[str, tuple[str, str]] = {}
    for prefix, project_filter in prefixes:
        for base in base_names:
            coll_name = f"{prefix}_{base}" if prefix else base
            collections.append(coll_name)
            coll_meta[coll_name] = (base, project_filter)

    # Self-only collection-name resolver kept for the expansion paths
    # (callers/callees, neighbour fetch). Expansion stays self-scoped on
    # purpose: chasing call graphs across peer projects would explode the
    # result set with cross-tenant noise that the user did not ask for —
    # peer fan-out is opt-in via the seed search above.
    def _project_collection(base: str) -> str:
        if effective_project:
            # v0.2.74 (BLOCKER-1): code-graph → underscore-PRESERVING sanitizer.
            prefix = _code_sanitize_collection_prefix(effective_project)
            return f"{prefix}_{base}"
        return base
    # Reverse map for backward-compat at the few remaining call sites
    # below that look up `_base_for[coll_name]` after fetching by self
    # collection.
    _base_for = {_project_collection(b): b for b in base_names}

    try:
        # v0.2.73 C-5: embed the query via the CLI-mirrored path
        # (svc.embed_code for ALL slots) so the MCP and CLI produce the
        # SAME query vector on every ladder tier. Using the codesage-biased
        # get_code_embedding here broke CLI≡MCP on qwen3/jina slots.
        query_embedding = await get_code_query_embedding(query)
        if not query_embedding:
            return json.dumps({"success": False, "error": "Failed to generate query embedding"}, indent=2)

        client = get_weaviate_client()

        # Gather candidates from each collection
        # v0.2.72 (P1/P2): over-fetch 2N per collection so the shared
        # `run_code_retrieval_pipeline` has a pool to floor-cull + rerank +
        # collapse before trimming to `limit`. Matches the CLI over-fetch.
        _fetch_limit = max(1, 2 * limit)

        def _gather_candidates(apply_layer: bool) -> list[dict]:
            gathered: list[dict] = []
            for coll_name in collections:
                try:
                    coll = client.collections.get(coll_name)
                    kwargs: dict = dict(
                        near_vector=query_embedding,
                        limit=_fetch_limit,
                        return_metadata=MetadataQuery(distance=True, score=True),
                    )
                    # v0.2.18: target the slot matching the active code
                    # backend (codesage_embed / ollama_code_embed /
                    # openai_code_embed / jina_embed). Falls back to the
                    # pre-v0.2.18 ACTIVE_EMBEDDING branching when the
                    # service isn't available.
                    if DUAL_EMBEDDING_ENABLED:
                        # v0.2.73 C-6: mirror the CLI's
                        # ``_active_code_vector_slot`` — svc present → its
                        # resolved slot; svc None → "codesage_embed" (the
                        # CLI's unconditional svc-None fallback). Previously
                        # the MCP branched on ACTIVE_EMBEDDING here while the
                        # CLI did not, so with e.g. ACTIVE_EMBEDDING=arctic +
                        # a broken vco_lib import the two surfaces searched
                        # different named vectors.
                        # MUST MATCH query_code_graph.py::_active_code_vector_slot.
                        kwargs["target_vector"] = _active_code_query_slot()
                    # v0.2.73 RL-2b: fetch each candidate's stored vector in the
                    # SAME query (no second round-trip) so citation staging has
                    # real per-node vectors to cosine against. When a named
                    # target slot is set, request exactly that slot — the
                    # near_vector call already references it, so this cannot
                    # introduce a new failure mode; the bare-vector path (no
                    # target_vector) requests the default vector.
                    _iv_slot = kwargs.get("target_vector")
                    kwargs["include_vector"] = [_iv_slot] if _iv_slot else True
                    # Build filters: project (per-collection) + optional layer.
                    base_name, project_filter = coll_meta.get(coll_name, (coll_name, ""))
                    active_filters = []
                    if project_filter:
                        active_filters.append(Filter.by_property("project").equal(project_filter))
                    if apply_layer and layer and base_name in ("CodeFunction", "CodeClass"):
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
                        # F11-iv (pre-gate audit): clamp to >= 0.0 — MUST MATCH
                        # the CLI's `max(0.0, 1.0 - distance)` normalisation
                        # (query_code_graph.py) so the two surfaces score
                        # identically on degenerate distances > 1.0.
                        score = max(0.0, 1.0 - distance)
                        # Store base name (e.g. "CodeFunction") for formatting
                        # (not the per-project name) + `_src` = the row's
                        # source-project filter value ("" for self/bare) so
                        # the render loop can gate peer rows off the SELF
                        # chunk fetcher (F5 — mirrors the CLI's `_src`).
                        cand = {
                            "_c": base_name, "_s": score, "_d": distance,
                            "_p": p, "_src": project_filter or "",
                        }
                        # v0.2.73 RL-2b: attach the row's stored vector as
                        # `n_emb` (same key the KG path uses) so citation
                        # staging downstream can cosine the answer against
                        # it. Soft-fail — a missing/odd-shaped vector just
                        # means this row won't be citable.
                        try:
                            _ov = getattr(obj, "vector", None)
                            _vec = None
                            if isinstance(_ov, dict):
                                if _iv_slot:
                                    _vec = _ov.get(_iv_slot)
                                elif _ov:
                                    _vec = _ov.get("default") or next(
                                        iter(_ov.values()), None
                                    )
                            elif _ov:
                                _vec = list(_ov)
                            if _vec:
                                cand["n_emb"] = list(_vec)
                        except Exception:  # noqa: BLE001 — never blocks search
                            pass
                        gathered.append(cand)
                except Exception as e:
                    logger.warning(f"search_code_graph: {coll_name} failed: {e}")
            return gathered

        candidates = _gather_candidates(apply_layer=True)
        layer_note: str | None = None
        if layer:
            # LAYER-FILTER TRAP (pre-gate audit): the `layer` property is
            # unpopulated on most indexes (and absent from older schemas,
            # where the filter ERRORS per-collection), so the filter silently
            # matches nothing. Gate the retry on the collections the filter
            # actually applies to (CodeFunction/CodeClass) — with scope="all"/
            # "code", unfiltered CodeModule/CodeAPI rows would otherwise keep
            # the raw pool non-empty and mask the zeroed-out filter (live-
            # confirmed). Re-run WITHOUT the filter and tell the caller via a
            # note instead of returning a misleading empty result.
            _layer_filtered_pool = [
                c for c in candidates if c.get("_c") in ("CodeFunction", "CodeClass")
            ]
            if not _layer_filtered_pool:
                candidates = _gather_candidates(apply_layer=False)
                layer_note = (
                    "layer filter ignored — the layer property is not populated "
                    "on this index"
                )

        # v0.2.72 (P1/P2/P3/P4): run the SHARED pipeline — two-stage per-slot
        # floor (retrieval 0.16 / post-rerank 0.22 for CodeSage) + relationship
        # rerank + multi-chunk collapse + score-tier allocation — then trim to
        # `limit`. This is the SAME `run_code_retrieval_pipeline` the CLI
        # (query_code_graph.py) calls with the SAME adapters, so the MCP and
        # hook paths cannot diverge (the hard invariant). anchor_props=None
        # here: a direct MCP call has no edit/grep anchor, so the relationship
        # boost degrades to none → pure semantic order.
        #
        # Every entity is on the P7-resynced data model (chunk_num /
        # total_chunks / embed_revision), so we render via the NEW score-tier
        # path (`_format_code_result_by_tier`), NOT the legacy rank-based one:
        # tier_fn annotates each survivor with `_tier` under the code-calibrated
        # gate (min 0.22), and the format loop below assembles 1/3/7 chunks by
        # tier. In "auto" detail the tier_fn drives verbosity; explicit
        # "titles"/"full" still override per-result below.
        try:
            _svc = _get_embedding_service() if DUAL_EMBEDDING_ENABLED else None
            _slot = _svc.code_vector_slot if _svc is not None else "codesage_embed"
        except Exception:
            _slot = "codesage_embed"

        # Normalize detail before tier selection.
        if detail not in ("auto", "titles", "full"):
            detail = "auto"

        # Score-tier allocation only runs in "auto" mode; explicit detail values
        # want uniform output, so we skip the budget allocator and let the
        # format loop honour `detail` directly (tier_fn=None → no `_tier` set).
        # F4 (pre-gate audit): the tier `min` gate DERIVES from the resolved
        # post-rerank floor so a GUI/env floor override changes what renders in
        # auto mode (identical wiring in the CLI — the hard invariant).
        #
        # NOTE (B2, design audit): the CLI grows an `--exclude-file` pre-pipeline
        # candidate filter for the Read/Edit hook's self-injection case. The MCP
        # has NO exclude concept (a direct tool call has no edited-file context),
        # so no equivalent filter exists here — deliberate, not drift.
        _post_floor = resolve_post_rerank_floor(_slot)
        _tier_fn = make_code_tier_fn(min_gate=_post_floor) if detail == "auto" else None
        candidates = run_code_retrieval_pipeline(
            candidates,
            retrieval_floor=resolve_retrieval_floor(_slot),
            post_rerank_floor=_post_floor,
            anchor_props=None,
            limit=limit,
            collapse_fn=make_code_collapse_fn(),
            tier_fn=_tier_fn,
            key_fields=("file_path", "full_name"),
        )

        # v0.2.73 RL-2: the code path now emits retrieval telemetry (it was a
        # black hole — the RL corpus was KG-only). One shared emit home with
        # the CLI; soft-fail, never blocks the search.
        _emit_code_retrieval_telemetry(
            query=query,
            query_emb=query_embedding,
            survivors=candidates,
            limit=limit,
            slot=_slot,
            task_type="code_search",
            retrieval_floor=resolve_retrieval_floor(_slot),
            post_rerank_floor=_post_floor,
            anchor_present=False,
            scope=scope,
        )

        def _fetch_file_siblings(file_path: str, hit_start_line: int, max_total: int, exclude_full_name: str) -> list[dict]:
            """Fetch up to (max_total - 1) siblings in the same source file,
            ordered by start_line, centred on hit_start_line. Returns formatted
            metadata-ref dicts (siblings are context, not primary results).
            Returns [] on any failure or if file_path is missing.

            Closes over `client`, `effective_project`, and `_project_collection`
            from the enclosing search_code_graph call. Passed to the shared
            `_format_code_result_by_rank` helper as `sibling_fetcher` so the
            helper itself stays Weaviate-agnostic.
            """
            if not file_path or max_total <= 1:
                return []
            try:
                fn_coll = client.collections.get(_project_collection("CodeFunction"))
                cls_coll = client.collections.get(_project_collection("CodeClass"))
            except Exception as exc:
                logger.debug("search_code_graph: sibling collection unavailable (%s)", exc)
                return []
            collected: list[tuple[int, str, dict]] = []
            for coll_obj, c_name in ((fn_coll, "CodeFunction"), (cls_coll, "CodeClass")):
                try:
                    sib_filter = Filter.by_property("file_path").equal(file_path)
                    if effective_project:
                        sib_filter = sib_filter & Filter.by_property("project").equal(effective_project)
                    sib_resp = coll_obj.query.fetch_objects(filters=sib_filter, limit=64)
                    for obj in sib_resp.objects:
                        sp = obj.properties or {}
                        if sp.get("full_name") == exclude_full_name:
                            continue
                        sl = sp.get("start_line")
                        try:
                            sl_int = int(sl) if sl is not None else 0
                        except (TypeError, ValueError):
                            sl_int = 0
                        collected.append((sl_int, c_name, sp))
                except Exception as exc:
                    logger.debug("search_code_graph: sibling fetch %s failed: %s", c_name, exc)
            if not collected:
                return []
            collected.sort(key=lambda t: abs(t[0] - hit_start_line))
            picked = collected[: max_total - 1]
            picked.sort(key=lambda t: t[0])
            siblings: list[dict] = []
            for sl_int, c_name, sp in picked:
                ref = _format_code_result_ref(c_name, sp)
                ref["sibling"] = True
                ref["start_line"] = sl_int
                ref["collection"] = c_name
                siblings.append(ref)
            return siblings

        def _fetch_code_chunks(full_name: str, hit_chunk: int, total: int, max_chunks: int, file_path: str = "") -> list[dict]:
            """Fetch up to `max_chunks` property-dicts for one code entity's
            chunks (matched + neighbours), ordered by chunk_num, centred on
            `hit_chunk`. Keyed on `full_name` (code's node identity) — the
            code analogue of `_fetch_node_chunks` (which keys on `title`).
            Returns [] on any failure or for a single-chunk entity.

            C-8 (v0.2.75 P2b): `file_path` scopes the fetch to the winning
            row's source file so two same-`full_name` entities in different
            files (a common stem like `run`/`main`/`__init__`) cannot interleave
            each other's chunk bodies. Empty `file_path` (older callers / rows
            with no stamp) preserves the pre-fix full_name+project filter.

            Closes over `client` + `_project_collection`; passed to
            `_format_code_result_by_tier` as `chunk_fetcher` so the helper
            stays Weaviate-agnostic. Searches CodeFunction + CodeClass (the
            only chunked code collections).
            """
            if not full_name or total <= 1 or max_chunks <= 1:
                return []
            collected: list[tuple[int, dict]] = []
            for base in ("CodeFunction", "CodeClass"):
                try:
                    coll_obj = client.collections.get(_project_collection(base))
                    flt = Filter.by_property("full_name").equal(full_name)
                    if effective_project:
                        flt = flt & Filter.by_property("project").equal(effective_project)
                    if file_path:
                        flt = flt & Filter.by_property("file_path").equal(file_path)
                    resp = coll_obj.query.fetch_objects(filters=flt, limit=max(total, max_chunks) + 4)
                    for obj in resp.objects:
                        cp = obj.properties or {}
                        cn = cp.get("chunk_num", 0) or 0
                        try:
                            collected.append((int(cn), cp))
                        except (TypeError, ValueError):
                            collected.append((0, cp))
                except Exception as exc:
                    logger.debug("search_code_graph: code chunk fetch %s failed: %s", base, exc)
            if not collected:
                return []
            # Centre a window of max_chunks around the hit chunk, ordered by chunk_num.
            collected.sort(key=lambda t: abs(t[0] - (hit_chunk or 0)))
            picked = collected[:max_chunks]
            picked.sort(key=lambda t: t[0])
            return [cp for _, cp in picked]

        # Render each survivor. In "auto" mode every candidate carries a
        # `_tier` (from the shared pipeline's tier_fn) → render via the
        # score-tier code renderer (`_format_code_result_by_tier`), assembling
        # 1/3/7 chunks by tier over the P7-resynced multi-chunk data model.
        # Explicit "titles"/"full" detail (tier_fn was None → no `_tier`) →
        # the rank-based formatter honours `detail` uniformly. BOTH paths are
        # shared with the CLI so the two surfaces stay identical.
        results = []
        for i, r in enumerate(candidates):
            coll_name, p, score, dist = r["_c"], r["_p"], r["_s"], r["_d"]
            tier = r.get("_tier")
            if tier is not None:
                results.append(_format_code_result_by_tier(
                    p, coll_name, tier,
                    score=score,
                    distance=dist,
                    # F5: peer rows must not assemble chunks from the SELF
                    # collections — the shared gate returns None for them so
                    # the tier degrades to single_chunk (matched chunk only).
                    chunk_fetcher=_self_project_chunk_fetcher(
                        r, effective_project, _fetch_code_chunks,
                    ),
                ))
            else:
                results.append(_format_code_result_by_rank(
                    p, coll_name, i,
                    detail=detail,
                    score=score,
                    distance=dist,
                    sibling_fetcher=_fetch_file_siblings,
                ))

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
                        if len(expanded_results) >= CODE_EXPANSION_LIMIT:
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
                                        if len(expanded_results) >= CODE_EXPANSION_LIMIT:
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
                                        if len(expanded_results) >= CODE_EXPANSION_LIMIT:
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
                                        if len(expanded_results) >= CODE_EXPANSION_LIMIT:
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

        response_payload = {
            "success": True,
            "query": query,
            "scope": scope,
            "expand_hops": effective_hops,
            "detail": detail,
            "count": len(results),
            "results": results,
        }
        if layer_note:
            # LAYER-FILTER TRAP: tell the caller the layer filter was dropped.
            response_payload["note"] = layer_note
        return _large_result(response_payload)

    except WeaviateUnreachable as exc:
        # Loud-fail per 2026-05-08 silent-zero antipattern fix.
        _reset_weaviate_client_cache()
        return _weaviate_unreachable_response(exc, query=query)
    except WeaviateSchemaError as exc:
        # PR-41 Issue A: cache reset on schema-not-found.
        _reset_weaviate_client_cache()
        return _weaviate_schema_error_response(exc, query=query)
    except WeaviateAuthError as exc:
        return _weaviate_auth_error_response(exc, query=query)
    except Exception as e:
        # Loud-fail v2: query-time failures (cached client + Weaviate
        # stopped mid-session) raise WeaviateQueryError, not
        # WeaviateUnreachable. Classify before falling through to the
        # generic error handler.
        classified = _classify_weaviate_failure(e)
        if isinstance(classified, WeaviateUnreachable):
            _reset_weaviate_client_cache()
            return _weaviate_unreachable_response(classified, query=query)
        if isinstance(classified, WeaviateSchemaError):
            _reset_weaviate_client_cache()
            return _weaviate_schema_error_response(classified, query=query)
        if isinstance(classified, WeaviateAuthError):
            return _weaviate_auth_error_response(classified, query=query)
        logger.error(f"Error in code graph search: {e}")
        return json.dumps({"success": False, "error": str(e)}, indent=2)


def _caller_match_terms(target: str) -> list[str]:
    """Candidate names to match against a CodeFunction's ``call_names``.

    The analyzer stores ``call_names`` as BARE leaf names
    (``['_start_services', 'strip', ...]`` — verified live), but the natural
    and documented input to a ``callers`` query is the fully-qualified
    ``full_name`` (Python ``install._start_services``, Rust
    ``server::start_hub_server``). A dotted / ``::``-qualified target therefore
    never matches the bare leaves, so ``callers`` silently returns nothing.

    Return ``[target, <leaf>]`` (deduped, order-preserving) so a ``callers``
    query resolves for BOTH ``module.fn`` and bare ``fn`` inputs. The leaf is
    the last segment after splitting on ``.`` or Rust ``::``.
    (WS-4 Finding 1, v0.2.62.)
    """
    terms = [target]
    leaf = re.split(r"::|\.", target)[-1]
    if leaf and leaf != target:
        terms.append(leaf)
    return terms


def _pick_canonical_chunk(objects: list):
    """From a list of Weaviate code objects sharing a full_name, return the
    CANONICAL (chunk_num == 0) one.

    v0.2.72 (P3): a chunked function/class is stored as N objects that all
    carry the same ``full_name`` but different ``chunk_num`` (0-indexed). The
    ``query_code_structure`` lookups (methods / extends / interactions) want
    THE object for that name — which must be the canonical chunk 0 (it holds
    the signature-leading body, the methods list, and is the reference target
    cross-references resolve to).

    Selection: prefer chunk_num == 0; then a legacy row with chunk_num
    None/absent (single-chunk pre-migration entity); else the lowest chunk_num
    present; else the first object. Never raises on a missing property.
    Returns ``None`` for an empty list.
    """
    if not objects:
        return None

    def _cn(o):
        try:
            v = o.properties.get("chunk_num")
        except Exception:  # noqa: BLE001
            return None
        return v

    # chunk_num == 0 → canonical.
    for o in objects:
        if _cn(o) == 0:
            return o
    # legacy single-chunk (property absent/None).
    for o in objects:
        if _cn(o) is None:
            return o
    # else: lowest chunk_num present.
    try:
        return min(objects, key=lambda o: (_cn(o) if _cn(o) is not None else 1 << 30))
    except Exception:  # noqa: BLE001
        return objects[0]


def _dedup_objects_by_full_name(objects: list) -> list:
    """One object per ``full_name`` from a fetch that may match EVERY chunk of
    a chunked entity (F6, pre-gate audit, SEV-3).

    The ``callers`` / ``composed_by`` / ``type_users`` branches filter with
    ``contains_any`` on entity-level TEXT_ARRAY properties (``call_names`` /
    ``composes`` / ``type_uses``) that are replicated on EVERY chunk row of a
    chunked matcher — so a 3-chunk caller came back as 3 result rows. Group by
    ``full_name`` (first-seen order preserved) and keep the CANONICAL chunk
    per group via :func:`_pick_canonical_chunk` (chunk 0 > legacy no-chunk_num
    > lowest chunk_num). Objects with no ``full_name`` are kept as-is (keyed
    by object identity — never merged).
    """
    groups: dict = {}
    order: list = []
    for obj in objects:
        try:
            fn = (obj.properties or {}).get("full_name") or ""
        except Exception:  # noqa: BLE001 — defensive on mocked objects
            fn = ""
        key = fn if fn else ("__id__", id(obj))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(obj)
    return [_pick_canonical_chunk(groups[k]) for k in order]


# M-2 / CG-2 (v0.2.73): query types whose relationships are populated only
# by language-specific analyzer passes. Calls/paths/type_users come from the
# call-graph + type-annotation extraction that the Python walker emits richly
# but many non-Python walkers don't — so an empty result on a non-Python
# target is "unsupported for that language", not "entity missing". This
# marker lets the caller distinguish the two.
_CALLGRAPH_QUERY_TYPES = {"callers", "path", "type_users"}


def _code_structure_not_found_hint(query_type: str, target: str,
                                   effective_project) -> str:
    """Build an ACTIONABLE hint for a code-structure 'not found' result.

    M-2: the bare "Class 'X' not found" errors gave no next step. The two
    most common causes are (a) the slug-vs-prefix trap — the caller passed a
    project SLUG where the collection PREFIX is expected, or the wrong
    project — and (b) a stale / un-analyzed code graph. Name both.
    """
    proj_note = (
        f"searched project '{effective_project}'"
        if effective_project else "searched the workspace-default project"
    )
    return (
        f" ({proj_note}.) Next steps: (1) confirm the exact identifier with "
        f"search_code_graph('{target}') — full_names are module-stem-qualified "
        f"(e.g. 'module.Class', not a bare name or a file path); (2) if you "
        f"passed `project`, it must be the analyzer's project NAME, not a slug "
        f"or the Weaviate collection prefix (the slug-vs-prefix trap); (3) if "
        f"the entity truly exists in source, the code graph may be stale — "
        f"re-run `.claude/scripts/code-graph-analyze . --project <name>`."
    )


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

        # v0.2.46 V46-D: per-call truncation metadata. Branches that
        # cap their result list at a known limit set _truncation_meta
        # so the response can carry a `truncated: bool` + `limit: int`
        # signal. Branches that return a small fixed-size list (1-element
        # lookups, methods/extends/composes that read a single object's
        # field) leave it as None (never truncated).
        _truncation_meta: dict | None = None

        # Resolve project: explicit arg > env default > no filter
        # Pass project="" to explicitly search all projects regardless of env default
        effective_project = project if project is not None else (CODE_GRAPH_PROJECT or None)

        # Per-project collection name resolution (uses effective_project, not env)
        def _proj_coll(base: str) -> str:
            if effective_project:
                # v0.2.74 (BLOCKER-1): code-graph → underscore-PRESERVING.
                prefix = _code_sanitize_collection_prefix(effective_project)
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
                    return json.dumps({
                        "success": False,
                        "error": f"Module '{target}' not found"
                        + _code_structure_not_found_hint(query_type, target, effective_project),
                    }, indent=2)

                imports = response.objects[0].references.get("imports", [])
                results = [{"path": imp.properties.get("path"), "file_path": imp.properties.get("path", "")} for imp in imports]

            else:  # imports
                # v0.2.46 V46-D: emit truncation signal so the LLM
                # consumer knows when the list is capped at IMPORTS_LIMIT.
                IMPORTS_LIMIT = 20
                response = coll.query.fetch_objects(
                    filters=with_project(Filter.by_property("imports").contains_any([target])),
                    limit=IMPORTS_LIMIT
                )
                results = [{"path": obj.properties.get("path"), "file_path": obj.properties.get("path", "")} for obj in response.objects]
                _truncation_meta = {
                    "truncated": len(response.objects) >= IMPORTS_LIMIT,
                    "limit": IMPORTS_LIMIT,
                }

        elif query_type == "methods":
            # List methods in a class
            coll = client.collections.get(_proj_coll("CodeClass"))
            # v0.2.72 (P3): a chunked class is N objects sharing full_name;
            # fetch a few and pick the CANONICAL (chunk_num==0) one, which
            # carries the methods list + canonical file_path.
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=8
            )

            canonical = _pick_canonical_chunk(response.objects)
            if canonical is None:
                return json.dumps({"success": False, "error": f"Class '{target}' not found" + _code_structure_not_found_hint(query_type, target, effective_project)}, indent=2)

            class_file_path = canonical.properties.get("file_path") or canonical.properties.get("path", "")
            methods = canonical.properties.get("methods", [])
            results = [{"name": method, "file_path": class_file_path} for method in methods]

        elif query_type == "extends":
            # Find base classes
            coll = client.collections.get(_proj_coll("CodeClass"))
            # v0.2.72 (P3): pick the canonical chunk (chunk 0 holds the
            # `extends` references).
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=8,
                return_references=["extends"]
            )

            canonical = _pick_canonical_chunk(response.objects)
            if canonical is None:
                return json.dumps({"success": False, "error": f"Class '{target}' not found" + _code_structure_not_found_hint(query_type, target, effective_project)}, indent=2)

            extends = canonical.references.get("extends", [])
            results = [{
                "name": base.properties.get("name"),
                "full_name": base.properties.get("full_name"),
                "file_path": base.properties.get("file_path") or base.properties.get("path", ""),
            } for base in extends]

        elif query_type == "callers":
            # Find all functions that call the target function.
            # v0.2.46 V46-D: emit truncation signal.
            CALLERS_LIMIT = 50
            coll = client.collections.get(_proj_coll("CodeFunction"))
            # WS-4 Finding 1: call_names holds BARE leaf names; match the
            # full_name target AND its bare leaf so dotted / `::`-qualified
            # inputs (the documented form) actually resolve.
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("call_names").contains_any(_caller_match_terms(target))),
                limit=CALLERS_LIMIT
            )
            # F6: `call_names` is replicated on every chunk row of a chunked
            # caller — collapse to one row per full_name (canonical chunk).
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "signature": obj.properties.get("signature", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in _dedup_objects_by_full_name(response.objects)
            ]
            _truncation_meta = {
                "truncated": len(response.objects) >= CALLERS_LIMIT,
                "limit": CALLERS_LIMIT,
            }

        elif query_type == "interactions":
            # Find outbound cross-service interactions from a function or module.
            # 1. Try matching target as CodeFunction full_name first, then CodeModule path.
            # 2. Filter CodeInteraction by source_function/source_module reference.
            # v0.2.46 V46-D: emit truncation signal.
            INTERACTIONS_LIMIT = 50
            interactions_coll = client.collections.get(_proj_coll("CodeInteraction"))

            func_coll = client.collections.get(_proj_coll("CodeFunction"))
            # v0.2.72 (P3): interaction rows reference the CANONICAL (chunk 0)
            # function UUID (the analyzer captures chunk-0's UUID as func_uuid).
            # Resolve the target's canonical chunk so the source_function ref
            # filter matches.
            func_resp = func_coll.query.fetch_objects(
                filters=with_project(Filter.by_property("full_name").equal(target)),
                limit=8
            )

            canonical_func = _pick_canonical_chunk(func_resp.objects)
            if canonical_func is not None:
                source_uuid = str(canonical_func.uuid)
                ix_resp = interactions_coll.query.fetch_objects(
                    filters=Filter.by_ref("source_function").by_id().equal(source_uuid),
                    limit=INTERACTIONS_LIMIT
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
                        + _code_structure_not_found_hint(query_type, target, effective_project)
                    }, indent=2)
                source_uuid = str(mod_resp.objects[0].uuid)
                ix_resp = interactions_coll.query.fetch_objects(
                    filters=Filter.by_ref("source_module").by_id().equal(source_uuid),
                    limit=INTERACTIONS_LIMIT
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
            _truncation_meta = {
                "truncated": len(ix_resp.objects) >= INTERACTIONS_LIMIT,
                "limit": INTERACTIONS_LIMIT,
            }

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
                    + _code_structure_not_found_hint(query_type, source_name, effective_project)
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
                # v0.2.73 (RL follow-up): a successful path query with no
                # path IS a structural result — emit before the early return
                # so the "no path" outcome lands in the corpus too.
                _emit_code_structure_telemetry(
                    query_type=query_type, target=target, results=[],
                )
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
                return json.dumps({"success": False, "error": f"Class '{target}' not found" + _code_structure_not_found_hint(query_type, target, effective_project)}, indent=2)

            composes = response.objects[0].properties.get("composes", []) or []
            results = [{"composed_class": name} for name in composes]

        elif query_type == "composed_by":
            # Find classes that compose (contain as a field) the given class name.
            # v0.2.46 V46-D: emit truncation signal.
            COMPOSED_BY_LIMIT = 50
            coll = client.collections.get(_proj_coll("CodeClass"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("composes").contains_any([target])),
                limit=COMPOSED_BY_LIMIT
            )
            # F6: one row per full_name — `composes` is replicated per chunk.
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in _dedup_objects_by_full_name(response.objects)
            ]
            _truncation_meta = {
                "truncated": len(response.objects) >= COMPOSED_BY_LIMIT,
                "limit": COMPOSED_BY_LIMIT,
            }

        elif query_type == "type_users":
            # Find functions that reference a given type name in their annotations.
            # v0.2.46 V46-D: emit truncation signal.
            TYPE_USERS_LIMIT = 50
            coll = client.collections.get(_proj_coll("CodeFunction"))
            response = coll.query.fetch_objects(
                filters=with_project(Filter.by_property("type_uses").contains_any([target])),
                limit=TYPE_USERS_LIMIT
            )
            # F6: one row per full_name — `type_uses` is replicated per chunk.
            results = [
                {
                    "full_name": obj.properties.get("full_name", ""),
                    "signature": obj.properties.get("signature", ""),
                    "file_path": obj.properties.get("file_path") or obj.properties.get("path", ""),
                }
                for obj in _dedup_objects_by_full_name(response.objects)
            ]
            _truncation_meta = {
                "truncated": len(response.objects) >= TYPE_USERS_LIMIT,
                "limit": TYPE_USERS_LIMIT,
            }

        else:
            return json.dumps({
                "success": False,
                "error": f"Unknown query type: {query_type}. Supported: dependencies, imports, callers, methods, extends, interactions, path, composes, composed_by, type_users"
            }, indent=2)

        # v0.2.46 V46-D: include truncation metadata for top-N queries so
        # the LLM consumer can decide whether to re-query with a higher
        # limit. Branches that don't apply a cap leave _truncation_meta
        # as None — the field is then omitted to keep response sizes
        # tight for trivial queries.
        response_payload = {
            "success": True,
            "query_type": query_type,
            "target": target,
            "count": len(results),
            "results": results,
        }
        if _truncation_meta is not None:
            response_payload["truncated"] = _truncation_meta["truncated"]
            response_payload["limit"] = _truncation_meta["limit"]
        # CG-2 (v0.2.73): a call-graph query (callers/path/type_users) that
        # returns EMPTY is ambiguous — the target may genuinely have no
        # callers, OR its language's analyzer walker doesn't populate the
        # call graph / type-use edges (rich for Python, sparse/absent for
        # several other walkers). Surface the marker so the caller doesn't
        # read "0 callers" as "definitely nothing calls this".
        if query_type in _CALLGRAPH_QUERY_TYPES and not results:
            response_payload["unsupported_for_language"] = True
            response_payload["note"] = (
                f"0 results for a '{query_type}' query. This can mean the "
                f"target truly has none, OR the target's language does not "
                f"have call-graph / type-use extraction (this edge type is "
                f"populated richly for Python, sparsely or not at all for "
                f"some other languages). Confirm the entity exists with "
                f"search_code_graph('{target}') before concluding it has no "
                f"{query_type}."
            )

        # v0.2.73 (RL follow-up): uniform telemetry coverage — structural
        # lookups emit a retrieval event too (no rerank, no citation; see
        # the helper's docstring). Soft-fail, never blocks the response.
        _emit_code_structure_telemetry(
            query_type=query_type,
            target=target,
            results=results,
            truncated=(
                _truncation_meta["truncated"]
                if _truncation_meta is not None
                else None
            ),
        )
        return _large_result(response_payload)

    except WeaviateUnreachable as exc:
        # Loud-fail per 2026-05-08 silent-zero antipattern fix.
        _reset_weaviate_client_cache()
        return _weaviate_unreachable_response(exc, query=f"{query_type}:{target}")
    except WeaviateSchemaError as exc:
        # PR-41 Issue A: cache reset on schema-not-found.
        _reset_weaviate_client_cache()
        return _weaviate_schema_error_response(exc, query=f"{query_type}:{target}")
    except WeaviateAuthError as exc:
        return _weaviate_auth_error_response(exc, query=f"{query_type}:{target}")
    except Exception as e:
        # Loud-fail v2: classify query-time failures as unreachable.
        classified = _classify_weaviate_failure(e)
        if isinstance(classified, WeaviateUnreachable):
            _reset_weaviate_client_cache()
            return _weaviate_unreachable_response(classified, query=f"{query_type}:{target}")
        if isinstance(classified, WeaviateSchemaError):
            _reset_weaviate_client_cache()
            return _weaviate_schema_error_response(classified, query=f"{query_type}:{target}")
        if isinstance(classified, WeaviateAuthError):
            return _weaviate_auth_error_response(classified, query=f"{query_type}:{target}")
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
    # V52-AI (v0.2.52): exit cleanly if an orchestrator update is in
    # progress. Breaks the Windows MCP fork-bomb (~97 python +
    # ~77 node processes the user reported on 2026-06-09) by making
    # every respawn during the update window exit immediately.
    try:
        from _lib.update_gate import exit_if_update_in_progress  # type: ignore
    except ImportError:
        _parent_dir = str(Path(__file__).resolve().parent.parent)
        if _parent_dir not in sys.path:
            sys.path.insert(0, _parent_dir)
        try:
            from _lib.update_gate import exit_if_update_in_progress  # type: ignore
        except ImportError:
            exit_if_update_in_progress = None  # type: ignore
    if exit_if_update_in_progress is not None:
        exit_if_update_in_progress("weaviate-kg MCP")

    # v0.2.74 T5-1 ROOT FIX: reap any OTHER weaviate_mcp subprocess that is
    # provably stale — cross-workspace AND (spawned by our own parent = a
    # superseded sibling from a workspace switch, OR orphaned = its harness
    # died). A stale-handle client always points at a process its own parent
    # spawned, so this covers every real drift scenario without ever touching
    # ANOTHER session's live MCP (Fable-review F1: killing cross-project live
    # peers caused a mutual kill/respawn ping-pong). Best-effort / soft-fail —
    # a failed reap never blocks THIS process from starting; leftover peers
    # simply coexist. (The per-tool-call _assert_workspace_unchanged check is
    # NOT a runtime mitigation here — a stdio MCP's env never mutates, so it
    # only guards exotic in-process env changes; see its docstring.)
    try:
        from vco_lib.mcp_singleton import reap_stale_weaviate_mcp  # type: ignore
    except ImportError:
        _parent_dir = str(Path(__file__).resolve().parent.parent.parent)
        if _parent_dir not in sys.path:
            sys.path.insert(0, _parent_dir)
        try:
            from vco_lib.mcp_singleton import reap_stale_weaviate_mcp  # type: ignore
        except ImportError:
            reap_stale_weaviate_mcp = None  # type: ignore
    if reap_stale_weaviate_mcp is not None:
        try:
            _reaped = reap_stale_weaviate_mcp(_MODULE_LOAD_WORKSPACE)
            if _reaped:
                logger.info(
                    "weaviate-kg: reaped %d stale weaviate_mcp subprocess(es) "
                    "for a clean single-instance-per-workspace start (T5-1).",
                    _reaped,
                )
        except Exception as _reap_exc:  # noqa: BLE001 — never block startup
            logger.debug("weaviate-kg: stale-MCP reap raised (%s); continuing", _reap_exc)

    logger.info(f"Starting Claude Orchestrator Weaviate MCP Server")
    logger.info(f"Primary Collection: {KG_COLLECTION}")
    read_state = "DISABLED" if SHARED_KG_READ_DISABLED else "enabled"
    logger.info(f"Shared Collection: {SHARED_KG_COLLECTION if SHARED_KG_COLLECTION else 'None'} (read: {read_state})")
    if SHARED_KG_READ_DISABLED:
        logger.info("Shared Collection reads: DISABLED (SHARED_KG_READ_DISABLED=true)")
    if SHARED_KG_WRITE_DISABLED:
        logger.info("Shared Collection writes: DISABLED (SHARED_KG_WRITE_DISABLED=true)")
    else:
        logger.info("Shared Collection writes: enabled")
    logger.info(f"Dual Embedding: {DUAL_EMBEDDING_ENABLED} (active: {ACTIVE_EMBEDDING})")
    logger.info(f"Weaviate: {WEAVIATE_URL}")
    logger.info(f"Code Graph Project: {CODE_GRAPH_PROJECT if CODE_GRAPH_PROJECT else '(all projects)'}")
    logger.info(f"Code Graph Collections: {_code_collection('Code*')}")

    # Run server with stdio transport
    asyncio.run(mcp.run_stdio_async())
