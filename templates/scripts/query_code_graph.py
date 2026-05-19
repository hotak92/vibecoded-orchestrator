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

# Import the shared rank-tier formatter from the MCP server module so the
# CLI emits results identically to `search_code_graph` MCP. The script
# lives at .claude/scripts/query_code_graph.py and the MCP module at
# claude_mcp_servers/weaviate_mcp/server.py — derive the project root
# from this file's location so the layout works without hardcoded paths.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MCP_SERVERS_PATH = _PROJECT_ROOT / "claude_mcp_servers"
if str(_MCP_SERVERS_PATH) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVERS_PATH))

try:
    from weaviate_mcp.server import (
        _format_code_result_by_rank,
        _format_code_result_ref,
        CODE_SIBLINGS_RANK_1,
        CODE_SIBLINGS_RANK_2,
    )
except ImportError as exc:  # pragma: no cover — surface a clear error
    print(
        f"Error: could not import weaviate_mcp.server rank-tier helper: {exc}\n"
        f"  Expected module at: {_MCP_SERVERS_PATH}/weaviate_mcp/server.py\n"
        "  This script requires the MCP server module for shared rendering.",
        file=sys.stderr,
    )
    sys.exit(1)

# P1-D (2026-05-08): centralized access-matrix helper. Resolved via
# $VCT_ORCHESTRATOR_ROOT (the orchestrator clone is where
# claude_mcp_servers/scripts/kg_access.py lives) with an in-tree
# fallback for the orchestrator self path. The graceful fallback is a
# self-only no-op so the CLI keeps working on a hand-edited venv.
try:
    # VCO-REWIRE-BEGIN: orchestrator-root-resolution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "claude_mcp_servers" / "scripts"))
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
        # Mirror sanitize_collection_prefix's contract for the fallback
        # path. Best-effort only — this branch fires when the helper
        # isn't on sys.path, which means the user is in a degraded
        # environment anyway.
        import re as _re
        prefix = _re.sub(r"[^a-zA-Z0-9_]", "_", self_project)
        if prefix and not prefix[0].isupper():
            prefix = prefix[0].upper() + prefix[1:]
        return [(f"{prefix}_{b}", self_project) for b in bases_t]

# Load MCP config
CONFIG_PATH = Path.home() / ".claude/workflow/config/mcp-config.json"

if CONFIG_PATH.exists():
    config = json.loads(CONFIG_PATH.read_text())
    WEAVIATE_URL = config["weaviate"]["url"]
    GRPC_PORT = config["weaviate"]["grpc_port"]
    OLLAMA_URL = config.get("ollama", {}).get("url", "http://localhost:11435")
else:
    WEAVIATE_URL = "http://localhost:8081"
    GRPC_PORT = 50052
    OLLAMA_URL = "http://localhost:11435"


def _sanitize_collection_prefix(name: str) -> str:
    """Sanitize project name for use as Weaviate collection prefix."""
    import re
    sanitized = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def _collection_name(base: str, project: str = None) -> str:
    """Return per-project collection name if project is set."""
    if not project:
        return base
    return f"{_sanitize_collection_prefix(project)}_{base}"


# Code embedding configuration — v0.2.18: centralised via
# EmbeddingService. Pre-v0.2.18 read CODE_EMBED_BACKEND / CODE_EMBED_-
# SERVICE_URL / CODE_EMBED_MODEL directly and hardcoded the slot.
CODE_EMBED_SERVICE_URL = os.getenv("CODE_EMBED_SERVICE_URL", "http://localhost:11440")

# Import EmbeddingService — graceful fallback for half-installed venvs.
try:
    _vco_lib_parent = _PROJECT_ROOT
    if str(_vco_lib_parent) not in sys.path:
        sys.path.insert(0, str(_vco_lib_parent))
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

    def search_by_concept(self, query: str, collection: str = "CodeFunction", limit: int = 5, detail: str = "auto", hook_format: bool = False):
        """Semantic search for code by concept.

        P1-D (2026-05-08): when ``self.project`` is set, fan out across
        self + every peer in ``VCT_CODE_GRAPH_ACCESS_LIST``. Each
        per-collection query stays bounded to ``limit`` over-fetched
        candidates; results are merged and re-ranked by distance before
        truncation. When ``self.project`` is unset (cross-tenant
        "search all projects" path), behaviour is unchanged: a single
        bare-collection query.

        Uses the shared rank-tier formatter (`_format_code_result_by_rank`)
        so output matches `search_code_graph` MCP exactly. Tier policy:
          - rank 0-1: full untruncated content + sibling rows in same file
          - rank 2-3: full content with doc/summary truncated at
            CODE_TRUNC_CHARS (default 1200)
          - rank 4+:  metadata-only refs

        detail:
          "auto"   — rank-driven tiering (top-2 + siblings, mid truncated, rest refs)
          "titles" — metadata refs for every result
          "full"   — full untruncated for every result

        hook_format: when True, emit a stable per-result header line
          'CODE: <full_name> | <collection> | distance=<d>' (with optional
          ' | source=<peer>' suffix for cross-tenant fan-out) and skip the
          human banner. The pre-edit hook uses this header to dedup repeat
          injections by entity name across a session via the regex
          ``^(KG|CODE):\\ (.+)$``. Body lines follow as ordinary indented
          content; blank lines separate blocks.
        """
        try:
            # Generate query embedding
            query_embedding = generate_code_embedding(query)
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

            # Header: show the fan-out scope so the user sees what was
            # actually queried (especially useful when the access list
            # is set in env and the user has forgotten what's granted).
            # Suppressed under --hook-format (the pre-edit hook only wants
            # parser-friendly per-result lines).
            if not hook_format:
                print(f"\n🔍 Semantic search in {collection}: '{query}'  (detail={detail})")
                if self.project:
                    peer_count = len(pairs) - 1
                    if peer_count > 0:
                        peer_names = ", ".join(p_filter for _, p_filter in pairs[1:] if p_filter)
                        print(f"   Project filter: {self.project} (+ {peer_count} peer(s): {peer_names})")
                    else:
                        print(f"   Project filter: {self.project}")

            # Fan-out: query each (collection, filter) pair, merge by
            # ascending distance. We over-fetch by `limit` per
            # collection so the merge has enough candidates to fill
            # `limit` even when the top-K is concentrated in peers.
            merged: list[tuple[float, str, object]] = []  # (distance, source_label, obj)
            for coll_name, project_filter in pairs:
                try:
                    coll = self.client.collections.get(coll_name)
                except Exception:
                    # Peer never indexed this base — skip silently.
                    continue
                nv_kwargs = dict(
                    near_vector=query_embedding,
                    limit=limit,
                    return_metadata=MetadataQuery(distance=True),
                    target_vector=_active_code_vector_slot(),
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

            # Sort by distance asc; truncate to limit. Multi-collection
            # fan-out can return the same full_name from multiple
            # collections (it shouldn't, but a misconfigured access list
            # with overlapping projects could) — dedupe by full_name
            # keeping the lowest-distance candidate.
            merged.sort(key=lambda t: t[0])
            seen_keys: set[str] = set()
            top: list[tuple[float, str, object]] = []
            for distance, source, obj in merged:
                key = obj.properties.get("full_name") or obj.properties.get("name") or str(obj.uuid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                top.append((distance, source, obj))
                if len(top) >= limit:
                    break

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

            if not hook_format:
                print(f"   Found {len(top)} results:\n")

            # Render through shared helper. i is 0-based for the helper;
            # human output uses 1-based numbering.
            for i, (distance, source, obj) in enumerate(top):
                props = obj.properties
                score = (1.0 - distance) if distance >= 0 else 0.0
                rendered = _format_code_result_by_rank(
                    props,
                    collection,
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
            print(f"CODE: {full_name} | {collection} | distance={distance_str}{src_suffix}")
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
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
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
            doc = rendered.get("doc", "")
            if doc:
                if hook_format:
                    print(f"{indent}Doc: {doc}")
                else:
                    snippet = doc[:200] + ("..." if len(doc) > 200 else "")
                    print(f"{indent}Doc: {snippet}")
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

                imports = response.objects[0].references.get("imports", [])
                print(f"\n🔗 Dependencies of module '{target}':")
                print(f"   Imports {len(imports)} modules:\n")

                for imp in imports:
                    print(f"   - {imp.properties.get('path')}")

            elif query_type == "callers":
                # Find callers of function
                coll = self.client.collections.get(self._coll("CodeFunction"))
                response = coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )

                if not response.objects:
                    print(f"❌ Function '{target}' not found")
                    return

                func_uuid = response.objects[0].uuid

                # Find references
                caller_response = coll.query.fetch_objects(
                    limit=50  # Increased limit for thorough search
                )

                # Filter for functions that call target
                callers = []
                for obj in caller_response.objects:
                    calls_refs = obj.references.get("calls", [])
                    if any(ref.uuid == func_uuid for ref in calls_refs):
                        callers.append(obj)

                print(f"\n🔗 Callers of function '{target}':")
                print(f"   Found {len(callers)} callers:\n")

                for caller in callers:
                    print(f"   - {caller.properties.get('full_name')}")
                    print(f"     {caller.properties.get('signature')}")

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

                extends = response.objects[0].references.get("extends", [])
                print(f"\n🔗 Base classes of '{target}':")
                print(f"   Extends {len(extends)} classes:\n")

                for base in extends:
                    print(f"   - {base.properties.get('full_name')}")

            elif query_type == "interactions":
                # Find outbound cross-service interactions for a function or module
                from weaviate.classes.query import QueryReference as QR
                interactions_coll = self.client.collections.get(self._coll("CodeInteraction"))
                func_coll = self.client.collections.get(self._coll("CodeFunction"))
                func_resp = func_coll.query.fetch_objects(
                    filters=Filter.by_property("full_name").equal(target),
                    limit=1
                )
                if func_resp.objects:
                    source_uuid = str(func_resp.objects[0].uuid)
                    ix_resp = interactions_coll.query.fetch_objects(
                        filters=Filter.by_ref("source_function").by_id().equal(source_uuid),
                        limit=50
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
                        limit=50
                    )

                print(f"\n🔗 Cross-service interactions from '{target}':")
                print(f"   Found {len(ix_resp.objects)} interactions:\n")
                for obj in ix_resp.objects:
                    p = obj.properties
                    print(f"   [{p.get('confidence','?')}] {p.get('interaction_type','')} {p.get('protocol','')} → {p.get('endpoint','')}")
                    print(f"     Direction: {p.get('direction','')} | Raw: {p.get('raw_target','')}")
                    if p.get('description'):
                        print(f"     {p.get('description','')}")
                    print()

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
                              help=("Verbosity per result. 'auto' (default) = top-4 full, "
                                    "rest as refs (matches search_code_graph MCP). "
                                    "'titles' = name+score only. 'full' = full details for all."))
    search_parser.add_argument('--hook-format', action='store_true',
                              help=("Emit one-line 'CODE: <full_name> | <collection> | "
                                    "distance=<d>' header per result so the pre-edit hook "
                                    "can dedup by entity name. Suppresses banner lines."))

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

    # Resolve project: explicit --project wins; otherwise fall back to
    # CODE_GRAPH_PROJECT or PROJECT_NAME env vars (same priority as the
    # MCP server's CODE_GRAPH_PROJECT resolution). Without this, the CLI
    # always queries unprefixed `CodeFunction` etc., which never exist in
    # multi-project Weaviate setups — every project ships its own
    # `<Project>_CodeFunction` collection. Pre-2026-05-08 the CLI was
    # effectively dead unless the user remembered to pass --project.
    effective_project = getattr(args, 'project', None) or os.getenv("CODE_GRAPH_PROJECT") or os.getenv("PROJECT_NAME") or None

    # Create query interface
    querier = CodeGraphQuery(project=effective_project)

    # Connect to Weaviate
    if not querier.connect():
        return 1

    try:
        # Execute command
        if args.command == 'search':
            querier.search_by_concept(args.query, args.collection, args.limit, args.detail,
                                       hook_format=getattr(args, 'hook_format', False))
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
