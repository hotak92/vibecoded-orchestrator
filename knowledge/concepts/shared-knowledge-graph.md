---
title: Shared Knowledge Graph (Cross-Project)
type: concept
tags: [knowledge-graph, weaviate, vibecoded-tools, mid-level-architecture, retrieval]
created: 2026-04-27T00:00:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# Shared Knowledge Graph (Cross-Project)

The vibecoded orchestrator ships with a **cross-project shared KG** — a single
Weaviate collection (`VibecodedOrchestrator_KnowledgeGraph`, renamed from
`VibeCodedTools_KnowledgeGraph` in v0.2.12 / PR-26 Group E) seeded at install
time from `vibecoded-orchestrator/knowledge/`. Every project on the machine
queries both its own per-project KG **and** the shared KG by default; results
are merged, de-duped, and re-ranked together. Users with data still under the
old name can designate it as canonical via the launcher's Identity tab
"Manage shared KG collection" picker — no data migration required.

## Goal

Reusable knowledge (LoRA fine-tuning notes, transformer architecture notes,
RAG patterns, etc.) shouldn't be re-learned per project. A junior project
that has never seen "FlashAttention" should still get a hit when asking about
attention optimization, because the shared KG already documents it.

## Architecture

Two collections, one MCP server, transparent merge:

```
project A           project B           project C
  │ KG_COLL=A_KG      │ KG_COLL=B_KG      │ KG_COLL=C_KG
  │                   │                   │
  └─────────┬─────────┴─────────┬─────────┘
            │                   │
            ▼                   ▼
    ┌─────────────────────────────────────┐
    │       weaviate_mcp/server.py        │
    │                                     │
    │  hybrid_search / semantic_graph_*   │
    │  query BOTH:                        │
    │    1. KG_COLLECTION    (per project) │
    │    2. SHARED_KG_COLLECTION (shared)  │
    │  → merge by (title, chunk)          │
    │  → keep best score per key          │
    │  → RL-rerank → auto-tier            │
    └─────────────────────────────────────┘
                   │
                   ▼
          single Weaviate instance
        ┌─────────────┬─────────────┐
        │ A_KG, B_KG, │ VibeCoded   │
        │ C_KG, …     │ Tools_KG    │
        └─────────────┴─────────────┘
```

The shared collection is bootstrapped once per Weaviate instance by `install.py`
Step 7d. Re-running install on an existing system is a no-op (existing-class
detection + per-file upsert in `sync_knowledge_graph.py`).

## Default behaviour

- **Read is unconditional.** Every project tri-write block (VS Code settings,
  `.claude/env`, `.claude/settings.json`) carries
  `SHARED_KG_COLLECTION=VibeCodedTools_KnowledgeGraph` and the MCP server's
  read paths (`hybrid_search`, `semantic_graph_search`) ALWAYS include it.
  This is non-negotiable: knowledge accumulation across projects is the
  headline value prop of the orchestrator.
- **Asymmetric write gate (since 2026-05-01).** Setting
  `SHARED_KG_WRITE_DISABLED=true` refuses `store_knowledge_node(scope="shared")`
  calls from THIS project with a clear error. Reads stay on. Legacy alias
  `SHARED_KG_OPT_OUT` kept for ~3 releases as a write-gate fallback (target
  removal: 2026-08); pre-2026-05-01 the same flag also zeroed reads — that
  behaviour was retired with the rename.

## Reading from the shared KG

Nothing to do. `hybrid_search`, `semantic_graph_search`, `kg-search`, and
`code-graph-query` all transparently include the shared collection in their
search scope when configured. Results carry a `collection` field (e.g.
`VibeCodedTools_KnowledgeGraph` vs `MyProject`) so callers can disambiguate
when needed.

## Writing to the shared KG

`store_knowledge_node` accepts a `scope` parameter:

- `scope="project"` (default) — writes go to the per-project KG. Use for
  project-specific decisions, ad-hoc notes, or anything that wouldn't make
  sense to other projects.
- `scope="shared"` — writes go to the shared KG. Use when capturing a
  pattern, technique, or reference that belongs to the public knowledge
  base (`vibecoded-orchestrator/knowledge/`).

The shared scope is intentionally **not** the default. Letting arbitrary
projects write to the shared collection by default would pollute it with
noise. Power-users / orchestrator maintainers explicitly set `scope="shared"`
when contributing.

## Sidecar resolution

The auto-tier retrieval system uses `.node_formats.json` sidecars (LLM-generated
descriptions, summaries, chunk maps). With two collections, we now have two
sidecar files:

| Collection                       | Sidecar                                                                     |
|----------------------------------|-----------------------------------------------------------------------------|
| `KG_COLLECTION` (per-project)    | `$KG_BASE_DIR/knowledge/.node_formats.json` (or `cwd/knowledge/...`)         |
| `VibeCodedTools_KnowledgeGraph`  | `<orchestrator>/knowledge/.node_formats.json` (override via `SHARED_KG_NODE_FORMATS`) |

`_format_result_by_tier` reads the result's `collection` field and routes the
sidecar lookup accordingly via `_load_node_formats_for_collection(name)`.
Per-collection caching (one read per collection per process) keeps the cost
flat.

## Integration with auto-tier

The shared KG flows through the same 5-tier retrieval pipeline as the project
KG (`discard / summary / single_chunk / three_chunks / full`). High-relevance
shared-KG hits get rich content; low-relevance hits get the LLM summary.
Threshold env vars (`KG_TIER_MIN`, `KG_TIER_SINGLE_CHUNK`, etc.) apply
uniformly across both collections.

See `[[relatedTo::score-driven-retrieval-tiers]]` for the tier semantics.

## Failure modes

- **Shared collection missing** — `_ensure_collections` recreates it on next
  install. Until then, queries return empty from the shared side; project KG
  results still work.
- **Sidecar missing** — `_load_node_formats_for_collection` returns `{}`;
  the `summary` tier falls back to a 200-char content snippet. No crash.
- **Opt-out flag flipped post-create** — restart the MCP server (it reads env
  at startup). VS Code reload re-spawns the server.

## Why install never auto-adopts a foreign shared KG

The installer's content-based service detection can find an existing Weaviate
collection that looks like a shared KG (e.g. `ClaudeKnowledgeGraph` from a prior
Claude orchestrator install on the same machine). The installer **does not
auto-adopt** it. Reason: the orchestrator's `sync_knowledge_graph.py` runs an
**orphan-prune** pass that deletes Weaviate entries whose `file_path` no longer
exists under the active project's `knowledge/` dir. Two installs sharing one
collection would silently delete each other's nodes on the next sync.

vco always creates its own `VibeCodedTools_KnowledgeGraph` (or skips creation if
that exact name already exists). Power-users who really want to share across
machines can override `SHARED_KG_COLLECTION` per-project to point at their own
team-shared name; they accept responsibility for managing the orphan-prune
collision themselves (typically by disabling orphan-prune for that collection
or by ensuring all participating projects sync from a common source-of-truth
folder).

## Why one shared collection (not many)

We considered per-team / per-org shared collections. Decision: **one
collection, one canonical name**, until we have a real multi-tenant story.
Reasons:

1. Simpler tri-write — projects don't have to know which shared collection
   they belong to.
2. Easier to seed at install time — fixed name = idempotent bootstrap.
3. Power users can override `SHARED_KG_COLLECTION` to point at a private
   team-shared collection (`AcmeTeam_SharedKG`); the MCP layer is generic.

The `kg.rs::is_shared` heuristic recognises both the canonical name and any
collection containing the substring "shared", so custom names get the same
"shared first" UI sort order.

## See also

- `[[relatedTo::score-driven-retrieval-tiers]]` — tier system used for both collections
- `[[uses::weaviate-knowledge-graph]]` — underlying vector store
- `[[implements::dual-collection-merge-pattern]]` — search-time merge approach
