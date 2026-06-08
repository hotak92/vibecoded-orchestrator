---
title: Project-extra codegraph paths (v0.2.47)
type: concept
tags:
  - v0.2.47
  - codegraph
  - launcher
  - multi-codebase
  - mid-level-architecture
  - identity-tab
  - hub-resolver
  - low-level-implementation
created: 2026-06-05T00:00:00Z
updated: 2026-06-05T00:00:00Z
status: active
---

# Project-extra codegraph paths

**Shipped**: v0.2.47 (alongside the [[supervisor-image-resolution-variant-gap-2026-06-04|supervisor image-auth fix]])

## What it is

A way to index **additional read-only filesystem paths** into a project's
codegraph collection from the launcher's Identity tab — without making
those paths launcher projects.

**Canonical use case**: a user working in a private fork wants their `hybrid_search`
+ `search_code_graph` queries to see entries from a sibling
`vibecoded-orchestrator/` public-repo clone, but doesn't want to register
that clone as a launcher project (which would create `.claude/` state in it
and turn it into a write target).

## Architecture

Four surfaces co-evolve:

| Layer | Component | What it does |
|---|---|---|
| DB | `project_codegraph_extra_paths` (migration 026) | Persists `(project_id, path, label, added_at, last_indexed_at, last_indexed_commit, enabled)` per row, PK `(project_id, path)`, CASCADE on project delete |
| Launcher (Tauri) | `commands::project_codegraph_extras` | 6 commands: list / add / remove / set_enabled / sync / reindex. Per-project mutex serialises add→reindex sequences. Disambiguation response when path is an existing launcher project |
| Hub resolver | `code_graph_extra_paths: Vec<{path, enabled, last_indexed_commit}>` on `/api/v1/projects/{id}/config` | Additive field; older clients ignore. Only enabled rows projected |
| GUI (Svelte) | `ExtraCodegraphPathsPanel.svelte` + `ExtrasDisambiguationModal` + `ExtrasSyncProgressModal` | Per-row sync/disable/remove. Auto-sync on add. Re-sync with `--prune-stale` on remove/disable. Hide-to-pill on long syncs |
| Hook | `code-graph-incremental.sh` (and `.ps1` sibling) | First-match-wins ordering: own-repo → extras → siblings → no-op. Edits under extras re-index into the CURRENT project's prefix |
| Analyzer CLI | `--extra-path <DIR>` (repeatable) + `--since-commit <SHA>` | Walks all source roots in ONE pass; `visited_uuids` is the UNION across primary repo + all extras. Schema adds `project_source` property to all 5 code collections |

## The critical invariant (§14.2 of the v0.2.47 plan)

**`--prune-stale` runs MUST visit every file currently meant to be in the
project's codegraph in ONE invocation.** Multi-pass with `--prune-stale`
would cause each pass to delete the OTHER passes' UUIDs.

Locked in at TWO layers:

1. **Launcher** (`reindex_project_codegraph_after_extras_change` in `project_codegraph_extras.rs:708`): always builds a single `code-graph-analyze` invocation with the project's `folder_path` as primary path AND every enabled extra via `--extra-path`. Per-project mutex prevents a concurrent add/remove from racing the snapshot read.
2. **Analyzer** (`analyze_repository` in `analyze_code_graph.py:1635`): `visited_uuids` is a single set initialised ONCE per run. The dispatcher loop iterates `[repo_path] + canonical_extras`, accumulating into the same set. `_prune_stale_objects()` runs ONCE at the end. Test `test_visited_uuids_is_single_shared_set` makes regression structurally impossible.

## Disambiguation flow

When the user picks a path that matches an existing launcher project's
`folder_path`, the Tauri command returns:

```json
{"action": "disambiguation_required",
 "existing_project": {"id": "...", "name": "...", "slug": "...", "folder_path": "..."},
 "path": "/canonical/input"}
```

The GUI shows a modal with three buttons:
- **"Add as project (grant access matrix)"** → calls existing
  `codegraph_grant_access` with `grantor=existing`, `grantee=current`,
  `access=read`. The current project gains read access on the
  existing project's already-maintained codegraph.
- **"Add as path anyway"** → re-calls `add_project_codegraph_extra_path`
  with `force: true`. Persists the row, triggers auto-sync.
- **"Cancel"** → no-op.

This pattern lets the user either re-use a sibling project's continuously-
maintained codegraph (cheaper, always-fresh) or treat the folder as a
read-only reference under the current project (single-collection queries,
no permission management).

## Path canonicalisation rules

Enforced at the Tauri-command boundary BEFORE INSERT (SQLite has no
path primitives):

- Reject empty / whitespace-only / relative / non-existent / non-directory
- `dunce::canonicalize` resolves symlinks, normalises segments (Windows
  drive-letter form rather than UNC `\\?\` form)
- Backslashes → forward slashes (cross-platform prefix-match storage form)
- Lowercase drive letter on Windows
- Trailing separator stripped (so SQL `WHERE ? LIKE path || '%'` is
  unambiguous)

The hub resolver returns the canonical form so hooks can substring-match
against `EDITED_FILE` directly.

## Hook ordering (§5a of plan)

`code-graph-incremental.sh` (and `.ps1`) check order, **first match wins**:

1. **Own-repo fast path** — edit under current `$REPO_PATH` → no override
   (also avoids one hub RTT per Edit in the common case)
2. **Extras of current project** — query hub resolver for
   `code_graph_extra_paths`; if `$EDITED_FILE` is under any enabled
   path, set `REPO_PATH=<extra>`, keep `PROJECT_NAME`
3. **Sibling project** — existing `detect-project.sh` behaviour
4. **None match** — graceful no-op

`EXTRAS_MATCHED=1` skips the subsequent sibling-detection block so an
extras-claimed file doesn't get re-routed to a sibling project's
codegraph.

Pre-v0.2.47 hubs lack the field → resolver exits 4 ("field not found")
→ `EXTRAS_LIST` stays empty → no-op fall-through to sibling detection.
Backwards-compatible.

## What's NOT in scope

- KG syncing: extra paths' `knowledge/` subdirs are NOT auto-synced into
  the project's KG. Codegraph only.
- Diagrams: same.
- Hook-driven writes against extra paths: extras are READ-ONLY references.
  `ruff`/`pyright` don't run against extras.
- Auto-drop of Weaviate entries on row delete: handled by the analyzer's
  `--prune-stale` pass in the re-analyze that fires after removal, NOT by
  destroying the collection.

## Audit-log events

Every transition records to `audit_log`:

- `codegraph_extra_path_added` / `codegraph_extra_path_added_force` (user
  picked "Add as path anyway" after disambiguation)
- `codegraph_extra_path_removed`
- `codegraph_extra_path_enabled_toggled`
- `codegraph_extra_path_synced`
- `codegraph_reindex_after_extras_change` (with the full path list +
  prune flag + counts)

## Cross-references

- [[refines::Multi-codebase code graph detection]] — extends the existing
  sibling-detection pattern with a new explicit-path source
- [[refines::Pre-install catalog architecture — L0 public endpoint + post-install on-disk manifest]] — uses the same hub-resolver field-addition pattern (additive, default-empty, schema_version unchanged)
- [[implements::v0.2.47 spec at .claude/context/plans/project-extra-codegraph-paths-2026-06-05.md]]

## Implementation files

- `launcher/src-tauri/vct-launcher-core/src/db/migrations/026_project_codegraph_extra_paths.sql`
- `launcher/src-tauri/vct-launcher-core/src/db/codegraph_extras.rs`
- `launcher/src-tauri/src/commands/project_codegraph_extras.rs`
- `launcher/src-tauri/vct-hub/src/config_api.rs` (additive field + struct)
- `launcher/src/lib/project-state/ExtraCodegraphPathsPanel.svelte`
- `launcher/src/lib/components/ExtrasDisambiguationModal.svelte`
- `launcher/src/lib/components/ExtrasSyncProgressModal.svelte`
- `launcher/src/lib/api/codegraph_extras.ts`
- `launcher/src/lib/types/codegraph-extras.ts`
- `templates/scripts/analyze_code_graph.py` (+ `.claude/scripts/` mirror)
- `templates/scripts/vct_project_config.sh` / `.ps1` (+ `.claude/scripts/` mirrors)
- `templates/hooks/code-graph-incremental.sh` / `.ps1` (+ `.claude/hooks/` mirrors)
- `vco_lib/project_config.py` (Python resolver client field)

## Tests

- `tests/test_analyze_code_graph_extras.py` — 12 tests, including the
  critical-invariant lock-in `test_visited_uuids_is_single_shared_set`
- `tests/test_code_graph_incremental_hook.sh` — 11 tests, all 4 ordering
  branches + pre-v0.2.47 hub fallback
- `tests/test_vct_project_config_extras.sh` — 9 tests, bash resolver
  `--field code_graph_extra_paths` rendering
- `tests/test_project_config.py::CodeGraphExtraPathsTest` — 8 tests,
  Python `ProjectConfig.code_graph_extra_paths` parse/round-trip
- Rust DB layer: 20 tests in `vct-launcher-core::db::codegraph_extras::tests`
- Rust Tauri commands: 26 tests in `project_codegraph_extras::tests`
- Rust hub resolver: 4 tests in `config_api::tests`
- Svelte wrapper: 11 vitest tests in `codegraph_extras.test.ts`

**Total: +101 tests across all 5 surfaces** (Rust core + Rust hub +
Rust launcher + Python + Svelte/TS).
