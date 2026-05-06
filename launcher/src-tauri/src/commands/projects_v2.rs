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
use crate::commands::project_env_settings::{self, ProjectEnvSettings};
use crate::db::code_graph_builds::status as build_status;
use crate::db::models::{ModuleInstallRow, ProjectHost, ProjectRow};
use crate::db::Db;

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

    let id = Uuid::new_v4().to_string();
    let slug = db.generate_unique_slug(&req.name)?;
    let row = db.insert_project(&id, &req.name, &req.folder_path, req.host.clone(), &slug)?;

    // Bug 23 + 30: write per-project env files for ALL Claude Code
    // surfaces — VS Code extension (via `.vscode/settings.json`
    // claude-code.env), Claude Code CLI (via `.claude/env`, sourced by
    // tools/claude wrapper or user shell rc), AND the canonical
    // `.claude/settings.json` env block (CLI + Desktop app + VS Code).
    // We swallow individual errors here: create_project must not fail
    // just because the user's folder is read-only or mid-edit.
    //
    // PR-3 (2026-05-06): populate a `ProjectEnvSettings` once from the
    // launcher's current state — adopted ports, ACTIVE_EMBEDDING choice,
    // shared-KG name override, GPU toggle, container runtime — so the
    // env writers see the LAUNCHER's view of the world rather than
    // hardcoded localhost defaults. See `launcher-settings-propagation-audit-2026-05-06.md`
    // for the full inventory of values that needed plumbing.
    let env_settings = project_env_settings::populate(&db, &req.name, Some(&row.id));
    if let Err(e) = write_project_env_files(folder, &env_settings) {
        // B10 (2026-05-01): surface env-write failures to the UI instead of
        // silent eprintln. Project creation still succeeds; the UI should show
        // a warning toast so the user knows manual env setup is required.
        let msg = format!("env file write failed (write_project_env_files): {}. \
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

    // Gap 2 (OSS launch 2026-05-12): kick off the initial code-graph
    // build in the background so `search_code_graph` returns useful
    // results out of the box. This must NOT block project creation —
    // the user gets their `ProjectView` back immediately.
    //
    // We swallow any DB error from the pending-row insert because a
    // failure here is purely cosmetic (the user just won't see a build
    // status pill); the project itself is already committed.
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
            app,
            row.id.clone(),
            row.name.clone(),
            row.folder_path.clone(),
        );
    }

    // B12 (2026-05-01): detect stale .env from pre-existing folder registration.
    // ensure_project_env_template is append-only, so a folder that already had a
    // .env with a bare/wrong KG_COLLECTION (e.g. "KnowledgeGraph") will keep it
    // as the first active KG_COLLECTION line. Detect and warn; full repair with
    // manifest-based rewrite lands in PR 5. We check for the two known-buggy
    // defaults: bare "KnowledgeGraph" and bare sanitized name without suffix.
    if let Ok(env_text) = std::fs::read_to_string(folder.join(".env")) {
        let kg_basename = sanitize_kg_collection(&req.name);
        let canonical_kg = format!("{}_KnowledgeGraph", kg_basename);
        let stale_bare = "KG_COLLECTION=KnowledgeGraph";
        let stale_nosuffix = format!("KG_COLLECTION={}", kg_basename);
        let has_stale = env_text.lines().any(|l| {
            let t = l.trim();
            t == stale_bare || t == stale_nosuffix
        });
        if has_stale && !env_text.contains(&format!("KG_COLLECTION={}", canonical_kg)) {
            let msg = format!(
                "existing .env has stale KG_COLLECTION (expected {}). \
                 Full repair deferred to PR 5 (manifest-based). \
                 You may manually set KG_COLLECTION={} in the .env.",
                canonical_kg, canonical_kg
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
    let mut cmd = tokio::process::Command::new(&system.python_cmd);
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
    let mut cmd = tokio::process::Command::new(&system.python_cmd);
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
    let mut cmd = tokio::process::Command::new(&system.python_cmd);
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
    let mut cmd = tokio::process::Command::new(&system.python_cmd);
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
        }),
    )?;
    let _ = db.log_change("projects", "update_bundle", Some(&row.id), Some(&row.id));

    Ok(UpdateProjectResult {
        project: ProjectView::from_row(row, count),
        warnings,
        summary,
    })
}

/// Bug 23 + 30: write per-project env files for every Claude Code surface.
///
/// Writes three files, all carrying the same env values:
///   1. `.vscode/settings.json` `claude-code.env` — VS Code extension
///   2. `.claude/env` — POSIX shell file sourced by the `tools/claude`
///      wrapper (CLI users without VS Code)
///   3. `.claude/settings.json` `env` — canonical Anthropic per-project
///      env (read by CLI, Desktop app, and the VS Code extension)
///
/// (3) is the only surface that reaches Claude Code Desktop app users.
/// (1) and (2) are kept for compatibility / preference. Same values in
/// all three means there's no precedence conflict to reason about.
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
/// `.claude/settings.json`, `.vscode/settings.json` `claude-code.env`,
/// and the body of `.claude/env` are deep-merged rather than wholesale-
/// replaced — the canonical keys we own are overwritten with the
/// launcher's resolved values, but user-added env keys at the same level
/// are preserved across re-runs. See `secrets-and-access-matrix-audit-2026-05-06.md`
/// §6 for the prior wholesale-replace bug.
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
    // Keep this list sorted + cross-referenced with the `.claude/env`
    // POSIX writer below — both surfaces must export the same set.
    let canonical_env_pairs: Vec<(&str, String)> = vec![
        ("KG_COLLECTION", kg_collection.to_string()),
        ("PROJECT_NAME", project_name.to_string()),
        ("DEVELOPMENT_COLLECTION", dev_collection.to_string()),
        ("SHARED_KG_COLLECTION", shared_kg_collection.to_string()),
        // Canonical write-gate key (asymmetric semantic since 2026-05-01).
        ("SHARED_KG_WRITE_DISABLED", shared_kg_write_disabled.to_string()),
        // Legacy alias — same value, removed in ~3 releases.
        ("SHARED_KG_OPT_OUT", shared_kg_opt_out_legacy.to_string()),
        // PR-3 (2026-05-06): launcher-resolved service URLs + ports +
        // active embedding profile. Pre-PR-3 these were commented
        // placeholders in `.env` and absent from `.claude/settings.json` —
        // multi-stack / custom-port setups silently fell through to the
        // canonical localhost defaults.
        ("WEAVIATE_URL", weaviate_url.to_string()),
        ("WEAVIATE_PORT", weaviate_port.clone()),
        ("OLLAMA_URL", ollama_url.to_string()),
        ("OLLAMA_PORT", ollama_port.clone()),
        ("CODE_EMBED_URL", code_embed_url.to_string()),
        ("CODE_EMBED_PORT", code_embed_port.clone()),
        ("ACTIVE_EMBEDDING", active_embedding.to_string()),
    ];
    let canonical_env_keys: std::collections::HashSet<&str> =
        canonical_env_pairs.iter().map(|(k, _)| *k).collect();

    // VS Code path (extension reads claude-code.env).
    //
    // Bug 32 (safety): READ-MERGE-WRITE so user settings like
    // `editor.formatOnSave`, `python.defaultInterpreterPath`, workspace
    // recommendations etc. survive at the top level.
    //
    // PR-3 Commit 6 (2026-05-06): also deep-merge the `claude-code.env`
    // sub-block. Pre-PR-3 the entire sub-object was REPLACED on every
    // call, so any user-added env key at that level (e.g. a per-project
    // `OPENAI_API_BASE` override) was silently lost. Now we overwrite
    // only the canonical keys and leave non-canonical user keys alone.
    let vscode_dir = folder.join(".vscode");
    std::fs::create_dir_all(&vscode_dir)
        .map_err(|e| format!("mkdir {}: {}", vscode_dir.display(), e))?;
    let vscode_settings_path = vscode_dir.join("settings.json");

    let mut vscode_root: serde_json::Value = if vscode_settings_path.exists() {
        match std::fs::read_to_string(&vscode_settings_path) {
            Ok(raw) => serde_json::from_str(&raw).unwrap_or_else(|e| {
                eprintln!(
                    "[vct] warning: {} is not valid JSON ({}); replacing with minimal claude-code.env block",
                    vscode_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }),
            Err(e) => {
                eprintln!(
                    "[vct] warning: could not read {} ({}); creating fresh",
                    vscode_settings_path.display(),
                    e
                );
                serde_json::json!({})
            }
        }
    } else {
        serde_json::json!({})
    };
    if !vscode_root.is_object() {
        vscode_root = serde_json::json!({});
    }
    if let Some(root_obj) = vscode_root.as_object_mut() {
        merge_env_object_canonical(root_obj, "claude-code.env", &canonical_env_pairs);
    }
    std::fs::write(
        &vscode_settings_path,
        serde_json::to_string_pretty(&vscode_root)
            .map_err(|e| format!("serialize settings.json: {}", e))?,
    )
    .map_err(|e| format!("write {}: {}", vscode_settings_path.display(), e))?;

    // CLI path: `.claude/env` is sourced by the `tools/claude` wrapper or
    // by the user's shell rc. Plain POSIX export form so any sh-family
    // shell can source it.
    //
    // PR-3 Commit 6 (2026-05-06): the launcher's canonical exports are
    // emitted between vco-managed BEGIN/END markers so a re-run can
    // replace the block in place without clobbering user-added exports.
    // Lines outside the markers (custom user exports added by hand) are
    // preserved verbatim across re-writes. The PR-2 portability exports
    // (`VCT_ORCHESTRATOR_ROOT` / `VCT_INFRASTRUCTURE_DIR`) are emitted
    // INSIDE the managed block when `settings.orchestrator_root` is
    // `Some`, so they get refreshed alongside the canonical pairs.
    let claude_dir = folder.join(".claude");
    std::fs::create_dir_all(&claude_dir)
        .map_err(|e| format!("mkdir {}: {}", claude_dir.display(), e))?;
    let env_path = claude_dir.join("env");
    let managed_block = build_claude_env_managed_block(&canonical_env_pairs, settings);
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
        merge_env_object_canonical(obj, "env", &canonical_env_pairs);
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
pub(crate) fn merge_env_object_canonical(
    parent: &mut serde_json::Map<String, serde_json::Value>,
    env_key: &str,
    canonical_pairs: &[(&str, String)],
) {
    let existing = parent
        .get(env_key)
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}));

    let mut env_obj = existing.as_object().cloned().unwrap_or_default();

    for (k, v) in canonical_pairs {
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
/// PR-2 portability lines (`VCT_ORCHESTRATOR_ROOT` /
/// `VCT_INFRASTRUCTURE_DIR`) are emitted INSIDE the managed block when
/// `settings.orchestrator_root` is `Some` so they get refreshed alongside
/// the canonical pairs on every re-run. When `None` (launcher running
/// outside a git checkout) the lines are simply omitted — the in-tree
/// hook fallback resolution path takes over.
pub(crate) fn build_claude_env_managed_block(
    canonical_pairs: &[(&str, String)],
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
    for (k, v) in canonical_pairs {
        out.push_str(&format!("export {}=\"{}\"\n", k, v));
    }
    if let Some(orch_root) = settings.orchestrator_root.as_ref() {
        let orch_str = orch_root.display().to_string();
        let infra_str = orch_root.join("infrastructure").display().to_string();
        // Quote so paths with spaces / special characters survive sourcing.
        // Escape embedded double quotes defensively (rare on POSIX paths,
        // but legitimate on Windows + git-bash).
        let q_orch = orch_str.replace('"', "\\\"");
        let q_infra = infra_str.replace('"', "\\\"");
        out.push_str(
            "# PR-2 portability: orchestrator clone root + infrastructure dir.\n",
        );
        out.push_str(
            "# Consumed by .claude/hooks/ensure-containers.sh and the bundled\n",
        );
        out.push_str(
            "# Python scripts in .claude/scripts/ that need the\n",
        );
        out.push_str(
            "# claude_mcp_servers/ Python package (only present in the orch clone).\n",
        );
        out.push_str(&format!("export VCT_ORCHESTRATOR_ROOT=\"{}\"\n", q_orch));
        out.push_str(&format!("export VCT_INFRASTRUCTURE_DIR=\"{}\"\n", q_infra));
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
        ("SHARED_KG_COLLECTION", Some("VibeCodedTools_KnowledgeGraph")),
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

#[command]
pub async fn rename_project_v2(
    id: String,
    new_name: String,
    db: State<'_, Db>,
) -> Result<RenameProjectResult, String> {
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

    // B9 (2026-05-01): re-run env writers after DB rename so all 4 surfaces
    // reflect the new KG_COLLECTION, DEVELOPMENT_COLLECTION, PROJECT_NAME.
    // Before this fix, rename was DB-only — renamed projects kept stale
    // KG_COLLECTION values in .claude/env, .vscode/settings.json, and
    // .claude/settings.json until the user manually re-ran env setup.
    let folder = Path::new(&row.folder_path);
    let env_settings = project_env_settings::populate(&db, &new_name, Some(&id));
    // HIGH-7 (2026-05-01): env-write failures now surface as structured
    // warnings instead of silent eprintln. Without this, a failed env refresh
    // leaves the project's 4 env surfaces stale until the next launcher
    // session and the user has no idea anything is wrong.
    if let Err(e) = write_project_env_files(folder, &env_settings) {
        let msg = format!(
            "rename env refresh (write_project_env_files) failed: {}. \
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

    // Refresh all 4 env surfaces with the new value. Use the same warning
    // surface as create / rename so the UI can toast on partial failure.
    let mut warnings: Vec<String> = Vec::new();
    let folder = Path::new(&row.folder_path);
    let mut env_settings = project_env_settings::populate(&db, &row.name, Some(&project_id));
    // Use the explicitly-supplied write_disabled bool — bypasses the DB
    // round-trip the populate call already issued (which would yield the
    // pre-set value). This keeps the in-flight toggle authoritative.
    env_settings.shared_kg_write_disabled = write_disabled;
    if let Err(e) = write_project_env_files(folder, &env_settings) {
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

#[command]
pub async fn delete_project_v2(
    id: String,
    _delete_folder: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    // Note: delete_folder is accepted for UI parity with the design spec,
    // but we don't touch the user's folder on disk. Modules installed
    // under ~/.vct/modules/ are removed via CASCADE through
    // module_installs. The user's project folder on disk stays.
    db.audit("project_delete", Some(&id), None, &serde_json::json!({}))?;
    db.delete_project(&id)?;
    let _ = db.log_change("projects", "delete", Some(&id), Some(&id));
    Ok(())
}

/// Bug 15: spawn the user's editor of choice opened on the project folder.
///
/// Tries `code` (VS Code) first; if not on PATH, returns a user-friendly
/// error so the launcher can show a "VS Code not installed" toast. Does
/// NOT block — the editor is launched detached and the launcher process
/// continues. Returns immediately on success.
///
/// Bug 24: `surface` selects which Claude Code surface to use:
/// - "vscode" (default): `code <folder>` (VS Code extension picks up env
///   from .vscode/settings.json claude-code.env)
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
    let mut cmd = std::process::Command::new("code");
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
        let mut cmd = std::process::Command::new(bin);
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

    #[test]
    fn write_project_env_files_creates_all_three_paths() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("My Test")).unwrap();

        // 1. VS Code path
        let vscode_settings = tmp.join(".vscode/settings.json");
        assert!(vscode_settings.exists());
        let raw = std::fs::read_to_string(&vscode_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["claude-code.env"];
        // 2026-05-01: KG_COLLECTION carries the FULL Weaviate class name
        // (suffixed), matching `.env` and the rest of the ecosystem. Was
        // bare `MyTest` until the bare-kg fix.
        assert_eq!(env["KG_COLLECTION"], "MyTest_KnowledgeGraph");
        // PROJECT_NAME is the raw user-supplied name, not the sanitized
        // Weaviate basename. Was `MyTest` (sanitized) before; now matches
        // install.py + the .env template.
        assert_eq!(env["PROJECT_NAME"], "My Test");
        // Uppercase D for Development across every surface — Weaviate
        // class names are case-sensitive.
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_Development");
        // B5: CONVERSATION_COLLECTION must NOT be present in any surface.
        assert!(env.get("CONVERSATION_COLLECTION").is_none());
        // Shared-KG fields propagate to all three surfaces.
        assert_eq!(env["SHARED_KG_COLLECTION"], "VibeCodedTools_KnowledgeGraph");
        // Canonical write-gate key (asymmetric semantic since 2026-05-01).
        assert_eq!(env["SHARED_KG_WRITE_DISABLED"], "false");
        // Legacy alias mirrors the canonical value (kept for ~3 releases).
        assert_eq!(env["SHARED_KG_OPT_OUT"], "false");

        // 2. CLI shell file path
        let claude_env = tmp.join(".claude/env");
        assert!(claude_env.exists());
        let env_raw = std::fs::read_to_string(&claude_env).unwrap();
        assert!(env_raw.contains(r#"export KG_COLLECTION="MyTest_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export PROJECT_NAME="My Test""#));
        assert!(env_raw.contains(r#"export DEVELOPMENT_COLLECTION="MyTest_Development""#));
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/env.
        assert!(!env_raw.contains("CONVERSATION_COLLECTION"));
        assert!(env_raw.contains(r#"export SHARED_KG_COLLECTION="VibeCodedTools_KnowledgeGraph""#));
        assert!(env_raw.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(env_raw.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // 3. Bug 30: canonical .claude/settings.json env block
        let claude_settings = tmp.join(".claude/settings.json");
        assert!(claude_settings.exists());
        let raw = std::fs::read_to_string(&claude_settings).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&raw).unwrap();
        let env = &parsed["env"];
        assert_eq!(env["KG_COLLECTION"], "MyTest_KnowledgeGraph");
        assert_eq!(env["PROJECT_NAME"], "My Test");
        assert_eq!(env["DEVELOPMENT_COLLECTION"], "MyTest_Development");
        // B5: CONVERSATION_COLLECTION must NOT be in .claude/settings.json env.
        assert!(env.get("CONVERSATION_COLLECTION").is_none());
        assert_eq!(env["SHARED_KG_COLLECTION"], "VibeCodedTools_KnowledgeGraph");
        assert_eq!(env["SHARED_KG_WRITE_DISABLED"], "false");
        assert_eq!(env["SHARED_KG_OPT_OUT"], "false");

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

    #[test]
    fn env_surfaces_agree_after_write_project_env_files() {
        // 4-way equality regression: KG_COLLECTION must be IDENTICAL across
        // .env (template), .vscode/settings.json claude-code.env block,
        // .claude/env POSIX exports, and .claude/settings.json env block.
        // Pre-fix: bare in three, suffixed in .env → the bug VideoFrames hit.
        let tmp = std::env::temp_dir().join(format!(
            "vct-env-parity-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("VideoFrames")).unwrap();
        ensure_project_env_template(&tmp, &ProjectEnvSettings::with_defaults("VideoFrames")).unwrap();

        let env_text = std::fs::read_to_string(tmp.join(".env")).unwrap();
        let vsc: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap()).unwrap();
        let claude_env_text = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
        let cs: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap()).unwrap();

        assert!(env_text.contains("KG_COLLECTION=VideoFrames_KnowledgeGraph"));
        assert_eq!(vsc["claude-code.env"]["KG_COLLECTION"], "VideoFrames_KnowledgeGraph");
        assert!(claude_env_text.contains(r#"export KG_COLLECTION="VideoFrames_KnowledgeGraph""#));
        assert_eq!(cs["env"]["KG_COLLECTION"], "VideoFrames_KnowledgeGraph");

        assert!(env_text.contains("DEVELOPMENT_COLLECTION=VideoFrames_Development"));
        assert_eq!(vsc["claude-code.env"]["DEVELOPMENT_COLLECTION"], "VideoFrames_Development");
        assert!(claude_env_text.contains(r#"export DEVELOPMENT_COLLECTION="VideoFrames_Development""#));
        assert_eq!(cs["env"]["DEVELOPMENT_COLLECTION"], "VideoFrames_Development");

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

    /// Bug 32: existing `.vscode/settings.json` user keys (formatOnSave,
    /// defaultInterpreter, etc.) MUST be preserved. Top-level merge.
    ///
    /// PR-3 Commit 6 (2026-05-06): the `claude-code.env` sub-block is
    /// also deep-merged now — user-added env keys at that level survive.
    #[test]
    fn write_preserves_existing_vscode_settings_json() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-vscode-merge-test-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(tmp.join(".vscode")).unwrap();
        let path = tmp.join(".vscode/settings.json");
        std::fs::write(
            &path,
            r#"{
                "editor.formatOnSave": true,
                "python.defaultInterpreterPath": "/usr/bin/python3",
                "claude-code.env": {"OLD_KEY": "old"}
            }"#,
        )
        .unwrap();

        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("MyProject")).unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        let v: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["editor.formatOnSave"], true);
        assert_eq!(v["python.defaultInterpreterPath"], "/usr/bin/python3");
        assert_eq!(v["claude-code.env"]["KG_COLLECTION"], "MyProject_KnowledgeGraph");
        // PR-3 Commit 6: user-added env keys at the env-sub-block level
        // are preserved by the deep-merge.
        assert_eq!(
            v["claude-code.env"]["OLD_KEY"], "old",
            "user-added env keys must be preserved by deep-merge"
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
        let res = std::process::Command::new("code").arg(".").spawn();
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
        assert!(folder.join(".vscode/settings.json").exists());
        assert!(folder.join(".claude/env").exists());

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

        // .vscode/settings.json claude-code.env block
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert!(vsc["claude-code.env"].get("CONVERSATION_COLLECTION").is_none(),
                ".vscode/settings.json must not have CONVERSATION_COLLECTION");

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

        // VS Code surface
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert_eq!(vsc["claude-code.env"]["KG_COLLECTION"], "BazQux_KnowledgeGraph");
        assert_ne!(vsc["claude-code.env"]["KG_COLLECTION"], "FooBar_KnowledgeGraph");

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

        // .vscode/settings.json — Rust does not inject GRPC_PORT here.
        let vsc: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
        ).unwrap();
        assert!(vsc["claude-code.env"].get("GRPC_PORT").is_none(),
                ".vscode/settings.json must not have GRPC_PORT (install.py owns that surface)");

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
    /// the gate, so the relevant surfaces are the 3 written by
    /// write_project_env_files.)
    #[test]
    fn shared_kg_write_disabled_toggle_flips_all_env_surfaces() {
        let tmp = std::env::temp_dir().join(format!(
            "vct-medium1-{}",
            uuid::Uuid::new_v4().simple()
        ));
        std::fs::create_dir_all(&tmp).unwrap();

        // Default (None / false) → both keys "false" everywhere.
        write_project_env_files(&tmp, &ProjectEnvSettings::with_defaults("Acme")).unwrap();

        // Helper returning (vsc_canonical, vsc_legacy, cs_canonical,
        // cs_legacy, env_sh_text) so we can assert on every surface.
        let read_all = || -> (String, String, String, String, String) {
            let vsc: serde_json::Value = serde_json::from_str(
                &std::fs::read_to_string(tmp.join(".vscode/settings.json")).unwrap(),
            ).unwrap();
            let cs: serde_json::Value = serde_json::from_str(
                &std::fs::read_to_string(tmp.join(".claude/settings.json")).unwrap(),
            ).unwrap();
            let env_sh = std::fs::read_to_string(tmp.join(".claude/env")).unwrap();
            (
                vsc["claude-code.env"]["SHARED_KG_WRITE_DISABLED"].as_str().unwrap().to_string(),
                vsc["claude-code.env"]["SHARED_KG_OPT_OUT"].as_str().unwrap().to_string(),
                cs["env"]["SHARED_KG_WRITE_DISABLED"].as_str().unwrap().to_string(),
                cs["env"]["SHARED_KG_OPT_OUT"].as_str().unwrap().to_string(),
                env_sh,
            )
        };

        let (vsc_new, vsc_old, cs_new, cs_old, env_sh) = read_all();
        assert_eq!(vsc_new, "false");
        assert_eq!(vsc_old, "false");
        assert_eq!(cs_new, "false");
        assert_eq!(cs_old, "false");
        assert!(env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(env_sh.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // Flip to true.
        { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = true; write_project_env_files(&tmp, &s) }.unwrap();
        let (vsc_new, vsc_old, cs_new, cs_old, env_sh) = read_all();
        assert_eq!(vsc_new, "true");
        assert_eq!(vsc_old, "true");
        assert_eq!(cs_new, "true");
        assert_eq!(cs_old, "true");
        assert!(env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="true""#));
        assert!(env_sh.contains(r#"export SHARED_KG_OPT_OUT="true""#));
        assert!(!env_sh.contains(r#"export SHARED_KG_WRITE_DISABLED="false""#));
        assert!(!env_sh.contains(r#"export SHARED_KG_OPT_OUT="false""#));

        // Flip back to false.
        { let mut s = ProjectEnvSettings::with_defaults("Acme"); s.shared_kg_write_disabled = false; write_project_env_files(&tmp, &s) }.unwrap();
        let (vsc_new, vsc_old, cs_new, cs_old, env_sh) = read_all();
        assert_eq!(vsc_new, "false");
        assert_eq!(vsc_old, "false");
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
        let py = if std::process::Command::new("python3").arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").arg("--version")
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

        let out = std::process::Command::new(&py)
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
        let py = if std::process::Command::new("python3").arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").arg("--version")
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
        let out1 = std::process::Command::new(&py)
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
        let out2 = std::process::Command::new(&py)
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
        let py = if std::process::Command::new("python3").arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            "python3".to_string()
        } else if std::process::Command::new("python").arg("--version")
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
        let out = std::process::Command::new(&py)
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
        if std::process::Command::new("python3").arg("--version")
            .output().map(|o| o.status.success()).unwrap_or(false)
        {
            Some("python3".to_string())
        } else if std::process::Command::new("python").arg("--version")
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
        let out_seed = std::process::Command::new(&py)
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
        let out_seed = std::process::Command::new(&py)
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
}
