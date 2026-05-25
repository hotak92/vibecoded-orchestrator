---
title: Config projection contract (single-writer rule for per-project env)
type: concept
tags: [vibecoded-orchestrator, install, launcher, architecture, single-writer, low-level-implementation]
created: 2026-05-24T18:00:00Z
updated: 2026-05-24T18:00:00Z
valid_from: 2026-05-24T00:00:00Z
valid_until: null
status: active
---

# Config projection contract (2026-05-24)

Phase 0.B of the [[diagrams-integration plan|diagrams-integration-excalidraw-mermaid-2026-05-24]]
introduced `vco_lib/config_projection.py` as the SINGLE LEGAL WRITER of
per-project canonical env values to the three on-disk surfaces:

1. `<project_root>/.claude/settings.json` env sub-block (Claude Code CLI
   + Desktop + VS Code extension propagation channel)
2. `<project_root>/.claude/env` (POSIX shell-source surface for
   `tools/claude` wrapper)
3. `<project_root>/.vscode/settings.json` claude-code.env sub-block
   (opt-in; PR-27 removed this from the default writer set after sentinel
   testing proved it does NOT propagate to MCP subprocesses on Linux as
   of Claude Code 2.1.143 — kept addressable for diagrams-flow use cases)

## The rule

**Every canonical env value flows through `project_env_from_db(project_id)`.**
**Every surface write flows through `apply_project_env(bundle)`.** New
writers anywhere in the codebase that touch the canonical key set fail
the CI lint in `tests/test_config_projection_single_writer.py`.

The canonical key registry is `vco_lib.config_projection.list_canonical_keys()`
— a closed set including `KG_COLLECTION`, `SHARED_KG_*`, `VCT_KG_ACCESS_LIST`,
`VCT_CODE_GRAPH_ACCESS_LIST`, `ACTIVE_EMBEDDING`, etc. (19 keys total).
Adding a new key requires updating both the Python registry AND the Rust
`CANONICAL_INSTALL_ENV_KEYS` const in
`launcher/src-tauri/src/commands/projects_v2.rs`.

**v0.2.34 update (A7)** — `VCT_DIAGRAMS_ACCESS_LIST` joined the canonical key
set. Sourced from `diagram_access` (joined to `projects.name`) and consumed
by `weaviate_mcp/server.py::_diagrams_peer_collections`. Replaces the Phase
1.5.C piggyback on `VCT_KG_ACCESS_LIST` that had wrong granularity (KG-only
grants leaked diagram visibility; diagram-only grants were invisible to the
MCP). Hub-side parallel field: `diagrams_access_list` on `ProjectConfigResponse`.

## Why a contract and not just a function

Pre-Phase-0, env values reached on-disk surfaces via **four independent
paths**: Rust `write_project_env_files`, Rust `ensure_project_env_template`,
Python `install.py` backfills, and per-grant-change Tauri commands that
called the Rust writer directly. Each path had its own opinion about
what to write and how to merge. Bug-4 of the
[[install-flow architectural overhaul|install-flow-architectural-overhaul-2026-05-06]]
was specifically a wholesale-replace of the `env` sub-block that
silently dropped user-added keys. The fix added deep-merge to the Rust
writer, but other writers remained free to regress the same bug by
accident.

Phase 0.B replaces that "many opinions, hope they agree" model with
"one writer, lint-enforced".

## Migration discipline

Production Rust callers (`write_project_env_files` in `projects_v2.rs`,
backfill helpers in `install.py`) are temporarily allowlisted by the
single-writer lint. Each allowlisted file carries the marker comment

```
config_projection: legacy_caller_pending_migration
```

A follow-up PR ("Phase 0.B Part 2 — flip production callers to the
Python CLI") will migrate them via subprocess (`python -m
vco_lib.config_projection apply --project-id <id>`). Removing the marker
in source AND removing the file from `_LEGACY_PRODUCTION_WRITERS` in
the lint test is the migration completion checklist. The CI lint then
re-fails on any future direct write.

## Option A vs B (Rust ↔ Python interop)

Two interop strategies were considered:

* **Option A** — Python is canonical; Rust callers subprocess into the
  `vco_lib.config_projection apply` CLI when they need to project env to
  disk. **Chosen.**
* **Option B** — Port the contract to Rust as a sibling module with
  parity tests pinning the two implementations together.

Option A trade-off: subprocess overhead (~100 ms per project mutation).
Acceptable because grant changes are user-driven (GUI clicks), not
hot-path. Win: single source of truth for byte layout, no parity to
maintain.

## Tests

* `tests/test_config_projection.py` — unit tests for `project_env_from_db`,
  `apply_project_env`, CLI entry points (31 tests).
* `tests/test_config_projection_byte_identical.py` — parity-by-shared-
  assertion against the Rust `write_project_env_files_creates_both_paths`
  test in `projects_v2.rs` (8 tests).
* `tests/test_config_projection_single_writer.py` — CI lint that
  forbids new direct writes to env surfaces outside the allowlisted
  legacy writers (4 tests).

## Out of scope

* User-bucket secrets (`user_secret_pairs` / `user_secret_known_keys`
  in `ProjectEnvSettings`). Lifecycle B's active-flag gate is currently
  Rust-only; bridging to Python is a follow-up.
* `GITHUB_TOKEN` keychain-resolved emission. Stays in the Rust resolver
  until a keychain bridge from Python exists.

## Phase 0.D — `.env` template contract (2026-05-25)

Phase 0.B explicitly carved out `<project_root>/.env` as out-of-scope (different rules, different audience). Phase 0.D builds the parallel contract:

* **Writer module**: `vco_lib/env_template.py` (sibling of `vco_lib/config_projection.py`).
* **Public API**: `project_env_template_from_db(project_id)` (resolver, filters Phase 0.B canonical to a curated subset), `apply_env_template(keys, project_folder)` (writer, block-replace under `# >>> VCO-MANAGED ENV (do not edit between markers) >>>` / `# <<< VCO-MANAGED ENV <<<` markers), `list_canonical_env_template_keys()` (closed subset).
* **CLI**: `python -m vco_lib.env_template {apply,list-keys,from-db}` (mirrors Phase 0.B CLI shape for Rust subprocess callers).
* **Canonical subset**: 15/20 Phase 0.B keys — INCLUDE identity + KG + service URLs + feature flags; EXCLUDE access-lists (per-session runtime), orchestrator-root paths (launcher-install-local), `GITHUB_TOKEN` (secret).
* **Marker pattern**: `# >>> VCO-MANAGED ENV (do not edit between markers) >>>` / `# <<< VCO-MANAGED ENV <<<` — frozen byte string; intentionally louder than `.claude/env`'s markers because `.env` is human-edited. Each managed line is preceded by a forensic `# added by vco — KEY=VALUE` comment.
* **Semantics**: block-replace (idempotent, byte-identical re-runs); content outside markers preserved verbatim. Legacy `# added by vco YYYY-MM-DD` append-only lines outside the markers are preserved unchanged.
* **Cross-OS**: LF line endings forced even on Windows (POSIX-shell consumption via WSL2 / git-bash); atomic write via `tempfile.mkstemp` + `os.replace`.
* **Single-writer lint**: `tests/test_config_projection_single_writer.py` extended with `.env`-write scanners (Python / Rust / shell, anchored to `".env"` quoted literals to avoid `.envrc` / `.env.example` false positives) + `_LEGACY_ENV_TEMPLATE_WRITERS` allowlist + `_LEGACY_ENV_TEMPLATE_MARKER` = `env_template: legacy_caller_pending_migration`.
* **Migrated**: `install.py::_ensure_env_template` (delegates to `apply_env_template`).
* **Phase 0.D Part 2 (follow-up)**: full migration of Rust `ensure_project_env_template` to subprocess-into-Python (mirrors Phase 0.B Part 2 / `write_project_env_files`); `install.py`'s fresh-write branch (currently writes non-canonical install-time-only keys directly) refactored to split managed-block keys from install-snapshot keys. Both currently on the legacy allowlist with markers.

## Cross-feature integration bug + fix (2026-05-25)

When Phase 0.D's tests landed, `test_cli_from_db_happy` crashed with `sqlite3.OperationalError: no such table: diagram_access` — because A7 (cross-project diagrams access split, same release) added a `_fetch_diagram_access_list` JOIN to `project_env_from_db` that assumes migration 022 has been applied. Phase 0.D's test fixtures create a minimal launcher DB without that table.

**Fix** (`config_projection.py::_fetch_diagram_access_list`): defensive catch on `sqlite3.OperationalError` with `"no such table"` in the message → return `[]`. Pre-migration-022 DBs + partial-install scenarios now resolve cleanly with an empty diagrams-access list (semantically "no grants"). Both Phase 0.D and Phase 1 fixtures pass.

The lesson is sibling to the wiring-audit one: same-release features can break each other's test fixtures even when individual branches pass. Defensive table-existence checks for any new cross-feature query are cheap insurance.

## See also

* `.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md`
  Phase 0 §3.0 item 4 (the contract spec).
* [[relatedTo::install-pipeline-self-healing-v0213]] — earlier
  iteration of the per-project install-time backfill discipline that
  this contract supersedes for env-surface writes.
