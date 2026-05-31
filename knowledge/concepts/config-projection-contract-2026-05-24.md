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

* `GITHUB_TOKEN` keychain-resolved emission. Stays in the Rust resolver
  until a keychain bridge from Python exists.

## Phase 0.E — user-secret writes through Python contract (2026-05-25)

Phase 0.B explicitly excluded `user_secret_pairs` / `user_secret_known_keys`
because their VALUE side lives in the OS keychain (Rust-owned; no Python
bridge). Phase 0.E extends the contract to cover their WRITE side without
bridging the keychain — the asymmetry is intentional:

* The Rust caller resolves keychain VALUES via the existing
  `commands::project_env_settings::resolve_user_secret_state` code path.
* The Rust caller serialises the resolved pairs to JSON and invokes
  `python -m vco_lib.config_projection apply-user-secrets
   --project-id <id> --pairs-json <file>`.
* Python owns the **byte LAYOUT**: settings.json deep-merge,
  `.claude/env` BEGIN/END managed block (with the user-secret section
  header byte-identical to the Rust writer), `.vscode/settings.json`
  deep-merge.
* Python reads the launcher DB via `user_secret_known_keys_from_db`
  to compute the STRIP set — union of three buckets
  (`per_project`, `shared`, `global`) from `secret_active_state`.
* Pairs in input AND in known-keys are EMITTED; known-keys NOT in
  input are STRIPPED from the JSON env blocks (signal-to-remove;
  `.claude/env` strip is implicit via wholesale BEGIN/END rebuild).

### New public API

* `UserSecretBundle` typed dict — Rust-side payload shape.
* `user_secret_known_keys_from_db(project_id, db_path=None)` — STRIP
  set resolver. Dedups across the three buckets; ASCII-sorts.
* `apply_user_secrets(secret_bundle, surfaces=None)` — surface writer.
  Returns `{surface: {emitted: [...], stripped: [...]}}` for audit.
* `apply_project_env(bundle, surfaces=None, user_secret_bundle=None)`
  gained the optional `user_secret_bundle` kwarg so canonical + secrets
  can be written in ONE atomic-per-surface pass.

### New CLI verbs

* `apply-user-secrets --project-id <id> --pairs-json <file>
  [--surfaces csv] [--db-path PATH]` — exit codes 0/2/3/4/5
  (success / project_not_found / db_unreachable / apply_failed /
  pairs_json_invalid).
* `user-secret-known-keys --project-id <id> [--json] [--db-path PATH]`
  — print the STRIP set without applying.

### Three lifecycle scenarios covered

1. **Fresh secret creation** — KEY in pairs AND in known-keys
   (because `set_secret_v2` writes the active-flag row before
   triggering the env-refresh hook). EMITTED to every surface.
2. **Secret update (overwrite existing)** — same shape as creation;
   new VALUE replaces old in JSON env blocks; `.claude/env` managed
   block is rebuilt with the new pair.
3. **Secret deletion / pause** — KEY in known-keys but NOT in pairs
   (keychain returned None / active flag is 0 / keychain unreachable).
   REMOVED from JSON env blocks via explicit strip; absent from the
   rebuilt `.claude/env` managed block.

### Why asymmetric (values stay Rust-owned, layout moves to Python)

* The OS keychain APIs (Linux Secret Service, macOS Keychain Services,
  Windows Credential Manager) have well-tested Rust bindings; a Python
  parallel implementation would duplicate the soft-fail discipline
  surface for no real win.
* The cross-launcher active-flag walker
  (`db::secret_active::is_secret_active_cross_launcher`) does
  sibling-DB discovery + version-tolerant column probing; re-
  implementing it defensively in Python doubles the bug surface.
* Two implementations of the same security-sensitive lifecycle is
  worse than one — but Python owning LAYOUT (shared with the canonical
  writer) eliminates the byte-drift risk Phase 0.B already solved.

### Tests (36 new in `tests/test_config_projection_user_secrets.py`)

* DB resolver: 10 cases including soft-fail on missing table,
  per-bucket coverage, dedup, sort, inactive-row inclusion, cross-
  project isolation, DbUnreachable.
* `apply_user_secrets`: 11 cases covering the three lifecycle
  scenarios across both JSON and `.claude/env` surfaces, idempotence,
  atomicity, double-quote escaping, opt-in VS Code surface.
* `apply_project_env(user_secret_bundle=...)` combined pass: 2 cases
  asserting canonical + secrets in one atomic-per-surface write
  with correct BYTE ORDER (canonical first, secrets after in
  `.claude/env`).
* CLI: 7 cases covering happy paths + every documented exit code
  (0/2/3/5) and edge cases (empty pairs strips known; invalid pair
  shape; omitted --pairs-json treated as empty).

The single-writer lint allowlist (`_LEGACY_PRODUCTION_WRITERS`) stays
EMPTY post-Phase 0.E. The Rust caller that triggers
`apply-user-secrets` is the existing `apply_project_env_via_python`
subprocess bridge — no new direct env-surface writer was introduced.

### Out of scope (still)

* Bridging the OS keychain into Python (see "Why asymmetric" above).
* Cross-launcher sibling-DB walker in Python (see same).

## Phase 0.B Part 2 — empty legacy-writer allowlist (2026-05-25)

The legacy direct writers were migrated:

* **Rust** (`projects_v2.rs`): production callers (`create_project_v2`,
  `rename_project_v2`, `set_shared_kg_write_disabled`,
  `refresh_project_env_with_db`) now go through
  `apply_project_env_via_python` which subprocesses
  `python -m vco_lib.config_projection apply --project-id <id>`.
  The legacy `write_project_env_files` body is retained (still used
  by the user-secret SecretsPanel flow + `#[cfg(test)]` byte-layout
  fixtures); just no longer reached from production code paths.
* **Python** (`install.py`, `vco_lib/project_init.py`): the
  `_backfill_*_env_in_project` helpers are bypassed in favor of
  direct `apply_project_env(project_env_from_db(...))` calls
  (Python→Python, no subprocess overhead).
* **`_LEGACY_PRODUCTION_WRITERS`** in
  `tests/test_config_projection_single_writer.py` is now EMPTY.
  A new regression-guard test
  (`test_legacy_writers_allowlist_is_empty_post_part_2`) fails
  loudly if anyone re-introduces a direct writer.

**Subprocess pattern (Rust → Python)**:
- argv: `<python> -m vco_lib.config_projection apply --project-id <id>`
- python resolution chain: `$VCT_VENV` → `$VCT_INSTALL_ROOT/.venv` →
  `$VCT_INSTALL_ROOT/claude_mcp_servers/.venv` → walks up from
  `current_exe()` (8 parents) → `python3` / `python.exe` on PATH.
- `env_clear()` + re-inject: `PATH`, `VCT_STATE_DIR`, `VCT_HUB_PORT`,
  `VCT_HUB_TOKEN`, `VCT_INSTALL_ROOT`, `TEMP`/`TMP`/`TMPDIR`, `HOME`
  (POSIX) or `USERPROFILE` + `APPDATA` + `LOCALAPPDATA` + `HOMEDRIVE`
  + `HOMEPATH` (Windows).
- `current_dir = folder`.
- Timeout: 30s wall-clock, polled at 50ms.

**Known regression**: user-secret writes (`GITHUB_TOKEN` from
SecretsPanel) no longer reach env surfaces via the create/rename/refresh
paths because the Python contract explicitly excludes user secrets
(active-flag gate lives in Rust). Tracked as Phase 0.E. Mitigation
today: SecretsPanel triggers `refresh_env_after_user_secret_change`
which still ends up at the Python writer; the active-set surface
only re-converges when the user toggles a secret in the GUI.

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

## Production bug — `apply_project_env_via_python` silently omits `--orchestrator-root` (2026-05-27, v0.2.37 Finding F1)

Discovered during v0.2.37 audit (`/tmp/vco-wt-v0237-V37-F/.claude/context/install-update-audit-2026-05-27.md`). The Rust caller `launcher/src-tauri/src/commands/projects_v2.rs::apply_project_env_via_python` (around line 1626) builds the Python subprocess command for `vco_lib.config_projection apply` and passes `--project-id` but **NEVER** passes `--orchestrator-root`.

Downstream consequence: `config_projection.py::_cli_apply` (lines 2065-2067) sees `args.orchestrator_root is None` → constructs `EnvBundle` with `orchestrator_root=None` → the `VCT_ORCHESTRATOR_ROOT` and `VCT_INFRASTRUCTURE_DIR` exports are omitted from BOTH surfaces (`.claude/env` AND `.claude/settings.json` env block). The omission is silent — neither writer logs a warning when these keys are absent.

Affected users: ALL launcher v0.2.x users since the Phase 0.B Part 2 migration to Python writer (2026-05-25). Every project created via `create_project_v2` or refreshed via `rename_project_v2` ships without the portability keys. Every bundled script that needs `claude_mcp_servers/` then dies with `RuntimeError: claude_mcp_servers/ not found`.

Compounded by `update_project_v2` (projects_v2.rs:1352-1423) which does NOT call `apply_project_env_via_python` at all — so existing projects never recover even after the upstream fix lands.

The Rust-side `write_project_env_files` correctly handles the omit-on-None semantic (projects_v2.rs:2033-2040) AND has tests for it (lines 4904-5099) — but **it's no longer the production writer** (see projects_v2.rs:3008 comment). The tests passed; the production code path silently regressed because of unrelated migration churn.

**Fix for v0.2.37**:
- `apply_project_env_via_python` resolves orchestrator-root from `app_state["launcher.install_path"]` (canonical, DB-cached) → falls back to `find_local_repo_root()` → omits the flag if both fail. Passes `--orchestrator-root <path>` to the subprocess.
- `update_project_v2` calls `apply_project_env_via_python` after `run_install_bundle_update` to backfill env keys for existing projects on every bundle update.

**Lessons for this single-writer contract**:

1. **Single-writer pattern needs end-to-end caller contracts, not just writer contracts**. The "every surface write flows through `apply_project_env`" rule was honored. But the inputs the writer used were silently truncated by callers. Future single-writer migrations must include a contract test that exercises EVERY production caller and asserts the full `EnvBundle` shape reaches the writer.

2. **Test-passing != production-correct when the wrong function is tested**. `write_project_env_files`'s tests at lines 4904-5099 verified the correct omit-on-None behavior — but the function had been demoted from production. The KG node for that test suite should carry a `valid_until` once a writer migrates, OR the tests should explicitly assert "this is the production writer" by deferring to a SoT pointer.

3. **Wrapper-script failures with cryptic stderr are the canary for env-projection bugs**. The instambul_map symptom `claude_mcp_servers/ not found` looked like a wrapper bug. It WAS upstream — but the wrappers' error message didn't surface the env-projection failure. v0.2.38 should add a single boot-time check ("VCT_ORCHESTRATOR_ROOT empty + claude_mcp_servers/ resolvable in inferred root? You probably have a v0.2.37 pre-fix env-projection regression; run `python -m vco_lib.config_projection apply --project-id ... --orchestrator-root ...`") in the wrapper templates so the diagnosis surface is fewer-hops-away from the symptom.

Sibling node (Issue 6 from v0.2.37 backlog): scripts/wrapper-level fixes belong in their own KG node once they land — `pre-install-catalog-architecture-l0-endpoint.md` and `launcher-paid-modules-schema.md` may need a cross-link.

[[relatedTo::Multi-Module Paid Distribution — Per-Bot-User Architecture]] (same release, sibling lesson)
[[supersedes::write_project_env_files as production writer]] (the demoted function — its test suite needs `valid_until` markers in a follow-up release)

## See also

* `.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md`
  Phase 0 §3.0 item 4 (the contract spec).
* [[relatedTo::install-pipeline-self-healing-v0213]] — earlier
  iteration of the per-project install-time backfill discipline that
  this contract supersedes for env-surface writes.
