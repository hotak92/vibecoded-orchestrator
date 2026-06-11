---
name: codegraph-diagram
description: "[PRE-ALPHA] Generate a Mermaid flowchart of a code subgraph centered on a function, class, or module. Runs `vco codegraph-diagram` under the hood, writes the .mmd to .claude/diagrams/codegraph/ so it's indexed for hybrid_search, and gives a 3-5 line summary plus a link to open it in the launcher's DiagramsTab. Output may be incomplete, inaccurate, or visually broken — always tell the user to verify against the source code before sharing. Use when the user asks \"show me a call graph for X\", \"what does Y depend on\", \"draw the inheritance chain rooted at Z\", or any other request that maps to a small-to-medium subgraph extraction from the code graph."
short_desc: "[PRE-ALPHA] auto-generate a Mermaid call/import/extends graph from a seed symbol"
keywords: ["code graph", Mermaid, "call graph", "dependency diagram", "class hierarchy", "architecture diagram", "show me a call graph", "what does X depend on"]
argument-hint: "<symbol> [--hops N] [--scope calls|imports|extends|composes|interactions|all]"
model: inherit
effort: medium
---

# /codegraph-diagram

> ⚠️ **PRE-ALPHA**. The codegraph→Mermaid pipeline is experimental. Output may be
> incomplete (graph traversal skips edges the schema doesn't yet capture),
> inaccurate (heuristic seed resolution can pick the wrong symbol when names
> collide), or visually broken (Mermaid auto-layout degrades past ~50 nodes
> even when the data is clean). **Always tell the user to verify the diagram
> against the source code before sharing it or making decisions from it.**
> File-, schema-, and pipeline-level bugs are expected; report surprising
> behaviour rather than working around it silently.

Generate a Mermaid `flowchart TD` of a subgraph rooted at one code symbol, write it to `.claude/diagrams/codegraph/<sanitised>.mmd`, index it so `hybrid_search` finds it later, and give a quick summary the user can read before opening the rendered diagram in the launcher.

## Inputs

| Arg | Required | Meaning |
|---|---|---|
| `<symbol>` | yes | A code entity. Resolved in order: `CodeFunction.full_name` → `CodeClass.full_name` → `CodeModule.path`. Pass exactly what `query_code_structure` would accept. |
| `--hops N` | no | BFS depth (default 2, max 3). Beyond 3 the auto-layout devolves into a "ball of yarn"; the CLI rejects higher values. |
| `--scope` | no | Which edges to follow (default `calls`): `calls`, `imports`, `extends`, `composes`, `interactions`, or `all`. See *Scope guide* below. |
| `--max-nodes N` | no | Soft cap (default 50 — the empirical Mermaid auto-layout knee). The CLI truncates and warns when traversal would exceed the cap. |
| `--no-modules` | no | Skip per-module `subgraph` grouping. Useful for tiny diagrams. |
| `--title TEXT` | no | Override the auto-derived title (default: the resolved seed's full_name). |
| `--output PATH` | no | Override the default output path (`./.claude/diagrams/codegraph/<sanitised>.mmd`). Anything outside `.claude/diagrams/` is written but NOT indexed. |
| `--print` | no | Write to stdout instead of a file. Skips indexing. |
| `--project NAME` | no | Override the code-graph project name (otherwise resolved via the launcher). |

If invoked with no arguments, ASK the user for a seed symbol before doing anything. Don't guess — picking the wrong seed produces an off-topic diagram that wastes the user's time.

## What you do

1. Run the CLI via Bash:
   ```bash
   vco codegraph-diagram "<symbol>" --hops <N> --scope <scope> --json
   ```
   Always use `--json` so you can parse the result deterministically. The exit code carries the success/failure signal:
   - `0` — diagram written and (when under `.claude/diagrams/`) indexed.
   - `1` — render error (Weaviate query failed mid-flight, file write failed, or indexer raised — diagram may or may not exist).
   - `2` — env problem (Weaviate down, seed not in the code graph, project unresolvable).
2. `Read` the resulting `.mmd` file (path is in the JSON payload's `path` field).
3. Produce a short summary for the user:
   - **First line: a PRE-ALPHA caveat.** "⚠️ codegraph→Mermaid is pre-alpha; verify against source before trusting." Don't skip this — even on a clean render the pipeline can quietly miss edges.
   - One line on the seed: kind + full name.
   - One line on the scope + counts: "12 nodes, 18 edges across 3 modules".
   - One line on truncation if `truncated=true`: "truncated at <reason>".
   - One pointer: "open in the launcher's DiagramsTab for an embedded render".
   - If exit was 2 with `seed_not_found`: suggest running `search_code_graph("<concept>")` first to discover the right `full_name`, then re-run with the corrected seed.

Keep the whole reply under 9 lines (8 + the caveat) unless the user explicitly asks for a deeper walk-through. The diagram itself is the answer — the summary is a navigation aid.

## Scope guide

| Scope | Use when | What you get |
|---|---|---|
| `calls` (default) | "what does function X call?" | Outbound call graph from a function. Edges = `calls`. |
| `imports` | "what does module M depend on?" | Outbound import graph from a module. Edges = `imports`. |
| `extends` | "what's the inheritance chain of class C?" | Base classes recursively. Edges = `extends`. |
| `composes` | "what classes does C hold as fields?" | Composition relationships. Edges = `composes`. |
| `interactions` | "what external services does X call?" | Cross-service HTTP/gRPC/MQ calls. Edges = `interacts`. Each remote endpoint shows up as a leaf node labelled `<protocol> <endpoint>`. |
| `all` | "show me everything around X" | Fan-out across every scope above. Hits the 50-node cap fast on busy entities — pair with `--max-nodes 30` to keep it readable. |

## The 50-node cap

Mermaid's `flowchart TD` auto-layout starts crossing edges chaotically past ~50 nodes; past 100 it's unusable. The CLI defaults to `--max-nodes 50` and TRUNCATES rather than producing a broken diagram. When you see `truncated=true` in the JSON, tell the user to:
- Narrow scope (`--scope calls` instead of `--scope all`).
- Reduce hops (`--hops 1` instead of `--hops 3`).
- Or split the request into multiple seed-rooted diagrams.

Don't bump `--max-nodes` past the default unless the user explicitly asks; the cap exists for a reason.

## Examples

```
/codegraph-diagram vco_lib.diagram_indexer.index_diagram
/codegraph-diagram vco_lib.codegraph_to_mermaid.fetch_subgraph --hops 1
/codegraph-diagram api.UserManager --scope extends
/codegraph-diagram launcher/src-tauri/src/main.rs --scope imports --hops 2
/codegraph-diagram api.routes.serve_request --scope interactions
/codegraph-diagram vco_lib.cli --scope all --hops 1 --max-nodes 30
```

## What you don't do

- Don't try to "see" the diagram. The Mermaid file is text and the user opens the visual in their browser or the launcher.
- Don't bump `--hops` above 3 hoping for more context — the CLI rejects it and rightly so.
- Don't write to a path outside `.claude/diagrams/` unless the user explicitly asks for `--output`. Anywhere else and `hybrid_search` won't find the diagram later.
- Don't re-derive the call graph by reading source files when you could query the code graph — that's what this tool exists to short-circuit.
