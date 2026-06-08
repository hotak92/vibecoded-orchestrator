# `vco codegraph-diagram` — auto-generate Mermaid call graphs from the code graph

> ⚠️ **PRE-ALPHA — DO NOT TRUST THE OUTPUT WITHOUT MANUAL VERIFICATION.**
>
> This pipeline ships in v0.2.34 but is explicitly experimental. Known limitations:
> - **Edge coverage is partial.** The code-graph schema captures `calls` / `imports` / `extends` / `composes` / `interactions`, but not every real edge of those kinds lands in Weaviate (parser misses, dynamic dispatch, conditional imports). Diagrams will under-report.
> - **Seed resolution is heuristic.** `Function → Class → Module` order means a symbol that exists at two levels resolves to the function and silently ignores the class/module version. Disambiguate by passing the fully-qualified name.
> - **Mermaid auto-layout degrades past ~50 nodes.** The default `--max-nodes 50` cap is the empirical ceiling for readability; the CLI truncates rather than producing a "ball of yarn" render.
> - **Cross-service `interactions` edges include synthetic remote endpoints** that don't correspond to a queryable Weaviate object. Useful for visualization, but don't follow them as if they were code edges.
> - **The output Mermaid file carries `%% [PRE-ALPHA]` comment lines** at the top — leave them in; they're the user-facing reminder when the file gets shared.
>
> **Always verify a generated diagram against the source code before sharing it or making decisions from it.** Report surprising omissions or wrong edges so the pipeline can improve; do not silently work around them.

**Phase 3 of the diagrams-integration plan** (maintainer-side internal notes). Status: shipped pre-alpha in v0.2.34.

Turns a subgraph rooted at one code symbol into a Mermaid `flowchart TD`, writes it under `.claude/diagrams/codegraph/`, and indexes it so `hybrid_search` can find it later. Pairs with the `/codegraph-diagram` slash skill — same arguments, same output.

## Quick start

```bash
# Default: 2-hop call graph rooted at the named function, written to
# .claude/diagrams/codegraph/<sanitised>.mmd
vco codegraph-diagram vco_lib.diagram_indexer.index_diagram

# Inheritance chain rooted at a class
vco codegraph-diagram api.UserManager --scope extends

# Module dependency tree, 1 hop, JSON output for scripting
vco codegraph-diagram api/routes.py --scope imports --hops 1 --json

# Print to stdout (skip file write + indexer)
vco codegraph-diagram vco_lib.cli.codegraph_diagram.cmd_codegraph_diagram --print
```

## Flags

| Flag | Default | Notes |
|---|---|---|
| `<seed_symbol>` (positional) | — | Resolved in order: `CodeFunction.full_name` → `CodeClass.full_name` → `CodeModule.path`. |
| `--hops N` | 2 | BFS depth. Capped at 3 — beyond that the auto-layout collapses. |
| `--scope` | `calls` | One of `calls`, `imports`, `extends`, `composes`, `interactions`, `all`. |
| `--max-nodes N` | 50 | Truncates with a warning past the cap; the empirical Mermaid auto-layout knee. |
| `--output PATH` | `./.claude/diagrams/codegraph/<sanitised>.mmd` | Anywhere outside `.claude/diagrams/` is written but NOT indexed. |
| `--no-modules` | off | Skip per-module `subgraph` grouping. Cleaner for tiny diagrams. |
| `--title TEXT` | seed's `full_name` | Override the auto-derived title. |
| `--print` | off | Source → stdout. Skips file write + indexing. |
| `--json` | off | Machine-readable single-object output. Exit codes unchanged. |
| `--project NAME` | resolved via launcher | Override the code-graph project name (Weaviate collection prefix). |

Exit codes mirror the rest of the `vco` CLI: `0` OK, `1` render/write error, `2` env problem (Weaviate down, seed not found, project unresolvable).

## Scope guide

| Scope | What it does |
|---|---|
| `calls` | Outbound function-call graph. Reads `CodeFunction.call_names`. |
| `imports` | Module dependency tree. Reads `CodeModule.import_names`. |
| `extends` | Base-class chain. Walks `CodeClass.extends` references. |
| `composes` | Composition relationships (held as field types). Reads `CodeClass.composes`. |
| `interactions` | Cross-service HTTP/gRPC/MQ calls from `CodeInteraction`. Each remote endpoint becomes a leaf labelled `<protocol> <endpoint>`. |
| `all` | Fan-out across every scope above. Hits the node cap fast on busy entities — pair with `--max-nodes 30`. |

The `calls` scope only fires on function seeds; `imports` only on module seeds; `extends` / `composes` only on class seeds. `all` is a union and skips fetchers that don't apply to the seed's kind.

## The 50-node cap

`flowchart TD`'s auto-layout starts crossing edges chaotically past ~50 nodes; past 100 it's effectively unreadable. The CLI **truncates** at the cap rather than producing a broken render — `truncated=true` shows up in the JSON payload, the human summary calls it out. When you see truncation, narrow the request:

- Reduce hops (`--hops 1` instead of `--hops 3`).
- Narrow scope (`--scope calls` instead of `--scope all`).
- Split into multiple seed-rooted diagrams.

Bumping `--max-nodes` past the default works but reproducibly produces diagrams humans give up on. Don't unless you're piping the result through a different renderer.

## How it integrates

* **Indexing**: the CLI calls `vco_lib.diagram_indexer.index_diagram` after a successful write IF the output landed under `.claude/diagrams/`. The diagram then surfaces in `hybrid_search` results alongside KG nodes (Phase 1.5.C of the plan) with `result_kind="diagram"`.
* **Project resolution**: same path as every other `vco` subcommand — launcher's vct-hub first, then `CODE_GRAPH_PROJECT` / `PROJECT_NAME` env vars, then bare (cross-tenant) query.
* **Soft-fail**: indexer raises are demoted to warnings; the diagram still lands on disk and can be re-indexed via `vco rebuild-diagram-index` later. Weaviate connection failures abort with exit 2 and a clear stderr message.

## Slash skill

The same CLI is wrapped by the `/codegraph-diagram` slash skill (`.claude/skills/codegraph-diagram/SKILL.md` after install). Invoke it inside a Claude Code session — the skill calls the CLI with `--json`, reads back the resulting `.mmd`, and gives a short summary the user can read before opening the diagram in the launcher's DiagramsTab.

## See also

- `vco rebuild-diagram-index` — re-index the whole `.claude/diagrams/` tree after model upgrades or schema migrations.
- `code-graph-query` — interactive querying of the same Weaviate collections this command consumes. Useful for discovering the right `seed_symbol` before rendering.
- `knowledge/concepts/` nodes tagged `diagrams-integration` for design rationale.
