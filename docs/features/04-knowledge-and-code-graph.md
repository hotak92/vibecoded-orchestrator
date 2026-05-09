# Knowledge Graph & Code Graph

The semantic memory layer: an Obsidian-style knowledge graph (`knowledge/`), a five-collection code graph (AST + optional Joern), the Weaviate schemas behind both, the embedding pipeline (text and code), and the shell scripts that let Claude — and human adopters — drive all of it from the CLI. Source mostly in `claude_mcp_servers/weaviate_mcp/`, `.claude/scripts/`, and `knowledge/`.

The OSS bundle ships seven canonical node types (`project`, `concept`, `tool`, `model`, `hardware`, `research`, `coordination`), but seed content only populates four folders (`concepts/`, `models/`, `tools/`, `patterns/`) — the rest ship empty for adopters to fill in as they author. `patterns/` is a content category, not a node type; pattern nodes carry `type: concept` in frontmatter. See the full reconciliation in [Bundled Seed Content](#knowledge-graph-bundled-seed-content) below.

For MCP tools that query this layer → see [02-mcps-and-agents.md](02-mcps-and-agents.md). For CLI scripts → also [02-mcps-and-agents.md](02-mcps-and-agents.md#infrastructure-scripts).

---

## Knowledge Graph: Node Format

KG nodes are plain Markdown — no proprietary database, no special editor required. Anything that reads `.md` reads them; anything that writes `.md` writes them. Weaviate is the search index, not the source of truth.

### Obsidian-style Markdown with YAML frontmatter
Every KG node is a plain `.md` file with a YAML frontmatter block. Fields: `title`, `type`, `tags`, `created`, `updated`, `valid_from`, `valid_until`, `status`. Readable and editable without any tooling.

### Node types (seven canonical)
`project`, `concept`, `tool`, `model`, `hardware`, `research`, `coordination` — each mapped to a `knowledge/` subfolder by `_NODE_TYPE_TO_FOLDER` in `server.py`. When `store_knowledge_node` is called without a `file_path`, the path is auto-derived from `node_type` + title slug (logic in `_normalize_kg_file_path`).

### Typed WikiLinks
Relationships expressed inline as `[[relationshipType::Target]]`. Supported types: `uses`, `implements`, `extends`, `buildsOn`, `relatedTo` (default for untyped `[[Target]]`). Parsed by `sync_knowledge_graph.py` and stored in the `typed_links` property in Weaviate.

### `valid_from` / `valid_until` temporal metadata
Optional frontmatter fields for point-in-time validity. Parsed from ISO 8601 or `YYYY-MM-DD` strings. Stored as `DataType.DATE` in Weaviate; queryable via `query_temporal.py`.

### `status` field lifecycle
Four values: `active`, `archived`, `deprecated`, `idea`. Queryable as a Weaviate filter. Deprecated nodes can carry a `replacedBy::` WikiLink.

### Node size conventions
Three tiers: high-level (broad overviews, <300 lines), mid-level (specific domains, <200 lines), low-level (individual tools/models, <150 lines). Convention only — not enforced by tooling.

### Large-node chunking
Nodes exceeding ~2500 tokens are automatically split into chunks by `Chunker` (`chunking.py`) before embedding. Chunks share a `source_id` and are reassembled at search time.

<details>
<summary>Details</summary>

The working limit is 2500 tokens (`MAX_EMBEDDING_TOKENS = 2500` in `sync_knowledge_graph.py`), conservative relative to the model's 8k spec. `TokenCounter` uses `langchain_ollama.ChatOllama` for accurate counting, falling back to character approximation. Chunks are stored as separate Weaviate objects; `search_knowledge.py` deduplicates them by `source_id` on retrieval.

</details>

---

## Knowledge Graph: Bundled Seed Content

### 64 seed nodes total — 48 concepts / 6 models / 9 tools / 1 pattern
A new install ships with 64 pre-authored `.md` nodes under `knowledge/`. Adopters get a semantic retrieval base on day one rather than an empty graph. Counts verified against the OSS bundle at v0.1.0: `ls knowledge/concepts/ | wc -l` → 48, `ls knowledge/models/` → 6, `ls knowledge/tools/` → 9, `ls knowledge/patterns/` → 1. The other three canonical node-type folders (`hardware/`, `research/`, `coordination/`) ship empty for the adopter to populate.

### 48 concept nodes
`knowledge/concepts/` covers AI/ML and orchestration topics, grouped roughly:
- **RL & alignment** — actor-critic, DPO, PPO, reward shaping, reinforcement learning, constitutional AI, LLM alignment.
- **Retrieval & inference** — RAG, GraphRAG, hybrid search, semantic search, chunking strategies for LLM RAG, structured output, speculative decoding, LLM inference optimization, model quantization.
- **Architecture topics** — transformer architecture, FlashAttention, LoRA, RoPE, sparse activation, fine-tuning techniques.
- **Reasoning & multimodal** — tree-of-thought, agentic LLM workflows, vision-language models, neurosymbolic AI, Bayesian inference.
- **Coordination & infrastructure** — agent orchestration, MCP architecture, blackboard coordination, RAFT consensus, AI infrastructure budget configurations.

### 6 model nodes
`knowledge/models/` covers the models the orchestrator actually uses: `qwen3.5` (text + vision), `gemma4-e4b` (low-VRAM fallback), `qwen3-embedding` (text embeddings, primary), `snowflake-arctic-embed2` (legacy text embeddings), `codesage-large-v2` (code embeddings, GPU), `jina-embeddings-v2-base-code` (code embeddings, CPU fallback).

### 9 tool nodes
`knowledge/tools/` covers: `weaviate` (vector DB), `weaviate-usage-patterns` (collection design), `ollama` (local LLM server), `ollama-mcp-server` (MCP wrapper), `fastapi` (Python web framework), `fastmcp` (MCP boilerplate), `llama-cpp` (GGUF inference), `claude-code-cli-headless` (programmatic CLI), `claude-code-mcp-configuration` (MCP config patterns).

### 1 pattern node
`knowledge/patterns/` ships `prompt-engineering-2026` (Claude 4.x-era prompt patterns).

### TAG_HIERARCHY.md
Formal 5-level tag taxonomy at `knowledge/TAG_HIERARCHY.md`: domain tags (`#AI`, `#database`, `#workflow`, …), abstraction level tags (`#high-level-plan`, `#mid-level-architecture`, `#low-level-implementation`, `#function-description`), technology tags, status tags (`#idea → #in-progress → #implemented → #tested → #deployed → #archived → #deprecated`), and pattern tags (`#RAG`, `#MCP`, etc.).

### VOCABULARY.md
Formal RDF-inspired vocabulary at `knowledge/VOCABULARY.md` defining namespaces (`co:`, `rdf:`, `skos:`, etc.) and canonical class definitions (`co:Project`, `co:Concept`, `co:Tool`, `co:Model`, etc.) for consistent node authoring.

---

## Weaviate Collections: Knowledge Graph

The Weaviate side is intentionally simple: one main KG collection per project, plus optional `SHARED_KG_COLLECTION` and `DEVELOPMENT_COLLECTION` collections that `hybrid_search` fans out to automatically. Per-project routing happens through env vars (`KG_COLLECTION`, etc.), so the same MCP server config works across every project on the machine.

### `ClaudeKnowledgeGraph` collection
Main KG collection. Properties: `title`, `content`, `file_path`, `node_type`, `tags` (text array), `links` (text array, untyped WikiLinks), `typed_links` (nested objects: `{relation, target}`), `created_at`, `updated_at`, `valid_from`, `valid_until`, `status`. Named vectors: `qwen3_embed` (1024d, active default), `ollama_embed` (1024d, legacy snowflake-arctic-embed2, preserved), `openai_embed` (1536d).

### `SHARED_KG_COLLECTION` (optional)
Second KG collection searched transparently alongside `KG_COLLECTION`. Set via env var. Used for cross-project shared knowledge when running multiple Claude Orchestrator instances.

### `DEVELOPMENT_COLLECTION` (project docs)
Per-project verbose documentation collection (e.g. `ClaudeOrchestrator_development`). Set via env var. `hybrid_search` fans out to it automatically alongside the KG collections — agents don't need to specify which collection to search.

**Schema-paired with KG**: same chunker, same three named-vector slots (`qwen3_embed` / `ollama_embed` / `openai_embed`), same `inverted_index_config(index_null_state=True)`. Schema is a strict subset — drops the KG-specific fields (`tags`, `links`, `typed_links`, `external_links`, `node_type`, `status`) since docs typically have no frontmatter. The launcher auto-pairs KG and dev collections per project: assigning KG access on a project automatically assigns dev access.

**`semantic_graph_search` excludes the dev collection** since docs have no WikiLinks — graph traversal can't find useful neighbours there. `hybrid_search` does include it.

**Sync behaviour** (`.claude/scripts/sync_knowledge_graph.py`):
- `docs/*.md` edits route to `sync_doc()` (dev collection)
- `knowledge/*.md` edits route to `sync_node()` (KG collection)
- Files under any `archive/` directory or with frontmatter `status: archived`/`deprecated` are skipped on sync AND removed from Weaviate if they were previously indexed.
- The `--all-docs` CLI flag bootstraps the dev collection in one pass (used at install time).

### `DocumentChunks` collection
Stores chunks from files dropped in `documents/`. Created on demand by `process_documents.py`. Properties: `content`, `chunk_number`, `total_chunks`, `token_count`, `source_id`, `metadata_json`, `created_at`.

### Multi-collection fan-out in `hybrid_search`
`hybrid_search` searches `KG_COLLECTION`, `SHARED_KG_COLLECTION` (if set), and `DEVELOPMENT_COLLECTION` (if set) in parallel, merges results, and deduplicates by `file_path`. Agent callers never need to manage routing.

### Per-project KG routing via `KG_COLLECTION` env var
Each VS Code workspace overrides `KG_COLLECTION` in `.vscode/settings.json`. Opening a different project workspace redirects all KG reads/writes to that project's collection without code changes.

---

## Weaviate Collections: Code Graph

Five collections, populated by `analyze_code_graph.py`: modules, classes, functions, API endpoints, and cross-service interactions. UUIDs are deterministic (`uuid5(NAMESPACE_DNS, f"{project}::{full_name}")`), so re-analyzing a file upserts cleanly rather than producing duplicates. Collection names are prefixed with the project name, so multiple projects can share one Weaviate instance without collision.

### `CodeModule` collection
One object per source file. Properties: `path`, `language`, `module_summary`, `imports` (text array), `import_names` (text array, added via schema migration if missing), `loc` (line count), `complexity`. Prefixed per project: e.g. `MyProject_CodeModule`.

### `CodeClass` collection
One object per class/struct. Properties: `name`, `full_name`, `class_body`, `methods` (text array), `extends` (text array, base class names), `field_types` (text array), `composes` (text array, composition relationships).

### `CodeFunction` collection
One object per function/method. Properties: `name`, `full_name`, `function_body`, `signature`, `calls` (text array, resolved to other `full_name`s), `type_uses` (text array), `cfg_summary` (control-flow summary from Joern, `skip_vectorization=True`), `data_flow_vars` (variable names from Joern PDG, `skip_vectorization=True`).

### `CodeAPI` collection
One object per inbound API endpoint. Properties: `endpoint`, `method` (HTTP verb), `api_description`, `handler` (function full_name). Populated for Flask, FastAPI, Express, ASP.NET (HTTP attribute annotations), gRPC service RPC methods, and Fastify routes.

### `CodeInteraction` collection
One object per detected outbound cross-service call. Properties: `interaction_type` (http/grpc/mq/websocket), `protocol`, `endpoint`, `direction`, `confidence` (high/medium), `raw_target`, plus references to source `CodeModule` and `CodeFunction` UUIDs.

### Per-project collection prefixing
All five code collections are prefixed with a sanitized project name (e.g. `VibecodeOrchestrator_CodeFunction`). `_sanitize_collection_prefix` ensures names are alphanumeric+underscore starting with uppercase. Bare base names (no prefix) are used as fallback when `project_name` is empty.

### Deterministic UUIDs for upserts
Entity UUIDs are generated as `uuid5(NAMESPACE_DNS, f"{project}::{full_name}")`. Re-analyzing the same file produces the same UUID, turning all inserts into Weaviate upserts and preventing duplicates.

---

## Code Graph Analysis

### `analyze_code_graph.py` — main analysis script
Full AST/regex code extraction engine at `.claude/scripts/analyze_code_graph.py`. Accepts a repo path and populates all five code graph collections.

### Language support
Python (full fidelity via `ast` module), plus regex-based extraction for: Lua, C++/C, JavaScript, TypeScript, JSX, Go, Rust, Java, Ruby, Shell.

### Cross-service interaction extraction
`_extract_external_calls` detects outbound HTTP (requests, httpx, aiohttp, axios, curl/wget), gRPC, message queue (Kafka, RabbitMQ, Redis pub/sub), and WebSocket calls. Three-gate false-positive filter: (1) import gate — only triggers if relevant library is imported; (2) literal gate — only extracts calls with a string literal target; (3) scope gate — strips triple-quoted strings to ignore URLs in docstrings.

### Schema migration for `CodeModule.import_names`
`ensure_schema_migration` adds the `import_names` property to `CodeModule` if created before this property was introduced. Non-destructive; skips if already present.

### `query_code_graph.py` — structural + semantic queries
Backend for code graph queries. Subcommands: `search "<concept>"`, `similar "<full_name>"`, `structure dependencies|callers|methods|extends|interactions "<target>"`.

---

## Embeddings

Two embedding stacks: text (KG, docs) and code (code-graph entities). Each has a primary, a legacy fallback for older installs that haven't re-indexed, and an optional OpenAI path. Switching between primary and legacy is a one-env-var change (`ACTIVE_EMBEDDING`) — no re-indexing required as long as both named vectors are populated.

### Primary text embedding: `qwen3-embedding:0.6b` via Ollama
1024-dimensional vectors stored under named vector `qwen3_embed`. Default for all KG and development collection searches. Model served by Ollama at `http://localhost:11435` (`OLLAMA_URL`). `num_ctx=8192` required (set by the MCP server at embedding time).

### Legacy text embedding: `snowflake-arctic-embed2` (preserved)
1024-dimensional vectors under named vector `ollama_embed`. Kept populated for backward compatibility; allows switching `ACTIVE_EMBEDDING` back to `"ollama"` without re-indexing.

### OpenAI text embedding: `text-embedding-3-small` (optional)
1536-dimensional vectors under named vector `openai_embed`. Only populated when `DUAL_EMBEDDING_ENABLED=true` and `OPENAI_API_KEY` is set.

### Primary code embedding: `CodeSage-Large-v2` via FastAPI service
2048-dimensional vectors under named vector `codesage_embed`. GPU-accelerated via the code embedding service at `http://localhost:11440` (`CODE_EMBED_SERVICE_URL`). Default for all code graph searches.

### Legacy code embedding: `jina-embeddings-v2-base-code` via Ollama (preserved)
768-dimensional vectors under named vector `ollama_code_embed`. Preserved for backward compatibility; CPU fallback path.

### `ACTIVE_EMBEDDING` env var
Controls which named vector is used for search queries. KG values: `"qwen3"` (default → `qwen3_embed`), `"openai"` (→ `openai_embed`), any other value (legacy → `ollama_embed`). Code values follow the same pattern: `"codesage"` (default → `codesage_embed`), `"openai"`, otherwise → `ollama_code_embed`. Switch without re-indexing as long as the target slot is populated.

### `DUAL_EMBEDDING_ENABLED` env var
When `true` (default for fresh installs), objects are stored with all named vectors populated simultaneously. Existing collections need migration before enabling.

### Smart code truncation (`code_truncation.py`)
Truncates code before embedding using priority order: (1) signature always included, (2) docstring/leading comment always included, (3) method/field names for classes, (4) body truncated at statement boundaries. Model-aware token budgets: CodeSage = 2048 tokens (~7168 chars), jina-v2 = 8192 tokens (~28672 chars).

### Text chunking (`chunking.py`)
`Chunker` and `TokenCounter` classes. Accurate token counting via `langchain_ollama.ChatOllama`; character approximation fallback. Produces `Chunk` / `DocumentChunk` dataclasses with `chunk_number`, `total_chunks`, `token_count`, `source_id`, `metadata`. Chunk target size: 800–2000 tokens.

---

## Knowledge Graph: Maintenance Scripts

### `sync_knowledge_graph.py`
Core sync backend: parses YAML frontmatter, extracts WikiLinks (typed and untyped), generates embeddings, upserts to Weaviate. Handles schema migration (adds `typed_links` property if missing on existing collections).

### `maintain_knowledge_graph.py`
Integrity checks: orphaned nodes, broken WikiLinks, nodes missing required frontmatter fields.

### `add_temporal_metadata.py`
Backfills `valid_from` / `created` / `updated` fields in YAML frontmatter from `git log` history. Falls back to filesystem timestamps for files not in git.

### `query_temporal.py`
Point-in-time KG queries: retrieve nodes that were `active` at a given timestamp using `valid_from` / `valid_until` Weaviate filters.

### `generate-kg-summary.py`
Generates LLM summaries (Claude Haiku) for KG nodes and stores them in `knowledge/.node_formats.json`. For multi-chunk nodes, generates both whole-node and per-chunk summaries. Content hash prevents regeneration when content is unchanged.

### `.node_formats.json` sidecar
JSON file at `knowledge/.node_formats.json`. Maps `file_path → {title, description, summary, chunk_summaries, total_chunks, generated_at, content_hash}`. Used by `hybrid_search`'s `detail` parameter: `"titles"` skips it, `"descriptions"` returns the 3-4 sentence description, `"full"` returns up to 300 chars of raw content.

### `detect_duplicates.py`
Semantic duplicate detection with configurable threshold (`DEFAULT_SIMILARITY_THRESHOLD = 0.95`). Three detection methods: semantic similarity (Weaviate vector distance), Levenshtein distance on filenames, title substring matching. `--auto-merge` for high-confidence cases.

### `migrate_to_vocabulary.py`
Validates and auto-corrects node frontmatter against the formal vocabulary: canonical `type` values, required fields, tag format (hyphens, lowercase, UPPERCASE acronyms). Invoked by `kg-migrate` wrapper.

---

## Document Ingestion

### `documents/` folder ingestion script
Files placed in `documents/` (markdown or PDF) can be ingested via `python .claude/scripts/process_documents.py <file>` or `--all`. There is no auto-trigger hook in the default `.claude/settings.json`; previous versions wired a `PostToolUse Write(documents/**)` hook but that wiring was removed. Run the script manually or wire it in a project-local settings overlay.

### `process_documents.py`
Full ingestion pipeline: (1) chunk content to 800–2000 tokens, (2) store chunks in `DocumentChunks` Weaviate collection, (3) create or update a KG node with a document summary, (4) link the document node to relevant existing nodes via WikiLinks, (5) maintain bidirectional links.

---

## Cross-Cutting Notes

### RL reranking integration (transparent)
`hybrid_search` and `semantic_graph_search` optionally rerank results via an RL server at `RL_SERVER_URL` (default `http://localhost:11439`). When the RL server is unreachable, Weaviate-order top-k is returned unchanged. The over-fetch multiplier (`_RL_OVERFETCH = 2`) fetches 2× the requested limit from Weaviate before passing to the RL ranker.

### Detail expansion logging for RL training
Each `hybrid_search` call logs `{query, detail_level, result_count, rl_bonus}` to `.claude/logs/YYYY-MM-DD_retrieval_expansion.jsonl`. Bonus values: `titles=0.0`, `descriptions=+0.3`, `full=+0.8`. Used to train retrieval expansion behavior.

### `KG_BASE_DIR` env var
When set, all path resolution (node writes, `kg-sync`, hook auto-sync) uses this as the project root. Enables using the same MCP server config across multiple projects — the KG collection in Weaviate is still distinguished by `KG_COLLECTION`.

### `query_logger.py`
Optional query usage logger imported by `search_knowledge.py` and `sync_knowledge_graph.py`. Logs tool invocations to JSONL. Silently skipped if import fails.

### Schema-Creation Gotchas (Weaviate ≤ 1.30)

These collection settings MUST be set at create time — `Reconfigure` cannot toggle them later:

- **`inverted_index_config=Configure.inverted_index(index_null_state=True)`** — required for `Filter.by_property("foo").is_none(True)` queries. Without it, Weaviate rejects with `"Nullstate must be indexed to be filterable!"`. The MCP `_stale_filter` (which excludes nodes whose `valid_until` is past) relies on this.
- **Named-vector slots: declare upfront**. Weaviate ≥ 1.31 supports `Reconfigure.NamedVectors.add()`; ≤ 1.30 (including the 1.28.4 we run by default) does NOT. Strategy: every collection declares **all three slots** (`qwen3_embed`, `ollama_embed`, `openai_embed` for KG; `codesage_embed`, `ollama_code_embed`, `openai_embed` for code-graph). Only the slot matching the active embedding profile is populated; the other two stay empty until the user switches profiles or runs a re-embed pass.
- **Date-property filters**: pass `datetime` objects (not ISO strings) to `Filter.by_property("foo").greater_than()`. The Python client serializes `datetime` to `valueDate`; ISO strings get serialized as `valueText` and Weaviate rejects.

### Embedding-Slot Discipline

Vectors emitted by model X must be stored under the slot whose name maps to X. Never cross-write — the slot name is its semantic contract. A vector produced by qwen3-embedding stored under `ollama_embed` (which is labelled for arctic) leads to silent retrieval-quality regressions when the user switches `ACTIVE_EMBEDDING`.

Enforcement: `_active_named_vector_for_kg()` in `sync_knowledge_graph.py` asserts `ACTIVE_EMBEDDING in ("qwen3", "codesage")` and refuses to run otherwise. For multi-slot writes (e.g. populating arctic + qwen3 + openai simultaneously), use the `store_knowledge_node` MCP tool — it dispatches to the right model per slot via `_get_all_kg_embeddings`.

### Cross-OS install support

`install.py` selects an embedding profile based on detected hardware:
- **GPU profile** (≥ 7.5 GB VRAM, AMD ROCm or NVIDIA CUDA detected): `qwen3-embedding:0.6b` (text) + CodeSage-Large-v2 (code, GPU service)
- **CPU profile** (no GPU): `qwen3-embedding:0.6b` (text) + jina-embeddings-v2-base-code (code, CPU via Ollama)
- **OpenAI profile** (`--openai-key`): `text-embedding-3-small` for both text and code
- **Low-resource profile** (`--low-resource` opt-in): snowflake-arctic-embed2 (text) + jina-v2-base-code (code)

Each profile sets `ACTIVE_EMBEDDING` to the corresponding identifier (`qwen3` / `qwen3` / `openai` / `arctic`). The MCP server reads `ACTIVE_EMBEDDING` to pick the named-vector slot for queries — automatic per-machine selection. (`arctic` is not a directly-recognized branch in the MCP — it routes via the negation branch to `ollama_embed`; see [§ACTIVE_EMBEDDING env var](#active_embedding-env-var).)

To switch profile post-install: drop the affected collections and re-ingest with the new profile, OR upgrade Weaviate to ≥ 1.31 and use `Reconfigure.NamedVectors.add()`.
