//! Per-project RL Reranker settings — the small remaining surface of
//! genuinely RL-specific Tauri commands that haven't (yet) been
//! converted to declarative-action manifest entries.
//!
//! These commands back the boolean toggles + the global-training-source
//! enumeration declared in `paid-modules/vct-rl-reranker/vct-module.json`'s
//! `gui.config_tab` block (via `ActionRef::Legacy("set_rl_use_global")`
//! etc.). Settings are stored in `module_settings` (the generic per-
//! project, per-module KV blob table) under `module_id = "vct-rl-reranker"`.
//!
//! Three flags are persisted per-project (boolean, default false):
//!
//!   * `rl_use_global` — "read-only global mode". When true, online
//!     training events from this project DO NOT update the local model.
//!   * `rl_online_training_disabled` — freezes the local model AND
//!     marks new events as log-only. Independent of `rl_use_global`.
//!   * `rl_global_training_source_flag` — opts this project's data
//!     INTO the global model's retraining corpus.
//!
//! v0.2.26 (2026-05-22): the four reset/retrain stub commands that
//! previously lived here (`rl_reset_to_global`, `rl_reset_and_specialize`,
//! `retrain_global_online`, `retrain_global_offline`) were removed
//! once the generic declarative dispatcher (`module_dispatch_action`)
//! landed. Those operations are now expressed as `ActionDescriptor::Http`
//! entries in the RL manifest — the launcher executes them generically
//! without per-module Rust code. The remaining commands stay legacy-
//! routed for now because they read/aggregate per-project DB state
//! (the dispatcher's HTTP-only descriptor doesn't speak SQL); a future
//! release MAY introduce a `ActionDescriptor::Db` variant or migrate
//! them through an RL-side endpoint. No urgency.
//!
//! Side-effect contract: the `on_change` Tauri commands declared in
//! the manifest accept the standard `{ moduleId, value, projectId }`
//! envelope the schema renderer sends.

use serde::Serialize;
use tauri::{command, State};

use crate::db::Db;
use crate::manifest::SelectOption;

const MODULE_ID: &str = "vct-rl-reranker";

// ─── Wire types ──────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize)]
pub struct ProjectOption {
    /// Value the multi-select control persists (project id).
    pub value: String,
    /// Human-readable label.
    pub label: String,
}

impl From<ProjectOption> for SelectOption {
    fn from(p: ProjectOption) -> Self {
        // v0.2.32 L6: SelectOption gained `badge` + `meta` (optional,
        // serde-default to None). Project-options carry no per-option
        // metadata so we leave both empty — back-compat preserved.
        SelectOption {
            value: p.value,
            label: p.label,
            badge: None,
            meta: None,
        }
    }
}

// (`RetrainResult` struct, previously the return type of the four
// retrain/reset stubs, was removed alongside them in v0.2.26.
// Descriptor-driven actions return raw JSON `serde_json::Value` from
// the dispatcher.)

// ─── Per-project flag setters ────────────────────────────────────────────
//
// Each setter writes the boolean into `module_settings` under the
// canonical key. We deliberately use the same key string the
// schema-rendered tab also writes to via the generic
// `set_module_setting` command — so this dedicated path stays in sync
// with the renderer's KV writes. Why both? The dedicated setters
// validate the value type (must be bool) and can log a typed audit
// event (TODO Stream 3); the generic path accepts any JSON value.

fn set_bool_flag(
    db: &Db,
    project_id: &str,
    key: &str,
    value: bool,
) -> Result<(), String> {
    db.set_setting(
        project_id,
        MODULE_ID,
        key,
        &serde_json::Value::Bool(value),
    )
}

fn get_bool_flag(db: &Db, project_id: &str, key: &str) -> Result<bool, String> {
    Ok(db
        .get_setting(project_id, MODULE_ID, key)?
        .and_then(|v| v.as_bool())
        .unwrap_or(false))
}

// ─── v0.2.71 T-B-flags: module-id-parameterised bool flag helpers ────────
//
// The two `set_bool_flag` / `get_bool_flag` helpers above are pinned to
// `MODULE_ID = "vct-rl-reranker"`. The T-B-flags pair below needs to write
// under TWO module_ids — `orchestrator-core` (dual_embedding_write_all_slots)
// and `vct-rl-reranker` (dual_rl_log_enabled) — so we add explicit
// module-id-taking variants rather than overloading the pinned ones. Same
// JSON-bool encoding + `unwrap_or(false)` default contract as the pinned
// helpers, so the hub resolver's `get_setting(...).as_bool().unwrap_or(false)`
// reader round-trips byte-identically.

/// Canonical `module_id` the `dual_embedding_write_all_slots` flag lives
/// under. Orchestrator-core scope (not RL-specific): the flag controls
/// the embedding service's secondary-slot dual-write, which is an
/// orchestrator-wide indexing concern.
const ORCHESTRATOR_CORE_MODULE_ID: &str = "orchestrator-core";

/// Setting key for the per-project "write embeddings to ALL named-vector
/// slots" flag. Consumed (via the projected `DUAL_EMBEDDING_WRITE_ALL_SLOTS`
/// env) by `vco_lib/embedding_service.py::_dual_embedding_write_all_slots`.
const DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY: &str = "dual_embedding_write_all_slots";

/// Setting key for the per-project "also log RL events under the secondary
/// embedding slot" flag. Consumed (via the projected `DUAL_RL_LOG_ENABLED`
/// env) by the RL telemetry path in
/// `claude_mcp_servers/weaviate_mcp/server.py::_resolve_dual_rl_log_enabled`
/// (T-C). Lives under `vct-rl-reranker` because it gates RL-specific logging.
const DUAL_RL_LOG_ENABLED_KEY: &str = "dual_rl_log_enabled";

/// v0.2.88 (DEFECT 5) — setting key for the per-project "also write embeddings
/// into a SECONDARY arctic slot" flag. Consumed (via the projected
/// `DUAL_EMBEDDING_ARCTIC_SECONDARY` env) by
/// `vco_lib/embedding_service.py::_resolve_dual_embedding_arctic_secondary`.
/// Orchestrator-core scope like `dual_embedding_write_all_slots` (an embedding
/// indexing concern, not RL-specific). Added this cycle so all THREE dual-write
/// flags share ONE canonical channel (DB → projection → env); pre-fix it was
/// env-only and survived updates only by being UNKNOWN to the projection (luck,
/// not design). Now the DB is the truth that POPULATES it.
const DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY: &str = "dual_embedding_arctic_secondary";

fn set_bool_flag_for_module(
    db: &Db,
    project_id: &str,
    module_id: &str,
    key: &str,
    value: bool,
) -> Result<(), String> {
    db.set_setting(project_id, module_id, key, &serde_json::Value::Bool(value))
}

fn get_bool_flag_for_module(
    db: &Db,
    project_id: &str,
    module_id: &str,
    key: &str,
) -> Result<bool, String> {
    Ok(db
        .get_setting(project_id, module_id, key)?
        .and_then(|v| v.as_bool())
        .unwrap_or(false))
}

/// "Use global model (read-only)" — when true, this project's online
/// training events are NOT applied to its local model. Project still
/// reads from the local checkpoint (which equals the last sync from
/// global until a reset re-forks it).
#[command]
pub async fn set_rl_use_global(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_rl_use_global: project_id required".into());
    }
    set_bool_flag(&db, &project_id, "rl_use_global", value)
}

/// "Disable online training for this project" — freezes the local
/// model. New rl_update events are still LOGGED so offline passes can
/// pick them up; they just don't update the live weights.
#[command]
pub async fn set_rl_online_training_disabled(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_rl_online_training_disabled: project_id required".into());
    }
    set_bool_flag(&db, &project_id, "rl_online_training_disabled", value)
}

/// "Use this project's data to train the global model" — independent
/// of the read-only / freeze toggles. Drives the multi-select in the
/// "Global Model" section: only projects with this flag set true
/// appear in `list_rl_global_training_source_projects`.
#[command]
pub async fn set_rl_global_training_source_flag(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    if project_id.is_empty() {
        return Err("set_rl_global_training_source_flag: project_id required".into());
    }
    set_bool_flag(&db, &project_id, "rl_global_training_source_flag", value)
}

// ─── Per-project flag getters (v0.2.40 H2) ───────────────────────────────
//
// Counterparts to the three setters above. v0.2.31 Agent J built the
// `RlRerankerDashboardWidget` but it never had a path to load the
// persisted flag state on mount — the renderer-driven `get_module_setting`
// path is fine for the schema-driven config tab, but the widget wraps
// the three flags into a single status summary and is cheaper to read
// via dedicated getters (no JSON deserialisation, typed bool returns,
// missing-row = false default baked in).
//
// Default for any missing row is `false`, matching `get_bool_flag`'s
// semantics. Rejects empty `project_id` (caller bug, not a soft-fail).

/// Read back the persisted "Use global model (read-only)" flag.
#[command]
pub async fn get_rl_use_global(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_rl_use_global: project_id required".into());
    }
    get_bool_flag(&db, &project_id, "rl_use_global")
}

/// Read back the persisted "Disable online training for this project" flag.
#[command]
pub async fn get_rl_online_training_disabled(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_rl_online_training_disabled: project_id required".into());
    }
    get_bool_flag(&db, &project_id, "rl_online_training_disabled")
}

/// Read back the persisted "Use this project's data to train the
/// global model" flag.
#[command]
pub async fn get_rl_global_training_source_flag(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_rl_global_training_source_flag: project_id required".into());
    }
    get_bool_flag(&db, &project_id, "rl_global_training_source_flag")
}

// ─── v0.2.71 T-B-flags: dual-write + dual-log per-project flags ───────────
//
// Two NEW per-project boolean flags, default OFF, with the launcher.db
// `module_settings` table as the SINGLE source of truth. Both mirror the
// well-behaved `module_set_enabled_for_project` reference: write to
// `module_settings`, resolve in `config_api.rs` into `ProjectConfig`,
// project to `.claude/settings.json env` via `config_projection.py`.
//
//   * `dual_embedding_write_all_slots` (orchestrator-core scope) →
//     `DUAL_EMBEDDING_WRITE_ALL_SLOTS` env. Before T-B-flags this flag was
//     env-only with the DB unaware; now the DB is the truth that POPULATES
//     the env. `embedding_service.py` keeps reading the env as-is.
//   * `dual_rl_log_enabled` (vct-rl-reranker scope) → `DUAL_RL_LOG_ENABLED`
//     env. Closes T-C's `TODO(T-B-flags)` in
//     `weaviate_mcp/server.py::_resolve_dual_rl_log_enabled` (T-C reads the
//     env; this projection populates it from the DB).
//
// Dependency invariant (enforced GUI-side + by the setter guard below):
// dual-logs ⟹ dual-write. Enabling `dual_rl_log_enabled` requires
// `dual_embedding_write_all_slots` to be ON, because the secondary-slot RL
// log rows can only be written if the secondary embedding slot is being
// populated at all. The setter force-enables the prerequisite when
// `dual_rl_log_enabled` is turned on so the two flags can never reach the
// incoherent (log=true, write=false) state.

/// Free-function core of `set_dual_embedding_write_all_slots` — the DB
/// write + coherence cascade + F5 env re-projection, testable without a
/// Tauri runtime. The `#[command]` wrapper below only delegates.
///
/// F5 (v0.2.72): the flag projects into `.claude/settings.json env` as
/// `DUAL_EMBEDDING_WRITE_ALL_SLOTS` (an `MCP_RELEVANT_ENV_KEYS` member),
/// so after the write we re-project the project's env files — that's what
/// lets the settings watcher's diff-guard fire the guarded MCP reload.
/// Soft-fail: projection warnings ride in the returned result; the DB
/// write is never rolled back.
pub fn set_dual_embedding_write_all_slots_with_db(
    db: &Db,
    project_id: &str,
    value: bool,
) -> Result<crate::commands::projects_v2::RefreshProjectEnvResult, String> {
    if project_id.is_empty() {
        return Err("set_dual_embedding_write_all_slots: project_id required".into());
    }
    set_bool_flag_for_module(
        db,
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY,
        value,
    )?;
    // Coherence cascade: a dependent dual-log flag cannot survive its
    // prerequisite being switched off. Mirror the GUI's grey-out by
    // force-disabling the dependent here so the DB never holds the
    // incoherent (log=true, write=false) pair even if the caller bypasses
    // the GUI (e.g. a direct Tauri invoke or a future scripted setter).
    if !value {
        set_bool_flag_for_module(
            db,
            project_id,
            MODULE_ID,
            DUAL_RL_LOG_ENABLED_KEY,
            false,
        )?;
    }
    Ok(crate::commands::projects_v2::reproject_env_soft(db, project_id))
}

/// Set the per-project "write embeddings to ALL named-vector slots" flag.
/// Stored in `module_settings(project_id, "orchestrator-core",
/// "dual_embedding_write_all_slots")`. Default OFF when no row exists.
///
/// Turning this OFF while `dual_rl_log_enabled` is ON would leave the
/// dependent flag incoherent (RL dual-logging with no secondary slot to
/// log into). We therefore cascade: disabling the prerequisite also
/// disables the dependent flag. Enabling has no cascade (the dependent
/// stays whatever it was).
#[command]
pub async fn set_dual_embedding_write_all_slots(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    set_dual_embedding_write_all_slots_with_db(&db, &project_id, value).map(|_| ())
}

/// Read back the persisted "write embeddings to ALL named-vector slots"
/// flag. Default `false` for a missing row (opt-in).
#[command]
pub async fn get_dual_embedding_write_all_slots(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_embedding_write_all_slots: project_id required".into());
    }
    get_bool_flag_for_module(
        &db,
        &project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY,
    )
}

/// Free-function core of `set_dual_rl_log_enabled` — DB write + coherence
/// cascade + F5 env re-projection, testable without a Tauri runtime. The
/// `#[command]` wrapper below only delegates.
///
/// F5 (v0.2.72): `DUAL_RL_LOG_ENABLED` is an `MCP_RELEVANT_ENV_KEYS`
/// member (the MCP's `_resolve_dual_rl_log_enabled` reads it from env),
/// so the write re-projects the project's env files. Soft-fail — see
/// `set_dual_embedding_write_all_slots_with_db`.
pub fn set_dual_rl_log_enabled_with_db(
    db: &Db,
    project_id: &str,
    value: bool,
) -> Result<crate::commands::projects_v2::RefreshProjectEnvResult, String> {
    if project_id.is_empty() {
        return Err("set_dual_rl_log_enabled: project_id required".into());
    }
    if value {
        // Force-enable the prerequisite BEFORE writing the dependent so an
        // observer can never read (log=true, write=false). dual-logging
        // requires the secondary slot to be populated.
        set_bool_flag_for_module(
            db,
            project_id,
            ORCHESTRATOR_CORE_MODULE_ID,
            DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY,
            true,
        )?;
    }
    set_bool_flag_for_module(
        db,
        project_id,
        MODULE_ID,
        DUAL_RL_LOG_ENABLED_KEY,
        value,
    )?;
    Ok(crate::commands::projects_v2::reproject_env_soft(db, project_id))
}

/// Set the per-project "also log RL events under the secondary embedding
/// slot" flag. Stored in `module_settings(project_id, "vct-rl-reranker",
/// "dual_rl_log_enabled")`. Default OFF when no row exists.
///
/// Dependency: dual-logs ⟹ dual-write. Turning this ON force-enables
/// `dual_embedding_write_all_slots` (the prerequisite) so the two flags
/// stay coherent regardless of GUI state. Turning it OFF has no cascade.
#[command]
pub async fn set_dual_rl_log_enabled(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    set_dual_rl_log_enabled_with_db(&db, &project_id, value).map(|_| ())
}

/// Read back the persisted "also log RL events under the secondary
/// embedding slot" flag. Default `false` for a missing row (opt-in).
#[command]
pub async fn get_dual_rl_log_enabled(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_rl_log_enabled: project_id required".into());
    }
    get_bool_flag_for_module(&db, &project_id, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY)
}

/// v0.2.88 (DEFECT 5) — free-function core of `set_dual_embedding_arctic_secondary`.
/// DB write + F5 env re-projection, testable without a Tauri runtime. Unlike the
/// two sibling flags there is NO coherence cascade: the arctic-secondary slot is
/// independent of the all-slots dual-write (a qwen3-active install can collect an
/// arctic secondary corpus without also writing every named-vector slot).
///
/// `DUAL_EMBEDDING_ARCTIC_SECONDARY` is an env the embedding service reads, so the
/// write re-projects the project's env files (soft-fail — the DB write is never
/// rolled back on a projection warning).
pub fn set_dual_embedding_arctic_secondary_with_db(
    db: &Db,
    project_id: &str,
    value: bool,
) -> Result<crate::commands::projects_v2::RefreshProjectEnvResult, String> {
    if project_id.is_empty() {
        return Err("set_dual_embedding_arctic_secondary: project_id required".into());
    }
    set_bool_flag_for_module(
        db,
        project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY,
        value,
    )?;
    Ok(crate::commands::projects_v2::reproject_env_soft(db, project_id))
}

/// Set the per-project "also write embeddings into a secondary arctic slot"
/// flag. Stored in `module_settings(project_id, "orchestrator-core",
/// "dual_embedding_arctic_secondary")`. Default OFF when no row exists.
///
/// Independent of the other two dual-write flags — no cascade either way.
#[command]
pub async fn set_dual_embedding_arctic_secondary(
    project_id: String,
    value: bool,
    db: State<'_, Db>,
) -> Result<(), String> {
    set_dual_embedding_arctic_secondary_with_db(&db, &project_id, value).map(|_| ())
}

/// Read back the persisted "also write embeddings into a secondary arctic slot"
/// flag. Default `false` for a missing row (opt-in).
#[command]
pub async fn get_dual_embedding_arctic_secondary(
    project_id: String,
    db: State<'_, Db>,
) -> Result<bool, String> {
    if project_id.is_empty() {
        return Err("get_dual_embedding_arctic_secondary: project_id required".into());
    }
    get_bool_flag_for_module(
        &db,
        &project_id,
        ORCHESTRATOR_CORE_MODULE_ID,
        DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY,
    )
}

// ─── Reset / retrain (STUBS for Stream 2) ───────────────────────────────

/// "Reset to global model" — re-forks this project's local model from
/// the current global checkpoint. STUB for Stream 2: returns ok with a
/// note. Stream 3 wires this to the RL container's `/reset` endpoint.
///
// ─── Removed in v0.2.26 (2026-05-22): four stub Tauri commands ──────────
//
// `rl_reset_to_global`, `rl_reset_and_specialize`, `retrain_global_online`,
// `retrain_global_offline` were stub bodies waiting for the now-shipped
// generic declarative dispatcher (`module_dispatch_action`). The RL
// module's `vct-module.json` migrates to ActionDescriptor::Http entries
// that hit the RL container directly — no per-module Tauri commands
// needed. See `knowledge/concepts/module-contributed-gui-tabs.md` for
// the wire shape + `knowledge/concepts/generic-per-module-db-architecture.md`
// for the port-resolution backstop the dispatcher uses.

// ─── Global training source enumeration ─────────────────────────────────

/// Enumerate projects whose `rl_global_training_source_flag == true`.
/// Backs the multi-select in the "Global Model" section so the user
/// only sees projects they've opted in.
///
/// Works without Stream 3 — purely a DB read against `module_settings`.
#[command]
pub async fn list_rl_global_training_source_projects(
    db: State<'_, Db>,
) -> Result<Vec<ProjectOption>, String> {
    let projects = db.list_projects()?;
    let mut out = Vec::new();
    for project in projects {
        let flag = get_bool_flag(&db, &project.id, "rl_global_training_source_flag")
            .unwrap_or(false);
        if flag {
            out.push(ProjectOption {
                value: project.id.clone(),
                label: project.name.clone(),
            });
        }
    }
    Ok(out)
}

// (The two `retrain_global_{online,offline}` stubs that previously
// lived here were removed in v0.2.26 alongside the per-project reset
// stubs above. See the deprecation note higher in this file.)

// ─── Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::Db;

    fn open_db_with_projects(count: usize) -> (Db, Vec<String>) {
        let db = Db::open_in_memory().expect("in-memory db");
        let mut ids = Vec::new();
        for i in 0..count {
            let id = uuid::Uuid::new_v4().to_string();
            db.insert_project(
                &id,
                &format!("Project {}", i),
                &format!("/tmp/project-{}", i),
                crate::db::models::ProjectHost::Base,
                &format!("project-{}", i),
            )
            .expect("insert project");
            ids.push(id);
        }
        (db, ids)
    }

    /// Each flag setter writes a bool into `module_settings` under the
    /// canonical key. Verifies the three independent flags don't collide
    /// (each lives at its own row).
    #[test]
    fn flag_setters_write_independent_rows() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        set_bool_flag(&db, p, "rl_use_global", true).unwrap();
        set_bool_flag(&db, p, "rl_online_training_disabled", false).unwrap();
        set_bool_flag(&db, p, "rl_global_training_source_flag", true).unwrap();

        assert!(get_bool_flag(&db, p, "rl_use_global").unwrap());
        assert!(!get_bool_flag(&db, p, "rl_online_training_disabled").unwrap());
        assert!(get_bool_flag(&db, p, "rl_global_training_source_flag").unwrap());
    }

    /// Default for any flag with no row is `false` — non-existent rows
    /// must NOT make the renderer think the user opted in.
    #[test]
    fn flag_getter_defaults_to_false_for_missing_row() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        assert!(!get_bool_flag(&db, p, "rl_use_global").unwrap());
        assert!(!get_bool_flag(&db, p, "nonexistent_key").unwrap());
    }

    /// v0.2.40 H2: the three getter command bodies (`get_rl_use_global`,
    /// `get_rl_online_training_disabled`, `get_rl_global_training_source_flag`)
    /// round-trip through `module_settings` via the same `get_bool_flag` /
    /// `set_bool_flag` helpers the setters use. Verifies the wire shape
    /// the dashboard widget reads.
    ///
    /// Default for a missing row is `false` (not an error) — the widget
    /// renders a "off" status for never-configured projects rather than
    /// blanking out the panel.
    #[test]
    fn getters_round_trip_with_setters_and_default_to_false() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        // Missing rows ⇒ all three getters return false (matches the
        // widget's "fresh install, no opt-in" copy).
        assert!(!get_bool_flag(&db, p, "rl_use_global").unwrap());
        assert!(!get_bool_flag(&db, p, "rl_online_training_disabled").unwrap());
        assert!(!get_bool_flag(&db, p, "rl_global_training_source_flag").unwrap());

        // Set the three flags via the setter helper, then read back via
        // the same helper the new commands wrap. Independent rows ⇒
        // independent readback (no cross-talk).
        set_bool_flag(&db, p, "rl_use_global", true).unwrap();
        set_bool_flag(&db, p, "rl_global_training_source_flag", true).unwrap();
        // rl_online_training_disabled left false on purpose.

        assert!(get_bool_flag(&db, p, "rl_use_global").unwrap());
        assert!(!get_bool_flag(&db, p, "rl_online_training_disabled").unwrap());
        assert!(get_bool_flag(&db, p, "rl_global_training_source_flag").unwrap());
    }

    /// `list_rl_global_training_source_projects` returns only projects
    /// whose flag is true. Stable order (DB's `list_projects` is sorted
    /// by name).
    #[test]
    fn list_global_training_source_filters_by_flag() {
        let (db, ids) = open_db_with_projects(3);
        // Project 0 + 2 opted in, project 1 didn't.
        set_bool_flag(&db, &ids[0], "rl_global_training_source_flag", true).unwrap();
        set_bool_flag(&db, &ids[2], "rl_global_training_source_flag", true).unwrap();

        // Direct body call (avoiding Tauri State wrapping in test).
        let projects = db.list_projects().unwrap();
        let mut out = Vec::new();
        for project in projects {
            let flag = get_bool_flag(&db, &project.id, "rl_global_training_source_flag")
                .unwrap_or(false);
            if flag {
                out.push(project.id.clone());
            }
        }
        assert_eq!(out.len(), 2);
        assert!(out.contains(&ids[0]));
        assert!(out.contains(&ids[2]));
        assert!(!out.contains(&ids[1]));
    }

    // ─── v0.2.71 T-B-flags: dual-write + dual-log flags ──────────────────

    /// Both new flags default to `false` on a missing row, and they live
    /// under DIFFERENT module_ids (`orchestrator-core` vs `vct-rl-reranker`)
    /// so they never collide with each other or with the three RL flags
    /// above.
    #[test]
    fn dual_flags_default_false_and_use_distinct_module_ids() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        assert!(!get_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
        )
        .unwrap());
        assert!(!get_bool_flag_for_module(
            &db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY
        )
        .unwrap());
    }

    /// Per-project `true` overrides the default. Writing one flag does NOT
    /// flip the other (independent rows under distinct module_ids).
    #[test]
    fn dual_flags_set_true_independently() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        set_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY, true,
        )
        .unwrap();
        assert!(get_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
        )
        .unwrap());
        // dual_rl_log untouched.
        assert!(!get_bool_flag_for_module(&db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY).unwrap());
    }

    // F5 (v0.2.72): the cascade + re-projection now live in the
    // `_with_db` free functions (`set_dual_rl_log_enabled_with_db` /
    // `set_dual_embedding_write_all_slots_with_db`) that the `#[command]`
    // wrappers delegate to — the tests below exercise the REAL logic,
    // replacing the pre-F5 "MUST stay in lock-step" mirror helpers.

    fn apply_set_dual_rl_log(db: &Db, project_id: &str, value: bool) -> Result<(), String> {
        set_dual_rl_log_enabled_with_db(db, project_id, value).map(|_| ())
    }

    fn apply_set_dual_write(db: &Db, project_id: &str, value: bool) -> Result<(), String> {
        set_dual_embedding_write_all_slots_with_db(db, project_id, value).map(|_| ())
    }

    /// F5 (v0.2.72): both dual-flag setters re-project the project's env
    /// after the DB write. Proof of invocation: `reproject_env_soft` runs
    /// `populate()` against THIS db, so a seeded `kg_collection_access`
    /// row must surface in the returned `kg_access_list` — a value only
    /// the refresh path computes. (The Python subprocess leg soft-fails
    /// into `warnings` in unit-test environments; that's the designed
    /// never-roll-back-the-write behaviour.)
    #[test]
    fn dual_flag_setters_reproject_env_after_write() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        db.kg_set_access(p, "PeerProj_KnowledgeGraph", "read").unwrap();

        let r_write = set_dual_embedding_write_all_slots_with_db(&db, p, true)
            .expect("dual-write setter must succeed");
        assert_eq!(
            r_write.kg_access_list,
            vec!["PeerProj".to_string()],
            "dual-write setter must have run the env re-projection (populate)",
        );

        let r_log = set_dual_rl_log_enabled_with_db(&db, p, true)
            .expect("dual-log setter must succeed");
        assert_eq!(
            r_log.kg_access_list,
            vec!["PeerProj".to_string()],
            "dual-log setter must have run the env re-projection (populate)",
        );
    }

    /// Dependency invariant: enabling `dual_rl_log_enabled` force-enables
    /// the prerequisite `dual_embedding_write_all_slots`. The setter must
    /// never leave the DB in the incoherent (log=true, write=false) state.
    #[test]
    fn dual_rl_log_on_forces_dual_write_on() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        // Sanity: both off to start.
        assert!(!get_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
        )
        .unwrap());

        apply_set_dual_rl_log(&db, p, true).unwrap();

        assert!(
            get_bool_flag_for_module(&db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY).unwrap(),
            "dual_rl_log must be true after enabling",
        );
        assert!(
            get_bool_flag_for_module(
                &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
            )
            .unwrap(),
            "enabling dual_rl_log must force-enable dual_embedding_write_all_slots",
        );
    }

    /// Cascade the other way: disabling the prerequisite
    /// `dual_embedding_write_all_slots` while the dependent is ON must also
    /// disable the dependent, so the DB never holds (log=true, write=false).
    #[test]
    fn dual_write_off_cascades_dual_log_off() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        // Seed the coherent (both-on) starting point.
        apply_set_dual_rl_log(&db, p, true).unwrap();
        assert!(get_bool_flag_for_module(&db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY).unwrap());

        // Now turn the prerequisite OFF — the dependent must cascade off.
        apply_set_dual_write(&db, p, false).unwrap();

        assert!(
            !get_bool_flag_for_module(
                &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
            )
            .unwrap(),
            "dual_embedding_write_all_slots must be false after disable",
        );
        assert!(
            !get_bool_flag_for_module(&db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY).unwrap(),
            "disabling the prerequisite must cascade the dependent off",
        );
    }

    // ─── v0.2.88 (DEFECT 5): arctic-secondary dual-write flag ────────────

    /// Default OFF on a missing row (opt-in), same as the two siblings.
    #[test]
    fn arctic_secondary_defaults_false() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        assert!(!get_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY
        )
        .unwrap());
    }

    /// The arctic-secondary flag is INDEPENDENT: setting it does not touch the
    /// other two dual flags, and vice versa (no cascade in either direction).
    #[test]
    fn arctic_secondary_is_independent_of_the_other_two() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];

        // Turn arctic-secondary ON — the other two must stay OFF.
        set_dual_embedding_arctic_secondary_with_db(&db, p, true).unwrap();
        assert!(get_bool_flag_for_module(
            &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY
        )
        .unwrap());
        assert!(
            !get_bool_flag_for_module(
                &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_WRITE_ALL_SLOTS_KEY
            )
            .unwrap(),
            "arctic-secondary must NOT force-enable write_all_slots",
        );
        assert!(
            !get_bool_flag_for_module(&db, p, MODULE_ID, DUAL_RL_LOG_ENABLED_KEY).unwrap(),
            "arctic-secondary must NOT touch dual_rl_log",
        );

        // Enabling write_all_slots must NOT flip arctic-secondary off.
        set_dual_embedding_write_all_slots_with_db(&db, p, true).unwrap();
        assert!(
            get_bool_flag_for_module(
                &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY
            )
            .unwrap(),
            "toggling write_all_slots must leave arctic-secondary untouched",
        );

        // Disabling write_all_slots (which cascades dual_rl_log off) must ALSO
        // leave arctic-secondary untouched (it's not part of that cascade).
        set_dual_embedding_write_all_slots_with_db(&db, p, false).unwrap();
        assert!(
            get_bool_flag_for_module(
                &db, p, ORCHESTRATOR_CORE_MODULE_ID, DUAL_EMBEDDING_ARCTIC_SECONDARY_KEY
            )
            .unwrap(),
            "the write_all_slots→dual_rl_log cascade must NOT reach arctic-secondary",
        );
    }

    /// Setter re-projects env after the write (proof: seeded kg-access surfaces).
    #[test]
    fn arctic_secondary_setter_reprojects_env() {
        let (db, ids) = open_db_with_projects(1);
        let p = &ids[0];
        db.kg_set_access(p, "PeerProj_KnowledgeGraph", "read").unwrap();
        let r = set_dual_embedding_arctic_secondary_with_db(&db, p, true)
            .expect("arctic-secondary setter must succeed");
        assert_eq!(
            r.kg_access_list,
            vec!["PeerProj".to_string()],
            "arctic-secondary setter must have run the env re-projection (populate)",
        );
    }
}
