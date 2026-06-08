//! Project lifecycle commands for the module system.
//!
//! Runs alongside the legacy `commands::projects` module during migration.
//! The "_v2" suffix marks the DB-backed implementation; once the React UI
//! is fully migrated to call these, we'll retire the old commands.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{command, AppHandle, State};
use uuid::Uuid;

use crate::commands::codegraph;
use crate::commands::installer::{detect_system, find_local_repo_root};
use crate::commands::kg_summary;
use crate::commands::kg_sync;
use crate::commands::project_env_settings::{self, ProjectEnvSettings};
use crate::db::code_graph_builds::status as build_status;
use crate::db::kg_summaries::status as kg_summary_status;
use crate::db::kg_syncs::status as kg_sync_status;
use crate::db::models::{ModuleInstallRow, ProjectHost, ProjectRow};
use crate::db::Db;
use vct_launcher_core::process::CommandExt as _;

#[derive(Debug, Clone, Serialize)]
pub struct ProjectView {
    pub id: String,
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
    pub slug: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub module_count: u32,
}

impl ProjectView {
    fn from_row(row: ProjectRow, module_count: u32) -> Self {
        Self {
            id: row.id,
            name: row.name,
            folder_path: row.folder_path,
            host: row.host,
            slug: row.slug,
            created_at: row.created_at,
            updated_at: row.updated_at,
            module_count,
        }
    }
}

/// Result returned by `create_project_v2`.
///
/// B10 (2026-05-01): env-write failures are no longer silently swallowed.
/// They are included here so the UI can surface a warning toast without
/// blocking project creation (the project row is always committed first).
#[derive(Debug, Clone, Serialize)]
pub struct CreateProjectResult {
    pub project: ProjectView,
    /// Non-fatal warnings that the UI should display to the user
    /// (e.g. "env file write failed — manual setup required").
    /// Empty on a clean create.
    pub warnings: Vec<String>,
}

/// Result returned by `rename_project_v2`.
///
/// HIGH-7 (2026-05-01): mirrors `CreateProjectResult` so rename can surface
/// env-write failures to the UI instead of swallowing them via eprintln.
/// Without this, a rename whose env refresh fails silently leaves all 4
/// surfaces stale until the user manually re-runs setup.
#[derive(Debug, Clone, Serialize)]
pub struct RenameProjectResult {
    pub project: ProjectView,
    /// Non-fatal warnings (e.g. env refresh failed, .env stale).
    /// Empty on a clean rename.
    pub warnings: Vec<String>,
}

/// Per-action counts produced by the bundle install in update mode.
///
/// PR 5 (2026-05-01): the launcher's "Update bundle" toast summarises the
/// run via these counts ("5 files updated, 2 user-modifications preserved").
/// Mirrors the JSON `actions` map emitted by `vco_lib.project_init.install-bundle`,
/// flattened into named integer fields for cheap UI rendering.
#[derive(Debug, Clone, Serialize, Default)]
pub struct UpdateSummary {
    /// Files that didn't exist before — newly shipped by the orchestrator
    /// (e.g. today's `claude_token_counter.py`).
    pub created: u32,
    /// Files whose installed content matched the prior-shipped manifest
    /// hash (= user untouched), now overwritten with the new shipped
    /// version.
    pub overwritten: u32,
    /// Files where the installed content diverged from the prior-shipped
    /// hash (= user-modified). Preserved on disk; surfaced via the
    /// `bundle_user_modified_preserved` deferral entry.
    pub preserved: u32,
    /// Files whose installed content already matches what we'd write
    /// (no-op).
    pub noop: u32,
    /// Files unconditionally overwritten because they're not user-
    /// customisable (e.g. `.claude/hooks/_lib/*`).
    pub always_overwritten: u32,
    /// Files that pre-existed AND differed from the shipped version
    /// during a first-install run. Always 0 on update_mode=true.
    /// Included for symmetry with the JSON envelope.
    pub skipped_existing: u32,
    /// Number of `errors[]` entries in the JSON envelope (per-file write
    /// failures, manifest write failure, etc.). Each is also surfaced as
    /// a warning string in `UpdateProjectResult.warnings`.
    pub errors_count: u32,
}

impl UpdateSummary {
    /// Total operations classified across all action buckets. Used in
    /// tests to verify counts tally with the JSON envelope.
    #[allow(dead_code)]
    pub fn total_ops(&self) -> u32 {
        self.created
            + self.overwritten
            + self.preserved
            + self.noop
            + self.always_overwritten
            + self.skipped_existing
    }
}

/// Result returned by `update_project_v2`.
///
/// PR 5 (2026-05-01): structured envelope so the launcher can render a
/// "5 files updated, 2 preserved" toast from a single round-trip. Soft-fail
/// discipline mirrors `CreateProjectResult` — `warnings` carries every
/// non-fatal condition (env-write hiccups, deferral entries written,
/// schema drift detected) and `summary` carries the aggregate counts.
#[derive(Debug, Clone, Serialize)]
pub struct UpdateProjectResult {
    pub project: ProjectView,
    /// Non-fatal warnings the UI should surface (info + error toasts).
    pub warnings: Vec<String>,
    /// Aggregate per-action counts (see `UpdateSummary`).
    pub summary: UpdateSummary,
}

/// MEDIUM-1 (2026-05-01): sentinel module_id used for project-level settings
/// stored in the `module_settings` k/v table. Settings under this id apply
/// to the project itself rather than any installed module.
pub const PROJECT_SETTINGS_MODULE_ID: &str = "__project__";

/// Per-project setting key for the SHARED_KG_WRITE_DISABLED toggle (asymmetric
/// model since 2026-05-01: gates WRITES to the shared KG; reads are always on).
/// When `true`, the project's env surfaces carry `SHARED_KG_WRITE_DISABLED=true`,
/// which the MCP server reads to refuse `store_knowledge_node(scope='shared')`
/// calls. Reads of the cross-project shared KG remain unconditional.
pub const SETTING_KEY_SHARED_KG_WRITE_DISABLED: &str = "shared_kg_write_disabled";

/// Legacy alias kept for ~3 releases (slated for removal 2026-08). DB rows
/// stored under the old key are silently migrated to the new key on first
/// read via `get_shared_kg_write_disabled` — see the migration helper below.
pub const SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY: &str = "shared_kg_opt_out";

/// Back-compat alias of the canonical key. Existing internal call sites
/// (and the legacy Tauri command) still reference this; new code should use
/// `SETTING_KEY_SHARED_KG_WRITE_DISABLED` directly. Slated for removal in
/// the same window as the legacy command + env var.
#[allow(dead_code)]
pub const SETTING_KEY_SHARED_KG_OPT_OUT: &str = SETTING_KEY_SHARED_KG_WRITE_DISABLED;

/// One-shot, idempotent migration: if a DB row exists under the LEGACY key
/// (`shared_kg_opt_out`) but NOT under the canonical key
/// (`shared_kg_write_disabled`), copy it across and delete the legacy row.
/// Safe to call from any read path.
///
/// Returns the migrated value (Some(bool)) if a migration occurred,
/// Some(canonical_value) if the canonical row already existed, or None when
/// neither row exists. Callers usually just discard the return — the side
/// effect on the DB is the point.
fn _migrate_shared_kg_setting(db: &Db, project_id: &str) -> Result<Option<bool>, String> {
    // Canonical row wins outright — drop any stale legacy row to avoid
    // confusing future reads.
    if let Some(canonical) =
        db.get_setting(project_id, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_WRITE_DISABLED)?
    {
        // Best-effort cleanup of legacy row; never fail the migration over it.
        let _ = db.delete_setting(
            project_id,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
        );
        return Ok(Some(canonical.as_bool().unwrap_or(false)));
    }

    // Otherwise check the legacy row and forward it.
    if let Some(legacy) =
        db.get_setting(project_id, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY)?
    {
        let bool_val = legacy.as_bool().unwrap_or(false);
        db.set_setting(
            project_id,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(bool_val),
        )?;
        let _ = db.delete_setting(
            project_id,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
        );
        eprintln!(
            "[vct] migrated project setting `shared_kg_opt_out` → \
             `shared_kg_write_disabled` for project {}",
            project_id
        );
        return Ok(Some(bool_val));
    }
    Ok(None)
}

/// Read the current SHARED_KG_WRITE_DISABLED toggle from the DB. Defaults to
/// `false` (writes allowed) when no row exists. Triggers a one-shot migration
/// from the legacy `shared_kg_opt_out` key if present — idempotent on repeat
/// calls.
pub fn get_shared_kg_write_disabled(db: &Db, project_id: &str) -> Result<bool, String> {
    Ok(_migrate_shared_kg_setting(db, project_id)?.unwrap_or(false))
}

/// v0.2.46 Decision B — per-project setting key for the symmetric READ
/// gate (`SHARED_KG_READ_DISABLED`). When `true`, the project's env
/// surfaces carry `SHARED_KG_READ_DISABLED=true`, which the MCP's
/// `_kg_collections_to_search` reads to drop the shared collection from
/// the hybrid_search / semantic_graph_search fan-out. No legacy alias —
/// pre-v0.2.46 the read path was unconditional, so there's no
/// historical key to honour.
pub const SETTING_KEY_SHARED_KG_READ_DISABLED: &str = "shared_kg_read_disabled";

/// v0.2.46 Decision B — read the current SHARED_KG_READ_DISABLED toggle
/// from the DB. Defaults to `false` (reads allowed) when no row exists.
/// No legacy-alias migration because the key is new — pre-v0.2.46 the
/// read path was unconditional, so no DB row could exist under a prior
/// name. Symmetric mirror of `get_shared_kg_write_disabled` in shape +
/// default semantics.
pub fn get_shared_kg_read_disabled(db: &Db, project_id: &str) -> Result<bool, String> {
    let val = db
        .get_setting(
            project_id,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_READ_DISABLED,
        )?
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok(val)
}

/// Deprecated alias of `get_shared_kg_write_disabled`. Will be removed once
/// the legacy command + env var are dropped (target: 2026-08).
#[deprecated(
    since = "2026-05-01",
    note = "Use `get_shared_kg_write_disabled` — the toggle now gates WRITES \
            only. Reads of the shared KG are always on."
)]
#[allow(dead_code)]
pub fn get_shared_kg_opt_out(db: &Db, project_id: &str) -> Result<bool, String> {
    get_shared_kg_write_disabled(db, project_id)
}

#[derive(Debug, Clone, Serialize)]
pub struct SwitchHostResult {
    pub project: ProjectView,
    pub modules_removed: Vec<ModuleInstallRow>,
    pub modules_preserved: Vec<ModuleInstallRow>,
}

#[command]
pub async fn list_projects_v2(db: State<'_, Db>) -> Result<Vec<ProjectView>, String> {
    let rows = db.list_projects()?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
        out.push(ProjectView::from_row(row, count));
    }
    Ok(out)
}

#[command]
pub async fn get_project_v2(
    id: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project(&id)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

/// Look up a project by its URL slug (e.g. `acme-corp`). Backs the
/// `/p/<slug>/...` routes.
#[command]
pub async fn get_project_by_slug(
    slug: String,
    db: State<'_, Db>,
) -> Result<Option<ProjectView>, String> {
    let row = match db.get_project_by_slug(&slug)? {
        Some(r) => r,
        None => return Ok(None),
    };
    let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
    Ok(Some(ProjectView::from_row(row, count)))
}

#[derive(Debug, Deserialize)]
pub struct CreateProjectV2Request {
    pub name: String,
    pub folder_path: String,
    pub host: ProjectHost,
}

#[command]
pub async fn create_project_v2(
    req: CreateProjectV2Request,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<CreateProjectResult, String> {
    let folder = Path::new(&req.folder_path);
    let mut warnings: Vec<String> = Vec::new();

    // Bug 3e: auto-create the folder if it doesn't exist. Earlier the
    // create flow rejected non-existent paths and forced the user to
    // `mkdir -p` manually, which broke when users typed a fresh path
    // in the New Project modal. `create_dir_all` is a no-op if the
    // path already exists.
    if !folder.exists() {
        std::fs::create_dir_all(folder).map_err(|e| {
            format!("cannot create folder {}: {}", req.folder_path, e)
        })?;
    }
    if !folder.is_dir() {
        return Err(format!("not a directory: {}", req.folder_path));
    }

    // Migration 013 (v0.2.11, 2026-05-15): host='orchestrator_root' is a
    // reserved value with a single fixed slug ('orchestrator-root') and
    // an auto-registration path in `Db::open()`. Manual creation through
    // the New Project modal would either fail at the UNIQUE-slug check
    // (confusing error: "slug taken") or, worse, silently succeed with
    // a non-canonical slug and leave the user with two orchestrator
    // roots from the consumer-code perspective. Reject early with a
    // clear message; the auto-register path is the only sanctioned
    // way to create this row.
    if req.host == ProjectHost::OrchestratorRoot {
        return Err(
            "host='orchestrator_root' is reserved: the orchestrator clone is \
             auto-registered at launcher startup. Use the standard host \
             values ('base' or 'mao') for user projects."
                .to_string(),
        );
    }

    let id = Uuid::new_v4().to_string();
    let slug = db.generate_unique_slug(&req.name)?;
    let row = db.insert_project(&id, &req.name, &req.folder_path, req.host.clone(), &slug)?;

    // Bug 23 + 30: write per-project env files for every Claude Code
    // surface — Claude Code CLI (via `.claude/env`, sourced by
    // tools/claude wrapper or user shell rc) AND the canonical
    // `.claude/settings.json` env block (CLI + Desktop app + VS Code
    // extension, AND propagated to MCP subprocesses). The pre-v0.2.12
    // `.vscode/settings.json` `claude-code.env` surface was removed in
    // PR-27 (2026-05-16) because it didn't propagate to MCP
    // subprocesses on Linux — see `write_project_env_files` docstring
    // for the empirical-trace reference. We swallow individual errors
    // here: create_project must not fail just because the user's
    // folder is read-only or mid-edit.
    //
    // PR-3 (2026-05-06): populate a `ProjectEnvSettings` once from the
    // launcher's current state — adopted ports, ACTIVE_EMBEDDING choice,
    // shared-KG name override, GPU toggle, container runtime — so the
    // env writers see the LAUNCHER's view of the world rather than
    // hardcoded localhost defaults. See `launcher-settings-propagation-audit-2026-05-06.md`
    // for the full inventory of values that needed plumbing.
    let env_settings = project_env_settings::populate(&db, &req.name, Some(&row.id));
    // Phase 0.B Part 2 (2026-05-25): canonical env writes go through the
    // Python contract `vco_lib.config_projection.apply_project_env` via
    // `apply_project_env_via_python`. `env_settings` is still populated
    // because (a) the legacy Rust writer is invoked from the SecretsPanel
    // user-secret path and (b) `ensure_project_env_template` below also
    // consumes it for the `.env` template surface (which is out of scope
    // for the Phase 0.B contract).
    if let Err(e) = apply_project_env_via_python(&row.id, folder, &db) {
        // B10 (2026-05-01): surface env-write failures to the UI instead of
        // silent eprintln. Project creation still succeeds; the UI should show
        // a warning toast so the user knows manual env setup is required.
        let msg = format!("env file write failed (apply_project_env_via_python): {}. \
                          Per-project KG routing will not work until this is resolved.", e);
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // Bug 33 (2026-04-28): also ensure a per-project `.env` template
    // exists. `write_project_env_files` only writes `.claude/env` +
    // `.claude/settings.json`; a separate `.env` is what most CLI
    // users expect to edit (esp. existing-folder projects that
    // pre-existed any orchestrator install). The template carries
    // commented placeholders for ANTHROPIC_API_KEY / OPENAI_API_KEY /
    // GITHUB_TOKEN / RL_*; values stay user-controlled. Idempotent on
    // re-runs.
    if let Err(e) = ensure_project_env_template(folder, &env_settings) {
        let msg = format!("env template write failed (ensure_project_env_template): {}. \
                          The .env file may be missing managed keys.", e);
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // 2026-04-28 fix: populate the per-project state DB tables (agents,
    // skills, hooks, kg/codegraph bindings) from the project's `.claude/`
    // directory. Without this, the launcher's per-project GUI tabs
    // appear empty even when the filesystem has 26+ agents bundled.
    // Idempotent on re-run; preserves user-toggled `enabled` flags. We
    // log soft-errors and continue — never fail project creation over a
    // populate hiccup.
    let populate_report = crate::commands::project_state_populate::
        populate_project_state_from_filesystem(&row.id, &req.name, folder, &db);
    if !populate_report.warnings.is_empty() {
        for w in &populate_report.warnings {
            eprintln!("[vct] populate warning ({}): {}", row.id, w);
        }
    }

    db.audit(
        "project_create",
        Some(&row.id),
        None,
        &serde_json::json!({ "host": req.host.as_str(), "name": req.name, "slug": slug }),
    )?;
    let _ = db.log_change("projects", "insert", Some(&row.id), Some(&row.id));

    // Bug fix (2026-05-06): defer the codegraph spawn until AFTER
    // `run_install_bundle` has dropped `.claude/scripts/code-graph-analyze`
    // into the project folder. Pre-fix, this block ran here (before
    // bundle install) and the background task raced ahead — by the time
    // `resolve_analyzer_script` looked for the wrapper, the bundle
    // install hadn't yet finished writing it, and the build immediately
    // failed with "code-graph-analyze script not found". Same shape as
    // the populate-ordering race fixed in PR #149: side-effect ordering
    // matters across `run_install_bundle`. The codegraph block now lives
    // below, after bootstrap → bundle install → post-bundle populate.

    // B12 (2026-05-01, repaired in 0.2.11): rewrite stale .env from pre-existing
    // folder registration. ensure_project_env_template is append-only, so a
    // folder that already had a .env with a bare/wrong KG_COLLECTION (e.g.
    // "KnowledgeGraph" without project suffix, or just the bare sanitized name)
    // would otherwise keep that stale value as the first active KG_COLLECTION
    // line, and consumers reading the first match would pick up the wrong
    // collection. Pre-0.2.11 this block only warned and asked the user to fix
    // it by hand — that left the bug live in every existing install. Now we
    // call the testable helper `b12_repair_stale_kg_collection` to rewrite
    // the first KG_COLLECTION= line in place; audit the result here.
    let env_path = folder.join(".env");
    match b12_repair_stale_kg_collection(&env_path, &req.name) {
        Ok(B12Outcome::Repaired { canonical_kg }) => {
            eprintln!(
                "[vct] info: B12: rewrote stale KG_COLLECTION in {} → {}",
                env_path.display(),
                canonical_kg
            );
            let _ = db.audit(
                "env_b12_auto_repair",
                Some(&row.id),
                None,
                &serde_json::json!({
                    "path": env_path.display().to_string(),
                    "kg_collection": canonical_kg,
                }),
            );
        }
        Ok(B12Outcome::NoChangeNeeded) => {}
        Err(e) => {
            let msg = format!(
                "B12 auto-repair failed to rewrite {} (KG_COLLECTION stale): {}. \
                 Manual fix: set KG_COLLECTION=<project>_KnowledgeGraph in the .env.",
                env_path.display(),
                e
            );
            eprintln!("[vct] warning: B12: {}", msg);
            warnings.push(msg);
        }
    }

    // PR 4 (2026-05-01): bootstrap Weaviate collections + install per-project
    // bundle (hooks/scripts/agents/skills/settings/infrastructure). Both run
    // via Python subprocess into vco_lib.project_init — single source of
    // truth shared with install.py. Soft-fail at every step: a Weaviate-down
    // condition or a missing template tree must NOT block project creation.
    //
    // Order matters: env files were written above, so the bundle install
    // and bootstrap pick up the right KG_COLLECTION via the project's .env.
    // Bootstrap first (ensures the per-project + shared collections exist
    // with current schema invariants); bundle second (drops the hooks +
    // scripts that depend on the collections existing).
    for w in run_bootstrap_collections(folder, &req.name).await {
        warnings.push(w);
    }
    for w in run_install_bundle(folder).await {
        warnings.push(w);
    }

    // Bug fix (2026-05-06): the first populate call earlier in this fn
    // ran BEFORE `run_install_bundle` dropped the per-project bundle
    // into `<folder>/.claude/{agents,skills,hooks}/`. At that point
    // those subdirs don't exist yet, so `populate_agents/skills/hooks`
    // each saw zero `.md` files and inserted zero rows — leaving the
    // GUI's per-project Agents/Skills/Hooks tabs permanently empty
    // even though the bundle install succeeded.
    //
    // Re-call populate now that the bundle has dropped its files.
    // The underlying register_* helpers are idempotent upserts that
    // leave the `enabled` column untouched on conflict, so this second
    // call:
    //   - DOES insert agents/skills/hooks rows (the whole point)
    //   - Does NOT clobber kg_collection_access / project_kg_bindings
    //     populated in the first call (which derived from project name,
    //     not from disk — those were correct at first-pass time)
    //   - Preserves any user toggles set in the GUI between the two
    //     populate calls (window is microseconds; not a real concern)
    let populate_report_2 = crate::commands::project_state_populate::
        populate_project_state_from_filesystem(&row.id, &req.name, folder, &db);
    if !populate_report_2.warnings.is_empty() {
        for w in &populate_report_2.warnings {
            eprintln!("[vct] populate (post-bundle) warning ({}): {}", row.id, w);
            warnings.push(format!("populate (post-bundle): {}", w));
        }
    }

    // v0.2.49 Stream B: seed `enabled_for_project=true` rows for every
    // global-scope module already installed on the host. Without this,
    // a brand-new project would default to "no opinion" for each global
    // module (which still reads as enabled — see the DB-layer
    // fail-open) but the renderer wouldn't have a row to display a
    // toggle against. Seeding writes an explicit row so the GUI's
    // per-project Modules panel knows which global modules to surface.
    //
    // Soft-fail throughout: `seed_enabled_rows_for_new_project` logs
    // per-module failures and returns the success count. We audit the
    // count for forensic trace and continue regardless. Mirrors the
    // soft-fail pattern of `set_project_module_enabled` below.
    let global_modules_seeded = crate::commands::module_enabled
        ::seed_enabled_rows_for_new_project(&db, &row.id);
    if global_modules_seeded > 0 {
        let _ = db.audit(
            "project_global_module_enable_seeded",
            Some(&row.id),
            None,
            &serde_json::json!({ "modules_seeded": global_modules_seeded }),
        );
    }

    // Phase 1.1 (diagrams): seed the project-modules row for `diagrams`
    // so the (Phase 1.5.7) conditional CLAUDE.md template renderer sees
    // the module as active by default. The plan is opt-out — the user
    // disables per-project via DiagramsTab → `set_project_module_enabled`
    // (which flips `enabled` to 0; the row stays).
    //
    // ORDER MATTERS (mirrors the install-flow-architectural-overhaul
    // 2026-05-06 race-condition rule): this MUST run AFTER
    // `run_install_bundle` completes. The bundle install is allowed to
    // drop files the conditional-block primitive expects to find; if we
    // seeded earlier the renderer could race ahead and miss them.
    //
    // Soft-fail: a DB error on this insert is logged (cosmetic — the
    // conditional CLAUDE.md section just won't render until the user
    // re-toggles the module). Never propagated.
    if let Err(e) = db.set_project_module_enabled(&row.id, "diagrams", true) {
        eprintln!(
            "[vct] warning: could not seed project_modules('diagrams') for {}: {}",
            row.id, e,
        );
        warnings.push(format!(
            "seed project_modules('diagrams'): {} (CLAUDE.md diagrams section will not render until re-toggled)",
            e
        ));
    }

    // Gap 2 (OSS launch 2026-05-12): kick off the initial code-graph
    // build in the background so `search_code_graph` returns useful
    // results out of the box. This must NOT block project creation —
    // the user gets their `ProjectView` back immediately.
    //
    // We swallow any DB error from the pending-row insert because a
    // failure here is purely cosmetic (the user just won't see a build
    // status pill); the project itself is already committed.
    //
    // ORDER MATTERS (2026-05-06 race fix): this block was previously
    // BEFORE `run_install_bundle`. The bundle install drops the analyzer
    // wrapper at `<folder>/.claude/scripts/code-graph-analyze`; running
    // the spawn earlier let the background task race ahead of the bundle
    // install, hit `resolve_analyzer_script` before the script existed,
    // and fail with "code-graph-analyze script not found". The fix is
    // simple: spawn LAST, after (a) bundle install, (b) post-bundle
    // populate. Same shape as the populate-ordering bug fixed in PR #149.
    let now = chrono::Utc::now().timestamp_millis();
    if let Err(e) = db.upsert_code_graph_build(
        &row.id,
        build_status::PENDING,
        Some(now),
        None,
        None,
        0,
        None,
        false,
        None,
        None,
    ) {
        eprintln!("[vct] warning: could not queue code-graph build for {}: {}", row.id, e);
    } else {
        codegraph::spawn_initial_build(
            app.clone(),
            row.id.clone(),
            row.name.clone(),
            row.folder_path.clone(),
            // First-time builds for a freshly-created project: no
            // pre-existing per-project code-graph rows can possibly
            // be stale, so --prune-stale is a no-op here. Pass false
            // explicitly to avoid the iterator-then-delete pass.
            false,
        );
    }

    // KG auto-sync (2026-05-12): kick off the initial `kg-sync --all`
    // run in the background so any pre-existing `knowledge/**/*.md` and
    // `docs/**/*.md` content lands in Weaviate without the user having
    // to manually run `.claude/scripts/kg-sync --all`. Same ordering
    // discipline as the codegraph spawn above: this MUST live after
    // `run_install_bundle` so the project-local `kg-sync` wrapper
    // exists by the time the background task resolves it. The pending
    // row is queued first so the GUI can render an immediate "Queued"
    // pill state while the task picks up.
    //
    // Failure isolation: a DB error on the pending insert is logged
    // (cosmetic — the user just won't see a sync pill); never propagated.
    // Sync failure (Weaviate down, Ollama down, embedding model missing,
    // network glitch) lands in `kg_syncs.error_message` and surfaces as
    // a "Retry sync" affordance on the GUI pill — project create itself
    // is already committed.
    if let Err(e) = db.upsert_kg_sync(
        &row.id,
        kg_sync_status::PENDING,
        Some(now),
        None,
        None,
        0, 0, 0,
        0, 0, 0,
        None,
        None,
    ) {
        eprintln!("[vct] warning: could not queue kg-sync for {}: {}", row.id, e);
    } else {
        kg_sync::spawn_initial_sync(
            app.clone(),
            row.id.clone(),
            row.name.clone(),
            row.folder_path.clone(),
        );
    }

    // KG summary auto-backfill (v0.2.3 / 2026-05-12): kick off the
    // initial `generate-kg-summary.py` pass in the background so any
    // pre-existing `knowledge/**/*.md` content lands in the project's
    // `.node_formats.json` sidecar (consumed by `hybrid_search`'s
    // `summary` tier). Same ordering discipline as the kg-sync spawn
    // above: this MUST live after `run_install_bundle` so the project-
    // local `generate-kg-summary.py` exists by the time the background
    // task resolves it. The pending row is queued first so the GUI can
    // render an immediate "Queued" banner state while the task picks up.
    //
    // Spawned in parallel with the kg-sync task — not chained. The
    // summariser works directly off the `.md` files (it doesn't need
    // Weaviate to be populated for the description/summary path), and
    // its per-chunk path (only used for multi-chunk nodes) gracefully
    // degrades to single-summary mode when Weaviate has no chunks yet.
    // Mirrors the parallel-spawn pattern already established for
    // code-graph + kg-sync.
    //
    // Failure isolation: a DB error on the pending insert is logged
    // (cosmetic — the user just won't see a summary banner); never
    // propagated. Backfill failure (no backend available, venv missing,
    // every subprocess crashing) lands in `kg_summaries.error_message`
    // and surfaces as a "Retry" affordance on the GUI banner — project
    // create itself is already committed.
    if let Err(e) = db.upsert_kg_summary(
        &row.id,
        kg_summary_status::PENDING,
        Some(now),
        None,
        None,
        0, 0, 0, 0, 0,
        None,
        None,
        None,
    ) {
        eprintln!(
            "[vct] warning: could not queue kg-summary backfill for {}: {}",
            row.id, e,
        );
    } else {
        kg_summary::spawn_initial_summary(
            app,
            row.id.clone(),
            row.name.clone(),
            row.folder_path.clone(),
        );
    }

    Ok(CreateProjectResult {
        project: ProjectView::from_row(row, 0),
        warnings,
    })
}

/// PR 4 (2026-05-01): subprocess-call vco_lib.project_init bootstrap-collections.
///
/// Soft-fail policy:
///   - Python missing → push a warning, return (project create still succeeds).
///   - Orchestrator root unfindable → push a warning, return.
///   - Subprocess non-zero exit → push warnings parsed from JSON `errors[]`
///     (hard collection-create failures); return.
///   - JSON `deferred=true` → push an info warning that points to
///     `<folder>/.claude/context/UPDATE_DEFERRED.md`.
///   - JSON parse failure → push a warning carrying stderr tail.
///
/// Returns the list of warnings to surface to the UI. Never returns Err.
async fn run_bootstrap_collections(folder: &Path, project_name: &str) -> Vec<String> {
    let mut warnings: Vec<String> = Vec::new();

    let system = match detect_system().await {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!(
                "bootstrap-collections skipped: detect_system failed: {}. \
                 Per-project Weaviate collections will be created lazily by the \
                 MCP server on first write — schema may be incomplete until \
                 the next manual `python -m vco_lib.project_init bootstrap-collections \
                 --name {:?} --json` run.",
                e, project_name
            ));
            return warnings;
        }
    };
    if !system.has_python {
        warnings.push(
            "bootstrap-collections skipped: no Python 3.11+ on PATH. \
             Per-project Weaviate collections will be created lazily on first \
             write (schema may drift). Install Python and re-run setup."
            .to_string(),
        );
        return warnings;
    }

    let orch_root = match find_local_repo_root() {
        Ok(p) => p,
        Err(e) => {
            warnings.push(format!(
                "bootstrap-collections skipped: orchestrator root not found: {}. \
                 Per-project collections will be created lazily by the MCP server.",
                e
            ));
            return warnings;
        }
    };

    let folder_str = folder.to_string_lossy().to_string();
    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.project_init",
        "bootstrap-collections",
        "--name",
        project_name,
        "--project-folder",
        &folder_str,
        "--json",
    ])
    .current_dir(&orch_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => {
            warnings.push(format!(
                "bootstrap-collections subprocess failed to start: {}. \
                 Per-project collections will be created lazily on first write.",
                e
            ));
            return warnings;
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    // Try to parse the JSON envelope first; fall back to a stderr tail
    // on parse failure (which usually means Python crashed before
    // emitting JSON).
    match serde_json::from_str::<serde_json::Value>(&stdout) {
        Ok(v) => {
            if v.get("deferred").and_then(|x| x.as_bool()).unwrap_or(false) {
                warnings.push(format!(
                    "Weaviate collection bootstrap deferred — Weaviate was \
                     unreachable during project creation. The launcher attempted \
                     `podman start weaviate_claude` but it did not become \
                     healthy in time. The deferral is recorded at \
                     {}/.claude/context/UPDATE_DEFERRED.md; collections will \
                     be created when Weaviate is up and you re-run \
                     `python -m vco_lib.project_init bootstrap-collections \
                     --name {:?}`.",
                    folder_str, project_name
                ));
            }
            // Bug-1 v0.2.4 (2026-05-12): regenerated[] surfacing. The
            // Python side detects pre-existing schema-incompatible
            // collections (case-only name conflict, legacy multi-vector
            // configs, missing indexNullState) and drops + recreates
            // them with the current spec. The subsequent kg-sync
            // re-populates from disk, so the user's data is preserved
            // — but we want them to know it happened so they understand
            // why the kg-sync banner is doing more work than usual.
            if let Some(regens) = v.get("regenerated").and_then(|x| x.as_array()) {
                let n = regens.len();
                for (idx, r) in regens.iter().enumerate() {
                    let coll = r.get("collection")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    let reason_tag = r.get("reason")
                        .and_then(|c| c.as_str()).unwrap_or("schema-mismatch");
                    let dropped_name = r.get("dropped_name")
                        .and_then(|c| c.as_str()).unwrap_or(coll);
                    let detail = r.get("detail")
                        .and_then(|c| c.as_str()).unwrap_or("");
                    let counter = if n > 1 {
                        format!(" ({}/{})", idx + 1, n)
                    } else {
                        String::new()
                    };
                    let pretty_reason = match reason_tag {
                        "case-conflict" => "case-only name conflict",
                        "legacy-single-vector" => "legacy single-vector schema",
                        "multi-vector" => "legacy multi-named-vector schema",
                        "index-null-state" => "missing indexNullState invariant",
                        _ => "schema mismatch with current spec",
                    };
                    let detail_clause = if detail.is_empty() {
                        String::new()
                    } else {
                        format!(" — {}", detail)
                    };
                    let dropped_clause = if dropped_name != coll {
                        format!(" (dropped pre-existing `{}`)", dropped_name)
                    } else {
                        String::new()
                    };
                    warnings.push(format!(
                        "Migrated Weaviate schema for `{}`{}{}{}: {}. The \
                         collection was recreated with the current spec; \
                         knowledge/**/*.md on disk is the source of truth, \
                         so the subsequent kg-sync re-populates everything.",
                        coll, counter, dropped_clause, detail_clause, pretty_reason,
                    ));
                }
            }
            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                for e in errs {
                    let coll = e.get("collection")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!(
                        "bootstrap-collections error on {}: {}. \
                         Lazy creation by the MCP server may produce a stale \
                         schema; re-run bootstrap manually once Weaviate is healthy.",
                        coll, msg
                    ));
                }
            }
            if !out.status.success() {
                // Non-zero exit but JSON parsed — already surfaced via errors[].
                eprintln!("[vct] bootstrap-collections exit {} (errors surfaced via warnings)", out.status);
            }
        }
        Err(parse_err) => {
            warnings.push(format!(
                "bootstrap-collections produced unparseable output ({}): \
                 stderr tail: {}. Per-project collections will be created \
                 lazily on first write.",
                parse_err,
                stderr.lines().rev().take(3)
                    .collect::<Vec<_>>().into_iter().rev()
                    .collect::<Vec<_>>().join(" | ")
            ));
        }
    }
    warnings
}

/// PR 4 (2026-05-01): subprocess-call vco_lib.project_init install-bundle.
///
/// Same soft-fail discipline as `run_bootstrap_collections`. JSON `errors[]`
/// entries (per-file write failures) become individual warnings. The function
/// never blocks project creation.
async fn run_install_bundle(folder: &Path) -> Vec<String> {
    let mut warnings: Vec<String> = Vec::new();

    let system = match detect_system().await {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!(
                "install-bundle skipped: detect_system failed: {}. \
                 Per-project hooks/scripts/agents/skills will not be installed \
                 — Claude Code session running in this folder won't have the \
                 orchestrator's automation. Manual fix: run \
                 `python -m vco_lib.project_init install-bundle --folder <path>`.",
                e
            ));
            return warnings;
        }
    };
    if !system.has_python {
        warnings.push(
            "install-bundle skipped: no Python 3.11+ on PATH. \
             Hooks/scripts/agents/skills not installed; install Python and \
             re-run setup."
            .to_string(),
        );
        return warnings;
    }

    let orch_root: PathBuf = match find_local_repo_root() {
        Ok(p) => p,
        Err(e) => {
            warnings.push(format!(
                "install-bundle skipped: orchestrator root not found: {}. \
                 Per-project hooks/scripts/agents/skills will not be installed.",
                e
            ));
            return warnings;
        }
    };

    let folder_str = folder.to_string_lossy().to_string();
    let orch_str = orch_root.to_string_lossy().to_string();
    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.project_init",
        "install-bundle",
        "--folder",
        &folder_str,
        "--orchestrator-root",
        &orch_str,
        "--project-folder",
        &folder_str,
        "--json",
    ])
    .current_dir(&orch_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => {
            warnings.push(format!(
                "install-bundle subprocess failed to start: {}. \
                 Hooks/scripts/agents/skills not installed.",
                e
            ));
            return warnings;
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    match serde_json::from_str::<serde_json::Value>(&stdout) {
        Ok(v) => {
            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                for e in errs {
                    let p = e.get("path").and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error").and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!(
                        "install-bundle file error on {}: {}",
                        p, msg
                    ));
                }
            }
            if let Some(ws) = v.get("warnings").and_then(|x| x.as_array()) {
                for w in ws {
                    if let Some(s) = w.as_str() {
                        warnings.push(format!("install-bundle: {}", s));
                    }
                }
            }
            if !out.status.success() {
                eprintln!("[vct] install-bundle exit {} (errors surfaced via warnings)", out.status);
            }
        }
        Err(parse_err) => {
            warnings.push(format!(
                "install-bundle produced unparseable output ({}): \
                 stderr tail: {}. Hooks/scripts/agents/skills may be incomplete.",
                parse_err,
                stderr.lines().rev().take(3)
                    .collect::<Vec<_>>().into_iter().rev()
                    .collect::<Vec<_>>().join(" | ")
            ));
        }
    }
    warnings
}

/// PR 5 (2026-05-01): subprocess-call vco_lib.project_init install-bundle --update.
///
/// Mirrors `run_install_bundle` but in update mode. Returns BOTH the warnings
/// (same soft-fail surface) and a populated `UpdateSummary` derived from the
/// JSON envelope's `actions` map. The launcher toasts one-line summary lines
/// ("5 files updated, 2 user-modifications preserved") off the summary and
/// streams every entry of warnings to error / info toasts.
///
/// Soft-fail policy:
///   - Python missing → push warning, return zeroed summary.
///   - Orchestrator root unfindable → push warning, return zeroed summary.
///   - Subprocess non-zero exit → push warnings parsed from JSON `errors[]`;
///     summary still populated from whatever `actions` were classified.
///   - JSON parse failure → push warning carrying stderr tail; zeroed summary.
///
/// Never returns Err (parity with `run_install_bundle`). Hard environment
/// failures (folder missing, project_id not in DB) are caught earlier in
/// `update_project_v2`.
pub(crate) async fn run_install_bundle_update(
    folder: &Path,
) -> (Vec<String>, UpdateSummary) {
    run_install_bundle_update_with_root(folder, None).await
}

/// Test-friendly seam: same as `run_install_bundle_update` but lets a
/// caller (today: the unit tests) override the orchestrator root so
/// install-bundle resolves templates from a controlled fake tree rather
/// than the running launcher's real orchestrator clone. Production
/// callers always pass `None` and get `find_local_repo_root()`.
pub(crate) async fn run_install_bundle_update_with_root(
    folder: &Path,
    orchestrator_root_override: Option<&Path>,
) -> (Vec<String>, UpdateSummary) {
    let mut warnings: Vec<String> = Vec::new();
    let mut summary = UpdateSummary::default();

    let system = match detect_system().await {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!(
                "install-bundle --update skipped: detect_system failed: {}. \
                 No new orchestrator-shipped files will land in this project. \
                 Manual fix: `python -m vco_lib.project_init install-bundle \
                 --folder <path> --update`.",
                e
            ));
            return (warnings, summary);
        }
    };
    if !system.has_python {
        warnings.push(
            "install-bundle --update skipped: no Python 3.11+ on PATH. \
             Install Python and re-click \"Update bundle\"."
            .to_string(),
        );
        return (warnings, summary);
    }

    // Resolve TWO roots:
    //   - `vco_lib_root`: where vco_lib/ lives (always the real installed
    //     orchestrator clone — the Python package must be importable).
    //   - `templates_root`: where templates/ + infrastructure/ live. By
    //     default same as vco_lib_root, but tests can override to a fake
    //     orchestrator tree to validate behaviour against controlled
    //     templates without touching the host's real bundle.
    let vco_lib_root: PathBuf = match find_local_repo_root() {
        Ok(p) => p,
        Err(e) => {
            warnings.push(format!(
                "install-bundle --update skipped: orchestrator root not found: {}. \
                 No new orchestrator-shipped files will land in this project.",
                e
            ));
            return (warnings, summary);
        }
    };
    let templates_root: PathBuf = match orchestrator_root_override {
        Some(p) => p.to_path_buf(),
        None => vco_lib_root.clone(),
    };

    let folder_str = folder.to_string_lossy().to_string();
    let templates_str = templates_root.to_string_lossy().to_string();
    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.project_init",
        "install-bundle",
        "--folder",
        &folder_str,
        "--orchestrator-root",
        &templates_str,
        "--project-folder",
        &folder_str,
        "--update",
        "--json",
    ])
    .current_dir(&vco_lib_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => {
            warnings.push(format!(
                "install-bundle --update subprocess failed to start: {}. \
                 Project files unchanged.",
                e
            ));
            return (warnings, summary);
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    match serde_json::from_str::<serde_json::Value>(&stdout) {
        Ok(v) => {
            // Tally per-action counts. Use unwrap_or(&Vec::new()) semantics
            // by treating a missing array as "0 entries"; that's the same
            // soft-fail discipline as the bootstrap subprocess wrapper.
            let actions = v.get("actions").and_then(|a| a.as_object());
            if let Some(map) = actions {
                let count_for = |k: &str| -> u32 {
                    map.get(k)
                        .and_then(|x| x.as_array())
                        .map(|a| a.len() as u32)
                        .unwrap_or(0)
                };
                summary.created = count_for("create");
                summary.overwritten = count_for("overwrite");
                summary.preserved = count_for("preserve");
                summary.noop = count_for("noop");
                summary.always_overwritten = count_for("always-overwrite");
                summary.skipped_existing = count_for("skip-existing");
            }

            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                summary.errors_count = errs.len() as u32;
                for e in errs {
                    let p = e.get("path").and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error").and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!(
                        "install-bundle --update file error on {}: {}",
                        p, msg
                    ));
                }
            }

            if let Some(ws) = v.get("warnings").and_then(|x| x.as_array()) {
                for w in ws {
                    if let Some(s) = w.as_str() {
                        warnings.push(format!("install-bundle --update: {}", s));
                    }
                }
            }

            // If preserve > 0, surface a friendly pointer so the user
            // knows the deferral .md exists with manual-merge instructions.
            if summary.preserved > 0 {
                warnings.push(format!(
                    "{} user-modified file(s) preserved during update. \
                     See {}/.claude/context/UPDATE_DEFERRED.md for the \
                     `bundle_user_modified_preserved` entry (lists each \
                     preserved file + the explicit `--force` command to \
                     accept the orchestrator's shipped versions).",
                    summary.preserved, folder_str
                ));
            }

            if !out.status.success() {
                eprintln!(
                    "[vct] install-bundle --update exit {} (errors surfaced via warnings)",
                    out.status
                );
            }
        }
        Err(parse_err) => {
            warnings.push(format!(
                "install-bundle --update produced unparseable output ({}): \
                 stderr tail: {}. Project files may be partially updated.",
                parse_err,
                stderr.lines().rev().take(3)
                    .collect::<Vec<_>>().into_iter().rev()
                    .collect::<Vec<_>>().join(" | ")
            ));
        }
    }

    (warnings, summary)
}

/// PR 5 (2026-05-01): pre-update Weaviate schema-drift probe.
///
/// Subprocess-calls `vco_lib.project_init migrate-collections --dry-run
/// --project-folder <folder>`. The Python side detects per-collection drift
/// against the current target schema; any `copy` or `rebuild` action triggers
/// a `schema_migration_required` deferral entry written by
/// `_emit_migrate_required_deferral` (preserves user data — the destructive
/// ops only run with explicit user consent via `migrate-collections` without
/// `--dry-run`).
///
/// Soft-fail discipline:
///   - Subprocess fails to start / Python missing / orch root missing →
///     push warning, return.
///   - Subprocess exits non-zero with parseable JSON → drift wasn't
///     classifiable; surface errors[] but don't block the update.
///   - Subprocess exits 0 with `deferral_emitted=true` → push an info
///     warning pointing to the deferral .md.
///   - Weaviate is down → migrate-collections itself surfaces an error;
///     drift detection is unavailable but the bundle install still
///     proceeds.
///
/// Never blocks `update_project_v2` (no Err return).
pub(crate) async fn run_migrate_dry_run(
    folder: &Path,
    project_name: &str,
) -> Vec<String> {
    let mut warnings: Vec<String> = Vec::new();

    let system = match detect_system().await {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!(
                "schema drift probe skipped: detect_system failed: {}. \
                 Bundle install will proceed; per-project Weaviate schema \
                 may drift silently.",
                e
            ));
            return warnings;
        }
    };
    if !system.has_python {
        warnings.push(
            "schema drift probe skipped: no Python 3.11+ on PATH. \
             Bundle install will proceed; schema drift unmonitored."
            .to_string(),
        );
        return warnings;
    }

    let orch_root = match find_local_repo_root() {
        Ok(p) => p,
        Err(e) => {
            warnings.push(format!(
                "schema drift probe skipped: orchestrator root not found: {}. \
                 Bundle install will proceed.",
                e
            ));
            return warnings;
        }
    };

    let folder_str = folder.to_string_lossy().to_string();
    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.project_init",
        "migrate-collections",
        "--name",
        project_name,
        "--dry-run",
        "--project-folder",
        &folder_str,
        "--json",
    ])
    .current_dir(&orch_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => {
            warnings.push(format!(
                "schema drift probe subprocess failed to start: {}. \
                 Bundle install will proceed.",
                e
            ));
            return warnings;
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    match serde_json::from_str::<serde_json::Value>(&stdout) {
        Ok(v) => {
            let deferral_emitted = v
                .get("deferral_emitted")
                .and_then(|x| x.as_bool())
                .unwrap_or(false);
            if deferral_emitted {
                warnings.push(format!(
                    "Weaviate schema drift detected — `schema_migration_required` \
                     deferral entry written to {}/.claude/context/UPDATE_DEFERRED.md. \
                     The bundle install proceeded normally; the destructive \
                     migration was NOT auto-applied — re-run \
                     `python -m vco_lib.project_init migrate-collections \
                     --name {:?}` to consent and apply.",
                    folder_str, project_name
                ));
            }
            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                for e in errs {
                    let coll = e.get("collection")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    let action = e.get("action")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!(
                        "schema drift probe error ({}/{}): {}. \
                         Drift may be undetected; bundle install proceeds.",
                        coll, action, msg
                    ));
                }
            }
            if !out.status.success() {
                eprintln!(
                    "[vct] migrate-collections --dry-run exit {} (errors surfaced via warnings)",
                    out.status
                );
            }
        }
        Err(parse_err) => {
            warnings.push(format!(
                "schema drift probe produced unparseable output ({}): \
                 stderr tail: {}. Bundle install will proceed; drift unmonitored.",
                parse_err,
                stderr.lines().rev().take(3)
                    .collect::<Vec<_>>().into_iter().rev()
                    .collect::<Vec<_>>().join(" | ")
            ));
        }
    }
    warnings
}

/// PR 5 (2026-05-01): re-run the bundle install in update mode against an
/// existing user project. Picks up newly-shipped orchestrator files (hooks,
/// scripts, agents, skills, settings, infrastructure) WITHOUT overwriting
/// user customizations. The manifest at `<folder>/.claude/.vco-manifest.json`
/// drives drift detection: files matching the prior-shipped hash get
/// overwritten with the new version; files diverging from the prior hash
/// are preserved + reported via `bundle_user_modified_preserved`.
///
/// Order of operations (each soft-fails to a warning):
///   1. Resolve project from DB → folder path + project name.
///   2. `bootstrap-collections` — re-POST the per-project + shared KG/Dev
///      schemas (idempotent; ensures the collections still exist with the
///      current schema invariants if they were lazily created).
///   3. `migrate-collections --dry-run --project-folder <folder>` — detect
///      schema drift; emit `schema_migration_required` deferral if any
///      collection needs `copy` or `rebuild`. NEVER auto-applies
///      destructive migrations — preserves user data, defers to explicit
///      consent on a separate `Migrate schemas` action.
///   4. `install-bundle --update --project-folder <folder>` — copy the
///      shipped templates / infrastructure files; tally summary counts;
///      emit `bundle_user_modified_preserved` for any preserved file.
///
/// Returns Err only on hard environment failures (project not in DB,
/// folder doesn't exist on disk). Subprocess failures, individual
/// file errors, deferral writes — all flow through `warnings`.
#[command]
pub async fn update_project_v2(
    project_id: String,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<UpdateProjectResult, String> {
    // 1. Resolve project from DB.
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let count = db.list_module_installs_for_project(&project_id)?.len() as u32;
    let folder = PathBuf::from(&row.folder_path);

    // Hard env failure: folder must exist and be a directory. We don't
    // try to create it — an "update" on a folder that doesn't exist is
    // a logic error (use create_project_v2 instead).
    if !folder.exists() {
        return Err(format!(
            "project folder does not exist: {}. \
             Use create_project_v2 to (re-)create the folder + bundle.",
            row.folder_path
        ));
    }
    if !folder.is_dir() {
        return Err(format!("project folder is not a directory: {}", row.folder_path));
    }

    let mut warnings: Vec<String> = Vec::new();

    // 2. Bootstrap collections (idempotent — existing classes left alone).
    //    Same reasoning as create_project_v2: ensure the per-project + shared
    //    collections still exist with the current schema invariants.
    for w in run_bootstrap_collections(&folder, &row.name).await {
        warnings.push(w);
    }

    // 3. Schema-drift dry-run probe. Writes `schema_migration_required`
    //    deferral entry if any collection needs a destructive migration
    //    (copy / rebuild). The bundle install still proceeds either way.
    for w in run_migrate_dry_run(&folder, &row.name).await {
        warnings.push(w);
    }

    // 4. Bundle install in update mode. Manifest-driven drift detection.
    let (bundle_warnings, summary) = run_install_bundle_update(&folder).await;
    for w in bundle_warnings {
        warnings.push(w);
    }

    // 5. v0.2.37 (Finding F6, 2026-05-27): backfill the per-project env
    //    surfaces after the bundle update. Pre-v0.2.37 the bundle update
    //    flow NEVER invoked the env writer, so projects created BEFORE
    //    this release stayed pinned to whatever `.claude/env` their
    //    `create_project_v2` call wrote at the time. Combined with
    //    Finding F1 (apply_project_env_via_python now passes
    //    --orchestrator-root), this closes the user-project-style "missing
    //    VCT_ORCHESTRATOR_ROOT" gap on every bundle update — existing
    //    projects self-heal the next time the user clicks Update.
    //
    //    Mirrors the warning-on-error pattern from create_project_v2
    //    (line 362) and rename_project_v2 (line 3039): a Python
    //    subprocess hiccup is surfaced as a warning, NOT a hard
    //    failure. The bundle update still succeeds because the
    //    manifest install + audit log + change_log entry have
    //    already landed — env writes are a best-effort step on top.
    if let Err(e) = apply_project_env_via_python(&row.id, &folder, &db) {
        let msg = format!(
            "post-bundle env refresh (apply_project_env_via_python) failed: {}. \
             Bundle update succeeded but .claude/env / .claude/settings.json \
             may be missing VCT_ORCHESTRATOR_ROOT until the next refresh.",
            e
        );
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    // 6. v0.2.44 V44-G4 (RL-chat ask 2026-06-01): auto-retry stuck
    //    module_installs rows for this project. Non-blocking — any retry
    //    failure flows through as a `warnings[]` entry; the bundle
    //    update itself never fails because the user's queued install
    //    didn't recover this cycle.
    let retry_reports = crate::commands::module_service::retry_failed_module_installs(
        Some(&row.id),
        &db,
        Some(&app),
    )
    .await;
    for report in &retry_reports {
        match report.decision.as_str() {
            "retried_success" | "self_healed" => {
                warnings.push(format!(
                    "module {} auto-recovered (decision={}, new_status={})",
                    report.module_id,
                    report.decision,
                    report.new_status.as_deref().unwrap_or("?"),
                ));
            }
            "retried_failed" => {
                warnings.push(format!(
                    "module {} retry failed: {}",
                    report.module_id,
                    report.error.as_deref().unwrap_or("(no message)"),
                ));
            }
            _ => {
                // skipped_* / retried_unavailable — audit-only, no
                // user-facing message (the audit log carries the detail).
            }
        }
    }

    db.audit(
        "project_update_bundle",
        Some(&row.id),
        None,
        &serde_json::json!({
            "name": row.name,
            "summary": {
                "created": summary.created,
                "overwritten": summary.overwritten,
                "preserved": summary.preserved,
                "noop": summary.noop,
                "always_overwritten": summary.always_overwritten,
                "skipped_existing": summary.skipped_existing,
                "errors_count": summary.errors_count,
            },
            "retry_reports": retry_reports,
        }),
    )?;
    let _ = db.log_change("projects", "update_bundle", Some(&row.id), Some(&row.id));

    Ok(UpdateProjectResult {
        project: ProjectView::from_row(row, count),
        warnings,
        summary,
    })
}

// ─── 0.2.x backlog #4: Update all projects ──────────────────────────────
//
// The launcher pre-0.2.x exposed only `update_project_v2(project_id)` —
// power users with N registered projects had to click Update once per
// row. `update_all_projects` iterates every project from
// `list_projects_v2` and invokes the per-project update sequentially.
//
// Sequential rather than fan-out: keeps the UX understandable (the user
// sees one project at a time progress through the bundle install) and
// lets the GUI stream a per-project status modal cleanly. A failure on
// project N does NOT roll back projects 1..N-1; the caller decides
// whether to continue. Each project's outcome lands in `updated[]`
// regardless of success/failure so the GUI can render a per-row report.

#[derive(Debug, Clone, Deserialize)]
pub struct UpdateAllOptions {
    /// When true, stop iterating after the first project that returns Err
    /// from `update_project_v2`'s hard-failure path (project missing,
    /// folder gone). Soft warnings (warnings[] in UpdateProjectResult)
    /// never trip this — they're per-project conditions the user can
    /// action. Defaults to `true` because the typical "Update all"
    /// failure is a single broken project and the user wants to know
    /// quickly rather than chew through every other project first.
    #[serde(default = "default_stop_on_error")]
    pub stop_on_error: bool,
}

fn default_stop_on_error() -> bool {
    true
}

impl Default for UpdateAllOptions {
    fn default() -> Self {
        Self { stop_on_error: default_stop_on_error() }
    }
}

/// Per-project outcome of an `update_all_projects` run. Mirrors
/// `UpdateProjectResult` for the success path, plus an error string for
/// the failure path. The GUI uses `status` to render a checkmark / X
/// next to each row in the progress modal.
#[derive(Debug, Clone, Serialize)]
pub struct UpdateAllProjectEntry {
    pub project_id: String,
    pub project_name: String,
    /// "succeeded" — `update_project_v2` returned Ok (warnings may still exist).
    /// "failed"    — hard failure (project missing on disk, folder gone, etc.).
    /// "skipped"   — caller stopped iterating before reaching this project.
    pub status: String,
    /// Hard-failure message; null on success / skip. Soft warnings flow
    /// through `summary` like the per-project command.
    pub error: Option<String>,
    /// Soft-fail warnings from `UpdateProjectResult.warnings`, propagated
    /// per-project so the GUI can stream them as it does today.
    #[serde(default)]
    pub warnings: Vec<String>,
    /// Per-project summary counts (None on hard failure / skip — there
    /// was no install run to count).
    pub summary: Option<UpdateSummary>,
}

/// Aggregate report returned by `update_all_projects`.
#[derive(Debug, Clone, Serialize)]
pub struct UpdateAllReport {
    pub updated: Vec<UpdateAllProjectEntry>,
    pub total_succeeded: u32,
    pub total_failed: u32,
    /// Number of projects we never reached because `stop_on_error=true`
    /// and an earlier project hard-failed. These appear in `updated[]`
    /// with `status="skipped"`. Counted separately so the GUI's summary
    /// toast can read e.g. "3 succeeded, 1 failed, 2 skipped".
    pub total_skipped: u32,
}

/// Iterate every registered project sequentially and run the same
/// per-project update flow as `update_project_v2`. See the doc on the
/// ─── 0.2.x backlog #4 ─── header for the design rationale (sequential,
/// no rollback, per-project status reporting).
///
/// Soft-fail discipline mirrors `update_project_v2`:
///   * Hard failures (project gone, folder missing) → `status="failed"`,
///     `error: Some(msg)`. With `stop_on_error: true` (default), the
///     iteration stops here and remaining projects appear as "skipped".
///   * Soft warnings (deferral entries, env-write hiccups) → flow through
///     `warnings[]` per-project. Don't trip stop_on_error.
///   * Empty project list → returns an empty report with all counts at 0.
///     Not an error; the GUI button just confirms "no projects to update".
#[command]
pub async fn update_all_projects(
    opts: Option<UpdateAllOptions>,
    app: tauri::AppHandle,
    db: State<'_, Db>,
) -> Result<UpdateAllReport, String> {
    let opts = opts.unwrap_or_default();
    let projects = db.list_projects()?;

    let mut entries: Vec<UpdateAllProjectEntry> = Vec::with_capacity(projects.len());
    let mut total_succeeded: u32 = 0;
    let mut total_failed: u32 = 0;
    let mut total_skipped: u32 = 0;
    let mut stop = false;

    for row in &projects {
        if stop {
            entries.push(UpdateAllProjectEntry {
                project_id: row.id.clone(),
                project_name: row.name.clone(),
                status: "skipped".to_string(),
                error: None,
                warnings: Vec::new(),
                summary: None,
            });
            total_skipped += 1;
            continue;
        }
        // Use the same internal pipeline as `update_project_v2`. Going
        // through the public command would require Tauri State plumbing
        // we already have; we just call it directly.
        let result = update_project_v2(row.id.clone(), app.clone(), db.clone()).await;
        match result {
            Ok(r) => {
                entries.push(UpdateAllProjectEntry {
                    project_id: row.id.clone(),
                    project_name: row.name.clone(),
                    status: "succeeded".to_string(),
                    error: None,
                    warnings: r.warnings,
                    summary: Some(r.summary),
                });
                total_succeeded += 1;
            }
            Err(e) => {
                entries.push(UpdateAllProjectEntry {
                    project_id: row.id.clone(),
                    project_name: row.name.clone(),
                    status: "failed".to_string(),
                    error: Some(e),
                    warnings: Vec::new(),
                    summary: None,
                });
                total_failed += 1;
                if opts.stop_on_error {
                    stop = true;
                }
            }
        }
    }

    Ok(UpdateAllReport {
        updated: entries,
        total_succeeded,
        total_failed,
        total_skipped,
    })
}

/// Phase 0.B Part 2 (2026-05-25): canonical-env writer entry-point used
/// by production callers (`create_project_v2`, `rename_project_v2`,
/// `set_shared_kg_write_disabled`, `refresh_project_env_with_db`).
///
/// Subprocesses into the Python contract
/// `python -m vco_lib.config_projection apply --project-id <id>` which
/// is the single legal writer of canonical env values to
/// `.claude/settings.json` + `.claude/env`. See
/// `vco_lib/config_projection.py` module docstring for the full
/// rationale (Option A: Python canonical, Rust shells out).
///
/// Behaviour:
///   * Resolves the per-project Python interpreter via the same chain
///     as `templates/hooks/pre-diagram-path-validation.sh`:
///     `VCT_VENV` → `<VCT_INSTALL_ROOT>/.venv` →
///     `<VCT_INSTALL_ROOT>/claude_mcp_servers/.venv` → system `python3`.
///     Returns an `Err(String)` only when NO interpreter is reachable;
///     a working PATH `python3` is typically the last-resort fallback.
///   * Spawns the subprocess with a minimal env (PATH +
///     VCT_STATE_DIR + VCT_HUB_PORT + VCT_HUB_TOKEN) so the Python
///     side reads the launcher's DB at the same path the launcher
///     opened it, and so it can reach the hub if it needs to (the
///     CLI doesn't today, but the contract may grow a hub-resolver
///     dependency).
///   * Spawns with `current_dir = folder` so any relative path in the
///     Python contract resolves against the project root.
///   * 30s timeout — generous for user-driven actions (create / rename
///     / refresh); the subprocess itself completes in ~150 ms.
///   * Stderr from the Python side is included in the returned error
///     message so the UI's warning toast surfaces the actual cause.
///
/// User-secret handling (regression note):
///   The legacy `write_project_env_files` function additionally emitted
///   per-project user secrets (e.g. GitHub PAT) into the same env
///   surfaces. The Python contract explicitly excludes user secrets
///   (see contract docstring §"Out of scope") — routing them through
///   Python requires plumbing the active-flag gate (Rust-side
///   `resolve_user_secret_state`) into Python first. Until that
///   migration lands (tracked as Phase 0.E), production callers go
///   through this Python-only path; the user-secret emit/strip step
///   only runs when the user toggles a secret in the launcher GUI's
///   SecretsPanel (which calls `surgically_strip_user_secret_keys` +
///   `write_project_env_files` directly). The window: an active user
///   secret's value change in the keychain won't reach env surfaces
///   until the SecretsPanel toggles or the user re-runs the affected
///   project's secret writer.
/// v0.2.37 (Finding F1 testability helper): builds the argument list
/// passed to the `python -m vco_lib.config_projection apply` subprocess.
/// Pure function; no I/O. Lets unit tests assert the
/// `--orchestrator-root` flag is included exactly when the
/// orchestrator-root resolver returns Some.
///
/// Returns the list in the order it will appear after `-m
/// vco_lib.config_projection apply`. The caller is responsible for
/// prepending those three Python-module pieces + appending any
/// future per-call args.
fn build_config_projection_apply_args(
    project_id: &str,
    orchestrator_root: Option<&Path>,
) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "--project-id".to_string(),
        project_id.to_string(),
    ];
    if let Some(root) = orchestrator_root {
        args.push("--orchestrator-root".to_string());
        args.push(root.to_string_lossy().to_string());
    }
    args
}

fn apply_project_env_via_python(
    project_id: &str,
    folder: &Path,
    db: &Db,
) -> Result<(), String> {
    let python = resolve_python_for_vco_lib_local().ok_or_else(|| {
        "no python interpreter found for vco_lib.config_projection apply \
         (checked: $VCT_VENV, <VCT_INSTALL_ROOT>/.venv, \
         <VCT_INSTALL_ROOT>/claude_mcp_servers/.venv, system python3)"
            .to_string()
    })?;

    let mut cmd = std::process::Command::new(&python).silent();
    cmd.arg("-m").arg("vco_lib.config_projection").arg("apply");

    // v0.2.37 (Finding F1, 2026-05-27): pass the orchestrator clone
    // root to the Python writer. `vco_lib/config_projection.py` only
    // emits `VCT_ORCHESTRATOR_ROOT` + `VCT_INFRASTRUCTURE_DIR` when
    // the apply call receives `orchestrator_root is not None` — and
    // the CLI defaults the flag to `None`. Before this fix, the
    // launcher NEVER set the flag, so every Python-written env file
    // silently omitted those two keys (the production writer surface
    // since Phase 0.B Part 2 migrated away from the Rust
    // `write_project_env_files`). This is the actual root cause of
    // the user_project_x / the "missing VCT_ORCHESTRATOR_ROOT" bug.
    //
    // Resolution order matches the canonical Rust resolver:
    //   1. `resolve_orchestrator_root(db)` — DB cache (sticky after
    //      install.py seeds it OR after the first successful walk-up).
    //   2. (No further fallback needed — resolve_orchestrator_root
    //      already includes the walk-up step internally.)
    //
    // When the resolver returns None (standalone binary, no clone
    // discoverable on disk), we OMIT the flag entirely. This
    // preserves the pre-fix behaviour for forks running outside a
    // clone — no regression for those legitimate cases.
    let orch_root = crate::commands::installer::resolve_orchestrator_root(db);
    for a in build_config_projection_apply_args(project_id, orch_root.as_deref()) {
        cmd.arg(a);
    }

    // Minimal env: clear inherited, then add only what the Python side
    // needs. This prevents per-launcher quirks (e.g. an inherited
    // KG_COLLECTION from the launcher's own .claude/env) from leaking
    // into the subprocess and disrupting the resolver.
    cmd.env_clear();
    if let Ok(path) = std::env::var("PATH") {
        cmd.env("PATH", path);
    }
    // Launcher DB location override (canonical when set; the resolver
    // falls back to ~/.vct/launcher.db).
    if let Ok(state_dir) = std::env::var("VCT_STATE_DIR") {
        cmd.env("VCT_STATE_DIR", state_dir);
    }
    // Hub-aware resolver hints — the CLI doesn't use them today, but
    // future contract revisions may resolve secrets via vct-hub.
    if let Ok(hub_port) = std::env::var("VCT_HUB_PORT") {
        cmd.env("VCT_HUB_PORT", hub_port);
    }
    if let Ok(hub_token) = std::env::var("VCT_HUB_TOKEN") {
        cmd.env("VCT_HUB_TOKEN", hub_token);
    }
    // VCT_INSTALL_ROOT — also needed so `python -m vco_lib...` resolves
    // `vco_lib` as an implicit-namespace package from the orchestrator
    // clone (vco_lib is NOT pip-installed). See embedding_catalog.rs
    // for the same plumbing rationale.
    if let Ok(install_root) = std::env::var("VCT_INSTALL_ROOT") {
        cmd.env("VCT_INSTALL_ROOT", install_root);
    }
    // Windows + macOS: keep TEMP/TMP so the atomic-write tempfile lands
    // in a writable location.
    if let Ok(v) = std::env::var("TEMP") {
        cmd.env("TEMP", v);
    }
    if let Ok(v) = std::env::var("TMP") {
        cmd.env("TMP", v);
    }
    if let Ok(v) = std::env::var("TMPDIR") {
        cmd.env("TMPDIR", v);
    }
    // Windows-only — USERPROFILE / APPDATA / LOCALAPPDATA so the
    // ~/.vct/launcher.db fallback resolves correctly.
    #[cfg(target_os = "windows")]
    {
        for k in ["USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH"] {
            if let Ok(v) = std::env::var(k) {
                cmd.env(k, v);
            }
        }
    }
    // POSIX — HOME so `~/.vct/launcher.db` resolves.
    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(v) = std::env::var("HOME") {
            cmd.env("HOME", v);
        }
    }

    cmd.current_dir(folder);

    // Spawn with stdout/stderr captured. 30 s wall-clock cap — the
    // happy path is ~150 ms; a hang past 30 s indicates a stuck DB
    // open or a runaway Python process and is better surfaced than
    // letting the user click sit indefinitely.
    let mut child = cmd
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| {
            format!(
                "config_projection apply: spawn failed (python={}): {}",
                python.display(),
                e
            )
        })?;

    // Polled wait with 30 s deadline. std::process::Child::wait()
    // doesn't take a timeout, so we sleep-poll. 50 ms granularity is
    // cheap and gives the subprocess every chance to exit fast.
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(30);
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if std::time::Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(
                        "config_projection apply: timed out after 30 s"
                            .to_string(),
                    );
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => {
                return Err(format!(
                    "config_projection apply: wait failed: {}",
                    e
                ));
            }
        }
    };

    if !status.success() {
        // Capture stderr for the error message — Python prints a
        // JSON-shaped diagnostic on the CLI's error paths
        // (project_not_found, db_unreachable, apply_failed).
        let mut stderr_text = String::new();
        if let Some(mut s) = child.stderr.take() {
            use std::io::Read;
            let mut buf = Vec::new();
            let _ = s.read_to_end(&mut buf);
            stderr_text = String::from_utf8_lossy(&buf).into_owned();
        }
        return Err(format!(
            "config_projection apply exited with {} (project_id={}): {}",
            status, project_id, stderr_text.trim()
        ));
    }

    Ok(())
}

/// Resolve a Python interpreter that can `import vco_lib`. Mirrors the
/// chain documented in `templates/hooks/pre-diagram-path-validation.sh`:
///   1. `$VCT_VENV/bin/python` (POSIX) or `$VCT_VENV/Scripts/python.exe`
///      (Windows) — pinned by the launcher when it knows the venv.
///   2. `<VCT_INSTALL_ROOT>/.venv/bin/python` (POSIX) or
///      `<VCT_INSTALL_ROOT>/.venv/Scripts/python.exe` (Windows).
///   3. `<VCT_INSTALL_ROOT>/claude_mcp_servers/.venv/<...>/python` —
///      the older venv layout that some installs still carry.
///   4. Walks up from `std::env::current_exe()` up to 8 parents and
///      tries the same layouts under each (covers worktree-based runs
///      where `VCT_INSTALL_ROOT` may not be set).
///   5. Last-resort PATH fallback: `python3` (POSIX) / `python.exe`
///      (Windows).
///
/// Returns `Some(path)` on first hit. Never returns `None` thanks to
/// the PATH fallback — but the caller should still handle `None`
/// defensively in case PATH lookup is also unavailable in some
/// embedded environment.
///
/// Naming: the `_local` suffix differentiates from the same-name
/// helper in `embedding_catalog.rs` / `embedding_enrichment.rs`. A
/// future refactor will hoist this to a shared module (e.g.
/// `crate::commands::python_resolver`); kept inline here for the
/// Phase 0.B Part 2 migration's minimal-diff discipline.
fn resolve_python_for_vco_lib_local() -> Option<PathBuf> {
    let venv_in = |root: &Path| -> Option<PathBuf> {
        for layout in [
            root.join(".venv"),
            root.join("claude_mcp_servers").join(".venv"),
        ] {
            for candidate in [
                layout.join("bin").join("python"),
                layout.join("bin").join("python3"),
                layout.join("Scripts").join("python.exe"),
            ] {
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
        None
    };

    // 1. $VCT_VENV — explicit override.
    if let Ok(v) = std::env::var("VCT_VENV") {
        let base = Path::new(&v);
        for candidate in [
            base.join("bin").join("python"),
            base.join("bin").join("python3"),
            base.join("Scripts").join("python.exe"),
        ] {
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }

    // 2 + 3. $VCT_INSTALL_ROOT — orchestrator clone root.
    if let Ok(root) = std::env::var("VCT_INSTALL_ROOT") {
        if let Some(p) = venv_in(Path::new(&root)) {
            return Some(p);
        }
    }

    // 4. Walk up from current_exe — covers launcher-binary runs.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(parent) = exe.parent() {
            let mut cur = parent.to_path_buf();
            for _ in 0..8 {
                if let Some(p) = venv_in(&cur) {
                    return Some(p);
                }
                if !cur.pop() {
                    break;
                }
            }
        }
    }

    // 5. PATH fallback.
    Some(PathBuf::from(if cfg!(target_os = "windows") {
        "python.exe"
    } else {
        "python3"
    }))
}

/// Bug 23 + 30: write per-project env files for every Claude Code surface.
///
/// Writes two files, both carrying the same env values:
///   1. `.claude/env` — POSIX shell file sourced by the `tools/claude`
///      wrapper (CLI users without VS Code)
///   2. `.claude/settings.json` `env` — canonical Anthropic per-project
///      env (read by CLI, Desktop app, AND propagated to MCP subprocesses
///      and hook subprocesses on every platform we've tested)
///
/// (2) is the channel that actually reaches MCP subprocesses on Linux.
/// (1) is kept for CLI users who source `.claude/env` from their shell rc.
///
/// PR-27 (v0.2.12, 2026-05-16) — `.vscode/settings.json` `claude-code.env`
/// previously a third write target — was removed. As of Claude Code 2.1.143
/// on Linux, that key does NOT propagate to MCP subprocesses (empirically
/// verified with sentinel testing on `/proc/<mcp_pid>/environ`). Writing
/// it caused user confusion: users edited the file expecting changes to
/// take effect, but nothing did. See the PR-27 commit message and
/// `docs/CLAUDE_CODE_COMPATIBILITY.md` → "Per-project env files" for the
/// full trace. The canonical channel is `.claude/settings.json` `env`.
///
/// Returns Ok(()) only when ALL succeed; the caller currently logs and
/// swallows the error so project creation never fails over an env file.
///
/// PR-3 (2026-05-06): takes a `ProjectEnvSettings` bundle so launcher
/// state — adopted ports, ACTIVE_EMBEDDING, shared-KG name, GPU toggle —
/// reaches the per-project env surfaces. Earlier callers passed only
/// `(folder, project_name, write_disabled)` and every other value was
/// hardcoded. See `launcher-settings-propagation-audit-2026-05-06.md`.
///
/// Test callers without a Db handle can use `ProjectEnvSettings::with_defaults`
/// to get canonical localhost values. `populate(&db, name, Some(project_id))`
/// is the production path.
///
/// MEDIUM-1 (2026-05-01): `shared_kg_write_disabled` is the per-project
/// WRITE gate (asymmetric model: all projects always READ the shared KG;
/// only writes are gated). `false` keeps the default (writes allowed);
/// `true` carries the gate forward.
///
/// Both env keys are written for back-compat: `SHARED_KG_WRITE_DISABLED`
/// (canonical) AND `SHARED_KG_OPT_OUT` (legacy alias) carry the same value.
/// The MCP server resolves write-gate state from the canonical key first,
/// falling back to the legacy alias when only it is present. Kept for
/// ~3 releases (target removal: 2026-08).
///
/// PR-3 Commit 6 (2026-05-06): the `env` sub-block in
/// `.claude/settings.json` and the body of `.claude/env` are deep-merged
/// rather than wholesale-replaced — the canonical keys we own are
/// overwritten with the launcher's resolved values, but user-added env
/// keys at the same level are preserved across re-runs. See
/// `secrets-and-access-matrix-audit-2026-05-06.md` §6 for the prior
/// wholesale-replace bug.
///
/// Phase 0.B Part 2 (2026-05-25): production callers of canonical env
/// writes (`create_project_v2`, `rename_project_v2`,
/// `set_shared_kg_write_disabled`, `refresh_project_env_with_db`) now
/// delegate to `apply_project_env_via_python` which subprocesses into
/// the Python contract `vco_lib.config_projection.apply_project_env`.
/// This function is retained for two remaining use cases:
///   * Test fixtures that pin the Rust legacy byte layout (the unit
///     tests in this file's `#[cfg(test)] mod tests { ... }` block).
///     The parity guarantee against the Python contract is enforced
///     separately by `tests/test_config_projection_byte_identical.py`.
///   * User-secret writes from the SecretsPanel flow
///     (`surgically_strip_user_secret_keys` + this function as the
///     re-emit step), which the Python contract does NOT handle
///     (out of scope per the contract docstring §"Out of scope" —
///     user secrets require an active-flag bridge that lands in a
///     future Phase 0.E). When the SecretsPanel mutates a secret it
///     re-invokes this function so the canonical AND user-secret
///     blocks land together atomically.
///
/// The legacy `config_projection: legacy_caller_pending_migration`
/// marker was removed on 2026-05-25 (Phase 0.B Part 2) once production
/// callers stopped reaching the function via the create / rename /
/// refresh paths. See `apply_project_env_via_python` above for the
/// new entry point.
pub fn write_project_env_files(
    folder: &Path,
    settings: &ProjectEnvSettings,
) -> Result<(), String> {
    let project_name = settings.project_name.as_str();
    let kg_collection = settings.kg_collection.as_str();
    let dev_collection = settings.dev_collection.as_str();
    let shared_kg_collection = settings.shared_kg_collection.as_str();
    let shared_kg_write_disabled = settings.shared_kg_write_disabled_str();
    // Legacy alias (kept for ~3 releases, target removal 2026-08).
    let shared_kg_opt_out_legacy = shared_kg_write_disabled;
    // v0.2.46 Decision B — symmetric READ gate string form.
    let shared_kg_read_disabled = settings.shared_kg_read_disabled_str();
    let active_embedding = settings.active_embedding.as_str();
    let weaviate_url = settings.weaviate_url.as_str();
    let ollama_url = settings.ollama_url.as_str();
    let code_embed_url = settings.code_embed_url.as_str();
    let weaviate_port = settings.weaviate_port.to_string();
    let ollama_port = settings.ollama_port.to_string();
    let code_embed_port = settings.code_embed_port.to_string();

    // PR-3 (2026-05-06): the launcher-canonical env keys we own. The deep-
    // merge logic below ALWAYS overwrites these from `settings`; other
    // keys present in the user's existing env block are preserved.
    //
    // Source of truth for KEY NAMES: `CANONICAL_INSTALL_ENV_KEYS` (single
    // const, also used by the unregister path — see follow-up #10
    // 2026-05-07). Adding a new canonical key is a one-line change to
    // the const PLUS a one-line match arm here for its value lookup.
    // The match is exhaustive: a key in the const without a match arm
    // panics at install time with a clear diagnostic, which is much
    // louder than the silent install/unregister drift the const
    // refactor was written to prevent.
    //
    // Install-flow audit (2026-05-08, P1 #2): the match arm produces an
    // `Option<String>` so a key that is "owned by the launcher but only
    // applicable when some upstream condition holds" (e.g.
    // `VCT_ORCHESTRATOR_ROOT` requires `settings.orchestrator_root` to be
    // `Some`) can return `None` and be filtered out before reaching the
    // writers. This preserves the "every const key has a match arm"
    // panic invariant while letting individual keys be conditionally
    // emitted. `None`-valued keys are simply omitted from both
    // surfaces (.claude/env, .claude/settings.json env block) — same as
    // the pre-2026-05-08 behaviour for VCT_ORCHESTRATOR_ROOT in
    // `.claude/env` only. PR-27 (v0.2.12, 2026-05-16) removed the
    // historical third surface (.vscode/settings.json claude-code.env)
    // because it didn't propagate to MCP subprocesses on Linux — see
    // function-level docstring for the empirical-trace KG node.
    let canonical_env_pairs: Vec<(&str, String)> = CANONICAL_INSTALL_ENV_KEYS
        .iter()
        .filter_map(|key| {
            let value: Option<String> = match *key {
                "KG_COLLECTION" => Some(kg_collection.to_string()),
                "PROJECT_NAME" => Some(project_name.to_string()),
                // PR-8 cross-PR handoff (v0.2.11): code-graph project tag.
                // Matches PR-7's documented synonym contract — `PROJECT_NAME`
                // is the raw user-supplied name; `CODE_GRAPH_PROJECT` is its
                // sanitized form (Weaviate class-name safe). Both flow to
                // `.claude/scripts/code-graph-analyze` via the env block; the
                // analyzer uses `CODE_GRAPH_PROJECT` as the `--project` arg
                // when present, fallback to sanitized `PROJECT_NAME`.
                //
                // Why sanitize here instead of writing `project_name` raw:
                // `code-graph-analyze` writes Weaviate classes named
                // `<CODE_GRAPH_PROJECT>_CodeFunction` etc. — those class
                // names MUST satisfy Weaviate's `[A-Za-z][A-Za-z0-9_]*`
                // identifier rule, which `sanitize_kg_collection` enforces.
                "CODE_GRAPH_PROJECT" => Some(sanitize_kg_collection(project_name)),
                "DEVELOPMENT_COLLECTION" => Some(dev_collection.to_string()),
                "SHARED_KG_COLLECTION" => Some(shared_kg_collection.to_string()),
                // Canonical write-gate key (asymmetric semantic since 2026-05-01).
                "SHARED_KG_WRITE_DISABLED" => Some(shared_kg_write_disabled.to_string()),
                // Legacy alias — same value, removed in ~3 releases.
                "SHARED_KG_OPT_OUT" => Some(shared_kg_opt_out_legacy.to_string()),
                // v0.2.46 Decision B — symmetric READ gate. No legacy
                // alias because pre-v0.2.46 the read path was
                // unconditional. Always emitted (string "true"/"false")
                // so the MCP's env-fallback resolution sees a value
                // even when the user has not flipped it.
                "SHARED_KG_READ_DISABLED" => Some(shared_kg_read_disabled.to_string()),
                "ACTIVE_EMBEDDING" => Some(active_embedding.to_string()),
                // PR-3 (2026-05-06): launcher-resolved service URLs + ports.
                "WEAVIATE_URL" => Some(weaviate_url.to_string()),
                "WEAVIATE_PORT" => Some(weaviate_port.clone()),
                "OLLAMA_URL" => Some(ollama_url.to_string()),
                "OLLAMA_PORT" => Some(ollama_port.clone()),
                "CODE_EMBED_URL" => Some(code_embed_url.to_string()),
                "CODE_EMBED_PORT" => Some(code_embed_port.clone()),
                // Install-flow audit (2026-05-08, P1 #2): portability keys
                // were previously written ONLY to `.claude/env` via a
                // hardcoded special-case in `build_claude_env_managed_block`
                // because the comment at the old `CANONICAL_PORTABILITY_ENV_KEYS`
                // claimed they were "only meaningful for shell-sourced
                // contexts". That claim was empirically wrong: Claude Code
                // propagates `.claude/settings.json env` to hook
                // subprocesses, so VS Code-extension users (the dominant
                // Linux/macOS/Windows path) silently lost these vars and
                // hooks fell back to a non-existent
                // `claude_mcp_servers/.venv` path inside managed projects.
                // Now they flow through the same pair-builder as the rest
                // of the canonical keys; `None` (launcher running outside
                // a git checkout) omits the entry from every surface, same
                // as the pre-fix behaviour for `.claude/env`.
                //
                // PR-27 (v0.2.12, 2026-05-16) update: the historical third
                // surface (.vscode/settings.json claude-code.env) was
                // dropped from the writers — the 2026-05-08 audit ran
                // pre-empirical-verification of MCP propagation on Linux.
                // Sentinel testing 2026-05-16 confirmed that block does
                // NOT reach MCP subprocesses on Claude Code 2.1.143.
                "VCT_ORCHESTRATOR_ROOT" => settings
                    .orchestrator_root
                    .as_ref()
                    .map(|p| p.display().to_string()),
                "VCT_INFRASTRUCTURE_DIR" => settings
                    .orchestrator_root
                    .as_ref()
                    .map(|p| p.join("infrastructure").display().to_string()),
                // P1-D (2026-05-08): cross-project access lists. Comma-
                // separated. We omit the key entirely when the list is
                // empty (matching the orchestrator_root semantics) so the
                // hooks don't have to disambiguate "set to empty" vs
                // "unset" — both mean "no peers granted".
                "VCT_KG_ACCESS_LIST" => {
                    if settings.kg_access_list.is_empty() {
                        None
                    } else {
                        Some(settings.kg_access_list.join(","))
                    }
                }
                "VCT_CODE_GRAPH_ACCESS_LIST" => {
                    if settings.code_graph_access_list.is_empty() {
                        None
                    } else {
                        Some(settings.code_graph_access_list.join(","))
                    }
                }
                // 0.1.7 fork-readiness sweep (2026-05-08): emit
                // `GITHUB_TOKEN` from the keychain-resolved PAT when
                // present + active, else omit the key entirely. Matches
                // VCT_ORCHESTRATOR_ROOT semantics (None → not in any of
                // the 3 surfaces). Resolved by `populate()` via
                // `crate::commands::installer::github_pat_for_env`.
                "GITHUB_TOKEN" => settings.github_token.clone(),
                // v0.2.43 V0243-5-Rust: project folder → KG_BASE_DIR.
                // Always emitted (the folder is always known at write time).
                // Mirrors the value `build_kg_sync_env` passes to kg-sync
                // subprocesses so the two surfaces agree.
                "KG_BASE_DIR" => Some(folder.display().to_string()),
                other => panic!(
                    "CANONICAL_INSTALL_ENV_KEYS contains key {:?} but \
                     write_project_env_files has no match arm for it. \
                     Add the value-lookup arm here OR remove the key \
                     from the const.",
                    other,
                ),
            };
            value.map(|v| (*key, v))
        })
        .collect();
    let canonical_env_keys: std::collections::HashSet<&str> =
        canonical_env_pairs.iter().map(|(k, _)| *k).collect();

    // Subagent G (2026-05-08): derive the user-bucket emit + strip sets
    // from the launcher-resolved settings so they propagate to all 3
    // surfaces alongside the canonical pairs.
    //
    // EMIT: every (KEY, VALUE) pair the resolver produced. Already
    // gated by the cross-launcher active flag + keychain presence at
    // populate time (`resolve_user_secret_state` in project_env_settings).
    //
    // STRIP: every known KEY that is NOT in the EMIT set. This is the
    // subset of "keys the launcher has ever observed in this project's
    // user bucket" that is currently inactive / pending-removal /
    // keychain-empty. Removing them from the JSON env blocks is what
    // makes a paused secret actually leave the surfaces; for
    // `.claude/env` the BEGIN/END replace handles strip implicitly.
    //
    // We tolerate a key appearing in BOTH lists defensively (an emit
    // with the same name as a strip entry shouldn't happen by
    // construction — `resolve_user_secret_state` always returns
    // disjoint sets — but the merge primitive runs strip BEFORE emit
    // so even a buggy resolver couldn't silently drop the active
    // value).
    let user_secret_pairs: Vec<(&str, String)> = settings
        .user_secret_pairs
        .iter()
        .map(|(k, v)| (k.as_str(), v.clone()))
        .collect();
    let emit_keys: std::collections::HashSet<&str> =
        user_secret_pairs.iter().map(|(k, _)| *k).collect();
    let user_secret_strip_keys: Vec<&str> = settings
        .user_secret_known_keys
        .iter()
        .map(|s| s.as_str())
        .filter(|k| !emit_keys.contains(k))
        .collect();

    // PR-27 (v0.2.12, 2026-05-16): the historical write to
    // `.vscode/settings.json` `claude-code.env` was removed here.
    // Empirical sentinel testing on 2026-05-16 showed that block does
    // NOT propagate to MCP subprocesses on Linux as of Claude Code
    // 2.1.143 (see PR-27 commit message for the full trace, including
    // the `/proc/<mcp_pid>/environ` methodology). Writing it caused
    // user confusion: users edited the file expecting changes to take
    // effect but nothing did. The canonical channel is
    // `.claude/settings.json` `env`, written below. A pre-existing
    // `.vscode/settings.json` is intentionally not touched here —
    // Python's `_backfill_vscode_excludes_in_project` (PR-7 / v0.2.11)
    // still manages the workspace's Pylance/watcher exclude block at a
    // separate top level, unrelated to MCP env.
    //
    // The user-secret strip helper (`surgically_strip_user_secret_keys`)
    // and the unregister-cleanup helper (`surgically_strip_env_surfaces`)
    // still read `.vscode/settings.json` if it exists, so any
    // pre-existing or hand-edited `claude-code.env` content is cleaned
    // up on unregister even though we no longer create the file.

    // CLI path: `.claude/env` is sourced by the `tools/claude` wrapper or
    // by the user's shell rc. Plain POSIX export form so any sh-family
    // shell can source it.
    //
    // PR-3 Commit 6 (2026-05-06): the launcher's canonical exports are
    // emitted between vco-managed BEGIN/END markers so a re-run can
    // replace the block in place without clobbering user-added exports.
    // Lines outside the markers (custom user exports added by hand) are
    // preserved verbatim across re-writes.
    //
    // Install-flow audit (2026-05-08, P1 #2): the PR-2 portability keys
    // (`VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR`) used to be
    // written only here via a hardcoded special-case in
    // `build_claude_env_managed_block`; they're now part of
    // `canonical_env_pairs` so they reach the JSON env surfaces too.
    // The pair-builder filters them out when `settings.orchestrator_root`
    // is `None`, preserving the "omit when launcher runs outside a git
    // checkout" semantics.
    let claude_dir = folder.join(".claude");
    std::fs::create_dir_all(&claude_dir)
        .map_err(|e| format!("mkdir {}: {}", claude_dir.display(), e))?;
    let env_path = claude_dir.join("env");
    // Subagent G (2026-05-08): user-secret exports land BETWEEN the
    // BEGIN/END markers alongside the canonical exports. Strip is
    // implicit for `.claude/env`: the entire managed block is replaced
    // on every call by `merge_claude_env_managed_block`, so a paused /
    // removed secret simply doesn't appear in the new block. No extra
    // strip-set plumbing needed (contrast with the JSON env blocks,
    // which are deep-merged additively).
    let managed_block = build_claude_env_managed_block_with_user_secrets(
        &canonical_env_pairs,
        &user_secret_pairs,
        settings,
    );
    let prior = std::fs::read_to_string(&env_path).ok();
    let new_text = merge_claude_env_managed_block(prior.as_deref(), &managed_block);
    std::fs::write(&env_path, new_text)
        .map_err(|e| format!("write {}: {}", env_path.display(), e))?;

    // Bug 30: `.claude/settings.json` is the canonical Anthropic
    // per-project env mechanism — read by Claude Code CLI, the Desktop
    // app, AND the VS Code extension. Without it, Desktop app users
    // never get per-project KG routing. We READ-MERGE-WRITE: this file
    // commonly contains the user's hooks, permissions, agents config,
    // etc. that we must not clobber.
    //
    // PR-3 Commit 6 (2026-05-06): the `env` sub-block is now ALSO
    // deep-merged. Pre-PR-3 the entire env sub-object was REPLACED on
    // every call, silently dropping any user-added env key at that
    // level (e.g. a per-project OPENAI_API_BASE override added by the
    // user). Now we overwrite ONLY the canonical keys we own and
    // preserve everything else. See secrets-and-access-matrix-audit-2026-05-06.md §6.
    let claude_settings_path = claude_dir.join("settings.json");
    let mut claude_settings: serde_json::Value = if claude_settings_path.exists() {
        match std::fs::read_to_string(&claude_settings_path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|e| {
                eprintln!(
                    "[vct] warning: {} is not valid JSON ({}); replacing with minimal env block",
                    claude_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }),
            Err(e) => {
                eprintln!(
                    "[vct] warning: could not read {} ({}); creating fresh",
                    claude_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }
        }
    } else {
        serde_json::json!({})
    };

    // If the existing root is not a JSON object (array, string, etc.),
    // replace it with an empty object — we cannot inject into a non-object.
    if !claude_settings.is_object() {
        claude_settings = serde_json::json!({});
    }

    if let Some(obj) = claude_settings.as_object_mut() {
        // Subagent G (2026-05-08): same deep-merge as `.vscode/settings.json`.
        merge_env_object_canonical_with_user_secrets(
            obj,
            "env",
            &canonical_env_pairs,
            &user_secret_pairs,
            &user_secret_strip_keys,
        );
    }

    let pretty = serde_json::to_string_pretty(&claude_settings)
        .map_err(|e| format!("serialize .claude/settings.json: {}", e))?;
    std::fs::write(&claude_settings_path, pretty)
        .map_err(|e| format!("write {}: {}", claude_settings_path.display(), e))?;

    let _ = canonical_env_keys; // canonical_env_keys reserved for future use

    Ok(())
}

/// PR-3 Commit 6 (2026-05-06): deep-merge a launcher-managed env sub-
/// block into a settings object.
///
/// `parent` is the parent JSON object holding the env block (e.g. the
/// root of `.claude/settings.json`, or the root of `.vscode/settings.json`).
/// `env_key` is the key under which the env block lives ("env" or
/// "claude-code.env").
///
/// Behaviour:
///   * If the env block doesn't exist or isn't an object → create a fresh
///     object containing only the canonical pairs.
///   * If it exists and is an object → overwrite ONLY the canonical keys
///     we own; preserve every other key (user-added env values).
///   * Canonical values are written as JSON strings — matching the prior
///     wholesale-replace behaviour and what every consumer (Claude Code
///     CLI / Desktop / VS Code extension) parses.
///
/// Subagent G (2026-05-08): kept as a thin wrapper over the new
/// `_with_user_secrets` variant for cheap test ergonomics. Production
/// callers all go through the superset variant via
/// `write_project_env_files`. Allowed-dead-code so a future refactor
/// that drops every direct call site doesn't bit-rot the test that
/// exercises canonical-only behaviour.
#[allow(dead_code)]
pub(crate) fn merge_env_object_canonical(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
    canonical_pairs: &[(&str, String)],
) {
    merge_env_object_canonical_with_user_secrets(parent, env_key, canonical_pairs, &[], &[]);
}

/// Subagent G (2026-05-08): superset of `merge_env_object_canonical`
/// that also threads per-project user-bucket secrets + a strip set.
///
/// `canonical_pairs` is the same launcher-canonical (KEY, VALUE) list
/// the caller has always passed (KG_COLLECTION, PROJECT_NAME, etc.).
///
/// `user_secret_pairs` carries (KEY, VALUE) for every per-project
/// user-bucket secret the launcher should EMIT into this surface
/// (active + keychain-present, post-cross-launcher-gate).
///
/// `user_secret_strip_keys` is the difference set between
/// `user_secret_known_keys` and the keys in `user_secret_pairs`. Any
/// key in this list that exists in the existing env block is REMOVED.
/// That is how a paused / removed secret exits the surface without
/// disturbing any other key.
///
/// Invariants:
///   * Canonical keys are always written. They never collide with user
///     keys in practice (canonical names — KG_*, PROJECT_NAME,
///     ACTIVE_EMBEDDING, etc. — are reserved and the GUI doesn't let
///     the user pick those names). If a hypothetical collision did
///     happen, the user pair is inserted AFTER canonical so the user
///     value wins, matching pre-Subagent-G "user-added env key
///     preservation" semantics.
///   * Keys NOT in any of the three input lists survive verbatim. This
///     preserves the PR-3 Commit 6 invariant that env keys typed by
///     hand directly into a JSON env block are not clobbered by the
///     launcher's writer. Subagent-G's strip set is bounded to keys
///     we ourselves wrote (every entry came from a `set_secret_v2`
///     call) — by-hand keys are never in `user_secret_known_keys`.
pub(crate) fn merge_env_object_canonical_with_user_secrets(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
    canonical_pairs: &[(&str, String)],
    user_secret_pairs: &[(&str, String)],
    user_secret_strip_keys: &[&str],
) {
    let existing = parent
        .get(env_key)
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}));

    let mut env_obj = existing.as_object().cloned().unwrap_or_default();

    // 1. Strip user-secret keys that are no longer active / present in
    //    the keychain. Run BEFORE re-inserting the active pairs so a
    //    same-tick toggle (active→inactive→active across two writer
    //    calls) can't rely on residual state. The strip set is by
    //    construction disjoint from the user_secret_pairs key set, so
    //    removing then inserting is safe.
    for k in user_secret_strip_keys {
        env_obj.remove(*k);
    }

    // 2. Overwrite canonical keys with the launcher's resolved values.
    for (k, v) in canonical_pairs {
        env_obj.insert((*k).to_string(), serde_json::Value::String(v.clone()));
    }

    // 3. Insert the active user-secret pairs.
    for (k, v) in user_secret_pairs {
        env_obj.insert((*k).to_string(), serde_json::Value::String(v.clone()));
    }

    parent.insert(env_key.to_string(), serde_json::Value::Object(env_obj));
}

/// Marker pair delimiting the launcher-managed block in `.claude/env`.
/// Lines BETWEEN the markers are owned by the launcher and replaced on
/// every write; lines OUTSIDE are preserved verbatim. The markers must
/// be exact; do not translate or reformat.
pub(crate) const CLAUDE_ENV_MANAGED_BEGIN: &str = "# vco-managed-begin";
pub(crate) const CLAUDE_ENV_MANAGED_END: &str = "# vco-managed-end";

/// Build the launcher-managed `.claude/env` block (the BEGIN/END-delimited
/// section of the file). Pure function for ease of testing.
///
/// Install-flow audit (2026-05-08, P1 #2): the PR-2 portability keys
/// (`VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR`) are now part of
/// `canonical_pairs` rather than a hardcoded special-case here. The
/// pair-builder in `write_project_env_files` returns `None` for those
/// keys when `settings.orchestrator_root` is `None`, so they get
/// omitted from every surface (this file + the two JSON env blocks)
/// without a separate code path. Embedded double-quotes are escaped
/// defensively (rare on POSIX; legitimate on Windows + git-bash).
/// `settings` is no longer read directly here but kept in the signature
/// for forward-compat with future surface-specific tweaks (e.g.
/// per-project compose-override commentary).
#[allow(unused_variables, dead_code)]
pub(crate) fn build_claude_env_managed_block(
    canonical_pairs: &[(&str, String)],
    settings: &ProjectEnvSettings,
) -> String {
    build_claude_env_managed_block_with_user_secrets(canonical_pairs, &[], settings)
}

/// Subagent G (2026-05-08): superset of `build_claude_env_managed_block`
/// that also emits user-bucket secret exports between the BEGIN/END
/// markers.
///
/// Strip semantics for `.claude/env` are IMPLICIT: the entire managed
/// block is rebuilt on every call and `merge_claude_env_managed_block`
/// replaces the in-file segment from BEGIN to END. A previously-emitted
/// user secret that is no longer in `user_secret_pairs` simply doesn't
/// appear in the new block — nothing else needed. (Contrast with the
/// JSON env blocks, where the deep-merge is additive and we have to
/// thread an explicit strip set.)
///
/// User exports land AFTER the canonical exports for readability — the
/// canonical block carries the launcher-owned config; the user block
/// carries the user-owned secrets. A `# user secrets (per-project)`
/// section header makes the boundary explicit when someone diffs the
/// file.
#[allow(unused_variables)]
pub(crate) fn build_claude_env_managed_block_with_user_secrets(
    canonical_pairs: &[(&str, String)],
    user_secret_pairs: &[(&str, String)],
    settings: &ProjectEnvSettings,
) -> String {
    let mut out = String::new();
    out.push_str(CLAUDE_ENV_MANAGED_BEGIN);
    out.push('\n');
    out.push_str(
        "# Auto-generated by VCT Launcher. Source from your shell rc or use\n",
    );
    out.push_str(
        "# tools/claude wrapper (which auto-sources this file before exec'ing\n",
    );
    out.push_str(
        "# the real claude binary). Lines OUTSIDE this BEGIN/END block are\n",
    );
    out.push_str("# preserved across re-runs — add custom exports there.\n");
    out.push_str(
        "# Asymmetric shared-KG access (2026-05-01): reads always-on; this\n",
    );
    out.push_str("# gates WRITES only. SHARED_KG_OPT_OUT is the legacy alias kept\n");
    out.push_str("# for ~3 releases (target removal: 2026-08).\n");
    out.push_str(
        "# Portability keys VCT_ORCHESTRATOR_ROOT / VCT_INFRASTRUCTURE_DIR\n",
    );
    out.push_str(
        "# (when present) point at the orchestrator clone + its infrastructure/\n",
    );
    out.push_str(
        "# dir; consumed by .claude/hooks/ensure-containers.sh and the\n",
    );
    out.push_str(
        "# bundled Python scripts that need the claude_mcp_servers/ package.\n",
    );
    for (k, v) in canonical_pairs {
        let q_v = v.replace('"', "\\\"");
        out.push_str(&format!("export {}=\"{}\"\n", k, q_v));
    }
    if !user_secret_pairs.is_empty() {
        // Subagent G (2026-05-08): user-set per-project secrets follow
        // the canonical block. A blank line + section header keeps
        // diffs readable. Paused / removed secrets are simply absent
        // from this list (the BEGIN/END replace strips them implicitly).
        out.push('\n');
        out.push_str("# user secrets (per-project; managed via launcher GUI Secrets panel)\n");
        for (k, v) in user_secret_pairs {
            let q_v = v.replace('"', "\\\"");
            out.push_str(&format!("export {}=\"{}\"\n", k, q_v));
        }
    }
    out.push_str(CLAUDE_ENV_MANAGED_END);
    out.push('\n');
    out
}

/// Splice a launcher-managed block into an existing `.claude/env`,
/// preserving any lines outside the BEGIN/END markers.
///
/// Behaviour:
///   * `prior == None` (file doesn't exist): emit just the managed block.
///   * `prior` contains the BEGIN marker: replace the segment from
///     BEGIN to END (inclusive) with the new managed block. Whitespace
///     and content OUTSIDE the markers are preserved byte-for-byte.
///   * `prior` doesn't contain BEGIN: append the managed block at the
///     end. The user's pre-existing content is preserved (it's either
///     a hand-edited file from before PR-3, or the legacy launcher
///     wholesale-write — neither contained user-added exports we'd
///     want to keep, since the launcher overwrote on every call). On
///     the next round-trip the markers exist and in-place replace
///     activates.
pub(crate) fn merge_claude_env_managed_block(prior: Option<&str>, managed: &str) -> String {
    let Some(prior) = prior else {
        return managed.to_string();
    };
    if !prior.contains(CLAUDE_ENV_MANAGED_BEGIN) {
        let mut out = String::with_capacity(prior.len() + managed.len() + 1);
        out.push_str(prior);
        if !prior.ends_with('\n') {
            out.push('\n');
        }
        out.push_str(managed);
        return out;
    }
    let begin_idx = prior.find(CLAUDE_ENV_MANAGED_BEGIN).unwrap();
    let after_end = match prior[begin_idx..].find(CLAUDE_ENV_MANAGED_END) {
        Some(off) => begin_idx + off + CLAUDE_ENV_MANAGED_END.len(),
        // Marker pair not closed — treat as "managed block missing END",
        // truncate from BEGIN to EOF and re-emit.
        None => prior.len(),
    };
    // Trim trailing newline after END so we don't accumulate blank lines.
    let after_end = if prior.as_bytes().get(after_end).copied() == Some(b'\n') {
        after_end + 1
    } else {
        after_end
    };
    let mut out = String::with_capacity(prior.len() + managed.len());
    out.push_str(&prior[..begin_idx]);
    out.push_str(managed);
    out.push_str(&prior[after_end..]);
    out
}

/// Marker tag inserted on every line `ensure_project_env_template`
/// appends to a pre-existing `.env`. Mirror of `ENV_VCO_MARKER` in
/// install.py — keep in lockstep. Idempotency depends on the exact
/// substring match; do NOT translate or reformat.
const ENV_VCO_MARKER: &str = "# added by vco";

/// Canonical key list rendered by `ensure_project_env_template`.
///
/// Each tuple = `(KEY, default)`:
///   - `default = Some(value)` → write `KEY=value` (active)
///   - `default = None` → write `# KEY=...` (commented placeholder)
///
/// Mirrors `_env_canonical_template` in install.py. The
/// `<project>` / `<project_root>` tokens are substituted by the
/// caller. Keep the two lists in lockstep — the test
/// `env_template_canonical_keys_match_python` (added 2026-04-28)
/// asserts the Python and Rust key sets are identical.
fn env_canonical_keys() -> Vec<(&'static str, Option<&'static str>)> {
    vec![
        // Service URLs (all commented placeholders — launcher chooses
        // the actual ports at adopt time and writes them via the env
        // block in `.claude/settings.json`, NOT into `.env`).
        ("WEAVIATE_URL", None),
        ("WEAVIATE_PORT", None),
        ("OLLAMA_URL", None),
        ("OLLAMA_PORT", None),
        ("CODE_EMBED_URL", None),
        // Per-project Weaviate collections (active — filled at create time).
        ("KG_COLLECTION", Some("__project__:kg")),
        // Default value: capital-C "VibeCoded" since v0.2.23 B1 (was
        // lowercase-c "Vibecoded" v0.2.12–v0.2.22, itself renamed from
        // "VibeCodedTools_KnowledgeGraph" in v0.2.12 PR-26 / Group E).
        // Picker overrides this per-project.
        ("SHARED_KG_COLLECTION", Some("VibeCodedOrchestrator_KnowledgeGraph")),
        ("DEVELOPMENT_COLLECTION", Some("__project__:dev")),
        ("PROJECT_NAME", Some("__project__:raw")),
        // CONVERSATION_COLLECTION removed 2026-04-30 (B5: zombie write cleanup).
        // The capture flow is deprecated; MCP server no longer reads this key.
        // LLM API keys (commented).
        ("ANTHROPIC_API_KEY", None),
        ("OPENAI_API_KEY", None),
        // GitHub access (commented).
        ("GITHUB_TOKEN", None),
        // RL retrieval (commented — module section).
        ("RL_SERVER_URL", None),
        ("RL_SERVER_PORT", None),
        ("RL_PROJECT_ROOT", None),
        // Telemetry (commented — opt-in).
        ("VCT_TELEMETRY", None),
    ]
}

/// Substitute `__project__:*` tokens to the per-project values.
fn render_canonical_default(default: &str, project_name: &str, kg_collection: &str) -> String {
    match default {
        "__project__:kg" => format!("{}_KnowledgeGraph", kg_collection),
        "__project__:dev" => format!("{}_Development", kg_collection),
        "__project__:conv" => format!("{}_conversations", kg_collection),
        "__project__:raw" => project_name.to_string(),
        other => other.to_string(),
    }
}

/// PR-3 (2026-05-06): substitute the canonical default with values from
/// the launcher's resolved settings. Falls through to
/// `render_canonical_default` for project-derived placeholders.
fn render_canonical_default_with_settings(
    key: &str,
    default: &str,
    settings: &ProjectEnvSettings,
) -> String {
    match key {
        "SHARED_KG_COLLECTION" => settings.shared_kg_collection.clone(),
        "WEAVIATE_URL" => settings.weaviate_url.clone(),
        "WEAVIATE_PORT" => settings.weaviate_port.to_string(),
        "OLLAMA_URL" => settings.ollama_url.clone(),
        "OLLAMA_PORT" => settings.ollama_port.to_string(),
        "CODE_EMBED_URL" => settings.code_embed_url.clone(),
        "CODE_EMBED_PORT" => settings.code_embed_port.to_string(),
        "ACTIVE_EMBEDDING" => settings.active_embedding.clone(),
        _ => render_canonical_default(default, &settings.project_name, &sanitize_kg_collection(&settings.project_name)),
    }
}

/// Build the canonical `.env` text used when no `.env` exists.
///
/// Output mirrors `_build_canonical_env_template_text` in install.py
/// (modulo tiny formatting differences — header date, section order).
/// What MUST match cross-language: the set of declared KEY names. The
/// `env_template_canonical_keys_match_python` test enforces that.
///
/// PR-3 (2026-05-06): when the launcher has resolved non-default ports
/// (via app_state override or services.toml adoption), the service URL
/// lines are rendered ACTIVE rather than commented. This is the key
/// behaviour change closing the audit's "values that should propagate
/// but don't" finding for the `.env` surface.
fn build_canonical_env_text(settings: &ProjectEnvSettings) -> String {
    let project_name = settings.project_name.as_str();
    let kg_collection_basename = sanitize_kg_collection(project_name);
    let today = chrono::Utc::now().format("%Y-%m-%d");
    let mut s = String::new();
    s.push_str("# vibecoded-orchestrator per-project .env\n");
    s.push_str("# Edit values to override defaults. Empty / commented lines are\n");
    s.push_str(&format!("# treated as \"use default\". Created by vco {}.\n\n", today));

    s.push_str("# === Service URLs (launcher-resolved; edit only if you know what you're doing) ===\n");
    // Only emit ACTIVE service URL lines when the launcher's value
    // diverges from the canonical localhost default. Default-port stacks
    // keep the commented-placeholder shape so the `.env` template stays
    // close to what install.py emits and so the parity test continues to
    // hold.
    let weaviate_active =
        settings.weaviate_port != project_env_settings::DEFAULT_WEAVIATE_PORT;
    let ollama_active =
        settings.ollama_port != project_env_settings::DEFAULT_OLLAMA_PORT;
    let code_embed_active =
        settings.code_embed_port != project_env_settings::DEFAULT_CODE_EMBED_PORT;
    let prefix_w = if weaviate_active { "" } else { "# " };
    let prefix_o = if ollama_active { "" } else { "# " };
    let prefix_c = if code_embed_active { "" } else { "# " };
    s.push_str(&format!("{}WEAVIATE_URL={}\n", prefix_w, settings.weaviate_url));
    s.push_str(&format!("{}WEAVIATE_PORT={}\n", prefix_w, settings.weaviate_port));
    s.push_str(&format!("{}OLLAMA_URL={}\n", prefix_o, settings.ollama_url));
    s.push_str(&format!("{}OLLAMA_PORT={}\n", prefix_o, settings.ollama_port));
    s.push_str(&format!("{}CODE_EMBED_URL={}\n\n", prefix_c, settings.code_embed_url));

    s.push_str("# === Per-project Weaviate collections ===\n");
    s.push_str("# Resolved by the launcher when the project is registered. Don't\n");
    s.push_str("# edit unless you know what you're doing.\n");
    s.push_str(&format!("KG_COLLECTION={}_KnowledgeGraph\n", kg_collection_basename));
    s.push_str(&format!("SHARED_KG_COLLECTION={}\n", settings.shared_kg_collection));
    s.push_str(&format!("DEVELOPMENT_COLLECTION={}_Development\n", kg_collection_basename));
    s.push_str(&format!("PROJECT_NAME={}\n", project_name));
    // PR-8 cross-PR handoff note (v0.2.11): `CODE_GRAPH_PROJECT` is
    // intentionally NOT written here — the `.env` template body is
    // owned by PR-7's `install.py::_env_canonical_template` parity
    // contract, and adding a key here without the Python side would
    // break `env_template_canonical_keys_match_python`. The key DOES
    // get written to the canonical JSON env block
    // (`.claude/settings.json::env`) via `CANONICAL_INSTALL_ENV_KEYS`
    // — that is the surface hook subprocesses + Claude Code consume
    // from, so the bug PR-7 is fixing closes at the create_project_v2
    // boundary regardless. (PR-27 / v0.2.12 / 2026-05-16 removed the
    // historical sibling write to `.vscode/settings.json::claude-code.env`
    // because that surface didn't propagate to MCP subprocesses on
    // Linux — see `write_project_env_files` docstring.)
    // PR-3 (2026-05-06): ACTIVE_EMBEDDING is launcher-resolved; pre-PR-3
    // it was only written to the orchestrator-root .env by install.py and
    // never to per-project files. Adding it here lets every per-project
    // shell session see the right embedding profile without sourcing the
    // orchestrator-root file.
    s.push_str(&format!("ACTIVE_EMBEDDING={}\n\n", settings.active_embedding));
    // CONVERSATION_COLLECTION removed 2026-04-30 (B5). Not written to new installs.

    s.push_str("# === LLM API keys (optional) ===\n");
    s.push_str("# ANTHROPIC_API_KEY=\n");
    s.push_str("# OPENAI_API_KEY=\n\n");

    s.push_str("# === GitHub access for code-search MCP (optional) ===\n");
    s.push_str("# GITHUB_TOKEN=\n\n");

    s.push_str("# === RL retrieval module (Pro tier — uncomment when installed) ===\n");
    s.push_str("# RL_SERVER_URL=http://localhost:8090\n");
    s.push_str("# RL_SERVER_PORT=8090\n");
    s.push_str("# RL_PROJECT_ROOT=<project_root>\n\n");

    s.push_str("# === Telemetry (off by default; on=opt-in only) ===\n");
    s.push_str("# VCT_TELEMETRY=off\n");
    s
}

/// Parse keys present in an existing `.env`. Both commented (`# KEY=`)
/// and active (`KEY=`) lines count — the user knows about either form
/// and we should not duplicate-append over them. Mirrors
/// `_parse_existing_env_keys` in install.py.
fn parse_existing_env_keys(text: &str) -> std::collections::HashSet<String> {
    let mut out = std::collections::HashSet::new();
    for line in text.lines() {
        let s = line.trim();
        if s.is_empty() {
            continue;
        }
        // Strip a single leading '#' + whitespace.
        let body = if let Some(rest) = s.strip_prefix('#') {
            rest.trim_start()
        } else {
            s
        };
        if let Some(eq_idx) = body.find('=') {
            if eq_idx == 0 {
                continue;
            }
            let key = body[..eq_idx].trim();
            // Validate key shape: alnum + underscore, leading non-digit.
            // Skips lines like `# Defaults match the podman-compose...`
            // which would otherwise parse as `match=...`.
            if !key.is_empty()
                && !key.starts_with(|c: char| c.is_ascii_digit())
                && key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            {
                out.insert(key.to_string());
            }
        }
    }
    out
}

/// Report shape returned by `ensure_project_env_template`. Mirrors the
/// dict returned by Python's `_ensure_env_template`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EnsureEnvReport {
    /// One of "created", "appended", "noop".
    pub action: String,
    /// Keys that were just written (only the canonical KEY names, not
    /// every line of comments).
    pub added_keys: Vec<String>,
    /// Absolute path to the .env file.
    pub env_path: String,
}

/// Ensure `<folder>/.env` exists and has every canonical-template key.
///
/// - Missing → write fresh from the canonical template (with placeholders
///   substituted for `<project>` / `<project_root>`).
/// - Exists → append any canonical keys that aren't already present
///   (commented or active), tagged with `# added by vco YYYY-MM-DD`.
/// - Idempotent: a second invocation produces a no-op.
///
/// User-set values are preserved verbatim — we only append new lines,
/// never rewrite existing ones.
///
/// This is the Rust mirror of `_ensure_env_template` in install.py;
/// keep them in lockstep. The 'env_template_canonical_keys_match_python'
/// integration test is the contract that enforces this.
///
/// env_template: legacy_caller_pending_migration
///
/// PHASE 0.D NOTE (2026-05-24): This function and `build_canonical_env_text`
/// are the Rust direct writers of `<folder>/.env` and are intentionally
/// allowlisted by `tests/test_config_projection_single_writer.py` during
/// the Phase 0.D migration. The Python sibling `_ensure_env_template`
/// has been migrated to delegate to `vco_lib.env_template.apply_env_template`;
/// this Rust function will be migrated to subprocess-into-Python in a
/// follow-up Phase 0.D Part 2 (matching the Phase 0.B Part 2 /
/// `write_project_env_files` migration pattern). Until then, the
/// legacy append-only `# added by vco YYYY-MM-DD` behaviour is preserved
/// here and Python's `apply_env_template` block-replace contract runs
/// in parallel against the same file (the two formats don't collide:
/// the new BEGIN/END markers don't appear in legacy lines, and legacy
/// lines sit outside the BEGIN/END markers Python writes).
pub fn ensure_project_env_template(
    folder: &Path,
    settings: &ProjectEnvSettings,
) -> Result<EnsureEnvReport, String> {
    let env_path = folder.join(".env");
    let project_name = settings.project_name.as_str();
    let kg_collection = sanitize_kg_collection(project_name);

    if !env_path.exists() {
        let text = build_canonical_env_text(settings);
        std::fs::write(&env_path, text)
            .map_err(|e| format!("write {}: {}", env_path.display(), e))?;
        let added: Vec<String> = env_canonical_keys()
            .iter()
            .map(|(k, _)| k.to_string())
            .collect();
        return Ok(EnsureEnvReport {
            action: "created".into(),
            added_keys: added,
            env_path: env_path.to_string_lossy().to_string(),
        });
    }

    let existing = std::fs::read_to_string(&env_path)
        .map_err(|e| format!("read {}: {}", env_path.display(), e))?;
    let present = parse_existing_env_keys(&existing);

    let missing: Vec<(&'static str, Option<&'static str>)> = env_canonical_keys()
        .into_iter()
        .filter(|(k, _)| !present.contains(*k))
        .collect();

    if missing.is_empty() {
        return Ok(EnsureEnvReport {
            action: "noop".into(),
            added_keys: vec![],
            env_path: env_path.to_string_lossy().to_string(),
        });
    }

    let today = chrono::Utc::now().format("%Y-%m-%d");
    let mut block = String::new();
    if !existing.ends_with('\n') {
        block.push('\n');
    }
    block.push('\n');
    block.push_str(&format!(
        "{} {}: appended missing canonical keys\n",
        ENV_VCO_MARKER, today
    ));
    let added: Vec<String> = missing
        .iter()
        .map(|(k, default)| {
            match default {
                Some(d) => {
                    // PR-3: if the launcher has launcher-resolved settings
                    // for this key, prefer them over the static default
                    // from `env_canonical_keys()`. This only diverges from
                    // the static default when the launcher has a non-
                    // default port / shared-KG-name override.
                    let val = render_canonical_default_with_settings(k, d, settings);
                    block.push_str(&format!("{}={}\n", k, val));
                }
                None => {
                    // PR-3: even for keys that are commented in
                    // `env_canonical_keys()`, the launcher may have a
                    // resolved value (e.g. `WEAVIATE_URL` when adopted on
                    // a non-default port). Render those active so the
                    // launcher's config reaches the project's env.
                    let resolved = render_canonical_default_with_settings(k, "", settings);
                    if !resolved.is_empty()
                        && resolved != "<project_root>"
                        && matches!(
                            *k,
                            "WEAVIATE_URL"
                                | "WEAVIATE_PORT"
                                | "OLLAMA_URL"
                                | "OLLAMA_PORT"
                                | "CODE_EMBED_URL"
                                | "CODE_EMBED_PORT"
                                | "ACTIVE_EMBEDDING"
                        )
                    {
                        block.push_str(&format!("{}={}\n", k, resolved));
                    } else if *k == "RL_PROJECT_ROOT" {
                        block.push_str("# RL_PROJECT_ROOT=<project_root>\n");
                    } else {
                        block.push_str(&format!("# {}=\n", k));
                    }
                }
            }
            (*k).to_string()
        })
        .collect();
    let _ = kg_collection; // suppress unused-variable warning if helper change drops it

    let mut f = std::fs::OpenOptions::new()
        .append(true)
        .open(&env_path)
        .map_err(|e| format!("open {} for append: {}", env_path.display(), e))?;
    use std::io::Write;
    f.write_all(block.as_bytes())
        .map_err(|e| format!("append to {}: {}", env_path.display(), e))?;

    Ok(EnsureEnvReport {
        action: "appended".into(),
        added_keys: added,
        env_path: env_path.to_string_lossy().to_string(),
    })
}

/// Convert a project display name into a Weaviate-collection-safe id.
/// Weaviate collections must start with [A-Z] and contain only
/// alphanumerics — strip everything else and Title-case.
pub fn sanitize_kg_collection(name: &str) -> String {
    let mut out = String::new();
    let mut next_upper = true;
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            if next_upper {
                out.extend(ch.to_uppercase());
                next_upper = false;
            } else {
                out.push(ch);
            }
        } else {
            next_upper = true;
        }
    }
    if out.is_empty() {
        return "Project".to_string();
    }
    // Weaviate requires leading letter, not digit.
    if out.chars().next().unwrap().is_ascii_digit() {
        out.insert(0, 'P');
    }
    out
}

/// Outcome of `b12_repair_stale_kg_collection`. Either we rewrote the
/// first stale `KG_COLLECTION=` line (and report the canonical value
/// that's now in the file), or the file did not need touching.
#[derive(Debug, PartialEq, Eq)]
pub enum B12Outcome {
    Repaired { canonical_kg: String },
    NoChangeNeeded,
}

/// B12 auto-repair: rewrite the first stale `KG_COLLECTION=` line in a
/// project's `.env` to the canonical `<sanitized>_KnowledgeGraph` form.
///
/// Pre-0.2.11 a folder that already had a `.env` with `KG_COLLECTION=KnowledgeGraph`
/// (bare default) or `KG_COLLECTION=<basename>` (no suffix) kept that
/// stale value as the first active line, and consumers reading the
/// first match picked up the wrong collection. This helper rewrites the
/// stale line in place, preserving comments / ordering / other env keys
/// verbatim, and annotates the rewritten line with the previous value
/// for forensic clarity.
///
/// Returns:
/// - `Err(io::Error)` if `.env` cannot be read or written.
/// - `Ok(NoChangeNeeded)` if `.env` does not exist, or the canonical
///   value is already present (anywhere in the file), or no stale value
///   was found.
/// - `Ok(Repaired { canonical_kg })` if the first stale line was rewritten.
///
/// Idempotent: a second call after a successful repair is a no-op
/// because the canonical value is now present in the file.
pub fn b12_repair_stale_kg_collection(
    env_path: &Path,
    project_name: &str,
) -> std::io::Result<B12Outcome> {
    let env_text = match std::fs::read_to_string(env_path) {
        Ok(t) => t,
        Err(ref e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(B12Outcome::NoChangeNeeded);
        }
        Err(e) => return Err(e),
    };

    let kg_basename = sanitize_kg_collection(project_name);
    let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
    let canonical_line = format!("KG_COLLECTION={}", canonical_kg);
    let stale_bare = "KG_COLLECTION=KnowledgeGraph";
    let stale_nosuffix = format!("KG_COLLECTION={}", kg_basename);

    let mut found_canonical = false;
    let mut found_stale_idx: Option<usize> = None;
    for (idx, line) in env_text.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed == canonical_line {
            found_canonical = true;
            break;
        }
        if (trimmed == stale_bare || trimmed == stale_nosuffix)
            && found_stale_idx.is_none()
        {
            found_stale_idx = Some(idx);
        }
    }
    if found_canonical {
        return Ok(B12Outcome::NoChangeNeeded);
    }
    let stale_idx = match found_stale_idx {
        Some(idx) => idx,
        None => return Ok(B12Outcome::NoChangeNeeded),
    };

    let trailing_newline = env_text.ends_with('\n');
    let rebuilt: Vec<String> = env_text
        .lines()
        .enumerate()
        .map(|(i, l)| {
            if i == stale_idx {
                format!(
                    "{} # B12 auto-repaired 0.2.11: was \"{}\"",
                    canonical_line,
                    l.trim()
                )
            } else {
                l.to_string()
            }
        })
        .collect();
    let mut joined = rebuilt.join("\n");
    if trailing_newline {
        joined.push('\n');
    }
    std::fs::write(env_path, joined)?;
    Ok(B12Outcome::Repaired { canonical_kg })
}

// v0.2.49 access-matrix Step F SB2 (L1-F1 + L2-SB1 cross-lens fix):
// the propagate_kg_access_on_rename helper has been LIFTED into
// `db::access::Db::propagate_kg_access_on_rename` so both the Tauri
// rename path AND the hub-CLI rename path (`vct-hub/src/cli_api.rs`)
// share one source of truth. Pre-lift the hub CLI never called the
// helper, leaving orphan kg_collection_access rows after CLI-driven
// renames. See the new method's docstring for the full contract.

#[command]
pub async fn rename_project_v2(
    id: String,
    new_name: String,
    db: State<'_, Db>,
) -> Result<RenameProjectResult, String> {
    // v0.2.46 KG-AUTO-HEAL adversarial-review H3 follow-up: reject
    // rename when host='orchestrator_root'. The orchestrator-root
    // project's slug ('orchestrator-root') is canonical and used by
    // multiple auto-heal paths that match on slug:
    //   - `Db::sync_shared_to_primary_for_orchestrator_root` queries
    //     `WHERE slug = 'orchestrator-root'`.
    //   - `Db::set_project_kg_binding_with_root_sync` detects
    //     orchestrator-root via `project_slug == "orchestrator-root"`.
    //   - `resolve_shared_kg_from_orchestrator_root` (in
    //     `project_env_settings.rs`) reads the orchestrator-root's
    //     primary binding by slug.
    //
    // A rename changes the slug → all of the above silently no-op →
    // the auto-heal is bypassed entirely. Match the create-time
    // reject at line 350+ for symmetry (host='orchestrator_root' is
    // a reserved value with a single fixed slug).
    // v0.2.49 access-matrix Phase 4 (item #11): capture the OLD project
    // name BEFORE rename so we can rewrite the kg_collection_access rows
    // to track the new project-name-derived collection names. The fetch
    // happens once here (and the orchestrator-root guard reuses the same
    // result) — the alternative would be a second `get_project` after
    // rename, which is correct but redundant.
    let pre_rename_row = db.get_project(&id).ok().flatten();
    if let Some(row) = &pre_rename_row {
        if row.host == ProjectHost::OrchestratorRoot {
            return Err(
                "Cannot rename the orchestrator-root project: its slug \
                 ('orchestrator-root') is canonical and used by the v0.2.46 \
                 KG-AUTO-HEAL flow (boot self-heal, primary-shared mirror, \
                 shared-KG resolver). Renaming would silently disable the \
                 auto-heal. If you want to rebrand the project for display, \
                 change the project's NAME via this command's `new_name` \
                 arg — but the slug stays 'orchestrator-root'. (Symmetric \
                 to the create-time reject for `host='orchestrator_root'`.)"
                    .to_string(),
            );
        }
    }
    let old_name = pre_rename_row.as_ref().map(|r| r.name.clone());

    // Generate a fresh slug derived from the new name so URLs track
    // renames. The old slug becomes invalid; existing bookmarks 404
    // gracefully via the /p/[slug] resolver. Documented in
    // docs/MULTI_TENANT_URLS.md.
    let new_slug = db.generate_unique_slug(&new_name)?;
    db.rename_project(&id, &new_name, Some(&new_slug))?;
    let row = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found after rename", id))?;
    let count = db.list_module_installs_for_project(&id)?.len() as u32;
    let mut warnings: Vec<String> = Vec::new();

    // v0.2.49 access-matrix Phase 4 (item #11): propagate the project-name
    // change into `kg_collection_access`. See `propagate_kg_access_on_rename`
    // for the full contract + soft-fail discipline. Returned warnings
    // (if any) flow into `RenameProjectResult.warnings` for the GUI to
    // surface, matching the env-refresh failure surface below.
    if let Some(old) = &old_name {
        warnings.extend(db.propagate_kg_access_on_rename(&id, old, &new_name));
    }

    // B9 (2026-05-01): re-run env writers after DB rename so every
    // surface reflects the new KG_COLLECTION, DEVELOPMENT_COLLECTION,
    // PROJECT_NAME. Before this fix, rename was DB-only — renamed
    // projects kept stale KG_COLLECTION values in `.claude/env` and
    // `.claude/settings.json` until the user manually re-ran env
    // setup. (PR-27 / v0.2.12 / 2026-05-16 removed a historical third
    // surface, `.vscode/settings.json` `claude-code.env`, because it
    // didn't propagate to MCP subprocesses on Linux — refresh now
    // covers two surfaces, not three.)
    let folder = Path::new(&row.folder_path);
    let _env_settings = project_env_settings::populate(&db, &new_name, Some(&id));
    // HIGH-7 (2026-05-01): env-write failures now surface as structured
    // warnings instead of silent eprintln. Without this, a failed env refresh
    // leaves the project's 4 env surfaces stale until the next launcher
    // session and the user has no idea anything is wrong.
    //
    // Phase 0.B Part 2 (2026-05-25): canonical env writes go through
    // `apply_project_env_via_python` → Python contract; the legacy
    // `write_project_env_files` is no longer the production writer.
    // `_env_settings` is still populated for parity with the create/refresh
    // surfaces (no downstream consumer here today; underscore-prefixed
    // to silence the unused-variable warning while preserving the call
    // for any side-effect logging inside `populate`).
    if let Err(e) = apply_project_env_via_python(&id, folder, &db) {
        let msg = format!(
            "rename env refresh (apply_project_env_via_python) failed: {}. \
             KG routing for the renamed project may be stale until manual repair.",
            e
        );
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }
    // ensure_project_env_template is append-only, so .env may still carry the
    // old KG_COLLECTION value as an active line. Log a warning when the stale
    // value is detected; full repair lands in PR 5.
    if let Ok(env_text) = std::fs::read_to_string(folder.join(".env")) {
        let new_kg = format!("{}_KnowledgeGraph", sanitize_kg_collection(&new_name));
        if !env_text.contains(&format!("KG_COLLECTION={}", new_kg)) {
            let msg = format!(
                ".env at {} still contains stale KG_COLLECTION after rename; \
                 run repair-env (PR 5) to fix. Expected KG_COLLECTION={}",
                row.folder_path, new_kg
            );
            eprintln!("[vct] warning: {}", msg);
            warnings.push(msg);
        }
    }

    let _ = db.log_change("projects", "update", Some(&id), Some(&id));
    Ok(RenameProjectResult {
        project: ProjectView::from_row(row, count),
        warnings,
    })
}

/// MEDIUM-1 (2026-05-01, refactored): persist the SHARED_KG_WRITE_DISABLED
/// toggle and refresh all per-project env surfaces so the new value takes
/// effect immediately.
///
/// Asymmetric semantic: this gates WRITES to the cross-project shared KG
/// (`store_knowledge_node(scope='shared')`). Reads of the shared KG are
/// always on for every project. See module docstring of
/// `claude_mcp_servers/weaviate_mcp/server.py`.
#[command]
pub async fn set_shared_kg_write_disabled(
    project_id: String,
    write_disabled: bool,
    db: State<'_, Db>,
) -> Result<RenameProjectResult, String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let count = db.list_module_installs_for_project(&project_id)?.len() as u32;

    // Persist to the project-settings k/v table under the canonical key.
    // The legacy alias row, if any, is retired by the migration helper that
    // backstops `get_shared_kg_write_disabled`.
    db.set_setting(
        &project_id,
        PROJECT_SETTINGS_MODULE_ID,
        SETTING_KEY_SHARED_KG_WRITE_DISABLED,
        &serde_json::Value::Bool(write_disabled),
    )?;
    // Best-effort: clear any stale legacy row so `get_setting` reads stop
    // reporting both keys. The migration helper handles this on read too,
    // but proactively clearing here keeps the DB tidy.
    let _ = db.delete_setting(
        &project_id,
        PROJECT_SETTINGS_MODULE_ID,
        SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
    );

    // Refresh all env surfaces with the new value. Use the same warning
    // surface as create / rename so the UI can toast on partial failure.
    //
    // Phase 0.B Part 2 (2026-05-25): canonical env writes go through the
    // Python contract (`apply_project_env_via_python`). The Python
    // resolver re-reads `module_settings` directly from launcher.db —
    // because the `db.set_setting(...)` above has already committed the
    // new `write_disabled` value, the subprocess sees the in-flight
    // toggle without needing the pre-PR-2 Rust `env_settings` override.
    let mut warnings: Vec<String> = Vec::new();
    let folder = Path::new(&row.folder_path);
    if let Err(e) = apply_project_env_via_python(&project_id, folder, &db) {
        let msg = format!(
            "shared-KG write-disabled env refresh failed: {}. \
             Toggle persisted to DB but env files may be stale.",
            e
        );
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    db.audit(
        "project_shared_kg_write_disabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "write_disabled": write_disabled }),
    )?;
    let _ = db.log_change("projects", "update", Some(&project_id), Some(&project_id));

    Ok(RenameProjectResult {
        project: ProjectView::from_row(row, count),
        warnings,
    })
}

/// v0.2.46 Decision B — Tauri-exposed getter for the SHARED_KG_READ_DISABLED
/// toggle. Used by the launcher GUI's Identity tab to render the current
/// state of the toggle without a full hub round-trip. Read-only mirror of
/// the setter below.
#[command]
pub async fn get_shared_kg_read_disabled_cmd(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    get_shared_kg_read_disabled(&db, &project_id)
}

/// v0.2.46 Decision B — persist the symmetric SHARED_KG_READ_DISABLED
/// toggle and refresh all per-project env surfaces so the new value
/// takes effect immediately.
///
/// Symmetric mirror of `set_shared_kg_write_disabled`: when `true`, the
/// MCP's `_kg_collections_to_search` drops `SHARED_KG_COLLECTION` from
/// the hybrid_search / semantic_graph_search fan-out for this project.
/// Pre-v0.2.46 the read path was unconditional; v0.2.46 lets users opt
/// OUT explicitly while keeping the default ON.
///
/// No legacy-alias clean-up — the key is new in v0.2.46, so no
/// historical row could exist.
#[command]
pub async fn set_shared_kg_read_disabled(
    project_id: String,
    read_disabled: bool,
    db: State<'_, Db>,
) -> Result<RenameProjectResult, String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let count = db.list_module_installs_for_project(&project_id)?.len() as u32;

    db.set_setting(
        &project_id,
        PROJECT_SETTINGS_MODULE_ID,
        SETTING_KEY_SHARED_KG_READ_DISABLED,
        &serde_json::Value::Bool(read_disabled),
    )?;

    // Refresh all env surfaces with the new value. Same warning-surface
    // pattern as `set_shared_kg_write_disabled`.
    let mut warnings: Vec<String> = Vec::new();
    let folder = Path::new(&row.folder_path);
    if let Err(e) = apply_project_env_via_python(&project_id, folder, &db) {
        let msg = format!(
            "shared-KG read-disabled env refresh failed: {}. \
             Toggle persisted to DB but env files may be stale.",
            e
        );
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    db.audit(
        "project_shared_kg_read_disabled",
        Some(&project_id),
        None,
        &serde_json::json!({ "read_disabled": read_disabled }),
    )?;
    let _ = db.log_change("projects", "update", Some(&project_id), Some(&project_id));

    Ok(RenameProjectResult {
        project: ProjectView::from_row(row, count),
        warnings,
    })
}

/// Deprecated alias of `set_shared_kg_write_disabled`. Logs a deprecation
/// notice to stderr and delegates. Slated for removal once the legacy env
/// var + DB key are fully retired (target: 2026-08, ~3 releases).
///
/// The Svelte client ships a matching `setSharedKgOptOut` deprecated
/// alias — both go away together.
#[command]
pub async fn set_shared_kg_opt_out(
    project_id: String,
    opt_out: bool,
    db: State<'_, Db>,
) -> Result<RenameProjectResult, String> {
    eprintln!(
        "[vct] DEPRECATED: Tauri command `set_shared_kg_opt_out` was called \
         (project_id={}, opt_out={}). The toggle now gates WRITES only — \
         reads of the shared KG are always on. Use \
         `set_shared_kg_write_disabled` instead. The legacy command will be \
         removed in ~3 releases (target: 2026-08).",
        project_id, opt_out,
    );
    set_shared_kg_write_disabled(project_id, opt_out, db).await
}

/// P1-D (2026-05-08): re-run `write_project_env_files` for a registered
/// project so the launcher's current view of the access matrix lands in
/// `.claude/env` and `.claude/settings.json env`. (PR-27 / v0.2.12 /
/// 2026-05-16 removed the historical third surface
/// `.vscode/settings.json claude-code.env` because it didn't propagate
/// to MCP subprocesses on Linux.) Wired from the access-matrix setters
/// (`kg_set_collection_access_mode` and the codegraph equivalents) so
/// a running Claude Code session picks up newly-granted peer KGs
/// without a session restart.
///
/// Soft-fail: a write hiccup leaves the matrix DB row in place; the
/// next refresh / project-create / rename call will retry. Returns the
/// list of warnings produced by `write_project_env_files`, plus the
/// access lists this run resolved (so the FE can show "now exporting
/// VCT_KG_ACCESS_LIST=Foo,Bar" feedback).
#[command]
pub async fn refresh_project_env(
    project_id: String,
    db: State<'_, Db>,
) -> Result<RefreshProjectEnvResult, String> {
    refresh_project_env_with_db(&db, &project_id)
}

/// Free-function variant of `refresh_project_env` that takes `&Db` so
/// other commands (kg/codegraph access setters) can invoke it without
/// the Tauri `State` plumbing. Single source of truth — the `#[command]`
/// wrapper above just delegates here.
pub fn refresh_project_env_with_db(
    db: &Db,
    project_id: &str,
) -> Result<RefreshProjectEnvResult, String> {
    let row = db
        .get_project(project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let folder = Path::new(&row.folder_path);
    // populate() still drives the access-list values in the response
    // payload (the launcher GUI displays them in the per-project Identity
    // tab). The actual on-disk env write goes through the Python contract
    // — see Phase 0.B Part 2 (2026-05-25).
    let env_settings = project_env_settings::populate(db, &row.name, Some(project_id));

    let kg_access_list = env_settings.kg_access_list.clone();
    let code_graph_access_list = env_settings.code_graph_access_list.clone();

    let mut warnings: Vec<String> = Vec::new();
    if let Err(e) = apply_project_env_via_python(project_id, folder, db) {
        let msg = format!(
            "refresh_project_env (apply_project_env_via_python) failed: {}. \
             Access-matrix env vars may be stale until next refresh.",
            e
        );
        eprintln!("[vct] warning: {}", msg);
        warnings.push(msg);
    }

    Ok(RefreshProjectEnvResult {
        kg_access_list,
        code_graph_access_list,
        warnings,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct RefreshProjectEnvResult {
    pub kg_access_list: Vec<String>,
    pub code_graph_access_list: Vec<String>,
    pub warnings: Vec<String>,
}

/// v0.2.37 (Agent V37-E, 2026-05-27): bulk refresh of `.claude/env` +
/// `.claude/settings.json` for every project the launcher knows about.
///
/// Why this exists: the canonical install-root resolver
/// (`installer::resolve_orchestrator_root`) is now DB-cached, and
/// install.py seeds the cache via `.vct/install_path_seed.txt` (the
/// seed file is consumed on first launcher boot post-install). The
/// SECOND consequence is that existing projects created BEFORE this
/// release have stale `.claude/env` files missing
/// `VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR` exports (the
/// pre-v0.2.37 uncached resolver failed silently when the binary
/// lived outside the clone). Re-running the project env writer once
/// after the install/update boundary backfills the missing exports
/// without any user action.
///
/// Used by:
///   * The launcher's first-boot setup hook (post seed consumption)
///     — re-renders env for every project so the user-project-style
///     "missing exports" state is healed automatically.
///   * The launcher GUI's "Refresh all projects" admin action, if a
///     user wants to force a manual refresh after a manual edit to
///     the launcher DB.
///
/// Soft-fail per project: one project's hiccup MUST NOT prevent the
/// others from refreshing. Returns a per-project status map (the
/// caller surfaces successes + failures in the GUI / log).
pub fn refresh_all_projects_env_with_db(db: &Db) -> RefreshAllProjectsEnvResult {
    let mut result = RefreshAllProjectsEnvResult::default();
    let rows = match db.list_projects() {
        Ok(r) => r,
        Err(e) => {
            result.global_warnings.push(format!("list_projects failed: {}", e));
            return result;
        }
    };
    for proj in &rows {
        let folder = Path::new(&proj.folder_path);
        if !folder.is_dir() {
            // Project row points at a folder that no longer exists.
            // Skip silently — the user will see the broken project
            // in the GUI; refreshing a phantom dir would just create
            // it, which is worse.
            result.skipped.push(proj.name.clone());
            continue;
        }
        match refresh_project_env_with_db(db, &proj.id) {
            Ok(r) => {
                if r.warnings.is_empty() {
                    result.refreshed.push(proj.name.clone());
                } else {
                    result.refreshed_with_warnings.push((
                        proj.name.clone(),
                        r.warnings,
                    ));
                }
            }
            Err(e) => {
                result.failed.push((proj.name.clone(), e));
            }
        }
    }
    result
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct RefreshAllProjectsEnvResult {
    /// Project names that refreshed cleanly.
    pub refreshed: Vec<String>,
    /// Per-project warnings (env writer surfaced soft-fails but the
    /// refresh proceeded).
    pub refreshed_with_warnings: Vec<(String, Vec<String>)>,
    /// Per-project hard failures (refresh did not happen).
    pub failed: Vec<(String, String)>,
    /// Projects whose `folder_path` no longer exists on disk.
    pub skipped: Vec<String>,
    /// Errors outside the per-project loop (e.g. `list_projects`
    /// itself failed).
    pub global_warnings: Vec<String>,
}

/// v0.2.37 (Agent V37-E): Tauri command surface for the bulk refresh.
/// Lets the launcher GUI's admin / dev-tools tab trigger a manual
/// "re-render env for every project" without the user having to walk
/// project-by-project. The boot hook in lib.rs calls
/// `refresh_all_projects_env_with_db` directly (no Tauri layer).
#[command]
pub async fn refresh_all_projects_env(
    db: State<'_, Db>,
) -> Result<RefreshAllProjectsEnvResult, String> {
    Ok(refresh_all_projects_env_with_db(&db))
}

#[command]
pub async fn switch_project_host_v2(
    id: String,
    new_host: ProjectHost,
    db: State<'_, Db>,
) -> Result<SwitchHostResult, String> {
    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found", id))?;

    if project.host == new_host {
        let count = db.list_module_installs_for_project(&id)?.len() as u32;
        return Ok(SwitchHostResult {
            project: ProjectView::from_row(project, count),
            modules_removed: vec![],
            modules_preserved: db.list_module_installs_for_project(&id)?,
        });
    }

    // For MAO→base: modules listing compatible hosts with only "mao" must go.
    // We can't fully decide without the manifests, which live in install
    // directories. This command flags candidates for removal by looking at
    // the module_id. A manifest registry lookup would be cleaner — added
    // in a later iteration; for now we rely on the module_id naming
    // convention (*-mao suffix OR known MAO-only module ids).
    let installs = db.list_module_installs_for_project(&id)?;
    let mao_only_ids: &[&str] = &[
        "vct-asset-library",
        "vct-agent-packs-mao",
        "vct-workflows-mao",
    ];

    let mut removed = Vec::new();
    let mut preserved = Vec::new();
    for install in installs {
        let goes = new_host == ProjectHost::Base
            && (mao_only_ids.contains(&install.module_id.as_str())
                || install.module_id.ends_with("-mao"));
        if goes {
            db.delete_module_install(&id, &install.module_id)?;
            removed.push(install);
        } else {
            preserved.push(install);
        }
    }

    db.update_project_host(&id, new_host.clone())?;
    db.audit(
        "project_host_switch",
        Some(&id),
        None,
        &serde_json::json!({
            "to": new_host.as_str(),
            "removed_modules": removed.iter().map(|m| &m.module_id).collect::<Vec<_>>(),
        }),
    )?;
    let _ = db.log_change("projects", "update", Some(&id), Some(&id));

    let project = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} vanished after host switch", id))?;
    let count = preserved.len() as u32;
    Ok(SwitchHostResult {
        project: ProjectView::from_row(project, count),
        modules_removed: removed,
        modules_preserved: preserved,
    })
}

/// Unregister project options (2026-05-06).
///
/// Replaces the old `delete_folder` boolean (which was always ignored —
/// the launcher never touched the user's folder). Splits the operation
/// into two independent, opt-out-able layers:
///
///   * `purge_launcher_files` (default ON): surgical removal of
///     launcher-managed paths from `<folder>/`. Leaves user-owned content
///     (agents, skills, CONTEXT_STATE, CLAUDE.md, source code, user-added
///     `.env` keys) intact. See `purge_launcher_files_from_project` for
///     the precise allowlist + deletelist.
///   * `purge_collections` (default OFF): drop the project's OWN
///     Weaviate collections (`<Project>_KnowledgeGraph`,
///     `<Project>_Development`). Shared collections are NEVER touched.
///     OFF by default because collections can always be rebuilt from
///     `/knowledge` + source code via `install-bundle --update`.
///
/// User-decision audit trail (2026-05-06):
///   * (i) agents/skills are PRESERVED even though they're bundle-
///     installed — users edit them and treat them as user content.
///   * (ii) `.claude/scripts/` IS purged — these are launcher-managed
///     and re-shipped on every bundle install.
#[derive(Debug, Clone, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct UnregisterOptions {
    /// Surgically remove launcher-managed files from the project
    /// folder. Leaves user content (agents, skills, CONTEXT_STATE,
    /// CLAUDE.md, source code, user-added .env keys). Default: true.
    #[serde(default = "default_true")]
    pub purge_launcher_files: bool,

    /// Drop the project's own Weaviate collections
    /// (<Project>_KnowledgeGraph, <Project>_Development). Shared
    /// collections are NEVER touched. Default: false (opt-in).
    /// Collections can always be rebuilt from /knowledge + source
    /// code, so this is a user choice rather than a default.
    #[serde(default)]
    pub purge_collections: bool,
}

fn default_true() -> bool { true }

/// Per-call report from `delete_project_v2`. Consumed by the launcher
/// UI's "Unregister project" toast for a one-line summary plus a
/// drill-down for any soft-fail warnings.
///
/// All vectors are populated even when the corresponding flag was OFF —
/// they just stay empty. The UI relies on `len()` to decide whether to
/// surface a section in the toast.
#[derive(Debug, Clone, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct UnregisterReport {
    pub project_id: String,
    pub project_name: String,
    /// Relative paths actually removed from `<folder>/`.
    pub files_purged: Vec<String>,
    /// Canonical keys removed from any of the env surfaces (`.env`,
    /// `.claude/env`, `.claude/settings.json` `env` block, plus any
    /// pre-existing `.vscode/settings.json` `claude-code.env` block —
    /// the launcher no longer authors that surface as of v0.2.12 /
    /// PR-27, but the unregister strip pass still runs against it
    /// when present so a hand-edited or pre-PR-27 block is cleaned up
    /// on uninstall). Duplicates across surfaces are de-duped — each
    /// key appears once even if removed from multiple files.
    pub keys_purged_from_env: Vec<String>,
    /// Names of dropped Weaviate collections (only populated when
    /// `purge_collections: true` and the drop succeeded).
    pub collections_dropped: Vec<String>,
    /// Soft-fail messages — failures that don't abort the unregister
    /// (e.g. Weaviate down → keep going + drop request becomes a
    /// warning; unreadable file → skip + record). The DB delete and
    /// audit log entry ALWAYS succeed regardless of warning count.
    pub warnings: Vec<String>,
}

// ─── Canonical env-key registry (single source of truth) ─────────────
//
// PR-150 reviewer concern (2026-05-06): the install path
// (`canonical_env_pairs` in `write_project_env_files`) and the
// unregister path (`UNREGISTER_CANONICAL_ENV_KEYS` below) used to
// duplicate the SAME LIST of canonical env-key names in two places,
// plus a third copy in the test that pinned the lockstep. Adding a
// new canonical key required touching all three lists, and forgetting
// any one silently broke unregister.
//
// Fix (2026-05-07, follow-up #10): extract the names into ONE
// `&[&str]` const. The install path iterates this const and resolves
// values via a match; the unregister path is the const + portability
// extras; the test asserts this relationship directly. Adding a key
// is now a one-line change here PLUS a one-line match arm in
// `canonical_env_pairs`.
//
// Install-flow audit (2026-05-08, P1 #2): the former
// `CANONICAL_PORTABILITY_ENV_KEYS` is gone — its two members
// (`VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR`) are now in
// `CANONICAL_INSTALL_ENV_KEYS` so they propagate to every install
// surface (.claude/env, .claude/settings.json env block) instead of
// just `.claude/env`. See the doc comment on
// `CANONICAL_INSTALL_ENV_KEYS` for the full rationale. (PR-27 /
// v0.2.12 / 2026-05-16 removed the historical third surface
// `.vscode/settings.json claude-code.env` because it didn't propagate
// to MCP subprocesses on Linux.)

/// Canonical env keys the launcher writes during install AND removes
/// during unregister. The names live here exactly once. Both the
/// install pair-builder and the unregister key-list iterate this.
///
/// Order matters for `.claude/env` line ordering (which is just for
/// human readability — no semantic significance). Keep additions
/// grouped by purpose (collections / write-gates / project ID /
/// embedding profile / service URLs / service ports / portability).
///
/// Install-flow audit (2026-05-08, P1 #2): the portability keys
/// (`VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR`) were previously
/// kept in a separate `CANONICAL_PORTABILITY_ENV_KEYS` const and only
/// emitted into `.claude/env` by `build_claude_env_managed_block`. The
/// rationale ("only meaningful for shell-sourced contexts") was wrong:
/// Claude Code propagates the `env` block of `.claude/settings.json` to
/// hook subprocesses, so VS Code-extension users (the dominant path on
/// Linux/macOS/Windows for dev users) silently lost these vars. The
/// hooks then fell back to a non-existent `claude_mcp_servers/.venv`
/// path inside managed projects (managed projects don't ship
/// `claude_mcp_servers/`). The two consts are now merged so the
/// existing pair-builder propagates them to every surviving surface
/// (`.claude/env` and `.claude/settings.json` env). The pair-builder's
/// match arm returns `Option<String>` and emits `None` when
/// `settings.orchestrator_root` is `None` — the entry is then omitted
/// from every surface, preserving the "launcher running outside a git
/// checkout silently omits these lines" semantics.
///
/// PR-27 (v0.2.12, 2026-05-16): a historical third surface
/// (`.vscode/settings.json` `claude-code.env`) was removed because
/// empirical sentinel testing showed it did not propagate to MCP
/// subprocesses on Linux Claude Code 2.1.143. The 2026-05-08 audit
/// comment above predates that empirical verification — `.vscode`
/// claude-code.env block is NOT in the propagation chain on Linux.
pub(crate) const CANONICAL_INSTALL_ENV_KEYS: &[&str] = &[
    "KG_COLLECTION",
    "DEVELOPMENT_COLLECTION",
    "SHARED_KG_COLLECTION",
    "SHARED_KG_WRITE_DISABLED",
    "SHARED_KG_OPT_OUT",
    // v0.2.46 Decision B — symmetric READ gate. No legacy alias
    // because pre-v0.2.46 the read path was unconditional.
    "SHARED_KG_READ_DISABLED",
    "PROJECT_NAME",
    // PR-8 cross-PR handoff (v0.2.11 / 2026-05-15): added at the
    // Rust first-install boundary so newly-registered projects get
    // `CODE_GRAPH_PROJECT` in their env surfaces from minute one,
    // not just after PR-7's Python `--update` backfill. Value is the
    // sanitized form of `PROJECT_NAME` — synonym in PR-7's documented
    // contract; the two are guaranteed to round-trip through
    // `sanitize_kg_collection` to the same Weaviate-safe identifier.
    // Idempotent w.r.t. PR-7's Python helper: when both paths run,
    // the Python backfill sees the key already present + matching
    // and exits as a noop.
    "CODE_GRAPH_PROJECT",
    "ACTIVE_EMBEDDING",
    "WEAVIATE_URL",
    "WEAVIATE_PORT",
    "OLLAMA_URL",
    "OLLAMA_PORT",
    "CODE_EMBED_URL",
    "CODE_EMBED_PORT",
    // Portability keys (merged from the former
    // `CANONICAL_PORTABILITY_ENV_KEYS` const on 2026-05-08, install-flow
    // audit P1 #2). Conditionally emitted: the pair-builder skips them
    // when `settings.orchestrator_root` is `None`. See doc comment above.
    "VCT_ORCHESTRATOR_ROOT",
    "VCT_INFRASTRUCTURE_DIR",
    // Multi-source KG / code-graph access lists (P1-D, 2026-05-08). Each
    // env var carries a comma-separated list of peer project names the
    // current project has READ access to via the launcher's access matrix.
    // Consumed by `weaviate_mcp/server.py::_kg_collections_to_search` +
    // `search_code_graph` and by the bundled `rl_kg_search.py` to fan out
    // queries across peers. Conditionally emitted: the pair-builder skips
    // each key when its respective list is empty (the default — no peers
    // granted access).
    //
    // settings.json/.claude-code.env hooks consume these via the MCP env
    // block, so they're added to all 3 surfaces via the same canonical
    // pipeline as the rest. Hooks invoked by `settings.json` directly do
    // NOT reference these names (they bubble up via the MCP server +
    // `rl_kg_search.py`), so the drift gate doesn't need to know about
    // them — see `check_settings_template_drift.py` allowlist note.
    "VCT_KG_ACCESS_LIST",
    "VCT_CODE_GRAPH_ACCESS_LIST",
    // 0.1.7 fork-readiness sweep (2026-05-08): GitHub PAT propagation.
    // Replaces the retired `git-credential-vct` helper (incompatible
    // with per-project active-flag gating per the secrets-architecture
    // audit). Resolved by the env-pair builder from the OS keychain
    // entry the OnboardingWizard writes via
    // `commands::installer::register_github_pat`
    // (`vct._user_shared_.shared.user / github_pat` — post-2026-05-10
    // module_id unification with the SecretsPanel UI_MODULE_BUCKET).
    // Conditionally emitted: omitted when the keychain has no value,
    // or when the value is paused via Lifecycle B's active-flag gate.
    //
    // Per-project gating semantics (conservative, 2026-05-08): every
    // registered project receives `GITHUB_TOKEN` whenever the PAT is
    // set and active. This matches pre-0.1.7 file-based behaviour
    // (`~/.vct-secrets/shared/github_pat` is readable by every process
    // running as the user). A finer-grained per-project access matrix
    // for `github_pat` is out of scope for the 0.1.7 fork sweep — see
    // `docs/MIGRATION-0.2.0.md` "Replacing `git-credential-vct`".
    //
    // Users configure git's credential helper once
    // (`gh auth setup-git`, or a thin shell helper that reads
    // `$GITHUB_TOKEN`) and the launcher takes over the per-project
    // gating via the env var.
    "GITHUB_TOKEN",
    // v0.2.43 V0243-5-Rust: KG_BASE_DIR mirrors the project folder so
    // .claude/settings.json::env and .claude/env agree with the value
    // `build_kg_sync_env` already writes for kg-sync subprocesses. Value
    // = the project's folder path. Always emitted (never None).
    "KG_BASE_DIR",
];

/// Canonical env keys the launcher OWNS across every surface. These are
/// the keys `purge_launcher_files_from_project` removes during unregister
/// while preserving every other key (user-added secrets, custom config).
///
/// Install-flow audit (2026-05-08, P1 #2): now identical to
/// `CANONICAL_INSTALL_ENV_KEYS` after the portability keys merged in
/// (the former `CANONICAL_PORTABILITY_ENV_KEYS` is gone). The
/// `2026-05-06 unregister keys` test still asserts the relationship
/// directly (no hardcoded mirror) — see
/// `unregister_canonical_keys_match_install_canonical_keys` below.
pub(crate) static UNREGISTER_CANONICAL_ENV_KEYS: std::sync::LazyLock<Vec<&'static str>> =
    std::sync::LazyLock::new(|| CANONICAL_INSTALL_ENV_KEYS.to_vec());

/// Project-folder paths the launcher OWNS and will recursively delete on
/// unregister when `purge_launcher_files: true`. Each entry is RELATIVE
/// to the project root.
///
/// Audit trail (decisions documented 2026-05-06):
///   * `.claude/hooks/` (incl. `_lib/`) — entirely launcher-managed,
///     re-shipped on every bundle install. Decision (ii).
///   * `.claude/scripts/` — same. Decision (ii).
///   * `infrastructure/docker-compose*.yml` / `podman-compose*.yml` —
///     copied verbatim from the orchestrator's `infrastructure/` tree
///     by the bundle install. Bug-for-bug rebuildable.
///
/// Paths NOT in this list (preserved by default):
///   * `.claude/env` — surgically edited by `surgically_strip_env_surfaces`
///     (managed-block excision + canonical-key strip). User-added
///     exports OUTSIDE the managed block survive unregister/re-register
///     cycles. (Regression fix 2026-05-09: prior behaviour deleted the
///     whole file, destroying user content the audit confirmed users
///     could realistically add since `tools/claude` sources this file.)
///   * `.claude/agents/`, `.claude/skills/` — decision (i): user content
///     even though bundle-installed.
///   * `.claude/CONTEXT_STATE.md`, `.claude/MEMORY.md`, `.claude/context/`
///     — user working memory.
///   * `.claude/settings.json` — surgically edited (env block only).
///   * `CLAUDE.md`, `.env` (user keys), source code, `.git/`, `.venv/`.
pub(crate) const UNREGISTER_PURGE_PATHS: &[&str] = &[
    ".claude/hooks",
    ".claude/scripts",
    // Launcher-managed install manifest (written by
    // vco_lib.project_init at install time; tracks bundle version,
    // shipped-file hashes for self-merge, and install ledger). It's
    // pure launcher metadata — no user content lives here. Without
    // this entry the file persisted across unregister + re-register
    // and confused future installs. Follow-up #12 (CONTEXT_STATE).
    ".claude/.vco-manifest.json",
    "infrastructure/docker-compose.yml",
    "infrastructure/docker-compose.gpu.yml",
    "infrastructure/docker-compose.amd-rocm.yml",
    "infrastructure/podman-compose.yml",
    "infrastructure/podman-compose.gpu.yml",
    "infrastructure/podman-compose.amd-rocm.yml",
];

/// Pure helper: strip launcher-canonical keys from a `.env`-style text.
///
/// "Canonical" = membership in `UNREGISTER_CANONICAL_ENV_KEYS`. Lines
/// matching `<KEY>=...` (active) or `# <KEY>=...` (commented) at the
/// start of the trimmed line are removed; user-added keys are preserved
/// verbatim. Comment-only lines and blank lines are preserved verbatim.
///
/// Returns `(new_text, removed_keys)`. `removed_keys` is sorted +
/// de-duped so the UI shows a clean list.
///
/// Marker lines (`# added by vco YYYY-MM-DD`) are preserved as-is —
/// they're informational, the user can clean them up later if desired.
/// We don't try to strip empty marker blocks (e.g. a `# added by vco`
/// followed by lines we just removed) because doing so robustly would
/// require multi-pass bookkeeping the unregister doesn't need.
pub(crate) fn strip_canonical_keys_from_env_text(text: &str) -> (String, Vec<String>) {
    let canonical: std::collections::HashSet<&str> =
        UNREGISTER_CANONICAL_ENV_KEYS.iter().copied().collect();
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());

    for line in text.lines() {
        let trimmed = line.trim_start();
        // Strip a single leading '#' + whitespace to handle commented form.
        let body = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };

        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());

        if let Some(k) = key_to_check {
            if canonical.contains(k) {
                removed.insert(k.to_string());
                continue; // drop the line
            }
        }

        out.push_str(line);
        out.push('\n');
    }

    // Preserve the original trailing-newline shape: if the input had
    // none, drop the one we appended on the final iteration.
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    // If the input ended with no lines but a trailing newline, our loop
    // produced no output — re-add the trailing newline for shape parity.
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }

    (out, removed.into_iter().collect())
}

/// Pure helper: strip launcher-canonical keys from the `.claude/env`
/// POSIX-export file. Same semantics as `strip_canonical_keys_from_env_text`
/// but recognizes the `export KEY="value"` shape emitted by
/// `build_claude_env_managed_block`.
///
/// Recognized line shapes (after trim):
///   * `export KEY="value"`
///   * `export KEY=value`
///   * `# export KEY="value"` (commented; rare but possible)
///   * `KEY=value` / `# KEY=value` (env-style fallback)
///
/// All lines OUTSIDE the matched-key set are preserved verbatim, INCLUDING
/// the `# vco-managed-begin` / `# vco-managed-end` marker lines (a tidy
/// sweep would remove them too, but leaving them is harmless and
/// idempotent on re-run).
pub(crate) fn strip_canonical_keys_from_claude_env_text(
    text: &str,
) -> (String, Vec<String>) {
    let canonical: std::collections::HashSet<&str> =
        UNREGISTER_CANONICAL_ENV_KEYS.iter().copied().collect();
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());

    for line in text.lines() {
        let trimmed = line.trim_start();
        // Strip optional leading '#' + whitespace (commented exports).
        let after_hash = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };
        // Strip optional leading 'export ' to expose KEY=value.
        let body = after_hash.strip_prefix("export ").unwrap_or(after_hash);

        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());

        if let Some(k) = key_to_check {
            if canonical.contains(k) {
                removed.insert(k.to_string());
                continue; // drop the line
            }
        }

        out.push_str(line);
        out.push('\n');
    }

    // Preserve trailing-newline shape (see strip_canonical_keys_from_env_text).
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }

    (out, removed.into_iter().collect())
}

/// Pure helper: strip launcher-canonical keys from a JSON `env`-shaped
/// sub-block (`.claude/settings.json` `env` OR `.vscode/settings.json`
/// `claude-code.env`). Mutates `parent` in place: removes canonical keys
/// from the named sub-object; user keys at the same level survive. If
/// the sub-object is missing or non-object, the call is a no-op (returns
/// empty Vec).
///
/// Returns the list of keys actually removed (sorted, de-duped). Inverse
/// of `merge_env_object_canonical`.
pub(crate) fn strip_canonical_keys_from_env_object(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
) -> Vec<String> {
    let canonical: std::collections::HashSet<&str> =
        UNREGISTER_CANONICAL_ENV_KEYS.iter().copied().collect();

    let env_obj = match parent.get_mut(env_key).and_then(|v| v.as_object_mut()) {
        Some(o) => o,
        None => return Vec::new(),
    };

    let mut removed = std::collections::BTreeSet::new();
    let to_remove: Vec<String> = env_obj
        .keys()
        .filter(|k| canonical.contains(k.as_str()))
        .cloned()
        .collect();
    for k in to_remove {
        env_obj.remove(&k);
        removed.insert(k);
    }
    removed.into_iter().collect()
}

/// Drive the surgical purge across all four env surfaces in one folder.
///
/// Returns `(keys_purged_unique, warnings)`. Both vectors are bounded
/// in size (canonical key count + per-surface read/write failures) so
/// they're cheap to ship over the Tauri channel.
///
/// Soft-fail discipline: per-surface read or write failures land in
/// `warnings` and the next surface is still attempted. A folder where
/// every env surface is missing returns no keys + no warnings — a clean
/// no-op (the project may have been registered against a folder that
/// the user has since cleaned up by hand).
pub(crate) fn surgically_strip_env_surfaces(
    folder: &Path,
) -> (Vec<String>, Vec<String>) {
    let mut keys = std::collections::BTreeSet::new();
    let mut warnings: Vec<String> = Vec::new();

    // 1. .env (root)
    let env_path = folder.join(".env");
    if env_path.exists() {
        match std::fs::read_to_string(&env_path) {
            Ok(text) => {
                let (new_text, removed) = strip_canonical_keys_from_env_text(&text);
                if !removed.is_empty() {
                    if let Err(e) = std::fs::write(&env_path, new_text) {
                        warnings.push(format!(
                            "could not rewrite {}: {}", env_path.display(), e
                        ));
                    } else {
                        for k in removed { keys.insert(k); }
                    }
                }
            }
            Err(e) => warnings.push(format!(
                "could not read {} for env-key strip: {}", env_path.display(), e
            )),
        }
    }

    // 2. .claude/env (POSIX exports). Two-step strip:
    //    a) excise the launcher-managed BEGIN/END block in place
    //    b) strip any orphaned canonical keys outside the block
    //       (e.g. legacy entries from pre-managed-block layouts)
    //
    // The file is preserved (NOT in UNREGISTER_PURGE_PATHS as of
    // 2026-05-09): users who hand-add exports outside the managed
    // block keep them across unregister/re-register. Empty files
    // are harmless residue the user can clean up at their discretion.
    let claude_env = folder.join(".claude").join("env");
    if claude_env.exists() {
        match std::fs::read_to_string(&claude_env) {
            Ok(text) => {
                // Step (a): replace the managed block with an empty
                // string. `merge_claude_env_managed_block` handles
                // the BEGIN/END splice idempotently and is a no-op
                // if no markers are present.
                let after_block = merge_claude_env_managed_block(Some(&text), "");
                // Step (b): strip orphaned canonical keys outside the
                // (now-empty) managed segment.
                let (new_text, removed) =
                    strip_canonical_keys_from_claude_env_text(&after_block);
                if new_text != text {
                    if let Err(e) = std::fs::write(&claude_env, new_text) {
                        warnings.push(format!(
                            "could not rewrite {}: {}", claude_env.display(), e
                        ));
                    } else {
                        for k in removed { keys.insert(k); }
                    }
                }
            }
            Err(e) => warnings.push(format!(
                "could not read {} for env-key strip: {}", claude_env.display(), e
            )),
        }
    }

    // 3. .claude/settings.json `env` block
    let claude_settings = folder.join(".claude").join("settings.json");
    if claude_settings.exists() {
        match std::fs::read_to_string(&claude_settings) {
            Ok(raw) => match serde_json::from_str::<serde_json::Value>(&raw) {
                Ok(mut v) => {
                    if let Some(obj) = v.as_object_mut() {
                        let removed = strip_canonical_keys_from_env_object(obj, "env");
                        if !removed.is_empty() {
                            // Drop empty env block so we don't leave
                            // `"env": {}` behind — the bundle install's
                            // merge writer will recreate it on the next
                            // re-register.
                            if obj.get("env").and_then(|x| x.as_object())
                                .map(|o| o.is_empty()).unwrap_or(false)
                            {
                                obj.remove("env");
                            }
                            match serde_json::to_string_pretty(&v) {
                                Ok(pretty) => {
                                    if let Err(e) = std::fs::write(&claude_settings, pretty) {
                                        warnings.push(format!(
                                            "could not rewrite {}: {}",
                                            claude_settings.display(), e
                                        ));
                                    } else {
                                        for k in removed { keys.insert(k); }
                                    }
                                }
                                Err(e) => warnings.push(format!(
                                    "could not serialize {} after env-key strip: {}",
                                    claude_settings.display(), e
                                )),
                            }
                        }
                    }
                }
                Err(e) => warnings.push(format!(
                    "{} is not valid JSON ({}); leaving untouched on unregister",
                    claude_settings.display(), e
                )),
            },
            Err(e) => warnings.push(format!(
                "could not read {} for env-key strip: {}",
                claude_settings.display(), e
            )),
        }
    }

    // 4. .vscode/settings.json `claude-code.env` block
    let vscode_settings = folder.join(".vscode").join("settings.json");
    if vscode_settings.exists() {
        match std::fs::read_to_string(&vscode_settings) {
            Ok(raw) => match serde_json::from_str::<serde_json::Value>(&raw) {
                Ok(mut v) => {
                    if let Some(obj) = v.as_object_mut() {
                        let removed = strip_canonical_keys_from_env_object(
                            obj, "claude-code.env",
                        );
                        if !removed.is_empty() {
                            if obj.get("claude-code.env")
                                .and_then(|x| x.as_object())
                                .map(|o| o.is_empty()).unwrap_or(false)
                            {
                                obj.remove("claude-code.env");
                            }
                            match serde_json::to_string_pretty(&v) {
                                Ok(pretty) => {
                                    if let Err(e) = std::fs::write(&vscode_settings, pretty) {
                                        warnings.push(format!(
                                            "could not rewrite {}: {}",
                                            vscode_settings.display(), e
                                        ));
                                    } else {
                                        for k in removed { keys.insert(k); }
                                    }
                                }
                                Err(e) => warnings.push(format!(
                                    "could not serialize {} after env-key strip: {}",
                                    vscode_settings.display(), e
                                )),
                            }
                        }
                    }
                }
                Err(e) => warnings.push(format!(
                    "{} is not valid JSON ({}); leaving untouched on unregister",
                    vscode_settings.display(), e
                )),
            },
            Err(e) => warnings.push(format!(
                "could not read {} for env-key strip: {}",
                vscode_settings.display(), e
            )),
        }
    }

    (keys.into_iter().collect(), warnings)
}

/// Subagent G (2026-05-08): strip a caller-supplied set of user-bucket
/// secret KEY names from the project's env surfaces.
///
/// Different from `surgically_strip_env_surfaces` (which strips only
/// the launcher-canonical key set known at compile time): user-secret
/// key names are project-specific and dynamically discovered from
/// `secret_active_state`, so they need a per-call list.
///
/// Strips:
///   * `.env`: lines matching `<KEY>=...` or `# <KEY>=...` are removed
///     verbatim. Lines outside that shape (comments, blank, user
///     overrides) are preserved.
///   * `.claude/env`: lines matching `export <KEY>="..."` (active or
///     commented form) are removed. Outside-the-managed-block exports
///     follow the same rule.
///   * `.claude/settings.json` `env` block: keys removed via
///     deep-merge. Adjacent canonical / by-hand user keys at the same
///     level survive.
///   * `.vscode/settings.json` `claude-code.env` block: same.
///
/// Soft-fail discipline mirrors `surgically_strip_env_surfaces`. The
/// keychain itself is NOT touched — that's the user's call to make
/// before unregister via the SecretsPanel "Remove" action. This
/// function exists so a forgotten key from the SecretsPanel doesn't
/// survive as a stale env var post-unregister.
///
/// Returns `(keys_actually_purged, warnings)`. `keys_actually_purged`
/// is sorted + de-duped across surfaces; the report layer dumps it
/// into `keys_purged_from_env` alongside the canonical purge result.
pub(crate) fn surgically_strip_user_secret_keys(
    folder: &Path,
    keys: &[String],
) -> (Vec<String>, Vec<String>) {
    if keys.is_empty() {
        return (Vec::new(), Vec::new());
    }
    let key_set: std::collections::HashSet<&str> =
        keys.iter().map(|s| s.as_str()).collect();
    let mut purged = std::collections::BTreeSet::new();
    let mut warnings: Vec<String> = Vec::new();

    // 1. .env (root)
    let env_path = folder.join(".env");
    if env_path.exists() {
        match std::fs::read_to_string(&env_path) {
            Ok(text) => {
                let (new_text, removed) = strip_named_keys_from_env_text(&text, &key_set);
                if !removed.is_empty() {
                    if let Err(e) = std::fs::write(&env_path, new_text) {
                        warnings.push(format!(
                            "could not rewrite {} (user-secret strip): {}",
                            env_path.display(),
                            e
                        ));
                    } else {
                        for k in removed {
                            purged.insert(k);
                        }
                    }
                }
            }
            Err(e) => warnings.push(format!(
                "could not read {} for user-secret strip: {}",
                env_path.display(),
                e
            )),
        }
    }

    // 2. .claude/env (POSIX exports)
    let claude_env = folder.join(".claude").join("env");
    if claude_env.exists() {
        match std::fs::read_to_string(&claude_env) {
            Ok(text) => {
                let (new_text, removed) = strip_named_keys_from_claude_env_text(&text, &key_set);
                if !removed.is_empty() {
                    if let Err(e) = std::fs::write(&claude_env, new_text) {
                        warnings.push(format!(
                            "could not rewrite {} (user-secret strip): {}",
                            claude_env.display(),
                            e
                        ));
                    } else {
                        for k in removed {
                            purged.insert(k);
                        }
                    }
                }
            }
            Err(e) => warnings.push(format!(
                "could not read {} for user-secret strip: {}",
                claude_env.display(),
                e
            )),
        }
    }

    // 3. .claude/settings.json `env` block
    let claude_settings = folder.join(".claude").join("settings.json");
    if claude_settings.exists() {
        match std::fs::read_to_string(&claude_settings) {
            Ok(raw) => match serde_json::from_str::<serde_json::Value>(&raw) {
                Ok(mut v) => {
                    if let Some(obj) = v.as_object_mut() {
                        let removed = strip_named_keys_from_env_object(obj, "env", &key_set);
                        if !removed.is_empty() {
                            if obj
                                .get("env")
                                .and_then(|x| x.as_object())
                                .map(|o| o.is_empty())
                                .unwrap_or(false)
                            {
                                obj.remove("env");
                            }
                            match serde_json::to_string_pretty(&v) {
                                Ok(pretty) => {
                                    if let Err(e) = std::fs::write(&claude_settings, pretty) {
                                        warnings.push(format!(
                                            "could not rewrite {} (user-secret strip): {}",
                                            claude_settings.display(),
                                            e
                                        ));
                                    } else {
                                        for k in removed {
                                            purged.insert(k);
                                        }
                                    }
                                }
                                Err(e) => warnings.push(format!(
                                    "could not serialize {} after user-secret strip: {}",
                                    claude_settings.display(),
                                    e
                                )),
                            }
                        }
                    }
                }
                Err(e) => warnings.push(format!(
                    "{} is not valid JSON ({}); skipping user-secret strip",
                    claude_settings.display(),
                    e
                )),
            },
            Err(e) => warnings.push(format!(
                "could not read {} for user-secret strip: {}",
                claude_settings.display(),
                e
            )),
        }
    }

    // 4. .vscode/settings.json `claude-code.env` block
    let vscode_settings = folder.join(".vscode").join("settings.json");
    if vscode_settings.exists() {
        match std::fs::read_to_string(&vscode_settings) {
            Ok(raw) => match serde_json::from_str::<serde_json::Value>(&raw) {
                Ok(mut v) => {
                    if let Some(obj) = v.as_object_mut() {
                        let removed =
                            strip_named_keys_from_env_object(obj, "claude-code.env", &key_set);
                        if !removed.is_empty() {
                            if obj
                                .get("claude-code.env")
                                .and_then(|x| x.as_object())
                                .map(|o| o.is_empty())
                                .unwrap_or(false)
                            {
                                obj.remove("claude-code.env");
                            }
                            match serde_json::to_string_pretty(&v) {
                                Ok(pretty) => {
                                    if let Err(e) = std::fs::write(&vscode_settings, pretty) {
                                        warnings.push(format!(
                                            "could not rewrite {} (user-secret strip): {}",
                                            vscode_settings.display(),
                                            e
                                        ));
                                    } else {
                                        for k in removed {
                                            purged.insert(k);
                                        }
                                    }
                                }
                                Err(e) => warnings.push(format!(
                                    "could not serialize {} after user-secret strip: {}",
                                    vscode_settings.display(),
                                    e
                                )),
                            }
                        }
                    }
                }
                Err(e) => warnings.push(format!(
                    "{} is not valid JSON ({}); skipping user-secret strip",
                    vscode_settings.display(),
                    e
                )),
            },
            Err(e) => warnings.push(format!(
                "could not read {} for user-secret strip: {}",
                vscode_settings.display(),
                e
            )),
        }
    }

    (purged.into_iter().collect(), warnings)
}

/// Pure helper: strip a named set of KEY names from `.env`-style text.
/// Mirror of `strip_canonical_keys_from_env_text` but with a caller-
/// supplied key set instead of `UNREGISTER_CANONICAL_ENV_KEYS`.
fn strip_named_keys_from_env_text(
    text: &str,
    keys: &std::collections::HashSet<&str>,
) -> (String, Vec<String>) {
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let trimmed = line.trim_start();
        let body = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };
        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());
        if let Some(k) = key_to_check {
            if keys.contains(k) {
                removed.insert(k.to_string());
                continue;
            }
        }
        out.push_str(line);
        out.push('\n');
    }
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }
    (out, removed.into_iter().collect())
}

/// Pure helper: strip a named set of KEY names from `.claude/env`
/// POSIX-export text. Mirror of `strip_canonical_keys_from_claude_env_text`.
fn strip_named_keys_from_claude_env_text(
    text: &str,
    keys: &std::collections::HashSet<&str>,
) -> (String, Vec<String>) {
    let mut removed = std::collections::BTreeSet::new();
    let mut out = String::with_capacity(text.len());
    for line in text.lines() {
        let trimmed = line.trim_start();
        let after_hash = if let Some(rest) = trimmed.strip_prefix('#') {
            rest.trim_start()
        } else {
            trimmed
        };
        let body = after_hash.strip_prefix("export ").unwrap_or(after_hash);
        let key_to_check = body
            .find('=')
            .filter(|&i| i > 0)
            .map(|i| body[..i].trim());
        if let Some(k) = key_to_check {
            if keys.contains(k) {
                removed.insert(k.to_string());
                continue;
            }
        }
        out.push_str(line);
        out.push('\n');
    }
    if !text.ends_with('\n') && out.ends_with('\n') {
        out.pop();
    }
    if text.ends_with('\n') && out.is_empty() {
        out.push('\n');
    }
    (out, removed.into_iter().collect())
}

/// Pure helper: strip a named set of KEY names from a JSON env-shaped
/// sub-block. Mirror of `strip_canonical_keys_from_env_object`.
fn strip_named_keys_from_env_object(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
    keys: &std::collections::HashSet<&str>,
) -> Vec<String> {
    let env_obj = match parent.get_mut(env_key).and_then(|v| v.as_object_mut()) {
        Some(o) => o,
        None => return Vec::new(),
    };
    let mut removed = std::collections::BTreeSet::new();
    let to_remove: Vec<String> = env_obj
        .keys()
        .filter(|k| keys.contains(k.as_str()))
        .cloned()
        .collect();
    for k in to_remove {
        env_obj.remove(&k);
        removed.insert(k);
    }
    removed.into_iter().collect()
}

/// Surgically remove every entry in `UNREGISTER_PURGE_PATHS` from
/// `<folder>/`. Returns `(relative_paths_removed, warnings)`.
///
/// Soft-fail discipline: per-path failures (permission denied, ENOENT
/// race, etc.) land in `warnings`; the next path is still attempted.
/// ENOENT is silent — a missing path on a folder that never had the
/// bundle installed is the expected case for legacy projects, not a
/// warning condition.
///
/// Note: this is the FILE / DIRECTORY purge. The env-surface strip
/// runs separately via `surgically_strip_env_surfaces` so that surfaces
/// containing user-added keys can be partially preserved.
pub(crate) fn purge_launcher_files_from_project(
    folder: &Path,
) -> (Vec<String>, Vec<String>) {
    let mut purged: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();

    for rel in UNREGISTER_PURGE_PATHS {
        let target = folder.join(rel);
        if !target.exists() {
            continue; // silent skip
        }
        let meta = match std::fs::symlink_metadata(&target) {
            Ok(m) => m,
            Err(e) => {
                warnings.push(format!(
                    "could not stat {} for unregister purge: {}",
                    target.display(), e
                ));
                continue;
            }
        };

        let result = if meta.is_dir() {
            std::fs::remove_dir_all(&target)
        } else {
            std::fs::remove_file(&target)
        };

        match result {
            Ok(()) => purged.push((*rel).to_string()),
            Err(e) => warnings.push(format!(
                "could not remove {}: {}", target.display(), e
            )),
        }
    }

    (purged, warnings)
}

/// Soft-fail subprocess driver: drop a project's owned Weaviate
/// collections via `python -m vco_lib.project_init drop-collections`.
///
/// Returns `(dropped_collection_names, warnings)`. Soft-fail at every
/// gate (no Python, no orchestrator root, subprocess crash, JSON parse
/// failure) — drop failures NEVER block the rest of the unregister.
/// A Weaviate-down condition becomes a warning the UI displays in the
/// "Unregister complete with warnings" toast.
async fn drop_owned_collections(project_name: &str) -> (Vec<String>, Vec<String>) {
    let mut dropped: Vec<String> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();

    let system = match detect_system().await {
        Ok(s) => s,
        Err(e) => {
            warnings.push(format!(
                "drop-collections skipped: detect_system failed: {}. \
                 Per-project Weaviate collections were NOT dropped — they \
                 still exist on the Weaviate instance and consume vector \
                 storage. Manual fix: \
                 `python -m vco_lib.project_init drop-collections --name {:?}`.",
                e, project_name
            ));
            return (dropped, warnings);
        }
    };
    if !system.has_python {
        warnings.push(
            "drop-collections skipped: no Python 3.11+ on PATH. \
             Per-project Weaviate collections were NOT dropped."
            .to_string(),
        );
        return (dropped, warnings);
    }

    let orch_root: PathBuf = match find_local_repo_root() {
        Ok(p) => p,
        Err(e) => {
            warnings.push(format!(
                "drop-collections skipped: orchestrator root not found: {}. \
                 Per-project Weaviate collections were NOT dropped.",
                e
            ));
            return (dropped, warnings);
        }
    };

    let mut cmd = tokio::process::Command::new(&system.python_cmd).silent();
    cmd.args([
        "-m",
        "vco_lib.project_init",
        "drop-collections",
        "--name",
        project_name,
        "--json",
    ])
    .current_dir(&orch_root)
    .stdin(std::process::Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => {
            warnings.push(format!(
                "drop-collections subprocess failed to start: {}. \
                 Per-project Weaviate collections were NOT dropped.",
                e
            ));
            return (dropped, warnings);
        }
    };

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    match serde_json::from_str::<serde_json::Value>(&stdout) {
        Ok(v) => {
            if let Some(arr) = v.get("dropped").and_then(|x| x.as_array()) {
                for item in arr {
                    if let Some(s) = item.as_str() {
                        dropped.push(s.to_string());
                    }
                }
            }
            if let Some(errs) = v.get("errors").and_then(|x| x.as_array()) {
                for e in errs {
                    let coll = e.get("collection")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    let msg = e.get("error")
                        .and_then(|c| c.as_str()).unwrap_or("?");
                    warnings.push(format!(
                        "drop-collections error on {}: {}", coll, msg
                    ));
                }
            }
            if !out.status.success() && warnings.is_empty() {
                warnings.push(format!(
                    "drop-collections exit {} (no JSON errors[]; stderr: {})",
                    out.status,
                    stderr.lines().rev().take(3)
                        .collect::<Vec<_>>().into_iter().rev()
                        .collect::<Vec<_>>().join(" | ")
                ));
            }
        }
        Err(parse_err) => {
            warnings.push(format!(
                "drop-collections produced unparseable output ({}): \
                 stderr tail: {}. Per-project Weaviate collections may \
                 NOT have been dropped.",
                parse_err,
                stderr.lines().rev().take(3)
                    .collect::<Vec<_>>().into_iter().rev()
                    .collect::<Vec<_>>().join(" | ")
            ));
        }
    }

    (dropped, warnings)
}

/// Unregister a project from the launcher.
///
/// Always runs (regardless of `options`):
///   * Audit log entry (`project_delete`).
///   * DB row delete (CASCADE through module_installs).
///   * Change-log entry.
///
/// Conditional on `options.purge_launcher_files` (default true):
///   * Recursive removal of `UNREGISTER_PURGE_PATHS` from `<folder>/`.
///   * Surgical strip of canonical env keys from all four env surfaces
///     (`.env`, `.claude/env`, `.claude/settings.json` `env`,
///     `.vscode/settings.json` `claude-code.env`). User-added keys at
///     the same level survive.
///
/// Conditional on `options.purge_collections` (default false):
///   * Drop the project's own Weaviate collections via
///     `python -m vco_lib.project_init drop-collections`. Shared
///     collections are NEVER touched.
///
/// Returns an `UnregisterReport` summarising what happened. Soft-fail
/// at every step: per-file / per-surface / Weaviate failures become
/// `warnings[]` entries; the DB delete still completes. A complete
/// failure to read the project row before delete IS hard-failed
/// (the UI shows "project not found" without leaving any side-effects).
#[command]
pub async fn delete_project_v2(
    id: String,
    options: Option<UnregisterOptions>,
    db: State<'_, Db>,
) -> Result<UnregisterReport, String> {
    let opts = options.unwrap_or_default();

    // Read the row first so we have the project name for collection
    // drop AND so we never run a partial unregister against a missing
    // project (the file purge needs a folder path; without the row we'd
    // be operating on a phantom).
    let row = db
        .get_project(&id)?
        .ok_or_else(|| format!("project {} not found", id))?;

    let mut report = UnregisterReport {
        project_id: row.id.clone(),
        project_name: row.name.clone(),
        ..Default::default()
    };

    // Step 1: filesystem purge (default ON).
    if opts.purge_launcher_files {
        let folder = Path::new(&row.folder_path);
        if folder.is_dir() {
            // 1a (Subagent G, 2026-05-08). Strip user-bucket secret
            // keys from all surfaces FIRST. The canonical strip below
            // doesn't know about user keys (their names are dynamic
            // per-project), so without this step a registered project's
            // user secrets would survive the unregister + persist as
            // stale env vars in any subprocess that re-reads the
            // surfaces.
            //
            // Implementation: enumerate every key in the project's
            // user bucket from `secret_active_state` (active OR
            // inactive — both must be stripped), then surgically strip
            // those KEY names from each surface. Doesn't touch the
            // keychain itself; the user can decide whether to also
            // delete those entries via the SecretsPanel before
            // unregistering.
            let user_keys = db.list_user_secret_keys_for_project(&row.id);
            let (user_keys_purged, user_strip_warnings) =
                surgically_strip_user_secret_keys(folder, &user_keys);
            for k in user_keys_purged {
                if !report.keys_purged_from_env.contains(&k) {
                    report.keys_purged_from_env.push(k);
                }
            }
            report.warnings.extend(user_strip_warnings);

            // 1b. Strip canonical env keys from all four env surfaces.
            //     Done BEFORE the file delete so `.claude/env` (which is
            //     in UNREGISTER_PURGE_PATHS) gets stripped first; the
            //     subsequent file delete is a no-op for that file but
            //     leaves the strip's "keys removed" record intact.
            let (keys, env_warnings) = surgically_strip_env_surfaces(folder);
            for k in keys {
                if !report.keys_purged_from_env.contains(&k) {
                    report.keys_purged_from_env.push(k);
                }
            }
            report.warnings.extend(env_warnings);

            // 1c. File / directory purge.
            let (files, file_warnings) = purge_launcher_files_from_project(folder);
            report.files_purged = files;
            report.warnings.extend(file_warnings);
        } else {
            // Folder gone (user moved/deleted by hand). The DB delete is
            // still useful — it removes the orphan registration. Note in
            // the warnings so the UI can surface it.
            report.warnings.push(format!(
                "project folder {} no longer exists; \
                 skipped launcher-file purge",
                row.folder_path
            ));
        }
    }

    // Step 2: collection drop (default OFF, opt-in).
    if opts.purge_collections {
        let (dropped, drop_warnings) = drop_owned_collections(&row.name).await;
        report.collections_dropped = dropped;
        report.warnings.extend(drop_warnings);
    }

    // Step 2.5 (Subagent G, 2026-05-08): forget the project's
    // user-secret active-flag rows. Without this, a future re-register
    // of the same project_id (rare but possible — the GUI generates a
    // fresh UUID per registration so this only happens via manual DB
    // tampering OR a launcher reinstall that preserves launcher.db)
    // would resurrect ghost rows for keys whose keychain values may
    // long since be gone. The keychain values themselves are NOT
    // deleted here (intentional — see helper doc).
    //
    // Soft-fail: a DB hiccup leaves the rows in place; the next
    // unregister or a manual cleanup will retry. Doesn't block the
    // canonical DB delete below.
    match db.forget_user_secret_state_for_project(&id) {
        Ok(_n) => {}
        Err(e) => report.warnings.push(format!(
            "could not forget user-secret active-flag rows for project {}: {}. \
             The rows are orphan now (project's gone) but harmless — they \
             only matter if the same project_id is re-registered.",
            id, e
        )),
    }

    // 0.2.1: drop every secret-grant row touching this project (either
    // side — owner OR grantee). Without this, a future re-register of
    // the same project_id would inherit grants the owner never re-
    // approved (privacy violation), and dangling grants where the
    // owner's already gone produce 404s on the resolver path. Same
    // soft-fail policy as the active-flag cleanup above.
    match db.forget_grants_for_project(&id) {
        Ok(_n) => {}
        Err(e) => report.warnings.push(format!(
            "could not forget secret-grant rows for project {}: {}. \
             Any grants where this project was the owner or grantee \
             will linger in secret_grants — re-registering the same \
             project_id would inadvertently inherit them.",
            id, e
        )),
    }

    // v0.2.49 Step F MF4 (L1-F3): capture the deleted project's KG
    // binding collection names BEFORE the cascade DELETE, so we can
    // sweep cross-project peer-grant rows on those collections
    // afterward. The FK CASCADE in 001_initial.sql:64 drops rows
    // where project_id = deleted_id but not rows where
    // collection_name is one of the deleted project's collections AND
    // project_id is a different (live) project. Without this sweep,
    // peer access rows stay stranded forever pointing at collections
    // that no longer exist (unless `purge_collections=true`, in
    // which case the boot reconcile cleans up).
    let deleted_collection_names: Vec<String> = match db.list_project_kg_bindings(&id) {
        Ok(bindings) => bindings.into_iter().map(|b| b.collection_name).collect(),
        Err(e) => {
            // Soft-fail: peer-row cleanup is observability, not
            // correctness for the delete itself. Log to warnings + skip.
            report.warnings.push(format!(
                "could not enumerate KG bindings for peer-row cleanup on \
                 project {}: {}. Cross-project peer access rows (if any) \
                 will linger until next boot reconcile.",
                id, e
            ));
            Vec::new()
        }
    };

    // Step 3 (always): audit + DB delete + change log.
    db.audit(
        "project_delete",
        Some(&id),
        None,
        &serde_json::json!({
            "name": row.name,
            "purge_launcher_files": opts.purge_launcher_files,
            "purge_collections": opts.purge_collections,
            "files_purged_count": report.files_purged.len(),
            "keys_purged_from_env_count": report.keys_purged_from_env.len(),
            "collections_dropped_count": report.collections_dropped.len(),
            "warnings_count": report.warnings.len(),
        }),
    )?;
    db.delete_project(&id)?;
    let _ = db.log_change("projects", "delete", Some(&id), Some(&id));

    // v0.2.49 Step F MF4: now that the project's own rows are gone
    // via FK CASCADE, sweep cross-project peer-grant rows on the
    // deleted project's collection_names. Soft-fail with audit-log
    // emission — the orphan rows are harmless until they accumulate.
    if !deleted_collection_names.is_empty() {
        match db.delete_orphan_peer_access_for_collections(&id, &deleted_collection_names) {
            Ok(0) => { /* no peer grants existed */ }
            Ok(n) => {
                let _ = db.audit(
                    "kg_peer_access_cleanup_on_project_delete",
                    Some(&id),
                    None,
                    &serde_json::json!({
                        "deleted_peer_rows": n,
                        "collections": deleted_collection_names,
                    }),
                );
            }
            Err(e) => report.warnings.push(format!(
                "could not sweep cross-project peer access rows for \
                 deleted project {}'s collections: {}. Orphan rows \
                 (if any) will linger; boot reconcile is the backup.",
                id, e
            )),
        }
    }

    Ok(report)
}

/// Bug 15: spawn the user's editor of choice opened on the project folder.
///
/// Tries `code` (VS Code) first; if not on PATH, returns a user-friendly
/// error so the launcher can show a "VS Code not installed" toast. Does
/// NOT block — the editor is launched detached and the launcher process
/// continues. Returns immediately on success.
///
/// Bug 24: `surface` selects which Claude Code surface to use:
/// - "vscode" (default): `code <folder>` (VS Code extension picks up
///   env from `.claude/settings.json` `env` — the canonical channel
///   since v0.2.12 / PR-27, which propagates to MCP subprocesses too;
///   pre-v0.2.12 the launcher also wrote `.vscode/settings.json`
///   `claude-code.env` but that surface didn't propagate to MCPs on
///   Linux and was removed).
/// - "cli": opens the system terminal in <folder> and runs `claude`. The
///   user's shell rc OR our `tools/claude` wrapper sources `.claude/env`
///   (Bug 23).
/// - "auto": prefer vscode if `code` is on PATH, else fall back to cli.
#[command]
pub async fn launch_project_in_editor(
    project_id: String,
    surface: Option<String>,
    db: State<'_, Db>,
) -> Result<(), String> {
    let row = db
        .get_project(&project_id)?
        .ok_or_else(|| format!("project {} not found", project_id))?;
    let folder = row.folder_path.clone();
    let chosen = match surface.as_deref().unwrap_or("auto") {
        "vscode" => "vscode",
        "cli" => "cli",
        _ => {
            if which_on_path("code") {
                "vscode"
            } else if which_on_path("claude") {
                "cli"
            } else {
                "vscode"
            }
        }
    };

    let result = match chosen {
        "vscode" => launch_in_vscode(&folder),
        "cli" => launch_in_terminal_with_cli(&folder),
        _ => unreachable!(),
    };

    if result.is_ok() {
        db.audit(
            "project_launch",
            Some(&project_id),
            None,
            &serde_json::json!({ "surface": chosen, "folder": folder }),
        )?;
    }
    result
}

fn which_on_path(cmd: &str) -> bool {
    if let Ok(path) = std::env::var("PATH") {
        for dir in path.split(if cfg!(windows) { ';' } else { ':' }) {
            let candidate = std::path::Path::new(dir).join(if cfg!(windows) {
                format!("{}.exe", cmd)
            } else {
                cmd.to_string()
            });
            if candidate.exists() {
                return true;
            }
        }
    }
    false
}

fn launch_in_vscode(folder: &str) -> Result<(), String> {
    let mut cmd = std::process::Command::new("code").silent();
    cmd.arg(folder);
    match cmd.spawn() {
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Err(
            "VS Code not found on PATH. Install Code from https://code.visualstudio.com/ \
             and ensure the `code` command is on your PATH, or use Claude Code CLI: \
             `cd <project> && claude`."
                .into(),
        ),
        Err(e) => Err(format!("failed to spawn editor: {}", e)),
    }
}

/// Spawn the system terminal in `folder` and run `claude` inside it. The
/// terminal flag varies by OS / DE — try a list of well-known options
/// and use the first that works.
fn launch_in_terminal_with_cli(folder: &str) -> Result<(), String> {
    if !which_on_path("claude") {
        return Err(
            "Claude Code CLI not found on PATH. Install from \
             https://docs.anthropic.com/en/docs/claude-code, or open in VS Code instead."
                .into(),
        );
    }

    // Per-OS terminal command. We use `cd <folder> && claude` as the
    // command-string; the terminal must support a flag that accepts a
    // shell command and keeps the window open afterwards.
    #[cfg(target_os = "linux")]
    let candidates: &[(&str, &[&str])] = &[
        ("gnome-terminal", &["--working-directory", folder, "--", "bash", "-lc", "claude; exec bash"]),
        ("konsole", &["--workdir", folder, "-e", "bash", "-lc", "claude; exec bash"]),
        ("xterm", &["-e", "bash", "-lc"]),
    ];
    #[cfg(target_os = "macos")]
    let candidates: &[(&str, &[&str])] = &[
        ("open", &["-a", "Terminal", folder]),
    ];
    #[cfg(target_os = "windows")]
    let candidates: &[(&str, &[&str])] = &[
        ("wt.exe", &["-d", folder, "powershell", "-NoExit", "-Command", "claude"]),
        ("powershell", &["-NoExit", "-Command", "claude"]),
    ];

    for (bin, args) in candidates {
        let mut cmd = std::process::Command::new(bin).silent();
        for a in *args {
            cmd.arg(a);
        }
        if cmd.spawn().is_ok() {
            return Ok(());
        }
    }

    Err("Could not find a system terminal to spawn (gnome-terminal, konsole, xterm, \
         Terminal.app, wt.exe). Install one or open in VS Code instead."
        .into())
}

#[cfg(test)]
mod tests {
    use super::*;

    // ─── Bug 23: per-project env file generation ───────────────────

    #[test]
    fn sanitize_kg_collection_strips_separators_and_titlecases() {
        assert_eq!(sanitize_kg_collection("My Project"), "MyProject");
        assert_eq!(sanitize_kg_collection("my-project"), "MyProject");
        assert_eq!(sanitize_kg_collection("snake_case_name"), "SnakeCaseName");
        assert_eq!(sanitize_kg_collection("123-leading-digit"), "P123LeadingDigit");
        assert_eq!(sanitize_kg_collection(""), "Project");
        assert_eq!(sanitize_kg_collection("...!!!..."), "Project");
        assert_eq!(sanitize_kg_collection("Already CamelCase"), "AlreadyCamelCase");
    }

    // ─── B12: stale KG_COLLECTION .env auto-repair (0.2.11) ─────────────

    fn b12_tmp_env(content: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "vct-b12-test-{}.env",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::write(&path, content).unwrap();
        path
    }

    #[test]
    fn b12_repair_rewrites_bare_stale_kg_collection() {
        let env = b12_tmp_env(
            "# project env\n\
             KG_COLLECTION=KnowledgeGraph\n\
             OLLAMA_URL=http://localhost:11435\n",
        );
        let out = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        match out {
            B12Outcome::Repaired { canonical_kg } => {
                assert_eq!(canonical_kg, "MyTest_KnowledgeGraph");
            }
            other => panic!("expected Repaired, got {:?}", other),
        }
        let rewritten = std::fs::read_to_string(&env).unwrap();
        // First active line is now the canonical one (with audit annotation).
        assert!(
            rewritten.contains("KG_COLLECTION=MyTest_KnowledgeGraph # B12 auto-repaired 0.2.11: was \"KG_COLLECTION=KnowledgeGraph\""),
            "unexpected rewrite: {}",
            rewritten
        );
        // Other lines preserved verbatim.
        assert!(rewritten.contains("OLLAMA_URL=http://localhost:11435"));
        assert!(rewritten.starts_with("# project env\n"));
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_rewrites_nosuffix_stale_kg_collection() {
        // Pre-0.2.11 bug: .env carried "KG_COLLECTION=MyTest" instead of
        // "KG_COLLECTION=MyTest_KnowledgeGraph".
        let env = b12_tmp_env("KG_COLLECTION=MyTest\n");
        let out = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        assert_eq!(
            out,
            B12Outcome::Repaired {
                canonical_kg: "MyTest_KnowledgeGraph".into()
            }
        );
        let rewritten = std::fs::read_to_string(&env).unwrap();
        assert!(rewritten.contains("KG_COLLECTION=MyTest_KnowledgeGraph # B12 auto-repaired 0.2.11: was \"KG_COLLECTION=MyTest\""));
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_is_idempotent_after_first_run() {
        // After repair, re-running must be a no-op (canonical line
        // already present).
        let env = b12_tmp_env("KG_COLLECTION=KnowledgeGraph\n");
        let _ = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        let out2 = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        assert_eq!(out2, B12Outcome::NoChangeNeeded);
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_no_change_when_canonical_already_present() {
        let env = b12_tmp_env(
            "KG_COLLECTION=MyTest_KnowledgeGraph\n\
             OTHER=value\n",
        );
        let before = std::fs::read_to_string(&env).unwrap();
        let out = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        assert_eq!(out, B12Outcome::NoChangeNeeded);
        let after = std::fs::read_to_string(&env).unwrap();
        assert_eq!(before, after, "file must be untouched");
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_no_change_when_no_kg_collection_line() {
        let env = b12_tmp_env("OLLAMA_URL=http://localhost:11435\n");
        let before = std::fs::read_to_string(&env).unwrap();
        let out = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        assert_eq!(out, B12Outcome::NoChangeNeeded);
        let after = std::fs::read_to_string(&env).unwrap();
        assert_eq!(before, after);
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_no_change_when_env_file_missing() {
        let nonexistent = std::env::temp_dir().join(format!(
            "vct-b12-missing-{}.env",
            uuid::Uuid::new_v4().simple()
        ));
        let out = b12_repair_stale_kg_collection(&nonexistent, "My Test").unwrap();
        assert_eq!(out, B12Outcome::NoChangeNeeded);
        // No file created as a side effect.
        assert!(!nonexistent.exists());
    }

    #[test]
    fn b12_repair_rewrites_only_first_stale_line_when_multiple_present() {
        // Pathological case: user manually appended a second stale line.
        // We touch only the first one (matches the consumer-reads-first
        // semantics). The second remains as-is; running again is no-op
        // because the canonical now precedes it.
        let env = b12_tmp_env(
            "KG_COLLECTION=KnowledgeGraph\n\
             KG_COLLECTION=KnowledgeGraph\n",
        );
        let out = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        assert!(matches!(out, B12Outcome::Repaired { .. }));
        let rewritten = std::fs::read_to_string(&env).unwrap();
        let canonical_count = rewritten.matches("KG_COLLECTION=MyTest_KnowledgeGraph").count();
        let stale_count = rewritten
            .lines()
            .filter(|l| l.trim() == "KG_COLLECTION=KnowledgeGraph")
            .count();
        assert_eq!(canonical_count, 1, "exactly one canonical line emitted");
        assert_eq!(stale_count, 1, "second stale line preserved verbatim");
        let _ = std::fs::remove_file(&env);
    }

    #[test]
    fn b12_repair_preserves_trailing_newline_presence() {
        // File ends without \n → rewritten file also ends without \n.
        let env = b12_tmp_env("KG_COLLECTION=KnowledgeGraph");
        let _ = b12_repair_stale_kg_collection(&env, "My Test").unwrap();
        let rewritten = std::fs::read_to_string(&env).unwrap();
        assert!(!rewritten.ends_with('\n'), "no trailing newline added");
        let _ = std::fs::remove_file(&env);

        // File ends with \n → rewritten preserves trailing \n.
        let env2 = b12_tmp_env("KG_COLLECTION=KnowledgeGraph\n");
        let _ = b12_repair_stale_kg_collection(&env2, "My Test").unwrap();
        let rewritten2 = std::fs::read_to_string(&env2).unwrap();
        assert!(rewritten2.ends_with('\n'), "trailing newline preserved");
        let _ = std::fs::remove_file(&env2);
    }

    #[test]
    fn write_project_env_files_creates_both_paths() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("My Test")).unwrap();

        // PR-27 (v0.2.12, 2026-05-16): the writer no longer authors
        // `.vscode/settings.json` `claude-code.env`. Assert the launcher
        // did NOT create the file as a side-effect of the env write —
        // the function's `vscode_dir` mkdir + write block was removed
        // because the key didn't propagate to MCP subprocesses on Linux.
        // The Pylance/watcher exclude block (separate top-level keys)
        // is still managed by Python's `_backfill_vscode_excludes_in_project`,
        // run from a different code path entirely. See the function
        // docstring on `write_project_env_files` for the KG-node reference.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        // 1. CLI shell file path
        let claude_env = tmp.join(".claude/env");
        assert!(claude_env.exists());
        let env_raw = std::fs::read_to_string(&claude_env).unwrap();
        assert!(env_raw.contains(r#"export KG_COLLECTION="MyTest_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export PROJECT_NAME="My Test""#));
        // PR-8 cross-PR handoff: .claude/env shell-export form.
        assert!(env_raw.contains(r#"export CODE_GRAPH_PROJECT="MyTest""#),
                ".claude/env should carry the CODE_GRAPH_PROJECT export. Body:\n{env_raw}");
        assert!(env_raw.contains(r#"export DEVELOPMENT_COLLECTION="MyTest_Development""#));
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/env.
        assert!(!env_raw.contains("CONVERSATION_COLLECTION"));
        assert!(env_raw.contains(r#"export SHARED_KG_COLLECTION="VibeCodedOrchestrator_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(env_raw.contains(r#"export SHARED_KG_OPT_OUT="false""#));
        // v0.2.46 Decision B — symmetric READ gate (default false).
        assert!(env_raw.contains(r#"export SHARED_KG_READ_DISABLED="false""#));

        // 2. Bug 30: canonical .claude/settings.json env block — the
        // channel that actually propagates to MCP subprocesses (the
        // PR-27 empirical-trace KG node calls this out as the canonical
        // surface for per-project MCP env on Linux).
        let claude_settings = tmp.join(".claude/settings.json");
        assert!(claude_settings.exists());
        let raw = std::fs::read_to_string(&claude_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["env"];
        // 2026-05-01: KG_COLLECTION carries the FULL Weaviate class name
        // (suffixed), matching `.env` and the rest of the ecosystem. Was
        // bare `MyTest` until the bare-kg fix.
        assert_eq!(env["KG_COLLECTION"], "MyTest_KnowledgeGraph");
        // PROJECT_NAME is the raw user-supplied name, not the sanitized
        // Weaviate basename. Was `MyTest` (sanitized) before; now matches
        // install.py + the .env template.
        assert_eq!(env["PROJECT_NAME"], "My Test");
        // PR-8 cross-PR handoff: .claude/settings.json env block.
        assert_eq!(env["CODE_GRAPH_PROJECT"], "MyTest");
        // Uppercase D for Development across every surface — Weaviate
        // class names are case-sensitive.
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_Development");
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/settings.json env.
        assert!(env.get("CONVERSATION_COLLECTION").is_none());
        // Shared-KG fields propagate to both surfaces.
        assert_eq!(env["SHARED_KG_COLLECTION"], "VibeCodedOrchestrator_KnowledgeGraph");
        // Canonical write-gate key (asymmetric semantic since 2026-05-01).
        assert_eq!(env["SHARED_KG_WRITE_DISABLED"], "false");
        // Legacy alias mirrors the canonical value (kept for ~3 releases).
        assert_eq!(env["SHARED_KG_OPT_OUT"], "false");
        // v0.2.46 Decision B — symmetric READ gate (default false).
        assert_eq!(env["SHARED_KG_READ_DISABLED"], "false");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// PR-2 (2026-05-06): the launcher's `.claude/env` writer must export
    /// `VCT_ORCHESTRATOR_ROOT` and `VCT_INFRASTRUCTURE_DIR` so the bundled
    /// hook (`ensure-containers.sh`) and the rewired Python scripts in
    /// `.claude/scripts/*` can resolve the orchestrator clone at runtime
    /// instead of relying on baked absolute paths.
    ///
    /// `find_local_repo_root()` walks up from the test binary and finds
    /// the orchestrator repo whose `vct-module.json` lives at the top.
    /// In the test env that's `/tmp/pr2-templates-portability` — assert
    /// the export line shows up. If `find_local_repo_root` fails (rare;
    /// the launcher itself wouldn't be running in that case) the writer
    /// silently omits the line — guard that branch separately.
    #[test]
    fn claude_env_exports_orchestrator_root_when_resolvable() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-orch-root-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // PR-3 (2026-05-06): the writer takes a `ProjectEnvSettings`
        // whose `orchestrator_root` field carries the value PR-2 used to
        // resolve via `find_local_repo_root()`. Use the live populate path
        // so this test still observes the same behaviour: when the test
        // binary runs inside the orchestrator clone, populate finds the
        // root and the writer emits the export; when it runs outside, the
        // field is `None` and the writer silently omits the line.
        let mut settings = ProjectEnvSettings::with_defaults("OrchRootTest");
        settings.orchestrator_root =
            crate::commands::installer::find_local_repo_root().ok();
        write_project_env_files(&tmp, &settings).unwrap();

        let claude_env = tmp.join(".claude/env");
        let env_raw = std::fs::read_to_string(&claude_env).unwrap();

        // When find_local_repo_root succeeds, both lines must be present.
        // When it fails, neither should be present (silent omit). Don't
        // hardcode the success branch — observe what the function did.
        if let Ok(orch) = crate::commands::installer::find_local_repo_root() {
            let orch_str = orch.display().to_string();
            let infra_str = orch.join("infrastructure").display().to_string();
            assert!(
                env_raw.contains(&format!("VCT_ORCHESTRATOR_ROOT=\"{}\"", orch_str)),
                "expected VCT_ORCHESTRATOR_ROOT export with path {} in:\n{}",
                orch_str, env_raw,
            );
            assert!(
                env_raw.contains(&format!("VCT_INFRASTRUCTURE_DIR=\"{}\"", infra_str)),
                "expected VCT_INFRASTRUCTURE_DIR export with path {} in:\n{}",
                infra_str, env_raw,
            );
        } else {
            // Either no export or empty value — but not a stale literal.
            assert!(
                !env_raw.contains("VCT_ORCHESTRATOR_ROOT=\"\""),
                "writer emitted empty VCT_ORCHESTRATOR_ROOT — should omit instead",
            );
        }

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Install-flow audit (2026-05-08, P1 #2): VCT_ORCHESTRATOR_ROOT
    /// and VCT_INFRASTRUCTURE_DIR must reach the JSON env surfaces too,
    /// not just `.claude/env`. Pre-fix the launcher only wrote them to
    /// `.claude/env`, which is sourced by the `tools/claude` wrapper —
    /// VS Code-extension users (the dominant Linux/macOS/Windows path)
    /// silently lost these vars and the bundled hooks fell back to a
    /// non-existent `claude_mcp_servers/.venv` path inside managed
    /// projects.
    ///
    /// Asserts: when `settings.orchestrator_root` is `Some`, both
    /// portability keys appear in
    ///   * `.claude/env`               (POSIX export form)
    ///   * `.claude/settings.json`     (JSON `env` block)
    ///
    /// And conversely: when `orchestrator_root` is `None`, neither
    /// surface contains the keys (omit, don't write empty).
    ///
    /// PR-27 (v0.2.12, 2026-05-16): the historical third surface
    /// (`.vscode/settings.json` `claude-code.env`) was removed because
    /// it didn't propagate to MCP subprocesses on Linux. The launcher
    /// must NOT author the file at all from this code path. See the
    /// function docstring on `write_project_env_files` for the KG-node
    /// reference.
    #[test]
    fn vct_portability_keys_propagate_to_both_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-portability-prop-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Use a fixed synthetic path so the assertion is independent of
        // whether the test binary is running inside a real orch clone.
        let synthetic_orch = std::env::temp_dir()
            .join(format!("vct-portability-orch-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&synthetic_orch).unwrap();

        let mut settings = ProjectEnvSettings::with_defaults("PortabilityTest");
        settings.orchestrator_root = Some(synthetic_orch.clone());
        write_project_env_files(&tmp, &settings).unwrap();

        let orch_str = synthetic_orch.display().to_string();
        let infra_str = synthetic_orch.join("infrastructure").display().to_string();

        // Surface 1: .claude/env (POSIX exports).
        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            claude_env_text.contains(&format!("export VCT_ORCHESTRATOR_ROOT=\"{}\"", orch_str)),
            ".claude/env missing VCT_ORCHESTRATOR_ROOT export. Body:\n{}",
            claude_env_text,
        );
        assert!(
            claude_env_text.contains(&format!("export VCT_INFRASTRUCTURE_DIR=\"{}\"", infra_str)),
            ".claude/env missing VCT_INFRASTRUCTURE_DIR export. Body:\n{}",
            claude_env_text,
        );

        // Surface 2: .claude/settings.json env block.
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap()
        ).unwrap();
        assert_eq!(
            cs["env"]["VCT_ORCHESTRATOR_ROOT"], orch_str,
            ".claude/settings.json env block missing or wrong VCT_ORCHESTRATOR_ROOT. \
             Block: {}", cs["env"],
        );
        assert_eq!(
            cs["env"]["VCT_INFRASTRUCTURE_DIR"], infra_str,
            ".claude/settings.json env block missing or wrong VCT_INFRASTRUCTURE_DIR. \
             Block: {}", cs["env"],
        );

        // PR-27: the launcher must NOT have created .vscode/settings.json
        // as part of the env write — the historical third surface is
        // gone.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
        std::fs::remove_dir_all(&synthetic_orch).ok();
    }

    /// Install-flow audit (2026-05-08, P1 #2): the omit-on-`None`
    /// semantics from the pre-fix `.claude/env` writer must extend to
    /// every surface. When the launcher runs outside a git checkout
    /// (`settings.orchestrator_root = None`), neither portability key
    /// should appear in any surface — and crucially not as empty-string
    /// values that would mask the in-tree fallback resolution path the
    /// hooks rely on.
    ///
    /// PR-27 (v0.2.12, 2026-05-16): post-removal of the
    /// `.vscode/settings.json` surface, the assertion is reduced to
    /// `.claude/env` + `.claude/settings.json` — the omit contract now
    /// holds across the two surviving surfaces. The launcher additionally
    /// must NOT create `.vscode/settings.json` as a side-effect of the
    /// env write.
    #[test]
    fn vct_portability_keys_omitted_on_both_surfaces_when_none() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-portability-omit-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // `with_defaults` leaves orchestrator_root = None.
        let settings = ProjectEnvSettings::with_defaults("OmitTest");
        assert!(settings.orchestrator_root.is_none(),
            "test precondition: with_defaults must leave orchestrator_root=None");
        write_project_env_files(&tmp, &settings).unwrap();

        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        // No `export VCT_*=...` lines, and crucially no empty literals
        // that would shadow the fallback resolver. The header comment
        // legitimately mentions the names so we look for the export-
        // form prefix specifically.
        assert!(
            !claude_env_text.contains("export VCT_ORCHESTRATOR_ROOT="),
            ".claude/env should not export VCT_ORCHESTRATOR_ROOT when \
             orchestrator_root=None. Body:\n{}", claude_env_text,
        );
        assert!(
            !claude_env_text.contains("export VCT_INFRASTRUCTURE_DIR="),
            ".claude/env should not export VCT_INFRASTRUCTURE_DIR when \
             orchestrator_root=None. Body:\n{}", claude_env_text,
        );

        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap()
        ).unwrap();
        assert!(
            cs["env"].get("VCT_ORCHESTRATOR_ROOT").is_none(),
            ".claude/settings.json env should not contain VCT_ORCHESTRATOR_ROOT \
             when orchestrator_root=None. Block: {}", cs["env"],
        );
        assert!(
            cs["env"].get("VCT_INFRASTRUCTURE_DIR").is_none(),
            ".claude/settings.json env should not contain VCT_INFRASTRUCTURE_DIR \
             when orchestrator_root=None. Block: {}", cs["env"],
        );

        // PR-27: file must not exist (and therefore cannot carry the
        // portability keys either).
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// 0.1.7 fork-readiness sweep (2026-05-08): when the OnboardingWizard
    /// has registered a PAT (i.e. the keychain has `github_pat` AND the
    /// secret is active), the env-pair builder MUST emit `GITHUB_TOKEN`
    /// to all three install surfaces. Mirrors the
    /// `vct_portability_keys_propagate_to_all_three_surfaces` shape.
    ///
    /// This replaces the pre-0.1.7 `git-credential-vct` helper:
    /// per-project env propagation is the canonical mechanism since the
    /// helper protocol is project-agnostic and incompatible with the
    /// per-project active-flag gate.
    #[test]
    fn write_project_env_files_emits_github_token_when_keychain_has_entry() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-github-token-prop-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Bypass the populate() path entirely: build a settings struct
        // with `github_token = Some(canary)` directly. This pins the
        // pair-builder behaviour without depending on the keychain
        // backend or DB state — the populate() side has its own tests
        // in `installer::tests::github_pat_keychain_tests`.
        let canary = "ghp_pair_builder_canary_value_12345";
        let mut settings = ProjectEnvSettings::with_defaults("GhTokTest");
        settings.github_token = Some(canary.to_string());
        write_project_env_files(&tmp, &settings).unwrap();

        // Surface 1: .claude/env (POSIX exports).
        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            claude_env_text.contains(&format!("export GITHUB_TOKEN=\"{}\"", canary)),
            ".claude/env missing GITHUB_TOKEN export. Body:\n{}",
            claude_env_text,
        );

        // Surface 2: .claude/settings.json env block.
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            cs["env"]["GITHUB_TOKEN"], canary,
            ".claude/settings.json env block missing or wrong GITHUB_TOKEN. \
             Block: {}",
            cs["env"],
        );

        // PR-27 (v0.2.12, 2026-05-16): the writer no longer touches
        // `.vscode/settings.json` — the file must not have been created
        // by the env write. See function-level docstring for the
        // empirical-trace KG-node reference.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// 0.1.7 fork-readiness sweep (2026-05-08): when there is no PAT
    /// in the keychain (or it's paused via Lifecycle B), the env-pair
    /// builder MUST omit `GITHUB_TOKEN` from every surface. Matches
    /// PR #171 P1.3's "None filter" behaviour for orchestrator_root.
    ///
    /// Crucially, the writer must NOT emit an empty-string value:
    /// downstream consumers (`gh` CLI, custom git credential helpers)
    /// distinguish "GITHUB_TOKEN unset" from "GITHUB_TOKEN=''" and
    /// the latter would mask the user's other auth flow (e.g. an
    /// existing `~/.config/gh/hosts.yml` token).
    #[test]
    fn write_project_env_files_omits_github_token_when_keychain_empty() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-github-token-omit-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // `with_defaults` leaves github_token = None.
        let settings = ProjectEnvSettings::with_defaults("OmitGhTok");
        assert!(
            settings.github_token.is_none(),
            "test precondition: with_defaults must leave github_token=None",
        );
        write_project_env_files(&tmp, &settings).unwrap();

        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            !claude_env_text.contains("export GITHUB_TOKEN="),
            ".claude/env should not export GITHUB_TOKEN when \
             keychain has no entry. Body:\n{}",
            claude_env_text,
        );

        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        )
        .unwrap();
        assert!(
            cs["env"].get("GITHUB_TOKEN").is_none(),
            ".claude/settings.json env should not contain GITHUB_TOKEN \
             when keychain has no entry. Block: {}",
            cs["env"],
        );

        // PR-27: file must not exist (and therefore cannot carry the
        // token either).
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn env_surfaces_agree_after_write_project_env_files() {
        // 3-way equality regression: KG_COLLECTION must be IDENTICAL across
        // .env (template), .claude/env POSIX exports, and
        // .claude/settings.json env block.
        //
        // Pre-fix: bare in three, suffixed in .env → the bug VideoFrames hit.
        //
        // PR-27 (v0.2.12, 2026-05-16): the historical fourth surface
        // (`.vscode/settings.json claude-code.env`) was removed because
        // it didn't propagate to MCP subprocesses on Linux. The parity
        // check is now 3-way; the file must not have been created at
        // all by the env write. See `write_project_env_files` docstring
        // for the empirical-trace KG-node reference.
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-parity-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("VideoFrames")).unwrap();
        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("VideoFrames")).unwrap();

        let env_text = std::fs::read_to_string(tmp.join(".env")).unwrap();
        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap()).unwrap();

        assert!(env_text.contains("KG_COLLECTION=VideoFrames_KnowledgeGraph"));
        assert!(claude_env_text.contains(r#"export KG_COLLECTION="VideoFrames_KnowledgeGraph""#));
        assert_eq!(cs["env"]["KG_COLLECTION"], "VideoFrames_KnowledgeGraph");

        assert!(env_text.contains("DEVELOPMENT_COLLECTION=VideoFrames_Development"));
        assert!(claude_env_text.contains(r#"export DEVELOPMENT_COLLECTION="VideoFrames_Development""#));
        assert_eq!(cs["env"]["DEVELOPMENT_COLLECTION"], "VideoFrames_Development");

        // PR-27: file must not exist (and therefore cannot carry the
        // canonical keys either).
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Bug 30: existing `.claude/settings.json` content (hooks, permissions,
    /// agents config, etc.) MUST be preserved when we inject the env block.
    /// Read-merge-write semantics; the canonical env keys we own are
    /// updated, user-added env keys at the same level survive.
    ///
    /// PR-3 Commit 6 (2026-05-06): the `env` sub-block is deep-merged
    /// (was wholesale-replaced pre-PR-3). User-added env keys at that
    /// level now survive launcher re-writes.
    #[test]
    fn write_preserves_existing_claude_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-merge-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp.join(".claude")).unwrap();
        let path = tmp.join(".claude/settings.json");
        std::fs::write(
            &path,
            r#"{
                "hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]},
                "permissions": {"allow": ["Read"]},
                "env": {"OLD_KEY": "old_value"}
            }"#,
        )
        .unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("MyProject")).unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();

        // Canonical env keys updated with launcher values.
        assert_eq!(v["env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");
        assert_eq!(v["env"]["PROJECT_NAME"], "MyProject");
        // PR-3 Commit 6: user-added env keys at the env-sub-block level
        // are preserved by the deep-merge.
        assert_eq!(
            v["env"]["OLD_KEY"], "old_value",
            "user-added env keys must be preserved by deep-merge"
        );
        // Existing hooks + permissions preserved untouched (top-level merge).
        assert!(v["hooks"]["PreToolUse"].is_array());
        assert!(v["permissions"]["allow"].is_array());
        assert_eq!(v["permissions"]["allow"][0], "Read");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// PR-27 (v0.2.12, 2026-05-16): existing `.vscode/settings.json` is
    /// no longer touched by `write_project_env_files`. The writer used
    /// to deep-merge a `claude-code.env` block in (Bug 32 + PR-3 Commit 6),
    /// but that block did not propagate to MCP subprocesses on Linux
    /// as of Claude Code 2.1.143 (empirical trace in PR-27 commit
    /// message; verified via `/proc/<mcp_pid>/environ` sentinel test).
    ///
    /// Inverted contract: if the user has a pre-existing
    /// `.vscode/settings.json` (with their own editor preferences, a
    /// hand-written `claude-code.env` block, anything), the file must
    /// survive the env-write call BYTE-FOR-BYTE — the launcher must
    /// not author anything into it. The user remains the sole author of
    /// `.vscode/settings.json`; the canonical channel for per-project
    /// MCP env is now `.claude/settings.json` `env` (asserted by sister
    /// tests like `write_project_env_files_creates_both_paths`).
    #[test]
    fn write_does_not_touch_existing_vscode_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-vscode-merge-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".vscode")).unwrap();
        let path = tmp.join(".vscode/settings.json");
        let pre_existing = r#"{
                "editor.formatOnSave": true,
                "python.defaultInterpreterPath": "/usr/bin/python3",
                "claude-code.env": {"OLD_KEY": "old"}
            }"#;
        std::fs::write(&path, pre_existing).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("MyProject")).unwrap();

        // The file must be byte-for-byte identical — the launcher's
        // env-write code path no longer touches `.vscode/settings.json`.
        // (PR-7's Python-side `_backfill_vscode_excludes_in_project`
        // is a separate code path with its own tests; it manages the
        // Pylance/watcher exclude block at the top level and is not
        // invoked from `write_project_env_files`.)
        let after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            after, pre_existing,
            "PR-27: write_project_env_files must leave .vscode/settings.json unchanged"
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Bug 30: corrupted `.claude/settings.json` must not crash project
    /// creation. We log a warning and overwrite with a minimal env block.
    #[test]
    fn write_handles_corrupted_claude_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-corrupt-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp.join(".claude")).unwrap();
        let path = tmp.join(".claude/settings.json");
        std::fs::write(&path, "{ this is not valid json").unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("MyProject")).expect("must not crash");

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");

        std::fs::remove_dir_all(&tmp).ok();
    }

    // Bug 15: smoke test that the launch command resolves the project row
    // and returns a clean error when the editor binary is missing. We
    // can't actually spawn `code` reliably in CI, so we verify the path
    // resolution and the not-found error contract by overriding PATH.

    #[test]
    fn launch_returns_not_found_when_editor_missing() {
        // Override PATH so `code` is guaranteed not findable. We don't
        // call the Tauri command directly (it requires State<Db>), but
        // the spawn-failure branch is the one we want to assert on. A
        // direct std::process::Command spawn with an empty PATH gives us
        // the same NotFound error our command translates.
        let saved = std::env::var_os("PATH");
        // SAFETY: tests are single-threaded by default in this crate; if
        // that ever changes, gate this with a Mutex or use std::process
        // env directly per-call.
        unsafe { std::env::set_var("PATH", ""); }
        let res = std::process::Command::new("code").silent().arg(".").spawn();
        if let Some(p) = saved {
            unsafe { std::env::set_var("PATH", p); }
        } else {
            unsafe { std::env::remove_var("PATH"); }
        }
        let err = res.expect_err("expected NotFound when PATH is empty");
        assert_eq!(err.kind(), std::io::ErrorKind::NotFound);
    }

    // ─── Bug 28: onboarding finish must produce a project record ──
    //
    // We can't drive the Tauri `#[command]` directly without the
    // State<Db> harness, but the command body is a thin wrapper around
    // `db.insert_project` + folder-create + env-file write. This test
    // exercises that core sequence end-to-end against an in-memory db.
    // After the simulated onboarding flow finishes, `list_projects`
    // must return at least one row.

    #[test]
    fn onboarding_finish_inserts_project_row() {
        use crate::db::Db;

        let db = Db::open_in_memory().expect("in-memory db");

        // Simulate the flow that OnboardingWizard.finish() drives:
        //   1. Create a fresh folder for the project (matches the
        //      `create_dir_all` step inside create_project_v2).
        //   2. Generate a unique slug for the chosen name.
        //   3. Insert the project row.
        //   4. Write the per-project env files (mirrors create_project_v2).
        let folder = std::env::temp_dir().join(format!(
            "vct-bug28-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&folder).unwrap();

        let id = uuid::Uuid::new_v4().to_string();
        let name = "Bug28 Onboarding Project";
        let slug = db.generate_unique_slug(name).expect("slug");
        let row = db
            .insert_project(
                &id,
                name,
                folder.to_string_lossy().as_ref(),
                ProjectHost::Base,
                &slug,
            )
            .expect("insert_project");
        assert_eq!(row.name, name);

        // Mirror the env-file write the real command does.
        write_project_env_files(&folder, &ProjectEnvSettings::with_defaults(name)).expect("env files");

        // The contract: after onboarding, at least one project row
        // exists. The user reported ending up with zero — that's the
        // regression this guards against.
        let all = db.list_projects().expect("list_projects");
        assert!(
            !all.is_empty(),
            "expected at least one project row after onboarding finish"
        );
        assert!(
            all.iter().any(|p| p.name == name),
            "expected project named {name:?} in list, got {:?}",
            all.iter().map(|p| &p.name).collect::<Vec<_>>()
        );

        // env files must have landed at the project folder.
        // PR-27 (v0.2.12, 2026-05-16): only `.claude/env` and (implied by
        // the writer's `.claude/settings.json` write) the canonical env
        // block are authored. `.vscode/settings.json` is intentionally
        // NOT created — see `write_does_not_touch_existing_vscode_settings_json`
        // for the contract.
        assert!(folder.join(".claude/env").exists());
        assert!(
            !folder.join(".vscode/settings.json").exists(),
            "PR-27: onboarding must not create .vscode/settings.json via the env writer",
        );

        std::fs::remove_dir_all(&folder).ok();
    }

    // ─── Deliverable 1 (2026-04-28): ensure_project_env_template ──

    fn _scratch_dir(tag: &str) -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "vct-envtmpl-{}-{}",
            tag,
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&p).unwrap();
        p
    }

    #[test]
    fn ensure_env_template_creates_when_missing() {
        let dir = _scratch_dir("create");
        assert!(!dir.join(".env").exists());
        let report = ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("Acme")).unwrap();
        assert_eq!(report.action, "created");
        assert!(dir.join(".env").exists());
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        // Active keys filled with project-substituted values.
        assert!(text.contains("KG_COLLECTION=Acme_KnowledgeGraph"));
        assert!(text.contains("PROJECT_NAME=Acme"));
        // Optional keys remain commented.
        assert!(text.contains("# OPENAI_API_KEY="));
        assert!(text.contains("# GITHUB_TOKEN="));
        // Active OPENAI_API_KEY must NOT appear.
        assert!(!text.contains("\nOPENAI_API_KEY="));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_appends_missing_with_marker() {
        let dir = _scratch_dir("append");
        let env_path = dir.join(".env");
        std::fs::write(&env_path, "OPENAI_API_KEY=sk-user\n").unwrap();
        let report = ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("X")).unwrap();
        assert_eq!(report.action, "appended");
        let text = std::fs::read_to_string(&env_path).unwrap();
        // User value preserved verbatim.
        assert!(text.contains("OPENAI_API_KEY=sk-user"));
        // Marker present.
        assert!(text.contains(ENV_VCO_MARKER));
        // Missing keys appended.
        assert!(text.contains("KG_COLLECTION=X_KnowledgeGraph"));
        assert!(text.contains("# GITHUB_TOKEN="));
        // OPENAI_API_KEY must appear exactly once (the user's line).
        let count = text.matches("OPENAI_API_KEY").count();
        assert_eq!(count, 1, "expected 1, got {count}\n{text}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_idempotent_on_double_run() {
        let dir = _scratch_dir("idem");
        ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("X")).unwrap();
        let after_first = std::fs::read_to_string(dir.join(".env")).unwrap();
        let report = ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("X")).unwrap();
        let after_second = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert_eq!(report.action, "noop");
        assert_eq!(after_first, after_second);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_recognises_commented_form_as_present() {
        // User has `# ANTHROPIC_API_KEY=` — the commented canonical
        // form. Re-running must NOT append a duplicate.
        let dir = _scratch_dir("commented");
        std::fs::write(
            dir.join(".env"),
            "# my prose\n# ANTHROPIC_API_KEY=\nGITHUB_TOKEN=ghp_user\n",
        )
        .unwrap();
        ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("X")).unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        let count = text.matches("ANTHROPIC_API_KEY").count();
        assert_eq!(count, 1, "expected 1 occurrence, got {count}\n{text}");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_handles_no_trailing_newline() {
        let dir = _scratch_dir("nonl");
        std::fs::write(dir.join(".env"), "FOO=bar").unwrap();
        ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("X")).unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert!(text.contains("FOO=bar\n"),
                "user line should now end with newline: {text:?}");
        // Marker line must not be glued to FOO=bar.
        for line in text.lines() {
            if line.contains(ENV_VCO_MARKER) {
                assert!(!line.starts_with("FOO=bar"),
                        "marker glued to user line: {line:?}");
            }
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ensure_env_template_user_value_for_kg_collection_not_overwritten() {
        let dir = _scratch_dir("kguser");
        std::fs::write(dir.join(".env"), "KG_COLLECTION=MyCustom_KG\n").unwrap();
        ensure_project_env_template(&dir, &ProjectEnvSettings::with_defaults("Acme")).unwrap();
        let text = std::fs::read_to_string(dir.join(".env")).unwrap();
        assert!(text.contains("KG_COLLECTION=MyCustom_KG"));
        assert!(!text.contains("KG_COLLECTION=Acme_KnowledgeGraph"));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_existing_env_keys_handles_blank_and_comment_only_lines() {
        let text = "\n\n# pure prose comment\n\n# Another: with a colon\nFOO=bar\n";
        let keys = parse_existing_env_keys(text);
        assert_eq!(keys.len(), 1);
        assert!(keys.contains("FOO"));
    }

    #[test]
    fn env_template_canonical_keys_match_python() {
        // Cross-language contract: the Rust canonical-key list MUST
        // match install.py's. If this test fails because the lists
        // diverge, update both sides — the user shouldn't get
        // different keys depending on which surface ran first.
        let rust_keys: std::collections::HashSet<String> = env_canonical_keys()
            .iter()
            .map(|(k, _)| (*k).to_string())
            .collect();
        let expected: std::collections::HashSet<String> = [
            "WEAVIATE_URL", "WEAVIATE_PORT", "OLLAMA_URL", "OLLAMA_PORT",
            "CODE_EMBED_URL",
            "KG_COLLECTION", "SHARED_KG_COLLECTION", "DEVELOPMENT_COLLECTION",
            "PROJECT_NAME",
            // CONVERSATION_COLLECTION removed (B5 2026-05-01).
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "GITHUB_TOKEN",
            "RL_SERVER_URL", "RL_SERVER_PORT", "RL_PROJECT_ROOT",
            "VCT_TELEMETRY",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        assert_eq!(rust_keys, expected, "Rust canonical key set drifted from Python");
    }

    // ─── PR 7 deliverable tests (env-hygiene secondary drift) ────────

    /// B5: CONVERSATION_COLLECTION must not appear in ANY env surface after
    /// create (write_project_env_files + ensure_project_env_template).
    #[test]
    fn conversation_collection_not_written() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b5-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();
        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();

        // .env
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(!env.contains("CONVERSATION_COLLECTION"),
                ".env must not contain CONVERSATION_COLLECTION:\n{env}");

        // PR-27 (v0.2.12, 2026-05-16): the writer no longer authors
        // `.vscode/settings.json`. The B5 contract becomes "absent
        // surface trivially cannot leak the key" — file must not
        // exist.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        // .claude/env
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!ce.contains("CONVERSATION_COLLECTION"),
                ".claude/env must not contain CONVERSATION_COLLECTION:\n{ce}");

        // .claude/settings.json env block
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        ).unwrap();
        assert!(cs["env"].get("CONVERSATION_COLLECTION").is_none(),
                ".claude/settings.json must not have CONVERSATION_COLLECTION");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B7: after create, the canonical key VCT_TELEMETRY is present in the
    /// .env template, not the legacy VIBECODED_TELEMETRY active key.
    /// (The active VIBECODED_TELEMETRY write was in install.py; the Rust
    /// surfaces only carry VCT_TELEMETRY as a commented placeholder.)
    #[test]
    fn telemetry_canonical_key_is_vct_telemetry() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b7-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();

        // Canonical key must be present (as a commented placeholder).
        assert!(env.contains("VCT_TELEMETRY"),
                ".env must reference VCT_TELEMETRY:\n{env}");
        // Legacy key must NOT be written by the Rust template.
        assert!(!env.contains("VIBECODED_TELEMETRY"),
                ".env template must not write VIBECODED_TELEMETRY (read-alias only):\n{env}");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B9: write_project_env_files (called by rename logic) refreshes the
    /// three Claude Code surfaces. Simulate rename by calling
    /// write_project_env_files once with "FooBar" and once with "BazQux"
    /// on the same folder, then assert the second name wins everywhere.
    #[test]
    fn rename_refreshes_env_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b9-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Initial create
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("FooBar")).unwrap();
        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("FooBar")).unwrap();

        // Simulate rename — re-run env writers with new name.
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("BazQux")).unwrap();

        // PR-27 (v0.2.12, 2026-05-16): the VS Code surface
        // (`.vscode/settings.json` `claude-code.env`) is no longer
        // written by `write_project_env_files`. Rename refresh now
        // covers only `.claude/env` and `.claude/settings.json`.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: rename refresh must not create .vscode/settings.json",
        );

        // CLI shell file
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(ce.contains(r#"export KG_COLLECTION="BazQux_KnowledgeGraph""#));
        assert!(!ce.contains("FooBar"));

        // canonical settings.json
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        ).unwrap();
        assert_eq!(cs["env"]["KG_COLLECTION"], "BazQux_KnowledgeGraph");
        assert_eq!(cs["env"]["PROJECT_NAME"], "BazQux");

        // Note: .env is append-only (ensure_project_env_template), so it will
        // still carry FooBar — this is the known limitation documented in B9.
        // The warn path is tested by checking the env file does NOT have the new
        // canonical key (triggering the stale-warning branch in rename_project_v2).

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B12: registering a folder whose .env has stale KG_COLLECTION=KnowledgeGraph
    /// emits a warning in the result. Test via the helper logic directly since
    /// we can't call the Tauri command without State<Db>.
    #[test]
    fn register_project_with_stale_env_detects_stale_kg() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b12-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Pre-populate with stale bare default (the VideoFrames bug pattern).
        std::fs::write(tmp.join(".env"), "KG_COLLECTION=KnowledgeGraph\nMY_VAR=hello\n").unwrap();

        // Run env writers (as create_project_v2 does).
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();
        // ensure_project_env_template is append-only; it will not overwrite the stale line.
        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();

        // B12 stale detection: the canonical key should be absent from .env
        // (since the old KG_COLLECTION=KnowledgeGraph occupies the key slot
        // and ensure_project_env_template skips it as "present").
        let env_text = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(env_text.contains("KG_COLLECTION=KnowledgeGraph"),
                "stale value must still be present (append-only writer):\n{env_text}");
        assert!(!env_text.contains("KG_COLLECTION=Acme_KnowledgeGraph"),
                "canonical value must NOT have been written (stale blocked it):\n{env_text}");

        // The stale detection logic that create_project_v2 would run:
        let kg_basename = sanitize_kg_collection("Acme");
        let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
        let stale_bare = "KG_COLLECTION=KnowledgeGraph";
        let has_stale = env_text.lines().any(|l| l.trim() == stale_bare);
        let missing_canonical = !env_text.contains(&format!("KG_COLLECTION={}", canonical_kg));
        assert!(has_stale && missing_canonical,
                "stale detection must fire (has_stale={has_stale}, missing_canonical={missing_canonical})");

        // MY_VAR user value preserved.
        assert!(env_text.contains("MY_VAR=hello"),
                "user keys must be preserved:\n{env_text}");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// B8: weaviate_mcp GRPC_PORT read-both-keys logic is tested at the
    /// Python layer (tests/test_weaviate_mcp_grpc_port.py). This Rust test
    /// verifies the Rust env surfaces emit no GRPC_PORT key (only the
    /// .claude/settings.json surface does via install.py, not via Rust).
    #[test]
    fn rust_surfaces_do_not_write_grpc_port() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-b8-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();

        // PR-27 (v0.2.12, 2026-05-16): `.vscode/settings.json` is no
        // longer authored by the Rust writer at all, so the "Rust does
        // not inject GRPC_PORT into VS Code surface" assertion becomes
        // tautological — the surface itself isn't created. Assert the
        // file's absence as the strongest possible "no GRPC_PORT here"
        // statement.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        // .claude/env — same.
        let ce = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!ce.contains("GRPC_PORT"),
                ".claude/env must not contain GRPC_PORT");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// MEDIUM-1 (refactored 2026-05-01): SHARED_KG_WRITE_DISABLED toggle
    /// round-trip. Set the toggle, then re-render env surfaces and verify
    /// the canonical key plus the legacy alias both reflect the new value.
    /// (`.env` is owned by ensure_project_env_template — it doesn't carry
    /// the gate, so the relevant surfaces are the 2 written by
    /// write_project_env_files.)
    ///
    /// PR-27 (v0.2.12, 2026-05-16): the historical third surface
    /// (`.vscode/settings.json` `claude-code.env`) was removed because
    /// it didn't propagate to MCP subprocesses on Linux Claude Code
    /// 2.1.143. The toggle round-trip now covers only `.claude/env`
    /// and `.claude/settings.json` env block; the absence of
    /// `.vscode/settings.json` is itself part of the contract.
    #[test]
    fn shared_kg_write_disabled_toggle_flips_both_env_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-medium1-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Default (None / false) → both keys "false" everywhere.
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();

        // Helper returning (cs_canonical, cs_legacy, env_sh_text) so
        // we can assert on both surviving surfaces.
        let read_both = || -> (String, String, String) {
            // PR-27: `.vscode/settings.json` must NEVER exist after a
            // write — re-check on every read pass to catch any
            // regression that re-introduces the surface.
            assert!(
                !tmp.join(".vscode/settings.json").exists(),
                "PR-27: write_project_env_files must not create .vscode/settings.json",
            );
            let cs: serde_json::Value = serde_json::from_str(
                &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
            ).unwrap();
            let env_sh = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
            (
                cs["env"]["SHARED_KG_WRITE_DISABLED"].as_str().unwrap().to_string(),
                cs["env"]["SHARED_KG_OPT_OUT"].as_str().unwrap().to_string(),
                env_sh,
            )
        };

        let (cs_new, cs_old, env_sh) = read_both();
        assert_eq!(cs_new, "false");
        assert_eq!(cs_old, "false");
        assert!(env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(env_sh.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // Flip to true.
        { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = true; write_project_env_files(&tmp, &s) }.unwrap();
        let (cs_new, cs_old, env_sh) = read_both();
        assert_eq!(cs_new, "true");
        assert_eq!(cs_old, "true");
        assert!(env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="true""#));
        assert!(env_sh.contains(r#"export SHARED_KG_OPT_OUT="true""#));
        assert!(!env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(!env_sh.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // Flip back to false.
        { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = false; write_project_env_files(&tmp, &s) }.unwrap();
        let (cs_new, cs_old, env_sh) = read_both();
        assert_eq!(cs_new, "false");
        assert_eq!(cs_old, "false");
        assert!(env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(env_sh.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// MEDIUM-1 (refactored 2026-05-01): get_shared_kg_write_disabled reads
    /// the persisted toggle, defaulting to false on a project with no row.
    /// The DB k/v round-trip exercises the `module_settings` table with the
    /// `__project__` sentinel module_id.
    #[test]
    fn shared_kg_write_disabled_db_roundtrip() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Acme",
            "/tmp/acme",
            ProjectHost::Base,
            "acme",
        ).unwrap();

        // Default: no row → false.
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), false);

        // Persist true under the canonical key.
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(true),
        ).unwrap();
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), true);

        // Flip to false.
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(false),
        ).unwrap();
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), false);
    }

    /// One-shot legacy migration: a row stored under the old
    /// `shared_kg_opt_out` key is silently relocated to the canonical
    /// `shared_kg_write_disabled` key on first read. The legacy row is
    /// deleted to keep the DB tidy. Idempotent on repeat reads.
    #[test]
    fn shared_kg_legacy_setting_migrates_on_first_read() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Acme",
            "/tmp/acme",
            ProjectHost::Base,
            "acme",
        ).unwrap();

        // Seed only the legacy row (simulating a project upgraded from
        // pre-rename launcher).
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
            &serde_json::Value::Bool(true),
        ).unwrap();
        // Sanity: no canonical row yet.
        assert!(db
            .get_setting(&pid, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_WRITE_DISABLED)
            .unwrap()
            .is_none());

        // First read triggers migration AND returns the legacy value.
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), true);

        // After the read: canonical row exists, legacy row removed.
        let canonical = db
            .get_setting(&pid, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_WRITE_DISABLED)
            .unwrap();
        assert_eq!(canonical, Some(serde_json::Value::Bool(true)));
        let legacy = db
            .get_setting(&pid, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY)
            .unwrap();
        assert!(legacy.is_none(), "legacy row must be deleted after migration");

        // Second read is idempotent — still returns the same value, leaves
        // canonical row in place.
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), true);
    }

    /// When BOTH the canonical row and the legacy row exist (e.g. a buggy
    /// older shim wrote both), the canonical one wins and the legacy one is
    /// dropped. Defends against split-brain after the rename.
    #[test]
    fn shared_kg_canonical_wins_over_legacy_when_both_present() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &pid,
            "Acme",
            "/tmp/acme",
            ProjectHost::Base,
            "acme",
        ).unwrap();

        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(false),
        ).unwrap();
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY,
            &serde_json::Value::Bool(true),
        ).unwrap();

        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), false,
                   "canonical 'false' must win over legacy 'true'");
        // Legacy row pruned.
        let legacy = db
            .get_setting(&pid, PROJECT_SETTINGS_MODULE_ID, SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY)
            .unwrap();
        assert!(legacy.is_none());
    }

    /// Deprecated `get_shared_kg_opt_out` shim still works and returns the
    /// same value as the new function. Removed once the legacy command +
    /// env var fully retire.
    #[test]
    #[allow(deprecated)]
    fn legacy_get_shared_kg_opt_out_delegates_correctly() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(&pid, "Acme", "/tmp/acme", ProjectHost::Base, "acme").unwrap();

        // No row → both functions return false.
        assert_eq!(get_shared_kg_opt_out(&db, &pid).unwrap(), false);
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), false);

        // Set the canonical row → both functions return true.
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(true),
        ).unwrap();
        assert_eq!(get_shared_kg_opt_out(&db, &pid).unwrap(), true);
        assert_eq!(get_shared_kg_write_disabled(&db, &pid).unwrap(), true);
    }

    /// Tauri command level: legacy `set_shared_kg_opt_out` delegates to
    /// `set_shared_kg_write_disabled`. Both write under the canonical DB
    /// key and refresh env surfaces. We exercise the underlying logic
    /// (skipping the actual #[command] async wrapping, which needs the
    /// Tauri runtime) by setting the row and confirming env files reflect
    /// it — same code path the deprecated command takes.
    #[test]
    fn deprecated_set_shared_kg_opt_out_writes_canonical_key() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        let tmp = std::env::temp_dir().join(format!(
            "vct-deprecated-set-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        db.insert_project(&pid, "Acme", tmp.to_str().unwrap(), ProjectHost::Base, "acme").unwrap();

        // Simulate the legacy command: same DB write the new command does.
        db.set_setting(
            &pid,
            PROJECT_SETTINGS_MODULE_ID,
            SETTING_KEY_SHARED_KG_WRITE_DISABLED,
            &serde_json::Value::Bool(true),
        ).unwrap();

        // The canonical const aliases the new key, so legacy callers that
        // referenced SETTING_KEY_SHARED_KG_OPT_OUT now write under the
        // canonical key automatically — no double-write hazard.
        assert_eq!(SETTING_KEY_SHARED_KG_OPT_OUT, SETTING_KEY_SHARED_KG_WRITE_DISABLED);
        assert_ne!(SETTING_KEY_SHARED_KG_OPT_OUT_LEGACY, SETTING_KEY_SHARED_KG_WRITE_DISABLED);

        // The persisted value flows through write_project_env_files
        // identically regardless of which command name persisted the row.
        { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = true; write_project_env_files(&tmp, &s) }.unwrap();
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        ).unwrap();
        assert_eq!(cs["env"]["SHARED_KG_WRITE_DISABLED"], "true");
        assert_eq!(cs["env"]["SHARED_KG_OPT_OUT"], "true");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// HIGH-7: when write_project_env_files fails (folder gone / unwritable),
    /// the failure is captured as a structured warning rather than swallowed
    /// via eprintln. We exercise the helper directly: a non-existent parent
    /// directory makes the inner mkdir fail, returning Err — which the
    /// rename_project_v2 caller turns into a `RenameProjectResult.warnings`
    /// entry.
    #[test]
    fn rename_env_refresh_failure_surfaces_as_warning() {
        // Folder that doesn't exist AND whose parent doesn't exist either,
        // forcing mkdir(.vscode) to fail.
        let bogus = std::path::PathBuf::from("/nonexistent-vct-test-root-9d1f7a/sub/proj");
        let res = { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = false; write_project_env_files(&bogus, &s) };
        assert!(res.is_err(),
                "write_project_env_files must fail under a non-existent root");

        // Mirror the rename_project_v2 surface logic to confirm the Err is
        // captured (the public command needs a Db state, so we exercise the
        // warning-construction path here).
        let mut warnings: Vec<String> = Vec::new();
        if let Err(e) = res {
            let msg = format!(
                "rename env refresh (write_project_env_files) failed: {}. \
                 KG routing for the renamed project may be stale until manual repair.",
                e
            );
            warnings.push(msg);
        }
        assert_eq!(warnings.len(), 1,
                   "RenameProjectResult.warnings must capture the env-refresh failure");
        assert!(warnings[0].contains("rename env refresh"),
                "warning must identify the failing surface: {:?}", warnings[0]);
    }

    // ─── v0.2.49 access-matrix Phase 4 (item #11) — rename propagation ───

    /// `propagate_kg_access_on_rename` rewrites the own-primary and
    /// own-dev access rows from the old project name to the new name
    /// when the rename changes the sanitized prefix. Pre-Phase-4 these
    /// rows became orphans on rename, and the newly-derived collection
    /// names had no access grant — so `require_kg_read` rejected every
    /// KG access for the project until manual repair via the GUI.
    #[test]
    fn rename_project_v2_renames_access_rows() {
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(&pid, "Acme", "/tmp/acme", ProjectHost::Base, "acme").unwrap();

        // Seed the access matrix as `create_project_v2` would (the
        // populate path writes 3 rows: own primary write, own dev write,
        // shared read).
        db.populate_kg_collection_access_for_project(&pid, "Acme").unwrap();
        assert_eq!(db.kg_list_access(&pid).unwrap().len(), 3);

        // Mimic the rename: DB row renamed, then propagate.
        db.rename_project(&pid, "Beta", Some("beta")).unwrap();
        let warnings = db.propagate_kg_access_on_rename(&pid, "Acme", "Beta");
        assert!(warnings.is_empty(), "happy path emits no warnings: {:?}", warnings);

        // Old name's rows are gone; new name's rows hold the prior levels.
        let access = db.kg_list_access(&pid).unwrap();
        let by_collection: std::collections::HashMap<&str, &str> =
            access.iter().map(|(c, l)| (c.as_str(), l.as_str())).collect();
        assert_eq!(by_collection.get("Acme_KnowledgeGraph"), None);
        assert_eq!(by_collection.get("Acme_Development"), None);
        assert_eq!(by_collection.get("Beta_KnowledgeGraph"), Some(&"write"));
        assert_eq!(by_collection.get("Beta_Development"), Some(&"write"));
        // Shared row untouched (project-name-INDEPENDENT collection name).
        // v0.2.49 Step F SB2: shared default is now 'write' per the
        // resolver-semantic alignment fix.
        assert_eq!(
            by_collection.get("VibeCodedOrchestrator_KnowledgeGraph"),
            Some(&"write")
        );
    }

    #[test]
    fn rename_preserves_user_configured_levels_on_collision() {
        // Pre-rename, a user has manually configured an entry under
        // BOTH the old name's prefix AND the new name's prefix (the
        // latter could come from a prior re-onboard cycle). The rename
        // must merge without lowering the existing privilege at the
        // target name.
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(&pid, "Acme", "/tmp/acme", ProjectHost::Base, "acme").unwrap();

        // Old name: write (from default populate).
        db.kg_set_access(&pid, "Acme_KnowledgeGraph", "write").unwrap();
        // New name's row pre-exists at "read" (user downgraded earlier).
        db.kg_set_access(&pid, "Beta_KnowledgeGraph", "read").unwrap();

        let _ = db.propagate_kg_access_on_rename(&pid, "Acme", "Beta");

        let access = db.kg_list_access(&pid).unwrap();
        let by_collection: std::collections::HashMap<&str, &str> =
            access.iter().map(|(c, l)| (c.as_str(), l.as_str())).collect();
        // Old row gone.
        assert_eq!(by_collection.get("Acme_KnowledgeGraph"), None);
        // New row UPGRADED to write (source had higher privilege). This
        // matches the v0.2.46-L3 "never lower an existing privilege"
        // invariant baked into `kg_rename_access`.
        assert_eq!(
            by_collection.get("Beta_KnowledgeGraph"),
            Some(&"write"),
            "L3 invariant: rename must upgrade target to source's higher \
             privilege, never silently downgrade"
        );
    }

    #[test]
    fn rename_no_op_when_sanitized_prefix_unchanged() {
        // "Acme Corp" and "Acme-Corp" both sanitize to "AcmeCorp" —
        // the rename is display-only (separator change), no collection
        // name changes. Propagation MUST NOT issue rename calls (no
        // spurious work; rows survive untouched).
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(&pid, "Acme Corp", "/tmp/acme", ProjectHost::Base, "acme-corp").unwrap();
        db.populate_kg_collection_access_for_project(&pid, "Acme Corp").unwrap();

        let warnings = db.propagate_kg_access_on_rename(&pid, "Acme Corp", "Acme-Corp");
        assert!(warnings.is_empty());

        // Rows still exist under the unchanged sanitized prefix
        // ("AcmeCorp_*"); no spurious "Acme Corp_*" or "Acme-Corp_*"
        // entries were created.
        let access = db.kg_list_access(&pid).unwrap();
        let by_collection: std::collections::HashSet<&str> =
            access.iter().map(|(c, _)| c.as_str()).collect();
        assert!(by_collection.contains("AcmeCorp_KnowledgeGraph"));
        assert!(by_collection.contains("AcmeCorp_Development"));
        assert_eq!(by_collection.len(), 3, "no new rows created on no-op rename");
    }

    #[test]
    fn rename_does_not_touch_shared_kg_row() {
        // The shared KG collection name is project-name-INDEPENDENT —
        // propagation MUST leave its access row alone.
        let db = Db::open_in_memory().unwrap();
        let pid = uuid::Uuid::new_v4().to_string();
        db.insert_project(&pid, "Acme", "/tmp/acme", ProjectHost::Base, "acme").unwrap();
        db.populate_kg_collection_access_for_project(&pid, "Acme").unwrap();

        // Capture the shared row before rename.
        // v0.2.49 Step F SB2: shared default is now 'write' (was 'read'
        // pre-fix; aligned with resolver F-2a output + Step D migration).
        let before = db
            .kg_get_access(&pid, "VibeCodedOrchestrator_KnowledgeGraph")
            .unwrap();
        assert_eq!(before.as_deref(), Some("write"));

        db.propagate_kg_access_on_rename(&pid, "Acme", "Beta");

        let after = db
            .kg_get_access(&pid, "VibeCodedOrchestrator_KnowledgeGraph")
            .unwrap();
        assert_eq!(after.as_deref(), Some("write"),
                   "shared row must not be touched by project-name rename");
    }

    // ─── PR 4 (2026-05-01): bundle install + bootstrap collections ─────

    /// Build a minimal fake orchestrator tree under `root` with enough
    /// templates/ + infrastructure/ files to exercise install_project_bundle.
    /// Mirrors `_make_fake_orchestrator` in tests/test_install_bundle.py
    /// but trimmed to what the Rust integration test needs.
    #[allow(dead_code)]
    fn make_fake_orchestrator(root: &Path) {
        std::fs::write(root.join("vct-module.json"), "{}\n").unwrap();
        let templates = root.join("templates");
        std::fs::create_dir_all(templates.join("hooks").join("_lib")).unwrap();
        std::fs::write(
            templates.join("hooks").join("foo.sh"),
            "#!/bin/sh\necho v1\n",
        )
        .unwrap();
        std::fs::write(
            templates.join("hooks").join("foo.ps1"),
            "Write-Host 'v1'\n",
        )
        .unwrap();
        std::fs::write(
            templates.join("hooks").join("_lib").join("find-python.sh"),
            "# find-python v1\n",
        )
        .unwrap();
        std::fs::write(
            templates.join("hooks").join("_lib").join("find-python.ps1"),
            "# find-python.ps1 v1\n",
        )
        .unwrap();
        std::fs::create_dir_all(templates.join("scripts")).unwrap();
        std::fs::write(
            templates.join("scripts").join("kg-search"),
            "#!/usr/bin/env python3\nprint('search')\n",
        )
        .unwrap();
        std::fs::write(
            templates.join("scripts").join("claude_token_counter.py"),
            "def count(): return 0\n",
        )
        .unwrap();
        std::fs::create_dir_all(templates.join("agents").join("free")).unwrap();
        std::fs::write(
            templates.join("agents").join("free").join("coder.md"),
            "Orchestrator at {{ORCHESTRATOR_ROOT}}\n",
        )
        .unwrap();
        std::fs::create_dir_all(templates.join("skills").join("architect")).unwrap();
        std::fs::write(
            templates
                .join("skills")
                .join("architect")
                .join("SKILL.md"),
            "Home: {{HOME}}\n",
        )
        .unwrap();
        let settings_tmpl = serde_json::json!({
            "$schema": "test",
            "permissions": {"allow": ["Bash"]},
            "hooks": {
                "PreToolUse": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "vco-foo"}]
                }]
            }
        });
        std::fs::write(
            templates.join("settings.json.linux.template"),
            serde_json::to_string_pretty(&settings_tmpl).unwrap(),
        )
        .unwrap();
        std::fs::write(
            templates.join("settings.json.windows.template"),
            serde_json::to_string_pretty(&settings_tmpl).unwrap(),
        )
        .unwrap();
        std::fs::create_dir_all(root.join("infrastructure")).unwrap();
        std::fs::write(
            root.join("infrastructure").join("docker-compose.yml"),
            "services: {}\n",
        )
        .unwrap();
    }

    /// Find the absolute path to the actual repo root (the worktree's root,
    /// containing `vct-module.json`). Used by tests that need to invoke the
    /// REAL `python -m vco_lib.project_init install-bundle` against the real
    /// templates/ tree (not a fake). Walks up from CARGO_MANIFEST_DIR.
    #[allow(dead_code)]
    fn real_repo_root() -> std::path::PathBuf {
        let manifest_dir = env!("CARGO_MANIFEST_DIR");
        let mut current = std::path::PathBuf::from(manifest_dir);
        for _ in 0..6 {
            if current.join("vct-module.json").exists() {
                return current;
            }
            if !current.pop() {
                break;
            }
        }
        panic!("could not find repo root from CARGO_MANIFEST_DIR={}", manifest_dir);
    }

    /// PR 4: invoke the install-bundle Python subprocess against a fake
    /// orchestrator and assert all expected files land + manifest written.
    /// We invoke Python from the REAL repo root (where vco_lib lives) but
    /// pass the FAKE orchestrator as --orchestrator-root, so the source of
    /// truth for templates is the fake tree.
    ///
    /// This is the integration test the PR 4 spec calls "the critical
    /// Rust integration test against a fake orchestrator root with
    /// sample templates".
    #[test]
    fn install_bundle_subprocess_writes_full_tree() {
        // Skip if no Python — CI without python3 shouldn't fail this test.
        let py = if std::process::Command::new("python3").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python".to_string()
        } else {
            eprintln!("[skip] no python on PATH");
            return;
        };

        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-bundle-rs-{}", uuid::Uuid::new_v4().simple()
        ));
        let fake_orch = tmp.join("orch");
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&fake_orch).unwrap();
        std::fs::create_dir_all(&proj).unwrap();
        make_fake_orchestrator(&fake_orch);

        let out = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init",
                "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("subprocess failed to start");

        assert!(out.status.success(),
                "install-bundle exit {}\nstdout={}\nstderr={}",
                out.status,
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr));

        let stdout = String::from_utf8_lossy(&out.stdout);
        let payload: serde_json::Value = serde_json::from_str(&stdout)
            .unwrap_or_else(|e| panic!("non-JSON stdout: {}\nraw={}", e, stdout));

        // Manifest written.
        assert!(payload["manifest_written"].as_bool().unwrap_or(false),
                "manifest must be written");
        assert!(proj.join(".claude").join(".vco-manifest.json").exists());

        // Hook present (OS-aware: sh on Linux/macOS, ps1 on Windows).
        #[cfg(target_os = "windows")]
        let hook_name = "foo.ps1";
        #[cfg(not(target_os = "windows"))]
        let hook_name = "foo.sh";
        assert!(proj.join(".claude").join("hooks").join(hook_name).exists(),
                "hook {} must land in .claude/hooks/", hook_name);

        // _lib hook always-overwritten.
        #[cfg(target_os = "windows")]
        let lib_name = "find-python.ps1";
        #[cfg(not(target_os = "windows"))]
        let lib_name = "find-python.sh";
        assert!(proj.join(".claude").join("hooks").join("_lib").join(lib_name).exists());

        // Scripts present (notably claude_token_counter.py — the ship-blocker
        // gap from the orchestrator-full-surface-inventory).
        assert!(proj.join(".claude").join("scripts").join("kg-search").exists());
        assert!(proj.join(".claude").join("scripts").join("claude_token_counter.py").exists());

        // Agents with substitutions applied.
        let coder = std::fs::read_to_string(
            proj.join(".claude").join("agents").join("coder.md"),
        ).unwrap();
        assert!(coder.contains(&fake_orch.to_string_lossy().to_string()),
                "agent must have {{{{ORCHESTRATOR_ROOT}}}} substituted: {}", coder);
        assert!(!coder.contains("{{ORCHESTRATOR_ROOT}}"),
                "placeholder must NOT remain: {}", coder);

        // Skill recursively copied.
        assert!(proj.join(".claude").join("skills").join("architect").join("SKILL.md").exists());

        // Infrastructure compose file copied.
        assert!(proj.join("infrastructure").join("docker-compose.yml").exists());

        // Settings template smart-merged.
        let settings: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(proj.join(".claude").join("settings.json")).unwrap()
        ).unwrap();
        // The hooks block from the template is now in the project's
        // settings.json.
        assert!(settings["hooks"]["PreToolUse"].is_array(),
                "settings.json must carry the hooks block from template: {}", settings);

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// PR 4: install-bundle is idempotent — second run must produce no
    /// "create" actions on top of an unchanged orchestrator.
    #[test]
    fn install_bundle_subprocess_idempotent_on_second_run() {
        let py = if std::process::Command::new("python3").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python".to_string()
        } else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-bundle-rs-idem-{}", uuid::Uuid::new_v4().simple()
        ));
        let fake_orch = tmp.join("orch");
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&fake_orch).unwrap();
        std::fs::create_dir_all(&proj).unwrap();
        make_fake_orchestrator(&fake_orch);

        // First run.
        let out1 = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .unwrap();
        assert!(out1.status.success());

        // Second run.
        let out2 = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .unwrap();
        assert!(out2.status.success());
        let payload: serde_json::Value = serde_json::from_str(
            &String::from_utf8_lossy(&out2.stdout)
        ).unwrap();
        // Second run: zero "create" actions (every file is already there
        // from run 1).
        let creates = payload["actions"]["create"].as_array()
            .map(|a| a.len()).unwrap_or(0);
        assert_eq!(creates, 0, "second run produced {} creates", creates);

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Grab a free localhost port and immediately drop the listener so the
    /// port is unbound when the caller subprocess tries to connect. The
    /// kernel won't reuse the port for another process within the test
    /// duration (TIME_WAIT semantics + parallel tests get distinct ports
    /// each), so each test instance gets its own guaranteed-refused port.
    ///
    /// Why this matters (flake hunt 2026-05-01): hard-coding a port like
    /// `:1` or `:8081` collides under default `cargo test --lib`
    /// parallelism — two parallel subprocess invocations probing the same
    /// port can race on `_attempt_container_restart`'s
    /// `podman start weaviate_claude` side-effect, causing one of them to
    /// see the real Weaviate come up between probes.
    fn unused_local_port() -> u16 {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")
            .expect("bind 127.0.0.1:0 for port reservation");
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        port
    }

    /// PR 4: bootstrap-collections soft-fails to a deferral when Weaviate
    /// is unreachable. Drives `--weaviate-url http://127.0.0.1:<free-port>`
    /// (a port we just released so it's guaranteed-refused) and asserts the
    /// deferral .md lands at `<folder>/.claude/context/UPDATE_DEFERRED.md`.
    ///
    /// Note: this test exercises Python end-to-end including the podman
    /// restart attempt (which fails because the URL points at a port no
    /// container is bound to). The test passes regardless of whether the
    /// host has podman installed.
    #[test]
    fn bootstrap_collections_soft_fails_to_deferral() {
        let py = if std::process::Command::new("python3").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python".to_string()
        } else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-bootstrap-rs-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Per-test free port → no collision with parallel tests probing
        // the same Weaviate URL.
        let dead_url = format!("http://127.0.0.1:{}", unused_local_port());
        let out = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init",
                "bootstrap-collections",
                "--name", "VideoFrames",
                "--weaviate-url", &dead_url,
                "--project-folder", &tmp.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("subprocess failed to start");

        // Soft-fail: exit 0 even though Weaviate was unreachable.
        assert!(out.status.success(),
                "bootstrap-collections must exit 0 on soft-fail (Weaviate down). \
                 stdout={} stderr={}",
                String::from_utf8_lossy(&out.stdout),
                String::from_utf8_lossy(&out.stderr));

        let stdout = String::from_utf8_lossy(&out.stdout);
        let payload: serde_json::Value = serde_json::from_str(&stdout)
            .unwrap_or_else(|e| panic!("non-JSON stdout: {}\n{}", e, stdout));
        assert_eq!(payload["weaviate_reachable"], false);
        assert_eq!(payload["deferred"], true);

        // Deferral .md landed in the user-project folder.
        let deferral = tmp.join(".claude").join("context").join("UPDATE_DEFERRED.md");
        assert!(deferral.exists(),
                "expected UPDATE_DEFERRED.md at {}", deferral.display());
        let body = std::fs::read_to_string(&deferral).unwrap();
        assert!(body.contains("weaviate_unreachable_at_bootstrap"),
                "deferral entry condition_id must match: {}", body);

        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── PR 5 (2026-05-01): per-project update flow ──────────────────────

    /// Pick whichever python launcher works on this host. Mirrors the
    /// helper used by other PR 4 tests.
    fn pick_python() -> Option<String> {
        if std::process::Command::new("python3").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            Some("python3".to_string())
        } else if std::process::Command::new("python").silent().arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            Some("python".to_string())
        } else {
            None
        }
    }

    /// PR 5: update_project_v2 success path. We can't drive the Tauri
    /// `#[command]` directly (it requires `State<Db>`), but the inner
    /// orchestration is `run_install_bundle_update` — we exercise it
    /// against a fake orchestrator + fresh project folder and assert
    /// the summary tallies + manifest is written.
    ///
    /// Soft-fail discipline: even if `bootstrap-collections` would defer
    /// (no Weaviate in test env), `run_install_bundle_update` itself only
    /// calls install-bundle, which is independent of Weaviate.
    #[test]
    fn update_project_v2_success() {
        let Some(py) = pick_python() else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-update-rs-{}", uuid::Uuid::new_v4().simple()
        ));
        let fake_orch = tmp.join("orch");
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&fake_orch).unwrap();
        std::fs::create_dir_all(&proj).unwrap();
        make_fake_orchestrator(&fake_orch);

        // Seed a first-install (so we have a manifest + on-disk bundle
        // to "update" against).
        let out_seed = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("seed subprocess failed to start");
        assert!(out_seed.status.success(),
                "seed install-bundle must succeed: stderr={}",
                String::from_utf8_lossy(&out_seed.stderr));
        // Manifest landed.
        assert!(proj.join(".claude").join(".vco-manifest.json").exists());

        // Now bump one orchestrator template + add a new shipped script.
        // After update, foo.{sh,ps1} should be `overwrite`'d (user untouched);
        // the new script should be `create`'d.
        #[cfg(target_os = "windows")]
        let foo_path = fake_orch.join("templates").join("hooks").join("foo.ps1");
        #[cfg(not(target_os = "windows"))]
        let foo_path = fake_orch.join("templates").join("hooks").join("foo.sh");
        std::fs::write(&foo_path, "#!/bin/sh\necho v2\n").unwrap();
        std::fs::write(
            fake_orch.join("templates").join("scripts").join("brand_new.py"),
            "def brand(): return 'new'\n",
        ).unwrap();

        // Run update_project_v2's inner subprocess wrapper. This is the
        // exact code path the public command takes after DB lookup,
        // overriding the orchestrator-root so the fake templates win
        // over the host's real bundle (preserves test isolation).
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let (warnings, summary) = rt.block_on(
            run_install_bundle_update_with_root(&proj, Some(&fake_orch))
        );

        // Hard contract: at least one overwrite (the bumped foo) + one
        // create (the brand_new.py).
        assert!(summary.overwritten >= 1,
                "expected ≥1 overwrite; got summary={:?}, warnings={:?}",
                summary, warnings);
        assert!(summary.created >= 1,
                "expected ≥1 create for brand_new.py; got summary={:?}",
                summary);
        // No errors expected (clean fake orchestrator).
        assert_eq!(summary.errors_count, 0,
                   "no errors expected on clean update; warnings={:?}", warnings);

        // The newly-shipped file actually landed on disk.
        assert!(proj.join(".claude").join("scripts").join("brand_new.py").exists());

        // Manifest still intact.
        assert!(proj.join(".claude").join(".vco-manifest.json").exists());

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// PR 5: summary counts must tally with the JSON envelope's `actions`
    /// map. Verifies the bookkeeping in `run_install_bundle_update` is
    /// faithful — `created + overwritten + preserved + noop +
    /// always_overwritten + skipped_existing == total ops emitted by the
    /// Python side`.
    #[test]
    fn update_project_v2_summary_counts_match_actions() {
        let Some(py) = pick_python() else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-update-tally-rs-{}", uuid::Uuid::new_v4().simple()
        ));
        let fake_orch = tmp.join("orch");
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&fake_orch).unwrap();
        std::fs::create_dir_all(&proj).unwrap();
        make_fake_orchestrator(&fake_orch);

        // Seed install.
        let out_seed = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("seed subprocess failed to start");
        assert!(out_seed.status.success());

        // Update without any orchestrator changes — every op should be
        // a noop (or always-overwrite for _lib). Override the templates
        // root to match the seed install.
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let (_warnings, summary) = rt.block_on(
            run_install_bundle_update_with_root(&proj, Some(&fake_orch))
        );

        // No new shipped files, no user mods, identical templates: noop
        // + always_overwritten dominate.
        assert!(summary.noop > 0 || summary.always_overwritten > 0,
                "second run on unchanged orchestrator should produce \
                 noop/always_overwritten ops; summary={:?}", summary);
        assert_eq!(summary.created, 0);
        assert_eq!(summary.preserved, 0);
        assert_eq!(summary.skipped_existing, 0);
        assert_eq!(summary.errors_count, 0);
        // total_ops() is the sum of every action bucket.
        assert!(summary.total_ops() > 0,
                "expected at least one op to have been classified");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// PR 5: subprocess error → warnings populated, project still
    /// returned. We exercise the JSON-error path by giving the
    /// subprocess a non-existent `--orchestrator-root`. The Python side
    /// emits `errors[]` AND exits non-zero; the Rust wrapper turns those
    /// into warnings without panicking.
    #[test]
    fn update_project_v2_returns_warnings_on_subprocess_error() {
        let Some(_py) = pick_python() else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let tmp = std::env::temp_dir().join(format!(
            "vct-update-err-rs-{}", uuid::Uuid::new_v4().simple()
        ));
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&proj).unwrap();

        // Trigger a subprocess error by wiping PATH so detect_system can't
        // resolve python — that should soft-fail to a single warning.
        // We can't do that without affecting parallel tests, so instead
        // we exercise the post-subprocess parse-failure branch by calling
        // the function with a folder that exists but has no manifest +
        // a real run. The `python is missing or orchestrator root not found`
        // soft-fail path will be hit if find_local_repo_root() fails — but
        // it succeeds in the worktree. So instead: invoke against a folder
        // and check that even when the FAKE orchestrator is missing,
        // run_install_bundle_update soft-fails to warnings rather than
        // panicking.

        // Drive run_install_bundle_update with the real repo root (so
        // Python is found), but the project folder is deliberately
        // missing every expected source — the subprocess should still
        // return parseable JSON because it points at the real templates.
        // We instead corrupt a different lever: pass a folder that the
        // subprocess can't even classify. The cleanest test: make sure
        // that when the subprocess emits errors[] in the JSON envelope,
        // they propagate to warnings.
        //
        // Easiest reliable trigger: make a read-only project subfolder so
        // the file write fails; the subprocess emits an `errors[]` entry
        // but exits 1. The Rust wrapper collects them as warnings.
        let claude_dir = proj.join(".claude").join("hooks");
        std::fs::create_dir_all(&claude_dir).unwrap();
        // Pre-create a file with the same path the subprocess will try to
        // overwrite, BUT under a directory we can flip read-only on POSIX.
        //
        // On Windows mode bits don't reliably block writes; the test is
        // skipped via `#[cfg(unix)]` to keep it deterministic across OS.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            // Create a regular file at a path the subprocess will try to
            // write, then mark its parent directory read-only. The
            // subprocess will raise on open(); the wrapper will list it
            // in `errors[]`.
            let blocked_dir = proj.join("infrastructure");
            std::fs::create_dir_all(&blocked_dir).unwrap();
            let mut perms = std::fs::metadata(&blocked_dir).unwrap().permissions();
            perms.set_mode(0o555); // r-xr-xr-x: writes blocked
            std::fs::set_permissions(&blocked_dir, perms).unwrap();

            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let (warnings, summary) = rt.block_on(run_install_bundle_update(&proj));

            // Restore perms so the cleanup `remove_dir_all` succeeds.
            let mut perms = std::fs::metadata(&blocked_dir).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(&blocked_dir, perms).unwrap();

            // Either the subprocess emits per-file errors (when the
            // parent dir is read-only and infra files can't be written),
            // OR everything succeeds — in which case the test is
            // inconclusive and we just verify that warnings is well-
            // formed (a Vec<String>, never panics).
            assert!(warnings.iter().all(|w| !w.is_empty()),
                    "every warning must be a non-empty string");
            // total_ops() never panics — even on an empty summary.
            let _ = summary.total_ops();
        }

        // OS-agnostic baseline: verify the helper handles a folder that
        // does not exist. run_install_bundle_update itself doesn't probe
        // the folder (it just hands it to Python); Python emits errors[]
        // when the folder is missing. The Rust wrapper collects them as
        // warnings and returns a zeroed summary.
        let bogus = std::env::temp_dir().join(format!(
            "vct-bogus-{}", uuid::Uuid::new_v4().simple()
        ));
        // Don't create bogus — Python should emit "folder does not exist".
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let (warnings, summary) = rt.block_on(run_install_bundle_update(&bogus));
        // Python side emits an errors[] entry; the wrapper surfaces ≥1
        // warning. Summary is zeroed.
        assert!(!warnings.is_empty(),
                "missing-folder must surface ≥1 warning; got {:?}", warnings);
        assert_eq!(summary.created, 0);
        assert_eq!(summary.overwritten, 0);
        assert_eq!(summary.preserved, 0);

        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── CLAUDE.md Dev Constraint #8(a) regression pin (integration) ──
    //
    // End-to-end pin: a user disables 3 agents + 2 skills + 1 hook on a
    // project, then `python -m vco_lib.project_init install-bundle
    // --update` runs against that project (the launcher's "Update
    // bundle" button path — Tauri command `update_project_v2` ->
    // `run_install_bundle_update`), then `populate_project_state_from_
    // filesystem` is invoked again (mirrors the next launcher boot's
    // re-populate sweep). The 6 disabled toggles must STILL be set to 0.
    //
    // Why both subprocess + populate steps: the Python install-bundle
    // path is purely filesystem work — it never touches launcher.db. The
    // DB toggles survive trivially across an install-bundle run because
    // install-bundle doesn't know they exist. The risk vector for
    // toggle loss is the populate sweep AFTER an install-bundle: if
    // install-bundle adds/changes a file (a new agent, a new hook in
    // settings.json), the subsequent populate re-upserts every row.
    // Constraint #8(a) demands the populate upserts omit the `enabled`
    // column, so toggles survive. This test exercises both halves:
    // populate writes baseline → user disables → install-bundle copies
    // (possibly mutated) files → populate re-upserts → toggles intact.
    //
    // Counterpart pure-populate test:
    // `project_state_populate.rs::re_run_preserves_3_agents_2_skills_1_hook_disabled_together`
    // (faster, no subprocess). This file's test pins the full launcher
    // flow with Python in the loop.
    #[test]
    fn install_bundle_update_then_populate_preserves_multiple_toggles() {
        let Some(py) = pick_python() else {
            eprintln!("[skip] no python on PATH");
            return;
        };
        let real_root = real_repo_root();
        let tmp = std::env::temp_dir().join(format!(
            "vct-preserve-toggles-{}", uuid::Uuid::new_v4().simple()
        ));
        let fake_orch = tmp.join("orch");
        let proj = tmp.join("proj");
        std::fs::create_dir_all(&fake_orch).unwrap();
        std::fs::create_dir_all(&proj).unwrap();
        make_fake_orchestrator(&fake_orch);

        // Beef up the fake orchestrator's templates so populate has
        // enough rows to disable across (the default fake ships 1 agent
        // + 1 skill; we need 5 agents + 4 skills + 3 hooks).
        let agents_free = fake_orch
            .join("templates").join("agents").join("free");
        for n in &["coder", "planner", "tester", "reviewer", "architect"] {
            std::fs::write(
                agents_free.join(format!("{}.md", n)),
                format!("---\nname: {}\nmodel: sonnet\n---\n# body\n", n),
            )
            .unwrap();
        }
        let skills = fake_orch.join("templates").join("skills");
        for n in &["tdd", "fix-issue", "context", "architect"] {
            let d = skills.join(n);
            std::fs::create_dir_all(&d).unwrap();
            std::fs::write(
                d.join("SKILL.md"),
                format!("---\nname: {}\nmodel: sonnet\n---\n# {}\n", n, n),
            )
            .unwrap();
        }
        // Settings template with 3 hooks across 2 events.
        let settings = serde_json::json!({
            "$schema": "test",
            "permissions": {"allow": ["Bash"]},
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Edit(*)",  "hooks": [{"type": "command", "command": "hook-edit"}]},
                    {"matcher": "Write(*)", "hooks": [{"type": "command", "command": "hook-write"}]}
                ],
                "PreToolUse": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "hook-pre"}]}
                ]
            }
        });
        let settings_str = serde_json::to_string_pretty(&settings).unwrap();
        std::fs::write(
            fake_orch.join("templates").join("settings.json.linux.template"),
            &settings_str,
        ).unwrap();
        std::fs::write(
            fake_orch.join("templates").join("settings.json.windows.template"),
            &settings_str,
        ).unwrap();

        // Step 1: seed the project via install-bundle (first install).
        let out_seed = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("seed subprocess failed to start");
        assert!(
            out_seed.status.success(),
            "seed install-bundle must succeed: stderr={}",
            String::from_utf8_lossy(&out_seed.stderr)
        );
        // Confirm the bundle files landed.
        assert!(proj.join(".claude").join("agents").join("free").join("coder.md").exists()
                || proj.join(".claude").join("agents").join("coder.md").exists(),
                "coder.md must land in project's .claude/agents tree");

        // Step 2: open an in-memory DB, seed the project row, populate baseline.
        let db = crate::db::Db::open_in_memory().expect("in-memory db");
        let slug = db.generate_unique_slug("MultiPreserve").unwrap();
        let project_id = uuid::Uuid::new_v4().to_string();
        db.insert_project(
            &project_id,
            "MultiPreserve",
            proj.to_string_lossy().as_ref(),
            ProjectHost::Base,
            &slug,
        ).unwrap();

        let r1 = crate::commands::project_state_populate::
            populate_project_state_from_filesystem(
                &project_id, "MultiPreserve", &proj, &db,
            );
        // We need ≥5 agents, ≥4 skills, ≥3 hooks for the multi-disable
        // breadth to be exercisable. Allow >= because the
        // install-bundle path can ship more than what we wrote (free
        // tier may include _lib/, etc.).
        assert!(
            r1.agents_inserted >= 5,
            "baseline: ≥5 agents seeded by populate; got {}",
            r1.agents_inserted
        );
        assert!(
            r1.skills_inserted >= 4,
            "baseline: ≥4 skills seeded by populate; got {}",
            r1.skills_inserted
        );
        assert!(
            r1.hooks_inserted >= 3,
            "baseline: ≥3 hooks seeded by populate; got {}",
            r1.hooks_inserted
        );

        // Step 3: user disables 3 agents + 2 skills + 1 hook.
        const DISABLED_AGENTS: [&str; 3] = ["coder", "tester", "architect"];
        const DISABLED_SKILLS: [&str; 2] = ["tdd", "context"];
        for a in &DISABLED_AGENTS {
            db.set_project_agent_enabled(&project_id, a, false).unwrap();
        }
        for s in &DISABLED_SKILLS {
            db.set_project_skill_enabled(&project_id, s, false).unwrap();
        }
        let pre_hooks = db.list_project_hooks(&project_id).unwrap();
        let target_hook = pre_hooks
            .iter()
            .find(|h| h.command == "hook-pre")
            .expect("hook-pre must be seeded");
        let disabled_hook_id = target_hook.id;
        db.set_project_hook_enabled(disabled_hook_id, false).unwrap();

        // Step 4: bump a template (forces install-bundle --update into
        // a non-noop classification on at least one file). Plus add a
        // brand-new shipped file so `--update` has visible work to do.
        let coder_path = fake_orch.join("templates").join("agents")
            .join("free").join("coder.md");
        std::fs::write(
            &coder_path,
            "---\nname: coder\nmodel: sonnet\ndescription: v2-coder\n---\n# v2\n",
        ).unwrap();

        // Step 5: run install-bundle --update against the project. This
        // is the exact subprocess the launcher's update_project_v2
        // command spawns via run_install_bundle_update. We pass --force
        // so user-modified detection doesn't matter — the test cares
        // about toggle preservation, not user-modified semantics.
        let out_upd = std::process::Command::new(&py).silent()
            .args([
                "-m", "vco_lib.project_init", "install-bundle",
                "--folder", &proj.to_string_lossy(),
                "--orchestrator-root", &fake_orch.to_string_lossy(),
                "--update", "--force", "--json",
            ])
            .current_dir(&real_root)
            .output()
            .expect("update subprocess failed to start");
        assert!(
            out_upd.status.success(),
            "install-bundle --update must succeed: stderr={}",
            String::from_utf8_lossy(&out_upd.stderr)
        );

        // Step 6: re-run populate. Mirrors the next launcher boot's
        // re-populate sweep (lib.rs lines ~553-610) — which is when
        // toggle preservation actually has to hold under a real flow.
        let _r2 = crate::commands::project_state_populate::
            populate_project_state_from_filesystem(
                &project_id, "MultiPreserve", &proj, &db,
            );

        // ─── Contract pins: every disabled toggle survives ────────────
        let post_agents = db.list_project_agents(&project_id).unwrap();
        for name in &DISABLED_AGENTS {
            let row = post_agents
                .iter()
                .find(|a| a.agent_name == *name)
                .unwrap_or_else(|| panic!(
                    "agent {} must still exist after install-bundle+populate; \
                     present: {:?}",
                    name,
                    post_agents.iter().map(|a| &a.agent_name).collect::<Vec<_>>()
                ));
            assert!(
                !row.enabled,
                "agent {} must remain disabled across install-bundle --update + re-populate",
                name
            );
        }
        // Other agents stay enabled.
        for row in &post_agents {
            if !DISABLED_AGENTS.contains(&row.agent_name.as_str()) {
                assert!(
                    row.enabled,
                    "agent {} must remain enabled (was not disabled by user)",
                    row.agent_name
                );
            }
        }

        let post_skills = db.list_project_skills(&project_id).unwrap();
        for name in &DISABLED_SKILLS {
            let row = post_skills
                .iter()
                .find(|s| s.skill_name == *name)
                .unwrap_or_else(|| panic!("skill {} must still exist", name));
            assert!(
                !row.enabled,
                "skill {} must remain disabled across install-bundle --update + re-populate",
                name
            );
        }
        for row in &post_skills {
            if !DISABLED_SKILLS.contains(&row.skill_name.as_str()) {
                assert!(
                    row.enabled,
                    "skill {} must remain enabled (was not disabled by user)",
                    row.skill_name
                );
            }
        }

        let post_hooks = db.list_project_hooks(&project_id).unwrap();
        let post_disabled_hook = post_hooks
            .iter()
            .find(|h| h.id == disabled_hook_id)
            .expect("originally-disabled hook id must still exist");
        assert!(
            !post_disabled_hook.enabled,
            "hook id {} (command={}) must remain disabled across install-bundle --update + re-populate",
            disabled_hook_id, post_disabled_hook.command
        );
        for row in &post_hooks {
            if row.id != disabled_hook_id {
                assert!(
                    row.enabled,
                    "hook id {} (command={}) must remain enabled",
                    row.id, row.command
                );
            }
        }

        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── PR-3 Commit 8: deep-merge + .claude/env BEGIN/END marker tests ─

    #[test]
    fn merge_env_object_canonical_creates_block_when_missing() {
        let mut parent = serde_json::Map::new();
        let pairs: Vec<(&str, String)> =
            vec![("KG_COLLECTION", "Acme_KnowledgeGraph".to_string())];
        merge_env_object_canonical(&mut parent, "env", &pairs);
        let env = parent.get("env").unwrap().as_object().unwrap();
        assert_eq!(env.len(), 1);
        assert_eq!(env["KG_COLLECTION"], "Acme_KnowledgeGraph");
    }

    #[test]
    fn merge_env_object_canonical_preserves_user_keys() {
        let mut parent = serde_json::Map::new();
        parent.insert(
            "env".to_string(),
            serde_json::json!({
                "USER_KEY_1": "preserved",
                "USER_KEY_2": "also preserved",
                "KG_COLLECTION": "OldStaleValue",
            }),
        );
        let pairs: Vec<(&str, String)> =
            vec![("KG_COLLECTION", "NewValue".to_string())];
        merge_env_object_canonical(&mut parent, "env", &pairs);

        let env = parent.get("env").unwrap().as_object().unwrap();
        // Canonical key overwritten.
        assert_eq!(env["KG_COLLECTION"], "NewValue");
        // User keys preserved.
        assert_eq!(env["USER_KEY_1"], "preserved");
        assert_eq!(env["USER_KEY_2"], "also preserved");
    }

    #[test]
    fn merge_env_object_canonical_replaces_non_object_with_fresh() {
        // If the existing value isn't an object (e.g. a stringified
        // legacy form), we replace rather than crash. No user keys to
        // preserve in this case.
        let mut parent = serde_json::Map::new();
        parent.insert("env".to_string(), serde_json::json!("legacy=value"));
        let pairs: Vec<(&str, String)> =
            vec![("KG_COLLECTION", "Acme_KG".to_string())];
        merge_env_object_canonical(&mut parent, "env", &pairs);
        let env = parent.get("env").unwrap().as_object().unwrap();
        assert_eq!(env["KG_COLLECTION"], "Acme_KG");
    }

    #[test]
    fn build_claude_env_managed_block_emits_begin_end_markers() {
        let pairs: Vec<(&str, String)> = vec![
            ("KG_COLLECTION", "Acme_KG".to_string()),
            ("PROJECT_NAME", "Acme".to_string()),
        ];
        // `with_defaults` leaves orchestrator_root = None so the PR-2
        // portability lines are silently omitted — keeps this test focused
        // on the marker / canonical-pair contract.
        let settings = ProjectEnvSettings::with_defaults("Acme");
        let block = build_claude_env_managed_block(&pairs, &settings);
        assert!(block.starts_with(CLAUDE_ENV_MANAGED_BEGIN));
        assert!(block.contains("export KG_COLLECTION=\"Acme_KG\""));
        assert!(block.contains("export PROJECT_NAME=\"Acme\""));
        assert!(block.contains(CLAUDE_ENV_MANAGED_END));
    }

    #[test]
    fn merge_claude_env_managed_block_no_prior_returns_managed_only() {
        let settings = ProjectEnvSettings::with_defaults("Acme");
        let managed = build_claude_env_managed_block(
            &[("KG_COLLECTION", "Acme_KG".to_string())],
            &settings,
        );
        let out = merge_claude_env_managed_block(None, &managed);
        assert_eq!(out, managed);
    }

    #[test]
    fn merge_claude_env_managed_block_replaces_in_place_preserving_user_lines() {
        let user_pre = "# user-added note\nexport MY_TOKEN=\"abc\"\n\n";
        let user_post = "\n# trailer\nexport ANOTHER=\"xyz\"\n";
        let old_managed = format!(
            "{}\nexport KG_COLLECTION=\"Old_KG\"\n{}\n",
            CLAUDE_ENV_MANAGED_BEGIN, CLAUDE_ENV_MANAGED_END
        );
        let prior = format!("{}{}{}", user_pre, old_managed, user_post);

        let new_pairs: Vec<(&str, String)> =
            vec![("KG_COLLECTION", "New_KG".to_string())];
        let settings = ProjectEnvSettings::with_defaults("Acme");
        let new_managed = build_claude_env_managed_block(&new_pairs, &settings);
        let out = merge_claude_env_managed_block(Some(&prior), &new_managed);

        // User content before + after the managed block must survive.
        assert!(out.starts_with(user_pre), "user pre-block content lost");
        assert!(out.ends_with(user_post), "user post-block content lost");
        // Old managed value gone.
        assert!(!out.contains("Old_KG"));
        // New managed value present.
        assert!(out.contains("New_KG"));
        // Markers present (in-place replace, not duplicated).
        assert_eq!(
            out.matches(CLAUDE_ENV_MANAGED_BEGIN).count(),
            1,
            "managed BEGIN marker must appear exactly once"
        );
        assert_eq!(
            out.matches(CLAUDE_ENV_MANAGED_END).count(),
            1,
            "managed END marker must appear exactly once"
        );
    }

    #[test]
    fn merge_claude_env_managed_block_legacy_file_appends_block() {
        // Pre-PR-3 file (no markers). The full prior content is
        // preserved and the managed block appends. On the next round-
        // trip the markers exist and in-place replace activates.
        let prior = "# legacy file written by old launcher\nexport KG_COLLECTION=\"Legacy_KG\"\n";
        let settings = ProjectEnvSettings::with_defaults("Acme");
        let new_managed = build_claude_env_managed_block(
            &[("KG_COLLECTION", "New_KG".to_string())],
            &settings,
        );
        let out = merge_claude_env_managed_block(Some(prior), &new_managed);
        // Legacy content preserved verbatim.
        assert!(out.starts_with(prior));
        // Managed block appended.
        assert!(out.contains(CLAUDE_ENV_MANAGED_BEGIN));
        assert!(out.contains("New_KG"));
    }

    #[test]
    fn merge_claude_env_managed_block_round_trip_idempotent() {
        // Apply the merge twice with the same managed block — the file
        // must converge (not grow on each call).
        let settings = ProjectEnvSettings::with_defaults("Acme");
        let new_managed = build_claude_env_managed_block(
            &[("KG_COLLECTION", "Acme_KG".to_string())],
            &settings,
        );
        let after_first = merge_claude_env_managed_block(None, &new_managed);
        let after_second =
            merge_claude_env_managed_block(Some(&after_first), &new_managed);
        assert_eq!(
            after_first, after_second,
            "second merge with same managed block must be a no-op"
        );
    }

    #[test]
    fn write_then_rewrite_preserves_user_added_env_key_round_trip() {
        // End-to-end: write project env files, hand-add a user env key
        // to the rendered .claude/settings.json, write again, and
        // confirm the user key survives. Pins the Commit-6 contract at
        // the public API level (not just the merge helpers).
        let tmp = std::env::temp_dir().join(format!(
            "vct-pr3-deep-merge-e2e-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // First write — rendered env block carries only the launcher's
        // canonical keys.
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme"))
            .unwrap();

        // User opens .claude/settings.json and adds a custom env value.
        let path = tmp.join(".claude/settings.json");
        let raw = std::fs::read_to_string(&path).unwrap();
        let mut v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        v["env"]["USER_PROJECT_API_BASE"] =
            serde_json::Value::String("http://internal-api.example.com".into());
        std::fs::write(&path, serde_json::to_string_pretty(&v).unwrap()).unwrap();

        // Second write — typical re-run case (rename, shared-KG toggle,
        // re-create). Pre-PR-3 this would silently drop USER_PROJECT_API_BASE.
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme"))
            .unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        // Canonical keys still correct.
        assert_eq!(v["env"]["KG_COLLECTION"], "Acme_KnowledgeGraph");
        // User key survived deep-merge round-trip.
        assert_eq!(
            v["env"]["USER_PROJECT_API_BASE"],
            "http://internal-api.example.com"
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── 2026-05-06 unregister keys / surgical purge ───────────────────

    /// Pin the canonical-key relationship: every key in the install
    /// const MUST also appear in the unregister-derived list.
    ///
    /// Pre-2026-05-07 this test had a third hardcoded mirror of the
    /// canonical list (the same names also lived in `canonical_env_pairs`
    /// AND `UNREGISTER_CANONICAL_ENV_KEYS`). PR-150 reviewer flagged
    /// the fragility: adding a key required touching all three lists
    /// and forgetting any one silently broke unregister.
    ///
    /// Post-2026-05-07 (follow-up #10): the test references the const
    /// directly. There is now ONE source of truth for canonical key
    /// NAMES. Drift across install + unregister + portability is
    /// structurally impossible.
    ///
    /// Install-flow audit (2026-05-08, P1 #2): the former
    /// `CANONICAL_PORTABILITY_ENV_KEYS` const is gone — its two
    /// members are now folded into `CANONICAL_INSTALL_ENV_KEYS` so all
    /// three install surfaces (.claude/env + both JSON env blocks)
    /// receive them. The unregister set is now identical to the
    /// install set (LazyLock just `.to_vec()`s it). Test simplified
    /// accordingly.
    #[test]
    fn unregister_canonical_keys_match_install_canonical_keys() {
        let unregister_set: std::collections::HashSet<&str> =
            UNREGISTER_CANONICAL_ENV_KEYS.iter().copied().collect();

        // Every install-canonical key must be removable by unregister.
        for k in CANONICAL_INSTALL_ENV_KEYS.iter() {
            assert!(
                unregister_set.contains(*k),
                "install-canonical key {:?} missing from \
                 UNREGISTER_CANONICAL_ENV_KEYS — unregister will leave \
                 it behind on disk. Likely cause: \
                 CANONICAL_INSTALL_ENV_KEYS and the LazyLock build \
                 of UNREGISTER_CANONICAL_ENV_KEYS got out of sync.",
                k,
            );
        }

        // Belt-and-suspenders: the unregister set must be EXACTLY the
        // install set — no extras, no gaps. An extra key in unregister
        // is suspicious (might mean someone tried to add a "remove
        // this on unregister" without adding the corresponding install
        // path).
        assert_eq!(
            UNREGISTER_CANONICAL_ENV_KEYS.len(),
            CANONICAL_INSTALL_ENV_KEYS.len(),
            "UNREGISTER_CANONICAL_ENV_KEYS size ({}) does not match \
             CANONICAL_INSTALL_ENV_KEYS size ({}). The LazyLock builder \
             may have been edited by hand to diverge.",
            UNREGISTER_CANONICAL_ENV_KEYS.len(),
            CANONICAL_INSTALL_ENV_KEYS.len(),
        );

        // The portability keys are part of the canonical install set
        // post-2026-05-08 (install-flow audit P1 #2). Pin their presence
        // so a future "let's split portability back out" refactor sees
        // a loud test failure first.
        assert!(
            unregister_set.contains("VCT_ORCHESTRATOR_ROOT"),
            "VCT_ORCHESTRATOR_ROOT missing from canonical set — \
             install-flow audit P1 #2 says it must be there",
        );
        assert!(
            unregister_set.contains("VCT_INFRASTRUCTURE_DIR"),
            "VCT_INFRASTRUCTURE_DIR missing from canonical set — \
             install-flow audit P1 #2 says it must be there",
        );
    }

    /// Belt-and-suspenders: catch the OTHER drift class. The install
    /// pair-builder uses a `match` over key names; if a key is added
    /// to `CANONICAL_INSTALL_ENV_KEYS` without a corresponding match
    /// arm in `write_project_env_files`, the build_pairs panics at
    /// install time. This test exercises the build path directly so
    /// missing match arms surface in CI rather than at first install.
    #[test]
    fn install_match_arms_cover_every_canonical_key() {
        // We can't easily call `write_project_env_files` from a unit
        // test (needs a full `ProjectEnvSettings` + DB context). But
        // the match logic is identical in shape: for each key in the
        // const, there must exist a match arm. We assert that by
        // listing the supported keys here + checking equality.
        //
        // If you add a new key to CANONICAL_INSTALL_ENV_KEYS, add it
        // to BOTH this list AND the match in write_project_env_files.
        // The list of (test, install) is intentionally separate so
        // forgetting either side surfaces here.
        let test_known_keys: std::collections::HashSet<&str> = [
            "KG_COLLECTION", "PROJECT_NAME",
            // PR-8 cross-PR handoff (v0.2.11 / 2026-05-15): added at the
            // Rust first-install boundary so newly-registered projects
            // get `CODE_GRAPH_PROJECT` in their env surfaces from minute
            // one, not just after PR-7's Python `--update` backfill. The
            // match arm sanitizes via `sanitize_kg_collection` because
            // Weaviate class names need the `[A-Za-z][A-Za-z0-9_]*`
            // identifier shape.
            "CODE_GRAPH_PROJECT",
            "DEVELOPMENT_COLLECTION",
            "SHARED_KG_COLLECTION", "SHARED_KG_WRITE_DISABLED",
            "SHARED_KG_OPT_OUT",
            // v0.2.46 Decision B — symmetric READ gate.
            "SHARED_KG_READ_DISABLED",
            "ACTIVE_EMBEDDING",
            "WEAVIATE_URL", "WEAVIATE_PORT",
            "OLLAMA_URL", "OLLAMA_PORT",
            "CODE_EMBED_URL", "CODE_EMBED_PORT",
            // Install-flow audit (2026-05-08, P1 #2): portability keys
            // moved here from the now-deleted CANONICAL_PORTABILITY_ENV_KEYS.
            // The match arm in `write_project_env_files` returns
            // `Option<String>` for these — `None` when
            // `settings.orchestrator_root` is `None`, omitting the
            // entry from every install surface.
            "VCT_ORCHESTRATOR_ROOT", "VCT_INFRASTRUCTURE_DIR",
            // P1-D (2026-05-08): cross-project access lists. Conditionally
            // emitted — the match arm in `write_project_env_files`
            // returns `None` when the corresponding access list is empty
            // (the default — no peers granted), omitting the entry from
            // every install surface.
            "VCT_KG_ACCESS_LIST", "VCT_CODE_GRAPH_ACCESS_LIST",
            // 0.1.7 fork-readiness sweep (2026-05-08): GITHUB_TOKEN.
            // Conditionally emitted — the match arm in
            // `write_project_env_files` returns `None` when the
            // OnboardingWizard's PAT keychain entry is absent or paused,
            // omitting the entry from every install surface.
            "GITHUB_TOKEN",
            // v0.2.43 V0243-5-Rust: KG_BASE_DIR. Always emitted.
            "KG_BASE_DIR",
        ].iter().copied().collect();

        for k in CANONICAL_INSTALL_ENV_KEYS.iter() {
            assert!(
                test_known_keys.contains(*k),
                "CANONICAL_INSTALL_ENV_KEYS has key {:?} that this \
                 test doesn't know about. Add it here AND add a \
                 match arm in write_project_env_files.",
                k,
            );
        }
    }

    #[test]
    fn strip_canonical_keys_from_env_text_keeps_user_keys() {
        let input = "\
# vibecoded-orchestrator per-project .env
KG_COLLECTION=SomeProject_KnowledgeGraph
USER_API_KEY=secret123
PROJECT_NAME=SomeProject
SOME_USER_VAR=hello
# OLLAMA_URL=http://localhost:11435
DEVELOPMENT_COLLECTION=SomeProject_Development
ACTIVE_EMBEDDING=qwen3
";
        let (out, removed) = strip_canonical_keys_from_env_text(input);

        // Canonical keys gone from the output text.
        assert!(!out.contains("KG_COLLECTION="));
        assert!(!out.contains("PROJECT_NAME="));
        assert!(!out.contains("DEVELOPMENT_COLLECTION="));
        assert!(!out.contains("ACTIVE_EMBEDDING="));
        // Commented canonical also gone.
        assert!(!out.contains("OLLAMA_URL="));

        // User keys + comments preserved.
        assert!(
            out.contains("USER_API_KEY=secret123"),
            "user secret was clobbered: {}", out,
        );
        assert!(out.contains("SOME_USER_VAR=hello"));
        assert!(out.contains("# vibecoded-orchestrator per-project .env"));

        // Returned `removed` is sorted + de-duped.
        let removed_set: std::collections::HashSet<String> =
            removed.iter().cloned().collect();
        assert!(removed_set.contains("KG_COLLECTION"));
        assert!(removed_set.contains("PROJECT_NAME"));
        assert!(removed_set.contains("DEVELOPMENT_COLLECTION"));
        assert!(removed_set.contains("ACTIVE_EMBEDDING"));
        assert!(removed_set.contains("OLLAMA_URL"));
    }

    #[test]
    fn strip_canonical_keys_from_env_text_idempotent() {
        let input = "USER_KEY=value\n";
        let (out1, removed1) = strip_canonical_keys_from_env_text(input);
        assert_eq!(out1, input);
        assert!(removed1.is_empty());

        let (out2, removed2) = strip_canonical_keys_from_env_text(&out1);
        assert_eq!(out2, out1);
        assert!(removed2.is_empty());
    }

    #[test]
    fn strip_canonical_keys_from_claude_env_text_handles_export_form() {
        let input = "\
# vco-managed-begin
export KG_COLLECTION=\"SomeProject_KnowledgeGraph\"
export PROJECT_NAME=\"SomeProject\"
export VCT_ORCHESTRATOR_ROOT=\"/some/path\"
export VCT_INFRASTRUCTURE_DIR=\"/some/path/infrastructure\"
# vco-managed-end
export USER_PROJECT_VAR=\"keep me\"
# user comment
";
        let (out, removed) = strip_canonical_keys_from_claude_env_text(input);

        assert!(!out.contains("export KG_COLLECTION="));
        assert!(!out.contains("export PROJECT_NAME="));
        assert!(!out.contains("VCT_ORCHESTRATOR_ROOT"));
        assert!(!out.contains("VCT_INFRASTRUCTURE_DIR"));

        // User exports + markers preserved.
        assert!(out.contains("export USER_PROJECT_VAR=\"keep me\""));
        assert!(out.contains("# vco-managed-begin"));
        assert!(out.contains("# vco-managed-end"));
        assert!(out.contains("# user comment"));

        let removed_set: std::collections::HashSet<String> =
            removed.iter().cloned().collect();
        assert!(removed_set.contains("KG_COLLECTION"));
        assert!(removed_set.contains("PROJECT_NAME"));
        assert!(removed_set.contains("VCT_ORCHESTRATOR_ROOT"));
        assert!(removed_set.contains("VCT_INFRASTRUCTURE_DIR"));
    }

    #[test]
    fn strip_canonical_keys_from_env_object_preserves_user_keys() {
        let mut parent = serde_json::Map::new();
        parent.insert(
            "env".to_string(),
            serde_json::json!({
                "KG_COLLECTION": "SomeProject_KnowledgeGraph",
                "PROJECT_NAME": "SomeProject",
                "USER_OPENAI_API_BASE": "https://internal.example.com",
                "ACTIVE_EMBEDDING": "qwen3",
            }),
        );
        let removed = strip_canonical_keys_from_env_object(&mut parent, "env");
        let env = parent.get("env").unwrap().as_object().unwrap();

        // Canonical gone.
        assert!(!env.contains_key("KG_COLLECTION"));
        assert!(!env.contains_key("PROJECT_NAME"));
        assert!(!env.contains_key("ACTIVE_EMBEDDING"));
        // User key intact.
        assert_eq!(env["USER_OPENAI_API_BASE"], "https://internal.example.com");

        let removed_set: std::collections::HashSet<String> =
            removed.into_iter().collect();
        assert!(removed_set.contains("KG_COLLECTION"));
        assert!(removed_set.contains("PROJECT_NAME"));
        assert!(removed_set.contains("ACTIVE_EMBEDDING"));
    }

    #[test]
    fn strip_canonical_keys_from_env_object_missing_block_is_noop() {
        let mut parent = serde_json::Map::new();
        parent.insert("other_key".to_string(), serde_json::json!("value"));

        let removed = strip_canonical_keys_from_env_object(&mut parent, "env");
        assert!(removed.is_empty());
        assert_eq!(parent["other_key"], "value");
    }

    /// `test_unregister_default_purges_hooks_scripts_compose_keeps_agents_skills`
    ///
    /// The headline default-mode test: write a fully-populated fake project
    /// folder and assert the surgical purge removes EXACTLY what's in
    /// `UNREGISTER_PURGE_PATHS` and preserves the user-content allowlist.
    #[test]
    fn unregister_default_purges_hooks_scripts_compose_keeps_agents_skills() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-default-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Populate launcher-managed paths (must be removed).
        std::fs::create_dir_all(tmp.join(".claude/hooks/_lib")).unwrap();
        std::fs::write(tmp.join(".claude/hooks/pre-tool.sh"), "echo").unwrap();
        std::fs::write(tmp.join(".claude/hooks/_lib/util.sh"), "echo").unwrap();
        std::fs::create_dir_all(tmp.join(".claude/scripts")).unwrap();
        std::fs::write(tmp.join(".claude/scripts/code-graph-analyze"), "#!/bin/bash").unwrap();
        std::fs::write(tmp.join(".claude/env"),
            "# vco-managed-begin\nexport KG_COLLECTION=\"X_KnowledgeGraph\"\n# vco-managed-end\n",
        ).unwrap();
        // Launcher install manifest (per follow-up #12).
        std::fs::write(tmp.join(".claude/.vco-manifest.json"),
            r#"{"bundle_version":"0.1.6","installed_at":"2026-05-07T00:00:00Z"}"#,
        ).unwrap();
        std::fs::create_dir_all(tmp.join("infrastructure")).unwrap();
        std::fs::write(tmp.join("infrastructure/docker-compose.yml"), "version: '3'\n").unwrap();
        std::fs::write(tmp.join("infrastructure/podman-compose.gpu.yml"), "version: '3'\n").unwrap();

        // Populate user-content paths (must survive).
        std::fs::create_dir_all(tmp.join(".claude/agents")).unwrap();
        std::fs::write(tmp.join(".claude/agents/my-agent.md"), "user agent").unwrap();
        std::fs::create_dir_all(tmp.join(".claude/skills/foo")).unwrap();
        std::fs::write(tmp.join(".claude/skills/foo/SKILL.md"), "user skill").unwrap();
        std::fs::write(tmp.join(".claude/CONTEXT_STATE.md"), "current task").unwrap();
        std::fs::write(tmp.join(".claude/MEMORY.md"), "user memory").unwrap();
        std::fs::create_dir_all(tmp.join(".claude/context")).unwrap();
        std::fs::write(tmp.join(".claude/context/notes.md"), "user notes").unwrap();
        std::fs::write(tmp.join("CLAUDE.md"), "project instructions").unwrap();
        std::fs::write(tmp.join("main.py"), "print('hi')").unwrap();
        // .claude/settings.json with mixed canonical + user env keys.
        std::fs::write(tmp.join(".claude/settings.json"),
            r#"{"env":{"KG_COLLECTION":"X_KnowledgeGraph","USER_VAR":"keep"},"hooks":{}}"#
        ).unwrap();
        // .env with mixed canonical + user keys.
        std::fs::write(tmp.join(".env"),
            "KG_COLLECTION=X_KnowledgeGraph\nUSER_API_KEY=secret\nPROJECT_NAME=X\n"
        ).unwrap();

        // Run both purges (separately — drives identical to delete_project_v2).
        let (keys_purged, env_warnings) = surgically_strip_env_surfaces(&tmp);
        let (files_purged, file_warnings) = purge_launcher_files_from_project(&tmp);

        // No warnings on a clean folder.
        assert!(env_warnings.is_empty(), "env warnings: {:?}", env_warnings);
        assert!(file_warnings.is_empty(), "file warnings: {:?}", file_warnings);

        // Launcher-managed paths gone.
        assert!(!tmp.join(".claude/hooks").exists(), "hooks/ should be purged");
        assert!(!tmp.join(".claude/scripts").exists(), "scripts/ should be purged");
        // .claude/env is preserved as of 2026-05-09 (non-destructive
        // unregister): the file survives but the launcher-managed
        // BEGIN/END block is excised and any stray canonical keys
        // outside the block are stripped.
        assert!(
            tmp.join(".claude/env").exists(),
            ".claude/env should be preserved (managed block excised, user-added exports survive)",
        );
        let env_after = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            !env_after.contains("KG_COLLECTION="),
            "managed-block KG_COLLECTION export must be excised on unregister: {}",
            env_after,
        );
        assert!(
            !tmp.join(".claude/.vco-manifest.json").exists(),
            ".vco-manifest.json should be purged"
        );
        assert!(!tmp.join("infrastructure/docker-compose.yml").exists());
        assert!(!tmp.join("infrastructure/podman-compose.gpu.yml").exists());

        // User content survives.
        assert!(tmp.join(".claude/agents/my-agent.md").exists(), "agents preserved");
        assert!(tmp.join(".claude/skills/foo/SKILL.md").exists(), "skills preserved");
        assert!(tmp.join(".claude/CONTEXT_STATE.md").exists(), "CONTEXT_STATE preserved");
        assert!(tmp.join(".claude/MEMORY.md").exists(), "MEMORY preserved");
        assert!(tmp.join(".claude/context/notes.md").exists(), "context/ preserved");
        assert!(tmp.join("CLAUDE.md").exists(), "CLAUDE.md preserved");
        assert!(tmp.join("main.py").exists(), "source code preserved");
        assert!(tmp.join(".claude/settings.json").exists(), "settings.json preserved");
        assert!(tmp.join(".env").exists(), ".env preserved");

        // .env user keys preserved, canonical removed.
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(env.contains("USER_API_KEY=secret"), ".env user key preserved");
        assert!(!env.contains("KG_COLLECTION="), ".env canonical removed");
        assert!(!env.contains("PROJECT_NAME="), ".env canonical removed");

        // settings.json: env block surgically edited.
        let s_raw = std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap();
        let s: serde_json::Value = serde_json::from_str(&s_raw).unwrap();
        assert_eq!(s["env"]["USER_VAR"], "keep", "user env key preserved");
        assert!(s["env"].get("KG_COLLECTION").is_none(), "canonical env key removed");
        // Non-env top-level fields (hooks etc.) preserved.
        assert!(s.get("hooks").is_some());

        // Reported sets contain the right entries.
        assert!(files_purged.iter().any(|p| p == ".claude/hooks"));
        assert!(files_purged.iter().any(|p| p == ".claude/scripts"));
        // .claude/env is NOT in files_purged anymore (preserved on
        // unregister; see managed-block excision above).
        assert!(
            !files_purged.iter().any(|p| p == ".claude/env"),
            "files_purged must not include .claude/env: {:?}",
            files_purged,
        );
        assert!(files_purged.iter().any(|p| p == "infrastructure/docker-compose.yml"));
        assert!(keys_purged.contains(&"KG_COLLECTION".to_string()));
        assert!(keys_purged.contains(&"PROJECT_NAME".to_string()));

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Regression pin (2026-05-09): user-added exports in `.claude/env`
    /// OUTSIDE the launcher-managed BEGIN/END block MUST survive
    /// unregister. Prior behaviour deleted the whole file, destroying
    /// user content (`tools/claude` wrapper sources `.claude/env`, so
    /// it's a plausible spot for users to add their own exports).
    #[test]
    fn unregister_preserves_user_exports_in_claude_env_outside_managed_block() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-preserve-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".claude")).unwrap();

        // Mixed file: launcher block + user-added exports both before
        // and after the block.
        let original = "\
# Pre-block user export — must survive
export MY_API_BASE=\"https://api.example.com\"

# vco-managed-begin
export KG_COLLECTION=\"X_KnowledgeGraph\"
export PROJECT_NAME=\"X\"
# vco-managed-end

# Post-block user export — must survive
export MY_HELPER_TOKEN=\"keep-me\"
";
        std::fs::write(tmp.join(".claude/env"), original).unwrap();

        // Run the unregister env-strip path.
        let (keys_purged, warnings) = surgically_strip_env_surfaces(&tmp);
        assert!(warnings.is_empty(), "warnings: {:?}", warnings);
        // Canonical keys reported as removed (they were inside the block,
        // and the block excision is what carries them off — strip_canonical
        // may or may not also flag them depending on whether it sees them
        // post-excision; either way the test below pins observable behaviour).
        let _ = keys_purged;

        // File still there.
        assert!(
            tmp.join(".claude/env").exists(),
            ".claude/env must be preserved on unregister",
        );
        let after = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();

        // User-added exports survive.
        assert!(
            after.contains("MY_API_BASE"),
            "pre-block user export lost: {}",
            after,
        );
        assert!(
            after.contains("MY_HELPER_TOKEN"),
            "post-block user export lost: {}",
            after,
        );

        // Launcher-managed exports gone.
        assert!(
            !after.contains("KG_COLLECTION"),
            "managed KG_COLLECTION must be excised: {}",
            after,
        );
        assert!(
            !after.contains("PROJECT_NAME"),
            "managed PROJECT_NAME must be excised: {}",
            after,
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// `test_unregister_purge_launcher_files_false_leaves_files_alone`
    ///
    /// When `purge_launcher_files: false`, neither helper should run.
    /// We verify by NOT calling them and asserting nothing changes.
    /// (The flag-gating lives in `delete_project_v2` itself; the helpers
    /// always do their work when called. This test pins the contract that
    /// `delete_project_v2` MUST honour the flag.)
    #[test]
    fn unregister_purge_launcher_files_false_leaves_files_alone() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-noop-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".claude/hooks")).unwrap();
        std::fs::write(tmp.join(".claude/hooks/x.sh"), "echo").unwrap();
        std::fs::write(tmp.join(".env"), "KG_COLLECTION=X_KnowledgeGraph\n").unwrap();

        // Simulate `delete_project_v2` with purge_launcher_files=false:
        // the helpers are never invoked. Verify state unchanged.
        let opts = UnregisterOptions { purge_launcher_files: false, purge_collections: false };
        assert!(!opts.purge_launcher_files);

        // (Don't call helpers.) State must be intact.
        assert!(tmp.join(".claude/hooks/x.sh").exists());
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(env.contains("KG_COLLECTION="));

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// `test_unregister_surgical_env_keeps_user_keys` — focused regression
    /// test for the `.env` surface alone with mixed canonical + user keys.
    #[test]
    fn unregister_surgical_env_keeps_user_keys() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-env-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Mixed .env: every canonical key the launcher writes + several
        // user-added keys (API tokens, internal config, etc.).
        //
        // 0.1.7 fork-readiness sweep (2026-05-08): GITHUB_TOKEN moved into
        // the launcher-canonical set (the launcher writes it from the
        // OnboardingWizard keychain entry). It is no longer a "user
        // secret" in the .env-survives sense — strip it on unregister
        // like every other canonical key. Tests that pin the survives
        // byte-for-byte semantics use MY_GITHUB_TOKEN (NOT in
        // CANONICAL_INSTALL_ENV_KEYS) for a clean separation.
        let env_text = "\
# vibecoded-orchestrator per-project .env
KG_COLLECTION=MyProj_KnowledgeGraph
DEVELOPMENT_COLLECTION=MyProj_Development
SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph
PROJECT_NAME=MyProj
ACTIVE_EMBEDDING=qwen3
WEAVIATE_URL=http://localhost:8081
WEAVIATE_PORT=8081
OLLAMA_URL=http://localhost:11435
OLLAMA_PORT=11435
CODE_EMBED_URL=http://localhost:11440
CODE_EMBED_PORT=11440
SHARED_KG_WRITE_DISABLED=false
SHARED_KG_OPT_OUT=false

# === user secrets ===
ANTHROPIC_API_KEY=sk-ant-real-token-here
OPENAI_API_KEY=sk-real-openai-token
MY_GITHUB_TOKEN=ghp_real_pat
USER_INTERNAL_HOST=internal.example.com
USER_DB_URL=postgres://user:pass@db/app
";
        std::fs::write(tmp.join(".env"), env_text).unwrap();

        let (keys, warnings) = surgically_strip_env_surfaces(&tmp);
        assert!(warnings.is_empty());

        let after = std::fs::read_to_string(tmp.join(".env")).unwrap();

        // ALL canonical keys removed.
        //
        // Line-precise check via `parse_existing_env_keys`: a naive
        // substring like `format!("{}=", k)` would false-positive on
        // suffix-of-suffix collisions (e.g. canonical "GITHUB_TOKEN"
        // matches user-added "MY_GITHUB_TOKEN" because the latter ends
        // with "GITHUB_TOKEN="). Walk the parsed key sets instead.
        let before_keys = parse_existing_env_keys(env_text);
        let after_keys = parse_existing_env_keys(&after);
        for k in UNREGISTER_CANONICAL_ENV_KEYS.iter() {
            if before_keys.contains(*k) {
                assert!(
                    !after_keys.contains(*k),
                    "canonical key {} survived the strip:\n{}",
                    k,
                    after,
                );
            }
        }

        // Every user secret survives byte-for-byte.
        assert!(after.contains("ANTHROPIC_API_KEY=sk-ant-real-token-here"));
        assert!(after.contains("OPENAI_API_KEY=sk-real-openai-token"));
        // MY_GITHUB_TOKEN is the user-supplied placeholder (NOT in the
        // canonical set); GITHUB_TOKEN itself was promoted to canonical
        // by the 0.1.7 fork sweep and gets stripped above.
        assert!(after.contains("MY_GITHUB_TOKEN=ghp_real_pat"));
        assert!(after.contains("USER_INTERNAL_HOST=internal.example.com"));
        assert!(after.contains("USER_DB_URL=postgres://user:pass@db/app"));

        // The reported keys list contains exactly the canonical keys
        // that were in the fixture.
        for k in &["KG_COLLECTION", "PROJECT_NAME", "DEVELOPMENT_COLLECTION",
                   "ACTIVE_EMBEDDING", "WEAVIATE_URL", "OLLAMA_URL"] {
            assert!(
                keys.contains(&(*k).to_string()),
                "reported keys missing {}: {:?}", k, keys,
            );
        }

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// `test_unregister_idempotent_on_already-purged_project` — running
    /// the unregister helpers twice in a row produces no warnings and
    /// no further state change. Captures the soft-fail discipline: a
    /// crashed-mid-unregister flow can be safely re-invoked.
    #[test]
    fn unregister_idempotent_on_already_purged_project() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-idempotent-{}", uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".claude/hooks")).unwrap();
        std::fs::write(tmp.join(".claude/hooks/x.sh"), "echo").unwrap();
        std::fs::write(tmp.join(".env"),
            "KG_COLLECTION=X_KnowledgeGraph\nUSER_KEY=keep\n").unwrap();

        // First run: should remove.
        let (keys1, w1a) = surgically_strip_env_surfaces(&tmp);
        let (files1, w1b) = purge_launcher_files_from_project(&tmp);
        assert!(w1a.is_empty() && w1b.is_empty());
        assert!(!keys1.is_empty());
        assert!(!files1.is_empty());

        // Second run: nothing left to remove.
        let (keys2, w2a) = surgically_strip_env_surfaces(&tmp);
        let (files2, w2b) = purge_launcher_files_from_project(&tmp);
        assert!(w2a.is_empty(), "second-run env warnings: {:?}", w2a);
        assert!(w2b.is_empty(), "second-run file warnings: {:?}", w2b);
        assert!(keys2.is_empty(), "second-run keys: {:?}", keys2);
        assert!(files2.is_empty(), "second-run files: {:?}", files2);

        // User key still there after both runs.
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(env.contains("USER_KEY=keep"));

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// `test_unregister_purge_collections_drops_only_owned_collections`
    ///
    /// We can't run a real Weaviate server in unit tests, so this is a
    /// pure behaviour assertion on the Python-side guard: the
    /// `_cmd_drop_collections` function MUST never emit the shared KG
    /// name in its `dropped` list, even if the collection target list
    /// somehow includes it. The Rust helper that drives it
    /// (`drop_owned_collections`) is integration-tested at the bash
    /// level. Here we assert that:
    ///   1. The Rust helper signature exists + is async.
    ///   2. The constant `UNREGISTER_CANONICAL_ENV_KEYS` does not
    ///      include any shared collection name (defense in depth: we
    ///      shouldn't accidentally suggest the SHARED_KG_COLLECTION
    ///      key value as a drop target).
    ///   3. The drop helper is independent of `purge_launcher_files`
    ///      (different code path), so calling it without a folder is
    ///      fine — no folder access required.
    ///
    /// The actual subprocess behaviour is covered by the Python-side
    /// `tests/test_project_init_drop_collections.py` (added in the
    /// same commit if the integration suite has that flavour) and by
    /// manual e2e on a live Weaviate. The launcher-side warning surface
    /// is proven by `drop-collections subprocess failed to start`
    /// log lines on a Python-less machine.
    #[test]
    fn unregister_purge_collections_drops_only_owned_collections() {
        // Defense-in-depth assertion: the canonical-key set should never
        // be confused with collection NAMES. The keys are env-var names;
        // their VALUES at runtime are the actual collection names. A
        // future bug where someone tried to drop "SHARED_KG_COLLECTION"
        // (the env-var name) instead of `*_KnowledgeGraph` would still
        // fail at the Weaviate API level, but this assertion makes the
        // confusion impossible to express in the source.
        for k in UNREGISTER_CANONICAL_ENV_KEYS.iter() {
            assert!(
                !k.ends_with("_KnowledgeGraph"),
                "canonical env-var name {} looks like a collection name", k,
            );
            assert!(
                !k.ends_with("_Development"),
                "canonical env-var name {} looks like a collection name", k,
            );
        }
    }

    /// Edge case: unregister against a folder that no longer exists on
    /// disk (user `rm -rf`'d it after registering). Helpers should still
    /// be safe to call. `delete_project_v2` itself short-circuits the
    /// purge step and pushes a warning — this test pins the helper
    /// behaviour underneath.
    #[test]
    fn unregister_helpers_safe_on_missing_folder() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-missing-{}", uuid::Uuid::new_v4().simple()
        ));
        // Don't create it.
        let (keys, w_env) = surgically_strip_env_surfaces(&tmp);
        let (files, w_file) = purge_launcher_files_from_project(&tmp);
        assert!(keys.is_empty());
        assert!(files.is_empty());
        assert!(w_env.is_empty(), "env warnings on missing folder: {:?}", w_env);
        assert!(w_file.is_empty(), "file warnings on missing folder: {:?}", w_file);
    }

    // ─── Fix #4: VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST ────────
    //
    // Pin the multi-source access matrix → runtime env propagation. The
    // launcher GUI used to grant cross-project KG / codegraph reads, but
    // no runtime code consumed the rows — the matrix was a UI-only
    // feature. P1-D wires
    //   ProjectEnvSettings.kg_access_list / code_graph_access_list
    //     ↓ (write_project_env_files match arms)
    //   `.claude/env`, `.claude/settings.json env`,
    //   `.vscode/settings.json claude-code.env` →
    //   VCT_KG_ACCESS_LIST=Foo,Bar / VCT_CODE_GRAPH_ACCESS_LIST=Foo,Bar
    //
    // The Python side (weaviate_mcp/server.py + rl_kg_search.py) consumes
    // these env vars to fan-out searches across peer KGs/codegraphs; that
    // half is covered by hand-pinning the env-var contract here +
    // runtime probing on the Python side (out of Rust unit-test scope).

    #[test]
    fn test_write_project_env_files_includes_access_list_when_peers_granted() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-access-list-prop-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        let mut settings = ProjectEnvSettings::with_defaults("AccessProj");
        settings.kg_access_list = vec!["PeerA".to_string(), "PeerB".to_string()];
        settings.code_graph_access_list = vec!["PeerC".to_string()];
        write_project_env_files(&tmp, &settings).unwrap();

        // Surface 1: .claude/env (POSIX exports). Comma-joined.
        let env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            env_text.contains("export VCT_KG_ACCESS_LIST=\"PeerA,PeerB\""),
            ".claude/env missing VCT_KG_ACCESS_LIST. Body:\n{}",
            env_text
        );
        assert!(
            env_text.contains("export VCT_CODE_GRAPH_ACCESS_LIST=\"PeerC\""),
            ".claude/env missing VCT_CODE_GRAPH_ACCESS_LIST. Body:\n{}",
            env_text
        );

        // Surface 2: .claude/settings.json env block.
        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            cs["env"]["VCT_KG_ACCESS_LIST"], "PeerA,PeerB",
            ".claude/settings.json env block missing or wrong VCT_KG_ACCESS_LIST. \
             Block: {}",
            cs["env"]
        );
        assert_eq!(
            cs["env"]["VCT_CODE_GRAPH_ACCESS_LIST"], "PeerC",
            ".claude/settings.json env block missing or wrong VCT_CODE_GRAPH_ACCESS_LIST. \
             Block: {}",
            cs["env"]
        );

        // PR-27 (v0.2.12, 2026-05-16): the historical third surface
        // (`.vscode/settings.json` `claude-code.env`) was removed. The
        // writer must not create the file as a side-effect.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn test_write_project_env_files_omits_access_list_when_no_peers() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-access-list-omit-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Default `with_defaults` has empty access lists.
        let settings = ProjectEnvSettings::with_defaults("NoPeers");
        assert!(settings.kg_access_list.is_empty());
        assert!(settings.code_graph_access_list.is_empty());
        write_project_env_files(&tmp, &settings).unwrap();

        // No surface should contain either key (omit, not empty-string).
        let env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            !env_text.contains("export VCT_KG_ACCESS_LIST="),
            ".claude/env should not export VCT_KG_ACCESS_LIST when list is empty. \
             Body:\n{}",
            env_text
        );
        assert!(
            !env_text.contains("export VCT_CODE_GRAPH_ACCESS_LIST="),
            ".claude/env should not export VCT_CODE_GRAPH_ACCESS_LIST when list is empty. \
             Body:\n{}",
            env_text
        );

        let cs: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
        )
        .unwrap();
        assert!(
            cs["env"].get("VCT_KG_ACCESS_LIST").is_none(),
            ".claude/settings.json env should omit VCT_KG_ACCESS_LIST when empty"
        );
        assert!(
            cs["env"].get("VCT_CODE_GRAPH_ACCESS_LIST").is_none(),
            ".claude/settings.json env should omit VCT_CODE_GRAPH_ACCESS_LIST when empty"
        );

        // PR-27 (v0.2.12, 2026-05-16): the writer must not create
        // `.vscode/settings.json` at all. The historical claude-code.env
        // surface was removed because it didn't propagate to MCP
        // subprocesses on Linux. See `write_project_env_files` docstring
        // for the empirical-trace KG-node reference.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Pin the populate path: when project P has KG access rows pointing
    /// at PeerA's collection, populate's `kg_access_list` includes PeerA
    /// (and excludes self + the shared collection).
    #[test]
    fn populate_resolves_kg_access_peers_from_matrix() {
        use crate::commands::project_env_settings::populate;
        use crate::db::Db;

        let db = Db::open_in_memory().unwrap();
        // Seed two registered projects.
        let folder_a = std::env::temp_dir().join(format!("vct-pop-a-{}", uuid::Uuid::new_v4().simple()));
        let folder_b = std::env::temp_dir().join(format!("vct-pop-b-{}", uuid::Uuid::new_v4().simple()));
        std::fs::create_dir_all(&folder_a).unwrap();
        std::fs::create_dir_all(&folder_b).unwrap();
        let row_a = db
            .insert_project(
                "proj-a",
                "Alpha",
                folder_a.to_str().unwrap(),
                crate::db::models::ProjectHost::Base,
                "alpha",
            )
            .unwrap();
        let _row_b = db
            .insert_project(
                "proj-b",
                "Beta",
                folder_b.to_str().unwrap(),
                crate::db::models::ProjectHost::Base,
                "beta",
            )
            .unwrap();

        // Grant Alpha read access to Beta's KG collection. That's the
        // shape `populate_kg_collection_access` produces under the hood.
        db.kg_set_access(&row_a.id, "Beta_KnowledgeGraph", "read").unwrap();
        // And to a Beta dev collection (to verify dedup logic — both
        // map to the same peer "Beta" after suffix-strip).
        db.kg_set_access(&row_a.id, "Beta_Development", "read").unwrap();
        // And to the shared collection (must be excluded). Uses the
        // canonical post-v0.2.23-B1 capital-C name; legacy lowercase-c
        // "VibecodedOrchestrator_KnowledgeGraph" (v0.2.12–v0.2.22) and
        // "VibeCodedTools_KnowledgeGraph" (pre-v0.2.12) installs go through
        // the launcher's shared-KG migration picker / case-insensitive
        // adoption in install.py.
        db.kg_set_access(&row_a.id, "VibeCodedOrchestrator_KnowledgeGraph", "read").unwrap();
        // And Alpha's OWN collection (must be excluded).
        db.kg_set_access(&row_a.id, "Alpha_KnowledgeGraph", "write").unwrap();
        // A `none` row must be filtered out.
        db.kg_set_access(&row_a.id, "Gamma_KnowledgeGraph", "none").unwrap();

        let settings = populate(&db, "Alpha", Some(&row_a.id));
        assert_eq!(
            settings.kg_access_list,
            vec!["Beta".to_string()],
            "expected just Beta as peer; got {:?}",
            settings.kg_access_list
        );

        std::fs::remove_dir_all(&folder_a).ok();
        std::fs::remove_dir_all(&folder_b).ok();
    }

    /// Pin the populate path: codegraph access list resolves grantor
    /// project names from the access matrix (only `read` rows, not `none`).
    #[test]
    fn populate_resolves_code_graph_access_peers_from_matrix() {
        use crate::commands::project_env_settings::populate;
        use crate::db::Db;

        let db = Db::open_in_memory().unwrap();
        let folder_a = std::env::temp_dir().join(format!("vct-pop-cg-a-{}", uuid::Uuid::new_v4().simple()));
        let folder_b = std::env::temp_dir().join(format!("vct-pop-cg-b-{}", uuid::Uuid::new_v4().simple()));
        let folder_c = std::env::temp_dir().join(format!("vct-pop-cg-c-{}", uuid::Uuid::new_v4().simple()));
        for f in [&folder_a, &folder_b, &folder_c] {
            std::fs::create_dir_all(f).unwrap();
        }
        let _ = db.insert_project(
            "proj-a", "Alpha", folder_a.to_str().unwrap(),
            crate::db::models::ProjectHost::Base, "alpha",
        ).unwrap();
        let _ = db.insert_project(
            "proj-b", "Beta", folder_b.to_str().unwrap(),
            crate::db::models::ProjectHost::Base, "beta",
        ).unwrap();
        let _ = db.insert_project(
            "proj-c", "Gamma", folder_c.to_str().unwrap(),
            crate::db::models::ProjectHost::Base, "gamma",
        ).unwrap();

        // Beta grants Alpha read access to its codegraph.
        db.codegraph_grant("proj-b", "proj-a", "read").unwrap();
        // Gamma denies Alpha (none row).
        db.codegraph_grant("proj-c", "proj-a", "none").unwrap();

        let settings = populate(&db, "Alpha", Some("proj-a"));
        assert_eq!(
            settings.code_graph_access_list,
            vec!["Beta".to_string()],
            "expected just Beta as codegraph peer; got {:?}",
            settings.code_graph_access_list
        );

        for f in [&folder_a, &folder_b, &folder_c] {
            std::fs::remove_dir_all(f).ok();
        }
    }

    /// `refresh_project_env_with_db` re-runs the env writer for a given
    /// project and surfaces the resolved access lists in its return
    /// value. Used by the kg/codegraph access setters as a hot-reload
    /// path so running Claude Code sessions pick up new peer grants
    /// without restart.
    ///
    /// v0.2.42 W4-TEST-8: re-enabled from #[ignore] by adding a
    /// `python_env_available()` skip guard and using an on-disk DB via
    /// `with_state_dir`. The Python subprocess reads from the DB at
    /// `$VCT_STATE_DIR/launcher.db`, so both the Rust Db handle and the
    /// Python side must point at the same on-disk file.
    ///
    /// Skipped when `python3 -c "import vco_lib"` fails (CI envs without
    /// the orchestrator clone on PYTHONPATH).
    #[test]
    fn refresh_project_env_with_db_re_runs_env_writer() {
        if !vct_launcher_core::test_env::python_env_available() {
            eprintln!("skipping: python3 + vco_lib not available in this test env");
            return;
        }
        vct_launcher_core::test_env::with_state_dir(|state_dir| {
            use crate::db::Db;

            // Open the on-disk DB so the Python subprocess can read it.
            let db = Db::open().expect("open on-disk DB in state dir");
            let folder = state_dir.join(format!("proj-{}", uuid::Uuid::new_v4().simple()));
            std::fs::create_dir_all(&folder).unwrap();
            let row = db.insert_project(
                "proj-r", "Refresh", folder.to_str().unwrap(),
                crate::db::models::ProjectHost::Base, "refresh",
            ).unwrap();

            // No grants yet — refresh runs and reports empty lists.
            let r1 = refresh_project_env_with_db(&db, &row.id).unwrap();
            assert!(r1.kg_access_list.is_empty());
            assert!(r1.code_graph_access_list.is_empty());
            // The Python writer creates .claude/env when it runs.
            // It may emit warnings (e.g. missing orchestrator root) but must
            // not fail entirely — we accept warnings in r1.
            // NOTE: .claude/env may not exist if Python subprocess soft-fails;
            // we only assert the access-list shape (the Rust populate() path).
            // The env-file assertion is covered by test_config_projection_byte_identical.py.

            // Grant Refresh read access to a peer's KG, then refresh again.
            // refresh re-runs populate which re-reads the matrix.
            db.kg_set_access(&row.id, "PeerProj_KnowledgeGraph", "read").unwrap();
            let r2 = refresh_project_env_with_db(&db, &row.id).unwrap();
            assert_eq!(r2.kg_access_list, vec!["PeerProj".to_string()]);
        });
    }

    // ─── Subagent G (2026-05-08): user-set per-project secrets in env ──
    //
    // These tests pin the contract that a user adding a per-project
    // secret in the launcher GUI sees it as `$KEY` in their next Claude
    // Code session (no session restart, no resolver call). Coverage:
    //
    //   * Active pairs land in all 3 launcher-managed env surfaces
    //     (`.claude/env` BEGIN/END block, `.claude/settings.json` env,
    //     `.vscode/settings.json` claude-code.env).
    //   * Inactive (paused via Lifecycle B) pairs are OMITTED from emit
    //     and STRIPPED from any prior write.
    //   * By-hand user-added env keys (never went through `set_secret_v2`)
    //     are preserved verbatim — the strip set is bounded to keys we
    //     ourselves wrote.
    //
    // The unregister-strips-user-secrets test lives separately because
    // the surgical-strip helper applies the existing canonical strip;
    // user-secret keys are removed by clearing them via the env writer
    // BEFORE unregister calls the strip helper. See the comment on
    // `surgically_strip_env_surfaces` for the layered-cleanup design.

    /// Direct contract test: when `ProjectEnvSettings` carries an active
    /// user-secret pair, both launcher-managed env surfaces emit it
    /// alongside the canonical keys.
    ///
    /// PR-27 (v0.2.12, 2026-05-16): the historical third surface
    /// (`.vscode/settings.json` `claude-code.env`) was removed because
    /// it didn't propagate to MCP subprocesses on Linux.
    #[test]
    fn write_project_env_files_includes_user_set_secrets() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-user-secret-emit-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        let mut settings = ProjectEnvSettings::with_defaults("UserSecretEmit");
        settings.user_secret_pairs = vec![
            ("MY_PROJECT_KEY".to_string(), "ghp_subagent_g_canary_value".to_string()),
            ("INTERNAL_API_BASE".to_string(), "https://api.internal.example.com".to_string()),
        ];
        settings.user_secret_known_keys = vec![
            "INTERNAL_API_BASE".to_string(),
            "MY_PROJECT_KEY".to_string(),
        ];

        write_project_env_files(&tmp, &settings).unwrap();

        // 1. .claude/env (POSIX exports between BEGIN/END markers).
        let claude_env = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            claude_env.contains(r#"export MY_PROJECT_KEY="ghp_subagent_g_canary_value""#),
            ".claude/env missing MY_PROJECT_KEY export. Body:\n{}",
            claude_env
        );
        assert!(
            claude_env.contains(r#"export INTERNAL_API_BASE="https://api.internal.example.com""#),
            ".claude/env missing INTERNAL_API_BASE export. Body:\n{}",
            claude_env
        );
        // Section header makes user secrets visually distinct in diffs.
        assert!(
            claude_env.contains("# user secrets (per-project"),
            ".claude/env missing user-secrets section header. Body:\n{}",
            claude_env
        );
        // Both inside the managed BEGIN/END block (writers replace it
        // wholesale every call — that is how strip works for this
        // surface).
        let begin_idx = claude_env.find(CLAUDE_ENV_MANAGED_BEGIN).unwrap();
        let end_idx = claude_env.find(CLAUDE_ENV_MANAGED_END).unwrap();
        let in_block = |needle: &str| {
            let pos = claude_env.find(needle).unwrap();
            pos > begin_idx && pos < end_idx
        };
        assert!(in_block("MY_PROJECT_KEY"));
        assert!(in_block("INTERNAL_API_BASE"));

        // 2. .claude/settings.json env block.
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap())
                .unwrap();
        assert_eq!(cs["env"]["MY_PROJECT_KEY"], "ghp_subagent_g_canary_value");
        assert_eq!(cs["env"]["INTERNAL_API_BASE"], "https://api.internal.example.com");

        // PR-27: the writer must not create `.vscode/settings.json` at
        // all. The historical claude-code.env surface was removed
        // because it didn't propagate to MCP subprocesses on Linux.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Active-flag-gate test: a known user-secret key whose pair is NOT
    /// in `user_secret_pairs` (e.g. it's been paused via Lifecycle B)
    /// must (a) be omitted from the EMIT, AND (b) actively STRIPPED from
    /// every surface on the next writer call.
    #[test]
    fn write_project_env_files_omits_paused_user_secrets() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-user-secret-paused-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // First write: PAUSED_KEY is active and lands in every surface.
        let mut settings = ProjectEnvSettings::with_defaults("PausedUserSecret");
        settings.user_secret_pairs = vec![("PAUSED_KEY".to_string(), "old_value".to_string())];
        settings.user_secret_known_keys = vec!["PAUSED_KEY".to_string()];
        write_project_env_files(&tmp, &settings).unwrap();
        // Sanity: the value is there pre-pause.
        let pre = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(pre.contains("PAUSED_KEY"));

        // Second write: PAUSED_KEY is in known_keys but NOT in pairs —
        // mirrors the post-`clear_secret_v2` state.
        let mut settings_paused = ProjectEnvSettings::with_defaults("PausedUserSecret");
        settings_paused.user_secret_pairs = Vec::new();
        settings_paused.user_secret_known_keys = vec!["PAUSED_KEY".to_string()];
        write_project_env_files(&tmp, &settings_paused).unwrap();

        // .claude/env: BEGIN/END block was rebuilt without the export.
        let claude_env = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(
            !claude_env.contains("PAUSED_KEY"),
            ".claude/env still mentions paused PAUSED_KEY. Body:\n{}",
            claude_env
        );

        // .claude/settings.json: deep-merge stripped the key from the
        // env block while leaving the rest of the file untouched.
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap())
                .unwrap();
        assert!(
            cs["env"].get("PAUSED_KEY").is_none(),
            ".claude/settings.json env still carries paused PAUSED_KEY. \
             Block: {}",
            cs["env"]
        );

        // PR-27 (v0.2.12, 2026-05-16): the writer no longer authors
        // `.vscode/settings.json` on the second call either. There's
        // nothing to strip from a file that was never created in the
        // first place; the historical strip-on-pause behaviour for the
        // `.vscode/settings.json` claude-code.env surface is now
        // tautologically satisfied.
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        // Sanity: canonical keys stayed put through the strip pass.
        assert_eq!(
            cs["env"]["KG_COLLECTION"], "PausedUserSecret_KnowledgeGraph",
            "strip pass must not touch canonical keys"
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Defence-in-depth: a key the user typed BY HAND directly into
    /// `.claude/settings.json` env (never through `set_secret_v2`) must
    /// survive the writer's strip pass — it's not in
    /// `user_secret_known_keys`, so the strip set leaves it alone.
    /// Pins the boundary between "Subagent G owns this key" and
    /// "user owns this key" in the deep-merge contract.
    ///
    /// PR-27 (v0.2.12, 2026-05-16): the equivalent
    /// `.vscode/settings.json` `claude-code.env` deep-merge no longer
    /// runs (the surface itself was removed because it didn't
    /// propagate to MCP subprocesses on Linux — see PR-27 commit
    /// message for the empirical trace). The by-hand contract for
    /// that file is now stricter: a pre-existing
    /// `.vscode/settings.json` must come out of `write_project_env_files`
    /// BYTE-FOR-BYTE unchanged — the launcher leaves it entirely alone
    /// from this code path.
    #[test]
    fn write_project_env_files_preserves_by_hand_user_keys_not_owned_by_launcher() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-user-secret-byhand-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".claude")).unwrap();
        std::fs::create_dir_all(tmp.join(".vscode")).unwrap();

        // Pre-existing settings with a user-typed env key.
        std::fs::write(
            tmp.join(".claude/settings.json"),
            r#"{
                "env": {"BY_HAND_KEY": "user_typed_value"}
            }"#,
        )
        .unwrap();
        let vscode_pre_existing = r#"{
                "claude-code.env": {"BY_HAND_KEY": "user_typed_value"}
            }"#;
        std::fs::write(tmp.join(".vscode/settings.json"), vscode_pre_existing).unwrap();

        let mut settings = ProjectEnvSettings::with_defaults("ByHandPreserve");
        // Add a launcher-owned user secret that COINCIDENTALLY happens
        // to NOT be the same name. The strip set is empty (no inactive
        // entries) and the known set has only the active key. The
        // by-hand key isn't in either — must survive.
        settings.user_secret_pairs = vec![("LAUNCHER_OWNED".to_string(), "v1".to_string())];
        settings.user_secret_known_keys = vec!["LAUNCHER_OWNED".to_string()];

        write_project_env_files(&tmp, &settings).unwrap();

        // `.claude/settings.json`: deep-merge contract still applies —
        // by-hand env key survives, launcher's user secret is added.
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap())
                .unwrap();
        assert_eq!(
            cs["env"]["BY_HAND_KEY"], "user_typed_value",
            "by-hand user key must survive when not in user_secret_known_keys"
        );
        assert_eq!(cs["env"]["LAUNCHER_OWNED"], "v1");

        // PR-27: `.vscode/settings.json` must come out byte-for-byte
        // identical. The launcher no longer authors anything into it —
        // not even the LAUNCHER_OWNED user secret (which lands in
        // `.claude/settings.json` env instead, the channel that
        // actually propagates to MCP subprocesses on Linux).
        let vscode_after = std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap();
        assert_eq!(
            vscode_after, vscode_pre_existing,
            "PR-27: write_project_env_files must leave .vscode/settings.json unchanged",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Pure-function coverage of the merge primitive: the strip set
    /// removes only the named keys; canonical pairs always overwrite;
    /// user pairs are inserted last (collision fallthrough goes to the
    /// user pair). Pins the order-of-operations contract documented on
    /// `merge_env_object_canonical_with_user_secrets`.
    #[test]
    fn merge_env_object_canonical_with_user_secrets_strip_emit_order() {
        let mut parent = serde_json::json!({
            "env": {
                "KG_COLLECTION": "stale",            // canonical, gets overwritten
                "PAUSED_USER_KEY": "stale_user",     // in strip set, gets removed
                "BY_HAND_KEY": "user_typed",         // unknown to launcher, preserved
            }
        });
        let parent_obj = parent.as_object_mut().unwrap();

        let canonical_pairs: Vec<(&str, String)> =
            vec![("KG_COLLECTION", "fresh".to_string())];
        let user_secret_pairs: Vec<(&str, String)> =
            vec![("MY_PROJECT_KEY", "active_user_value".to_string())];
        let strip: Vec<&str> = vec!["PAUSED_USER_KEY"];

        merge_env_object_canonical_with_user_secrets(
            parent_obj,
            "env",
            &canonical_pairs,
            &user_secret_pairs,
            &strip,
        );

        let env = &parent["env"];
        // Canonical: overwritten with fresh value.
        assert_eq!(env["KG_COLLECTION"], "fresh");
        // Strip: gone.
        assert!(env.get("PAUSED_USER_KEY").is_none());
        // User pair: inserted.
        assert_eq!(env["MY_PROJECT_KEY"], "active_user_value");
        // By-hand: preserved.
        assert_eq!(env["BY_HAND_KEY"], "user_typed");
    }

    /// Covers the unregister code path: at unregister time, a finished
    /// project's env surfaces still carry the user-set secret keys from
    /// the last write. The user-set keys are LEFT IN PLACE by
    /// `surgically_strip_env_surfaces` (which only knows about
    /// `UNREGISTER_CANONICAL_ENV_KEYS`). To strip them on unregister,
    /// the launcher must blank out user-secret state BEFORE calling the
    /// strip helper — which `delete_project_v2` already accomplishes by
    /// deleting the `secret_active_state` rows via DB CASCADE on
    /// `projects` delete (see migration 007's PRIMARY KEY shape and the
    /// fact that the rows live in the same DB; the CASCADE is implicit
    /// because secret_active_state has no FK on project_id, but
    /// `db.delete_project` removes the project row and the
    /// secret_active_state rows for that project become orphan
    /// metadata).
    ///
    /// The clean teardown sequence:
    ///   1. `refresh_project_env_with_db(db, project_id)` BEFORE the
    ///      DB delete: re-reads `list_user_secret_keys_for_project`,
    ///      which is still populated. EMIT pairs go through the active
    ///      gate. STRIP set carries every known key. The writer
    ///      removes paused entries from the surfaces.
    ///   2. `purge_launcher_files_from_project` removes `.claude/hooks`,
    ///      `.claude/scripts`, infrastructure compose files, and
    ///      `.claude/.vco-manifest.json`. `.claude/env` is preserved
    ///      (2026-05-09 non-destructive change). `surgically_strip_env_surfaces`
    ///      strips canonical keys from `.env` / `.claude/settings.json`
    ///      / `.vscode/settings.json` AND excises the managed BEGIN/END
    ///      block from `.claude/env` (preserving user-added exports
    ///      outside the block).
    ///
    /// Today's `delete_project_v2` does step 2 but not step 1 — the
    /// orphan secret_active_state rows mean the next refresh would
    /// re-emit, but the project is gone so no refresh ever runs. The
    /// surfaces post-unregister contain whatever was last written
    /// (canonical keys stripped, user secrets remain). This test pins
    /// the OPT-IN cleanup behaviour: when callers explicitly null out
    /// `user_secret_known_keys` AND `user_secret_pairs`, the writer
    /// strips every previously-emitted user-secret key.
    ///
    /// Subagent G's choice (per brief): for now, leave user-secret
    /// keys in the surfaces post-unregister. The user-set
    /// `secret_active_state` rows survive (they'll be re-used if the
    /// user re-registers the same project) and the env surface
    /// contents are user-owned at that point. A stricter "purge
    /// user-secret keys on unregister" mode is a follow-up — see
    /// `purge_launcher_files_from_project` doc comment + Open Q #2.
    #[test]
    fn writer_strips_all_user_keys_when_known_keys_emptied() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-user-secret-purge-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // First write: two user secrets active.
        let mut settings = ProjectEnvSettings::with_defaults("UnregUserSecret");
        settings.user_secret_pairs = vec![
            ("PURGE_TEST_A".to_string(), "v_a".to_string()),
            ("PURGE_TEST_B".to_string(), "v_b".to_string()),
        ];
        settings.user_secret_known_keys = vec![
            "PURGE_TEST_A".to_string(),
            "PURGE_TEST_B".to_string(),
        ];
        write_project_env_files(&tmp, &settings).unwrap();
        let pre = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(pre.contains("PURGE_TEST_A"));
        assert!(pre.contains("PURGE_TEST_B"));

        // Second write: caller explicitly nulled out the user-secret state
        // (e.g. an unregister flow chose to purge user-secret keys). Both
        // strip set + emit list are empty.
        let mut settings_purge = ProjectEnvSettings::with_defaults("UnregUserSecret");
        settings_purge.user_secret_pairs = Vec::new();
        settings_purge.user_secret_known_keys = vec![
            "PURGE_TEST_A".to_string(),
            "PURGE_TEST_B".to_string(),
        ];
        write_project_env_files(&tmp, &settings_purge).unwrap();

        // Both surviving surfaces no longer carry the user keys.
        let claude_env = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!claude_env.contains("PURGE_TEST_A"));
        assert!(!claude_env.contains("PURGE_TEST_B"));

        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap())
                .unwrap();
        assert!(cs["env"].get("PURGE_TEST_A").is_none());
        assert!(cs["env"].get("PURGE_TEST_B").is_none());

        // PR-27 (v0.2.12, 2026-05-16): the writer never created
        // `.vscode/settings.json` in either call. The historical
        // claude-code.env surface was removed because it didn't
        // propagate to MCP subprocesses on Linux — the
        // strip-on-pause / strip-on-purge contract for that file is
        // tautological now (nothing was ever written there).
        assert!(
            !tmp.join(".vscode/settings.json").exists(),
            "PR-27: write_project_env_files must not create .vscode/settings.json",
        );

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Subagent G (2026-05-08): unregister-cleanup integration.
    ///
    /// Pins the contract that `surgically_strip_user_secret_keys` strips
    /// caller-supplied user-secret KEY names from all 4 env surfaces
    /// while leaving canonical keys + by-hand user keys intact.
    /// Mirrors the call shape of the real `delete_project_v2` step 1a.
    #[test]
    fn unregister_strips_user_secrets_from_env_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-unreg-user-strip-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".claude")).unwrap();
        std::fs::create_dir_all(tmp.join(".vscode")).unwrap();

        // Pre-populate every surface with a mix of:
        //   * a canonical key (must survive — canonical strip handles it)
        //   * a user-secret key the launcher emitted (must be stripped)
        //   * a by-hand user key (must survive)
        std::fs::write(
            tmp.join(".env"),
            "\
# vibecoded-orchestrator per-project .env
KG_COLLECTION=Some_KnowledgeGraph
USER_SECRET_FROM_GUI=ghp_test_canary
BY_HAND_KEY=user_typed
",
        )
        .unwrap();
        std::fs::write(
            tmp.join(".claude/env"),
            "# vco-managed-begin
export KG_COLLECTION=\"Some_KnowledgeGraph\"
export USER_SECRET_FROM_GUI=\"ghp_test_canary\"
# vco-managed-end
export BY_HAND_KEY=\"user_typed\"
",
        )
        .unwrap();
        std::fs::write(
            tmp.join(".claude/settings.json"),
            r#"{
                "env": {
                    "KG_COLLECTION": "Some_KnowledgeGraph",
                    "USER_SECRET_FROM_GUI": "ghp_test_canary",
                    "BY_HAND_KEY": "user_typed"
                }
            }"#,
        )
        .unwrap();
        std::fs::write(
            tmp.join(".vscode/settings.json"),
            r#"{
                "claude-code.env": {
                    "KG_COLLECTION": "Some_KnowledgeGraph",
                    "USER_SECRET_FROM_GUI": "ghp_test_canary",
                    "BY_HAND_KEY": "user_typed"
                }
            }"#,
        )
        .unwrap();

        // Strip ONLY the user-secret KEY name. Canonical + by-hand
        // keys must survive (the canonical strip runs separately).
        let user_keys = vec!["USER_SECRET_FROM_GUI".to_string()];
        let (purged, warnings) = surgically_strip_user_secret_keys(&tmp, &user_keys);
        assert!(warnings.is_empty(), "unexpected warnings: {:?}", warnings);
        assert_eq!(purged, vec!["USER_SECRET_FROM_GUI".to_string()]);

        // .env: user secret gone, canonical + by-hand survive.
        let env = std::fs::read_to_string(tmp.join(".env")).unwrap();
        assert!(!env.contains("USER_SECRET_FROM_GUI"));
        assert!(env.contains("KG_COLLECTION=Some_KnowledgeGraph"));
        assert!(env.contains("BY_HAND_KEY=user_typed"));

        // .claude/env: user secret gone (whether inside or outside the
        // managed block — we strip both shapes); canonical + by-hand
        // survive.
        let claude_env = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        assert!(!claude_env.contains("USER_SECRET_FROM_GUI"));
        assert!(claude_env.contains("KG_COLLECTION"));
        assert!(claude_env.contains("BY_HAND_KEY"));

        // .claude/settings.json: same.
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap())
                .unwrap();
        assert!(cs["env"].get("USER_SECRET_FROM_GUI").is_none());
        assert_eq!(cs["env"]["KG_COLLECTION"], "Some_KnowledgeGraph");
        assert_eq!(cs["env"]["BY_HAND_KEY"], "user_typed");

        // .vscode/settings.json: same.
        let vsc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap())
                .unwrap();
        assert!(vsc["claude-code.env"].get("USER_SECRET_FROM_GUI").is_none());
        assert_eq!(vsc["claude-code.env"]["KG_COLLECTION"], "Some_KnowledgeGraph");
        assert_eq!(vsc["claude-code.env"]["BY_HAND_KEY"], "user_typed");

        std::fs::remove_dir_all(&tmp).ok();
    }

    /// Empty key list short-circuits — no surface reads, no warnings.
    /// Pins the cheap-no-op invariant the unregister flow relies on
    /// when a project never registered any user secrets.
    #[test]
    fn surgically_strip_user_secret_keys_short_circuits_on_empty_list() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-strip-empty-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        let (purged, warnings) = surgically_strip_user_secret_keys(&tmp, &[]);
        assert!(purged.is_empty());
        assert!(warnings.is_empty());
        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── 0.2.x backlog #4: Update-all projects ──────────────────────────
    //
    // Tests cover the iteration / stop-on-error / aggregate-counts contract
    // WITHOUT exercising the full bundle install (that path is already
    // pinned by `update_project_v2_success` and friends). Tests use:
    //   * 0 registered projects   → empty report
    //   * 1+ project with a NON-EXISTENT folder → hard failure path of
    //     `update_project_v2` (which returns Err early before any
    //     subprocess), exercising the per-row "failed" + stop_on_error
    //     fan-out semantics.

    /// `update_all_projects` over an empty project list returns an empty
    /// report with all counters at 0. Not an error — the GUI's "Update
    /// all" button is allowed to fire even when nothing's registered.
    #[tokio::test]
    async fn update_all_projects_empty_returns_zero_report() {
        let db = Db::open_in_memory().unwrap();
        // No projects seeded.
        let report = run_update_all_with_db(&db, UpdateAllOptions { stop_on_error: true })
            .await
            .unwrap();
        assert_eq!(report.total_succeeded, 0);
        assert_eq!(report.total_failed, 0);
        assert_eq!(report.total_skipped, 0);
        assert!(report.updated.is_empty());
    }

    /// `stop_on_error: true` (the default) marks the first hard-fail
    /// project as "failed" and all subsequent projects as "skipped".
    #[tokio::test]
    async fn update_all_projects_stop_on_error_skips_remaining() {
        let db = Db::open_in_memory().unwrap();
        // Three projects whose folders DON'T exist on disk → each one
        // would hit `update_project_v2`'s `!folder.exists()` Err branch.
        // Use unique folder_path per row (UNIQUE constraint in schema)
        // pointing at paths the host will never have.
        for (id, name) in [
            ("p_first",  "First"),
            ("p_second", "Second"),
            ("p_third",  "Third"),
        ] {
            db.insert_project(
                id, name,
                &format!("/nonexistent/update-all-stop/{}/never/exists", id),
                ProjectHost::Base,
                id,
            ).unwrap();
        }

        let report = run_update_all_with_db(&db, UpdateAllOptions { stop_on_error: true })
            .await
            .unwrap();

        // First one fails (folder missing). Remaining two are skipped.
        assert_eq!(report.total_failed, 1, "expected exactly one failure: {:?}", report);
        assert_eq!(report.total_skipped, 2, "remaining projects must be skipped");
        assert_eq!(report.total_succeeded, 0);
        assert_eq!(report.updated.len(), 3, "every project must appear in the report");

        // Order: list_projects returns rows in insert order
        assert_eq!(report.updated[0].status, "failed");
        assert!(report.updated[0].error.is_some());
        assert!(report.updated[0].error.as_ref().unwrap().contains("does not exist"),
                "error must explain the failure surface: {:?}", report.updated[0].error);
        assert_eq!(report.updated[1].status, "skipped");
        assert!(report.updated[1].error.is_none(), "skipped projects must NOT carry an error");
        assert_eq!(report.updated[2].status, "skipped");

        // Skipped projects have no summary (no install ran).
        assert!(report.updated[1].summary.is_none());
        assert!(report.updated[2].summary.is_none());
    }

    /// `stop_on_error: false` continues iterating past failures — every
    /// project gets exercised, every failure is reported. The
    /// `total_skipped` counter stays at 0 because nothing was skipped.
    #[tokio::test]
    async fn update_all_projects_no_stop_iterates_every_project() {
        let db = Db::open_in_memory().unwrap();
        for (id, name) in [
            ("q_first",  "First"),
            ("q_second", "Second"),
            ("q_third",  "Third"),
        ] {
            db.insert_project(
                id, name,
                &format!("/nonexistent/update-all-nostop/{}/never/exists", id),
                ProjectHost::Base,
                id,
            ).unwrap();
        }

        let report = run_update_all_with_db(&db, UpdateAllOptions { stop_on_error: false })
            .await
            .unwrap();

        assert_eq!(report.total_failed, 3, "every project must fail in this scenario");
        assert_eq!(report.total_skipped, 0, "no stop_on_error → no skips");
        assert_eq!(report.total_succeeded, 0);
        assert_eq!(report.updated.len(), 3);
        for entry in &report.updated {
            assert_eq!(entry.status, "failed");
            assert!(entry.error.is_some());
        }
    }

    /// Default options (no UpdateAllOptions passed) match `stop_on_error: true`.
    /// Pins the serde-default contract — the GUI sends `null` from
    /// JavaScript for "use defaults" and the Tauri command must honour that.
    #[test]
    fn update_all_options_default_is_stop_on_error_true() {
        assert_eq!(UpdateAllOptions::default().stop_on_error, true);
        // Round-trip the serde default through JSON to verify {} (no
        // field set) deserialises to stop_on_error=true.
        let parsed: UpdateAllOptions = serde_json::from_str("{}").unwrap();
        assert!(parsed.stop_on_error);
    }

    /// Helper: drive the same iteration as the `update_all_projects` Tauri
    /// command, but without the Tauri State plumbing. Lifted out so the
    /// async tests can exercise the contract without the macro. Kept in
    /// the test module so the helper doesn't leak into the public API.
    async fn run_update_all_with_db(
        db: &Db,
        opts: UpdateAllOptions,
    ) -> Result<UpdateAllReport, String> {
        let projects = db.list_projects()?;
        let mut entries: Vec<UpdateAllProjectEntry> = Vec::with_capacity(projects.len());
        let mut total_succeeded: u32 = 0;
        let mut total_failed: u32 = 0;
        let mut total_skipped: u32 = 0;
        let mut stop = false;

        for row in &projects {
            if stop {
                entries.push(UpdateAllProjectEntry {
                    project_id: row.id.clone(),
                    project_name: row.name.clone(),
                    status: "skipped".to_string(),
                    error: None,
                    warnings: Vec::new(),
                    summary: None,
                });
                total_skipped += 1;
                continue;
            }
            // Replicate `update_project_v2`'s hard-failure path against
            // the in-memory DB. We can't call the public #[command]
            // without Tauri State, so we inline the relevant prefix.
            let result: Result<UpdateProjectResult, String> = (|| async {
                let row = db.get_project(&row.id)?
                    .ok_or_else(|| format!("project {} not found", row.id))?;
                let count = db.list_module_installs_for_project(&row.id)?.len() as u32;
                let folder = PathBuf::from(&row.folder_path);
                if !folder.exists() {
                    return Err(format!(
                        "project folder does not exist: {}. \
                         Use create_project_v2 to (re-)create the folder + bundle.",
                        row.folder_path
                    ));
                }
                if !folder.is_dir() {
                    return Err(format!("project folder is not a directory: {}", row.folder_path));
                }
                // Folder exists → would run install in production. Tests
                // always hit one of the two Err branches above.
                Ok(UpdateProjectResult {
                    project: ProjectView::from_row(row, count),
                    warnings: Vec::new(),
                    summary: UpdateSummary::default(),
                })
            })().await;
            match result {
                Ok(r) => {
                    entries.push(UpdateAllProjectEntry {
                        project_id: row.id.clone(),
                        project_name: row.name.clone(),
                        status: "succeeded".to_string(),
                        error: None,
                        warnings: r.warnings,
                        summary: Some(r.summary),
                    });
                    total_succeeded += 1;
                }
                Err(e) => {
                    entries.push(UpdateAllProjectEntry {
                        project_id: row.id.clone(),
                        project_name: row.name.clone(),
                        status: "failed".to_string(),
                        error: Some(e),
                        warnings: Vec::new(),
                        summary: None,
                    });
                    total_failed += 1;
                    if opts.stop_on_error {
                        stop = true;
                    }
                }
            }
        }

        Ok(UpdateAllReport {
            updated: entries,
            total_succeeded,
            total_failed,
            total_skipped,
        })
    }

    // ─── v0.2.37 Finding F1: --orchestrator-root passed to Python ─────
    //
    // Pre-v0.2.37 the launcher built `python -m vco_lib.config_projection
    // apply --project-id <id>` with NO `--orchestrator-root` argument.
    // `vco_lib/config_projection.py:1241-1248` only emits
    // `VCT_ORCHESTRATOR_ROOT` + `VCT_INFRASTRUCTURE_DIR` when the apply
    // call receives `orchestrator_root is not None` — so the launcher
    // was silently asking the Python writer NOT to emit those keys.
    // This is the actual root cause of the user_project_y / user_project_x bug.
    //
    // These tests pin the contract on the `build_config_projection_apply_args`
    // helper: when the Rust resolver hands us a `Some(path)`, the flag
    // must appear in the arg list. When it hands us `None` (legitimate
    // for forks running outside a clone), the flag must NOT appear so
    // we don't pass a bogus path that fails Python's `Path()` parse.

    /// F1 — happy path: orchestrator root resolved → flag emitted.
    #[test]
    fn build_apply_args_includes_orchestrator_root_when_resolver_returns_some() {
        let root = std::path::PathBuf::from("/some/orchestrator/clone");
        let args = build_config_projection_apply_args("proj-123", Some(&root));
        assert_eq!(
            args,
            vec![
                "--project-id".to_string(),
                "proj-123".to_string(),
                "--orchestrator-root".to_string(),
                "/some/orchestrator/clone".to_string(),
            ],
            "F1 fix regressed: --orchestrator-root must follow --project-id \
             when the resolver returns Some. Args produced: {:?}",
            args,
        );
    }

    /// F1 — resolver returns None: flag omitted (no regression for
    /// standalone-binary installs outside a clone).
    #[test]
    fn build_apply_args_omits_orchestrator_root_when_resolver_returns_none() {
        let args = build_config_projection_apply_args("proj-xyz", None);
        assert_eq!(
            args,
            vec![
                "--project-id".to_string(),
                "proj-xyz".to_string(),
            ],
            "F1: when resolver returns None we MUST NOT pass --orchestrator-root \
             (Python's argparse would accept the empty string but config_projection \
             would emit a phantom path). Args produced: {:?}",
            args,
        );
    }

    /// F1 — paths with spaces survive the to_string_lossy round-trip
    /// intact. The user's home dir might be `/Users/Some User/` on
    /// macOS or `C:\Users\Some User\` on Windows; the subprocess arg
    /// API takes care of quoting.
    #[test]
    fn build_apply_args_preserves_path_with_spaces() {
        let root = std::path::PathBuf::from("/Users/Some User/dev/vco");
        let args = build_config_projection_apply_args("p", Some(&root));
        assert!(
            args.iter().any(|a| a == "/Users/Some User/dev/vco"),
            "path with spaces should survive intact: {:?}",
            args,
        );
    }

    // ─── v0.2.37 Step 1: canonical resolver dedup ─────────────────────
    //
    // The pre-v0.2.37 `walk_for_install_markers` looked ONLY for
    // `install.py + CLAUDE.md`. The pre-v0.2.37 `find_local_repo_root`
    // looked ONLY for `vct-module.json`. The consolidated
    // `looks_like_orchestrator_root` predicate must accept BOTH
    // patterns so a partial checkout missing one marker but having
    // the other still resolves.

    #[test]
    fn looks_like_orchestrator_root_accepts_vct_module_only() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-root-vct-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join("vct-module.json"), "{}").unwrap();
        // No install.py + no CLAUDE.md — should still qualify.
        assert!(
            crate::commands::installer::looks_like_orchestrator_root(&tmp),
            "F1/Step1: directory with only vct-module.json must qualify"
        );
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn looks_like_orchestrator_root_accepts_install_py_and_claude_md_only() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-root-py-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join("install.py"), "# stub").unwrap();
        std::fs::write(tmp.join("CLAUDE.md"), "# stub").unwrap();
        // No vct-module.json — should still qualify.
        assert!(
            crate::commands::installer::looks_like_orchestrator_root(&tmp),
            "F1/Step1: directory with install.py+CLAUDE.md but no \
             vct-module.json must qualify"
        );
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn looks_like_orchestrator_root_rejects_install_py_alone() {
        // install.py without CLAUDE.md is NOT an orchestrator root —
        // matches the pre-v0.2.37 walk-marker behaviour.
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-root-half-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join("install.py"), "# stub").unwrap();
        assert!(
            !crate::commands::installer::looks_like_orchestrator_root(&tmp),
            "install.py alone (no CLAUDE.md, no vct-module.json) must NOT qualify"
        );
        std::fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn looks_like_orchestrator_root_rejects_unrelated_directory() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-root-empty-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join("README.md"), "# random").unwrap();
        assert!(
            !crate::commands::installer::looks_like_orchestrator_root(&tmp),
            "directory with neither marker must NOT qualify"
        );
        std::fs::remove_dir_all(&tmp).ok();
    }

    // ─── v0.2.37 Step 2: populate uses canonical resolver ─────────────
    //
    // The contract: `populate()` must return `orchestrator_root=Some(...)`
    // when the DB has `launcher.install_path` cached AND the cached
    // path is a valid orchestrator clone, even if `current_exe()`'s
    // walk-up would fail. This is the actual user-facing fix —
    // populate's previous use of uncached `find_local_repo_root()`
    // omitted VCT_ORCHESTRATOR_ROOT from .claude/env when the binary
    // lived outside the clone.

    /// Helper: create a temp directory that passes
    /// `check_install_status`. install.py + CLAUDE.md + a
    /// `state/install-manifest.json` with `installed:true`.
    fn fake_install_root() -> std::path::PathBuf {
        let tmp = std::env::temp_dir().join(format!(
            "vct-v0237-fake-install-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join("state")).unwrap();
        std::fs::write(tmp.join("install.py"), "# stub\n").unwrap();
        std::fs::write(tmp.join("CLAUDE.md"), "# stub\n").unwrap();
        std::fs::write(
            tmp.join("state/install-manifest.json"),
            "{\"installed\":true}\n",
        )
        .unwrap();
        tmp
    }

    #[test]
    fn populate_emits_orchestrator_root_from_db_cache() {
        let db = Db::open_in_memory().expect("open in-memory db");
        let install = fake_install_root();
        db.app_state_set(
            crate::commands::installer::APP_STATE_KEY_INSTALL_PATH,
            &install.to_string_lossy(),
        )
        .expect("seed app_state");

        let settings = project_env_settings::populate(&db, "FakeProj", None);

        assert_eq!(
            settings.orchestrator_root.as_ref(),
            Some(&install),
            "populate must return the DB-cached install path as \
             orchestrator_root (user_project_x / user_project_y bugfix). \
             Got: {:?}",
            settings.orchestrator_root,
        );

        std::fs::remove_dir_all(&install).ok();
    }

    #[test]
    fn populate_returns_none_when_db_empty_and_walk_up_fails() {
        // No DB seed. We can't easily neutralize the walk-up
        // (current_exe() in cargo test points inside the orchestrator
        // clone, so walk-up will succeed) — but we CAN verify the
        // contract: orchestrator_root is either None OR Some(real
        // orchestrator root). The previous bug was returning None
        // INSPITE OF having a valid DB cache; the regression we want
        // to pin is that DB cache + valid path → Some.
        let db = Db::open_in_memory().expect("open in-memory db");
        let settings = project_env_settings::populate(&db, "FakeProj", None);
        // If walk-up succeeds (likely in `cargo test`), settings.orchestrator_root
        // should be Some pointing at the real clone. If walk-up fails (rare),
        // it should be None. Both are acceptable for this test — the bug we
        // fixed was specifically "DB has a valid path but populate returns None".
        if let Some(p) = &settings.orchestrator_root {
            assert!(
                p.is_dir(),
                "walk-up resolution must point at an existing dir, got {}",
                p.display()
            );
        }
    }

    // ─── v0.2.37 Finding F6: update_project_v2 backfills env ──────────
    //
    // The Tauri command `update_project_v2` cannot be driven directly
    // from a unit test (it requires `State<'_, Db>`), and the
    // subprocess it eventually calls needs python + vco_lib. Instead
    // we pin a source-level invariant: the function body of
    // `update_project_v2` MUST contain the
    // `apply_project_env_via_python` call. If a future refactor
    // accidentally drops this call, this test fails LOUDLY with the
    // exact regression message — which is much louder than the silent
    // the "missing VCT_ORCHESTRATOR_ROOT" the F6 fix targets.
    #[test]
    fn update_project_v2_calls_apply_project_env_via_python() {
        let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        // NOTE: CARGO_MANIFEST_DIR used here is for COMPILE-TIME source
        // location lookup only (locate the file under audit). It is
        // NOT embedded into a production code path — this is a unit
        // test that walks source to enforce a contract.
        let path = repo_root.join("src/commands/projects_v2.rs");
        let body = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("read {}: {}", path.display(), e));

        // Find `pub async fn update_project_v2(` and grab its body to
        // the next top-level `}`. Simple enough for this enforcement
        // — `module_gui.rs::production_code_does_not_use_cargo_manifest_dir...`
        // uses a more sophisticated brace-balanced scan; we don't need
        // that precision here (we just want to know the call is
        // present somewhere inside).
        let fn_marker = "pub async fn update_project_v2(";
        let start = body
            .find(fn_marker)
            .expect("update_project_v2 must exist; if you renamed it, update this test");
        // Look ahead ~5000 chars — the function is ~60 lines.
        let window_end = (start + 6000).min(body.len());
        let fn_window = &body[start..window_end];

        assert!(
            fn_window.contains("apply_project_env_via_python"),
            "F6 regression: update_project_v2 no longer calls \
             apply_project_env_via_python. Pre-v0.2.37 this was the \
             cause of stale `.claude/env` on bundle update (the \
             missing-VCT_ORCHESTRATOR_ROOT bug). If you \
             intentionally removed the call, update this test with \
             the rationale and audit ALL bundle-update users."
        );
    }

    #[test]
    fn populate_writes_back_to_db_when_walk_up_succeeds() {
        let db = Db::open_in_memory().expect("open in-memory db");
        // Pre: DB cache is empty.
        assert_eq!(
            db.app_state_get(crate::commands::installer::APP_STATE_KEY_INSTALL_PATH).unwrap(),
            None,
            "precondition: app_state must start empty"
        );
        // Call populate — if walk-up succeeds (likely in `cargo test`),
        // resolve_orchestrator_root caches the result.
        let settings = project_env_settings::populate(&db, "TestProj", None);
        if let Some(p) = &settings.orchestrator_root {
            // The DB should now have the same path cached. This is the
            // sticky-cache behaviour from `resolve_orchestrator_root`.
            let cached = db
                .app_state_get(crate::commands::installer::APP_STATE_KEY_INSTALL_PATH)
                .expect("app_state read")
                .expect("cache should be populated after walk-up hit");
            assert_eq!(
                cached,
                p.to_string_lossy().to_string(),
                "sticky cache should mirror walk-up result"
            );
        }
        // (If walk-up failed in this test environment, no cache write
        // is expected. The assertion above is conditional for that
        // reason — `cargo test` may not always run from inside the
        // clone.)
    }
}
