---
title: VCO install + update audit (2026-05-27)
type: audit
tags: [installer, update-flow, audit, v0.2.37, project-bundle]
created: 2026-05-27T14:00:00Z
status: active
---

# VCO install + update audit (2026-05-27)

## TL;DR

- **15 gaps identified** (5 already-known from instambul_map report + 10 new findings)
- **Highest-leverage fix**: pass `--orchestrator-root` from Rust `apply_project_env_via_python` to the Python `vco_lib.config_projection apply` subprocess. This is the root cause of instambul_map Gap 1 (missing `VCT_ORCHESTRATOR_ROOT` exports) — the Rust `write_project_env_files` writer was correctly updated 2026-05-08 to emit these keys when `settings.orchestrator_root` is `Some`, but the production env-write path migrated to a Python subprocess (`config_projection apply`) that defaults `orchestrator_root=None` and the Rust caller never sets the CLI flag. Every project created after that migration is missing the keys; the broken state is silent.
- **Quick-win cluster (≤30 LoC each)**:
  - F1: pass `--orchestrator-root` in `apply_project_env_via_python` (~10 LoC).
  - F2: add `chunk_num`/`total_chunks`/`source_node_id` to `temporal_props` additive migration in `sync_knowledge_graph.py` (~5 LoC).
  - F3: env-var fallback in `analyze_code_graph.py` argparse (~2 LoC: `args.project or os.environ.get("CODE_GRAPH_PROJECT") or repo_path.name`).
  - F4: backport VCT_INSTALL_ROOT/-has-deps probe from `code-graph-analyze` into `kg-sync`, `kg-search`, `kg-info`, `kg-migrate`, `code-graph-query` wrappers (~30 LoC each, but bulk-fixable via shared helper).
  - F5: query_code_graph.py: same VCT_ORCHESTRATOR_ROOT fallback already used by `sync_knowledge_graph.py` (~15 LoC).
  - F8: env backfill on `update_project_v2` (run `apply_project_env_via_python` like rename does).
- **Architectural concerns deferred to v0.2.38+**:
  - A1: install.py (CLI) and launcher GUI install paths diverge in MCP registration (install.py registers MCPs via `_register_mcps`; standalone `vco_lib.project_init install-bundle` does NOT write `.claude/env` or `.claude/settings.json`).
  - A2: Supabase migrations are SQL files with no CI deploy automation (manual apply by maintainer; drift across environments is unverifiable).
  - A3: `weaviate_mcp` package isn't pip-installable; every consumer hand-rolls `sys.path.insert` from `VCT_ORCHESTRATOR_ROOT`. Consider proper `pip install -e .` layout in v0.3.

## Background investigations leveraged

- `.claude/context/plans/mcp-install-pipeline-audit-2026-05-16.md` — confirmed install.py + launcher don't auto-register MCP entries in `~/.claude.json` (already-known).
- `.claude/context/plans/install-paths-audit-2026-05-06.md` — earlier sweep of absolute-vs-portable path issues.
- `.claude/context/plans/install-adaptation-audit-2026-05-06.md` — adaptation-of-existing-projects edge cases.
- `.claude/context/plans/container-install-and-collections-audit-2026-05-16.md` — Weaviate-side install/seed flow.
- `.claude/context/plans/v0.2.34-install-path-audit-2026-05-25.md` — post-pull module install bugs (mostly addressed in v0.2.34/v0.2.35).
- `/home/martino/Desktop/instambul_map/.claude/context/INSTALL-GAPS-2026-05-27.md` — five concrete gaps observed during fresh project bootstrap.

## Surface-by-surface analysis

### Surface 1: `install.py` (root orchestrator install)

**What it does**: ~15k-LoC CLI that bootstraps the orchestrator clone itself: creates `.venv`, seeds Weaviate collections, writes `.env` config, registers MCP servers in `~/.claude.json` via `_register_mcps`, materializes optional boot service. Two modes: `install` (fresh) and `update` (preserves `.env`, Claude settings).

**What it persists**:
- `.venv/` at install root.
- `~/.claude.json` `mcpServers` entries (via `_register_mcps`, install.py:3019).
- `.env` at install root (install.py:13608+, only on first install).
- `.claude/settings.json` at orchestrator root (only on first install).
- Weaviate collections (`bootstrap_collections` from `vco_lib.project_init`).

**Idempotency**: Re-runnable; `mode == "update"` skips `_write_env_config` and `_configure_claude_settings` to preserve user edits (install.py:2779-2789).

**Known gaps**:
- MCP registration is only done by install.py (CLI) — the launcher GUI's `install_orchestrator` does NOT call `_register_mcps` (per `mcp-install-pipeline-audit-2026-05-16.md`).

**Likely latent gaps**:
- N1 (new): install.py's update mode preserves `.env` but never reconciles new env keys added by a release. Users who installed pre-v0.2.x then ran `--update` keep stale `.env` even when the orchestrator now requires keys like `DIAGRAMS_COLLECTION`. No deferral entry is emitted for this drift (unlike per-project `.claude/env` which is auto-rebuilt from launcher DB).

### Surface 2: `vco_lib/project_init.py install-bundle` (per-project install)

**What it does**: Per-project bundle install (`install_project_bundle` at vco_lib/project_init.py:5450). Copies `templates/` (agents, skills, hooks, scripts) and `infrastructure/` into the user project. Updates `.claude/.vco-manifest.json` for hash-diff drift detection.

**What it persists**:
- `.claude/{agents,skills,hooks,scripts}/*.md` and scripts.
- `.claude/.vco-manifest.json` — sha256 of shipped files for future drift detection.
- `.claude/context/UPDATE_DEFERRED.md` when conditions can't auto-resolve.
- `.claude/settings.json` (merge-mode; doesn't touch `env` block).

**What it does NOT persist**:
- `.claude/env` — written ONLY by launcher via `apply_project_env_via_python` (Python `vco_lib.config_projection apply`).
- `.claude/settings.json` `env` block — same as above.
- DB rows in launcher.db.

**Idempotency**: Manifest-driven (`install_project_bundle` at line 5450). User-modified files preserved as `preserve`; un-modified files refreshed; new files added; deleted upstream files orphan-removed if still untouched.

**Known gaps** (all 5 from instambul_map):
- Gap 1 → ROOT CAUSE F1 below (not an install-bundle bug per se — the bundle doesn't write `.claude/env` at all).
- Gap 2-3 (wrapper scripts venv/PYTHONPATH) → templates/scripts not bundle bug, but the wrappers shipped by the bundle have venv-resolution bugs (F4/F5).
- Gap 4 (chunking schema migration) → bundled `sync_knowledge_graph.py` (templates/scripts/sync_knowledge_graph.py:723) → F2.
- Gap 5 (CODE_GRAPH_PROJECT env not honored) → bundled `analyze_code_graph.py` (templates/scripts/analyze_code_graph.py:4512) → F3.

**Likely latent gaps**:
- N2: `install-bundle` standalone (run without launcher) leaves the project with NO `.claude/env` and no `.claude/settings.json` env block — the entire project is non-functional until the launcher writes those. There is no error/warning printed.
- N3: `install-bundle --update` doesn't re-run env writes even when called from the launcher (`update_project_v2` at projects_v2.rs:1352–1423: calls `run_bootstrap_collections`, `run_migrate_dry_run`, `run_install_bundle_update` — NOT `apply_project_env_via_python`). New canonical env keys (e.g., `DIAGRAMS_COLLECTION` was added between v0.2.30 and v0.2.34) won't reach existing projects on bundle update.

### Surface 3: launcher self-update (`commands/self_update.rs`)

**What it does**: Updates the launcher binary in-place by `git pull`-ing the orchestrator clone, rebuilding Cargo/npm bundles if changed, restarting the launcher. Pins `vco_upstream` remote independently of `origin` (so forks don't pull from the fork URL).

**What it persists**:
- New launcher binary at `launcher/dist/<target>/vct-launcher[.exe]`.
- `~/.vct/self-update-state.json` — last-check timestamp, cached status.

**Idempotency**: Refuses non-fast-forward pulls; uses `pre-pull-rename` on Windows to handle file-locking. Detects diverged user files and emits a deferral structure to the FE rather than auto-clobbering.

**Known gaps**: None new.

**Likely latent gaps**:
- N4: self-update doesn't trigger `apply_project_env_via_python` for any registered project. Same staleness shape as N3: launcher version V exports key K; user updates launcher to V+1 which adds key K'; until the next `create_project_v2` or rename, every project carries V's env block (missing K').

### Surface 4: GitHub Release CI (`.github/workflows/release.yml`)

**What it does**: On `push: tags v*.*.*`, builds vct-launcher + vct-hub on Linux/Windows/macOS-ARM, signs with Sigstore attestation, uploads release archive, and auto-commits binaries back to `main` via `commit-dist-binaries` job.

**What it persists**:
- GitHub Release with archive + .sha256.
- Workflow artifacts for QA.
- Direct commit to `main` updating `launcher/dist/<target>/vct-{launcher,hub}[.exe]`.

**Idempotency**: Re-runnable via `workflow_dispatch`. Auto-commit skipped if binaries are byte-identical to HEAD (release.yml:835).

**Known gaps**:
- DIST_COMMIT_TOKEN required on user-owned/protected-branch repos (release.yml:722-734). Already documented; failure is visible via warning step.

**Likely latent gaps**:
- N5: `manifest-validate.yml` gate only validates committed paid-module fixtures; it does NOT verify that the version in `Cargo.toml` matches the tag pushed (drift between source version + release tag can silently happen — saw historical hotfix `32bb935` for v0.2.33 force-re-pointing the tag).
- N6: no CI step ensures `templates/scripts/sync_knowledge_graph.py` schema matches `vco_lib.weaviate_schema` (the canonical class definition). Schema drift discovered only at runtime by the user (Gap 4).
- N7: no end-to-end install smoke test in CI (`install.py` + `vco_lib.project_init install-bundle` against a fresh tmpdir + tmp Weaviate). The instambul_map gaps would have been caught by such a test.

### Surface 5: Supabase migrations + edge function deploys

**What it does**: SQL migrations live at `launcher/supabase/migrations/*.sql` (10 files). Edge functions live at `launcher/supabase/functions/` (not surveyed in this audit).

**What it persists**: Database schema on the user's / hosted Supabase project. There is **no CI automation** to apply migrations or deploy edge functions.

**Idempotency**: SQL migrations are presumably written idempotently (file naming convention + Supabase's migration tracker), but unverifiable without CI lint.

**Known gaps**: No CI deploy → maintainer must manually apply via `supabase db push` (and edge functions via `supabase functions deploy`). Risk: prod Supabase drifts from repo state silently.

**Likely latent gaps**:
- N8: no migration linter (no enforced "every PR touching `supabase/migrations/` must add a numbered file"). A contributor could edit existing migration files in place, breaking Supabase's checksum guarantee.
- N9: no schema-drift detection between `supabase/migrations/*.sql` and the Rust types that consume the schema (`launcher/src/lib/supabase.ts` for the FE, but no equivalent Rust-side schema artifact). The recent `20260526_rebind_admin_machine.sql` (added v0.2.36) has no associated Rust test.

## Cross-cutting findings

### Finding F1: `apply_project_env_via_python` does NOT pass `--orchestrator-root`

**Symptom**: Fresh projects' `.claude/env` and `.claude/settings.json` env block are missing `VCT_ORCHESTRATOR_ROOT` and `VCT_INFRASTRUCTURE_DIR`. Every bundled script that needs `claude_mcp_servers/` then fails with `RuntimeError: claude_mcp_servers/ not found`.

**Affected users**: ALL users on launcher v0.2.x (post the Rust→Python env-writer migration in Phase 0.B Part 2, 2026-05-25). Both new projects via `create_project_v2` and refresh calls.

**Root cause**:
- `launcher/src-tauri/src/commands/projects_v2.rs:1626-1645` builds the Python subprocess command for `vco_lib.config_projection apply`. It passes `--project-id` but NOT `--orchestrator-root`.
- `vco_lib/config_projection.py:1241-1248` only emits `VCT_ORCHESTRATOR_ROOT` and `VCT_INFRASTRUCTURE_DIR` when `orchestrator_root is not None`.
- Default in `_cli_apply` (config_projection.py:2065-2067): `orchestrator_root=Path(args.orchestrator_root) if args.orchestrator_root else None` — and the launcher never sets the flag.
- The Rust `write_project_env_files` function (projects_v2.rs:1931) still has correct logic for these keys (lines 2033-2040), and the unit tests at lines 4904-5099 verify the correct emission — but that function is no longer the production writer. See projects_v2.rs:3008: "`write_project_env_files` is no longer the production writer."

**Fix scope**: Add 5 lines to `apply_project_env_via_python` in projects_v2.rs: resolve install root from `VCT_INSTALL_ROOT` env var (or via `find_local_repo_root`) and pass `--orchestrator-root <path>` to the subprocess.

**Severity**: **P0** — every fresh project is broken; the breakage is silent until the user runs a bundled script.

### Finding F2: Additive KG schema migration omits chunking properties

**Symptom**: Pre-existing per-project KG collections (created before chunking landed) fail every sync with `no such prop with name 'chunk_num' found in class '<Project>_KnowledgeGraph'`.

**Affected users**: Anyone porting an existing project from a pre-chunking VCO install. Confirmed on instambul_map.

**Root cause**: `templates/scripts/sync_knowledge_graph.py:692-780`. The `ensure_collection_exists` function's `temporal_props` dict (line 723) lists `created`, `updated`, `valid_from`, `valid_until`, `status`, `content_hash`, plus separate adds for `typed_links`, `external_links`, `linksTo` — but NOT `chunk_num`, `total_chunks`, `source_node_id`. The fresh-creation path at line 845-847 has them correctly; only the additive-on-existing path misses them.

**Fix scope**: ~5 lines: add a `chunking_props` dict with the three INT/TEXT props and append the same `add_property` loop.

**Severity**: **P0** for affected users. Fix is trivial.

### Finding F3: `analyze_code_graph.py` ignores `CODE_GRAPH_PROJECT` env

**Symptom**: Running `.claude/scripts/code-graph-analyze .` (with no `--project` arg) creates Weaviate classes named after the repo directory (e.g., `Instambul_map_CodeFunction`), not the `CODE_GRAPH_PROJECT="Instambul1860"` declared in `.claude/env`. Result: KG collection prefix and code-graph prefix diverge silently.

**Affected users**: Anyone running the analyzer wrapper without `--project` explicitly. Project sanitized-name != repo-folder-name is common (renamed folders, casing).

**Root cause**: `templates/scripts/analyze_code_graph.py:4511-4512`:
```python
if not project_name:
    project_name = args.project or repo_path.name
```
No `os.environ.get("CODE_GRAPH_PROJECT")` fallback. The env var IS already exported by `.claude/env` (and `.claude/settings.json` env reaches subprocesses), but the script never reads it.

**Fix scope**: 2 lines: `project_name = args.project or os.environ.get("CODE_GRAPH_PROJECT") or repo_path.name`.

**Severity**: **P1**. Silent divergence; user discovers when KG/codegraph queries return zero hits.

### Finding F4: Wrapper scripts' venv probe doesn't fall back to install root

**Symptom**: `kg-sync`, `kg-search`, `kg-info`, `kg-migrate`, `code-graph-query` fail with `weaviate-client not installed` when run from a project that has no `<project>/.venv` with weaviate-client. The wrappers ONLY probe `$PROJECT_ROOT/.venv` and `$PROJECT_ROOT/claude_mcp_servers/.venv`.

**Affected users**: Every fresh OSS install. The orchestrator's `.venv` is at `$VCT_INSTALL_ROOT/.venv`, not under the user project.

**Root cause**: `templates/scripts/kg-sync:11-16`, `kg-search:7-13`, `kg-info:7-13`, `kg-migrate:18-23`, `code-graph-query:12-18`. The `code-graph-analyze` wrapper (templates/scripts/code-graph-analyze:34-39) already has the correct probe order (VCT_INSTALL_ROOT first, with `venv_has_analyzer_deps` validation) — this is the canonical pattern to backport.

**Fix scope**: ~30 LoC each, OR extract a shared `.claude/scripts/_venv_probe.sh` helper and source it from all 5 wrappers (cleaner). PowerShell siblings need parallel fix.

**Severity**: **P1**. Every freshly-bundled project hits this on first KG/codegraph operation.

### Finding F5: `query_code_graph.py` hardcodes claude_mcp_servers path relative to project

**Symptom**: `code-graph-query search "..."` fails with `No module named 'weaviate_mcp'`.

**Affected users**: Every user project (claude_mcp_servers/ does not exist there).

**Root cause**: `templates/scripts/query_code_graph.py:47-50` hardcodes `_PROJECT_ROOT = Path(__file__).resolve().parents[2]` → `<project_root>/claude_mcp_servers`. Doesn't check `VCT_ORCHESTRATOR_ROOT`. The sibling `sync_knowledge_graph.py:44-58` and `search_knowledge.py:73-80` already have the right pattern (env-var first, in-tree fallback).

**Fix scope**: ~15 LoC: drop in `_resolve_mcp_servers_dir()` from `sync_knowledge_graph.py`.

**Severity**: **P1**. Same blast radius as F4.

### Finding F6: `populate_per_project_state` flow asymmetric vs `update_project_v2`

**Symptom**: A project created with launcher vN gets env keys K1..Kn at create time. Launcher updates to vN+1 add Kn+1; the user's existing project never gets Kn+1 even after `update_project_v2` runs.

**Affected users**: Long-lived projects. Confirmed: SD15's `.claude/settings.json` env block is missing `VCT_ORCHESTRATOR_ROOT` and `VCT_INFRASTRUCTURE_DIR` that were added by the 2026-05-08 install-flow audit.

**Root cause**: `update_project_v2` (projects_v2.rs:1352-1423) calls `run_bootstrap_collections`, `run_migrate_dry_run`, `run_install_bundle_update` — and stops. It does NOT call `apply_project_env_via_python`. Compare to `create_project_v2` (line 362) and `rename_project_v2` (line 3013) which both do.

**Fix scope**: 5 LoC: add `if let Err(e) = apply_project_env_via_python(&row.id, folder) { warnings.push(...) }` between steps 2 and 3.

**Severity**: **P1**. Silent staleness. Compounds with F1: even if F1 is fixed, existing projects don't pick up the fix until create/rename.

### Finding F7: `vco_lib.project_init install-bundle` (standalone) leaves no env file

**Symptom**: A 3rd-party user running `python -m vco_lib.project_init install-bundle --folder . --orchestrator-root /path/to/vco` (e.g., from a CI script or per-project setup hook, without going through the launcher) gets a project with `.claude/{agents,skills,hooks,scripts}/` populated but NO `.claude/env` and NO env block in `.claude/settings.json`. Every bundled script then fails because the env-keys it expects don't exist.

**Affected users**: Anyone scripting bundle installs outside the launcher (3rd-party fork integrators, CI users, advanced devs).

**Root cause**: `vco_lib.project_init.install_project_bundle` (vco_lib/project_init.py:5450) is purely a file-copy + manifest writer. The env-writing contract lives in `vco_lib.config_projection.apply_project_env` which is invoked separately by the launcher; install-bundle never invokes it.

**Fix scope**: Two options:
  1. (Quick) Print a clear warning at end of `install-bundle` standalone runs: "Reminder: env files were NOT written. If running outside the launcher, follow up with `python -m vco_lib.config_projection apply --project-id ... --orchestrator-root ...` (note: requires launcher DB row to exist)."
  2. (Better) Add `install-bundle --write-env` flag that resolves a sensible env bundle (using `--project-name` instead of DB row) and calls `apply_project_env` directly.

**Severity**: **P2**. Niche today but blocks the OSS-developer story.

### Finding F8: install.py CLI doesn't reconcile `.env` keys on update

**Symptom**: An install.py user who pre-installed at v0.2.10 (when `DIAGRAMS_COLLECTION` didn't exist) and ran `install.py --update` after v0.2.34 (where Diagrams ships) keeps a `.env` with no `DIAGRAMS_COLLECTION` line. The MCP server then falls back to the bundled default, silently.

**Affected users**: install.py CLI users on long-running orchestrator clones.

**Root cause**: install.py:2779-2783 skips `_write_env_config` entirely on `mode == "update"`.

**Fix scope**: Either (a) emit a deferral entry naming missing keys + giving the user the line to add, or (b) additive write: parse existing `.env`, add missing keys at the end, never touch user-set values. The bundle-update path's `bundle_user_modified_preserved` pattern is the right shape.

**Severity**: **P2**. Mostly hits dev/maintainer-style installs.

### Finding F9: No CI guard against schema drift between fresh-create + additive-migrate paths

**Symptom**: Gap 4 (chunking props missing from additive migration) is exactly this — fresh-create schema in `sync_knowledge_graph.py:845-847` lists three props that the additive path doesn't add (line 723).

**Affected users**: Project porters / long-lived KG collections.

**Root cause**: The two paths are independent code blocks within the same function. No invariant check enforces "every prop in fresh-create must appear in additive-migrate".

**Fix scope**: Refactor into a single canonical prop list + iterate both creation and migration over it. ~20 LoC + unit test.

**Severity**: **P2**.

### Finding F10: Supabase migrations + edge functions have no deploy automation

**Symptom**: Maintainer manually applies migrations to prod Supabase. No CI check that the repo's migrations are in sync with the live DB.

**Affected users**: Maintainer (operational risk), and downstream users hitting auth/license endpoints that depend on schema.

**Root cause**: No GitHub Action invokes `supabase db push` or `supabase functions deploy`.

**Fix scope**: Add a `supabase.yml` workflow on tag push that runs `supabase db push --dry-run` (gate the actual push behind a manual `workflow_dispatch` to avoid auto-applying to prod).

**Severity**: **P2** (architectural).

## Recommendations for v0.2.37 (in scope)

Concrete fixes ≤30 LoC each, no architectural rework:

1. **F1**: `apply_project_env_via_python` — add `--orchestrator-root` arg using `find_local_repo_root()` or `VCT_INSTALL_ROOT` env. (projects_v2.rs:1626)
2. **F2**: `sync_knowledge_graph.py::ensure_collection_exists` — add `chunking_props` dict + additive migration loop. (templates/scripts/sync_knowledge_graph.py:723)
3. **F3**: `analyze_code_graph.py` — read `CODE_GRAPH_PROJECT` env as fallback. (templates/scripts/analyze_code_graph.py:4512)
4. **F5**: `query_code_graph.py` — port the env-var probe from `sync_knowledge_graph.py`. (templates/scripts/query_code_graph.py:47)
5. **F6**: `update_project_v2` — call `apply_project_env_via_python` after `run_install_bundle_update`. (projects_v2.rs:1394)
6. **F4 (partial)**: Backport venv probe pattern from `code-graph-analyze` to `code-graph-query` first (highest blast radius); the four other wrappers next. (templates/scripts/code-graph-query, kg-sync, kg-search, kg-info, kg-migrate)

Combined: ~80 LoC of changes, all bug-fix-only. Recommended as v0.2.37.

## Recommendations for v0.2.38+ (architectural)

1. **A1**: Pip-installable `weaviate_mcp` package (vco_lib + weaviate_mcp shipped as wheels). Removes all sys.path/VCT_ORCHESTRATOR_ROOT plumbing.
2. **A2**: Bundle-install standalone should be self-sufficient (F7) — either write env defaults or refuse to run without a launcher-DB-row reference.
3. **A3**: install.py CLI update flow should reconcile `.env` keys (F8).
4. **A4**: CI schema-drift test for sync_knowledge_graph fresh vs migrate paths (F9).
5. **A5**: CI smoke test that does `install.py` + `vco_lib.project_init install-bundle` + first KG sync against a tmp Weaviate. Catches F1-F5 class of regressions automatically. (N7)
6. **A6**: Supabase migration deploy in CI (F10/N8/N9).

## Appendix: command traces

### Fresh install of fresh project on fresh machine

1. User clones VCO_dev, runs `first-install.sh` → invokes `install.py` (CLI fresh mode).
2. install.py:
   - `_setup_venv()` → `.venv/` at install root.
   - `_seed_collections()` → Weaviate KG/dev collections (shared).
   - `_write_env_config()` → `.env` at install root.
   - `_configure_claude_settings()` → install-root `.claude/settings.json`.
   - `_register_mcps()` → `~/.claude.json` mcpServers entries.
3. User opens launcher GUI; launcher's `app_state` records install path.
4. User clicks "Create project" → `create_project_v2`:
   - `apply_project_env_via_python(project_id, folder)` — **FAILS to pass `--orchestrator-root` (F1)** → `.claude/env` and `.claude/settings.json` env block written without `VCT_ORCHESTRATOR_ROOT`/`VCT_INFRASTRUCTURE_DIR`.
   - `run_bootstrap_collections` → per-project collections seeded.
   - `run_install_bundle` → templates copied, `.vco-manifest.json` written.
5. User opens project in Claude Code, runs `kg-sync` → wrapper script can't find `claude_mcp_servers/` (F1+F4+F5) → falls back / crashes.

**Break point**: Step 4 sub-step 1, silently.

### Update of stale project (SD15) through 3+ launcher versions

1. Project created with launcher v0.2.10 (some old version).
2. User updates launcher (`self_update`) to v0.2.36.
3. New env keys (`DIAGRAMS_COLLECTION`, `VCT_ORCHESTRATOR_ROOT`, etc.) ship in v0.2.x.
4. User opens launcher, clicks "Update bundle" on SD15 → `update_project_v2`:
   - `run_bootstrap_collections` (idempotent — fine).
   - `run_migrate_dry_run` → writes `schema_migration_required` deferral if collections need migration (confirmed in SD15's UPDATE_DEFERRED.md).
   - `run_install_bundle_update` → templates refreshed via manifest diff.
   - **NO `apply_project_env_via_python` call (F6)** → `.claude/env` and `.claude/settings.json` env block remain at v0.2.10 shape.
5. The user's `.claude/env` is missing `DIAGRAMS_COLLECTION`, `VCT_ORCHESTRATOR_ROOT`, etc. Bundled scripts that depend on these silently use bundled defaults (wrong collection) or crash (`claude_mcp_servers/ not found`).

**Break point**: Step 4 silently.

### Standalone `install-bundle` outside the launcher

```
$ python -m vco_lib.project_init install-bundle --folder /tmp/myproj \
    --orchestrator-root /home/user/VCO_dev
```

1. Creates `/tmp/myproj/.claude/{agents,skills,hooks,scripts}/`.
2. Writes `.claude/.vco-manifest.json`.
3. Does NOT write `.claude/env`.
4. Does NOT write `.claude/settings.json` env block.
5. Returns 0.

User then tries any bundled wrapper → "claude_mcp_servers/ not found", "KG_COLLECTION undefined", etc. (F7)

**Break point**: Step 3 silently.

---

End of audit.
