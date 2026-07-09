<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Code-graph golden fixture corpus

A small, committed, multi-language fixture repo used by
`tests/test_codegraph_golden.py` to PIN the analyzer's current behaviour
(walk order, dedup keys, property stamping, chunk boundaries, `content_hash`)
before and across the P2f `CodeEntity` IR refactor.

## Layout

`repo/` is the analyzed tree. All names are neutral (no real project names).

| File | Exercises |
|---|---|
| `repo/src/widgets.py` | Python: module docstring, imports, classes + inheritance, nested function, decorators, type annotations, internal calls (n_callers source) |
| `repo/tests/test_widgets.py` | Python: the `is_test` path-heuristic axis (lives under a `tests/` part) |
| `repo/src/big_module.py` | Python: one function whose body exceeds the codesage 7168-char budget → CHUNKING (multiple chunk rows) |
| `repo/src/engine.rs` | Rust (regex): struct/enum/trait, `impl` blocks, `async fn` |
| `repo/src/client.js` | JavaScript (regex): class, function, arrow fn, imports |
| `repo/src/models.ts` | TypeScript (regex): interface, class, function, arrow fn |
| `repo/src/service.go` | Go (regex): struct, methods, functions |
| `repo/src/Account.java` | Java (regex): class + methods |
| `repo/src/deploy.ps1` | PowerShell: function + filter, plus a nested function at 8-space indent (the v0.2.75 deep-indent regression case) |
| `repo/src/routes.js` | Fastify-style routes → CodeAPI rows |
| `repo/src/vendor.js` | Minified content (single huge line) → MUST BE SKIPPED (CG-5) |
| `repo/node_modules/ignored.js` | Lives in an ignored dir → MUST NOT appear in any snapshot |

## Snapshots

`expected/*.json` is one normalized snapshot per code collection
(`CodeModule`, `CodeClass`, `CodeFunction`, `CodeAPI`, `CodeInteraction`).
Each is the analyzer's stored output, normalized (sorted by
`(path, full_name)`, volatile fields stripped, `content_hash` KEPT). They are
the CONTRACT: the P2f IR refactor must leave them byte-identical.

## Regenerating (requires human review of the diff)

Snapshots are regenerated only deliberately:

```
CODEGRAPH_GOLDEN_REGEN=1 python -m pytest tests/test_codegraph_golden.py -q
```

A snapshot diff after a refactor is a SEMANTIC REGRESSION unless a human has
reviewed and accepted it. Do NOT regen to make a red test pass.
