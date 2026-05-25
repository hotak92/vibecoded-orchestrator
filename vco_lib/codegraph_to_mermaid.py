# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Codegraph subgraph → Mermaid renderer (Phase 3 of the diagrams-integration plan).

Why this module exists
----------------------
The plan's Phase 3 (.claude/context/plans/diagrams-integration-excalidraw-mermaid-
2026-05-24.md §3 Phase 3) asks for an auto-generator that turns a subgraph from
the per-project Weaviate code-graph collections into a Mermaid ``flowchart TD``
source string. Two deliverables:

  1. This module — pure-Python, no MCP runtime needed. Wrapping the Weaviate
     client directly avoids the MCP server's module-level state (search-result
     caches, embedding-service lookup, hub resolver) which is irrelevant to a
     "fetch + render" pipeline and would force a hub round-trip per CLI call.
  2. A thin CLI in ``vco_lib/cli/codegraph_diagram.py`` that exposes it as
     ``vco codegraph-diagram <symbol>``.
  3. A slash skill ``.claude/skills/codegraph-diagram/SKILL.md`` that wraps the
     CLI for in-conversation use.

Heuristics
----------
* **Seed resolution**: try ``CodeFunction.full_name`` exact match first
  (functions are the most common subjects of a codegraph question), then
  ``CodeClass.full_name``, then ``CodeModule.path``. If none match,
  ``fetch_subgraph`` returns ``seed_found=False`` and an empty subgraph
  rather than raising — callers (CLI, skill) can format the rejection
  message themselves.
* **BFS depth**: capped at 3 hops. Beyond that the auto-layout flat-line
  pile-up makes Mermaid unreadable.
* **Node cap**: 50 default (auto-layout cliff observed empirically with
  ``mmdc``); above that we truncate + warn.
* **Node IDs**: Mermaid forbids ``.`` in node IDs but our symbols use it
  liberally. We hash each ``full_name`` to a stable short ID
  (``n_<sha256[:12]>``) so the diagram is reproducible across runs.

Cross-OS
--------
* No subprocess calls.
* ``pathlib.Path`` everywhere if/when files are touched (rendering itself
  is pure-string).
* The Weaviate connection picks host/port from env (``WEAVIATE_URL``,
  ``GRPC_PORT``); the rest of the pipeline is platform-agnostic.

Cross-module dependencies
-------------------------
* ``vco_lib.diagram_paths.validate_scoped_path`` — when the CLI writes the
  ``.mmd`` file, it MUST land under ``.claude/diagrams/<category>/`` or
  it won't be indexed. The CLI checks; this module stays I/O-free.
* ``vco_lib.diagram_indexer.index_diagram`` — called by the CLI after a
  successful file write so the new diagram is searchable via
  ``hybrid_search``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ─── Public types ─────────────────────────────────────────────────────────

#: Scope value enumerates which edge types are traversed by ``fetch_subgraph``.
#: "all" = union of every supported edge type for the seed entity.
CodeGraphScope = Literal["calls", "imports", "extends", "composes", "interactions", "all"]


_ALLOWED_SCOPES: tuple[str, ...] = (
    "calls", "imports", "extends", "composes", "interactions", "all",
)

#: Hard ceiling on hops. Going beyond 3 produces flowcharts that mermaid-cli
#: can render but no human can read — every Mermaid flowchart we've inspected
#: with >3 hops devolves into a "ball of yarn".
MAX_HOPS = 3

#: Default safety cap on nodes in the final subgraph. 50 is the empirical
#: "auto-layout knee" — past this point ``mmdc -i`` still produces SVG but
#: edge crossings dominate and the diagram stops communicating.
DEFAULT_MAX_NODES = 50


@dataclass(frozen=True)
class SubgraphSpec:
    """Inputs to ``fetch_subgraph`` / ``generate``.

    Frozen so the spec can be hashed/cached and so callers can't mutate it
    mid-flight (BFS state lives separately).
    """

    seed_symbol: str
    hops: int
    scope: CodeGraphScope
    max_nodes: int = DEFAULT_MAX_NODES
    include_modules: bool = True

    def __post_init__(self) -> None:
        # Defensive validation. The CLI also validates via argparse but we
        # keep the contract enforceable at the library boundary so library
        # consumers (tests, future hooks) get the same guarantees.
        if not self.seed_symbol or not self.seed_symbol.strip():
            raise ValueError("seed_symbol must be non-empty")
        if not isinstance(self.hops, int) or self.hops < 1:
            raise ValueError(f"hops must be a positive int, got {self.hops!r}")
        if self.hops > MAX_HOPS:
            raise ValueError(
                f"hops={self.hops} exceeds MAX_HOPS={MAX_HOPS}; "
                f"diagrams beyond {MAX_HOPS} hops are unreadable"
            )
        if self.scope not in _ALLOWED_SCOPES:
            raise ValueError(
                f"scope={self.scope!r} not in {_ALLOWED_SCOPES}"
            )
        if not isinstance(self.max_nodes, int) or self.max_nodes < 1:
            raise ValueError(
                f"max_nodes must be a positive int, got {self.max_nodes!r}"
            )


# ─── Helpers: collection naming, node-id hashing ──────────────────────────


# Sanitisation mirrors ``claude_mcp_servers/weaviate_mcp/server.py::
# _sanitize_collection_prefix``. Kept in-module rather than imported so we
# stay independent of the MCP server's import-time side effects.
def _sanitize_collection_prefix(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if sanitized and not sanitized[0].isupper():
        sanitized = sanitized[0].upper() + sanitized[1:]
    return sanitized


def _collection_name(base: str, project: Optional[str]) -> str:
    """Return per-project collection name; bare base when no project filter."""
    if not project:
        return base
    return f"{_sanitize_collection_prefix(project)}_{base}"


def _node_id(symbol: str) -> str:
    """Stable, Mermaid-safe ID for a code symbol.

    Mermaid node IDs must match ``[A-Za-z_][A-Za-z0-9_]*``. We can't pass
    dotted symbols (``vco_lib.diagram_indexer.index_diagram``) verbatim, so
    we hash to a fixed-length prefix. The hash is deterministic so re-runs
    against the same subgraph produce byte-identical Mermaid output (the
    golden-file test in ``tests/test_codegraph_to_mermaid.py`` relies on
    this).

    The hash IS lossy — node IDs can't be reverse-engineered from the
    Mermaid source. The diagram's title and the per-node label (the human-
    readable symbol name) carry the full info; the hash is only for layout.
    """
    h = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return f"n_{h[:12]}"


def _module_id(module_path: str) -> str:
    """Stable Mermaid-safe ID for a module subgraph block."""
    h = hashlib.sha256(("module:" + module_path).encode("utf-8")).hexdigest()
    return f"m_{h[:12]}"


def _escape_label(label: str) -> str:
    """Escape a label so it sits cleanly inside ``["..."]`` brackets.

    Mermaid treats unquoted brackets / pipes / quotes inside node labels as
    syntax; quoting eliminates most of those but we still need to backslash-
    escape embedded double-quotes. Newlines flattened to spaces — multi-line
    labels in flowcharts always render badly.
    """
    safe = label.replace("\\", "\\\\").replace('"', '\\"')
    safe = safe.replace("\n", " ").replace("\r", " ")
    return safe


# ─── Node + edge dataclasses (internal — exposed via dict in fetch_subgraph) ──


@dataclass
class _GraphNode:
    """Internal node record before it's serialised into the return dict."""

    id: str
    label: str
    kind: str  # "function" | "class" | "module"
    module: str  # parent module path; "" for module nodes themselves
    full_name: str  # canonical symbol (for the dict payload)


@dataclass
class _GraphEdge:
    """Internal edge record before serialisation."""

    src: str  # source node_id
    dst: str  # destination node_id
    kind: str  # "calls" | "imports" | "extends" | "composes" | "interacts"
    label: str  # human-readable edge annotation; may match `kind`


# ─── Weaviate client (lazy + best-effort connection) ──────────────────────


def _connect_weaviate():  # noqa: ANN202 — return type bound to weaviate module
    """Open a Weaviate v4 client using env defaults.

    Mirrors the same connection shape the MCP server uses but doesn't
    import the server module (which has heavy side effects). Raises
    RuntimeError if the weaviate-client isn't installed.
    """
    try:
        import weaviate  # type: ignore
    except ImportError as exc:  # pragma: no cover — verified at runtime
        raise RuntimeError(
            "weaviate-client not installed in this venv; install via "
            "`pip install weaviate-client` and retry."
        ) from exc

    url = os.environ.get("WEAVIATE_URL", "http://localhost:8081")
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    http_port = parsed.port or 8081
    grpc_port = int(os.environ.get("GRPC_PORT", "50052"))
    return weaviate.connect_to_custom(
        http_host=host,
        http_port=http_port,
        http_secure=parsed.scheme == "https",
        grpc_host=host,
        grpc_port=grpc_port,
        grpc_secure=parsed.scheme == "https",
    )


# ─── Seed resolution ──────────────────────────────────────────────────────


def _resolve_seed(client, project: Optional[str], symbol: str) -> Optional[dict]:
    """Try function → class → module in order. Returns the resolved record:

        {
          "kind": "function" | "class" | "module",
          "full_name": str,        # canonical symbol (or path for modules)
          "module_path": str,      # parent module path; "" for module seeds
          "uuid": str,             # UUID for ref-based queries
          "label": str,            # short name for the node label
        }

    Returns ``None`` when no collection matches.
    """
    from weaviate.classes.query import Filter  # type: ignore

    # 1. CodeFunction.full_name
    try:
        coll = client.collections.get(_collection_name("CodeFunction", project))
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(symbol),
            limit=1,
        )
        if resp.objects:
            obj = resp.objects[0]
            p = obj.properties or {}
            full = str(p.get("full_name") or symbol)
            module_path = full.rsplit(".", 1)[0] if "." in full else full
            return {
                "kind": "function",
                "full_name": full,
                "module_path": module_path,
                "uuid": str(obj.uuid),
                "label": str(p.get("name") or full.rsplit(".", 1)[-1]),
            }
    except Exception as exc:  # noqa: BLE001 — best-effort; try next layer
        logger.debug("Seed lookup CodeFunction failed: %s", exc)

    # 2. CodeClass.full_name
    try:
        coll = client.collections.get(_collection_name("CodeClass", project))
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(symbol),
            limit=1,
        )
        if resp.objects:
            obj = resp.objects[0]
            p = obj.properties or {}
            full = str(p.get("full_name") or symbol)
            module_path = full.rsplit(".", 1)[0] if "." in full else full
            return {
                "kind": "class",
                "full_name": full,
                "module_path": module_path,
                "uuid": str(obj.uuid),
                "label": str(p.get("name") or full.rsplit(".", 1)[-1]),
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("Seed lookup CodeClass failed: %s", exc)

    # 3. CodeModule.path
    try:
        coll = client.collections.get(_collection_name("CodeModule", project))
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("path").equal(symbol),
            limit=1,
        )
        if resp.objects:
            obj = resp.objects[0]
            p = obj.properties or {}
            path = str(p.get("path") or symbol)
            return {
                "kind": "module",
                "full_name": path,
                "module_path": path,
                "uuid": str(obj.uuid),
                "label": path,
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("Seed lookup CodeModule failed: %s", exc)

    return None


# ─── Edge fetchers — one per scope ────────────────────────────────────────
#
# Each fetcher takes a "current node" record and returns a list of
# ``(target_record, edge_kind)`` tuples for the BFS frontier to consume.
# Target records have the same shape as `_resolve_seed`'s return so the
# BFS doesn't need per-kind branches when assembling _GraphNode entries.


def _fetch_calls(client, project: Optional[str], node: dict) -> list[tuple[dict, str]]:
    """Outbound calls from a CodeFunction node.

    Uses ``call_names`` (TEXT_ARRAY) — same property the MCP's BFS path
    query reads. If ``call_names`` isn't populated (legacy index), falls
    back to ``properties.get("calls")`` which some old indexer versions
    populated with a parallel string array.
    """
    if node["kind"] != "function":
        return []
    from weaviate.classes.query import Filter  # type: ignore

    coll_name = _collection_name("CodeFunction", project)
    try:
        coll = client.collections.get(coll_name)
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(node["full_name"]),
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001 — collection may not exist
        logger.debug("calls fetch failed for %s: %s", node["full_name"], exc)
        return []
    if not resp.objects:
        return []

    props = resp.objects[0].properties or {}
    callees: list[str] = list(props.get("call_names") or props.get("calls") or [])
    out: list[tuple[dict, str]] = []
    for callee_name in callees:
        if not callee_name or not isinstance(callee_name, str):
            continue
        # Look up the callee to grab its module_path + label (best-effort —
        # the callee may be external to the indexed graph, in which case we
        # synthesize a minimal record).
        callee_rec = _lookup_function(client, project, callee_name)
        if callee_rec is None:
            mod = callee_name.rsplit(".", 1)[0] if "." in callee_name else callee_name
            callee_rec = {
                "kind": "function",
                "full_name": callee_name,
                "module_path": mod,
                "uuid": "",
                "label": callee_name.rsplit(".", 1)[-1],
            }
        out.append((callee_rec, "calls"))
    return out


def _lookup_function(client, project: Optional[str], full_name: str) -> Optional[dict]:
    """Hydrate a function record (used by edge fetchers to enrich callees)."""
    from weaviate.classes.query import Filter  # type: ignore
    try:
        coll = client.collections.get(_collection_name("CodeFunction", project))
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(full_name),
            limit=1,
        )
    except Exception:  # noqa: BLE001
        return None
    if not resp.objects:
        return None
    obj = resp.objects[0]
    p = obj.properties or {}
    full = str(p.get("full_name") or full_name)
    return {
        "kind": "function",
        "full_name": full,
        "module_path": full.rsplit(".", 1)[0] if "." in full else full,
        "uuid": str(obj.uuid),
        "label": str(p.get("name") or full.rsplit(".", 1)[-1]),
    }


def _fetch_imports(client, project: Optional[str], node: dict) -> list[tuple[dict, str]]:
    """Outbound imports from a CodeModule node.

    Reads ``import_names`` (TEXT_ARRAY) when present — the MCP server's
    ``query_code_structure("dependencies", ...)`` uses the ``imports``
    reference, but iterating refs in a vendored BFS is expensive; the
    TEXT_ARRAY mirror is cheaper. Falls back to a reference traversal
    when ``import_names`` is empty (older indexer versions).
    """
    if node["kind"] != "module":
        return []
    from weaviate.classes.query import Filter  # type: ignore

    coll_name = _collection_name("CodeModule", project)
    try:
        coll = client.collections.get(coll_name)
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("path").equal(node["full_name"]),
            limit=1,
            return_references=["imports"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("imports fetch failed for %s: %s", node["full_name"], exc)
        return []
    if not resp.objects:
        return []

    obj = resp.objects[0]
    p = obj.properties or {}
    out: list[tuple[dict, str]] = []
    seen_paths: set[str] = set()

    # Prefer the TEXT_ARRAY (cheap, doesn't require an extra fetch round).
    for imp_name in p.get("import_names") or []:
        if not imp_name or not isinstance(imp_name, str):
            continue
        if imp_name in seen_paths:
            continue
        seen_paths.add(imp_name)
        out.append((
            {
                "kind": "module",
                "full_name": imp_name,
                "module_path": imp_name,
                "uuid": "",
                "label": imp_name,
            },
            "imports",
        ))

    # Reference fallback — only walk if TEXT_ARRAY was empty/missing.
    if not out:
        refs = obj.references.get("imports") if obj.references else None
        if refs:
            for imp in refs.objects:  # type: ignore[attr-defined]
                ip = imp.properties or {}
                path = str(ip.get("path") or "")
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                out.append((
                    {
                        "kind": "module",
                        "full_name": path,
                        "module_path": path,
                        "uuid": str(imp.uuid),
                        "label": path,
                    },
                    "imports",
                ))
    return out


def _fetch_extends(client, project: Optional[str], node: dict) -> list[tuple[dict, str]]:
    """Base classes of a CodeClass node."""
    if node["kind"] != "class":
        return []
    from weaviate.classes.query import Filter  # type: ignore

    coll_name = _collection_name("CodeClass", project)
    try:
        coll = client.collections.get(coll_name)
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(node["full_name"]),
            limit=1,
            return_references=["extends"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("extends fetch failed for %s: %s", node["full_name"], exc)
        return []
    if not resp.objects:
        return []

    obj = resp.objects[0]
    out: list[tuple[dict, str]] = []
    refs = obj.references.get("extends") if obj.references else None
    if refs:
        for base in refs.objects:  # type: ignore[attr-defined]
            bp = base.properties or {}
            full = str(bp.get("full_name") or "")
            if not full:
                continue
            out.append((
                {
                    "kind": "class",
                    "full_name": full,
                    "module_path": full.rsplit(".", 1)[0] if "." in full else full,
                    "uuid": str(base.uuid),
                    "label": str(bp.get("name") or full.rsplit(".", 1)[-1]),
                },
                "extends",
            ))
    return out


def _fetch_composes(client, project: Optional[str], node: dict) -> list[tuple[dict, str]]:
    """Classes composed (held as field types) by a CodeClass node."""
    if node["kind"] != "class":
        return []
    from weaviate.classes.query import Filter  # type: ignore

    coll_name = _collection_name("CodeClass", project)
    try:
        coll = client.collections.get(coll_name)
        resp = coll.query.fetch_objects(
            filters=Filter.by_property("full_name").equal(node["full_name"]),
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("composes fetch failed for %s: %s", node["full_name"], exc)
        return []
    if not resp.objects:
        return []

    props = resp.objects[0].properties or {}
    composes_names: list[str] = list(props.get("composes") or [])
    out: list[tuple[dict, str]] = []
    for composed_name in composes_names:
        if not composed_name or not isinstance(composed_name, str):
            continue
        out.append((
            {
                "kind": "class",
                "full_name": composed_name,
                "module_path": (
                    composed_name.rsplit(".", 1)[0]
                    if "." in composed_name else composed_name
                ),
                "uuid": "",
                "label": composed_name.rsplit(".", 1)[-1],
            },
            "composes",
        ))
    return out


def _fetch_interactions(
    client, project: Optional[str], node: dict,
) -> list[tuple[dict, str]]:
    """Outbound cross-service interactions from a function or module.

    Mirrors ``query_code_structure("interactions", target)``. The target
    nodes are synthesised as pseudo-modules labelled by the interaction
    endpoint — there's no canonical Weaviate object for "the remote
    /api/users endpoint", so we treat each interaction as a leaf node.
    """
    if node["kind"] not in ("function", "module"):
        return []
    from weaviate.classes.query import Filter  # type: ignore

    interactions_coll_name = _collection_name("CodeInteraction", project)
    try:
        interactions_coll = client.collections.get(interactions_coll_name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("interactions collection missing: %s", exc)
        return []

    # We need the seed object's UUID to filter by source_function/source_module.
    if node["kind"] == "function":
        if not node.get("uuid"):
            # Stranger (synthesised) function record — no UUID, can't query.
            return []
        try:
            ix_resp = interactions_coll.query.fetch_objects(
                filters=Filter.by_ref("source_function").by_id().equal(node["uuid"]),
                limit=50,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("interactions query (function) failed: %s", exc)
            return []
    else:  # module
        if not node.get("uuid"):
            return []
        try:
            ix_resp = interactions_coll.query.fetch_objects(
                filters=Filter.by_ref("source_module").by_id().equal(node["uuid"]),
                limit=50,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("interactions query (module) failed: %s", exc)
            return []

    out: list[tuple[dict, str]] = []
    for ix_obj in ix_resp.objects:
        p = ix_obj.properties or {}
        endpoint = str(p.get("endpoint") or p.get("raw_target") or "")
        protocol = str(p.get("protocol") or "")
        if not endpoint:
            continue
        # Label the target node by endpoint; the edge kind carries the
        # interaction protocol for clarity.
        synthetic_full = f"interaction:{protocol}:{endpoint}"
        out.append((
            {
                "kind": "module",  # render alongside modules — they're leaf-ish
                "full_name": synthetic_full,
                "module_path": "_interactions",
                "uuid": "",
                "label": (
                    f"{protocol} {endpoint}" if protocol else endpoint
                ),
            },
            "interacts",
        ))
    return out


# Dispatcher used by ``fetch_subgraph``. "all" fans out across every scope.
_EDGE_FETCHERS = {
    "calls": _fetch_calls,
    "imports": _fetch_imports,
    "extends": _fetch_extends,
    "composes": _fetch_composes,
    "interactions": _fetch_interactions,
}


def _fetchers_for_scope(scope: str):
    if scope == "all":
        return list(_EDGE_FETCHERS.values())
    if scope in _EDGE_FETCHERS:
        return [_EDGE_FETCHERS[scope]]
    return []


# ─── Public: fetch_subgraph ───────────────────────────────────────────────


def fetch_subgraph(spec: SubgraphSpec, project: Optional[str] = None) -> dict:
    """Query Weaviate for the subgraph centered on ``spec.seed_symbol``.

    Returns a dict with the shape::

        {
          "nodes": [{"id": str, "label": str, "kind": str, "module": str,
                     "full_name": str}, ...],
          "edges": [{"from": str, "to": str, "kind": str, "label": str}, ...],
          "seed_found": bool,
          "seed_kind": str | None,    # "function" | "class" | "module"
          "seed_full_name": str | None,
          "truncated": bool,
          "truncation_reason": str | None,
        }

    The dict is JSON-serialisable.

    The function is best-effort: any per-query Weaviate error degrades to
    "no edges in this direction" rather than raising, so a partially-indexed
    project still produces a partial diagram.
    """
    project = project or os.environ.get("CODE_GRAPH_PROJECT") or os.environ.get("PROJECT_NAME") or None

    empty_payload: dict = {
        "nodes": [],
        "edges": [],
        "seed_found": False,
        "seed_kind": None,
        "seed_full_name": None,
        "truncated": False,
        "truncation_reason": None,
    }

    try:
        client = _connect_weaviate()
    except Exception as exc:
        logger.warning("Weaviate connection failed: %s", exc)
        empty_payload["truncation_reason"] = f"weaviate-unreachable: {exc}"
        return empty_payload

    try:
        seed = _resolve_seed(client, project, spec.seed_symbol)
        if seed is None:
            return empty_payload

        nodes: dict[str, _GraphNode] = {}
        edges: list[_GraphEdge] = []

        seed_id = _node_id(seed["full_name"])
        nodes[seed_id] = _GraphNode(
            id=seed_id,
            label=seed["label"],
            kind=seed["kind"],
            module=seed["module_path"] or "",
            full_name=seed["full_name"],
        )

        fetchers = _fetchers_for_scope(spec.scope)

        # BFS frontier; each entry: (current_node_record, depth)
        frontier: deque[tuple[dict, int]] = deque([(seed, 0)])
        # full_name → node_id map so we can detect repeated visits across
        # different scopes / hops and dedup edges.
        seen_full_names: dict[str, str] = {seed["full_name"]: seed_id}
        # Edge-uniqueness key (src_id, dst_id, kind) — same source→target via
        # the same kind should appear once even if discovered via two
        # different BFS branches.
        seen_edges: set[tuple[str, str, str]] = set()

        truncated = False
        truncation_reason: Optional[str] = None

        while frontier and not truncated:
            current, depth = frontier.popleft()
            if depth >= spec.hops:
                continue

            for fetcher in fetchers:
                targets = fetcher(client, project, current)
                for tgt_rec, edge_kind in targets:
                    tgt_full = tgt_rec["full_name"]
                    tgt_id = seen_full_names.get(tgt_full)
                    if tgt_id is None:
                        tgt_id = _node_id(tgt_full)
                        if len(nodes) >= spec.max_nodes:
                            truncated = True
                            truncation_reason = (
                                f"node cap reached ({spec.max_nodes}); "
                                f"truncation prevents Mermaid auto-layout "
                                f"degradation"
                            )
                            break
                        nodes[tgt_id] = _GraphNode(
                            id=tgt_id,
                            label=tgt_rec["label"],
                            kind=tgt_rec["kind"],
                            module=tgt_rec["module_path"] or "",
                            full_name=tgt_full,
                        )
                        seen_full_names[tgt_full] = tgt_id
                        # Enqueue for next-hop expansion only when the
                        # target is a real (UUID-bearing) record OR a
                        # module/function we can re-lookup; synthesised
                        # interaction leaves don't expand further.
                        if depth + 1 < spec.hops and not tgt_full.startswith("interaction:"):
                            frontier.append((tgt_rec, depth + 1))

                    src_id = _node_id(current["full_name"])
                    edge_key = (src_id, tgt_id, edge_kind)
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
                    edges.append(_GraphEdge(
                        src=src_id,
                        dst=tgt_id,
                        kind=edge_kind,
                        label=edge_kind,
                    ))
                if truncated:
                    break

        return {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "kind": n.kind,
                    "module": n.module,
                    "full_name": n.full_name,
                }
                for n in nodes.values()
            ],
            "edges": [
                {"from": e.src, "to": e.dst, "kind": e.kind, "label": e.label}
                for e in edges
            ],
            "seed_found": True,
            "seed_kind": seed["kind"],
            "seed_full_name": seed["full_name"],
            "truncated": truncated,
            "truncation_reason": truncation_reason,
        }
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


# ─── Mermaid rendering ────────────────────────────────────────────────────


def render_mermaid(
    subgraph: dict,
    *,
    title: Optional[str] = None,
    include_modules: bool = True,
) -> str:
    """Emit a Mermaid ``flowchart TD`` source for the given subgraph.

    Format::

        ---
        title: <title>
        ---
        flowchart TD
            subgraph m_<hash> ["module_a"]
                n_<hash>["function_a"]
                n_<hash>["function_b"]
            end
            ...
            n_<hash> --> n_<hash>
            n_<hash> -->|"calls"| n_<hash>

    Notes:
      - Module subgraphs are emitted only when ``include_modules=True`` AND
        more than one distinct module is present (single-module renders are
        cleaner without the wrapping block).
      - Edge labels follow a "dominant kind, no label" heuristic: the most
        common edge kind in the diagram is rendered without a label
        (visual noise reduction); minority kinds get explicit ``|"kind"|``
        annotations.
      - Empty subgraph (no nodes) renders a single comment line so the
        output is still valid Mermaid (and indexable as a diagram).
    """
    nodes = list(subgraph.get("nodes") or [])
    edges = list(subgraph.get("edges") or [])

    # Header.
    lines: list[str] = []
    # PRE-ALPHA marker — every codegraph→Mermaid render carries this
    # comment so downstream readers know the diagram is generated and
    # may need manual review. `%%` is the Mermaid comment syntax;
    # renderers strip these from the visual output but the source file
    # (which Claude reads via `Read`) keeps them visible.
    lines.append("%% [PRE-ALPHA] auto-generated by `vco codegraph-diagram`.")
    lines.append("%% Output may be incomplete, inaccurate, or visually broken.")
    lines.append("%% Verify against the source code before sharing or making decisions.")
    if title:
        lines.append("---")
        lines.append(f"title: {_escape_label(title)}")
        lines.append("---")
    lines.append("flowchart TD")

    if not nodes:
        # Mermaid accepts a flowchart with just a comment.
        lines.append("    %% empty subgraph — seed not found or scope produced no edges")
        return "\n".join(lines) + "\n"

    # Group nodes by module for the optional subgraph blocks.
    by_module: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        by_module[n.get("module") or ""].append(n)

    use_subgraphs = include_modules and len(by_module) > 1

    if use_subgraphs:
        # Sort modules by name for stable output; nodes within each module
        # by label.
        for module_name in sorted(by_module.keys(), key=lambda s: (s == "", s)):
            module_nodes = sorted(
                by_module[module_name],
                key=lambda n: (n.get("label", ""), n.get("id", "")),
            )
            if module_name:
                mid = _module_id(module_name)
                lines.append(f'    subgraph {mid} ["{_escape_label(module_name)}"]')
                for n in module_nodes:
                    lines.append(_format_node_decl(n, indent="        "))
                lines.append("    end")
            else:
                # Nodes with no module → emit at top level (rare).
                for n in module_nodes:
                    lines.append(_format_node_decl(n, indent="    "))
    else:
        for n in sorted(nodes, key=lambda n: (n.get("label", ""), n.get("id", ""))):
            lines.append(_format_node_decl(n, indent="    "))

    # Edges with dominant-kind annotation suppression.
    if edges:
        kind_counts: dict[str, int] = defaultdict(int)
        for e in edges:
            kind_counts[e.get("kind", "")] += 1
        # Tiebreak: dominant is the most-frequent kind; ties broken
        # alphabetically so output is deterministic.
        dominant_kind = sorted(
            kind_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[0][0]

        sorted_edges = sorted(
            edges,
            key=lambda e: (e.get("from", ""), e.get("to", ""), e.get("kind", "")),
        )
        for e in sorted_edges:
            src = e.get("from", "")
            dst = e.get("to", "")
            kind = e.get("kind", "")
            if kind == dominant_kind:
                lines.append(f"    {src} --> {dst}")
            else:
                lines.append(f'    {src} -->|"{_escape_label(kind)}"| {dst}')

    return "\n".join(lines) + "\n"


def _format_node_decl(node: dict, *, indent: str) -> str:
    """Render a single node declaration line.

    Shape varies by kind so the rendered Mermaid reads naturally:
      * function → ``id["label()"]`` (parens hint at callable)
      * class    → ``id[/"label"/]`` (parallelogram for classes is too
        visually different — keep the rectangle but suffix the label
        with a discriminator)
      * module   → ``id(("label"))`` (circle/cylinder hint at a unit)
    """
    nid = node.get("id", "")
    label = node.get("label") or node.get("full_name", "")
    kind = node.get("kind", "")
    safe_label = _escape_label(label)
    if kind == "function":
        return f'{indent}{nid}["{safe_label}()"]'
    if kind == "class":
        return f'{indent}{nid}["{safe_label}"]'
    # module / synthetic / unknown
    return f'{indent}{nid}(["{safe_label}"])'


# ─── Convenience: end-to-end generator ────────────────────────────────────


def generate(
    spec: SubgraphSpec,
    *,
    project: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Fetch + render in one call.

    Returns the Mermaid source. The empty-subgraph case (seed not found)
    still returns a valid Mermaid string with a single ``%%`` comment;
    callers that want to distinguish "seed not found" from "seed found,
    no edges" should call ``fetch_subgraph`` directly and inspect
    ``seed_found``.
    """
    subgraph = fetch_subgraph(spec, project=project)
    # Auto-derived title when caller didn't pass one.
    effective_title = title
    if effective_title is None and subgraph.get("seed_full_name"):
        effective_title = subgraph["seed_full_name"]
    return render_mermaid(
        subgraph,
        title=effective_title,
        include_modules=spec.include_modules,
    )
