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

use std::path::{Path, PathBuf};

use crate::db::Db;
use vct_launcher_core::db::settings::ModuleEnableState;
use vct_launcher_core::manifest::ModuleManifest;
use vct_launcher_core::process::CommandExt as _;

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
/// — same as the sibling `set_project_module_enabled` command below
/// (moved here from `diagrams_cmd` in v0.2.102). The audit log records
/// every flip for forensic trace.
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

    // v0.2.100 W5R-02: no "turn on" write while the module's effect is locked.
    vct_launcher_core::rl_scoring_lock::refuse_enable_while_locked(&module_id, enabled)?;
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
    // write. Mirrors the pattern in `set_project_module_enabled` below.
    let payload = ModuleEnabledChangedEvent {
        project_id: project_id.clone(),
        module_id: module_id.clone(),
        enabled,
    };
    if let Err(e) = app.emit(MODULE_ENABLED_EVENT, payload) {
        tracing::warn!(
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

    // v0.2.100 W5R-02: no "turn on" write while the module's effect is locked.
    vct_launcher_core::rl_scoring_lock::refuse_enable_while_locked(&module_id, enabled)?;
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
        tracing::warn!(
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

/// v0.2.100 W5R-02: the RL scoring lock the GUI must render — `Some(reason)`
/// while RL scoring is locked off (`vco_lib/rl_scoring_lock.toml`, read by
/// `vct_launcher_core::rl_scoring_lock`), `None` when unlocked. The GUI has no
/// copy of this value; every RL scoring control reads it from here (or from
/// `ModuleEnableState.lock_reason`, the same source).
#[command]
pub fn rl_scoring_lock() -> Option<String> {
    vct_launcher_core::rl_scoring_lock::rl_scoring_lock().map(str::to_string)
}

/// Read the effective enable flag for a (project, module) pair using
/// the v0.2.52 V52-AD cascade (per-project → global → fail-open true).
/// This is the value the hub resolver emits as
/// `rl_reranker_enabled_for_project`.
///
/// GUI code should call [`module_enable_state`] instead: this returns a
/// bare bool, which cannot say WHICH tier answered, and a control rendered
/// from it is the lying-toggle shape (F-3/F-4/F-22). Kept because it is a
/// shipped IPC surface and the cheapest read for a caller that only needs
/// the answer.
#[command]
pub async fn module_effective_enabled(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    db.module_effective_enabled(&project_id, &module_id)
}

// ─── v0.2.91 decision #23 — the tri-state per-project control ────────────
//
// F-3: the only per-project module control a user could click wrote
// `module_installs.enabled`, which the RL gate NEVER reads — unchecking
// "Enabled" on the RL tile flipped a checkbox, toasted success, and changed
// nothing about reranking. F-4: the host-wide panel could never return to
// "no override set" once clicked, at EITHER layer.
//
// Both are the same defect in two places: a control that cannot express, or
// cannot re-enter, a state the backend genuinely has. The fix is WP-L's
// shape, reused rather than reinvented — a setter that takes `Option<bool>`
// (None = CLEAR) and returns the RE-RESOLVED state so the GUI renders from
// truth instead of from what it optimistically asked for.
//
// Routing (unchanged design, now actually honoured by the GUI): for
// PROJECT-scope modules `module_installs.enabled` remains the gate; for
// GLOBAL-scope modules it is this key. See the DB-layer docstring at
// `vct_launcher_core::db::settings::MODULE_ENABLED_FOR_PROJECT_KEY`.
//
// SCOPE OF THE GATE (plan §F #25): reranking. Never telemetry collection —
// the hub's rl-events ingest consults none of this.

/// Read the resolved enable state of one module for one project, WITH
/// provenance (`explicit` / `global_default` / `effective` / `source`).
///
/// One round trip for the whole control. `effective` is the same value
/// [`module_effective_enabled`] returns, by construction — pinned by
/// `resolve_module_enable_matches_effective` in the DB layer.
#[command]
pub async fn module_enable_state(
    project_id: String,
    module_id: String,
    db: State<'_, Db>,
) -> Result<ModuleEnableState, String> {
    if project_id.trim().is_empty() {
        return Err("module_enable_state: project_id required".to_string());
    }
    if module_id.trim().is_empty() {
        return Err("module_enable_state: module_id required".to_string());
    }
    db.resolve_module_enable(&project_id, &module_id)
}

/// Free-function core of [`module_set_enabled_for_project_v2`] — argument
/// guards, the write-or-clear, the audit row, and the re-resolve. No Tauri
/// runtime needed, so every branch is unit-testable.
///
/// The unknown-project guard runs BEFORE the write: returning a clear error
/// here beats the opaque `FOREIGN KEY constraint failed` the `set_setting`
/// INSERT would otherwise surface.
pub fn set_module_enabled_for_project_v2_with_db(
    db: &Db,
    project_id: &str,
    module_id: &str,
    value: Option<bool>,
) -> Result<ModuleEnableState, String> {
    if db.get_project(project_id)?.is_none() {
        return Err(format!(
            "project {} not found; cannot set module enable flag",
            project_id
        ));
    }
    if module_id.trim().is_empty() {
        return Err("module_id must not be empty".to_string());
    }

    // v0.2.100 W5R-02: refuse an explicit ON while locked. OFF and CLEAR stay
    // allowed: neither can turn scoring on, and the rows are the user's.
    vct_launcher_core::rl_scoring_lock::refuse_enable_while_locked(module_id, value == Some(true))?;
    db.module_write_enabled_for_project(project_id, module_id, value)?;

    db.audit(
        "module_enabled_for_project_changed",
        Some(project_id),
        Some(module_id),
        &serde_json::json!({
            // `null` means the override was CLEARED — distinguishable from
            // an explicit false in the forensic trace, which is the whole
            // point of the third state.
            "enabled": value,
            "cleared": value.is_none(),
        }),
    )?;

    db.resolve_module_enable(project_id, module_id)
}

/// Set — or CLEAR — the per-project enable flag for a (global-scope) module.
///
/// `value = None` DELETES the per-project row, returning the project to
/// following the host-wide default. That is a first-class value, not an edge
/// case: without it the control is a one-way door (F-4's shape at the
/// per-project layer).
///
/// Returns the re-resolved state. Auth/permission model + audit + renderer
/// notification are identical to [`module_set_enabled_for_project`]; the
/// event payload carries the EFFECTIVE value so a listener that only wants
/// "is it on now?" needs no follow-up read.
///
/// Guards + write + audit live in the free-function core
/// [`set_module_enabled_for_project_v2_with_db`] so they are unit-testable
/// without a Tauri runtime (the same split WP-L used for
/// `set_dual_flag_global_default_with_db`). Only the emit is left here.
#[command]
pub async fn module_set_enabled_for_project_v2(
    project_id: String,
    module_id: String,
    value: Option<bool>,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<ModuleEnableState, String> {
    let state = set_module_enabled_for_project_v2_with_db(&db, &project_id, &module_id, value)?;

    let payload = ModuleEnabledChangedEvent {
        project_id: project_id.clone(),
        module_id: module_id.clone(),
        enabled: state.effective,
    };
    if let Err(e) = app.emit(MODULE_ENABLED_EVENT, payload) {
        tracing::warn!(
            "[module_enabled] emit({}) failed for ({}, {}): {}",
            MODULE_ENABLED_EVENT, project_id, module_id, e
        );
    }

    Ok(state)
}

/// Clear the GLOBAL (host-wide) enable row for a module — the way back to
/// "no host-wide override set" (F-4).
///
/// Per-project overrides are untouched (only the `project_id IS NULL` row is
/// removed); the uninstall-time `module_clear_enabled_for_project_all` is
/// the one that wipes everything. Idempotent.
///
/// Returns the host-wide value AFTER the clear — always `None`. The GUI
/// re-renders from it rather than assuming.
///
/// Core split out for the same reason as the per-project setter: testable
/// without a Tauri runtime.
pub fn clear_global_enabled_with_db(
    db: &Db,
    module_id: &str,
) -> Result<Option<bool>, String> {
    if module_id.trim().is_empty() {
        return Err("module_id must not be empty".to_string());
    }

    let removed = db.module_clear_global_enabled(module_id)?;

    db.audit(
        "module_global_enabled_changed",
        None,
        Some(module_id),
        &serde_json::json!({
            "enabled": serde_json::Value::Null,
            "cleared": true,
            "rows_removed": removed,
            "scope": "global",
        }),
    )?;

    Ok(db.module_global_enabled(module_id)?)
}

#[command]
pub async fn module_clear_global_enabled(
    module_id: String,
    db: State<'_, Db>,
    app: AppHandle,
) -> Result<Option<bool>, String> {
    clear_global_enabled_with_db(&db, &module_id)?;

    // Same channel as the two setters — every project inheriting the
    // host-wide row now resolves differently, so every mounted panel needs
    // the redraw. `enabled` carries the post-clear system default.
    let payload = ModuleEnabledChangedEvent {
        project_id: GLOBAL_PROJECT_SENTINEL.to_string(),
        module_id: module_id.clone(),
        enabled: true,
    };
    if let Err(e) = app.emit(MODULE_ENABLED_EVENT, payload) {
        tracing::warn!(
            "[module_enabled] emit({}) failed for global clear ({}): {}",
            MODULE_ENABLED_EVENT, module_id, e
        );
    }

    Ok(None)
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
            tracing::warn!(
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
            tracing::warn!(
                "[rl-auto-enable] module_global_enabled probe failed: {}",
                e
            );
            return;
        }
    };

    // v0.2.100 W5R-02: never invite "enable RL" while scoring is locked —
    // the setters would refuse the very action the prompt offers.
    if !should_emit_rl_auto_enable(
        count,
        global,
        RL_AUTO_ENABLE_EVENT_THRESHOLD,
        vct_launcher_core::rl_scoring_lock::rl_scoring_lock(),
    ) {
        return;
    }
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
                tracing::warn!(
                    "[rl-auto-enable] emit({}) failed: {}",
                    RL_AUTO_ENABLE_EVENT, e
                );
            } else {
                tracing::info!(
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

/// The boot probe's decision, pure so every branch is unit-testable: prompt
/// only at/over the threshold, only while the host-wide row is the seeded
/// `false`, and never while RL scoring is locked.
pub fn should_emit_rl_auto_enable(
    count: i64,
    global: Option<bool>,
    threshold: i64,
    lock: Option<&str>,
) -> bool {
    lock.is_none() && count >= threshold && matches!(global, Some(false))
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
            tracing::error!(
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
                tracing::error!(
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
                        tracing::error!(
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
            tracing::error!(
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
            tracing::error!(
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
            tracing::error!(
                "[module_enabled] clear_enabled_rows_for_uninstalled_module({}): \
                 delete failed: {}",
                module_id, e
            );
            0
        }
    }
}

// ─── project_modules machinery (moved from diagrams_cmd.rs, v0.2.102) ─────
//
// These commands read/write the `project_modules` table (migration 022) —
// the per-project active flag behind `{{#if_module_active <name>}}`
// rendering and module-gated bundle delivery. They lived in
// `diagrams_cmd.rs` because `diagrams` was the first module to use them;
// when the diagrams integrations were dropped (owner ruling 2026-10-07)
// every diagram command in that file was deleted and this GENERIC
// machinery moved here, beside the other module-toggle commands.
// `model_gateway.ts` invokes set/clear for the model-gateway module; the
// Python renderer reads `project_modules` from launcher.db directly
// (`vco_lib.module_gated_delivery.active_modules_verdict`), never through
// these commands.

#[command]
pub async fn set_project_module_enabled(
    project_id: String,
    module_name: String,
    enabled: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    db.set_project_module_enabled(&project_id, &module_name, enabled)?;
    db.audit(
        "project_module_set_enabled",
        Some(&project_id),
        None,
        &serde_json::json!({
            "module": module_name,
            "enabled": enabled,
        }),
    )?;

    // Phase 1.5.7 — when a module's enabled flag changes, the
    // `{{#if_module_active <name>}}` block in CLAUDE.md needs to be
    // re-evaluated. Spawn a background subprocess so the toggle
    // remains snappy from the user's perspective; never block the
    // command result on the re-render. Soft-fail throughout: if the
    // subprocess can't be spawned (missing venv, missing project on
    // disk, CLI error) we log a warning and return success — the
    // user already saw the DB toggle land; the re-render is a
    // side-effect.
    spawn_re_render_claude_md(&db, &project_id);

    // v0.2.100 (AD-7, L5-F03): a module row can change what the bundle
    // DELIVERS (today: the model-gateway agent definitions), so the toggle
    // runs the ordinary bundle update instead of leaving the delivery — and
    // the re-rendered CLAUDE.md's claim about it — to whenever the user next
    // updates. The decision of WHAT to deliver stays in Python
    // (`vco_lib.module_gated_delivery`); this only triggers the engine.
    schedule_bundle_update_for_project(&db, &project_id, spawn_bundle_update);

    Ok(())
}

/// Clear a project's EXPLICIT module choice — the way back to "follows the
/// machine" (v0.2.101, G1 follow-up).
///
/// The inverse of [`set_project_module_enabled`], same shape: the row is
/// deleted (nothing is remembered of the previous choice), then the same
/// two side effects a flip triggers run, because a cleared row changes
/// what the bundle DELIVERS exactly like a flip — with no row, the module
/// falls back to its default resolution (for `model_gateway`, the machine
/// gateway signal). `Ok(false)` when no row existed: the state is already
/// the default one, so neither the re-render nor the bundle update has
/// anything to do, and a double-click must not look like a failure.
#[command]
pub async fn clear_project_module(
    project_id: String,
    module_name: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    let cleared = db.clear_project_module(&project_id, &module_name)?;
    if !cleared {
        return Ok(false);
    }
    db.audit(
        "project_module_cleared",
        Some(&project_id),
        None,
        &serde_json::json!({
            "module": module_name,
            "cleared": true,
        }),
    )?;
    // Same reasoning as `set_project_module_enabled`: the CLAUDE.md
    // conditional block and the bundle delivery both read the row (or its
    // absence), so both must re-run. Soft-fail throughout.
    spawn_re_render_claude_md(&db, &project_id);
    schedule_bundle_update_for_project(&db, &project_id, spawn_bundle_update);
    Ok(true)
}

/// Resolve `project_id`'s folder and hand it to `run`. Split from the spawn
/// so the "a toggle triggers delivery" wiring is testable without a Tauri
/// runtime. Soft-fail: an unknown project or a missing folder is logged and
/// nothing runs (the DB row already landed; the next update delivers).
fn schedule_bundle_update_for_project(db: &Db, project_id: &str, run: impl FnOnce(String, PathBuf)) {
    match db.get_project(project_id) {
        Ok(Some(p)) => {
            let folder = PathBuf::from(&p.folder_path);
            if folder.is_dir() {
                run(project_id.to_string(), folder);
            } else {
                tracing::warn!(
                    "[vct] module toggle: project folder {} does not exist; the next \
                     bundle update delivers",
                    folder.display()
                );
            }
        }
        Ok(None) => tracing::warn!(
            "[vct] module toggle: project {} not found; no bundle update",
            project_id
        ),
        Err(e) => tracing::warn!(
            "[vct] module toggle: db lookup for {} failed: {}; no bundle update",
            project_id,
            e
        ),
    }
}

/// Background `install-bundle --update` for one project. Never two engines on
/// one manifest: the engine itself takes the per-folder turn
/// (`single_flight::bundle_engine_turn`, v0.2.100 W3R-06), which "Update all"
/// and the per-project update share — a toggle during either WAITS and then
/// delivers, instead of racing it (or being refused and losing the delivery).
fn spawn_bundle_update(project_id: String, folder: PathBuf) {
    tauri::async_runtime::spawn(async move {
        let (warnings, _summary) =
            crate::commands::projects_v2::run_install_bundle_update(&folder).await;
        for w in &warnings {
            tracing::warn!("[vct] module toggle bundle update ({}): {}", project_id, w);
        }
        tracing::info!(
            "[vct] module toggle: bundle update finished for project {} ({} warning(s))",
            project_id,
            warnings.len()
        );
    });
}

/// Background re-render of `<project_folder>/CLAUDE.md` after a module
/// toggle. Resolves the project folder via `Db::get_project`, locates a
/// usable Python interpreter, and spawns `python -m vco_lib.project_init
/// re-render-claude-md --folder <path> --project-name <name>
/// --project-id <id> --json` detached from the Tauri command. The
/// command returns immediately — the re-render runs in the background.
///
/// Soft-fail philosophy: every error in this path is logged and
/// swallowed. The DB row write already happened in the caller; the
/// template re-render is a follow-on side effect, not a contract.
/// Failures here surface only in the launcher's log and (eventually)
/// the next time `re-render-claude-md` is invoked by some other path.
fn spawn_re_render_claude_md(db: &Db, project_id: &str) {
    let project = match db.get_project(project_id) {
        Ok(Some(p)) => p,
        Ok(None) => {
            tracing::warn!(
                "[vct] re-render-claude-md: project {} not found; \
                 skipping background re-render",
                project_id
            );
            return;
        }
        Err(e) => {
            tracing::warn!(
                "[vct] re-render-claude-md: db lookup for {} failed: {}; \
                 skipping background re-render",
                project_id, e
            );
            return;
        }
    };

    let folder = PathBuf::from(&project.folder_path);
    if !folder.is_dir() {
        tracing::warn!(
            "[vct] re-render-claude-md: project folder {} does not exist; \
             skipping background re-render",
            folder.display()
        );
        return;
    }

    let python = match resolve_project_python(&folder) {
        Some(p) => p,
        None => {
            tracing::warn!(
                "[vct] re-render-claude-md: no usable Python for project \
                 {} (folder={}); skipping background re-render",
                project_id,
                folder.display()
            );
            return;
        }
    };

    // Spawn detached — we don't await stdout/stderr from this child.
    // The CLI itself writes its own logs and falls back gracefully.
    let project_id_str = project_id.to_string();
    let project_name = project.name.clone();
    let folder_str = folder.to_string_lossy().into_owned();
    let spawn_result = std::process::Command::new(&python).silent()
        .arg("-m")
        .arg("vco_lib.project_init")
        .arg("re-render-claude-md")
        .arg("--folder")
        .arg(&folder_str)
        .arg("--project-name")
        .arg(&project_name)
        .arg("--project-id")
        .arg(&project_id_str)
        .arg("--json")
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .stdin(std::process::Stdio::null())
        .spawn();

    match spawn_result {
        Ok(child) => {
            tracing::info!(
                "[vct] re-render-claude-md: spawned pid={} for project {} ({})",
                child.id(),
                project_name,
                project_id_str
            );
        }
        Err(e) => {
            tracing::warn!(
                "[vct] re-render-claude-md: spawn failed for project {} ({}): {}",
                project_name, project_id_str, e
            );
        }
    }
}

/// Pick a Python interpreter for the project-folder re-render. Prefers
/// the project's own `.venv` and the orchestrator's `claude_mcp_servers/.venv`
/// (the canonical install layout). Falls back to `python3` / `python` on
/// PATH. Returns `None` only when nothing usable is found — the caller
/// soft-fails in that case.
fn resolve_project_python(folder: &Path) -> Option<PathBuf> {
    let exe_name = if cfg!(windows) { "python.exe" } else { "python" };
    let bin_dir = if cfg!(windows) { "Scripts" } else { "bin" };

    let candidates = [
        folder.join(".venv").join(bin_dir).join(exe_name),
        folder
            .join("claude_mcp_servers")
            .join(".venv")
            .join(bin_dir)
            .join(exe_name),
    ];
    for c in candidates.iter() {
        if c.is_file() {
            return Some(c.clone());
        }
    }
    // Last resort: PATH lookup. Use `which`-style on POSIX, where the
    // PATH chain typically has `python3` first. We avoid shelling out
    // to `which` directly; the spawn will fail later if PATH lookup is
    // empty and we'll log a warning then.
    if cfg!(windows) {
        Some(PathBuf::from("python.exe"))
    } else {
        Some(PathBuf::from("python3"))
    }
}

// ─── Module-active lookup ───────────────────────────────────────────────

/// Set of modules that are orchestrator-bundled-default-active. When
/// `is_project_module_active` is called for one of these and no row
/// exists in `project_modules` yet, we treat the absence as a
/// pre-v0.2.33 backfill gap (these projects existed before the seed
/// path in `projects_v2.rs:521` was wired) — auto-seed the row with
/// `enabled=true` and return true.
///
/// The user's explicit opt-out writes `enabled=0` which is a real row,
/// so the backfill never re-enables what the user disabled.
///
/// EMPTY since v0.2.102: the only member was `diagrams`, dropped with
/// the diagrams integrations (owner ruling 2026-10-07) along with its
/// project-creation seeding in `projects_v2.rs`. The mechanism stays for
/// any future orchestrator-bundled module that ships default-active.
const ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES: &[&str] = &[];

/// Read the `project_modules` active flag for a (project, module) pair.
/// Returns `false` for unknown pairs (no row) UNLESS the module is in
/// the orchestrator-bundled-default-active set (see
/// `ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES` — empty since v0.2.102),
/// in which case the row is seeded with `enabled=true` and we return true.
///
/// Soft-fail: if the seed write fails (e.g. transient DB lock) we log
/// + still return `true` so the user-facing surface isn't blocked on a
/// write retry. The next call retries the seed.
#[command]
pub async fn is_project_module_active(
    project_id: String,
    module_name: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    // First check: row exists?
    let active = db
        .is_module_active(&project_id, &module_name)
        .map_err(|e| format!("is_project_module_active: {e}"))?;
    if active {
        return Ok(true);
    }

    // Row absent or explicitly disabled. The DB layer collapses both
    // cases into `false`, so we need to disambiguate before deciding
    // whether to backfill. A direct query keeps the DB layer pure
    // (no new method needed on `Db`) and is cheap (~1 row scan).
    if ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES.contains(&module_name.as_str())
        && !has_module_row(&db, &project_id, &module_name)?
    {
        // Backfill: pre-v0.2.33 projects predate the seed path in
        // `projects_v2.rs:521`, so the row is missing rather than
        // user-disabled. Seed it default-active.
        if let Err(e) = db.set_project_module_enabled(&project_id, &module_name, true) {
            tracing::warn!(
                "[vct] is_project_module_active: backfill seed failed for \
                 ({}, {}): {} — returning true anyway; next call retries",
                project_id, module_name, e
            );
        } else {
            // Audit so we can trace retroactive seeds in support cases.
            let _ = db.audit(
                "project_module_backfill_seed",
                Some(&project_id),
                None,
                &serde_json::json!({
                    "module": module_name,
                    "reason": "orchestrator_bundled_default_active",
                }),
            );
        }
        return Ok(true);
    }

    Ok(false)
}

/// Returns true if `(project_id, module_name)` has any row in
/// `project_modules` (regardless of `enabled` value). Used to
/// disambiguate "row absent" from "row present, disabled" in
/// `is_project_module_active`. Soft-fails on DB errors — returns
/// false so the caller's backfill path runs (the row will get
/// recreated, idempotent UPSERT).
fn has_module_row(db: &Db, project_id: &str, module_name: &str) -> Result<bool, String> {
    let guard = db.lock();
    let exists: bool = guard
        .query_row(
            "SELECT 1 FROM project_modules
             WHERE project_id = ?1 AND module_name = ?2
             LIMIT 1",
            rusqlite::params![project_id, module_name],
            |_| Ok(true),
        )
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(format!("has_module_row({}, {}): {}", project_id, module_name, other)),
        })?;
    Ok(exists)
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

    /// The real decision fn with the lock lifted (the pre-lock matrix).
    fn _should_emit_rl_auto_enable(count: i64, global: Option<bool>, threshold: i64) -> bool {
        should_emit_rl_auto_enable(count, global, threshold, None)
    }

    /// v0.2.100 W5R-02: while RL scoring is locked the prompt never fires,
    /// even in the one cell that would otherwise prompt.
    #[test]
    fn rl_auto_enable_never_prompts_while_locked() {
        assert!(should_emit_rl_auto_enable(10_000, Some(false), 500, None));
        assert!(!should_emit_rl_auto_enable(10_000, Some(false), 500, Some("locked")));
        assert!(vct_launcher_core::rl_scoring_lock::rl_scoring_lock().is_some(), "ships locked");
    }

    /// v0.2.100 W5R-02: while locked, the per-project setter refuses an
    /// explicit ON and writes nothing; OFF and CLEAR are allowed; the
    /// resolved state carries the lock reason.
    #[test]
    fn locked_setter_refuses_on_and_allows_off_and_clear() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");
        let m = "vct-rl-reranker";
        let err = set_module_enabled_for_project_v2_with_db(&db, "p1", m, Some(true)).unwrap_err();
        assert!(err.contains("locked"), "{err}");
        assert_eq!(db.resolve_module_enable("p1", m).unwrap().explicit, None, "nothing written");

        let st = set_module_enabled_for_project_v2_with_db(&db, "p1", m, Some(false)).unwrap();
        assert_eq!(st.explicit, Some(false));
        assert!(st.lock_reason.is_some());
        let st = set_module_enabled_for_project_v2_with_db(&db, "p1", m, None).unwrap();
        assert_eq!(st.explicit, None);

        // Unlocked modules are unaffected.
        assert!(set_module_enabled_for_project_v2_with_db(&db, "p1", "vct-coordination", Some(true)).is_ok());
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

    // ─── v0.2.91 decision #23 — command-layer contracts ──────────────
    //
    // The cascade itself is tested in vct-launcher-core. What is tested
    // HERE is what this layer owns: the argument guards, and the fact
    // that the audit trail can tell a CLEAR apart from an explicit off
    // (without that, the third state is invisible to forensics even
    // though it is visible in the GUI). The Tauri wrappers need an
    // AppHandle to emit on, so — following this file's existing pattern
    // for `probe_rl_auto_enable_at_boot` — the emit stays uncovered at
    // the unit level and the decisions are driven against `&Db`.

    use vct_launcher_core::db::settings::ModuleEnableSource;

    /// The whole tri-state walk through the command core: inherit → on →
    /// back to inherit, then the host-wide clear. Every step asserts the
    /// state the command RETURNS (which is what the control re-renders
    /// from), not just the row.
    #[test]
    fn tri_state_round_trip_through_the_command_core() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");
        db.module_set_global_enabled("vct-rl-reranker", false).unwrap();

        // Explicit on for this project — beats the host-wide off. Written at
        // the DB layer: an ON row stored before the v0.2.100 RL scoring lock
        // (the command core refuses a NEW ON while locked — see
        // `locked_setter_refuses_on_and_allows_off_and_clear`).
        db.module_write_enabled_for_project("p1", "vct-rl-reranker", Some(true))
            .expect("write must succeed");
        let st = db.resolve_module_enable("p1", "vct-rl-reranker").unwrap();
        assert_eq!(st.explicit, Some(true));
        assert_eq!(st.source, ModuleEnableSource::Project);
        assert!(st.effective);

        // Back to inheriting — the state that had no way back before #23.
        let st = set_module_enabled_for_project_v2_with_db(&db, "p1", "vct-rl-reranker", None)
            .expect("clear must succeed");
        assert_eq!(st.explicit, None, "the row is gone, not set to a value");
        assert_eq!(st.source, ModuleEnableSource::GlobalDefault);
        assert!(!st.effective, "now following the host-wide off");

        // And the host-wide clear leaves the fail-open system default.
        assert_eq!(
            clear_global_enabled_with_db(&db, "vct-rl-reranker").expect("clear"),
            None,
        );
        let st = db.resolve_module_enable("p1", "vct-rl-reranker").unwrap();
        assert_eq!(st.source, ModuleEnableSource::SystemDefault);
        assert!(
            !st.effective,
            "the RL reranker resolves OFF when nothing is set anywhere (v0.2.100)"
        );
    }

    /// The audit trail can tell a CLEAR apart from an explicit off. Without
    /// this the third state is invisible to forensics even though it is
    /// visible in the GUI. Read back from the audit table the core writes.
    #[test]
    fn audit_row_distinguishes_clear_from_explicit_off() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");

        set_module_enabled_for_project_v2_with_db(&db, "p1", "vct-rl-reranker", Some(false))
            .unwrap();
        set_module_enabled_for_project_v2_with_db(&db, "p1", "vct-rl-reranker", None).unwrap();

        let guard = db.lock();
        let mut stmt = guard
            .prepare(
                "SELECT detail FROM audit_log
                  WHERE operation = 'module_enabled_for_project_changed'
               ORDER BY id ASC",
            )
            .expect("prepare");
        let rows: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .expect("query")
            .map(|r| r.expect("row"))
            .collect();
        drop(stmt);
        drop(guard);

        assert_eq!(rows.len(), 2, "one audit row per write");
        let first: serde_json::Value = serde_json::from_str(&rows[0]).unwrap();
        let second: serde_json::Value = serde_json::from_str(&rows[1]).unwrap();
        assert_eq!(first["enabled"], serde_json::json!(false));
        assert_eq!(first["cleared"], serde_json::json!(false));
        assert_eq!(second["enabled"], serde_json::Value::Null);
        assert_eq!(second["cleared"], serde_json::json!(true));
    }

    /// Guards, both refusing branches. An unknown project is refused with a
    /// clear message BEFORE any write (rather than surfacing the opaque
    /// FK-violation cascade), and an empty module id never writes an orphan
    /// row the cleanup helpers could not find again.
    #[test]
    fn command_core_guards_refuse_and_write_nothing() {
        let db = mkdb();
        mkproject(&db, "p1", "p1");

        let err = set_module_enabled_for_project_v2_with_db(
            &db, "ghost", "vct-rl-reranker", Some(true),
        )
        .expect_err("unknown project must be refused");
        assert!(err.contains("not found"), "unhelpful message: {err}");
        assert!(db
            .list_module_settings("ghost", "vct-rl-reranker")
            .unwrap()
            .is_empty());

        let err = set_module_enabled_for_project_v2_with_db(&db, "p1", "   ", Some(true))
            .expect_err("empty module_id must be refused");
        assert!(err.contains("module_id"), "unhelpful message: {err}");
        assert!(clear_global_enabled_with_db(&db, "").is_err());

        // Leave-alone half: the real project/module pair still writes. An
        // explicit OFF, because a NEW explicit ON is refused while the RL
        // scoring lock is set (v0.2.100 W5R-02 — see
        // `locked_setter_refuses_on_and_allows_off_and_clear`).
        assert_eq!(
            set_module_enabled_for_project_v2_with_db(
                &db, "p1", "vct-rl-reranker", Some(false)
            )
            .expect("valid write must succeed")
            .explicit,
            Some(false),
        );
    }

    // ─── project_modules machinery tests (moved from diagrams_cmd.rs, v0.2.102)
    //
    // `make_db_with_project` mirrors the helper these tests had in
    // diagrams_cmd.rs: an in-memory Db plus a project row pointing at a
    // REAL folder (the bundle-update scheduler refuses folder-less rows).

    fn make_db_with_project(id: &str, name: &str, folder: &Path) -> Db {
        let db = mkdb();
        db.insert_project(id, name, &folder.to_string_lossy(), ProjectHost::Base, id)
            .expect("insert project");
        db
    }

    #[test]
    fn is_module_active_returns_false_for_absent_row() {
        // For an unregistered (project, module) pair the answer is `false`
        // — never an error.
        //
        // NOTE: this exercises the DB layer directly (Db::is_module_active),
        // NOT the Tauri command. The command layer adds the backfill for
        // modules in ORCHESTRATOR_BUNDLED_DEFAULT_ACTIVE_MODULES (empty
        // since v0.2.102) — see `has_module_row_distinguishes_absent_from_disabled`
        // below for the command-layer contract.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        let r = db.is_module_active("p1", "model_gateway").unwrap();
        assert!(!r, "unknown module should be inactive (got {})", r);
    }

    /// v0.2.100 (AD-7, L5-F03): the toggle triggers the bundle update for
    /// the toggled project's folder (act) — and runs nothing for a project
    /// whose folder is gone or that is not registered (leave-alone).
    #[test]
    fn module_toggle_schedules_the_bundle_update_for_the_project_folder() {
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        let mut seen: Vec<(String, PathBuf)> = Vec::new();
        schedule_bundle_update_for_project(&db, "p1", |id, f| seen.push((id, f)));
        assert_eq!(seen, vec![("p1".to_string(), dir.path().to_path_buf())]);

        let mut none: Vec<(String, PathBuf)> = Vec::new();
        schedule_bundle_update_for_project(&db, "missing", |id, f| none.push((id, f)));
        let gone = make_db_with_project("p2", "Gone", &dir.path().join("absent"));
        schedule_bundle_update_for_project(&gone, "p2", |id, f| none.push((id, f)));
        assert!(none.is_empty(), "nothing may run without a real folder: {none:?}");
    }

    #[test]
    fn has_module_row_distinguishes_absent_from_disabled() {
        // The Tauri command layer's backfill relies on `has_module_row`
        // to tell apart "no row exists yet" from "row exists with
        // enabled=0". Without this distinction we'd re-enable modules
        // the user explicitly opted out of.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());

        // Absent row → returns false.
        assert!(!has_module_row(&db, "p1", "model_gateway").unwrap());

        // Disabled row → returns true (row exists, even if enabled=0).
        db.set_project_module_enabled("p1", "model_gateway", false).unwrap();
        assert!(has_module_row(&db, "p1", "model_gateway").unwrap());

        // Enabled row → also true.
        db.set_project_module_enabled("p1", "model_gateway", true).unwrap();
        assert!(has_module_row(&db, "p1", "model_gateway").unwrap());
    }

    #[test]
    fn is_module_active_round_trips_through_set_then_get() {
        // Toggle on → reflects as true; toggle off → reflects as false.
        // Mirrors the lifecycle exercised by `set_project_module_enabled`
        // + `is_project_module_active` from the frontend.
        let dir = tempfile::tempdir().unwrap();
        let db = make_db_with_project("p1", "Acme", dir.path());
        db.set_project_module_enabled("p1", "model_gateway", true).unwrap();
        assert!(db.is_module_active("p1", "model_gateway").unwrap());
        db.set_project_module_enabled("p1", "model_gateway", false).unwrap();
        assert!(!db.is_module_active("p1", "model_gateway").unwrap());
    }

    #[test]
    fn resolve_project_python_prefers_project_venv() {
        // Mirror the canonical install layout: <project>/.venv/{bin|Scripts}/python
        // wins over <project>/claude_mcp_servers/.venv/...
        let dir = tempfile::tempdir().unwrap();
        let bin_dir = if cfg!(windows) { "Scripts" } else { "bin" };
        let exe_name = if cfg!(windows) { "python.exe" } else { "python" };
        let project_venv_py = dir.path().join(".venv").join(bin_dir).join(exe_name);
        let mcp_venv_py = dir
            .path()
            .join("claude_mcp_servers")
            .join(".venv")
            .join(bin_dir)
            .join(exe_name);
        std::fs::create_dir_all(project_venv_py.parent().unwrap()).unwrap();
        std::fs::create_dir_all(mcp_venv_py.parent().unwrap()).unwrap();
        std::fs::write(&project_venv_py, b"").unwrap();
        std::fs::write(&mcp_venv_py, b"").unwrap();
        let picked = resolve_project_python(dir.path()).unwrap();
        assert_eq!(picked, project_venv_py);
    }

    #[test]
    fn resolve_project_python_falls_back_to_path_when_no_venv() {
        let dir = tempfile::tempdir().unwrap();
        let picked = resolve_project_python(dir.path()).unwrap();
        // The fallback is OS-dependent but never None — the caller
        // soft-fails on spawn if the PATH lookup also misses.
        let expected = if cfg!(windows) { "python.exe" } else { "python3" };
        assert_eq!(picked, PathBuf::from(expected));
    }
}
