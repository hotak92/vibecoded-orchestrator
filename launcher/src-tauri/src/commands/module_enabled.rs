// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//! v0.2.49 Stream B — per-project enable/disable toggle for global-scope modules.
//!
//! A *global-scope* module (declared via `manifest.install.scope = "global"`
//! by Stream A) has at most one install row on the host but is shareable
//! across every registered project. Without this toggle, the user has no
//! way to silence such a module per-project — the cosine-based RL
//! reranker, for instance, would fire on every project's `hybrid_search`
//! call once a single license activates it.
//!
//! ## Design
//!
//! The toggle is a row in `module_settings` with key
//! [`vct_launcher_core::db::settings::MODULE_ENABLED_FOR_PROJECT_KEY`]
//! ("`enabled_for_project`") and JSON-boolean value. Default when no row
//! exists: enabled. Reader fails open on malformed values (see DB-layer
//! docstring).
//!
//! Three seeding paths keep the rows in sync with the rest of the system:
//!
//! 1. **Project creation** — `create_project_v2` calls
//!    [`seed_enabled_rows_for_new_project`] which enumerates every
//!    currently-installed global-scope module across the host and writes
//!    `enabled=true` for the new project.
//! 2. **Module install** — when a global-scope module finishes installing,
//!    `install_module_for_project` (in `commands::modules`) calls
//!    [`seed_enabled_rows_for_new_global_module`] which writes
//!    `enabled=true` for every existing project so the module is on by
//!    default everywhere.
//! 3. **Module uninstall** — when a global-scope module is uninstalled,
//!    `uninstall_module_v2` calls [`clear_enabled_rows_for_uninstalled_module`]
//!    which deletes every per-project enable row so a future reinstall
//!    starts clean.
//!
//! All three paths are soft-fail per-row so a single broken row doesn't
//! block the surrounding action (project creation, install, uninstall).
//!
//! ## What this does NOT gate
//!
//! Stream B is explicitly **not** allowed to silently drop training-event
//! logs from the RL Reranker. The gate prevents the RL CLIENT (the
//! `weaviate_mcp` server) from issuing rerank requests when the project's
//! flag is `false`; the SERVER's local-JSONL telemetry path is unaffected
//! and continues writing per-project event files. See the
//! `_rl_cache_and_rerank` call site for the consumer.

use tauri::{command, AppHandle, Emitter, State};

use crate::db::Db;
use vct_launcher_core::manifest::ModuleManifest;

/// Tauri event name used to notify the Svelte renderer that a module's
/// per-project enable flag flipped. The renderer redraws affected tabs
/// (per-project Modules panel, RL Reranker dashboard) on receipt.
pub const MODULE_ENABLED_EVENT: &str = "module:enabled-for-project-changed";

#[derive(Debug, Clone, serde::Serialize)]
pub struct ModuleEnabledChangedEvent {
    pub project_id: String,
    pub module_id: String,
    pub enabled: bool,
}

/// Set the per-project enable flag for a (global-scope) module.
///
/// Auth/permission model: the launcher's Tauri command surface is local-
/// only (no remote callers), so no per-call authentication is performed
/// — same as the sibling `set_project_module_enabled` command in
/// `diagrams_cmd`. The audit log records every flip for forensic trace.
///
/// Returns `Ok(())` on success. Errors propagate the DB-layer message
/// (parser failure on a hand-edited row, FK violation if `project_id`
/// doesn't exist, etc.) — the GUI surfaces them via toast.
#[command]
pub async fn module_set_enabled_for_project(
    project_id: String,
    module_id: String,
    enabled: bool,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    // Verify the project exists. Returning a clear error here is better
    // than the FK-violation cascade from set_setting → INSERT, which
    // surfaces an opaque "FOREIGN KEY constraint failed" message.
    if db.get_project(&project_id)?.is_none() {
        return Err(format!(
            "project {} not found; cannot toggle module enable flag",
            project_id
        ));
    }

    // Empty module_id is almost always a bug in the caller (e.g. a stale
    // store ref in Svelte). Reject up front rather than writing an
    // orphan row that the cleanup helpers would never find again.
    if module_id.trim().is_empty() {
        return Err("module_id must not be empty".to_string());
    }

    db.module_set_enabled_for_project(&project_id, &module_id, enabled)?;

    db.audit(
        "module_enabled_for_project_changed",
        Some(&project_id),
        Some(&module_id),
        &serde_json::json!({
            "enabled": enabled,
        }),
    )?;

    // Fire-and-forget renderer notification. Soft-fail: a missing window
    // handle (rare; happens during shutdown) must not roll back the DB
    // write. Mirrors the pattern in `diagrams_cmd::set_project_module_enabled`.
    let payload = ModuleEnabledChangedEvent {
        project_id: project_id.clone(),
        module_id: module_id.clone(),
        enabled,
    };
    if let Err(e) = app.emit(MODULE_ENABLED_EVENT, payload) {
        eprintln!(
            "[module_enabled] emit({}) failed for ({}, {}): {}",
            MODULE_ENABLED_EVENT, project_id, module_id, e
        );
    }

    Ok(())
}

/// Read the per-project enable flag for a module. Convenience wrapper so
/// the renderer can hydrate its toggle UI without a separate generic
/// settings call. Default `true` when no row exists (matches the DB-layer
/// reader contract).
#[command]
pub async fn module_is_enabled_for_project(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.module_is_enabled_for_project(&project_id, &module_id)
}

// ─── Seeding helpers (called from project_create / install / uninstall) ──

/// Resolve the manifest for an installed (project_id, module_id) pair and
/// return its scope. Returns `Ok(None)` when the manifest can't be loaded
/// — callers should treat that as "skip this module" rather than as an
/// error, so a single broken manifest doesn't poison the iteration.
///
/// Manifest resolution mirrors the cold-start chain in
/// `install_path_manifest_lookup` but is best-effort here: an extracted
/// on-disk manifest is preferred; absent it, we return `None`.
fn resolve_manifest_scope_global_best_effort(
    module_id: &str,
) -> Option<bool> {
    // Prefer the post-install extracted manifest at
    // `~/.vct/modules/<id>/vct-module.json`. This is the canonical path
    // populated by `module_manifest_extract` for container_pull modules
    // and by the install bundle for git/local installs.
    let extracted = crate::paths::vct_root_dir()
        .join("modules")
        .join(module_id)
        .join("vct-module.json");
    if !extracted.exists() {
        return None;
    }
    let raw = match std::fs::read_to_string(&extracted) {
        Ok(s) => s,
        Err(_) => return None,
    };
    let manifest: ModuleManifest = match serde_json::from_str(&raw) {
        Ok(m) => m,
        Err(_) => return None,
    };
    Some(manifest.install_scope_is_global())
}

/// On `create_project_v2`, enumerate every installed global-scope module
/// in the launcher DB and write `enabled=true` for the new project.
///
/// Soft-fail per module: a single bad manifest or DB write does not
/// abort the seeding loop. Caller (`create_project_v2`) collects no
/// errors from this function — warnings are eprintln'd for ops visibility.
///
/// Returns the number of (module, project) rows actually seeded. Callers
/// may include this in the audit payload for forensic trace.
pub fn seed_enabled_rows_for_new_project(db: &Db, project_id: &str) -> usize {
    // Walk every (project, module) install row and collect the distinct
    // module_ids that are currently installed somewhere on the host. We
    // can't use a SQL `DISTINCT module_id` shortcut without a new DB
    // helper, so we de-dupe in-memory — the install count is small
    // (typically <20), so this is fine.
    let all_projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            eprintln!(
                "[module_enabled] seed_enabled_rows_for_new_project({}): \
                 list_projects failed: {}",
                project_id, e
            );
            return 0;
        }
    };

    let mut seen_module_ids: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    let mut seeded = 0usize;

    for other_project in &all_projects {
        // Skip the project we're seeding for — it's brand new and has
        // no install rows yet.
        if other_project.id == project_id {
            continue;
        }
        let installs = match db.list_module_installs_for_project(&other_project.id) {
            Ok(rows) => rows,
            Err(e) => {
                eprintln!(
                    "[module_enabled] seed_enabled_rows_for_new_project({}): \
                     list_module_installs_for_project({}) failed: {}",
                    project_id, other_project.id, e
                );
                continue;
            }
        };
        for row in installs {
            if !seen_module_ids.insert(row.module_id.clone()) {
                continue;
            }
            // Only seed for *global*-scope modules. Per-project modules
            // already have their own enable column on `module_installs`
            // and don't need the toggle.
            match resolve_manifest_scope_global_best_effort(&row.module_id) {
                Some(true) => {
                    if let Err(e) = db.module_set_enabled_for_project(
                        project_id,
                        &row.module_id,
                        true,
                    ) {
                        eprintln!(
                            "[module_enabled] seed_enabled_rows_for_new_project({}, {}): \
                             write failed: {}",
                            project_id, row.module_id, e
                        );
                    } else {
                        seeded += 1;
                    }
                }
                Some(false) | None => {
                    // Per-project scope (or manifest absent): no row needed.
                }
            }
        }
    }

    seeded
}

/// On install completion of a global-scope module, enumerate every
/// existing project and seed `enabled=true` rows so the module is on by
/// default across the host. Soft-fail per project.
///
/// The caller (`install_module_for_project`) should call this only after
/// `set_module_status(Installed)` has landed for the (project, module)
/// pair, since this function inspects `manifest` directly rather than
/// re-reading from disk.
///
/// Returns the number of projects seeded.
pub fn seed_enabled_rows_for_new_global_module(
    db: &Db,
    manifest: &ModuleManifest,
    module_id: &str,
) -> usize {
    if !manifest.install_scope_is_global() {
        // Sanity: callers gate on this, but defend in depth.
        return 0;
    }

    let projects = match db.list_projects() {
        Ok(p) => p,
        Err(e) => {
            eprintln!(
                "[module_enabled] seed_enabled_rows_for_new_global_module({}): \
                 list_projects failed: {}",
                module_id, e
            );
            return 0;
        }
    };

    let mut seeded = 0usize;
    for project in &projects {
        if let Err(e) = db.module_set_enabled_for_project(&project.id, module_id, true) {
            eprintln!(
                "[module_enabled] seed_enabled_rows_for_new_global_module({}, {}): \
                 write failed: {}",
                project.id, module_id, e
            );
        } else {
            seeded += 1;
        }
    }
    seeded
}

/// On uninstall of a global-scope module, drop every per-project enable
/// row so a future reinstall starts clean (no stale "false" lingering
/// after the user re-activates a license). Idempotent: returns Ok even
/// when no rows existed.
///
/// Caller (`uninstall_module_v2`) calls this regardless of the module's
/// current scope — a project-scope module just has no rows to clear, so
/// the helper short-circuits to a 0-row delete. Cheap.
pub fn clear_enabled_rows_for_uninstalled_module(db: &Db, module_id: &str) -> usize {
    match db.module_clear_enabled_for_project_all(module_id) {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
                "[module_enabled] clear_enabled_rows_for_uninstalled_module({}): \
                 delete failed: {}",
                module_id, e
            );
            0
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use vct_launcher_core::db::models::ProjectHost;

    fn mkdb() -> Db {
        Db::open_in_memory().expect("in-memory db")
    }

    fn mkproject(db: &Db, id: &str, slug: &str) {
        db.insert_project(
            id,
            &format!("Project {}", slug),
            &format!("/tmp/{}", slug),
            ProjectHost::Base,
            slug,
        )
        .expect("insert project");
    }

    /// seed_enabled_rows_for_new_project returns 0 when no other
    /// projects exist — there's nothing to discover global-scope modules
    /// from yet. Important boundary case for the very first project the
    /// user creates.
    #[test]
    fn seed_enabled_rows_for_new_project_empty_host_is_zero() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");
        let n = seed_enabled_rows_for_new_project(&db, "p1");
        assert_eq!(n, 0);
    }

    /// seed_enabled_rows_for_new_project skips the project being seeded
    /// itself (which has no install rows by construction), and only
    /// considers OTHER projects' installs to discover candidate global-
    /// scope modules.
    #[test]
    fn seed_enabled_rows_for_new_project_skips_self() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");
        mkproject(&db, "p2", "p2");
        // p1 has a per-project install. No manifest on disk, so it
        // resolves to None → not global → 0 seeds. This proves the
        // skip-self path doesn't crash on a fresh project_id with no
        // installs.
        let _ = db.insert_module_install(
            "i-1", "p1", "vct-coordination", "0.1.0", "/tmp/x",
        );
        let n = seed_enabled_rows_for_new_project(&db, "p2");
        assert_eq!(n, 0, "no manifest on disk → resolver returns None → no seeds");
    }

    /// clear_enabled_rows_for_uninstalled_module is idempotent and
    /// returns 0 when no rows existed for the module_id.
    #[test]
    fn clear_enabled_rows_for_uninstalled_module_idempotent() {
        let db = mkdb();
        let n = clear_enabled_rows_for_uninstalled_module(&db, "vct-rl-reranker");
        assert_eq!(n, 0);
    }

    /// clear_enabled_rows_for_uninstalled_module deletes only the
    /// target module's rows across all projects. End-to-end coverage of
    /// the cross-project delete loop.
    #[test]
    fn clear_enabled_rows_for_uninstalled_module_scopes_to_module_id() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");
        mkproject(&db, "p2", "p2");
        db.module_set_enabled_for_project("p1", "vct-rl-reranker", true)
            .unwrap();
        db.module_set_enabled_for_project("p2", "vct-rl-reranker", false)
            .unwrap();
        db.module_set_enabled_for_project("p1", "vct-coordination", true)
            .unwrap();

        let n = clear_enabled_rows_for_uninstalled_module(&db, "vct-rl-reranker");
        assert_eq!(n, 2);

        // Coordination row untouched.
        assert!(db
            .module_is_enabled_for_project("p1", "vct-coordination")
            .unwrap());
        // RL rows now reading default (true) since they were deleted.
        assert!(db
            .module_is_enabled_for_project("p1", "vct-rl-reranker")
            .unwrap());
        assert!(db
            .module_is_enabled_for_project("p2", "vct-rl-reranker")
            .unwrap());
    }
}
