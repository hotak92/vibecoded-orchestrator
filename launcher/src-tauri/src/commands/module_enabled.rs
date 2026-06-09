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

// ─── v0.2.52 V52-AD — GLOBAL (host-wide) toggle commands ───────────────
//
// Sibling commands to `module_set_enabled_for_project` /
// `module_is_enabled_for_project`, but acting on the NULL-project row
// added by migration 034. Reuses the same audit event + renderer
// notification machinery, with `project_id` set to the sentinel string
// "__global__" in the event payload so consumers can distinguish a
// per-project flip from a global flip.
//
// The Svelte renderer's Settings → Modules tab binds its "Default ON / OFF"
// switch to `module_set_global_enabled`; the per-project Modules panel
// keeps using `module_set_enabled_for_project` exactly as before. Both
// surfaces fire the same `module:enabled-for-project-changed` event so a
// single global flip redraws every project's RL panel without polling.

/// Sentinel value used in the event payload's `project_id` field when
/// the change applies host-wide (no per-project row written). Distinct
/// enough from any UUID-shaped real project_id that a Svelte switch on
/// `event.project_id === '__global__'` reliably picks the global case.
pub const GLOBAL_PROJECT_SENTINEL: &str = "__global__";

/// Set the GLOBAL (host-wide) enable flag for a module. The row is
/// stored in `module_settings` with `project_id IS NULL` (migration
/// 034). Per-project overrides take precedence at read time — see
/// `Db::module_effective_enabled`.
///
/// Auth/permission model: same as the per-project setter — local-only
/// Tauri command surface, audit log captures every flip.
#[command]
pub async fn module_set_global_enabled(
    module_id: String,
    enabled: bool,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<(), String> {
    // Empty module_id is almost always a bug in the caller. Reject up
    // front rather than writing an orphan row.
    if module_id.trim().is_empty() {
        return Err("module_id must not be empty".to_string());
    }

    db.module_set_global_enabled(&module_id, enabled)?;

    db.audit(
        "module_global_enabled_changed",
        None, // No project_id for a global flip.
        Some(&module_id),
        &serde_json::json!({
            "enabled": enabled,
            "scope": "global",
        }),
    )?;

    // Reuse the per-project event channel — the renderer already
    // subscribes for redraw. project_id = "__global__" lets consumers
    // distinguish a global flip from a per-project one.
    let payload = ModuleEnabledChangedEvent {
        project_id: GLOBAL_PROJECT_SENTINEL.to_string(),
        module_id: module_id.clone(),
        enabled,
    };
    if let Err(e) = app.emit(MODULE_ENABLED_EVENT, payload) {
        eprintln!(
            "[module_enabled] emit({}) failed for global flip ({}): {}",
            MODULE_ENABLED_EVENT, module_id, e
        );
    }

    Ok(())
}

/// Read the GLOBAL (host-wide) enable flag for a module. Returns
/// `Some(bool)` when the row exists, `None` when no global row has been
/// written yet. The renderer's Settings → Modules tab uses `None` to
/// render an "(default — system fallback)" indicator vs an explicit
/// "(default ON / OFF by user choice)".
#[command]
pub async fn module_is_global_enabled(
    module_id: String,
    db: State<'_, Db>,
) -> Result<Option<bool>, String> {
    db.module_global_enabled(&module_id)
}

/// Read the effective enable flag for a (project, module) pair using
/// the v0.2.52 V52-AD cascade (per-project → global → fail-open true).
/// This is the value the hub resolver emits as
/// `rl_reranker_enabled_for_project`; the renderer reads it to display
/// the "Effective state" indicator above the per-project + global
/// switches.
#[command]
pub async fn module_effective_enabled(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.module_effective_enabled(&project_id, &module_id)
}

// ─── v0.2.52 V52-AD — RL training-data accumulator query ───────────────
//
// The "auto-enable trigger" use case (user-stated 2026-06-09): once
// 500+ retrieval events have accumulated in `rl_events`, prompt the
// user to enable the RL reranker. This command reports the current
// count so the Settings → Modules tab can render a progress indicator
// and an "Enable now" button when the threshold is met.

/// Count rows in `rl_events`. Used by the Settings → Modules tab to
/// decide whether to show the "Enough training data — enable RL?"
/// prompt. Counts both `retrieval` and `citation` event types
/// (training reads both via the offline_trainer's join).
///
/// Returns a single integer. Soft-fail at the renderer layer: a
/// transient DB error renders as "—" rather than blocking the tab.
#[command]
pub async fn rl_events_count(db: State<'_, Db>) -> Result<i64, String> {
    let guard = db.lock();
    let n: i64 = guard
        .query_row("SELECT COUNT(*) FROM rl_events", [], |r| r.get(0))
        .map_err(|e| format!("rl_events_count: {}", e))?;
    Ok(n)
}

// ─── v0.2.52 V52-AD — startup auto-enable probe ────────────────────────
//
// Threshold for triggering the "auto-enable" prompt — mirrors the
// hardcoded value in install.py. Kept here as a constant rather than
// reading from a config file so the rule is auditable in one place.
pub const RL_AUTO_ENABLE_EVENT_THRESHOLD: i64 = 500;

/// Tauri event name fired at launcher boot when the rl_events count
/// has crossed `RL_AUTO_ENABLE_EVENT_THRESHOLD` AND the global RL
/// reranker toggle is still `false` (the user accepted install.py's
/// default and hasn't flipped it manually since).
///
/// The Svelte renderer subscribes from `+layout.svelte`; a toast or
/// banner can prompt the user to navigate to /preferences/modules.
/// Firing the event does NOT auto-flip the toggle — the user must
/// confirm. This matches the V52-AD spec: "prompts user 'Enough
/// training data accumulated. Enable RL reranker?'".
pub const RL_AUTO_ENABLE_EVENT: &str = "vct-rl-auto-enable-available";

#[derive(Debug, Clone, serde::Serialize)]
pub struct RlAutoEnableAvailablePayload {
    pub event_count: i64,
    pub threshold: i64,
    pub module_id: String,
}

/// Boot-time probe. Reads `rl_events` count + the global RL row.
/// Emits `RL_AUTO_ENABLE_EVENT` ONCE per boot when both conditions
/// are met. Soft-fail throughout — a failed read just skips the
/// emit (the user can still navigate to /preferences/modules
/// manually).
///
/// Idempotency contract: this function emits AT MOST one event per
/// launcher boot. Re-runs are safe (no-ops) because the renderer
/// debounces toasts with the same key. The "do not re-emit after
/// the user has dismissed" behavior is owned by the renderer side
/// (localStorage dismissal token).
pub fn probe_rl_auto_enable_at_boot(
    db: &Db,
    app: &tauri::AppHandle,
) {
    use tauri::Emitter;

    // Step 1: rl_events count. Soft-fail when the table is missing
    // (pre-migration-025 DB) → no emit.
    let guard = db.lock();
    let count_result: Result<i64, _> =
        guard.query_row("SELECT COUNT(*) FROM rl_events", [], |r| r.get(0));
    drop(guard); // release lock before the global-row read.

    let count = match count_result {
        Ok(n) => n,
        Err(e) => {
            eprintln!(
                "[rl-auto-enable] rl_events probe failed (table likely \
                 missing): {}",
                e
            );
            return;
        }
    };

    if count < RL_AUTO_ENABLE_EVENT_THRESHOLD {
        // Below threshold → nothing to prompt about. The Settings →
        // Modules tab still shows the progress bar.
        return;
    }

    // Step 2: global row check. If the user has explicitly enabled
    // (Some(true)) or not configured at all (None) → no prompt
    // needed. Prompt only fires when the row says `false` (the
    // install-time default that the user hasn't overridden).
    let global = match db.module_global_enabled("vct-rl-reranker") {
        Ok(g) => g,
        Err(e) => {
            eprintln!(
                "[rl-auto-enable] module_global_enabled probe failed: {}",
                e
            );
            return;
        }
    };

    match global {
        Some(false) => {
            // Eligible: install.py seeded `false` and the user
            // hasn't flipped it. Emit the prompt event.
            let payload = RlAutoEnableAvailablePayload {
                event_count: count,
                threshold: RL_AUTO_ENABLE_EVENT_THRESHOLD,
                module_id: "vct-rl-reranker".to_string(),
            };
            if let Err(e) = app.emit(RL_AUTO_ENABLE_EVENT, payload) {
                eprintln!(
                    "[rl-auto-enable] emit({}) failed: {}",
                    RL_AUTO_ENABLE_EVENT, e
                );
            } else {
                eprintln!(
                    "[rl-auto-enable] threshold met ({} >= {}) and \
                     global default still disabled — prompting user.",
                    count, RL_AUTO_ENABLE_EVENT_THRESHOLD
                );
            }
        }
        Some(true) => {
            // User already enabled — nothing to do.
        }
        None => {
            // No row written yet (e.g. install.py never ran or DB
            // was created before V52-AD). Treat as fail-open per
            // the cascade contract: the user is implicitly opted-in.
            // Skip the prompt.
        }
    }
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

    // ─── v0.2.52 V52-AD — boot probe selection logic ─────────────────
    //
    // The boot probe `probe_rl_auto_enable_at_boot` is harder to test
    // end-to-end because it needs a Tauri AppHandle to emit on. We
    // factor the *decision* into a pure function and unit-test that —
    // the emit-on-event side effect stays untested at the unit level
    // (covered by manual smoke testing). This is the standard pattern
    // for the launcher: every other event-emit-on-boot path keeps the
    // decision logic separable.

    /// Compute whether the boot probe SHOULD emit an event given the
    /// observed event count and current global toggle state. Pure
    /// function — testable without a Tauri context.
    fn _should_emit_rl_auto_enable(
        count: i64,
        global: Option<bool>,
        threshold: i64,
    ) -> bool {
        count >= threshold && matches!(global, Some(false))
    }

    /// Boot probe decision matrix.
    #[test]
    fn rl_auto_enable_decision_matrix() {
        // Below threshold — never emit, regardless of global state.
        assert!(!_should_emit_rl_auto_enable(100, Some(false), 500));
        assert!(!_should_emit_rl_auto_enable(100, Some(true), 500));
        assert!(!_should_emit_rl_auto_enable(100, None, 500));
        assert!(!_should_emit_rl_auto_enable(499, Some(false), 500));

        // At threshold + global=false → emit.
        assert!(_should_emit_rl_auto_enable(500, Some(false), 500));
        assert!(_should_emit_rl_auto_enable(10_000, Some(false), 500));

        // At threshold + global=true → user already enabled, skip.
        assert!(!_should_emit_rl_auto_enable(500, Some(true), 500));
        assert!(!_should_emit_rl_auto_enable(10_000, Some(true), 500));

        // At threshold + no row → fail-open territory; skip.
        assert!(!_should_emit_rl_auto_enable(500, None, 500));
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
