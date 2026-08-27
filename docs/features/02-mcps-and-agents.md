# MCP Servers & Infrastructure Scripts

Five MCP servers ship with VCO by default:

- `weaviate-kg` — semantic + graph search over the knowledge graph, project docs, and code graph (Python, `claude_mcp_servers/weaviate_mcp/server.py`). **Enabled per project by default.**
- `search` — academic-paper search via OpenAlex + arXiv (Python, `claude_mcp_servers/search_mcp/`). **Enabled per project by default.**
- `mermaid` — diagram describe/extract for Mermaid (`.mmd`) sources (Python wrapper around `vco_lib/mermaid_mcp_fork/`). **Registered but project-default-disabled** via `BUNDLED_MCP_DEFAULT_DISABLED` in `launcher/src-tauri/vct-launcher-core/src/db/project_mcp_servers.rs`. `claude mcp list` shows it Connected, but its tools are not callable until the user enables it in the launcher's Diagrams tab. Since v0.2.91 every seeding path applies that rule through one shared DB helper, so the claim holds for the orchestrator root too.
- `excalidraw` — diagram describe/extract for Excalidraw sources (Python wrapper around `vco_lib/excalidraw_mcp_fork/`). **Registered but project-default-disabled** — same opt-in path as `mermaid`.
- `playwright` — browser automation via `npx -y @playwright/mcp@latest`. **Enabled** by default; opt out with `VCT_SKIP_PLAYWRIGHT=1`.

Authoritative writer for the four Python MCPs: `launcher/src-tauri/src/mcp_registration.rs::build_default_mcp_entries`. Pure-Python fallback for installs that bypass the launcher: `install.py:20654-20661`. Playwright is invoked separately via `_install_playwright_browsers` (`install.py:24784-24858`) so users without npx still get the other four MCPs.

The Python MCPs run from `claude_mcp_servers/.venv`. Pro-tier MCPs are excluded from the default install — see `mcp_registration.rs:16` for the rationale. The `mermaid` + `excalidraw` default-disabled list in `project_mcp_servers.rs` keeps the per-project tool surface narrow for users who don't author diagrams; the GUI toggle flips them on without re-running install.py.

### Wrapper-MCP spawn shape and `PYTHONPATH` (v0.2.91)

`mermaid` and `excalidraw` are the only entries invoked as a **module** rather than a script: `<venv-python> -m claude_mcp_servers.wrappers.<proxy>`. Resolving that dotted name needs the package's PARENT on `sys.path`, so their registered `env.PYTHONPATH` is `<install_root><pathsep><install_root>/claude_mcp_servers` — both roots, install root first. The absolute-script entries (`weaviate-kg`, `search`) keep the package-internal path alone, because they import their neighbours as top-level modules.

Through v0.2.90 the wrapper entries carried only the package-internal path, and the sole thing making them work was `python -m`'s implicit cwd-prepend. Claude Code spawns stdio MCPs with cwd = the **session's project directory** and `~/.claude.json` is global, so the entries resolved for the orchestrator root and died with `ModuleNotFoundError: No module named 'claude_mcp_servers'` (rc=1) in every other project — the long-standing mermaid/excalidraw "Failed to connect". The failure happens during `-m` module resolution, before any package code runs, so the proxies' own script-mode import fallbacks could never help. Regression-pinned by `tests/test_wrapper_mcp_cwd_independence_v0291.py`, which spawns the built entry from a temp cwd (a shape test on the env string cannot catch this).

### Per-project MCP rows and the convergence pass (v0.2.91)

`project_mcp_servers` in `launcher.db` is the per-project catalog behind the launcher's MCP surfaces. Two things keep it current:

- `populate_project_state_from_filesystem` stays a **pure disk mirror** of `.claude/settings.json::mcpServers` + `.mcp.json`. It does not know the default set — and must not, or it would re-mirror a stale legacy `settings.json` back in.
- The **convergence engine** (`launcher/src-tauri/src/commands/convergence.rs`) owns catalog alignment. It runs once per launcher boot (all projects) and again after each per-project bundle update (that project only — the other moment the shipped default set can move under a project). It seeds any missing entry from `[entries].default_names` (fresh inserts honouring `[bundled].default_disabled`), and retires rows whose MCP left the default set (`[deprecated.*]` in `vco_lib/mcp_scan_rules.toml`) by disabling them and stamping a "retired in vX.Y.Z: reason" badge into the row's `config_json`. It **never deletes** a row: actual removal stays behind install.py's consent-gated `--remove-deprecated-mcps`.

Two invariants bound every write: **provenance wins** (a `is_user_added = 1` / `source = "user"` row is never touched, and an explicit disable is never reverted — seeding is insert-if-absent, so existing rows are not even re-stamped), and **positive evidence only** (if a project's rows cannot be READ, the pass does nothing for that project — a failed read is not evidence of an empty catalog). The pass is idempotent, soft-fail, and writes audit rows; when it cannot finish it emits the registered `convergence_pending` deferral and clears it on the next clean pass.

This replaces the migration-010 follow-up backfill that ran at boot from 2026-05-10 to v0.2.90 and converged nothing: it was gated to projects with zero rows (so stale rows were unreachable) and its only action was to re-run the disk mirror (which inserts nothing once bundled MCPs live in the global `~/.claude.json`). `project_mcp_servers` is the engine's first write-enabled tenant; the other declared tenants (`project_backfill`, `codegraph_bindings`, `module_settings`) are report-only this cycle and keep their own reconcilers until v0.2.92.

**Deliberately NOT MCPs**: Ollama runs as infrastructure only (Weaviate text embeddings + code-embedding CPU fallback) — there is no Ollama MCP; Claude's native reasoning, `Read` tool, and built-in vision serve chat, document-reading, and image use cases at higher quality. The Search MCP exposes `search_papers` only — Claude's built-in WebFetch covers ad-hoc web retrieval, and no local search proxy runs in the default container stack.

For agents, skills, and hooks built on top of these MCPs → see [03-agents-skills-hooks.md](03-agents-skills-hooks.md). For the knowledge graph and code graph data layer → see [04-knowledge-and-code-graph.md](04-knowledge-and-code-graph.md).

---

## MCP: Weaviate-KG (`claude_mcp_servers/weaviate_mcp/server.py`)

Five MCP tools for semantic search over the knowledge graph, project docs, and code graph. Collections are resolved from env vars at call time, so agents never hard-code collection names and the same agent template works across projects.

### `hybrid_search`
Default search tool. Combines vector + BM25 search across project KG, shared KG, and development docs in one call.

<details>
<summary>Details</summary>

Params: `query` (natural language), `limit` (default 5), `node_type` (filter: project/concept/tool/model/hardware/research), `tags` (list filter), `days` (recency filter), `detail` (`"auto"` default | `"titles"` | `"summary"` | `"single_chunk"` | `"three_chunks"` | `"full"`).

The `detail` param controls token cost. `"auto"` picks a verbosity tier per result from its relevance score (higher-scoring results render richer detail — summary → single chunk → three chunks → full node); the explicit values apply one tier uniformly to every result. Summaries are pre-generated and stored in `knowledge/.node_formats.json`, so summary-tier renders are fast and cheap.

Collections searched: `KG_COLLECTION` + `SHARED_KG_COLLECTION` + `DEVELOPMENT_COLLECTION` (all resolved from env, set per-project in `.claude/settings.json` `env` — the canonical channel; `.vscode/settings.json` `claude-code.env` does not propagate to MCP subprocesses on Linux and is not used). Results are deduplicated across collections. RL reranking applies transparently when `RL_SERVER_URL` is reachable (Pro tier only).

</details>

### `semantic_graph_search`
GraphRAG-style search: finds concept matches and walks typed WikiLinks out to related nodes.

<details>
<summary>Details</summary>

Params: `query`, `limit` (default 5), `depth` (default 2, max 3). Returns `primary_results` + `connected_nodes` with relationship types (`uses`, `implements`, `extends`, `buildsOn`, `relatedTo`). Use when you want to understand the neighborhood of a concept, not just find it.

RL reranking applies to primary results (over-fetches by `_RL_OVERFETCH = 2` multiplier before passing to RL server). Falls through cleanly when RL server is absent.

</details>

### `store_knowledge_node`
Upserts a KG `.md` file on disk and syncs to Weaviate. Skips write if content hash matches existing.

<details>
<summary>Details</summary>

Params: `title`, `content`, `node_type`, `tags`, `file_path` (relative or absolute), `scope` (`"project"` default | `"shared"`), and optional temporal metadata (`valid_from`, `valid_until`, `status`).

File path resolution priority: (1) absolute path → written directly; (2) relative + `KG_BASE_DIR` set → `KG_BASE_DIR/file_path`; (3) relative fallback → inferred project root. Always check `file_written: true` and `absolute_path` in the response. Preferred workflow is writing `.md` directly and letting the `post-file-edit.sh` hook sync to Weaviate; this tool is for agents that cannot write files.

</details>

### `search_code_graph`
Semantic search over code entities (functions, classes, modules, APIs, cross-service interactions) by describing what they do in natural language.

<details>
<summary>Details</summary>

Params: `query`, `scope` (`"all"` | `"code"` | `"interaction"`), `limit` (default 8), `expand_hops` (0/1/2 — follow call/interaction edges from seed nodes), `layer` (architectural layer filter: API/Service/Data/UI/Utility), `project` (override workspace default).

Uses CodeSage-Large-v2 embeddings (2048-dim) via the code embedding service (port 11440 by default). Falls back to Ollama jina-v2-base-code when `CODE_EMBED_BACKEND=ollama`. Top 4 results get full details (body + signature); the rest are compact refs. `expand_hops=1` follows outgoing call edges from seeds.

Collections searched: `CodeFunction`, `CodeClass`, `CodeModule`, `CodeAPI`, `CodeInteraction` — all prefixed with the project name when `CODE_GRAPH_PROJECT` is set.

</details>

### `query_code_structure`
Precise structural queries on the code graph by known entity name. Returns exact relationships, not fuzzy matches.

<details>
<summary>Details</summary>

Params: `query_type`, `target`, `project`.

Supported query types:
- `dependencies` — what a module imports
- `imports` — who imports a given module (reverse)
- `callers` — what functions call a given function
- `methods` — methods belonging to a class
- `extends` — inheritance chain for a class
- `interactions` — cross-service HTTP/gRPC calls from a module
- `path` — BFS shortest call path between two functions (`"source.func->dest.func"`, depth limit 6)
- `composes` / `composed_by` — composition relationships
- `type_users` — functions using a given type in annotations

Use `search_code_graph` first to discover entity names, then `query_code_structure` to navigate their exact relationships.

</details>

### RL Reranking (ambient, opt-in)
When `RL_SERVER_URL` is reachable, KG search results pass through a reinforcement-learning reranker before being returned. When it's not, free-tier installs see plain cosine-ordered results — no error, no warning. The RL server is a **Pro-tier component** shipped as a signed binary via CDN; it is not in the OSS bundle.

### v0.2.53: case-rebind for the dev-collection suffix swap (NEW-2)

When the hub derives `DEVELOPMENT_COLLECTION` (resp. `SHARED_KG_COLLECTION`, `DIAGRAMS_COLLECTION`) from the primary KG via suffix-swap (`_KnowledgeGraph` → `_Development` / `_Shared` / `_Diagrams`), the result historically followed the casing of the launcher.db binding row. On installs whose Weaviate on-disk class has a different casing (e.g. lowercase-c `Vibecodedorchestrator_Development` from a pre-canonical install), the hub's reply caused `sync_knowledge_graph.py` to call `.exists()` (case-sensitive) → False → `.create()` → Weaviate refused with "found similar class". The dev-collection case-mismatch symptom (Symptom B) on v0.2.52.

v0.2.53 Track F adds a case-insensitive resolver: `launcher/src-tauri/vct-hub/src/weaviate_schema_probe.rs::resolve_existing_casing_for_class()` probes `GET /v1/schema`, builds a `lowercased → actual` map, and returns the on-disk casing if a sibling exists. Cached for 5 s per `weaviate_url` to keep `/api/v1/projects/{id}/config` resolves flat under burst load. Fail-open: if Weaviate is unreachable the candidate name is returned unchanged. Wired at four config-resolver sites in `vct-hub/src/config_api.rs` (lines 726, 735, 757, 775). Two-layer port: install.py also gained `_resolve_existing_casing` for the install-time path; see [07-architecture.md](07-architecture.md#track-f-two-layer-case-rebind-pattern).

### v0.2.53: `claude_mcp_servers/_lib/update_gate.py` — MCP-side update gate

`claude_mcp_servers/_lib/update_gate.py` (v0.2.52, refreshed v0.2.53) is a deliberate mirror of `vco_lib.update_gate` for use from MCP-server processes. The MCPs cannot import `vco_lib` directly (separate venv, different `sys.path`); the mirror exposes the same `exit_if_update_in_progress()` and `LOCKFILE_BASENAME` constants. Constants are kept in lock-step manually with the comment block at the top calling out the invariant. The canonical implementation stays in `vco_lib.update_gate` (also mirrored in Rust at `launcher/src-tauri/src/commands/update_gate.rs`).

### Env Vars (Weaviate MCP)

| Variable | Default | Purpose |
|---|---|---|
| `KG_COLLECTION` | `ClaudeKnowledgeGraph` | Project KG collection |
| `SHARED_KG_COLLECTION` | `""` | Cross-project shared KG |
| `DEVELOPMENT_COLLECTION` | `""` | Project docs collection |
| `CODE_GRAPH_PROJECT` | `""` | Project name prefix for code collections |
| `ACTIVE_EMBEDDING` | `qwen3` | Which embedding model drives search vectors |
| `RL_SERVER_URL` | `http://localhost:11439` | Optional RL reranking endpoint (Pro) |
| `WEAVIATE_URL` | `http://localhost:8081` | Weaviate HTTP endpoint |
| `OLLAMA_URL` | `http://localhost:11435` | Ollama endpoint for embeddings |

---

## Ollama — infrastructure, not an MCP

Ollama runs as infrastructure, not as an MCP server: it serves `qwen3-embedding:0.6b` for Weaviate text embeddings and acts as a fallback backend for the code-embedding service. KG-summary generation (`generate-kg-summary.py`) can target Ollama directly when the Claude CLI is unavailable. There are no Ollama chat/document/image MCP tools — Claude's own reasoning covers analysis and rewriting, the native `Read` tool with `offset`/`limit` covers large-file extraction, and Claude's built-in vision (pass an image path to `Read`) covers image analysis.

If you need local-LLM inference for a specific use case (e.g., privacy-sensitive processing, offline workflows), Ollama runs at `http://localhost:11435` — interact with it via the standard Ollama REST API or a custom MCP adapter outside of VCO's default stack.

---

## MCP: Search (`claude_mcp_servers/search_mcp/server.py`)

The Search MCP exposes a single tool: `search_papers`. General web search and page fetching are covered by Claude's built-in WebFetch, and in-project code search is covered by the semantic `search_code_graph` tool — so no local search proxy is part of the default container stack.

The `search_papers` tool carries clear value because OpenAlex and arXiv are structured APIs that return citation-rich, date-filtered, deduplicated academic metadata that ad-hoc web search cannot replicate.

### `search_papers`
Search academic papers via OpenAlex (240M works, CC0) or arXiv (CS/ML preprints). Returns structured metadata: title, authors, DOI, abstract excerpt, citation count, publication year.

Params: `query`, `limit` (1-25, default 10), `source` (`"openalex"` default | `"arxiv"`), `year_from`. Set `OPENALEX_EMAIL` env var for polite-pool priority on OpenAlex API. Calls the structured OpenAlex and arXiv HTTP APIs directly — no local search proxy required.

---

## Code Embedding Service (`claude_mcp_servers/code_embedding_service/`)

FastAPI service that produces code embeddings via CodeSage-Large-v2 (1.3B params, 2048-dim, Apache 2.0) — preferentially on GPU, with an Ollama fallback for CPU-only users. Used by the Weaviate MCP for `search_code_graph` queries.

<details>
<summary>Details</summary>

API: `POST /embed {"texts": [...], "is_query": false}` → `{"embeddings": [[...]], "dim": 2048}`. `GET /health` returns status, backend, model, and dim.

Two backends: `gpu` (default, sentence-transformers on CUDA/CPU) and `ollama` (delegates to Ollama, e.g. `jina-embeddings-v2-base-code` for CPU-only users). Backend controlled by `CODE_EMBED_BACKEND` env var. Default port: 11440 (configurable via `CODE_EMBED_PORT`).

The `ensure-code-embed-service.sh` SessionStart hook auto-starts this container if it exists. CPU-only users can leave the `code_embed` service commented out in `compose.yaml` — the MCP falls back to Ollama code embeddings automatically.

Environment vars: `CODE_EMBED_BACKEND`, `CODE_EMBED_MODEL`, `CODE_EMBED_DEVICE`, `CODE_EMBED_DTYPE` (bfloat16 default), `CODE_EMBED_PORT`, `CODE_EMBED_BATCH_SIZE` (32 default), `CODE_EMBED_MAX_CONCURRENT` (4 default).

</details>

---

## MCP: Playwright (`@playwright/mcp` via npx)

Browser automation MCP. Not in `claude_mcp_servers/` — registered against `~/.claude.json` and pre-cached at install time so first browser launch doesn't stall.

Install path: `install.py::_install_playwright_browsers` runs `npx -y @playwright/mcp@latest --version` (caches the package) then `npx playwright install chromium` (fetches the Chromium binary). Skip with `VCT_SKIP_PLAYWRIGHT=1`. Exposed to Claude Code as the `playwright` MCP server (tool prefix `mcp__playwright__browser_*`). Used by the `gui-tester` agent and the `gui-test` skill for visual regression / GUI smoke runs.

Failure modes: `npx` not on PATH → the install step skips with WARN **and the registered MCP cannot spawn at all** (its command string *is* `npx`, so there is nothing to invoke and nothing to lazy-install into — Claude Code reports only "Failed to connect"). Since v0.2.91 that case is no longer silent: the doctor phase at the end of every install/update probes the ladder in `vco_lib/npx_resolver.py`, defers `npx_missing_mcp_unspawnable` into `UPDATE_DEFERRED.md`, and the launcher's MCP-registration badge turns yellow with the same remediation. Chromium fetch timeout (600 s) → install step skips with WARN; that one *is* a genuine lazy-install-later case (the MCP fetches the browser on the first browser call). The orchestrator works without Playwright; only `gui-tester` / `gui-test` go inert.

---

## Infrastructure Scripts

Shell wrappers under `.claude/scripts/` that auto-activate `claude_mcp_servers/.venv` before calling their Python backends. Most have a PowerShell `.ps1` sibling for Windows users.

### `kg-search` CLI
Fast keyword/metadata search over the KG without going through Weaviate MCP.

Usage: `.claude/scripts/kg-search search "<query>" [--type TYPE] [--tags TAGS] [--limit N]`. Also: `list`, `recent`, `created`. Backed by `search_knowledge.py`. Handles chunked nodes (reassembles all chunks from same source).

### `kg-info` CLI
Inspect a single KG node by title: show metadata + connections.

Usage: `.claude/scripts/kg-info info "Node Title"` / `connections "Node Title"`. Backed by `get_node_info.py`.

### `kg-sync` CLI
Manually sync one or all KG `.md` files to Weaviate.

Usage: `.claude/scripts/kg-sync FILE | --all`. Backed by `sync_knowledge_graph.py`. Called automatically by the `PostToolUse` hook on `Edit(knowledge/**/*.md)` and `Write(knowledge/**/*.md)`.

### `kg-duplicates` CLI
Detect near-duplicate KG nodes above a similarity threshold.

Usage: `.claude/scripts/kg-duplicates [--threshold 0.95]`. `--auto-merge` flag merges high-confidence duplicates. Backed by `detect_duplicates.py`. Triggered automatically every 10 edits by the `post-file-edit.sh` hook.

### `kg-infer` CLI (roadmap — not shipped)
Removed from the bundle in v0.2.54 (Track G parity sweep): the wrapper shipped without its backing `infer_knowledge.py` module, so it was broken-on-arrival on every OS. The intent — infer typed WikiLink relationships using a local LLM and apply tag-propagation rules from `TAG_HIERARCHY.md` — remains roadmap; the wrapper returns together with `infer_knowledge.py` when that lands.

### `kg-migrate` CLI
Validate and fix KG nodes against `VOCABULARY.md` and `TAG_HIERARCHY.md`. Flags: `--check`, `--fix`, `--interactive`, `--file <path>`. Backed by `migrate_to_vocabulary.py`.

### `code-graph-analyze` CLI
Analyze a repository and populate code graph collections in Weaviate.

<details>
<summary>Details</summary>

Usage: `.claude/scripts/code-graph-analyze /path/to/repo [--project NAME] [--incremental] [--language LANG] [--create-collections]`.

- `--incremental`: only re-analyze files changed since last run (tracked via content hash).
- `--language <lang>`: restricts analysis to one language (useful for polyglot repos).
- `--create-collections`: explicitly creates Weaviate collections before analysis; safe to call on existing collections (schema migration adds missing properties without data loss).

Windows users: `.claude/scripts/code-graph-analyze.ps1` PowerShell wrapper also ships.

</details>

### `code-graph-query` CLI
Query the code graph from the shell.

Usage: `.claude/scripts/code-graph-query search "<concept>"` / `similar "<full_name>"` / `structure dependencies|callers|methods|extends|interactions "<target>"`. Backed by `query_code_graph.py`.

### `cost-summary` CLI
Print a summary of Claude API token costs from `~/.claude/metrics/costs.jsonl`.

Portable entry point (Linux / macOS / Windows, stdlib-only Python, v0.2.54 Track G): `python .claude/scripts/cost-summary.py [--days N] [--session ID]`. The bash `cost-summary` wrapper survives as a POSIX shim delegating to the `.py`.

### `add_temporal_metadata.py`
Backfill `valid_from` / `created` / `updated` fields in KG node YAML frontmatter from `git log` history. `--dry-run` previews changes; `--file PATH` scopes to one file.

### `query_temporal.py`
Point-in-time KG queries — "what did the knowledge graph look like on date X?" — using `valid_from` / `valid_until` Weaviate filters.

### `maintain_knowledge_graph.py`
Integrity checks: orphaned nodes, broken WikiLinks, missing required frontmatter fields.

### `process_documents.py`
Auto-chunk uploaded documents in `documents/` to Weaviate `DocumentChunks` collection and create a KG node for the source.

### `smart_file_ops.py`
Context-efficient file operations: `check`, `summary`, `find`, `section`. Avoids reading large files in full. Global script also available at `~/.claude/scripts/smart_file_ops.py`.

### `generate-kg-summary.py`
Generates LLM summaries (Claude Haiku) for KG nodes and stores them in `knowledge/.node_formats.json`. Called by `kg-summary-generator.sh` hook. Content hash prevents regeneration when content is unchanged.

### `bash_security.py`
Security scan backend for `pre-tool-use.sh`: SSRF guards, shell injection pattern matching.

### `precompact_prune.py`
Pre-compaction pruning of `CONTEXT_STATE.md` to stay within line limits. Invoked by the `PreCompact` hook.

### `detect-project.sh`
Walks up from `$PWD` looking for a `.vct-project` marker file and prints the project name. Used by `vct-secrets` and the git credential helper to auto-scope secrets to the right project without explicit `--project` flags.

### `get_node_info.py`
Backend for `kg-info`. Loads a single KG node, parses its frontmatter, and prints metadata + connections.

### Windows PowerShell Wrappers
`.ps1` variants of `kg-search`, `kg-info`, `kg-sync`, `code-graph-analyze`, `code-graph-query` ship alongside the bash wrappers for Windows adopters.
