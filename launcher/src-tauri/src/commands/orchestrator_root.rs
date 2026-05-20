//! Auto-registration of the orchestrator clone itself as a first-class
//! `projects` row, plus a Tauri command surfacing its current state to
//! the UI.
//!
//! ## Why this exists
//!
//! Before migration 013 (v0.2.11, 2026-05-15) the orchestrator clone was
//! modelled OUTSIDE the `projects` table. The launcher resolved it via
//! `find_orchestrator_manifest()` walks + a path string in
//! `launcher.toml`. That arrangement made the clone invisible to every
//! FK-strict subsystem: `codegraph_access`, `kg_collection_access`,
//! `project_permissions`, `project_kg_bindings`,
//! `project_codegraph_bindings`, `code_graph_builds`,
//! `project_mcp_servers`, `kg_syncs`, `kg_summaries` all carry FKs to
//! `projects(id) ON DELETE CASCADE`. With no row to point at, the clone
//! could not natively participate in access grants, per-project MCP
//! toggles, KG bindings, etc. — every feature touching those tables
//! needed a side-channel.
//!
//! Migration 013 extends the `projects.host` CHECK from
//! `IN ('base','mao')` to `IN ('base','mao','orchestrator_root')`. This
//! module then auto-registers exactly ONE row at launcher startup whose
//! host is the new variant, with a fixed reserved slug
//! (`"orchestrator-root"`) and a real UUID. The row's
//! `folder_path` is the canonicalized clone directory (the parent of
//! `vct-module.json`).
//!
//! ## Idempotence
//!
//! `ensure_orchestrator_root` is safe to call any number of times. It
//! looks up the existing row by host first; if one is present, it
//! returns `Ok(())` without touching the DB. If no row exists AND no
//! clone is detectable from disk (`find_orchestrator_manifest()` returns
//! `None`), it ALSO returns `Ok(())` — there is nothing to register.
//! Manual registration via a future Settings → "Re-detect orchestrator
//! root" action is the recovery path for that case.
//!
//! ## Concurrency
//!
//! Two parallel calls racing the insert are prevented by the UNIQUE
//! constraints (`projects.slug` is unique, and the slug is the fixed
//! string `"orchestrator-root"`). Whichever transaction commits first
//! wins; the loser sees a UNIQUE violation and falls through to the
//! "row already exists" branch on its retry. We don't expect this race
//! in practice (the function only runs once at process start, before
//! Tauri commands are accepted) but the guarantee is structural.
//!
//! ## Cross-OS path handling
//!
//! On Windows, `std::fs::canonicalize` returns a `\\?\` verbatim path
//! prefix. That prefix is correct but not what the rest of the
//! launcher's tooling expects to compare against (most consumers use
//! `Path::canonicalize` results from elsewhere or untreated absolute
//! paths). We strip the prefix when present so the stored path is the
//! normal `C:\...` form. On Linux/macOS the canonicalized path is
//! returned as-is.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{command, State};
use uuid::Uuid;

use crate::commands::modules::{find_orchestrator_manifest, read_orchestrator_manifest};
use crate::commands::projects_v2::sanitize_kg_collection;
use crate::db::models::ProjectHost;
use crate::db::Db;

/// Reserved slug for the orchestrator-root project row. Fixed (never
/// generated from a project name) so the DB-level UNIQUE index on
/// `projects.slug` enforces "at most one orchestrator root per
/// launcher.db" without needing a separate application check.
pub const ORCHESTRATOR_ROOT_SLUG: &str = "orchestrator-root";

/// Display name for the auto-registered row. Matches the
/// `vct-module.json` manifest's identity for the orchestrator core (the
/// builtin catalog uses the same string in `commands::modules`).
pub const ORCHESTRATOR_ROOT_NAME: &str = "VibeCoded Orchestrator";

// v0.2.21 (Step 3d): the `impl Db { has_orchestrator_root_project }`
// method block moved to `vct_launcher_core::db::orchestrator_root_helpers`
// because Rust's orphan rule prohibits the launcher crate from adding
// inherent methods to a type defined in the core crate. The method is
// still reachable from this file as `db.has_orchestrator_root_project()`
// — it's an inherent method on Db now imported via the core re-export
// at the top of lib.rs.

/// Strip Windows' `\\?\` verbatim prefix (and the `\\?\UNC\` UNC
/// variant) from a path-string, leaving the conventional `C:\...` /
/// `\\server\share\...` form. No-op on non-Windows / no-prefix input.
///
/// `std::fs::canonicalize` on Windows always returns a `\\?\`-prefixed
/// path. That form is technically equivalent and works for any Win32
/// API call, but it confuses display, comparison against
/// non-canonicalized paths stored elsewhere, and shell-friendliness.
/// The launcher's other path-storing code uses unprefixed absolute
/// paths, so we normalize here for consistency.
fn strip_windows_verbatim_prefix(s: &str) -> String {
    // Match the two Windows verbatim forms used by canonicalize:
    //   \\?\C:\foo\bar         (drive form)
    //   \\?\UNC\server\share   (UNC form, must rebuild as \\server\share)
    if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
        // UNC: replace `\\?\UNC\` with `\\`.
        format!(r"\\{}", rest)
    } else if let Some(rest) = s.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        s.to_string()
    }
}

/// Canonicalize a folder path to its absolute form and return as a
/// String. On Windows, strips the `\\?\` verbatim prefix returned by
/// `std::fs::canonicalize`. On Linux/macOS just returns the absolute
/// path. If the path doesn't exist on disk, falls back to the raw input
/// (turned absolute via `Path::canonicalize` on its parent isn't worth
/// the complexity here — the clone directory always exists when this
/// function is called).
fn canonicalize_folder_path(folder: &Path) -> Result<String, String> {
    let canon = std::fs::canonicalize(folder)
        .map_err(|e| format!("canonicalize {}: {}", folder.display(), e))?;
    let raw = canon.to_string_lossy().to_string();
    Ok(strip_windows_verbatim_prefix(&raw))
}

/// Ensure there is exactly one row in `projects` with
/// `host='orchestrator_root'`. Runs once at launcher startup, after
/// `migrations::apply()` and `Db::ensure_change_log()`.
///
/// Returns Ok(()) in three cases:
///   1. A row with `host='orchestrator_root'` already exists. No-op.
///   2. No row exists AND no clone manifest is findable on disk (the
///      launcher is running as a standalone binary outside any clone).
///      No-op — there is nothing to register. The user can manually
///      register via a future "Re-detect orchestrator root" Settings
///      action once that lands.
///   3. No row exists AND a clone IS findable — INSERT a row with
///      a fresh UUID, the reserved slug, the manifest version (purely
///      informational; the row itself doesn't carry version data —
///      that's read back from the manifest each time), the
///      canonicalized clone folder, and `host='orchestrator_root'`.
///
/// Returns Err only on actual DB failures or unrecoverable disk-walk
/// errors. Soft-fails on manifest-read errors (no row inserted, but
/// startup continues).
pub fn ensure_orchestrator_root(db: &Db) -> Result<(), String> {
    if db.has_orchestrator_root_project()? {
        // PR-9 (v0.2.11): row exists from a prior boot but the primary
        // KG binding may not. Pre-0.2.11 orchestrator clones never had
        // the binding because PR-9 introduced it. Seed it idempotently
        // (the binding upsert is a no-op when present).
        if let Ok(Some(row)) = db.get_project_by_slug(ORCHESTRATOR_ROOT_SLUG) {
            ensure_orchestrator_root_kg_binding(db, &row.id);
            // v0.2.22 item #17: re-populate the per-project agents/skills/
            // hooks/MCP tables from the orchestrator clone's `.claude/`.
            // Pre-v0.2.22 the orchestrator-root row was created but
            // `populate_project_state_from_filesystem` was never invoked,
            // leaving the launcher GUI's per-project tabs empty even
            // though the clone has 65 agents / 52 skills / 28 hooks on
            // disk. The startup MCP backfill at lib.rs guarded on
            // `count_project_mcp_servers == 0` and so skipped re-populate
            // once any MCP rows existed, never recovering the agents/
            // skills/hooks state. Populate here is idempotent (UPSERT
            // preserves enabled toggles) and runs every startup so the
            // orchestrator-root tabs stay in sync with the clone.
            ensure_orchestrator_root_state_populated(db, &row.id, &row.folder_path, &row.name);
        }
        return Ok(());
    }

    // No row yet. Try to locate the clone on disk.
    let manifest_path: PathBuf = match find_orchestrator_manifest() {
        Some(p) => p,
        None => {
            // Standalone-binary scenario. Nothing to register. Not an
            // error — the launcher boots fine without an orchestrator
            // clone (it just can't show the root card in the UI).
            return Ok(());
        }
    };

    // The manifest path is `<clone>/vct-module.json`; we want the
    // parent. `find_orchestrator_manifest` only returns paths whose
    // file existed at the time, so `.parent()` is always `Some`.
    let folder: PathBuf = match manifest_path.parent() {
        Some(p) => p.to_path_buf(),
        None => {
            // Defensive: should never happen given the manifest walker's
            // contract, but treat as "no clone found" rather than panic.
            return Ok(());
        }
    };

    // Best-effort manifest read for diagnostics. Failure is non-fatal;
    // the row insert can still proceed with a hardcoded name + version.
    let _manifest = read_orchestrator_manifest();

    let folder_path = canonicalize_folder_path(&folder)?;

    // Defensive: another launcher process (e.g. a sibling launcher
    // sharing the same launcher.db via VCT_STATE_DIR) might have
    // inserted the row between our existence check and this insert.
    // The UNIQUE slug constraint will return an error in that case.
    // We treat that error as success (the row exists, our job is done).
    let id = Uuid::new_v4().to_string();
    let insert_result = db.insert_project(
        &id,
        ORCHESTRATOR_ROOT_NAME,
        &folder_path,
        ProjectHost::OrchestratorRoot,
        ORCHESTRATOR_ROOT_SLUG,
    );

    match insert_result {
        Ok(_) => {
            eprintln!(
                "[vct] auto-registered orchestrator root: id={}, folder={}",
                id, folder_path
            );
            let _ = db.audit(
                "orchestrator_root_register",
                Some(&id),
                None,
                &serde_json::json!({
                    "folder_path": folder_path,
                    "slug": ORCHESTRATOR_ROOT_SLUG,
                    "auto": true,
                }),
            );
            let _ = db.log_change("projects", "insert", Some(&id), Some(&id));
            // PR-9 (v0.2.11): seed the Orchestrator Project's primary
            // KG binding so every other project on this machine derives
            // the shared KG name from this binding (opzione A — see
            // .claude/context/plans/0.2.11-release-2026-05-16.md §PR-9).
            // sanitize_kg_collection("VibeCoded Orchestrator") returns
            // "VibeCodedOrchestrator"; the canonical KG collection name
            // for the orchestrator clone is therefore
            // "VibeCodedOrchestrator_KnowledgeGraph". User can override
            // by writing to `app_state[shared_kg.collection_name]` (the
            // existing Priority-1 path in project_env_settings.rs).
            ensure_orchestrator_root_kg_binding(db, &id);
            // v0.2.22 item #17: populate agents/skills/hooks tables
            // from the freshly-detected clone's `.claude/`. See the
            // already-exists branch above for the full rationale —
            // this is the first-time-register path's mirror.
            ensure_orchestrator_root_state_populated(
                db,
                &id,
                &folder_path,
                ORCHESTRATOR_ROOT_NAME,
            );
            Ok(())
        }
        Err(e) => {
            // Race lost (UNIQUE slug or UNIQUE folder_path) — verify
            // the row exists and treat as success. Surface any other
            // error. Even on the race path we still attempt to seed the
            // KG binding: if the race-winner already wrote it the
            // upsert is a no-op, and if it crashed before reaching that
            // step we recover.
            if db.has_orchestrator_root_project().unwrap_or(false) {
                eprintln!(
                    "[vct] orchestrator_root row already exists (raced insert: {})",
                    e
                );
                if let Ok(Some(row)) = db.get_project_by_slug(ORCHESTRATOR_ROOT_SLUG) {
                    ensure_orchestrator_root_kg_binding(db, &row.id);
                    // Race path also wants populate so the agents/skills/
                    // hooks tables don't stay empty on the loser side.
                    ensure_orchestrator_root_state_populated(
                        db,
                        &row.id,
                        &row.folder_path,
                        &row.name,
                    );
                }
                Ok(())
            } else {
                Err(format!("auto-register orchestrator_root: {}", e))
            }
        }
    }
}

/// v0.2.22 item #17: idempotent populate of the orchestrator-root
/// project's agents/skills/hooks/MCP rows from disk.
///
/// Soft-fails for two reasons:
///   1. A scan hiccup MUST NOT block launcher boot — the row already
///      exists, the KG binding is seeded, the rest of the launcher
///      runs fine even if the GUI's Agents tab stays empty.
///   2. `populate_project_state_from_filesystem` returns a report with
///      `warnings: Vec<String>` but no fatal error path; we just log
///      to stderr.
///
/// Pre-v0.2.22 this was missing. `ensure_orchestrator_root` registered
/// the row + KG binding but never invoked the FS scanner, leaving
/// `project_agents` / `project_skills` / `project_hooks` empty for the
/// VibeCoded Orchestrator project. The startup MCP-backfill at
/// `lib.rs::setup` ran the scanner ONLY when `count_project_mcp_servers
/// == 0`, so once any MCP rows landed (the migration-010 sweep adds
/// a couple by default), the gate closed and the agents/skills/hooks
/// tables stayed empty forever — visible in the launcher GUI as
/// "No agents registered." despite 65 .md files on disk.
fn ensure_orchestrator_root_state_populated(
    db: &Db,
    project_id: &str,
    folder_path: &str,
    project_name: &str,
) {
    let folder = std::path::Path::new(folder_path);
    if !folder.is_dir() {
        eprintln!(
            "[vct] WARN: orchestrator-root folder missing on disk: {} (skipping populate)",
            folder_path
        );
        return;
    }
    let report = crate::commands::project_state_populate::
        populate_project_state_from_filesystem(project_id, project_name, folder, db);
    let total = report.agents_inserted
        + report.skills_inserted
        + report.hooks_inserted
        + report.mcp_servers_inserted;
    if total > 0 {
        eprintln!(
            "[vct] orchestrator-root populate: {} agents, {} skills, {} hooks, {} MCP servers",
            report.agents_inserted,
            report.skills_inserted,
            report.hooks_inserted,
            report.mcp_servers_inserted,
        );
    }
    for w in &report.warnings {
        eprintln!("[vct] orchestrator-root populate warning: {}", w);
    }
}

/// PR-9 (v0.2.11): idempotent seed of the Orchestrator Project's
/// primary KG binding.
///
/// The shared KG collection name is derived from the Orchestrator
/// Project's display name via the canonical `sanitize_kg_collection`
/// helper (the same one PR-7 + PR-8 use everywhere else for collection
/// naming). Suffix `_KnowledgeGraph` matches the convention from
/// `project_state_populate::populate_kg_collection_access` and the
/// per-project bundle install.
///
/// Soft-fail: any error here logs to stderr but does NOT propagate.
/// The Orchestrator Project row insert succeeded; missing KG binding
/// only means the shared KG falls back to `DEFAULT_SHARED_KG_COLLECTION`
/// const for now. The function is called again on next launcher boot
/// (idempotent via `ON CONFLICT(project_id, role)` upsert in
/// `set_project_kg_binding`).
fn ensure_orchestrator_root_kg_binding(db: &Db, root_id: &str) {
    let collection_name = format!(
        "{}_KnowledgeGraph",
        sanitize_kg_collection(ORCHESTRATOR_ROOT_NAME)
    );
    match db.set_project_kg_binding(
        root_id,
        "primary",
        &collection_name,
        // embedding_model / dim / kg_dir / weaviate_url / config left
        // None — defaults inherit from launcher.toml env block. The
        // binding's job is to declare ownership of the collection
        // name; the embedding/host knobs live in the global config.
        None,
        None,
        None,
        None,
        &serde_json::json!({"auto_seeded_by": "ensure_orchestrator_root_kg_binding"}),
    ) {
        Ok(_) => {
            eprintln!(
                "[vct] seeded orchestrator-root primary KG binding: {}",
                collection_name
            );
        }
        Err(e) => {
            // Don't propagate — the binding seed is a Priority-2
            // optimization; without it the shared KG resolution
            // falls back to DEFAULT_SHARED_KG_COLLECTION.
            eprintln!(
                "[vct] WARN: ensure_orchestrator_root_kg_binding failed (non-fatal): {}",
                e
            );
        }
    }
}

// ─── Tauri command surface ───────────────────────────────────────────

/// Read-model returned to the UI. Carries enough state for a Settings
/// card to render "is the orchestrator root detected? registered?".
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrchestratorRootView {
    /// Project UUID if the row is present in `projects`, else `None`.
    pub id: Option<String>,
    /// Display name. From the auto-registered row when present, else
    /// the static fallback `ORCHESTRATOR_ROOT_NAME`.
    pub name: String,
    /// Version string from `vct-module.json` when readable, else
    /// `"unknown"` (no row, no manifest).
    pub version: String,
    /// Canonical absolute path to the clone directory. Empty string
    /// when neither the row nor the manifest walker locates one.
    pub folder_path: String,
    /// True iff a row with `host='orchestrator_root'` is present in
    /// `projects`. False on a standalone-binary install or before
    /// migration 013 has run (impossible in practice — migrations run
    /// before any command can fire).
    pub is_registered: bool,
    /// True iff `find_orchestrator_manifest()` currently returns a
    /// path. Independent of `is_registered`: a user might have an
    /// orphan row (registered but the folder moved/deleted) or a
    /// fresh clone (present on disk but not yet registered if
    /// auto-register failed for some reason).
    pub is_present: bool,
}

/// Tauri command — return the current orchestrator-root view.
///
/// Always returns `Some(view)` once migration 013 has run (the row is
/// either registered or not, plus an `is_present` indicator). The
/// `Option` wrapper is reserved for a future case where the view
/// itself can't be computed (currently never).
#[command]
pub async fn get_orchestrator_root_view(
    db: State<'_, Db>,
) -> Result<Option<OrchestratorRootView>, String> {
    // 1. Disk-walk result (independent of DB state).
    let manifest_path = find_orchestrator_manifest();
    let is_present = manifest_path.is_some();
    let manifest = read_orchestrator_manifest();
    let manifest_version = manifest
        .as_ref()
        .map(|m| m.version.clone())
        .unwrap_or_else(|| "unknown".to_string());

    // 2. DB-side lookup. The row is uniquely identified by its
    //    reserved slug, so we look it up by slug rather than by host
    //    (cheaper: hits the unique index).
    let row_opt = db
        .get_project_by_slug(ORCHESTRATOR_ROOT_SLUG)
        .map_err(|e| format!("get_project_by_slug: {}", e))?;

    let view = if let Some(row) = row_opt {
        OrchestratorRootView {
            id: Some(row.id),
            name: row.name,
            version: manifest_version,
            folder_path: row.folder_path,
            is_registered: true,
            is_present,
        }
    } else {
        // No row yet. Synthesize a view from manifest + walker output.
        let folder_path = manifest_path
            .as_ref()
            .and_then(|p| p.parent())
            .and_then(|p| canonicalize_folder_path(p).ok())
            .unwrap_or_default();
        OrchestratorRootView {
            id: None,
            name: ORCHESTRATOR_ROOT_NAME.to_string(),
            version: manifest_version,
            folder_path,
            is_registered: false,
            is_present,
        }
    };

    Ok(Some(view))
}

// ─── Tests ───────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_windows_verbatim_drive_form() {
        assert_eq!(
            strip_windows_verbatim_prefix(r"\\?\C:\Users\martino\repo"),
            r"C:\Users\martino\repo"
        );
    }

    #[test]
    fn strip_windows_verbatim_unc_form() {
        // `\\?\UNC\server\share\foo` → `\\server\share\foo`
        assert_eq!(
            strip_windows_verbatim_prefix(r"\\?\UNC\server\share\foo"),
            r"\\server\share\foo"
        );
    }

    #[test]
    fn strip_windows_verbatim_noop_on_posix_paths() {
        assert_eq!(
            strip_windows_verbatim_prefix("/home/martino/repo"),
            "/home/martino/repo"
        );
    }

    #[test]
    fn strip_windows_verbatim_noop_on_plain_windows_paths() {
        assert_eq!(
            strip_windows_verbatim_prefix(r"C:\Users\martino\repo"),
            r"C:\Users\martino\repo"
        );
    }

    /// Idempotence: calling ensure twice in a row is fine and leaves
    /// the DB in the same state. We can't easily test the "clone
    /// findable" path in unit tests (it would need a fixture filesystem
    /// containing `vct-module.json`), so we cover the "no clone
    /// findable, no-op" + "row already exists, no-op" cases.
    #[test]
    fn ensure_is_idempotent_when_no_clone_findable() {
        // In-memory DBs are created outside any clone (current_exe
        // points at the cargo test binary, not a launcher dist). The
        // walker won't find a manifest above the test runner, so
        // ensure_orchestrator_root is a no-op.
        //
        // CAVEAT: when run from the launcher's own source tree, the
        // walker WILL find this clone's vct-module.json. The test then
        // exercises the "row inserted" branch instead — also valid;
        // we just verify idempotence either way.
        let db = Db::open_in_memory().expect("in-memory db");

        let pre = db.has_orchestrator_root_project().unwrap();
        ensure_orchestrator_root(&db).expect("first ensure");
        let mid = db.has_orchestrator_root_project().unwrap();
        ensure_orchestrator_root(&db).expect("second ensure");
        let post = db.has_orchestrator_root_project().unwrap();

        // Either both branches stayed false (no clone found) or both
        // stayed true after the first insert. Either way, mid == post
        // and the second call did not error.
        assert_eq!(
            mid, post,
            "ensure_orchestrator_root must be idempotent; first call made row={}, second call made row={}",
            mid, post
        );

        if !pre {
            // We started empty. After first ensure, either the clone
            // was found and a row exists (true), or it wasn't (false).
            // Either is acceptable. The point is consistency.
            assert!(
                mid == post,
                "post-state mismatch (mid={}, post={})",
                mid,
                post
            );
        }
    }

    /// has_orchestrator_root_project returns false for a fresh DB and
    /// true after inserting a row with the relevant host.
    #[test]
    fn has_orchestrator_root_project_detects_presence() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert!(!db.has_orchestrator_root_project().unwrap());

        let id = Uuid::new_v4().to_string();
        db.insert_project(
            &id,
            "Test",
            "/tmp/test-orchroot-detect",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .expect("insert orchestrator_root row");

        assert!(db.has_orchestrator_root_project().unwrap());
    }

    /// Calling ensure when a row already exists is a no-op (same row
    /// id; no insert audit entry created on the second call).
    #[test]
    fn ensure_short_circuits_when_row_exists() {
        let db = Db::open_in_memory().expect("in-memory db");

        // Pre-seed a row so the walker path doesn't get executed.
        let pre_id = Uuid::new_v4().to_string();
        db.insert_project(
            &pre_id,
            "Pre-seeded VCO",
            "/tmp/pre-seed-orchroot",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();

        ensure_orchestrator_root(&db).expect("ensure short-circuits");

        // Row id should be the one we seeded, not a fresh UUID.
        let row = db
            .get_project_by_slug(ORCHESTRATOR_ROOT_SLUG)
            .unwrap()
            .expect("row exists");
        assert_eq!(row.id, pre_id, "ensure must not replace an existing row");
        assert_eq!(row.name, "Pre-seeded VCO");
    }

    /// Slug UNIQUE constraint prevents two rows with the reserved
    /// orchestrator-root slug. This is the structural guard that backs
    /// the "max one orchestrator_root per launcher.db" invariant.
    #[test]
    fn cannot_insert_second_orchestrator_root_row() {
        let db = Db::open_in_memory().expect("in-memory db");

        db.insert_project(
            "first",
            "First",
            "/tmp/orchroot-first",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .expect("first insert");

        let second = db.insert_project(
            "second",
            "Second",
            "/tmp/orchroot-second",
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        );
        assert!(second.is_err(), "second insert must fail on UNIQUE slug");
    }

    // ─── v0.2.22 item #17: ensure_orchestrator_root_state_populated ──────

    /// When `ensure_orchestrator_root` runs and the row exists with a
    /// real folder_path on disk containing `.claude/agents/*.md`, the
    /// `project_agents` table must be populated. This is the core
    /// regression guard for v0.2.22 item #17 — pre-fix the row existed
    /// but `populate_project_state_from_filesystem` was never invoked,
    /// leaving the GUI Agents tab empty despite files on disk.
    #[test]
    fn ensure_orchestrator_root_populates_agents_from_disk() {
        use std::fs;

        let db = Db::open_in_memory().expect("in-memory db");

        // Stage a fake orchestrator-root folder with .claude/agents.
        let tmp = std::env::temp_dir().join(format!(
            "orchroot-populate-{}",
            Uuid::new_v4().simple()
        ));
        let agents_dir = tmp.join(".claude/agents");
        fs::create_dir_all(&agents_dir).unwrap();
        fs::write(
            agents_dir.join("planner.md"),
            "---\nname: planner\nmodel: sonnet\n---\n# Planner\n",
        )
        .unwrap();
        fs::write(
            agents_dir.join("coder.md"),
            "---\nname: coder\nmodel: haiku\n---\n# Coder\n",
        )
        .unwrap();
        // README.md must be filtered out by the v0.2.22 fix.
        fs::write(agents_dir.join("README.md"), "# docs").unwrap();

        // Pre-seed the orchestrator-root row pointing at our tmp folder.
        let root_id = Uuid::new_v4().to_string();
        db.insert_project(
            &root_id,
            ORCHESTRATOR_ROOT_NAME,
            tmp.to_string_lossy().as_ref(),
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();

        // No agents yet.
        assert_eq!(
            db.list_project_agents(&root_id).unwrap().len(),
            0,
            "pre-condition: no agents registered"
        );

        // Run ensure — it should populate the agents table.
        ensure_orchestrator_root(&db).expect("ensure_orchestrator_root");

        let rows = db.list_project_agents(&root_id).unwrap();
        let names: Vec<&str> = rows.iter().map(|a| a.agent_name.as_str()).collect();
        assert!(
            names.contains(&"planner"),
            "planner must be populated, got: {:?}",
            names
        );
        assert!(
            names.contains(&"coder"),
            "coder must be populated, got: {:?}",
            names
        );
        assert!(
            !names.iter().any(|n| n.eq_ignore_ascii_case("readme")),
            "README must be filtered out by the v0.2.22 fix, got: {:?}",
            names
        );

        fs::remove_dir_all(&tmp).ok();
    }

    /// Idempotency: running ensure twice with the same on-disk state
    /// does not duplicate rows AND preserves user-disabled toggles.
    /// This is the contract that lets us safely call ensure on EVERY
    /// launcher boot rather than gating on "is populate needed?".
    #[test]
    fn ensure_orchestrator_root_populate_is_idempotent_and_preserves_toggles() {
        use std::fs;

        let db = Db::open_in_memory().expect("in-memory db");

        let tmp = std::env::temp_dir().join(format!(
            "orchroot-idem-{}",
            Uuid::new_v4().simple()
        ));
        let agents_dir = tmp.join(".claude/agents");
        fs::create_dir_all(&agents_dir).unwrap();
        fs::write(
            agents_dir.join("planner.md"),
            "---\nname: planner\nmodel: sonnet\n---\n",
        )
        .unwrap();

        let root_id = Uuid::new_v4().to_string();
        db.insert_project(
            &root_id,
            ORCHESTRATOR_ROOT_NAME,
            tmp.to_string_lossy().as_ref(),
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();

        // First ensure populates.
        ensure_orchestrator_root(&db).expect("first ensure");
        assert_eq!(db.list_project_agents(&root_id).unwrap().len(), 1);

        // User disables the agent via the GUI.
        db.set_project_agent_enabled(&root_id, "planner", false)
            .unwrap();
        assert!(!db.list_project_agents(&root_id).unwrap()[0].enabled);

        // Second ensure — must NOT duplicate the row AND must NOT
        // re-enable it.
        ensure_orchestrator_root(&db).expect("second ensure");
        let rows = db.list_project_agents(&root_id).unwrap();
        assert_eq!(rows.len(), 1, "ensure must not duplicate rows");
        assert!(
            !rows[0].enabled,
            "ensure must preserve user-disabled toggle"
        );

        fs::remove_dir_all(&tmp).ok();
    }

    /// When the orchestrator-root folder_path stored in DB no longer
    /// exists on disk (user deleted the clone but the DB row survives),
    /// the populate helper must not panic; it should soft-fail with a
    /// stderr warning and leave the row alone.
    #[test]
    fn ensure_orchestrator_root_populate_soft_fails_when_folder_missing() {
        let db = Db::open_in_memory().expect("in-memory db");

        let root_id = Uuid::new_v4().to_string();
        // Path that intentionally does not exist on disk.
        let missing = std::env::temp_dir().join(format!(
            "orchroot-MISSING-{}",
            Uuid::new_v4().simple()
        ));
        db.insert_project(
            &root_id,
            ORCHESTRATOR_ROOT_NAME,
            missing.to_string_lossy().as_ref(),
            ProjectHost::OrchestratorRoot,
            ORCHESTRATOR_ROOT_SLUG,
        )
        .unwrap();

        // Should not panic or error.
        ensure_orchestrator_root(&db).expect("ensure must not error on missing folder");

        // Row stays.
        let row = db.get_project_by_slug(ORCHESTRATOR_ROOT_SLUG).unwrap();
        assert!(row.is_some(), "row must not be deleted");
        // No agents registered (because folder doesn't exist).
        assert_eq!(db.list_project_agents(&root_id).unwrap().len(), 0);
    }
}
