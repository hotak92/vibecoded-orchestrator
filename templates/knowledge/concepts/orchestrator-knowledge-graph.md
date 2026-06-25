---
title: Orchestrator Knowledge Graph
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, knowledge-graph, weaviate, semantic-search]
created: 2026-04-27T18:30:00Z
updated: 2026-06-25T00:00:00Z
status: active
---

# Orchestrator Knowledge Graph

The Knowledge Graph (KG) is the orchestrator's long-term memory. It stores cross-project patterns, architectural decisions, tool documentation, and research findings as Obsidian-style markdown files, with automatic synchronization to a Weaviate vector database for semantic search.

[[implements::Knowledge Graph]] [[uses::Weaviate]] [[uses::Ollama]] [[relatedTo::Orchestrator MCP Servers]] [[relatedTo::Orchestrator Hook System]]

## Overview

The KG serves two audiences simultaneously:

1. **Humans**: browse and edit `.md` files in `knowledge/` using any editor (Obsidian, VS Code).
2. **Claude (and any MCP-aware client)**: search via MCP tools (`hybrid_search`, `semantic_graph_search`) before implementing features.

The core principle is **write once, search everywhere** — every architectural decision, gotcha, and pattern is written to the KG so future sessions don't rediscover the same ground.

## File Format

Every KG node is an Obsidian-compatible `.md` file with YAML frontmatter:

```yaml
---
title: Node Title
type: concept           # project | concept | tool | research | model | hardware
tags: [mid-level-architecture, AI, python]
created: 2026-01-15T10:30:00Z
updated: 2026-01-28T14:22:00Z
valid_from: 2026-01-15T00:00:00Z   # optional
valid_until: null                   # optional; set when superseded
status: active                      # active | archived | deprecated | idea
---
```

Required fields are `title`, `type`, `tags`, `created`, `updated`, `status`; `valid_from` / `valid_until` are optional temporal markers. The sync pipeline parses these on every write and treats absent frontmatter as an empty header.

### Typed WikiLinks

```
[[uses::Tool Name]]          — Uses a tool or technology
[[implements::Pattern Name]] — Implements an architectural pattern
[[extends::Parent Node]]     — Specializes or extends a concept
[[buildsOn::Prior Work]]     — Builds upon existing work
[[relatedTo::Other Node]]    — General relationship (default)
```

Links are unidirectional: project nodes link to concept nodes, not the reverse. This keeps the graph acyclic and navigable.

## Folder Structure

```
knowledge/
├── projects/       — Project-specific overviews (one node per managed project)
├── concepts/       — Architectural patterns, algorithms, techniques (largest folder)
├── tools/          — Tool documentation (MCP servers, CLI tools, libraries)
├── models/         — AI model profiles
├── hardware/       — Hardware specs (GPU, CPU, memory configurations)
├── research/       — Research papers, findings, experimental results
└── coordination/   — Team decisions, cross-project agreements
```

Supporting files at `knowledge/` root:
- `TAG_HIERARCHY.md` — canonical tag list with hierarchy (prevents tag fragmentation)
- `VOCABULARY.md` — project-specific terminology definitions
- `.node_formats.json` — auto-generated sidecar with per-node titles, descriptions, summaries (see [[KG-Summary Three-Tier Generation Pipeline]])

## Sync Pipeline

When a KG file is edited, the PostToolUse hook triggers synchronization:

```
File edited in knowledge/
        |
[PostToolUse hook: KG auto-sync]
        |
        v
sync_knowledge_graph.py
        |
        +-- Parse YAML frontmatter (title, type, tags, links, dates, status)
        +-- Extract typed WikiLinks → stored as structured graph edges
        +-- Token-based chunking (1500 target / 2000 max tokens, sentence boundaries)
        +-- Embed via Ollama (qwen3-embedding:0.6b by default, 1024 dimensions)
        +-- Upsert to Weaviate KG collection (skip if content identical)
```

The `--all` flag on `kg-sync` processes every file in `knowledge/` — used for initial setup or after bulk edits.

## Weaviate Collection Schema

Default collection name: `<ProjectBasename>_KnowledgeGraph` (e.g. `Myapp_KnowledgeGraph`). The shared cross-project collection is `VibeCodedOrchestrator_KnowledgeGraph`, set as `SHARED_KG_COLLECTION` in the project env. Two legacy aliases (`VibecodedOrchestrator_KnowledgeGraph` and an earlier `VibeCodedTools_KnowledgeGraph`) are still recognised so installs that created data under those names keep matching.

**Schema invariant**: both the KG and Development collections are created with `indexNullState=True`. This is required for `valid_until is_none OR > now` temporal filters to work correctly — without it, Weaviate cannot index null values and filters that test for `null` return no results.

| Property | Type | Notes |
|---|---|---|
| title | text | Node title, used in search |
| content | text | Full markdown content |
| file_path | text | Relative path from project root |
| node_type | text | project/concept/tool/research/model/hardware |
| tags | text[] | All YAML tags |
| links | text[] | All WikiLink targets (untyped) |
| typed_links | text[] | `"type::target"` format |
| created_at | date | From YAML frontmatter |
| updated_at | date | From YAML frontmatter |
| valid_from | date | Temporal validity start |
| valid_until | date | Temporal validity end (null = still valid) |
| status | text | active/archived/deprecated/idea |

Each KG entry carries multiple named vectors (active embedding + legacy slot) so embedding-model migrations don't force a re-embed of every entry. See [[Qwen3 Embedding]] and [[Snowflake Arctic Embed 2.0]].

## Search Interface

### Keyword Search (CLI)

```bash
.claude/scripts/kg-search search "error handling" --type concept
.claude/scripts/kg-search search "FastAPI" --tags python
.claude/scripts/kg-search list --days 7       # Recently updated
.claude/scripts/kg-info info "Node Title"     # Full node details
.claude/scripts/kg-info connections "Title"   # Graph neighbors
```

Speed: ~100ms. Best for known exact terms.

### Semantic Search (MCP)

```python
hybrid_search("authentication patterns for web APIs")
# Combines BM25 keyword + vector similarity
# Searches per-project KG + shared KG + development docs simultaneously
# Default detail="auto": verbosity per result is chosen from its score
```

Parameters:
- `detail`: `"auto"` (default — per-result score tiering) | `"titles"` | `"summary"` | `"single_chunk"` | `"three_chunks"` | `"full"`. In auto mode, score < 0.42 is discarded; higher-scoring results render richer (summary → single_chunk → three_chunks → full at ≥ 0.75).
- `node_type`: filter to specific type (concept, tool, etc.)
- `tags`: filter by tag list
- `days`: recency filter
- `limit`: max results (default 5)

```python
semantic_graph_search("authentication patterns", depth=2)
# GraphRAG: traverses WikiLinks from seed results
# Returns connected subgraph of related nodes
```

Search results pass through an optional RL reranker before return — see [[Orchestrator RL Retrieval]].

## Programmatic Writes

The `store_knowledge_node` MCP tool writes nodes programmatically (used by agents that can't write files directly):

```python
store_knowledge_node(
    title="My Pattern",
    content="# My Pattern\n...",
    node_type="concept",
    tags=["mid-level-architecture"],
    links=["relatedTo::Some Other Node"],   # typed WikiLinks
    file_path="knowledge/concepts/my-pattern.md",
    scope="project"  # "project" (default) or "shared"
)
```

File path resolution:
1. Absolute path → written directly.
2. Relative + `KG_BASE_DIR` set → `KG_BASE_DIR/file_path`.
3. Relative + no `KG_BASE_DIR` → inferred project root.

**Subagent caveat**: subagents spawned via the Agent tool may inherit a different MCP config pointing to a different project's collection. Always pass an absolute `file_path` to ensure the file lands in the correct `knowledge/` folder.

Preferred workflow: write `.md` file directly → PostToolUse hook auto-syncs. `store_knowledge_node` is a secondary path.

## Maintenance

**Integrity checks** (`maintain_knowledge_graph.py`):
- Detects broken WikiLinks (references to non-existent nodes).
- Finds nodes missing required frontmatter fields.
- Checks tag-vocabulary violations.
- Reports orphaned nodes (no incoming or outgoing links).

**Duplicate detection** (`kg-duplicates`):
- Runs every 10 file edits (triggered by PostToolUse hook).
- Cosine similarity on embeddings; default threshold 0.95.
- Reports candidates but does not auto-merge.

## Integration Points

- **Hook system**: PostToolUse triggers sync on every KG file edit.
- **MCP servers**: the `weaviate-kg` server exposes all search tools.
- **Code graph**: separate collections in the same Weaviate instance.
- **RL retrieval**: search results pass through RL reranking before returning to Claude — see [[Orchestrator RL Retrieval]].
