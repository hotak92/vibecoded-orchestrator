#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Query Code Graph

Command-line interface for querying the code graph with semantic and structural search.

Usage:
    # Semantic search
    python query_code_graph.py search "authentication middleware"
    python query_code_graph.py search "file upload" --collection CodeFunction --limit 3
    python query_code_graph.py search "HTTP calls to external API" --collection CodeInteraction

    # Find similar code
    python query_code_graph.py similar "api.auth.validate_token" --limit 5

    # Structural queries
    python query_code_graph.py structure dependencies "api/routes.py"
    python query_code_graph.py structure callers "utils.validate_input"
    python query_code_graph.py structure methods "api.UserManager"
    python query_code_graph.py structure extends "api.BaseHandler"
    python query_code_graph.py structure interactions "api/routes.py"       # all outbound calls from a module
    python query_code_graph.py structure interactions "api.users.create_user"  # calls from a function
"""

import argparse
import json
import os
import re
import sys
import requests
from pathlib import Path
from typing import Optional, List

try:
    import weaviate
    from weaviate.classes.query import Filter, MetadataQuery, QueryReference
except ImportError:
    print("Error: weaviate-client not installed. Install with: pip install weaviate-client", file=sys.stderr)
    sys.exit(1)


# VCO-SHARED-BEGIN: _resolve_orchestrator_root (verbatim across templates/scripts/*.py)
def _resolve_orchestrator_root() -> "Path | None":
    """Return the orchestrator clone root — the directory that CONTAINS
    ``claude_mcp_servers/`` — or ``None`` when it cannot be located.

    THE one shape for this question across ``templates/scripts/*.py``. It is
    copied VERBATIM into every shipped script that asks it and pinned
    byte-identical by
    ``tests/test_v0292_cli_root_resolution_and_prefix.py::test_root_resolver_bodies_identical``
    — a DOCUMENTED class-C mirror with an enforcing test rather than a silent
    copy, because these scripts must answer "where is the orchestrator?"
    BEFORE they can import anything from it (importing ``vco_lib`` to find
    ``vco_lib`` is circular).

    Candidate order — the first candidate that actually CONTAINS
    ``claude_mcp_servers/`` wins; a candidate that does not is SKIPPED, never
    returned:

      1. ``$VCT_ORCHESTRATOR_ROOT`` — canonical; written into ``.claude/env``
         and ``.claude/settings.json`` by the bundle installer
         (``vco_lib/config_projection.py``).
      2. ``$VCT_INSTALL_ROOT`` — legacy alias carrying the same value; some
         launcher subprocess spawns set only this one.
      3. ``<script>/../..`` — the in-tree layout, correct ONLY when the script
         sits in the orchestrator clone's own ``.claude/scripts/`` (or in
         ``templates/scripts/`` in the clone). On an INSTALLED project this
         resolves to the USER project root, which has no
         ``claude_mcp_servers/`` — which is exactly why every rung is
         validated and why this rung is LAST.

    Never raises. Path joins go through ``pathlib`` so no separator is
    assumed (a Windows ``\\``-separator bug shipped once already, v0.2.81).
    """
    for _candidate in (
        os.environ.get("VCT_ORCHESTRATOR_ROOT", "").strip(),
        os.environ.get("VCT_INSTALL_ROOT", "").strip(),
        str(Path(__file__).resolve().parent.parent.parent),
    ):
        if not _candidate:
            continue
        try:
            _root = Path(_candidate)
            if (_root / "claude_mcp_servers").is_dir():
                return _root
        except (OSError, ValueError):
            continue
    return None
# VCO-SHARED-END: _resolve_orchestrator_root


# Import the shared rank-tier formatter from the MCP server module so the
# CLI emits results identically to `search_code_graph` MCP. The script
# lives at .claude/scripts/query_code_graph.py and the MCP module at
# <orchestrator>/claude_mcp_servers/weaviate_mcp/server.py.
#
# v0.2.37 (Gap 6c): when this script ships into a 3rd-party project via
# install-bundle, the script-relative guess resolves to the USER PROJECT
# root, which has no `claude_mcp_servers/` directory. v0.2.92: the env-arm
# ladder that fixed that is no longer written out here — it is
# `_resolve_orchestrator_root()` above, the ONE shape every script in this
# directory uses (three copies lived in THIS FILE alone before v0.2.92).
# A1 (v0.2.38): weaviate_mcp is pip-installed as an editable package by
# install.py, so `from weaviate_mcp.server import ...` works without a
# sys.path entry.  _MCP_SERVERS_PATH survives ONLY to name a concrete path in
# the two ImportError messages below (the P1-D block derives its own
# `scripts/` directory from `_ORCHESTRATOR_ROOT`).
#
# _PROJECT_ROOT is the USER PROJECT root (this script's `.claude/scripts/`
# grandparent) and answers a DIFFERENT question than
# `_resolve_orchestrator_root()` — do not collapse the two.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR_ROOT: Optional[Path] = _resolve_orchestrator_root()
# When the root cannot be located, keep the script-relative guess so the
# ImportError message below still names a concrete path.
_MCP_SERVERS_PATH: Optional[Path] = (
    _ORCHESTRATOR_ROOT or _PROJECT_ROOT
) / "claude_mcp_servers"

# v0.2.72 (P1/P2/P4 CLI parity): besides the rank/tier FORMATTERS, import the
# shared pipeline ADAPTER factories (`make_code_collapse_fn` / `make_code_tier_fn`)
# from the SERVER module. Both this CLI and `search_code_graph` (MCP) build their
# `run_code_retrieval_pipeline` collapse_fn/tier_fn from THESE factories, so the
# two surfaces cannot diverge on collapse/tier behaviour (the hard invariant).
# Do NOT reimplement them per-surface.
try:
    from weaviate_mcp.server import (
        _caller_match_terms,
        # v0.2.92 (B1): the ONE code-graph prefix rule, shared with the MCP.
        # The underscore-PRESERVING `canonical_class_prefix` the ANALYZER
        # writes `<prefix>_Code*` with, with the MCP's runtime posture for a
        # pathological project name (ValueError -> the "vct" sentinel from the
        # dropping rule) so a bad `--project` degrades instead of crashing.
        # This CLI used to compute the prefix a SECOND way with a private
        # regex, which turned every space in a project name into `_`:
        # 'VibeCoded Orchestrator' -> 'VibeCoded_Orchestrator_CodeFunction',
        # a class that does not exist, read silently as zero results.
        _code_sanitize_collection_prefix,
        _dedup_objects_by_full_name,
        _format_code_result_by_rank,
        _format_code_result_by_tier,
        _format_code_result_ref,
        _self_project_chunk_fetcher,
        make_code_collapse_fn,
        make_code_tier_fn,
    )
    # NOT imported: CODE_SIBLINGS_RANK_1 / _2. The sibling budget is decided by
    # the SHARED formatter (`_format_code_result_by_rank`, server.py) which
    # computes `max_total` and passes it INTO the fetcher this CLI supplies.
    # Importing them here would be a second, unenforced copy of a decision the
    # shared code already owns — v0.2.92 removed them as dead.
except ImportError as exc:  # pragma: no cover — surface a clear error
    print(
        f"Error: could not import weaviate_mcp.server rank-tier helper: {exc}\n"
        f"  Expected module at: {_MCP_SERVERS_PATH}/weaviate_mcp/server.py\n"
        "  Ensure install.py has run (pip install -e claude_mcp_servers/) or set\n"
        "  VCT_ORCHESTRATOR_ROOT to the orchestrator clone root.",
        file=sys.stderr,
    )
    sys.exit(1)

# v0.2.92 — the ONE home for reading a Weaviate cross-reference off a fetched
# object. `structure dependencies` / `structure extends` below resolve one, and
# `weaviate_mcp.server.query_code_structure` + the analyzer's
# `create_cross_references` write pass resolve the same shapes; a third inline
# copy here is what CLAUDE.md's modularity rule forbids.
#
# Import discipline: this is NOT a new dependency. The `from
# weaviate_mcp.server import ...` above is already a HARD requirement of this
# script (it exits 1 on failure), and that module imports `vco_lib.log_setup`
# at module scope — so vco_lib is importable wherever this CLI can run at all.
# A failure here therefore means a BROKEN install and must surface loudly
# (traceback + non-zero exit), never degrade to a silent inline copy.
from vco_lib.codegraph_references import (  # noqa: E402 — must follow the
    # weaviate_mcp import above, which is what guarantees vco_lib is importable
    dedup_ref_targets,
    normalize_reference_targets,
    read_cross_reference,
)

# P1-D (2026-05-08): centralized access-matrix helper. `kg_access` lives at
# <orchestrator>/claude_mcp_servers/scripts/kg_access.py; the editable install
# of `weaviate_mcp` only puts <orchestrator>/claude_mcp_servers on sys.path, so
# the directory has to be added explicitly.
#
# v0.2.92 (B2): this site used to hardcode the SCRIPT-RELATIVE guess with no
# env arm at all. On an installed project that guess is the USER PROJECT root,
# which has no `claude_mcp_servers/scripts` — so the import ALWAYS failed there
# and the self-only fallback below silently dropped the launcher's
# cross-project code-graph grants (`VCT_CODE_GRAPH_ACCESS_LIST`). Routed
# through `_resolve_orchestrator_root()` (env arms first, validated in-tree
# last) it resolves on installed projects and on the orchestrator clone alike.
#
# The try/except is RETAINED: the helper is genuinely optional for a project
# whose orchestrator clone is not reachable. What the fallback loses is the
# ACCESS MATRIX (peers), never the prefix rule — the prefix comes from the
# same shared home the MCP uses (imported above).
try:
    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    # v0.2.92 (R4/R21) — INSTALL-TIME BAKED ROOT. `vco_lib/rewire.py`
    # substitutes the placeholder below when this file is installed into a
    # project, so an installed copy still finds `kg_access` (and therefore the
    # launcher's cross-project code-graph grants) with NOTHING in the
    # environment. In the clone the placeholder stays literal,
    # `Path("{{ORCHESTRATOR_ROOT}}")/"vco_lib"` is not a directory, and this
    # block is inert — the validation IS the placeholder guard. It is used
    # ONLY when NEITHER env pin ($VCT_ORCHESTRATOR_ROOT, $VCT_INSTALL_ROOT)
    # names a real orchestrator root — a VALID pin always wins, a provably
    # stale one is healed.
    #
    # `_ORCHESTRATOR_ROOT` was resolved near the top of the module, BEFORE
    # this region runs, so re-ask the ONE resolver once the env arm can
    # answer — do not re-implement the ladder here. Only `_MCP_SERVERS_PATH`
    # consumed the earlier value, and only to name a path inside an
    # ImportError message.
    _VCO_BAKED_ORCHESTRATOR_ROOT = "{{ORCHESTRATOR_ROOT}}"
    _vco_env_pins = [os.environ.get(_k, "").strip()
                     for _k in ("VCT_ORCHESTRATOR_ROOT", "VCT_INSTALL_ROOT")]
    if (Path(_VCO_BAKED_ORCHESTRATOR_ROOT) / "vco_lib").is_dir() and not any(
        _p and (Path(_p) / "vco_lib").is_dir() for _p in _vco_env_pins
    ):
        os.environ["VCT_ORCHESTRATOR_ROOT"] = _VCO_BAKED_ORCHESTRATOR_ROOT
        _ORCHESTRATOR_ROOT = _resolve_orchestrator_root() or _ORCHESTRATOR_ROOT
    _kg_access_dir = (
        (_ORCHESTRATOR_ROOT or _PROJECT_ROOT) / "claude_mcp_servers" / "scripts"
    )
    # APPEND, not insert(0): nothing else on the path provides `kg_access`, and
    # this directory holds a dozen loose script modules — putting it first
    # would let any of them shadow a same-named import for the whole process.
    # (Same reasoning, same words, as search_knowledge.py's P1-D block.)
    if _kg_access_dir.is_dir() and str(_kg_access_dir) not in sys.path:
        sys.path.append(str(_kg_access_dir))
    # VCO-REWIRE-END: orchestrator-root-resolution
    from kg_access import code_graph_collections_to_query as _code_graph_collections_to_query  # type: ignore[import-not-found]
except Exception:
    def _code_graph_collections_to_query(  # type: ignore[no-redef]
        self_project: str,
        bases=None,
    ):
        bases_t = tuple(bases) if bases is not None else (
            "CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction",
        )
        if not self_project:
            return [(b, "") for b in bases_t]
        # Self-only fan-out (no access matrix). The PREFIX still comes from the
        # one shared home — no inline copy of the rule lives here.
        prefix = _code_sanitize_collection_prefix(self_project)
        return [(f"{prefix}_{b}", self_project) for b in bases_t]

# Load MCP config.
#
# Routed through `vco_lib.paths.claude_user_dir` (v0.2.92 W-CLAUDE) instead of
# reconstructing `Path.home() / ".claude"` inline, so `$VCT_CLAUDE_DIR` steers
# it. Why that matters here even though this is a READ: this block runs at
# MODULE IMPORT and, when the file exists, replaces the WEAVIATE_URL /
# GRPC_PORT / OLLAMA_URL defaults with whatever the developer's machine has.
# 11 tests across 6 files import this module, so on a box that happens to have
# `~/.claude/workflow/config/mcp-config.json` they exercised the machine's
# values while CI (no such file) exercised the defaults — a test that means
# something different per machine. The import is hard for the same reason the
# `vco_lib.codegraph_references` import above is: this script already requires
# `weaviate_mcp.server`, which imports vco_lib at module scope.
from vco_lib.paths import claude_user_dir  # noqa: E402 — see import discipline above

CONFIG_PATH = claude_user_dir() / "workflow" / "config" / "mcp-config.json"

if CONFIG_PATH.exists():
    config = json.loads(CONFIG_PATH.read_text())
    WEAVIATE_URL = config["weaviate"]["url"]
    GRPC_PORT = config["weaviate"]["grpc_port"]
    OLLAMA_URL = config.get("ollama", {}).get("url", "http://localhost:11435")
else:
    WEAVIATE_URL = "http://localhost:8081"
    GRPC_PORT = 50052
    OLLAMA_URL = "http://localhost:11435"


def _collection_name(base: str, project: str = None) -> str:
    """Return the per-project code-graph collection name for ``base``.

    Routed through ``weaviate_mcp.server._code_sanitize_collection_prefix`` —
    the ONE home for the code-graph prefix rule, shared with the MCP and
    delegating to ``vco_lib.codegraph_naming.canonical_class_prefix``, which
    is what the ANALYZER (``templates/scripts/analyze_code_graph.py``) writes
    ``<prefix>_Code*`` classes with.

    v0.2.92 (B1) — this used to call a PRIVATE `_sanitize_collection_prefix`
    defined in this file: ``re.sub(r'[^a-zA-Z0-9_]', '_', name)`` + upper-first.
    That rule maps WHITESPACE to ``_`` where the canonical rule drops it and
    capitalises the next word, so every project name containing a space
    resolved to a class that has never existed:

        'VibeCoded Orchestrator' -> 'VibeCoded_Orchestrator_CodeFunction'  (0 live classes)
        canonical/analyzer       -> 'VibeCodedOrchestrator_CodeFunction'   (the real one)

    The failure was SILENT — an absent class yields no results, not an error —
    and it drove every CLI mode (search, similar, all `structure` modes).
    The two rules that legitimately coexist in this repo are the
    underscore-DROPPING KG rule and the underscore-PRESERVING code rule; this
    is the CODE family, so it takes the preserving one. Do not "unify" them.
    """
    if not project:
        return base
    return f"{_code_sanitize_collection_prefix(project)}_{base}"


# Code embedding configuration — v0.2.18: centralised via
# EmbeddingService. Pre-v0.2.18 read CODE_EMBED_BACKEND / CODE_EMBED_-
# SERVICE_URL / CODE_EMBED_MODEL directly and hardcoded the slot.
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# Import EmbeddingService — graceful fallback for half-installed venvs.
#
# v0.2.92: the `sys.path.insert(0, _PROJECT_ROOT)` that used to guard this
# import is GONE, for two reasons.
#   * It was DEAD. `from vco_lib.codegraph_references import ...` above is
#     unguarded and runs first, so if `vco_lib` were not already importable
#     this module would have died before reaching here. The insert could
#     never be what made the import below work.
#   * It was HARMFUL. Inserting a whole orchestrator root at sys.path[0]
#     re-points `claude_mcp_servers` (and anything else that root ships) for
#     the ENTIRE process, so merely importing this CLI changed which clone a
#     later `import claude_mcp_servers...` resolved to. On a machine with two
#     clones that is a silent cross-repo import.
# The try/except stays: `vco_lib.embedding_service` has optional deps of its
# own, and --detail must degrade rather than crash when they are missing.
try:
    from vco_lib.embedding_service import (
        EmbeddingService,
        NoEmbeddingBackendError,
    )
    HAS_EMBEDDING_SERVICE = True
except Exception:
    HAS_EMBEDDING_SERVICE = False
    EmbeddingService = None  # type: ignore[assignment]
    NoEmbeddingBackendError = Exception  # type: ignore[assignment]


_cached_embedding_service: "EmbeddingService | None" = None


def _get_or_create_embedding_service():
    """Lazy-construct EmbeddingService, cached for the CLI's lifetime."""
    global _cached_embedding_service
    if _cached_embedding_service is not None:
        return _cached_embedding_service
    if not HAS_EMBEDDING_SERVICE:
        return None
    try:
        _cached_embedding_service = EmbeddingService.for_project()
        return _cached_embedding_service
    except Exception as e:
        print(f"⚠️  EmbeddingService construction failed: {e}", file=sys.stderr)
        return None


def _active_code_vector_slot() -> str:
    """Return the active code-vector slot. Falls back to codesage_embed
    when EmbeddingService isn't available (pre-v0.2.18 default)."""
    svc = _get_or_create_embedding_service()
    if svc is None:
        return "codesage_embed"
    return svc.code_vector_slot


# v0.2.72 T-FLOOR (P1) + integration: the two-stage per-slot floor table,
# resolvers AND the retrieval pipeline live in the SINGLE SHARED home
# ``weaviate_mcp.code_ranking`` so the CLI path and the MCP path
# (server.py::search_code_graph) cannot diverge. This CLI already imports shared
# helpers from weaviate_mcp via the pip-editable install (see the
# ``from weaviate_mcp.server import ...`` block above) — mirror that style here.
#
# ``CODE_FLOOR_BY_SLOT`` (measured CodeSage 0.16/0.22, jina 0.16/0.22, qwen3
# conservative 0.20/0.30) is the (retrieval_floor, post_rerank_floor) contract;
# ``resolve_retrieval_floor`` / ``resolve_post_rerank_floor`` own the env-
# override + empty-string-coercion discipline.
#
# MUST MATCH (3-way mirror): the floor VALUES in code_ranking.py are the
# contract between this CLI, the MCP server
# (claude_mcp_servers/weaviate_mcp/server.py::search_code_graph), and any hook
# that pre-filters code-graph results. v0.2.72 moved the table to the shared
# module so the three surfaces cannot drift; changing a value re-opens the
# cross-scale-floor bug unless every surface + the experiment re-run agree.
#
# Hard-required (no fallback reimplementation): reimplementing the pipeline or
# the floor table per-surface is exactly the divergence this module exists to
# prevent. If `weaviate_mcp.server` imported above, `weaviate_mcp.code_ranking`
# is importable too (server.py imports it at module scope), so this branch can
# only fail alongside the server import — which already sys.exit(1)s with the
# remediation message.
try:
    from weaviate_mcp.code_ranking import (
        CODE_FLOOR_BY_SLOT,  # noqa: F401 — pinned re-export, see below
        resolve_post_rerank_floor,
        resolve_retrieval_floor,
        run_code_retrieval_pipeline,
    )
except ImportError as exc:  # pragma: no cover — surface a clear error
    print(
        f"Error: could not import weaviate_mcp.code_ranking pipeline: {exc}\n"
        f"  Expected module at: {_MCP_SERVERS_PATH}/weaviate_mcp/code_ranking.py\n"
        "  Ensure install.py has run (pip install -e claude_mcp_servers/) or set\n"
        "  VCT_ORCHESTRATOR_ROOT to the orchestrator clone root.",
        file=sys.stderr,
    )
    sys.exit(1)


def generate_code_embedding(text: str) -> Optional[List[float]]:
    """Generate code embedding via EmbeddingService.

    v0.2.18: routes through EmbeddingService.embed_code (which picks
    CodeEmbed / Ollama / OpenAI from env). Falls back to direct
    CodeEmbed-service HTTP call when the service isn't available.
    """
    svc = _get_or_create_embedding_service()
    if svc is not None:
        try:
            return svc.embed_code(text)
        except Exception as e:
            print(f"⚠️  EmbeddingService.embed_code failed: {e}", file=sys.stderr)
    # Legacy fallback: direct CodeEmbed HTTP call.
    try:
        response = requests.post(
            f"{CODE_EMBED_SERVICE_URL}/api/embeddings",
            json={"model": "", "prompt": text},
            timeout=60,
        )
        if response.status_code == 200:
            return response.json()["embedding"]
        print(f"❌ Embedding generation failed: HTTP {response.status_code}")
        return None
    except Exception as e:
        print(f"❌ Embedding error: {e}")
        return None


class CodeGraphQuery:
    """Query interface for code graph."""

    def __init__(self, project: Optional[str] = None):
        self.project = project
        self.client = None

    def _coll(self, base: str) -> str:
        """Get per-project collection name."""
        return _collection_name(base, self.project)

    def connect(self):
        """Connect to Weaviate."""
        try:
            self.client = weaviate.connect_to_custom(
                http_host='localhost',
                http_port=8081,
                http_secure=False,
                grpc_host='localhost',
                grpc_port=50052,
                grpc_secure=False
            )
            return True
        except Exception as e:
            print(f"❌ Failed to connect to Weaviate: {e}", file=sys.stderr)
            return False

    def _resolve_anchor_props(self, anchor: str) -> Optional[dict]:
        """Resolve an ``--anchor`` value (edited file path OR symbol full_name)
        to the anchor entity's Weaviate props.

        The anchor is the edit/grep seed the hook path passes so the shared
        pipeline's relationship rerank (call-linked / same-module /
        shared-type — code_ranking.rerank_score) fires relative to it. Queries
        CodeFunction + CodeClass in the SELF project only (peers are search
        targets, not anchors), attempting in priority order:
          1. full_name == anchor          (exact symbol)
          2. file_path == anchor          (exact path, as passed)
          3. file_path LIKE *<tail>       (path-shaped anchor — an absolute
             editor path still hits the analyzer's repo-relative file_path)
          4. full_name LIKE *.<anchor>    (bare symbol — qualified leaf)
        Among matches, the lowest chunk_num wins (entity-level props like
        call_names / type_uses are replicated per chunk row).

        Failure-soft by contract: empty anchor / no client / no match / any
        Weaviate error → None (pure semantic ordering, byte-identical to a
        direct MCP call). Never raises into the search path.
        """
        if not anchor or self.client is None:
            return None
        try:
            anchor = str(anchor).strip()
            if not anchor:
                return None
            attempts = [
                Filter.by_property("full_name").equal(anchor),
                Filter.by_property("file_path").equal(anchor),
            ]
            norm = anchor.replace("\\", "/")
            if "/" in norm:
                parts = [p for p in norm.split("/") if p]
                tail = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "")
                if tail:
                    attempts.append(Filter.by_property("file_path").like(f"*{tail}"))
            elif all(ch.isalnum() or ch in "_.:" for ch in anchor):
                # Bare / qualified symbol: also match full_names ending in the
                # anchor's LEAF (e.g. anchor "validate_token" hits
                # "api.auth.validate_token"; Rust "mod::my_fn" leaf-matches on
                # "my_fn" — split on `.` OR `::`, F10 pre-gate audit).
                #
                # F10 tightening: SKIP the bare-leaf LIKE fallback for
                # over-generic leaves (len < 4 or a ubiquitous name like
                # "run"/"main") — `LIKE *.run` matches half the codebase and
                # anchors the rerank to an arbitrary entity. All failure
                # paths still resolve to None (pure semantic ordering).
                leaf = re.split(r"::|\.", anchor)[-1]
                _generic = {"run", "main", "init", "new", "get", "set"}
                if len(leaf) >= 4 and leaf.lower() not in _generic:
                    attempts.append(Filter.by_property("full_name").like(f"*.{leaf}"))
                    if "::" in anchor:
                        # Rust-style storage may qualify with `::` too.
                        attempts.append(Filter.by_property("full_name").like(f"*::{leaf}"))

            for flt in attempts:
                best: Optional[dict] = None
                best_chunk = 0
                for base in ("CodeFunction", "CodeClass"):
                    try:
                        coll_obj = self.client.collections.get(self._coll(base))
                        q = flt
                        if self.project:
                            q = flt & Filter.by_property("project").equal(self.project)
                        resp = coll_obj.query.fetch_objects(filters=q, limit=4)
                    except Exception:
                        continue
                    for obj in resp.objects:
                        p = obj.properties or {}
                        try:
                            cn = int(p.get("chunk_num") or 0)
                        except (TypeError, ValueError):
                            cn = 0
                        if best is None or cn < best_chunk:
                            best, best_chunk = p, cn
                if best is not None:
                    return best
            return None
        except Exception:
            return None

    def search_by_concept(self, query: str, collection: str = "CodeFunction", limit: int = 5, detail: str = "auto", hook_format: bool = False, anchor: str = None, exclude_file: str = None, transcript: str = None):
        """Semantic search for code by concept.

        P1-D (2026-05-08): when ``self.project`` is set, fan out across
        self + every peer in ``VCT_CODE_GRAPH_ACCESS_LIST``. Each
        per-collection query over-fetches ``2*limit`` candidates (the
        shared-pipeline pool, matching the MCP); results are merged and
        ranked by the shared pipeline before truncation. When
        ``self.project`` is unset (cross-tenant "search all projects"
        path), behaviour is unchanged: a single bare-collection query.

        v0.2.72 (P1/P2/P4 CLI parity): ranking runs through the SHARED
        ``run_code_retrieval_pipeline`` (weaviate_mcp.code_ranking) with the
        SAME adapter factories (`make_code_collapse_fn` / `make_code_tier_fn`
        from weaviate_mcp.server) and the SAME per-slot floor resolution the
        MCP uses — two-stage floor + relationship rerank + multi-chunk
        collapse + score-tier allocation. Rendering matches the MCP loop:
        pipeline-tiered candidates go through `_format_code_result_by_tier`
        (1/3/7-chunk assembly); explicit detail goes through the rank-based
        `_format_code_result_by_rank`.

        detail:
          "auto"   — score-tier verbosity from the shared pipeline's tier_fn
                     (summary / single_chunk / three_chunks / full per score)
          "titles" — metadata refs for every result
          "full"   — full untruncated for every result

        anchor: edited file path OR symbol full_name (the hook path passes
          this). Resolved to the anchor entity's Weaviate props and passed as
          ``anchor_props`` so the pipeline's relationship boost (call-linked /
          same-module / shared-type — code_ranking.rerank_score) fires
          relative to it. None / unresolvable → anchor_props=None → pure
          semantic ordering, byte-identical to a direct MCP call. That is
          data, not behaviour — the pipeline code is identical either way.

        exclude_file (B2, design audit): drop candidates whose source file IS
          this path BEFORE the pipeline trims to `limit`. The Read/Edit hook
          passes the edited file as BOTH anchor and exclude — the anchor's
          +0.03 same-file boost used to promote the edited file's own entities
          into the top-2, and the hook's old line-wise `grep -v` then stripped
          only their header lines, leaving orphaned body lines. Filtering here
          (pre-trim, on the normalized candidates — the pipeline itself stays
          pure) keeps the trimmed top-K full of OTHER files' context. The MCP
          body has no exclude concept (no edited-file context on a direct
          tool call) — documented there as deliberately N/A, not drift.

        hook_format: when True, emit a stable per-result header line
          'CODE: <full_name> | <collection> | distance=<d>' (with optional
          ' | source=<peer>' suffix for cross-tenant fan-out) and skip the
          human banner. The pre-edit hook uses this header to dedup repeat
          injections by entity name across a session via the regex
          ``^(KG|CODE):\\ (.+)$``. Body lines follow as ordinary indented
          content; blank lines separate blocks.

        transcript (WP-E, v0.2.92): optional path to the live JSONL
          transcript. Only the EMBEDDED vector is affected — the raw
          ``query`` string keeps flowing to the banner, the no-results line,
          and the retrieval telemetry unchanged (never persist enriched or
          thinking text). See ``vco_lib.query_enrichment.build_query`` for
          the shared enrich-or-cap decision (one home, also used by the KG
          hook leg in ``rl_kg_search.py``).
        """
        # WP-E query-budget SSOT: enrich a SHORT query by walking backwards
        # through the agent's own prior output (never tool payloads), or cap
        # an OVERSIZED one via the EXISTING query_chunking machinery — never
        # both (build_query's own budget gate picks one). The embedding-model
        # list is THIS CLI's own resolved code model — never invented — so
        # the budget matches the vector actually generated below.
        effective_query = query
        try:
            _svc = _get_or_create_embedding_service()
            if _svc is not None:
                _code_model = _svc.code_model_id
            else:
                # No service: lean install, or no embedding backend reachable
                # (the case NoEmbeddingBackendError names). Fall back to the
                # SHARED default so the budget still matches the model the
                # embed path will use — never a literal invented here.
                from vco_lib.embedding_service import (
                    DEFAULT_CODE_MODEL as _code_model,
                )
            from vco_lib.query_enrichment import build_query as _build_query

            effective_query = _build_query(
                query, transcript_path=transcript, embedding_models=[_code_model]
            ).text

            from claude_mcp_servers.rl_client import query_chunking as _qc

            if _qc.is_oversized(effective_query, _code_model):
                # Oversized even unenriched (or enrichment made no
                # difference — build_query never enriches when there is no
                # budget room): cap via the shared chunker's FIRST chunk.
                # This CLI has no multi-chunk-retrieve/union wiring for code
                # graph — that path (query_chunking.combine_codegraph_results)
                # is deliberately reserved scaffolding until a real
                # oversized-codegraph-query surface needs it; a single-chunk
                # cap mirrors search_knowledge.py's existing truncation shape
                # instead of half-wiring the reserved union path.
                _chunks = _qc.chunk_query(effective_query, _code_model)
                if _chunks:
                    effective_query = _chunks[0]
                    print(
                        f"⚠️  Query truncated to fit {_code_model}'s budget",
                        file=sys.stderr,
                    )
        except Exception as _exc:  # noqa: BLE001 — enrichment is best-effort
            print(f"⚠️  Query enrichment skipped: {_exc}", file=sys.stderr)
            effective_query = query

        try:
            # Generate query embedding
            query_embedding = generate_code_embedding(effective_query)
            if not query_embedding:
                print("❌ Failed to generate query embedding")
                return

            # Build per-(collection_name, project_filter_value) fan-out
            # list. Self comes first; peers appear in
            # VCT_CODE_GRAPH_ACCESS_LIST order. The helper handles
            # dedupe + the cross-tenant fallback.
            pairs = _code_graph_collections_to_query(
                self_project=self.project or "",
                bases=(collection,),
            )

            # Header emit MOVED to AFTER `top` is built — see below.
            # v0.2.21 (audit fix, 2026-05-20): emitting the banner BEFORE
            # the query means a zero-result run still prints the
            # "🔍 Semantic search... Project filter: X" header, which the
            # pre-edit hook captures into $CODE_RESULT and treats as a
            # non-empty result (HAS_CODE=1), bypassing its own 0-result
            # short-circuit and injecting useless context every Edit.
            # See docs/HOOK_TOKEN_AUDIT_2026-05-20.md §3 and the
            # query_code_graph.py side of the fix.

            # Fan-out: query each (collection, filter) pair, merge into one
            # candidate pool. v0.2.72 (P1/P2): over-fetch 2N per collection so
            # the shared `run_code_retrieval_pipeline` has a pool to floor-cull
            # + rerank + collapse before trimming to `limit`. Matches the MCP
            # over-fetch (server.py::search_code_graph).
            _fetch_limit = max(1, 2 * limit)
            merged: list[tuple[float, str, object]] = []  # (distance, source_label, obj)
            for coll_name, project_filter in pairs:
                try:
                    coll = self.client.collections.get(coll_name)
                except Exception:
                    # Peer never indexed this base — skip silently.
                    continue
                # R2-6: request the stored node vector for the active code slot
                # (include_vector). This lets the retrieval-telemetry emitter's
                # fast-path carry each node's ``n_emb`` for FREE — so the
                # code_hook/code_cli events are trainable WITHOUT the emitter
                # having to re-embed each vector-less node one-by-one on this
                # synchronous hook path (the "lock on retrieval" hazard). The
                # emitter's re-embed recovery is now the rare residual it was
                # designed to be (cold-fetch races), bounded by a per-emit budget.
                _code_slot = _active_code_vector_slot()
                nv_kwargs = dict(
                    near_vector=query_embedding,
                    limit=_fetch_limit,
                    return_metadata=MetadataQuery(distance=True),
                    target_vector=_code_slot,
                    include_vector=[_code_slot],
                )
                if project_filter:
                    nv_kwargs["filters"] = Filter.by_property("project").equal(project_filter)
                try:
                    response = coll.query.near_vector(**nv_kwargs)
                except Exception:
                    # Collection exists but query failed (e.g. wrong
                    # vector dim from a stale schema). Skip and keep
                    # going so peer issues don't break self queries.
                    continue
                for obj in response.objects:
                    distance = obj.metadata.distance if obj.metadata.distance is not None else 1.0
                    merged.append((distance, project_filter or "", obj))

            # v0.2.72 (P1/P2/P3/P4 CLI parity): normalise the fan-out into the
            # shared candidate shape and run the SAME
            # `run_code_retrieval_pipeline` the MCP calls
            # (server.py::search_code_graph) with the SAME adapter factories —
            # two-stage per-slot floor (retrieval 0.16 / post-rerank 0.22 for
            # CodeSage) + relationship rerank + multi-chunk collapse +
            # score-tier allocation — then trim to `limit`. This replaced the
            # v0.2.21/v0.2.70 single-stage floor break-loop; floor history
            # lives in weaviate_mcp/code_ranking.py. The ONLY per-surface
            # difference is `anchor_props`: the hook path passes a resolved
            # anchor entity (edited file / grep symbol) so the P2 relationship
            # boost fires; a direct MCP call passes None. That is data, not
            # behaviour — the pipeline code is identical (the hard
            # non-divergence invariant).
            candidates: list[dict] = []
            for distance, source, obj in merged:
                cand = {
                    "_c": collection,
                    "_s": max(0.0, 1.0 - distance),
                    "_d": distance,
                    "_p": obj.properties,
                    "_src": source,
                }
                # R2-6: attach the stored node vector (fetched via include_vector
                # above) as ``n_emb`` so the telemetry emitter's fast-path uses it
                # directly instead of re-embedding. ``obj.vector`` is a
                # dict[slot -> vector] for named-vector collections; pull the
                # active code slot only. Soft-fail: a missing/oddly-shaped vector
                # just leaves the candidate without ``n_emb`` (the emitter's
                # bounded recovery then handles the rare residual).
                try:
                    _v = getattr(obj, "vector", None)
                    if isinstance(_v, dict):
                        _slot_vec = _v.get(_code_slot)
                    elif isinstance(_v, list):
                        _slot_vec = _v
                    else:
                        _slot_vec = None
                    if _slot_vec:
                        cand["n_emb"] = list(_slot_vec)
                except Exception:
                    pass
                candidates.append(cand)

            # B2 (design audit): cull the excluded file's own entities BEFORE
            # the pipeline (pre-trim), so the +0.03 same-file anchor boost
            # cannot fill the top-K with the file being edited. Pipeline stays
            # pure — this is caller-side candidate normalisation. Matching is
            # separator-normalised and boundary-aware so an absolute editor
            # path still culls the analyzer's repo-relative file_path.
            if exclude_file:
                _ex = str(exclude_file).replace("\\", "/").strip()

                def _is_excluded(props: dict) -> bool:
                    fp = (props.get("file_path") or props.get("path") or "")
                    fp = str(fp).replace("\\", "/")
                    if not fp or not _ex:
                        return False
                    return (
                        fp == _ex
                        or _ex.endswith("/" + fp)
                        or fp.endswith("/" + _ex)
                    )

                candidates = [
                    c for c in candidates if not _is_excluded(c.get("_p") or {})
                ]

            try:
                _slot = _active_code_vector_slot()
            except Exception:
                _slot = "codesage_embed"

            anchor_props = self._resolve_anchor_props(anchor) if anchor else None

            # Score-tier allocation only runs in "auto" mode; explicit detail
            # values want uniform output, so we skip the budget allocator and
            # let the format loop honour `detail` directly (tier_fn=None → no
            # `_tier` set). Same rule as the MCP. F4 (pre-gate audit): the
            # tier `min` gate DERIVES from the resolved post-rerank floor so a
            # GUI/env floor override changes what renders in auto mode —
            # identical wiring in the MCP (the hard invariant).
            _post_floor = resolve_post_rerank_floor(_slot)
            _tier_fn = make_code_tier_fn(min_gate=_post_floor) if detail == "auto" else None
            survivors = run_code_retrieval_pipeline(
                candidates,
                retrieval_floor=resolve_retrieval_floor(_slot),
                post_rerank_floor=_post_floor,
                anchor_props=anchor_props,
                limit=limit,
                collapse_fn=make_code_collapse_fn(),
                tier_fn=_tier_fn,
                key_fields=("file_path", "full_name"),
            )

            # v0.2.73 RL-2: the CLI (and the pre-edit hooks routing through
            # it) now emits code retrieval telemetry via the SAME shared
            # server helper the MCP uses — one emit home, no divergence.
            # Soft-fail: telemetry never breaks the search.
            try:
                from weaviate_mcp.server import _emit_code_retrieval_telemetry

                _emit_code_retrieval_telemetry(
                    query=query,
                    query_emb=query_embedding,
                    survivors=survivors,
                    limit=limit,
                    slot=_slot,
                    task_type="code_hook" if hook_format else "code_cli",
                    retrieval_floor=resolve_retrieval_floor(_slot),
                    post_rerank_floor=_post_floor,
                    anchor_present=anchor_props is not None,
                    scope=collection,
                )
            except Exception:
                pass

            # Rebuild `top` in the (distance, source, candidate) shape the
            # downstream banner + format loop consume. `_src` / `_d` were
            # preserved through the pipeline (it never strips caller keys).
            top = [(c.get("_d", 1.0), c.get("_src", ""), c) for c in survivors]

            # Sibling fetcher: closes over self.client + self.project so
            # the shared helper stays Weaviate-agnostic. Only invoked for
            # top-2 ranks in auto mode by `_format_code_result_by_rank`.
            # Note: siblings come from the SELF project only — peer
            # projects don't expose their full source files for the
            # sibling enrichment path. This matches the MCP behaviour
            # where _project_collection scopes the sibling lookup.
            def _sibling_fetcher(file_path: str, hit_start_line: int, max_total: int, exclude_full_name: str) -> list[dict]:
                if not file_path or max_total <= 1:
                    return []
                try:
                    fn_coll = self.client.collections.get(self._coll("CodeFunction"))
                    cls_coll = self.client.collections.get(self._coll("CodeClass"))
                except Exception:
                    return []
                collected = []  # (start_line, c_name, properties)
                for coll_obj, c_name in ((fn_coll, "CodeFunction"), (cls_coll, "CodeClass")):
                    try:
                        sib_filter = Filter.by_property("file_path").equal(file_path)
                        if self.project:
                            sib_filter = sib_filter & Filter.by_property("project").equal(self.project)
                        # v0.2.46 V46-D: limit=64 is an intentional top-N cap
                        # (siblings are sorted by proximity AFTER the fetch).
                        # We don't emit a per-call truncation signal here
                        # because the caller (`_format_code_result_by_rank`)
                        # already only uses up to `max_total - 1` results.
                        SIBLING_FETCH_LIMIT = 64
                        sib_resp = coll_obj.query.fetch_objects(filters=sib_filter, limit=SIBLING_FETCH_LIMIT)
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
                    except Exception:
                        continue
                if not collected:
                    return []
                collected.sort(key=lambda t: abs(t[0] - hit_start_line))
                picked = collected[: max_total - 1]
                picked.sort(key=lambda t: t[0])
                siblings = []
                for sl_int, c_name, sp in picked:
                    ref = _format_code_result_ref(c_name, sp)
                    ref["sibling"] = True
                    ref["start_line"] = sl_int
                    ref["collection"] = c_name
                    siblings.append(ref)
                return siblings

            # v0.2.21 audit fix: emit the per-result banner ONLY when there
            # is at least one result. The pre-fix code emitted the
            # "🔍 Semantic search... Project filter: X / Found 0 results:"
            # block BEFORE the query ran, so even zero-result runs produced
            # non-empty stdout — the pre-edit hook's HAS_CODE=0 guard then
            # never fired and every Edit injected ~300 bytes of useless
            # "Found 0 results" context.
            #
            # For zero-result runs we now emit a SHORT identifying line
            # (one for hook-format, one human-readable). This is deliberate
            # over emitting nothing: per user direction 2026-05-20,
            # "multiple empty might be worse than multiple short texts to
            # let [the model] understand where it's coming from". The
            # model sees the hook fired AND knows the search scope, so it
            # can judge whether the absence of results is meaningful for
            # the task at hand. Cost: ~50-80 bytes per Edit vs ~300 bytes
            # for the pre-fix banner-on-empty, vs 0 bytes for full
            # suppression that would leave the model wondering.
            if not hook_format:
                if top:
                    print(f"\n🔍 Semantic search in {collection}: '{query}'  (detail={detail})")
                    if self.project:
                        peer_count = len(pairs) - 1
                        if peer_count > 0:
                            peer_names = ", ".join(p_filter for _, p_filter in pairs[1:] if p_filter)
                            print(f"   Project filter: {self.project} (+ {peer_count} peer(s): {peer_names})")
                        else:
                            print(f"   Project filter: {self.project}")
                    print(f"   Found {len(top)} results:\n")
                else:
                    # One short line, no banner block, so caller (human or
                    # hook) sees the search WAS attempted and its scope.
                    proj_part = f" project={self.project}" if self.project else ""
                    print(f"   No matches in {collection} for '{query}'{proj_part}")
            else:
                # --hook-format path. On empty, emit a single stable-format
                # line the pre-edit hook can capture; its in-session dedup
                # keyed by "title" treats this as a normal entry and so
                # rate-limits identical (collection, project, query) tuples.
                # Distinct (collection, project, query) → distinct dedup
                # keys → unique short lines reach the model.
                if not top:
                    proj_part = f" | project={self.project}" if self.project else ""
                    print(f"CODE: no-results | collection={collection}{proj_part} | query='{query}'")

            # Code-chunk fetcher: closes over self.client + self._coll so the
            # shared tier formatter stays Weaviate-agnostic. Mirrors the MCP's
            # `_fetch_code_chunks` closure (keys on full_name — code's node
            # identity; CodeFunction + CodeClass are the only chunked code
            # collections). Returns [] on any failure or for a single-chunk
            # entity; only invoked for the three_chunks / full tiers.
            def _code_chunk_fetcher(full_name: str, hit_chunk: int, total: int, max_chunks: int, file_path: str = "") -> list[dict]:
                # C-8 (v0.2.75 P2b): `file_path` scopes the fetch to the winning
                # row's source file — two same-`full_name` entities in different
                # files cannot interleave chunk bodies. Empty preserves the
                # pre-fix full_name+project filter. MUST MATCH the MCP's
                # `_fetch_code_chunks` (server.py) — CLI≡MCP parity.
                if not full_name or total <= 1 or max_chunks <= 1:
                    return []
                collected_chunks: list[tuple[int, dict]] = []
                for base in ("CodeFunction", "CodeClass"):
                    try:
                        coll_obj = self.client.collections.get(self._coll(base))
                        flt = Filter.by_property("full_name").equal(full_name)
                        if self.project:
                            flt = flt & Filter.by_property("project").equal(self.project)
                        if file_path:
                            flt = flt & Filter.by_property("file_path").equal(file_path)
                        resp = coll_obj.query.fetch_objects(filters=flt, limit=max(total, max_chunks) + 4)
                        for obj in resp.objects:
                            cp = obj.properties or {}
                            cn = cp.get("chunk_num", 0) or 0
                            try:
                                collected_chunks.append((int(cn), cp))
                            except (TypeError, ValueError):
                                collected_chunks.append((0, cp))
                    except Exception:
                        continue
                if not collected_chunks:
                    return []
                # Centre a window of max_chunks around the hit, ordered by chunk_num.
                collected_chunks.sort(key=lambda t: abs(t[0] - (hit_chunk or 0)))
                picked = collected_chunks[:max_chunks]
                picked.sort(key=lambda t: t[0])
                return [cp for _, cp in picked]

            # Render each survivor through the shared helpers — SAME split as
            # the MCP loop: in "auto" mode every candidate carries a `_tier`
            # (from the shared pipeline's tier_fn) → score-tier renderer
            # (`_format_code_result_by_tier`, 1/3/7-chunk assembly); explicit
            # "titles"/"full" (tier_fn was None → no `_tier`) → the rank-based
            # formatter honours `detail` uniformly. i is 0-based for the
            # helper; human output uses 1-based numbering.
            for i, (distance, source, cand) in enumerate(top):
                props = cand.get("_p") or {}
                score = cand.get("_s", 0.0)
                tier = cand.get("_tier")
                if tier is not None:
                    rendered = _format_code_result_by_tier(
                        props,
                        cand.get("_c", collection),
                        tier,
                        score=score,
                        distance=distance,
                        # F5: peer rows must not assemble chunks from the SELF
                        # project's collections — the shared gate (imported
                        # from weaviate_mcp.server, same as the MCP loop)
                        # returns None so the tier degrades to single_chunk.
                        chunk_fetcher=_self_project_chunk_fetcher(
                            cand, self.project, _code_chunk_fetcher,
                        ),
                    )
                else:
                    rendered = _format_code_result_by_rank(
                        props,
                        cand.get("_c", collection),
                        rank=i,
                        detail=detail,
                        score=score,
                        distance=distance,
                        sibling_fetcher=_sibling_fetcher,
                    )
                # Source-project annotation for fan-out clarity. Empty
                # for self-only queries (the pre-P1-D shape).
                src_label = ""
                src_suffix = ""
                if self.project and source and source != self.project:
                    src_label = f"  [peer:{source}]"
                    src_suffix = f" | source={source}"
                self._print_code_result(
                    rendered, rank=i, hook_format=hook_format,
                    src_label=src_label, src_suffix=src_suffix,
                )

        except Exception as e:
            print(f"❌ Search error: {e}", file=sys.stderr)

    @staticmethod
    def _print_code_result(rendered: dict, rank: int, hook_format: bool,
                           src_label: str = "", src_suffix: str = "") -> None:
        """Print one rank-tier-formatted code-graph result.

        Both surfaces (hook + human) walk the same rendered dict so the
        body content is identical; only the per-block header differs.

        Hook format: ``CODE: <full_name> | <collection> | distance=<d>``
        first line (with optional ``| source=<peer>`` suffix), body lines
        indented two spaces, terminating blank line. The pre-edit hook
        regex ``^(KG|CODE):\\ (.+)$`` matches the header; indented body
        lines fall through to the block-content accumulator.

        Human format: ``<rank>. <full_name>`` with optional ``[peer:<src>]``
        and similarity / tier annotation, body lines indented three
        spaces, terminating blank line.
        """
        collection = rendered.get("collection", "")
        tier = rendered.get("tier", "ref")
        score_str = rendered.get("score", "")
        distance_str = rendered.get("distance", "")

        # Identifier for the dedup key: full_name when present, else the
        # closest substitute per collection. Mirrors the MCP _format_ref
        # priorities so the dedup key is stable across surfaces.
        full_name = rendered.get("full_name", "")
        if not full_name:
            if collection == "CodeModule":
                full_name = rendered.get("path", "Unknown")
            elif collection in ("CodeAPI", "CodeInteraction"):
                ep = rendered.get("endpoint", "")
                method = rendered.get("method", "")
                full_name = f"{method} {ep}".strip() or rendered.get("interaction_type", "Unknown")
            else:
                full_name = "Unknown"

        if hook_format:
            # v0.2.70 Stream E: append a "| src=<file_path>" trailer (LAST
            # field) so the shared seen-store can suppress a CODE block whose
            # source the model already Read explicitly (reads-ledger match). The
            # seen-store extracts the src via the last "| src=" occurrence, so it
            # MUST be last. Empty file_path -> no src trailer (key-only dedup).
            _fp = rendered.get("file_path", "") or ""
            _src_trailer = f" | src={_fp}" if _fp else ""
            print(f"CODE: {full_name} | {collection} | distance={distance_str}{src_suffix}{_src_trailer}")
            CodeGraphQuery._print_body(rendered, indent="  ", hook_format=True)
            # Blank line terminates the block (matches the KG block
            # contract that pre-edit-context-inject.sh _filter_seen
            # parses).
            print()
        else:
            similarity = 0.0
            try:
                similarity = 1.0 - float(distance_str) if distance_str else 0.0
            except (TypeError, ValueError):
                similarity = 0.0
            tier_suffix = f"  [tier={tier}]"
            print(f"{rank + 1}. {full_name}{src_label}{tier_suffix}")
            print(f"   Distance: {distance_str} (similarity: {similarity:.3f}, score: {score_str})")
            if tier == "ref":
                print()
                return
            CodeGraphQuery._print_body(rendered, indent="   ", hook_format=False)
            print()

    @staticmethod
    def _print_identity_extras(rendered: dict, indent: str) -> None:
        """v0.2.73 M2/M4: print the sidecar one-liner + analyzer fan-in count.

        The shared formatter (weaviate_mcp.server ``_format_code_result_by_*``)
        already populated the fields; the CLI only prints them — no logic here
        (the CLI/MCP non-divergence invariant). Absent fields → no lines
        (older rows / missing sidecar keep the pre-M2 output). Shared by the
        CodeFunction and CodeClass branches of ``_print_body``.
        """
        one_liner = rendered.get("one_liner", "")
        if one_liner:
            print(f"{indent}One-liner: {one_liner}")
        n_callers = rendered.get("n_callers")
        if n_callers is not None:
            print(f"{indent}Callers: {n_callers}")

    @staticmethod
    def _print_body(rendered: dict, indent: str, hook_format: bool) -> None:
        """Render the per-collection body section of a code-graph result.

        Uses the same fields the shared helper populates so the output
        is byte-identical between MCP JSON consumers and CLI human
        consumers (after stripping the prefix). Sibling rows render
        underneath the seed for top-2 ranks.
        """
        collection = rendered.get("collection", "")
        if collection == "CodeFunction":
            sig = rendered.get("signature", "")
            if sig:
                print(f"{indent}Signature: {sig}")
            CodeGraphQuery._print_identity_extras(rendered, indent)
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
            # B1 (design audit): the summary tier carries its content under
            # `summary` — previously only the CodeModule branch printed it,
            # so summary-tier functions rendered as a bare header (content
            # silently dropped on the CLI while the MCP JSON carried it).
            # Skip when identical to the doc already printed above (R1 makes
            # the summary tier prefer doc — avoid the duplicate line).
            summary = rendered.get("summary", "")
            if summary and summary != doc:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:400] + ("..." if len(summary) > 400 else "")
                    print(f"{indent}Summary: {snippet}")
            loc = rendered.get("location", "")
            if loc:
                print(f"{indent}Location: {loc}")
            body = rendered.get("function_body", "")
            if body and hook_format:
                print(f"{indent}Body:")
                for body_line in body.splitlines():
                    print(f"{indent}  {body_line}")
        elif collection == "CodeClass":
            sig = rendered.get("signature", "")
            if sig:
                print(f"{indent}Signature: {sig}")
            CodeGraphQuery._print_identity_extras(rendered, indent)
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
            # B1: same summary-tier rendering as the CodeFunction branch.
            summary = rendered.get("summary", "")
            if summary and summary != doc:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:400] + ("..." if len(summary) > 400 else "")
                    print(f"{indent}Summary: {snippet}")
            method_count = rendered.get("method_count")
            if method_count is not None:
                print(f"{indent}Methods: {method_count} methods")
            loc = rendered.get("location", "")
            if loc:
                print(f"{indent}Location: {loc}")
            body = rendered.get("class_body", "")
            if body and hook_format:
                print(f"{indent}Body:")
                for body_line in body.splitlines():
                    print(f"{indent}  {body_line}")
        elif collection == "CodeModule":
            path = rendered.get("path", "")
            if path:
                print(f"{indent}Path: {path}")
            lang = rendered.get("language", "")
            loc = rendered.get("loc", 0)
            if lang or loc:
                print(f"{indent}Language: {lang}, LOC: {loc}")
            summary = rendered.get("summary", "")
            if summary:
                if hook_format:
                    print(f"{indent}Summary: {summary}")
                else:
                    snippet = summary[:200] + ("..." if len(summary) > 200 else "")
                    print(f"{indent}Summary: {snippet}")
        elif collection == "CodeAPI":
            ep = rendered.get("endpoint", "")
            method = rendered.get("method", "")
            if ep or method:
                print(f"{indent}Endpoint: {method} {ep}".rstrip())
            desc = rendered.get("description", "")
            if desc:
                if hook_format:
                    print(f"{indent}Description: {desc}")
                else:
                    snippet = desc[:200] + ("..." if len(desc) > 200 else "")
                    print(f"{indent}Description: {snippet}")
        elif collection == "CodeInteraction":
            itype = rendered.get("interaction_type", "")
            direction = rendered.get("direction", "")
            if itype or direction:
                print(f"{indent}Type: {itype} | {direction}")
            proto = rendered.get("protocol", "")
            ep = rendered.get("endpoint", "")
            if proto or ep:
                print(f"{indent}{proto} -> {ep}")
            confidence = rendered.get("confidence", "")
            if confidence:
                print(f"{indent}Confidence: {confidence}")
            desc = rendered.get("description", "")
            if desc:
                if hook_format:
                    print(f"{indent}Description: {desc}")
                else:
                    snippet = desc[:200] + ("..." if len(desc) > 200 else "")
                    print(f"{indent}Description: {snippet}")

        # Sibling rows: only present for top-2 ranks in auto mode. Render
        # as one indented line each so the pre-edit hook treats them as
        # body content of the parent CODE: block.
        siblings = rendered.get("siblings") or []
        if siblings:
            print(f"{indent}Siblings ({len(siblings)}):")
            for sib in siblings:
                sib_coll = sib.get("collection", "")
                sib_name = (
                    sib.get("full_name")
                    or sib.get("path")
                    or sib.get("endpoint", "")
                )
                start_line = sib.get("start_line", "?")
                print(f"{indent}  - [{sib_coll}] {sib_name} (line {start_line})")

    def find_similar(self, reference_name: str, collection: str = "CodeFunction", limit: int = 5):
        """Find code similar to reference."""
        try:
            coll = self.client.collections.get(self._coll(collection))

            # Get reference object
            ref_query = coll.query.fetch_objects(
                filters=Filter.by_property("full_name").equal(reference_name),
                limit=1
            )

            if not ref_query.objects:
                print(f"❌ Reference '{reference_name}' not found in {collection}")
                return

            ref_obj = ref_query.objects[0]

            # Find similar
            similar_query = coll.query.near_object(
                near_object=ref_obj.uuid,
                limit=limit + 1
            )

            if self.project:
                similar_query = similar_query.where(
                    Filter.by_property("project").equal(self.project)
                )

            response = similar_query.do()

            # Format and print results
            print(f"\n🔍 Finding code similar to: '{reference_name}'")
            print(f"   Found {len(response.objects) - 1} similar items:\n")  # -1 for reference itself

            for i, obj in enumerate(response.objects, 1):
                if obj.uuid == ref_obj.uuid:
                    continue  # Skip reference itself

                props = obj.properties
                distance = obj.metadata.distance if obj.metadata.distance is not None else -1.0
                similarity = 1.0 - distance if distance >= 0 else 0.0

                print(f"{i}. {props.get('full_name')}")
                print(f"   Similarity: {similarity:.3f} (distance: {distance:.3f})")
                print(f"   Signature: {props.get('signature')}")
                if props.get('doc'):
                    print(f"   Doc: {props.get('doc')[:100]}...")
                print()

        except Exception as e:
            print(f"❌ Error finding similar code: {e}", file=sys.stderr)

    def query_structure(self, query_type: str, target: str):
        """Structural query (dependencies, callers, etc.)."""
        try:
            if query_type == "dependencies":
                # Module imports
                coll = self.client.collections.get(self._coll("CodeModule"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("path").equal(target),
                    limit=1,
                    return_references=QueryReference(link_on="imports")
                )

                if not response.objects:
                    print(f"❌ Module '{target}' not found")
                    return

                # Two DIFFERENT guards, both required — v0.2.70 C1c added only
                # the first, which is why this branch stayed broken on the
                # SUCCESS path until v0.2.92:
                #   1. `references` is None when the object carries no linked
                #      refs — guard before .get to avoid 'NoneType' has no
                #      attribute 'get'. Soft-fall to an empty dict.
                #   2. when a link DOES resolve, `.get()` hands back a
                #      `_CrossReference`, NOT a list. Verified on
                #      weaviate-client 4.21.0: `len()` and `iter()` both raise
                #      TypeError (and `bool()` is always True, so an `if refs:`
                #      guard looks fine while doing nothing), so the
                #      `len(imports)` line below crashed BEFORE the loop was
                #      ever reached. The targets live on `.objects`;
                #      normalize_reference_targets is the shared home for that.
                # The dedup matches the MCP: the analyzer stored one beacon per
                # discovered edge per analyze, so a re-analyzed repo carries
                # duplicate beacons for one edge (live data: 1518 `imports`
                # beacons on a single module). Printing them raw answers "what
                # does X import?" with N copies of the same path.
                _refs = response.objects[0].references or {}
                imports = dedup_ref_targets(
                    normalize_reference_targets(_refs.get("imports")), ("path",)
                )
                print(f"\n🔗 Dependencies of module '{target}':")
                print(f"   Imports {len(imports)} modules:\n")

                for imp in imports:
                    print(f"   - {imp.properties.get('path')}")

            elif query_type == "callers":
                # Find callers of function.
                #
                # v0.2.92 — this branch answered "Found 0 callers" for EVERY
                # input, silently, always. TWO independent defects, and fixing
                # only the first still answers 0:
                #
                #   1. the candidate fetch requested no `return_references`, so
                #      `obj.references` was ALWAYS None -> `(… or {}).get(
                #      "calls", [])` -> `[]` -> the `any(...)` test could never
                #      be True. (The `or {}` guard from v0.2.70 C1c was doing
                #      its job; there was simply nothing to read.)
                #   2. the candidate pool was `fetch_objects(limit=50)` with NO
                #      filter — an ARBITRARY 50 rows out of the collection
                #      (25 837 on the maintainer machine, i.e. 0.19%). Even
                #      with the references resolved, a caller outside that
                #      arbitrary slice can never be found.
                #
                # The fix filters SERVER-SIDE on `call_names`, exactly as the
                # working `query_code_structure("callers", …)` MCP branch does
                # (same `_caller_match_terms` / `_dedup_objects_by_full_name`
                # helpers, imported — not re-implemented). The limit now caps
                # MATCHING rows rather than candidates, which is also what
                # makes the truncation signal below meaningful.
                coll = self.client.collections.get(self._coll("CodeFunction"))
                # The target's own row(s). `full_name` is NOT unique: a chunked
                # function is N rows, and (live data) the same qualified name
                # legitimately exists in several files. Collect every uuid —
                # the call edge below is confirmed against the whole set.
                TARGET_ROWS_LIMIT = 32
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=TARGET_ROWS_LIMIT,
                    return_properties=["full_name"],
                )

                if not response.objects:
                    print(f"❌ Function '{target}' not found")
                    return

                target_uuids = {str(obj.uuid) for obj in response.objects}

                # Pattern B (intentional top-N cap, with a truncation signal so
                # the user knows when the list is capped — v0.2.46 V46-D).
                CALLERS_FETCH_LIMIT = 50
                caller_response = coll.query.fetch_objects(
                    filters=Filter.by_property("call_names").contains_any(
                        _caller_match_terms(target)
                    ),
                    limit=CALLERS_FETCH_LIMIT,
                    return_references=QueryReference(link_on="calls"),
                )

                # `call_names` holds BARE leaf names, so a name match alone can
                # point at a same-named function elsewhere. A resolved `calls`
                # EDGE is uuid-precise and settles it — where one exists. It is
                # a CORROBORATION, never a gate: the analyzer resolves an
                # ambiguous short name to a single candidate and its whole
                # cross-reference pass soft-fails, so a missing edge is not
                # evidence that the call is not there, and filtering on it
                # would drop real callers (live check: 11/11 confirmed for one
                # target, 0/50 for another whose leaf name is shared).
                # read_cross_reference is the shared home for the two shape
                # guards — `references is None` (v0.2.70 C1c) AND the
                # `_CrossReference` that `.get()` returns once a link actually
                # resolves (`bool()` of it is True even when empty, so a
                # hand-rolled `if refs:` looks right while doing nothing).
                confirmed_names = set()
                for obj in caller_response.objects:
                    for ref in read_cross_reference(obj, "calls"):
                        if str(getattr(ref, "uuid", "")) in target_uuids:
                            confirmed_names.add(
                                (obj.properties or {}).get("full_name") or ""
                            )
                            break

                # `call_names` is replicated on every chunk row of a chunked
                # caller — collapse to one row per full_name (same as the MCP).
                callers = _dedup_objects_by_full_name(caller_response.objects)
                truncated = len(caller_response.objects) >= CALLERS_FETCH_LIMIT

                print(f"\n🔗 Callers of function '{target}':")
                print(f"   Found {len(callers)} callers:\n")

                for caller in callers:
                    full_name = caller.properties.get('full_name')
                    mark = "  [call edge]" if full_name in confirmed_names else ""
                    print(f"   - {full_name}{mark}")
                    print(f"     {caller.properties.get('signature')}")

                if callers and len(confirmed_names) < len(callers):
                    print(
                        "\nℹ️  Rows without [call edge] matched the call NAME "
                        "only — a same-named function elsewhere may be the "
                        "actual callee."
                    )

                if truncated:
                    print(
                        f"\n⚠️  Capped at the first {CALLERS_FETCH_LIMIT} "
                        f"matching rows. Some callers may be missing."
                    )
                    print(
                        "   For a thorough scan, use the MCP "
                        "`query_code_structure(\"callers\", ...)` tool or "
                        "raise the limit in this script."
                    )

            elif query_type == "methods":
                # List class methods
                coll = self.client.collections.get(self._coll("CodeClass"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )

                if not response.objects:
                    print(f"❌ Class '{target}' not found")
                    return

                methods = response.objects[0].properties.get("methods", [])
                print(f"\n🔗 Methods in class '{target}':")
                print(f"   {len(methods)} methods:\n")

                for method in methods:
                    print(f"   - {method}")

            elif query_type == "extends":
                # Find base classes
                coll = self.client.collections.get(self._coll("CodeClass"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1,
                    return_references=QueryReference(link_on="extends")
                )

                if not response.objects:
                    print(f"❌ Class '{target}' not found")
                    return

                # v0.2.70 C1c guards the None `references`; v0.2.92
                # normalize_reference_targets guards the `_CrossReference`
                # shape `.get()` returns once a link actually resolves
                # (`len()`/`iter()` on it raise TypeError — same defect as the
                # dependencies branch above, same shared home). The dedup
                # collapses the analyzer's duplicate beacons (live data: 506
                # `extends` beacons for one base class).
                extends = dedup_ref_targets(
                    normalize_reference_targets(
                        (response.objects[0].references or {}).get("extends")
                    ),
                    ("full_name", "name"),
                )
                print(f"\n🔗 Base classes of '{target}':")
                print(f"   Extends {len(extends)} classes:\n")

                for base in extends:
                    print(f"   - {base.properties.get('full_name')}")

            elif query_type == "interactions":
                # Find outbound cross-service interactions for a function or module
                interactions_coll = self.client.collections.get(self._coll("CodeInteraction"))
                func_coll = self.client.collections.get(self._coll("CodeFunction"))
                func_resp = func_coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )
                # v0.2.46 V46-D: emit truncation signal (Pattern B). The
                # `limit=50` is an intentional top-N cap, but previously
                # the user had no way to know when the cap was hit.
                INTERACTIONS_FETCH_LIMIT = 50
                if func_resp.objects:
                    source_uuid = str(func_resp.objects[0].uuid)
                    ix_resp = interactions_coll.query.fetch_objects(
                        filters=Filter.by_ref("source_function").by_id().equal(source_uuid),
                        limit=INTERACTIONS_FETCH_LIMIT
                    )
                else:
                    mod_coll = self.client.collections.get(self._coll("CodeModule"))
                    mod_resp = mod_coll.query.fetch_objects(
                        filters=Filter.by_property("path").equal(target),
                        limit=1
                    )
                    if not mod_resp.objects:
                        print(f"❌ Function or module '{target}' not found")
                        return
                    source_uuid = str(mod_resp.objects[0].uuid)
                    ix_resp = interactions_coll.query.fetch_objects(
                        filters=Filter.by_ref("source_module").by_id().equal(source_uuid),
                        limit=INTERACTIONS_FETCH_LIMIT
                    )

                truncated = len(ix_resp.objects) >= INTERACTIONS_FETCH_LIMIT
                print(f"\n🔗 Cross-service interactions from '{target}':")
                print(f"   Found {len(ix_resp.objects)} interactions:\n")
                for obj in ix_resp.objects:
                    p = obj.properties
                    print(f"   [{p.get('confidence','?')}] {p.get('interaction_type','')} {p.get('protocol','')} → {p.get('endpoint','')}")
                    print(f"     Direction: {p.get('direction','')} | Raw: {p.get('raw_target','')}")
                    if p.get('description'):
                        print(f"     {p.get('description','')}")
                    print()

                if truncated:
                    print(
                        f"⚠️  Result list capped at {INTERACTIONS_FETCH_LIMIT}. "
                        f"Some interactions from '{target}' may be missing."
                    )

            else:
                print(f"❌ Unknown query type: {query_type}")
                print("   Supported: dependencies, callers, methods, extends, interactions")

        except Exception as e:
            print(f"❌ Structure query error: {e}", file=sys.stderr)

    def close(self):
        """Close Weaviate connection."""
        if self.client:
            self.client.close()


def main():
    parser = argparse.ArgumentParser(
        description="Query code graph with semantic and structural search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest='command', help='Query type')

    # Semantic search
    search_parser = subparsers.add_parser('search', help='Semantic search for code')
    search_parser.add_argument('query', type=str, help='Search query')
    search_parser.add_argument('--collection', '-c', type=str, default="CodeFunction",
                              choices=["CodeFunction", "CodeClass", "CodeModule", "CodeAPI", "CodeInteraction"],
                              help='Collection to search (default: CodeFunction)')
    search_parser.add_argument('--limit', '-l', type=int, default=5,
                              help='Maximum results (default: 5)')
    search_parser.add_argument('--project', '-p', type=str,
                              help='Filter by project name')
    search_parser.add_argument('--detail', type=str, default='auto',
                              choices=['auto', 'titles', 'full'],
                              help=("Verbosity per result. 'auto' (default) = score-tiered "
                                    "per result (summary / single_chunk / three_chunks / "
                                    "full as score rises; the min gate derives from the "
                                    "post-rerank floor — matches search_code_graph MCP). "
                                    "'titles' = name+score only. 'full' = full details for all."))
    search_parser.add_argument('--hook-format', action='store_true',
                              help=("Emit one-line 'CODE: <full_name> | <collection> | "
                                    "distance=<d>' header per result so the pre-edit hook "
                                    "can dedup by entity name. Suppresses banner lines."))
    search_parser.add_argument('--anchor', type=str, default=None,
                              help=('Edited file path or symbol full_name — biases rerank '
                                    'toward call-linked / same-module / shared-type code'))
    search_parser.add_argument('--exclude-file', type=str, default=None,
                              help=('Drop candidates whose source file is this path BEFORE '
                                    'trimming to --limit (the Read/Edit hook passes the '
                                    'edited file here to avoid self-injection)'))
    search_parser.add_argument('--transcript', type=str, default=None,
                              help=('WP-E (v0.2.92): path to the live JSONL transcript — a '
                                    'PATH, never text. When the query is short relative to '
                                    "the code-embedding model's budget, the shared "
                                    'vco_lib.query_enrichment component walks backwards '
                                    "through the agent's own output (never tool payloads) "
                                    'to enrich it. Omitted/absent reproduces the unenriched '
                                    'query byte-for-byte.'))

    # Similar code
    similar_parser = subparsers.add_parser('similar', help='Find similar code')
    similar_parser.add_argument('reference', type=str, help='Reference code full name')
    similar_parser.add_argument('--collection', '-c', type=str, default="CodeFunction",
                               choices=["CodeFunction", "CodeClass"],
                               help='Collection type (default: CodeFunction)')
    similar_parser.add_argument('--limit', '-l', type=int, default=5,
                               help='Maximum results (default: 5)')
    similar_parser.add_argument('--project', '-p', type=str,
                               help='Filter by project name')

    # Structural queries
    structure_parser = subparsers.add_parser('structure', help='Structural queries')
    structure_parser.add_argument('query_type', type=str,
                                 choices=['dependencies', 'callers', 'methods', 'extends', 'interactions'],
                                 help='Query type')
    structure_parser.add_argument('target', type=str, help='Target entity')
    structure_parser.add_argument('--project', '-p', type=str,
                                 help='Filter by project name')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Resolve project: explicit --project wins; otherwise route through
    # the launcher's vct-hub for the canonical per-project value, falling
    # back to CODE_GRAPH_PROJECT / PROJECT_NAME env vars when the hub is
    # unreachable (v0.2.21 Step 18 caller migration). Without this, the
    # CLI always queries unprefixed `CodeFunction` etc., which never
    # exist in multi-project Weaviate setups — every project ships its
    # own `<Project>_CodeFunction` collection. Pre-2026-05-08 the CLI
    # was effectively dead unless the user remembered to pass --project.
    effective_project = getattr(args, 'project', None)
    if not effective_project:
        try:
            from vco_lib.project_config import resolve as _vco_resolve  # type: ignore[import-not-found]
            from pathlib import Path as _Path
            _cfg = _vco_resolve(_Path.cwd())
            # v0.2.70 Bug C1a: use the canonical binding-row prefix
            # (code_graph_collection_prefix), NOT the slug alias
            # code_graph_project. The slug sanitises to a nonexistent
            # `<Slug>_CodeFunction` (e.g. Orchestrator_root_CodeFunction) so the
            # CLI returned no-results / crashed `structure` since v0.2.21.
            # Mirrors server.py:2293-2294 (W3) and post-file-edit.sh:445; keep
            # code_graph_project as a secondary fallback for legacy resolver
            # shapes that populate only the slug. MUST MATCH those siblings.
            effective_project = (
                _cfg.code_graph_collection_prefix
                or _cfg.code_graph_project
                or None
            )
        except Exception:
            effective_project = None
    if not effective_project:
        effective_project = os.getenv("CODE_GRAPH_PROJECT") or os.getenv("PROJECT_NAME") or None

    # Create query interface
    querier = CodeGraphQuery(project=effective_project)

    # Connect to Weaviate
    if not querier.connect():
        return 1

    try:
        # Execute command
        if args.command == 'search':
            querier.search_by_concept(args.query, args.collection, args.limit, args.detail,
                                       hook_format=getattr(args, 'hook_format', False),
                                       anchor=getattr(args, 'anchor', None),
                                       exclude_file=getattr(args, 'exclude_file', None),
                                       transcript=getattr(args, 'transcript', None))
        elif args.command == 'similar':
            querier.find_similar(args.reference, args.collection, args.limit)
        elif args.command == 'structure':
            querier.query_structure(args.query_type, args.target)

        return 0

    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        return 1
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        querier.close()


if __name__ == "__main__":
    sys.exit(main())
