---
title: Orchestrator Code Graph
type: concept
tags: [mid-level-architecture, vibecoded-orchestrator, code-analysis, weaviate, semantic-search]
created: 2026-04-27T18:30:00Z
updated: 2026-05-16T20:30:00Z
status: active
---

# Orchestrator Code Graph

The Code Graph is a semantic index of source code in the active project. It extracts structural entities (modules, classes, functions, API endpoints, cross-service interactions) from source code, embeds them with code-specialized embeddings, and stores them in Weaviate for both semantic ("find code that does X") and structural ("what calls function Y?") queries.

[[implements::Code Analysis]] [[uses::Weaviate]] [[relatedTo::Orchestrator Knowledge Graph]] [[relatedTo::Orchestrator MCP Servers]]

## Overview

Traditional code search (grep, file read) finds exact strings. The Code Graph finds code by **purpose** — "authentication middleware", "database retry logic", "cross-service HTTP calls". It also answers structural questions without reading files: "what are the callers of this function?", "what does this module import?", "what classes compose this type?"

## Analysis Pipeline

```
Source repository
        |
[code-graph-analyze script]
        |
        +-- Language detection (Python/JS/TS/Go/Rust/Java/...)
        |
        +-- For Python: ast module (full AST parse)
        |   For others: regex-based extraction (10+ languages)
        |
        +-- Entity extraction:
        |     CodeModule -> CodeClass -> CodeFunction
        |                              -> CodeAPI
        |                              -> CodeInteraction
        |
        +-- Incremental mode (--incremental):
        |     git diff to find changed files since last analysis
        |     Only re-analyze changed files
        |
        +-- Optional Joern integration (--cfg --pdg):
        |     cfg_summary    — control flow graph summary per function
        |     data_flow_vars — data-flow variable tracking
        |
        +-- Embed via the code-embedding service
        |   (CodeSage-Large-v2 on GPU, qwen3-embedding:0.6b on CPU via Ollama)
        |
        +-- Upsert to Weaviate per-project collections
```

Run:
```bash
.claude/scripts/code-graph-analyze /path/to/repo --project "ProjectName" --incremental
# With CFG/PDG (requires joern in PATH):
.claude/scripts/code-graph-analyze /path/to/repo --project "ProjectName" --cfg --pdg
```

## Entity Types

### CodeModule

Represents a source file.

| Property | Description |
|---|---|
| path | Relative file path |
| language | python, javascript, typescript, go, rust, java, etc. |
| module_summary | Short description of the module's purpose |
| loc | Lines of code |
| complexity | Cyclomatic complexity estimate |
| imports | List of imported modules/packages |

### CodeClass

| Property | Description |
|---|---|
| name | Short class name |
| full_name | `module.ClassName` |
| class_body | Full class source text |
| methods | List of method names |
| extends | Parent class names |
| field_types | Dict of field name → type annotation |
| composes | Classes used as field types (composition relationships) |

### CodeFunction

| Property | Description |
|---|---|
| name | Short function name |
| full_name | `module.ClassName.method_name` or `module.function_name` |
| function_body | Full function source text |
| signature | Type-annotated signature string |
| calls | List of functions this function calls |
| type_uses | Types referenced in annotations |
| cfg_summary | Control flow graph summary (optional, requires `--cfg`) |
| data_flow_vars | Data flow variable tracking (optional, requires `--pdg`) |

### CodeAPI

HTTP API endpoints.

| Property | Description |
|---|---|
| endpoint | URL path pattern (e.g., `/users/{id}`) |
| method | HTTP method (GET, POST, etc.) |
| api_description | Purpose and parameters |
| handler | `module.function_name` of the handler |

### CodeInteraction

Cross-service or cross-module call.

| Property | Description |
|---|---|
| interaction_type | http_call, db_query, message_queue, etc. |
| protocol | HTTP, gRPC, AMQP, etc. |
| endpoint | Target URL or service identifier |
| confidence | 0.0-1.0 extraction confidence |
| direction | outbound or inbound |

## Embedding

Code entities embed via the orchestrator's code-embedding service (port 11440). Two backends:

- **GPU (default if CUDA available)**: [[CodeSage-Large-v2]] — 2048-dim, 1.3B params, Apache 2.0.
- **CPU fallback** (`CODE_EMBED_BACKEND=ollama`): `qwen3-embedding:0.6b` via Ollama (1024-dim). Set `CODE_EMBED_MODEL` to override the Ollama model.

Each code collection registers both named vectors (`codesage_embed` + `ollama_code_embed`) so existing entries remain searchable after a backend switch. KG nodes use a separate text embedding ([[Qwen3 Embedding]] / [[Snowflake Arctic Embed 2.0]]); the two embedding spaces are kept disjoint.

## Per-Project Collection Naming

Collections are prefixed by project name to avoid cross-project contamination:

```
{ProjectName}_CodeModule
{ProjectName}_CodeClass
{ProjectName}_CodeFunction
{ProjectName}_CodeAPI
{ProjectName}_CodeInteraction
```

The `--project` flag in `code-graph-analyze` sets this prefix.

## Query Interface

### Semantic Search (MCP)

```python
search_code_graph(
    query="authentication middleware that validates JWT tokens",
    scope="code",          # "all" | "code" | "interaction"
    limit=10,
    expand_hops=1          # 0=seed only | 1=follow call edges | 2=two hops
)
```

`expand_hops` follows call/interaction edges after the initial seed retrieval, surfacing the call-graph context around matching functions.

CLI:
```bash
.claude/scripts/code-graph-query search "auth middleware"
```

### Structural Queries (MCP)

```python
query_code_structure(
    query_type="callers",
    target="api.auth.validate_token",
    project="MyProject"
)
```

| query_type | What it returns |
|---|---|
| `dependencies` | Modules imported by target module |
| `imports` | Modules that import the target |
| `callers` | Functions that call the target function |
| `methods` | All methods of a class |
| `extends` | Inheritance chain (parents and children) |
| `interactions` | All outbound cross-service calls from a module |
| `path` | Shortest call path between two functions (BFS, depth 6) |
| `composes` | Classes used as fields in target class |
| `composed_by` | Classes that use the target as a field |
| `type_users` | Functions that use the target type in annotations |

Path query format:
```python
query_code_structure("path", "source.module.func->dest.module.func")
```

## Incremental Updates

The `--incremental` flag makes analysis fast enough to run on every commit:

1. Read last analysis timestamp from Weaviate metadata.
2. Run `git diff --name-only <last_timestamp>` to find changed files.
3. Re-analyze only those files.
4. Upsert new/updated entities, delete removed ones.

The PostToolUse hook (`code-graph-incremental.sh`) queues code files for incremental analysis after every edit.

## Joern Integration (Optional)

Joern is a static-analysis platform for precise CFG and PDG extraction. Not required for basic operation; enables:

- **CFG summary** (`cfg_summary`): high-level control flow (conditionals, loops, exception paths).
- **Data flow variables** (`data_flow_vars`): tracks which variables flow into which function parameters.

Joern must be in PATH and is only invoked with `--cfg` / `--pdg` flags.

## Integration Points

- **Hook system**: PostToolUse hook queues code files for analysis after edits.
- **MCP server**: the `weaviate-kg` server exposes `search_code_graph` and `query_code_structure`.
- **Knowledge Graph**: code graph and KG are separate collections in the same Weaviate instance.
- **Pre-edit hook**: `pre-edit-context-inject.sh` runs a code-graph search before file edits to provide relevant context.
