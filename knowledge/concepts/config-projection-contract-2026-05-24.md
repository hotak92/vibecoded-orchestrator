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

* The `<project_root>/.env` template file (`ensure_project_env_template`
  in Rust, `_ensure_env_template` in Python). That file uses different
  rules (append-only, `# added by vco` markers, commented placeholders)
  and a different audience (CLI users). Will be migrated through a
  parallel `apply_project_env_template` contract in a future Phase 0.D.
* User-bucket secrets (`user_secret_pairs` / `user_secret_known_keys`
  in `ProjectEnvSettings`). Lifecycle B's active-flag gate is currently
  Rust-only; bridging to Python is a follow-up.
* `GITHUB_TOKEN` keychain-resolved emission. Stays in the Rust resolver
  until a keychain bridge from Python exists.

## See also

* `.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md`
  Phase 0 §3.0 item 4 (the contract spec).
* [[relatedTo::install-pipeline-self-healing-v0213]] — earlier
  iteration of the per-project install-time backfill discipline that
  this contract supersedes for env-surface writes.
