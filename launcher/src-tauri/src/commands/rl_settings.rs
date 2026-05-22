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
        SelectOption {
            value: p.value,
            label: p.label,
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
}
