<!--
SPDX-License-Identifier: AGPL-3.0-or-later
Copyright (c) 2026 VibeCoded Tools
-->
# Orchestrator schema migrations

This directory holds **version-gated schema-migration edges** that
`install.py --update` runs via `vco_lib.schema_migration_runner`. It is the
orchestrator-level migration store for the schema artifacts tracked in the
launcher.db `artifact_schema_versions` registry (Weaviate collection schemas,
the KG-summary cache, launcher.db row shapes, vocabularies, etc.).

**Today it ships EMPTY** (this README + `.gitkeep` only). With zero edge files
the runner is a verified **no-op**: every artifact is either registered at its
current canonical version (never-materialized) or left untouched
(up-to-date + not stale). No schema is mutated. The mechanism exists so a
**future** release whose schema actually changes can drop ONE edge file that
runs automatically at update time, gated on the DB-tracked version — without
any code change to "turn the runner on".

See the ratified design: `.claude/context/plans/SPEC-v0260-migration-runner.md`
and the binding policy in
`.claude/context/plans/PLAN-v0260-consolidated-update-system.md`.

## Layout

```
migrations/
  <artifact_type>/
    <from>_to_<to>.<ext>     # one file per ascending version EDGE
```

- `<artifact_type>` MUST be a key of
  `vco_lib.schema_versions.CANONICAL_VERSIONS` (e.g. `kg_collection`,
  `shared_kg_collection`, `development_collection`, `diagrams_collection`,
  `codegraph_collection`, `kg_node_frontmatter`, ...). A directory naming an
  unknown type is skipped with a WARNING — never a crash.
- `<from>` / `<to>` are integers; **`to == from + 1` is REQUIRED** (one edge
  per release bump — no version skipping). The runner asserts contiguity from
  the DB-recorded version up to canonical and aborts on a gap.
- `<ext>` ∈ `{sql, sh, ps1, py}`.

## Per-project vs orchestrator-wide (who runs what)

Projects update **separately**. The runner is invoked once per project with
that project's `--project-id`:

- The **orchestrator self-update** (`install.py --update`) migrates (a) the
  ROOT project's own per-project collections keyed by the root's real
  project_id, AND (b) the **orchestrator-wide** artifacts keyed
  `project_id=NULL` — the shared KG (`shared_kg_collection`) and the Layer-5
  launcher/global telemetry shapes (`launcher_db_table_set`,
  `rl_events_payload_shape`).
- A **non-root project's bundle update** migrates ONLY that project's own
  per-project collections (its KG / Development / Diagrams / Codegraph + its
  row-shape/vocabulary rows). It does NOT touch the shared KG or the
  launcher-global shapes (`include_orchestrator_wide=False`).

One edge file under `migrations/<artifact_type>/` applies to whichever
project(s) the runner is invoked for; the version gate + `project_id` keying
decide whether it actually runs for a given project.

## Codegraph is one artifact_type, five live classes

`codegraph_collection` is a single `artifact_type` (one recorded version) that
maps to the project's **five** live Weaviate classes — `<prefix>_CodeModule`,
`<prefix>_CodeClass`, `<prefix>_CodeFunction`, `<prefix>_CodeAPI`,
`<prefix>_CodeInteraction` (prefix from `CODE_GRAPH_PROJECT` / `PROJECT_NAME`).
A `codegraph_collection` edge runs against all five. CodeSage vectors are
expensive to regenerate, so the same preserving / regenerate-or-defer policy
applies (no silent drop).

## Filename grammar

`^(?P<from>\d+)_to_(?P<to>\d+)\.(sql|sh|ps1|py)$`

## Extension → store dispatch

| ext | target | how it runs |
|-----|--------|-------------|
| `.sql` | SQLite (launcher.db or hub.db) | opened by the runner; split into statements and run one-at-a-time inside a SINGLE manual transaction (`isolation_level=None` + explicit `BEGIN`/`COMMIT`, full `ROLLBACK` on any statement error — truly atomic, NOT `executescript`). Declare the target DB with `-- @db: launcher` or `-- @db: hub` (default `launcher`). **A `.sql` edge MUST NOT embed its own `BEGIN`/`COMMIT`/`ROLLBACK`** — the runner owns the transaction; an embedded `BEGIN` errors ("cannot start a transaction within a transaction") and the whole edge rolls back. Edges needing trigger / `BEGIN...END` bodies (or any explicit transaction control) should ship a `.py` edge instead (the naive `;`-splitter targets DDL + simple DML). |
| `.sh` | Weaviate / POSIX | spawned via `bash` (cwd = clone root, 300 s timeout). Reads `$WEAVIATE_URL` etc. from env. |
| `.ps1` | Weaviate / Windows | spawned via `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`. |
| `.py` | needs vco_lib helpers | spawned via the active interpreter. |

A given edge ships **EITHER** a cross-OS `.sh` + `.ps1` sibling pair (enforced
by the multi-OS sibling discipline) **OR** a single OS-agnostic `.sql` / `.py`.
The runner picks `.ps1` on Windows, `.sh` otherwise; `.sql`/`.py` always run.

## Mandatory header block

Every edge file declares, in its first comment block:

```
# @idempotent: yes
# @destructive: yes|no
# @classification: derived|user_curated
```

(Use `--` instead of `#` for `.sql`.) The runner reads `@destructive` and
`@classification` and **cross-checks** them against
`schema_versions.is_derived(<artifact_type>)`. A contradiction (e.g. a
`derived`-typed edge declaring `@classification: user_curated`, or a
`@destructive: yes` script offered as a data-preserving derived edge) is a
packaging bug → the runner **aborts that artifact** with a clear error and
mutates nothing (fail-closed).

## Idempotency is the script's contract

Re-running an applied edge must be a no-op (`.sql` via
`IF NOT EXISTS` / `OR IGNORE`; shell scripts presence-check before mutating).
On edge failure the runner stops at that edge, does NOT advance the recorded
version, and writes a `schema_migration_failed_<id>` deferral — the next
`install.py --update` re-attempts the same edge.

## Derived-collection (Weaviate) binding policy

For a DERIVED Weaviate collection the runner applies the FIRST matching rule:

1. **Not stale** (live fingerprint unchanged) → DO NOTHING.
2. **Stale + a data-preserving edge exists** → run it (no re-embed). Preferred
   over recreate even for derived collections.
3. **Stale + schema changed + no preserving edge** → surface a
   regenerate-or-defer decision (launcher modal in the GUI; a
   `schema_migration_needs_choice` deferral headless). **Never auto-drops.**

Drop + recreate (re-embed from disk) is the LAST resort, reachable only when
the fingerprint actually changed AND no preserving edge exists AND the user
explicitly chooses "Regenerate now".
